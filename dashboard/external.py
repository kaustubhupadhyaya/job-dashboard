"""Company-site jobs: relevant jobs whose Apply button leads to the employer's own site, which no bot applies to.

Sources (read-only): the two Naukri databases (status 'External (Manual Required)': the job passed the bot's relevance gates but has no
Easy Apply button) and LinkedIn's history file (a company-site link the LinkedIn bot recorded for a job that passed its filters, from the
day the 'actively reviewing' badge became mandatory, 2026-10-13). The LinkedIn bot searches Easy Apply jobs only (config easy_apply_only),
so today it records none; the reader is ready for when it does.

Duplicates are removed when the list is built: the same posting found by both Naukri accounts (same job id), and the same job posted again
(same company and same title), become one row that says how many postings it stands for and who found it. The review file
(state\\external_jobs_review.json, git-ignored) remembers what has been marked Done or Skip, keyed by company+title, so a reviewed job never
comes back when it is found again. The only write this module makes is that file, through mark(). All times are IST.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import quality
import week

JT, NK, LI = week.JT, week.NK, week.LI
REVIEW = JT / "state" / "external_jobs_review.json"
BADGE_RULE_FROM = "2026-10-13"          # LinkedIn links count from the day the 'actively reviewing' badge became a mandatory filter
_lock = threading.Lock()
_li_cache: tuple[tuple, list[dict]] | None = None


# ------------------------------------------------------------------ readers
def _naukri_ext(db: Path, source: str) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        out = []
        for job_id, url, slug, comp, when, notes in con.execute(
                "SELECT job_id, job_url, job_title, company_name, application_date, notes FROM applied_jobs "
                "WHERE status LIKE 'External%'"):
            title, company, location, exp = week._naukri_fields(notes or "", slug or "", comp or "")
            ts = str(when)[:19] if when and re.match(r"^\d{4}-\d{2}-\d{2}", str(when)) else ""   # rows from before dated saves have none
            out.append({"id": f"nk:{job_id}", "ts": ts, "title": title, "company": company, "location": location,
                        "exp": exp, "url": url or "", "source": source})
        return out
    finally:
        con.close()


def _linkedin_ext() -> list[dict]:
    """Company-site links the LinkedIn bot recorded (Date Applied 'Pending', External Job link a real URL) for jobs found since the badge rule."""
    global _li_cache
    path = LI / "all excels" / "all_applied_applications_history.csv"
    st = path.stat()
    sig = (st.st_mtime_ns, st.st_size)
    if _li_cache and _li_cache[0] == sig:
        return _li_cache[1]
    out = []
    csv.field_size_limit(10 ** 9)
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        for r in csv.DictReader(fh):
            link = (r.get("External Job link") or "").strip()
            if not link.startswith("http") or (r.get("Date Applied") or "").strip() != "Pending":
                continue
            posted = (r.get("Date Posted") or "").strip()
            if posted[:10] < BADGE_RULE_FROM:
                continue
            loc = ", ".join(x for x in (r.get("Work Location"), r.get("Work Style")) if x)
            out.append({"id": "li:" + ((r.get("Job ID") or "").strip() or link), "ts": posted[:19].replace("T", " "),
                        "title": r.get("Title") or "", "company": r.get("Company") or "", "location": loc,
                        "exp": (r.get("Experience required") or "").strip()[:22], "url": link, "source": "LinkedIn"})
    _li_cache = (sig, out)
    return out


# ------------------------------------------------------------------ merge + review state
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _key(x: dict) -> str:
    """The same job: same company and same title. A row with no usable company stands on its own id."""
    comp, title = _norm(x["company"]), _norm(x["title"])
    base = f"{comp}|{title}" if comp and comp != "unknown" and title else "id|" + x["id"]
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def _groups() -> tuple[list[dict], list[str], int]:
    """(merged jobs, source errors, raw rows read). Merged: one per key, newest data wins."""
    rows, errors = [], []
    for name, fn in (("Naukri 1", lambda: _naukri_ext(NK / "naukri_jobs.db", "Naukri 1")),
                     ("Naukri 2", lambda: _naukri_ext(NK / "profiles" / "naukri2" / "naukri_jobs.db", "Naukri 2")),
                     ("LinkedIn", _linkedin_ext)):
        try:
            rows += fn()
        except Exception as ex:      # a source that cannot be read is said so on the page, never shown as empty
            errors.append(f"{name}: {type(ex).__name__}: {ex}"[:200])
    merged: dict[str, dict] = {}
    for x in sorted(rows, key=lambda r: r["ts"]):
        k = _key(x)
        g = merged.get(k)
        if g is None:
            merged[k] = {"key": k, "first": x["ts"], "last": x["ts"], "ids": {x["id"]}, "sources": {x["source"]},
                         "title": x["title"], "company": x["company"], "location": x["location"], "exp": x["exp"], "url": x["url"]}
            continue
        g["ids"].add(x["id"]); g["sources"].add(x["source"])
        g["last"] = max(g["last"], x["ts"])
        if x["ts"] >= g["last"]:     # newest posting supplies the link and the text
            g.update(title=x["title"] or g["title"], company=x["company"] or g["company"], location=x["location"] or g["location"],
                     exp=x["exp"] or g["exp"], url=x["url"] or g["url"])
    out = []
    for g in merged.values():
        out.append({**g, "postings": len(g["ids"]), "sources": sorted(g["sources"]), "ids": None})
    return out, errors, len(rows)


def _load_review() -> dict:
    try:
        with open(REVIEW, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_review(data: dict) -> None:
    REVIEW.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="ext_review_", suffix=".json", dir=str(REVIEW.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, REVIEW)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def mark(key: str, state: str) -> bool:
    """state 'done' (applied by hand), 'skip' (not for me) or 'open' (undo). Returns False for anything else."""
    if not re.fullmatch(r"[0-9a-f]{12}", key or "") or state not in ("done", "skip", "open"):
        return False
    with _lock:
        data = _load_review()
        if state == "open":
            data.pop(key, None)
        else:
            g = next((x for x in _groups()[0] if x["key"] == key), None)
            data[key] = {"state": state, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "title": (g or {}).get("title", ""), "company": (g or {}).get("company", "")}
        _save_review(data)
    return True


def report(days: int = 7) -> dict:
    days = max(1, min(int(days), 60))
    groups, errors, raw = _groups()
    review = _load_review()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    open_, closed, older = [], [], 0
    for g in groups:
        rv = review.get(g["key"])
        if rv and rv.get("state") in ("done", "skip"):
            if g["last"] >= cutoff:
                closed.append({**g, "state": rv["state"], "reviewed": rv.get("at", "")})
        elif g["last"] >= cutoff:
            open_.append(g)
        else:
            older += 1
    open_.sort(key=lambda g: g["last"], reverse=True)
    closed.sort(key=lambda g: g["reviewed"], reverse=True)
    li_n = sum(1 for g in groups if "LinkedIn" in g["sources"])
    return {"generated": datetime.now().isoformat(timespec="seconds"), "days": days, "open": open_, "closed": closed,
            "older_open": older, "raw_rows": raw, "merged": len(groups), "duplicates_removed": raw - len(groups),
            "linkedin_links": li_n, "errors": errors}


# ------------------------------------------------------------------ page
_CSS = """.q table.ext th, .q table.ext td { vertical-align: top; }
.q table.ext .when { white-space: nowrap; color: var(--muted); font-size: 12px; }
.q table.ext .tag { font-size: 11px; letter-spacing: .06em; border: 1px solid var(--ink); padding: 0 4px; margin-left: 4px; white-space: nowrap; }
.q table.ext .act { white-space: nowrap; }
.q table.ext .act button { margin-right: 6px; }
.q .tabs { display: flex; flex-wrap: wrap; gap: 4px 16px; }
.q .tabs b { font-weight: 700; }
.q details > summary { cursor: pointer; padding: 10px 0; font-size: 16px; font-weight: 600; }
.q .cnt { color: var(--muted); font-weight: 400; font-size: 14px; margin-left: 8px; }"""

_JS = """<script>
/* Done / Skip / Reopen: one POST to the board, then the row leaves the list (the page re-reads itself every minute anyway). */
document.addEventListener('click', function (e) {
  var b = e.target.closest ? e.target.closest('button[data-mark]') : null;
  if (!b) return;
  var row = b.closest('tr'); b.disabled = true;
  fetch('/api/external/mark', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Board': '1' },
                                body: JSON.stringify({ key: b.getAttribute('data-key'), state: b.getAttribute('data-mark') }) })
    .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); if (row) row.remove(); })
    .catch(function () { b.disabled = false; b.textContent = 'Failed, try again'; });
});
</script>"""


def _nice(ts: str) -> str:
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").strftime("%d %b %H:%M")
    except ValueError:
        return ts or ""


def _row(g: dict, closed: bool) -> str:
    e = html.escape
    title = e(g["title"]) or "(untitled)"
    if g["url"].startswith("http"):
        title = f'<a href="{e(g["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{title}</a>'
    tags = "".join(f'<span class="tag">{e(s)}</span>' for s in g["sources"])
    more = f' <span class="tag">{g["postings"]} postings</span>' if g["postings"] > 1 else ""
    where = " · ".join(x for x in (g["location"], g["exp"]) if x)
    if closed:
        acts = (f'<span class="when">{e(g["state"])} {e(_nice(g["reviewed"]))}</span> '
                f'<button type="button" class="textbtn" data-mark="open" data-key="{g["key"]}">Reopen</button>')
    else:
        acts = (f'<button type="button" class="textbtn" data-mark="done" data-key="{g["key"]}">Done</button>'
                f'<button type="button" class="textbtn" data-mark="skip" data-key="{g["key"]}">Skip</button>')
    return (f'<tr><td class="when">{e(_nice(g["last"]))}</td><td>{title}{tags}{more}</td><td>{e(g["company"])}</td>'
            f'<td>{e(where)}</td><td class="act">{acts}</td></tr>')


def page(days: int = 7) -> bytes:
    e = html.escape
    try:
        r = report(days)
    except Exception as ex:
        return quality._frame(f"<h1>Company-site jobs are unavailable</h1><p>{e(str(ex))}</p>", datetime.now().strftime("%H:%M:%S"),
                              title="Company-site jobs", extra_css=_CSS)
    head = "<thead><tr><th>Found</th><th>Job</th><th>Company</th><th>Where</th><th></th></tr></thead>"
    open_rows = "".join(_row(g, False) for g in r["open"]) or '<tr><td colspan="5">Nothing waiting. Every job found in these days is marked.</td></tr>'
    closed_rows = "".join(_row(g, True) for g in r["closed"]) or '<tr><td colspan="5">Nothing marked in these days.</td></tr>'
    errs = "".join(f'<p class="sign">Cannot read: {e(x)}</p>' for x in r["errors"])
    tabs = " ".join(f"<b>{d} days</b>" if d == r["days"] else f'<a href="/external?days={d}">{d} days</a>' for d in (7, 14, 30))
    li_note = ("" if r["linkedin_links"] else
               " LinkedIn has added none: its bot searches Easy Apply jobs only, so it records no company-site links yet.")
    inner = f"""<header><h1>Company-site jobs</h1>
<p>Relevant jobs whose Apply button leads to the employer's own site. The bots never apply to these; this is the list to go through by hand,
for example at the end of the week. {r['merged']} jobs from {r['raw_rows']} finds: {r['duplicates_removed']} duplicates were merged (the same
posting found by both Naukri accounts, or the same company and title posted again). Mark a job Done or Skip and it never comes back.{e(li_note)}
Computed <span id="computed">{e(r['generated'][11:19])}</span> IST. See also <a href="/week">Week report</a>.</p></header>
{errs}
<section><h2>Waiting for you<span class="cnt">{len(r['open'])} found in the last {r['days']} days{', ' + str(r['older_open']) + ' older ones not shown' if r['older_open'] else ''}</span></h2>
<div class="tabs">Look back: {tabs}</div>
<div class="box"><table class="ext">{head}<tbody>{open_rows}</tbody></table></div></section>
<section><details id="closed"><summary>Marked Done or Skip<span class="cnt">{len(r['closed'])} in the last {r['days']} days</span></summary>
<div class="box"><table class="ext">{head}<tbody>{closed_rows}</tbody></table></div></details></section>
{_JS}"""
    return quality._frame(inner, r["generated"][11:19], title="Company-site jobs", extra_css=_CSS)
