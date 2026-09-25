# sizuru-automatic-posting

ローカルの9:16 MP4クリップを、Buffer GraphQL API経由でTikTok / Instagram Reels / X に
自動投稿するCLIツール（`sizuru-post`）です。投稿は常に人間の承認を経てから実行されます。

**Claude Codeスキル（切り抜き作成・予約投稿）を使うだけの人は
[docs/team-setup.md](docs/team-setup.md) を参照してください。** Buffer/S3/Supabaseの秘密鍵は不要です。

## 概要

```
[ローカルのmp4] --S3アップロード--> [S3 公開プレフィックス (public URL)]
                                            |
                                            v
                                    [Buffer GraphQL API]
                                    /        |        \
                              TikTok   Instagram      X
                                            |
                                            v
                                [Supabase に結果を記録]
                                (channels / submissions / posts)
```

- 動画は一度S3の公開プレフィックス配下にアップロードし、安定した公開URLを得る
  （`{PREFIX}videos/yyyy/mm/uuid.mp4` に動画本体、`{PREFIX}thumbnails/yyyy/mm/uuid.jpg` に
  サムネイルを配置する。動画は60日で自動削除、サムネイルは管理画面カレンダー表示のため
  保持する）
- そのURLをBufferに渡し、TikTok / Instagram Reels / X への投稿（即時・キュー・予約）を行う
- 投稿結果（成功/失敗/外部リンクなど）はSupabaseの `posts` テーブルに記録される

## 前提ツール

