#!/usr/bin/env python3
"""Daily developer update pipeline — entry point for the 8 PM cron job.

Flow:
    1. Fetch ClickUp tasks assigned to the user and updated today.
    2. Extract the working branch from each task description
       (regex first, local LLM as fallback).
    3. Ask GitHub for the diff of  <base branch>...<task branch>.
    4. Clean the diff (drop lockfiles/binaries, cap the size).
    5. Ask Ollama to write a structured "Today Work Report".
    6. Post the report back to the ClickUp task as a comment.

Every stage also appends to per-day markdown files under data/ so there is
always a local audit trail, and a failure on one task never stops the rest.
"""

import logging
import sys

import ai
import clickup
import config
import github
import utils

log = logging.getLogger("main")


def resolve_branch(task):
    """Find the git branch for a task: cheap regex first, LLM as fallback."""
    description = task.get("description") or ""
    if not description.strip():
        return None

    branch = utils.extract_branch_regex(description)
    if branch:
        log.info("Task %s: branch '%s' found via regex", task["id"], branch)
        return branch

    log.info("Task %s: regex found no branch — falling back to AI", task["id"])
    try:
        branch = ai.extract_branch(description)
    except Exception as exc:
        # An AI hiccup must not kill the task run; treat as "no branch".
        log.warning("Task %s: AI branch extraction failed: %s", task["id"], exc)
        return None
    if branch:
        log.info("Task %s: branch '%s' found via AI", task["id"], branch)
    return branch


def _run_pipeline(task, date_str):
    """Run steps 2-6 for a single task. Returns a short outcome string."""
    branch = resolve_branch(task)
    if not branch:
        return "skipped — no branch name found in description"
    task["branch"] = branch

    comparison = github.compare_with_base(branch)
    if comparison is None:
        return (f"skipped — branch '{branch}' not found on GitHub "
                f"({config.GITHUB_REPO})")

    diff_text, kept_files, skipped_files = utils.build_clean_diff(comparison["files"])
    utils.save_diff_markdown(task, diff_text, kept_files, skipped_files, comparison)

    if not kept_files:
        # Nothing left after filtering (branch merged, or only lockfiles/
        # binaries changed) — a report would be pure speculation.
        return f"skipped — no reportable code changes on '{branch}'"

    report = ai.generate_report(task, diff_text, date_str)
    utils.save_report_markdown(task, report)

    clickup.post_comment(task["id"], report)
    if config.DRY_RUN:
        return "report generated (dry run — comment not posted)"
    return "report posted to ClickUp"


def process_task(task, date_str):
    """Pipeline wrapper that records the outcome no matter what happens."""
    try:
        outcome = _run_pipeline(task, date_str)
    except Exception as exc:
        log.exception("Task %s failed: %s", task["id"], exc)
        outcome = f"failed — {exc}"
    utils.save_task_markdown(task, outcome)
    return outcome


def main():
    utils.setup_logging()
    log.info("=" * 60)
    log.info("ClickUp Git Daily Report started (dry run: %s)", config.DRY_RUN)
    config.ensure_dirs()

    missing = config.validate()
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        log.error("Copy .env.example to .env and fill in the values.")
        return 1

    date_str = utils.today_str()

    # Step 1: who are we reporting on, and what did they touch today?
    try:
        user_id = config.CLICKUP_USER_ID or clickup.get_authorized_user_id()
        tasks = clickup.get_todays_tasks(user_id)
    except Exception as exc:
        log.error("Could not fetch tasks from ClickUp: %s", exc)
        return 1

    if not tasks:
        log.info("No tasks updated today for user %s — nothing to do.", user_id)
        utils.append_daily_md(config.TASKS_DIR, "_No tasks updated today._", "Tasks")
        return 0

    # Steps 2-6 per task; one failure never blocks the others.
    log.info("Processing %d task(s)", len(tasks))
    summary = []
    for task in tasks:
        log.info("--- Task %s: %s", task["id"], task["title"])
        outcome = process_task(task, date_str)
        summary.append((task, outcome))
        log.info("Task %s outcome: %s", task["id"], outcome)

    # Persist and log a run summary.
    summary_lines = [f"- **{t['title']}** (`{t['id']}`): {outcome}"
                     for t, outcome in summary]
    utils.append_daily_md(config.REPORTS_DIR,
                          "## Run Summary\n\n" + "\n".join(summary_lines),
                          "Daily Work Reports")

    reported = sum(1 for _, outcome in summary if outcome.startswith("report"))
    log.info("Done: %d/%d task(s) got a report. Daily files are in %s",
             reported, len(summary), config.DATA_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
