# Used only by `sam build` (see infra/sam/template.yaml, BuildMethod: makefile).
# Not used by the sizuru-post CLI itself (that's `uv run ...`).
#
# SAM's makefile build method requires this file to live in each
# function's CodeUri (the project root here, so `uv export`/pyproject.toml
# are available) and a `build-<LogicalId>` target per function. Both
# Lambdas share the same runtime dependency set and package layout, so
# both targets just delegate to one shared recipe.
#
# Requires `uv` on the machine running `sam build` (not inside the Lambda
# itself — this only prepares the deployment artifact). Does NOT require a
# `pip` on PATH — see _sam-build's use of `uv run ... python -m pip`.
#
# IMPORTANT: `sam build` runs this Makefile from a *copy* of the whole
# source tree (CustomMakeBuilder:CopySource) in a scratch tmp directory,
# which includes a copy of the repo's own .venv/ if one exists at build
# time. That copy dereferences symlinks, turning .venv/bin/python3 (which
# is normally a symlink to uv's toolchain-managed interpreter) into a
# broken standalone binary whose baked-in @rpath can no longer find
# libpython3.12.dylib — invoking it SIGABRTs with a dyld error. Every
# command below is written to avoid ever needing that (possibly broken,
# possibly copied) .venv/bin/python3 at all:
#   - `uv export --frozen` skips resolving/locking against any project
#     environment entirely (trusts uv.lock as-is), so it never queries a
#     Python interpreter in the first place — this is the actual fix.
#   - UV_PROJECT_ENVIRONMENT is still set defensively, pointing outside the
#     copied source tree (into SAM's own artifacts area), so that even if a
#     future uv version ever did need a project environment for `export`,
#     it would create a fresh throwaway one there rather than touching
#     (or, worse, trying to reuse) the copied .venv.
#   - The later `pip install` step uses `uv run --no-project --python 3.12
#     -- python -m pip`, a known-good interpreter from uv's own toolchain
#     cache, instead of relying on whatever `pip`/`python` happens to be on
#     PATH (which may not exist, or may be a different Python entirely).
# None of this ever deletes, modifies, or depends on the *original* repo
# .venv/ — safe even if `sam build` is ever run in-source instead of via a
# copy.

.PHONY: build-ApiFunction build-SyncFunction _sam-build verify-lambda-deps

build-ApiFunction: _sam-build
build-SyncFunction: _sam-build

# No host-platform fallback in the pip install below: any dependency
# without a manylinux2014_aarch64 cp312 wheel must fail this build
# loudly, not silently produce a macOS/x86_64 .so that can't load in the
# arm64 Python 3.12 Lambda runtime (see verify-lambda-deps, which also
# guards against this).
_sam-build:
	req_file=$$(mktemp "$${TMPDIR:-/tmp}/sizuru-post-requirements.XXXXXX"); \
	UV_PROJECT_ENVIRONMENT="$(ARTIFACTS_DIR)/../.uv-export-venv" \
		uv export --frozen --no-dev --no-hashes --no-emit-project \
			--format requirements-txt -o "$$req_file"; \
	status=$$?; \
	if [ $$status -ne 0 ]; then rm -f "$$req_file"; exit $$status; fi; \
	uv run --no-project --python 3.12 -- python -m pip install --no-cache-dir \
		--platform manylinux2014_aarch64 --python-version 3.12 \
		--only-binary=:all: -r "$$req_file" -t "$(ARTIFACTS_DIR)"; \
	status=$$?; \
	rm -f "$$req_file"; \
	if [ $$status -ne 0 ]; then exit $$status; fi
	cp -r src/sizuru_post "$(ARTIFACTS_DIR)/"

# Post-build smoke check: fails if any compiled extension in the built
# artifact is for the wrong platform (e.g. built on a macOS/x86_64 dev
# machine without --platform actually being honored). Run after `sam build`,
# e.g.: `make verify-lambda-deps LAMBDA_BUILD_DIR=.aws-sam/build/ApiFunction`.
LAMBDA_BUILD_DIR ?= .aws-sam/build/ApiFunction

verify-lambda-deps:
	@if [ ! -d "$(LAMBDA_BUILD_DIR)" ]; then \
		echo "verify-lambda-deps: $(LAMBDA_BUILD_DIR) not found — run 'sam build' first" >&2; \
		exit 1; \
	fi
	@bad=$$(find "$(LAMBDA_BUILD_DIR)" -name '*.so' -exec file {} \; | grep -Ei 'mach-o|x86-64|x86_64' || true); \
	if [ -n "$$bad" ]; then \
		echo "verify-lambda-deps: found non-Lambda-platform native libraries:" >&2; \
		echo "$$bad" >&2; \
		exit 1; \
	fi
	@echo "verify-lambda-deps: OK — no darwin/x86_64 .so files found in $(LAMBDA_BUILD_DIR)"
