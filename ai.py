"""Ollama integration (local LLM, called via subprocess).

Two jobs:
  * generate the structured "Today Work Report" from ticket + diff
  * fallback branch-name extraction when the regex pass finds nothing

Prompts are plain-text templates in prompts/ with {{TOKEN}} placeholders.
str.replace() is used instead of str.format() on purpose: descriptions and
diffs routinely contain `{`/`}` characters that would break format().
"""

import logging
import re
import subprocess
import time

import config
import utils

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level Ollama call
# ---------------------------------------------------------------------------

def _run_ollama(prompt):
    """Run `ollama run <model>` with the prompt on stdin, return cleaned stdout."""
    command = [config.OLLAMA_BIN, "run", config.OLLAMA_MODEL]
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.OLLAMA_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"'{config.OLLAMA_BIN}' not found. Install Ollama, or set OLLAMA_BIN "
            f"to the full binary path in .env (cron runs with a minimal PATH)."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Ollama timed out after {config.OLLAMA_TIMEOUT_SECONDS}s "
            f"(model '{config.OLLAMA_MODEL}'). Increase OLLAMA_TIMEOUT_SECONDS "
            f"or lower MAX_DIFF_CHARS."
        )

    elapsed = time.monotonic() - started
    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()[-400:]
        raise RuntimeError(
            f"Ollama exited with code {result.returncode}: {stderr_tail} "
            f"(is the model pulled? try: ollama pull {config.OLLAMA_MODEL})"
        )
    log.info("Ollama '%s' answered in %.1fs (prompt %d chars)",
             config.OLLAMA_MODEL, elapsed, len(prompt))
    return _clean_output(result.stdout)


def _clean_output(text):
    """Normalise raw model output into clean markdown."""
    # Reasoning models sometimes emit <think>...</think> blocks — drop them.
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Unwrap the whole answer if the model fenced it in ``` ... ```.
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:-1]).strip()

    # Cut chatty preambles ("Sure, here is your report:") by jumping to the
    # header our report template mandates, when present.
    marker = "## Today Work Report"
    index = cleaned.find(marker)
    if index > 0:
        cleaned = cleaned[index:]
    return cleaned


def _load_prompt(filename):
    """Read a prompt template from the prompts/ directory."""
    path = config.PROMPTS_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RuntimeError(f"Prompt file missing: {path}")


# ---------------------------------------------------------------------------
# High-level operations
# ---------------------------------------------------------------------------

def generate_report(task, diff_text, date_str):
    """Ask the LLM for a structured 'Today Work Report' for one task."""
    template = _load_prompt("report_prompt.txt")
    description = (task.get("description") or "(no description)").strip()
    prompt = (
        template
        .replace("{{DATE}}", date_str)
        .replace("{{TASK_TITLE}}", task["title"])
        .replace("{{TASK_ID}}", str(task["id"]))
        .replace("{{BRANCH_NAME}}", task.get("branch", ""))
        .replace("{{TASK_DESCRIPTION}}", description[:4000])
        .replace("{{GIT_DIFF}}", diff_text)
    )
    report = _run_ollama(prompt)
    if not report.strip():
        raise RuntimeError("Ollama returned an empty report")
    return report


def extract_branch(description):
    """AI fallback for branch extraction. Returns a branch name or None.

    The answer is never trusted blindly: whatever the model says is run
    through the same validation as the regex pass.
    """
    template = _load_prompt("branch_prompt.txt")
    prompt = template.replace("{{TASK_DESCRIPTION}}", description[:4000])
    answer = _run_ollama(prompt).strip()

    if not answer or answer.upper().startswith("NONE"):
        return None

    # Happy path: the model obeyed and answered with a single clean token.
    first_line = answer.splitlines()[0].strip().strip("`'\"")
    if utils.is_valid_branch_name(first_line):
        return first_line

    # Chatty answer ("The branch is `feature/x`.") — mine it with the same
    # regexes used on descriptions, then fall back to token scanning.
    found = utils.extract_branch_regex(answer)
    if found:
        return found
    for token in re.split(r"[\s`'\",]+", answer):
        token = token.strip(".,;:!?()[]{}")
        # Require a separator so stray words ("The", "branch") never pass.
        if any(sep in token for sep in "/-_") and utils.is_valid_branch_name(token):
            return token
    return None
