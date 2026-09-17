---
name: sns-publish
description: ローカルの9:16 MP4動画を、人間の承認を経てBuffer経由でTikTok/Instagram Reels/Xに投稿する。トリガー例:「SNSに投稿して」「TikTokとインスタとXに投稿」「この動画を投稿して」「予約投稿して」「sns-publish」。動画クリップの作成自体は対象外（kiyoto-video-cut等の出力を入力として使う）。
---

# sns-publish

ローカルにある9:16のMP4動画（例: `kiyoto-video-cut` の出力）を、S3経由の公開URLとして
Buffer GraphQL APIに渡し、TikTok / Instagram Reels / X に投稿する。**投稿は必ず人間の
明示的な承認を得てから実行する。** クリップの切り抜き・編集はこのスキルの対象外。

## Phase 0: 環境チェック

- `.env` が存在するか確認する。存在しない、または以下の必須変数が欠けている場合は
  ユーザーに知らせて `.env.example` からのセットアップを促す。**値そのものは絶対に
  出力・ログに表示しない。**
  - `BUFFER_API_KEY`
  - `S3_BUCKET`
  - `S3_REGION`
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_ROLE_KEY`
  （Supabaseプロジェクト `sizuru-automatic-posting` は作成・マイグレーション適用済みで、
  通常の `.env` には既にこれらの値が入っている）
- DBにチャンネルが登録されているか未確認/不明な場合は `uv run sizuru-post channels` を
  実行して、投稿先として使える連携済みチャンネル一覧を確認する。

## Phase 1: 投稿条件のヒアリング（AskUserQuestion）

以下をAskUserQuestionで確認する（選択肢は具体的に提示する）:

1. **投稿先**（multiSelect）: TikTok / Instagram / X
2. **タイミング**:
   - 今すぐ投稿 (`shareNow`)
   - キューの次枠に追加 (`addToQueue`)
   - 日時指定 (`customScheduled`) — この場合、日時をJST（日本時間）で聞き、
     ISO8601形式 (`+09:00` オフセット付き、例: `2026-09-20T21:00:00+09:00`) に変換する
3. **下書きのみ (draft) か本投稿か**

媒体ごとにタイミングを変えたいという要望があれば（例:「Xだけ今すぐ、他はキューで」）、
どの媒体をどうしたいか個別に確認する。これは request.json の各 `targets[]` の
`timing` オーバーライドとして反映する（Phase 3参照）。

## Phase 2: キャプション作成

動画の文脈を集める:

- 動画と同じフォルダに字幕ファイル（`.srt` / `.ass`）や `kiyoto-video-cut` の出力フォルダに
  文字起こし（transcript）があれば読み込んで内容を把握する
- なければユーザーに一言サマリーを聞く

上記の文脈をもとに、サービスごとにキャプションを作成する:

- **TikTok**: 冒頭1行でフックを作る＋ハッシュタグ3〜5個。上限2200字。任意で `tiktok_title` も提案する
- **Instagram Reels**: 改行を入れて読みやすくし、保存/フォローを促す一文を含める。
  ハッシュタグは30個以内。上限2200字
- **X**: 短文で、全角換算140字程度（280文字の重み付けカウント以内）に収める。
  ハッシュタグは1〜2個

## Phase 3: request.json 作成と validate

`posts/requests/<YYYYMMDD-HHMMSS>.json` に以下のスキーマで書き出す:

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
    { "service": "x", "caption": "...", "channel_id": null }
  ]
}
```

各 `targets[]` には、トップレベルの `timing` を上書きする任意の `timing` フィールドを
指定できる（例:「Xだけ今すぐ、TikTok/Instagramはキューに追加」のように媒体ごとに
タイミングを変えたい場合）。ユーザーから媒体ごとに異なるタイミングの希望があれば、
Phase 1のヒアリング時に確認し、該当するtargetにのみ `timing` を追加する。

書き出した後、必ず以下を実行する:

```bash
uv run sizuru-post validate <request.json>
```

エラーが出た場合はキャプションを修正し、再度validateする（例: 「X が140秒超」→
Xを対象から外すか、キャプションを詰めるようユーザーに提案する）。

## Phase 4: 最終確認（承認は必須・毎回取り直す）

以下をユーザーに分かりやすく提示する:

- サービスごとの最終キャプション
- タイミング（shareNow / addToQueue / customScheduled + due_at）
- 動画情報（パス、タイトル、下書きかどうか）

その上でAskUserQuestionで明示的に承認を得る:「承認して投稿」/「修正する」/「やめる」。

**重要**: `--yes` を付けた投稿系コマンド（publish / retry / edit / cancel）は、この会話内で
明示的な承認を得るまで絶対に実行しない。ある動画・投稿への承認が、別の動画・投稿への承認を
意味することはない（対象ごとに毎回承認を取り直す）。

## Phase 5: 投稿実行

```bash
uv run sizuru-post publish <request.json> --yes
```

出力のJSONレポートをパースし、サービスごとに分かりやすく報告する:

- 予約済 / 投稿済 / 下書き / 失敗（理由） / スキップ（理由）

失敗があれば、リトライを提案する（これも承認を得てから実行する）:

```bash
uv run sizuru-post retry <submission_id> --yes
```

### 予約済み/下書き投稿の編集・キャンセル

ユーザーから既存の予約投稿・下書きの変更/取り消しを頼まれた場合は、以下のコマンドを使う。
どちらも対象の投稿が「予約済み」または「下書き」のときのみ有効で、`publish` / `retry`
と同様にPhase 4と同じ要領で必ず承認を得てから `--yes` を付けて実行する:

```bash
# キャプションや投稿日時を変更
uv run sizuru-post edit <post_id> [--caption-file caption.txt] [--due-at 2026-09-20T21:00:00+09:00] --yes

# 予約投稿・下書きをキャンセル
uv run sizuru-post cancel <post_id> --yes
```

## Phase 6: 事後のステータス同期

`uv run sizuru-post sync` は、Buffer側の投稿ステータスを確認し、Supabase上のstatusを
「投稿済」に更新し、外部リンク（external_link）を埋める。投稿直後に実行してもよいし、
後で改めて実行してもよい旨をユーザーに伝える。

## 注意事項

- **TikTokのURL取り込み**: Bufferの仕様上、動画URLのドメイン確認が必要になり、
  取り込みに失敗する可能性がある。失敗した場合はエラーメッセージをそのままユーザーに
  伝え、原因（ドメイン未確認など）を推測して報告する。
- **Buffer APIのレート制限**: Freeプランは3,000リクエスト/30日、Essentialsプランは
  7,500リクエスト/30日。頻繁な投稿・同期で制限に近づく場合は注意を促す。
- **S3のオブジェクトやDBのレコードは、ユーザーの確認なしに削除しない。**
  予約投稿が完了していない（`due_at` 未到来）submissionの動画（`{PREFIX}videos/` 配下）は
  特に消さない。なお動画は60日で自動削除されるが、サムネイル（`{PREFIX}thumbnails/` 配下）は
  管理画面カレンダー表示のため自動削除されず保持される。
