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

/** メールアドレスへマジックリンクを送信する。 */
export async function signInWithMagicLink(
  email: string,
  redirectTo: string,
): Promise<{ error: string | null }> {
  try {
    const { error } = await getSupabaseClient().auth.signInWithOtp({
      email,
      options: { emailRedirectTo: redirectTo },
    });
    return { error: error?.message ?? null };
  } catch {
    return { error: "通信エラーが発生しました。時間をおいて再度お試しください。" };
  }
}

export async function signOut(): Promise<void> {
  await getSupabaseClient().auth.signOut();
}
