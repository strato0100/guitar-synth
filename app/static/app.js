"use strict";

const state = {
  ws: null,
  settings: {},
  presets: {},
  instruments: [],
  categories: [],
  status: {},
  library: { dirs: [], engines: [] },
  category: null,
  search: "",
  signature: null,
  dragging: new Set(),   // sliders que el usuario está moviendo
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// --- conexión --------------------------------------------------------

let retryDelay = 500;

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;

  ws.onopen = () => {
    retryDelay = 500;
    $("#conn").className = "dot on";
  };
  ws.onclose = () => {
    $("#conn").className = "dot off";
    $("#status-text").textContent = "Sin conexión, reintentando…";
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 5000);
  };
  ws.onmessage = (event) => handleMessage(JSON.parse(event.data));
}

function send(message) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(message));
  }
}

function setValues(values) {
  Object.assign(state.settings, values);
  send({ type: "set", values });
}

function handleMessage(msg) {
  switch (msg.type) {
    case "init":
      state.instruments = msg.instruments;
      state.categories = msg.categories;
      state.presets = msg.presets;
      state.settings = msg.settings;
      state.status = msg.status;
      state.library = msg.library;
      renderCategories();
      renderEngines();
      renderPresets();
      renderSettings();
      renderStatus();
      break;
    case "settings":
      state.settings = msg.settings;
      renderSettings();
      break;
    case "library":
      state.library = msg;
      renderEngines();
      break;
    case "notice":
      toast(msg.text, msg.error);
      break;
    case "presets":
      state.presets = msg.presets;
      renderPresets();
      break;
    case "status":
      state.status = msg;
      renderStatus();
      break;
    case "live":
      renderLive(msg);
      break;
    case "error":
      toast(msg.message, true);
      break;
  }
}

// --- panel en vivo -----------------------------------------------------

function renderLive(live) {
  const name = $("#live-name");
  const sub = $("#live-sub");
  const cents = $("#cents");

  if (live.mode === "poly") {
    cents.classList.add("hidden");
    const notes = live.notes || [];
    if (!notes.length) {
      name.textContent = "—";
      name.className = "live-name idle";
      sub.textContent = "Toca un acorde…";
    } else {
      const chord = live.chord;
      name.textContent = chord ? chord.name : "?";
      name.className = "live-name" + (chord ? (chord.exact ? "" : " approx") : " unknown");
      sub.textContent = notes.join(" ");
    }
  } else {
    if (live.note) {
      name.textContent = live.note;
      name.className = "live-name";
      sub.textContent = live.freq ? `${live.freq.toFixed(1)} Hz` : "";
      cents.classList.remove("hidden");
      const c = live.cents == null ? 0 : Math.max(-50, Math.min(50, live.cents));
      const needle = $("#cents-needle");
      needle.style.left = `${50 + c}%`;
      needle.style.background = Math.abs(c) <= 5 ? "var(--green)"
        : Math.abs(c) <= 15 ? "var(--yellow)" : "var(--red)";
    } else {
      name.textContent = "—";
      name.className = "live-name idle";
      sub.textContent = "Toca una nota…";
      cents.classList.add("hidden");
    }
  }

  const loading = !!live.loading;
  $("#loading").classList.toggle("gone", !loading);
  $("#engine-loading").textContent = loading ? "cargando…" : "";

  const synth = live.synth || [];
  const transpose = state.settings.transpose || 0;
  $("#live-synth").textContent = synth.length && transpose ? `→ synth: ${synth.join(" ")}` : "";

  // Medidor de entrada: -80 dB .. 0 dB
  const db = live.level_db == null ? -180 : live.level_db;
  const fraction = Math.max(0, Math.min(1, (db + 80) / 80));
  const fill = $("#meter-fill");
  fill.style.width = `${fraction * 100}%`;
  fill.style.background = db > -3 ? "var(--red)" : db > -12 ? "var(--yellow)" : "var(--green)";
  $("#meter-db").textContent = db < -120 ? "-∞ dB" : `${db.toFixed(0)} dB`;
}

