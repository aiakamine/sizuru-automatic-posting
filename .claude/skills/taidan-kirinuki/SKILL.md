---
name: taidan-kirinuki
description: 対談・解説・実演系（複数人トーク、製品の使い方解説、画面共有つき実演）の本編動画からYouTubeショート用の切り抜きを複数本制作するスキル。元データの焼き込みテロップの切替をフレーム単位で検出しVision OCRで一覧化したうえで、テロップ表示がずれないカット・フックから入る構成・ハイライト→本編の白フラッシュ・シリーズ実測レイアウト（バナー＋映像バンド＋CTA）で書き出し、OCRとラウドネスでQCして編集指示と投稿タイトル案まで納品する。「この対談回から切り抜き作って」「解説動画からショート◯本」等の依頼で使用する。密着動画は mitchaku-kirinuki を使う。
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: 'for d in "$CLAUDE_PROJECT_DIR/.claude/skills/taidan-kirinuki" "$HOME/.claude/skills/taidan-kirinuki"; do if [ -f "$d/hooks/gate_hearing.py" ]; then exec python3 "$d/hooks/gate_hearing.py"; fi; done; echo "{\"systemMessage\": \"taidan-kirinuki のフック（hooks/gate_hearing.py）が見つからないため、この呼び出しは検査していません。install.sh を実行してください。\"}"; exit 0'
    - matcher: "Write|Edit"
      hooks:
        - type: command
          command: 'for d in "$CLAUDE_PROJECT_DIR/.claude/skills/taidan-kirinuki" "$HOME/.claude/skills/taidan-kirinuki"; do if [ -f "$d/hooks/gate_improve_write.py" ]; then exec python3 "$d/hooks/gate_improve_write.py"; fi; done; echo "{\"systemMessage\": \"taidan-kirinuki のフック（hooks/gate_improve_write.py）が見つからないため、この呼び出しは検査していません。install.sh を実行してください。\"}"; exit 0'
  Stop:
    - hooks:
        - type: command
          command: 'for d in "$CLAUDE_PROJECT_DIR/.claude/skills/taidan-kirinuki" "$HOME/.claude/skills/taidan-kirinuki"; do if [ -f "$d/hooks/stop_improve.py" ]; then exec python3 "$d/hooks/stop_improve.py"; fi; done; echo "{\"systemMessage\": \"taidan-kirinuki のフック（hooks/stop_improve.py）が見つからないため、この終了は検査していません。install.sh を実行してください。\"}"; exit 0'
---

# 対談・解説系 本編 → ショート切り抜き制作

本編のURL（または動画ファイル）から、ショート切り抜きを指定本数（既定6本）制作する。
回答・成果物は全て日本語。実制作は ffmpeg＋`scripts/` のツールで行い、
**シーン選定・バナー文言・タイトルはClaudeが判断する**。

## 絶対ルール（この4つは必ず守る）

1. **カットの繋ぎによるテロップの表示ずれをなくす**
   元テロップをそのまま使う。カット頭＝新テロップの最初のフレーム、
   カット尻＝次のテロップに変わり始める1つ前のフレーム。`qc.py` で全カット検査する。
   映像と音声のズレも同様に禁止（trim/atrim＋concatフィルタで1コマンドに通す）。
2. **冒頭のフックを意識したシーン選定**
   C1は「いちばん強い一言・画」から入る。結果画面の実況、文脈前提の出だし（「2つ目が」）、
   静止画、主張だけで画が無いカットは使わない。
3. **ハイライト→本編はトランジションを使用する**
   C1→C2は白フラッシュ0.35秒。元データの章アイキャッチを使う回だけ例外（`xfade: 0.0`）。
4. **文脈がつながらないシーン繋ぎ・終わり方はしない**
   テロップの区切り＝文の区切りではない。語尾が欠ける／次の話題の頭が残るカットを作らない。

詳細と判定方法は `references/rules.md` を必ず読む。

## 毎回の進め方（フックで強制される）

```
0. ヒアリング（グリルミー） ─┐ 並行
1〜2. 素材・レイアウト取得、テロップ表 ─┘
3〜6. 制作（ヒアリング確定まで、バナー生成・書き出し・納品物生成はフックが止める）
6.5. 完成動画アーティファクト公開
7. FB → 改善フェーズ（終わるまで、終了はフックが差し戻す）
```

