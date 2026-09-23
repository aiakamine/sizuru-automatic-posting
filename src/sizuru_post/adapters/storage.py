"""S3 storage adapter: uploads video/thumbnail files and returns stable public URLs.

Buffer fetches media from a public URL at publish time (presigned URLs are
forbidden), so the bucket must already grant public read on this prefix via
bucket policy — this adapter deliberately does not set an ACL. Videos live
under `{prefix}videos/...` (S3 lifecycle expires them after 60 days);
thumbnails live under `{prefix}thumbnails/...` and are kept indefinitely
for the admin dashboard's calendar view.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class S3ClientProtocol(Protocol):
    def put_object(self, **kwargs: Any) -> object: ...

    def generate_presigned_post(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class StoredObject:
    key: str
    public_url: str


@dataclass(frozen=True)
class PresignedUpload:
    """A presigned S3 POST for direct-from-client uploads (the scheduling
    API's `/schedule/uploads`), plus the key/URL the client should report
    back once the upload succeeds."""

    key: str
    public_url: str
    upload_url: str
    fields: dict[str, str]


class S3Storage:
    def __init__(
        self,
        bucket: str,
        region: str,
        prefix: str,
        client: S3ClientProtocol,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._prefix = prefix if prefix.endswith("/") or prefix == "" else f"{prefix}/"
        self._client = client
        self._clock = clock
        self._uuid_factory = uuid_factory

    def upload_video(self, path: Path) -> StoredObject:
        return self._upload(path, subdir="videos", default_extension=".mp4", content_type="video/mp4")

    def upload_thumbnail(self, path: Path) -> StoredObject:
        return self._upload(path, subdir="thumbnails", default_extension=".jpg", content_type="image/jpeg")

    def _upload(self, path: Path, *, subdir: str, default_extension: str, content_type: str) -> StoredObject:
        extension = path.suffix.lower() or default_extension
        key = self._new_key(subdir, extension)

        with path.open("rb") as file_obj:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=file_obj,
                ContentType=content_type,
            )

        return StoredObject(key=key, public_url=self.public_url_for(key))

    def presign_post(
        self, *, subdir: str, extension: str, content_type: str, max_bytes: int, expires_in: int
    ) -> PresignedUpload:
        """A presigned S3 POST the client can upload directly to (see
        `/schedule/uploads`), scoped to a freshly-generated key, exact
        Content-Type, and a `content-length-range` upper bound — same key
        layout/prefix conventions as `_upload`, just without the server
        ever touching the file bytes."""
        key = self._new_key(subdir, extension)
        presigned = self._client.generate_presigned_post(
            Bucket=self._bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, max_bytes],
            ],
            ExpiresIn=expires_in,
        )
        return PresignedUpload(
            key=key,
            public_url=self.public_url_for(key),
            upload_url=presigned["url"],
            fields=presigned["fields"],
        )

    def exists(self, key: str) -> bool:
        """True if `key` exists in the bucket.

        Deliberately implemented via ListObjectsV2(Prefix=key), not
        HeadObject: HeadObject's own IAM check for a *missing* key needs
        plain s3:ListBucket without the request-scoped s3:prefix condition
        key our IAM grant is conditioned on (see infra/sam/template.yaml),
        so it would come back as a generic, ambiguous 403 rather than a
        clean "doesn't exist" regardless of that grant. ListObjectsV2 with
        Prefix=key DOES carry s3:prefix in the request (matching the
        grant) and needs no s3:GetObject permission at all — a prefix
        match isn't an exact match, though, so the returned key is
        compared exactly rather than just checking Contents is non-empty.
        """
        listing = self._client.list_objects_v2(Bucket=self._bucket, Prefix=key, MaxKeys=1)
        return any(obj["Key"] == key for obj in listing.get("Contents", []))

    def public_url_for(self, key: str) -> str:
        return f"https://{self._bucket}.s3.{self._region}.amazonaws.com/{key}"

    def _new_key(self, subdir: str, extension: str) -> str:
        now = self._clock()
        return f"{self._prefix}{subdir}/{now:%Y}/{now:%m}/{self._uuid_factory().hex}{extension}"
