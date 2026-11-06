"""LinkedIn auto-applier speed: where its time goes. Read-only; every time here is IST (this PC's clock).

Sources, all written by the bot itself:
  supervisor.log                       when each run (attempt) launched and ended, and why
  all_applied / all_failed history     one timestamped row per application / per rejected job
  linkedin_filter_*.jsonl, title_skips  one timestamped row per card decision (title check, activity badge, pass)
  linkedin_region_cycles.jsonl         the UAE pass: start, end, cards, visits
  unattended-hardening.log             timestamped events (Easy Apply clicks, discard reloads, official count checks)
  supervised-attempt-N.log             the bot's own text log: NO timestamps, but it names the job id on every skip line

MEASURED = taken straight from timestamps. ESTIMATED = the text log's line counts times a per-line cost, where the cost comes from a
non-negative least-squares fit of "seconds between two timestamped skip lines" on "what the text log counted between them" (well over a
thousand such intervals; R^2 is reported). The estimate is shown as an estimate everywhere.

A "count" is LinkedIn's own day for the 35-a-day limit: 05:30 IST to 05:30 IST. Every row here is per count, so a run that works past
midnight belongs to the morning it began in, and a chain that was already running at 05:30 is split at 05:30.

compute() reads ~40 MB, so start() runs it in a background thread every 10 minutes and the page reads the last result.
"""
from __future__ import annotations

import ast
import bisect
import collections
import csv
import glob
import json
import os
import re
import statistics
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import paths
except ImportError:     # imported as part of the package (python -m dashboard.lispeed)
    from . import paths

LI = paths.LI
LOGS = LI / "logs"
EXC = LI / "all excels"
COVERAGE_FROM = "2026-10-19"      # the card-decision logs (title check) start here: time attribution works from this count on
GAP_CAP = 900                     # seconds; a longer gap between two logged events is a restart or a rest, not a card
RESET = timedelta(hours=5, minutes=30)
BADGE_RULE_FROM = "2026-10-13"    # the "actively reviewing" badge became mandatory (memory note linkedin-actively-reviewing-filter)
PAIR_SAFE_FROM = datetime(2026, 10, 1)   # attempt-2's text log holds one run fewer than the supervisor launched, somewhere in September
RC_WORDS = {0: "all search terms done, restarted", 1: "stopped by the supervisor", 44: "daily limit reached", 49: "fit rules unreadable"}

_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_cache: dict = {"at": None, "data": None, "error": ""}
_lock = threading.Lock()
_busy = threading.Event()


def P(s) -> datetime:
    return datetime.fromisoformat(str(s).replace(" ", "T")[:19])


