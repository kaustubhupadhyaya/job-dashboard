"""Autonomous job-application tracker.

Single source of truth: the xlsx named by xlsx_path in tracker_config.json

Sheets:
  Applications    - one row per real application (dedup by Source+Job ID)
  Daily Totals    - per-day counts (LinkedIn / Naukri / Instahyre / Gulf / day total / cumulative)
  Skills Summary  - per-skill application counts (all-time + today)
  Reconciliation  - periodic official-count vs tracker-count tallies

Commands:
  sync                                            rebuild xlsx from both bots
  official --source S --count N --method M [...]  append a reconciliation row
  status                                          print one-line summary

No AI required. Idempotent. Safe while bots run: if the xlsx is locked
(open in Excel), new rows are parked in pending_rows.jsonl and merged on the
next successful sync.
"""
import argparse
import contextlib
import csv
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, date

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "tracker_config.json")
SKILLS_PATH = os.path.join(BASE, "skill_tags.json")

APPLICATIONS_COLS = [
    "Date", "Time", "Source", "Job ID", "Title", "Company",
    "Skill Tags", "Primary Skill", "Status", "Job URL", "Notes",
]
DAILY_COLS = ["Date", "LinkedIn Applied", "Naukri Applied", "Instahyre Applied", "Gulf Applied",
              "Day Total", "Cumulative Total", "Last Sync"]
# Every source that is not one of these three is a Gulf portal (its own label: NaukriGulf, Bayt, ...).
NAMED_SOURCES = ("LinkedIn", "Naukri", "Instahyre")
SKILLS_COLS = ["Skill", "Applied Count", "Today Count", "Last Updated"]
RECON_COLS = ["Timestamp", "Source", "Official Count", "Tracker Count",
              "Delta", "Method / Note"]


def log(msg):
    print(f"[tracker] {msg}", flush=True)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        log(f"could not load {os.path.basename(path)}: {e!r}")
        return default


CONFIG = load_json(CONFIG_PATH, {})
SKILLS = load_json(SKILLS_PATH, {"rules": []})
XLSX = CONFIG.get("xlsx_path", os.path.join(os.path.dirname(BASE),
                                            "job_applications_tracker.xlsx"))
TRACK_FROM = CONFIG.get("track_from", "2026-09-10")
PENDING = os.path.join(BASE, "pending_rows.jsonl")
LOCKFILE = XLSX + ".lock"


@contextlib.contextmanager
def xlsx_lock(timeout):
    """Exclusive lock around read-modify-write of the xlsx. 2026-09-30: the LinkedIn supervisor
    and both Naukri bots ran syncs concurrently with no lock, so one could read a half-written
    file and the next write would drop rows. The OS releases the lock if the process dies."""
    import msvcrt
    fh = open(LOCKFILE, "a+")
    deadline = time.time() + timeout
    while True:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if time.time() > deadline:
                fh.close()
                raise TimeoutError(f"tracker lock still held after {timeout}s")
            time.sleep(1)
    try:
        yield
    finally:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        fh.close()


# ------------------------------------------------------------------ skill tags
def tag_skills(title, description):
    title = (title or "").lower()
    description = (description or "").lower()
    scores = {}
    for idx, rule in enumerate(SKILLS.get("rules", [])):
        score = 0
        for kw in rule.get("kw", []):
            pat = r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])"
            if title and re.search(pat, title):
                score += 3
            if description and re.search(pat, description):
                score += 1
        if score:
            scores[rule["tag"]] = (score, -idx)
    ordered = sorted(scores, key=lambda t: (scores[t][0], scores[t][1]), reverse=True)
    return ordered


def apply_tags(title, description):
    ordered = tag_skills(title, description)
    if not ordered:
        return "Untagged", "Untagged"
    return ", ".join(ordered[:3]), ordered[0]


# --------------------------------------------------------------- src: linkedin
def read_linkedin(src):
    path = src.get("path")
    rows = []
    if not path or not os.path.exists(path):
        log(f"linkedin csv not found: {path}")
        return rows
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                when = (r.get("Date Applied") or "").strip()
                if len(when) < 10 or not when[:4].isdigit():
                    continue
                d = when[:10]
                if d < TRACK_FROM:
                    continue
                title = (r.get("Title") or "").strip()
                desc = (r.get("About Job") or "")[:3000]
                tags, primary = apply_tags(title, desc)
                rows.append({
                    "date": d,
                    "time": when[11:19] if len(when) >= 19 else "",
                    "source": "LinkedIn",
                    "job_id": (r.get("Job ID") or "").strip(),
                    "title": title,
                    "company": (r.get("Company") or "").strip(),
                    "tags": tags,
                    "primary": primary,
                    "status": "Applied",
                    "url": (r.get("Job Link") or "").strip(),
                    "notes": "",
                })
    except Exception as e:
        log(f"linkedin read error: {e!r}")
    return rows


