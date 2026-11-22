const phrases = [
  { name: "Открытие станции", text: "Новая станция открылась сегодня утром. Первые пассажиры приехали к началу движения." },
  { name: "Расписание поездов", text: "Поезда ходят с шести утра до полуночи. По выходным интервалы немного больше." },
  { name: "Доступ к платформам", text: "К платформам ведёт крытая галерея. Для пассажиров с колясками работают два лифта." }
];
const example = phrases.map(item => item.text).join("\n\n");
const presets = {
  "Спокойный диктор": "Calm Russian narrator, clear articulation, measured pace.",
  "Спокойный репортаж": "Russian field reporter, warm conversational tone, natural pauses.",
  "Нейтральный рассказчик": "Neutral Russian narrator, even pace, clear pronunciation."
};
const labels = { prep: "Подготовка", studio: "Озвучка", export: "Экспорт" };
const stages = Object.keys(labels);
const root = document.querySelector("#app");
let concept, view, state, timer;

function reset(scene = "empty") {
  clearTimeout(timer);
  view = scene === "export" ? "export" : scene === "work" ? "studio" : "prep";
  state = {
    name: scene === "empty" ? "Новый клип" : "Новая станция",
    text: scene === "empty" ? "" : example,
    voice: ["work", "export"].includes(scene) ? "Спокойный диктор" : "",
    voiceOpen: false, mode: "design", preview: false, previewPlayed: false,
    preset: "Спокойный диктор", description: presets["Спокойный диктор"],
    synthesized: ["work", "export"].includes(scene), busy: false,
    selection: 1, playing: 0, listened: false, accepted: scene === "export",
    current: 1, variant: ["work", "export"].includes(scene), showAll: false, filter: false, warningOK: false,
    format: "MP3", draft: false, built: scene === "export", issue: ""
  };
}
const escape = (value) => String(value).replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
})[c]);
const button = (label, action, options = "") => `<button type="button" data-action="${action}" ${options}>${label}</button>`;
const findings = () => state.text.match(/[0-9]+(?:[.,:]\d+)*(?:[ \u00a0]*кг)?|(?<!\p{L})(?:НИИ|МУП)(?!\p{L})/gu) ?? [];
const segmentItems = () => state.text.trim() === example ? phrases : state.text.trim().split(/\n+/)
  .filter(Boolean).map((text, index) => ({ name: "Фрагмент " + (index + 1), text }));
const ready = () => state.synthesized && (segmentItems().length < 2 || state.accepted || state.current === 2);
const needsReview = () => state.synthesized && segmentItems().length >= 2 && !ready();
const player = (caption, action, seconds = "00:08", running = false) =>
  `<div class="player"><strong class="small">${caption}</strong>
    <div class="player-row">${button(running ? "Пауза" : "Слушать", action)}
    <progress value="${running ? 35 : 0}" max="100" aria-label="Пример позиции"></progress>
    <time>${running ? "00:03" : "00:00"} / ${seconds}</time></div></div>`;

function voiceForm() {
  if (!state.voiceOpen) return "";
  return `<div class="voice-surface stack" id="voice-form">
    <div class="inline" aria-label="Способ создания">${button("По описанию", "design", `aria-pressed="${state.mode === "design"}"`)}
      ${button("По образцу WAV", "clone", `aria-pressed="${state.mode === "clone"}"`)}</div>
    ${state.mode === "design" ? `
      <label class="input">Пресет<select id="preset">${Object.keys(presets).map(name =>
        `<option ${name === state.preset ? "selected" : ""}>${name}</option>`).join("")}</select></label>
      <details><summary>Описание и расширенные настройки</summary>
      <label class="input">Описание голоса
        <textarea id="voice-description">${escape(state.description)}</textarea>
      </label><p class="small muted">Рекомендуем описание на английском. Фраза для пробы остаётся русской.</p>
      <label class="input">Seed<input class="field" type="number" value="42"></label></details>` : `
      <label class="input">Образец голоса <input type="file" accept=".wav,audio/wav" id="voice-wav"></label>
      <p class="small muted">WAV до 30 секунд и 16 МиБ. Подойдёт чистая речь одного человека.</p>
      <details><summary>Расширенные настройки</summary><label class="input">Seed<input class="field" type="number" value="42"></label></details>`}
    <label class="input">Фраза для пробы<input class="field" value="Добрый день. Начинаем выпуск."></label>
    ${state.preview ? player("Проба голоса", "listen-preview", "00:03", state.previewPlaying) : ""}
    ${state.issue ? `<p role="alert" class="hint warn">${escape(state.issue)}</p>` : ""}
    <div class="inline">${state.preview ? button("Использовать", "use-voice", `class="primary" ${state.previewPlayed ? "" : "disabled"}`) : button("Создать пробу", "preview", 'class="primary"')}
    ${button(state.preview ? "Изменить пробу" : "Закрыть", "preview-back")}</div>
  </div>`;
}

