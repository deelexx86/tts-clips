import { phases, states, type Asset, type Job } from "./audio";

export type RecoveryCommand = {
  revision: string; key: string; source_take?: string; include_suspect: boolean; speech_confirmation?: string;
};
type Attempt = {
  strategy: "full" | "split"; job_id: string; state: Job["state"]; phase: Job["phase"];
  qc_state: string; seed: number; error: string | null;
  parts: { number: number; asset: Asset }[];
  assessment: { usable: boolean; reasons: string[]; error: string | null } | null;
};
export type RecoveryRun = {
  id: string;
  items: { id: string; source_id: string; text: string;
    state: "active" | "recovered" | "exhausted" | "cancelled" | "interrupted" | "stale";
    outcome: string | null; attempts: Attempt[] }[];
};

const outcomes: Record<string, string> = {
  candidate_ready: "Найден вариант для сравнения",
  no_sentence_boundary: "Повтор не прошёл проверку, подходящей границы предложения нет",
  attempts_exhausted: "Обе попытки требуют внимания",
  material_changed: "Текст или голос изменился. Результаты сохранены в истории",
  cancelled: "Отменено",
  interrupted: "Прервано во время синтеза",
};
const reasons: Record<string, string> = {
  synthesis_failed: "Ошибка синтеза", qc_failed: "Нет результата Whisper",
  likely_defect: "Вероятен пропуск речи", suspect: "Whisper рекомендует прослушивание",
  qc_changed: "Изменилась версия проверки",
  synthesis_changed: "Изменилась конфигурация синтеза",
  pace_review: "Длительность составного дубля заметно отличается от исходного",
  join_review: "На стыке частей обрывается заметный звуковой сигнал",
  body_review: "В тексте обнаружены пропуски или заметные расхождения",
};

export const strategyName = (strategy: "full" | "split") =>
  strategy === "full" ? "Повтор полной реплики" : "Сборка из двух частей";

function stage(attempt: Attempt) {
  if (attempt.assessment) return attempt.assessment.usable ? "Кандидат прошёл проверку" : "Требуется прослушивание";
  if (attempt.state === "ready" && ["queued", "running"].includes(attempt.qc_state)) return "Проверяем WAV в Whisper…";
  return attempt.phase ? phases[attempt.phase] : states[attempt.state];
}

export function RecoveryRuns({ runs, disabled, cancel }: {
  runs: RecoveryRun[]; disabled: boolean; cancel: (id: string) => void;
}) {
  if (!runs.length) return null;
  return <div className="recovery-runs">
    {runs.map((run, index) => {
      const active = run.items.some((item) => item.state === "active");
      return <details key={run.id} open={index === 0 || active ? true : undefined}>
        <summary>Восстановление · {run.items.filter((item) => item.state === "recovered").length} из {run.items.length}</summary>
        {active && <button type="button" className="subtle" disabled={disabled} onClick={() => cancel(run.id)}>
          Остановить восстановление
        </button>}
        {!run.items.length && <p>Подходящих выбранных дублей нет.</p>}
        {run.items.map((item) => <div className="recovery-result" key={item.id}>
          <p>{item.text}</p>
          {item.outcome && <p role="status">{outcomes[item.outcome] ?? item.outcome}</p>}
          <ul>{item.attempts.map((attempt) => <li key={attempt.job_id}>
            <span>{strategyName(attempt.strategy)} · {stage(attempt)}</span>
            {attempt.assessment?.reasons.map((reason) => <span className="recovery-reason" key={reason}>
              {reasons[reason] ?? reason}
            </span>)}
            {(attempt.assessment?.error || attempt.error) &&
              <p className="voice-error">{attempt.assessment?.error ?? attempt.error}</p>}
            {attempt.state !== "ready" && attempt.parts.map((part) =>
              <a key={part.number} className="recovery-reason" href={"/api/audio/" + part.asset.sha256}>
                Сохранённая часть {part.number}
              </a>)}
          </li>)}</ul>
        </div>)}
      </details>;
    })}
  </div>;
}
