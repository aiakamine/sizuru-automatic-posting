import { getApiUrl } from "./env";
import type { ApiErrorBody, PostActionResult, SyncResult } from "./types";

function apiBaseUrl(): string {
  const url = getApiUrl();
  if (!url) {
    throw new Error("NEXT_PUBLIC_API_URL is not configured");
  }
  return url.replace(/\/+$/, "");
}

/** API から返るエラー（`{error:{code,message}}`）をラップする例外。 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
  }
}

/**
 * 401（JWT 期限切れ等）を受け取ったときのハンドラ。
 * ログアウト・ログイン画面への遷移という副作用は、アプリ起点（layout）で
 * 一度だけ登録し、この層はそれを呼び出すだけにする。
 */
let onUnauthorized: () => void = () => {};

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

async function parseErrorBody(response: Response): Promise<ApiErrorBody> {
  try {
    const json = (await response.json()) as { error?: ApiErrorBody };
    if (json?.error?.code && json?.error?.message) {
      return json.error;
    }
  } catch {
    // レスポンスが JSON でない場合はフォールバックする
  }
  return { code: "unknown", message: "予期しないエラーが発生しました" };
}

async function request<T>(path: string, init: RequestInit, accessToken: string): Promise<T> {
  const response = await fetch(`${apiBaseUrl()}${path}`, {
    ...init,
    headers: {
      ...init.headers,
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
  });

  if (!response.ok) {
    const errorBody = await parseErrorBody(response);
    if (response.status === 401) {
      onUnauthorized();
    }
    throw new ApiError(response.status, errorBody);
  }

  return (await response.json()) as T;
}

export interface PatchPostBody {
  caption?: string;
  due_at?: string;
}

export interface PostActionResponse {
  result: PostActionResult;
}

export function patchPost(
  postId: string,
  body: PatchPostBody,
  accessToken: string,
): Promise<PostActionResponse> {
  return request<PostActionResponse>(
    `/posts/${postId}`,
    { method: "PATCH", body: JSON.stringify(body) },
    accessToken,
  );
}

export function cancelPost(postId: string, accessToken: string): Promise<PostActionResponse> {
  return request<PostActionResponse>(`/posts/${postId}/cancel`, { method: "POST" }, accessToken);
}

export interface SyncResponse {
  sync: SyncResult;
}

export function syncPosts(accessToken: string): Promise<SyncResponse> {
  return request<SyncResponse>("/sync", { method: "POST" }, accessToken);
}
