/* HyperQwen Settings UI — app.js
   Talks to ui/serve.py on the same origin. No dependencies. */
"use strict";

const $ = (s) => document.querySelector(s);

const state = {
  schema: null,
  values: {},
  baseline: {},
  meta: {},
  backups: [],
  customPresets: [],
  models: [],
  modelAuto: "",
  port: "18020",
  envExists: true,
};

/* ------------------------------------------------ api helpers */
async function api(path, body) {
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  let r;
  try { r = await fetch(path, opt); }
  catch (e) { throw new Error("Can't reach the settings server — is the black command window still open?"); }
  let j = {};
  try { j = await r.json(); } catch (e) { /* non-JSON */ }
  if (!j.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

let toastTimer = null;
function toast(msg, kind) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + (kind || "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast"; }, 4500);
}

/* ------------------------------------------------ values */
function isSet(v) { return v !== undefined && v !== null && v !== "" && v !== "__KEEP__"; }

function fieldOf(key) {
  if (!state.schema) return null;
  for (const g of state.schema.groups)
    for (const f of g.fields) if (f.key === key) return f;
  return null;
}

function setField(key, val) {
  const el = document.getElementById("f_" + key);
  const f = fieldOf(key);
  if (!el || !f) return;
  if (f.type === "bool") el.checked = (val === "1" || val === "true");
  else if (f.type === "secret") el.value = "";
  else if (f.type === "model") setModelField(el, val);
  else el.value = (val && val !== "__KEEP__") ? val : "";
}

/* model select: (auto) | known models | Custom path… (reveals a text field) */
function setModelField(sel, val) {
  const v = (val && val !== "__KEEP__") ? val : "";
  const custom = document.getElementById(sel.id + "_custom");
  const known = v === "" || Array.prototype.some.call(sel.options, (o) => o.value === v);
  sel.value = (v !== "" && !known) ? "__custom__" : v;
  if (custom) {
    custom.value = (v !== "" && !known) ? v : "";
    custom.style.display = (v !== "" && !known) ? "" : "none";
  }
  modelInfoHtml("MODEL");
}

function modelOptionLabel(m) {
  const bits = m.quant && m.quant.bits;
  const q = bits ? " " + bits + "-bit" : "";
  const rows = m.draft_rows ? " · " + Number(m.draft_rows).toLocaleString() + " draft rows" : "";
  return m.name + " — " + (m.size_mib != null ? Math.round(m.size_mib).toLocaleString() + " MiB" : "? MiB") + q + rows;
}

function modelInfoHtml(key) {
  const el = document.getElementById("f_" + key);
  const info = document.getElementById("f_" + key + "_info");
  if (!el || !info) return;
  if (el.value === "__custom__") {
    const cust = document.getElementById(el.id + "_custom");
    info.textContent = "Custom path → .env: MODEL=" + ((cust && cust.value) || "(empty — launcher default)");
    return;
  }
  if (el.value === "") {
    info.textContent = "(auto): the launcher picks the -fast build if it exists, else the base model.";
    return;
  }
  const m = (state.models || []).find((x) => x.model === el.value);
  if (!m) { info.textContent = ""; return; }
  const q = (m.quant && m.quant.bits)
    ? m.quant.bits + "-bit (group " + m.quant.group_size + ", " + (m.quant.symmetric ? "symmetric" : "asymmetric") + ")"
    : ((m.quant && m.quant.method) || "");
  info.textContent = (m.size_mib != null ? Math.round(m.size_mib).toLocaleString() + " MiB on disk" : "") + (q ? " · " + q : "")
    + " · MTP tensors: " + (m.mtp ? "yes" : "no")
    + (m.draft_rows ? " · draft head " + Number(m.draft_rows).toLocaleString() + " rows" : "");
}

function collectValues() {
  const out = {};
  if (!state.schema) return out;
  for (const g of state.schema.groups) for (const f of g.fields) {
    const el = document.getElementById("f_" + f.key);
    if (!el) continue;
    if (f.type === "bool") out[f.key] = el.checked ? "1" : "";
    else if (f.type === "secret") {
      const v = el.value.trim();
      out[f.key] = (v === "") ? "__KEEP__" : v;
    } else if (f.type === "model") {
      const cust = document.getElementById(el.id + "_custom");
      out[f.key] = (el.value === "__custom__" ? (cust ? cust.value.trim() : "") : el.value);
    } else out[f.key] = el.value.trim();
  }
  return out;
}

function changedCount() {
  const cur = collectValues();
  let n = 0;
  for (const k of Object.keys(cur)) if (cur[k] !== (state.baseline[k] || "")) n++;
  return n;
}