function prepFeedback() {
  const warnings = findings();
  return state.text.trim() ? warnings.length ?
    `<div class="hint warn guide" role="status">Подготовка текста · замечаний ${warnings.length}
      <div class="inline">${[...new Set(warnings)].slice(0, 6).map(value =>
        button(escape(value), "locate", `data-fragment="${escape(value)}"`)).join("")}</div>
      <p class="small">Запишите числа и сокращения так, как их следует произнести.</p></div>` :
    '<p class="hint guide" role="status">Текст готов к озвучке · письменные формы не найдены</p>' :
    '<p class="muted guide">Запишите числа, даты и сокращения словами до озвучки.</p>';
}

function nextPrep() {
  return !state.text.trim() ? "Сначала добавьте текст клипа." :
    !state.voice ? "Выберите сохранённый голос или создайте пробу." : "Теперь можно озвучить сегменты.";
}

function preparation() {
  return `<div class="prep">
    <div><div class="field-head"><label for="clip-text">Русский текст</label>
      ${button("Вставить пример", "example")}</div>
      <textarea id="clip-text" class="script" placeholder="Вставьте готовый русский перевод…">${escape(state.text)}</textarea>
      <div id="prep-feedback">${prepFeedback()}</div></div>
    <div class="stack"><h3>Голос клипа</h3>
      <label class="input">Сохранённые голоса<select id="voice">
        <option value="" ${state.voice ? "" : "selected"}>Выберите голос</option>
        <option ${state.voice ? "selected" : ""}>${escape(state.voice || "Спокойный диктор")}</option></select></label>
      ${state.voiceOpen ? voiceForm() : `<div>${button("Создать голос", "voice-open")}</div>`}
    </div></div>
    <div class="stage-foot"><p class="muted" id="prep-next">${nextPrep()}</p>
      ${button("К озвучке", "stage-studio", state.text.trim() && state.voice ? 'class="primary"' : "")}</div>`;
}

function take(number, needsCheck, active) {
  const accepted = number === 1 && state.accepted;
  const status = accepted ? "Принят" : needsCheck ? "Проверьте точность произнесения" : "Проверен";
  return `<div class="take"><div class="take-line"><div>
      <h3>Дубль ${number}${active ? '<span class="active-label">Выбран</span>' : ""}</h3>
      <p class="${needsCheck && !accepted ? "warn-text" : ""}">${status}</p></div>
      <div class="inline">${button("Слушать", "play-take", `data-take="${number}" aria-label="Слушать дубль ${number}"`)}
      ${active ? "" : button("Выбрать", "select-take", `data-take="${number}"`)}</div>
    </div>
    ${needsCheck ? `<details><summary>Проверка и решение</summary>
      <p>В расшифровке «до полудня», в тексте «до полуночи». Сверьте с записью.</p>
      <div class="inline">${button("Прослушал", "mark", state.listened || !state.playing ? "disabled" : "")}
      ${button("Принять", "accept", `class="primary" ${!state.listened || accepted ? "disabled" : ""}`)}</div>
      <details><summary>Технические сведения</summary><p>Whisper · полнота подтверждена. Seed 42.</p></details>
    </details>` : ""}</div>`;
}

function inspector(items) {
  const i = Math.min(state.selection, items.length - 1);
  const row = items[i];
  if (!row) return "";
  const isReview = items.length > 1 && i === 1;
  const playing = state.playing || (isReview ? state.current : 1);
  return `<article id="segment-focus" tabindex="-1" class="inspector" aria-label="Активный сегмент">
    <div class="inspector-top"><h3>Сегмент ${i + 1} из ${items.length} · ${escape(row.name)}</h3>
      ${button("К тексту", "back-text")}</div>
    <blockquote>${escape(row.text)}</blockquote>
    ${state.busy ? '<p role="status" class="hint">Озвучиваем. Готовые дубли появятся здесь.</p>' : !state.synthesized ?
      '<p class="muted">Здесь появится плеер после озвучки сегмента.</p>' :
      `${player("Сегмент " + (i + 1) + " · Дубль " + playing, "toggle-playing", "00:08", Boolean(state.playing))}
      <div><div class="field-head"><h3>Варианты озвучки</h3>
      ${isReview ? button("Новый вариант", "variant") : ""}</div>
        ${take(1,isReview,!isReview || state.current === 1)}${isReview && state.variant ? take(2,false,state.current === 2) : ""}
      </div>`}
  </article>`;
}

