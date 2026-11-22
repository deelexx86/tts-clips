import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, HttpError, json, pending, phases, states, type Job } from "./audio";
import { hasProblem, QcDetails, qcPending, type QC } from "./qc";

type Review = { listened: boolean; decision: "pending" | "accepted" | "rejected"; note: string };
type Take = Job & { generation_id: string | null; review?: Review; qc?: QC };
type Choice = { clip_id: string; segment_id: string; text_hash: string; voice_id: string;
  current: string; previous: string | null; version: number };
type Workspace = { jobs: Take[]; selections: Choice[] };
type Command = {
  revision: string; key: string; mode: "selected" | "missing" | "repeat" | "variant";
  segments: string[]; source_take?: string;
};
type Props = {
  clip: { id: string; revision: string };
  voice: string | null;
  saved: boolean;
  segment: { segment_id: string; text_sha256: string } | null;
  segments: { segment_id: string; text_sha256: string }[];
  marked: string[];
};

const decisions = { pending: "Без решения", accepted: "Принят", rejected: "Отклонён" };

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

export function TakesPanel({ clip, voice, segment, segments, marked, saved }: Props) {
  const [workspace, setWorkspace] = useState<Workspace>({ jobs: [], selections: [] });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [networkError, setNetworkError] = useState(false);
  const [retry, setRetry] = useState<Command | null>(null);
  const [slotA, setSlotA] = useState("");
  const [slotB, setSlotB] = useState("");
  const [playing, setPlaying] = useState("");
  const [problemsOnly, setProblemsOnly] = useState(false);
  const player = useRef<HTMLAudioElement>(null);
  const requestSeq = useRef(0);
  const via = "clips/" + clip.id;
  const jobs = workspace.jobs;
  const inProgress = jobs.some((job) => pending(job) || qcPending(job.qc));

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
    setPlaying("");
  }, [segment?.segment_id, voice]);

  const finished = jobs.filter((job) => job.state === "ready" && job.result && job.review);
  const segmentHashes = new Map(segments.map((item) => [item.segment_id, item.text_sha256]));
  const currentTakes = finished.filter((job) => job.segment_id && job.voice_id === voice &&
    segmentHashes.get(job.segment_id) === job.text_hash).reverse();
  const takes = currentTakes.filter((job) => job.segment_id === segment?.segment_id);
  const problems = currentTakes.filter((job) => hasProblem(job.qc));
  const visible = problemsOnly ? problems : takes;
  const history = finished.filter((job) => job.voice_id !== voice || !job.segment_id ||
    segmentHashes.get(job.segment_id) !== job.text_hash);
  const choice = workspace.selections.find((item) => item.segment_id === segment?.segment_id &&
    item.text_hash === segment?.text_sha256 && item.voice_id === voice);
  const a = takes.find((job) => job.id === slotA) ?? takes.find((job) => job.id === choice?.current) ?? takes[0];
  const b = takes.find((job) => job.id === slotB && job.id !== a?.id) ?? takes.find((job) => job.id !== a?.id);
  const targets = marked.length ? marked : segment ? [segment.segment_id] : [];
  const enabled = Boolean(voice && saved && !busy);
  const groupIds = [...new Set(jobs.filter(pending).map((job) => job.generation_id))];
  const lastJob = jobs[0];
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
    void action(async () => {
      try {
        const batch = await api<{jobs: Job[]}>(via + "/generations", json(command));
        setRetry(null);
        if (!batch.jobs.length) setNotice("Для текущего голоса недостающих сегментов нет.");
      } catch (cause) {
        setRetry(cause instanceof HttpError && cause.status < 500 ? null : command);
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

  function play(take: Take, label: string) {
    const audio = player.current;
    if (!audio || !take.result) return;
    setPlaying(label);
    audio.src = "/api/audio/" + take.result.asset.sha256;
    void audio.play().catch(() => setError("Не удалось воспроизвести WAV."));
  }

  return <section className="take-workspace" aria-labelledby="takes-heading">
    <div className="voice-bar">
      <h2 id="takes-heading">Дубли и озвучка</h2>
      <div className="actions">
        <button className="subtle" disabled={!enabled || !targets.length} onClick={() => generate("selected")}>
          Озвучить выбранные{targets.length ? " · " + targets.length : ""}
        </button>
        <button className="primary" disabled={!enabled || !segments.length} onClick={() => generate("missing")}>
          Озвучить недостающие
        </button>
      </div>
    </div>
    {!voice && <p>Выберите сохранённый голос или создайте пробу.</p>}
    {voice && !segment && <p>Щёлкните по сегменту для просмотра дублей. Флажки задают цели пакетной озвучки.</p>}
    {error && <p className="voice-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {networkError && <p role="alert">Связь с сервером прервана.</p>}
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
          <ul>{unsuccessful.map((job) => <li key={job.id}>{states[job.state]} · {job.request.text}{job.error && " · " + job.error}</li>)}</ul>
        </details>}
      </div>;
    })}
    {currentTakes.length > 0 && <label className="qc-filter">
      <input type="checkbox" checked={problemsOnly} onChange={(event) => setProblemsOnly(event.target.checked)}/>
      Проблемные дубли · {problems.length}
    </label>}
    {problemsOnly && !problems.length && <p>Проблемных дублей для текущего голоса нет.</p>}
    {(visible.length > 0 || history.length > 0) && <div className="take-player">
      {takes.length > 1 && <div className="actions">
        {([["A", a, setSlotA], ["B", b, setSlotB]] as const).map(([label, job, setSlot]) =>
          <div className="take-compare" key={label}>
            <label>{label}
              <select aria-label={"Дубль " + label} value={job?.id ?? ""} onChange={(event) => setSlot(event.target.value)}>
                {takes.map((item, index) => <option key={item.id} value={item.id}
                  disabled={label === "B" && item.id === a?.id}>Дубль {index + 1}</option>)}
              </select>
            </label>
            <button className="subtle" disabled={!job} onClick={() => job && play(job, "Дубль " + label)}>Слушать {label}</button>
          </div>
        )}
      </div>}
      <audio aria-label="Плеер дублей" ref={player} controls preload="none" onPlay={(event) => {
        for (const other of document.querySelectorAll("audio")) if (other !== event.currentTarget) other.pause();
      }}/>
      {playing && <p>{playing}</p>}
    </div>}
    {visible.length > 0 && <div className="take-list">
      {visible.map((take) => {
        const takeNumber = currentTakes.filter((item) => item.segment_id === take.segment_id).indexOf(take) + 1;
        const title = "Дубль " + takeNumber;
        const entry = workspace.selections.find((item) => item.segment_id === take.segment_id &&
          item.text_hash === take.text_hash && item.voice_id === take.voice_id);
        const selected = entry?.current === take.id;
        const reviewState = take.review!;
        return <article key={take.id} aria-label={problemsOnly ? title + " · " + take.request.text : title}>
          <div className="voice-bar">
            <h3>{title}{problemsOnly && " · " + take.request.text}</h3>
            <p>{selected && "Выбран · "}{decisions[reviewState.decision]} · seed {take.request.seed}</p>
          </div>
          <div className="actions">
            <button className="subtle" onClick={() => play(take, title)}>Слушать</button>
            <button className="subtle" disabled={selected || !enabled} onClick={() => select(take.id, entry?.version ?? 0)}>Выбрать</button>
            {entry?.previous === take.id && <button className="subtle" disabled={!enabled}
              onClick={() => select(take.id, entry.version)}>Вернуть предыдущий</button>}
            <button className="subtle" disabled={!enabled} onClick={() => generate("repeat", take)}>Повторить параметры</button>
            <button className="subtle" disabled={!enabled} onClick={() => generate("variant", take)}>Новый вариант</button>
          </div>
          {take.qc && <QcDetails qc={take.qc} disabled={busy} recheck={() => recheck(take)}/>}
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
    {history.length > 0 && saved && <details className="take-history">
      <summary>История других редакций и голосов · {history.length}</summary>
      <ul>{history.map((take) => <li key={take.id}>
        <div>
          <button className="subtle" onClick={() => play(take, take.request.text)}>Слушать</button>
          <span>{take.request.text} · seed {take.request.seed}{take.review?.note && " · " + take.review.note}</span>
          {take.qc && <QcDetails qc={take.qc} disabled={busy} recheck={() => recheck(take)}/>}
        </div>
      </li>)}</ul>
    </details>}
  </section>;
}