// The floppy save icon is about the PRESET, not the .env: it is live (bright
// green) whenever the current settings differ from what the target preset
// stores — so it stays usable after "Save changes" (which never touches
// presets) until the preset itself is updated.
function presetSaveable() {
  const p = currentPresetTarget();
  if (!p) return false;
  const cur = collectValues();
  const sets = p.sets || {};
  return Object.keys(sets).some((k) => (cur[k] || "") !== (sets[k] || ""));
}

function refreshChanged() {
  const n = changedCount();
  $("#changedCount").textContent = n ? (n + " unsaved change" + (n > 1 ? "s" : "")) : "";
  $("#saveBtn").disabled = (n === 0);
  const spb = $("#saveToPresetBtn");
  if (spb) spb.disabled = !presetSaveable();  // dull green only when the preset already matches the settings
}

/* ------------------------------------------------ rendering */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function presetMatches(p) {
  const cur = state.values;
  const sets = p.sets || {};
  const keys = Object.keys(sets);
  return keys.length > 0 && keys.every((k) => (cur[k] || "") === (sets[k] || ""));
}

function renderPresets() {
  const sel = $("#presetSelect");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = "";
  const ph = document.createElement("option");
  ph.value = "";
  ph.textContent = "— pick a preset —";
  sel.appendChild(ph);
  let activeName = null;
  const builtins = (state.schema && state.schema.presets) || [];
  if (builtins.length) {
    const grp = document.createElement("optgroup");
    grp.label = "Built-in";
    for (const p of builtins) {
      const o = document.createElement("option");
      o.value = "b:" + p.id;
      const active = presetMatches(p);
      o.textContent = p.name + (active ? "   ✓ current" : "");
      grp.appendChild(o);
      if (active) activeName = p.name;
    }
    sel.appendChild(grp);
  }
  if (state.customPresets.length) {
    const grp = document.createElement("optgroup");
    grp.label = "Your presets";
    for (const p of state.customPresets) {
      const o = document.createElement("option");
      o.value = "c:" + p.id;
      const active = presetMatches(p);
      o.textContent = p.name + (active ? "   ✓ current" : "");
      grp.appendChild(o);
      if (active) activeName = p.name;
    }
    sel.appendChild(grp);
  }
  if (prev && Array.prototype.some.call(sel.options, (o) => o.value === prev)) sel.value = prev;
  const curEl = $("#presetCurrent");
  if (curEl) curEl.textContent = activeName ? "matches: " + activeName : "";
  renderSelectedCustom();
}

// The preset the row's buttons act on: the one picked in the list, or (when
// nothing is picked) the unique one of yours that matches the current settings.
function currentPresetTarget() {
  const v = ($("#presetSelect") || {}).value || "";
  if (v.startsWith("c:")) {
    const p = state.customPresets.find((x) => x.id === v.slice(2));
    if (p) return p;
  }
  const matches = state.customPresets.filter(presetMatches);
  return matches.length === 1 ? matches[0] : null;
}

function renderSelectedCustom() {
  const row = $("#customPresetRow");
  const saveBtn = $("#saveToPresetBtn");
  const delBtn = $("#deletePresetBtn");
  const p = currentPresetTarget();
  // the floppy (save-to-preset) and trash (delete) icons sit inline in the preset row
  if (saveBtn) saveBtn.hidden = !p;
  if (delBtn) delBtn.hidden = !p;
  if (!row) return;
  if (!p) { row.hidden = true; return; }
  row.hidden = false;
  const keys = Object.keys(p.sets || {});
  const bits = keys.slice(0, 4).map((k) => k + "=" + (p.sets[k] === "" ? "off" : p.sets[k]));
  const meta = $("#customPresetMeta");
  const when = p.updated || p.created;
  if (meta) meta.textContent =
    (when ? (p.updated ? "updated " : "saved ") + when.slice(0, 10) + " — " : "") +
    keys.length + " fields: " + bits.join("  ·  ") + (keys.length > 4 ? "  ·  …" : "");
}

function findPresetByOption(v) {
  if (!v) return null;
  if (v.startsWith("c:")) return state.customPresets.find((x) => x.id === v.slice(2)) || null;
  const builtins = (state.schema && state.schema.presets) || [];
  return builtins.find((p) => p.id === v.slice(2)) || null;
}

function applyPreset(p) {
  for (const k of Object.keys(p.sets || {})) {
    state.values[k] = p.sets[k];
    setField(k, p.sets[k]);
  }
  renderPresets();
  renderWarnings();
  refreshChanged();
  refreshVramLive();
  toast("Preset applied: " + p.name +
    (p.mode ? " — press Save, then Start ⚡ " + p.mode
            : " — press Save, then start the mode that fits"));
}

/* ------------------------------------------------ custom presets */
async function loadCustomPresets() {
  const r = await api("/api/presets");
  state.customPresets = r.custom || [];
  return state.customPresets;
}

