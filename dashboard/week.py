"""Week report: every application each applier saved in the last 7 days, per day and as a list.

Read-only and computed from the bots' own files on each request (the Naukri and Instahyre databases, LinkedIn's history file),
like the quality page. The page frame, theme and live refresh are quality._frame's, so this page also updates itself.
"Saved" means what the bot recorded as submitted (Naukri 'Applied (Easy Apply)', Instahyre 'applied', LinkedIn Easy Apply).
A source that cannot be read is said so on its row; it is never shown as zero.
All times and days here are IST (midnight to midnight), like everything on this PC. LinkedIn's own count resets at 05:30 IST, not at
midnight, so a calendar day can hold the end of one count and the start of the next (44 on 17 Oct was 9 before 05:30 plus 35 after):
the page marks such days. The limit itself (35, banner on the next try) is read from the bot's history and the
LINKEDIN_DAILY_LIMIT_* files, not assumed: see _li_limit.
"""
from __future__ import annotations

import csv
import html
import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import quality

try:
    import paths
except ImportError:     # imported as part of the package (python -m dashboard.week)
    from . import paths

JT, NK, LI = paths.JT, paths.NK, paths.LI
ORDER = ["naukri1", "naukri2", "instahyre1", "instahyre2", "linkedin"]
CAPS = {"naukri1": 50, "naukri2": 50, "instahyre1": 30, "instahyre2": 30, "linkedin": 35}   # the board's lane caps
LI_RESET = timedelta(hours=5, minutes=30)   # LinkedIn's Easy Apply count resets at 05:30 IST (measured, not assumed: see _li_limit)
_EXP = re.compile(r"(\d{1,2})\s*(?:-|to)\s*(\d{1,2})")


# ------------------------------------------------------------------ readers (one list of dicts per applier)
def _naukri_fields(notes: str, slug: str, company_col: str) -> tuple[str, str, str, str]:
    """(title, company, location, experience). Notes are 'Title - City,City - Company - 5 to 8 years of experience at Company'
    when the bot read the job page, else the URL slug 'job listings title words company words cities 5 to 8 ye at Unknown'."""
    body = notes.rsplit(" at ", 1)[0] if " at " in notes else notes
    parts = [p.strip() for p in body.split(" - ")]
    m = _EXP.search(parts[-1]) if parts else None
    if len(parts) >= 4 and m:
        return " - ".join(parts[:-3]), parts[-2], parts[-3], f"{m.group(1)}-{m.group(2)} yrs"
    if len(parts) == 3 and not _EXP.search(notes):
        return parts[0], parts[2], parts[1], ""
    text = re.sub(r"^job listings\s+", "", (slug or notes or "").strip(), flags=re.I)
    text = re.sub(r"\s+at\s+unknown$", "", text, flags=re.I)
    exp = ""
    m = re.search(r"\s(\d{1,2})\s+to\s+(\d{1,2})(?:\s+y\w*)?\s*(?:\d{6,})?\s*$", text)
    if m:
        exp = f"{m.group(1)}-{m.group(2)} yrs"
        text = text[:m.start()]
    company = "" if (company_col or "").strip().lower() in ("", "unknown") else company_col.strip()
    return (text[:1].upper() + text[1:]).strip(), company, "", exp


def _naukri(db: Path, since: str) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        out = []
        for url, slug, comp, when, notes in con.execute(
                "SELECT job_url, job_title, company_name, application_date, notes FROM applied_jobs "
                "WHERE status LIKE 'Applied%' AND application_date >= ? ORDER BY application_date", (since,)):
            title, company, location, exp = _naukri_fields(notes or "", slug or "", comp or "")
            out.append({"ts": str(when)[:19], "title": title, "company": company, "location": location, "exp": exp,
                        "url": url or "", "tag": ""})
        return out
    finally:
        con.close()


def _instahyre(db: Path, since: str) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        return [{"ts": str(ts).replace("T", " ")[:19], "title": title or "", "company": comp or "", "location": locs or "",
                 "exp": "", "url": url or "", "tag": ""}
                for title, comp, locs, url, ts in con.execute(
                    "SELECT title, company, locations, url, ts FROM applications WHERE decision='applied' AND ts >= ? "
                    "ORDER BY ts", (since,))]
    finally:
        con.close()


