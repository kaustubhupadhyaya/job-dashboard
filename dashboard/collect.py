"""Reads every bot's own files (read-only) and turns them into the board's rows.

Counts come only from saved rows (Naukri statuses starting with "Applied", LinkedIn's history CSV,
Instahyre applications the server confirmed), never from a bot's own run stats or from the
keepalive saying the process is alive. Liveness is drawn separately (rail + start ticks).
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import paths
except ImportError:     # imported as part of the package (python -m dashboard.collect)
    from . import paths

csv.field_size_limit(10**9)

try:    # the gate for the Gulf rows (earned_rows below); if it cannot even be imported, no Gulf row is shown
    import earned
except Exception:   # noqa: BLE001
    earned = None

JT, NK, LI = paths.JT, paths.NK, paths.LI
CFG = paths.SETTINGS
try:
    IH_CFG = json.loads((JT / "instahyre_config.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    IH_CFG = {}
IST = timezone(timedelta(hours=5, minutes=30))

# Every file the board may show as a read-only view. The page links to /view/log/<key>.
LOGS = {
    "naukri1-bot": NK / "naukri_bot.log",
    "naukri1-keepalive": NK / "naukri_keepalive.log",
    "naukri2-bot": NK / "profiles" / "naukri2" / "naukri_bot.log",
    "naukri2-keepalive": NK / "profiles" / "naukri2" / "naukri_keepalive.log",
    "linkedin-keepalive": LI / "logs" / "linkedin_keepalive.log",
    "linkedin-supervisor": LI / "logs" / "linkedin_keepalive_stdout.log",
    "linkedin-bot": LI / "logs" / "log.txt",
    "instahyre-apply": JT / "logs" / "instahyre_apply.log",
    "instahyre-keepalive": JT / "logs" / "instahyre_apply_keepalive.log",
    "refresh": JT / "logs" / "resume_refresh.log",
    "refresh-keepalive": JT / "logs" / "resume_refresh_keepalive.log",
    "digest": JT / "logs" / "gulf_digest.log",
}
SHOTS = JT / "logs" / "resume_refresh_shots"

LINE_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})\] \[(\w+)\] (.*)$")
SUP_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}) \[supervisor\] (.*)$")
UPTIME = re.compile(r"uptime ([\d.]+)m")

_read_errors: dict[str, float] = {}   # file -> first time it could not be read (process lifetime)
_files_read: dict[str, dict] = {}


# ------------------------------------------------------------------ the daily window
WINDOW_FILE = JT / "appliers_window.txt"


def window_info(now: datetime) -> dict:
    """The daily window for ALL appliers (job-tracker/appliers_window.txt: start=HH:MM, end=HH:MM, IST; set up by appliers_window.ps1). Outside it the
    appliers are stopped on purpose, so a row that is not saving is resting by schedule, not failing. {'configured': False} when there is no window."""
    s = e = None
    try:
        for ln in WINDOW_FILE.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(start|end)\s*=\s*(\d{1,2}):(\d{2})\s*$", ln, re.I)
            if m:
                t = (int(m.group(2)), int(m.group(3)))
                if m.group(1).lower() == "start":
                    s = t
                else:
                    e = t
    except OSError:
        return {"configured": False}
    if s is None or e is None or s == e or not (0 <= s[0] < 24 and 0 <= e[0] < 24):
        return {"configured": False}
    cur = (now.hour, now.minute)
    inside = (s <= cur < e) if s < e else (cur >= s or cur < e)
    return {"configured": True, "start": f"{s[0]:02d}:{s[1]:02d}", "end": f"{e[0]:02d}:{e[1]:02d}", "inside": inside, "start_min": s[0] * 60 + s[1],
            "crosses_midnight": s > e}


# ------------------------------------------------------------------ helpers
def mins(dt: datetime) -> float:
    return dt.hour * 60 + dt.minute + dt.second / 60


def hhmm(dt: datetime | None) -> str | None:
    return dt.strftime("%H:%M") if dt else None


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def note_file(path: Path, ok: bool, err: str | None = None) -> None:
    key = str(path)
    try:
        mt = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        mt = None
    _files_read[key] = {"path": key, "ok": ok, "modified": iso(mt), "error": err}
    if ok:
        _read_errors.pop(key, None)
    else:
        _read_errors.setdefault(key, time.time())


def tail_lines(path: Path, max_bytes: int = 400_000) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
        note_file(path, True)
    except OSError as e:
        note_file(path, False, str(e))
        return []
    text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
    lines = text.splitlines()
    return lines[1:] if size > max_bytes else lines


def parse_log(path: Path, day: date) -> list[tuple[datetime, str, str]]:
    """[(time, level, message)] for lines of `day` in a '[YYYY-MM-DD HH:MM:SS] [LEVEL] msg' log."""
    out = []
    ds = day.isoformat()
    for ln in tail_lines(path):
        if not ln.startswith("[" + ds):
            continue
        m = LINE_TS.match(ln)
        if m:
            d, h, mi, s, lvl, msg = m.groups()
            out.append((datetime.fromisoformat(f"{d} {h}:{mi}:{s}"), lvl, msg))
    return out


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def merge_intervals(iv: list[list[float]], gap: float = 6.0) -> list[list[float]]:
    iv = sorted([max(0.0, a), b] for a, b in iv if b > 0)
    out: list[list[float]] = []
    for a, b in iv:
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[round(a, 1), round(b, 1)] for a, b in out]


def ago_min(dt: datetime | None, now: datetime) -> float | None:
    return None if dt is None else (now - dt).total_seconds() / 60


def fmt_dur(m: float) -> str:
    m = int(round(m))
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    if h >= 10:
        return f"{h} h"
    return f"{h} h {r} min" if r else f"{h} h"


def task_info(name: str) -> dict | None:
    """Scheduled task status via schtasks (cached per poll by the caller)."""
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", name, "/fo", "csv", "/v"], capture_output=True, text=True,
                           timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    rows = list(csv.reader(r.stdout.splitlines()))
    if len(rows) < 2:
        return None
    rec = dict(zip(rows[0], rows[1]))
    return {"status": rec.get("Status"), "next_run": rec.get("Next Run Time"), "last_run": rec.get("Last Run Time"),
            "last_result": rec.get("Last Result")}


# ------------------------------------------------------------------ saved applications
def naukri_rows(db: Path) -> list[tuple[datetime, str]] | None:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        rows = con.execute("SELECT application_date, status, company_name FROM applied_jobs").fetchall()
        con.close()
        note_file(db, True)
    except sqlite3.Error as e:
        note_file(db, False, str(e))
        return None
    out = []
    for when, status, company in rows:
        if not (status or "").strip().lower().startswith("applied") or not when:
            continue
        try:
            out.append((datetime.fromisoformat(str(when)[:26]), company or ""))
        except ValueError:
            continue
    return out


def linkedin_rows() -> list[tuple[datetime, str]] | None:
    p = LI / "all excels" / "all_applied_applications_history.csv"
    try:
        out = []
        with open(p, encoding="utf-8", errors="replace", newline="") as fh:
            for r in csv.DictReader(fh):
                w = (r.get("Date Applied") or "").strip()
                if len(w) >= 19 and w[:4].isdigit():
                    try:
                        out.append((datetime.fromisoformat(w[:26]), (r.get("Company") or "").strip()))
                    except ValueError:
                        pass
        note_file(p, True)
        return out
    except OSError as e:
        note_file(p, False, str(e))
        return None


def instahyre_rows(acct: str) -> tuple[list[tuple[datetime, str]] | None, list[dict]]:
    db = JT / "state" / f"instahyre_{acct}.db"
    if not db.exists():
        return [], []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        apps = con.execute("SELECT ts, company, title FROM applications WHERE decision='applied'").fetchall()
        runs = [dict(zip(("started", "ended", "exit_code", "applied", "note"), r)) for r in
                con.execute("SELECT started, ended, exit_code, applied, note FROM runs ORDER BY run_id DESC LIMIT 40")]
        con.close()
        note_file(db, True)
    except sqlite3.Error as e:
        note_file(db, False, str(e))
        return None, []
    return [(datetime.fromisoformat(ts), f"{c}") for ts, c, _t in apps], runs


# ------------------------------------------------------------------ pipelines
def base_row(key: str, rows: list[tuple[datetime, str]] | None, now: datetime, cap: int, file_for_error: Path) -> dict:
    today = now.date()
    r = {"key": key, "cap": cap, "kind": "apply", "starts": [], "alive": [], "events": [], "links": [],
         "rest_from": None, "sign": False, "state": "applying", "word": "Applying", "detail": "", "cause": None}
    if rows is None:
        first_bad = _read_errors.get(str(file_for_error), time.time())
        r.update(state="cant_read", word="Can't read", count=None, points=[], apps=[], history=[],
                 last=None, last_company=None,
                 sign=(time.time() - first_bad) > 300,
                 detail=f"Can't read {file_for_error.name} since {datetime.fromtimestamp(first_bad):%H:%M}",
                 cause=[{"t": f"Can't read "}, {"code": file_for_error.name},
                        {"t": f" since {datetime.fromtimestamp(first_bad):%H:%M}. Check the file."}])
        return r
    todays = sorted((t, c) for t, c in rows if t.date() == today)
    r["count"] = len(todays)
    r["points"] = [round(mins(t), 2) for t, _ in todays]
    r["apps"] = [{"t": hhmm(t), "company": c} for t, c in reversed(todays)][:12]
    last = max((t for t, _ in rows), default=None)
    r["last"] = iso(last)
    r["last_company"] = next((c for t, c in rows if t == last), None) if last else None
    if len(todays) >= cap:
        r["cap_reached_at"] = round(mins(todays[cap - 1][0]), 2)
    # 14 days, oldest first; today last (drawn as an outline until midnight)
    first_day = min((t.date() for t, _ in rows), default=None)
    counts: dict[date, int] = {}
    for t, _ in rows:
        counts[t.date()] = counts.get(t.date(), 0) + 1
    hist = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        hist.append({"date": d.isoformat(), "count": counts.get(d, 0), "exists": bool(first_day and d >= first_day)})
    r["history"] = hist
    return r


def silence_min(r: dict, now: datetime) -> float:
    last = datetime.fromisoformat(r["last"]) if r.get("last") else None
    midnight = datetime.combine(now.date(), datetime.min.time())
    w = window_info(now)
    if w["configured"] and not w["crosses_midnight"] and w["inside"]:
        midnight += timedelta(minutes=w["start_min"])    # nothing runs before the daily window opens, so the quiet clock starts there
    since = max(last, midnight) if last else midnight
    return (now - since).total_seconds() / 60, since


def judge(r: dict, now: datetime, threshold: int, failing_evidence, stall_cause, resting: tuple[bool, str] | None):
    """Sets state/word/detail/sign by the board's precedence once the row-specific facts are known."""
    if r["state"] in ("cant_read", "logged_out", "stopped", "switched_off", "paused"):
        return
    if resting and resting[0]:
        r.update(state="resting", word="Resting", detail=resting[1])
        return
    quiet, since = silence_min(r, now)
    last_hour = sum(1 for p in r["points"] if mins(now) - p <= 60)
    if quiet > threshold:
        ev = failing_evidence(since)
        if ev:
            r.update(state="failing", word="Failing", sign=True, detail=f"Nothing saved for {fmt_dur(quiet)}", cause=ev)
        else:
            r.update(state="stalled", word="Stalled", sign=True, detail=f"Nothing saved for {fmt_dur(quiet)}",
                     cause=stall_cause(since))
        return
    if r["count"]:
        r.update(state="applying", word="Applying", detail=f"{last_hour} in the last hour")
    else:
        first = r["starts"][0] if r["starts"] else None
        r.update(state="applying", word="Applying",
                 detail=f"Started {first_hhmm(first)}, none saved yet" if first is not None else "None saved yet")


