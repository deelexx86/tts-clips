import { useEffect, useRef, useState } from "react";
import { api, json } from "./audio";

type Format = "wav" | "mp3";
type Options = { revision: string; mode: "draft" | "final"; format: Format; leading_ms: number; gap_ms: number };
type Issue = { code: string; number?: number; segment_id?: string; text?: string };
type Plan = Options & {
  plan_id: string; title: string; frames: number; seconds: number | null;
  blockers: Issue[]; warnings: Issue[];
  inputs: { number: number; segment_id: string; text: string; take_id: string;
    approval: string; asset: { seconds: number } }[];
};
type Published = {
  id: string; created_at: string; plan: Plan; current: boolean; mp3_error: string | null;
  files: Partial<Record<Format, { sha256: string; bytes: number }>>;
};

const messages: Record<string, string> = {
  empty_clip: "Добавьте текст клипа.",
  no_selection: "Выберите озвученный дубль.",
  stale: "Дубль относится к другому тексту или голосу.",
  unavailable: "Дубль недоступен.",
  rejected: "Выбранный дубль отклонён.",
  missing_wav: "WAV выбранного дубля отсутствует.",
  invalid_wav: "WAV выбранного дубля повреждён или недоступен.",
  wav_too_large: "Размер мастера превышает предел формата RIFF WAV.",
  suspect: "Whisper рекомендует прослушать дубль.",
  likely_defect: "Whisper обнаружил вероятный пропуск речи.",
  qc_stale: "Проверка относится к другой версии Whisper или правил. Повторите QC или примите дубль после прослушивания.",
  qc_unchecked: "Дубль ещё не проходил проверку.",
  qc_queued: "Проверка ожидает запуска.",
  qc_running: "Проверка ещё выполняется.",
  qc_failed: "Проверка завершилась ошибкой.",
  qc_unavailable: "Whisper недоступен.",
  verified: "Полнота подтверждена",
  manual: "Принят после прослушивания",
};

const seconds = (value: number) => value.toLocaleString("ru-RU", { maximumFractionDigits: 3 }) + " с";
const formats = ["wav", "mp3"] as const;

function Issues({ heading, issues, locate }: { heading: string; issues: Issue[]; locate: (id: string) => void }) {
  if (!issues.length) return null;
  return <div className="export-issues">
    <h3>{heading}</h3>
    <ul>{issues.map((issue) => <li key={issue.code + (issue.segment_id ?? "")}>
      {issue.segment_id && <button type="button" className="subtle" onClick={() => locate(issue.segment_id!)}>
        Сегмент {issue.number}
      </button>}
      <span>{messages[issue.code] ?? issue.code}</span>
    </li>)}</ul>
  </div>;
}

