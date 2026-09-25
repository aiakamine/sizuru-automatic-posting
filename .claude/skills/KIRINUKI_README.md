# 切り抜き生成スキル（Claude Code）

YouTube本編から、ショート用の切り抜きを**複数本まとめて**制作する Claude Code スキル2種。
シーン選定と文言はClaudeが判断し、書き出しとQCは同梱スクリプト（ffmpeg＋Vision OCR）が行う。

**元動画に焼き込みテロップがあることが前提。** テロップの切替をフレーム単位で検出し、
その表を唯一のカット座標系として使う設計になっている。テロップの無い素材には向かない。

**初めて使う人は [docs/team-setup.md](../../docs/team-setup.md) の手順（クローン・`.env`・
環境チェック）から始めてください。**

## 2つの使い分け

| スキル | 対象コンテンツ | レイアウト | 既定本数 |
| --- | --- | --- | --- |
| `mitchaku-kirinuki` | 密着動画（1日密着・完全密着） | 黒背景・白黄見出し・赤引用（全編静止） | 5本 |
| `taidan-kirinuki` | 対談・解説・実演系 | バナー＋映像バンド＋CTA帯 | 6本 |

**対談・解説系に密着レイアウトを使わない。逆も同様。** 見た目が別物になる。

## 共通の設計思想

1. 元動画の**焼き込みテロップの切替をフレーム単位で検出**し、Vision OCR で `telops.txt`
   （番号／開始-終了／本文）を作る。**カット点は必ずこの表の境界に置く**。
   自動字幕（VTT）はタイミングが不正確なので、文脈把握にしか使わない。
2. 4つの絶対ルール（各 SKILL.md 冒頭）を機械判定で潰す。
   テロップの表示ずれ／フックの弱い出だし／トランジション欠落／文脈の断絶・文中切り。
3. 書き出しは trim＋concat **フィルタ**で1コマンド（個別mp4のconcatデムクサはA/Vズレが累積する）。
4. QC（全編OCR・ラウドネス・尺・A/V duration差）まで通してから納品する。
5. 納品物は mp4 だけでなく、`編集指示/`（元データのタイムコードとテロップ全文）、
   `投稿タイトル案.txt`、`00_はじめに.txt` まで含める。

## 使うたびに精度を上げる仕組み（ヒアリング → 制作 → 改善フェーズ）

両スキルとも、SKILL.md の frontmatter に Claude Code のフックを持っている。スキルを起動するとそのセッションで有効になる。

| タイミング | 仕組み | 止める条件 |
| --- | --- | --- |
| 制作の前 | `hooks/gate_hearing.py`（PreToolUse / Bash） | 作業フォルダの `hearing.md` が `状態: 確定` でない、または必須項目が空のまま、ベース画像・バナー生成や書き出しを実行しようとした |
| `improve.md` を書く前 | `hooks/gate_improve_write.py`（PreToolUse / Write\|Edit） | DREAM.md 1.5「使えると判断した本を予約投稿しますか？」をこのセッションでまだAskUserQuestionしていない（予約する／今回はしない、どちらの回答でもよい） |
| 終了の前 | `hooks/stop_improve.py`（Stop） | `out/` に mp4 があるのに `improve.md` が `状態: 完了` でない、`$D/artifact.json`（完成動画アーティファクト）が無い、または上記の予約確認が未回答。注意は「前回の注意から mp4 の顔ぶれ（名前とサイズ）が変わったとき」だけ出る。1回の終了につき注意は1回で、そのまま終えれば2回目は通る |

- **ヒアリング**：`references/hearing.md` の手順で、1問ずつ深掘りして前提を確定する（グリルミー方式）。素材取得・テロップ走査は並行で走らせる
- **改善フェーズ**：`DREAM.md` の手順で、FB → （1.5 投稿予約の確認）→ 改善提案 → **ユーザーの承認** → 反映。承認されたものだけがスキルに入る
- **改善ログ**：`references/learnings.md` に毎回追記する。却下した提案も残すので、同じ提案を繰り返さない
- 記録ファイルの雛形は `templates/hearing.md` / `templates/improve.md`。見出し名はフックの検査と一致させてある。
  先頭の `skill:` 行が記録の持ち主で、別スキルのフックはその記録を検査しない
