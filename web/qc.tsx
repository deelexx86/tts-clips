export type QC = {
  state: "unchecked" | "queued" | "running" | "unavailable" | "failed" | "complete";
  cached: boolean;
  error: string | null;
  report: {
    id: string;
    status: "verified" | "suspect" | "likely_defect";
    reasons: string[];
    expected: string;
    transcript: { full: string; ending: string };
    full_coverage: number;
    full_ending_missing: number;
    tail_ending_missing: number;
    expected_ending: string[];
    heard_ending: string[];
    edge_dbfs: number;
    basis: { policy: string };
  } | null;
};

export const qcPending = (qc?: QC) => qc?.state === "queued" || qc?.state === "running";

export const hasProblem = (qc?: QC) => qc?.state === "failed" ||
  qc?.report?.status === "suspect" || qc?.report?.status === "likely_defect";

const verdicts = {
  verified: "Полнота подтверждена",
  suspect: "Проверить на слух",
  likely_defect: "Вероятен дефект",
};
const statuses = {
  unchecked: "Ещё не проверен",
  queued: "В очереди",
  running: "Распознаём речь…",
  unavailable: "Требуется настройка",
  failed: "Ошибка проверки",
  complete: "Проверка завершена",
};
const reasons: Record<string, string> = {
  no_expected_words: "В исходном тексте нет слов для сравнения.",
  ending_mismatch: "Окончание распознано иначе.",
  ending_words_missing: "В полной и хвостовой расшифровках не распознаны как минимум два слова в конце.",
  low_coverage: "Распознано менее 82% слов.",
  long_discrepancy: "Найдено расхождение длиной от трёх слов.",
  omission: "В распознавании пропущены как минимум два слова подряд.",
  loud_edge: "В последних 10 мс WAV сохраняется заметный сигнал.",
};

export function QcDetails({ qc, disabled, recheck }: { qc: QC; disabled: boolean; recheck: () => void }) {
  const report = qc.report;
  const label = qc.state === "complete" && report ? verdicts[report.status] : statuses[qc.state];
  return <details className="take-qc">
    <summary>
      Whisper <span role="status" className={"qc-status " + (qc.state === "complete" ? report?.status : qc.state)}
        title={qc.state === "complete" ? report?.status : qc.state}>
        {label}
      </span>
    </summary>
    {qc.error && <p role="alert" className="voice-error">{qc.error}</p>}
    <div className="actions">
      <button className="subtle" disabled={disabled || qcPending(qc)} onClick={recheck}>
        {qc.state === "unchecked" ? "Проверить WAV" : "Проверить повторно"}
      </button>
      {qc.cached && <span>Использован сохранённый результат</span>}
    </div>
    {report && <>
      {qc.state !== "complete" && <p>Предыдущий результат · {verdicts[report.status]}</p>}
      <div className="qc-transcripts">
        <div><h4>Ожидаемый текст</h4><p>{report.expected}</p></div>
        <div><h4>Распознано</h4><p>{report.transcript.full || "Слова не распознаны."}</p></div>
      </div>
      <h4>Окончание фразы</h4>
      <dl>
        <dt>Ожидалось</dt><dd>{report.expected_ending.join(" ") || "—"}</dd>
        <dt>Последние распознанные слова</dt><dd>{report.heard_ending.join(" ") || "—"}</dd>
      </dl>
      {report.transcript.ending !== report.transcript.full &&
        <p>Расшифровка хвоста · {report.transcript.ending || "Слова не распознаны."}</p>}
      {report.reasons.length > 0 && <ul className="qc-reasons">
        {report.reasons.map((reason) => <li key={reason}>{reasons[reason] ?? reason}</li>)}
      </ul>}
      <p className="qc-meta">Покрытие {Math.round(report.full_coverage * 100)}%
        {" · "}{report.basis.policy}{" · "}{report.id.slice(0, 8)}</p>
    </>}
  </details>;
}