function renderStatus() {
  const s = state.status;
  const parts = [];
  if (s.error) parts.push(`⚠ ${s.error}`);
  if (!s.synth_running && !s.loading) parts.push("⚠ Motor de sonido detenido");
  parts.push(s.modep ? "MODEP activo" : "Guitarra limpia");
  if (s.samplerate) parts.push(`${s.samplerate / 1000} kHz`);
  if (s.cpu_load != null) parts.push(`CPU ${Math.round(s.cpu_load)}%`);
  if (s.xruns_recent) parts.push(`⚠ ${s.xruns_recent} cortes en 10 s`);
  else if (s.xruns) parts.push(`${s.xruns} cortes en total`);
  $("#status-text").textContent = parts.join(" · ");
  const advice = $("#advice");
  advice.textContent = s.advice ? `⚠ ${s.advice}` : "";
  advice.classList.toggle("gone", !s.advice);
  if (s.backend_log) $("#engine-log").textContent = s.backend_log.length ? s.backend_log.join("\n") : "(vacío)";
  $("#status-text").title = `Buffer JACK: ${s.buffer || "?"} muestras · cortes (xruns): ${s.xruns || 0}` +
    (s.xruns ? ` · el peor, ${s.worst_xrun_ms} ms` : "");
  $("#footer").textContent = `Entrada ${s.capture_port || "?"} · ${s.engine_label || "sin motor"}` +
    (s.sound ? ` · ${s.sound}` : "");
}

// --- instrumentos --------------------------------------------------------

function normalize(text) {
  return text.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
}

function renderCategories() {
  const box = $("#categories");
  box.innerHTML = "";
  const all = [{ name: null, icon: "", label: "Todos" }]
    .concat(state.categories.map((c) => ({ name: c.name, icon: c.icon, label: c.name })));
  for (const c of all) {
    const b = document.createElement("button");
    b.textContent = `${c.icon} ${c.label}`.trim();
    b.classList.toggle("active", state.category === c.name);
    b.onclick = () => {
      state.category = c.name;
      renderCategories();
      renderInstruments();
    };
    box.appendChild(b);
  }
}

function currentEngine() {
  return state.library.engines.find((e) => e.id === state.settings.engine) || null;
}

function soundName(engine, id) {
  const found = engine && engine.sounds.find((x) => x.id === id);
  return found ? found.name : (id || "").split("/").pop().replace(/\.[^.]+$/, "");
}

function renderEngines() {
  const box = $("#engines");
  box.innerHTML = "";
  for (const e of state.library.engines) {
    const b = document.createElement("button");
    b.textContent = e.available ? e.label : `${e.label} · no instalado`;
    b.title = e.available ? `${e.sounds.length} archivo(s) .${e.kind}` : e.hint;
    b.classList.toggle("active", e.id === state.settings.engine);
    b.classList.toggle("unavailable", !e.available);
    b.onclick = () => {
      if (!e.available) {
        toast(e.hint, true);
        return;
      }
      setValues({ engine: e.id });
      renderEngines();
    };
    box.appendChild(b);
  }
  renderEngineUI();
}

function renderEngineUI() {
  const engine = currentEngine();
  if (!engine) return;

  // Aviso cuando falta el programa o no hay archivos
  const hint = $("#engine-hint");
  const folders = state.library.dirs.join(", ");
  const missing = state.library.engines.filter((e) => !e.available);
  if (!engine.available) hint.textContent = engine.hint;
  else if (missing.length) hint.textContent = missing.map((e) => `${e.label}: ${e.hint}`).join(" · ");
  else if (!engine.sounds.length) hint.textContent = `No hay archivos .${engine.kind}. Copia alguno a: ${folders}`;
  else hint.textContent = engine.gm
    ? "Los 128 instrumentos General MIDI del banco elegido."
    : "Cada archivo SFZ es un instrumento completo.";

  // Selector de banco: sólo cuando el motor usa SoundFonts
  const bankRow = $("#bank-row");
  bankRow.classList.toggle("gone", engine.kind !== "sf2");
  const select = $("#sound-select");
  select.innerHTML = "";
  for (const sound of engine.sounds) {
    const o = document.createElement("option");
    o.value = sound.id;
    o.textContent = sound.size_mb ? `${sound.name} (${sound.size_mb} MB)` : sound.name;
    select.appendChild(o);
  }
  select.value = state.settings[engine.setting] || "";

  $("#categories").classList.toggle("gone", !engine.gm);
  $("#search").classList.toggle("gone", false);
  $("#search").placeholder = engine.gm ? "Buscar: sax, violín, organ, pad…" : "Buscar instrumento…";
  renderInstruments();
}