- フックの中身は `kirinuki_hooks/`（リポジトリ直下）にまとめてあり、各スキルの `hooks/*.py` はそこを呼ぶだけ。
  見つからない場合は「検査していない」と毎回知らせる（黙って素通りしない）
- `kirinuki_hooks/asks.py` は、全てのAskUserQuestionの質問・回答をセッションごとに記録する
  （`~/.claude/kirinuki-state/asks/<session_id>.jsonl`。`KIRINUKI_STATE_DIR`で上書き可）。
  記録自体はこのリポジトリの `.claude/settings.json` に登録された `.claude/hooks/record_asks.py`
  （プロジェクト全体で有効なPostToolUseフック）が行い、`gate_improve_write.py`・`stop_improve.py`の
  予約確認チェックと、sns-schedule の `.claude/hooks/gate_submit.py`（本編リンク必須・承認必須。
  詳細は `.claude/skills/sns-schedule/SKILL.md` の「hooksによる強制」節）がこのログを読む
- 発話とテロップの一致検査（`align_check.py`）・語尾分類や無音判定（`cutpoints.py`）・
  テロップ帯の走査（`subscan.py`/`subtext.py`系）・fps取得の判定ロジックは
  `kirinuki_core/`（リポジトリ直下）にまとめてあり、両スキルの `scripts/*.py` がそこから import する。
  各スキルの `scripts/_core.py` が実行時に「スキルディレクトリの親→`$CLAUDE_PROJECT_DIR`→`~/.claude`」の順で
  `kirinuki_core` を探し、見つからなければ install.sh の実行を案内して終了コード1で止まる
- テスト：`python3 -m unittest discover -s kirinuki_hooks/tests`（共通フック）、
  `python3 -m unittest discover -s kirinuki_core/tests`（共通判定ロジック）、
  `python3 -m unittest discover -s <スキル>/tests`（スキル側）
- 回帰テスト（mitchaku の書き出し〜QC を実素材で通す。約3〜4分）：
  `python3 tests/regression/run.py --ocrcli <ビルドした ocrcli>`。フレーム数・元素材との頭尻対応・
  LUFS・QC の NG 数・重複フレームを `tests/regression/expected.json` と比べる。
  素材 `tests/regression/fixture/clip.mp4` は社内検証用に公開済み密着動画から約60秒を抜き出したもの。
  **このリポジトリの外へ再配布しない（リポジトリを公開する場合は差し替える）**

## 動作環境

**macOS 専用。** Vision.framework（OCR）と ヒラギノ角ゴシック に依存しているため、
Windows / Linux では動かない。

| 必要なもの | 用途 | 確認コマンド |
| --- | --- | --- |
| Xcode Command Line Tools | `swiftc` で OCR CLI をビルド | `swiftc --version` |
| ffmpeg / ffprobe | 書き出し・無音検出・ラウドネス測定 | `ffmpeg -version` |
| yt-dlp | 本編と自動字幕の取得 | `yt-dlp --version` |
| Whisper（`openai-whisper`、CLI: `whisper`、model: small） | `align_check.py`（発話とテロップの一致検査・必須工程）の音声認識 | `whisper --help` |
| Python 3 + Pillow | ベース画像・バナーのPNG生成 | `python3 -c "import PIL"` |
| ヒラギノ角ゴシック W8 / W9 | 見出し・バナーの書体（macOS標準搭載） | `ls /System/Library/Fonts/ヒラギノ*` |

```bash
xcode-select --install
brew install ffmpeg yt-dlp
python3 -m pip install --user Pillow openai-whisper
```

## 導入

このリポジトリ（`sizuru-automatic-posting`）の `.claude/skills/` 配下に、2スキル
（`mitchaku-kirinuki/`・`taidan-kirinuki/`）と共通コード（`kirinuki_core/`・`kirinuki_hooks/`）が
そのまま入っている。追加のインストール手順は不要で、このリポジトリを Claude Code で開けば
`.claude/skills/` 探索の対象として自動的に認識される。

