"""Tests for sizuru_post.adapters.storage: S3 upload key/url format."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import boto3
import pytest
from botocore.stub import ANY, Stubber

from sizuru_post.adapters.storage import S3Storage


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fake video bytes")
    return path


@pytest.fixture
def thumbnail_file(tmp_path: Path) -> Path:
    path = tmp_path / "thumb.jpg"
    path.write_bytes(b"fake jpeg bytes")
    return path


@pytest.fixture
def s3_client_and_stubber():  # type: ignore[no-untyped-def]
    client = boto3.client("s3", region_name="ap-northeast-1")
    stubber = Stubber(client)
    yield client, stubber
    stubber.deactivate()


class TestUploadVideo:
    def test_puts_object_under_videos_prefix_and_returns_public_url(
        self, video_file: Path, s3_client_and_stubber: tuple[object, Stubber]
    ) -> None:
        # Arrange
        client, stubber = s3_client_and_stubber
        fixed_uuid = UUID("12345678123456781234567812345678")
        fixed_now = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
        stubber.add_response(
            "put_object",
            {},
            expected_params={
                "Bucket": "my-bucket",
                "Key": "public/videos/2026/03/12345678123456781234567812345678.mp4",
                "Body": ANY,
                "ContentType": "video/mp4",
            },
        )
        stubber.activate()
        storage = S3Storage(
            bucket="my-bucket",
            region="ap-northeast-1",
            prefix="public/",
            client=client,
            clock=lambda: fixed_now,
            uuid_factory=lambda: fixed_uuid,
        )

        # Act
        result = storage.upload_video(video_file)

        # Assert
        assert result.key == "public/videos/2026/03/12345678123456781234567812345678.mp4"
        assert (
            result.public_url == "https://my-bucket.s3.ap-northeast-1.amazonaws.com/public/videos/2026/03/"
            "12345678123456781234567812345678.mp4"
        )
        stubber.assert_no_pending_responses()

    def test_lowercases_extension(
        self, video_file: Path, s3_client_and_stubber: tuple[object, Stubber]
    ) -> None:
        # Arrange
        client, stubber = s3_client_and_stubber
        upper_path = video_file.with_name("CLIP.MP4")
        video_file.rename(upper_path)
        fixed_uuid = UUID("abcdefabcdefabcdefabcdefabcdefab")
        fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
        stubber.add_response(
            "put_object",
            {},
            expected_params={
                "Bucket": "my-bucket",
                "Key": "public/videos/2026/01/abcdefabcdefabcdefabcdefabcdefab.mp4",
                "Body": ANY,
                "ContentType": "video/mp4",
            },
        )
        stubber.activate()
        storage = S3Storage(
            bucket="my-bucket",
            region="ap-northeast-1",
            prefix="public/",
            client=client,
            clock=lambda: fixed_now,
            uuid_factory=lambda: fixed_uuid,
        )

        # Act
        storage.upload_video(upper_path)

        # Assert
        stubber.assert_no_pending_responses()

    def test_prefix_without_trailing_slash_is_normalized(
        self, video_file: Path, s3_client_and_stubber: tuple[object, Stubber]
    ) -> None:
        # Arrange
        client, stubber = s3_client_and_stubber
        fixed_uuid = UUID("11111111-1111-1111-1111-111111111111")
        fixed_now = datetime(2026, 5, 9, tzinfo=UTC)
        stubber.add_response(
            "put_object",
            {},
            expected_params={
                "Bucket": "bucket",
                "Key": "public/videos/2026/05/11111111111111111111111111111111.mp4",
                "Body": ANY,
                "ContentType": "video/mp4",
            },
        )
        stubber.activate()
        storage = S3Storage(
            bucket="bucket",
            region="ap-northeast-1",
            prefix="public",
            client=client,
            clock=lambda: fixed_now,
            uuid_factory=lambda: fixed_uuid,
        )

        # Act
        result = storage.upload_video(video_file)

        # Assert
        assert result.key.startswith("public/videos/2026/05/")


class TestUploadThumbnail:
    def test_puts_object_under_thumbnails_prefix_with_jpeg_content_type(
        self, thumbnail_file: Path, s3_client_and_stubber: tuple[object, Stubber]
    ) -> None:
        # Arrange
        client, stubber = s3_client_and_stubber
        fixed_uuid = UUID("22222222-2222-2222-2222-222222222222")
        fixed_now = datetime(2026, 4, 2, tzinfo=UTC)
        stubber.add_response(
            "put_object",
            {},
            expected_params={
                "Bucket": "my-bucket",
                "Key": "public/thumbnails/2026/04/22222222222222222222222222222222.jpg",
                "Body": ANY,
                "ContentType": "image/jpeg",
            },
        )
        stubber.activate()
        storage = S3Storage(
            bucket="my-bucket",
            region="ap-northeast-1",
            prefix="public/",
            client=client,
            clock=lambda: fixed_now,
            uuid_factory=lambda: fixed_uuid,
        )

        # Act
        result = storage.upload_thumbnail(thumbnail_file)

        # Assert
        assert result.key == "public/thumbnails/2026/04/22222222222222222222222222222222.jpg"
        stubber.assert_no_pending_responses()