function studio() {
  const items = segmentItems();
  if (!items.length || !state.voice) return `<div class="empty stack">
    <p>${items.length ? "Выберите голос в подготовке, чтобы озвучить текст." : "Добавьте русский текст. Сегменты появятся после сохранения."}</p>
    <div>${button("К подготовке", "stage-prep", 'class="primary"')}</div></div>`;
  const index = Math.min(state.selection, items.length - 1);
  const rows = items.map((row, i) => {
    if (state.filter && needsReview() && i !== 1) return "";
    const status = !state.synthesized ? "Нет записи" : i === 1 && needsReview() ? "Требует прослушивания" : "Готов к экспорту";
    return `<li class="segment"><button class="segment-row" data-action="segment" data-segment="${i}" aria-pressed="${i === index}">
      <span class="segment-index">${String(i + 1).padStart(2,"0")}</span><span><span class="segment-title">${escape(row.name)}</span>
      <span class="segment-caption">${status}</span></span></button>
      ${concept === "b" && i === index ? inspector(items) : ""}</li>`;
  }).join("");
  const warnings = findings().length && state.issue === "warnings";
  return `<header class="stage-head"><div>${concept === "a" ? "<h2>Озвучка</h2>" : ""}<p class="muted">${state.synthesized ?
    `${items.length} из ${items.length} озвучены${needsReview() ? " · 1 требует решения" : ""}` : `Сегментов в клипе · ${items.length}`}</p></div>
    <div class="inline">${state.synthesized ?
      button(needsReview() ? "К замечанию" : "К экспорту", needsReview() ? "review" : "stage-export", 'class="primary"') :
      button(state.busy ? "Озвучиваем…" : "Озвучить недостающие", "synthesize", `class="primary" ${state.busy ? "disabled" : ""}`)}
    ${state.busy ? button("Остановить", "stop") : ""}</div>
    ${warnings ? `<div class="hint warn"><p>В тексте остались письменные формы.</p>
      <div class="inline">${button("Проверить текст", "stage-prep")}${button("Озвучить с предупреждениями", "allow", 'class="primary"')}</div></div>` : ""}</header>
    <div class="studio"><aside class="segment-list ${state.showAll ? "show-all" : ""}" aria-label="Список сегментов">
      <h3>Сегменты ${concept === "a" ? button(state.showAll ? "Свернуть список" : "Открыть список", "list",
        `class="narrow" aria-expanded="${state.showAll}" aria-controls="segment-list"`) : ""}</h3>
      ${needsReview() ? `<label class="small"><input id="only-problems" type="checkbox" ${state.filter ? "checked" : ""}> Только с замечаниями</label>` : ""}
      <ol id="segment-list">${rows}</ol></aside>${concept === "a" ? inspector(items) : ""}</div>`;
}