- Bashは呼び出しごとにシェルが変わる。**`S=` と `D=` は毎回同じコマンド内で定義する**
  （フックがコマンドの `render.py <src> <workdir>` / `gen_docs.py <workdir>` から作業フォルダを読むため）

### 0. ヒアリング（必須）

`references/hearing.md` の手順で、1問ずつ深掘りして前提を固める。**素材の取得とテロップ走査は待たずに裏で走らせる。**

- 先に `references/learnings.md` を読み、過去のFBから推奨回答を作る
- `templates/hearing.md` を `$D/hearing.md` にコピーし、決まった項目から埋める
  （本編URL・本数（既定6本）・公開チャンネル・目的とCTA・納品先を含む）
- telops.txt ができたら候補シーンを提示して選んでもらう（これもヒアリングの一部）
- 全項目が埋まったら要約を見せて確認を取り、`状態: 確定` にする
- **`状態: 確定` でない間、`banner.py` `render.py` `gen_docs.py` の実行は gate_hearing フックが拒否する**
  （記録の先頭の `skill:` 行が持ち主。素材取得・テロップ走査・safe/pick/chop・qc は止めない）

## フロー

### 1. 素材とレイアウトの取得

```bash
S="$CLAUDE_PROJECT_DIR/.claude/skills/taidan-kirinuki/scripts"
D=<スクラッチパッド>/kirinuki

# 本編。403が出たら --cookies-from-browser chrome で通る（player_client切替は360pしか取れない）
yt-dlp --cookies-from-browser chrome -f "bv*[height<=1080]+ba/b" --merge-output-format mp4 \
       -o $D/source.mp4 <本編URL>
yt-dlp --cookies-from-browser chrome --skip-download --write-auto-subs --sub-langs ja \
       -o $D/cap <本編URL>          # 文脈把握用。カット点には使わない

# 完成版のショートを1本落としてレイアウトとCTAを取る（デザイン変更の検知も兼ねる）
yt-dlp --cookies-from-browser chrome -f "bv*+ba/b" --merge-output-format mp4 -o $D/ref.mp4 <公開済みショートURL>
python3 $S/measure_layout.py $D/ref.mp4
python3 $S/extract_cta.py    $D/ref.mp4 $D/cta.mp4
```

実測値が `references/layout.md` と違ったら、`clips.py` の LAYOUT と `scripts/banner.py` の
定数を合わせてから進む。

### 2. テロップ表の作成（カットの唯一の座標系）

```bash
swiftc -O -o $D/ocrcli $S/ocr.swift
python3 $S/subscan.py $D/source.mp4 $D                      # 全フレーム走査。約3〜5分
python3 $S/subtext.py $D/source.mp4 $D $D/ocrcli --from 200 --to 1900
```

`$D/telops.txt` が「番号／開始-終了／本文」の一覧。**カット点は必ずこの表の境界に置く**。

### 3. シーン選定と構成（Claudeの仕事）

- 本編の章立てを掴む（自動字幕＋telops.txt）。実演パートは1本ずつに割るのが基本
- 検索需要の枠を1本入れる。製品を扱う回なら**指名検索が大きい製品名**の回を1本立てる
  （実測例：2026年時点のGoogle Trendsで Claude Cowork=100 に対し Claude Code≒920。
  数値は時期で大きく動くので、企画のたびに取り直す）
- 各本の構成：C1フック（5〜10秒）→白フラッシュ→本編（合計53〜63秒目安）
- フック候補は「数字・断定」「成果物そのもの」「視聴者の疑問の代弁」から選ぶ。
  静止画になっていないか、フレーム間差分で確認する：
  ```bash
  ffmpeg -v error -ss <t> -t 20 -i $D/source.mp4 -vf "crop=...,scale=120:66,format=gray" -f rawvideo - | \
    python3 -c "..."   # 差分が0.00の区間は動いていない＝フックに使わない
  ```

### 4. カット定義を書く

`templates/clips_template.py` をコピーして `$D/clips.py` を作る。
`src` は telops.txt の境界の値をそのまま入れる。バナーは「製品名で／結果」の2行。

