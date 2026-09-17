import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const signInWithMagicLinkMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  signInWithMagicLink: signInWithMagicLinkMock,
}));

const { default: LoginPage } = await import("./page");

/** テストから任意のタイミングで解決/拒否できる Promise を作る。 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("LoginPage", () => {
  beforeEach(() => {
    signInWithMagicLinkMock.mockReset();
  });

  it("blocks submission for an invalid email (native type=email validation) without calling signInWithMagicLink", async () => {
    render(<LoginPage />);

    await userEvent.type(screen.getByLabelText("メールアドレス"), "not-an-email");
    await userEvent.click(screen.getByRole("button", { name: "ログインリンクを送信" }));

    expect(signInWithMagicLinkMock).not.toHaveBeenCalled();
  });

  it("disables the submit button while the magic link is being sent", async () => {
    const { promise } = deferred<{ error: string | null }>();
    signInWithMagicLinkMock.mockReturnValue(promise);
    render(<LoginPage />);

    await userEvent.type(screen.getByLabelText("メールアドレス"), "a@example.com");
    const button = screen.getByRole("button", { name: "ログインリンクを送信" });
    await userEvent.click(button);

    expect(screen.getByRole("button", { name: "送信中..." })).toBeDisabled();
  });

  it("disables the submit button until an email is entered", () => {
    render(<LoginPage />);
    expect(screen.getByRole("button", { name: "ログインリンクを送信" })).toBeDisabled();
  });

  it("shows the error message and re-enables the submit button when sending fails", async () => {
    signInWithMagicLinkMock.mockResolvedValue({ error: "送信に失敗しました" });
    render(<LoginPage />);

    await userEvent.type(screen.getByLabelText("メールアドレス"), "a@example.com");
    await userEvent.click(screen.getByRole("button", { name: "ログインリンクを送信" }));

    await screen.findByText("送信に失敗しました");
    expect(screen.getByRole("button", { name: "ログインリンクを送信" })).toBeEnabled();
  });

  it("shows the sent confirmation instead of the form on success", async () => {
    signInWithMagicLinkMock.mockResolvedValue({ error: null });
    render(<LoginPage />);

    await userEvent.type(screen.getByLabelText("メールアドレス"), "a@example.com");
    await userEvent.click(screen.getByRole("button", { name: "ログインリンクを送信" }));

    await screen.findByText(/ログイン用のリンクをメールで送信しました/);
    expect(screen.queryByLabelText("メールアドレス")).not.toBeInTheDocument();
  });
});
