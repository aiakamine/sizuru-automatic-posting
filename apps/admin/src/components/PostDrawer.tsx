"use client";

import { useId, useState } from "react";
import { ApiError, cancelPost, patchPost, type PatchPostBody } from "@/lib/api";
import { evaluateSave, isCancelable, isEditableStatus } from "@/lib/postEditRules";
import { fromDatetimeLocalValue, toDatetimeLocalValue, toDueAtIsoString } from "@/lib/week";
import { captionLength, captionLimit } from "@/lib/xWeightedLength";
import type { PostActionResult, PostWithRelations } from "@/lib/types";
import { PlatformMark } from "./PlatformMark";
import { StatusPill } from "./StatusPill";
import { VideoThumbnail } from "./VideoThumbnail";
import styles from "./PostDrawer.module.css";

const SERVICE_LABELS: Record<PostWithRelations["channels"]["service"], string> = {
  tiktok: "TikTok",
  instagram: "Instagram",
  x: "X",
};

interface ErrorDescription {
  message: string;
  /** "warn": Buffer 側は成功しているなど、致命的ではないが対応が必要な状態。 */
  tone: "err" | "warn";
}

function buildErrorDescription(err: unknown): ErrorDescription {
  if (err instanceof ApiError) {
    if (err.code === "ambiguous") {
      return { message: `${err.message}（Buffer の管理画面もご確認ください）`, tone: "err" };
    }
    if (err.code === "invalid_state") {
      return { message: `${err.message}（画面を再読み込みしてください）`, tone: "err" };
    }
    if (err.code === "db_write_failed") {
      // Buffer への反映自体は成功しているため、致命的なエラーではなく「同期して確認」を促す表示にする。
      return { message: err.message, tone: "warn" };
    }
    if (err.code === "internal") {
      // 500 の本文（スタックトレース等）をそのまま画面に出さない。固定の日本語メッセージにする。
      return { message: "サーバーでエラーが発生しました。時間をおいて再度お試しください", tone: "err" };
    }
    return { message: err.message, tone: "err" };
  }
  return {
    message: "通信エラーが発生しました。しばらくしてからもう一度お試しください。",
    tone: "err",
  };
}

/** Buffer への反映は成功したが DB 記録に失敗した場合、週・帯を再取得して確認を促すべきかどうか。 */
function requiresResync(err: unknown): boolean {
  return err instanceof ApiError && err.code === "db_write_failed";
}

/**
 * 週ボード右側（1080px 未満では下シート）の編集パネル。
 * 日時・投稿文の編集と、確認を挟んだ予約キャンセルを担当する。
 * 保存可否・キャンセル可否の判定ロジックは `@/lib/postEditRules` に切り出し、ここでは呼ぶだけにする。
 *
 * 呼び出し側は必ず `<PostDrawer key={post.id} .../>` と `post.id` を key に使うこと。
 * key が変わらないまま `post` だけ差し替えると、このコンポーネント内の編集中の入力や
 * キャンセル確認ダイアログの状態が前の投稿のまま残ってしまう。
 */
