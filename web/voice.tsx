import { useEffect, useRef, useState, type FormEvent } from "react";

import { api, json, pending, states, phases, type Asset, type Job } from "./audio";

const presets = {
  "Спокойный диктор": "Calm Russian narrator, clear articulation, measured pace.",
  "Спокойный репортаж": "Russian field reporter, warm conversational tone, natural pauses.",
  "Нейтральный рассказчик": "Neutral Russian narrator, even pace, clear pronunciation.",
};
type Preset = keyof typeof presets;
const defaultPreset = "Спокойный диктор";

type Voice = { id: string; name: string; mode: "design" | "clone"; preview_id: string; reference: Asset };
type Props = { voice: string | null; disabled: boolean; selectVoice: (id: string | null) => void; };
function Player({ asset, label, listened, failed }: { asset: Asset; label: string; listened: () => void; failed: () => void }) {
  return <audio aria-label={label} controls preload="metadata" src={"/api/audio/" + asset.sha256}
    onError={failed} onEnded={listened} onPause={(event) => {
      if (event.currentTarget.currentTime > 0 && !event.currentTarget.error) listened();
    }} onPlay={(event) => {
      for (const player of document.querySelectorAll("audio")) {
        if (player !== event.currentTarget) player.pause();
      }
    }}/>;
}

function JobActivity({ job, cancel }: { job: Job; cancel: (job: Job) => void }) {
  return <div className="voice-activity">
    <span role="status">{job.phase ? phases[job.phase] : states[job.state]}</span>
    {pending(job) && <button type="button" className="subtle" onClick={() => cancel(job)}>Остановить</button>}
    {job.error && <div role="alert">
      Не удалось создать пробу. Проверьте настройки и нажмите «Создать пробу» ещё раз.
      <details><summary>Причина ошибки</summary>{job.error}</details>
    </div>}
  </div>;
}