語尾（言い切り／引用止め／継続形）は人が本文を読んで最終判断するが、まず機械の候補表で
当たりを付ける：

```bash
python3 $S/cutpoints.py $D/source.mp4 $D                # → 語尾分類・無音判定の候補表
```

```bash
python3 $S/safe.py $D/source.mp4                       # → safe.txt（切っていい点の一覧）
python3 $S/pick.py $D/source.mp4 <開始秒> <終了秒>       # 区間ごとに候補を見る
```

**カット点は safe.txt の SAFE 行からしか選ばない。** テロップの境界であっても無音でなければ
語尾が切れる。テロップが出ていない区間（作業画面・BGMだけ）はどこで切ってもよい。
書いたら必ず：

```bash
python3 $S/chop.py $D/source.mp4 $D $D/clips.py        # ぶつ切りが0件になるまで直す
python3 $S/align_check.py $D/source.mp4 $D $D/clips.py # 発話とテロップの一致検査。NG 0件になるまで直す
```

ここが品質のほぼ全て。この段階の流れは
**`cutpoints.py` で候補表 → `safe.py`/`pick.py` で点を選ぶ → `chop.py` 0件 → `align_check.py` NG 0件**。
`src` を直すたびに`chop.py`・`align_check.py`を両方やり直す。

### 5. 書き出しと検査

```bash
python3 $S/render.py $D/source.mp4 $D $D/clips.py --cta $D/cta.mp4
python3 $S/qc.py   $D $D/clips.py $D/ocrcli
python3 $S/chop.py $D/source.mp4 $D $D/clips.py
python3 $S/align_check.py $D/source.mp4 $D $D/clips.py
for f in $D/out/*.mp4; do
  ffprobe -v error -select_streams v -show_entries stream=duration -of csv=p=0 "$f"
  ffprobe -v error -select_streams a -show_entries stream=duration -of csv=p=0 "$f"
  ffmpeg -nostats -i "$f" -filter_complex ebur128 -f null - 2>&1 | grep -E "^ *I: " | tail -1
done
```

合格ライン：qc.py の不一致がOCRの認識ゆれのみ／`align_check.py` のNGが0件／
映像と音声のdurationの差が1フレーム以内／尺53〜63秒／
ラウドネスは-15〜-18 LUFS（BGM前なのでこれでよい。本編集で-14へ）。

### 6. 納品物

```bash
python3 $S/gen_docs.py $D $D/clips.py <納品ディレクトリ>
cp $D/out/*.mp4 <納品ディレクトリ>/
```

- 切り抜き本数分のmp4
- `編集指示/`（カットリスト＝元データのタイムコード、各カットの元テロップ全文、本編集の申し送り）
- `投稿タイトル案.txt`（下記）
- `00_はじめに.txt`（選定理由・仕様・未実施事項）

### 6.5 完成動画アーティファクト

納品物ができたら、動画を見られるページ（アーティファクト）を作って共有する。

```bash
python3 "$CLAUDE_PROJECT_DIR/.claude/skills/sns-schedule/scripts/schedule.py" preview \
  "<納品ディレクトリの絶対パス>" --out "$D/preview"
```

- **20MiB上限に対し19MiBで判定（余裕1MiB）**。19MiB以下のクリップはそのまま、超えるものは
  幅720px（高さは自動でアスペクト比を維持）・crf28（それでも超えたらcrf32）で再エンコードして
  `$D/preview/` に並べる。**`$D/out/` には書かない**（Stopフックの再発火防止）
- 各本のデータ（投稿タイトル・`投稿タイトル案.txt`の代替案と根拠・尺・サイズ）に加え、
  トップレベルの `main_video_url` に `$D/hearing.md` の `## 本編URL` 節の値を入れたJSONを作る。
  `gallery` コマンドでHTMLを生成する:

  ```bash
  python3 "$CLAUDE_PROJECT_DIR/.claude/skills/sns-schedule/scripts/schedule.py" gallery \
    --data "$D/gallery_data.json" --out "$D/preview/page.html"
  ```

