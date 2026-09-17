import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PostWithRelations } from "@/lib/types";

const patchPostMock = vi.fn();
const cancelPostMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    patchPost: patchPostMock,
    cancelPost: cancelPostMock,
  };
});

const { PostDrawer } = await import("./PostDrawer");

const NOW = new Date("2026-09-16T06:00:00+09:00");

function buildPost(overrides: Partial<PostWithRelations> = {}): PostWithRelations {
  return {
    id: "p1",
    submission_id: "s1",
    channel_id: "c1",
    caption: "起業1年目、300万円溶かしました。",
    mode: "customScheduled",
    due_at: "2026-09-16T19:00:00+09:00",
    is_draft: false,
    buffer_post_id: "b1",
    status: "scheduled",
    external_link: null,
    sent_at: null,
    error_message: null,
    retry_count: 0,
    created_at: "2026-09-15T00:00:00+09:00",
    updated_at: "2026-09-15T00:00:00+09:00",
    submissions: {
      id: "s1",
      title: "起業1年目で失敗した話",
      video_url: "https://example.com/v.mp4",
      thumbnail_url: null,
      duration_sec: 58,
      approved_at: "2026-09-13T21:04:00+09:00",
      created_at: "2026-09-13T21:00:00+09:00",
    },
    channels: {
      id: "c1",
      service: "tiktok",
      buffer_channel_id: "buf-1",
      name: "sizuru_clips",
      display_name: "@sizuru_clips",
      is_disconnected: false,
      is_queue_paused: false,
    },
    ...overrides,
  };
}

function renderDrawer(overrides: Partial<PostWithRelations> = {}, extraProps: Partial<React.ComponentProps<typeof PostDrawer>> = {}) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  const onCanceled = vi.fn();
  const utils = render(
    <PostDrawer
      post={buildPost(overrides)}
      accessToken="token-abc"
      now={NOW}
      onClose={onClose}
      onSaved={onSaved}
      onCanceled={onCanceled}
      {...extraProps}
    />,
  );
  return { ...utils, onClose, onSaved, onCanceled };
}

describe("PostDrawer disabled states", () => {
  beforeEach(() => {
    patchPostMock.mockReset();
    cancelPostMock.mockReset();
  });

  it("disables Save when nothing has changed", () => {
    renderDrawer();
    expect(screen.getByRole("button", { name: "変更を保存" })).toBeDisabled();
  });

  it("enables Save once the caption is edited", async () => {
    renderDrawer();
    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "新しい投稿文");
    expect(screen.getByRole("button", { name: "変更を保存" })).toBeEnabled();
  });

  it("disables Save and shows the over-limit counter when the caption exceeds the service limit", async () => {
    renderDrawer();
    const textarea = screen.getByLabelText("投稿文");
    fireEvent.change(textarea, { target: { value: "あ".repeat(2201) } });

    expect(screen.getByRole("button", { name: "変更を保存" })).toBeDisabled();
    expect(screen.getByText("2201 / 2200")).toBeInTheDocument();
  });

  it("disables Save when the edited due date is in the past", () => {
    renderDrawer();
    const input = screen.getByLabelText("投稿日時");
    fireEvent.change(input, { target: { value: "2020-01-01T00:00" } });

    expect(screen.getByRole("button", { name: "変更を保存" })).toBeDisabled();
  });

  it("disables Save and Cancel within 5 minutes of the current due_at", () => {
    renderDrawer({ due_at: "2026-09-16T06:03:00+09:00" });
    expect(screen.queryByRole("button", { name: "変更を保存" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "予約をキャンセル" })).toBeDisabled();
  });
});

