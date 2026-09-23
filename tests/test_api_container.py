"""Tests for sizuru_post.api._container: the per-container cache shared by
api/handler.py's admin routes and api/schedule.py's /schedule/* routes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import sizuru_post.api._container as container
from sizuru_post.config import Settings


def _fake_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "buffer_api_key": "k",
        "supabase_url": "https://example.supabase.co",
        "supabase_service_role_key": "k",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_container_state() -> Iterator[None]:
    """Every test starts from (and leaves) a clean slate, regardless of
    what an earlier test left cached."""
    container.reset()
    yield
    container.reset()


class TestGetSettingsCaching:
    def test_caches_within_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        calls: list[Settings] = []

        def _build(**kwargs: object) -> Settings:
            settings = _fake_settings()
            calls.append(settings)
            return settings

        monkeypatch.setattr(container, "build_settings", _build)
        monkeypatch.setattr(container, "_clock", lambda: 1000.0)

        # Act
        first = container.get_settings()
        second = container.get_settings()

        # Assert
        assert first is second
        assert len(calls) == 1

    def test_rebuilds_after_ttl_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        calls: list[Settings] = []

        def _build(**kwargs: object) -> Settings:
            settings = _fake_settings()
            calls.append(settings)
            return settings

        monkeypatch.setattr(container, "build_settings", _build)
        now = [1000.0]
        monkeypatch.setattr(container, "_clock", lambda: now[0])

        # Act
        first = container.get_settings()
        now[0] += 301.0  # past the 300s TTL
        second = container.get_settings()

        # Assert
        assert first is not second
        assert len(calls) == 2

    def test_still_within_ttl_boundary_does_not_rebuild(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        calls: list[Settings] = []

        def _build(**kwargs: object) -> Settings:
            settings = _fake_settings()
            calls.append(settings)
            return settings

        monkeypatch.setattr(container, "build_settings", _build)
        now = [1000.0]
        monkeypatch.setattr(container, "_clock", lambda: now[0])

        # Act
        container.get_settings()
        now[0] += 299.0  # still within the 300s TTL
        container.get_settings()

        # Assert
        assert len(calls) == 1

    def test_exactly_at_ttl_boundary_does_not_rebuild(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: staleness is `age > _SETTINGS_TTL_SECONDS` (strict), so
        # age landing exactly on the 300s boundary must NOT be stale yet.
        calls: list[Settings] = []

        def _build(**kwargs: object) -> Settings:
            settings = _fake_settings()
            calls.append(settings)
            return settings

        monkeypatch.setattr(container, "build_settings", _build)
        now = [1000.0]
        monkeypatch.setattr(container, "_clock", lambda: now[0])

        # Act
        first = container.get_settings()
        now[0] += 300.0  # exactly at the 300s TTL boundary
        second = container.get_settings()

        # Assert
        assert first is second
        assert len(calls) == 1

    def test_ttl_rebuild_clears_the_ssm_cache_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: otherwise build_settings() (when SSM_PREFIX is set)
        # would just re-read the same stale cached SSM values on "rebuild".
        reset_calls: list[bool] = []
        monkeypatch.setattr(container, "reset_ssm_cache", lambda: reset_calls.append(True))
        monkeypatch.setattr(container, "build_settings", lambda **kwargs: _fake_settings())
        now = [1000.0]
        monkeypatch.setattr(container, "_clock", lambda: now[0])

        # Act
        container.get_settings()
        now[0] += 301.0
        container.get_settings()

        # Assert: once for the initial build, once for the TTL rebuild
        assert len(reset_calls) == 2


class TestDependentCachesInvalidateWithSettings:
    def test_publisher_storage_repo_survive_within_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setattr(container, "build_settings", lambda **kwargs: _fake_settings())
        monkeypatch.setattr(container, "_clock", lambda: 1000.0)
        monkeypatch.setattr(container, "build_publisher_without_uploads", lambda settings: object())
        monkeypatch.setattr(container, "build_storage", lambda settings: object())
        monkeypatch.setattr(container, "build_repo", lambda settings: object())

        # Act
        publisher_1, publisher_2 = container.get_publisher(), container.get_publisher()
        storage_1, storage_2 = container.get_storage(), container.get_storage()
        repo_1, repo_2 = container.get_repo(), container.get_repo()

        # Assert
        assert publisher_1 is publisher_2
        assert storage_1 is storage_2
        assert repo_1 is repo_2

    def test_ttl_expiry_drops_publisher_storage_repo_together(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: a stale Publisher/Storage/Repo would still hold secrets
        # baked in from the now-rotated-away Settings.
        monkeypatch.setattr(container, "build_settings", lambda **kwargs: _fake_settings())
        now = [1000.0]
        monkeypatch.setattr(container, "_clock", lambda: now[0])
        monkeypatch.setattr(container, "build_publisher_without_uploads", lambda settings: object())
        monkeypatch.setattr(container, "build_storage", lambda settings: object())
        monkeypatch.setattr(container, "build_repo", lambda settings: object())

        # Act
        publisher_before = container.get_publisher()
        storage_before = container.get_storage()
        repo_before = container.get_repo()
        now[0] += 301.0
        publisher_after = container.get_publisher()
        storage_after = container.get_storage()
        repo_after = container.get_repo()

        # Assert
        assert publisher_before is not publisher_after
        assert storage_before is not storage_after
        assert repo_before is not repo_after


class TestReset:
    def test_reset_clears_settings_immediately_regardless_of_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        calls: list[Settings] = []

        def _build(**kwargs: object) -> Settings:
            settings = _fake_settings()
            calls.append(settings)
            return settings

        monkeypatch.setattr(container, "build_settings", _build)
        monkeypatch.setattr(container, "_clock", lambda: 1000.0)  # never advances

        # Act
        container.get_settings()
        container.reset()
        container.get_settings()

        # Assert: rebuilt despite the clock never reaching the TTL
        assert len(calls) == 2

    def test_reset_clears_the_ssm_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        reset_calls: list[bool] = []
        monkeypatch.setattr(container, "reset_ssm_cache", lambda: reset_calls.append(True))

        # Act
        container.reset()

        # Assert
        assert reset_calls == [True]

    def test_reset_drops_the_jwk_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setattr(
            container, "build_settings", lambda **kwargs: _fake_settings(supabase_jwt_issuer="https://x")
        )
        monkeypatch.setattr(container, "_clock", lambda: 1000.0)
        built: list[object] = []

        def _build_jwk_client(issuer: str) -> object:
            client = object()
            built.append(client)
            return client

        monkeypatch.setattr(container, "build_jwk_client", _build_jwk_client)

        # Act
        first = container.get_jwk_client()
        container.reset()
        second = container.get_jwk_client()

        # Assert
        assert first is not second
        assert len(built) == 2

    def test_get_jwk_client_raises_clearly_when_issuer_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setattr(container, "build_settings", lambda **kwargs: _fake_settings())
        monkeypatch.setattr(container, "_clock", lambda: 1000.0)

        # Act / Assert
        with pytest.raises(RuntimeError, match="SUPABASE_JWT_ISSUER"):
            container.get_jwk_client()
