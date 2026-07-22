"""Central configuration for the daily ticket update system.

Everything tunable lives here, sourced from environment variables (a local
.env file next to the code is loaded automatically). This module performs
no network calls — it only reads settings and validates them.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = the directory containing this file, so cron can invoke the
# script from any working directory without breaking relative paths.
BASE_DIR = Path(__file__).resolve().parent

# Pick up a .env file sitting next to the code, if present.
load_dotenv(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# ClickUp
# ---------------------------------------------------------------------------
CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"
CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
# Workspace (team) id — the number after app.clickup.com/ in the browser URL.
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "")
# Optional: numeric user id whose tasks are reported. When empty, the owner
# of the API token is auto-detected at runtime.
CLICKUP_USER_ID = os.getenv("CLICKUP_USER_ID", "")

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
GITHUB_API_URL = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # "owner/repo"
# Branch every feature branch is compared against (base...feature).
GITHUB_BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "development")

# ---------------------------------------------------------------------------
# Ollama (local LLM)
# ---------------------------------------------------------------------------
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-coder")
# Full binary path is recommended for cron, whose PATH usually lacks
# /usr/local/bin (find yours with:  which ollama).
OLLAMA_BIN = os.getenv("OLLAMA_BIN", "ollama")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300"))

# ---------------------------------------------------------------------------
# Diff processing
# ---------------------------------------------------------------------------
# Overall character budget for the diff embedded in the LLM prompt, plus a
# per-file cap so one giant file cannot eat the entire budget.
MAX_DIFF_CHARS = int(os.getenv("MAX_DIFF_CHARS", "12000"))
MAX_DIFF_CHARS_PER_FILE = int(os.getenv("MAX_DIFF_CHARS_PER_FILE", "4000"))

# Generated / lock / binary files: all noise, no signal for a daily report.
EXCLUDED_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "npm-shrinkwrap.json",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "composer.lock",
    "Gemfile.lock",
    "Cargo.lock",
    "go.sum",
    "mix.lock",
}
EXCLUDED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".jar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".db", ".sqlite",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".mov", ".avi", ".webm",
    ".pyc", ".class", ".map", ".lock",
}
# Any path containing one of these directory names is skipped outright.
EXCLUDED_PATH_PARTS = {
    "node_modules", "dist", "build", ".next", "vendor",
    "__snapshots__", "coverage",
}

# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------
# DRY_RUN=true runs the full pipeline (including report generation) but does
# NOT post anything to ClickUp — ideal for the first test run.
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"}
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Local storage layout — one markdown file per day in each folder.
# ---------------------------------------------------------------------------
PROMPTS_DIR = BASE_DIR / "prompts"
DATA_DIR = BASE_DIR / "data"
TASKS_DIR = DATA_DIR / "tasks"        # tasks picked up today + their outcome
DIFFS_DIR = DATA_DIR / "diffs"        # cleaned diffs that were sent to the LLM
REPORTS_DIR = DATA_DIR / "reports"    # generated reports + run summary
LOGS_DIR = BASE_DIR / "logs"


def ensure_dirs():
    """Create the data/log directories on first run (idempotent)."""
    for directory in (TASKS_DIR, DIFFS_DIR, REPORTS_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def validate():
    """Return the names of required settings that are missing/empty."""
    required = {
        "CLICKUP_API_TOKEN": CLICKUP_API_TOKEN,
        "CLICKUP_TEAM_ID": CLICKUP_TEAM_ID,
        "GITHUB_TOKEN": GITHUB_TOKEN,
        "GITHUB_REPO": GITHUB_REPO,
    }
    return [name for name, value in required.items() if not value.strip()]