describe("PostDrawer non-editable statuses", () => {
  it("locks inputs and hides Save/Cancel for a sent post, showing where to edit instead", () => {
    renderDrawer({ status: "sent", external_link: "https://tiktok.com/@x/video/1" });

    expect(screen.getByLabelText("投稿文")).toBeDisabled();
    expect(screen.getByLabelText("投稿日時")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "変更を保存" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "予約をキャンセル" })).not.toBeInTheDocument();
    expect(screen.getByText(/公開済みの投稿は日時・投稿文を変更できません/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /投稿を見る/ })).toHaveAttribute("href", "https://tiktok.com/@x/video/1");
  });

  it("shows a canceled note for a canceled post", () => {
    renderDrawer({ status: "canceled" });
    expect(screen.getByText("この予約はキャンセル済みです。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "変更を保存" })).not.toBeInTheDocument();
  });

  it("shows the error message for a failed post", () => {
    renderDrawer({ status: "failed", error_message: "動画が140秒を超えています" });
    expect(screen.getByText("動画が140秒を超えています")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "変更を保存" })).not.toBeInTheDocument();
  });
});

describe("PostDrawer cancel two-step confirmation", () => {
  beforeEach(() => {
    patchPostMock.mockReset();
    cancelPostMock.mockReset();
    // handleConfirmCancel は送信直前に `new Date()`（実時刻）で再判定するため、
    // 実時刻を固定しないと「今日」が NOW/due_at を追い越した時点でテストが壊れる。
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("asks for confirmation before cancelling, and 戻る backs out without calling the API", async () => {
    renderDrawer();

    await userEvent.click(screen.getByRole("button", { name: "予約をキャンセル" }));
    expect(screen.getByText(/予約をキャンセルします/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "戻る" }));
    expect(screen.queryByText(/予約をキャンセルします/)).not.toBeInTheDocument();
    expect(cancelPostMock).not.toHaveBeenCalled();
  });

  it("calls cancelPost only after the confirmation step", async () => {
    cancelPostMock.mockResolvedValue({
      result: { service: "tiktok", status: "canceled", post_id: "p1", buffer_post_id: "b1", due_at: null, error: null, issues: [] },
    });
    const { onCanceled } = renderDrawer();

    await userEvent.click(screen.getByRole("button", { name: "予約をキャンセル" }));
    await userEvent.click(screen.getByRole("button", { name: "キャンセルする" }));

    expect(cancelPostMock).toHaveBeenCalledWith("p1", "token-abc");
    await vi.waitFor(() => expect(onCanceled).toHaveBeenCalled());
  });
});

describe("PostDrawer save payload", () => {
  beforeEach(() => {
    patchPostMock.mockReset();
    cancelPostMock.mockReset();
    // handleSave/handleConfirmCancel は送信直前に `new Date()`（実時刻）で再判定するため、
    // 実時刻を固定しないと「今日」が NOW/due_at を追い越した時点でテストが壊れる。
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("calls patchPost with only the changed fields and the access token", async () => {
    patchPostMock.mockResolvedValue({
      result: {
        service: "tiktok",
        status: "scheduled",
        post_id: "p1",
        buffer_post_id: "b1",
        due_at: "2026-09-16T20:00:00+09:00",
        error: null,
        issues: [],
      },
    });
    const { onSaved } = renderDrawer();

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "新しい投稿文です");

    const input = screen.getByLabelText("投稿日時");
    fireEvent.change(input, { target: { value: "2026-09-16T20:00" } });

    await userEvent.click(screen.getByRole("button", { name: "変更を保存" }));

    expect(patchPostMock).toHaveBeenCalledWith(
      "p1",
      {
        caption: "新しい投稿文です",
        due_at: "2026-09-16T20:00:00+09:00",
      },
      "token-abc",
    );
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it("shows the API error message when the save fails", async () => {
    const { ApiError } = await import("@/lib/api");
    patchPostMock.mockRejectedValue(new ApiError(400, { code: "invalid_state", message: "状態が変わっています" }));
    renderDrawer();

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "新しい投稿文");
    await userEvent.click(screen.getByRole("button", { name: "変更を保存" }));

    const note = await screen.findByText(/状態が変わっています/);
    expect(note).toHaveAttribute("data-tone", "err");
  });

  it("shows ambiguous errors with the err tone and a hint to check Buffer's admin screen", async () => {
    const { ApiError } = await import("@/lib/api");
    patchPostMock.mockRejectedValue(new ApiError(409, { code: "ambiguous", message: "対象が一意に決まりません" }));
    renderDrawer();

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "新しい投稿文");
    await userEvent.click(screen.getByRole("button", { name: "変更を保存" }));

    const note = await screen.findByText(/対象が一意に決まりません（Buffer の管理画面もご確認ください）/);
    expect(note).toHaveAttribute("data-tone", "err");
  });

  it("shows an unrecognized/default error code's server message as-is with the err tone", async () => {
    const { ApiError } = await import("@/lib/api");
    patchPostMock.mockRejectedValue(new ApiError(400, { code: "bad_request", message: "リクエストが不正です" }));
    renderDrawer();

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "新しい投稿文");
    await userEvent.click(screen.getByRole("button", { name: "変更を保存" }));

    const note = await screen.findByText("リクエストが不正です");
    expect(note).toHaveAttribute("data-tone", "err");
  });

  it("shows a generic message with the err tone when the request fails without an ApiError (network failure)", async () => {
    patchPostMock.mockRejectedValue(new TypeError("Failed to fetch"));
    renderDrawer();

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "新しい投稿文");
    await userEvent.click(screen.getByRole("button", { name: "変更を保存" }));

    const note = await screen.findByText("通信エラーが発生しました。しばらくしてからもう一度お試しください。");
    expect(note).toHaveAttribute("data-tone", "err");
  });

  it("shows db_write_failed as a warning (Buffer succeeded, our record failed) with the server's message, and triggers a resync", async () => {
    const { ApiError } = await import("@/lib/api");
    patchPostMock.mockRejectedValue(
      new ApiError(502, {
        code: "db_write_failed",
        message: "Bufferには反映済みですが記録に失敗しました。更新ボタンで同期してください",
      }),
    );
    const onRequireResync = vi.fn();
    renderDrawer({}, { onRequireResync });

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "新しい投稿文");
    await userEvent.click(screen.getByRole("button", { name: "変更を保存" }));

    const note = await screen.findByText("Bufferには反映済みですが記録に失敗しました。更新ボタンで同期してください");
    expect(note).toHaveAttribute("data-tone", "warn");
    expect(onRequireResync).toHaveBeenCalledTimes(1);
  });

  it("triggers a resync for db_write_failed on cancel too", async () => {
    const { ApiError } = await import("@/lib/api");
    cancelPostMock.mockRejectedValue(
      new ApiError(502, { code: "db_write_failed", message: "Bufferには反映済みですが記録に失敗しました" }),
    );
    const onRequireResync = vi.fn();
    renderDrawer({}, { onRequireResync });

    await userEvent.click(screen.getByRole("button", { name: "予約をキャンセル" }));
    await userEvent.click(screen.getByRole("button", { name: "キャンセルする" }));

    await screen.findByText("Bufferには反映済みですが記録に失敗しました");
    expect(onRequireResync).toHaveBeenCalledTimes(1);
  });

  it("shows a fixed generic message for internal (500) errors, not the raw server message, with the normal error tone", async () => {
    const { ApiError } = await import("@/lib/api");
    // サーバーが送ってくる 500 の本文は信頼せず、固定の日本語メッセージを表示する
    patchPostMock.mockRejectedValue(new ApiError(500, { code: "internal", message: "Unhandled exception at line 42" }));
    renderDrawer();

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "新しい投稿文");
    await userEvent.click(screen.getByRole("button", { name: "変更を保存" }));

    const note = await screen.findByText("サーバーでエラーが発生しました。時間をおいて再度お試しください");
    expect(note).toHaveAttribute("data-tone", "err");
    expect(screen.queryByText(/Unhandled exception/)).not.toBeInTheDocument();
  });

  it("never resubmits due_at when only the caption changed, even though the original due_at has non-zero seconds", async () => {
    // due_at の元の値が :56 秒でも、日時欄を一切触っていなければ送らない
    // （datetime-local は秒を保持できないため、単純な Date 比較では毎回「変更あり」に誤検出してしまう）
    patchPostMock.mockResolvedValue({
      result: { service: "tiktok", status: "scheduled", post_id: "p1", buffer_post_id: "b1", due_at: null, error: null, issues: [] },
    });
    renderDrawer({ due_at: "2026-09-16T19:00:56+09:00" });

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "新しい投稿文");
    await userEvent.click(screen.getByRole("button", { name: "変更を保存" }));

    expect(patchPostMock).toHaveBeenCalledWith("p1", { caption: "新しい投稿文" }, "token-abc");
  });
});

