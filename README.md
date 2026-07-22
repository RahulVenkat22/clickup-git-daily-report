# ClickUp Git Daily Report

Automates the end-of-day developer update: pulls today's ClickUp tasks, reads
the git diff of each task's branch from GitHub, has a **local LLM (Ollama)**
write a structured "Today Work Report", and posts it back to the ClickUp task
as a comment — every evening at 8 PM via cron. No web server, no cloud AI.

## How it works

```
cron (20:00)
   └── main.py
        ├── clickup.py   1. fetch my tasks updated today (+ full descriptions)
        ├── utils.py     2. extract branch name from description (regex)
        │    └── ai.py      ... LLM fallback when regex finds nothing
        ├── github.py    3. GET /compare/development...<branch>
        ├── utils.py     4. drop lockfiles/binaries, cap diff size
        ├── ai.py        5. ollama run <model>  →  "Today Work Report"
        └── clickup.py   6. POST report as a task comment
```

Every stage also appends to **one markdown file per day** under `data/`,
so there is always a local audit trail of exactly what was fetched,
what was sent to the model, and what was posted:

| File | Contents |
|---|---|
| `data/tasks/YYYY-MM-DD.md` | tasks picked up, branch found, final outcome |
| `data/diffs/YYYY-MM-DD.md` | the cleaned diffs that were sent to the LLM |
| `data/reports/YYYY-MM-DD.md` | generated reports + end-of-run summary |
| `logs/run-YYYY-MM-DD.log` | full execution log (also echoed to stdout) |

## Project structure

```
clickup-git-daily-report/
├── main.py            # entry point / orchestration
├── config.py          # all settings, read from environment (.env)
├── clickup.py         # ClickUp API client (fetch tasks, post comments)
├── github.py          # GitHub API client (branch comparison)
├── ai.py              # Ollama subprocess calls (report + branch fallback)
├── utils.py           # logging, HTTP retries, regex, diff cleaning, storage
├── prompts/
│   ├── report_prompt.txt   # template for the daily report
│   └── branch_prompt.txt   # template for AI branch extraction
├── data/              # per-day markdown output (git-ignored)
├── logs/              # per-day run logs (git-ignored)
├── requirements.txt
└── .env.example
```

## Prerequisites

- Python 3.9+
- [Ollama](https://ollama.com) installed and running, with a model pulled:

  ```bash
  ollama pull deepseek-coder     # or: ollama pull llama3
  ```

- A ClickUp personal API token and a GitHub personal access token.

## Setup

```bash
cd clickup-git-daily-report
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env    # fill in the values below
```

Where to find each credential:

| Variable | Where to get it |
|---|---|
| `CLICKUP_API_TOKEN` | ClickUp → avatar → **Settings → Apps → API Token** (starts with `pk_`) |
| `CLICKUP_TEAM_ID` | the number after `app.clickup.com/` in your browser URL |
| `GITHUB_TOKEN` | github.com → **Settings → Developer settings → Tokens** (`repo` scope, or fine-grained with read-only *Contents*) |
| `GITHUB_REPO` | `owner/repo` of the codebase your branches live in |
| `OLLAMA_BIN` | output of `which ollama` (full path — important for cron) |

## Ticket convention

Put the branch name somewhere in the task description, e.g.:

```
Branch: feature/CU-1234-login-api
```

Also auto-detected: GitHub links (`.../tree/<branch>`), `git checkout` snippets,
`origin/<branch>` mentions, and conventional names (`feature/…`, `bugfix/…`,
`hotfix/…`, …). If the regexes find nothing, the local LLM gets one shot at
extracting it; if that also fails, the task is recorded as skipped.

## Test run

```bash
DRY_RUN=true venv/bin/python3 main.py
```

Dry run executes the entire pipeline — including AI report generation into
`data/reports/` — but posts **nothing** to ClickUp. When the output looks
right, do a real run:

```bash
venv/bin/python3 main.py
```

## Cron (daily at 8 PM)

```bash
crontab -e
```

Add (adjust the path to where you cloned the project):

```cron
0 20 * * * cd /path/to/clickup-git-daily-report && ./venv/bin/python3 main.py >> logs/cron.log 2>&1
```

Weekdays only instead: `0 20 * * 1-5`.

Cron notes:
- Cron runs with a minimal `PATH` — that is why `OLLAMA_BIN` in `.env` should
  be the **full path** to the binary (e.g. `/usr/local/bin/ollama`).
- The Ollama server must be running (`ollama serve`, or the systemd service
  installed by the official installer — check with `systemctl status ollama`).
- All output lands in `logs/cron.log` plus the per-day `logs/run-*.log`.

## Edge-case behaviour

| Situation | What happens |
|---|---|
| No tasks updated today | Logged, noted in the daily file, exits 0 |
| No branch found in a description | Task recorded as *skipped*, no comment posted |
| Branch missing on GitHub (404) | Task recorded as *skipped*, others continue |
| Diff empty after filtering (merged branch, lockfiles only) | Skipped — no speculative reports |
| One task throws an error | Logged with traceback; remaining tasks still run |
| ClickUp/GitHub rate limits or 5xx | Automatic retry with backoff (honours `Retry-After`) |

## Extending

- **Different model** — set `OLLAMA_MODEL=llama3` (or any pulled model); no code change.
- **Report style** — edit `prompts/report_prompt.txt`; placeholders are
  `{{DATE}}`, `{{TASK_TITLE}}`, `{{TASK_ID}}`, `{{BRANCH_NAME}}`,
  `{{TASK_DESCRIPTION}}`, `{{GIT_DIFF}}`.
- **Another repository** — the compare call is isolated in
  `github.compare_with_base()`; extend it to try a list of repos if your
  branches are spread across several.
- **Weekly rollup** — the per-day files in `data/reports/` are already
  perfect input for a Monday summary job.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `'ollama' not found` (works in terminal, fails in cron) | Set `OLLAMA_BIN` to the full path from `which ollama` |
| Ollama error mentioning the model | `ollama pull <model>` first |
| `HTTP 401` from ClickUp | Token wrong/expired; regenerate in ClickUp settings |
| Every compare returns 404 | Check `GITHUB_REPO` spelling and that `GITHUB_BASE_BRANCH` exists |
| Reports cut off / model slow | Lower `MAX_DIFF_CHARS`, raise `OLLAMA_TIMEOUT_SECONDS` |
| Missing env vars error on start | `cp .env.example .env` and fill in the four required values |
