r"""Shared guard for every Gulf tool (plan section 3.1): a per-site daily request budget, wall detection, the park ladder, the
machine check before a Gulf browser starts, one Gulf browser at a time, and one toast per incident per day.

    python portal_kit\site_guard.py status            ledger, park markers, machine check, browser slot, today's toasts (read-only)
    python portal_kit\site_guard.py unpark <site>     the USER's command: lift a park or hold; one gentle look is then due

Nothing here opens a browser or sends a request to a site. The state is the same set of files in job-tracker\state whichever
checkout runs this code:
  site_budget_<site>_<date>.json     the day's ledger. One unit is one navigation or one in-page API call made by our code
                                     (the page's own sub-requests are not counted).
  NAUKRIGULF_THROTTLED, _CHALLENGE   the park markers {until, since, what, step}; a hold is {"until": null, "hold": "user"}.
  gulf_browser.lock                  one Gulf browser at a time.
  gulf_toasts.json                   one toast per incident key per day; the format linkedin_keepalive.ps1 defined.
  site_guard.lock                    serialises the writes of Python callers of this module. The PowerShell keepalives never take
                                     it, although they write the same toast registry, so their writes and ours can interleave.
Callers exit 47 when machine_ok() gives a reason (recording its kind), and 48 on a wall. Standard library only (ctypes for the
Windows calls), Python 3.11: no other module is imported, so the PATH python runs status too.

HAND-OFF LOCK (READERS_FIXED is False). park() and unpark() refuse to run until it is True; the CLI unpark prints the refusal and
exits 2. The reason: the readers of the park markers do not honour a hold yet, so a hold written now would not stop them.
  - naukrigulf_apply.check_markers (l.226-235) ignores CHALLENGE, and deletes THROTTLED 30 min after it was written even when
    `until` is null (a hold);
  - naukrigulf_gentle_check.py keeps its own counter (`blocks` in its result file) and returns exit 3 while parked, where the
    exit table of plan section 3.6 says 47.
Both must be fixed in Phase 2 step 3 before the flag is flipped. status and parked() work either way.

STILL WRONG IN THE OLD READERS (also Phase 2 step 3, before READERS_FIXED is set to True; line numbers checked 2026-10-14):
  - naukrigulf_gentle_check.py:80 returns 47 for "still blocked"; plan section 3.6 says 48 (naukrigulf_apply.py defines
    EXIT_THROTTLED = 47 and no EXIT_CHALLENGE; ats_apply.py has EXIT_CHALLENGE = 48);
  - naukrigulf_gentle_check.py:76 takes LADDER_HOURS[min(blocks, 2)], so the third block gets 48 h and a hold is never written;
  - its own park() (:44-48) writes neither a hold nor a step: the markers it writes carry until, what and status or since only.

LEDGER REPAIR. A day's ledger that cannot be read, or whose `used` or `held` is not an object of whole numbers, blocks that site
for that day only (BudgetSpent; status shows "ledger corrupt"). Repair it by hand, keeping the counts: make `used` and `held`
objects of whole numbers. Do not delete the file: that resets the day's count to zero. The next day starts a new ledger.
"""
import argparse
import ctypes
import json
import msvcrt
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

# ------------------------------------------------------------------ settings (starting values, decision 16; never narrowed silently)
GITHUB = Path(os.environ.get("JOB_TRACKER_HOME") or Path.home() / "GitHub")
JT = Path(os.environ.get("JOB_DASHBOARD_JT") or GITHUB / "job-tracker")
STATE, LOGS = JT / "state", JT / "logs"

# Units a day per site and tool. A site or tool not listed has 0 units: no automated request without a budget.
BUDGETS = {"naukrigulf": {"applier": 31, "refresher": 5, "gentle": 4}}          # 40 a day
# Park markers per site (THROTTLED, CHALLENGE) and the ladder: 12 h, 24 h, 48 h, then a hold that only the user lifts.
MARKERS = {"naukrigulf": ("NAUKRIGULF_THROTTLED", "NAUKRIGULF_CHALLENGE")}
LADDER_HOURS = (12, 24, 48)
# The time a new Gulf browser may start, per tool; "*" for every tool (no new Gulf browser between 00:00 and 10:00).
WINDOWS = {"*": (dtime(10, 0), dtime.max)}
PAUSE_MARKER = LOGS / "APPLIERS_PAUSED_UNTIL.txt"   # appliers_pause.ps1; the VBS launchers' gate
INSTAHYRE_STATE = LOGS / "instahyre_apply.state.json"
INSTAHYRE_MARGIN_MIN = 45       # Instahyre's next cycle must be more than this far away
INSTAHYRE_SILENT_MIN = 30       # dashboard\collect.py: a NextCheck more than 30 min past means its supervisor is gone
CYCLE_STUCK_MIN = 190           # FALLBACK only, when the state declares no NextCheck: a cycle older than two 90-min attempts plus slack is stuck
CYCLE_START_SLACK_S = 60        # a supervisor that started this long after its cycle began is a reused PID, not that supervisor
SUPERVISOR_IMAGES = ("powershell.exe", "pwsh.exe")    # instahyre_apply.cmd starts the keepalive under Windows PowerShell
RENDER_EXES = ("sd-cli.exe", "sd.exe")     # the facecard overnight render (stable-diffusion.cpp, local_gen.py)
MODEL_EXE = "llama-server.exe"             # the llama router; a model is held by a child llama-server
MODEL_MB = 1024                            # a llama-server this big holds a model even without a router parent
RAM_WAIT_MB, RAM_WAIT_MIN, RAM_FLOOR_MB = 1024, 10, 1500
SLOT_LOCK = STATE / "gulf_browser.lock"
TOASTS = STATE / "gulf_toasts.json"
GUARD_LOCK = STATE / "site_guard.lock"
# The hand-off lock (module docstring): park() and unpark() refuse to run until this is True. Set it only after Phase 2 step 3 has
# made the marker readers honour a hold.
READERS_FIXED = False
HANDOFF_LOCKED = ("park and unpark are locked (READERS_FIXED is False): the park-marker readers, naukrigulf_apply.check_markers and "
                  "naukrigulf_gentle_check.py, do not honour a hold yet. Nothing was written.")


