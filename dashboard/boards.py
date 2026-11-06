"""Pluggable board registry: each applier is described in boards.json, and this module reads its files.

Read-only. Nothing here imports, starts or edits applier code; a missing repo or status file is
reported as installed=false with a message, never raised. All times are IST (UTC+05:30).
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import paths
except ImportError:     # imported as part of the package (python -m dashboard.boards)
    from . import paths

ROOT = Path(__file__).resolve().parent.parent
CONFIG = paths.BOARDS
IST = timezone(timedelta(hours=5, minutes=30))
FIELDS = ("id", "name", "repo", "path", "status", "applications", "control", "enabled")
_TS_KEYS = ("ts", "time", "timestamp", "applied_at", "datetime", "date")


# ------------------------------------------------------------------ config
def _resolve(base: Path, value) -> str:
    """A relative path is relative to boards.json's folder; an absolute one is kept as written."""
    if not value:
        return ""
    p = Path(str(value)).expanduser()
    if not p.is_absolute():
        p = base / p
    return str(p)


def load_boards(path: str | Path | None = None) -> list[dict]:
    """Read boards.json (or `path`). A missing or malformed config is an empty registry, not an error."""
    cfg = Path(path) if path else CONFIG
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("boards") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    base = cfg.resolve().parent
    out = []
    for raw in items:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        board = {k: raw.get(k) for k in FIELDS}
        board["id"] = str(raw["id"])
        board["name"] = str(raw.get("name") or board["id"])
        board["repo"] = str(raw.get("repo") or "")
        board["path"] = _resolve(base, raw.get("path"))
        board["status"] = str(raw.get("status") or "")
        board["applications"] = str(raw.get("applications") or "")
        board["control"] = str(raw.get("control") or "")
        board["enabled"] = bool(raw.get("enabled", True))
        out.append(board)
    return out


def _repo(board: dict) -> Path | None:
    p = board.get("path")
    return Path(p) if p else None


def _file(board: dict, key: str) -> Path | None:
    """The `status` / `applications` / `control` path, relative to the board's own folder."""
    repo, value = _repo(board), board.get(key)
    if repo is None or not value:
        return None
    p = Path(str(value)).expanduser()
    if not p.is_absolute():
        p = repo / p
    return p


# ------------------------------------------------------------------ status
def _int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_status(board: dict) -> dict:
    """The board's own status.json. Missing repo or status -> installed=false with a clear message."""
    repo = _repo(board)
    missing = {"installed": False, "state": "not installed", "last_run": None, "cap": None, "message": ""}
    if repo is None:
        return {**missing, "message": "No local path configured for this board."}
    if not repo.is_dir():
        return {**missing, "message": f"Repo not found: {repo}"}
    path = _file(board, "status")
    if path is None:
        return {**missing, "message": "No status file configured for this board."}
    if not path.is_file():
        return {**missing, "message": f"Status file not found: {path}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"installed": True, "state": "unknown", "last_run": None, "cap": None,
                "message": f"Status file unreadable: {e}"}
    if not isinstance(data, dict):
        data = {}
    accounts = data.get("accounts")
    accounts = [a for a in accounts.values() if isinstance(a, dict)] if isinstance(accounts, dict) else []
    state = data.get("state") or data.get("status")
    last = data.get("last_run") or data.get("last_run_at") or data.get("lastRun")
    cap = _int(data.get("cap", data.get("daily_cap")))
    applied = _int(data.get("applied_today"))
    message = str(data.get("message") or "")
    if accounts:
        # A board with several accounts (Instahyre runs two) reports each under `accounts`.
        states = list(dict.fromkeys(str(a.get("state") or "unknown") for a in accounts))
        if not state:
            state = ", ".join(states)
            if len(accounts) > 1:
                state = f"{state} ({len(accounts)} accounts)"
        if not last:
            times = [_parse_ts(a.get("last_run") or a.get("last_run_at")) for a in accounts]
            times = [t for t in times if t is not None]
            if times:
                last = max(times).isoformat(timespec="seconds")
        caps = [_int(a.get("cap", a.get("daily_cap"))) for a in accounts]
        caps = [n for n in caps if n is not None]
        if cap is None and caps:
            cap = sum(caps)
        todays = [_int(a.get("applied_today")) for a in accounts]
        todays = [n for n in todays if n is not None]
        if applied is None and todays:
            applied = sum(todays)
        if not message:
            msgs = list(dict.fromkeys(str(a["message"]) for a in accounts if a.get("message")))
            message = "; ".join(msgs)
    state = state or "unknown"
    if not last:
        # A board that does not write a run time still has a status file mtime; that is the last write.
        try:
            last = datetime.fromtimestamp(path.stat().st_mtime, IST).isoformat(timespec="seconds")
        except OSError:
            last = None
    return {"installed": True, "state": str(state), "last_run": str(last) if last else None,
            "cap": cap, "applied_today": applied, "message": message}


