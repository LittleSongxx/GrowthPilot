let token = '';
export function setToken(value: string) { token = value; }

export async function response(path: string, body?: unknown, signal?: AbortSignal, timeoutMs = 45_000): Promise<Response> {
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = body instanceof Blob ? body.type : 'application/json';
  // 普通请求不能永久卡住；一轮语音对话允许跑几分钟（ASR + 模型 + 工具），
  // 传 timeoutMs=0 关闭客户端整体超时，由服务端会话预算与用户打断负责收尾。
  const timeout = timeoutMs > 0 ? AbortSignal.timeout(timeoutMs) : undefined;
  const combined = signal && timeout ? AbortSignal.any([signal, timeout]) : signal ?? timeout;
  const result = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST', headers, signal: combined,
    body: body === undefined ? undefined : body instanceof Blob ? body : JSON.stringify(body),
  });
  if (!result.ok) {
    const error = await result.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : `请求失败（${result.status}）`);
  }
  return result;
}
export async function request<T = unknown>(path: string, body?: unknown, signal?: AbortSignal, timeoutMs?: number): Promise<T> {
  return (await response(path, body, signal, timeoutMs)).json();
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
export interface Trace { events: unknown[]; last_seq: number; has_more?: boolean }
export interface Turn { reply_message_id: string | null; end_after_playback?: boolean }