function openPresetModal() {
  const values = collectValues();
  for (const k of Object.keys(values))
    if (k === "VLLM_API_KEY" || k === "HF_TOKEN") delete values[k];  // secrets are never stored
  $("#presetModalCount").textContent = String(Object.keys(values).length);
  const input = $("#presetNameInput");
  // if a custom preset is selected (or matches the current settings), pre-fill
  // its name: re-saving under the same name offers to overwrite it (with a
  // confirm popup) instead of dead-ending
  const tgt = currentPresetTarget();
  input.value = tgt ? tgt.name : "";
  $("#presetModal").style.display = "flex";
  setTimeout(() => { input.focus(); input.select(); }, 10);
}

function closePresetModal() {
  $("#presetModal").style.display = "none";
}

async function savePresetFromModal() {
  const name = $("#presetNameInput").value.trim();
  if (!name) { toast("Give the preset a name first", "bad"); return; }
  // same name as an existing preset -> confirm once, then overwrite it in place
  const existing = state.customPresets.find(
    (p) => (p.name || "").trim().toLowerCase() === name.toLowerCase());
  if (existing &&
      !confirm("A preset named '" + existing.name + "' already exists.\n\nOverwrite it with the current settings?")) {
    toast("Not overwritten — pick a different name to save a new preset");
    return;
  }
  if (existing) name = existing.name;  // keep the stored name (and its casing)
  closePresetModal();
  const btn = $("#newPresetBtn");
  btn.disabled = true;
  try {
    const r = await api("/api/presets",
      { action: "create", name: name, sets: collectValues(), overwrite: !!existing });
    await loadCustomPresets();
    renderPresets();
    if (r.preset && r.preset.id) $("#presetSelect").value = "c:" + r.preset.id;
    toast((existing ? "Saved over preset: " : "Preset saved: ") + name);
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

// "Save to this preset" — overwrite the target preset in place (the one
// picked in the list, or the unique one matching the current settings).
async function saveToCurrentPreset() {
  const p = currentPresetTarget();
  if (!p) {
    toast("No preset to save to — pick one of yours in the list, or make one first", "bad");
    return;
  }
  if (!confirm("Overwrite the preset '" + p.name + "' with the current settings?\n\nIts stored settings are replaced, the name is kept. Your .env is not touched — press Save as usual to change it."))
    return;
  const btn = $("#saveToPresetBtn");
  if (btn) btn.disabled = true;
  try {
    await api("/api/presets", { action: "create", name: p.name, sets: collectValues(), overwrite: true });
    await loadCustomPresets();
    renderPresets();
    toast("Saved to preset: " + p.name);
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    refreshChanged();  // re-evaluate: the preset now matches the settings, so the icon goes dull again
  }
}

async function deleteSelectedPreset() {
  let p = null;
  const v = $("#presetSelect").value;
  if (v && v.startsWith("c:")) p = state.customPresets.find((x) => x.id === v.slice(2)) || null;
  if (!p) p = currentPresetTarget();
  if (!p) return;
  if (!confirm("Delete preset '" + p.name + "'?\nYour .env is not touched.")) return;
  try {
    await api("/api/presets", { action: "delete", id: p.id });
    await loadCustomPresets();
    renderPresets();
    toast("Deleted preset: " + p.name);
  } catch (e) { toast(e.message, "bad"); }
}

function controlFor(f) {
  const id = "f_" + f.key;
  let h = "";
  if (f.type === "enum") {
    h = "<select id='" + id + "'><option value=''>—</option>";
    for (const o of (f.options || [])) h += "<option value='" + esc(o) + "'>" + esc(o) + "</option>";
    h += "</select>";
  } else if (f.type === "bool") {
    h = "<input type='checkbox' id='" + id + "'>";
  } else if (f.type === "secret") {
    const meta = state.meta[f.key]
      ? "currently " + state.meta[f.key] + " — leave blank to keep"
      : "leave blank to keep current";
    h = "<input type='password' id='" + id + "' placeholder='" + esc(meta) + "' autocomplete='off' spellcheck='false'>";
  } else if (f.type === "int" || f.type === "float") {
    h = "<input type='number' id='" + id + "' step='" + (f.type === "float" ? "0.01" : "1") +
        "' placeholder='" + esc(f.default != null ? f.default : "") + "'>";
  } else if (f.type === "model") {
    h = "<select id='" + id + "'>";
    h += "<option value=''" + esc(state.modelAuto ? "(auto) — " + state.modelAuto : "(auto)") + "</option>";
    for (const m of state.models || [])
      h += "<option value='" + esc(m.model) + "'>" + esc(modelOptionLabel(m)) + "</option>";
    h += "<option value='__custom__'>Custom path…</option></select>";
    h += "<div class='model-info' id='" + id + "_info'></div>";
    h += "<input type='text' class='model-custom' id='" + id + "_custom' spellcheck='false' "
      + "placeholder='" + esc(f.default != null ? f.default : "") + "'>";

  } else {
    h = "<input type='text' id='" + id + "' placeholder='" + esc(f.default != null ? f.default : "") +
        "' spellcheck='false'>";
  }
  return h;
}

function renderGroups() {
  const box = $("#groups");
  box.innerHTML = "";
  for (const g of state.schema.groups) {
    const card = document.createElement("section");
    card.className = "card";
    let html = "<h2>" + esc(g.title) + "</h2>";
    if (g.blurb) html += "<p class='blurb'>" + esc(g.blurb) + "</p>";
    for (const f of g.fields) {
      const modes = (f.modes || []).length
        ? "<span class='tag'>" + f.modes.join(" / ") + "</span>" : "";
      html += "<div class='row'>" +
        "<div class='lab'><b>" + esc(f.label || f.key) + modes +
        "</b><span class='k'>" + esc(f.key) + "</span></div>" +
        "<div class='ctl'>" + controlFor(f) +
        (f.desc ? "<div class='desc'>" + esc(f.desc) + "</div>" : "") +
        "</div></div>";
    }
    card.innerHTML = html;
    box.appendChild(card);
    for (const f of g.fields) {
      const el = document.getElementById("f_" + f.key);
      if (el) el.addEventListener("input", onFieldInput);
      if (f.type === "model" && el) {
        el.addEventListener("change", onFieldInput);
        const cust = document.getElementById("f_" + f.key + "_custom");
        if (cust) {
          cust.style.display = "none";
          cust.addEventListener("input", onFieldInput);
        }
      }
    }
  }
  for (const k of Object.keys(state.values)) setField(k, state.values[k]);
  modelInfoHtml("MODEL");
  renderPresets();
  renderWarnings();
  refreshChanged();
}

function onFieldInput(e) {
  if (e.target.id === "f_MODEL_custom") {
    state.values.MODEL = e.target.value.trim();
    modelInfoHtml("MODEL");
    renderWarnings(); refreshChanged(); refreshVramLive();
    return;
  }
  const key = e.target.id.replace(/^f_/, "");
  const f = fieldOf(key);
  if (!f) return;
  if (f.type === "bool") state.values[key] = e.target.checked ? "1" : "";
  else if (f.type === "model") {
    const cust = document.getElementById("f_" + key + "_custom");
    if (e.target.value === "__custom__" && cust) cust.style.display = "";
    else if (cust) { cust.style.display = "none"; cust.value = ""; }
    state.values[key] = (e.target.value === "__custom__" ? (cust ? cust.value.trim() : "") : e.target.value);
  }
  else state.values[key] = e.target.value.trim();
  if (key === "MODEL") modelInfoHtml("MODEL");
  renderWarnings();
  refreshChanged();
  refreshVramLive();
}

/* ------------------------------------------------ warnings */
function renderWarnings() {
  const v = state.values;
  const warn = [];
  const info = [];
  if (v.SPEC === "dflash2" && v.CTX === "long")
    warn.push("Non-standard combo: CTX=long with SPEC=dflash2. The docs pair long context with SPEC=mtp (preset D) — dflash2 at 150k is untested.");
  if (!isSet(v.VLLM_API_KEY))
    warn.push("No API key set. If the port is reachable from other devices, anyone can use the model for free. Use \u201cGenerate new API key\u201d below.");
  if (v.CTX === "huge")
    info.push("CTX=huge (240k) uses KVarN KV cache — slower, fine for very long documents.");
  if (v.VLLM_WSL2_ENABLE_PIN_MEMORY === "1")
    info.push("WSL2 pin-memory is on — good on WSL2, harmless elsewhere.");
  $("#warnings").innerHTML =
    warn.map((t) => "<div class='warn'><span class='x'>!</span>" + esc(t) + "</div>").join("") +
    info.map((t) => "<div class='info'>i&nbsp;&nbsp;" + esc(t) + "</div>").join("");
}

/* ------------------------------------------------ status polling */
async function pollStatus() {
  try {
    const s = await api("/api/status");
    const up = s.health && s.health.up;
    $("#dot").className = "dot " + (up ? "up" : "down");
    $("#statusText").textContent = up
      ? "model is up — port " + s.port
      : "model is down — port " + s.port;
  } catch (e) {
    $("#dot").className = "dot";
    $("#statusText").textContent = "status unavailable";
  }
}

/* ------------------------------------------------ server actions */
async function doAction(name, btn) {
  if (name === "down" && !confirm("Stop the HyperQwen server?")) return;
  btn.disabled = true;
  $("#actionOut").innerHTML = "<p class='muted'>running: " + esc(name) + " …</p>";
  let j = null;
  try {
    const r = await fetch("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: name, confirm: name === "down" }),
    });
    j = await r.json();
    if (j.ok) {
      $("#actionOut").innerHTML = "<pre>" + esc(j.out || "(no output)") + "</pre>";
    } else {
      $("#actionOut").innerHTML =
        "<div class='warn'><span class='x'>!</span>" + esc(j.error || ("exited with code " + j.code)) +
        (j.out ? "</div><pre>" + esc(j.out) : "</div>");
    }
  } catch (e) {
    $("#actionOut").innerHTML = "<div class='warn'><span class='x'>!</span>" + esc(e.message) + "</div>";
  } finally {
    btn.disabled = false;
    if (j && j.ok) {
      if (name === "up-single" || name === "up-batch") bootPoll(); // start tracking the new boot
      if (name === "down") {                                       // container is gone again
        stopBootPoll();
        boot = null;
        renderBoot({ run: { state: "stopped" } });
      }
    }
    pollStatus();
  }
}

async function doKeygen() {
  if (!confirm("Generate a NEW API key and save it to .env?\nClients still using the old key will stop working until updated.")) return;
  try {
    const r = await api("/api/keygen");
    toast("New API key saved (" + r.keyMeta + ")");
    await loadAll();
  } catch (e) { toast(e.message, "bad"); }
}

function renderBackups() {
  const box = $("#backups");
  if (!state.backups.length) {
    box.innerHTML = "<span class='muted'>no backups yet — one is made on every save</span>";
    return;
  }
  box.innerHTML = "";
  for (const b of state.backups) {
    const row = document.createElement("div");
    row.className = "b";
    row.innerHTML = "<span>" + esc(b.name.replace(".env.bak-", "")) + "</span>" +
      "<span class='muted'>" + esc(b.mtime) + "</span>" +
      "<button class='btn ghost' style='padding:3px 10px;font-size:12px'>restore</button>";
    row.querySelector("button").onclick = async () => {
      if (!confirm("Restore " + b.name + " over the current .env?\nA new backup of the current file is made first.")) return;
      try {
        await api("/api/restore", { name: b.name });
        toast("Restored " + b.name);
        await loadAll();
      } catch (e) { toast(e.message, "bad"); }
    };
    box.appendChild(row);
  }
}

/* ------------------------------------------------ load / save */
async function loadModels() {
  try {
    const r = await api("/api/models");
    state.models = r.models || [];
  } catch (e) { state.models = []; }
  const fast = state.models.find((m) => /-fast$/.test(m.name)) || state.models[0];
  state.modelAuto = fast ? fast.name : "";
}

async function loadSettings() {
  const s = await api("/api/settings");
  state.schema = s.schema;
  state.values = s.values;
  state.baseline = Object.assign({}, s.values);
  state.meta = {};
  for (const k of Object.keys(s.values))
    if (k.endsWith("_meta")) state.meta[k.replace(/_meta$/, "")] = s.values[k];
  state.backups = s.backups;
  state.port = s.port;
  state.envExists = s.envExists;
  try {
    const pr = await api("/api/presets");
    state.customPresets = pr.custom || [];
  } catch (e) { state.customPresets = []; }
  $("#sub").textContent = "editing " + (state.envExists ? ".env" : "no .env found") +
    " — model port " + state.port;
  $("#keyState").textContent = isSet(s.values.VLLM_API_KEY)
    ? "set (" + (state.meta.VLLM_API_KEY || "") + ")" : "not set";
  await loadModels();
  renderGroups();
  renderBackups();
}

async function loadRaw() {
  try {
    const r = await api("/api/raw");
    $("#raw").value = r.text;
  } catch (e) { /* raw endpoint missing */ }
}

async function loadAll() { await loadSettings(); await loadRaw(); }

async function save() {
  const values = collectValues();
  try {
    const r = await api("/api/settings", { values });
    state.baseline = Object.assign({}, values);
    $("#saveNote").textContent = r.backup ? "last save: " + r.backup : "saved";
    toast("Saved" + (r.backup ? " — backup " + r.backup : "") + ". Restart the server to apply.");
    await loadAll();
    loadVram();
  } catch (e) { toast(e.message, "bad"); }
}

/* ================================================================
   VRAM CALCULATOR (sidebar) — reads settings directly, no separate controls
   ================================================================ */

let vramData = null;
let vramTimer = null;

/** Build API params from state.values (the settings panel is the single source of truth) */
function vramParams() {
  const v = state.values;
  const spec = v.SPEC || "mtp";
  return {
    mode:   "single",
    variant:(v.FAST_VARIANT === "0") ? "base" : "fast",
    spec:   (spec === "off" || spec === "none" || spec === "") ? "off" : spec,
    ctx:    v.CTX || "fast",
    dflash_tokens:  v.DFLASH_TOKENS || "7",
    draft_tokens:   v.DRAFT_TOKENS || "4",
    vision:         v.VISION === "1" ? "1" : "0",
    vision_offload: v.VISION_OFFLOAD === "1" ? "1" : "0",
    prefix_cache:   v.PREFIX_CACHE === "1" ? "1" : "0",
    kv:             v.KV || "fp8",
    gpu_util:       v.GPU_UTIL || "",
    max_len:        v.MAX_LEN || "",
    kv_mem:         v.KV_MEM || "",
    max_seqs:       v.MAX_SEQS || "",
    gpu_count:      v.GPU_COUNT || "1",
    spec_attn:      v.SPEC_ATTN === "0" ? "0" : "1",
    lookup:         v.LOOKUP === "1" ? "1" : "0",
    mtp_draft_vocab: v.MTP_DRAFT_VOCAB === "1" ? "1" : "0",
    int8_act:       v.INT8_ACT || "",
    prefill_attn:   v.PREFILL_ATTN || "",
    extra_args:     v.EXTRA_ARGS || "",
    os_vram:        v.OS_VRAM || "512",
    model:          v.MODEL || "",
  };
}

/** Debounced live update — called on every settings field change */
function refreshVramLive() {
  clearTimeout(vramTimer);
  vramTimer = setTimeout(() => loadVram(), 250);
}

async function loadVram() {
  const p = vramParams();
  const qs = Object.entries(p).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
  const summary = $("#vramSummary");
  if (summary && !vramData) {
    summary.innerHTML = '<div class="vram-stat"><span class="lbl">Loading…</span><span class="val muted">—</span></div>';
  }
  try {
    const r = await api("/api/vram?" + qs);
    vramData = r;
    renderVram();
  } catch (e) {
    if (summary) summary.innerHTML = "<div class='warn'><span class='x'>!</span>" + esc(e.message) + "</div>";
  }
}

function renderVram() {
  if (!vramData) return;
  const d = vramData;
  drawPie(d.components, d.gpu_usable_mib);
  renderVramSummary(d);
  renderVramTable(d);
}

function drawPie(components, usableMib) {
  const cv = document.getElementById("vramPie");
  if (!cv) return;
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 10;
  ctx.clearRect(0, 0, W, H);

  const total = components.reduce((s, c) => s + c.mib, 0);
  if (total <= 0) return;

  let angle = -Math.PI / 2;
  for (const c of components) {
    const sweep = (c.mib / total) * Math.PI * 2;
    if (sweep < 0.001) continue;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, R, angle, angle + sweep);
    ctx.closePath();
    ctx.fillStyle = c.color;
    ctx.fill();
    ctx.strokeStyle = "#171a21";
    ctx.lineWidth = 1.5;
    ctx.stroke();
    angle += sweep;
  }

  const holeR = R * 0.52;
  ctx.beginPath();
  ctx.arc(cx, cy, holeR, 0, Math.PI * 2);
  ctx.fillStyle = "#171a21";
  ctx.fill();

  const usedMib = components.reduce((s, c) => s + c.mib, 0) -
    (components.find(c => c.name === "Headroom") || { mib: 0 }).mib;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "#e8ebf0";
  ctx.font = "bold 20px -apple-system,Segoe UI,sans-serif";
  ctx.fillText((usedMib / 1024).toFixed(1) + " GiB", cx, cy - 10);
  ctx.font = "11px -apple-system,Segoe UI,sans-serif";
  ctx.fillStyle = "#9aa3b2";
  ctx.fillText("of " + (usableMib / 1024).toFixed(1) + " GiB usable", cx, cy + 12);
}

