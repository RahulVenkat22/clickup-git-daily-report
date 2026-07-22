"""Shared helpers: logging, resilient HTTP, branch-name extraction,
diff cleaning, and per-day markdown storage.
"""

import logging
import re
import time
from datetime import datetime
from pathlib import PurePosixPath

import requests

import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging / dates
# ---------------------------------------------------------------------------

def setup_logging():
    """Log to stdout AND to logs/run-<date>.log so cron runs leave a trace."""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.handlers.clear()  # avoid duplicate handlers if called twice

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    logfile = logging.FileHandler(
        config.LOGS_DIR / f"run-{today_str()}.log", encoding="utf-8"
    )
    logfile.setFormatter(formatter)
    root.addHandler(logfile)

    # Third-party HTTP internals are too chatty below WARNING.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def today_str():
    """Date stamp used for daily filenames, e.g. '2026-07-23'."""
    return datetime.now().strftime("%Y-%m-%d")


def today_start_ms():
    """Epoch milliseconds for local midnight — ClickUp expects ms timestamps."""
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


# ---------------------------------------------------------------------------
# HTTP with retries — shared by the ClickUp and GitHub clients
# ---------------------------------------------------------------------------

def http_request(method, url, headers=None, params=None, json_body=None,
                 timeout=30, retries=3, allowed_statuses=()):
    """requests wrapper with retry + exponential backoff.

    Retries network errors, HTTP 429 (honouring Retry-After) and 5xx.
    Other 4xx errors fail immediately — retrying a bad token is pointless.
    Statuses listed in `allowed_statuses` (e.g. 404 for a missing branch)
    are returned to the caller instead of raising, so it can react.
    """
    last_error = ""
    for attempt in range(1, retries + 1):
        wait_hint = None
        try:
            response = requests.request(
                method, url,
                headers=headers, params=params, json=json_body, timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = f"network error: {exc}"
        else:
            if response.ok or response.status_code in allowed_statuses:
                return response
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                wait_hint = response.headers.get("Retry-After")
            else:
                # Non-retryable client error: surface the body for debugging.
                raise RuntimeError(
                    f"{method} {url} -> HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )
        if attempt < retries:
            wait = int(wait_hint) if (wait_hint and wait_hint.isdigit()) else 2 ** attempt
            log.warning("%s %s failed (%s) — retry %d/%d in %ds",
                        method, url, last_error, attempt, retries, wait)
            time.sleep(wait)
    raise RuntimeError(f"{method} {url} failed after {retries} attempts ({last_error})")


# ---------------------------------------------------------------------------
# Branch-name extraction (regex pass — the AI fallback lives in ai.py)
# ---------------------------------------------------------------------------

# Ordered from most to least explicit; the first valid hit wins.
BRANCH_PATTERNS = [
    # "Branch: feature/x", "branch name - feature/x", "Branch = feature/x"
    re.compile(r"\bbranch(?:\s*name)?\b\s*[:=\-]\s*[`'\"]*([A-Za-z0-9][A-Za-z0-9._/\-]*)",
               re.IGNORECASE),
    # GitHub links: https://github.com/owner/repo/tree/<branch>
    re.compile(r"github\.com/[\w.\-]+/[\w.\-]+/tree/([A-Za-z0-9][A-Za-z0-9._/\-]*)",
               re.IGNORECASE),
    # GitHub compare links: .../compare/development...<branch>
    re.compile(r"github\.com/[\w.\-]+/[\w.\-]+/compare/[^\s]*?\.\.\.([A-Za-z0-9][A-Za-z0-9._/\-]*)",
               re.IGNORECASE),
    # Shell snippets: "git checkout -b feature/x"
    re.compile(r"git\s+checkout\s+(?:-b\s+)?[`'\"]*([A-Za-z0-9][A-Za-z0-9._/\-]*)",
               re.IGNORECASE),
    # Remote refs: "pushed to origin/feature/x"
    re.compile(r"\borigin/([A-Za-z0-9][A-Za-z0-9._/\-]*)"),
    # Conventional prefixes anywhere in the text: feature/..., bugfix/...
    re.compile(r"\b((?:feature|feat|bugfix|fix|hotfix|chore|refactor|release|task)"
               r"/[A-Za-z0-9][A-Za-z0-9._\-/]*)", re.IGNORECASE),
]

_VALID_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,249}$")

# Never treat shared mainline branches (or junk placeholders) as the work
# branch — comparing the base against itself/mainlines produces nonsense.
_REJECTED_NAMES = {"main", "master", "develop", "dev", "development",
                   "staging", "production", "none", "null", "n/a", "tbd", "todo"}


def is_valid_branch_name(name):
    """Cheap sanity check that a candidate string is a plausible git branch."""
    if not name or not _VALID_BRANCH_RE.match(name):
        return False
    if ".." in name or "//" in name:
        return False
    if name.endswith("/") or name.endswith(".") or name.endswith(".lock"):
        return False
    if name.lower() in _REJECTED_NAMES or name == config.GITHUB_BASE_BRANCH:
        return False
    return True


def _clean_candidate(raw):
    """Strip quotes/backticks and trailing sentence punctuation from a match."""
    return raw.strip("`'\"").rstrip(".,;:!?)([]{}")


def extract_branch_regex(text):
    """Return the first valid branch name found in `text`, or None."""
    if not text:
        return None
    for pattern in BRANCH_PATTERNS:
        for match in pattern.finditer(text):
            candidate = _clean_candidate(match.group(1))
            if candidate and is_valid_branch_name(candidate):
                return candidate
    return None


# ---------------------------------------------------------------------------
# Diff cleaning — drop noise files, cap the size sent to the LLM
# ---------------------------------------------------------------------------

def should_skip_file(path):
    """True for lockfiles, binaries, minified bundles and vendored paths."""
    p = PurePosixPath(path)  # GitHub API always reports POSIX paths
    if p.name in config.EXCLUDED_FILENAMES:
        return True
    if p.suffix.lower() in config.EXCLUDED_EXTENSIONS:
        return True
    if ".min." in p.name:  # foo.min.js / foo.min.css
        return True
    if any(part in config.EXCLUDED_PATH_PARTS for part in p.parts):
        return True
    return False


def build_clean_diff(files):
    """Turn GitHub compare `files` into one bounded diff string.

    Returns (diff_text, kept_filenames, skipped_filenames). Files without a
    textual patch (binary / oversized on GitHub's side) stay in `kept` so the
    report can mention them, but contribute only a one-line note.
    """
    parts, kept, skipped = [], [], []
    budget = config.MAX_DIFF_CHARS

    for index, entry in enumerate(files):
        name = entry.get("filename", "")
        if should_skip_file(name):
            skipped.append(name)
            continue

        if budget <= 0:
            # Budget exhausted: list what got cut so nothing is silently lost.
            remaining = [f.get("filename", "") for f in files[index:]
                         if not should_skip_file(f.get("filename", ""))]
            parts.append(
                f"... [diff size limit reached — {len(remaining)} more changed "
                f"file(s) omitted: {', '.join(remaining[:10])}"
                f"{' ...' if len(remaining) > 10 else ''}]"
            )
            break

        header = (f"--- FILE: {name} ({entry.get('status', 'modified')}, "
                  f"+{entry.get('additions', 0)}/-{entry.get('deletions', 0)}) ---")
        patch = entry.get("patch")
        if patch is None:
            body = "(no textual patch from GitHub — binary or very large file)"
        else:
            if len(patch) > config.MAX_DIFF_CHARS_PER_FILE:
                patch = patch[:config.MAX_DIFF_CHARS_PER_FILE] + "\n... [file diff truncated]"
            body = patch

        block = f"{header}\n{body}"
        if len(block) > budget:
            block = block[:max(budget, 200)] + "\n... [diff truncated at size limit]"
        parts.append(block)
        kept.append(name)
        budget -= len(block)

    return "\n\n".join(parts), kept, skipped


# ---------------------------------------------------------------------------
# Per-day markdown storage — one file per day in each data/ subfolder
# ---------------------------------------------------------------------------

def append_daily_md(directory, section_md, header_title):
    """Append a section to today's markdown file, creating it with a header
    on first write. Returns the file path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{today_str()}.md"
    is_new = not path.exists()
    with open(path, "a", encoding="utf-8") as fh:
        if is_new:
            fh.write(f"# {header_title} — {today_str()}\n")
        fh.write("\n---\n\n" + section_md.rstrip() + "\n")
    return path


def save_task_markdown(task, outcome):
    """Record a task (and what happened to it) in data/tasks/<date>.md."""
    description = (task.get("description") or "").strip() or "(no description)"
    if len(description) > 1500:
        description = description[:1500] + "\n... [truncated]"
    section = (
        f"## {task['title']}\n\n"
        f"- **Task ID:** `{task['id']}`\n"
        f"- **Status:** {task.get('status', '')}\n"
        f"- **URL:** {task.get('url', '')}\n"
        f"- **Branch:** `{task.get('branch') or 'not found'}`\n"
        f"- **Outcome:** {outcome}\n\n"
        f"### Description\n\n{description}"
    )
    return append_daily_md(config.TASKS_DIR, section, "Tasks")


def save_diff_markdown(task, diff_text, kept, skipped, comparison):
    """Record the cleaned diff for a task in data/diffs/<date>.md."""
    lines = [
        f"## {task['title']} (`{task['id']}`) — `{task.get('branch', '')}`",
        "",
        f"- **Compared:** `{config.GITHUB_BASE_BRANCH}...{task.get('branch', '')}`",
        f"- **Commits on branch:** {comparison.get('total_commits', '?')}",
        f"- **Files included:** {len(kept)}",
    ]
    if skipped:
        shown = ", ".join(f"`{s}`" for s in skipped[:20])
        lines.append(f"- **Files filtered out:** {shown}"
                     + (" ..." if len(skipped) > 20 else ""))
    # Four-backtick fence so diff content containing ``` cannot break it.
    lines += ["", "````diff", diff_text if diff_text.strip() else "(no textual diff)", "````"]
    return append_daily_md(config.DIFFS_DIR, "\n".join(lines), "Git Diffs")


def save_report_markdown(task, report):
    """Record the generated report in data/reports/<date>.md."""
    section = (
        f"## {task['title']} (`{task['id']}`)\n\n"
        f"- **Branch:** `{task.get('branch', '')}`\n"
        f"- **Task URL:** {task.get('url', '')}\n\n"
        f"{report.rstrip()}"
    )
    return append_daily_md(config.REPORTS_DIR, section, "Daily Work Reports")
