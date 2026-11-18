// FX Box UI. Plain JS, no dependencies, no build step: it must work offline on the Pi.
"use strict";

const SAMPLE_RATE = 48000;
const BUFFER_SIZES = [16, 32, 64, 128, 256, 512, 1024, 2048];
const HEALTH_POLL_MS = 2000;

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of kids) n.append(k);
  return n;
};

const state = {
  catalog: null,   // {effects: [...]}
  effects: {},     // id -> effect info
  presets: [],
  loaded: null,    // {id, name, buffer_size, modified}
  draft: null,     // {id|null, preset}
  dirty: false,
  lastXruns: null,
};

// ---------- API ----------

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `${r.status} ${r.statusText}`);
  return data;
}

// ---------- helpers ----------

const latencyMs = (buf) => (2 * buf) / SAMPLE_RATE * 1000 + 1;

function stepFor(spec) {
  const span = spec.max - spec.min;
  return span > 50 ? 1 : span > 5 ? 0.1 : 0.01;
}
function decimals(step) { return step >= 1 ? 0 : step >= 0.1 ? 1 : 2; }
function roundTo(v, step) { const d = decimals(step); return Number(v.toFixed(d)); }
function fmtNum(v) { return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(6))); }

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isError ? 6000 : 2500);
}

async function busy(btn, fn) {
  const was = btn.disabled;
  btn.disabled = true;
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { btn.disabled = was; }
}

// ---------- now playing + health ----------

function renderLoaded() {
  const l = state.loaded;
  $("#now-name").textContent = l ? l.name : "Nothing loaded";
  $("#now-chip").hidden = !(l && l.modified);
  document.querySelectorAll(".preset").forEach((li) => {
    const on = !!(l && !l.modified && li.dataset.id === l.id);
    li.classList.toggle("active", on);
    $(".chip.on", li).hidden = !on;
  });
}

function setTile(key, text, level = "") {
  const tile = $(`.tile[data-k="${key}"]`);
  $("dd", tile).textContent = text;
  tile.classList.toggle("warn", level === "warn");
  tile.classList.toggle("bad", level === "bad");
}

function renderHealth(h) {
  const badge = $("#engine-badge");
  badge.textContent = h.engine === "fake" ? "fake engine" : h.engine;
  badge.classList.toggle("fake", h.engine === "fake");

  setTile("buffer", h.buffer_size ?? "—");
  setTile("latency", h.latency_ms == null ? "—" : `${h.latency_ms.toFixed(1)} ms`,
    h.latency_ms > 10 ? "warn" : "");
  setTile("dsp", h.dsp_load_pct == null ? "—" : `${Math.round(h.dsp_load_pct)}%`,
    h.dsp_load_pct > 85 ? "bad" : h.dsp_load_pct > 60 ? "warn" : "");
  setTile("xruns", String(h.xruns), h.xruns > 0 ? "warn" : "");
  setTile("temp", h.cpu_temp_c == null ? "—" : `${Math.round(h.cpu_temp_c)}°C`,
    h.cpu_temp_c >= 80 ? "bad" : h.cpu_temp_c >= 70 ? "warn" : "");

  if (state.lastXruns != null && h.xruns > state.lastXruns) {
    const t = $('.tile[data-k="xruns"]');
    t.classList.remove("flash"); void t.offsetWidth; t.classList.add("flash");
  }
  state.lastXruns = h.xruns;

  const notes = [];
  if (h.engine === "fake") notes.push("Fake engine: DSP and xruns are simulated.");
  if (!h.latency_measured && h.latency_ms != null) notes.push("Latency is estimated from the buffer size.");
  if (h.cpu_temp_simulated) notes.push("No temperature sensor here, so CPU temp is simulated.");
  $("#health-note").textContent = notes.join(" ");
}

async function pollHealth() {
  if (document.visibilityState !== "visible") return;
  try {
    const [h, e] = await Promise.all([api("GET", "/api/health"), api("GET", "/api/engine")]);
    renderHealth(h);
    if (JSON.stringify(e.loaded) !== JSON.stringify(state.loaded)) {
      state.loaded = e.loaded;
      renderLoaded();
    }
  } catch {
    $("#health-note").textContent = "Can't reach the box. Is the phone on its Wi-Fi?";
  }
}

// ---------- preset list ----------

function effectName(id) { return state.effects[id]?.name ?? id; }

function renderList() {
  const ul = $("#preset-list");
  ul.replaceChildren();
  if (!state.presets.length) {
    ul.append(el("li", { className: "empty", textContent: "No presets yet. Tap + New." }));
  }
  for (const p of state.presets) {
    const li = $("#tpl-preset").content.firstElementChild.cloneNode(true);
    li.dataset.id = p.id;
    $(".p-name", li).textContent = p.name;
    $(".p-edit", li).href = `#/edit/${encodeURIComponent(p.id)}`;
    if (p.valid) {
      $(".p-desc", li).textContent = p.description || "";
      $(".p-meta", li).textContent =
        `${p.effects.map(effectName).join(" → ") || "empty chain"} · buffer ${p.buffer_size} (≈${latencyMs(p.buffer_size).toFixed(1)} ms)`;
      const btn = $(".preset-main", li);
      btn.setAttribute("aria-label", `Load ${p.name}`);
      btn.addEventListener("click", () => busy(btn, () => loadPreset(p.id)));
    } else {
      li.classList.add("invalid");
      $(".p-meta", li).textContent = `Invalid: ${p.error}`;
      $(".preset-main", li).disabled = true;
    }
    ul.append(li);
  }
  renderLoaded();
}