class BudgetSpent(RuntimeError):
    """This tool's share of the site's daily requests is used up (or none is set). Stop before the request."""


class SlotBusy(RuntimeError):
    """Another Gulf browser is open. The caller exits 47 (kind busy)."""


# ------------------------------------------------------------------ files and locks
_O_BINARY = getattr(os, "O_BINARY", 0)
_LOCK_AT = 4096           # the locked byte lies past any content, so reading a lock file never touches it


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _read_json(path: Path):
    """The parsed file, or None when it is missing, unreadable or not JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, obj, indent: int = 1) -> None:
    """A temporary file next to it, then os.replace: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    data = json.dumps(obj, indent=indent, ensure_ascii=False)
    err = None
    for _ in range(10):
        try:
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError as e:            # a reader holding the file without delete sharing: try again shortly
            err = e
            time.sleep(0.2)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise err


def _try_lock(fd: int) -> bool:
    os.lseek(fd, _LOCK_AT, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    os.lseek(fd, _LOCK_AT, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def _guard_lock(timeout: float = 30):
    STATE.mkdir(parents=True, exist_ok=True)
    fd = os.open(GUARD_LOCK, os.O_RDWR | os.O_CREAT | _O_BINARY)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{GUARD_LOCK} stayed locked for {timeout:.0f} s")
            time.sleep(0.1)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


# ------------------------------------------------------------------ request budget
def ledger_path(site: str, day: date | None = None) -> Path:
    return STATE / f"site_budget_{site}_{(day or date.today()).isoformat()}.json"


def _ledger_problem(led) -> str:
    """'' when a day's ledger has the shape this module writes, else what is wrong with it (the repair is in the module docstring)."""
    if led is None:
        return "the file cannot be read as JSON"
    if not isinstance(led, dict):
        return "the file is not a JSON object"
    for key in ("used", "held"):
        part = led.get(key)
        if part is None:
            continue
        if not isinstance(part, dict):
            return f"`{key}` is not an object"
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in part.values()):
            return f"`{key}` has a value that is not a whole number"
    if led.get("entries") is not None and not isinstance(led.get("entries"), list):
        return "`entries` is not a list"
    return ""


def ledger(site: str, day: date | None = None) -> dict:
    """The day's ledger, read-only: {site, date, shares, budget, used, held, entries}. A ledger that exists but is corrupt raises
    BudgetSpent for this site and this day only: an unknown spend is never taken as zero, and status shows "ledger corrupt"."""
    day = day or date.today()
    shares = dict(BUDGETS.get(site) or {})
    p = ledger_path(site, day)
    led = _read_json(p) if p.exists() else {}
    why = _ledger_problem(led)
    if why:
        raise BudgetSpent(f"{site}: ledger corrupt ({p.name}: {why}); no request is allowed on this day until the file is repaired")
    return {"site": site, "date": day.isoformat(), "shares": shares, "budget": sum(shares.values()),
            "used": {**{t: 0 for t in shares}, **(led.get("used") or {})},
            "held": {**{t: 0 for t in shares}, **(led.get("held") or {})},
            "entries": list(led.get("entries") or [])}


def _spend(site: str, tool: str, n: int, what: str, kind: str) -> int:
    if int(n) < 1:
        raise ValueError(f"{kind} needs at least 1 unit, not {n}")
    n = int(n)
    with _guard_lock():
        led = ledger(site)
        share = int(led["shares"].get(tool, 0))
        used, held = int(led["used"].get(tool, 0)), int(led["held"].get(tool, 0))
        from_held = min(held, n) if kind == "debit" else 0
        need, free = n - from_held, share - used - held
        if need > free:
            raise BudgetSpent(f"{site}: no budget for {n} more {tool} unit(s)" + (f" ({what})" if what else "") +
                              (f"; today {used} used and {held} reserved of {share}" if share else "; none is set for this tool"))
        if kind == "debit":
            led["held"][tool], led["used"][tool] = held - from_held, used + n
        else:
            led["held"][tool] = held + n
        led["entries"].append([_iso(datetime.now()), tool, kind, n, str(what)[:120]])
        led["updated"] = _iso(datetime.now())
        _write_json_atomic(ledger_path(site), led)
        return share - led["used"][tool] - led["held"][tool]


def debit(site: str, tool: str, n: int = 1, what: str = "") -> int:
    """Charge n units to this tool's share before the request is made; units reserved earlier are used first. Raises BudgetSpent
    (and charges nothing) when the share cannot cover it. Returns the units still free in the share."""
    return _spend(site, tool, n, what, "debit")


def reserve(site: str, tool: str, n: int, what: str = "") -> int:
    """Set n units aside now for requests that must not be refused later (a click's own requests and its read-back), so that
    BudgetSpent can never fall between a click and its verification. Later debits draw on them first. A reservation not
    spent stays held for the rest of the day. Raises BudgetSpent when the share cannot cover it."""
    return _spend(site, tool, n, what, "reserve")


# ------------------------------------------------------------------ walls
# WALL_TEXT and NET_BLOCK are copied unchanged from profiles\tools\gulf_helpers\gulf_refresh.py (git-ignored).
WALL_TEXT = re.compile(r"Just a moment|Verify you are human|Checking if the site connection is secure|Additional Verification Required|"
                       r"needs to review the security of your connection|Attention Required|Access denied|Sorry, you have been blocked|"
                       r"You have been blocked|Request blocked|Security Check|Are you a robot|unusual activity|"
                       r"Please enable JS and disable any ad blocker|Verification Required|Access is temporarily restricted|"
                       r"slide right to (complete|secure)", re.I)
NET_BLOCK = re.compile(r"HTTP2|PROTOCOL_ERROR|CONNECTION_RESET|CONNECTION_CLOSED|EMPTY_RESPONSE|BLOCKED_BY_RESPONSE|Failed to fetch", re.I)
# Local failures, never walls: a CDP or socket timeout (a frozen tab gives these), a lost DevTools connection, a crashed tab,
# and the errors of a machine that is offline or changing networks.
LOCAL_ERROR = re.compile(r"no reply within|did not load within|timed? ?out|websocket|target closed|target crashed|"
                         r"ERR_INTERNET_DISCONNECTED|ERR_NAME_NOT_RESOLVED|ERR_NETWORK_CHANGED|ERR_ADDRESS_UNREACHABLE|"
                         r"ERR_NETWORK_IO_SUSPENDED|ERR_PROXY_CONNECTION_FAILED", re.I)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    u = uuid.UUID(s)
    return _GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


def _nlm_online():
    """INetworkListManager.IsConnectedToInternet: Windows' own verdict (the taskbar's 'Internet access'). Nothing is sent."""
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes, ole32.CoInitializeEx.restype = [ctypes.c_void_p, wintypes.DWORD], ctypes.c_long
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_GUID),
                                       ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    inited = ole32.CoInitializeEx(None, 0) in (0, 1)      # COINIT_MULTITHREADED; a thread already in another mode keeps it
    try:
        clsid, iid = _guid("DCB00C01-570F-4A9B-8D69-199FDBA5723B"), _guid("DCB00000-570F-4A9B-8D69-199FDBA5723B")
        p = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, 0x17, ctypes.byref(iid), ctypes.byref(p)) != 0 or not p.value:
            return None
        vtbl = ctypes.cast(p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        try:            # vtable: IUnknown 0-2, IDispatch 3-6, then GetNetworks .. GetNetworkConnection 7-10, IsConnectedToInternet 11
            getter = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_short))(vtbl[11])
            v = ctypes.c_short(0)
            return None if getter(p, ctypes.byref(v)) != 0 else v.value != 0
        finally:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(p)
    finally:
        if inited:
            ole32.CoUninitialize()