function renderInstruments() {
  const grid = $("#instruments");
  grid.innerHTML = "";
  const engine = currentEngine();
  const q = normalize(state.search.trim());

  if (engine && !engine.gm) {
    for (const sound of engine.sounds) {
      if (q && !normalize(`${sound.name} ${sound.folder}`).includes(q)) continue;
      const b = document.createElement("button");
      b.className = "instrument";
      b.dataset.sound = sound.id;
      b.innerHTML = `<span class="num"></span><span></span>`;
      b.children[0].textContent = sound.folder;
      b.children[1].textContent = sound.name;
      b.onclick = () => {
        setValues({ [engine.setting]: sound.id });
        markInstrument();
      };
      grid.appendChild(b);
    }
    markInstrument();
    return;
  }

  let lastCategory = null;
  for (const inst of state.instruments) {
    if (state.category && inst.category !== state.category) continue;
    if (q && !normalize(`${inst.name} ${inst.en} ${inst.category} ${inst.program + 1}`).includes(q)) continue;

    if (!state.category && !q && inst.category !== lastCategory) {
      const h = document.createElement("h3");
      h.className = "category-title";
      h.textContent = `${inst.icon} ${inst.category}`;
      grid.appendChild(h);
    }
    lastCategory = inst.category;

    const b = document.createElement("button");
    b.className = "instrument";
    b.dataset.program = inst.program;
    b.innerHTML = `<span class="num">${inst.program + 1}</span>
                   <span>${inst.name}</span><span class="en">${inst.en}</span>`;
    b.onclick = () => {
      setValues({ program: inst.program });
      renderSettings();
    };
    grid.appendChild(b);
  }
  markInstrument();
}

function markInstrument() {
  const engine = currentEngine();
  for (const b of $$(".instrument")) {
    const active = engine && !engine.gm
      ? b.dataset.sound === state.settings[engine.setting]
      : Number(b.dataset.program) === state.settings.program;
    b.classList.toggle("active", active);
  }
  let label = "—";
  if (engine && !engine.gm) {
    label = `🎼 ${soundName(engine, state.settings[engine.setting])}`;
  } else {
    const inst = state.instruments[state.settings.program];
    if (inst) label = `${inst.icon} ${inst.name}`;
  }
  $("#live-instrument").textContent = label;
}

// --- ajustes -------------------------------------------------------------

function formatValue(key, value) {
  if (key === "room" || key === "confidence" || key === "salience") return Number(value).toFixed(2);
  if (key === "gate_db") return `${value} dB`;
  if (key === "onset_db") return `${value} dB`;
  if (key === "poly_hold") return `${value} ms`;
  if (key === "a4") return `${Number(value).toFixed(1)} Hz`;
  return String(value);
}

function buildSliders() {
  for (const box of $$(".control.slider")) {
    const key = box.dataset.key;
    box.innerHTML = `<label>${box.dataset.label} <span class="value"></span></label>
      <input type="range" min="${box.dataset.min}" max="${box.dataset.max}" step="${box.dataset.step}">
      ${box.dataset.help ? `<small class="muted">${box.dataset.help}</small>` : ""}`;
    const input = box.querySelector("input");
    const label = box.querySelector(".value");
    let timer = null;

    input.addEventListener("input", () => {
      state.dragging.add(key);
      label.textContent = formatValue(key, input.value);
      if (key === "gate_db") updateGateMarker(Number(input.value));
      clearTimeout(timer);
      timer = setTimeout(() => setValues({ [key]: Number(input.value) }), 60);
    });
    input.addEventListener("change", () => {
      clearTimeout(timer);
      setValues({ [key]: Number(input.value) });
      state.dragging.delete(key);
    });
  }
}

function updateGateMarker(db) {
  $("#meter-gate").style.left = `${Math.max(0, Math.min(1, (db + 80) / 80)) * 100}%`;
}

function renderSettings() {
  const s = state.settings;

  for (const box of $$(".control.slider")) {
    const key = box.dataset.key;
    if (state.dragging.has(key) || s[key] === undefined) continue;
    box.querySelector("input").value = s[key];
    box.querySelector(".value").textContent = formatValue(key, s[key]);
  }
  for (const seg of $$(".segmented")) {
    for (const b of seg.querySelectorAll("button")) {
      b.classList.toggle("active", b.dataset.value === s[seg.dataset.key]);
    }
  }
  $("#bend").checked = !!s.bend_enabled;

  const t = s.transpose || 0;
  const octaves = t % 12 === 0 && t !== 0 ? ` (${t > 0 ? "+" : ""}${t / 12} oct)` : "";
  $("#transpose-label").textContent = `${t > 0 ? "+" : ""}${t} st${octaves}`;

  updateGateMarker(s.gate_db);
  // Reconstruir la lista de instrumentos sólo si cambia algo que la afecta
  const signature = [s.engine, s.sf2, s.sfz, s.program].join("|");
  if (signature !== state.signature && state.library.engines.length) {
    state.signature = signature;
    renderEngines();
  } else {
    markInstrument();
  }
}

// --- presets -------------------------------------------------------------

