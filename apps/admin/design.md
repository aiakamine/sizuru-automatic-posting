# Sizuru 管理画面 デザイントークン

出典: `sizuru-admin-ui.html` モック内の「パターン B（週カレンダー型・採用案）」。
このアプリ（`apps/admin/`）のスタイルはすべてここに定義した CSS カスタムプロパティ経由で参照する。
コンポーネント内でのアドホックな hex / px 直書きは禁止。新しいトークンが必要になったら、先にこのファイルへ追記してから使う。

トークンは `src/app/globals.css` の `:root` / `[data-theme="dark"]` / `prefers-color-scheme: dark` に実装する。

## カラー（ライト）

| トークン | 値 | 用途 |
|---|---|---|
| `--bg` | `#F4F6FA` | ページ背景 |
| `--surface` | `#FFFFFF` | カード・サイドバー・入力欄の背景 |
| `--surface-2` | `#F8FAFD` | カード内のセクション背景（薄い区切り） |
| `--line` | `#E4E8EF` | 罫線・ボーダー |
| `--text` | `#1B2330` | 本文文字色 |
| `--muted` | `#6A7486` | 補足文字色 |
| `--faint` | `#9AA3B2` | さらに薄い文字色（プレースホルダ等） |
| `--primary` | `#1D6FE8` | 主要アクション・選択状態 |
| `--primary-ink` | `#1559C0` | primary 系の濃い文字色 |
| `--primary-soft` | `#E9F1FE` | primary 系の淡い背景 |
| `--ok` | `#15803D` | 成功・投稿済み |
| `--ok-soft` | `#E6F5EB` | 成功系の淡い背景 |
| `--err` | `#D23B30` | エラー |
| `--err-soft` | `#FDECEA` | エラー系の淡い背景 |
| `--draft` | `#5B6678` | 下書き |
| `--draft-soft` | `#EEF1F5` | 下書き系の淡い背景 |
| `--cancel` | `#8A93A2` | キャンセル済み |
| `--cancel-soft` | `#F1F3F6` | キャンセル系の淡い背景 |
| `--shadow` | `0 1px 2px rgba(20,32,56,.05), 0 4px 16px rgba(20,32,56,.05)` | カード等の影 |
| `--overlay` | `rgba(15,23,36,.38)` | モーダル背景（確認ダイアログ等） |

## カラー（ダーク）

`prefers-color-scheme: dark` かつ `[data-theme]` 未指定、または `[data-theme="dark"]` のとき適用。

| トークン | 値 |
|---|---|
| `--bg` | `#0D121A` |
| `--surface` | `#141B25` |
| `--surface-2` | `#18212D` |
| `--line` | `#263142` |
| `--text` | `#E5EAF1` |
| `--muted` | `#98A3B4` |
| `--faint` | `#6D788A` |
| `--primary` | `#5A9BFF` |
| `--primary-ink` | `#8DBBFF` |
| `--primary-soft` | `#16273F` |
| `--ok` | `#5BCB85` |
| `--ok-soft` | `#14301F` |
| `--err` | `#F08378` |
| `--err-soft` | `#3A1D1B` |
| `--draft` | `#A5B0C0` |
| `--draft-soft` | `#1F2835` |
| `--cancel` | `#7D8798` |
| `--cancel-soft` | `#1B222D` |
| `--shadow` | `0 1px 2px rgba(0,0,0,.3)` |
| `--overlay` | `rgba(0,0,0,.55)` |

`[data-theme="light"]` は常にライトのトークンを強制する（`prefers-color-scheme` を上書き）。

## タイポグラフィ

- 本文フォント: `"Noto Sans JP", "Hiragino Sans", "Yu Gothic", system-ui, sans-serif`（ウェイト 400/500/700/900）
- 数値フォント（`.num` クラス — 時刻・件数・文字数カウンターなど数字を含む箇所すべて）: `"Roboto Mono", ui-monospace, monospace`、`font-variant-numeric: tabular-nums`（ウェイト 500）
- ベース: `font-size: 14px`, `line-height: 1.6`
- 見出し: ページタイトル `h1`/`h2` は `font-weight: 900`。`h2` は `22px`。カードの `h3` は `15px` / `700`。

## スペーシング・角丸・枠線（トークン化した数値）

| トークン | 値 | 用途 |
|---|---|---|
| `--radius-sm` | `8px` | ボタン・入力欄・サムネ（通常サイズ） |
| `--radius-md` | `10px` | slot カード・サムネ（拡大サイズ） |
| `--radius-lg` | `12px` | カード・週ボード全体 |
| `--radius-xl` | `16px` | 外枠（frame 相当。管理画面本体では未使用だが将来用に予約） |
| `--radius-pill` | `999px` | ステータスピル |
| `--space-1` `--space-2` `--space-3` `--space-4` `--space-5` `--space-6` `--space-7` | `4px 6px 8px 10px 12px 16px 20px` | 汎用余白スケール |
| `--border` | `1px solid var(--line)` | カード・入力欄の枠線 |

## ステータス色（`status: pending`/`draft`/`scheduled`/`sent`/`failed`/`canceled`）

| status | 文字色 | 背景 | ラベル |
|---|---|---|---|
| `pending` | `--muted` | `--surface-2` | 確認中（Buffer への送信結果をまだ確認できていない内部状態。編集不可） |
| `scheduled` | `--primary-ink` | `--primary-soft` | 予約済み |
| `sent` | `--ok` | `--ok-soft` | 投稿済み |
| `draft` | `--draft` | `--draft-soft` | 下書き |
| `failed` | `--err` | `--err-soft` | エラー |
| `canceled` | `--cancel` | `--cancel-soft` | キャンセル済み |

`pending` はパターン B のモックには無かった状態だが、実装レビューで「同期前のごく短い間、週カレンダーや PostDrawer に現れうる」ことが判明したため追加した。新しい色トークンは起こさず、既存の `--muted` / `--surface-2`（本文の補足色・カードの薄い背景）を流用する。

## プラットフォームマーク（`service: tiktok`/`instagram`/`x`）

- 共通: 白アイコン、角丸 `7px`（小サイズ `5px`）、サイズ `26x26`（小 `20x20`）
- `tiktok`: 背景 `#111`
- `x`: 背景 `#111`
- `instagram`: 背景 `radial-gradient(circle at 30% 107%, #FDD55A 0%, #F7713A 35%, #D6249F 62%, #6A3EDB 100%)`

## サムネイル

- アスペクト比 `9 / 16`（縦動画）。通常サイズ幅 `40px` 角丸 `--radius-sm`、拡大サイズ（帯 upcoming strip）幅 `112px` 角丸 `--radius-md`
- `thumbnail_url` が無い場合のフォールバック: グラデーション背景（`linear-gradient(160deg, var(--primary), var(--primary-ink))` を既定値として使用）
- 尺（`duration_sec`）バッジ: 右下に `mm:ss`、`Roboto Mono`、白文字、半透明黒背景

## ブレークポイント

- `1080px` 以下: 右パネル（PostDrawer）はサイドバー形式ではなく下シート（bottom sheet）表示に切り替える
- `760px` 以下: サイドバーは横並び、週ボードの日カラムは縦積みに切り替える

## 適用範囲

これらのトークンは `apps/admin/` 配下にのみ適用する。リポジトリ直下や他アプリ・共有パッケージにこのトークンを持ち出さない。