function renderVramSummary(d) {
  const box = document.getElementById("vramSummary");
  if (!box) return;
  const headClass = d.fits ? "ok" : "bad";
  const headLabel = d.fits ? "Headroom"
    : (d.max_len_ok === false && d.headroom_mib >= 0 ? "KV CAPACITY SHORT" : "OVER BUDGET");
  const headVal = (d.headroom_mib / 1024).toFixed(2) + " GiB";
  const headPct = Math.abs(d.headroom_pct).toFixed(1) + "%";

  let capHtml = "";
  if (d.kv_capacity_tokens > 0) {
    const cap = d.kv_capacity_tokens.toLocaleString();
    capHtml = d.max_len_ok
      ? '<div class="vram-stat"><span class="lbl">KV capacity</span>' +
        '<span class="val ok">' + cap + ' tok</span></div>'
      : '<div class="vram-stat"><span class="lbl">KV capacity</span>' +
        '<span class="val bad">' + cap + ' tok (MAX_LEN too big)</span></div>';
  }

  let notesHtml = (d.notes || [])
    .filter(n => d.weights_source !== "fallback-fast" || !/estimated from the reference fast variant/.test(n))
    .map(n => '<div class="vram-note">' + esc(n) + "</div>").join("");
  if (d.weights_source === "fallback-fast") {
    notesHtml = '<div class="vram-note bad">⚠ Weights estimated from the reference fast variant' +
      (d.model_dir ? " — '" + esc(d.model_dir) + "' could not be read" : "") + ".</div>" + notesHtml;
  }

  box.innerHTML =
    '<div class="vram-stat"><span class="lbl">GPU total</span>' +
    '<span class="val">' + (d.gpu_total_mib / 1024).toFixed(0) + ' GiB' +
    (d.gpu_count > 1 ? " × " + d.gpu_count : "") + '</span></div>' +
    '<div class="vram-stat"><span class="lbl">Usable (util ' + d.gpu_util + ')</span>' +
    '<span class="val">' + (d.gpu_usable_mib / 1024).toFixed(2) + ' GiB</span></div>' +
    '<div class="vram-stat"><span class="lbl">Total used</span>' +
    '<span class="val">' + (d.total_used_mib / 1024).toFixed(2) + ' GiB</span></div>' +
    '<div class="vram-stat"><span class="lbl">' + headLabel + '</span>' +
    '<span class="val ' + headClass + '">' + headVal + ' (' + headPct + ')</span></div>' +
    '<div class="vram-stat"><span class="lbl">Max context</span>' +
    '<span class="val">' + (d.max_len ? d.max_len.toLocaleString() + ' tok' : '—') + '</span></div>' +
    capHtml +
    notesHtml;
}

