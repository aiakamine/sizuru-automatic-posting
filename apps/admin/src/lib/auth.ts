import { getSupabaseClient } from "./supabase";

export interface SessionInfo {
  accessToken: string;
  userId: string;
  email: string | null;
}

/**
 * JWT のクレームから `SessionInfo` を組み立てる。
 * `auth.users` の最新値（メール変更・ban 状態）は不要なユースケースのため、
 * ネットワーク往復のある `getUser()` ではなく `getClaims()`（ローカル検証）を使う。
 */
function sessionInfoFromClaims(accessToken: string, claims: Record<string, unknown>): SessionInfo {
  const userId = claims.sub;
  const email = claims.email;
  return {
    accessToken,
    userId: typeof userId === "string" ? userId : "",
    email: typeof email === "string" ? email : null,
  };
}

/**
 * 現在のセッションを取得し、そのアクセストークンを明示的に渡して `getClaims(token)` で検証する。
 * 引数なしの `getClaims()` は「今のセッション」を暗黙に参照するため、`getSession()` の呼び出し後に
 * セッションが変わっていると捕捉したトークンとクレームがずれうる。明示的に渡すことでそれを避ける。
 */
async function fetchSessionInfo(): Promise<SessionInfo | null> {
  const client = getSupabaseClient();
  const { data: sessionData, error: sessionError } = await client.auth.getSession();
  if (sessionError || !sessionData.session) {
    return null;
  }
  const accessToken = sessionData.session.access_token;

  const { data: claimsData, error: claimsError } = await client.auth.getClaims(accessToken);
  if (claimsError || !claimsData) {
    return null;
  }

  return sessionInfoFromClaims(accessToken, claimsData.claims);
}

/** 現在のログインセッションを取得する。未ログイン・検証失敗・通信エラーのいずれでも null。 */
export async function getCurrentSession(): Promise<SessionInfo | null> {
  try {
    return await fetchSessionInfo();
  } catch {
    return null;
  }
}

/**
 * ログイン状態の変化を購読する。
 *
 * - 初回ロード（`fetchSessionInfo`）と `onAuthStateChange` のイベントは非同期に競合しうる。
 *   世代カウンタで「今もっとも新しいリクエスト」だけの結果を反映し、後から解決した古い結果は捨てる。
 * - 購読解除（返り値の関数）後に遅れて届いた結果は無視する。
 * - 各イベントは、そのイベントで受け取ったセッションのアクセストークンを明示的に
 *   `getClaims(token)` へ渡して検証する（「現在の」セッションの暗黙参照はしない）。
 */
export function subscribeToSession(callback: (session: SessionInfo | null) => void): () => void {
  const client = getSupabaseClient();
  let active = true;
  let generation = 0;

  function emit(forGeneration: number, session: SessionInfo | null): void {
    if (!active || forGeneration !== generation) return;
    callback(session);
  }

  const initialGeneration = generation;
  fetchSessionInfo()
    .then((session) => emit(initialGeneration, session))
    .catch(() => emit(initialGeneration, null));

  const { data } = client.auth.onAuthStateChange((_event, session) => {
    generation += 1;
    const myGeneration = generation;

    if (!session) {
      emit(myGeneration, null);
      return;
    }

    client.auth
      .getClaims(session.access_token)
      .then(({ data: claimsData, error }) => {
        if (error || !claimsData) {
          emit(myGeneration, null);
          return;
        }
        emit(myGeneration, sessionInfoFromClaims(session.access_token, claimsData.claims));
      })
      .catch(() => emit(myGeneration, null));
  });

  return () => {
    active = false;
    data.subscription.unsubscribe();
  };
}

/**
 * `shouldCreateUser: false` にしたとき、未登録アドレスに対して GoTrue が返すエラー。
 * これをそのまま表示すると「アドレスが登録されているか」を外部から判別できてしまうため、
 * 成功時と同じ応答（`error: null`）に丸める。バージョンによって `code` が無いことがあるため
 * メッセージ文字列でも判定する。
 */
function isUnknownAddressOtpError(error: { code?: string; message?: string }): boolean {
  if (error.code === "signup_disabled") return true;
  return typeof error.message === "string" && error.message.toLowerCase().includes("signups not allowed");
}