# ------------------------------------------------------------------ loaders
def _jsonl(name: str, since: str = "") -> list[dict]:
    out = []
    try:
        with open(LOGS / name, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                ts = o.get("ts") or o.get("start") or ""
                if ts and ts >= since:
                    out.append(o)
    except OSError:
        pass
    return out


def _saved_rows() -> list[dict]:
    csv.field_size_limit(10 ** 9)
    out = []
    with open(EXC / "all_applied_applications_history.csv", encoding="utf-8", errors="replace", newline="") as fh:
        for r in csv.DictReader(fh):
            ts = (r.get("Date Applied") or "").strip()
            if not _STAMP.match(ts):
                continue
            posted = (r.get("Date Posted") or "").strip()
            out.append({"t": P(ts), "id": (r.get("Job ID") or "").strip(), "posted": P(posted) if _STAMP.match(posted) else None,
                        "q": len(re.findall(r"\((?:'|\")", r.get("Questions Found") or ""))})
    out.sort(key=lambda r: r["t"])
    return out


def _failed_cat(reason: str) -> str:
    r = (reason or "").lower()
    if "experience is high" in r:
        return "experience too high"
    if "problem in easy applying" in r:
        return "Easy Apply problem"
    if "bad word" in r:
        return "bad word in the job text"
    if "security clearance" in r:
        return "asks for security clearance"
    if "blacklist" in r:
        return "blacklisted company words"
    return "other"


def _failed_rows(since: str) -> list[dict]:
    csv.field_size_limit(10 ** 9)
    out = []
    with open(EXC / "all_failed_applications_history.csv", encoding="utf-8", errors="replace", newline="") as fh:
        for r in csv.DictReader(fh):
            ts = (r.get("Date Tried") or "").strip()
            if _STAMP.match(ts) and ts >= since:
                out.append({"t": P(ts), "id": (r.get("Job ID") or "").strip(), "cat": _failed_cat(r.get("Assumed Reason") or "")})
    out.sort(key=lambda r: r["t"])
    return out


def _supervisor() -> list[dict]:
    """Sessions (supervisor started -> stopped) with their attempts."""
    sessions, cur = [], None
    rx = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[supervisor\] (.*)$")
    try:
        with open(LOGS / "supervisor.log", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return []
    for ln in lines:
        m = rx.match(ln)
        if not m:
            continue
        t, msg = P(m.group(1)), m.group(2)
        if msg.startswith("started"):
            cur = {"start": t, "stop": None, "attempts": [], "limit_at": None}
            sessions.append(cur)
        elif cur is None:
            continue
        elif msg.startswith("stopped"):
            cur["stop"] = t
        elif "daily Easy Apply limit reached" in msg:
            cur["limit_at"] = t
        elif msg.startswith("yield watchdog") and cur["attempts"]:
            cur["attempts"][-1]["note"] = msg.split(" — ")[0][:140]        # "yield watchdog: +0 applies in 181min (< 1/180min)"
        else:
            m2 = re.match(r"attempt (\d+): launching bot \(saved=(\d+)\)", msg)
            if m2:
                cur["attempts"].append({"n": int(m2.group(1)), "start": t, "end": None, "rc": None, "saved": None, "applied": None, "failed": None})
                continue
            m2 = re.match(r"attempt (\d+): exited rc=(-?\d+) after ([\d.]+)min saved\+(\d+)(?: run_stats\(applied=(\d+), failed=(\d+)\))?", msg)
            if m2 and cur["attempts"]:
                cur["attempts"][-1].update(end=t, rc=int(m2.group(2)), saved=int(m2.group(4)),
                                           applied=int(m2.group(5)) if m2.group(5) else None, failed=int(m2.group(6)) if m2.group(6) else None)
    return [s for s in sessions if s["attempts"]]


def _hardening(since: str) -> dict:
    out = {"reconcile": [], "discard_reload": [], "easy_click": [], "uae_failed": [], "render_miss": []}
    try:
        with open(LOGS / "unattended-hardening.log", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if ln[:10] < since or not _STAMP.match(ln):      # later lines of multi-line entries (stack traces) carry no stamp
                    continue
                t = P(ln[:19])
                if "official reconcile" in ln:
                    out["reconcile"].append(t)
                elif ln[20:].startswith("discard:") and "reloading" in ln:
                    out["discard_reload"].append(t)
                elif "easy-apply click candidates" in ln:
                    out["easy_click"].append(t)
                elif ln[20:].startswith("uae: pass failed"):
                    out["uae_failed"].append(t)
                elif ln[20:].startswith("render: card still has no link"):
                    out["render_miss"].append(t)         # written by the card-render step once it is deployed
    except OSError:
        pass
    return out


def _text_files() -> dict[int, str]:
    out = {}
    for f in glob.glob(str(LOGS / "supervised-attempt-*.log")):
        m = re.search(r"attempt-(\d+)\.log$", f)
        if m:
            out[int(m.group(1))] = f
    return out


def _resolve(sup: list[dict], now: datetime, ev: list[datetime]) -> None:
    """Give every session and attempt an end. A supervisor that was killed (the pause tool, a reboot) never wrote 'stopped' or 'exited',
    and an open-ended row would swallow every later day: such a row ends at its last logged event instead."""
    files = _text_files()

    def last_event(a: datetime, b: datetime) -> datetime:
        i = bisect.bisect_left(ev, b) - 1
        return ev[i] if i >= 0 and ev[i] >= a else a

    for i, s in enumerate(sup):
        nxt = sup[i + 1]["start"] if i + 1 < len(sup) else None
        last = s["attempts"][-1]
        running = False
        if s["stop"] is None and nxt is None and last["end"] is None:
            f = files.get(last["n"])
            try:
                running = f is not None and now.timestamp() - os.path.getmtime(f) < 900      # the bot writes its text log all the time
            except OSError:
                running = False
        if s["stop"]:
            s["_end"] = s["stop"]
        elif running:
            s["_end"] = now
        else:
            s["_end"] = last_event(s["start"], nxt or now + timedelta(days=1)) + timedelta(minutes=1)
            if nxt:
                s["_end"] = min(s["_end"], nxt)
        s["_running"] = running
        for j, a in enumerate(s["attempts"]):
            if a["end"]:
                a["_end"] = a["end"]
            elif j + 1 < len(s["attempts"]):
                a["_end"] = s["attempts"][j + 1]["start"]
            else:
                a["_end"] = s["_end"]
            a["running"] = bool(running and j == len(s["attempts"]) - 1)


# ------------------------------------------------------------------ text log: one compact summary per run
_BANNER = b"IF YOU HAVE MORE THAN 10 TABS"
_ANCHOR_B = re.compile(rb"^(?:Title gate|Filter): skipping .*Job ID: (\d+)!")
_NEXT_B = re.compile(rb"^Click Failed! Didn't find '(?:Next|Review|Done|Submit application)'")
_SAVED_B = re.compile(rb"^Successfully saved .*Job ID: (\d+)")
VARS = ["click_fail", "discard_extra", "next_fail", "trying", "page", "easy_click", "ai_answer", "prev_rejected", "already_applied"]
_scan_cache: dict = {}
_EMPTY: dict = {}


def _scan(path: str) -> list[dict]:
    """The runs (banner to banner) of one supervised-attempt text log, read as a stream (the largest file is 16 MB). Per run: totals of the
    line types that cost time, the ids of the jobs it saved, and, for the latest runs, every skip line with the counts seen since the
    previous skip line (the timestamps come later, from the jsonl logs, by job id)."""
    st = os.stat(path)
    sig = (st.st_mtime_ns, st.st_size)
    hit = _scan_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    runs, cur, seg = [], None, collections.Counter()
    with open(path, "rb") as fh:
        for raw in fh:
            head = raw[:4]
            if head == b"IF Y" and raw.startswith(_BANNER):
                cur = {"anchors": [], "totals": collections.Counter(), "saved_ids": []}
                runs.append(cur)
                seg = collections.Counter()
                continue
            if cur is None:
                continue
            kind = None
            if head == b"Titl" or head == b"Filt":
                m = _ANCHOR_B.match(raw)
                if m:
                    cur["anchors"].append(("title" if head == b"Titl" else "badge", m.group(1).decode(), dict(seg) if seg else _EMPTY))
                    seg = collections.Counter()
                continue
            if head == b"Job-":
                if raw.startswith(b"Job-card click failed"):
                    kind = "click_fail"
            elif head == b"Clic":
                if raw.startswith(b"Click Failed! Didn't find 'Discard'"):
                    kind = "discard_fail"
                elif _NEXT_B.match(raw):
                    kind = "next_fail"
            elif head == b"Tryi":
                if raw.startswith(b"Trying to Apply to"):
                    kind = "trying"
            elif head == b">-> ":
                if raw.startswith(b">-> Now on Page"):
                    kind = "page"
            elif head == b"[una":
                if raw.startswith(b"[unattended] easy-apply click candidates"):
                    kind = "easy_click"
            elif head == b"AI A":
                if raw.startswith(b"AI Answered received"):
                    kind = "ai_answer"
            elif head == b"Skip":
                if raw.startswith(b"Skipping previously rejected"):
                    kind = "prev_rejected"
            elif head == b"Alre":
                if raw.startswith(b"Already applied to"):
                    kind = "already_applied"
            elif head == b"Succ":
                m = _SAVED_B.match(raw)
                if m:
                    cur["saved_ids"].append(m.group(1).decode())
            if kind:
                seg[kind] += 1
                cur["totals"][kind] += 1
    for i, r in enumerate(runs):
        t = r["totals"]
        t["discard_extra"] = max(0, t.get("discard_fail", 0) - 2 * t.get("click_fail", 0))
        if i < len(runs) - 12:
            r["anchors"] = []          # old runs are never aligned to timestamps (the title-check log starts on COVERAGE_FROM)
    if len(_scan_cache) > 8:
        _scan_cache.clear()
    _scan_cache[path] = (sig, runs)
    return runs


def _pair_runs(sup: list[dict]) -> tuple[dict, int]:
    """attempt launch time -> its run in the text log. Both are in the same order, so they pair from the end of each file. Checked against
    the number of 'Successfully saved' lines (the supervisor logs saved+N for every attempt); a pair that disagrees is dropped."""
    files = _text_files()
    by_n: dict[int, list[dict]] = collections.defaultdict(list)
    for s in sup:
        for a in s["attempts"]:
            by_n[a["n"]].append(a)
    out, dropped = {}, 0
    for n, alist in by_n.items():
        if n not in files:
            continue
        runs = _scan(files[n])
        k = min(len(alist), len(runs))
        shaky = len(runs) != len(alist)
        for a, r in zip(alist[-k:], runs[-k:]):
            if a["start"] < PAIR_SAFE_FROM:
                continue                    # September: attempt-2's log is one run short, so no September pairing is trusted
            if a["saved"] is not None and abs(a["saved"] - len(r["saved_ids"])) > 1:
                dropped += 1
                continue
            out[a["start"]] = r
    return out, dropped


# ------------------------------------------------------------------ cost fit
def _nnls(A: list[list[float]], y: list[float], iters: int = 3000) -> list[float]:
    m, k = len(A), len(A[0])
    x = [0.0] * k
    r = list(y)
    col2 = [sum(A[i][j] ** 2 for i in range(m)) or 1e-9 for j in range(k)]
    for _ in range(iters):
        moved = 0.0
        for j in range(k):
            g = sum(A[i][j] * r[i] for i in range(m)) / col2[j]
            nx = max(0.0, x[j] + g)
            d = nx - x[j]
            if d:
                for i in range(m):
                    r[i] -= A[i][j] * d
                x[j] = nx
                moved += abs(d)
        if moved < 1e-6:
            break
    return x


def _design(u: tuple) -> list[float]:
    """One row of the fit: which kind of skip line ended the interval (title or badge), then the counts of the costly lines inside it."""
    _, c, _, _, kind = u
    return [1.0 if kind == "title" else 0.0, 1.0 if kind == "badge" else 0.0] + [float(c.get(v, 0)) for v in VARS]


def _fit(rows: list[tuple]) -> dict:
    """rows: (seconds, counts, t0, t1, kind of the skip line that ends it). Trimmed non-negative least squares:
    seconds = base_title or base_badge + sum(cost[line] * count[line])."""
    use = rows
    x = [0.0] * (len(VARS) + 2)
    dropped = 0
    for _ in range(3):
        if len(use) < 30:
            return {"ok": False, "n": len(use)}
        A = [_design(u) for u in use]
        y = [u[0] for u in use]
        x = _nnls(A, y)
        res = [yi - sum(a * b for a, b in zip(row, x)) for row, yi in zip(A, y)]
        sd = statistics.pstdev(res)
        keep = [u for u, rr in zip(use, res) if abs(rr) < 3 * sd]
        dropped += len(use) - len(keep)
        use = keep
    A = [_design(u) for u in use]
    y = [u[0] for u in use]
    yhat = [sum(a * b for a, b in zip(row, x)) for row in A]
    ybar = statistics.mean(y)
    ss_tot = sum((yi - ybar) ** 2 for yi in y) or 1.0
    ss_res = sum((yi - yh) ** 2 for yi, yh in zip(y, yhat))
    return {"ok": True, "n": len(use), "dropped": dropped, "r2": round(1 - ss_res / ss_tot, 3),
            "sd": round(statistics.pstdev([a - b for a, b in zip(y, yhat)]), 1), "base_title": x[0], "base_badge": x[1], "cost": dict(zip(VARS, x[2:]))}


def _align(sup: list[dict], since: str, paired: dict, now: datetime) -> dict:
    """Give the text log timestamps through job ids; return the aligned intervals and, per attempt, how many skip lines were matched."""
    anch: dict[tuple, list[datetime]] = collections.defaultdict(list)
    for name, kind in (("linkedin_filter_skips.jsonl", "badge"), ("linkedin_title_skips.jsonl", "title")):
        for o in _jsonl(name, since):
            anch[(kind, str(o.get("job_id")))].append(P(o["ts"]))
    for v in anch.values():
        v.sort()
    intervals, per_attempt = [], {}
    for s in sup:
        for a in s["attempts"]:
            run = paired.get(a["start"])
            if run is None or a["start"].strftime("%Y-%m-%d") < since:
                continue
            end = a["_end"]
            last, acc, matched, seen = None, collections.Counter(), 0, 0
            for kind, jid, seg in run["anchors"]:
                seen += 1
                for k, v in seg.items():
                    acc[k] += v
                cand = [t for t in anch.get((kind, jid), []) if a["start"] <= t <= end + timedelta(seconds=90)]
                if not cand:
                    continue
                ts = cand[0]
                anch[(kind, jid)].remove(ts)
                matched += 1
                if last is not None and 0 <= (ts - last).total_seconds() < GAP_CAP:
                    c = dict(acc)
                    c["discard_extra"] = max(0, c.get("discard_fail", 0) - 2 * c.get("click_fail", 0))
                    intervals.append(((ts - last).total_seconds(), c, last, ts, kind))
                last, acc = ts, collections.Counter()
            per_attempt[a["start"]] = {"anchors": matched, "seen": seen, "coverage": round(matched / seen, 2) if seen else 0.0}
    return {"intervals": intervals, "per_attempt": per_attempt}


# ------------------------------------------------------------------ what the code itself says (read live, so the page cannot go stale)
def _settings() -> list[dict]:
    out: list[dict] = []

    def grab(path: Path, pattern: str, fmt, name: str, what: str) -> None:
        try:
            m = re.search(pattern, path.read_text(encoding="utf-8", errors="replace"), re.M)
            if m:
                out.append({"name": name, "value": fmt(m), "what": what, "source": path.name})
        except OSError:
            pass

    local, sup, search, settings, ai = LI / "run_linkedin_local.py", LI / "run_linkedin_supervised.py", LI / "config" / "search.py", LI / "config" / "settings.py", LI / "runAiBot.py"
    grab(local, r"^_JOB_GAP\s*=\s*\(([\d.]+),\s*([\d.]+)\)", lambda m: f"{float(m.group(1)):g} to {float(m.group(2)):g} s, random",
         "Pacing wait before every card that passes the title check", "The bot sleeps this long before it opens a job card, whatever the card turns out to be. Average 14 s.")
    grab(local, r"^_REC_EVERY\s*=\s*(\d+)\s*\*\s*(\d+)", lambda m: f"every {int(m.group(1)) * int(m.group(2)) // 60} min",
         "Official count check", "The bot reads LinkedIn's own count of submitted applications this often, so its own tally cannot drift.")
    grab(ai, r"wait_span_click\(driver, 'Discard', (\d+)\)", lambda m: f"{m.group(1)} s", "Wait for the Discard button after a form is left",
         "Each button search is capped; a missing button costs its full cap, and the bot's wrapper tries once more with 3 s added.")
    grab(ai, r"wait_span_click\(driver, \"Review\", (\d+)", lambda m: f"{m.group(1)} s", "Wait for the Review button", "Same cap rule; the form's Next button is searched the same way.")
    grab(ai, r"wait_span_click\(driver, \"Submit application\", (\d+)", lambda m: f"{m.group(1)} s", "Wait for the Submit button", "After Submit the bot also waits up to the same time for Done.")
    grab(search, r"^switch_number\s*=\s*(\d+)", lambda m: f"{m.group(1)} jobs", "Jobs read per search term before the next term", "A term is left after this many cards were handled.")
    grab(settings, r"^click_gap\s*=\s*(\d+)", lambda m: f"{m.group(1)} s", "Extra gap after a click", "Zero: no extra wait is added to clicks.")
    grab(sup, r"^MAX_RESTARTS\s*=\s*(\d+)", lambda m: f"{m.group(1)} per session", "Restarts the supervisor allows after the bot exits",
         "The bot exits when its search terms are done (code 0); the supervisor starts it again until this budget is used.")
    grab(sup, r"cooldown (\d+)s, then restart", lambda m: f"{m.group(1)} s", "Pause before each restart", "Then the UAE pass, the sign-in check and the search terms start over.")
    grab(sup, r"^RAM_WAIT_MAX_MIN\s*=\s*(\d+)", lambda m: f"up to {m.group(1)} min", "Wait for free memory before a start", "A start waits for enough free RAM, then launches anyway.")
    try:
        txt = sup.read_text(encoding="utf-8", errors="replace")
        w, a = re.search(r"^YIELD_WINDOW_MIN\s*=\s*(\d+)", txt, re.M), re.search(r"^YIELD_MIN_APPLIES\s*=\s*(\d+)", txt, re.M)
        if w and a:
            out.append({"name": "Zero-yield watchdog", "value": f"under {a.group(1)} save in {w.group(1)} min", "source": sup.name,
                        "what": "A run that has saved fewer than this after that long is stopped, and the supervisor waits for a human instead of restarting (exit code 1)."})
    except OSError:
        pass
    try:
        tree = ast.parse(search.read_text(encoding="utf-8", errors="replace"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "search_terms" for t in node.targets):
                out.append({"name": "Search terms", "value": f"{len(ast.literal_eval(node.value))} terms", "source": search.name,
                            "what": "The bot works through them in order; when all are done the run ends (exit code 0) and the supervisor restarts it."})
    except (OSError, ValueError, SyntaxError):
        pass
    return out


# ------------------------------------------------------------------ the analysis
def _pct(v: list[float], q: float) -> float:
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))] if v else 0.0


def _dist(v: list[float]) -> dict:
    if not v:
        return {"n": 0}
    return {"n": len(v), "median": statistics.median(v), "mean": statistics.mean(v), "p90": _pct(v, .9), "max": max(v), "total_min": sum(v) / 60}


def _latest_before(times: list[datetime], t: datetime) -> datetime | None:
    i = bisect.bisect_right(times, t) - 1
    return times[i] if i >= 0 else None


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


def _count_key(t: datetime) -> str:
    return (t - RESET).strftime("%Y-%m-%d")


def compute() -> dict:
    now = datetime.now()
    saved = _saved_rows()
    sup = _supervisor()
    failed = _failed_rows("2026-09-01")
    passes_by: dict[str, list[datetime]] = collections.defaultdict(list)
    for o in _jsonl("linkedin_filter_passes.jsonl", COVERAGE_FROM):
        passes_by[str(o["job_id"])].append(P(o["ts"]))
    for v in passes_by.values():
        v.sort()
    pass_times = sorted(t for v in passes_by.values() for t in v)
    badge = sorted(P(o["ts"]) for o in _jsonl("linkedin_filter_skips.jsonl", COVERAGE_FROM))
    title = sorted(P(o["ts"]) for o in _jsonl("linkedin_title_skips.jsonl", COVERAGE_FROM))
    uae_rows = _jsonl("linkedin_region_cycles.jsonl", "2026-10-01")
    hard = _hardening("2026-10-01")
    ai_n = len(_jsonl("ai_answers_india.jsonl", COVERAGE_FROM))
    ev = sorted([r["t"] for r in saved if r["t"] >= datetime(2026, 9, 1)] + [r["t"] for r in failed] + pass_times + badge + title
                + [t for v in hard.values() for t in v])
    _resolve(sup, now, ev)
    paired, dropped_pairs = _pair_runs(sup)

    def pass_for(job_id: str, t: datetime) -> datetime | None:
        p = _latest_before(passes_by.get(job_id, []), t)
        return p if p is not None and 0 <= (t - p).total_seconds() < 600 else None

    # ---- measured stage times: a job passes the badge check -> saved / rejected
    flow_saved, flow_by_q, flow_fail, flow_of = [], collections.defaultdict(list), collections.defaultdict(list), {}
    for r in saved:
        if r["t"] < P(COVERAGE_FROM):
            continue
        p = pass_for(r["id"], r["t"])
        if p:
            d = (r["t"] - p).total_seconds()
            flow_of[(r["id"], r["t"])] = d
            flow_saved.append(d)
            flow_by_q["0-3 questions" if r["q"] <= 3 else "4-6 questions" if r["q"] <= 6 else "7+ questions"].append(d)
    for r in failed:
        if r["t"] >= P(COVERAGE_FROM):
            p = pass_for(r["id"], r["t"])
            if p:
                d = (r["t"] - p).total_seconds()
                flow_of[(r["id"], r["t"])] = d
                flow_fail[r["cat"]].append(d)

    # ---- gap before each kind of logged event (measured, inside one run)
    evs = [(t, "title") for t in title] + [(t, "badge") for t in badge] + [(t, "pass") for t in pass_times]
    evs += [(r["t"], "saved") for r in saved if r["t"] >= P(COVERAGE_FROM)] + [(r["t"], "fail") for r in failed if r["t"] >= P(COVERAGE_FROM)]
    evs.sort()
    gap_by, title_clean = collections.defaultdict(list), []
    for a, b in zip(evs, evs[1:]):
        g = (b[0] - a[0]).total_seconds()
        if g < GAP_CAP:
            gap_by[b[1]].append(g)
            if a[1] == "title" and b[1] == "title" and g < 10:
                title_clean.append(g)       # two title rejects in a row: nothing else happened in between, so this is the check itself

    # ---- text-log alignment and the per-line cost fit
    al = _align(sup, COVERAGE_FROM, paired, now)
    form_ev = sorted((t, d) for (_, t), d in flow_of.items())      # the forms are measured, so their seconds are taken out of the intervals first
    form_t = [t for t, _ in form_ev]

    def forms_between(t0: datetime, t1: datetime) -> float:
        return sum(d for _, d in form_ev[bisect.bisect_right(form_t, t0):bisect.bisect_right(form_t, t1)])

    fit = _fit([(max(0.0, dt - forms_between(t0, t1)), c, t0, t1, kind) for dt, c, t0, t1, kind in al["intervals"]])
    cost, bt, bb = fit.get("cost", {}), fit.get("base_title", 0.0), fit.get("base_badge", 0.0)

    # ---- restart gaps and the UAE pass (measured)
    restart_gaps = [(s["attempts"][j + 1]["start"] - a["end"]).total_seconds() for s in sup for j, a in enumerate(s["attempts"][:-1])
                    if a["end"] and a["start"] >= PAIR_SAFE_FROM]
    restart_gaps = [g for g in restart_gaps if 0 <= g < 3600]
    uae_all = [{"start": P(o["start"]), "end": P(o["end"]), "minutes": round((P(o["end"]) - P(o["start"])).total_seconds() / 60, 1), "cards": o.get("cards"),
                "visits": o.get("visits"), "applied": o.get("applied") or 0, "mode": o.get("mode")} for o in uae_rows if o.get("end")]

    # ---- one row per LinkedIn count (05:30 IST -> 05:30 IST)
    first_day = (min(s["start"] for s in sup) - RESET).date()
    last_day = (now - RESET).date()
    days = []
    d = first_day
    while d <= last_day:
        w0 = datetime.combine(d, datetime.min.time()) + RESET
        w1 = w0 + timedelta(days=1)
        d += timedelta(days=1)
        clips = [(max(s["start"], w0), min(s["_end"], w1), s) for s in sup if s["start"] < w1 and s["_end"] > w0]
        if not clips:
            continue
        first, last_end = min(c[0] for c in clips), max(c[1] for c in clips)
        saved_rows = [r for r in saved if w0 <= r["t"] < w1]
        failed_rows = [r for r in failed if w0 <= r["t"] < w1]
        times = [r["t"] for r in saved_rows]
        active = sum((b - a).total_seconds() for a, b, _ in clips) / 60
        wall = (last_end - first).total_seconds() / 60
        reach = {k: round((times[k - 1] - first).total_seconds() / 60) for k in (10, 20, 30, 35) if len(times) >= k}
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
        atts, restart_s, cnts, cover = [], 0.0, collections.Counter(), []
        dec = {"title": sum(1 for t in title if w0 <= t < w1), "badge": sum(1 for t in badge if w0 <= t < w1), "pass": sum(1 for t in pass_times if w0 <= t < w1)}
        for s in sup:
            for j, a in enumerate(s["attempts"]):
                ov = _overlap(a["start"], a["_end"], w0, w1)
                if ov <= 0:
                    continue
                frac = min(1.0, ov / max(1.0, (a["_end"] - a["start"]).total_seconds()))     # a run that crosses 05:30 is split by time
                r = paired.get(a["start"])
                pa = al["per_attempt"].get(a["start"])
                tot = dict(r["totals"]) if r else {}
                atts.append({"n": a["n"], "start": a["start"], "end": None if a["running"] else a["_end"], "running": a["running"], "carry": a["start"] < w0,
                             "share": round(frac, 2), "minutes": round((a["_end"] - a["start"]).total_seconds() / 60, 1), "rc": a["rc"], "saved": a["saved"],
                             "note": a.get("note", ""), "click_fail": tot.get("click_fail"), "opened": tot.get("trying")})
                if r:
                    for k, v in tot.items():
                        cnts[k] += v * frac
                if pa:
                    cover.append(pa["coverage"])
                if j + 1 < len(s["attempts"]) and a["end"] and w0 <= a["end"] < w1 and a["start"] >= PAIR_SAFE_FROM:
                    restart_s += max(0.0, (s["attempts"][j + 1]["start"] - a["end"]).total_seconds())
        atts.sort(key=lambda x: x["start"])
        uae = [u for u in uae_all if w0 <= u["start"] < w1]
        uae_m = sum(u["minutes"] for u in uae)
        covered = bool(cover) and fit.get("ok") and d - timedelta(days=1) >= datetime.strptime(COVERAGE_FROM, "%Y-%m-%d").date()
        budget = None
        if covered:
            fl = sum(flow_of.get((r["id"], r["t"]), 0) for r in saved_rows) / 60
            ff = sum(flow_of.get((r["id"], r["t"]), 0) for r in failed_rows) / 60
            est = [("Clicking cards that were not drawn yet", (cnts["click_fail"] * cost.get("click_fail", 0) + cnts["discard_extra"] * cost.get("discard_extra", 0)) / 60),
                   ("Opening cards: pacing wait, click, read", cnts["trying"] * cost.get("trying", 0) / 60),
                   ("Cards seen before: pacing wait, then skipped", (cnts["prev_rejected"] * cost.get("prev_rejected", 0) + cnts["already_applied"] * cost.get("already_applied", 0)) / 60),
                   ("Moving between cards and small waits", (dec["title"] * bt + dec["badge"] * bb + cnts["next_fail"] * cost.get("next_fail", 0)
                                                        + cnts["ai_answer"] * cost.get("ai_answer", 0) + cnts["easy_click"] * cost.get("easy_click", 0)) / 60),
                   ("Changing result pages", cnts["page"] * cost.get("page", 0) / 60)]
            idle = max(0.0, wall - active)
            meas = [("Easy Apply forms that were saved", fl), ("Easy Apply attempts that were rejected", ff), ("UAE pass", uae_m),
                    ("Gaps while the bot restarts", restart_s / 60), ("Stopped between runs", idle)]
            fixed, e_sum, scale = sum(m for _, m in meas), sum(m for _, m in est), 1.0
            if fixed + e_sum > wall and e_sum > 0:
                scale = max(0.0, (wall - fixed) / e_sum)      # the UAE pass runs the same code, so its lines sit in the same text log
            parts = [(n, m * scale, "estimated") for n, m in est] + [(n, m, "measured") for n, m in meas]
            parts.append(("Not explained", max(0.0, wall - sum(p[1] for p in parts)), "remainder"))
            budget = {"parts": [{"name": n, "min": round(m, 1), "how": h} for n, m, h in parts], "wall": round(wall, 1), "scale": round(scale, 2)}
        days.append({
            "key": w0.strftime("%Y-%m-%d"), "label": w0.strftime("%a %d %b"), "start": first, "end": last_end,
            "running": any(s["_running"] for _, _, s in clips) and last_end >= now - timedelta(minutes=3),
            "wall": round(wall, 1), "active": round(active, 1), "idle": round(max(0.0, wall - active), 1),
            "saved": len(times), "per_hour": round(len(times) / (active / 60), 2) if active > 5 else 0.0,
            "first_save_min": round((times[0] - first).total_seconds() / 60) if times else None, "reach": reach,
            "tail_min": round((last_end - times[34]).total_seconds() / 60, 1) if len(times) >= 35 else None,
            "gap_median": round(statistics.median(gaps), 1) if gaps else 0, "gap_p90": round(_pct(gaps, .9), 1) if gaps else 0,
            "gap_max": round(max(gaps), 1) if gaps else 0, "gaps": gaps, "saved_times": times, "saved_min": [(t - first).total_seconds() / 60 for t in times],
            "failed_times": [r["t"] for r in failed_rows], "failed": dict(collections.Counter(r["cat"] for r in failed_rows)),
            "decision_times": sorted(t for src in (title, badge, pass_times) for t in src if w0 <= t < w1),
            "decisions": dec, "discard_extra": round(cnts["discard_extra"]), "next_fail": round(cnts["next_fail"]), "ai_answers": round(cnts["ai_answer"]),
            "prev_rejected": round(cnts["prev_rejected"]), "already_applied": round(cnts["already_applied"]),
            "attempts": atts, "runs": len(atts), "uae": uae, "uae_minutes": round(uae_m, 1), "uae_applied": sum(u["applied"] for u in uae),
            "unrendered": round(cnts["click_fail"]), "opened": round(cnts["trying"]), "pages": round(cnts["page"]), "has_text": bool(cnts),
            "limit": any(s["limit_at"] and w0 <= s["limit_at"] < w1 for _, _, s in clips), "covered": bool(covered), "budget": budget,
        })

    # ---- per-count history of the text log (reaches back to 1 Oct): how many cards could not be clicked, per saved application
    hist = [{"key": x["key"], "label": x["start"].strftime("%d %b"), "runs": x["runs"], "minutes": round(x["active"]), "saved": x["saved"], "click_fail": x["unrendered"],
             "opened": x["opened"], "per_saved": round(x["unrendered"] / x["saved"], 1) if x["saved"] >= 10 else None,
             "per_hour": round(x["unrendered"] / (x["active"] / 60), 1) if x["active"] >= 60 else None} for x in days if x["has_text"]]

    # ---- what a card costs when nothing is wrong (measured medians), then the what-if for each covered count
    n_t, n_b, n_p = len(title), len(badge), len(pass_times)
    t_title = statistics.mean(title_clean) if title_clean else 1.0
    t_badge = statistics.median(gap_by["badge"]) if gap_by["badge"] else 21.0
    t_pass = (statistics.median(gap_by["pass"]) if gap_by["pass"] else 21.0) + (statistics.mean(flow_saved + [x for v in flow_fail.values() for x in v]) if flow_saved else 16.0)
    avg_card = (n_t * t_title + n_b * t_badge + n_p * t_pass) / max(1, n_t + n_b + n_p)
    rate = len([1 for r in saved if r["t"] >= P(COVERAGE_FROM)]) / max(1, n_t + n_b + n_p)      # saved per card decided
    for dd in days:
        dd["what_if"] = None
        b = dd["budget"]
        if b and 35 in dd["reach"] and dd["wall"] > 0:
            unr = next((p["min"] for p in b["parts"] if p["name"].startswith("Clicking cards")), 0.0)
            kwn = next((p["min"] for p in b["parts"] if p["name"].startswith("Cards seen")), 0.0)
            new_s = dd["unrendered"] * (avg_card + 1.0)       # +1 s: drawing the card first
            known_n = dd["prev_rejected"] + dd["already_applied"]
            share = min(1.0, dd["reach"][35] / dd["wall"])
            save1 = max(0.0, unr - new_s / 60)                # change 1: draw every card before reading it
            save2 = max(0.0, kwn - known_n * 1.0 / 60)        # change 2: skip a card the bot has seen before at once, ~1 s, before the pacing wait
            dd["what_if"] = {"unrendered_min": round(unr, 1), "if_min": round(new_s / 60, 1), "to35_now": dd["reach"][35],
                             "to35_without": round(max(0.0, dd["reach"][35] - save1 * share)),
                             "known_n": known_n, "known_min": round(kwn, 1), "known_if_min": round(known_n / 60, 1),
                             "to35_known": round(max(0.0, dd["reach"][35] - save2 * share)),
                             "to35_both": round(max(0.0, dd["reach"][35] - (save1 + save2) * share)),
                             "extra_cards": dd["unrendered"], "extra_saves": round(dd["unrendered"] * rate)}

    # ---- long gaps and what the text log counted in them; bands for the timeline
    ivs = al["intervals"]
    bursts = [{"seconds": round(dt), "from": t0, "to": t1, "unrendered": c.get("click_fail", 0), "opened": c.get("trying", 0), "pages": c.get("page", 0)}
              for dt, c, t0, t1, _ in sorted(ivs, key=lambda r: -r[0])[:10]]
    bands = [{"from": t0, "to": t1, "unrendered": c.get("click_fail", 0)} for dt, c, t0, t1, _ in ivs if c.get("click_fail", 0) >= 8]
    big = [(dt, c) for dt, c, _, _, _ in ivs if c.get("click_fail", 0) >= 10]
    burst_summary = {"intervals": len(ivs), "aligned_min": round(sum(dt for dt, *_ in ivs) / 60), "burst_n": len(big),
                     "burst_min": round(sum(dt for dt, _ in big) / 60), "burst_median_s": round(statistics.median([dt for dt, _ in big])) if big else 0,
                     "burst_cards_median": round(statistics.median([c.get("click_fail", 0) for _, c in big])) if big else 0}

    # ---- freshness, hour profile, trend, eras, funnel
    fresh = [(r["t"] - r["posted"]).total_seconds() / 3600 for r in saved
             if r["posted"] and r["t"] >= now - timedelta(days=21) and 0 <= (r["t"] - r["posted"]).total_seconds() < 86400 * 60]
    recent = [x for x in days if x["saved"] >= 3][-7:]
    hours = list(range(5, 24)) + list(range(0, 5))
    heat, dec_hour = [], collections.Counter()
    for x in recent:
        row = collections.Counter(t.hour for t in x["saved_times"])
        heat.append({"label": x["label"], "cells": [row.get(h, 0) for h in hours]})
        for t in x["decision_times"]:
            dec_hour[t.hour] += 1
    full = [x for x in days if 35 in x["reach"]]
    first_title_key = _count_key(title[0]) if title else "9999-99-99"

    def era(key: str) -> str:
        return "A" if key < BADGE_RULE_FROM else "B" if key < first_title_key else "C"

    hist_of = {h["key"]: h for h in hist}
    eras = []
    dd_ = lambda s, back=0: (datetime.strptime(s, "%Y-%m-%d") - timedelta(days=back)).strftime("%d %b")
    for code, name, rule in (("A", "Before the activity-badge rule", f"up to {dd_(BADGE_RULE_FROM, 1)}"),
                             ("B", "Badge rule on", f"{dd_(BADGE_RULE_FROM)} to {dd_(first_title_key, 1) if title else 'now'}"),
                             ("C", "Title check on as well", f"from {dd_(first_title_key) if title else 'n/a'}")):
        rows = [x for x in full if era(x["key"]) == code]
        hs = [hist_of[x["key"]] for x in rows if x["key"] in hist_of and hist_of[x["key"]]["per_saved"] is not None]
        eras.append({"code": code, "name": name, "rule": rule, "n": len(rows), "to35": [x["reach"][35] for x in rows],
                     "median_to35": round(statistics.median([x["reach"][35] for x in rows])) if rows else None,
                     "median_per_hour": round(statistics.median([x["per_hour"] for x in rows]), 1) if rows else None,
                     "unrendered_per_saved": round(statistics.median([h["per_saved"] for h in hs]), 1) if hs else None})
    typical = {}
    if full:
        typical = {"n": len(full), "to35": round(statistics.median([x["reach"][35] for x in full])), "best": min(x["reach"][35] for x in full),
                   "worst": max(x["reach"][35] for x in full), "per_hour": round(statistics.median([x["per_hour"] for x in full]), 2), "since": min(x["start"] for x in full)}
    trend = [{"key": x["key"], "label": x["start"].strftime("%d %b"), "to35": x["reach"].get(35), "saved": x["saved"], "wall": x["wall"],
              "to_last": round((x["saved_times"][-1] - x["start"]).total_seconds() / 60) if x["saved_times"] else None, "per_hour": x["per_hour"],
              "era": era(x["key"])} for x in days if x["saved"] >= 20][-45:]
    win = [x for x in days if x["key"] >= COVERAGE_FROM]
    funnel = {"days": len(win), "title": sum(x["decisions"]["title"] for x in win), "badge": sum(x["decisions"]["badge"] for x in win),
              "pass": sum(x["decisions"]["pass"] for x in win), "saved": sum(x["saved"] for x in win),
              "failed": dict(collections.Counter(c for x in win for c, n in x["failed"].items() for _ in range(n))),
              "unrendered": sum(x["unrendered"] for x in win), "opened": sum(x["opened"] for x in win), "pages": sum(x["pages"] for x in win),
              "minutes": round(sum(x["wall"] for x in win)), "avg_card_s": round(avg_card, 1), "saves_per_card": round(rate, 4)}
    return {
        "generated": now, "coverage_from": COVERAGE_FROM, "days": days, "hist": hist, "eras": eras, "typical": typical, "trend": trend, "funnel": funnel,
        "settings": _settings(), "dropped_pairs": dropped_pairs,
        "card_costs": {"title": round(t_title, 1), "badge": round(t_badge, 1), "pass": round(t_pass, 1), "avg": round(avg_card, 1)},
        "flow": {"saved": _dist(flow_saved), "by_q": {k: _dist(v) for k, v in flow_by_q.items()}, "fail": {k: _dist(v) for k, v in flow_fail.items()}},
        "gap_by": {k: _dist(v) for k, v in gap_by.items()}, "title_clean": _dist(title_clean), "restart_gaps": _dist(restart_gaps), "fit": fit, "bursts": bursts, "bands": bands,
        "burst_summary": burst_summary,
        "fresh": {"n": len(fresh), "median_h": round(statistics.median(fresh), 1) if fresh else None,
                  "within": {k: round(100 * sum(1 for x in fresh if x <= k) / len(fresh)) for k in (1, 6, 24, 72)} if fresh else {}},
        "heat": {"hours": hours, "rows": heat, "decisions": [dec_hour.get(h, 0) for h in hours]},
        "hardening": {k: len(v) for k, v in hard.items()}, "ai_answers": ai_n, "uae": uae_all,
    }


# ------------------------------------------------------------------ background refresh
def refresh() -> None:
    with _lock:
        _busy.set()
        try:
            data = compute()
            _cache.update(at=datetime.now(), data=data, error="")
        except Exception as ex:      # keep the last good result and say so on the page
            _cache["error"] = f"{type(ex).__name__}: {ex}"[:300]
        finally:
            _busy.clear()


def _loop() -> None:
    while True:
        refresh()
        time.sleep(600)


_started = False


def start() -> None:
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, name="lispeed", daemon=True).start()