- [uv](https://docs.astral.sh/uv/)（Pythonパッケージ管理・実行）
- `ffprobe`（動画の尺・解像度取得。ffmpegに同梱）
- AWS CLI（`aws`）（S3セットアップ・利用）
- Supabase CLI（`supabase`）（マイグレーション適用）

## セットアップ手順

1. **Buffer側の準備**
   - [Buffer](https://buffer.com) にサインアップ/ログイン
   - TikTok（ビジネスアカウントまたはクリエイターアカウント）、Instagram（プロアカウント）、
     Xアカウントをそれぞれ接続する
   - Buffer の Settings → API から**パーソナルアクセストークン**を発行する
     （投稿だけでなく指標取得にもパーソナルAPIキーが必要）

2. **S3バケットのセットアップ**

   ```bash
   ./infra/setup_s3.sh          # まずはdry-runで内容を確認
   ./infra/setup_s3.sh --apply  # 問題なければ実際に適用
   ```

   バケット名・リージョン・公開プレフィックスは `S3_BUCKET` / `S3_REGION` /
   `S3_PUBLIC_PREFIX` 環境変数で上書きできます（デフォルトは
   `sizuru-clips-<AWSアカウントID>` / `ap-northeast-1` / `public/`）。
   アップロード用の最小権限IAMポリシー例は `infra/iam-policy-uploader.json` を参照してください
   （`REPLACE_WITH_S3_BUCKET` / `REPLACE_WITH_S3_PUBLIC_PREFIX` を実際の値に置き換えて、
   アップロード用IAMユーザーに手動でアタッチしてください。IAMユーザー自体はこのスクリプトでは
   作成しません）。

3. **Supabaseプロジェクトの準備**

   Supabaseプロジェクト `sizuru-automatic-posting`（ref: `hdqrvhnhlyykocphetfg`、
   SnsCool's Org、`ap-northeast-1`）は作成済み・マイグレーション適用済みです。
   `.env` には既に `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` が設定されています。
   別環境で新規にプロジェクトを作る場合は以下を実行してください。

   ```bash
   supabase link --project-ref <YOUR_PROJECT_REF>
   supabase db push
   ```

4. **環境変数の設定**

   ```bash
   cp .env.example .env
   # .env を編集し、BUFFER_API_KEY / S3_* / SUPABASE_* を埋める
   ```

5. **依存関係のインストール**

   ```bash
   uv sync
   ```

6. **Buffer GraphQLスキーマの確認**

   ```bash
   uv run sizuru-post introspect --out buffer-schema.json
   ```

7. **接続済みチャンネルの確認**

   ```bash
   uv run sizuru-post channels
   ```

## 使い方

投稿リクエストは以下のようなJSONファイルとして作成します
（`.claude/skills/sns-publish` スキルを使うとClaude Codeが対話的に生成します）:

```json
{
  "video_path": "/abs/path/clip.mp4",
  "title": "切り抜きタイトル",
  "timing": { "mode": "addToQueue", "due_at": null },
  "draft": false,
  "thumbnail_offset_ms": null,
  "targets": [
    { "service": "tiktok", "caption": "...", "tiktok_title": null, "channel_id": null },
    { "service": "instagram", "caption": "...", "share_to_feed": true, "channel_id": null },
    {
      "service": "x",
      "caption": "...",
      "channel_id": null,
      "timing": { "mode": "shareNow", "due_at": null }
    }
  ]
}
```

各 `targets[]` にはトップレベルの `timing` を上書きする任意の `timing` を指定できます
（例: 上記のXだけ即時投稿にする、他は `addToQueue` のまま、など）。省略した場合は
トップレベルの `timing` が使われます。

```bash
# リクエストの検証
uv run sizuru-post validate posts/requests/20260914-210000.json

# 投稿の実行（--yes が無いと実行されない）
uv run sizuru-post publish posts/requests/20260914-210000.json --yes

# 失敗した投稿の再投稿（submission単位）
uv run sizuru-post retry <submission_id> --yes

# 予約済み/下書きの投稿を編集（承認必須）
uv run sizuru-post edit <post_id> [--caption-file caption.txt] [--due-at 2026-09-20T21:00:00+09:00] --yes

# 予約済み/下書きの投稿をキャンセル（承認必須）
uv run sizuru-post cancel <post_id> --yes

# Buffer側のステータスをSupabaseに反映（投稿済みへの更新・外部リンク取得）
uv run sizuru-post sync
```

`edit` / `cancel` は対象の投稿が「予約済み」または「下書き」のときのみ有効で、
`publish` / `retry` と同様に必ず人間の承認を得てから `--yes` を付けて実行します。

## スケジューリングAPI（sns-schedule スキル向け）

`.claude/skills/sns-schedule` スキルは、このCLIとは別にAWS上のスケジューリングAPI
（`sizuru_post.api.schedule`、`infra/sam/template.yaml` の `ApiFunction` に相乗り）を
HTTP経由で叩いて動画のアップロードと予約投稿を行います。認証は共有APIキー1本
（`x-api-key` ヘッダー、管理画面のSupabase JWTとは完全に別方式）。詳細な運用手順・
キー発行手順は `infra/sam/README.md` の「Auth: the scheduling API」「Local usage:
the sns-schedule skill」を参照してください。

- `POST /schedule/uploads` — 動画・サムネイル用の署名付きPOST URLを発行
- `GET /schedule/posts?from=&to=` — 指定期間内の予約状況（空き枠提案用）
- `POST /schedule/submissions` — アップロード済み動画からBuffer予約を作成（`dry_run` で検証のみも可）

## 制約

- **X（無料プラン）は動画尺140秒まで**。超える場合はXを投稿対象から外すか、動画を詰める
- **TikTokのドメイン確認リスク**: Buffer側でURLのドメイン確認が必要な場合があり、
  取り込みに失敗することがある
- **指標（いいね・再生数など）の取得は次段階**。本ツールは投稿と結果記録が対象
- **予約投稿中はS3オブジェクトを消さない**。`{PREFIX}videos/` 配下の動画は60日で自動削除される
  （`infra/setup_s3.sh` が設定するライフサイクルルール）ため、予約投稿が完了する前に
  手動で消さないよう注意する。なお `{PREFIX}thumbnails/` 配下のサムネイルは管理画面カレンダー
  表示のため自動削除の対象外で、保持され続ける

## 開発

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```
