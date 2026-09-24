"""Admin-dashboard login account management (`sizuru-post admin ...`).

Sits between cli.py (I/O: argument parsing, stdout/exit codes) and
Repo/SupabaseRepo (I/O: Supabase Admin Auth API + the `admin_users` table) —
this module holds the pure login-id/email/password rules and the
create/reset/list orchestration, so both are unit-testable without a
network (see tests/test_admin_accounts.py) and without typer
(tests/test_cli.py only has to check argument wiring and exit codes).

Login IDs are short dummy strings (e.g. `sizuru-admin1`); Supabase Auth
still needs an email, so we map id -> `<id>@sizuru.local` unless the id
already looks like a real email (e.g. the existing admin,
kiyoto_hata@levela.co.jp, keeps using their real address). This mapping
must match the frontend's `lib/auth.ts` exactly — see plan
tender-wibbling-boot.md section 2.
"""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sizuru_post.adapters.repo import AdminUserRow, AuthUserAlreadyExistsError

_LOGIN_EMAIL_DOMAIN = "sizuru.local"
_LOGIN_EMAIL_SUFFIX = f"@{_LOGIN_EMAIL_DOMAIN}"

LOGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
# A deliberately simple "looks like an email" check (not RFC 5322) — this
# only guards against obvious typos before we hand the value to Supabase;
# the length cap matches RFC 5321's overall address limit.
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MAX_EMAIL_LENGTH = 254

_PASSWORD_LENGTH = 16
_MIN_PASSWORD_LENGTH = 8
# Excludes visually-ambiguous characters (0/O, l/1) so a password read aloud
# or hand-copied isn't misread.
_PASSWORD_LETTERS = "".join(c for c in string.ascii_letters if c not in "lO")
_PASSWORD_DIGITS = "".join(c for c in string.digits if c not in "01")
_PASSWORD_ALPHABET = _PASSWORD_LETTERS + _PASSWORD_DIGITS


class InvalidLoginIdError(ValueError):
    """`login_id` matches neither the dummy-id pattern nor "looks like an
    email" (contains `@`) — see normalize_login_id()."""


class LoginIdAlreadyExistsError(ValueError):
    """create_admin_account(): an account for this login_id already exists.
    Never overwrite/reset an existing account from `admin add` — the caller
    must use `admin reset-password` instead."""


class AdminUserNotFoundError(ValueError):
    """reset_admin_password(): login_id has no matching `admin_users` row
    (whether or not an unrelated auth user happens to exist for that
    email — see reset_admin_password()'s security note)."""


class AdminAccountRollbackError(RuntimeError):
    """create_admin_account(): the `admin_users` insert failed AND the
    compensating delete of the just-created auth user also failed. The auth
    user `user_id` is now orphaned (exists in auth.users with no matching
    admin_users row) and needs manual cleanup — both underlying errors are
    included in the message so neither is silently masked."""

    def __init__(self, user_id: UUID, insert_error: Exception, delete_error: Exception) -> None:
        super().__init__(
            "admin_users への登録に失敗し、ロールバック（auth ユーザー削除）にも失敗しました。"
            f"auth ユーザー {user_id} が孤立している可能性があるため、手動で確認・削除してください。"
            f" insert error: {insert_error!r} / rollback delete error: {delete_error!r}"
        )
        self.user_id = user_id
        self.insert_error = insert_error
        self.delete_error = delete_error


class _AdminRepoPort(Protocol):
    """The slice of Repo this module needs — kept narrow and local, same
    pattern as channels_sync.py's _BufferPort/_RepoPort."""

    def create_auth_user(self, *, email: str, password: str) -> UUID: ...

    def delete_auth_user(self, user_id: UUID) -> None: ...

    def insert_admin_user(self, *, user_id: UUID, email: str, display_name: str | None) -> None: ...

    def find_admin_user_id_by_email(self, email: str) -> UUID | None: ...

    def update_auth_user_password(self, user_id: UUID, password: str) -> None: ...

    def list_admin_users(self) -> list[AdminUserRow]: ...


def normalize_login_id(login_id: str) -> str:
    """Trim + lowercase (matching the frontend's normalization exactly),
    then validate: a dummy id must match LOGIN_ID_PATTERN, and anything
    containing `@` must look like a syntactically well-formed email (and
    fit within RFC 5321's 254-character address limit). Raises
    InvalidLoginIdError otherwise."""
    normalized = login_id.strip().lower()
    if "@" in normalized:
        if len(normalized) > _MAX_EMAIL_LENGTH or not _EMAIL_PATTERN.fullmatch(normalized):
            raise InvalidLoginIdError(f"メールアドレスの形式が不正です: {login_id}")
        return normalized
    if not LOGIN_ID_PATTERN.fullmatch(normalized):
        raise InvalidLoginIdError(
            "id の形式が不正です（英小文字・数字・ハイフンで3〜32文字、先頭は英数字にしてください）: "
            f"{login_id}"
        )
    return normalized


