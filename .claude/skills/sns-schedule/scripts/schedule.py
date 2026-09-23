#!/usr/bin/env python3
"""sns-schedule skill CLI: schedule clips to Buffer via the /schedule/* API.

Subcommands (see SKILL.md for the full workflow): check / probe / preview /
slots / validate / submit / gallery. Every subcommand prints JSON to stdout
on success; errors go to stderr with a nonzero exit code. The API key is
never printed or logged.

I/O (filesystem, subprocess, network) lives here; the pure scheduling and
validation rules live in schedule_core.py so they can be unit tested without
mocking ffmpeg/curl/urllib.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date as date_cls
from datetime import datetime, timedelta
from html import escape as escape_html
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from http_client import ApiError, request_json  # noqa: E402
from schedule_core import (  # noqa: E402
    JST,
    PREVIEW_CRF_FIRST,
    PREVIEW_CRF_RETRY,
    is_fully_settled,
    issues_to_dicts,
    merge_gallery_data,
    needs_transcode,
    parse_dotenv_text,
    parse_ffprobe,
    plan_has_error,
    propose_slots,
    resolve_day_times,
    resolve_main_video_url,
    submit_has_problems,
    summarize_submit_results,
    validate_clip_locally,
    validate_main_video_url_locally,
)

DEFAULT_THUMBNAIL_OFFSET_MS = 1000
DEFAULT_MAX_SUBMIT_RETRIES = 3
POSTS_RANGE_DAYS = 62


class CommandError(Exception):
    """A subprocess (ffprobe/ffmpeg/curl) failed."""


class SettingsError(Exception):
    """.env is missing SIZURU_API_URL / SIZURU_API_KEY."""


class StateError(Exception):
    """<plan>.state.json is missing content we need, corrupt, or its
    fingerprint no longer matches the clip's source file. Always a stop
    condition -- never silently reset/ignored, since that risks a
    re-upload (new video_key) or a double-booking of an already-scheduled
    clip."""


# --- settings ---------------------------------------------------------------


def find_dotenv(script_dir: Path | None = None) -> Path | None:
    """Locate the repo's .env. If CLAUDE_PROJECT_DIR is set, look ONLY at
    $CLAUDE_PROJECT_DIR/.env -- no walk-up. Falling through to a directory
    walk-up when a project dir IS set but has no .env would risk silently
    picking up an unrelated .env from above this script's own location
    (e.g. this repo's real, secret-bearing .env, if the skill were ever
    invoked from elsewhere with CLAUDE_PROJECT_DIR pointing at a project
    that genuinely has none configured). Only when CLAUDE_PROJECT_DIR is
    unset do we walk up from `script_dir` (defaults to this script's own
    directory) -- a fallback for running schedule.py directly, outside
    Claude Code. `script_dir` is a parameter (not hardcoded) so tests can
    walk up a tmp tree instead of this real repo's directory tree."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidate = Path(project_dir) / ".env"
        return candidate if candidate.is_file() else None

    start = script_dir if script_dir is not None else Path(__file__).resolve().parent
    for directory in (start, *start.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_settings() -> tuple[str, str]:
    api_url = os.environ.get("SIZURU_API_URL")
    api_key = os.environ.get("SIZURU_API_KEY")
    if not api_url or not api_key:
        dotenv_path = find_dotenv()
        if dotenv_path is not None:
            values = parse_dotenv_text(dotenv_path.read_text())
            api_url = api_url or values.get("SIZURU_API_URL")
            api_key = api_key or values.get("SIZURU_API_KEY")
    if not api_url or not api_key:
        raise SettingsError(
            "SIZURU_API_URL / SIZURU_API_KEY が設定されていません。"
            "リポジトリの .env に追記してください（値は表示しません）"
        )
    return api_url, api_key


# --- subprocess helpers (ffprobe / ffmpeg) ----------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise CommandError(f"コマンドに失敗しました: {' '.join(cmd)}\n{result.stderr}")
    return result


def run_ffprobe(path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = _run(cmd)
    return parse_ffprobe(json.loads(result.stdout))


def make_thumbnail(video: Path, out_path: Path, offset_ms: int | None) -> None:
    offset_sec = (offset_ms if offset_ms is not None else DEFAULT_THUMBNAIL_OFFSET_MS) / 1000
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{offset_sec:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        "scale=540:-2",
        "-q:v",
        "3",
        str(out_path),
    ]
    _run(cmd)


def _transcode_for_preview(src: Path, dst: Path, crf: int) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-vf",
        "scale=720:-2",  # width-only: height auto-derives (even) so non-9:16 sources aren't stretched
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    _run(cmd)


def _copy_preview(src: Path, dst: Path) -> None:
    """Always a real, independent copy -- never a hard link. The Artifact
    tool's upload_asset refuses hard links in a batch (`file_paths`) upload;
    each would otherwise need its own separate approval. Idempotent:
    re-running preview into the same --out simply overwrites `dst` (or, in
    the degenerate case where `dst` already IS `src` -- e.g. --out pointed
    at the source directory -- does nothing, since shutil.copyfile on two
    paths to the same inode would raise SameFileError)."""
    if dst.exists() and dst.samefile(src):
        return
    shutil.copyfile(src, dst)


def make_preview(src: Path, out_dir: Path) -> dict[str, Any]:
    dst = out_dir / src.name
    size = src.stat().st_size
    if not needs_transcode(size):
        _copy_preview(src, dst)
    else:
        _transcode_for_preview(src, dst, PREVIEW_CRF_FIRST)
        if needs_transcode(dst.stat().st_size):
            _transcode_for_preview(src, dst, PREVIEW_CRF_RETRY)
    probe = run_ffprobe(dst)
    return {
        "file": str(src),
        "preview_path": str(dst),
        "size_bytes": dst.stat().st_size,
        "duration_sec": probe["duration_sec"],
    }


# --- curl multipart upload ---------------------------------------------------


def multipart_upload(upload: dict[str, Any], file_path: Path) -> None:
    """POST `file_path` to a presigned S3 POST URL: all `fields` first (as
    literal --form-string values, so a policy/signature value that happens
    to start with '@' or '<' is never misread by curl as a file/stdin
    reference), the file field last via plain -F (S3 requires the file part
    be the last field). On failure, the raised message is curl's own
    stderr/body ONLY -- never the full argv, which contains short-lived but
    still-sensitive presigned policy/signature fields."""
    cmd = ["curl", "-sS", "--fail-with-body", "-X", "POST", upload["url"]]
    for key, value in upload["fields"].items():
        cmd += ["--form-string", f"{key}={value}"]
    cmd += ["-F", f"file=@{file_path}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise CommandError(f"アップロードに失敗しました（curl）: {detail}")


# --- state (plan.json.state.json) -------------------------------------------


def state_path_for(plan_path: Path) -> Path:
    return Path(str(plan_path) + ".state.json")


def load_state_file(path: Path) -> dict[str, Any]:
    """Load a state file from a concrete path. Missing is a legitimate
    "nothing submitted yet" (empty state); anything present but corrupt or
    the wrong shape is a StateError -- silently resetting it would let
    `submit` re-upload (new video_key) or re-book an already-settled clip."""
    if not path.is_file():
        return {"clips": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise StateError(
            f"状態ファイルが壊れています（{path}）。誤って再アップロード/二重予約しないよう、"
            f"自動では扱いません。内容を確認してから対応してください: {exc}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("clips"), dict):
        raise StateError(f"状態ファイルの形式が不正です（{path}）。内容を確認してください")
    return data


def load_state(plan_path: Path) -> dict[str, Any]:
    return load_state_file(state_path_for(plan_path))


def save_state(plan_path: Path, state: dict[str, Any]) -> None:
    """Atomic write (tmp file + os.replace) so a crash mid-write never
    leaves a half-written/corrupt state file behind."""
    path = state_path_for(plan_path)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.replace(tmp_path, path)


def clip_fingerprint(path: Path) -> dict[str, Any]:
    """Identify a clip's source file well enough to detect "this is a
    different file than what we uploaded last time" (absolute path + size +
    mtime). Not a content hash -- cheap, and good enough to catch the
    realistic case (re-exported/re-edited file at the same path)."""
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime": stat.st_mtime}


# --- submission body ----------------------------------------------------------


def build_submission_body(
    clip: dict[str, Any],
    video_key: str,
    thumbnail_key: str | None,
    video_probe: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "video_key": video_key,
        "thumbnail_key": thumbnail_key,
        "title": clip.get("title"),
        "video": {
            "duration_sec": video_probe["duration_sec"],
            "width": video_probe["width"],
            "height": video_probe["height"],
            "video_codec": video_probe["video_codec"],
            "audio_codec": video_probe.get("audio_codec"),
        },
        "targets": [_target_body(t) for t in clip.get("targets", [])],
        "thumbnail_offset_ms": clip.get("thumbnail_offset_ms"),
        "dry_run": dry_run,
    }


def _target_body(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "service": target["service"],
        "caption": target["caption"],
        "due_at": target["due_at"],
        "tiktok_title": target.get("tiktok_title"),
        "share_to_feed": target.get("share_to_feed", True),
    }


# --- upload orchestration (idempotent via state) -----------------------------


def ensure_uploaded(
    clip: dict[str, Any],
    clip_state: dict[str, Any],
    api_url: str,
    api_key: str,
    plan_path: Path,
    state: dict[str, Any],
) -> None:
    """Upload the video (and a freshly-generated thumbnail) if not already
    marked uploaded in `clip_state`. Only the assets that never succeeded
    request new presigned URLs; a previously-uploaded asset's key is kept.

    If we have a fingerprint on record for this clip, it must still match
    the file on disk -- otherwise we'd silently reuse a video_key that
    points to different (stale) S3 content than what's about to be booked."""
    both_done = bool(clip_state.get("video_uploaded")) and bool(clip_state.get("thumbnail_uploaded"))
    stored_fingerprint = clip_state.get("fingerprint")

    if both_done and stored_fingerprint is None:
        return  # nothing left to do, nothing on record to verify -- skip filesystem access

    current_fingerprint = clip_fingerprint(Path(clip["file"]))
    if stored_fingerprint is not None and stored_fingerprint != current_fingerprint:
        raise StateError(
            f"クリップ{clip.get('no')}のファイルが前回アップロード時から変わっています"
            f"（{clip['file']}）。誤って別内容を予約しないよう、既存のvideo_key/thumbnail_keyは"
            "再利用しません。ファイルを元に戻すか、状態ファイルの該当エントリを確認してください。"
        )
    if both_done:
        return

    uploads = request_json(api_url, api_key, "POST", "/schedule/uploads", body={})

    if not clip_state.get("video_uploaded"):
        video_path = Path(clip["file"])
        multipart_upload(uploads["video"]["upload"], video_path)
        clip_state["video_key"] = uploads["video"]["key"]
        clip_state["video_uploaded"] = True
        clip_state["fingerprint"] = current_fingerprint
        save_state(plan_path, state)

    if not clip_state.get("thumbnail_uploaded"):
        with tempfile.TemporaryDirectory() as tmp:
            thumbnail_path = Path(tmp) / "thumbnail.jpg"
            make_thumbnail(Path(clip["file"]), thumbnail_path, clip.get("thumbnail_offset_ms"))
            multipart_upload(uploads["thumbnail"]["upload"], thumbnail_path)
        clip_state["thumbnail_key"] = uploads["thumbnail"]["key"]
        clip_state["thumbnail_uploaded"] = True
        clip_state["fingerprint"] = current_fingerprint
        save_state(plan_path, state)


def submit_clip(
    clip: dict[str, Any],
    clip_state: dict[str, Any],
    api_url: str,
    api_key: str,
    plan_path: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Submit one clip. Never raises ApiError -- a network/API failure for
    this clip is recorded on its result so `cmd_submit` can continue with
    the rest of the plan (StateError, e.g. a fingerprint mismatch, is NOT
    caught here: that's a data-integrity stop condition for the whole run)."""
    previous = clip_state.get("submission_result")
    if previous and is_fully_settled(previous.get("results", [])):
        return {"clip_no": clip["no"], "skipped": False, "already_settled": True, "result": previous}

    try:
        ensure_uploaded(clip, clip_state, api_url, api_key, plan_path, state)

        video_probe = run_ffprobe(Path(clip["file"]))
        body = build_submission_body(
            clip, clip_state["video_key"], clip_state.get("thumbnail_key"), video_probe, dry_run=True
        )
        dry = request_json(api_url, api_key, "POST", "/schedule/submissions", body=body)
        if dry["has_error"]:
            clip_state["last_dry_run"] = dry["plan"]
            save_state(plan_path, state)
            return {"clip_no": clip["no"], "skipped": True, "plan": dry["plan"]}

        body["dry_run"] = False
        result = request_json(api_url, api_key, "POST", "/schedule/submissions", body=body)
        attempts = 1
        while result.get("incomplete") and attempts < DEFAULT_MAX_SUBMIT_RETRIES:
            result = request_json(api_url, api_key, "POST", "/schedule/submissions", body=body)
            attempts += 1
    except ApiError as exc:
        clip_state["last_error"] = str(exc)
        save_state(plan_path, state)
        return {"clip_no": clip["no"], "skipped": False, "error": str(exc)}

    clip_state["submission_result"] = result
    save_state(plan_path, state)
    return {"clip_no": clip["no"], "skipped": False, "result": result}


# --- gallery ------------------------------------------------------------------


def inject_gallery_data(html: str, data: dict[str, Any]) -> str:
    project = data.get("project")
    if project is not None:
        # The Artifact tool names the published page from this static
        # <title> tag -- document.title set by the page's own JS is
        # ignored -- so every generated page needs its own title, not the
        # template's static placeholder (else every published page would
        # share the same name). No-op if the template has no <title> tag.
        title_text = escape_html(f"{project} 切り抜き")
        html = re.sub(r"<title>.*?</title>", f"<title>{title_text}</title>", html, count=1, flags=re.DOTALL)

    json_text = json.dumps(data, ensure_ascii=False)
    # Escape the 3 characters that could let a value break out of the
    # <script type="application/json"> tag (e.g. a caption containing
    # "</script>" or "<!--<script"). These can only appear inside JSON
    # string values (JSON's own structural characters are {}[]:," and
    # whitespace), so \uXXXX-escaping them is always valid JSON wherever it
    # occurs -- JSON.parse() decodes it back to the exact original text.
    for char, escaped in (("<", "\\u003C"), (">", "\\u003E"), ("&", "\\u0026")):
        json_text = json_text.replace(char, escaped)
    pattern = re.compile(
        r'(<script type="application/json" id="gallery-data">).*?(</script>)', re.DOTALL
    )

    def _replace(match: re.Match[str]) -> str:
        return match.group(1) + json_text + match.group(2)

    new_html, count = pattern.subn(_replace, html, count=1)
    if count == 0:
        raise ValueError("テンプレートに gallery-data のプレースホルダが見つかりません")
    return new_html


# --- CLI subcommands ------------------------------------------------------------


def _check_failure_reason(exc: ApiError) -> str:
    if exc.status == 401 or exc.code == "unauthorized":
        return "unauthorized"
    if exc.status == 503 or exc.code == "scheduler_disabled":
        return "scheduler_disabled"
    return "unreachable"


def cmd_check(args: argparse.Namespace) -> int:
    api_url, api_key = load_settings()
    now = datetime.now(JST)
    frm = now.isoformat()
    to = (now + timedelta(days=1)).isoformat()
    try:
        request_json(api_url, api_key, "GET", f"/schedule/posts?from={quote(frm)}&to={quote(to)}")
    except ApiError as exc:
        reason = _check_failure_reason(exc)
        print(json.dumps({"ok": False, "reason": reason, "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True}, ensure_ascii=False))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    path = Path(args.mp4)
    result = run_ffprobe(path)
    result["size_bytes"] = path.stat().st_size
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _collect_preview_inputs(paths: list[str]) -> list[Path]:
    if len(paths) == 1 and Path(paths[0]).is_dir():
        return sorted(Path(paths[0]).glob("*.mp4"), key=lambda p: p.name)
    return sorted((Path(p) for p in paths), key=lambda p: p.name)


def cmd_preview(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    if "out" in out_dir.parts:
        print(
            "--out のパスに「out」という名前のディレクトリを含められません"
            "（Stopフックの再発火防止。例: 納品フォルダの out/ 直下ではなく別名にしてください）",
            file=sys.stderr,
        )
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs = _collect_preview_inputs(args.inputs)
    results = []
    for no, src in enumerate(inputs, start=1):
        preview = make_preview(src, out_dir)
        preview["no"] = no
        results.append(preview)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_slots(args: argparse.Namespace) -> int:
    api_url, api_key = load_settings()
    start = date_cls.fromisoformat(args.start)
    times = resolve_day_times(args.time, args.times, args.per_day)
    now = datetime.now(JST)

    from_dt = datetime.combine(start, datetime.min.time(), tzinfo=JST)
    to_dt = from_dt + timedelta(days=POSTS_RANGE_DAYS)
    posts = request_json(
        api_url,
        api_key,
        "GET",
        f"/schedule/posts?from={quote(from_dt.isoformat())}&to={quote(to_dt.isoformat())}",
    )["posts"]
    existing = [datetime.fromisoformat(p["due_at"]) for p in posts]

    # Bound the search to the days we actually fetched `existing` posts for
    # (POSTS_RANGE_DAYS): searching further would silently treat unfetched
    # (possibly already-booked) days as free.
    slots = propose_slots(existing, start, times, args.per_day, args.count, now, max_days=POSTS_RANGE_DAYS)
    print(json.dumps([s.isoformat() for s in slots], ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan).read_text())
    now = datetime.now(JST)
    all_issues = []
    for clip in plan["clips"]:
        probe = run_ffprobe(Path(clip["file"]))
        all_issues.extend(validate_clip_locally(clip, probe["duration_sec"], now))
        main_video_url = resolve_main_video_url(plan, clip)
        all_issues.extend(validate_main_video_url_locally(clip, main_video_url))
    print(json.dumps(issues_to_dicts(all_issues), ensure_ascii=False, indent=2))
    return 1 if plan_has_error(all_issues) else 0


def cmd_submit(args: argparse.Namespace) -> int:
    if not args.yes:
        print("承認が必要です（--yes を付けて、会話内で承認を得てから再実行してください）", file=sys.stderr)
        return 2

    api_url, api_key = load_settings()
    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text())
    state = load_state(plan_path)
    clips_state: dict[str, Any] = state.setdefault("clips", {})

    results = []
    for clip in plan["clips"]:
        key = str(clip["no"])
        clip_state = clips_state.setdefault(key, {})
        results.append(submit_clip(clip, clip_state, api_url, api_key, plan_path, state))

    summary = summarize_submit_results(results)
    print(json.dumps({"clips": results, "summary": summary}, ensure_ascii=False, indent=2))
    return 1 if submit_has_problems(results, summary) else 0


def cmd_gallery(args: argparse.Namespace) -> int:
    base = json.loads(Path(args.data).read_text())
    plan = json.loads(Path(args.plan).read_text()) if args.plan else None
    state = None
    if plan is not None:
        state_path = Path(args.state) if args.state else state_path_for(Path(args.plan))
        state = load_state_file(state_path)
    data = merge_gallery_data(base, plan, state)

    template_path = Path(__file__).resolve().parent.parent / "templates" / "clips_gallery.html"
    html = inject_gallery_data(template_path.read_text(), data)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(html)
    print(json.dumps({"out": str(args.out)}, ensure_ascii=False))
    return 0


# --- argument parsing ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schedule.py", description="sns-schedule skill CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="API疎通確認").set_defaults(func=cmd_check)

    probe_parser = subparsers.add_parser("probe", help="1本のmp4をffprobe")
    probe_parser.add_argument("mp4")
    probe_parser.set_defaults(func=cmd_probe)

    preview_parser = subparsers.add_parser("preview", help="納品フォルダ/mp4群からプレビューを作る")
    preview_parser.add_argument("inputs", nargs="+")
    preview_parser.add_argument("--out", required=True)
    preview_parser.set_defaults(func=cmd_preview)

    slots_parser = subparsers.add_parser("slots", help="空き枠を提案する")
    slots_parser.add_argument("--start", required=True)
    slots_parser.add_argument("--per-day", type=int, required=True)
    slots_parser.add_argument("--count", type=int, required=True)
    time_group = slots_parser.add_mutually_exclusive_group(required=True)
    time_group.add_argument("--time")
    time_group.add_argument("--times")
    slots_parser.set_defaults(func=cmd_slots)

    validate_parser = subparsers.add_parser("validate", help="plan.jsonをローカル検証する")
    validate_parser.add_argument("plan")
    validate_parser.set_defaults(func=cmd_validate)

    submit_parser = subparsers.add_parser("submit", help="plan.jsonをアップロード・予約する")
    submit_parser.add_argument("plan")
    submit_parser.add_argument("--yes", action="store_true")
    submit_parser.set_defaults(func=cmd_submit)

    gallery_parser = subparsers.add_parser("gallery", help="ギャラリーHTMLを生成する")
    gallery_parser.add_argument("--data", required=True, help="base.json（動画・タイトル案など）")
    gallery_parser.add_argument("--plan", help="plan.json。指定するとschedule情報をdataへマージする")
    gallery_parser.add_argument("--state", help="状態ファイル（省略時は <--plan>.state.json）")
    gallery_parser.add_argument("--out", required=True)
    gallery_parser.set_defaults(func=cmd_gallery)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ApiError, CommandError, SettingsError, StateError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
