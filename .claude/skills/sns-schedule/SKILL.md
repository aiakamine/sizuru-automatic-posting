---
name: sns-schedule
description: 切り抜き動画を予約管理API経由でBuffer予約する（TikTok/Instagram/X、複数本・媒体別キャプション対応）。トリガー例:「予約投稿して」「切り抜きを予約」「sns-schedule」。単体でも使えるが、mitchaku-kirinuki／taidan-kirinukiの納品→FB後（DREAM.md 1.5）からも呼ばれる。動画の切り抜き自体やBufferの直接投稿（今すぐ投稿・下書き）はsns-publishの対象。
---

# sns-schedule

複数本の切り抜き動画を、`.env` の `SIZURU_API_URL` / `SIZURU_API_KEY`（管理画面と同じ予約API、
`x-api-key` 認証）経由でBuffer予約する。API呼び出し前に**必ず会話内で承認を得る**
（承認 = API呼び出しの許可）。実装は `scripts/schedule.py`（Python標準ライブラリ＋
ffprobe/ffmpeg/curl）のサブコマンドを呼び出す形で進める。

## 0. 設定確認

```bash
python3 "$CLAUDE_PROJECT_DIR/.claude/skills/sns-schedule/scripts/schedule.py" check
```

`{"ok": true}` なら次へ。`unauthorized`/`unreachable`/設定なしのいずれかで失敗したら、
ユーザーに「リポジトリの `.env` に `SIZURU_API_URL` と `SIZURU_API_KEY` を追記してください」と伝える。
**値そのものは絶対に出力・ログに表示しない。**（`schedule.py` 自体もAPIキーを一切印字しない）

## 1. 対象クリップを決める

- kirinuki系スキルのFB（DREAM.md 1.5）から呼ばれた場合：FBで「使える」と判断された本だけを対象にする
- 単体で呼ばれた場合：ユーザーに納品フォルダ（またはmp4のパス）を確認する
- 各クリップについて、納品時に決めた**投稿時タイトル**と`編集指示/`のテロップ全文（無ければ
  ユーザーに一言サマリーを聞く）を読み込み、キャプション作成の材料にする

### 本編（YouTube）URLの確認

投稿は必ずYouTubeの本編に誘導する。この本編URLを確認する:

- mitchaku-kirinuki／taidan-kirinukiから呼ばれた場合：`$D/hearing.md`の`## 本編URL`節から
  読み取り、ユーザーに確認だけ取る（両スキルとも必須項目なので、hearing完了時点で
  必ず入っている）
- 単体利用の場合：ユーザーに直接聞く
- 複数クリップで本編が異なる場合は、クリップごとに個別のURLを聞く（`plan.json`の
  クリップ単位`main_video_url`で上書きする。手順4参照）

**取得したURLは、表示・`plan.json`への記載・キャプションへの埋め込みの前に必ず正規化する**：
どんな形（`youtu.be/<ID>`・`m.youtube.com`・`www.`無しの`youtube.com/watch`・
共有ボタンで付く`si`等のトラッキングパラメータ付き）で来ても、**`https://www.youtube.com/watch?v=<ID>`
の一つの形に統一する**（再生開始秒`t`は残す。`si`/`feature`/`pp`/`utm_*`等のトラッキングと
`list`は落とす）。例: `https://youtu.be/MCmsezNx-Os?si=rwDOsGT0rpujKWYP`
→ `https://www.youtube.com/watch?v=MCmsezNx-Os`。
`schedule.py`側（`validate`/`submit`のゲート/`gallery`）も読み取り時に同じ正規化を
かけるので二重に保険が効くが、**ここで正規化してから使うことで、キャプション文中の
URL表記とplan.jsonの`main_video_url`が常に一致した形になる**（一致していないと、
文字列としては同じ動画を指していても`validate`/`gate_submit.py`のチェックで
食い違いと判定されうる）。

## 2. 開始日・時刻・1日何本をAskUserQuestionで聞く

**既定ルールは持たない。毎回聞く**（1週間空ける・毎日1本などの基本ルールは無い）:

