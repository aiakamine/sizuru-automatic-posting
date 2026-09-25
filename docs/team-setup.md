# チーム向けセットアップ手順（スキル利用者向け）

このドキュメントは、`mitchaku-kirinuki` / `taidan-kirinuki`（切り抜き作成）と
`sns-schedule`（SNS予約投稿）の3つの Claude Code スキルを**使うだけ**の人向けです。
Buffer・S3・Supabase の秘密鍵は不要です。必要なのは `SIZURU_API_URL` と
`SIZURU_API_KEY`（管理者から個別に受け取る共有キー）の2つだけです。

## 1. 前提

- macOS（Vision OCR とヒラギノ角ゴシックに依存するため macOS 専用）
- [Claude Code](https://claude.com/claude-code) が使える状態になっていること
- このリポジトリ（`levela-inc/sizuru-automatic-posting`、非公開）への GitHub アクセス権
- [Homebrew](https://brew.sh/)

以下のツールも必要です（インストール手順は後述の手順3で自動チェックできます）：

| 必要なもの | 用途 |
| --- | --- |
| Xcode Command Line Tools（`swiftc`） | OCR CLI のビルド |
| ffmpeg / ffprobe | 動画の書き出し・無音検出・ラウドネス測定 |
| yt-dlp | 本編動画・自動字幕の取得 |
| Whisper（`openai-whisper`） | 発話とテロップの一致検査 |
| Python 3（3.9以上） + Pillow | 画像・バナー生成 |
| curl | サーバーへのアップロード・API呼び出し |

まとめて入れる場合：

```bash
xcode-select --install
brew install ffmpeg yt-dlp
python3 -m pip install --user Pillow openai-whisper
```

## 2. クローンする

**PR #1（`feat/buffer-auto-posting` → `main`）がマージされるまでは、
`main` ブランチではなく `feat/buffer-auto-posting` ブランチをクローンしてください。**

```bash
git clone -b feat/buffer-auto-posting git@github.com:levela-inc/sizuru-automatic-posting.git
cd sizuru-automatic-posting
```

（マージ後は普通に `git clone` で `main` を使えます）

## 3. `.env` を作る

```bash
cp .env.example .env
```

`.env` を開いて、ファイル冒頭の**スキル利用者向けの2行だけ**を埋めてください：

```
SIZURU_API_URL=https://4ynlgoe3h2.execute-api.ap-northeast-1.amazonaws.com
SIZURU_API_KEY=（管理者から個別に受け取ったキーをここに貼る）
```

それ以外（Buffer / S3 / Supabase の項目）は「CLI・運用者のみ」向けなので**空のままで構いません**。

`SIZURU_API_KEY` は他人と共有しない・Slack等に貼らない・コミットしないでください
（`.env` は `.gitignore` 済みです）。

## 4. 環境チェックを実行する

```bash
bash scripts/check_skill_env.sh
```

必要なツールと `.env` の設定、サーバーへの疎通を確認し、項目ごとに ✅/❌ で表示します。
❌ が出た項目は、表示された「直す」の手順に従ってください。すべて ✅ になってから次に進みます。

## 5. Claude Code を開いて使う

```bash
cd sizuru-automatic-posting
claude
```

日本語で頼むだけで、スキルの説明文（description）を見て自動的に発火します。
スラッシュコマンドを明示する必要はありません。

例：

```
この密着からショート5本作って https://youtu.be/XXXXXXXX
この対談回から切り抜き6本お願い https://youtu.be/XXXXXXXX
予約投稿して
```

聞かれること：本編URL／本数／公開チャンネル／目的（認知・送客・採用など）／納品先。

所要時間の目安：切り抜き作成は1本あたり数分〜、6本で1〜2時間程度。

## 6. hooksによる強制事項（プロンプトだけに頼らない）

このリポジトリには、以下をコマンドレベルで**強制的にチェックする仕組み（hooks）**が入っています。
スキルにお任せしていれば自然に満たされますが、念のため何をチェックしているかを知っておくと安心です。

- **本編URL必須**：予約するすべてのクリップに、本編（YouTube）のURLが `main_video_url` として
  設定されていること。設定なしでは予約できません。
- **本編URLを示した上での「承認して予約」**：本編URLを含む内容についてAskUserQuestionで
  ユーザーに確認し、「承認して予約」で始まる回答を得てからでないと、実際の予約実行
  （`submit --yes`）はブロックされます。
- **完成動画ページの作成**：切り抜き作業を終える（Stop）前に、完成動画一覧のアーティファクト
  （ページ）が作られていることが必須です。
- **予約投稿するかどうかの確認**：作業を終える前に「使えると判断した本を予約投稿しますか？」
  という質問に回答済みであることが必須です（回答が「いいえ」でも構いませんが、質問と回答は必須）。

これらを満たさない状態で無理に進めようとすると、コマンドが拒否されたりStopが差し戻されたりします。
エラーメッセージに従って手順を戻ってやり直してください。

## 7. 管理画面

予約した投稿の一覧・編集・キャンセルは管理画面から行えます。

- URL: https://d41a4erk550yt.cloudfront.net
- ログインIDは別途、管理者から共有されます（このドキュメントには載せません）

## 8. 更新する

スキルやhooksは随時更新されます。作業前に最新化してください：

```bash
git pull
```

## 9. トラブルシューティング

| 症状 | 原因・対処 |
| --- | --- |
| `check` コマンドが `401` エラーになる | `SIZURU_API_KEY` が間違っている・失効している。管理者に確認してください |
| `check` コマンドが `503` エラーになる | サーバー側のキー設定が未完了。管理者に連絡してください（利用者側の設定ミスではありません） |
| `python3 -c "import PIL"` が失敗する（Pillowが無い） | `python3 -m pip install --user Pillow` |
| `yt-dlp` が `403` で失敗する | `yt-dlp --cookies-from-browser chrome` を使うと通ります（`player_client` の切替は画質が落ちるため非推奨） |

上記で解決しない場合は、`scripts/check_skill_env.sh` の出力を添えて管理者に相談してください。
