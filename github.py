"""GitHub API client.

Compares a task's feature branch against the base branch (default:
`development`) and returns the changed files with their patches.
"""

import logging

import config
import utils

log = logging.getLogger(__name__)


def _headers():
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def compare_with_base(branch):
    """Fetch the `base...branch` comparison from GitHub.

    The three-dot form means "everything the branch changed since it diverged
    from base" — exactly what a daily work report should describe.

    Returns a dict with `files`, `total_commits`, `ahead_by` and `html_url`,
    or None when the branch (or repo/base) does not exist. A missing branch
    is an expected, per-task condition — it is logged, not raised, so the
    rest of the day's tasks still get processed.
    """
    base = config.GITHUB_BASE_BRANCH
    url = (f"{config.GITHUB_API_URL}/repos/{config.GITHUB_REPO}"
           f"/compare/{base}...{branch}")
    log.info("GitHub compare: %s...%s in %s", base, branch, config.GITHUB_REPO)

    response = utils.http_request("GET", url, headers=_headers(),
                                  allowed_statuses=(404,))
    if response.status_code == 404:
        log.warning("Compare returned 404 — branch '%s' (or repo/base branch) "
                    "not found in %s", branch, config.GITHUB_REPO)
        return None

    data = response.json()
    files = data.get("files", [])
    log.info("Branch '%s': %d commit(s) ahead, %d changed file(s)",
             branch, data.get("ahead_by", 0), len(files))
    return {
        "files": files,
        "total_commits": data.get("total_commits", 0),
        "ahead_by": data.get("ahead_by", 0),
        "html_url": data.get("html_url", ""),
    }
