#!/usr/bin/env bash
# infra/setup_s3.sh
#
# Idempotently provisions the S3 bucket used by sizuru-post to host publicly
# reachable submission URLs (S3_PUBLIC_PREFIX only — the rest of the bucket
# stays private). Layout under the prefix:
#   {PREFIX}videos/yyyy/mm/uuid.mp4      (expires after 60 days, see step 6)
#   {PREFIX}thumbnails/yyyy/mm/uuid.jpg  (kept indefinitely, for the admin calendar)
#
# Usage:
#   ./infra/setup_s3.sh              # dry-run: prints the aws commands it would run
#   ./infra/setup_s3.sh --apply      # actually creates/updates the bucket
#   ./infra/setup_s3.sh --apply --force
#                                     # also overwrite a pre-existing bucket
#                                     # policy / lifecycle configuration that
#                                     # differs from what this script sets
#
# Config (env vars, all optional):
#   S3_BUCKET          default: sizuru-clips-<AWS account id>
#   S3_REGION          default: ap-northeast-1
#   S3_PUBLIC_PREFIX   default: public/
#   AWS_PROFILE         respected as-is by the aws CLI (no special handling needed here)
set -euo pipefail

APPLY=false
FORCE=false
for arg in "$@"; do
  case "$arg" in
    --apply)
      APPLY=true
      ;;
    --force)
      FORCE=true
      ;;
    -h|--help)
      sed -n '2,24p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

log() {
  echo "==> $*" >&2
}

# Semantically compares two JSON blobs (ignores key order/whitespace).
# Exit status 0 = equal, 1 = different (or either side isn't valid JSON).
json_equal() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

try:
    a = json.loads(sys.argv[1])
    b = json.loads(sys.argv[2])
except (ValueError, IndexError):
    sys.exit(1)
sys.exit(0 if a == b else 1)
PY
}

# Stops (unless --force) if $2 (a "get" command's current output, possibly
# empty when nothing is configured yet) is non-empty and differs from $3
# (the JSON file this script is about to apply). $1 is used only in the
# error message to name what's being checked.
guard_existing_config() {
  local label="$1" existing="$2" expected_file="$3"

  if [ -z "$existing" ]; then
    return 0
  fi
  if json_equal "$existing" "$(cat "$expected_file")"; then
    return 0
  fi

  if [ "$FORCE" = true ]; then
    log "既存の${label}が本スクリプトの内容と異なりますが、--force が指定されたため上書きします。"
    return 0
  fi

  cat >&2 <<EOF

エラー: バケット ${S3_BUCKET} には既に、本スクリプトが設定しようとしている内容と異なる
${label}が設定されています。意図しない上書きを防ぐため処理を中止します。

既存の${label}:
${existing}

本スクリプトが設定しようとしている内容:
$(cat "$expected_file")

内容を確認のうえ、上書きしてよい場合は --force を付けて再実行してください。
EOF
  exit 1
}

# Runs (--apply) or prints (dry-run) an aws CLI invocation.
run() {
  if [ "$APPLY" = true ]; then
    log "実行: $*"
    "$@"
  else
    echo "[dry-run] $*"
  fi
}

command -v aws >/dev/null 2>&1 || {
  echo "error: aws CLI が見つかりません。AWS CLI をインストールしてください。" >&2
  exit 1
}

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

S3_REGION="${S3_REGION:-ap-northeast-1}"
S3_BUCKET="${S3_BUCKET:-sizuru-clips-${AWS_ACCOUNT_ID}}"
S3_PUBLIC_PREFIX="${S3_PUBLIC_PREFIX:-public/}"
# Normalize: must end with a single trailing slash.
S3_PUBLIC_PREFIX="${S3_PUBLIC_PREFIX%/}/"

log "S3_BUCKET=${S3_BUCKET}"
log "S3_REGION=${S3_REGION}"
log "S3_PUBLIC_PREFIX=${S3_PUBLIC_PREFIX}"
log "AWS_PROFILE=${AWS_PROFILE:-<default>}"
if [ "$APPLY" = false ]; then
  log "dry-run モードです。実際に変更するには --apply を付けて実行してください。"
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# ---------------------------------------------------------------------------
# 1. Create the bucket if it doesn't exist yet.
# ---------------------------------------------------------------------------
if aws s3api head-bucket --bucket "$S3_BUCKET" >/dev/null 2>&1; then
  log "バケット ${S3_BUCKET} は既に存在します。作成をスキップします。"
else
  log "バケット ${S3_BUCKET} が見つかりません。作成します。"
  if [ "$S3_REGION" = "us-east-1" ]; then
    run aws s3api create-bucket \
      --bucket "$S3_BUCKET" \
      --region "$S3_REGION"
  else
    run aws s3api create-bucket \
      --bucket "$S3_BUCKET" \
      --region "$S3_REGION" \
      --create-bucket-configuration "LocationConstraint=${S3_REGION}"
  fi
fi

# ---------------------------------------------------------------------------
# 2. Object Ownership: BucketOwnerEnforced (ACLs disabled; access is governed
#    purely by the bucket policy below).
# ---------------------------------------------------------------------------
run aws s3api put-bucket-ownership-controls \
  --bucket "$S3_BUCKET" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'

# ---------------------------------------------------------------------------
# 3. Check the ACCOUNT-level public access block. If BlockPublicPolicy or
#    RestrictPublicBuckets is enabled there, it overrides anything we set at
#    the bucket level and our public-read policy below would silently fail.
#    We never change account-level settings automatically — that's an
#    account-wide security control outside this script's scope.
# ---------------------------------------------------------------------------
log "アカウントレベルの Public Access Block 設定を確認します..."
ACCOUNT_PAB_JSON="$(aws s3control get-public-access-block \
  --account-id "$AWS_ACCOUNT_ID" --output json 2>/dev/null || echo '{}')"