def get() -> tuple[dict | None, datetime | None, str, bool]:
    """(last result, when it was computed, last error, computing right now). Never blocks: the first result takes ~15 s after the server starts."""
    return _cache["data"], _cache["at"], _cache["error"], _busy.is_set()


if __name__ == "__main__":
    t0 = time.time()
    r = compute()
    print("computed in %.1fs | pairs dropped %d" % (time.time() - t0, r["dropped_pairs"]))
    f = r["fit"]
    print("fit:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in f.items() if k != "cost"}, {k: round(v, 1) for k, v in f.get("cost", {}).items()})
    print("title_clean:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r["title_clean"].items()})
    print("card costs:", r["card_costs"], "| funnel avg", r["funnel"]["avg_card_s"], "saves/card", r["funnel"]["saves_per_card"])
    for x in r["days"][-14:]:
        print(x["key"], x["start"].strftime("%a %H:%M"), "->", x["end"].strftime("%a %H:%M"), "| wall %.0f active %.0f idle %.0f" % (x["wall"], x["active"], x["idle"]),
              "| saved", x["saved"], "| %.1f/h" % x["per_hour"], "| reach", x["reach"], "| runs", x["runs"], "| uae", x["uae_minutes"], "| unrendered", x["unrendered"],
              "| tail", x["tail_min"], "| limit", x["limit"], "| running", x["running"])
        if x["budget"]:
            print("    budget:", [(p["name"][:14], p["min"]) for p in x["budget"]["parts"]], "scale", x["budget"]["scale"])
            print("    what-if:", x["what_if"])
    print("typical:", r["typical"])
    for e in r["eras"]:
        print("era", e["code"], e["name"], e["rule"], "| n", e["n"], "| median to35", e["median_to35"], "| per hour", e["median_per_hour"], "| unrendered/saved", e["unrendered_per_saved"], e["to35"])
    print("hist:", [(h["label"], h["runs"], h["saved"], h["click_fail"], h["per_saved"]) for h in r["hist"]])
    print("funnel:", r["funnel"])
    print("restart gaps:", {k: round(v, 1) if isinstance(v, float) else v for k, v in r["restart_gaps"].items()})
    print("settings:", [(s["name"][:30], s["value"]) for s in r["settings"]])
    print("hardening:", r["hardening"], "| uae passes", len(r["uae"]))