def _wininet_online():
    flags = wintypes.DWORD()
    return bool(ctypes.WinDLL("wininet").InternetGetConnectedState(ctypes.byref(flags), 0))


def machine_online():
    """True or False as Windows reports internet access, asked locally (Network List Manager, else WinINet); None if unknown."""
    for ask in (_nlm_online, _wininet_online):
        try:
            v = ask()
        except Exception:      # noqa: BLE001 - an API that cannot be asked is "unknown", never a crash
            v = None
        if v is not None:
            return v
    return None


def is_wall(error=None, page_text: str = "", title: str = "", challenge_frame: bool = False) -> bool:
    """True only for an answer from the site: a challenge frame the caller found on the page (challenge_frame: the cf, dd or rc
    kinds of gulf_refresh.py, a Cloudflare, DataDome or captcha iframe); challenge text in the title or in a short page body (a full
    page that merely mentions "security check" is not a wall); or a connection the site cut ("Failed to fetch",
    ERR_HTTP2_PROTOCOL_ERROR, a connection reset) while Windows reports the machine online. An unknown online state is never a
    wall. A CDP timeout, a frozen tab or an offline machine is a local error (exit 1 or 47), never a wall. HTTP 403 and 429 are
    throttles, which the caller handles itself."""
    if challenge_frame:
        return True
    if WALL_TEXT.search(title or "") or (page_text and len(page_text) < 1500 and WALL_TEXT.search(page_text)):
        return True
    if error is None or error == "":
        return False
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    if isinstance(error, TimeoutError) or not NET_BLOCK.search(text) or LOCAL_ERROR.search(text):
        return False
    return machine_online() is True


# ------------------------------------------------------------------ park ladder
def parked(site: str) -> dict:
    """The site's park state, the same for every tool: {in_force, until, look_due, hold, step, markers}.
    - hold ({"until": null, "hold": "user"}): in force until the user runs unpark. No time rule applies to a hold: the 30-minute
      fallback below is never applied to one.
    - a marker whose until is ahead: in force until then. A CHALLENGE that cannot be read stays in force until unpark (as the
      board shows it). A THROTTLED whose until cannot be read is taken to end 30 min after the file was written: a fallback for an
      unreadable THROTTLED only, never for a hold (the module docstring says why naukrigulf_apply's own rule is not the model).
    - a CHALLENGE that has run out: look_due, i.e. one gentle look comes before anything else touches the site. Only the gentle
      check removes the markers, when the site answers again. A THROTTLED alone (HTTP 403/429) simply runs out.
    step is the ladder rung of the last park (a CHALLENGE written before the ladder existed counts as 1)."""
    out = {"site": site, "in_force": False, "until": None, "look_due": False, "hold": False, "step": 0, "markers": {}}
    names = MARKERS.get(site)
    if not names:
        return out
    now, ends = datetime.now(), []
    for kind, name in zip(("throttled", "challenge"), names):
        p = STATE / name
        if not p.exists():
            continue
        d = _read_json(p)
        info: dict = {}
        if isinstance(d, dict) and d.get("hold"):
            info["state"] = "hold"
            out["hold"] = out["in_force"] = True
        else:
            until = None
            if isinstance(d, dict):
                try:
                    until = datetime.fromisoformat(str(d.get("until")))
                except ValueError:
                    until = None
            if until is None and kind == "throttled":
                until = datetime.fromtimestamp(p.stat().st_mtime) + timedelta(minutes=30)
                info["note"] = "until unreadable: the file's time + 30 min"
            if until is None:
                info["state"] = "unreadable: in force until unpark"
                out["in_force"] = True
            elif until > now:
                info.update(state="in force", until=_iso(until))
                out["in_force"] = True
                ends.append(until)
            else:
                info.update(state="ran out", until=_iso(until))
                out["look_due"] = out["look_due"] or kind == "challenge"
        if isinstance(d, dict):
            info.update(since=d.get("since"), what=re.sub(r"\s+", " ", str(d.get("what") or ""))[:160])
        if kind == "challenge":
            raw = d.get("step") if isinstance(d, dict) else None
            try:
                out["step"] = max(1, int(raw))
            except (TypeError, ValueError):
                out["step"] = 1
            info["step"] = raw if raw is not None else "not recorded (counted as 1)"
        out["markers"][name] = info
    out["until"] = None if out["hold"] else (_iso(max(ends)) if ends else None)
    return out