/**
 * メールアドレスへマジックリンクを送信する。
 * `shouldCreateUser: false` を指定し、このフォームから新規 auth ユーザーが作られないようにする
 * （ダミー ID の管理者アカウントは CLI 経由でのみ発行する運用のため）。
 */
export async function signInWithMagicLink(
  email: string,
  redirectTo: string,
): Promise<{ error: string | null }> {
  try {
    const { error } = await getSupabaseClient().auth.signInWithOtp({
      email,
      options: { emailRedirectTo: redirectTo, shouldCreateUser: false },
    });
    if (error && isUnknownAddressOtpError(error)) {
      return { error: null };
    }
    return { error: error?.message ?? null };
  } catch {
    return { error: "通信エラーが発生しました。時間をおいて再度お試しください。" };
  }
}

/** 資格情報そのものが誤っている場合の固定文言。ID の存在有無は漏らさない。 */
const AUTH_FAILED_MESSAGE = "ID またはパスワードが正しくありません";
/** サーバー障害・通信断など、資格情報とは無関係な失敗の固定文言。 */
const NETWORK_FAILED_MESSAGE = "通信に失敗しました。時間をおいてもう一度お試しください";
/** レート制限にかかった場合の固定文言。 */
const RATE_LIMITED_MESSAGE = "ログインの試行が多すぎます。しばらく待ってからお試しください";

/** バックエンドが発行する ID の正規化ルール（trim + 小文字化）と揃える。 */
function normalizeLoginId(id: string): string {
  return id.trim().toLowerCase();
}

/**
 * ログイン ID を Supabase auth 用のメールアドレスに変換する。
 * 発行される ID は `sizuru-admin1` のような短い文字列なので `@sizuru.local` を補う。
 * `@` を含む値（実メールアドレス。既存の kiyoto_hata 運用）はそのまま使う。
 */
export function toLoginEmail(id: string): string {
  const trimmed = id.trim();
  return trimmed.includes("@") ? trimmed : `${trimmed}@sizuru.local`;
}

interface PasswordAuthErrorLike {
  name?: string;
  status?: number;
  code?: string;
}

/**
 * Supabase から返るエラーが「サーバー障害・通信断」由来かを判定する。
 * 5xx や `AuthRetryableFetchError`（fetch 自体の失敗）はこちらに分類し、
 * 正しい資格情報を持つ利用者に「ID/パスワードが違う」と誤って伝えないようにする。
 */
function isNetworkFailure(error: PasswordAuthErrorLike): boolean {
  if (error.name === "AuthRetryableFetchError") return true;
  return typeof error.status === "number" && error.status >= 500;
}

const RATE_LIMIT_CODES = ["over_request_rate_limit", "over_email_send_rate_limit"];

/** レート制限によるエラー（HTTP 429、または既知のレート制限コード）かを判定する。 */
function isRateLimited(error: PasswordAuthErrorLike): boolean {
  if (error.status === 429) return true;
  return typeof error.code === "string" && RATE_LIMIT_CODES.includes(error.code);
}

/**
 * ID とパスワードでログインする。
 * - サーバー障害・通信断（5xx・`AuthRetryableFetchError`・fetch 自体の例外）→ `NETWORK_FAILED_MESSAGE`
 *   （こちらを認証失敗と伝えると、正しい資格情報の利用者が自分の入力を疑ってしまうため区別する）
 * - レート制限（HTTP 429・既知のレート制限コード）→ `RATE_LIMITED_MESSAGE`
 * - それ以外（無効な ID/パスワードの組み合わせ）→ `AUTH_FAILED_MESSAGE`（ID の存在有無を漏らさない）
 */
export async function signInWithPassword(id: string, password: string): Promise<{ error: string | null }> {
  try {
    const { error } = await getSupabaseClient().auth.signInWithPassword({
      email: toLoginEmail(normalizeLoginId(id)),
      password,
    });
    if (!error) return { error: null };
    if (isNetworkFailure(error)) return { error: NETWORK_FAILED_MESSAGE };
    if (isRateLimited(error)) return { error: RATE_LIMITED_MESSAGE };
    return { error: AUTH_FAILED_MESSAGE };
  } catch {
    return { error: NETWORK_FAILED_MESSAGE };
  }
}

export async function signOut(): Promise<void> {
  await getSupabaseClient().auth.signOut();
}