def first_hhmm(m: float | None) -> str:
    return "--:--" if m is None else f"{int(m // 60):02d}:{int(m % 60):02d}"


def naukri(acct: int, now: datetime) -> dict:
    today = now.date()
    base = NK if acct == 1 else NK / "profiles" / "naukri2"
    db = base / "naukri_jobs.db"
    r = base_row(f"naukri{acct}", naukri_rows(db), now, 50, db)
    r.update(name=f"Naukri {acct}", identity="Data Platform Engineer" if acct == 1 else "AI Engineer", cap_label="50")
    kl = LOGS[f"naukri{acct}-keepalive"]
    lines = parse_log(kl, today)
    starts, alive, events = [], [], []
    for t, lvl, msg in lines:
        if msg.startswith("Bot started successfully"):
            starts.append(round(mins(t), 2))
            events.append((t, "Started by the keepalive"))
        elif msg.startswith("Bot alive"):
            m = UPTIME.search(msg)
            if m:
                up = float(m.group(1))
                alive.append([mins(t) - up, mins(t)])
        elif msg.startswith("Daily application limit reached"):
            events.append((t, "Reached the daily limit; the keepalive closed the bot"))
        elif lvl in ("ERROR", "WARN") and not msg.startswith("Bot not seen"):
            events.append((t, msg[:140]))
        elif msg.startswith("Process is DEAD"):
            events.append((t, "Bot process ended"))
    r["starts"], r["alive"] = starts, merge_intervals(alive)
    r["events"] = [{"t": hhmm(t), "text": x} for t, x in sorted(events, reverse=True)[:12]]
    r["links"] = [{"label": "Open the Naukri log", "href": f"/view/log/naukri{acct}-bot"},
                  {"label": "Open the keepalive log", "href": f"/view/log/naukri{acct}-keepalive"}]
    task = "NaukriKeepalive" if acct == 1 else "NaukriKeepalive2"
    st = read_state(base / "naukri_keepalive.state.json" if acct == 2 else NK / "naukri_keepalive.state.json")
    ds = today.isoformat()
    marker = lambda k: (base if acct == 2 else NK) / f"NAUKRI_{k}_{ds}"
    # capped history days: count >= 50 or a limit marker for that day
    for h in r.get("history", []):
        h["capped"] = h["count"] >= 50 or ((base if acct == 2 else NK) / f"NAUKRI_DAILY_LIMIT_{h['date']}").exists()
        h["ran"] = h["exists"]
    if r["state"] == "cant_read":
        return r
    if marker("LOGIN_REQUIRED").exists():
        t = datetime.fromtimestamp(marker("LOGIN_REQUIRED").stat().st_mtime)
        r.update(state="logged_out", word="Logged out", sign=True, detail=f"Signed out at {hhmm(t)}",
                 cause=[{"t": f"Naukri signed Naukri {acct} out at {hhmm(t)}. Log in once in its Edge window, then start the {task} task."}])
    elif marker("RELAUNCH_HALTED").exists():
        t = datetime.fromtimestamp(marker("RELAUNCH_HALTED").stat().st_mtime)
        r.update(state="stopped", word="Stopped", sign=True, detail=f"Relaunching halted at {hhmm(t)}",
                 cause=[{"t": "Relaunching halted after repeated failed runs. Delete "},
                        {"code": marker("RELAUNCH_HALTED").name}, {"t": f", then start the {task} task."}])
    else:
        rest_marker = marker("DAILY_LIMIT").exists() or st.get("LastDailyCapDate") == ds
        capped = r["count"] >= 50
        if capped or rest_marker:
            at = r.get("cap_reached_at")
            if at is None:
                lim = next((t for t, _l, m in lines if m.startswith("Daily application limit reached")), None)
                at = round(mins(lim), 2) if lim else None
            r["rest_from"] = at
            resting = (True, f"Reached {r['count']} at {first_hhmm(at)}. Back after midnight."
                       if capped else f"Naukri refused more at {first_hhmm(at)} ({r['count']} saved).")
        else:
            resting = None
            km = ago_min(datetime.fromtimestamp(kl.stat().st_mtime) if kl.exists() else None, now)
            if km is None or km > 10:
                r.update(state="stopped", word="Stopped", sign=True,
                         detail=f"Keepalive silent for {fmt_dur(km or 0)}",
                         cause=[{"t": f"The keepalive hasn't checked in since "
                                      f"{hhmm(now - timedelta(minutes=km or 0))}. Start the {task} task."}])

        def failing(since: datetime):
            s_in = [s for s in starts if s >= mins(since)]
            errs = [1 for t, l, m in lines if t >= since and l == "ERROR"]
            if len(s_in) >= 2 or errs:
                n = len(s_in)
                return [{"t": (f"Relaunched {n} times since {hhmm(since)}, saving nothing. " if n >= 2 else
                               f"{len(errs)} failed runs since {hhmm(since)}. ")},
                        {"link": "Open the keepalive log", "href": f"/view/log/naukri{acct}-keepalive"}]
            return None

        def stalled(since: datetime):
            up = r["alive"][-1][0] if r["alive"] else None
            nothing_new = sum(1 for ln in tail_lines(LOGS[f"naukri{acct}-bot"], 200_000)[-2000:]
                              if "No new jobs on this page" in ln)
            what = "finding nothing new to apply to" if nothing_new else "saving nothing"
            lead = f"Up since {first_hhmm(up)}, {what}. " if up is not None else f"Nothing saved since {hhmm(since)}. "
            return [{"t": lead}, {"link": "Open the Naukri log", "href": f"/view/log/naukri{acct}-bot"}]

        judge(r, now, CFG["stall_minutes"][f"naukri{acct}"], failing, stalled, resting)
    return r