export function VoicePanel({ voice, disabled, selectVoice }: Props) {
  const loadSequence = useRef(0);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [previews, setPreviews] = useState<Job[]>([]);
  const [previewId, setPreviewId] = useState("");
  const [mode, setMode] = useState<"design" | "clone">("design");
  const [preset, setPreset] = useState<Preset>(defaultPreset);
  const [description, setDescription] = useState(presets[defaultPreset]);
  const [played, setPlayed] = useState<string[]>([]);
  const [names, setNames] = useState<Record<string, string>>({});
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
      const stamp = ++loadSequence.current;
      try {
        const [nextVoices, nextPreviews] = await Promise.all([
          api<Voice[]>("voices"), api<Job[]>("previews"),
        ]);
        if (!valid || stamp !== loadSequence.current) return;
        setVoices(nextVoices);
        setPreviews(nextPreviews);
        setNetworkError("");
      } catch {
        if (valid && stamp === loadSequence.current) setNetworkError("Не удалось обновить звук. Проверьте подключение к приложению.");
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
    ++loadSequence.current;
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
        if (!description.trim()) throw new Error("Выберите пресет или добавьте описание голоса.");
        job = await api("previews/design", json({ text, seed, description }));
      } else {
        const file = form.get("reference");
        if (!(file instanceof File) || !file.size) throw new Error("Выберите WAV с образцом голоса.");
        if (file.size > 16 * 1024 ** 2) throw new Error("Референс превышает 16 МиБ.");
        const query = new URLSearchParams({ text, seed: String(seed) });
        job = await api("previews/clone?" + query, { method: "POST", body: file });
      }
      const defaultName = mode === "design" ? preset : "Голос по образцу";
      setNames((list) => ({ ...list, [job.id]: defaultName }));
      remember(job);
      setPreviewId(job.id);
      setName("");
    });
  }

  function pin(event: FormEvent) {
    event.preventDefault();
    if (!preview) return;
    void action(async () => {
      const item = await api<Voice>("voices", json({ preview_id: preview.id, name: name.trim() || names[preview.id] || "Голос из пробы" }));
      ++loadSequence.current;
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
        <select id="clip-voice" disabled={disabled} value={voice ?? ""} onChange={(event) => selectVoice(event.target.value || null)}>
          <option value="">Выберите голос</option>
          {voices.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </div>
      <button className="subtle" type="button" disabled={disabled} aria-expanded={studio} aria-controls="voice-studio"
        onClick={() => setStudio(!studio)}>{studio ? "Скрыть пробы" : "Создать голос"}</button>
    </div>
    {networkError && <p className="voice-error" role="alert">{networkError}</p>}
    {error && <p className="voice-error" role="alert">{error}</p>}
    {studio && <div id="voice-studio" className="voice-studio">
      <form onSubmit={createPreview}>
        <fieldset>
          <legend>Новая проба</legend>
          <div className="voice-modes">
            <label><input type="radio" name="mode" checked={mode === "design"} onChange={() => setMode("design")}/> По описанию</label>
            <label><input type="radio" name="mode" checked={mode === "clone"} onChange={() => setMode("clone")}/> По образцу</label>
          </div>
          {mode === "design" ? <label>Пресет
            <select value={preset} onChange={(event) => {
              const next = event.target.value as Preset;
              setPreset(next);
              setDescription(presets[next]);
            }}>{Object.keys(presets).map((item) => <option key={item}>{item}</option>)}</select>
          </label> : <label>Образец голоса (WAV до 30 с)
            <input name="reference" required type="file" accept=".wav,audio/wav"/>
          </label>}
          <label>Текст пробы<input name="phrase" required maxLength={240}
            defaultValue="Сегодня мы расскажем о главных событиях дня."/></label>
          <details>
            <summary>Дополнительные настройки</summary>
            {mode === "design" && <label>Описание голоса (рекомендуем английский)
              <input name="description" maxLength={1000} value={description}
                onChange={(event) => setDescription(event.target.value)}/>
            </label>}
            <label>Seed<input className="seed" name="seed" type="number" required min={0}
              max={2147483647} defaultValue={42}/></label>
          </details>
          <button type="submit" className="primary" disabled={busy}>Создать пробу</button>
        </fieldset>
      </form>
      <div className="voice-preview">
        <h2>Прослушивание пробы</h2>
        {preview && <>
          <label>История проб
            <select value={preview.id} onChange={(event) => { setPreviewId(event.target.value); setName(""); }}>
              {previews.map((job, index) => <option key={job.id} value={job.id}>
                {previews.length - index}. {job.request.mode === "design" ? "По описанию" : "По образцу"} · {job.request.text.slice(0, 55)}
              </option>)}
            </select>
          </label>
          <JobActivity job={preview} cancel={cancel}/>
          {preview.result && <>
            <Player key={preview.id} asset={preview.result.asset} label="Аудио голосовой пробы"
              listened={() => setPlayed((ids) => ids.includes(preview.id) ? ids : [...ids, preview.id])}
              failed={() => setError("Проба недоступна. Создайте новую или выберите другую из истории.")}/>
            {pinned ? <button className="subtle" disabled={disabled || voice === pinned.id}
              onClick={() => selectVoice(pinned.id)}>{voice === pinned.id ? "Голос используется" : "Использовать"}</button> :
              <form className="voice-pin" onSubmit={pin}>
                <details>
                  <summary>Название голоса</summary>
                  <label>Имя при сохранении<input maxLength={120} value={name}
                    placeholder={names[preview.id] ?? "Голос из пробы"} onChange={(event) => setName(event.target.value)}/></label>
                </details>
                {!played.includes(preview.id) && <p>Прослушайте пробу, чтобы использовать голос.</p>}
                <button className="primary" type="submit" disabled={disabled || busy || !played.includes(preview.id)}>Использовать</button>
              </form>}
          </>}
        </>}
      </div>
    </div>}
  </section>;
}