以前使っていた `~/.claude/skills/` への symlink 導入（`install.sh`）はこのリポジトリでは使わない
（全体側の同名スキルへの symlink は既に外してある）。動作環境（Vision OCR・ffmpeg・yt-dlp・
Whisper・ヒラギノ角ゴシック）は上記の「動作環境」節のとおり用意する。

## 使い方

Claude Code に日本語で頼むだけでよい（スラッシュコマンドを明示しなくても description で発火する）。

```
この密着からショート5本作って https://youtu.be/XXXXXXXX
この対談回から切り抜き6本お願い https://youtu.be/XXXXXXXX
```

聞かれること：本編URL／本数／公開チャンネル／目的（認知・送客・採用など）／納品先。
不明なままでも素材取得は先に走る。

所要時間の目安：全フレーム走査（`subscan.py`）に3〜5分、書き出しとQCを含めて1本あたり数分。
6本で1〜2時間程度みておく。

## 使う前に必ず自分のチャンネルで実測すること

このスキルに書いてある数値は**別チャンネルでの実測値をサンプルとして残したもの**。
そのまま使わず、必ず自分のチャンネルで取り直して上書きする。

- **レイアウト**：`taidan-kirinuki` は自チャンネルの公開済みショートを1本落として
  `measure_layout.py` で実測し、`references/layout.md` の表と違ったら
  `clips.py` の `LAYOUT` と `scripts/banner.py` の定数
  （`BODY_W_TOP` / `BODY_H` / `SLANT` / `LEFT` / `RAD` / `C_L` / `C_R`）を
  合わせてから進む（SKILL.md の手順1）。バナーの意匠自体もシリーズごとに違うので、
  `banner.py` は自チャンネルのデザインに作り替える前提で読む。
  `mitchaku-kirinuki` のレイアウト（黒背景・見出し・赤引用の座標）も同様に実測値のハードコード。
- **投稿タイトルの文法**：`mitchaku-kirinuki/SKILL.md` の「投稿タイトルの文法」は
  ある企業チャンネルの密着系4本の実測から出した型。
  `yt-dlp --flat-playlist --print "%(view_count)s|%(title)s" <チャンネル>/shorts` で
  自チャンネルの再生数つき一覧を取り、上位本の共通型を出し直してから使う。
- **検索需要・ハッシュタグの数値**：Google Trends の実測値は時期で大きく動く。
  企画のたびに取り直す。
- **ジャンル実測（n=293で有意なのは異常性ワードのみ）**：密着ジャンル全体の傾向であって、
  個別チャンネルで必ず再現するとは限らない。

## つまずきどころ（先に読むと早い）

- **yt-dlp が 403**：`--cookies-from-browser chrome` で通る。
  `player_client` 切替では360pしか取れないので使わない。
- **ffmpeg に drawtext / subtitles / ass フィルタが無い環境がある**：
  文字は PIL で PNG 化して overlay する設計になっている（`banner.py` / `mkbase.py`）。
- **xfade は両入力に `settb=1001/30000` が要る**：timebase 不一致でエラーになる。
- **LRA が大きい素材（>11）は一発 loudnorm が外れる**：ebur128 で実測し、
  ±1.5dB を超えたら `measured_*` 付きの2パスで正規化し直す。
- **フェードインするテロップは区間の中央フレームだけでは取り逃すことがある**（telops.txt で
  空白行や誤った開始フレームになる）。第3段階以降は区間先頭フレームも自動でOCRし、中央と
  食い違えば真の開始フレームを二分探索で補正する（`kirinuki_core.telop_scan.refine_segment_start`）。
  それでも空行率が高い・帯位置がずれた警告が出たら、±数秒の実フレームを `ocrcli` で
  直接OCRして実体を確認する。
- **横クロップ禁止**（`taidan-kirinuki`）：元テロップは画面の左右いっぱいまで使われている。

各スキルの詳細は `mitchaku-kirinuki/SKILL.md`、`taidan-kirinuki/SKILL.md`
（＋ `taidan-kirinuki/references/rules.md`, `layout.md`）を読むこと。