def linkedin(now: datetime) -> dict:
    today = now.date()
    r = base_row("linkedin", linkedin_rows(), now, 35, LI / "all excels" / "all_applied_applications_history.csv")
    r.update(name="LinkedIn", identity=CFG.get("linkedin_identity", "Data Platform Engineer"), cap_label="~35")
    ds = today.isoformat()
    for h in r.get("history", []):
        h["capped"] = (LI / "logs" / f"LINKEDIN_DAILY_LIMIT_{h['date']}").exists() or h["count"] >= 35
        h["ran"] = h["exists"]
    kl = LOGS["linkedin-keepalive"]
    lines = parse_log(kl, today)
    starts, alive, events = [], [], []
    for t, lvl, msg in lines:
        if msg.startswith("Supervisor started successfully"):
            starts.append(round(mins(t), 2))
            events.append((t, "Relaunched by the keepalive"))
        elif msg.startswith("Supervisor alive"):
            m = UPTIME.search(msg)
            if m:
                alive.append([mins(t) - float(m.group(1)), mins(t)])
        elif lvl in ("ERROR", "WARN") and not msg.startswith("Supervisor not seen"):
            events.append((t, msg[:140]))
    sup_runs = []   # (time, saved) for supervisor attempts that ended
    for ln in tail_lines(LOGS["linkedin-supervisor"], 300_000):
        m = SUP_TS.match(ln)
        if not m or m.group(1) != ds:
            continue
        t = datetime.fromisoformat(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}")
        msg = m.group(5)
        if re.match(r"attempt \d+: launching", msg):
            if not any(abs(s - mins(t)) < 1.5 for s in starts):
                starts.append(round(mins(t), 2))
        am = re.match(r"attempt (\d+): exited rc=(-?\d+) after ([\d.]+)min saved\+(\d+)", msg)
        if am:
            saved = int(am.group(4))
            sup_runs.append((t, saved))
            events.append((t, f"Run {am.group(1)} ended after {am.group(3)} min, "
                              + ("nothing saved" if not saved else f"{saved} saved")))
        elif "stop" in msg.lower() or "persistent breakage" in msg.lower():
            events.append((t, msg[:140]))
    r["starts"], r["alive"] = sorted(starts), merge_intervals(alive)
    r["events"] = [{"t": hhmm(t), "text": x} for t, x in sorted(events, reverse=True)[:12]]
    r["links"] = [{"label": "Open the supervisor log", "href": "/view/log/linkedin-supervisor"},
                  {"label": "Open the keepalive log", "href": "/view/log/linkedin-keepalive"},
                  {"label": "Open the bot log", "href": "/view/log/linkedin-bot"}]
    if r["state"] == "cant_read":
        return r
    st = read_state(LI / "logs" / "linkedin_keepalive.state.json")
    need = LI / "logs" / "NEEDS_HUMAN"
    limit = LI / "logs" / f"LINKEDIN_DAILY_LIMIT_{ds}"
    if need.exists():
        why = need.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        why = why[0][:120] if why else "the supervisor stopped"
        t = datetime.fromtimestamp(need.stat().st_mtime)
        login = "login" in why.lower() or "sign" in why.lower()
        r.update(state="logged_out" if login else "stopped", word="Logged out" if login else "Stopped", sign=True,
                 detail=f"Stopped at {hhmm(t)}",
                 cause=[{"t": f"The supervisor stopped at {hhmm(t)}: {why}. Fix it, delete "},
                        {"code": r"logs\NEEDS_HUMAN"}, {"t": ", then start the LinkedInKeepalive task."}])
        return r
    resting = None
    try:
        rest_until = datetime.fromisoformat(st["LimitRestUntil"]) if st.get("LimitRestUntil") else None
    except (TypeError, ValueError):
        rest_until = None
    if rest_until and rest_until > now:
        # 2026-10-08: the keepalive rests until LinkedIn's own reset (05:30 IST) after any limit,
        # across local midnight; its log is quiet while it waits.
        t = datetime.fromtimestamp(limit.stat().st_mtime) if limit.exists() else None
        r["rest_from"] = round(mins(t), 2) if t else r.get("cap_reached_at")
        resting = (True, f"LinkedIn's daily limit is used up; resting until {hhmm(rest_until)}, when LinkedIn resets it.")
    elif limit.exists() or st.get("LastLimitDate") == ds:
        t = datetime.fromtimestamp(limit.stat().st_mtime) if limit.exists() else None
        r["rest_from"] = round(mins(t), 2) if t else r.get("cap_reached_at")
        resting = (True, f"LinkedIn's limit came at {hhmm(t) or '--:--'}"
                         + (f" ({r['count']} saved)." if r["count"] == 0 else "."))
        try:
            retry_at = datetime.fromisoformat(st["LimitRetryAt"]) if st.get("LimitRetryAt") else None
        except (TypeError, ValueError):
            retry_at = None
        if retry_at and retry_at > now:
            # A limit carried over from yesterday: the keepalive declares when it retries.
            resting = (True, f"Limit left over from yesterday; retrying at {hhmm(retry_at)}.")
    else:
        km = ago_min(datetime.fromtimestamp(kl.stat().st_mtime) if kl.exists() else None, now)
        if km is None or km > 10:
            r.update(state="stopped", word="Stopped", sign=True, detail=f"Keepalive silent for {fmt_dur(km or 0)}",
                     cause=[{"t": f"The keepalive hasn't checked in since {hhmm(now - timedelta(minutes=km or 0))}. "
                                  "Start the LinkedInKeepalive task."}])

    def failing(since: datetime):
        n = sum(1 for s in r["starts"] if s >= mins(since))
        zero_runs = sum(1 for t, saved in sup_runs if t >= since and saved == 0)
        if n >= 2 or zero_runs >= 2:
            return [{"t": f"Relaunched {n} times since {hhmm(since)}, saving nothing. "},
                    {"link": "Open the supervisor log", "href": "/view/log/linkedin-supervisor"}]
        return None

    def stalled(since: datetime):
        return [{"t": f"Nothing saved since {hhmm(since)}. "},
                {"link": "Open the bot log", "href": "/view/log/linkedin-bot"}]

    judge(r, now, CFG["stall_minutes"]["linkedin"], failing, stalled, resting)
    return r