export function PostDrawer({
  post,
  accessToken,
  now,
  onClose,
  onSaved,
  onCanceled,
  onRequireResync,
}: {
  post: PostWithRelations;
  accessToken: string;
  now: Date;
  onClose: () => void;
  onSaved: (result: PostActionResult) => void;
  onCanceled: (result: PostActionResult) => void;
  /**
   * Buffer への反映は成功したが DB 記録に失敗した（db_write_failed）ときに呼ばれる。
   * 呼び出し側は週・帯を再取得して最新状態を見せる（このコンポーネント自身の投稿データは
   * 更新されていない可能性があるため、確定した状態は再取得でしか分からない）。
   */
  onRequireResync?: () => void;
}) {
  const captionFieldId = useId();
  const dueAtFieldId = useId();

  const [caption, setCaption] = useState(post.caption);
  const [dueAtLocalValue, setDueAtLocalValue] = useState(
    post.due_at ? toDatetimeLocalValue(new Date(post.due_at)) : "",
  );
  // datetime-local は秒以下を保持できないため、「変更されたか」は値の比較ではなく
  // 実際にユーザーが触ったかどうかで判定する（postEditRules.ts のコメントも参照）。
  const [dueAtTouched, setDueAtTouched] = useState(false);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  // 保存とキャンセルは同時に走らせない。片方が進行中はどちらのボタンも押せなくする。
  const [mutating, setMutating] = useState(false);
  const [errorDescription, setErrorDescription] = useState<ErrorDescription | null>(null);

  const status = post.status;
  const editable = isEditableStatus(status);
  const service = post.channels.service;
  const originalDueAt = post.due_at ? new Date(post.due_at) : null;
  const captionChanged = caption !== post.caption;

  let nextDueAt: Date | null = null;
  let dueAtParseError = false;
  try {
    nextDueAt = dueAtLocalValue ? fromDatetimeLocalValue(dueAtLocalValue) : null;
  } catch {
    dueAtParseError = true;
    nextDueAt = null;
  }

  const evaluation =
    nextDueAt && !dueAtParseError
      ? evaluateSave({
          status,
          service,
          captionChanged,
          dueAtChanged: dueAtTouched,
          nextCaption: caption,
          originalDueAt,
          nextDueAt,
          now,
        })
      : { canSave: false, reasons: (dueAtParseError ? ["invalid_date"] : []) as ReturnType<typeof evaluateSave>["reasons"] };

  const captionLen = captionLength(service, caption);
  const captionLimitValue = captionLimit(service);
  const overLimit = captionLen > captionLimitValue;
  const cancelable = isCancelable(status, originalDueAt, now);

  async function handleSave() {
    if (mutating || !nextDueAt || !evaluation.canSave) return;

    // now プロップの更新が間に合っていない可能性があるため、送信直前に実時刻で再判定する。
    const freshNow = new Date();
    const freshEvaluation = evaluateSave({
      status,
      service,
      captionChanged,
      dueAtChanged: dueAtTouched,
      nextCaption: caption,
      originalDueAt,
      nextDueAt,
      now: freshNow,
    });
    if (!freshEvaluation.canSave) {
      setErrorDescription({
        message: freshEvaluation.reasons.includes("near_due_at")
          ? "まもなく送信されるため、編集できませんでした。画面を更新してください。"
          : "保存できませんでした。内容を確認してください。",
        tone: "err",
      });
      return;
    }

    const body: PatchPostBody = {};
    if (captionChanged) {
      body.caption = caption;
    }
    if (dueAtTouched) {
      body.due_at = toDueAtIsoString(nextDueAt);
    }
    setMutating(true);
    setErrorDescription(null);
    try {
      const response = await patchPost(post.id, body, accessToken);
      onSaved(response.result);
    } catch (err) {
      setErrorDescription(buildErrorDescription(err));
      if (requiresResync(err)) onRequireResync?.();
    } finally {
      setMutating(false);
    }
  }

  async function handleConfirmCancel() {
    if (mutating) return;

    // 確認ダイアログを開いてから実際に送信するまでの間に予約時刻へ近づいている可能性があるため、
    // 送信直前に実時刻で再判定する。
    const freshNow = new Date();
    if (!isCancelable(status, originalDueAt, freshNow)) {
      setConfirmingCancel(false);
      setErrorDescription({
        message: "予約時刻が近いため、キャンセルできませんでした。画面を更新してください。",
        tone: "err",
      });
      return;
    }

    setMutating(true);
    setErrorDescription(null);
    try {
      const response = await cancelPost(post.id, accessToken);
      setConfirmingCancel(false);
      onCanceled(response.result);
    } catch (err) {
      setErrorDescription(buildErrorDescription(err));
      if (requiresResync(err)) onRequireResync?.();
    } finally {
      setMutating(false);
    }
  }

  return (
    <div className={styles.drawer}>
      <div className={styles.header}>
        <div className={styles.headerInfo}>
          <VideoThumbnail
            thumbnailUrl={post.submissions.thumbnail_url}
            durationSec={post.submissions.duration_sec}
            alt={post.submissions.title ?? "動画"}
          />
          <div>
            <h3>{post.submissions.title ?? "(タイトル未設定)"}</h3>
            <div className={styles.headerMeta}>
              <PlatformMark service={service} size="sm" />
              <span>{SERVICE_LABELS[service]}</span>
              <StatusPill status={status} />
            </div>
          </div>
        </div>
        <button type="button" className={styles.closeButton} aria-label="閉じる" onClick={onClose}>
          ×
        </button>
      </div>

      <div className={styles.form}>
        {status === "pending" && (
          <p className={styles.note}>送信結果を確認中です。更新ボタンで同期してください。</p>
        )}
        {status === "sent" && (
          <p className={styles.note}>
            公開済みの投稿は日時・投稿文を変更できません。修正は {SERVICE_LABELS[service]} のアプリで行ってください。
            {post.external_link ? (
              <>
                {" "}
                <a href={post.external_link} target="_blank" rel="noreferrer">
                  投稿を見る
                </a>
              </>
            ) : null}
          </p>
        )}
        {status === "canceled" && <p className={styles.note}>この予約はキャンセル済みです。</p>}
        {status === "failed" && (
          <p className={styles.note} data-tone="err">
            {post.error_message ?? "投稿に失敗しました"}
          </p>
        )}

        <div className={styles.field}>
          <label htmlFor={dueAtFieldId}>投稿日時</label>
          <input
            id={dueAtFieldId}
            type="datetime-local"
            step={300}
            value={dueAtLocalValue}
            disabled={!editable}
            onChange={(event) => {
              setDueAtLocalValue(event.target.value);
              setDueAtTouched(true);
            }}
          />
        </div>

        <div className={styles.field}>
          <label htmlFor={captionFieldId}>投稿文</label>
          <textarea
            id={captionFieldId}
            value={caption}
            disabled={!editable}
            onChange={(event) => setCaption(event.target.value)}
          />
          <span className={styles.counter} data-over={overLimit}>
            {captionLen} / {captionLimitValue}
            {service === "x" ? "（全角は2で計算）" : ""}
          </span>
        </div>

        {editable && evaluation.reasons.includes("near_due_at") && (
          <p className={styles.note}>まもなく送信されるため、編集・キャンセルはできません。</p>
        )}
        {editable && evaluation.reasons.includes("past_time") && (
          <p className={styles.note} data-tone="err">
            過去の日時は指定できません。今より後の日時を選んでください。
          </p>
        )}

        {errorDescription && (
          <p className={styles.note} data-tone={errorDescription.tone}>
            {errorDescription.message}
          </p>
        )}

        {confirmingCancel && (
          <div className={styles.confirm} role="alertdialog" aria-label="予約キャンセルの確認">
            <p>
              <b>{SERVICE_LABELS[service]}</b> の予約をキャンセルします。Buffer のキューからも削除され、元に戻せません。
            </p>
            <div className={styles.actions}>
              <button
                type="button"
                className={styles.btn}
                data-variant="danger"
                disabled={mutating}
                onClick={handleConfirmCancel}
              >
                キャンセルする
              </button>
              <button
                type="button"
                className={styles.btn}
                disabled={mutating}
                onClick={() => setConfirmingCancel(false)}
              >
                戻る
              </button>
            </div>
          </div>
        )}

        {editable && (
          <div className={styles.actions}>
            <button
              type="button"
              className={styles.btn}
              data-variant="primary"
              disabled={!evaluation.canSave || mutating}
              onClick={handleSave}
            >
              変更を保存
            </button>
            <span className={styles.spacer} />
            {!confirmingCancel && (
              <button
                type="button"
                className={styles.btn}
                data-variant="danger"
                disabled={!cancelable || mutating}
                onClick={() => setConfirmingCancel(true)}
              >
                予約をキャンセル
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
