// Thin fetch wrapper. Every POST carries X-Algo: 1 (the server's CSRF guard).
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function handle<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, msg);
  }
  return r.json() as Promise<T>;
}

export function get<T>(path: string): Promise<T> {
  return fetch(path, { credentials: "same-origin" }).then((r) => handle<T>(r));
}

export function post<T>(path: string, body: unknown = {}): Promise<T> {
  return fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Algo": "1" },
    body: JSON.stringify(body),
  }).then((r) => handle<T>(r));
}

export const inr = (v: string | number | null | undefined): string => {
  if (v === null || v === undefined || v === "") return "-";
  const n = typeof v === "string" ? parseFloat(v) : v;
  return (n >= 0 ? "+" : "") + n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
};

export const ago = (iso: string | null | undefined, nowIso?: string): string => {
  if (!iso) return "never";
  const now = nowIso ? Date.parse(nowIso) : Date.now();
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
};
