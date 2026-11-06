"""Where the dashboard reads its data from.

Resolution order for each root: environment variable, then `config.json`, then a demo fixture.
With no `config.json` the dashboard reads `demo/`, so `python -m dashboard.server` works on a
fresh clone without any bot repo.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "port": 8787,
    "stall_minutes": {"linkedin": 30, "naukri1": 30, "naukri2": 15},
    "refresh_window": ["09:00", "09:55"],
    "start_command": "python -m dashboard.server",
}


def _load_config() -> dict:
    path = Path(os.environ.get("JOB_DASHBOARD_CONFIG") or ROOT / "config.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


CFG = _load_config()
DEMO = (os.environ.get("JOB_DASHBOARD_DEMO", "").lower() in ("1", "true", "yes")
        or bool(CFG.get("demo")) or not CFG)

DEMO_ROOT = ROOT / "demo"


def _path(value, fallback: Path) -> Path:
    p = Path(str(value)).expanduser() if value else fallback
    if not p.is_absolute():
        p = ROOT / p
    return p


if DEMO:
    JT = _path(os.environ.get("JOB_DASHBOARD_JT") or CFG.get("job_tracker"), DEMO_ROOT / "job-tracker")
    NK = _path(os.environ.get("JOB_DASHBOARD_NK") or CFG.get("naukri"), DEMO_ROOT / "naukri")
    LI = _path(os.environ.get("JOB_DASHBOARD_LI") or CFG.get("linkedin"), DEMO_ROOT / "linkedin")
else:
    home = Path.home() / "GitHub"
    JT = _path(os.environ.get("JOB_DASHBOARD_JT") or CFG.get("job_tracker"), home / "job-tracker")
    NK = _path(os.environ.get("JOB_DASHBOARD_NK") or CFG.get("naukri"), home / "Naukri-autoapply-bot-for-claude")
    LI = _path(os.environ.get("JOB_DASHBOARD_LI") or CFG.get("linkedin"), home / "FORKED_Auto_job_applier_linkedIn")

BOARDS = _path(
    os.environ.get("JOB_DASHBOARD_BOARDS") or CFG.get("boards"),
    (ROOT / "boards.json") if (ROOT / "boards.json").is_file() else DEMO_ROOT / "boards.json",
)

SETTINGS = {**DEFAULTS, **{k: CFG.get(k, v) for k, v in DEFAULTS.items() if k in CFG}}
if "port" in CFG:
    SETTINGS["port"] = int(CFG["port"])
if os.environ.get("JOB_DASHBOARD_PORT"):
    SETTINGS["port"] = int(os.environ["JOB_DASHBOARD_PORT"])