async function refreshList() {
  const data = await api("GET", "/api/presets");
  state.presets = data.presets;
  state.loaded = data.loaded;
  renderList();
}

async function loadPreset(id) {
  const r = await api("POST", "/api/engine/load", { preset_id: id });
  state.loaded = r.loaded;
  renderLoaded();
  toast(`Loaded ${r.loaded.name}`);
  pollHealth();
}

// ---------- editor ----------

function withDefaults(preset) {
  const p = structuredClone(preset);
  p.description ??= "";
  p.buffer_size ??= 128;
  for (const slot of p.chain) {
    const info = state.effects[slot.effect];
    slot.params ??= {};
    for (const spec of info?.params ?? []) slot.params[spec.id] ??= spec.default;
  }
  return p;
}

function markDirty(on = true) {
  state.dirty = on;
  $("#edit-dirty").hidden = !on;
}

function openEditor(id, preset) {
  state.draft = { id, preset: withDefaults(preset) };
  markDirty(id === null);
  const p = state.draft.preset;
  $("#f-name").value = p.name;
  $("#f-desc").value = p.description;
  const sel = $("#f-buf");
  sel.replaceChildren(...BUFFER_SIZES.map((b) =>
    el("option", { value: b, textContent: `${b} samples (≈${latencyMs(b).toFixed(1)} ms)` })));
  if (!BUFFER_SIZES.includes(p.buffer_size)) sel.append(el("option", { value: p.buffer_size, textContent: p.buffer_size }));
  sel.value = String(p.buffer_size);
  $("#f-buf-hint").textContent = "smallest safe size, from profiling";
  $("#delete-btn").hidden = id === null;
  $("#save-btn").textContent = id === null ? "Create" : "Save";
  $("#saveas-btn").hidden = id === null;
  renderChain();
}

function renderChain() {
  const ol = $("#chain");
  ol.replaceChildren();
  const chain = state.draft.preset.chain;
  if (!chain.length) ol.append(el("li", { className: "empty", textContent: "No effects: the guitar passes straight through." }));
  chain.forEach((slot, i) => {
    const info = state.effects[slot.effect];
    const li = el("li", { className: "slot" });
    const head = el("div", { className: "slot-head" },
      el("span", { className: "title", textContent: info?.name ?? slot.effect }));
    const mk = (label, title, disabled, fn) => {
      const b = el("button", { type: "button", className: "btn ghost icon-btn", textContent: label, title, disabled });
      b.setAttribute("aria-label", title);
      b.addEventListener("click", fn);
      return b;
    };
    head.append(
      mk("↑", "Move up", i === 0, () => moveSlot(i, -1)),
      mk("↓", "Move down", i === chain.length - 1, () => moveSlot(i, 1)),
      mk("✕", "Remove effect", false, () => {
        chain.splice(i, 1); markDirty(); renderChain();
      }),
    );
    li.append(head);
    if (info?.description) li.append(el("p", { className: "slot-desc", textContent: info.description }));
    for (const spec of info?.params ?? []) li.append(paramRow(slot, spec, i));
    ol.append(li);
  });
}

function paramRow(slot, spec, slotIndex) {
  const step = stepFor(spec);
  const id = `p-${slotIndex}-${spec.id}`;
  const row = el("div", { className: "param" });
  const range = el("input", { type: "range", min: spec.min, max: spec.max, step, value: slot.params[spec.id] });
  range.setAttribute("aria-label", `${spec.name} slider`);
  const num = el("input", { type: "number", id, min: spec.min, max: spec.max, step: "any", inputMode: "decimal" });
  num.value = fmtNum(slot.params[spec.id]);
  const reset = el("button", { type: "button", className: "reset", textContent: `reset to ${fmtNum(spec.default)}` });
  const err = el("div", { className: "err", hidden: true });

  const sync = () => { reset.hidden = slot.params[spec.id] === spec.default; };
  const set = (v, from) => {
    slot.params[spec.id] = v;
    if (from !== range) range.value = v;
    if (from !== num) num.value = fmtNum(v);
    num.classList.remove("invalid"); err.hidden = true;
    markDirty(); sync();
  };
  range.addEventListener("input", () => set(roundTo(Number(range.value), step), range));
  num.addEventListener("input", () => {
    const v = Number(num.value);
    if (num.value.trim() === "" || !Number.isFinite(v) || v < spec.min || v > spec.max) {
      num.classList.add("invalid");
      err.textContent = `Must be between ${fmtNum(spec.min)} and ${fmtNum(spec.max)}`;
      err.hidden = false;
      return;
    }
    set(v, num);
  });
  num.addEventListener("blur", () => { num.value = fmtNum(slot.params[spec.id]); num.classList.remove("invalid"); err.hidden = true; });
  reset.addEventListener("click", () => set(spec.default, null));
  sync();

  row.append(
    el("label", { htmlFor: id, textContent: spec.name }),
    el("div", { className: "num" }, num, el("span", { className: "unit", textContent: spec.unit })),
    range, reset, err,
  );
  return row;
}

