# sizuru admin dashboard — AWS deploy

Deploys: `ApiFunction` (edit/cancel/sync API — verifies the Supabase JWT
itself, see "Auth" below), `SyncFunction` (EventBridge Scheduler target,
every 15 minutes), an HTTP API with no gateway-level authorizer, and an S3
+ CloudFront (OAC) static site for the Next.js frontend (`apps/admin`).
See `infra/sam/template.yaml` for the full resource list and `docs`/the
approved plan for the overall architecture.

Nothing here has been deployed by the agent that wrote it — every step
below requires your explicit go-ahead (SSM writes, `sam deploy`, Supabase
Auth config changes, and the `admin_users` row are all things Claude Code
will ask before doing).

## Prerequisites

- AWS CLI configured with credentials for the target account.
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
  (or run it via `uvx --from aws-sam-cli sam ...` — no separate install).
- `uv` and `pip` on the machine running `sam build` (the Makefile build
  method uses `uv export` + `pip install` to produce the Lambda artifact —
  see the repo-root `Makefile`).
- A Supabase project already migrated (`supabase/migrations/`) with Buffer
  channels synced (`sizuru-post channels`) and at least one submission
  published — i.e. the CLI already works end-to-end locally.

## Auth: why there's no gateway-level JWT authorizer

The HTTP API (`infra/sam/template.yaml`'s `HttpApi` resource) has **no**
authorizer attached — every route is unauthenticated at the API Gateway
layer. This is deliberate, not an oversight: API Gateway HTTP API's native
JWT authorizer only verifies **RSA**-signed tokens (RS256/RS512), but
Supabase Auth signs its access tokens with **ES256** (EC P-256) — check
`https://<SupabaseUrl>/auth/v1/.well-known/jwks.json` and you'll see an
`"kty": "EC"` / `"crv": "P-256"` key, never an RSA one. Attaching the
authorizer anyway would reject every real token with a 401 before the
Lambda ever runs.

Instead, `ApiFunction` verifies the `Authorization: Bearer <token>` header
itself with [PyJWT](https://pyjwt.readthedocs.io/) (`sizuru_post.api.auth`
/ `api/handler.py::_authenticate`): it fetches the issuer's JWKS (cached
in-process per container, like the SSM secrets), checks the signature
against the matching key, and requires `exp`/`sub`/`aud`/`iss` with the
expected issuer/audience — algorithm whitelisted to `["ES256", "RS256"]`
explicitly (not trusted from the token's own header) to block
alg-confusion attacks. Only then does it check `admin_users`, same as
before. `tests/test_auth.py` covers the verification logic in isolation
(expired/wrong-aud/wrong-iss/bad-signature/alg-confusion), and
`tests/test_api_handler.py::TestAuth` proves the end-to-end wiring.

`SupabaseJwtIssuer`/`SupabaseJwtAudience` (the same CloudFormation
parameters as before) are now passed to `ApiFunction` as plain
`SUPABASE_JWT_ISSUER`/`SUPABASE_JWT_AUDIENCE` environment variables
instead of being consumed by an authorizer config.

## 1. Put secrets in SSM Parameter Store

Both Lambdas resolve `BUFFER_API_KEY` and `SUPABASE_SERVICE_ROLE_KEY` from
SSM SecureString parameters at runtime (`factory.build_settings()`),
**not** from plain Lambda environment variables — so `describe-stack` /
CloudFormation drift never exposes them in plaintext.

```bash
aws ssm put-parameter \
  --name /sizuru/BUFFER_API_KEY \
  --type SecureString \
  --value "<your Buffer API key>"

aws ssm put-parameter \
  --name /sizuru/SUPABASE_SERVICE_ROLE_KEY \
  --type SecureString \
  --value "<your Supabase service_role key, from Supabase project settings>"
```

Use a different `--name` prefix if you pass a non-default
`SsmParameterPrefix` parameter below — the two must match.

## 2. Build

```bash
uvx --from aws-sam-cli sam build -t infra/sam/template.yaml
```

This runs the repo-root `Makefile`'s `build-ApiFunction`/`build-SyncFunction`
targets: `uv export` the project's runtime dependencies (dev tools like
pytest/ruff/mypy are excluded), `pip install` them for
`manylinux2014_aarch64`/Python 3.12 (matching the Lambda runtime), and copy
`src/sizuru_post` into the build artifact.

## 3. Deploy

```bash
uvx --from aws-sam-cli sam deploy \
  --template-file infra/sam/template.yaml \
  --stack-name sizuru-admin \
  --capabilities CAPABILITY_IAM \
  --resolve-s3 \
  --parameter-overrides \
    SupabaseUrl=https://hdqrvhnhlyykocphetfg.supabase.co \
    AllowedFrontendOrigin=https://<cloudfront-domain-once-known> \
    SsmParameterPrefix=/sizuru/
```