1. **開始日**（YYYY-MM-DD）
2. **1日何本**（`--per-day`）
3. **投稿時刻**
   - 1日1本なら、時刻を1つ聞く（`--time`）
   - 1日2本以上なら、**本数と同じ数だけ時刻を1つずつ**聞く（例: 1日2本なら
     「1本目は何時？」「2本目は何時？」と個別に聞き、`12:00` と `19:00` のように集める）。
     まとめて `--times 12:00,19:00` として渡す。**`--times` の個数と `--per-day` が
     一致しないと `slots` はエラーで止まる**（黙って本数を間引いたり単一時刻を使い回したり
     はしない仕様）

3媒体（TikTok/Instagram/X）は**同じクリップなら同日時**に揃える。

## 3. 空き枠を提案する

```bash
python3 "$CLAUDE_PROJECT_DIR/.claude/skills/sns-schedule/scripts/schedule.py" slots \
  --start 2026-09-25 --time 19:00 --per-day 1 --count 4
# 複数時刻: --times 12:00,19:00 --per-day 2
```

既存の予約・投稿済み（62日先まで）と±30分以内に被る枠は自動で飛ばされる。結果を表で見せ、
「この日時でよいか」を確認する。ずらしたい・本数を変えたい場合は`--start`/`--per-day`/`--count`を
変えて再実行する（提案は取得時点のスナップショットなので、管理画面側で同時に編集されていた
場合はズレることがある旨を伝える）。

## 4. 媒体別キャプションを作る

キャプションのルールは `.claude/skills/sns-publish/SKILL.md` の **Phase 2** に従う
（TikTok: フック＋ハッシュタグ3〜5個／Instagram: 改行＋保存・フォロー訴求＋ハッシュタグ30個以内／
X: 全角換算140字程度）。文脈は**投稿時タイトル＋`編集指示/`のテロップ全文**から作る。

**本編（YouTube）への誘導を必ず入れる**（手順1で確認した本編URL）:

全プラットフォームのキャプションに本編URLをそのままテキストで含める（プロフィールリンクは
設定しない方針。TikTok／Instagramはクリック可能なリンクにできないが、そのまま受け入れる）：

- **X**: キャプションに本編URLをそのまま含める（Xの重み付き文字数カウントでは
  URLは長さに関わらず23文字として扱う。全角換算140字程度の枠内に収める）
- **TikTok／Instagram**: 「**本編はこちら** `<本編URL>`」のように短い導入＋URLの形で
  キャプションに含める

固まったら、作業フォルダ（kirinukiスキルの `$D`、単体利用ならスクラッチパッド配下）に
`plan.json` を書き出す:

```json
{
  "project": "営業統括密着",
  "delivery_dir": "/abs/path",
  "main_video_url": "https://www.youtube.com/watch?v=XXXXXXXX",
  "clips": [
    {
      "no": 1,
      "file": "/abs/01_評価されない営業マンへ.mp4",
      "title": "評価されない営業マンへ",
      "thumbnail_offset_ms": null,
      "main_video_url": null,
      "targets": [
        {"service": "tiktok", "caption": "... 本編はこちら https://www.youtube.com/watch?v=XXXXXXXX #...", "due_at": "2026-09-25T19:00:00+09:00", "tiktok_title": null},
        {"service": "instagram", "caption": "... 本編はこちら https://www.youtube.com/watch?v=XXXXXXXX #...", "due_at": "2026-09-25T19:00:00+09:00", "share_to_feed": true},
        {"service": "x", "caption": "... https://www.youtube.com/watch?v=XXXXXXXX", "due_at": "2026-09-25T19:00:00+09:00"}
      ]
    }
  ]
}
```

- トップレベルの `main_video_url`（文字列・必須）：この企画の本編URL
- クリップ単位の `main_video_url`（省略可・省略時は`null`）：このクリップだけ別の本編に
  誘導したい場合の上書き（手順1参照）。通常は`null`のままでよい

## 5. ローカル検証

```bash
python3 "$CLAUDE_PROJECT_DIR/.claude/skills/sns-schedule/scripts/schedule.py" validate "$D/plan.json"
```

キャプション文字数（Xは概算。厳密な判定は`submit`のdry-runで行う）・Instagramハッシュタグ数・
X動画140秒制限・due_atの未来/タイムゾーン・媒体重複に加えて、本編URL関連もチェックする:
`main_video_url`が無ければ**エラー**、YouTube形式（`youtube.com/watch`・`youtu.be/`・
`youtube.com/live/`）でなければ**警告**、X／TikTok／Instagramいずれかのキャプションに
本編URLが含まれていなければ**エラー**。
エラーがあればキャプションや日時を直して再実行する。

