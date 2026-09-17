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


@dataclass(frozen=True)
class StoredObject:
    key: str
    public_url: str


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
        now = self._clock()
        extension = path.suffix.lower() or default_extension
        key = f"{self._prefix}{subdir}/{now:%Y}/{now:%m}/{self._uuid_factory().hex}{extension}"

        with path.open("rb") as file_obj:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=file_obj,
                ContentType=content_type,
            )

        public_url = f"https://{self._bucket}.s3.{self._region}.amazonaws.com/{key}"
        return StoredObject(key=key, public_url=public_url)