def to_login_email(login_id: str) -> str:
    """Must match the frontend's login-email mapping exactly (lib/auth.ts)."""
    if "@" in login_id:
        return login_id
    return f"{login_id}{_LOGIN_EMAIL_SUFFIX}"


def login_id_from_email(email: str) -> str:
    """Inverse of to_login_email(), for `admin list` — a real email (no
    dummy suffix) is shown as-is."""
    if email.endswith(_LOGIN_EMAIL_SUFFIX):
        return email[: -len(_LOGIN_EMAIL_SUFFIX)]
    return email


def generate_password(length: int = _PASSWORD_LENGTH) -> str:
    """A `secrets`-backed random password with at least one letter and one
    digit, excluding ambiguous characters. Resamples the whole password
    (rather than patching a fixed password after the fact) so every
    character stays uniformly drawn from `secrets.choice`.

    Rejects length < 8 outright: below that, "at least one letter and one
    digit" can make the resampling loop take a very long time to succeed
    (or never, at length 0), rather than actually being insecure at exactly
    8 — this is a sanity guard on the input, not a security cliff."""
    if length < _MIN_PASSWORD_LENGTH:
        raise ValueError(f"password length must be at least {_MIN_PASSWORD_LENGTH} characters: {length}")
    while True:
        password = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        if any(c in _PASSWORD_LETTERS for c in password) and any(c in _PASSWORD_DIGITS for c in password):
            return password


@dataclass(frozen=True)
class AdminAccountCreated:
    login_id: str
    email: str
    display_name: str | None
    password: str


@dataclass(frozen=True)
class AdminPasswordReset:
    login_id: str
    email: str
    password: str


@dataclass(frozen=True)
class AdminAccountSummary:
    login_id: str
    email: str
    display_name: str | None


def create_admin_account(
    repo: _AdminRepoPort, *, login_id: str, display_name: str | None
) -> AdminAccountCreated:
    """Create a Supabase auth user + matching `admin_users` row.

    The two writes are treated as one transaction: if the `admin_users`
    insert fails after the auth user was created, the auth user is deleted
    again so no orphaned auth.users row is left behind with no matching
    admin_users row. If that rollback delete *also* fails, both errors are
    raised together as AdminAccountRollbackError instead of letting the
    delete's outcome silently replace or hide the original insert failure.
    """
    normalized_id = normalize_login_id(login_id)
    email = to_login_email(normalized_id)
    password = generate_password()

    try:
        user_id = repo.create_auth_user(email=email, password=password)
    except AuthUserAlreadyExistsError as exc:
        raise LoginIdAlreadyExistsError(f"既にアカウントが存在します: {normalized_id}") from exc

    try:
        repo.insert_admin_user(user_id=user_id, email=email, display_name=display_name)
    except Exception as insert_exc:
        try:
            repo.delete_auth_user(user_id)
        except Exception as delete_exc:
            raise AdminAccountRollbackError(user_id, insert_exc, delete_exc) from delete_exc
        raise

    return AdminAccountCreated(
        login_id=normalized_id, email=email, display_name=display_name, password=password
    )


def reset_admin_password(repo: _AdminRepoPort, *, login_id: str) -> AdminPasswordReset:
    """Issue a new password for an existing *admin* account only.

    Resolves the target via `admin_users` (by email), not a full
    auth-directory scan — an auth user that exists but was never granted
    admin access must never have its password reset through here. Never
    reveals whether a login_id exists via a different error message than
    "not found" — the caller just gets AdminUserNotFoundError either way.
    """
    normalized_id = normalize_login_id(login_id)
    email = to_login_email(normalized_id)
    user_id = repo.find_admin_user_id_by_email(email)
    if user_id is None:
        raise AdminUserNotFoundError(f"見つかりません: {normalized_id}")

    password = generate_password()
    repo.update_auth_user_password(user_id, password)
    return AdminPasswordReset(login_id=normalized_id, email=email, password=password)


def list_admin_accounts(repo: _AdminRepoPort) -> list[AdminAccountSummary]:
    """No passwords here — `admin_users` never stores one."""
    return [
        AdminAccountSummary(
            login_id=login_id_from_email(row.email), email=row.email, display_name=row.display_name
        )
        for row in repo.list_admin_users()
    ]
