# Board registry

The dashboard can show any applier as a board without knowing its code. `boards.json` at the repo root lists
the boards; `dashboard/boards.py` reads each board's files and `GET /api/boards` returns the result. The
reader is read-only: it never imports, starts or edits applier code, and it never writes to a board's repo.

`boards.json` is machine-specific and git-ignored. `boards.example.json` is the tracked example.

## The config

```json
{
  "boards": [
    {
      "id": "instahyre",
      "name": "Instahyre",
      "repo": "https://github.com/your-org/instahyre-autoapply",
      "path": "../instahyre-autoapply",
      "status": "state/status.json",
      "applications": "state/applications.jsonl",
      "control": "state/control.json",
      "enabled": true
    }
  ]
}
```

A bare JSON list is also accepted in place of `{"boards": [...]}`.

| Field | Meaning |
|---|---|
| `id` | Stable short key, unique per board. Required; an entry without one is skipped. |
| `name` | Display name. Defaults to `id`. |
| `repo` | Public repository URL shown on the page. |
| `path` | The board's local checkout. Relative paths resolve against the folder holding `boards.json`; absolute paths are kept. |
| `status` | Status file, relative to `path` (or absolute). |
| `applications` | Applications log, relative to `path` (or absolute). |
| `control` | Control file, relative to `path` (or absolute). |
| `enabled` | Whether the board is meant to run. Defaults to `true`. |

`boards.example.json` uses relative sibling paths. The local `boards.json` may use absolute Windows paths.

## Status contract

`status` points to a JSON object written by the board itself:

```json
{
  "state": "running",
  "last_run": "2026-11-06T19:14:00+05:30",
  "cap": 30,
  "message": ""
}
```

- `state` is free text (`running`, `idle`, `paused`, `error`, `signed_out`, ...). `status` is accepted as an
  alias. Missing state reads as `unknown`.
- `last_run` accepts ISO 8601 (`last_run_at` and `lastRun` also work). If absent, the status file's mtime is
  used. Timestamps are shown as written and are expected in IST.
- `cap` is the daily application cap (`daily_cap` also works). Missing or non-numeric reads as no cap.
- `message` is optional and appears under the board's name.
- `applied_today` (int) is used for the page count only when there is no applications log.

A board with several accounts may nest them:

```json
{
  "accounts": {
    "1": {"state": "dry_run", "last_run": "...", "applied_today": 0, "daily_cap": 30, "message": "..."},
    "2": {"state": "dry_run", "last_run": "...", "applied_today": 0, "daily_cap": 30, "message": "..."}
  }
}
```

The summary combines them: distinct states (with an account count), the latest `last_run`, summed caps,
summed `applied_today` and the distinct messages.

A missing repo or missing status file returns `installed: false` with a message such as
`Status file not found: C:\...\state\status.json`. The page says "not installed"; it does not crash.
An unreadable status file returns `installed: true`, `state: "unknown"` and the read error in `message`.

## Applications contract

`applications` is a log of submitted applications, one record per application:

- `.jsonl` / `.ndjson`: one JSON object per line.
- `.json`: a list of objects, or `{"applications": [...]}`.
- `.csv` / `.tsv`: one row per application.

A record is a timestamp plus any extra fields. The timestamp key can be `ts`, `time`, `timestamp`,
`applied_at`, `datetime` or `date`. ISO 8601 with an offset is preferred; a naive value is treated as IST,
and a `Z` value is converted to IST before counting. `applied_today` counts records whose IST calendar date
is today. A missing or unreadable log is `[]`; the page shows "no log" instead of a false zero.

CSV example:

```csv
ts,company,title
2026-11-06T09:12:00+05:30,Acme,Data Engineer
```

## Control contract

`control` is optional. When the file exists, it may be `{"enabled": true}` or a bare `true`/`false`; the
summary reports `present` and `enabled`. It is informational on this page. Nothing writes it.

## API and page

- `GET /api/boards` returns:

```json
{
  "generated": "2026-11-06T19:20:00+05:30",
  "boards": [
    {
      "id": "instahyre",
      "name": "Instahyre",
      "repo": "https://github.com/your-org/instahyre-autoapply",
      "path": "C:\\path\\to\\instahyre-autoapply",
      "enabled": true,
      "installed": false,
      "state": "not installed",
      "last_run": null,
      "applied_today": 0,
      "applied_today_from": null,
      "applications_available": false,
      "cap": null,
      "control": {"path": "C:\\...\\state\\control.json", "present": false, "enabled": null},
      "message": "Status file not found: C:\\...\\state\\status.json"
    }
  ]
}
```

- `GET /boards` is the page. It is linked from `/`.

## Adding a board

1. Add an entry to `boards.json` with a new `id`, the local `path` and the three file paths.
2. Have the applier write the status JSON above after each run, and append one record per submitted
   application to the applications log. Until it does, the board shows as installed with no log or as not
   installed, depending on which file is missing.
3. Keep `repo` a public URL and never put credentials or account fields in `boards.json`.
4. Check it without restarting the dashboard:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/boards | ConvertTo-Json -Depth 6
```

The registry reads the config on every request, so edits to `boards.json` appear on the next page load.
No unit tests; verify through the running dashboard.

