"""JSON-serialization helpers shared by the CLI and the API Lambda handler.

Kept as pure functions with no framework dependency (no typer, no AWS
event/response shapes) so both entry points render identical JSON for the
same domain objects.
"""

from __future__ import annotations

from typing import Any

from sizuru_post.domain.validate import ValidationIssue
from sizuru_post.publisher import PlannedTarget, PublishPlan, PublishReport, TargetResult


def issue_to_dict(issue: ValidationIssue) -> dict[str, str]:
    return {"service": issue.service, "level": issue.level, "message": issue.message}


def planned_target_to_dict(target: PlannedTarget) -> dict[str, Any]:
    return {
        "service": target.target.service,
        "valid": target.is_valid,
        "channel_id": target.channel.buffer_channel_id if target.channel else None,
        "issues": [issue_to_dict(i) for i in target.issues],
    }


def plan_to_dict(plan: PublishPlan) -> dict[str, Any]:
    return {
        "video": plan.video.model_dump(),
        "targets": [planned_target_to_dict(t) for t in plan.targets],
    }


def target_result_to_dict(result: TargetResult) -> dict[str, Any]:
    return {
        "service": result.service,
        "status": result.status,
        "post_id": str(result.post_id) if result.post_id else None,
        "buffer_post_id": result.buffer_post_id,
        "due_at": result.due_at,
        "error": result.error,
        "issues": [issue_to_dict(i) for i in result.issues],
    }


def report_to_dict(report: PublishReport) -> dict[str, Any]:
    return {
        "submission_id": str(report.submission_id) if report.submission_id else None,
        "results": [target_result_to_dict(r) for r in report.results],
    }


def plan_has_error(plan: PublishPlan) -> bool:
    return any(issue.level == "error" for target in plan.targets for issue in target.issues)


SUCCESSFUL_STATUSES = {"scheduled", "sent", "draft"}


def has_unsuccessful_result(report: PublishReport) -> bool:
    """True if any target ended up failed/unknown/skipped/db_write_failed —
    anything short of an actually-scheduled/sent/draft post."""
    return any(r.status not in SUCCESSFUL_STATUSES for r in report.results)
