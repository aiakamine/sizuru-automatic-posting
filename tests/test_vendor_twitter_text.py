"""Tests for the vendored `sizuru_post._vendor.twitter_text` package.

The upstream `twitter-text-parser` PyPI package (MIT) is vendored under
`src/sizuru_post/_vendor/twitter_text/` because its bundled emoji regexp
module imports `pkg_resources`, which was removed from setuptools 81 —
see `src/sizuru_post/_vendor/README.md` for the single line changed.
These tests pin down the two guarantees that make vendoring safe:
`pkg_resources` must never be imported, and the vendored package must
behave identically to upstream for the caption-length contract.
"""

import subprocess
import sys
from importlib import resources


class TestNoPkgResourcesImport:
    def test_importing_validate_does_not_import_pkg_resources(self) -> None:
        # Arrange / Act: a fresh subprocess so sys.modules isn't polluted
        # by anything already imported in this test process.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, sizuru_post.domain.validate; "
                "assert 'pkg_resources' not in sys.modules",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        # Assert
        assert result.returncode == 0, result.stderr


class TestVendoredEmojiData:
    def test_emoji_test_txt_is_readable_and_non_empty(self) -> None:
        # Arrange / Act
        data = (
            resources.files("sizuru_post._vendor.twitter_text.regexp")
            .joinpath("emoji-test.txt")
            .read_text(encoding="utf-8")
        )

        # Assert
        assert len(data) > 0
        assert "# group:" in data


class TestVendoredParseTweet:
    def test_parse_tweet_is_importable_and_usable_directly(self) -> None:
        # Arrange
        from sizuru_post._vendor.twitter_text import parse_tweet

        # Act
        result = parse_tweet("hello")

        # Assert
        assert result.weightedLength == 5

    def test_caption_length_matches_upstream_family_emoji_weight(self) -> None:
        # Arrange: validate.caption_length is the code path the rest of the
        # app calls through; this pins its result to the vendored library's
        # behavior, unchanged from upstream (a family ZWJ sequence weighs 2).
        from sizuru_post.domain.validate import caption_length

        # Act / Assert
        assert caption_length("x", "👨‍👩‍👧‍👦") == 2
