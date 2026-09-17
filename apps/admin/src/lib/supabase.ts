import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { getSupabaseAnonKey, getSupabaseUrl } from "./env";

let client: SupabaseClient | null = null;

/**
 * ブラウザで初めて使われたときにだけ Supabase クライアントを生成する。
 * `output: "export"` の静的書き出しはビルド時にクライアントコンポーネントも
 * 一度評価するため、モジュール読み込み時に即座に `createClient` を呼ぶと
 * 環境変数が無いビルド環境（CI など）で失敗しうる。遅延生成でそれを避ける。
 */
export function getSupabaseClient(): SupabaseClient {
  if (!client) {
    const url = getSupabaseUrl();
    const anonKey = getSupabaseAnonKey();
    if (!url || !anonKey) {
      throw new Error("NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY is not configured");
    }
    client = createClient(url, anonKey);
  }
  return client;
}