function renderVramTable(d) {
  const tbody = document.getElementById("vramTbody");
  if (!tbody) return;
  const usable = d.gpu_usable_mib;
  tbody.innerHTML = "";
  for (const c of d.components) {
    const tr = document.createElement("tr");
    const pct = usable ? (c.mib / usable * 100) : 0;
    tr.innerHTML =
      '<td><span class="swatch" style="background:' + c.color + '"></span>' + esc(c.name) + '</td>' +
      '<td class="num">' + c.mib.toLocaleString(undefined, { maximumFractionDigits: 0 }) + '</td>' +
      '<td class="num">' + c.gib.toFixed(3) + '</td>' +
      '<td><div class="vram-bar"><div class="vram-bar-fill" style="width:' + Math.min(100, pct) + '%;background:' + c.color + '"></div></div></td>';
    tbody.appendChild(tr);
  }
}

/* ------------------------------------------------ VRAM sidebar toggle */
function toggleVramPanel() {
  const panel = $("#vramPanel");
  panel.classList.toggle("open");
  const btn = $("#vramToggle");
  const isOpen = panel.classList.contains("open");
  btn.textContent = isOpen ? "✕ VRAM" : "VRAM";
  if (isOpen && !vramData) {
    loadVram();
  }
}

/* ================================================================
   STARTUP PROGRESS
   Time-based bar calibrated by ui/serve.py from the serve container's
   timestamped docker log (GET /api/bootlog). Bar width = elapsed /
   estimated total. A new boot re-calibrates on the fly: measured phases
   replace their estimates, and the weight/draft/CUDA phases show the real
   N% from the log. Polls every 2 s while a boot is in progress, then stops
   (the 15 s status poll keeps the header dot).
   ================================================================ */