## 6. アーティファクトに予約案を追記する

`$D/gallery_data.json` は「完成動画アーティファクト」の手順で作った、動画URL（`video_url`）
入りのbaseデータ。**このファイルの`clips[].schedule`を手で書き換えたり作り直したりしない**
（`video_url`など既存の情報を失う）。代わりに`--plan`を渡して`gallery`コマンドにマージさせる:

```bash
python3 "$CLAUDE_PROJECT_DIR/.claude/skills/sns-schedule/scripts/schedule.py" gallery \
  --data "$D/gallery_data.json" --plan "$D/plan.json" --out "$D/preview/page.html"
```

`--plan`を指定すると、`plan.json`の`due_at`・媒体別キャプション（文字数・上限も自動計算）を
`gallery_data.json`の各クリップに`no`で対応付けてマージする。`submit`前なので全ターゲットは
自動的に`status: "proposed"`になる。`video_url`は常に`gallery_data.json`側の値が使われる
（`plan.json`は動画URLを持たない）。

生成したHTMLを、**既存のアーティファクト（`$D/artifact.json` の `url`）と同じファイル／URLに
対してArtifact toolで再公開する**（動画本体は「完成動画アーティファクト」の手順で既に
添付済みのはず。無ければ先にそちらを行う）。

### gallery_data.json / gallery コマンドのデータスキーマ

`--data`（base）のトップレベル: `project, delivered_at, delivery_dir, main_video_url, clips`
（`main_video_url`はページ上部に本編へのリンクとして表示される。「完成動画アーティファクト」の
手順で本編URLを入れておく。手順5参照）。各クリップ: `no, file, title,
title_options[{text,reason}], duration_sec, size_bytes, video_url, schedule`
（base単体では`schedule`は`null`のままでよい）。`--plan`を渡すと`schedule`が次の形で埋まり
（`no`が一致しないclipは`schedule`をbaseのまま保持する）、`plan.json`の`main_video_url`が
あればトップレベルの`main_video_url`を上書きする（無ければbaseの値を保持）:

```json
{"due_at": "2026-09-25T19:00:00+09:00", "targets": [
  {"service": "tiktok", "caption": "...", "length": 32, "limit": 2200,
   "status": "proposed", "external_link": null, "error": null}
]}
```

`--state`（省略時は`<--plan>.state.json`）に`submission_result`があれば、そこから
`status`/`external_link`/`error`を反映する（無ければ全て`status: "proposed"`）。

## 7. 承認を得る（必須・スキップ不可）

日時・キャプション・対象クリップを表で見せ、AskUserQuestionで
「承認して予約」／「修正する」／「やめる」を聞く。**このplanへの承認が、別のplanや
別回の予約を意味することはない。** 「修正する」なら該当ステップに戻る。

**質問文（question）には、このplanで使う本編URL（`main_video_url`。クリップごとに
異なる場合は全て）を必ず文字通り含める**（例:「本編URL https://www.youtube.com/watch?v=XXXXXXXX
でこの内容を予約してよいですか？」）。選択肢のラベルは**必ず「承認して予約」で始める**
（末尾に「(推奨)」等を付けてもよい）。`submit --yes` の実行は、PreToolUseフック
（`.claude/hooks/gate_submit.py`）がこの質問文とラベルをそのまま検査するため、
文言がずれると承認していても拒否される。plan.jsonをこの承認の後に直したら、
**同じ質問を聞き直す**（ファイルの更新時刻より前の承認は無効として扱われる）。

## 8. 予約実行

```bash
python3 "$CLAUDE_PROJECT_DIR/.claude/skills/sns-schedule/scripts/schedule.py" submit "$D/plan.json" --yes
```

クリップごとに（未アップロードなら）動画・サムネイルをS3へ署名付きPOSTでアップロードし
（`plan.json.state.json` にキーを保存するので、失敗しても同じキーで再実行できる＝二重アップロード
されない）、dry-run検証→本予約の順で呼ぶ。`incomplete: true` は自動で最大3回再送する
（冪等なので安全）。

## 9. 結果報告とアーティファクト更新

