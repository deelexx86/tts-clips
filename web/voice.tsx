import { useEffect, useState, type FormEvent } from "react";

import { api, json, pending, states, phases, type Asset, type Job } from "./audio";

type Voice = { id: string; name: string; mode: "design" | "clone"; preview_id: string; reference: Asset };
type Props = { voice: string | null; selectVoice: (id: string | null) => void; };
function Player({ asset, label }: { asset: Asset; label: string }) {
  return <audio aria-label={label} controls preload="metadata" src={"/api/audio/" + asset.sha256}
    onPlay={(event) => {
      for (const player of document.querySelectorAll("audio")) {
        if (player !== event.currentTarget) player.pause();
      }
    }}/>;
}

function JobActivity({ job, cancel }: { job: Job; cancel: (job: Job) => void }) {
  return <div className="voice-activity">
    <span role="status">{job.phase ? phases[job.phase] : states[job.state]}</span>
    {pending(job) && <button type="button" className="subtle" onClick={() => cancel(job)}>Остановить</button>}
    {job.error && <p role="alert">{job.error}</p>}
  </div>;
}

export function VoicePanel({ voice, selectVoice }: Props) {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [previews, setPreviews] = useState<Job[]>([]);
  const [previewId, setPreviewId] = useState("");
  const [mode, setMode] = useState<"design" | "clone">("design");
  const [name, setName] = useState("");
  const [studio, setStudio] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [networkError, setNetworkError] = useState("");
  const preview = previews.find((job) => job.id === previewId) ?? previews[0];
  const shouldPoll = previews.some(pending);
  const pinned = voices.find((item) => item.preview_id === preview?.id);

  useEffect(() => {
    let valid = true;
    async function load() {
      try {
        const [nextVoices, nextPreviews] = await Promise.all([
          api<Voice[]>("voices"), api<Job[]>("previews"),
        ]);
        if (!valid) return;
        setVoices(nextVoices);
        setPreviews(nextPreviews);
        setNetworkError("");
      } catch {
        if (valid) setNetworkError("Не удалось обновить звук. Проверьте подключение к приложению.");
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), shouldPoll ? 900 : 5000);
    return () => { valid = false; window.clearInterval(timer); };
  }, [shouldPoll]);

  async function action(run: () => Promise<void>) {
    setError("");
    setBusy(true);
    try {
      await run();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Ошибка запроса.");
    } finally {
      setBusy(false);
    }
  }

  function remember(job: Job) {
    const update = (current: Job[]) => [job, ...current.filter((item) => item.id !== job.id)];
    setPreviews(update);
  }

  function createPreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const text = String(form.get("phrase"));
    const seed = Number(form.get("seed"));
    void action(async () => {
      let job: Job;
      if (mode === "design") {
        job = await api("previews/design", json({ text, seed, description: form.get("description") }));
      } else {
        const file = form.get("reference");
        if (!(file instanceof File) || !file.size) throw new Error("Выберите WAV с образцом голоса.");
        if (file.size > 16 * 1024 ** 2) throw new Error("Референс превышает 16 МиБ.");
        const query = new URLSearchParams({ text, seed: String(seed) });
        job = await api("previews/clone?" + query, { method: "POST", body: file });
      }
      remember(job);
      setPreviewId(job.id);
      setName("");
    });
  }

  function pin(event: FormEvent) {
    event.preventDefault();
    if (!preview) return;
    void action(async () => {
      const item = await api<Voice>("voices", json({ preview_id: preview.id, name }));
      setVoices((list) => [item, ...list.filter((entry) => entry.id !== item.id)]);
      selectVoice(item.id);
    });
  }

  function cancel(job: Job) {
    void action(async () => { remember(await api("audio-jobs/" + job.id + "/cancel", { method: "POST" })); });
  }

  return <section className="voice-workspace" aria-labelledby="voice-heading">
    <div className="voice-bar">
      <div className="voice-select">
        <h2 id="voice-heading"><label htmlFor="clip-voice">Голос клипа</label></h2>
        <select id="clip-voice" value={voice ?? ""} onChange={(event) => selectVoice(event.target.value || null)}>
          <option value="">Выберите голос</option>
          {voices.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </div>
      <button className="subtle" type="button" aria-expanded={studio} aria-controls="voice-studio"
        onClick={() => setStudio(!studio)}>{studio ? "Закрыть студию" : "Студия голоса"}</button>
    </div>
    {networkError && <p className="voice-error" role="alert">{networkError}</p>}
    {error && <p className="voice-error" role="alert">{error}</p>}
    {studio && <div id="voice-studio" className="voice-studio">
      <form onSubmit={createPreview}>
        <fieldset>
          <legend>Новая проба</legend>
          <div className="voice-modes">
            <label><input type="radio" name="mode" checked={mode === "design"} onChange={() => setMode("design")}/> Voice Design</label>
            <label><input type="radio" name="mode" checked={mode === "clone"} onChange={() => setMode("clone")}/> Voice Clone</label>
          </div>
          {mode === "design" ? <label>Описание голоса
            <input name="description" required maxLength={1000} placeholder="Например, спокойный голос с чёткой дикцией"/>
          </label> : <label>Образец голоса (WAV до 30 с)
            <input name="reference" required type="file" accept=".wav,audio/wav"/>
          </label>}
          <label>Текст пробы<input name="phrase" required maxLength={240}/></label>
          <label>Seed<input className="seed" name="seed" type="number" required min={0} max={2147483647} defaultValue={42}/></label>
          <button type="submit" className="primary" disabled={busy}>Создать пробу</button>
        </fieldset>
      </form>
      <div className="voice-preview">
        <h2>Прослушивание пробы</h2>
        {preview && <>
          <label>Проба
            <select value={preview.id} onChange={(event) => { setPreviewId(event.target.value); setName(""); }}>
              {previews.map((job, index) => <option key={job.id} value={job.id}>
                {previews.length - index}. {job.request.mode === "design" ? "Design" : "Clone"} · {job.request.text.slice(0, 55)}
              </option>)}
            </select>
          </label>
          <JobActivity job={preview} cancel={cancel}/>
          {preview.result && <>
            <Player asset={preview.result.asset} label="Аудио голосовой пробы"/>
            {pinned ? <p>Закреплённый голос · {pinned.name}</p> :
              <form className="voice-pin" onSubmit={pin}>
                <label>Название голоса<input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)}/></label>
                <button className="primary" type="submit" disabled={busy || !name.trim()}>Закрепить и выбрать</button>
              </form>}
          </>}
        </>}
      </div>
    </div>}
  </section>;
}