ACCOUNT_BLOCK_POLICY="$(echo "$ACCOUNT_PAB_JSON" | \
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("PublicAccessBlockConfiguration", {}).get("BlockPublicPolicy", False))' 2>/dev/null || echo "False")"
ACCOUNT_RESTRICT_BUCKETS="$(echo "$ACCOUNT_PAB_JSON" | \
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("PublicAccessBlockConfiguration", {}).get("RestrictPublicBuckets", False))' 2>/dev/null || echo "False")"

if [ "$ACCOUNT_BLOCK_POLICY" = "True" ] || [ "$ACCOUNT_RESTRICT_BUCKETS" = "True" ]; then
  cat >&2 <<EOF

エラー: このAWSアカウントには、アカウントレベルの S3 Public Access Block が
有効になっており (BlockPublicPolicy または RestrictPublicBuckets が true)、
これはバケットレベルの設定より優先されます。そのため、このスクリプトが設定
しようとしている「public/ プレフィックス配下のみ公開読み取り可能」という
バケットポリシーは無効化されてしまいます。

このスクリプトはアカウント全体のセキュリティ設定を勝手に変更しません。
公開URLでの動画配信が必要な場合は、AWSアカウント管理者と相談のうえ、
必要な範囲だけ以下のコマンドでアカウントレベルの設定を緩めてください
（自己責任で、影響範囲を理解した上で実行してください）:

  aws s3control put-public-access-block \\
    --account-id ${AWS_ACCOUNT_ID} \\
    --public-access-block-configuration \\
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false

設定変更後、このスクリプトを再実行してください。
EOF
  exit 1
fi
log "アカウントレベルの Public Access Block は問題ありません（バケットレベルの公開設定が有効になります）。"

# ---------------------------------------------------------------------------
# 4. Bucket-level public access block: allow a bucket policy to grant public
#    read, but keep blocking public ACLs.
# ---------------------------------------------------------------------------
run aws s3api put-public-access-block \
  --bucket "$S3_BUCKET" \
  --public-access-block-configuration \
    'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false'

# ---------------------------------------------------------------------------
# 5. Bucket policy: public GetObject, restricted to the public prefix only.
# ---------------------------------------------------------------------------
POLICY_FILE="${WORKDIR}/bucket-policy.json"
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowPublicReadForSubmissionsPrefix",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::${S3_BUCKET}/${S3_PUBLIC_PREFIX}*"
    }
  ]
}
EOF

EXISTING_POLICY="$(aws s3api get-bucket-policy --bucket "$S3_BUCKET" --query Policy --output text 2>/dev/null || echo "")"
guard_existing_config "バケットポリシー" "$EXISTING_POLICY" "$POLICY_FILE"

log "バケットポリシー (${S3_PUBLIC_PREFIX}* のみ公開読み取り可):"
cat "$POLICY_FILE" >&2
run aws s3api put-bucket-policy --bucket "$S3_BUCKET" --policy "file://${POLICY_FILE}"

# ---------------------------------------------------------------------------
# 6. Lifecycle rule: expire objects under {PREFIX}videos/ after 60 days only.
#    Thumbnails under {PREFIX}thumbnails/ are kept (used by the admin
#    calendar) and are intentionally NOT covered by this rule.
# ---------------------------------------------------------------------------
S3_VIDEOS_PREFIX="${S3_PUBLIC_PREFIX}videos/"
LIFECYCLE_FILE="${WORKDIR}/lifecycle.json"
cat > "$LIFECYCLE_FILE" <<EOF
{
  "Rules": [
    {
      "ID": "expire-public-videos-60d",
      "Filter": { "Prefix": "${S3_VIDEOS_PREFIX}" },
      "Status": "Enabled",
      "Expiration": { "Days": 60 }
    }
  ]
}
EOF

EXISTING_LIFECYCLE="$(aws s3api get-bucket-lifecycle-configuration --bucket "$S3_BUCKET" --output json 2>/dev/null || echo "")"
guard_existing_config "ライフサイクル設定" "$EXISTING_LIFECYCLE" "$LIFECYCLE_FILE"

run aws s3api put-bucket-lifecycle-configuration \
  --bucket "$S3_BUCKET" \
  --lifecycle-configuration "file://${LIFECYCLE_FILE}"

# ---------------------------------------------------------------------------
# 7. Default server-side encryption (SSE-S3 / AES256).
# ---------------------------------------------------------------------------
run aws s3api put-bucket-encryption \
  --bucket "$S3_BUCKET" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# ---------------------------------------------------------------------------
# Done.
# ---------------------------------------------------------------------------
PUBLIC_URL_PATTERN="https://${S3_BUCKET}.s3.${S3_REGION}.amazonaws.com/${S3_PUBLIC_PREFIX}..."

echo
if [ "$APPLY" = true ]; then
  log "S3 バケットのセットアップが完了しました。"
else
  log "dry-run 完了です。上記の内容で問題なければ --apply を付けて再実行してください。"
fi
echo
echo "公開URLパターン: ${PUBLIC_URL_PATTERN}"
echo
echo ".env に以下を追記してください:"
echo "  S3_BUCKET=${S3_BUCKET}"
echo "  S3_REGION=${S3_REGION}"
echo "  S3_PUBLIC_PREFIX=${S3_PUBLIC_PREFIX}"
echo
echo "アップロード用の最小権限 IAM ポリシー例は infra/iam-policy-uploader.json を参照してください"
echo "（このスクリプトは IAM ユーザーを作成しません。手動で作成しポリシーをアタッチしてください）。"
