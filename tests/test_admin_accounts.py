"""Tests for sizuru_post.admin_accounts: login-id normalization, email
mapping, password generation, and the add/reset-password/list orchestration
that sits between the CLI and Repo (see plan tender-wibbling-boot.md
section 2, and the Codex review follow-up: reset-password must only ever
touch an actual admin, ids are normalized consistently with the frontend,
a failed rollback must not mask the original error, and generate_password
must not spin forever on a too-short length)."""

from __future__ import annotations

import string

import pytest

from sizuru_post.admin_accounts import (
    AdminAccountRollbackError,
    AdminUserNotFoundError,
    InvalidLoginIdError,
    LoginIdAlreadyExistsError,
    create_admin_account,
    generate_password,
    list_admin_accounts,
    login_id_from_email,
    normalize_login_id,
    reset_admin_password,
    to_login_email,
)
from tests.fakes import InMemoryRepo


class TestNormalizeLoginId:
    @pytest.mark.parametrize("login_id", ["sizuru-admin1", "abc", "a" * 32])
    def test_accepts_valid_dummy_ids_unchanged(self, login_id: str) -> None:
        assert normalize_login_id(login_id) == login_id

    def test_trims_and_lowercases_dummy_ids(self) -> None:
        assert normalize_login_id("  Sizuru-Admin1  ") == "sizuru-admin1"

    @pytest.mark.parametrize(
        "login_id",
        ["ab", "-abc", "has space", "a" * 33, "under_score"],
    )
    def test_rejects_invalid_dummy_ids(self, login_id: str) -> None:
        with pytest.raises(InvalidLoginIdError):
            normalize_login_id(login_id)

    def test_accepts_and_lowercases_a_well_formed_email(self) -> None:
        assert normalize_login_id("Kiyoto_Hata@Levela.co.jp") == "kiyoto_hata@levela.co.jp"

    def test_trims_whitespace_around_an_email(self) -> None:
        assert normalize_login_id("  kiyoto_hata@levela.co.jp  ") == "kiyoto_hata@levela.co.jp"

    @pytest.mark.parametrize(
        "login_id",
        ["not-an-email@", "@no-local-part.com", "double@@at.com", "no-tld@localhost"],
    )
    def test_rejects_malformed_emails(self, login_id: str) -> None:
        with pytest.raises(InvalidLoginIdError):
            normalize_login_id(login_id)

    def test_rejects_emails_longer_than_254_characters(self) -> None:
        too_long = f"{'a' * 250}@example.com"
        with pytest.raises(InvalidLoginIdError):
            normalize_login_id(too_long)

    def test_accepts_an_email_of_exactly_254_characters(self) -> None:
        # Arrange: local(242) + "@" + "example.com"(11) == 254, the RFC 5321 limit.
        email = f"{'a' * 242}@example.com"
        assert len(email) == 254

        # Act / Assert
        assert normalize_login_id(email) == email

    def test_rejects_an_email_of_exactly_255_characters(self) -> None:
        # Arrange: one character over the 254 boundary tested above.
        email = f"{'a' * 243}@example.com"
        assert len(email) == 255

        # Act / Assert
        with pytest.raises(InvalidLoginIdError):
            normalize_login_id(email)

    def test_accepts_an_email_with_a_plus_tag_and_a_multi_level_subdomain(self) -> None:
        assert (
            normalize_login_id("Kiyoto+Tag@Mail.Sub.Example.co.jp")
            == "kiyoto+tag@mail.sub.example.co.jp"
        )

    @pytest.mark.parametrize(
        "login_id",
        [
            "café-admin1",  # unicode letter, no leading @
            "３izuru-admin1",  # fullwidth digit (U+FF13), no leading @
            "ｓｉｚｕｒｕ－ａｄｍｉｎ",  # entirely fullwidth id
        ],
    )
    def test_rejects_unicode_and_fullwidth_dummy_ids(self, login_id: str) -> None:
        with pytest.raises(InvalidLoginIdError):
            normalize_login_id(login_id)