describe("PostDrawer mutating state (save/cancel mutual exclusion)", () => {
  beforeEach(() => {
    patchPostMock.mockReset();
    cancelPostMock.mockReset();
    // handleSave/handleConfirmCancel は送信直前に `new Date()`（実時刻）で再判定するため、
    // 実時刻を固定しないと「今日」が NOW/due_at を追い越した時点でテストが壊れる。
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("disables both Save and Cancel while a save is in flight, and ignores a second click", async () => {
    let resolveSave!: (value: unknown) => void;
    patchPostMock.mockReturnValue(new Promise((resolve) => (resolveSave = resolve)));
    renderDrawer();

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "新しい投稿文");

    const saveButton = screen.getByRole("button", { name: "変更を保存" });
    await userEvent.click(saveButton);

    expect(saveButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "予約をキャンセル" })).toBeDisabled();

    // in-flight 中の追加クリックは無視される
    await userEvent.click(saveButton);
    expect(patchPostMock).toHaveBeenCalledTimes(1);

    resolveSave({
      result: { service: "tiktok", status: "scheduled", post_id: "p1", buffer_post_id: "b1", due_at: null, error: null, issues: [] },
    });
  });

  it("disables the confirm/back buttons while a cancel is in flight", async () => {
    let resolveCancel!: (value: unknown) => void;
    cancelPostMock.mockReturnValue(new Promise((resolve) => (resolveCancel = resolve)));
    renderDrawer();

    await userEvent.click(screen.getByRole("button", { name: "予約をキャンセル" }));
    const confirmButton = screen.getByRole("button", { name: "キャンセルする" });
    await userEvent.click(confirmButton);

    expect(confirmButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "戻る" })).toBeDisabled();

    resolveCancel({
      result: { service: "tiktok", status: "canceled", post_id: "p1", buffer_post_id: "b1", due_at: null, error: null, issues: [] },
    });
  });
});