def _same_incident(st: dict) -> bool:
    """A CHALLENGE park still in force, held or unreadable: a new wall now is the same incident."""
    ch = st["markers"].get(MARKERS[st["site"]][1]) or {}
    return ch.get("state") in ("in force", "hold") or str(ch.get("state", "")).startswith("unreadable")


def _require_readers_fixed() -> None:
    """The hand-off lock: park and unpark raise until READERS_FIXED is True. Nothing is read or written before this check."""
    if not READERS_FIXED:
        raise RuntimeError(HANDOFF_LOCKED)


def park(site: str, why: str) -> dict:
    """A wall on this site: park it for every tool, one rung up the ladder from the last park (12 h, 24 h, 48 h, then a hold
    that only the user lifts). Writes both markers {until, since, what, step}. A wall reported while the CHALLENGE park is
    still in force or held is the same incident: nothing changes. Returns parked(site). Refused (RuntimeError) while READERS_FIXED
    is False."""
    _require_readers_fixed()
    names = MARKERS.get(site)
    if not names:
        raise ValueError(f"no park markers are set for {site!r}")
    with _guard_lock():
        cur = parked(site)
        if _same_incident(cur):
            return cur
        step, now = cur["step"] + 1, datetime.now()
        hold = step > len(LADDER_HOURS)
        until = None if hold else _iso(now + timedelta(hours=LADDER_HOURS[step - 1]))
        extra = {"hold": "user"} if hold else {}
        what = str(why)[:300]
        _write_json_atomic(STATE / names[0], {"until": until, "since": _iso(now), "what": what, "status": None, "step": step, **extra})
        _write_json_atomic(STATE / names[1], {"until": until, "since": _iso(now), "what": what, "step": step, **extra})
    return parked(site)


def unpark(site: str) -> dict:
    """The user's command (agents do not run it): lift a park or a hold. The markers stay, with until set to now and no hold,
    so the ladder keeps its rung and one gentle look is due before any tool goes back; the gentle check removes the markers
    when the site answers. Returns parked(site). Refused (RuntimeError) while READERS_FIXED is False."""
    _require_readers_fixed()
    names = MARKERS.get(site)
    if not names:
        raise ValueError(f"no park markers are set for {site!r}")
    with _guard_lock():
        now = _iso(datetime.now())
        for name in names:
            p = STATE / name
            if not p.exists():
                continue
            d = _read_json(p)
            d = dict(d) if isinstance(d, dict) else {}
            d.pop("hold", None)
            d.update(until=now, unparked=now)
            _write_json_atomic(p, d)
    return parked(site)


# ------------------------------------------------------------------ the machine check before a Gulf browser starts
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


_K32 = None


