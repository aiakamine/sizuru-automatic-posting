# Vendored: `twitter_text` (from PyPI `twitter-text-parser`)

- **Source**: https://github.com/swen128/twitter-text-python
- **Package on PyPI**: `twitter-text-parser`
- **Version vendored**: 3.0.0
- **License**: MIT (Copyright (c) 2019 swen128) — see `twitter_text/LICENSE`

## Why vendored

`sizuru_post.domain.validate.caption_length` needs X's official weighted
character-counting rules for captions, which this library implements. Its
only problem: `twitter_text/regexp/emoji.py` does `import pkg_resources`
to load its bundled `emoji-test.txt` data file at import time.
`pkg_resources` was removed from setuptools starting with 81.0.0, which
forced this project to pin `setuptools<81` just to keep this one
transitive import working. Vendoring removes that pin without touching
the counting logic at all.

## Modifications

Two changes, both required only because the package now lives at
`sizuru_post._vendor.twitter_text` instead of installed as a top-level
`twitter_text` package; the counting logic itself is untouched.

1. `twitter_text/regexp/emoji.py` — drop the `pkg_resources` dependency:

   ```diff
   -import pkg_resources
   +import importlib.resources
    ...
   -emoji_raw = pkg_resources.resource_string(__name__, 'emoji-test.txt').decode('utf-8')
   +emoji_raw = importlib.resources.files(__package__).joinpath('emoji-test.txt').read_text(encoding='utf-8')
   ```

2. `twitter_text/__init__.py` — every other module in the package already
   uses relative imports (`from .parse_tweet import ...`), but this one
   file imported from the absolute top-level name `twitter_text`, which
   only resolved because the package used to be installed at that
   top-level name. Changed to match the rest of the package:

   ```diff
   -from twitter_text.parse_tweet import parse_tweet, ParsedResult
   -from twitter_text.extract_urls import extract_urls, extract_urls_with_indices
   -from twitter_text.extract_emojis import extract_emojis_with_indices
   +from .parse_tweet import parse_tweet, ParsedResult
   +from .extract_urls import extract_urls, extract_urls_with_indices
   +from .extract_emojis import extract_emojis_with_indices
   ```

Everything else — including `emoji-test.txt` and every other module — is
byte-identical to the upstream 3.0.0 release. This directory is excluded
from ruff and mypy's strict checks (see `pyproject.toml`) since it is
upstream code, not ours to restyle or type-annotate.
