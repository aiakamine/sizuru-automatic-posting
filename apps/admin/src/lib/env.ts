/**
 * `NEXT_PUBLIC_*` 環境変数を読む唯一の場所。
 *
 * `output: "export"` の静的書き出しでは、ビルド時に `process.env.NEXT_PUBLIC_XXX` という
 * **リテラルな**参照だけが文字列置換で値に埋め込まれる。`process.env[name]` のような動的な
 * キーアクセスは埋め込まれず、実行時には常に `undefined` になる。
 * そのためここでは必ず `process.env.NEXT_PUBLIC_...` の形でリテラルに参照する。
 */

export function getSupabaseUrl(): string | undefined {
  return process.env.NEXT_PUBLIC_SUPABASE_URL;
}

export function getSupabaseAnonKey(): string | undefined {
  return process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
}

export function getApiUrl(): string | undefined {
  return process.env.NEXT_PUBLIC_API_URL;
}

export interface AppEnv {
  supabaseUrl: string;
  supabaseAnonKey: string;
  apiUrl: string;
}

/** 3つすべてが設定されていれば返す。1つでも欠けていれば null（設定エラー画面の表示判定用）。 */
export function getAppEnv(): AppEnv | null {
  const supabaseUrl = getSupabaseUrl();
  const supabaseAnonKey = getSupabaseAnonKey();
  const apiUrl = getApiUrl();
  if (!supabaseUrl || !supabaseAnonKey || !apiUrl) {
    return null;
  }
  return { supabaseUrl, supabaseAnonKey, apiUrl };
}