# ------------------------------------------------------------------ applications
def _parse_ts(value) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), IST)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"      # UTC at the edge only; the result is converted to IST below
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)      # appliers write local (IST) times
    return dt.astimezone(IST)


def _stamp(row: dict) -> float:
    for key in _TS_KEYS:
        dt = _parse_ts(row.get(key))
        if dt is not None:
            return dt.timestamp()
    return 0.0


def _read_rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    try:
        if suffix in (".jsonl", ".ndjson"):
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        elif suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data.get("applications", []) if isinstance(data, dict) else data
        elif suffix in (".csv", ".tsv"):
            with path.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh, delimiter="\t" if suffix == ".tsv" else ","))
        else:
            return []
    except (OSError, ValueError, csv.Error):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def read_applications(board: dict, limit: int = 200) -> list[dict]:
    """Newest-first application records from the board's applications log (JSONL, JSON or CSV/TSV).

    A missing or unreadable file is an empty list; the page says 'no log' instead of a false zero.
    """
    path = _file(board, "applications")
    if path is None or not path.is_file():
        return []
    rows = _read_rows(path)
    rows.sort(key=_stamp, reverse=True)
    return rows[:max(0, int(limit))]


def _has_applications(board: dict) -> bool:
    path = _file(board, "applications")
    return bool(path and path.is_file())


# ------------------------------------------------------------------ control
def _control(board: dict) -> dict:
    path = _file(board, "control")
    if path is None:
        return {"path": None, "present": False, "enabled": None}
    present = path.is_file()
    enabled = None
    if present:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                enabled = data.get("enabled")
            elif isinstance(data, bool):
                enabled = data
        except (OSError, ValueError):
            enabled = None
    return {"path": str(path), "present": present, "enabled": enabled if isinstance(enabled, bool) else None}


# ------------------------------------------------------------------ summary
def summary(path: str | Path | None = None) -> dict:
    """Everything /api/boards returns: one row per board, read from files on every call."""
    today = datetime.now(IST).date()
    rows = []
    for board in load_boards(path):
        status = read_status(board)
        apps_available = _has_applications(board)
        apps = read_applications(board, 100_000) if apps_available else []
        if apps_available:
            applied_today, applied_from = 0, "log"
            for app in apps:
                state = str(app.get("status") or app.get("decision") or "").strip().lower()
                if state and state not in ("applied", "accepted", "submitted"):
                    continue
                dt = None
                for key in _TS_KEYS:
                    dt = _parse_ts(app.get(key))
                    if dt is not None:
                        break
                if dt is not None and dt.date() == today:
                    applied_today += 1
        elif status.get("applied_today") is not None:
            applied_today, applied_from = status["applied_today"], "status"
        else:
            applied_today, applied_from = 0, None
        rows.append({
            "id": board["id"],
            "name": board["name"],
            "repo": board["repo"],
            "path": board["path"],
            "enabled": board["enabled"],
            "installed": status["installed"],
            "state": status["state"],
            "last_run": status["last_run"],
            "applied_today": applied_today,
            "applied_today_from": applied_from,
            "applications_available": apps_available,
            "cap": status["cap"],
            "control": _control(board),
            "message": status["message"],
        })
    return {"generated": datetime.now(IST).isoformat(timespec="seconds"), "boards": rows}
