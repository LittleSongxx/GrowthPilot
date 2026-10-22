let token = '';
export function setToken(value: string) { token = value; }

export async function response(path: string, body?: unknown, signal?: AbortSignal): Promise<Response> {
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = body instanceof Blob ? body.type : 'application/json';
  // 网络故障不能永久卡住一轮；用户开口时传入的取消信号仍会立即生效。
  const timeout = AbortSignal.timeout(45_000);
  const result = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST', headers, signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    body: body === undefined ? undefined : body instanceof Blob ? body : JSON.stringify(body),
  });
  if (!result.ok) {
    const error = await result.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : `请求失败（${result.status}）`);
  }
  return result;
}
export async function request<T = unknown>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return (await response(path, body, signal)).json();
}

export interface Scenario { id: string; title: string; industry: string }
export interface Message { id: string; origin: 'customer' | 'customer_transcript' | 'agent' | 'host_control'; text: string }
export interface SessionState {
  session: { session_id: string; state: string; reason: string | null };
  messages?: Message[];
  memory?: { field: string; value: string | number | boolean | null; status: string; version?: number }[];
  field_definitions?: { id: string; description: string }[];
  profile_update?: { status: string; attempts: number; error: string | null };
  audit?: unknown[];
}
export interface Trace { events: unknown[]; last_seq: number }
export interface Turn { reply_message_id: string | null; end_after_playback?: boolean }
