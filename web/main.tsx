import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { editorRange, replacement, type Edit, type Range } from "./text";
import { VoicePanel } from "./voice";
import { TakesPanel } from "./takes";
import { ExportPanel } from "./export";
import { SpeechWarnings, type SpeechWarning } from "./speech-ready";
import "./style.css";

type Summary = { id: string; title: string; updated_at: string };
type Span = { start: number; end: number; segment_id: string | null; text_sha256: string | null };
type SpeechSpan = Span & { segment_id: string; text_sha256: string };
type Snapshot = Summary & {
  revision: string; text: string; spans: Span[]; voice_id: string | null; speech_ready: SpeechWarning[];
};
type SaveState = "saved" | "pending" | "saving" | "error" | "conflict";

const stateLabel: Record<SaveState, string> = {
  saved: "Сохранено", pending: "Есть изменения", saving: "Сохраняется…",
  error: "Ошибка сохранения", conflict: "Конфликт редакций",
};

async function request(url: string, init?: RequestInit) {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function App() {
  const [clips, setClips] = useState<Summary[]>([]);
  const [clip, setClip] = useState<Snapshot | null>(null);
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [voice, setVoice] = useState<string | null>(null);
  const [status, setStatus] = useState<SaveState>("saved");
  const [conflict, setConflict] = useState<Snapshot | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [marked, setMarked] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");
  const [showLibrary, setShowLibrary] = useState(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const ruler = useRef<HTMLDivElement>(null);
  const conflictHeading = useRef<HTMLHeadingElement>(null);
  const selectedRange = useRef<{ text: string; range: Range; inputType: string } | null>(null);
  const composing = useRef(false);
  const timer = useRef<number | undefined>(undefined);
  const saved = useRef<Snapshot | null>(null);
  const pending = useRef<Edit[]>([]);
  const draft = useRef({ text: "", title: "", voice: null as string | null });
  const latestTitle = useRef("");
  const inFlight = useRef<Promise<boolean> | null>(null);
  const competing = useRef<Snapshot | null>(null);

  function mergeSummary(snapshot: Snapshot) {
    setClips((list) => [
      { id: snapshot.id, title: snapshot.title, updated_at: snapshot.updated_at },
      ...list.filter((item) => item.id !== snapshot.id),
    ]);
  }

  function adopt(snapshot: Snapshot) {
    saved.current = snapshot;
    competing.current = null;
    pending.current = [];
    latestTitle.current = snapshot.title;
    draft.current = { text: snapshot.text, title: snapshot.title, voice: snapshot.voice_id };
    setClip(snapshot);
    setText(snapshot.text);
    setTitle(snapshot.title);
    setVoice(snapshot.voice_id);
    setConflict(null);
    setStatus("saved");
    setActive(null);
    setMarked([]);
    const url = new URL(window.location.href);
    url.searchParams.set("clip", snapshot.id);
    window.history.replaceState(null, "", url);
  }

  async function boot() {
    setLoading(true);
    setNotice("");
    try {
      const list: Summary[] = await request("/api/clips");
      setClips(list);
      const target = new URLSearchParams(window.location.search).get("clip");
      const id = list.find((item) => item.id === target)?.id ?? list[0]?.id;
      if (id) adopt(await request("/api/clips/" + id));
    } catch {
      setNotice("Не удалось открыть рабочую область. Проверьте, запущен ли сервер.");
    } finally {
      setLoading(false);
    }
  }

  function dirty() {
    return pending.current.length > 0 || draft.current.title !== latestTitle.current ||
      (saved.current !== null && draft.current.voice !== saved.current.voice_id);
  }

  function persist(): Promise<boolean> {
    window.clearTimeout(timer.current);
    if (inFlight.current) return inFlight.current;
    const base = saved.current;
    if (competing.current || composing.current) return Promise.resolve(false);
    if (!base || !dirty()) return Promise.resolve(true);
    const edits = pending.current.slice();
    const titleSent = draft.current.title;
    const voiceSent = draft.current.voice;
    setStatus("saving");
    inFlight.current = (async () => {
      try {
        const response = await fetch("/api/clips/" + base.id, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ base_revision: base.revision, title: titleSent, edits, voice_id: voiceSent }),
        });
        if (response.status === 409) {
          const { current } = await response.json() as { current: Snapshot };
          competing.current = current;
          setConflict(current);
          setStatus("conflict");
          return false;
        }
        if (!response.ok) throw new Error(await response.text());
        const snapshot: Snapshot = await response.json();
        saved.current = snapshot;
        pending.current.splice(0, edits.length);
        latestTitle.current = snapshot.title;
        if (draft.current.title === titleSent) {
          draft.current.title = snapshot.title;
          setTitle(snapshot.title);
        }
        setClip(snapshot);
        mergeSummary(snapshot);
        setStatus(dirty() ? "pending" : "saved");
        if (dirty() && !composing.current) schedule();
        return true;
      } catch {
        setStatus("error");
        return false;
      } finally {
        inFlight.current = null;
      }
    })();
    return inFlight.current;
  }

  function schedule() {
    window.clearTimeout(timer.current);
    if (!competing.current && !composing.current) {
      timer.current = window.setTimeout(() => void persist(), 380);
    }
  }

  function editText(value: string) {
    const before = draft.current.text;
    const selection = selectedRange.current;
    selectedRange.current = null;
    if (value === before) return;
    let range = selection?.text === before ? selection.range : undefined;
    if (range && range[0] === range[1] && selection && value.length < before.length) {
      const caret = range[0];
      const removed = before.length - value.length;
      if (selection.inputType.endsWith("Backward")) range = [caret - removed, caret];
      if (selection.inputType.endsWith("Forward")) range = [caret, caret + removed];
    }
    pending.current.push(replacement(before, value, range));
    draft.current.text = value;
    setText(value);
    if (!competing.current) setStatus("pending");
    schedule();
  }

  async function open(id?: string) {
    if (loading || id === saved.current?.id && id !== undefined) return;
    setLoading(true);
    setNotice("");
    try {
      do {
        if (!await persist()) return;
      } while (dirty());
      const snapshot = await request(id ? "/api/clips/" + id : "/api/clips",
        id ? undefined : { method: "POST" });
      adopt(snapshot);
      if (!id) mergeSummary(snapshot);
      setShowLibrary(false);
      requestAnimationFrame(() => textarea.current?.focus());
    } catch {
      setNotice("Не удалось открыть клип. Проверьте подключение и повторите.");
    } finally {
      setLoading(false);
    }
  }

  function resolveConflict(keepDraft: boolean) {
    const current = competing.current;
    if (!current) return;
    const local = { ...draft.current };
    adopt(current);
    mergeSummary(current);
    if (keepDraft) {
      if (local.text !== current.text) {
        pending.current = [{ start: 0, end: Array.from(current.text).length, text: local.text }];
      }
      draft.current = local;
      setText(local.text);
      setTitle(local.title);
      setVoice(local.voice);
      if (dirty()) void persist();
    }
    textarea.current?.focus();
  }

  useEffect(() => { void boot(); }, []);
  useEffect(() => {
    function confirmClose(event: BeforeUnloadEvent) {
      if (!dirty() && !inFlight.current) return;
      event.preventDefault();
      event.returnValue = "";
    }
    const saveOnHide = () => {
      if (document.visibilityState === "hidden" && !inFlight.current && dirty()) void persist();
    };
    window.addEventListener("beforeunload", confirmClose);
    document.addEventListener("visibilitychange", saveOnHide);
    return () => {
      window.removeEventListener("beforeunload", confirmClose);
      document.removeEventListener("visibilitychange", saveOnHide);
      window.clearTimeout(timer.current);
    };
  }, []);
  useEffect(() => {
    if (conflict) conflictHeading.current?.focus();
  }, [conflict]);
  useEffect(() => {
    const input = textarea.current;
    if (!input) return;
    function remember(event: Event) {
      const edit = event as InputEvent;
      if (!input) return;
      const start = input.selectionStart;
      const end = input.selectionEnd;
      selectedRange.current = /^(insert|delete)/.test(edit.inputType)
        ? { text: input.value, range: [start, end], inputType: edit.inputType } : null;
    }
    input.addEventListener("beforeinput", remember);
    return () => input.removeEventListener("beforeinput", remember);
  }, [clip?.id]);

  const points = Array.from(text);
  const currentPlan = clip?.text === text;
  const segments = currentPlan ? clip?.spans.filter(
    (span): span is SpeechSpan => span.segment_id !== null && span.text_sha256 !== null
  ) ?? [] : [];

  function locate(span: Pick<Span, "start" | "end">, segmentId: string | null = null) {
    const input = textarea.current;
    if (!input) return;
    const [start, end] = editorRange(points, span.start, span.end);
    input.focus();
    input.setSelectionRange(start, end);
    if (ruler.current) {
      ruler.current.style.width = input.clientWidth + "px";
      ruler.current.textContent = text.slice(0, start) + "\u200b";
      input.scrollTop = Math.max(0, ruler.current.scrollHeight - input.clientHeight / 2);
    }
    setActive(segmentId);
  }

  return (
    <div className="shell">
      <header className="top">
        <div className="brand"><span className="mark" aria-hidden="true"><i/><i/><i/><i/></span>TTS Clips</div>
        <button className="subtle mobile-library" aria-expanded={showLibrary}
          aria-controls="library" onClick={() => setShowLibrary(!showLibrary)}>Клипы</button>
        {clip && <span className={"save " + status} role="status">{stateLabel[status]}</span>}
        {status === "error" && <button className="subtle" onClick={() => void persist()}>Повторить</button>}
      </header>

      <div className="layout">
        <nav id="library" className={"library " + (showLibrary ? "expanded" : "")} aria-label="Клипы">
          <div className="library-head">
            <h2>Клипы</h2>
            <button className="primary" onClick={() => void open()} disabled={loading}>＋ Новый клип</button>
          </div>
          <ul>
            {clips.map((item) => <li key={item.id}>
              <button className="clip-link" aria-current={clip?.id === item.id ? "page" : undefined}
                onClick={() => void open(item.id)} disabled={loading}>
                <span aria-hidden="true" className="file-icon">≡</span><span>{item.title.trim() || "Без названия"}</span>
              </button>
            </li>)}
          </ul>
        </nav>

        <main className={clip ? "work" : "empty-work"}>
          {notice && <div className="notice" role="alert">{notice}
            {!clip && <button className="subtle" onClick={() => void boot()}>Повторить</button>}
          </div>}
          {clip ? <>
            <div className="work-header">
              <label className="eyebrow" htmlFor="clip-title">Название клипа</label>
              <input id="clip-title" className="title" value={title} maxLength={120} placeholder="Без названия"
                disabled={loading} onChange={(event) => {
                  draft.current.title = event.target.value;
                  setTitle(event.target.value);
                  if (!competing.current) setStatus("pending");
                  schedule();
                }} onBlur={() => { if (dirty()) void persist(); }}/>
            </div>
            {conflict && <section className="notice conflict-box" aria-labelledby="conflict-title">
              <h2 id="conflict-title" tabIndex={-1} ref={conflictHeading}>Клип изменён в другой вкладке</h2>
              <p>Здесь остаётся ваш черновик. Сравните его с редакцией на сервере.</p>
              <details open><summary>На сервере · {conflict.title.trim() || "Без названия"}</summary>
                <pre>{conflict.text || "Пустой текст"}</pre>
              </details>
              <div className="actions">
                <button className="subtle" onClick={() => resolveConflict(false)}>Открыть сохранённую</button>
                <button className="primary" onClick={() => resolveConflict(true)}>Сохранить мой черновик</button>
              </div>
            </section>}
            <VoicePanel key={"voice-" + clip.id} voice={voice} selectVoice={(id) => {
                draft.current.voice = id;
                setVoice(id);
                if (!competing.current) setStatus("pending");
                schedule();
              }}/>
            <ExportPanel key={"export-" + clip.id} clip={clip} saved={status === "saved" && !loading}
              locate={(id) => {
                const span = segments.find((item) => item.segment_id === id);
                if (span) locate(span, span.segment_id);
              }}/>
            <TakesPanel key={"takes-" + clip.id} clip={clip} voice={voice} segments={segments}
              segment={segments.find((span) => span.segment_id === active) ?? null}
              marked={marked.filter((id) => segments.some((span) => span.segment_id === id))}
              saved={status === "saved" && !loading} locate={locate}/>
            {currentPlan && clip.speech_ready.length > 0 && <details className="speech-ready" open>
              <summary>Подготовка к озвучке · {clip.speech_ready.length}</summary>
              <SpeechWarnings warnings={clip.speech_ready} locate={locate}/>
            </details>}
            <div className="editor-grid">
              <section className="editor" aria-labelledby="text-heading">
                <div className="pane-head">
                  <h2 id="text-heading"><label htmlFor="clip-text">Текст</label></h2>
                  <span>{points.length.toLocaleString("ru-RU")} зн.</span>
                </div>
                <textarea ref={textarea} id="clip-text" value={text} placeholder="Вставьте или напишите текст…"
                  disabled={loading} onChange={(event) => editText(event.target.value)}
                  onCompositionStart={() => { composing.current = true; }}
                  onCompositionEnd={() => { composing.current = false; schedule(); }}
                  onBlur={() => { if (dirty()) void persist(); }}/>
                <div ref={ruler} aria-hidden="true" className="ruler"/>
              </section>
              <section className="segments" aria-labelledby="segment-heading">
                <div className="pane-head">
                  <h2 id="segment-heading">Сегменты</h2>
                  {currentPlan && <span>{segments.length}</span>}
                </div>
                {segments.length > 0 ? <ol>{segments.map((span, index) =>
                  <li key={span.segment_id}>
                    <input type="checkbox" aria-label={"Включить сегмент " + (index + 1) + " в генерацию"}
                      checked={marked.includes(span.segment_id)} onChange={(event) => {
                        const checked = event.target.checked;
                        setMarked((ids) => checked ? [...ids, span.segment_id] : ids.filter((id) => id !== span.segment_id));
                      }}/>
                    <button aria-pressed={active === span.segment_id} onClick={() => locate(span, span.segment_id)}>
                      <span className="number">{String(index + 1).padStart(2, "0")}</span>
                      <span>{points.slice(span.start, span.end).join("")}</span>
                    </button>
                  </li>)}</ol> : <p className="hint">
                    {currentPlan ? "Вставьте текст, чтобы увидеть его сегменты." :
                      conflict ? "Разрешите конфликт, чтобы обновить сегменты." :
                        status === "error" ? "Повторите сохранение, чтобы обновить сегменты." : "Обновляем сегменты…"}
                  </p>}
              </section>
            </div>
          </> : !loading && !notice && <section className="welcome">
            <span className="mark large" aria-hidden="true"><i/><i/><i/><i/></span>
            <h1>Первый клип</h1>
            <p>Создайте клип и вставьте текст. Редактор выделит сегменты и сохранит правки.</p>
            <button className="primary" onClick={() => void open()}>＋ Новый клип</button>
          </section>}
          {loading && !clip && <p className="hint" role="status">Открываем клипы…</p>}
        </main>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App/>);