# ------------------------------------------------------------------ src: naukri
def read_naukri(src):
    path = src.get("path")
    rows = []
    if not path or not os.path.exists(path):
        log(f"naukri db not found: {path}")
        return rows
    wanted = {s.lower() for s in src.get("applied_statuses",
                                         ["applied", "applied (easy apply)"])}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("PRAGMA table_info(applied_jobs)")
        cols = {r[1] for r in cur.fetchall()}
        has_company = "company_name" in cols
        has_date = "application_date" in cols
        q = "SELECT job_id, job_url, job_title, status, notes" \
            + (", company_name" if has_company else ", NULL") \
            + (", application_date" if has_date else ", NULL") \
            + " FROM applied_jobs"
        cur.execute(q)
        for job_id, job_url, job_title, status, notes, company, app_date in cur.fetchall():
            if (status or "").strip().lower() not in wanted:
                continue
            when = (app_date or "").strip() if app_date else ""
            if len(when) >= 10:
                d = when[:10]
            else:
                d = datetime.now().strftime("%Y-%m-%d")
            if d < TRACK_FROM:
                continue
            title = (job_title or "").strip()
            desc = (notes or "")
            if title.lower() in ("applied job", "") and job_url:
                slug = job_url.rstrip("/").split("/")[-1]
                title = slug.replace("-", " ")[:120] or title
            tags, primary = apply_tags(title, desc)
            rows.append({
                "date": d,
                "time": when[11:19] if len(when) >= 19 else "",
                "source": "Naukri",
                "job_id": str(job_id or "").strip(),
                "title": title,
                "company": (company or "").strip(),
                "tags": tags,
                "primary": primary,
                "status": "Applied",
                "url": (job_url or "").strip(),
                "notes": "",
            })
        con.close()
    except Exception as e:
        log(f"naukri read error: {e!r}")
    return rows


# --------------------------------------------------------------- src: instahyre
def read_instahyre(src):
    """state/instahyre_<n>.db written by instahyre_apply.py; only server-confirmed applications."""
    path = src.get("path")
    rows = []
    if not path or not os.path.exists(path):
        log(f"instahyre db not found: {path}")
        return rows
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for job_id, title, company, keywords, url, ts in con.execute(
                "SELECT job_id, title, company, keywords, url, ts FROM applications WHERE decision='applied'"):
            d = (ts or "")[:10]
            if d < TRACK_FROM:
                continue
            title = (title or "").strip()
            try:
                desc = " ".join(json.loads(keywords or "[]"))
            except ValueError:
                desc = ""
            tags, primary = apply_tags(title, desc)
            rows.append({
                "date": d,
                "time": (ts or "")[11:19],
                "source": src.get("label", "Instahyre"),
                "job_id": str(job_id),
                "title": title,
                "company": (company or "").strip(),
                "tags": tags,
                "primary": primary,
                "status": "Applied",
                "url": (url or "").strip(),
                "notes": "",
            })
        con.close()
    except Exception as e:
        log(f"instahyre read error: {e!r}")
    return rows


# --------------------------------------------------------------- src: gulf portals
def read_gulf(src):
    """state/<portal>_apply.db of a Gulf portal script (the portal contract: an `applications` table whose
    verified=1 rows were confirmed in the site's own Applied list). Only confirmed rows count, so an
    application the site never listed does not inflate the totals. Accepts the NaukriGulf column
    `location` and Instahyre's `locations`."""
    path = src.get("path")
    rows = []
    if not path or not os.path.exists(path):
        log(f"gulf db not found: {path}")      # normal until that portal has run once
        return rows
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cols = {r[1] for r in con.execute("PRAGMA table_info(applications)")}
        loc = "location" if "location" in cols else ("locations" if "locations" in cols else "NULL")
        for job_id, title, company, location, url, ts in con.execute(
                f"SELECT job_id, title, company, {loc}, url, ts FROM applications "
                "WHERE decision='applied' AND verified=1"):
            d = (ts or "")[:10]
            if d < TRACK_FROM:
                continue
            title = (title or "").strip()
            tags, primary = apply_tags(title, "")
            rows.append({
                "date": d,
                "time": (ts or "")[11:19],
                "source": src.get("label", "Gulf"),
                "job_id": str(job_id),
                "title": title,
                "company": (company or "").strip(),
                "tags": tags,
                "primary": primary,
                "status": "Applied",
                "url": (url or "").strip(),
                "notes": (location or "").strip(),
            })
        con.close()
    except Exception as e:
        log(f"gulf read error: {e!r}")
    return rows