def instahyre(acct: str, now: datetime, task: dict | None) -> dict:
    today = now.date()
    rows, runs = instahyre_rows(acct)
    cap = int(IH_CFG.get("daily_cap", 30))
    r = base_row(f"instahyre{acct}", rows, now, cap, JT / "state" / f"instahyre_{acct}.db")
    r.update(name=f"Instahyre {acct}", identity="Data Platform Engineer" if acct == "1" else "AI Engineer",
             cap_label=str(cap), scheduled=True)
    for h in r.get("history", []):
        h["capped"] = h["count"] >= cap
        h["ran"] = h["exists"]
    starts, alive, events = [], [], []
    for run in runs:
        st = datetime.fromisoformat(run["started"])
        en = datetime.fromisoformat(run["ended"]) if run["ended"] else now
        if en.date() == today or st.date() == today:
            a = mins(st) if st.date() == today else 0
            if st.date() == today:
                starts.append(round(a, 2))
            alive.append([a, mins(en)])
            dry = "dry-run" in (run["note"] or "")
            how = f"ended, exit {run['exit_code']}" if run["ended"] else "running"
            applied = "" if dry else f", {run['applied'] or 0} applied"
            note = (run["note"] or "").replace("dry-run; ", "")[:80]
            events.append((st, f"{'Dry run' if dry else 'Run'} {how}{applied} ({note})"))
    for t, lvl, msg in parse_log(LOGS["instahyre-apply"], today):
        if msg.startswith(f"[acct {acct}] ") and lvl in ("WARN", "ERROR"):
            events.append((t, msg[len(f"[acct {acct}] "):][:140]))
    r["starts"], r["alive"] = sorted(starts), merge_intervals(alive, gap=1)
    r["events"] = [{"t": hhmm(t), "text": x} for t, x in sorted(events, reverse=True)[:12]]
    r["links"] = [{"label": "Open the applier log", "href": "/view/log/instahyre-apply"},
                  {"label": "Open the supervisor log", "href": "/view/log/instahyre-keepalive"}]
    if r["state"] == "cant_read":
        return r
    m = lambda k: JT / "state" / f"INSTAHYRE_{k}_{acct}"
    real_runs = [x for x in runs if "dry-run" not in (x["note"] or "")]
    last_real = real_runs[0] if real_runs else None
    if m("LOGIN_REQUIRED").exists():
        t = datetime.fromtimestamp(m("LOGIN_REQUIRED").stat().st_mtime)
        r.update(state="logged_out", word="Logged out", sign=True, detail=f"Signed out at {hhmm(t)}",
                 cause=[{"t": "The apply profile is signed out. Run "}, {"code": f"instahyre_apply.cmd setup {acct}"},
                        {"t": " and sign in there once."}])
        return r
    if task is None and not real_runs:
        r.update(state="switched_off", word="Switched off", detail="Dry runs only so far" if runs else "Not scheduled",
                 cause=None)
        r["off_hint"] = [{"t": "Turn on: "}, {"code": "instahyre_apply.cmd install"}]
        return r
    # (2026-10-08: with real runs on record, a missing task answer is a failed schtasks query, not "switched
    # off"; the row is judged from its runs and saved applications like any other.)
    thr = m("THROTTLED")
    if thr.exists():
        try:
            until = datetime.fromisoformat(json.loads(thr.read_text(encoding="utf-8"))["until"])
        except Exception:
            until = None
        if until and until > now:
            r.update(state="paused", word="Paused by Instahyre", detail=f"Until {hhmm(until)} (HTTP 429)")
            return r
    if m("PROTECTION").exists() and m("PROTECTION").read_text(encoding="utf-8")[:10] == today.isoformat():
        r.update(state="stopped", word="Stopped", sign=True, detail="Instahyre's abuse limit fired",
                 cause=[{"t": "Instahyre's abuse protection stopped today's applying. Lower "},
                        {"code": "daily_cap"}, {"t": " in instahyre_config.json. "},
                        {"link": "Open the applier log", "href": "/view/log/instahyre-apply"}])
        return r
    if r["count"] >= cap:
        r["rest_from"] = r.get("cap_reached_at")
        r.update(state="resting", word="Resting", detail=f"Reached {cap} at {first_hhmm(r['rest_from'])}. Back tomorrow.")
        return r
    if last_real and last_real["ended"] and last_real["exit_code"] not in (0, 44, 47) \
            and last_real["started"][:10] == today.isoformat():
        r.update(state="failing", word="Failing", sign=True, detail=f"Last run exit {last_real['exit_code']}",
                 cause=[{"t": f"The {last_real['started'][11:16]} run failed: {(last_real['note'] or '')[:60]}. "},
                        {"link": "Open the applier log", "href": "/view/log/instahyre-apply"}])
        return r
    if last_real and not last_real["ended"]:
        r.update(state="applying", word="Applying", detail="Applying now")
        return r
    # The supervisor runs all day and declares its next check (an end time for the wait). A wait with
    # no declaration, or one that ran out more than 30 min ago, means the supervisor is gone.
    sup = read_state(JT / "logs" / "instahyre_apply.state.json")
    try:
        nxt = datetime.fromisoformat(sup["NextCheck"])
        last_cycle = datetime.fromisoformat(sup["LastCycle"])
    except (KeyError, TypeError, ValueError):
        nxt = last_cycle = None
    if nxt is None or now > nxt + timedelta(minutes=30):
        since = hhmm(last_cycle) if last_cycle else "--:--"
        r.update(state="stopped", word="Stopped", sign=True, detail=f"Supervisor silent since {since}",
                 cause=[{"t": f"The Instahyre supervisor hasn't checked in since {since}. Start the InstahyreApply task. "},
                        {"link": "Open the supervisor log", "href": "/view/log/instahyre-keepalive"}])
        return r
    if sup.get("InCycle"):
        r.update(state="waiting", word="Waiting", detail=f"{r['count']} today; its turn this cycle")
    else:
        r.update(state="waiting", word="Waiting", detail=f"{r['count']} today; next check {hhmm(nxt)}")
    return r


