import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, HttpError, json, pending, phases, states, type Job } from "./audio";
import { QcDetails, qcPending, type QC } from "./qc";
import { RecoveryRuns, strategyName, type RecoveryCommand, type RecoveryRun } from "./recovery";
import { SpeechWarnings, speechGate, type SpeechGate, type SpeechWarning } from "./speech-ready";

import { takeStatus } from "./take-status";

type Segment = { segment_id: string; text_sha256: string; text: string; start: number; end: number };
type Review = { listened: boolean; decision: "pending" | "accepted" | "rejected"; note: string };
type Take = Job & { generation_id: string | null; review?: Review; qc?: QC };
type Choice = { clip_id: string; segment_id: string; text_hash: string; voice_id: string;
  current: string; previous: string | null; version: number };
type Workspace = { jobs: Take[]; selections: Choice[]; recoveries: RecoveryRun[] };
type Command = {
  revision: string; key: string; mode: "selected" | "missing" | "repeat" | "variant";
  segments: string[]; source_take?: string; speech_confirmation?: string;
};
type Props = {
  clip: { id: string; revision: string };
  voice: string | null;
  saved: boolean;
  segment: Segment | null;
  segments: Segment[];
  selectSegment: (id: string) => void;
  setMarked: (ids: string[]) => void;
  prepare: () => void;
  next: () => void;
  visible: boolean;
  marked: string[];
  locate: (range: Pick<SpeechWarning, "start" | "end">, id?: string) => void;
};

const statusOf = (take?: Take) =>
  takeStatus(take?.state, take?.review?.decision, take?.qc, Boolean(take?.request.recovery));

function Status({ status }: { status: ReturnType<typeof takeStatus> }) {
  return <span className={"record-status " + status.tone}>{status.label}</span>;
}

function Note({ value, disabled, save }: { value: string; disabled: boolean; save: (note: string) => Promise<boolean> }) {
  const [draft, setDraft] = useState<string | null>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (draft !== null && await save(draft)) setDraft(null);
  }
  return <form className="take-note" onSubmit={submit}>
    <label>Заметка <input value={draft ?? value} maxLength={500} onChange={(event) => setDraft(event.target.value)}/></label>
    <button className="subtle" type="submit" disabled={disabled || draft === null || draft === value}>Сохранить заметку</button>
  </form>;
}