describe("PostDrawer revalidates with a fresh clock right before submitting", () => {
  beforeEach(() => {
    patchPostMock.mockReset();
    cancelPostMock.mockReset();
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("refuses to cancel if real time has entered the lockout window since the drawer was opened, without calling the API", async () => {
    // due_at はレンダー時点の now (06:00) から見て5分30秒後 → ロックアウトではないのでボタンは押せる
    renderDrawer({ due_at: "2026-09-16T06:05:30+09:00" });
    const cancelButton = screen.getByRole("button", { name: "予約をキャンセル" });
    expect(cancelButton).toBeEnabled();

    fireEvent.click(cancelButton);

    // 確認前に実時刻が進み、5分ロックアウトの範囲に入る
    vi.setSystemTime(new Date(NOW.getTime() + 60 * 1000));

    fireEvent.click(screen.getByRole("button", { name: "キャンセルする" }));

    expect(cancelPostMock).not.toHaveBeenCalled();
    expect(screen.getByText(/キャンセルできませんでした/)).toBeInTheDocument();
  });

  it("refuses to save if real time has entered the lockout window since the drawer was opened, without calling the API", async () => {
    renderDrawer({ due_at: "2026-09-16T06:05:30+09:00" });

    const textarea = screen.getByLabelText("投稿文");
    fireEvent.change(textarea, { target: { value: "新しい投稿文" } });
    expect(screen.getByRole("button", { name: "変更を保存" })).toBeEnabled();

    vi.setSystemTime(new Date(NOW.getTime() + 60 * 1000));

    fireEvent.click(screen.getByRole("button", { name: "変更を保存" }));

    expect(patchPostMock).not.toHaveBeenCalled();
    expect(screen.getByText(/まもなく送信されるため/)).toBeInTheDocument();
  });
});

describe("PostDrawer remounts per post via key={post.id} (state must not leak across posts)", () => {
  beforeEach(() => {
    patchPostMock.mockReset();
    cancelPostMock.mockReset();
  });

  function Host({ postId }: { postId: "p1" | "p2" }) {
    const posts: Record<string, PostWithRelations> = {
      p1: buildPost({ id: "p1", caption: "投稿A", due_at: "2026-09-16T19:00:00+09:00" }),
      p2: buildPost({ id: "p2", caption: "投稿B", due_at: "2026-09-17T19:00:00+09:00" }),
    };
    const post = posts[postId] as PostWithRelations;
    return (
      <PostDrawer
        key={post.id}
        post={post}
        accessToken="token-abc"
        now={NOW}
        onClose={vi.fn()}
        onSaved={vi.fn()}
        onCanceled={vi.fn()}
      />
    );
  }

  it("resets unsaved caption edits and an open cancel-confirmation when switching to a different post", async () => {
    const { rerender } = render(<Host postId="p1" />);

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "未保存の編集");
    await userEvent.click(screen.getByRole("button", { name: "予約をキャンセル" }));
    expect(screen.getByText(/予約をキャンセルします/)).toBeInTheDocument();

    rerender(<Host postId="p2" />);

    expect(screen.getByLabelText("投稿文")).toHaveValue("投稿B");
    expect(screen.queryByText(/予約をキャンセルします/)).not.toBeInTheDocument();
  });
});

describe("PostDrawer pending status", () => {
  it("shows a neutral 確認中 note and hides Save/Cancel for a pending post", () => {
    renderDrawer({ status: "pending" });
    expect(screen.getByText("確認中")).toBeInTheDocument();
    expect(screen.getByText(/送信結果を確認中です/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "変更を保存" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "予約をキャンセル" })).not.toBeInTheDocument();
  });
});