function moveSlot(i, d) {
  const c = state.draft.preset.chain;
  [c[i], c[i + d]] = [c[i + d], c[i]];
  markDirty(); renderChain();
}

function draftPreset() {
  const p = state.draft.preset;
  const out = { format: 1, name: $("#f-name").value.trim(), description: $("#f-desc").value.trim(),
    buffer_size: Number($("#f-buf").value), chain: p.chain };
  if (!out.name) throw new Error("Give the preset a name.");
  return out;
}

async function save() {
  const preset = draftPreset();
  if (state.draft.id === null) return saveAs(preset);
  const r = await api("PUT", `/api/presets/${encodeURIComponent(state.draft.id)}`, { preset });
  markDirty(false);
  toast(r.reloaded ? `Saved and reloaded ${r.preset.name}` : `Saved ${r.preset.name}`);
  await refreshList();
  pollHealth();
}

async function saveAs(preset = draftPreset()) {
  if (state.draft.id !== null) {
    const name = prompt("Name for the new preset:", `${preset.name} copy`);
    if (name === null) return;
    if (!name.trim()) throw new Error("Give the preset a name.");
    preset.name = name.trim();
  }
  const r = await api("POST", "/api/presets", { preset });
  markDirty(false);
  toast(`Saved as ${r.preset.name}`);
  await refreshList();
  location.hash = `#/edit/${encodeURIComponent(r.id)}`;
}

async function tryDraft() {
  const preset = draftPreset();
  const r = await api("POST", "/api/engine/load", { preset, preset_id: state.draft.id });
  state.loaded = r.loaded;
  renderLoaded();
  toast(`Playing your edits of ${preset.name} (not saved)`);
  pollHealth();
}

async function deletePreset() {
  const { id } = state.draft;
  if (!confirm(`Delete "${state.draft.preset.name}"? The file presets/${id}.json is removed.`)) return;
  await api("DELETE", `/api/presets/${encodeURIComponent(id)}`);
  markDirty(false);
  toast("Deleted");
  location.hash = "#/";
}

// ---------- routing ----------

function show(view) {
  $("#view-list").hidden = view !== "list";
  $("#view-edit").hidden = view !== "edit";
  document.body.classList.toggle("editing", view === "edit");
  window.scrollTo(0, 0);
}

let currentHash = location.hash;
async function route() {
  if (state.dirty && !$("#view-edit").hidden && location.hash !== currentHash) {
    if (!confirm("Discard unsaved edits?")) { history.replaceState(null, "", currentHash); return; }
    markDirty(false);
  }
  currentHash = location.hash;
  const m = location.hash.match(/^#\/edit\/(.+)$/);
  try {
    if (m) {
      const id = decodeURIComponent(m[1]);
      const r = await api("GET", `/api/presets/${encodeURIComponent(id)}`);
      openEditor(id, r.preset);
      show("edit");
    } else if (location.hash === "#/new") {
      openEditor(null, { format: 1, name: "New preset", description: "", buffer_size: 128,
        chain: [{ effect: "gain", params: {} }].filter((s) => state.effects[s.effect]) });
      show("edit");
    } else {
      await refreshList();
      show("list");
    }
  } catch (e) {
    toast(e.message, true);
    if (m) location.hash = "#/";
  }
}

// ---------- init ----------

async function init() {
  state.catalog = await api("GET", "/api/catalog");
  for (const e of state.catalog.effects) state.effects[e.id] = e;
  $("#add-effect").replaceChildren(...state.catalog.effects.map((e) => el("option", { value: e.id, textContent: e.name })));

  $("#f-name").addEventListener("input", () => markDirty());
  $("#f-desc").addEventListener("input", () => markDirty());
  $("#f-buf").addEventListener("change", () => markDirty());
  $("#add-btn").addEventListener("click", () => {
    state.draft.preset.chain.push(withDefaults({ chain: [{ effect: $("#add-effect").value }] }).chain[0]);
    markDirty(); renderChain();
    $("#chain").lastElementChild?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("#edit-form").addEventListener("submit", (e) => e.preventDefault());
  $("#save-btn").addEventListener("click", (e) => busy(e.currentTarget, save));
  $("#saveas-btn").addEventListener("click", (e) => busy(e.currentTarget, () => saveAs()));
  $("#try-btn").addEventListener("click", (e) => busy(e.currentTarget, tryDraft));
  $("#delete-btn").addEventListener("click", (e) => busy(e.currentTarget, deletePreset));
  window.addEventListener("beforeunload", (e) => { if (state.dirty) e.preventDefault(); });
  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", pollHealth);

  await route();
  pollHealth();
  setInterval(pollHealth, HEALTH_POLL_MS);
}

init().catch((e) => toast(`Couldn't start: ${e.message}`, true));
