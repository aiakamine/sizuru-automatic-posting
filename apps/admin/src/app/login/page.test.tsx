import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const signInWithMagicLinkMock = vi.fn();
const signInWithPasswordMock = vi.fn();
const routerReplaceMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  signInWithMagicLink: signInWithMagicLinkMock,
  signInWithPassword: signInWithPasswordMock,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplaceMock }),
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

/** デフォルト表示（ID/パスワードフォーム）からマジックリンクフォームに切り替える。 */
async function switchToMagicLink() {
  await userEvent.click(screen.getByRole("button", { name: "メールのリンクでログイン" }));
}

describe("LoginPage", () => {
  beforeEach(() => {
    signInWithMagicLinkMock.mockReset();
    signInWithPasswordMock.mockReset();
    routerReplaceMock.mockReset();
  });

  describe("ID とパスワードでログイン（既定表示）", () => {
    it("shows the id/password form by default", () => {
      render(<LoginPage />);

      expect(screen.getByLabelText("ID")).toBeInTheDocument();
      expect(screen.getByLabelText("パスワード")).toHaveAttribute("type", "password");
      expect(screen.getByRole("button", { name: "ログイン" })).toBeInTheDocument();
    });

    it("disables the submit button until both id and password are entered", async () => {
      render(<LoginPage />);
      const button = screen.getByRole("button", { name: "ログイン" });
      expect(button).toBeDisabled();

      await userEvent.type(screen.getByLabelText("ID"), "sizuru-admin1");
      expect(button).toBeDisabled();

      await userEvent.type(screen.getByLabelText("パスワード"), "hunter2");
      expect(button).toBeEnabled();
    });

    it("disables the submit button while signing in", async () => {
      const { promise } = deferred<{ error: string | null }>();
      signInWithPasswordMock.mockReturnValue(promise);
      render(<LoginPage />);

      await userEvent.type(screen.getByLabelText("ID"), "sizuru-admin1");
      await userEvent.type(screen.getByLabelText("パスワード"), "hunter2");
      const button = screen.getByRole("button", { name: "ログイン" });
      await userEvent.click(button);

      expect(screen.getByRole("button", { name: "送信中..." })).toBeDisabled();
    });

    it("navigates to /calendar/ on success", async () => {
      signInWithPasswordMock.mockResolvedValue({ error: null });
      render(<LoginPage />);

      await userEvent.type(screen.getByLabelText("ID"), "sizuru-admin1");
      await userEvent.type(screen.getByLabelText("パスワード"), "hunter2");
      await userEvent.click(screen.getByRole("button", { name: "ログイン" }));

      await vi.waitFor(() => expect(routerReplaceMock).toHaveBeenCalledWith("/calendar/"));
    });

    it("shows the fixed error message and re-enables the submit button on failure", async () => {
      signInWithPasswordMock.mockResolvedValue({ error: "ID またはパスワードが正しくありません" });
      render(<LoginPage />);

      await userEvent.type(screen.getByLabelText("ID"), "sizuru-admin1");
      await userEvent.type(screen.getByLabelText("パスワード"), "wrong");
      await userEvent.click(screen.getByRole("button", { name: "ログイン" }));

      await screen.findByText("ID またはパスワードが正しくありません");
      expect(screen.getByRole("button", { name: "ログイン" })).toBeEnabled();
      expect(routerReplaceMock).not.toHaveBeenCalled();
    });

    it("passes the entered id and password through to signInWithPassword unchanged", async () => {
      signInWithPasswordMock.mockResolvedValue({ error: null });
      render(<LoginPage />);

      await userEvent.type(screen.getByLabelText("ID"), "sizuru-admin1");
      await userEvent.type(screen.getByLabelText("パスワード"), "hunter2");
      await userEvent.click(screen.getByRole("button", { name: "ログイン" }));

      expect(signInWithPasswordMock).toHaveBeenCalledWith("sizuru-admin1", "hunter2");
    });

    it("submits on pressing Enter in the password field, without a direct click on the submit button", async () => {
      signInWithPasswordMock.mockResolvedValue({ error: null });
      render(<LoginPage />);

      await userEvent.type(screen.getByLabelText("ID"), "sizuru-admin1");
      await userEvent.type(screen.getByLabelText("パスワード"), "hunter2{Enter}");

      await vi.waitFor(() => expect(signInWithPasswordMock).toHaveBeenCalledWith("sizuru-admin1", "hunter2"));
    });

    it("clears the previous error message as soon as a retry is submitted", async () => {
      signInWithPasswordMock.mockResolvedValueOnce({ error: "ID またはパスワードが正しくありません" });
      render(<LoginPage />);

      await userEvent.type(screen.getByLabelText("ID"), "sizuru-admin1");
      await userEvent.type(screen.getByLabelText("パスワード"), "wrong");
      await userEvent.click(screen.getByRole("button", { name: "ログイン" }));
      await screen.findByText("ID またはパスワードが正しくありません");

      const { promise } = deferred<{ error: string | null }>();
      signInWithPasswordMock.mockReturnValueOnce(promise);
      await userEvent.clear(screen.getByLabelText("パスワード"));
      await userEvent.type(screen.getByLabelText("パスワード"), "hunter2");
      await userEvent.click(screen.getByRole("button", { name: "ログイン" }));

      expect(screen.queryByText("ID またはパスワードが正しくありません")).not.toBeInTheDocument();
    });

    it("disables the submit button when the id is only whitespace", async () => {
      render(<LoginPage />);

      await userEvent.type(screen.getByLabelText("ID"), "   ");
      await userEvent.type(screen.getByLabelText("パスワード"), "hunter2");

      expect(screen.getByRole("button", { name: "ログイン" })).toBeDisabled();
    });

    it("disables the submit button when the password is only whitespace", async () => {
      render(<LoginPage />);

      await userEvent.type(screen.getByLabelText("ID"), "sizuru-admin1");
      await userEvent.type(screen.getByLabelText("パスワード"), "   ");

      expect(screen.getByRole("button", { name: "ログイン" })).toBeDisabled();
    });
  });

  describe("メールのリンクでログイン（切り替え）", () => {
    it("switches to the magic-link form and back", async () => {
      render(<LoginPage />);

      await switchToMagicLink();
      expect(screen.getByLabelText("メールアドレス")).toBeInTheDocument();
      expect(screen.queryByLabelText("ID")).not.toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: "ID とパスワードでログイン" }));
      expect(screen.getByLabelText("ID")).toBeInTheDocument();
    });

    it("clears a password-mode error message when switching to magic-link mode", async () => {
      signInWithPasswordMock.mockResolvedValue({ error: "ID またはパスワードが正しくありません" });
      render(<LoginPage />);

      await userEvent.type(screen.getByLabelText("ID"), "sizuru-admin1");
      await userEvent.type(screen.getByLabelText("パスワード"), "wrong");
      await userEvent.click(screen.getByRole("button", { name: "ログイン" }));
      await screen.findByText("ID またはパスワードが正しくありません");

      await switchToMagicLink();

      expect(screen.queryByText("ID またはパスワードが正しくありません")).not.toBeInTheDocument();
    });

    it("blocks submission for an invalid email (native type=email validation) without calling signInWithMagicLink", async () => {
      render(<LoginPage />);
      await switchToMagicLink();

      await userEvent.type(screen.getByLabelText("メールアドレス"), "not-an-email");
      await userEvent.click(screen.getByRole("button", { name: "ログインリンクを送信" }));

      expect(signInWithMagicLinkMock).not.toHaveBeenCalled();
    });

    it("disables the submit button while the magic link is being sent", async () => {
      const { promise } = deferred<{ error: string | null }>();
      signInWithMagicLinkMock.mockReturnValue(promise);
      render(<LoginPage />);
      await switchToMagicLink();

      await userEvent.type(screen.getByLabelText("メールアドレス"), "a@example.com");
      const button = screen.getByRole("button", { name: "ログインリンクを送信" });
      await userEvent.click(button);

      expect(screen.getByRole("button", { name: "送信中..." })).toBeDisabled();
    });

    it("disables the submit button until an email is entered", async () => {
      render(<LoginPage />);
      await switchToMagicLink();
      expect(screen.getByRole("button", { name: "ログインリンクを送信" })).toBeDisabled();
    });

    it("shows the error message and re-enables the submit button when sending fails", async () => {
      signInWithMagicLinkMock.mockResolvedValue({ error: "送信に失敗しました" });
      render(<LoginPage />);
      await switchToMagicLink();

      await userEvent.type(screen.getByLabelText("メールアドレス"), "a@example.com");
      await userEvent.click(screen.getByRole("button", { name: "ログインリンクを送信" }));

      await screen.findByText("送信に失敗しました");
      expect(screen.getByRole("button", { name: "ログインリンクを送信" })).toBeEnabled();
    });

    it("shows the sent confirmation instead of the form on success", async () => {
      signInWithMagicLinkMock.mockResolvedValue({ error: null });
      render(<LoginPage />);
      await switchToMagicLink();

      await userEvent.type(screen.getByLabelText("メールアドレス"), "a@example.com");
      await userEvent.click(screen.getByRole("button", { name: "ログインリンクを送信" }));

      await screen.findByText(/ログイン用のリンクをメールで送信しました/);
      expect(screen.queryByLabelText("メールアドレス")).not.toBeInTheDocument();
    });

    it("submits on pressing Enter in the email field, without a direct click on the submit button", async () => {
      signInWithMagicLinkMock.mockResolvedValue({ error: null });
      render(<LoginPage />);
      await switchToMagicLink();

      await userEvent.type(screen.getByLabelText("メールアドレス"), "a@example.com{Enter}");

      await vi.waitFor(() => expect(signInWithMagicLinkMock).toHaveBeenCalledTimes(1));
    });

    // Unlike the id/password fields (type="text"/"password"), a native
    // type="email" input's value-sanitization algorithm strips a
    // whitespace-only value down to "" on its own, so the (otherwise
    // truthiness-only) `!email` disabled check happens to be correct here —
    // this is a regression guard, not evidence the check itself is sound.
    it("disables the submit button when only whitespace is typed into the native email field", async () => {
      render(<LoginPage />);
      await switchToMagicLink();

      await userEvent.type(screen.getByLabelText("メールアドレス"), "   ");

      expect(screen.getByRole("button", { name: "ログインリンクを送信" })).toBeDisabled();
    });
  });
});
