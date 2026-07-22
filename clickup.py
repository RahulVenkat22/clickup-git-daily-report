"""ClickUp API client.

Responsibilities:
  * find the user to report on (auto-detected from the token when not set)
  * fetch tasks assigned to that user which were updated today
  * post the generated report back to a task as a comment
"""

import logging

import config
import utils

log = logging.getLogger(__name__)


def _headers():
    return {
        "Authorization": config.CLICKUP_API_TOKEN,
        "Content-Type": "application/json",
    }


def get_authorized_user_id():
    """Return the user id owning the API token (used when CLICKUP_USER_ID
    is not configured)."""
    response = utils.http_request(
        "GET", f"{config.CLICKUP_BASE_URL}/user", headers=_headers()
    )
    user = response.json().get("user", {})
    user_id = str(user.get("id", "")).strip()
    if not user_id:
        raise RuntimeError("Could not determine user id from ClickUp /user response")
    log.info("Authorized ClickUp user: %s (id %s)", user.get("username", "?"), user_id)
    return user_id


def get_todays_tasks(user_id):
    """Fetch all tasks assigned to `user_id` whose last update is today.

    Uses the 'Get Filtered Team Tasks' endpoint with pagination, then
    re-fetches each task individually because the list endpoint truncates
    descriptions — and the full text is needed for branch extraction.
    """
    url = f"{config.CLICKUP_BASE_URL}/team/{config.CLICKUP_TEAM_ID}/task"
    collected, page = [], 0

    while True:
        params = {
            "assignees[]": user_id,
            "date_updated_gt": utils.today_start_ms(),
            "include_closed": "true",   # tasks finished today still get a report
            "subtasks": "true",
            "page": page,
        }
        data = utils.http_request("GET", url, headers=_headers(), params=params).json()
        batch = data.get("tasks", [])
        collected.extend(batch)
        log.info("ClickUp page %d: %d task(s)", page, len(batch))
        if data.get("last_page", True) or not batch:
            break
        page += 1

    # Deduplicate across pages, keeping first occurrence.
    unique = list({t["id"]: t for t in collected if t.get("id")}.values())
    log.info("Total tasks updated today: %d", len(unique))

    detailed = []
    for item in unique:
        try:
            detailed.append(get_task_detail(item["id"]))
        except Exception as exc:
            # Fall back to the (possibly truncated) list data rather than
            # dropping the task from today's report entirely.
            log.warning("Could not fetch details for task %s (%s) — using list data",
                        item.get("id"), exc)
            detailed.append({
                "id": item.get("id", ""),
                "title": item.get("name", "(untitled)"),
                "description": item.get("description") or item.get("text_content") or "",
                "status": (item.get("status") or {}).get("status", ""),
                "url": item.get("url", ""),
            })
    return detailed


def get_task_detail(task_id):
    """Fetch one task with its full (markdown) description."""
    data = utils.http_request(
        "GET",
        f"{config.CLICKUP_BASE_URL}/task/{task_id}",
        headers=_headers(),
        params={"include_markdown_description": "true"},
    ).json()
    description = (
        data.get("markdown_description")
        or data.get("description")
        or data.get("text_content")
        or ""
    )
    return {
        "id": data["id"],
        "title": data.get("name", "(untitled)"),
        "description": description,
        "status": (data.get("status") or {}).get("status", ""),
        "url": data.get("url", ""),
    }


def post_comment(task_id, comment_text):
    """Post the daily report as a comment on the task (skipped in DRY_RUN)."""
    if config.DRY_RUN:
        log.info("[DRY RUN] Would post a %d-char comment to task %s",
                 len(comment_text), task_id)
        return
    utils.http_request(
        "POST",
        f"{config.CLICKUP_BASE_URL}/task/{task_id}/comment",
        headers=_headers(),
        json_body={"comment_text": comment_text, "notify_all": False},
    )
    log.info("Report comment posted to task %s", task_id)