def _k32():
    """A private kernel32 handle, so these prototypes never change the ones other modules set on ctypes.windll."""
    global _K32
    if _K32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.argtypes, k.CreateToolhelp32Snapshot.restype = [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE
        k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        k.OpenProcess.argtypes, k.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        k.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GlobalMemoryStatusEx.argtypes, k.GlobalMemoryStatusEx.restype = [ctypes.POINTER(_MEMORYSTATUSEX)], wintypes.BOOL
        k.GetProcessTimes.argtypes, k.GetProcessTimes.restype = [wintypes.HANDLE] + [ctypes.POINTER(_FILETIME)] * 4, wintypes.BOOL
        _K32 = k
    return _K32


def _processes() -> list:
    """(pid, parent pid, exe name) of every running process, from a Toolhelp snapshot (read-only; no WMI)."""
    k = _k32()
    snap = k.CreateToolhelp32Snapshot(0x2, 0)                 # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        out, ok = [], k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
            ok = k.Process32NextW(snap, ctypes.byref(e))
        return out
    finally:
        k.CloseHandle(snap)


def _process_mb(pid: int):
    """The larger of a process's private bytes and working set, in MB; None when it cannot be read."""
    k = _k32()
    h = k.OpenProcess(0x1000 | 0x0010, False, pid) or k.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION | VM_READ
    if not h:
        return None
    try:
        c = _PROCESS_MEMORY_COUNTERS_EX()
        c.cb = ctypes.sizeof(c)
        if not k.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return None
        return int(max(c.PrivateUsage, c.WorkingSetSize) // (1024 * 1024))
    finally:
        k.CloseHandle(h)


# Each check returns (problem, info): problem is '' when it passes; info describes what was seen.
def _check_window(tool: str, now: datetime) -> tuple:
    start, end = WINDOWS.get(tool, WINDOWS["*"])
    if now.time() < start:
        return f"no new Gulf browser before {start:%H:%M} (now {now:%H:%M})", ""
    if now.time() > end:
        return f"no new {tool} browser after {end:%H:%M} (now {now:%H:%M})", ""
    return "", f"{start:%H:%M}-{end:%H:%M}, now {now:%H:%M}"


def _check_pause(now: datetime) -> tuple:
    """The VBS launchers' rule: the marker's first line, trimmed, read as a date; a time ahead means paused. A past, empty,
    garbled or missing marker blocks nothing, as the launchers do. A marker that exists but cannot be read blocks (the safe side)."""
    try:
        if not PAUSE_MARKER.exists():
            return "", "no pause marker"
        first = (PAUSE_MARKER.read_bytes().decode("latin-1").splitlines() or [""])[0].strip(" ")
    except OSError as e:
        return f"pause marker unreadable: blocking to be safe ({type(e).__name__})", ""
    try:
        until = datetime.fromisoformat(first)
    except ValueError:
        return "", f"pause marker holds no date ({first[:40]!r}): blocks nothing, like the launchers"
    if until > now:
        return f"the appliers are paused until {until:%Y-%m-%d %H:%M} (logs\\{PAUSE_MARKER.name})", ""
    return "", f"the last pause ended {until:%Y-%m-%d %H:%M}"


def _parse_dt(text) -> datetime | None:
    """An ISO time from the state file, or None (missing, blank or garbled)."""
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


def _process_start(pid: int) -> datetime | None:
    """When the process started, as a local naive datetime (the clock the state file uses), or None when it cannot be read. Read-only:
    OpenProcess with query-limited rights, then GetProcessTimes."""
    k = _k32()
    h = k.OpenProcess(0x1000, False, pid)                                  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        created, spare1, spare2, spare3 = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not k.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(spare1), ctypes.byref(spare2), ctypes.byref(spare3)):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime                 # 100-ns units since 1601-01-01 UTC
        return datetime.fromtimestamp(ticks / 10_000_000 - 11_644_473_600)           # Unix seconds, shown in local time
    except (OverflowError, OSError, ValueError):
        return None
    finally:
        k.CloseHandle(h)


def _supervisor_running(sup, cycle_start: datetime | None) -> bool:
    """True when the Instahyre supervisor (the state's Supervisor key, its $PID) is running: (a) the PID is in the process list,
    (b) its image is powershell.exe or pwsh.exe, (c) it started no later than the cycle start plus CYCLE_START_SLACK_S. A later
    start means the number was reused, so that process is not the supervisor. With no cycle start only (a) and (b) can be checked. A
    process whose start time cannot be read counts as RUNNING (the gate fails closed: it cannot be shown to be a reused number); an
    unreadable process list counts as not running here, and _check_heavy blocks Gulf starts on its own in that case."""
    if not isinstance(sup, int) or isinstance(sup, bool):
        return False
    try:
        exe = next((name for pid, _parent, name in _processes() if pid == sup), None)           # (a)
    except OSError:
        return False
    if exe is None or exe.lower() not in SUPERVISOR_IMAGES:                                      # (b)
        return False
    if cycle_start is None:
        return True
    started = _process_start(sup)                                                                # (c)
    return started is None or started <= cycle_start + timedelta(seconds=CYCLE_START_SLACK_S)


def _cycle_stuck(now: datetime, in_cycle: bool, start, nxt) -> str:
    """"" unless the Instahyre cycle is stuck, else a description of why. A cycle's declared deadline is NextCheck (one 90-min attempt
    per pending account, so a legitimate two-account cycle runs up to 180 min): InCycle more than INSTAHYRE_SILENT_MIN past that
    deadline is stuck. With no readable deadline, a cycle older than CYCLE_STUCK_MIN is."""
    if not in_cycle:
        return ""
    if nxt is not None:
        over = (now - nxt).total_seconds() / 60
        return f"{over:.0f} min past its declared deadline ({nxt:%H:%M}; limit {INSTAHYRE_SILENT_MIN})" if over > INSTAHYRE_SILENT_MIN else ""
    if start is not None and (now - start).total_seconds() / 60 > CYCLE_STUCK_MIN:
        return f"{(now - start).total_seconds() / 60:.0f} min with no declared deadline (limit {CYCLE_STUCK_MIN})"
    return ""


def _instahyre_stuck(now: datetime) -> str:
    """The stuck description for status (see _cycle_stuck), "" when the cycle is fine or the state cannot be read."""
    st = _read_json(INSTAHYRE_STATE)
    if not isinstance(st, dict):
        return ""
    in_cycle = bool(st.get("InCycle"))
    return _cycle_stuck(now, in_cycle, _parse_dt(st.get("LastCycle")) if in_cycle else None, _parse_dt(st.get("NextCheck")))


def _check_instahyre(now: datetime) -> tuple:
    """Instahyre's supervisor state (logs\\instahyre_apply.state.json, written by instahyre_apply_keepalive.ps1 at lines 159 and 202).
    LastCycle is the cycle start while InCycle is true, and the end time after it. NextCheck is the cycle's declared deadline while
    InCycle (line 160: one attempt timeout per pending account), and the next scheduled check after it. Plan 3.1: no Gulf browser
    beside an Instahyre cycle, and none unless its next check is more than INSTAHYRE_MARGIN_MIN away or its supervisor is
    verifiably gone. The gate FAILS CLOSED wherever it cannot judge. "running" = _supervisor_running (the PID is live, a PowerShell
    image, started no later than LastCycle + CYCLE_START_SLACK_S: the same test in a cycle and outside one).

      state file missing ........................ free (no supervisor has ever run)
      state file exists but does not parse ...... busy (written non-atomically: mid-write, or corrupt)
      InCycle, supervisor running ............... busy ALWAYS; a cycle more than INSTAHYRE_SILENT_MIN past its declared deadline
                                                  only adds a WARNING (here and in `status`): a stuck supervisor holds Gulf starts
                                                  back, it never releases them
      InCycle, not running, no start, no NextCheck busy (cannot be judged)
      InCycle, not running, NextCheck readable .. busy until NextCheck + INSTAHYRE_SILENT_MIN, then free (the supervisor is gone)
      InCycle, not running, only a start ........ busy until start + CYCLE_STUCK_MIN, then free
      idle, supervisor running, NextCheck unknown
        or more than INSTAHYRE_SILENT_MIN overdue  busy (its next cycle may start at any moment)
      idle, supervisor not running, no NextCheck
        or overdue beyond INSTAHYRE_SILENT_MIN .. free (the supervisor is gone, as the board reads it)
      idle, NextCheck <= INSTAHYRE_MARGIN_MIN ahead (or overdue by up to INSTAHYRE_SILENT_MIN): busy
      idle, NextCheck further ahead ............. free"""
    if not INSTAHYRE_STATE.exists():
        return "", "no Instahyre supervisor state"
    st = _read_json(INSTAHYRE_STATE)
    if not isinstance(st, dict):
        return ("Instahyre's state file exists but cannot be read (being written, or corrupt): treated as busy to be safe"), ""
    in_cycle = bool(st.get("InCycle"))
    last = _parse_dt(st.get("LastCycle"))        # the cycle start while InCycle; else the last cycle's end, written by a LIVE supervisor
    start = last if in_cycle else None
    nxt = _parse_dt(st.get("NextCheck"))
    silent = nxt is None or now > nxt + timedelta(minutes=INSTAHYRE_SILENT_MIN)
    # (c) is checked against LastCycle in BOTH states: a process that started after the last write is a reused number, not the
    # supervisor (a fresh supervisor reads "not running" for the seconds until its first write, which is accepted).
    running = _supervisor_running(st.get("Supervisor"), last)
    stuck = _cycle_stuck(now, in_cycle, start, nxt)
    note = f"; WARNING: InCycle {stuck}" if stuck else ""
    when = f"since {start:%H:%M}" if start is not None else "since an unreadable start"
    if in_cycle:
        if running:
            return f"Instahyre is in a cycle ({when}; its supervisor, pid {st.get('Supervisor')}, is running{note})", ""
        if nxt is not None:
            if not silent:
                return f"Instahyre is in a cycle ({when}; due by {nxt:%H:%M}; its supervisor is not running)", ""
            return "", (f"Instahyre's supervisor has been silent since its {nxt:%Y-%m-%d %H:%M} check (gone, as the board reads it); "
                        "not counted")
        if start is None:
            return "Instahyre's state says it is in a cycle but gives neither a start nor a deadline: treated as busy to be safe", ""
        age = (now - start).total_seconds() / 60
        if age <= CYCLE_STUCK_MIN:
            return (f"Instahyre is in a cycle ({when}; no deadline declared and its supervisor is not running): busy until "
                    f"{CYCLE_STUCK_MIN} min after the start"), ""
        return "", f"Instahyre's cycle began {age:.0f} min ago, declares no deadline and has no supervisor: gone; not counted"
    if running and silent:
        return "Instahyre's supervisor is running but its next check is unknown or overdue: treated as busy (a cycle may start any moment)", ""
    if nxt is None:
        return "", "Instahyre declares no next check: its supervisor is gone (the board's rule); not counted"
    if silent:
        return "", (f"Instahyre's supervisor has been silent since its {nxt:%Y-%m-%d %H:%M} check (gone, as the board reads it); "
                    "not counted")
    left = (nxt - now).total_seconds() / 60
    if left <= INSTAHYRE_MARGIN_MIN:
        return f"Instahyre's next cycle is due at {nxt:%H:%M} (in {max(0.0, left):.0f} min; needs more than {INSTAHYRE_MARGIN_MIN})", ""
    return "", f"Instahyre's next cycle at {nxt:%Y-%m-%d %H:%M} (in {left / 60:.1f} h)"


def _check_heavy() -> tuple:
    try:
        procs = _processes()
    except OSError as e:
        return f"cannot list the running processes ({e})", ""
    names = {pid: exe.lower() for pid, _parent, exe in procs}
    renders = sorted(pid for pid, _parent, exe in procs if exe.lower() in RENDER_EXES)
    if renders:
        return "the facecard overnight render is running (" + ", ".join(f"{names[p]} pid {p}" for p in renders) + ")", ""
    models, idle = [], []
    for pid, parent, exe in procs:
        if exe.lower() != MODEL_EXE:
            continue
        mb = _process_mb(pid)
        line = f"pid {pid}, {'?' if mb is None else mb} MB"
        (models if names.get(parent) == MODEL_EXE or (mb is not None and mb >= MODEL_MB) else idle).append(line)
    if models:
        return f"llama-server holds a model ({'; '.join(models)})", ""
    return "", "no facecard render; " + (f"llama-server router idle ({'; '.join(idle)})" if idle else "no llama-server")


def free_ram_mb() -> int | None:
    """Available physical memory in MB (Task Manager's 'Available'), or None when it cannot be read (GlobalMemoryStatusEx)."""
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if not _k32().GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return int(st.ullAvailPhys // (1024 * 1024))
    except Exception:          # noqa: BLE001 - an unreadable figure is None, which the floor treats as no start
        return None


def wait_for_ram(min_mb: int = RAM_WAIT_MB, max_min: float = RAM_WAIT_MIN, poll_sec: float = 30) -> str | None:
    """A bounded wait for free RAM before a Gulf browser starts, with the rule of edge_cdp.wait_for_ram (which this module no longer
    imports, because that module needs websocket): None when there is nothing to wait for (enough RAM, or no reading); else a line
    for the log once the wait ends. It polls every poll_sec seconds for up to max_min minutes; the 1.5 GB floor is checked after."""
    free = free_ram_mb()
    if free is None or free >= min_mb:
        return None
    start = time.monotonic()
    deadline = start + max_min * 60
    while time.monotonic() < deadline:
        time.sleep(poll_sec)
        free = free_ram_mb()
        if free is None or free >= min_mb:
            return f"waited {time.monotonic() - start:.0f}s for free RAM (now {free} MB)"
    return f"free RAM still {free} MB after {max_min:g} min; the floor decides"


def _check_ram(wait_min: float) -> tuple:
    """(problem, info, waited): a bounded wait of wait_min minutes for RAM_WAIT_MB (wait_for_ram), then the 1.5 GB floor."""
    try:
        waited = wait_for_ram(RAM_WAIT_MB, wait_min) if wait_min > 0 else None
        free = free_ram_mb()
    except Exception as e:     # noqa: BLE001 - no reading means no start
        return f"cannot read free RAM ({type(e).__name__}: {str(e)[:80]})", "", False
    tail = f"; {waited}" if waited else ""
    if free is None:
        return "free RAM is unreadable" + tail, "", bool(waited)
    if free < RAM_FLOOR_MB:
        return f"{free} MB free, {RAM_FLOOR_MB} MB needed before a Gulf browser starts{tail}", "", bool(waited)
    return "", f"{free} MB free (floor {RAM_FLOOR_MB} MB){tail}", bool(waited)


def machine_ok(tool: str, wait_min: float = RAM_WAIT_MIN) -> str:
    """'' when a new Gulf browser may start now, else '<kind>: <why>' with kind window, paused, busy or ram. The caller exits 47
    and records the kind. In order: the tool's time window, the appliers' pause gate, Instahyre's cycle (not running, and its
    next cycle more than 45 min away), no facecard render and no local model loaded, then the RAM floor: up to wait_min
    minutes for 1 GB free (wait_for_ram), then 1.5 GB. After a RAM wait, the other checks are made once more.
    NOT IMPLEMENTED: the keepalive loop-gap check of plan section 3.1 (no applier keepalive log shows a loop gap over 4 min in the
    last 10 min). Until it exists the RAM floor is the only guard against India-chain stalls; build it before the first Gulf
    browser start."""
    for attempt in (1, 2):
        now = datetime.now()
        for kind, check in (("window", lambda: _check_window(tool, now)), ("paused", lambda: _check_pause(now)),
                            ("busy", lambda: _check_instahyre(now)), ("busy", _check_heavy)):
            problem = check()[0]
            if problem:
                return f"{kind}: {problem}"
        if attempt == 2:
            return ""
        problem, _info, waited = _check_ram(wait_min)
        if problem:
            return f"ram: {problem}"
        if not waited:
            return ""
    return ""


# ------------------------------------------------------------------ one Gulf browser at a time
def _slot_write(fd: int, info: dict) -> None:
    data = json.dumps(info, ensure_ascii=False).encode("utf-8")[:1000]
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data.ljust(1024))


def _slot_info() -> dict:
    try:
        with open(SLOT_LOCK, "rb") as fh:
            return json.loads(fh.read(1024).decode("utf-8", "replace").strip() or "{}")
    except (OSError, ValueError):
        return {}


@contextmanager
def browser_slot(name: str, wait_min: float = 0):
    """Hold this around a Gulf browser's whole life (start to Browser.close): one Gulf browser at a time across every tool.
    Waits up to wait_min minutes for another tool's browser to finish, else raises SlotBusy. The lock belongs to the operating
    system, so a holder that crashes frees it at once."""
    STATE.mkdir(parents=True, exist_ok=True)
    fd = os.open(SLOT_LOCK, os.O_RDWR | os.O_CREAT | _O_BINARY)
    try:
        deadline = time.monotonic() + wait_min * 60
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                h = _slot_info()
                raise SlotBusy(f"another Gulf browser is open ({h.get('holder', '?')}, pid {h.get('pid', '?')}, since "
                               f"{h.get('since', '?')}); waited {wait_min:g} min")
            time.sleep(10)
        _slot_write(fd, {"holder": str(name)[:80], "pid": os.getpid(), "since": _iso(datetime.now())})
        try:
            yield
        finally:
            _slot_write(fd, {"free_since": _iso(datetime.now()), "last_holder": str(name)[:80]})
            _unlock(fd)
    finally:
        os.close(fd)


def slot_state() -> dict:
    """Read-only: whether the slot is held (a lock probe that holds nothing), and what its file says. No file means never used."""
    if not SLOT_LOCK.exists():
        return {"held": False, "never_used": True}
    fd = os.open(SLOT_LOCK, os.O_RDONLY | _O_BINARY)
    try:
        held = not _try_lock(fd)
        if not held:
            _unlock(fd)
    finally:
        os.close(fd)
    return {"held": held, **_slot_info()}


# ------------------------------------------------------------------ one toast per incident per day
# The same toast as Show-Toast in the keepalives (instahyre_apply_keepalive.ps1) and ats_apply.toast: hidden PowerShell, no window.
_TOAST_PS = r"""
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
  $t = [System.Security.SecurityElement]::Escape($env:SITE_GUARD_TOAST_TITLE)
  $b = [System.Security.SecurityElement]::Escape($env:SITE_GUARD_TOAST_BODY)
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
  $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
} catch { exit 1 }
"""
_TOAST_KEY = re.compile(r"\d{4}-\d{2}-\d{2}\|.+")
_SHOWN: set = set()      # this process's own record, so an unreadable registry never repeats a toast


def read_toasts():
    """The registry: {} when absent or blank; None when it cannot be read or is not the LinkedIn keepalive's format (one object
    keyed "<yyyy-MM-dd>|<key>" whose values are objects {at, by, title}). A None registry is never overwritten."""
    if not TOASTS.exists():
        return {}
    raw = None
    for _ in range(3):
        try:
            raw = TOASTS.read_text(encoding="utf-8-sig")
            break
        except OSError:
            time.sleep(0.2)
    if raw is None:
        return None
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not all(_TOAST_KEY.match(k) and isinstance(v, dict) for k, v in obj.items()):
        return None
    return obj


def _toast(title: str, body: str) -> bool:
    try:
        env = {**os.environ, "SITE_GUARD_TOAST_TITLE": title, "SITE_GUARD_TOAST_BODY": body}
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _TOAST_PS],
                           env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def notify_once(key: str, title: str, body: str, *, day: str | None = None, by: str = "site_guard") -> bool:
    """One toast per incident key per day across every tool (the PowerShell keepalives write the same registry). The registry
    records a toast only when Windows took it; one that failed is not retried by this process. An alarm is never silenced: when the
    registry cannot be read or is not the agreed object, the toast is still shown, at most once per process per key, with a WARN on
    stderr, and nothing is saved (a registry that is not the agreed object is never overwritten). True when this call showed it."""
    ident = f"{day or date.today().isoformat()}|{key}"
    if ident in _SHOWN:
        return False
    reg = read_toasts()
    if reg is not None and ident in reg:
        _SHOWN.add(ident)
        return False
    if reg is None:
        print(f"WARN site_guard: {TOASTS.name} cannot be read as the toast registry; showing {key!r} once in this process "
              "and not saving it", file=sys.stderr)
    shown = _toast(title, body)
    _SHOWN.add(ident)
    if not shown or reg is None:
        return shown
    try:
        with _guard_lock(timeout=10):
            reg = read_toasts()             # read again: another tool may have written while the toast was shown
            if reg is not None and ident not in reg:
                reg[ident] = {"at": _iso(datetime.now()), "by": by, "title": title}
                _write_json_atomic(TOASTS, reg, indent=2)
    except (OSError, TimeoutError):
        pass
    return True


