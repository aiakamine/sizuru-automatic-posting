#!/usr/bin/env bash
# check_skill_env.sh
#
# mitchaku-kirinuki / taidan-kirinuki / sns-schedule の3スキルを使うために
# 必要な環境が揃っているかを確認する（読み取り専用・何もインストールしない）。
# 各項目を ✅/❌ で表示し、❌ には直す手順を添える。値（APIキー等）は表示しない。
#
# 使い方: bash scripts/check_skill_env.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

fail_count=0

ok() {
  printf '✅ %s\n' "$1"
}

ng() {
  printf '❌ %s\n' "$1"
  printf '   直す: %s\n' "$2"
  fail_count=$((fail_count + 1))
}

check_command() {
  # check_command <表示名> <コマンド名> <導入コマンド>
  local label="$1" cmd="$2" fix="$3"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "${label}（${cmd}）"
  else
    ng "${label}（${cmd}）が見つかりません" "$fix"
  fi
}

echo "== 環境チェック（sns-schedule / kirinuki スキル） =="
echo

# --- OS ----------------------------------------------------------------
if [[ "$(uname -s)" == "Darwin" ]]; then
  ok "macOS"
else
  ng "macOS ではありません（Vision OCR・ヒラギノ角ゴシックに依存するため macOS 専用）" \
    "macOS 環境で実行してください"
fi

# --- Python 3.9+ ---------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    py_version="$(python3 -c 'import platform; print(platform.python_version())')"
    ok "Python 3（${py_version}、3.9以上）"
  else
    py_version="$(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null)"
    ng "Python 3.9以上が必要です（現在: ${py_version:-不明}）" \
      "brew install python3 で最新版を導入してください"
  fi
else
  ng "python3 が見つかりません" "brew install python3"
fi

# --- Pillow --------------------------------------------------------------
if command -v python3 >/dev/null 2>&1 && python3 -c 'import PIL' >/dev/null 2>&1; then
  ok "Python Pillow"
else
  ng "Python Pillow が import できません" \
    "python3 -m pip install --user Pillow"
fi

# --- CLI tools -------------------------------------------------------------
check_command "Xcode Command Line Tools" "swiftc" "xcode-select --install"
check_command "ffmpeg" "ffmpeg" "brew install ffmpeg"
check_command "ffprobe" "ffprobe" "brew install ffmpeg"
check_command "yt-dlp" "yt-dlp" "brew install yt-dlp"
check_command "Whisper" "whisper" "python3 -m pip install --user openai-whisper"
check_command "curl" "curl" "brew install curl"

# --- .env: SIZURU_API_URL / SIZURU_API_KEY --------------------------------
# 値は一切表示しない。「設定されているか（空でないか）」だけを見る。
env_has_nonempty_value() {
  # env_has_nonempty_value <KEY> <ファイル>
  local key="$1" file="$2"
  [[ -f "$file" ]] || return 1
  local line
  line="$(grep -E "^${key}=" "$file" | tail -n 1)"
  [[ -n "$line" ]] || return 1
  local value="${line#*=}"
  [[ -n "$value" ]]
}

sizuru_url_ok=0
sizuru_key_ok=0

if [[ -n "${SIZURU_API_URL:-}" ]] || env_has_nonempty_value "SIZURU_API_URL" "$ENV_FILE"; then
  ok "SIZURU_API_URL が設定されています"
  sizuru_url_ok=1
else
  ng "SIZURU_API_URL が未設定です" \
    "cp .env.example .env の上、SIZURU_API_URL を設定してください"
fi

if [[ -n "${SIZURU_API_KEY:-}" ]] || env_has_nonempty_value "SIZURU_API_KEY" "$ENV_FILE"; then
  ok "SIZURU_API_KEY が設定されています"
  sizuru_key_ok=1
else
  ng "SIZURU_API_KEY が未設定です" \
    "管理者から個別に受け取ったキーを .env の SIZURU_API_KEY に設定してください（共有しない）"
fi

# --- サーバー疎通確認（sns-schedule check） ---------------------------------
echo
if [[ "$sizuru_url_ok" -eq 1 && "$sizuru_key_ok" -eq 1 ]]; then
  export CLAUDE_PROJECT_DIR="$REPO_ROOT"
  schedule_py="$REPO_ROOT/.claude/skills/sns-schedule/scripts/schedule.py"
  if [[ -f "$schedule_py" ]]; then
    if check_output="$(python3 "$schedule_py" check 2>&1)"; then
      ok "サーバー疎通確認（schedule.py check）"
    else
      ng "サーバー疎通確認（schedule.py check）に失敗しました: $check_output" \
        "SIZURU_API_URL / SIZURU_API_KEY の値を確認するか、管理者に問い合わせてください"
    fi
  else
    ng "schedule.py が見つかりません（${schedule_py}）" \
      "リポジトリを最新に更新してください（git pull）"
  fi
else
  echo "⏭  サーバー疎通確認: SIZURU_API_URL / SIZURU_API_KEY が未設定のためスキップします"
fi

echo
if [[ "$fail_count" -eq 0 ]]; then
  echo "すべてのチェックに合格しました。"
  exit 0
else
  echo "未解決の項目が ${fail_count} 件あります（上記の「直す」を参照してください）。"
  exit 1
fi