describe("PostDrawer is clock-independent (guard test)", () => {
  // このスイート全体が「今日の日付」に依存しないことを保証するガードテスト。
  // 実時刻を、固定フィクスチャの日付から遠く未来（2030年）にずらしても、
  // NOW/due_at を同じだけ未来にずらしてあれば save が成功することを確認する。
  const FAR_FUTURE_NOW = new Date("2030-01-01T10:00:00+09:00");
  const FAR_FUTURE_DUE_AT = "2030-01-01T20:00:00+09:00";

  beforeEach(() => {
    patchPostMock.mockReset();
    cancelPostMock.mockReset();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(FAR_FUTURE_NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("still saves successfully when the real clock is far past every fixture date, as long as now/due_at are pinned together", async () => {
    patchPostMock.mockResolvedValue({
      result: {
        service: "tiktok",
        status: "scheduled",
        post_id: "p1",
        buffer_post_id: "b1",
        due_at: null,
        error: null,
        issues: [],
      },
    });
    const onSaved = vi.fn();
    render(
      <PostDrawer
        post={buildPost({ due_at: FAR_FUTURE_DUE_AT })}
        accessToken="token-abc"
        now={FAR_FUTURE_NOW}
        onClose={vi.fn()}
        onSaved={onSaved}
        onCanceled={vi.fn()}
      />,
    );

    const textarea = screen.getByLabelText("投稿文");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "未来の投稿文");
    await userEvent.click(screen.getByRole("button", { name: "変更を保存" }));

    expect(patchPostMock).toHaveBeenCalledWith("p1", { caption: "未来の投稿文" }, "token-abc");
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalled());
  });
});
