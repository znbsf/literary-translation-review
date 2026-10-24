// JSON requests to the local workbench API: timeouts, stale-response guards, readable errors.

const MESSAGES = {
  400: "请求参数无效", 403: "请求被拒绝（需要同源访问）", 404: "未找到请求的数据",
  409: "数据已变化或验源失败", 413: "提交内容过大", 415: "提交格式不被接受",
  500: "本地服务处理失败", 503: "本地服务暂不可用",
};

export class RequestError extends Error {
  constructor(kind, status = 0, code = null) {
    super(kind);
    this.kind = kind;
    this.status = status;
    this.code = code;
  }

  get message() {
    if (this.kind === "network") return "无法连接本地服务，请确认工作台仍在运行。";
    if (this.kind === "timeout") return "请求超时。";
    if (this.kind === "invalid") return "服务返回了无法识别的响应。";
    const base = MESSAGES[this.status] || `请求失败（HTTP ${this.status}）`;
    return this.code ? `${base}：${this.code}` : base;
  }
}

export function url(path, params = {}) {
  const target = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "" && value !== false) {
      target.searchParams.set(key, value === true ? "1" : String(value));
    }
  }
  return target;
}

export async function request(path, { params, body, timeout = 20000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  let response;
  try {
    response = await fetch(url(path, params), {
      method: body ? "POST" : "GET",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
      credentials: "same-origin",
    });
    const text = await response.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      throw new RequestError("invalid", response.status);
    }
    if (!response.ok) throw new RequestError("http", response.status, data && data.error);
    return data;
  } catch (error) {
    if (error instanceof RequestError) throw error;
    throw new RequestError(controller.signal.aborted ? "timeout" : "network");
  } finally {
    clearTimeout(timer);
  }
}

// Returns a function that wraps a request so only the latest call's result is delivered.
export function latest() {
  let ticket = 0;
  return async (promise) => {
    const mine = ++ticket;
    const value = await promise;
    if (mine !== ticket) throw new RequestError("stale");
    return value;
  };
}