def _uae_verified_ids() -> set[str]:
    """Job ids the LinkedIn UAE pass verified (they sit in the same history file; they get a UAE tag)."""
    ids: set[str] = set()
    try:
        with open(LI / "logs" / "linkedin_uae_jobs.jsonl", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"verified"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("outcome") == "verified" and o.get("job_id"):
                    ids.add(str(o["job_id"]))
    except OSError:
        pass
    return ids


_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")


_li_cache: tuple[tuple, list[dict]] | None = None


def _li_all(csv_path: Path) -> list[dict]:
    """Every LinkedIn history row with a real timestamp, parsed once per change of the file (the week list and the limit check share it)."""
    global _li_cache
    uae_file = LI / "logs" / "linkedin_uae_jobs.jsonl"
    sig = (csv_path.stat().st_mtime_ns, csv_path.stat().st_size,
           uae_file.stat().st_mtime_ns if uae_file.exists() else 0)
    if _li_cache and _li_cache[0] == sig:
        return _li_cache[1]
    uae = _uae_verified_ids()
    out = []
    with open(csv_path, encoding="utf-8", errors="replace", newline="") as fh:
        for r in csv.DictReader(fh):
            applied = (r.get("Date Applied") or "").strip()
            # 41 old rows (2025 format) hold 'Pending' or a resume name here: only a real timestamp counts
            if not _STAMP.match(applied):
                continue
            loc = ", ".join(x for x in (r.get("Work Location"), r.get("Work Style")) if x)
            out.append({"ts": applied.replace("T", " ")[:19], "title": r.get("Title") or "", "company": r.get("Company") or "",
                        "location": loc, "exp": (r.get("Experience required") or "").strip()[:22],
                        "url": r.get("Job Link") or "", "tag": "UAE" if (r.get("Job ID") or "") in uae else ""})
    _li_cache = (sig, out)
    return out


def _linkedin(csv_path: Path, since: str) -> list[dict]:
    return [x for x in _li_all(csv_path) if x["ts"] >= since]


def _li_period(ts: str) -> str:
    """The date a LinkedIn submission's count started on. The count runs 05:30 IST to 05:30 IST, so a submission at 02:00 still
    used the count that began the day before."""
    return (datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S") - LI_RESET).date().isoformat()


def _li_banners() -> list[datetime]:
    """When LinkedIn showed its 'we limit daily submissions' banner: every LINKEDIN_DAILY_LIMIT_* file starts with that time."""
    out = []
    for p in (LI / "logs").glob("LINKEDIN_DAILY_LIMIT_*"):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                out.append(datetime.strptime(fh.read(19), "%Y-%m-%d %H:%M:%S"))
        except (OSError, ValueError):
            continue
    return sorted(out)


def _li_limit(day_list: list[str]) -> dict:
    """Per LinkedIn count (05:30 IST to 05:30 IST, named by the date it starts): submissions, first and last time, and when the
    banner came (the first one at or after the last submission). 'history' sums up every count that reached the banner, so the
    limit is read from what happened, not assumed."""
    rows = _li_all(LI / "all excels" / "all_applied_applications_history.csv")
    banners = _li_banners()
    per: dict[str, list[datetime]] = {}
    for x in rows:
        per.setdefault(_li_period(x["ts"]), []).append(datetime.strptime(x["ts"], "%Y-%m-%d %H:%M:%S"))

    def window(d: str) -> dict:
        ts = sorted(per.get(d, []))
        if not ts:
            return {"day": d, "n": 0, "first": "", "last": "", "banner": "", "gap_min": None}
        start = datetime.fromisoformat(d) + LI_RESET
        b = next((x for x in banners if start <= x < start + timedelta(days=1) and x >= ts[-1] - timedelta(seconds=60)), None)
        fmt = "%Y-%m-%d %H:%M:%S"
        return {"day": d, "n": len(ts), "first": ts[0].strftime(fmt), "last": ts[-1].strftime(fmt),
                "banner": b.strftime(fmt) if b else "", "gap_min": round((b - ts[-1]).total_seconds() / 60, 1) if b else None}

    reached = [w for w in (window(d) for d in sorted(per)) if w["banner"]]
    dist = Counter(w["n"] for w in reached)
    return {"days": [window(d) for d in day_list],
            "history": {"reached": len(reached), "since": reached[0]["day"] if reached else "", "dist": dict(sorted(dist.items()))}}


SOURCES = {
    "naukri1": lambda since: _naukri(NK / "naukri_jobs.db", since),
    "naukri2": lambda since: _naukri(NK / "profiles" / "naukri2" / "naukri_jobs.db", since),
    "instahyre1": lambda since: _instahyre(JT / "state" / "instahyre_1.db", since),
    "instahyre2": lambda since: _instahyre(JT / "state" / "instahyre_2.db", since),
    "linkedin": lambda since: _linkedin(LI / "all excels" / "all_applied_applications_history.csv", since),
}


def report(days: int = 7, now: dict | None = None) -> dict:
    """`now` is each applier's state in the board's words ({key: {word, text, sign}}); the server passes it, a bare call leaves it out."""
    today = date.today()
    day_list = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    since = day_list[0]
    out = {}
    for key in ORDER:
        err, rows = "", []
        try:
            rows = SOURCES[key](since)
        except Exception as ex:   # say so on the row; a source we cannot read is not a zero
            err = f"{type(ex).__name__}: {ex}"[:200]
        counts = Counter(x["ts"][:10] for x in rows)
        out[key] = {"per_day": [counts.get(d, 0) for d in day_list], "total": len(rows), "cap": CAPS[key], "error": err,
                    "last": max((x["ts"] for x in rows), default=""), "rows": sorted(rows, key=lambda x: x["ts"], reverse=True)}
        if key == "linkedin":
            # days whose calendar count passes the cap: how many of those came before 05:30, while the count of the day before was still running
            early = Counter(x["ts"][:10] for x in rows if x["ts"][11:16] < "05:30")
            out[key]["before_reset"] = {d: early[d] for d in day_list if counts.get(d, 0) > CAPS[key] and early.get(d)}
    try:
        limit = _li_limit(day_list)
    except Exception as ex:
        limit = {"error": f"{type(ex).__name__}: {ex}"[:200]}
    return {"since": since, "days": day_list, "generated": datetime.now().isoformat(timespec="seconds"), "appliers": out,
            "limit": limit, "now": now or {}}


# ------------------------------------------------------------------ page
_WEEK_CSS = """.q table.days th, .q table.days td { white-space: nowrap; }
.q table.days .n { text-align: right; }
.q table.days .zero { color: var(--muted); }
.q table.days .today { font-weight: 700; }
.q table.days .tot { font-weight: 600; }
.q table.days .when { color: var(--muted); font-size: 12px; }
.q table.days tr.sum th, .q table.days tr.sum td { border-top: 2px solid var(--ink); font-weight: 600; }
.q p.foot { color: var(--muted); font-size: 13px; margin: 8px 0 0; }
.q table.now { min-width: 0; }
.q table.now td, .q table.now th { white-space: normal; }
.q table.now td:first-of-type { white-space: nowrap; font-weight: 600; }
.q table.limit .n, .q table.limit .when { white-space: nowrap; }
.q table.list { min-width: 640px; }
.q table.list .when { white-space: nowrap; color: var(--muted); font-size: 12px; }
.q details { border-top: 1px solid var(--grid); }
.q details > summary { cursor: pointer; padding: 10px 0; font-size: 16px; font-weight: 600; }
.q summary .id { display: inline; margin-left: 6px; }
.q .cnt { color: var(--muted); font-weight: 400; font-size: 14px; margin-left: 8px; }
.q .tag { font-size: 11px; letter-spacing: .06em; border: 1px solid var(--ink); padding: 0 4px; margin-left: 4px; }"""


def _ago(ts: str) -> str:
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    m = int((datetime.now() - t).total_seconds() // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    if m < 6 * 60:
        return f"{m // 60} h {m % 60:02d} min ago"
    if m < 48 * 60:
        return f"{m // 60} h ago"
    return f"{m // 1440} days ago"


def _nice(ts: str) -> str:
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").strftime("%d %b %H:%M")
    except ValueError:
        return ts


def _row(x: dict) -> str:
    e = html.escape
    title = e(x["title"]) or "(untitled)"
    if x["url"].startswith("http"):
        title = f'<a href="{e(x["url"], quote=True)}" target="_blank" rel="noopener">{title}</a>'
    tag = f' <span class="tag">{e(x["tag"])}</span>' if x.get("tag") else ""
    return (f'<tr><td class="when">{e(_nice(x["ts"]))}</td><td>{title}{tag}</td><td>{e(x["company"])}</td>'
            f'<td>{e(x["location"])}</td><td>{e(x["exp"])}</td></tr>')


def page(days: int = 7, now: dict | None = None) -> bytes:
    e = html.escape
    try:
        r = report(days, now)
    except Exception as ex:
        return quality._frame(f"<h1>Week report is unavailable</h1><p>{e(str(ex))}</p>", datetime.now().strftime("%H:%M:%S"),
                              title="Week report", extra_css=_WEEK_CSS)
    dl = r["days"]
    heads = "".join(f'<th class="n{" today" if i == len(dl) - 1 else ""}">'
                    f'{"Today" if i == len(dl) - 1 else e(date.fromisoformat(d).strftime("%a %d"))}</th>' for i, d in enumerate(dl))
    trs, sums, notes = [], [0] * len(dl), []
    for key in ORDER:
        a = r["appliers"][key]
        label = f'<th scope="row">{e(quality.NAMES[key])}<span class="id">{e(quality.IDENT[key])}</span></th>'
        if a["error"]:
            trs.append(f'<tr>{label}<td colspan="{len(dl) + 3}" class="sign">Cannot read this applier\'s files: {e(a["error"])}</td></tr>')
            continue
        sums = [s + n for s, n in zip(sums, a["per_day"])]
        br = a.get("before_reset") or {}
        for d, early in br.items():   # a calendar day over the cap: say which part belongs to the count before 05:30
            notes.append(f'{date.fromisoformat(d).strftime("%a %d %b")}: {a["per_day"][dl.index(d)]} = {early} before 05:30 + '
                         f'{a["per_day"][dl.index(d)] - early} after')
        cells = "".join(
            f'<td class="n{" zero" if n == 0 else ""}"' + (f' title="{br[d]} of these were made before 05:30 IST"' if d in br else "") + f'>{n}'
            + ("<sup>*</sup>" if d in br else "") + '</td>'
            for d, n in zip(dl, a["per_day"]))
        last = f'{e(_nice(a["last"]))}, {e(_ago(a["last"]))}' if a["last"] else "nothing in these days"
        trs.append(f'<tr>{label}{cells}<td class="n tot">{a["total"]}</td><td class="n">{a["cap"]}</td><td class="when">{last}</td></tr>')
    total = sum(sums)
    sum_cells = "".join(f'<td class="n{" zero" if n == 0 else ""}">{n}</td>' for n in sums)
    trs.append(f'<tr class="sum"><th scope="row">All appliers</th>{sum_cells}<td class="n tot">{total}</td><td></td><td></td></tr>')
    lists = []
    for key in ORDER:
        a = r["appliers"][key]
        body = "".join(_row(x) for x in a["rows"]) or '<tr><td colspan="5">No applications in these days.</td></tr>'
        lists.append(f'<details id="d-{key}"><summary>{e(quality.NAMES[key])}<span class="id">{e(quality.IDENT[key])}</span>'
                     f'<span class="cnt">{a["total"]} applications</span></summary><div class="box"><table class="list"><thead><tr>'
                     f'<th>Saved</th><th>Role</th><th>Company</th><th>Location</th><th>Exp</th></tr></thead>'
                     f'<tbody>{body}</tbody></table></div></details>')
    first = date.fromisoformat(r["since"]).strftime("%a %d %b")
    try:    # the appliers run only inside the daily window (appliers_window.txt); say so, so a quiet evening is not read as a fault
        import collect
        w = collect.window_info(datetime.now())
    except Exception:
        w = {"configured": False}
    win_note = (f' Since 21 Oct the appliers run only inside the daily window {e(w["start"])} to {e(w["end"])} IST and are stopped outside it, '
                f'so the days before it hold activity at any hour and the days after hold only that window.' if w.get("configured") else "")
    now_trs = []
    for key in ORDER:
        s = (r.get("now") or {}).get(key)
        if s:
            now_trs.append(f'<tr><th scope="row">{e(quality.NAMES[key])}</th><td class="{"sign" if s.get("sign") else ""}">'
                           f'{e(s.get("word", ""))}</td><td>{e(s.get("text", ""))}</td></tr>')
    now_html = (f'<section><h2>Right now</h2><div class="box"><table class="now"><tbody>{"".join(now_trs)}</tbody></table></div></section>'
                if now_trs else "")
    foot = (f'<p class="foot"><sup>*</sup> LinkedIn counts from 05:30 IST, not from midnight, so one day can hold the end of one count and '
            f'the start of the next. {e("; ".join(notes))}.</p>' if notes else "")
    # LinkedIn's limit, read from what happened: one row per count (05:30 IST to 05:30 IST), and a sentence over every count that reached the banner
    lim, cap = r.get("limit") or {}, CAPS["linkedin"]
    if lim.get("error"):
        lim_html = f'<section><h2>LinkedIn\'s daily limit</h2><p class="sign">Cannot read the limit evidence: {e(lim["error"])}</p></section>'
    else:
        lrows = []
        for w in lim.get("days", []):
            dlabel = e(date.fromisoformat(w["day"]).strftime("%a %d %b")) + ", 05:30"
            if not w["n"]:
                lrows.append(f'<tr><th scope="row">{dlabel}</th><td class="n zero">0</td><td colspan="3" class="when">nothing submitted</td></tr>')
                continue
            banner = (f'{e(_nice(w["banner"]))}, {w["gap_min"]:g} min after the last submission' if w["banner"]
                      else "not shown: this count has not reached the limit")
            lrows.append(f'<tr><th scope="row">{dlabel}</th><td class="n">{w["n"]}</td><td class="when">{e(_nice(w["first"]))}</td>'
                         f'<td class="when">{e(_nice(w["last"]))}</td><td class="when">{banner}</td></tr>')
        h, dist = lim.get("history") or {}, (lim.get("history") or {}).get("dist") or {}
        sent = ""
        if dist:
            top = max(dist, key=lambda n: (dist[n], n))
            rest = ", ".join(f"after {n} on {c}" for n, c in dist.items() if n != top)
            sent = (f'Since {date.fromisoformat(h["since"]).strftime("%d %b")} the banner appeared after {h["reached"]} counts: '
                    f'after exactly {top} submissions in {dist[top]} of them' + (f", {rest}." if rest else "."))
        over = [w for w in lim.get("days", []) if w["n"] > cap]
        warn = (f'<p class="sign">The count starting {", ".join(date.fromisoformat(w["day"]).strftime("%a %d %b") for w in over)} held more '
                f'than {cap} submissions, so the cap of {cap} is out of date: check it.</p>' if over else "")
        lim_html = f"""<section><h2>LinkedIn's daily limit</h2>
<p>LinkedIn stops Easy Apply with its "we limit daily submissions" banner on the next try after a count is used up. The count resets
at 05:30 IST, not at midnight, so the rows below start at 05:30 IST and run to 05:30 IST the next morning. {e(sent)} The cap column
says {cap}.</p>{warn}
<div class="box"><table class="days limit"><thead><tr><th>Count starting</th><th class="n">Submitted</th><th>First</th><th>Last</th>
<th>Banner</th></tr></thead><tbody>{''.join(lrows)}</tbody></table></div></section>"""
    inner = f"""<header><h1>Week report</h1>
<p>Every application each applier saved from {e(first)} to now, {len(dl)} days, counted from the bots' own files. Computed
<span id="computed">{e(r['generated'][11:19])}</span>. A zero is not always a fault: LinkedIn rests once its daily limit is
used, the others rest at their cap, and a bot with no new matching jobs saves nothing; "Right now" says which, in the board's
words. Every day is an IST day, midnight to midnight, and every time is IST.{win_note}
See also <a href="/quality">Match quality</a>, <a href="/external">Company-site jobs</a> and <a href="/linkedin-speed">LinkedIn speed</a>.</p></header>
{now_html}
<section><h2>Applications per day</h2><div class="box"><table class="days">
<thead><tr><th>Applier</th>{heads}<th class="n">{len(dl)} days</th><th class="n">Cap/day</th><th>Last saved</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>{foot}</section>
{lim_html}
<section><h2>Every application</h2><div>{''.join(lists)}</div></section>"""
    return quality._frame(inner, r["generated"][11:19], title="Week report", extra_css=_WEEK_CSS)
