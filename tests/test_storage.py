"""Tests for sizuru_post.adapters.storage: S3 upload key/url format."""

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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


@pytest.fixture
def signing_client() -> Any:
    # generate_presigned_post() signs entirely locally (no network call), but
    # still needs *some* credentials to sign with — real or not.
    return boto3.client(
        "s3",
        region_name="ap-northeast-1",
        aws_access_key_id="AKIAFAKE0000000000",
        aws_secret_access_key="fakeSecretKeyForTestsOnly0000000000000",
    )


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


class TestPresignPost:
    def test_presign_post_returns_key_matching_uploaded_key_layout(self, signing_client: Any) -> None:
        # Arrange
        fixed_uuid = UUID("33333333-3333-3333-3333-333333333333")
        fixed_now = datetime(2026, 6, 8, tzinfo=UTC)
        storage = S3Storage(
            bucket="my-bucket",
            region="ap-northeast-1",
            prefix="public/",
            client=signing_client,
            clock=lambda: fixed_now,
            uuid_factory=lambda: fixed_uuid,
        )

        # Act
        result = storage.presign_post(
            subdir="videos",
            extension=".mp4",
            content_type="video/mp4",
            max_bytes=314_572_800,
            expires_in=900,
        )

        # Assert: same {prefix}{subdir}/{YYYY}/{MM}/{uuid}{ext} layout as upload_video()
        assert result.key == "public/videos/2026/06/33333333333333333333333333333333.mp4"
        assert result.public_url == (
            "https://my-bucket.s3.ap-northeast-1.amazonaws.com/"
            "public/videos/2026/06/33333333333333333333333333333333.mp4"
        )
        assert result.upload_url

    def test_presign_post_fields_include_exact_key_and_content_type(self, signing_client: Any) -> None:
        # Arrange
        storage = S3Storage(bucket="my-bucket", region="ap-northeast-1", prefix="public/", client=signing_client)

        # Act
        result = storage.presign_post(
            subdir="thumbnails",
            extension=".jpg",
            content_type="image/jpeg",
            max_bytes=314_572_800,
            expires_in=900,
        )

        # Assert: the client must upload with these exact fields (plus `file` last)
        assert result.fields["key"] == result.key
        assert result.fields["Content-Type"] == "image/jpeg"
        assert "policy" in result.fields
        assert "signature" in result.fields or "x-amz-signature" in result.fields

    def test_presign_post_conditions_enforce_content_length_range_and_content_type(
        self, signing_client: Any
    ) -> None:
        # Arrange
        storage = S3Storage(bucket="my-bucket", region="ap-northeast-1", prefix="public/", client=signing_client)

        # Act
        result = storage.presign_post(
            subdir="videos",
            extension=".mp4",
            content_type="video/mp4",
            max_bytes=314_572_800,
            expires_in=900,
        )

        # Assert: decode the signed policy document to verify the actual
        # constraints S3 will enforce on upload, not just the visible fields.
        policy = json.loads(base64.b64decode(result.fields["policy"]))
        assert ["content-length-range", 1, 314_572_800] in policy["conditions"]
        assert {"Content-Type": "video/mp4"} in policy["conditions"]
        assert {"key": result.key} in policy["conditions"]

    def test_presign_post_thumbnail_conditions_enforce_the_smaller_5mb_cap(self, signing_client: Any) -> None:
        # Arrange: api/schedule.py presigns the thumbnail upload with a much
        # smaller cap than the video (a single JPEG frame, not a 300MB
        # video) — verify the signed policy actually enforces 5MB, not just
        # that the call was made with that argument.
        storage = S3Storage(bucket="my-bucket", region="ap-northeast-1", prefix="public/", client=signing_client)

        # Act
        result = storage.presign_post(
            subdir="thumbnails",
            extension=".jpg",
            content_type="image/jpeg",
            max_bytes=5_242_880,
            expires_in=900,
        )

        # Assert
        policy = json.loads(base64.b64decode(result.fields["policy"]))
        assert ["content-length-range", 1, 5_242_880] in policy["conditions"]
        assert {"Content-Type": "image/jpeg"} in policy["conditions"]


class TestExists:
    """exists() uses ListObjectsV2(Prefix=key), not HeadObject: HeadObject's
    own IAM check for a *missing* key needs plain s3:ListBucket without the
    request-scoped s3:prefix condition key our IAM grant is conditioned on
    (see infra/sam/template.yaml), so it would come back as a generic 403
    rather than a clean "doesn't exist" regardless of that grant.
    ListObjectsV2 with Prefix=key DOES carry s3:prefix in the request,
    matching the grant, and needs no s3:GetObject permission at all."""

    def test_exists_returns_true_when_key_is_in_the_listing(
        self, s3_client_and_stubber: tuple[object, Stubber]
    ) -> None:
        # Arrange
        client, stubber = s3_client_and_stubber
        stubber.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "public/videos/x.mp4"}], "KeyCount": 1},
            expected_params={"Bucket": "my-bucket", "Prefix": "public/videos/x.mp4", "MaxKeys": 1},
        )
        stubber.activate()
        storage = S3Storage(bucket="my-bucket", region="ap-northeast-1", prefix="public/", client=client)

        # Act
        exists = storage.exists("public/videos/x.mp4")

        # Assert
        assert exists is True
        stubber.assert_no_pending_responses()

    def test_exists_returns_false_when_listing_is_empty(
        self, s3_client_and_stubber: tuple[object, Stubber]
    ) -> None:
        # Arrange
        client, stubber = s3_client_and_stubber
        stubber.add_response(
            "list_objects_v2",
            {"KeyCount": 0},
            expected_params={"Bucket": "my-bucket", "Prefix": "public/videos/missing.mp4", "MaxKeys": 1},
        )
        stubber.activate()
        storage = S3Storage(bucket="my-bucket", region="ap-northeast-1", prefix="public/", client=client)

        # Act
        exists = storage.exists("public/videos/missing.mp4")

        # Assert
        assert exists is False

    def test_exists_returns_false_when_only_a_longer_key_shares_the_prefix(
        self, s3_client_and_stubber: tuple[object, Stubber]
    ) -> None:
        # Arrange: a prefix match isn't an exact match — guard against a
        # hypothetical key that merely starts with the requested key.
        client, stubber = s3_client_and_stubber
        stubber.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "public/videos/x.mp4.bak"}], "KeyCount": 1},
            expected_params={"Bucket": "my-bucket", "Prefix": "public/videos/x.mp4", "MaxKeys": 1},
        )
        stubber.activate()
        storage = S3Storage(bucket="my-bucket", region="ap-northeast-1", prefix="public/", client=client)

        # Act
        exists = storage.exists("public/videos/x.mp4")

        # Assert
        assert exists is False


class TestPublicUrlFor:
    def test_returns_the_same_format_upload_uses(self) -> None:
        # Arrange
        storage = S3Storage(bucket="my-bucket", region="ap-northeast-1", prefix="public/", client=object())  # type: ignore[arg-type]

        # Act
        url = storage.public_url_for("public/videos/2026/01/x.mp4")

        # Assert
        assert url == "https://my-bucket.s3.ap-northeast-1.amazonaws.com/public/videos/2026/01/x.mp4"