function exporting() {
  if (!state.synthesized) return `<div class="empty stack"><p>Сначала озвучьте клип. Здесь появится готовность сборки.</p>
    <div>${button("К озвучке", "stage-studio", 'class="primary"')}</div></div>`;
  const admitted = ready();
  const build = admitted || state.draft;
  const items = segmentItems();
  return `<header class="stage-head"><div><h2>Экспорт клипа</h2>
    <p role="status" class="muted">${admitted ? `Готов к выпуску · ${items.length} из ${items.length} сегментов допущены` :
      `Для выпуска требуется решение по сегменту 2`}</p></div></header>
    <div class="export-grid"><div class="stack"><h3>Сборка</h3>
      <label class="input">Режим<select id="mode-export"><option value="release" ${state.draft ? "" : "selected"}>Выпуск</option>
        <option value="draft" ${state.draft ? "selected" : ""}>Черновик с замечаниями</option></select></label>
      <label class="input">Формат<select id="format"><option ${state.format === "MP3" ? "selected" : ""}>MP3</option><option ${state.format === "WAV" ? "selected" : ""}>WAV</option></select></label>
      <details><summary>Паузы и состав</summary><label class="input">В начале, мс<input class="field" type="number" value="0"></label>
        <label class="input">Между сегментами, мс<input class="field" type="number" value="150"></label>
        <ul>${items.map(x => `<li>${escape(x.name)}</li>`).join("")}</ul></details>
      <div>${button(state.built ? "Собрать снова" : "Собрать " + state.format, "build",
        `${state.built ? "" : 'class="primary"'} ${build ? "" : "disabled"}`)}</div>
    </div><div class="stack"><h3>${state.built ? "Готовый файл" : admitted ? "Полный клип" : "Требует внимания"}</h3>
      ${!admitted ? `<div class="hint warn"><p>${state.draft ? "Черновик сохранит замечание" : "Проверьте точность произнесения"} · сегмент 2.</p>
       ${button("Открыть сегмент 2", "review")}</div>` : ""}
      ${state.built ? `${player("Полный клип · " + (state.draft ? "Черновик" : "Выпуск"), "listen-export", "00:24", state.exportPlaying)}
        <p class="small muted">Пример результата · файлы для скачивания показаны в макете.</p>
        <div class="inline"><button class="primary" disabled>Скачать ${state.format}</button>${state.format === "MP3" ? "<button disabled>Скачать WAV</button>" : ""}</div>
        <details><summary>История экспортов</summary><p>Текущий снимок · ${state.format}</p></details>` :
      admitted ? '<p class="muted">После сборки здесь можно прослушать клип и скачать файл.</p>' : ""}
    </div></div>`;
}

function render(focus = "") {
  document.body.dataset.concept = concept;
  document.querySelectorAll("[data-concept]").forEach(el => {
    if (el.tagName === "BUTTON") el.setAttribute("aria-pressed", el.dataset.concept === concept);
  });
  const content = { prep: preparation, studio, export: exporting };
  const live = { prep: state.text.trim() ? (findings().length ? "Есть замечания" : state.voice ? "Текст и голос готовы" : "Выберите голос") :
    "Добавьте текст", studio: state.synthesized ? (needsReview() ? "1 требует решения" : "Озвучка готова") : "Ожидает озвучки",
    export: state.built ? "Файл готов" : ready() ? "Можно собрать" : "Ожидает озвучки и проверки" };
  root.innerHTML = `<div class="top"><div class="top-inner"><p class="wordmark">TTS Clips</p><span class="save" role="status">Сохранено в сеансе</span></div></div>
    <main class="workspace"><header class="clip-header"><div><h1><input class="clip-name" id="clip-name" aria-label="Название клипа"
      value="${escape(state.name)}" maxlength="120"></h1></div></header>
    ${concept === "a" ? `<nav class="stages" aria-label="Этапы клипа">${stages.map(stage =>
      button(labels[stage], "stage-" + stage, view === stage ? 'aria-current="page"' : "")).join("")}</nav>
      <section class="stage" id="work-stage" tabindex="-1" aria-label="${labels[view]}">${content[view]()}</section>` :
      stages.map(stage => `<section class="inline-stage" aria-label="${labels[stage]}">
        ${button(labels[stage] + ` <span>${live[stage]}</span>`, "stage-" + stage,
          `class="inline-stage-head" aria-expanded="${view === stage}" aria-controls="panel-${stage}"`)}
        <div id="panel-${stage}" ${view === stage ? "" : "hidden"}>${view === stage ? content[stage]() : ""}</div></section>`).join("")}
    </main>`;
  if (focus) document.querySelector(focus)?.focus({ preventScroll: false });
}

function go(stage, focus = true) {
  state.playing = 0;
  state.exportPlaying = false;
  view = stage;
  render(focus ? (concept === "a" ? "#work-stage" : `[data-action="stage-${stage}"]`) : "");
}

function fromHash() {
  const [letter, scene] = location.hash.slice(1).split("/");
  concept = letter === "a" ? "a" : "b";
  reset(!letter ? "work" : ["work", "prep", "export"].includes(scene) ? scene : "empty");
  render();
}
window.addEventListener("hashchange", fromHash);
fromHash();