class TestToLoginEmail:
    def test_appends_the_dummy_domain(self) -> None:
        assert to_login_email("sizuru-admin1") == "sizuru-admin1@sizuru.local"

    def test_leaves_existing_emails_untouched(self) -> None:
        assert to_login_email("kiyoto_hata@levela.co.jp") == "kiyoto_hata@levela.co.jp"


class TestLoginIdFromEmail:
    def test_strips_the_dummy_domain(self) -> None:
        assert login_id_from_email("sizuru-admin1@sizuru.local") == "sizuru-admin1"

    def test_leaves_other_domains_as_is(self) -> None:
        assert login_id_from_email("kiyoto_hata@levela.co.jp") == "kiyoto_hata@levela.co.jp"


class TestGeneratePassword:
    def test_default_length_is_16(self) -> None:
        assert len(generate_password()) == 16

    def test_contains_at_least_one_letter_and_one_digit(self) -> None:
        password = generate_password()
        assert any(c in string.ascii_letters for c in password)
        assert any(c in string.digits for c in password)

    def test_never_contains_ambiguous_characters(self) -> None:
        password = generate_password()
        assert not any(c in "0Ol1" for c in password)

    def test_generates_different_passwords_each_call(self) -> None:
        assert generate_password() != generate_password()

    def test_accepts_the_minimum_length_of_8(self) -> None:
        assert len(generate_password(length=8)) == 8

    @pytest.mark.parametrize("length", [0, 1, 7])
    def test_rejects_lengths_below_8(self, length: int) -> None:
        with pytest.raises(ValueError):
            generate_password(length=length)

    def test_every_password_over_many_samples_has_both_classes_correct_length_and_no_ambiguous_chars(
        self,
    ) -> None:
        # Arrange / Act: a single sample could pass these invariants by luck
        # (e.g. never drawing an ambiguous character); sampling many times
        # makes a violation of any invariant far more likely to surface.
        passwords = [generate_password() for _ in range(200)]

        # Assert
        for password in passwords:
            assert len(password) == 16
            assert any(c in string.ascii_letters for c in password)
            assert any(c in string.digits for c in password)
            assert not any(c in "0Ol1" for c in password)