export function TakesPanel({ clip, voice, segment, segments, marked, setMarked, saved, locate, selectSegment, prepare, next, visible }: Props) {
  const [workspace, setWorkspace] = useState<Workspace>({ jobs: [], selections: [], recoveries: [] });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [networkError, setNetworkError] = useState(false);
  const [retry, setRetry] = useState<Command | null>(null);
  const [media, setMedia] = useState({ time: 0, duration: 0, paused: true });
  const [mediaError, setMediaError] = useState(false);
  const [listOpen, setListOpen] = useState(false);
  const [batchMode, setBatchMode] = useState(false);
  const [playing, setPlaying] = useState<{ id: string; label: string } | null>(null);
  const list = useRef<HTMLOListElement>(null);
  const inspector = useRef<HTMLDivElement>(null);
  const [problemsOnly, setProblemsOnly] = useState(false);
  const [includeSuspect, setIncludeSuspect] = useState(false);
  const [retryRecovery, setRetryRecovery] = useState<RecoveryCommand | null>(null);
  const [confirmation, setConfirmation] = useState<{
    gate: SpeechGate; revision: string; run: () => void;
  } | null>(null);
  const gate = saved && confirmation?.revision === clip.revision ? confirmation.gate : null;
  const player = useRef<HTMLAudioElement>(null);
  const requestSeq = useRef(0);
  const via = "clips/" + clip.id;
  const jobs = workspace.jobs;
  const inProgress = jobs.some((job) => pending(job) || qcPending(job.qc)) ||
    workspace.recoveries.some((run) => run.items.some((item) => item.state === "active"));
  const activeSources = new Set(workspace.recoveries.flatMap((run) =>
    run.items.filter((item) => item.state === "active").map((item) => item.source_id)));

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      const data = await api<Workspace>(via + "/takes");
      if (seq === requestSeq.current) {
        setWorkspace(data);
        setNetworkError(false);
      }
    } catch {
      if (seq === requestSeq.current) setNetworkError(true);
    }
  }, [via]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), inProgress ? 800 : 2500);
    return () => { ++requestSeq.current; window.clearInterval(timer); };
  }, [refresh, inProgress]);

  useEffect(() => {
    if (player.current) {
      player.current.pause();
      player.current.removeAttribute("src");
      player.current.load();
    }
    setPlaying(null);
    setMediaError(false);
    if (inspector.current) inspector.current.scrollTop = 0;
  }, [segment?.segment_id, voice]);

  const finished = jobs.filter((job) => job.state === "ready" && job.result && job.review);
  const segmentHashes = new Map(segments.map((item) => [item.segment_id, item.text_sha256]));
  const currentTakes = finished.filter((job) => job.segment_id && job.voice_id === voice &&
    segmentHashes.get(job.segment_id) === job.text_hash).reverse();
  const takes = currentTakes.filter((job) => job.segment_id === segment?.segment_id);
  const history = finished.filter((job) => job.voice_id !== voice || !job.segment_id ||
    segmentHashes.get(job.segment_id) !== job.text_hash);
  const choice = workspace.selections.find((item) => item.segment_id === segment?.segment_id &&
    item.text_hash === segment?.text_sha256 && item.voice_id === voice);
  const groups = new Map<string, Take[]>();
  for (const take of currentTakes) {
    const id = take.segment_id!;
    const group = groups.get(id) ?? [];
    group.push(take);
    groups.set(id, group);
  }
  const choices = new Map(workspace.selections.filter((item) => item.voice_id === voice &&
    segmentHashes.get(item.segment_id) === item.text_hash).map((item) => [item.segment_id, item]));
  const latest = new Map<string, Take>();
  for (const job of jobs) {
    if (job.segment_id && job.voice_id === voice && segmentHashes.get(job.segment_id) === job.text_hash &&
        !latest.has(job.segment_id)) latest.set(job.segment_id, job);
  }
  const rows = segments.map((item, index) => {
    const id = item.segment_id;
    const selected = groups.get(id)?.find((take) => take.id === choices.get(id)?.current);
    return { item, index, status: selected ? statusOf(selected) :
      groups.get(id)?.length ? { label: "Выберите дубль", tone: "warning", problem: true } : statusOf(latest.get(id)) };
  });
  const problemRows = rows.filter((row) => row.status.problem);
  const activeIndex = segments.findIndex((item) => item.segment_id === segment?.segment_id);

  useEffect(() => {
    if (!visible || !list.current || !segment) return;
    const scroller = list.current;
    const row = scroller.querySelector<HTMLElement>("[aria-current='true']")?.closest("li");
    if (!row || scroller.scrollHeight <= scroller.clientHeight) return;
    const bounds = scroller.getBoundingClientRect();
    const target = row.getBoundingClientRect();
    if (target.top < bounds.top) scroller.scrollTop -= bounds.top - target.top;
    else if (target.bottom > bounds.bottom) scroller.scrollTop += target.bottom - bounds.bottom;
  }, [visible, segment?.segment_id, problemsOnly]);

  const validMarked = marked.filter((id) => segmentHashes.has(id));
  const targets = validMarked.length ? validMarked : segment ? [segment.segment_id] : [];
  const enabled = Boolean(voice && saved && !busy);
  const groupIds = [...new Set(jobs.filter((job) => pending(job) && !job.request.recovery)
    .map((job) => job.generation_id))];
  const lastJob = jobs.find((job) => !job.request.recovery);
  if (lastJob && !groupIds.includes(lastJob.generation_id)) groupIds.push(lastJob.generation_id);

  async function action(run: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await run();
      await refresh();
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Ошибка запроса.");
      await refresh();
      return false;
    } finally {
      setBusy(false);
    }
  }

  function send(command: Command) {
    setConfirmation(null);
    void action(async () => {
      try {
        const batch = await api<{jobs: Job[]}>(via + "/generations", json(command));
        setRetry(null);
        if (!batch.jobs.length) setNotice("Для текущего голоса недостающих сегментов нет.");
      } catch (cause) {
        setRetry(cause instanceof HttpError && cause.status < 500 ? null : command);
        const gate = cause instanceof HttpError && speechGate(cause.detail);
        if (gate) {
          setConfirmation({ gate, revision: command.revision,
            run: () => send({ ...command, speech_confirmation: gate.confirmation }) });
          return;
        }
        throw cause;
      }
    });
  }

  function generate(mode: Command["mode"], source?: Take) {
    send({
      revision: clip.revision, key: crypto.randomUUID(), mode,
      segments: mode === "missing" ? [] : source?.segment_id ? [source.segment_id] : targets,
      ...(source && { source_take: source.id }),
    });
  }

  function recover(command: RecoveryCommand) {
    setConfirmation(null);
    void action(async () => {
      try {
        await api(via + "/recoveries", json(command));
        setRetryRecovery(null);
      } catch (cause) {
        setRetryRecovery(cause instanceof HttpError && cause.status < 500 ? null : command);
        const gate = cause instanceof HttpError && speechGate(cause.detail);
        if (gate) {
          setConfirmation({ gate, revision: command.revision,
            run: () => recover({ ...command, speech_confirmation: gate.confirmation }) });
          return;
        }
        throw cause;
      }
    });
  }

  function startRecovery(source?: Take) {
    recover({ revision: clip.revision, key: crypto.randomUUID(), include_suspect: includeSuspect,
      ...(source && { source_take: source.id }) });
  }

  function canRecover(take: Take) {
    return take.qc?.state === "complete" && (take.qc.report?.status === "likely_defect" ||
      (includeSuspect && take.qc.report?.status === "suspect"));
  }

  function select(take: string, version: number) {
    void action(async () => {
      await api(via + "/take-selection", { ...json({ take_id: take, version }), method: "PUT" });
    });
  }

  function review(take: Take, fields: Partial<Review>) {
    return action(async () => {
      await api(via + "/takes/" + take.id + "/review", { ...json(fields), method: "PATCH" });
    });
  }

  function recheck(take: Take) {
    void action(async () => {
      await api(via + "/takes/" + take.id + "/qc", { method: "POST" });
    });
  }

  function updateMedia(audio: HTMLAudioElement) {
    setMedia({ time: audio.currentTime, duration: Number.isFinite(audio.duration) ? audio.duration : 0, paused: audio.paused });
  }

  function startAudio(audio: HTMLAudioElement) {
    void audio.play().catch((cause) => {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setMediaError(true);
    });
  }

  function play(take: Take, label: string) {
    const audio = player.current;
    if (!audio || !take.result) return;
    setMediaError(false);
    setPlaying({ id: take.id, label });
    audio.src = "/api/audio/" + take.result.asset.sha256;
    startAudio(audio);
  }

  function jumpToProblem() {
    const row = problemRows.find(({ index }) => index > activeIndex) ?? problemRows[0];
    if (row) selectSegment(row.item.segment_id);
  }

  const voiced = rows.filter(({ item }) => groups.has(item.segment_id)).length;
  const missing = rows.some(({ item }) => {
    const choice = choices.get(item.segment_id);
    return !groups.get(item.segment_id)?.some((take) => take.id === choice?.current && take.review?.decision !== "rejected");
  });
  const variantSource = takes.find((take) => take.id === choice?.current) ?? takes[0];
  const clock = (value: number) => Math.floor(value / 60) + ":" + String(Math.floor(value % 60)).padStart(2, "0");

  return <section className="take-workspace work-stage" aria-labelledby="takes-heading">
    <header className="stage-head">
      <div><h2 id="takes-heading">Озвучка</h2>
        <p>{voiced} из {segments.length} озвучены{problemRows.length > 0 && " · С замечаниями: " + problemRows.length}</p>
      </div>
      <div className="actions">
        {(missing || validMarked.length > 0) && <>
          <button className="subtle" disabled={!enabled || !targets.length} onClick={() => generate("selected")}>
            {validMarked.length ? "Озвучить отмеченные · " + validMarked.length : "Озвучить сегмент"}
          </button>
          <button className={problemRows.length ? "subtle" : "primary"} disabled={!enabled || !segments.length} onClick={() => generate("missing")}>
            Озвучить недостающие
          </button>
        </>}
        {problemRows.length > 0 ? <button className="primary" onClick={jumpToProblem}>К замечанию</button> :
          voiced > 0 && voiced === segments.length && <button className="primary" onClick={next}>К экспорту</button>}
      </div>
    </header>
    <div className="speech-grid">
      <aside className="segments" aria-label="Сегменты">
        <div className="pane-head">
          <button className="subtle segment-toggle" aria-expanded={listOpen} aria-controls="segment-rows"
            onClick={() => setListOpen(!listOpen)}>{listOpen ? "Скрыть список" : "Список сегментов"} · {segments.length}</button>
          <h3 className="segment-heading">Сегменты · {segments.length}</h3>
          <button className="subtle" aria-pressed={batchMode} onClick={() => {
            if (batchMode) setMarked([]);
            setBatchMode(!batchMode);
            setListOpen(true);
          }}>{batchMode ? "Снять отметки" : "Отметить"}</button>
        </div>
        <div className={"segment-rows" + (listOpen ? " expanded" : "")} id="segment-rows">
          <label className="qc-filter"><input type="checkbox" checked={problemsOnly}
            onChange={(event) => setProblemsOnly(event.target.checked)}/> Только с замечаниями · {problemRows.length}</label>
          <ol ref={list} tabIndex={0} aria-label="Список реплик">
            {(problemsOnly ? problemRows : rows).map(({ item, index, status }) => <li key={item.segment_id}>
              {batchMode && <input type="checkbox" aria-label={"Включить сегмент " + (index + 1) + " в генерацию"}
                checked={marked.includes(item.segment_id)} onChange={(event) => setMarked(event.target.checked ?
                  [...marked, item.segment_id] : marked.filter((id) => id !== item.segment_id))}/>}
              <button aria-current={segment?.segment_id === item.segment_id ? "true" : undefined}
                onClick={() => { selectSegment(item.segment_id); setListOpen(false); }}>
                <span className="number">{String(index + 1).padStart(2, "0")}</span>
                <span><span className="segment-preview">{item.text}</span><Status status={status}/></span>
              </button>
            </li>)}
          </ol>
          {problemsOnly && !problemRows.length && <p className="hint">Сегментов с замечаниями нет.</p>}
        </div>
      </aside>
      <div className="inspector">
        <div className="inspector-scroll" ref={inspector}>
          <div className="inspector-head">
            <h3 id="active-segment" tabIndex={-1}>{segment ? "Сегмент " + (activeIndex + 1) + " из " + segments.length : "Нет сегментов"}</h3>
            {segment && <div className="actions">
              <button className="subtle" aria-label="Предыдущий сегмент" title="Предыдущий сегмент"
                disabled={activeIndex <= 0} onClick={() => selectSegment(segments[activeIndex - 1].segment_id)}>←</button>
              <button className="subtle" aria-label="Следующий сегмент" title="Следующий сегмент"
                disabled={activeIndex >= segments.length - 1} onClick={() => selectSegment(segments[activeIndex + 1].segment_id)}>→</button>
              <button className="subtle text-jump" disabled={!saved} onClick={() => locate(segment, segment.segment_id)}>К тексту</button>
            </div>}
          </div>
          {segment && <blockquote className="segment-copy">{segment.text}</blockquote>}
          <div className="take-player" hidden={!takes.length && !playing}>
            <strong>{playing ? "Сегмент " + (activeIndex + 1) + " · " + playing.label : "Выберите дубль для прослушивания"}</strong>
            <div className="take-player-controls">
              <button className="subtle" onClick={() => {
                const audio = player.current;
                if (!audio) return;
                if (!playing && variantSource) {
                  play(variantSource, "Дубль " + (takes.indexOf(variantSource) + 1));
                } else if (audio.paused) startAudio(audio);
                else audio.pause();
              }}>{media.paused ? "Слушать запись" : "Пауза"}</button>
              <input type="range" aria-label="Позиция воспроизведения" min={0}
                max={media.duration || 1} step={0.01} value={media.time} disabled={!media.duration}
                onChange={(event) => {
                  if (!player.current) return;
                  player.current.currentTime = Number(event.target.value);
                  updateMedia(player.current);
                }}/>
              <time>{clock(media.time)} / {clock(media.duration)}</time>
            </div>
            <audio aria-label="Плеер дублей" ref={player} preload="none"
              onTimeUpdate={(event) => updateMedia(event.currentTarget)}
              onLoadedMetadata={(event) => updateMedia(event.currentTarget)}
              onEmptied={(event) => updateMedia(event.currentTarget)}
              onPause={(event) => updateMedia(event.currentTarget)}
              onEnded={(event) => updateMedia(event.currentTarget)}
              onError={() => { if (player.current?.getAttribute("src")) setMediaError(true); }}
              onPlay={(event) => {
                updateMedia(event.currentTarget);
                for (const other of document.querySelectorAll("audio")) if (other !== event.currentTarget) other.pause();
              }}/>
            {mediaError && <p role="alert">Запись недоступна. Выберите другой дубль или озвучьте сегмент заново.</p>}
          </div>
          {(!voice || !segment) && <p className="stage-footer">
            {!segment ? "Вставьте русский текст." : "Выберите или создайте голос."}
            <button className="subtle" onClick={prepare}>К подготовке</button>
          </p>}
          {voice && segment && !takes.length && <p>Озвучьте сегмент или запустите озвучку недостающих.</p>}
          {error && <div role="alert" className="voice-error">
            Не удалось выполнить действие. Проверьте подключение и повторите.
            <details><summary>Причина</summary><p>{error}</p></details>
          </div>}
          {gate && confirmation && <section className="speech-ready speech-confirmation" aria-label="Подтверждение озвучки">
            <p role="status">В выбранном для озвучки тексте остались замечания.</p>
            <SpeechWarnings warnings={gate.warnings} locate={locate}/>
            <div className="actions">
              <button className="subtle" type="button" onClick={() => setConfirmation(null)}>Отмена</button>
              <button className="primary" type="button" disabled={!enabled} onClick={confirmation.run}>
                Озвучить с предупреждениями
              </button>
            </div>
          </section>}
          {notice && <p role="status">{notice}</p>}
          {networkError && <p role="alert">Связь с сервером прервана. <button className="subtle" onClick={() => void refresh()}>Обновить</button></p>}
          {retry && <button className="subtle" disabled={busy} onClick={() => send(retry)}>Повторить отправку</button>}
          {groupIds.map((id) => {
            const group = id ? jobs.filter((job) => job.generation_id === id) : lastJob ? [lastJob] : [];
            const active = group.find((job) => job.state === "running") ?? group.find(pending);
            const unsuccessful = group.filter((job) => !pending(job) && job.state !== "ready");
            return <div className="take-progress" key={id ?? "legacy"}>
              <span role="status">Готово {group.filter((job) => job.state === "ready").length} из {group.length}
                {active && " · " + (active.phase ? phases[active.phase] : states[active.state])}</span>
              {active && <button className="subtle" disabled={busy} onClick={() => void action(async () => {
                await api(id ? "generations/" + id + "/cancel" : "audio-jobs/" + active.id + "/cancel", {method: "POST"});
              })}>Остановить генерацию</button>}
              {unsuccessful.length > 0 && <details>
                <summary>Незавершённые · {unsuccessful.length}</summary>
                <p>Готовые записи сохранены. Для новой попытки озвучьте нужный сегмент.</p>
                <ul>{unsuccessful.map((job) => <li key={job.id}>{states[job.state]} · {job.request.text}
                  {job.error && <details><summary>Причина ошибки</summary>{job.error}</details>}
                </li>)}</ul>
              </details>}
            </div>;
          })}
          {takes.length > 0 && <div className="take-list">
            <div className="variants-head">
              <h3>Варианты озвучки</h3>
              {variantSource && <button className="subtle" disabled={!enabled} onClick={() => generate("variant", variantSource)}>
                Новый вариант
              </button>}
            </div>
            {takes.map((take, index) => {
              const takeNumber = index + 1;
              const title = "Дубль " + takeNumber;
              const entry = choice;
              const selected = entry?.current === take.id;
              const reviewState = take.review!;
              return <article key={take.id} aria-label={title}>
                <div className="take-line">
                  <div>
                    <h4>{title}{selected && <span className="picked" title="Выбран для сборки">Выбран</span>}</h4>
                    <Status status={statusOf(take)}/>
                  </div>
                  <div className="actions">
                    <button className="subtle" aria-pressed={playing?.id === take.id} onClick={() => play(take, title)}>Слушать</button>
                    {!selected && <button className="subtle" disabled={!enabled || reviewState.decision === "rejected"}
                      onClick={() => select(take.id, entry?.version ?? 0)}>Выбрать</button>}
                  </div>
                </div>
                <details className="take-review">
                  <summary>Дополнительные действия и проверка</summary>
                  <p>Seed повтора: {take.request.seed}{take.request.recovery &&
                    " · " + strategyName(take.request.recovery.strategy)}</p>
                  <div className="actions">
                    {entry?.previous === take.id && <button className="subtle" disabled={!enabled}
                      onClick={() => select(take.id, entry.version)}>Вернуть предыдущий</button>}
                    <button className="subtle" disabled={!enabled} onClick={() => generate("repeat", take)}>Повторить параметры</button>
                    <button className="subtle" disabled={!enabled} onClick={() => generate("variant", take)}>Новый вариант</button>
                    {canRecover(take) && <button className="subtle" disabled={!enabled || activeSources.has(take.id)}
                      onClick={() => startRecovery(take)}>Восстановить дубль</button>}
                  </div>
                  {take.qc && <QcDetails qc={take.qc} disabled={busy} recheck={() => recheck(take)}/>}
                </details>
                <details className="take-review">
                  <summary>Прослушивание и решение{reviewState.listened && " · Прослушан"}</summary>
                  <div className="actions">
                    <button className="subtle" disabled={busy || reviewState.listened}
                      onClick={() => void review(take, { listened: true })}>Прослушал</button>
                    <button className="subtle" disabled={busy || !reviewState.listened || reviewState.decision === "accepted"}
                      onClick={() => void review(take, { decision: "accepted" })}>Принять</button>
                    <button className="subtle" disabled={busy || reviewState.decision === "rejected"}
                      onClick={() => void review(take, { decision: "rejected" })}>Отклонить</button>
                  </div>
                  <Note value={reviewState.note} disabled={busy} save={(note) => review(take, { note })}/>
                </details>
              </article>;
            })}
          </div>}
          {workspace.recoveries.length > 0 || currentTakes.some((take) =>
            take.qc?.report?.status === "likely_defect" || take.qc?.report?.status === "suspect") ? <details className="take-history"
              open={activeSources.size ? true : undefined}>
            <summary>Восстановление речи</summary>
            {voice && currentTakes.some((take) => take.qc?.report?.status === "likely_defect" ||
                take.qc?.report?.status === "suspect") && <div className="actions recovery-options">
              <button type="button" className="subtle" disabled={!enabled} onClick={() => startRecovery()}>
                Восстановить проблемные сегменты
              </button>
              <label><input type="checkbox" checked={includeSuspect} onChange={(event) => setIncludeSuspect(event.target.checked)}/>
                Включить сомнительные</label>
            </div>}
            {retryRecovery && <button className="subtle" disabled={busy}
              onClick={() => recover(retryRecovery)}>Повторить отправку восстановления</button>}
            <RecoveryRuns runs={workspace.recoveries} disabled={busy} cancel={(id) => void action(async () => {
              await api("recoveries/" + id + "/cancel", {method: "POST"});
            })}/>
          </details> : null}
          {history.length > 0 && saved && <details className="take-history">
            <summary>История других редакций и голосов · {history.length}</summary>
            <ul>{history.map((take, index) => <li key={take.id}>
              <div>
                <button className="subtle" onClick={() => play(take, "Дубль из истории " + (index + 1))}>Слушать</button>
                <span>{take.request.text} · seed {take.request.seed}{take.review?.note && " · " + take.review.note}</span>
                {take.qc && <QcDetails qc={take.qc} disabled={busy} recheck={() => recheck(take)}/>}
              </div>
            </li>)}</ul>
          </details>}
        </div>
      </div>
    </div>
  </section>;
}