def collect_source_rows():
    rows = []
    for name, src in (CONFIG.get("sources") or {}).items():
        stype = src.get("type")
        if stype == "linkedin_csv":
            rows.extend(read_linkedin(src))
        elif stype == "naukri_sqlite":
            rows.extend(read_naukri(src))
        elif stype == "instahyre_sqlite":
            rows.extend(read_instahyre(src))
        elif stype == "gulf_sqlite":
            rows.extend(read_gulf(src))
    return rows


# ------------------------------------------------------------------ pending I/O
def read_pending():
    rows = []
    if os.path.exists(PENDING):
        try:
            with open(PENDING, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except Exception as e:
            log(f"pending read error: {e!r}")
    return rows


def write_pending(rows):
    try:
        with open(PENDING, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        log(f"pending write error: {e!r}")
        return False


def clear_pending():
    try:
        if os.path.exists(PENDING):
            os.remove(PENDING)
    except Exception:
        pass


# ------------------------------------------------------------------ excel I/O
def read_xlsx_rows(ws_name):
    try:
        from openpyxl import load_workbook
        wb = load_workbook(XLSX, read_only=True, data_only=True)
        if ws_name not in wb.sheetnames:
            wb.close()
            return None
        ws = wb[ws_name]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        return rows
    except FileNotFoundError:
        return None
    except Exception as e:
        log(f"xlsx read error: {e!r}")
        return None


def write_workbook(app_rows, recon_rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E78")
    wrap = Alignment(wrap_text=False, vertical="top")

    def _head(ws, cols, widths):
        ws.append(cols)
        for c, w in zip(ws[1], widths):
            c.font = header_font
            c.fill = header_fill
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

    # Applications
    ws = wb.active
    ws.title = "Applications"
    _head(ws, APPLICATIONS_COLS, [12, 10, 10, 16, 44, 26, 30, 18, 10, 60, 24])
    for r in app_rows:
        ws.append([r[k] for k in
                   ["date", "time", "source", "job_id", "title", "company",
                    "tags", "primary", "status", "url", "notes"]])

    # Daily Totals
    by_day = defaultdict(lambda: defaultdict(int))
    for r in app_rows:
        by_day[r["date"]][r["source"]] += 1
    ws = wb.create_sheet("Daily Totals")
    _head(ws, DAILY_COLS, [12, 16, 16, 18, 14, 12, 18, 20])
    cum = 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for d in sorted(by_day):
        li = by_day[d].get("LinkedIn", 0)
        nk = by_day[d].get("Naukri", 0)
        ih = by_day[d].get("Instahyre", 0)
        gulf = sum(v for s, v in by_day[d].items() if s not in NAMED_SOURCES)
        # 2026-10-02: Day Total and Cumulative used to add only LinkedIn + Naukri, so every Instahyre
        # application (and now every Gulf one) was missing from them.
        day = li + nk + ih + gulf
        cum += day
        ws.append([d, li, nk, ih, gulf, day, cum, now_str])

    # Skills Summary
    today = datetime.now().strftime("%Y-%m-%d")
    all_time, today_time = defaultdict(int), defaultdict(int)
    for r in app_rows:
        for tag in [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]:
            all_time[tag] += 1
            if r["date"] == today:
                today_time[tag] += 1
    ws = wb.create_sheet("Skills Summary")
    _head(ws, SKILLS_COLS, [22, 16, 14, 20])
    for tag in sorted(all_time, key=lambda t: -all_time[t]):
        ws.append([tag, all_time[tag], today_time.get(tag, 0), now_str])

    # Reconciliation
    ws = wb.create_sheet("Reconciliation")
    _head(ws, RECON_COLS, [20, 10, 15, 14, 8, 60])
    for r in recon_rows:
        ws.append(r)

    # Atomic: a sync killed mid-save (the callers used to kill it after 120 s) could truncate
    # the only copy. Write a temp file, then swap it in; os.replace raises PermissionError
    # while Excel holds the xlsx open, which cmd_sync already turns into pending rows.
    tmp = XLSX + ".tmp"
    try:
        wb.save(tmp)
        os.replace(tmp, XLSX)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_existing():
    """Return (app_rows, recon_rows) from xlsx, normalized to dict/list forms."""
    app_rows, recon_rows = [], []
    rows = read_xlsx_rows("Applications")
    if rows and len(rows) > 1:
        hdr = [str(h) for h in rows[0]]
        keys = ["date", "time", "source", "job_id", "title", "company",
                "tags", "primary", "status", "url", "notes"]
        pos = {h: i for i, h in enumerate(hdr)}
        for r in rows[1:]:
            if not r or not r[0]:
                continue
            app_rows.append({
                k: (str(r[pos[c]]) if pos.get(c) is not None
                    and r[pos[c]] is not None else "")
                for k, c in zip(keys, APPLICATIONS_COLS)
            })
    rows = read_xlsx_rows("Reconciliation")
    if rows and len(rows) > 1:
        for r in rows[1:]:
            if r and r[0]:
                recon_rows.append([("" if v is None else v) for v in r])
    return app_rows, recon_rows


def dedup(rows):
    seen = set()
    out = []
    for r in sorted(rows, key=lambda x: (x.get("date", ""), x.get("time", ""))):
        key = (r.get("source", ""), r.get("job_id", ""))
        if not key[1]:
            key = (r.get("source", ""), r.get("url", ""), r.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ------------------------------------------------------------------ commands
def cmd_sync(_args):
    try:
        with xlsx_lock(timeout=5):
            return _sync_locked()
    except TimeoutError:
        log("another sync is running; skipping (it reads the same sources)")
        return 0


def _sync_locked():
    existing_apps, recon_rows = load_existing()
    pending = read_pending()
    source = collect_source_rows()
    merged = dedup(existing_apps + pending + source)
    try:
        write_workbook(merged, recon_rows)
        clear_pending()
        log(f"synced: {len(merged)} applications "
            f"({len(source)} read from bots, {len(pending)} merged from pending)")
        return 0
    except PermissionError as e:
        write_pending([r for r in merged if r not in existing_apps])
        log(f"xlsx locked (open in Excel?) — parked rows in pending_rows.jsonl ({e!r})")
        return 0
    except Exception as e:
        log(f"sync failed: {e!r}")
        return 1


def cmd_official(args):
    try:
        with xlsx_lock(timeout=110):
            return _official_locked(args)
    except TimeoutError as e:
        log(f"official tally skipped: {e}")
        return 1


def _official_locked(args):
    count = None if str(args.count).lower() in ("unknown", "none", "") else int(args.count)
    existing_apps, recon_rows = load_existing()
    pending = read_pending()
    tracker_count = sum(1 for r in existing_apps + pending
                        if r.get("source", "").lower() == args.source.lower())
    delta = "" if count is None else count - tracker_count
    recon_rows.append([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        args.source,
        "unknown" if count is None else count,
        tracker_count,
        delta,
        args.method or "",
    ])
    try:
        write_workbook(existing_apps, recon_rows)
        log(f"official tally: {args.source} official={count} tracker={tracker_count} "
            f"delta={delta} ({args.method})")
        return 0
    except Exception as e:
        log(f"official tally write failed: {e!r}")
        return 1


def cmd_status(_args):
    apps, recon = load_existing()
    today = datetime.now().strftime("%Y-%m-%d")
    def split(rows):
        out = {"LinkedIn": 0, "Naukri": 0, "Instahyre": 0, "Gulf": 0}
        for r in rows:
            s = r.get("source")
            out[s if s in NAMED_SOURCES else "Gulf"] += 1
        return out
    allt, tod = split(apps), split([r for r in apps if r.get("date") == today])
    parts = lambda c: " / ".join(f"{k} {v}" for k, v in c.items())
    log(f"total: {sum(allt.values())} ({parts(allt)}) | today: {sum(tod.values())} ({parts(tod)})")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Job application tracker")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("sync")
    o = sub.add_parser("official")
    o.add_argument("--source", required=True)
    o.add_argument("--count", required=True)
    o.add_argument("--method", default="")
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "official":
        return cmd_official(args)
    if args.cmd == "status":
        return cmd_status(args)
    return cmd_sync(args)


if __name__ == "__main__":
    sys.exit(main())