- Artifact toolでこのHTMLを公開する（`capabilities: {"assets": {}}`、アイコンは`video`）。
  公開後、`upload_asset`アクションで `$D/preview/` の各mp4を添付する（1回の呼び出しにつき最大25本）。
  返ってきたURLを各クリップの `video_url` に設定してデータJSONを作り直し、`gallery` で再生成して
  **同じファイル**へ再公開する
- 公開したURLを `{"url": "..."}` の形で `$D/artifact.json` に保存する（後で予約案を追記する際に使う）
- ユーザーにリンクを渡す。**このページは組織内限定（organization-internal）**であることを伝える

### 7. FB と改善フェーズ（毎回必須）

納品したら終わりにしない。`DREAM.md` の手順で実施する。

- 各本のFB（使えるか・良かった点／悪かった点とその理由）をユーザーに聞く
- FBが終わったら、改善提案に入る前に `DREAM.md` の **1.5 投稿予約（任意）** に進む
  （使える本があれば予約投稿するかどうかを確認する）
- 今回のFB・詰まった点・QC指摘と `references/learnings.md` の過去記録から改善提案を作る
- 提案ごとに対象ファイル・削除する内容・追加する内容・根拠を見せ、**承認を得たものだけ**反映する
- `templates/improve.md` を `$D/improve.md` にコピーして記録し、`状態: 完了` にする。learnings.md にも追記する
- **`$D/out/` に mp4 があるのに improve.md が完了していなければ、stop_improve フックが終了を差し戻す**
  （注意は mp4 の顔ぶれが前回から変わったときだけ。1回の終了につき1回で、続けて終えれば通る）

## 投稿タイトルとバナーの書き分け

- **バナー（動画内）＝製品名が先**。「Claude Coworkで／◯◯」
- **投稿タイトル＝タスク名が先**。「◯◯｜Claude Cowork」
- 先頭を入れ替えて、同じ動画から違う語を拾う。バナーで使った語はタイトルに入れない
- 先頭20文字は「得すること・損しなくなること」に使う（説明にしない）。
  製品名は後半でよい。フィードで切れるのは表示だけで、検索の照合はタイトル全文に対して行われる
- 検索の受け皿は**一般名詞のタスク名**に置く（フォルダ整理／ゲーム開発／日程調整／
  スライド作成／リサーチ／使い分け）。製品名＋「使い方」等の複合語は実測でほぼ検索されない
- ハッシュタグは、扱う製品名より検索の大きい上位概念を1つ足す
  （実測例：AI系では「AIエージェント」がClaude Coworkの2.27倍・上昇中で競合の使用なし）
- 各本に主案＋代替2案＋ハッシュタグ＋説明欄1行目の案を付ける

## 本編集への申し送り（毎回入れる）

1. BGM（納品時は元データの音のみ。BGM後に-14 LUFS前後へ）
2. バナーは公開版デザインの再現であって正規テンプレそのものではない
3. 元データ左上のチャプター表示の映り込み（編集データ側でレイヤーをオフにすれば消える）
4. 個人情報のマスク確認（過去にドライブのファイル名が未モザイクだった事例あり）

## つまずきどころ

- **ffmpegに drawtext / subtitles / ass フィルタが無い環境がある**。文字はPILでPNG化して
  overlayする（banner.py がそう）。テロップは元データのものを使うので原則これで足りる
- **横クロップは禁止**。元テロップは最長 x=113〜1793 まで使う。寄せていいのは無テロップ区間だけ
- **音声RMSの局所中央値方式（pauses.py）はBGMが大きい場面で過検知する**。最終判定は
  絶対レベル（chop.py／-26dBFS）で行う。最後は必ずテロップ本文を読んで文の切れ目か確かめる
- **render.py / qc.py の対象を絞るときは `--only slug` を使う**（位置引数だとargparseが弾く）
- **subtext.pyのフェードイン補正はtelops.txt/segs.jsonにしか反映されない**。`bounds.json`
  （`safe.py`/`pick.py`の候補、`render.py`等のgstartスナップの元）は補正前のフレームのまま。
  `subtext.py`実行時に「補正: セグメントN の開始フレームを...」がstderrに出たら、
  その区間の頭を`safe.txt`から選ぶ際に`align_check.py`のNGで気づくことがある。
  NGが出たら該当区間がフェードイン補正されていないか確認する