let boot = null;
let bootTimer = null;

function stopBootPoll() {
  if (bootTimer) clearTimeout(bootTimer);
  bootTimer = null;
}

function bootPoll() {
  api("/api/bootlog").then((j) => {
    boot = j;
    renderBoot(j);
    if (j.run && j.run.state === "starting") {
      stopBootPoll();
      bootTimer = setTimeout(bootPoll, 2000);
    }
  }).catch(() => {
    /* settings-server window went away — the 15 s status poll will notice */
  });
}

function fmtDur(s) {
  if (s == null || s === undefined || isNaN(s)) return "—";
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60);
  return m ? m + "m" + (s % 60 ? (s % 60) + "s" : "") : s + "s";
}

function fmtUtc(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds());
}

function progText(p) {
  if (!p) return "";
  return p.kind === "shards" ? "shard " + p.i + "/" + p.n
       : p.kind === "steps" ? "step " + p.i + "/" + p.n : "";
}

function renderBoot(j) {
  const run = j.run || {};
  const st = run.state || "stopped";
  const title = $("#bootTitle"), meta = $("#bootMeta"),
        fill = $("#bootFill"), pctEl = $("#bootPct"),
        phasesEl = $("#bootPhases"), errEl = $("#bootErrors");
  if (!title || !meta || !fill || !pctEl || !phasesEl || !errEl) return;

  const pct = Math.max(0, Math.min(100, run.pct || 0));
  fill.style.width = pct + "%";
  fill.className = "boot-fill" + (st === "ready" ? " ok" : st === "failed" ? " bad" : "");

  const port = state.port || "18020";
  let cls = "", metaTxt = "";
  if (st === "ready") {
    title.textContent = "Model loaded — server ready on port " + port;
    cls = "ok";
    if (run.bootDur != null) metaTxt = "boot took " + fmtDur(run.bootDur);
    if (run.readyAt) metaTxt += " · ready " + fmtUtc(run.readyAt) + " UTC";
    if (run.note) metaTxt += (metaTxt ? " · " : "") + run.note;
  } else if (st === "failed") {
    title.textContent = "Boot failed";
    cls = "bad";
    if (run.elapsed != null) metaTxt = fmtDur(run.elapsed) + " elapsed";
  } else if (st === "starting") {
    const phAll = run.phases || [];
    const a = phAll.find((p) => p.status === "active" || p.status === "fail");
    const ai = a ? phAll.indexOf(a) : -1;
    title.textContent = a
      ? "Starting — " + a.label + (ai >= 0 ? " (step " + (ai + 1) + " of " + phAll.length + ")" : "")
          + (a.progress ? " · " + progText(a.progress) : "")
      : "Starting";
    cls = "run";
    if (run.elapsed != null) metaTxt = fmtDur(run.elapsed) + " elapsed";
    if (run.totalEst != null) metaTxt += " · est. total " + fmtDur(run.totalEst);
    if (run.note) metaTxt += (metaTxt ? " · " : "") + run.note;
  } else if (st === "stopped") {
    title.textContent = "Server stopped";
    metaTxt = "press “Start” above — the bar tracks the boot in real time";
  } else if (st === "no-docker") {
    title.textContent = "Docker is not running";
  } else if (st === "unknown") {
    title.textContent = "Status unknown" + (run.error ? " — " + run.error : "");
  }
  title.className = "boot-title " + cls;

  if (run.calSrc && (st === "starting" || st === "failed" || st === "ready"))
    metaTxt += (metaTxt ? " · " : "") + "calibrated: " + run.calSrc;
  meta.textContent = metaTxt;

  pctEl.textContent =
    st === "ready" ? "100% — the model is loaded and serving"
    : st === "starting" ? Math.floor(pct) + "% of the whole boot (start → model loaded; per-step progress is in the pills below)"
    : st === "failed" ? "frozen at " + Math.floor(pct) + "%" : "";

  const pills = [];
  for (const p of (run.phases || [])) {
    const cls2 = "boot-phase " +
      (p.status === "done" ? "done" : p.status === "active" ? "active" : p.status === "fail" ? "fail" : "");
    let txt = p.label;
    if (p.status === "done") {
      txt = "✓ " + p.label + (p.dur != null ? " " + fmtDur(p.dur) : "");
    } else if (p.status === "active" || p.status === "fail") {
      txt = (p.status === "fail" ? "✗ " : "▸ ") + p.label;
      if (p.progress) txt += " · " + progText(p.progress) + " " + p.progress.pct + "%";
      else if (p.elapsed != null && p.expected != null)
        txt += " · " + fmtDur(p.elapsed) + " / ~" + fmtDur(p.expected) + " (est.)";
      if (p.overrun) txt += " — running longer than expected";
    }
    pills.push("<span class='" + cls2 + "'>" + esc(txt) + "</span>");
  }
  phasesEl.innerHTML = pills.join("");

  errEl.innerHTML = (run.errors || []).slice(0, 4)
    .map((e) => "<div class='boot-err'>" + esc(e) + "</div>")
    .join("");
}

