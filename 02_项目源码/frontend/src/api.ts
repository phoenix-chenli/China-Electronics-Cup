export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(`/api${path}`, { ...options, headers });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { const error = await response.json(); message = typeof error.detail === 'string' ? error.detail : message; } catch { /* Non-JSON server errors keep the HTTP status. */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}
export const isAbort = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';
export const messageOf = (error: unknown) => error instanceof Error ? error.message : '操作没有完成，请重试。';
