# Case study: four appliers, one read-only board

This is the system-level view of the public job automation suite. It covers four appliers and the dashboard that reads them:

- [`naukri-autoapply`](https://github.com/kaustubhupadhyaya/naukri-autoapply) for Naukri.com
- [`instahyre-autoapply`](https://github.com/kaustubhupadhyaya/instahyre-autoapply) for Instahyre
- [`linkedin-autoapply-public`](https://github.com/kaustubhupadhyaya/linkedin-autoapply-public) for LinkedIn
- [`gulf-autoapply`](https://github.com/kaustubhupadhyaya/gulf-autoapply) for NaukriGulf and employer ATS forms
- [`job-dashboard`](https://github.com/kaustubhupadhyaya/job-dashboard) for the read-only board

The appliers never import each other. They share a file contract and a few habits: dry run first, saved rows as proof, explicit stop codes, and no success claim without the site's own confirmation.

## Why the appliers are separate

| Board | Driver | Apply surface | Durable store | Public licence |
|---|---|---|---|---|
| Naukri | Selenium and Edge | Easy Apply chat drawer | SQLite `naukri_jobs.db` | Source-available, inherited 2022 code unlicensed |
| Instahyre | Edge over the DevTools protocol | Instahyre's matching feed and apply API | `state/status.json` and `state/applications.jsonl` | MIT |
| LinkedIn | Selenium and Chrome | Easy Apply modal | Applied-history CSV and logs | AGPL-3.0 fork |
| Gulf | Edge over CDP and an ATS adapter | NaukriGulf Easy Apply, Workable forms | SQLite per tool | MIT |

The separation follows the work, not the organisation chart:

- The login and session models differ. Naukri uses a Selenium browser profile. Instahyre uses a persistent, already signed-in Edge profile over CDP. LinkedIn reads credentials from the environment or an optional vault. Gulf uses persistent browser profiles and a closed-world facts file for screening answers.
- The apply surface differs. Naukri drives a chat drawer that asks one question at a time. Instahyre posts to the site's own apply endpoint. LinkedIn fills the Easy Apply modal. Gulf both presses NaukriGulf's Easy Apply button and fills employer ATS forms.
- The limits differ. The Naukri runner stops at 50 applied rows for the day. Instahyre defaults to a 30-application daily cap. LinkedIn's Easy Apply count is 35 per 05:30-to-05:30 IST count, measured from its logs. Gulf's NaukriGulf tools share a 40-request daily budget.
- The licences and provenance differ. The LinkedIn repo is an AGPL fork. Naukri is a sanitized copy of a private working repository whose 2022 base is unlicensed. Instahyre and Gulf are MIT. Keeping them apart keeps those obligations separate.
- The failure domains differ. A Naukri daily quota, an Instahyre HTTP 429, a LinkedIn verification wall, or a Gulf challenge should park one pipeline, not all four. Separate processes and separate state make that possible.

The dashboard is the only shared component, and it is read-only. It never applies, starts a browser, or edits an applier.

## The board registry is the seam

The dashboard can show any applier without importing its code. [`docs/boards.md`](https://github.com/kaustubhupadhyaya/job-dashboard/blob/main/docs/boards.md) defines the contract and `dashboard/boards.py` implements it.

`boards.json` is a machine-specific, git-ignored file. Each entry has:

| Field | Meaning |
|---|---|
| `id` | Stable key. An entry without one is skipped. |
| `name` | Display name. Defaults to `id`. |
| `repo` | Public repository URL shown on the page. |
| `path` | Local checkout. Relative paths resolve against `boards.json`'s folder. |
| `status` | Status JSON, relative to `path` or absolute. |
| `applications` | Applications log, relative to `path` or absolute. |
| `control` | Optional control file. |
| `enabled` | Whether the board is meant to run. |

The status contract is deliberately small:

```json
{
  "state": "running",
  "last_run": "2026-11-06T09:14:00+05:30",
  "cap": 30,
  "message": ""
}
```

`status` is accepted as an alias for `state`. `last_run_at` and `lastRun` are also accepted, and a missing run time falls back to the file's modification time. `cap` accepts `daily_cap`. A board with several accounts may nest them under `accounts`. When the top-level fields are absent, the reader combines their states, takes the latest run, sums the caps and `applied_today`, and joins the distinct messages. Missing files return `installed: false` with a message. An unreadable file returns `state: "unknown"` with the read error. Neither crashes the page.

The applications contract accepts JSONL and NDJSON, a JSON list or `{"applications": [...]}`, and CSV or TSV. A record is a timestamp plus any extra fields. The timestamp key may be `ts`, `time`, `timestamp`, `applied_at`, `datetime`, or `date`. Naive values are treated as IST, a `Z` value is converted to IST before counting, and `applied_today` counts records whose IST calendar date is today. A missing or unreadable log reads as an empty list, and the page says "no log" rather than showing a false zero.

The optional control file may be `{"enabled": true}` or a bare `true`/`false`. It is informational. Nothing in the dashboard writes it.

There are two read paths, and the difference matters:

- `/api/status` and the main board use native adapters in `dashboard/collect.py`. They read each applier's own store: Naukri's SQLite database and logs, LinkedIn's history CSV and logs, Instahyre's state files and supervisor log, and Gulf's SQLite databases and progress files. This is where the detailed rows come from.
- `/boards` and `/api/boards` use the generic registry. In the public code, Instahyre is the applier that already writes the status and applications contract. `boards.example.json` lists all four as the target shape, not as a claim that every public applier currently writes `state/status.json`.

The registry reads its config on every request. It is an extension point, not a second database.

## Resume refreshers

Instahyre sorts candidates by profile-updated date, so the public suite includes [`refresh.py`](https://github.com/kaustubhupadhyaya/instahyre-autoapply/blob/main/refresh.py). It re-uploads the same resume once a day. A refresh counts as done only when Instahyre's own profile API reports a new `resume.uploaded_on`. Dry run is the default. Live mode needs both `--live` and `--confirm-live`. A profile already refreshed today is skipped unless `--force` is passed. A logged-out profile exits 42. A pause exits 43. The script writes `refreshed` or `already_done` into `state/status.json` and records `last_success` only on success.

The live setup also runs a separate resume-refresh supervisor for Naukri account 1, Naukri account 2, and both Instahyre accounts. That supervisor and its scripts are not in the five public repositories. The dashboard only reads its log and screenshots. It treats the site's own upload date as the oracle: a row is "Refreshed" only when the site date is today, "Not confirmed" when today's attempt left an older date, "Missed" after the refresh window, and "Needs your login" after exit 42. The public example configuration puts that refresh window at 09:00 to 09:55, inside the wider applier window.

A resume refresh is not the same job as the resume text Naukri uses for AI answers. The Naukri runner caches a profile digest from the resume for question answering. That digest is not a site refresh and does not change the resume on Naukri.

## The 04:00 to 10:00 IST window and control

On the machine that produced this case study, every applier is configured to run only between 04:00 and 10:00 IST. The window lives in a small file read by the dashboard:

```text
start=04:00
end=10:00
```

`window_info()` in [`dashboard/collect.py`](https://github.com/kaustubhupadhyaya/job-dashboard/blob/main/dashboard/collect.py) parses those two lines, treats them as IST, and handles a window that crosses midnight. When the current time is outside the window, `/api/status` changes rows that would otherwise read "applying", "failing", "stalled", "stopped", or "waiting" to "resting", with the schedule in the detail. A quiet board outside the window is a scheduled rest, not a failure. The dashboard also starts its silence clock at the window opening, so the gap before 04:00 does not count as a stall.

The control side has four parts:

- A daily close at 10:00 stops every chain. On the live machine this is a scheduled task that calls a pause tool.
- Start tasks at 04:00 to 04:06 bring the chains back. The live policy also says the launchers refuse to start anything outside the window.
- A pause marker named `APPLIERS_PAUSED_UNTIL.txt` is read by the Gulf guard. Its first line is an ISO time; a time ahead means paused. A missing, past, empty, or unparsable marker blocks nothing. An unreadable marker blocks to be safe.
- Instahyre has its own public [`control.py`](https://github.com/kaustubhupadhyaya/instahyre-autoapply/blob/main/control.py): `pause` writes `state/PAUSED`, `resume` deletes it, and both Instahyre tools check it before doing work.

The window file, the scheduled tasks, and the pause tool are not in the five public repositories. The value 04:00 to 10:00 and the start/close behaviour are verified on this machine, not from a fresh clone.

There is one public inconsistency. [`portal_kit/site_guard.py`](https://github.com/kaustubhupadhyaya/job-dashboard/blob/main/portal_kit/site_guard.py) sets the earliest start for every Gulf tool to 10:00, so a new Gulf browser is refused before 10:00. That would leave no room for a Gulf run inside a 04:00-to-10:00 window. The public repositories do not reconcile the two rules. Treat the Gulf start gate and the all-applier window as separate, verified only where their own files say so.

## Telemetry, watchdogs, deduplication, and failure handling

### What is written

- Naukri writes one JSON line per chat event to `naukri_telemetry_12h.jsonl`: timestamp, format, question, answer, success, and extra data. It also writes a heartbeat log line at least every five minutes in every state, and a `NAUKRI_HEARTBEAT_ON` marker that tells the keepalive the heartbeat is active.
- Instahyre writes `state/status.json` atomically and appends one fsynced row per decision to `state/applications.jsonl`. A row carries `board`, `job_id`, `title`, `company`, `url`, `applied_at`, and `status` (`applied`, `skipped`, or `failed`).
- LinkedIn writes a history CSV, `logs/log.txt`, supervisor logs, per-attempt logs, daily-limit markers, a `NEEDS_HUMAN` marker, a `NETWORK_DOWN` marker, and wall-evidence JSON.
- Gulf writes one SQLite database per tool. Live outcomes only go into the `applications` table. Dry runs do not write there. The shared guard writes a daily request ledger, park markers, a browser lock, and a toast registry.
- The dashboard writes one file: `state/external_jobs_review.json`, when a company-site job is marked Done or Skip. It does not write any applier's state.

### What watches it

Naukri's public runner writes the heartbeat and the stop markers. The keepalive that reads them is private. Its documented rule is to stop a bot whose log has been silent for 10 minutes. The runner also exits 44 when Naukri's daily quota is confirmed, 45 when the account is signed out, and 46 when the driver does not match the installed Edge.

LinkedIn's public [`run_linkedin_supervised.py`](https://github.com/kaustubhupadhyaya/linkedin-autoapply-public/blob/main/run_linkedin_supervised.py) is the clearest watchdog in the suite. It restarts the local runner after a crash, but stops on a verification wall, a sign-in failure, unreadable fit rules, two consecutive short runs with zero applications, or six restarts. It also runs a rolling yield check: after 180 minutes with fewer than one application, it terminates the run. A local-network failure does not count as a crash; it waits for LinkedIn to be reachable, for up to eight hours, and stops after six such runs that saved nothing. It waits up to 20 minutes for 1024 MB of free RAM before launching, retries a failed browser launch three times, and sweeps stale Chrome processes holding its profile.

Instahyre enforces its daily cap across runs by counting today's `applied` rows. A 429 with `protection_activated` stops for the day. A plain 429 parks until the retry-after time, with a minimum of 30 minutes. A logged-out profile leaves a marker and exits 42. The private supervisor watches its cycle deadlines.

Gulf's shared guard reserves or debits request units before a request, limits each tool to its share, allows one Gulf browser at a time, and checks free RAM, running renders, and model processes before a start. A wall parks the site for every Gulf tool on a 12-hour, 24-hour, 48-hour ladder and then a hold that only the user can lift. The public code currently has `READERS_FIXED = False`, so `park()` and `unpark()` refuse to write and the hold path is locked off. `parked()` and `status` still work.

The dashboard judges a row from saved rows, not from the bot's own claim that it is alive. Liveness is drawn separately from keepalive start and "alive" lines. A row with no saved application for longer than its stall threshold (30 minutes for LinkedIn, 30 for Naukri account 1, 15 for Naukri account 2 in the example config) becomes "Stalled"; if there is failing evidence in the logs, it becomes "Failing". One broken row does not kill `/api/status`.

### Deduplication

Every applier deduplicates at its own level, and Gulf deduplicates across tools:

- Naukri uses a SQLite `applied_jobs` table keyed by `job_id`. A row whose status starts with `Unverified` does not block a later attempt, because a false-confirm row is not proof of an application. The bot also checks the page's "already applied" layer.
- Instahyre keeps a set of applied job IDs from `state/applications.jsonl` and a per-feed `seen_ids` set. It appends a row for every decision, including skips.
- LinkedIn loads job IDs from its applied-history CSV, checks the visible "Applied" label, and records failed applications separately.
- Gulf's [`portal_kit/dedupe.py`](https://github.com/kaustubhupadhyaya/gulf-autoapply/blob/main/portal_kit/dedupe.py) treats two jobs as the same role when their normalised titles match and their company names share a distinctive word. It reads every applier database, the LinkedIn UAE job log, and the user's manual application list. It is read-only. A repost under a new job ID, or the same opening reached through another tool, is therefore one role.

### Failure handling

The appliers use exit codes and marker files instead of silent retries:

| Code | Meaning across the suite |
|---|---|
| 0 | Finished, nothing left, or already done for the day |
| 1 | Other error |
| 3 | Live mode requested but not armed |
| 42 | Needs the user (sign-in, wrong account, missing resume, gate stop) |
| 43 | Paused by the control tool |
| 44 | Daily cap reached |
| 45 | Signed out (Naukri and LinkedIn) |
| 46 | Driver unavailable (Naukri) |
| 47 | Throttled, blocked, or machine-starved |
| 48 | Verification challenge or wall |
| 49 | Fit rules unavailable (LinkedIn) |

The point is to stop in a state a human can read, not to keep clicking. A wall, a sign-out, a spent quota, a missing resume, or an unreadable rule file all end the run with a marker.

## What is verified and what is not

### Verified from the public repositories

- All five repositories are public and their files are readable at the linked paths.
- Each repo's CI compiles the Python tree. The dashboard CI also starts the demo and asserts that `/api/boards` returns boards.
- The board reader implements the documented missing-file, unreadable-file, mixed-timestamp, and nested-account behaviour.
- Instahyre's public dry run, live gate, pause/resume, status writer, application log, daily cap, and refresh verification path are in the repository. Its CI compiles the files and runs `--help` and `control.py status`.
- LinkedIn's supervisor contains the restart limit, yield watchdog, wall stop, sign-in stop, network-outage wait, RAM wait, and stale-browser sweep.
- Gulf's dry run is the default, live mode requires both `--live` and an arming environment variable, and the dedupe and facts modules are in the repository.
- The Naukri runner contains the heartbeat, telemetry, daily-limit, sign-out, and driver-mismatch paths. The repository is not open source, and CI does not run the bot.

### Verified on the live machine, not from a fresh clone

- The window file contains `start=04:00` and `end=10:00`.
- The scheduled start and close tasks exist (`AppliersPauseResume` and `AppliersWindowClose`), and the live applier tasks exist.
- The private keepalives, the private resume-refresh supervisor, and the live account configuration exist. The dashboard reads their logs.
- Applications recorded by the live bots are in their own stores, and the dashboard counts only those saved rows.

### Not verified

- No live application was made from a fresh clone for this document. The public CI never opens a browser, signs in, or submits a form.
- No claim is made about current account status, application acceptance, replies, interviews, or offers.
- The board contract is not implemented by every public applier. Instahyre writes it. Naukri, LinkedIn, and Gulf are read through native adapters. The example registry is a target shape.
- The 04:00-to-10:00 window, its scheduled tasks, and the pause tool are not in the five public repositories.
- The Gulf park ladder's hold path is locked off in the public code (`READERS_FIXED = False`), so the full ladder has not been exercised from these repositories.
- LinkedIn's 35-per-count limit and its 05:30 IST reset are measured from logs and used by the dashboard. The public bot reacts to the site's limit banner; it does not hard-code the number.
- The private keepalives and resume-refresh supervisor are not public, so their internal behaviour is not verified here.

## Public sources

- [job-dashboard](https://github.com/kaustubhupadhyaya/job-dashboard)
- [job-dashboard board contract](https://github.com/kaustubhupadhyaya/job-dashboard/blob/main/docs/boards.md)
- [job-dashboard collector](https://github.com/kaustubhupadhyaya/job-dashboard/blob/main/dashboard/collect.py)
- [job-dashboard Gulf guard](https://github.com/kaustubhupadhyaya/job-dashboard/blob/main/portal_kit/site_guard.py)
- [naukri-autoapply](https://github.com/kaustubhupadhyaya/naukri-autoapply)
- [naukri-autoapply unattended runner](https://github.com/kaustubhupadhyaya/naukri-autoapply/blob/main/run_naukri_unattended.py)
- [naukri-autoapply structural faults](https://github.com/kaustubhupadhyaya/naukri-autoapply/blob/main/STRUCTURAL_FAULTS.md)
- [instahyre-autoapply](https://github.com/kaustubhupadhyaya/instahyre-autoapply)
- [instahyre-autoapply resume refresh](https://github.com/kaustubhupadhyaya/instahyre-autoapply/blob/main/refresh.py)
- [instahyre-autoapply control](https://github.com/kaustubhupadhyaya/instahyre-autoapply/blob/main/control.py)
- [linkedin-autoapply-public](https://github.com/kaustubhupadhyaya/linkedin-autoapply-public)
- [linkedin-autoapply-public supervisor](https://github.com/kaustubhupadhyaya/linkedin-autoapply-public/blob/main/run_linkedin_supervised.py)
- [gulf-autoapply](https://github.com/kaustubhupadhyaya/gulf-autoapply)
- [gulf-autoapply dedupe](https://github.com/kaustubhupadhyaya/gulf-autoapply/blob/main/portal_kit/dedupe.py)