# ------------------------------------------------------------------ Gulf portals
# A Gulf portal has a row on the board only once it has earned it (earned.py: its full daily quota in VERIFIED
# applications on each of the last 3 completed days); see earned_rows(). The job digest is not an applier and never has one.
GULF_REG_PATH = Path(__file__).with_name("gulf_portals.json")
GULF_DIGEST = JT / "state" / "gulf_digest.json"     # still served read-only at /view/shortlist/digest


def gulf_registry() -> dict:
    """The committed registry of Gulf portals; also registers each portal's log for /view/log/<key>-apply."""
    try:
        reg = json.loads(GULF_REG_PATH.read_text(encoding="utf-8"))
        note_file(GULF_REG_PATH, True)
    except (OSError, ValueError) as e:
        note_file(GULF_REG_PATH, False, str(e))
        return {"portals": []}
    good = []
    for p in reg.get("portals", []):
        if isinstance(p, dict) and p.get("key") and p.get("name") and p.get("db"):
            good.append(p)
            if p.get("log"):
                LOGS[f"{p['key']}-apply"] = JT / p["log"]
        else:   # one bad entry must not take the whole board down
            note_file(GULF_REG_PATH, False, f"portal entry without key, name and db: {str(p)[:80]}")
    reg["portals"] = good
    return reg


def gulf_shortlist_path(key: str) -> Path | None:
    if key == "digest":
        return GULF_DIGEST
    if key == "linkedin-uae":
        return LI / "logs" / "linkedin_uae_shortlist.json"
    for p in gulf_registry().get("portals", []):
        if p["key"] == key and p.get("shortlist"):
            return JT / p["shortlist"]
    return None


def read_user_env(name: str) -> str | None:
    """A user-scope environment variable straight from HKCU\\Environment: the board's server may have
    started before the variable was set, so os.environ would not show it."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except (OSError, ImportError):
        return None


def gulf_armed(p: dict, today: date) -> tuple[bool, str]:
    """Live applying is armed by the user only: a flag variable (NaukriGulf: 1) or an expiry date (newer portals)."""
    a = p.get("armed") or {}
    v = (read_user_env(a.get("env", "")) or "").strip()
    if not v:
        return False, ""
    if a.get("kind") == "flag":
        return v == "1", ""
    try:
        exp = date.fromisoformat(v[:10])
    except ValueError:
        return False, "Arming value is not a date"
    return (exp >= today), (f"Armed until {exp:%d %b}" if exp >= today else f"Arming expired {exp:%d %b}")


def gulf_data(p: dict):
    """(rows the site confirmed, count applied-but-unconfirmed, recent runs) from the portal's own DB.
    (None, 0, []) when unreadable; ([], 0, []) before its first run."""
    db = JT / p["db"]
    if not db.exists():
        return [], 0, []
    con = None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        ver = con.execute("SELECT COALESCE(verified_at, ts), company FROM applications WHERE verified=1").fetchall()
        unver = con.execute("SELECT COUNT(*) FROM applications WHERE decision='applied' AND verified=0").fetchone()[0]
        runs = [dict(zip(("started", "ended", "mode", "exit_code", "fetched", "candidates", "applied", "note"), r))
                for r in con.execute("SELECT started, ended, mode, exit_code, fetched, candidates, applied, note "
                                     "FROM runs ORDER BY run_id DESC LIMIT 40")]
        note_file(db, True)
    except sqlite3.Error as e:
        note_file(db, False, str(e))
        return None, 0, []
    finally:
        if con is not None:   # a failed query used to leave the handle (and the file) open in the server
            con.close()
    return [(datetime.fromisoformat(str(v)[:26]), c or "") for v, c in ver], unver, runs


def day_hhmm(t: datetime, today: date) -> str:
    return hhmm(t) if t.date() == today else f"{t:%d %b} {hhmm(t)}"


def run_failed(run: dict | None) -> bool:
    """An ended run whose exit code is none of the normal ones (done, cap, throttled, not armed)."""
    return bool(run and run.get("ended") and run.get("exit_code") not in (0, 44, 47, 3))


def run_why(run: dict) -> str:
    """The first line of a failed run's note, without the exception class noise."""
    line = ((run.get("note") or "").splitlines() or [""])[0]
    return line.replace("CDPError: page JS error: ", "").strip()[:80]