document.addEventListener("change", event => {
  const input = event.target;
  if (input.id === "clip-name") state.name = input.value || "Новый клип";
  if (input.id === "clip-text" && state.text !== input.value) {
    state.text = input.value;
    state.synthesized = state.built = state.warningOK = state.accepted = false;
    state.current = 1;
    document.querySelector("#prep-feedback").innerHTML = prepFeedback();
    document.querySelector("#prep-next").textContent = nextPrep();
    document.querySelector(".stage-foot button").classList.toggle("primary", Boolean(state.text.trim() && state.voice));
  }
  if (input.id === "voice" && state.voice !== input.value) {
    state.voice = input.value;
    state.synthesized = state.built = state.accepted = false;
    render();
  }
  if (input.id === "preset") {
    state.preset = input.value;
    state.description = presets[state.preset];
    document.querySelector("#voice-description").value = state.description;
  }
  if (input.id === "voice-description") state.description = input.value;
  if (input.id === "only-problems") { state.filter = input.checked; if (state.filter) state.selection = 1; render("#only-problems"); }
  if (input.id === "mode-export") { state.draft = input.value === "draft"; state.built = false; render("#mode-export"); }
  if (input.id === "format") { state.format = input.value; state.built = false; render("#format"); }
});

document.addEventListener("click", event => {
  const el = event.target.closest("button");
  if (!el) return;
  if (el.dataset.concept) { concept = el.dataset.concept; render(); return; }
  const action = el.dataset.action;
  if (!action) return;
  if (action.startsWith("stage-")) {
    go(action.slice(6));
    return;
  }
  switch (action) {
    case "reset": reset(); render("#clip-text"); return;
    case "example": state.text = example; state.synthesized = state.built = state.accepted = false; state.current = 1; break;
    case "voice-open": state.voiceOpen = true; break;
    case "design": case "clone": state.mode = action; state.preview = state.previewPlayed = false; state.issue = ""; break;
    case "preview":
      if (state.mode === "clone" && !document.querySelector("#voice-wav").files.length) {
        state.issue = "Выберите WAV с образцом голоса.";
      } else {
        state.preview = true; state.previewPlayed = false; state.previewPlaying = false; state.issue = "";
      }
      break;
    case "preview-back": if (state.preview) state.preview = false; else state.voiceOpen = false; state.issue = ""; break;
    case "listen-preview": state.previewPlayed = true; state.previewPlaying = !state.previewPlaying; break;
    case "use-voice": state.voice = state.mode === "design" ? state.preset : "Голос по образцу";
      state.voiceOpen = false; state.synthesized = state.built = state.accepted = false; break;
    case "locate": {
      const editor = document.querySelector("#clip-text"), fragment = el.dataset.fragment;
      const start = editor.value.indexOf(fragment);
      editor.focus();
      editor.setSelectionRange(start, start + fragment.length);
      return;
    }
    case "allow": state.warningOK = true;
    // Confirmation runs the same demonstration as a clean text.
    case "synthesize":
      if (findings().length && !state.warningOK) { state.issue = "warnings"; break; }
      state.issue = ""; state.busy = true;
      timer = setTimeout(() => { state.busy = false; state.synthesized = true; render(); }, 500);
      break;
    case "stop": clearTimeout(timer); state.busy = false; break;
    case "segment": state.selection = Number(el.dataset.segment); state.playing = 0; state.listened = false;
      state.showAll = false; render("#segment-focus"); return;
    case "list": state.showAll = !state.showAll; break;
    case "back-text": {
      const rows = segmentItems();
      const phrase = rows[Math.min(state.selection, rows.length - 1)]?.text;
      go("prep", false);
      const editor = document.querySelector("#clip-text");
      editor.focus();
      if (phrase) {
        const position = editor.value.indexOf(phrase);
        if (position >= 0) editor.setSelectionRange(position, position + phrase.length);
      }
      return;
    }
    case "review": state.selection = 1; go("studio", false); document.querySelector("#segment-focus")?.focus(); return;
    case "play-take": state.playing = Number(el.dataset.take); break;
    case "toggle-playing": state.playing = state.playing ? 0 : state.selection === 1 ? state.current : 1; break;
    case "select-take": state.current = Number(el.dataset.take); state.built = false; break;
    case "mark": state.listened = true; render(); document.querySelector(".take details").open = true; return;
    case "accept": state.accepted = true; state.filter = false; break;
    case "variant": state.variant = true; state.playing = 2; break;
    case "build": state.built = true; break;
    case "listen-export": state.exportPlaying = !state.exportPlaying; break;
  }
  render();
});
