export type Asset = { sha256: string; seconds: number };
export type Job = {
  id: string;
  state: "queued" | "running" | "ready" | "failed" | "cancelled" | "interrupted";
  phase: "connecting" | "loading" | "synthesizing" | null;
  error: string | null;
  request: { text: string; seed: number; mode: "design" | "clone";
    recovery?: { source_take: string; strategy: "full" | "split" } };
  result: { asset: Asset; reused: boolean } | null;
  segment_id: string | null;
  text_hash: string | null;
  voice_id: string | null;
};
export const pending = (job?: Job) => job?.state === "queued" || job?.state === "running";
export const states = {
  queued: "В очереди", running: "Выполняется", ready: "WAV готов", failed: "Ошибка",
  cancelled: "Отменено", interrupted: "Прервано",
};
export const phases = { connecting: "Подключаем VoxCPM2…", loading: "Загружаем модель…", synthesizing: "Синтезируем речь…" };

export class HttpError extends Error {
  constructor(message: string, readonly status: number, readonly detail?: unknown) { super(message); }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch("/api/" + path, init);
  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    const fallback = response.status === 409 ? "Клип изменился. Откройте актуальную редакцию и повторите."
      : response.status >= 500 ? "Сервер не подтвердил запрос." : "Проверьте поля и повторите запрос.";
    throw new HttpError(typeof problem?.detail === "string" ? problem.detail : fallback, response.status, problem?.detail);
  }
  return response.json();
}

export const json = (data: object): RequestInit => ({
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
});
