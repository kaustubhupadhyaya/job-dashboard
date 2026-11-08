# job-dashboard

A local, read-only dashboard for job-application bots. It reads the files each bot already writes (status JSON, application logs, SQLite databases, logs) and serves a board on `127.0.0.1`. Nothing here applies to a job, starts a browser, or edits a bot.

Standard library only, Python 3.11 or newer.

## Case study

[docs/case-study.md](docs/case-study.md) describes how the four appliers, the board registry, the resume refreshers, the daily window, and the watchdogs fit together, with the verified and unverified parts separated.

## Run the demo

```bash
python -m dashboard.server
```

Open <http://127.0.0.1:8787/>. With no `config.json`, every path points at `demo/`, so the demo starts on a fresh clone with no real bot repository. `demo/boards.json` holds three fake boards with fake status files and application rows.

Useful endpoints:

| Path | What it shows |
|---|---|
| `/` | the board |
| `/api/status` | every bot row as JSON |
| `/boards` | the pluggable board registry page |
| `/api/boards` | board registry as JSON |
| `/quality` | saved applications graded by the fit rules |
| `/week` | applications per day for the last 7 days |
| `/external` | company-site jobs that need a manual application |
| `/linkedin-speed` | where the LinkedIn bot spends its time |

## Point it at real checkouts

```bash
cp config.example.json config.json
cp boards.example.json boards.json
```

Edit the roots in `config.json` (`job_tracker`, `naukri`, `linkedin`) and the board entries in `boards.json`. Each root can also come from `JOB_DASHBOARD_JT`, `JOB_DASHBOARD_NK` and `JOB_DASHBOARD_LI`; the board registry path from `JOB_DASHBOARD_BOARDS`. Relative paths resolve against the repository root.

`config.json`, `boards.json` and `tracker_config.json` are git-ignored because they describe one machine.

## What is read

- the Naukri bot's databases, logs and keepalive state
- the LinkedIn bot's history CSV, logs and region files
- the Gulf applier's SQLite database, progress files and logs
- each board's `status.json` and applications log, as described in `docs/boards.md`

The only write the dashboard makes is `state/external_jobs_review.json`, when a company-site job is marked Done or Skip.

## Fit rules

`/quality` grades saved applications with `portal_kit/fit.py`. The rules live outside the repository, by default at `~/.secrets/fit_rules.json` or the path in `FIT_RULES`. The page says plainly when the rules cannot be read.

## Tracker

`track_job_applications.py` rebuilds one xlsx from the bots' own stores. It is optional and needs `openpyxl`:

```bash
pip install -e ".[tracker]"
cp tracker_config.example.json tracker_config.json
python track_job_applications.py sync
```

## Tests

Continuous integration compiles the Python files and starts the demo, then checks that `/api/boards` returns JSON. There are no unit tests; the demo run is the test.

## Licence

MIT. See `LICENSE`.