`submit`の出力には各クリップの結果に加えて `summary`（`scheduled`/`failed`/`unknown`/
`skipped`/`not_attempted`の件数）が入る。この4つのいずれかが1件でもあると`submit`は
終了コード非0で終わる（`--yes`忘れによる終了コード2とは別）。サービスごとの結果を表
（本・媒体・日時・状態）で報告する:

- **unknown**（結果不明）: 再送しない。Buffer管理画面で実際に予約されたか確認するよう案内する
- **not_attempted**（29秒の実行時間内に処理できなかった）: `incomplete: true`のまま3回
  自動再送しても解決しなかった場合にこの状態で返る。ユーザーに状況を伝え、少し時間を置いてから
  同じ`submit --yes`を再実行してよい旨を案内する（冪等なので安全）
- **skipped**（dry-run検証でエラーが出て本予約に進まなかったクリップ）: dry-runの`plan`に
  含まれる`issues`をユーザーに見せ、キャプションや日時を直してから再実行するよう案内する
- **failed**: 原因を伝える。再送する場合は次の順で進める：
  1. `plan.json` の該当ターゲットの `caption`/`due_at` を修正する
  2. `gallery --data "$D/gallery_data.json" --plan "$D/plan.json" --out ...` で再生成し
     （手順6と同じ書き方）、直したターゲットが `予約案` 表示になっていることを確認する
  3. 手順7と同じ要領で**改めてAskUserQuestionで承認を得る**
  4. 同じ `submit --yes` を再実行する（video_key等はstateに残っているので再アップロードされない）。
     同じvideo_keyでの再送は**failedのターゲットだけ**をサーバーが引き取り、
     `plan.json`の新しいcaption/due_atで再送する
- **scheduled/sent/draftになったターゲットは再送では一切変更されない**。日時・キャプションを
  直したい場合は、再送ではなく管理画面（https://d41a4erk550yt.cloudfront.net ）で編集する
- **canceledのターゲットは再送しても取り消し状態のまま**（管理画面で編集・再登録する）
- 成功分は `gallery --data "$D/gallery_data.json" --plan "$D/plan.json" --out ...`
  （手順6と同じ書き方。`plan.json.state.json`から`status`/`external_link`/`error`が自動反映される）
  で再生成し、手順6と同じURLへ再公開する
- 管理画面URL（https://d41a4erk550yt.cloudfront.net ）を案内する

## 注意事項

- **ターンを終えずに次のAskUserQuestionへ進める**：kirinukiスキルから呼ばれている場合、
  会話を終えると`stop_improve`フックが差し戻す。手順2・7の質問はターン内で連続して行う
- **X は動画140秒超で予約不可**（`validate`/`submit`のdry-runで検出）
- S3のオブジェクトはユーザーの確認なしに削除しない
- APIキーは`.env`から読むのみで、標準出力・ログに一切出さない

## hooksによる強制（プロンプトだけに頼らない）

このリポジトリの `.claude/settings.json` に、プロジェクト全体で有効な2つのhookが登録されている:

- **PostToolUse（`AskUserQuestion`）→ `.claude/hooks/record_asks.py`**：全ての
  AskUserQuestionの質問文・選択肢・回答を `kirinuki_hooks.asks` 経由でセッションごとの
  ログ（`~/.claude/kirinuki-state/asks/<session_id>.jsonl`。`KIRINUKI_STATE_DIR`で
  上書き可）に記録する。何もブロックしない（事後フック）
- **PreToolUse（`Bash`）→ `.claude/hooks/gate_submit.py`**：コマンドが
  `schedule.py submit <plan.json> --yes` のとき、実際に`submit`が実行される前に
  (1) `plan.json`の本編リンク要件（`main_video_url`、および X/TikTok/Instagram
  全キャプションへの本編URL含有）と、(2) 上記asksログに
  「本編URLを含む質問」への「承認して予約」で始まる回答が**plan.jsonの最終更新時刻より後**に
  記録されているか、を検査する。どちらかを満たさなければ`submit --yes`自体を拒否する
  （手順7の質問文・選択肢ラベルの書き方に依存するので、そこを勝手に変えない）

`schedule.py submit --yes`を含まない・解析できないコマンドは検査せず許可する
（無関係な操作を壊さない）。依存（`kirinuki_hooks`/`sns-schedule/scripts`）が
見つからない場合もsystemMessageで知らせた上で許可する（黙って素通ししない）。