class TestCreateAdminAccount:
    def test_creates_auth_user_and_admin_row_and_returns_the_password_once(self) -> None:
        # Arrange
        repo = InMemoryRepo()

        # Act
        account = create_admin_account(repo, login_id="sizuru-admin1", display_name="管理者1")

        # Assert
        assert account.login_id == "sizuru-admin1"
        assert account.email == "sizuru-admin1@sizuru.local"
        assert account.display_name == "管理者1"
        assert len(account.password) == 16
        rows = repo.list_admin_users()
        assert len(rows) == 1
        assert rows[0].email == "sizuru-admin1@sizuru.local"
        assert rows[0].display_name == "管理者1"

    def test_normalizes_the_login_id_before_creating(self) -> None:
        # Arrange
        repo = InMemoryRepo()

        # Act
        account = create_admin_account(repo, login_id="  Sizuru-Admin1  ", display_name=None)

        # Assert
        assert account.login_id == "sizuru-admin1"
        assert account.email == "sizuru-admin1@sizuru.local"

    def test_rejects_invalid_login_id_without_touching_the_repo(self) -> None:
        # Arrange
        repo = InMemoryRepo()

        # Act / Assert
        with pytest.raises(InvalidLoginIdError):
            create_admin_account(repo, login_id="AB", display_name=None)
        assert repo.list_admin_users() == []

    def test_duplicate_login_id_errors_without_overwriting(self) -> None:
        # Arrange
        repo = InMemoryRepo()
        create_admin_account(repo, login_id="sizuru-admin1", display_name="管理者1")

        # Act / Assert
        with pytest.raises(LoginIdAlreadyExistsError):
            create_admin_account(repo, login_id="sizuru-admin1", display_name="別名")
        rows = repo.list_admin_users()
        assert len(rows) == 1
        assert rows[0].display_name == "管理者1"

    def test_rolls_back_the_auth_user_when_the_admin_users_insert_fails(self) -> None:
        # Arrange: e.g. a transient DB error right after the auth user was
        # created — must not leave an orphaned auth.users row with no
        # matching admin_users row.
        repo = InMemoryRepo()
        repo.insert_admin_user_exception = RuntimeError("db down")

        # Act / Assert
        with pytest.raises(RuntimeError):
            create_admin_account(repo, login_id="sizuru-admin1", display_name=None)
        assert repo.find_admin_user_id_by_email("sizuru-admin1@sizuru.local") is None
        assert repo.list_admin_users() == []

    def test_raises_a_combined_error_when_the_rollback_itself_also_fails(self) -> None:
        # Arrange: both the admin_users insert AND the compensating auth-user
        # delete fail — neither error may be silently swallowed, and the
        # orphaned auth user id must be surfaced for manual cleanup.
        repo = InMemoryRepo()
        repo.insert_admin_user_exception = RuntimeError("db down")
        repo.delete_auth_user_exception = RuntimeError("network down")

        # Act / Assert
        with pytest.raises(AdminAccountRollbackError) as exc_info:
            create_admin_account(repo, login_id="sizuru-admin1", display_name=None)
        message = str(exc_info.value)
        assert "db down" in message
        assert "network down" in message
        assert str(exc_info.value.user_id) in message


class TestResetAdminPassword:
    def test_generates_a_new_password_for_an_existing_account(self) -> None:
        # Arrange
        repo = InMemoryRepo()
        create_admin_account(repo, login_id="sizuru-admin1", display_name=None)

        # Act
        reset = reset_admin_password(repo, login_id="sizuru-admin1")

        # Assert
        assert reset.login_id == "sizuru-admin1"
        assert reset.email == "sizuru-admin1@sizuru.local"
        assert len(reset.password) == 16

    def test_normalizes_the_login_id_before_looking_it_up(self) -> None:
        # Arrange
        repo = InMemoryRepo()
        create_admin_account(repo, login_id="sizuru-admin1", display_name=None)

        # Act
        reset = reset_admin_password(repo, login_id="  Sizuru-Admin1  ")

        # Assert
        assert reset.login_id == "sizuru-admin1"

    def test_unknown_login_id_raises(self) -> None:
        # Arrange
        repo = InMemoryRepo()

        # Act / Assert
        with pytest.raises(AdminUserNotFoundError):
            reset_admin_password(repo, login_id="no-such-admin")

    def test_does_not_reset_an_auth_user_that_is_not_an_admin(self) -> None:
        # Arrange: SECURITY — an auth user exists for this email (e.g. some
        # unrelated account) but was never added to admin_users. Resetting
        # its password through this path would let anyone who can guess/
        # brute-force login ids hijack an arbitrary account.
        repo = InMemoryRepo()
        repo.create_auth_user(email="sizuru-admin1@sizuru.local", password="whatever-not-admin1")

        # Act / Assert
        with pytest.raises(AdminUserNotFoundError):
            reset_admin_password(repo, login_id="sizuru-admin1")


class TestListAdminAccounts:
    def test_lists_accounts_without_passwords(self) -> None:
        # Arrange
        repo = InMemoryRepo()
        create_admin_account(repo, login_id="sizuru-admin1", display_name="管理者1")
        create_admin_account(repo, login_id="sizuru-admin2", display_name="管理者2")

        # Act
        accounts = list_admin_accounts(repo)

        # Assert
        assert {a.login_id for a in accounts} == {"sizuru-admin1", "sizuru-admin2"}
        assert not any(hasattr(a, "password") for a in accounts)