`AllowedFrontendOrigin` is the CloudFront domain from this same stack's
`CloudFrontDomain` output — on the very first deploy you won't know it yet,
so deploy once with a placeholder (or `http://localhost:3000` twice; CORS
just won't allow the real CloudFront origin until you redeploy), read the
output, then redeploy with the real value. Subsequent deploys just reuse
the known domain.

After deploy, note the outputs:

```bash
aws cloudformation describe-stacks --stack-name sizuru-admin \
  --query 'Stacks[0].Outputs' --output table
```

- `ApiUrl` → frontend's `NEXT_PUBLIC_API_URL`
- `CloudFrontDomain` → the site's URL, and the value for `AllowedFrontendOrigin` above
- `FrontendBucket` → where to upload the frontend build (step 5)

## 4. Supabase Auth: allow the CloudFront domain

In the Supabase dashboard → Authentication → URL Configuration, add the
CloudFront domain (`https://<CloudFrontDomain output>`) to **Redirect
URLs** so the magic-link login can redirect back to it. Keep
`http://localhost:3000` there too for local frontend dev. Also confirm
email/magic-link auth is enabled (Authentication → Providers → Email).

## 5. Register the admin user

The browser reads `submissions`/`posts`/`channels` directly via
Supabase RLS (`is_admin()`), and the API additionally checks
`admin_users` server-side (`Repo.is_admin`) before allowing an
edit/cancel/sync request. A user must exist in **both** `auth.users`
(via a real login) and `admin_users` to do anything:

1. Have the admin (`kiyoto_hata@levela.co.jp`) sign in once via the
   frontend's magic-link login — this creates their `auth.users` row.
2. Find their `auth.users.id` (Supabase dashboard → Authentication →
   Users, or `select id from auth.users where email = '...'`).
3. Insert the `admin_users` row (SQL editor, or `psql`):

   ```sql
   insert into public.admin_users (user_id, email, display_name)
   values ('<uuid from step 2>', 'kiyoto_hata@levela.co.jp', 'Kiyoto Hata');
   ```

## 6. Build and upload the frontend

From `apps/admin` (owned by the other agent working on this plan):

```bash
NEXT_PUBLIC_SUPABASE_URL=https://hdqrvhnhlyykocphetfg.supabase.co \
NEXT_PUBLIC_SUPABASE_ANON_KEY=<Supabase anon/public key, NOT the service_role key> \
NEXT_PUBLIC_API_URL=<ApiUrl output from step 3> \
  yarn build   # Next.js `output: "export"` — produces apps/admin/out/

aws s3 sync apps/admin/out s3://<FrontendBucket output> --delete

aws cloudfront create-invalidation \
  --distribution-id <CloudFront distribution ID, from the console or
    `aws cloudformation describe-stack-resources --stack-name sizuru-admin \
     --logical-resource-id CloudFrontDistribution`> \
  --paths '/*'
```

Re-run the `s3 sync` + invalidation on every frontend change; the Lambdas
redeploy independently via `sam deploy` (step 3).

## Updating secrets later

```bash
aws ssm put-parameter --name /sizuru/BUFFER_API_KEY --type SecureString \
  --value "<new key>" --overwrite
```

Both Lambdas cache the resolved value in-process for the life of their
execution environment (container). If the cached key is still valid,
that's just normal container reuse — no action needed. If you're
*rotating away* a key that a warm container is actively using, Buffer will
reject it (`BufferAuthError`); both handlers catch that, clear the SSM
cache and the cached Publisher, and rebuild from a fresh SSM read on the
*next* invocation (the failing request itself is not retried) — see
`factory.reset_ssm_cache()` / `api/handler.py::reset_publisher()` /
`api/sync_handler.py::reset_publisher()`. So a rotation self-heals within
one failed request, without waiting for a cold start. Force an immediate
refresh instead (e.g. right before decommissioning the old key) by
updating any function configuration (e.g. `aws lambda
update-function-configuration --function-name <ApiFunction physical name>
--environment "Variables={SSM_PREFIX=/sizuru/,SUPABASE_URL=...}"`) or
simply waiting out normal Lambda container recycling.

## Local verification without deploying

```bash
uvx --from aws-sam-cli sam validate --lint -t infra/sam/template.yaml
uvx --from aws-sam-cli sam build -t infra/sam/template.yaml   # build only, no deploy
make verify-lambda-deps LAMBDA_BUILD_DIR=.aws-sam/build/ApiFunction
make verify-lambda-deps LAMBDA_BUILD_DIR=.aws-sam/build/SyncFunction
```

`verify-lambda-deps` scans the built artifact for `.so` files and fails if
any are for the wrong platform (darwin/x86_64) — a sanity check for the
`sam build`/Makefile step above, since a dependency without a
`manylinux2014_aarch64`/cp312 wheel now fails the build outright rather
than silently falling back to host-platform wheels that can't load in the
arm64 Python 3.12 Lambda runtime.

## 既知の制約 (Known limitations)

Two things this backend deliberately does not handle, and why:

- **No per-post optimistic locking for concurrent edits.** Two concurrent
  `edit()`/`cancel()` calls for the same post (e.g. two admins, or a
  double-click) can both read the same row and write back in either order,
  potentially losing one caller's Buffer-confirmed change. This is
  intentionally out of scope: the dashboard has a single admin user today,
  and the existing 5-minute near-due guard (`Publisher._NEAR_DUE_GUARD`)
  already prevents the highest-risk case — editing/canceling a post that's
  about to be (or already being) sent by Buffer. If multi-admin access is
  added later, add a `version`/`updated_at`-based conditional update in
  `SupabaseRepo.update_post` (`WHERE updated_at = :expected`) and surface a
  409 on mismatch.
- **No pagination beyond Supabase's default ~1,000-row cap.** `list_syncable_posts`/`list_pending_posts` (see `adapters/repo.py`) don't
  paginate; if the number of concurrently scheduled/draft/pending posts
  ever approaches that limit, `sync()` could silently miss rows past the
  cutoff. At current posting volume this is far from a concern; if it
  becomes one, switch to keyset pagination (`.gt("id", last_seen_id)`) and
  loop until a page comes back short.