export function ExportPanel({ clip, saved, locate }: {
  clip: { id: string; revision: string };
  saved: boolean;
  locate: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Options["mode"]>("final");
  const [format, setFormat] = useState<Format>("wav");
  const [leading, setLeading] = useState("0");
  const [gap, setGap] = useState("150");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [history, setHistory] = useState<Published[]>([]);
  const [playing, setPlaying] = useState("");
  const [busy, setBusy] = useState<"planning" | "assembling" | null>(null);
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState(false);
  const historyRequest = useRef(0);
  const via = "clips/" + clip.id;
  const validPauses = [leading, gap].every((value) => /^\d+$/.test(value) && Number(value) <= 10000);
  const options = { revision: clip.revision, mode, format, leading_ms: Number(leading), gap_ms: Number(gap) };
  const selected = history.find((item) => item.id === playing) ?? history[0];
  const isCurrent = (item: Published) => item.current && item.plan.revision === clip.revision;
  const available = plan && saved && validPauses && plan.revision === clip.revision &&
    mode === plan.mode && format === plan.format && Number(leading) === plan.leading_ms && Number(gap) === plan.gap_ms
    ? plan : null;

  useEffect(() => {
    if (!open) return;
    let alive = true;
    async function refresh() {
      const stamp = ++historyRequest.current;
      try {
        const records = await api<Published[]>(via + "/exports");
        if (alive && stamp === historyRequest.current) {
          setHistory(records);
          setHistoryError(false);
        }
      } catch {
        if (alive && stamp === historyRequest.current) setHistoryError(true);
      }
    }
    void refresh();
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 3000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [clip.revision, via, open]);

  async function checkPlan() {
    setBusy("planning");
    setError("");
    setPlan(null);
    try {
      setPlan(await api(via + "/export-plan", json(options)));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось проверить сборку.");
    } finally {
      setBusy(null);
    }
  }

  async function assemble() {
    if (!available) return;
    setBusy("assembling");
    setError("");
    try {
      const response = await fetch("/api/" + via + "/exports", json({ ...options, plan_id: available.plan_id }));
      const result = await response.json();
      if (response.status === 409) {
        setPlan(result.plan ?? null);
        setError(result.current ? "Клип изменился. Откройте актуальную редакцию."
          : "Выбранные дубли или решения изменились. Проверьте обновлённую сборку.");
        return;
      }
      if (!response.ok) throw new Error(result.detail ?? "Не удалось собрать аудио.");
      ++historyRequest.current;
      setHistory((items) => [result, ...items.filter((item) => item.id !== result.id)]);
      setPlaying(result.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось собрать аудио.");
    } finally {
      setBusy(null);
    }
  }

  return <section className="export-workspace" aria-labelledby="export-heading">
    <div className="voice-bar">
      <h2 id="export-heading">Сборка и экспорт</h2>
      <button className="subtle" aria-expanded={open} aria-controls="export-settings" onClick={() => setOpen(!open)}>
        {open ? "Свернуть экспорт" : "Открыть экспорт"}
      </button>
    </div>
    {open && <div className="export-panel" id="export-settings">
      <form onSubmit={(event) => { event.preventDefault(); void checkPlan(); }}>
        <fieldset className="export-settings" disabled={Boolean(busy)}>
          <legend>Параметры сборки</legend>
          <label>Режим
            <select value={mode} onChange={(event) => setMode(event.target.value as Options["mode"])}>
              <option value="final">Окончательная</option>
              <option value="draft">Черновая с замечаниями</option>
            </select>
          </label>
          <label>Формат
            <select value={format} onChange={(event) => setFormat(event.target.value as Format)}>
              <option value="wav">WAV · PCM 48 кГц</option>
              <option value="mp3">MP3 · 128 кбит/с + WAV</option>
            </select>
          </label>
          <label>Пауза в начале (мс)
            <input type="number" required step="1" min="0" max="10000" value={leading} onChange={(event) => setLeading(event.target.value)}/>
          </label>
          <label>Между сегментами (мс)
            <input type="number" required step="1" min="0" max="10000" value={gap} onChange={(event) => setGap(event.target.value)}/>
          </label>
        </fieldset>
        <div className="actions export-actions">
          <button className="subtle" type="submit" disabled={!saved || Boolean(busy) || !validPauses}>Проверить сборку</button>
          <button className="primary" type="button" disabled={!available || !!available.blockers.length || Boolean(busy)}
            onClick={() => void assemble()}>Собрать аудио</button>
          {busy && <span role="status">{busy === "planning" ? "Проверяем дубли…" : "Собираем аудио…"}</span>}
        </div>
      </form>
      {!saved && <p>Дождитесь сохранения клипа перед сборкой.</p>}
      {error && <p className="voice-error" role="alert">{error}</p>}
      {available && <div className="export-plan">
        {available.seconds !== null && <p role="status">{available.inputs.length} сегм. · {seconds(available.seconds)}
          {" · "}{available.frames.toLocaleString("ru-RU")} кадров PCM</p>}
        <Issues heading="Сборка заблокирована" issues={available.blockers} locate={locate}/>
        <Issues heading="Предупреждения черновика" issues={available.warnings} locate={locate}/>
        {available.inputs.length > 0 && <details>
          <summary>Состав сборки</summary>
          <ol>{available.inputs.map((item) => <li key={item.segment_id}>
            {item.number}. {item.text} · {seconds(item.asset.seconds)} · {messages[item.approval] ?? item.approval}
          </li>)}</ol>
        </details>}
      </div>}
      {historyError && <p role="alert">Не удалось обновить историю экспорта.</p>}
      {selected && <div className="export-history">
        <label>История экспортов
          <select value={selected.id} onChange={(event) => setPlaying(event.target.value)}>
            {history.map((item) => <option key={item.id} value={item.id}>
              {new Date(item.created_at).toLocaleString("ru-RU")} · {item.plan.format.toUpperCase()}
              {" · "}{item.plan.mode === "draft" ? "Черновик" : "Выпуск"}{!isCurrent(item) && " · Прежний снимок"}
            </option>)}
          </select>
        </label>
        <p>{seconds(selected.plan.seconds!)} · Паузы {selected.plan.leading_ms} / {selected.plan.gap_ms} мс
          {!isCurrent(selected) && " · Прежний снимок"}
          {selected.plan.warnings.length > 0 && " · Замечания: " + selected.plan.warnings.length}</p>
        <audio key={selected.id} aria-label="Плеер полного клипа" controls preload="metadata"
          src={"/api/exports/" + selected.id + (selected.files.mp3 ? "/mp3" : "/wav")} onPlay={(event) => {
            for (const other of document.querySelectorAll("audio")) if (other !== event.currentTarget) other.pause();
          }}/>
        {selected.mp3_error && <p role="alert" className="voice-error">{selected.mp3_error}</p>}
        <div className="actions">
          {formats.filter((item) => selected.files[item]).map((item) =>
            <a className="subtle" key={item} href={"/api/exports/" + selected.id + "/" + item + "?download=true"}>
              Скачать {item.toUpperCase()}
            </a>
          )}
        </div>
      </div>}
    </div>}
  </section>;
}