# ------------------------------------------------------------------ status (read-only)
def status() -> int:
    now = datetime.now()
    print(f"site_guard status, {now:%Y-%m-%d %H:%M} (read-only; state in {STATE})")
    print("READERS_FIXED: " + (f"{READERS_FIXED}; park and unpark are open" if READERS_FIXED else
                               f"{READERS_FIXED}; park and unpark are LOCKED (the old marker readers do not honour a hold yet; "
                               "see the module docstring)"))
    print("Request ledger, today:")
    for site, shares in BUDGETS.items():
        p = ledger_path(site)
        head = f"  {site}: shares " + ", ".join(f"{t} {n}" for t, n in shares.items()) + f" = {sum(shares.values())} units a day"
        try:
            led = ledger(site)
        except BudgetSpent as e:
            print(f"{head}; {e}")
            continue
        if not p.exists():
            print(f"{head}; nothing spent today ({p.name} not written yet)")
            continue
        use = ", ".join(f"{t} {int(led['used'].get(t, 0))}/{n}" + (f" (+{int(led['held'][t])} reserved)" if led["held"].get(t) else "")
                        for t, n in shares.items())
        last = led["entries"][-1] if led["entries"] else None
        print(f"{head}; used {use}; {len(led['entries'])} entries" + (f", last {last[0]} {last[1]} {last[2]} {last[3]}" if last else ""))
    print("Park markers:")
    for site in MARKERS:
        st = parked(site)
        if not st["markers"]:
            print(f"  {site}: not parked (no markers)")
            continue
        if st["hold"]:
            verdict = "HOLD: in force until the user runs unpark"
        elif st["in_force"]:
            verdict = f"in force until {st['until'] or 'the user unparks'}"
        else:
            verdict = "not in force" + ("; a gentle look is due before any other request" if st["look_due"] else "")
        print(f"  {site}: {verdict}; ladder rung {st['step']} of {len(LADDER_HOURS)} "
              f"({'/'.join(f'{h} h' for h in LADDER_HOURS)}, then a hold); the next wall parks "
              + ("nothing new (same incident)" if _same_incident(st) else
                 (f"{LADDER_HOURS[st['step']]} h" if st["step"] < len(LADDER_HOURS) else "with a hold")))
        for name, info in st["markers"].items():
            bits = [info.get("state", "?")] + [f"{k} {info[k]}" for k in ("until", "since", "step", "note") if info.get(k) is not None]
            print(f"    {name}: " + "; ".join(str(b) for b in bits) + (f"; what: {info['what']}" if info.get("what") else ""))
    print("Machine check for a new Gulf browser now (tool '*', no waiting):")
    checks = [("window", "window", lambda: _check_window("*", now)), ("pause gate", "paused", lambda: _check_pause(now)),
              ("Instahyre", "busy", lambda: _check_instahyre(now)), ("render/model", "busy", _check_heavy),
              ("RAM", "ram", lambda: _check_ram(0)[:2])]
    verdict = ""
    for label, kind, check in checks:
        problem, info = check()
        print(f"  {label:<13} {'BLOCKS' if problem else 'ok':<6} {problem or info}")
        verdict = verdict or (f"{kind}: {problem}" if problem else "")
    print(f"  machine_ok verdict: {('NOT OK -> a tool starting now exits 47 (' + verdict + ')') if verdict else 'OK'}")
    print("  keepalive loop-gap check: NOT IMPLEMENTED; RAM floor is the only guard against India-chain stalls until it exists; "
          "build it before the first Gulf browser start")
    stuck = _instahyre_stuck(now)
    if stuck:
        print(f"  WARN Instahyre: InCycle {stuck}: its supervisor may be hung; Gulf starts stay blocked while it runs, look at it")
    s = slot_state()
    if s.get("never_used"):
        print("Browser slot: free (gulf_browser.lock never created)")
    elif s["held"]:
        print(f"Browser slot: HELD by {s.get('holder', '?')} (pid {s.get('pid', '?')}) since {s.get('since', '?')}")
    else:
        print("Browser slot: free" + (f" (last {s['last_holder']}, released {s.get('free_since')})" if s.get("last_holder") else ""))
    online = machine_online()
    print(f"Network: Windows reports the machine {'online' if online else 'OFFLINE' if online is False else 'as unknown'} "
          "(asked locally; is_wall counts a cut connection as a wall only while online)")
    reg = read_toasts()
    today = date.today().isoformat()
    if reg is None:
        print(f"Toasts: {TOASTS.name} is not in the registry format; tools show toasts but never overwrite it")
    elif not TOASTS.exists():
        print(f"Toasts: none recorded yet ({TOASTS.name} not created)")
    else:
        mine = sorted(k for k in reg if k.startswith(today + "|"))
        print(f"Toasts: {len(reg)} recorded, {len(mine)} today" + (": " + ", ".join(k.split("|", 1)[1] for k in mine) if mine else ""))
    return 0


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="ledger, park markers, machine check, browser slot and today's toasts (read-only)")
    u = sub.add_parser("unpark", help="the user's command: lift a park or hold; one gentle look is then due")
    u.add_argument("site", choices=sorted(MARKERS))
    args = ap.parse_args(argv)
    if args.cmd == "status":
        return status()
    try:
        st = unpark(args.site)
    except RuntimeError as e:          # the hand-off lock: refused before anything is read or written
        print(e)
        return 2
    print(f"{args.site}: unparked; in force {st['in_force']}, gentle look due {st['look_due']}, ladder rung {st['step']}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(_main())