function describePreset(p) {
  const engine = state.library.engines.find((e) => e.id === p.engine);
  let first;
  if (engine && !engine.gm) first = soundName(engine, p[engine.setting]);
  else first = (state.instruments[p.program] || { name: `Programa ${p.program}` }).name;
  const parts = [first];
  if (engine && engine.gm && p.sf2) parts.push(soundName(engine, p.sf2));
  if (p.mode) parts.push(p.mode === "poly" ? "acordes" : "notas");
  if (p.transpose) parts.push(`${p.transpose > 0 ? "+" : ""}${p.transpose} st`);
  if (p.bend_enabled) parts.push("bends y vibrato");
  return parts.join(" · ");
}

function renderPresets() {
  const list = $("#presets");
  list.innerHTML = "";
  const q = normalize(($("#preset-search").value || "").trim());
  const names = Object.keys(state.presets)
    .filter((n) => !q || normalize(`${n} ${describePreset(state.presets[n])}`).includes(q));
  // Los que no tienen grupo ("Grupo · Nombre") son del usuario y van primero
  const groupOf = (n) => (n.includes(" · ") ? n.split(" · ")[0] : "Mis presets");
  names.sort((a, b) => {
    const ga = groupOf(a), gb = groupOf(b);
    if (ga !== gb) return ga === "Mis presets" ? -1 : gb === "Mis presets" ? 1 : ga.localeCompare(gb, "es");
    return a.localeCompare(b, "es");
  });
  if (!names.length) {
    list.innerHTML = `<li class="muted">${q ? "Ningún preset coincide." : "No hay presets guardados."}</li>`;
    return;
  }
  let lastGroup = null;
  for (const name of names) {
    const group = groupOf(name);
    if (group !== lastGroup) {
      const h = document.createElement("li");
      h.className = "preset-group";
      h.textContent = group;
      list.appendChild(h);
      lastGroup = group;
    }
    const li = document.createElement("li");
    const info = document.createElement("div");
    info.className = "info";
    info.innerHTML = `<div></div><div></div>`;
    info.children[0].textContent = name.includes(" · ") ? name.split(" · ").slice(1).join(" · ") : name;
    info.children[1].textContent = describePreset(state.presets[name]);

    const load = document.createElement("button");
    load.className = "primary";
    load.textContent = "Cargar";
    load.onclick = () => {
      send({ type: "preset.load", name });
      toast(`Preset «${name}» cargado`);
    };

    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "✕";
    del.title = "Borrar";
    del.onclick = () => {
      if (confirm(`¿Borrar el preset «${name}»?`)) send({ type: "preset.delete", name });
    };

    li.append(info, load, del);
    list.appendChild(li);
  }
}

// --- utilidades ----------------------------------------------------------

let toastTimer = null;
function toast(text, error = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast show" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 2500);
}

// --- eventos -------------------------------------------------------------

function setup() {
  buildSliders();

  for (const seg of $$(".segmented")) {
    for (const b of seg.querySelectorAll("button")) {
      b.onclick = () => {
        setValues({ [seg.dataset.key]: b.dataset.value });
        renderSettings();
      };
    }
  }

  for (const b of $$(".tabs button")) {
    b.onclick = () => {
      for (const other of $$(".tabs button")) other.classList.toggle("active", other === b);
      for (const panel of $$(".tab-panel")) {
        panel.classList.toggle("active", panel.id === `tab-${b.dataset.tab}`);
      }
    };
  }

  for (const b of $$("[data-transpose]")) {
    b.onclick = () => {
      const step = Number(b.dataset.transpose);
      const value = step === 0 ? 0 : Math.max(-36, Math.min(36, (state.settings.transpose || 0) + step));
      setValues({ transpose: value });
      renderSettings();
    };
  }

  $("#sound-select").onchange = (e) => {
    const engine = currentEngine();
    if (engine && e.target.value) setValues({ [engine.setting]: e.target.value });
  };
  $("#rescan").onclick = () => send({ type: "library.rescan" });

  $("#preset-search").addEventListener("input", renderPresets);

  $("#bend").onchange = (e) => setValues({ bend_enabled: e.target.checked });

  $("#search").addEventListener("input", (e) => {
    state.search = e.target.value;
    renderInstruments();
  });

  $("#preset-form").onsubmit = (e) => {
    e.preventDefault();
    const name = $("#preset-name").value.trim();
    if (!name) return;
    if (state.presets[name] && !confirm(`Ya existe «${name}». ¿Sobrescribir?`)) return;
    send({ type: "preset.save", name });
    $("#preset-name").value = "";
    toast(`Preset «${name}» guardado`);
  };

  $("#detection-reset").onclick = () => {
    if (confirm("¿Restablecer los ajustes de detección a sus valores por defecto?")) {
      send({ type: "detection.reset" });
    }
  };

  $("#panic").onclick = () => {
    send({ type: "panic" });
    toast("Todas las notas apagadas");
  };

  connect();
}

setup();