def run_numbers(run: dict | None, today: date) -> str:
    """What the last run found, always stated, so a quiet row is never mistaken for a healthy one.
    A run that crashed says so: "0 jobs, 0 eligible" would read as a quiet scan."""
    if not run:
        return "No run yet"
    if not run.get("ended"):
        return "Running now"
    when = day_hhmm(datetime.fromisoformat(run["started"]), today)
    kind = "Dry run" if run["mode"] == "dry-run" else "Last run"
    if run_failed(run):
        why = run_why(run)
        return f"{kind} {when} failed (exit {run['exit_code']})" + (f": {why}" if why else "")
    s = f"{kind} {when}: {run['fetched'] or 0} jobs, {run['candidates'] or 0} eligible"
    return s if run["mode"] == "dry-run" else f"{s}, {run['applied'] or 0} applied"


def gulf_row(p: dict, now: datetime) -> dict:
    today = now.date()
    cap = int(p.get("cap", 25))
    rows, unver, runs = gulf_data(p)
    r = base_row(p["key"], rows, now, cap, JT / p["db"])
    r.update(name=p["name"], identity=p.get("identity", ""), cap_label=str(cap))
    for h in r.get("history", []):
        h["capped"] = h["count"] >= cap
        h["ran"] = h["exists"]
    starts, alive, events = [], [], []
    for run in runs:
        st = datetime.fromisoformat(run["started"])
        en = datetime.fromisoformat(run["ended"]) if run["ended"] else None
        if st.date() == today or (en and en.date() == today):
            a = mins(st) if st.date() == today else 0.0
            if st.date() == today:
                starts.append(round(a, 2))
            alive.append([a, mins(en) if en and en.date() == today else mins(now)])
            events.append((st, f"{run_numbers(run, today)} (exit {run['exit_code']})" if run["ended"] else "Run in progress"))
    log_path = JT / p["log"] if p.get("log") else None
    if log_path and log_path.exists():
        for t, lvl, msg in parse_log(log_path, today):
            if lvl in ("WARN", "ERROR"):
                events.append((t, msg[:140]))
    r["starts"], r["alive"] = sorted(starts), merge_intervals(alive, gap=1)
    r["events"] = [{"t": hhmm(t), "text": x} for t, x in sorted(events, reverse=True)[:12]]
    r["links"] = [{"label": "Open the applier log", "href": f"/view/log/{p['key']}-apply"}] if log_path else []
    if p.get("shortlist") and (JT / p["shortlist"]).exists():
        r["links"].append({"label": "Open the shortlist", "href": f"/view/shortlist/{p['key']}"})
    if r["state"] == "cant_read":
        return r

    log_link = {"link": "Open the applier log", "href": f"/view/log/{p['key']}-apply"}
    mk = {k: JT / v for k, v in (p.get("markers") or {}).items()}
    last_run = runs[0] if runs else None
    last_live = next((x for x in runs if x["mode"] == "live"), None)
    numbers = run_numbers(last_run, today)
    tail = f"; {unver} unconfirmed" if unver else ""

    def marker_until(path: Path) -> datetime | None:
        try:
            return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["until"])
        except Exception:
            return None

    if mk.get("login") and mk["login"].exists():
        t = datetime.fromtimestamp(mk["login"].stat().st_mtime)
        r.update(state="logged_out", word="Logged out", sign=True, detail=f"Signed out at {hhmm(t)}",
                 cause=[{"t": f"{p['name']} signed the bot out at {hhmm(t)}. Sign in once with "},
                        {"code": p.get("setup_command", "the setup command")}, {"t": ". "}, log_link])
        return r
    if mk.get("challenge") and mk["challenge"].exists():
        until = marker_until(mk["challenge"])
        if until is None or until > now:
            r.update(state="challenge", word="Bot check", sign=True,
                     detail=f"Blocked until {day_hhmm(until, today)}; {numbers}" if until else "Blocked by the site's bot check",
                     cause=[{"t": f"{p['name']} is blocking the bot's browser, so nothing is retried "
                                  f"{'until ' + day_hhmm(until, today) if until else 'until you have looked'}. "}, log_link])
            return r
    if mk.get("throttled") and mk["throttled"].exists():
        until = marker_until(mk["throttled"])
        if until and until > now:
            r.update(state="paused", word="Paused by the site", detail=f"Until {day_hhmm(until, today)}; {numbers}")
            return r
    if run_failed(last_run):     # a crashed run is shown even before the portal is armed
        r.update(state="failing", word="Failing", sign=True, detail=numbers + tail,
                 cause=[{"t": f"The last {last_run['mode']} did not finish normally"
                              f"{': ' + run_why(last_run) if run_why(last_run) else ''}. "}, log_link])
        return r
    armed, armed_note = gulf_armed(p, today)
    if not armed:
        r.update(state="switched_off", word="Not armed", detail=(f"{armed_note}; " if armed_note else "") + numbers + tail)
        return r
    if r["count"] >= cap:
        r["rest_from"] = r.get("cap_reached_at")
        r.update(state="resting", word="Resting", detail=f"Reached {cap} at {first_hhmm(r['rest_from'])}. Back tomorrow.")
        return r
    if last_run and not last_run["ended"]:
        started = datetime.fromisoformat(last_run["started"])
        if now - started < timedelta(hours=3):
            r.update(state="applying", word="Applying", detail="Run in progress")
        else:
            r.update(state="failing", word="Failing", sign=True, detail=f"Run from {hhmm(started)} never finished",
                     cause=[{"t": f"The run that started at {hhmm(started)} never wrote its end. "}, log_link])
        return r
    if last_live and last_live["ended"] and last_live["exit_code"] not in (0, 44, 47, 3) \
            and last_live["started"][:10] == today.isoformat():
        r.update(state="failing", word="Failing", sign=True, detail=f"Last live run exit {last_live['exit_code']}",
                 cause=[{"t": f"The {last_live['started'][11:16]} live run failed: {(last_live['note'] or '')[:70]}. "}, log_link])
        return r
    # Gulf supply is thin and the runs are two slots a day, so an armed portal between slots is "watching",
    # not "applying" (a long quiet here is normal and must not top the board's "longest quiet" line).
    r.update(state="watching", word="Watching", detail=numbers + tail)
    return r