/* ------------------------------------------------ init */
function init() {
  $("#saveBtn").onclick = save;
  $("#reloadBtn").onclick = () => loadAll().then(() => toast("Reloaded from disk"));
  $("#keygenBtn").onclick = doKeygen;
  const sel = $("#presetSelect");
  if (sel) sel.onchange = () => {
    const p = findPresetByOption(sel.value);
    if (p) applyPreset(p);
  };
  $("#newPresetBtn").onclick = openPresetModal;
  $("#saveToPresetBtn").onclick = saveToCurrentPreset;
  $("#deletePresetBtn").onclick = deleteSelectedPreset;
  $("#presetModalCancel").onclick = closePresetModal;
  $("#presetModalOk").onclick = savePresetFromModal;
  $("#presetNameInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") savePresetFromModal();
    else if (e.key === "Escape") closePresetModal();
  });
  $("#presetModal").onclick = (e) => { if (e.target === e.currentTarget) closePresetModal(); };
  document.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = () => doAction(b.dataset.act, b);
  });

  // VRAM sidebar toggle
  $("#vramToggle").onclick = toggleVramPanel;
  $("#vramCloseBtn").onclick = toggleVramPanel;

  loadAll().then(() => {
    pollStatus();
    loadVram();
    bootPoll();
  });
  setInterval(pollStatus, 15000);
}
init();
