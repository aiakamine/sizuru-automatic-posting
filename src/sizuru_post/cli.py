"""sizuru-post CLI: channels / validate / publish / retry / sync / introspect.

All commands print machine-readable JSON to stdout (a Claude skill parses
it), and use non-zero exit codes to signal validation errors or missing
approval so scripts/skills can branch on `$?` without parsing text.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from pydantic import ValidationError

from sizuru_post.adapters.buffer import BufferAmbiguousError, BufferProtocolError
from sizuru_post.admin_accounts import (
    AdminAccountRollbackError,
    AdminUserNotFoundError,
    InvalidLoginIdError,
    LoginIdAlreadyExistsError,
    create_admin_account,
    list_admin_accounts,
    reset_admin_password,
)
from sizuru_post.channels_sync import NoBufferOrganizationError, sync_channels
from sizuru_post.config import Settings
from sizuru_post.domain.models import PublishRequest
from sizuru_post.factory import build_buffer_client, build_publisher, build_repo, build_settings
from sizuru_post.publisher import ApprovalRequiredError
from sizuru_post.serialization import (
    has_unsuccessful_result,
    plan_has_error,
    plan_to_dict,
    report_to_dict,
    target_result_to_dict,
)

# Settings loads .env itself (scoped to the Settings instance, via
# pydantic-settings' env_file=".env"); we deliberately do not also call
# python-dotenv's load_dotenv() here, since that mutates the real process
# environment at import time and can leak stale/empty values (e.g. an
# empty AWS_PROFILE) into unrelated code that reads os.environ directly.
app = typer.Typer(help="TikTok / Instagram Reels / X への投稿を Buffer 経由で行う CLI")

admin_app = typer.Typer(help="管理画面にログインできる管理者アカウントを管理する")
app.add_typer(admin_app, name="admin")


def _load_settings() -> Settings:
    """Construct Settings, turning a ValidationError into a message that
    names only the offending environment variables — never their values
    (secret or not), even if hide_input_in_errors were ever misconfigured."""
    try:
        return build_settings()
    except ValidationError as exc:
        field_to_env = {
            name: str(field.validation_alias or name).upper() for name, field in Settings.model_fields.items()
        }
        names = sorted(
            {
                field_to_env.get(str(error["loc"][0]), str(error["loc"][0]))
                for error in exc.errors()
                if error["loc"]
            }
        )
        message = "設定が不正です。次の環境変数を確認してください: " + ", ".join(names)
        typer.echo(message, err=True)
        raise typer.Exit(code=1) from exc


def _print_json(data: object) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _load_request(request_path: Path) -> PublishRequest:
    data = json.loads(request_path.read_text())
    request = PublishRequest.model_validate(data)
    if request.video_path is None:
        # video_path=None only makes sense for the scheduling API's
        # already-uploaded flow (Publisher.publish_uploaded); the CLI
        # always needs an actual local file to probe/upload.
        typer.echo("video_path は必須です", err=True)
        raise typer.Exit(code=1)
    return request


# --- commands ------------------------------------------------------------------------


@app.command()
def channels() -> None:
    """Fetch channels from Buffer, upsert them into Supabase, and print a table."""
    settings = _load_settings()
    buffer = build_buffer_client(settings)
    repo = build_repo(settings)

    try:
        raw_channels = sync_channels(buffer, repo, settings.buffer_organization_id)
    except NoBufferOrganizationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    for c in raw_channels:
        typer.echo(f"{c.get('service')}\t{c.get('displayName') or c.get('name')}\t{c.get('id')}")


@app.command()
def validate(request_path: Path) -> None:
    """Load a request JSON, run planning/validation only, print issues.

    Exits 1 if any target has a validation error.
    """
    settings = _load_settings()
    request = _load_request(request_path)
    publisher = build_publisher(settings)

    plan = publisher.plan(request)
    _print_json(plan_to_dict(plan))

    if plan_has_error(plan):
        raise typer.Exit(code=1)


@app.command()
def publish(
    request_path: Path,
    yes: Annotated[bool, typer.Option("--yes", help="承認済みとして実行する")] = False,
) -> None:
    """Publish a request. Without --yes, print the plan and exit 2 (approval required)."""
    settings = _load_settings()
    request = _load_request(request_path)
    publisher = build_publisher(settings)

    if not yes:
        plan = publisher.plan(request)
        _print_json(plan_to_dict(plan))
        typer.echo("承認が必要です。--yes を付けて再実行してください。", err=True)
        raise typer.Exit(code=2)

    try:
        report = publisher.publish(request, approved=True)
    except ApprovalRequiredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    _print_json(report_to_dict(report))
    if has_unsuccessful_result(report):
        raise typer.Exit(code=3)


@app.command()
def retry(
    submission_id: str,
    yes: Annotated[bool, typer.Option("--yes", help="承認済みとして実行する")] = False,
) -> None:
    """Resend only the failed posts for a submission."""
    settings = _load_settings()
    publisher = build_publisher(settings)

    if not yes:
        typer.echo("承認が必要です。--yes を付けて再実行してください。", err=True)
        raise typer.Exit(code=2)

    try:
        report = publisher.retry(UUID(submission_id), approved=True)
    except ApprovalRequiredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    _print_json(report_to_dict(report))
    if has_unsuccessful_result(report):
        raise typer.Exit(code=3)


@app.command()
def edit(
    post_id: str,
    caption_file: Annotated[
        Path | None, typer.Option("--caption-file", help="新しい本文を含むテキストファイル")
    ] = None,
    due_at: Annotated[str | None, typer.Option("--due-at", help="新しい予定日時（ISO8601）")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="承認済みとして実行する")] = False,
) -> None:
    """Edit a scheduled/draft post's caption and/or due date (also used by the admin dashboard)."""
    if not yes:
        typer.echo("承認が必要です。--yes を付けて再実行してください。", err=True)
        raise typer.Exit(code=2)

    caption = caption_file.read_text().strip() if caption_file is not None else None
    parsed_due_at: datetime | None = None
    if due_at is not None:
        try:
            parsed_due_at = datetime.fromisoformat(due_at)
        except ValueError as exc:
            typer.echo(f"--due-at の形式が不正です（ISO8601で指定してください）: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        if parsed_due_at.tzinfo is None:
            typer.echo("--due-at にはタイムゾーンを含めてください（例: 2030-01-01T09:00:00+09:00）", err=True)
            raise typer.Exit(code=1)

    settings = _load_settings()
    publisher = build_publisher(settings)
    try:
        result = publisher.edit(UUID(post_id), caption=caption, due_at=parsed_due_at)
    except (ValueError, BufferAmbiguousError, BufferProtocolError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    _print_json(target_result_to_dict(result))


@app.command()
def cancel(
    post_id: str,
    yes: Annotated[bool, typer.Option("--yes", help="承認済みとして実行する")] = False,
) -> None:
    """Cancel a scheduled/draft post (also used by the admin dashboard)."""
    if not yes:
        typer.echo("承認が必要です。--yes を付けて再実行してください。", err=True)
        raise typer.Exit(code=2)

    settings = _load_settings()
    publisher = build_publisher(settings)
    try:
        result = publisher.cancel(UUID(post_id))
    except (ValueError, BufferAmbiguousError, BufferProtocolError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    _print_json(target_result_to_dict(result))


@app.command()
def sync() -> None:
    """Poll Buffer for scheduled/draft posts and update Supabase.

    A manual/human-triggered sync (like the admin dashboard's "sync now"
    button) — reconciles every scheduled/draft post regardless of its local
    due_at, since a human may have rescheduled/canceled/edited directly in
    Buffer's UI (see Publisher.sync's `full` parameter).
    """
    settings = _load_settings()
    publisher = build_publisher(settings)
    counts = publisher.sync(full=True)
    _print_json(counts)


@app.command()
def introspect(
    out: Annotated[Path | None, typer.Option("--out", help="出力先ファイル")] = None,
) -> None:
    """Dump Buffer GraphQL `__type` introspection for CreatePostInput/Post."""
    settings = _load_settings()
    buffer = build_buffer_client(settings)
    data = buffer.introspect()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if out is not None:
        out.write_text(text)
    else:
        typer.echo(text)


# --- admin account management ---------------------------------------------------------


@admin_app.command("add")
def admin_add(
    login_id: str,
    display_name: Annotated[str | None, typer.Option("--display-name", help="表示名")] = None,
) -> None:
    """管理者アカウントを新規作成し、ID・メール・パスワードを一度だけ表示する。

    パスワードはここでしか表示されない（どこにも保存しない）。忘れた場合は
    `admin reset-password` で再発行すること。
    """
    settings = _load_settings()
    repo = build_repo(settings)
    try:
        account = create_admin_account(repo, login_id=login_id, display_name=display_name)
    except (InvalidLoginIdError, LoginIdAlreadyExistsError, AdminAccountRollbackError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    _print_json(
        {
            "id": account.login_id,
            "email": account.email,
            "display_name": account.display_name,
            "password": account.password,
        }
    )


@admin_app.command("reset-password")
def admin_reset_password(login_id: str) -> None:
    """既存の管理者アカウントのパスワードを再発行し、一度だけ表示する。"""
    settings = _load_settings()
    repo = build_repo(settings)
    try:
        reset = reset_admin_password(repo, login_id=login_id)
    except (InvalidLoginIdError, AdminUserNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    _print_json({"id": reset.login_id, "email": reset.email, "password": reset.password})


@admin_app.command("list")
def admin_list() -> None:
    """管理者アカウント一覧を表示する（パスワードは含まない）。"""
    settings = _load_settings()
    repo = build_repo(settings)
    accounts = list_admin_accounts(repo)
    _print_json(
        [{"id": a.login_id, "email": a.email, "display_name": a.display_name} for a in accounts]
    )