def _jsonl(path: Path) -> list:
    out = []
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        note_file(path, True)
    except FileNotFoundError:
        pass
    except OSError as e:
        note_file(path, False, str(e))
    return out


def linkedin_uae_row(now: datetime) -> dict | None:
    """The LinkedIn UAE pass (an overlay in the LinkedIn bot's own browser): armed or not, and what its last pass found."""
    logs = LI / "logs"
    arm_path, jobs_path, cyc_path = logs / "LINKEDIN_UAE_ARMED", logs / "linkedin_uae_jobs.jsonl", logs / "linkedin_region_cycles.jsonl"
    if not (arm_path.exists() or jobs_path.exists()):
        return None                 # never armed: no row rather than a row that claims something
    today = now.date()
    arm = None
    try:
        a = json.loads(arm_path.read_text(encoding="utf-8"))
        if a.get("mode") in ("inventory", "live") and date.fromisoformat(str(a.get("until"))[:10]) >= today:
            arm = a
    except (OSError, ValueError):
        pass
    jobs = _jsonl(jobs_path)
    cycles = [c for c in _jsonl(cyc_path) if c.get("region") == "UAE"]
    quota = int((arm or {}).get("quota") or 12)
    applied = []
    for j in jobs:
        if j.get("outcome") == "applied" and j.get("mode") == "live":
            try:
                applied.append((datetime.fromisoformat(str(j["ts"])[:19]), j.get("company") or ""))
            except (KeyError, ValueError):
                pass
    r = base_row("linkedin-uae", applied, now, quota, jobs_path)
    r.update(name="LinkedIn UAE", identity="Data Platform Engineer", cap_label=str(quota),
             links=[{"label": "Open the skipped-job list", "href": "/view/shortlist/linkedin-uae"}])
    r["events"] = [{"t": f"{str(c['start'])[5:10]} {str(c['start'])[11:16]}",
                    "text": f"{c['mode'].capitalize()} pass: {c.get('visits', 0)} jobs looked at, "
                            f"{c.get('applied', 0) + c.get('would_apply', 0)} answerable, {c.get('skipped_unanswerable', 0)} blocked by a question"}
                   for c in reversed(cycles[-8:])]
    last = cycles[-1] if cycles else None
    last_txt = ("No pass yet" if not last else
                f"Last pass {day_hhmm(datetime.fromisoformat(str(last['start'])[:19]), today)}: {last.get('visits', 0)} jobs looked at, "
                f"{last.get('applied', 0) + last.get('would_apply', 0)} answerable, {last.get('skipped_unanswerable', 0)} blocked by a question")
    todays = [c for c in cycles if str(c.get("start", ""))[:10] == today.isoformat()]
    if arm and arm["mode"] == "inventory":
        r.update(state="watching", word="Inventory", today_unit="answerable",
                 count=sum(c.get("would_apply", 0) for c in todays),
                 detail=f"Applies to nothing; armed until {date.fromisoformat(str(arm['until'])[:10]):%d %b}. {last_txt}")
    elif arm:
        r.update(state="watching", word="Live", detail=f"{r['count']} of {quota} today; armed until "
                 f"{date.fromisoformat(str(arm['until'])[:10]):%d %b}. {last_txt}")
    else:
        r.update(state="switched_off", word="Not armed", detail=last_txt)
    return r


def gulf_rows(now: datetime, ids: set[str]) -> list[dict]:
    """The rows of the Gulf portals named in `ids` (only ever the earned ones, see earned_rows), built from each
    portal's own files. An earned row joins the normal Applications list, so none carries a "group"."""
    rows = []
    if "linkedin-uae" in ids:
        try:
            d = linkedin_uae_row(now)
            if d:
                rows.append(d)
        except Exception as e:      # a bug here must show up as a row, never as a dead /api/status
            rows.append({"key": "linkedin-uae", "kind": "apply", "name": "LinkedIn UAE", "identity": "Data Platform Engineer",
                         "cap": 0, "cap_label": "", "count": None, "points": [], "apps": [], "history": [], "starts": [], "alive": [],
                         "events": [], "links": [], "last": None, "state": "cant_read", "word": "Can't read", "sign": True,
                         "detail": f"Board error: {type(e).__name__}", "cause": [{"t": f"The board could not build the LinkedIn UAE row ({type(e).__name__}: {str(e)[:80]})."}]})
    for p in gulf_registry().get("portals", []):
        if p["key"] not in ids:
            continue
        try:
            rows.append(gulf_row(p, now))
        except Exception as e:      # a bug here must show up as a row, never as a dead /api/status
            rows.append({"key": p.get("key", "gulf"), "kind": "apply", "name": p.get("name", "Gulf portal"),
                         "identity": p.get("identity", ""), "cap": int(p.get("cap", 25)), "cap_label": str(p.get("cap", "")),
                         "count": None, "points": [], "apps": [], "history": [], "starts": [], "alive": [], "events": [],
                         "links": [], "last": None, "state": "cant_read", "word": "Can't read", "sign": True,
                         "detail": f"Board error: {type(e).__name__}",
                         "cause": [{"t": f"The board could not build the {p.get('name', 'Gulf')} row "
                                         f"({type(e).__name__}: {str(e)[:80]}). Check the dashboard code."}]})
    for r in rows:
        r["earned"] = True          # lets the page explain a portal row in its footer; there is no Gulf section any more
    return rows


def earned_rows(now: datetime) -> list[dict]:
    """A Gulf portal gets a row only once earned.py says it earned it: its FULL daily quota in VERIFIED applications on
    each of the last 3 completed days. Anything else shows nothing, and so does an earned.py that fails (it cannot be
    imported, or raises): the board then hides rows, it never shows one on trust. What is wrong with a portal that has
    not earned its row is read with `python dashboard\\earned.py status`, not on the board. The job digest is not an
    applier and never gets a row."""
    if earned is None:
        return []
    try:
        ids = {k for k, v in earned.earned_portals(markers=False).items() if v.get("earned") is True}
        return gulf_rows(now, ids) if ids else []
    except Exception:       # noqa: BLE001
        return []


# ------------------------------------------------------------------ resume refresh
VER = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2}\] \[INFO\] (naukri|instahyre): VERIFIED - (.*?) \((.*?\.png)\)\s*$")


def refresh_rows(now: datetime) -> list[dict]:
    today = now.date()
    ds = today.isoformat()
    verified: dict[tuple[str, int], dict] = {}
    attempts_today: list[datetime] = []
    for ln in tail_lines(LOGS["refresh"], 600_000):
        m = VER.match(ln)
        if not m:
            continue
        d, t, site, what, shot = m.groups()
        acct = 2 if "_acct2_" in shot else 1
        site_date = None
        dm = re.search(r"Uploaded on (\w{3}) (\d{1,2}), (\d{4})", what)
        if dm:
            site_date = datetime.strptime(" ".join(dm.groups()), "%b %d %Y").date()
        um = re.search(r"uploaded_on=(\S+)", what)
        if um:
            try:
                site_date = datetime.fromisoformat(um.group(1)).astimezone(IST).date()
            except ValueError:
                pass
        verified[(site, acct)] = {"when": datetime.fromisoformat(f"{d} {t}"), "site_date": site_date,
                                  "shot": Path(shot).name}
    kl = parse_log(LOGS["refresh-keepalive"], today)
    started = next((t for t, _l, msg in kl if msg.startswith("Resume refresh supervisor started")), None)
    finished = next((t for t, l, msg in reversed(kl) if l in ("OK", "ERROR") and started and t >= started
                     and ("succeeded" in msg or "Gave up" in msg or "needs you" in msg or "Already refreshed" in msg)), None)
    login_needed = any("needs you (exit 42)" in msg for _t, _l, msg in kl)
    retry = next(((t, msg) for t, l, msg in reversed(kl) if l == "WARN" and "Retrying in" in msg), None)
    for t, _l, msg in kl:
        if msg.startswith("Resume refresh supervisor started") or "Attempt" in msg:
            attempts_today.append(t)
    win_open, win_close = CFG["refresh_window"]
    wo = datetime.combine(today, datetime.strptime(win_open, "%H:%M").time())
    wc = datetime.combine(today, datetime.strptime(win_close, "%H:%M").time())
    out = []
    for site, acct in (("naukri", 1), ("naukri", 2), ("instahyre", 1), ("instahyre", 2)):
        v = verified.get((site, acct))
        r = {"key": f"refresh-{site}{acct}", "kind": "refresh", "name": f"{site.capitalize()} {acct}",
             "identity": "Data Platform Engineer" if acct == 1 else "AI Engineer", "sign": False,
             "last": iso(v["when"]) if v else None,
             "site_date": v["site_date"].isoformat() if v and v["site_date"] else None,
             "verified_at": round(mins(v["when"]), 2) if v and v["when"].date() == today else None,
             "confirmed": bool(v and v["site_date"] == today),
             "attempts": [round(mins(t), 2) for t in attempts_today],
             "window": [win_open, win_close], "events": [], "apps": [],
             "links": [{"label": "Open the refresh log", "href": "/view/log/refresh"}]}
        if v:
            r["links"].insert(0, {"label": "Open the screenshot", "href": f"/view/shot/{v['shot']}"})
        pretty = "Naukri" if site == "naukri" else "Instahyre"
        if v and v["when"].date() == today and r["confirmed"]:
            r.update(state="refreshed", word="Refreshed", detail="Confirmed on the site")
        elif v and v["when"].date() == today:
            r.update(state="unconfirmed", word="Not confirmed",
                     detail=f"{pretty} still shows {v['site_date']:%d %b}" if v["site_date"] else "No site date")
            if now > wc:
                r.update(sign=True, cause=[{"t": f"{pretty} still shows an older upload date. Run "},
                                           {"code": "resume_refresh.cmd"}, {"t": " now."}])
        elif login_needed:
            r.update(state="login", word="Needs your login", sign=True, detail="Sign-in check",
                     cause=[{"t": f"Sign-in check on {pretty}: run "}, {"code": "resume_refresh.cmd setup"},
                            {"t": " and finish it there."}])
        elif started and not finished:
            if retry:
                r.update(state="retrying", word="Retrying", detail=retry[1][:60])
            else:
                r.update(state="refreshing", word="Refreshing", detail=f"Started {hhmm(started)}")
        elif now < wo:
            r.update(state="due", word=f"Due at {win_open}", detail="")
        elif now <= wc:
            r.update(state="due", word="Due now", detail=f"Window {win_open}–{win_close}")
        else:
            r.update(state="missed", word="Missed today", sign=True, detail=f"Nothing verified by {win_close}",
                     cause=[{"t": f"Nothing verified on {pretty} by {win_close}. Run "}, {"code": "resume_refresh.cmd"},
                            {"t": " now."}])
        out.append(r)
    return out


# ------------------------------------------------------------------ entry
_task_cache: dict[str, tuple[float, dict | None]] = {}


def cached_task(name: str) -> dict | None:
    hit = _task_cache.get(name)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    info = task_info(name)
    if info is None and hit and hit[1] is not None:
        # 2026-10-08: a schtasks call that timed out on a busy PC used to be cached as "no task" for
        # 120 s, and Instahyre showed "Switched off" while it was applying. Keep the last good answer.
        _task_cache[name] = (time.time(), hit[1])
        return hit[1]
    _task_cache[name] = (time.time(), info)
    return info


def status() -> dict:
    now = datetime.now()
    t0 = time.time()
    _files_read.clear()   # the footer lists the files read by THIS pass; a removed file must not linger (_read_errors persists)
    ih_task = cached_task("InstahyreApply")
    pipelines = [linkedin(now), naukri(1, now), naukri(2, now), instahyre("1", now, ih_task),
                 instahyre("2", now, ih_task)] + earned_rows(now)
    refresh = refresh_rows(now)
    win = window_info(now)
    if win["configured"] and not win["inside"]:
        # outside the daily window the appliers are stopped on purpose: a scheduled rest, not a failure. Rows that need the user (logged out,
        # cannot read a file, switched off, paused by a site) keep their own state.
        for p in pipelines:
            if p.get("kind") == "apply" and p.get("state") in ("applying", "failing", "stalled", "stopped", "waiting"):
                p.update(state="resting", word="Resting", sign=False, cause=None, rest_from=None,
                         detail=f"Scheduled rest. The appliers run {win['start']}-{win['end']} IST and start again at {win['start']}.")
    return {
        "generated": iso(now),
        "now_min": round(mins(now), 2),
        "window": {k: win.get(k) for k in ("configured", "start", "end", "inside")},
        "pipelines": pipelines,
        "refresh": refresh,
        "stall_minutes": CFG["stall_minutes"],
        "refresh_window": CFG["refresh_window"],
        "files": sorted(_files_read.values(), key=lambda f: f["path"]),
        "start_command": CFG["start_command"],
        "took_ms": int((time.time() - t0) * 1000),
    }


if __name__ == "__main__":
    print(json.dumps(status(), indent=1, default=str)[:6000])
