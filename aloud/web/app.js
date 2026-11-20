"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let S = { settings: {}, voices: [] };
let D = null;                 // current document
let els = [];                 // paragraph elements (by para index)
let cur = -1;                 // current unit (a run of sentences inside a paragraph)
let resumeT = 0;
let wordEl = null, wordK = -1;
let token = 0;
let userScrolled = false, autoScrollUntil = 0;
const metaCache = new Map();  // voice|u -> meta
const inflight = new Map();   // voice|u -> promise
const blobCache = new Map();  // key|clean -> objectURL
// Two players: the current passage plays in `audio` while the next one waits, fully
// loaded, in `spare`; at the end they swap, so passages follow each other without a gap.
let audio = new Audio(), spare = new Audio(), spareFor = null;
for (const a of [audio, spare]) {
  a.preload = "auto"; a.preservesPitch = true; a.webkitPreservesPitch = true;
  a.addEventListener("ended", (e) => { if (e.target === audio) onEnded(); });
  a.addEventListener("timeupdate", (e) => { if (e.target === audio && !audio.paused) step(); });
  a.addEventListener("seeked", (e) => { if (e.target === audio) step(); });
}
const sample = new Audio();
let captionRun = null;

const paraOf = (u) => D.units[u].p;
const VOICE_LANG = { a: "en", b: "en", e: "es", f: "fr", i: "it", p: "pt", d: "de" };
const LANG_NAME = { en: "English", es: "Spanish", fr: "French", it: "Italian", pt: "Portuguese", de: "German" };
// The voice for the open document: your chosen voice, unless the document is in another
// language that has its own voices.
function voice() {
  const sv = S.settings.voice, dl = (D && D.lang) || "en";
  if (VOICE_LANG[sv[0]] === dl || !(S.defaultVoice || {})[dl]) return sv;
  return (S.settings.lang_voices || {})[dl] || S.defaultVoice[dl];
}
const isBusy = () => !!captionRun || !audio.paused || $("#play").classList.contains("loading");

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) { const err = new Error(j.error || r.statusText); err.signin = j.signin; throw err; }
  return j;
}
function toast(msg, ms = 1800) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms); }
function setStatus(s) { $("#status").textContent = s || ""; }
function saveSettings(patch) { Object.assign(S.settings, patch); return api("/api/settings", patch); }

// ---------------------------------------------------------------- settings UI
function applyLook() {
  document.documentElement.dataset.theme = S.settings.theme;
  document.querySelectorAll("#optTheme button").forEach((b) => b.classList.toggle("on", b.dataset.t === S.settings.theme));
  $("#themeIcon").innerHTML = isDark()
    ? '<circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'
    : '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>';
  $("#themeBtn").title = isDark() ? "Switch to light (D)" : "Switch to dark (D)";
  document.documentElement.style.setProperty("--font", S.settings.font + "px");
  document.body.classList.toggle("hideskip", !S.settings.show_skipped);
  $("#rate").textContent = (+S.settings.rate).toFixed(2).replace(/0$/, "") + "×";
  audio.playbackRate = S.settings.rate; spare.playbackRate = S.settings.rate;
}
function buildSettings() {
  const v = $("#voices"); v.innerHTML = "";
  let lastLang = null;
  for (const [id, label, grade] of S.voices) {
    const lang = VOICE_LANG[id[0]] || "en";
    if (lang !== lastLang) {
      lastLang = lang;
      const h = document.createElement("div"); h.className = "vlang";
      h.textContent = LANG_NAME[lang] + (lang === "en" ? "" : ` — used for ${LANG_NAME[lang]} documents`);
      v.appendChild(h);
    }
    const d = document.createElement("div");
    d.className = "voice" + (id === voice() ? " sel" : "");
    d.innerHTML = `<button class="icon" title="Hear a sample"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15l13-7.5z"/></svg></button><span class="n">${esc(label)}</span>${lang === "en" && grade ? `<span class="g" title="Quality grade">${grade}</span>` : ""}`;
    d.querySelector("button").onclick = (e) => { e.stopPropagation(); playSample(id, d.querySelector("button")); };
    d.onclick = () => chooseVoice(id);
    v.appendChild(d);
  }
  $("#optClean").checked = S.settings.clean; $("#optCite").checked = S.settings.skip_citations;
  $("#optSkip").checked = S.settings.show_skipped; $("#optFollow").checked = S.settings.follow;
  $("#optFigures").checked = S.settings.figure_previews !== false;
  $("#optFont").value = S.settings.font;
  buildLexicon();
}
async function playSample(id, btn) {
  btn.style.opacity = .4;
  try {
    const m = await api(`/api/sample?voice=${id}`);
    if (isBusy()) pause();
    sample.src = `/audio/${m.key}.wav?clean=${S.settings.clean ? 1 : 0}`; sample.play();
  } catch (e) { toast("Sample failed: " + e.message); }
  finally { btn.style.opacity = 1; }
}
async function chooseVoice(id) {
  const vl = VOICE_LANG[id[0]] || "en";
  if (vl === "en") await saveSettings({ voice: id });
  else await saveSettings({ lang_voices: { ...(S.settings.lang_voices || {}), [vl]: id } });
  buildSettings();
  const docLang = (D && D.lang) || "en";
  toast("Voice: " + S.voices.find((v) => v[0] === id)[1] + (vl !== docLang && vl !== "en" ? ` (used for ${LANG_NAME[vl]} documents)` : ""), 2600);
  restartCurrent();
}
$("#optClean").onchange = async (e) => { await saveSettings({ clean: e.target.checked }); toast(e.target.checked ? "Voice clean-up on" : "Voice clean-up off (raw)"); restartCurrent(); };
$("#optCite").onchange = async (e) => {
  const r = await saveSettings({ skip_citations: e.target.checked });
  if (r.reload && D) { const p = paraOf(cur), was = isBusy(); await openDoc({ id: D.id }, p, was); }
};
$("#optSkip").onchange = (e) => { saveSettings({ show_skipped: e.target.checked }); applyLook(); };
$("#optFollow").onchange = (e) => { saveSettings({ follow: e.target.checked }); };
$("#optFigures").onchange = (e) => {
  saveSettings({ figure_previews: e.target.checked });
  if (!e.target.checked) resetFigurePreview();
};
$("#optFont").oninput = (e) => { S.settings.font = +e.target.value; applyLook(); };
$("#optFont").onchange = (e) => saveSettings({ font: +e.target.value });
const isDark = () => S.settings.theme === "dark" || (S.settings.theme === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
function setTheme(t) { saveSettings({ theme: t }); applyLook(); }
document.querySelectorAll("#optTheme button").forEach((b) => (b.onclick = () => setTheme(b.dataset.t)));
$("#themeBtn").onclick = () => setTheme(isDark() ? "light" : "dark");
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => applyLook());
$("#setBtn").onclick = () => $("#settings").classList.toggle("show");
document.addEventListener("mousedown", (e) => { if (!e.target.closest("#settings, #setBtn")) $("#settings").classList.remove("show"); });

function setRate(r) {
  r = Math.round(Math.min(3, Math.max(0.6, r)) * 20) / 20;
  S.settings.rate = r; applyLook(); clearTimeout(setRate._h);
  if (captionRun) captionRun.clip.playbackRate = r;
  setRate._h = setTimeout(() => saveSettings({ rate: r }), 400);
  updateInfo();
}
$("#slower").onclick = () => setRate(S.settings.rate - 0.1);
$("#faster").onclick = () => setRate(S.settings.rate + 0.1);
$("#rate").onclick = () => setRate(1);

// ---------------------------------------------------------------- library
let libFolder = null;
try { libFolder = localStorage.getItem("libFolder") || null; } catch (e) { /* no storage */ }
const kindOf = (p) => p.includes("/ALOUD/web/") ? "Web article" : p.includes("/ALOUD/pasted/") ? "Pasted text"
  : ({ pdf: "PDF", epub: "Book", mobi: "Book", docx: "Word", doc: "Word", md: "Markdown", txt: "Text", html: "Web page", rtf: "Document" })[p.split(".").pop().toLowerCase()] || "Document";
function timeLeft(d) {
  if (!d.chars) return "";
  const frac = d.units ? 1 - (d.pos?.unit || 0) / Math.max(1, d.units) : 1;
  const min = Math.max(1, Math.round(d.chars * frac / 14.5 / 60 / (S.settings.rate || 1)));
  if (frac < 0.02) return "Finished";
  return (min >= 60 ? `${Math.floor(min / 60)} h ${min % 60} min` : `${min} min`) + (frac > 0.98 ? "" : " left");
}
const FOLDER_SVG = `<svg viewBox="0 0 48 40" aria-hidden="true"><path d="M4 8a4 4 0 0 1 4-4h10.5a4 4 0 0 1 3 1.4L24.6 9H40a4 4 0 0 1 4 4v19a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" fill="var(--folder-back)"/><path d="M4 14a3 3 0 0 1 3-3h34a3 3 0 0 1 3 3v18a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" fill="var(--folder)"/></svg>`;
async function showLibrary(keepScroll) {
  pause();
  resetFigurePreview();
  const st = await api("/api/state"); S.settings = st.settings; S.queue = st.queue || []; S.docs = st.docs; S.folders = st.folders || {};
  document.body.classList.add("lib"); $("#title").textContent = "ALOUD"; document.title = "ALOUD";
  if (libFolder && !S.folders[libFolder]) libFolder = null;
  renderLibrary();
}
function folderPath(fid) {
  const out = []; let f = fid, guard = 0;
  while (f && S.folders[f] && guard++ < 50) { out.unshift(f); f = S.folders[f].parent; }
  return out;
}
function folderCount(fid) {
  const docs = S.docs.filter((d) => (d.folder || null) === fid).length;
  const subs = Object.values(S.folders).filter((f) => (f.parent || null) === fid).length;
  return docs + subs;
}
function goFolder(fid) { libFolder = fid; try { fid ? localStorage.setItem("libFolder", fid) : localStorage.removeItem("libFolder"); } catch (e) {} renderLibrary(); }
async function moveTo(to, docs = [], folders = []) {
  const r = await api("/api/folder", { op: "move", to, docs, folders }); S.folders = r.folders;
  for (const d of S.docs) if (docs.includes(d.id)) d.folder = to;
  renderLibrary();
  toast(`Moved to ${to ? S.folders[to].name : "Library"}`, 1400);
}
function renderLibrary() {
  const r = $("#recent");
  const q = ($("#libFind")?.value || "").toLowerCase().trim();
  const crumbs = [`<a class="crumb" data-f="">Library</a>`].concat(folderPath(libFolder).map((f) => `<span class="sep">›</span><a class="crumb" data-f="${f}">${esc(S.folders[f].name)}</a>`)).join("");
  r.innerHTML = `<div class="lib-head"><nav class="crumbs">${crumbs}</nav><div class="lib-tools">
      <button class="pill-btn" id="newFolder"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 11v5M9.5 13.5h5"/></svg>New folder</button>
      <input id="libFind" placeholder="Search all" autocomplete="off" spellcheck="false" value="${esc(q)}"></div></div><div class="grid" id="grid"></div>`;
  const grid = $("#grid");
  const folders = q ? Object.entries(S.folders).filter(([id, f]) => f.name.toLowerCase().includes(q))
    : Object.entries(S.folders).filter(([id, f]) => (f.parent || null) === libFolder);
  folders.sort((x, y) => x[1].name.localeCompare(y[1].name));
  const docs = q ? S.docs.filter((d) => (d.title + " " + d.path).toLowerCase().includes(q)) : S.docs.filter((d) => (d.folder || null) === libFolder);
  for (const [fid, f] of folders) {
    const n = folderCount(fid);
    const t = document.createElement("div"); t.className = "card folder"; t.dataset.f = fid; t.draggable = true;
    t.innerHTML = `<div class="cover">${FOLDER_SVG}</div><b class="fname">${esc(f.name)}</b><span>${n ? n + (n === 1 ? " item" : " items") : "Empty"}${q && f.parent ? " · in " + esc(S.folders[f.parent]?.name || "") : ""}</span>`;
    t.onclick = (e) => { if (t.classList.contains("editing")) return; goFolder(fid); };
    t.oncontextmenu = (e) => { e.preventDefault(); menuAt(e, [["Open", () => goFolder(fid)], ["Rename…", () => renameFolder(t, fid)], ["Move to…", null, moveTargets([], [fid])], ["-"], ["Delete folder", () => deleteFolder(fid)]]); };
    dragSource(t, { folders: [fid] }); dropTarget(t, fid);
    t.addEventListener("mouseenter", () => fanSoon(t, fid));
    t.addEventListener("mouseleave", fanLeave);
    grid.appendChild(t);
  }
  for (const d of docs) grid.appendChild(docCard(d, !!q));
  grid.querySelectorAll(".card").forEach((c, i) => c.style.setProperty("--i", Math.min(i, 14)));
  if (!folders.length && !docs.length) grid.innerHTML = `<div class="lib-empty">${q ? `Nothing matches “${esc(q)}”.` : libFolder ? "This folder is empty. Drag documents here, or right-click a document › Move to…" : "Open a document or paste some text to start."}</div>`;
  r.querySelectorAll(".crumb").forEach((c) => { const f = c.dataset.f || null; c.onclick = () => goFolder(f); dropTarget(c, f); });
  $("#newFolder").onclick = newFolder;
  const find = $("#libFind");
  find.oninput = () => { const pos = find.selectionStart; renderLibrary(); const f2 = $("#libFind"); f2.focus(); f2.setSelectionRange(pos, pos); };
}
function docCard(d, showWhere) {
  const pct = d.units && d.pos ? Math.min(100, Math.round(100 * (d.pos.unit || 0) / Math.max(1, d.units - 1))) : 0;
  const inQ = (S.queue || []).includes(d.id);
  const card = document.createElement("div"); card.className = "card"; card.draggable = true;
  const where = showWhere && d.folder && S.folders[d.folder] ? "in " + S.folders[d.folder].name : "";
  card.innerHTML = `<div class="cover"><img alt="" loading="lazy" draggable="false" src="/api/cover?id=${encodeURIComponent(d.id)}"><span class="kind">${esc(kindOf(d.path))}</span>
      <button class="rm" title="Remove from library">×</button><button class="qadd${inQ ? " on" : ""}">${inQ ? "✓ Up next" : "+ Up next"}</button>${pct ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}</div>
    <b title="${esc(d.title || "Untitled")}">${esc(d.title || "Untitled")}</b><span>${esc([timeLeft(d), d.pages > 1 ? d.pages + " pages" : "", where].filter(Boolean).join(" · "))}</span>`;
  card.querySelector("img").onerror = (e) => { e.target.replaceWith(Object.assign(document.createElement("div"), { className: "ph", textContent: (d.title || "?").trim()[0] || "?" })); };
  card.onclick = () => openDoc({ id: d.id });
  card.oncontextmenu = (e) => { e.preventDefault(); menuAt(e, [["Open", () => openDoc({ id: d.id })], [inQ ? "Remove from Up next" : "Add to Up next", () => card.querySelector(".qadd").click()], ["Move to…", null, moveTargets([d.id], [])], ["-"], ["Remove from library", () => card.querySelector(".rm").click()]]); };
  card.querySelector(".qadd").onclick = async (e) => {
    e.stopPropagation();
    const on = (S.queue || []).includes(d.id);
    S.queue = (await api("/api/queue", { op: on ? "remove" : "add", id: d.id })).queue;
    e.target.classList.toggle("on", !on); e.target.textContent = on ? "+ Up next" : "✓ Up next";
    toast(on ? "Removed from Up next" : `Added to Up next (${S.queue.length})`, 1500);
  };
  card.querySelector(".rm").onclick = async (e) => {
    e.stopPropagation();
    if (!confirm(`Remove “${d.title}” from your library? The file itself is not touched.`)) return;
    await api("/api/forget", { id: d.id }); S.docs = S.docs.filter((x) => x.id !== d.id); renderLibrary();
  };
  dragSource(card, { docs: [d.id] });
  return card;
}
// ---- drag & drop between folders
let dragging = null;
function dragSource(el, what) {
  el.addEventListener("dragstart", (e) => { dragging = what; el.classList.add("dragging-me"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", "aloud"); });
  el.addEventListener("dragend", () => { dragging = null; el.classList.remove("dragging-me"); document.querySelectorAll(".drop-on").forEach((x) => x.classList.remove("drop-on")); });
}
function dropTarget(el, fid) {
  el.addEventListener("dragover", (e) => { if (!dragging) return; if (dragging.folders?.includes(fid)) return; e.preventDefault(); e.stopPropagation(); el.classList.add("drop-on"); });
  el.addEventListener("dragleave", () => el.classList.remove("drop-on"));
  el.addEventListener("drop", (e) => { if (!dragging) return; e.preventDefault(); e.stopPropagation(); el.classList.remove("drop-on"); const w = dragging; dragging = null; moveTo(fid, w.docs || [], w.folders || []); });
}
// ---- folder actions
async function newFolder() {
  const r = await api("/api/folder", { op: "create", name: "New folder", parent: libFolder }); S.folders = r.folders;
  renderLibrary();
  const t = document.querySelector(`.card.folder[data-f="${r.id}"]`); if (t) renameFolder(t, r.id, true);
}
function renameFolder(tile, fid, isNew) {
  const b = tile.querySelector(".fname"); tile.classList.add("editing");
  b.contentEditable = "true"; b.focus();
  const range = document.createRange(); range.selectNodeContents(b); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    b.contentEditable = "false"; tile.classList.remove("editing");
    const name = b.textContent.trim();
    if (save && name) { S.folders = (await api("/api/folder", { op: "rename", id: fid, name })).folders; }
    else if (!save && isNew) { S.folders = (await api("/api/folder", { op: "delete", id: fid })).folders; }
    renderLibrary();
  };
  b.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter") { e.preventDefault(); finish(true); } if (e.key === "Escape") { e.preventDefault(); finish(false); } };
  b.onblur = () => finish(true);
  b.onclick = (e) => e.stopPropagation();
}
async function deleteFolder(fid) {
  const f = S.folders[fid], n = folderCount(fid);
  if (n && !confirm(`Delete the folder “${f.name}”? Its ${n} item${n === 1 ? "" : "s"} move to ${f.parent ? "“" + S.folders[f.parent].name + "”" : "the Library"}. No files are deleted.`)) return;
  S.folders = (await api("/api/folder", { op: "delete", id: fid })).folders;
  const r = await api("/api/state"); S.docs = r.docs;
  renderLibrary();
}
function moveTargets(docs, folders) {
  // every folder (indented), minus the ones being moved and their insides
  const blocked = new Set(folders);
  const out = [["Library (top level)", () => moveTo(null, docs, folders)]];
  const walk = (parent, depth) => {
    for (const [id, f] of Object.entries(S.folders).filter(([, f]) => (f.parent || null) === parent).sort((a, b) => a[1].name.localeCompare(b[1].name))) {
      if (blocked.has(id)) continue;
      out.push([" ".repeat(depth) + "📁 " + f.name, () => moveTo(id, docs, folders)]);
      walk(id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}
// ---- small context menu
function menuAt(e, items) {
  let m = $("#ctxMenu"); if (!m) { m = document.createElement("div"); m.id = "ctxMenu"; document.body.appendChild(m); }
  const build = (list) => list.map(([label, fn, sub], i) => label === "-" ? `<hr>` : `<button data-i="${i}" class="${sub ? "has-sub" : ""}">${esc(label)}${sub ? "<span>›</span>" : ""}</button>`).join("");
  let level = items;
  const show = (list) => { level = list; m.innerHTML = build(list); };
  show(items);
  m.classList.add("show");
  const W = innerWidth, H = innerHeight;
  m.style.left = Math.min(e.clientX, W - 240) + "px"; m.style.top = Math.min(e.clientY, H - Math.min(400, m.offsetHeight) - 10) + "px";
  m.onclick = (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    const [, fn, sub] = level[+b.dataset.i];
    if (sub) { show(sub); return; }
    m.classList.remove("show"); fn && fn();
  };
}
document.addEventListener("mousedown", (e) => { if (!e.target.closest("#ctxMenu")) $("#ctxMenu")?.classList.remove("show"); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#ctxMenu")?.classList.remove("show"); });
async function chooseFile() {
  let path = null;
  if (window.pywebview?.api) path = await window.pywebview.api.choose_file();
  else path = prompt("Path to a PDF:");
  if (path) openDoc({ path });
}
$("#openBtn").onclick = chooseFile; $("#openBig").onclick = chooseFile;

// ---------------------------------------------------------------- paste & drop
async function readText(text) {
  text = (text || "").trim();
  if (!text) return;
  try { const r = await api("/api/paste", { text }); $("#pasteDlg").classList.remove("show"); await openDoc({ path: r.path }, null, true); }
  catch (e) {
    if (e.signin && window.pywebview?.api?.open_signin) {
      if (confirm(e.message + "\n\nIf you subscribe, sign in to the site once in ALOUD's own browser window, close it, then paste the link again. ALOUD will then read the whole article.\n\nSign in now?")) {
        $("#pasteDlg").classList.remove("show");
        window.pywebview.api.open_signin(e.signin);
      }
      return;
    }
    toast(/^\s*(https?:\/\/|www\.)/i.test(text) ? e.message + " If you can see it in your browser, save it as a PDF (⌘P → Save as PDF) or copy the text, and give that to ALOUD." : "Couldn't read that: " + e.message, 7000); }
}
function showPaste() { $("#pasteDlg").classList.add("show"); $("#pasteText").value = ""; setTimeout(() => $("#pasteText").focus(), 30); }
$("#pasteBtn").onclick = showPaste;
(() => {
  const ta = $("#pasteInline"), box = $("#pasteBox");
  const fit = () => { ta.style.height = "auto"; ta.style.height = Math.min(160, ta.scrollHeight) + "px"; box.classList.toggle("has", !!ta.value.trim()); };
  const go = async () => {
    if (!ta.value.trim() || box.classList.contains("busy")) return;
    box.classList.add("busy"); setStatus("Reading…");
    try { await readText(ta.value); if (!document.body.classList.contains("lib")) { ta.value = ""; fit(); } } finally { box.classList.remove("busy"); setStatus(""); }
  };
  ta.addEventListener("input", fit);
  ta.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); } if (e.key === "Escape") ta.blur(); });
  $("#pasteInlineGo").onclick = (e) => { e.preventDefault(); go(); };
})();
$("#pasteCancel").onclick = () => $("#pasteDlg").classList.remove("show");
$("#pasteGo").onclick = () => readText($("#pasteText").value);
$("#pasteText").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); readText(e.target.value); } if (e.key === "Escape") $("#pasteDlg").classList.remove("show"); });
$("#pasteDlg").addEventListener("mousedown", (e) => { if (e.target.id === "pasteDlg") $("#pasteDlg").classList.remove("show"); });
document.addEventListener("paste", (e) => {
  if (e.target.matches("input, textarea")) return;
  const t = e.clipboardData?.getData("text/plain");
  if (t && t.trim().length > 1) { e.preventDefault(); toast(/^\s*(https?:\/\/|www\.)\S+\s*$/i.test(t) ? "Fetching the article…" : "Reading what you pasted…"); readText(t); }
});
let dragN = 0;
// "Drop to read" is only for files dragged in from Finder, never for moving cards inside the library
const fileDrag = (e) => !dragging && [...(e.dataTransfer?.types || [])].includes("Files");
const endFileDrag = () => { dragN = 0; document.body.classList.remove("dragging"); };
document.addEventListener("dragenter", (e) => { if (!fileDrag(e)) return; e.preventDefault(); dragN++; document.body.classList.add("dragging"); });
document.addEventListener("dragleave", (e) => { if (!fileDrag(e)) return; if (--dragN <= 0) endFileDrag(); });
document.addEventListener("dragend", endFileDrag, true);
document.addEventListener("drop", endFileDrag, true);                 // runs first, even when a folder handles the drop
window.addEventListener("blur", endFileDrag);
document.addEventListener("mousemove", (e) => { if (!e.buttons && document.body.classList.contains("dragging")) endFileDrag(); });
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault(); dragN = 0; document.body.classList.remove("dragging");
  const f = e.dataTransfer?.files?.[0];
  if (f && f.pywebviewFullPath) { window._dropOpened = f.pywebviewFullPath; return openDoc({ path: f.pywebviewFullPath }); }
  if (f) { setTimeout(() => { if (!window._dropHandled) toast("Use Open… for that file"); window._dropHandled = false; }, 600); return; }
  const t = e.dataTransfer?.getData("text/plain");
  if (t) readText(t);
});
window.aloudOpen = (path) => { window._dropHandled = true; if (window._dropOpened === path) return; openDoc({ path }); };
$("#libBtn").onclick = showLibrary;
$("#tocBtn").onclick = () => document.body.classList.toggle("notoc");

// ---------------------------------------------------------------- document
async function openDoc(q, atPara = null, autoplay = false) {
  pause(); token++;
  resetFigurePreview();
  setStatus("Opening…");
  const qs = q.id ? `id=${encodeURIComponent(q.id)}` : `path=${encodeURIComponent(q.path)}`;
  let doc;
  let polling = true;
  (async () => { await sleep(1200); while (polling) { try { const st = await api("/api/status"); if (polling && st.msg) setStatus(st.msg); } catch (_) {} await sleep(800); } })();
  try { doc = await api(`/api/doc?${qs}`); } catch (e) { polling = false; setStatus(""); toast("Couldn't open: " + e.message, 4000); return; }
  finally { polling = false; }
  setStatus("");
  if (!doc.units.length) { toast("No readable text found in this file (is it a scanned PDF?)", 5000); return; }
  D = doc; metaCache.clear(); inflight.clear(); spareFor = null;
  if ((S.queue || []).includes(D.id)) api("/api/queue", { op: "remove", id: D.id }).then((r) => { S.queue = r.queue; updateQueueBtn(); }).catch(() => {});
  document.body.classList.remove("lib");
  $("#title").textContent = D.title; document.title = D.title + " — ALOUD";
  renderDoc(); renderToc(); renderPdf(); applyView(); buildSettings();
  watchEquations(); loadNotes();
  if (D.lang && D.lang !== "en") toast(S.defaultVoice[D.lang] ? `${LANG_NAME[D.lang]} document — reading with ${S.voices.find((v) => v[0] === voice())?.[1] || voice()}` : `${LANG_NAME[D.lang] || D.lang} document — no ${LANG_NAME[D.lang] || ""} voice is available, so it's read with an English voice`, 3800);
  if (atPara != null) { cur = unitAtPara(atPara); resumeT = 0; }
  else {
    const pos = D.pos || {};
    cur = (pos.unit != null && D.units[pos.unit] && D.units[pos.unit].p === pos.para) ? pos.unit : unitAtPara(pos.para || 0);
    resumeT = pos.t || 0;
    if (pos.unit && cur > 0) { const sec = sectionFor(paraOf(cur)); toast(`Continuing from ${sec ? "“" + sec.title + "”" : "where you left off"} — press Space to listen`, 3200); }
  }
  markCurrent(); scrollToPara(paraOf(cur), false); updateInfo();
  if (autoplay) startAt(cur, 0); else prefetch(cur);
}
function unitAtPara(i) {
  for (let j = i; j < D.paras.length; j++) if (D.paras[j].units?.length) return D.paras[j].units[0];
  return D.units.length - 1;
}
function cropUrl([n, x0, y0, x1, y1], width) {
  return `/api/crop?doc=${D.id}&n=${n}&r=${x0},${y0},${x1},${y1}&w=${width}`;
}
function renderDoc() {
  const t = $("#text"); t.innerHTML = ""; els = [];
  const frag = document.createDocumentFragment();
  D.paras.forEach((p, i) => {
    const tag = p.kind === "h" ? "h" + Math.min(3, p.level || 1) : "p";
    const el = document.createElement(tag);
    el.dataset.i = i;
    if (p.kind === "fig") {                        // a picture or diagram from the document
      const f = document.createElement("figure"); f.className = "fig"; f.dataset.i = i;
      const [n, x0, y0, x1, y1] = p.fig;
      const w = Math.min(1600, Math.round((x1 - x0) * 2.4));
      f.innerHTML = `<img loading="lazy" alt="" src="${cropUrl(p.fig, w)}" style="aspect-ratio:${(x1 - x0).toFixed(1)} / ${(y1 - y0).toFixed(1)}; max-width:${Math.round((x1 - x0) * 1.35)}px">`;
      f.querySelector("img").onclick = (e) => { e.stopPropagation(); if (hasPdf()) { setView("split"); pdfScrollTo(n, y0); } };
      els.push(f); frag.appendChild(f); return;
    }
    if (p.kind === "skip") el.className = "skip";
    if (p.eq) { el.className = "eq"; el.title = p.eq.number ? `Equation ${p.eq.number}` : "Equation"; }
    el.innerHTML = paraHTML(i);
    els.push(el); frag.appendChild(el);
  });
  t.appendChild(frag);
  $("#reader").scrollTop = 0;
}
function paraHTML(i) {
  const p = D.paras[i];
  if (p.eqTex && window.katex) {                       // equations read by ALOUD: typeset them
    try { els[i]?.classList.add("typeset"); return katex.renderToString(p.eqTex, { displayMode: true, throwOnError: false, output: "html" }); }
    catch (e) { /* fall back to the PDF's characters */ }
  }
  const ivs = [];
  for (const [s, e, v] of p.spans || []) if (v === null) ivs.push([s, e, "c"]);
  for (const u of p.units || []) {
    const m = metaCache.get(voice() + "|" + u);
    if (m) m.words.forEach((w, k) => ivs.push([w[0], w[1], k, u]));
  }
  ivs.sort((a, b) => a[0] - b[0]);
  let out = "", pos = 0;
  for (const [s, e, k, u] of ivs) {
    if (s < pos) continue;
    out += esc(p.text.slice(pos, s));
    const inner = esc(p.text.slice(s, e));
    out += k === "c" ? `<span class="cite" data-s="${s}" data-e="${e}">${inner}</span>` : `<span class="w" data-u="${u}" data-k="${k}">${inner}</span>`;
    pos = e;
  }
  return out + esc(p.text.slice(pos));
}
let eqTimer = null;
function watchEquations() {
  clearInterval(eqTimer);
  if (!D.paras.some((p) => p.eq)) return;
  const id = D.id;
  const pull = async () => {
    if (!D || D.id !== id) return clearInterval(eqTimer);
    try {
      const got = await api(`/api/eqs?doc=${id}`);
      for (const [i, tex] of Object.entries(got)) if (D.paras[i] && D.paras[i].eqTex !== tex) { D.paras[i].eqTex = tex; rerenderPara(+i); }
      if (D.paras.every((p) => !p.eq || p.eqTex)) clearInterval(eqTimer);
    } catch (e) { /* try again later */ }
  };
  pull(); eqTimer = setInterval(pull, 8000);
}
function rerenderPara(i) {
  if (!els[i]) return;
  els[i].innerHTML = paraHTML(i);
  if (finds.length && $("#find").classList.contains("show") && finds.some((f) => f.p === i)) { clearTimeout(rerenderPara._f); rerenderPara._f = setTimeout(() => { const keep = findI; runFind(true); findI = keep; showFindQuiet(); }, 50); }
  if (paraOf(cur) === i) { wordEl = null; wordK = -1; }
}
function renderToc() {
  const n = $("#toc"); n.innerHTML = "";
  const top = document.createElement("a"); top.textContent = "Beginning"; top.className = "l1"; top.dataset.p = 0;
  n.appendChild(top);
  for (const s of D.sections) {
    const a = document.createElement("a"); a.textContent = s.title; a.className = "l" + Math.min(3, s.level);
    if (D.paras[s.para]?.refs) a.classList.add("refs");
    a.dataset.p = s.para; n.appendChild(a);
  }
  n.onclick = (e) => { const a = e.target.closest("a"); if (!a || a.closest("#notesList")) return; const u = unitAtPara(+a.dataset.p); jumpTo(u); scrollToPara(+a.dataset.p, true); };
}
function sectionFor(i) {
  let s = null; for (const x of D.sections) { if (x.para <= i) s = x; else break; } return s;
}
function nextUnit(u, dir) { const n = u + dir; return n >= 0 && n < D.units.length ? n : null; }
function nextParaUnit(u, dir) {
  const p = paraOf(u);
  if (dir > 0) { for (let j = p + 1; j < D.paras.length; j++) if (D.paras[j].units?.length) return D.paras[j].units[0]; return null; }
  const first = D.paras[p].units[0];
  if (u !== first) return first;
  for (let j = p - 1; j >= 0; j--) if (D.paras[j].units?.length) return D.paras[j].units[0];
  return null;
}

// ---------------------------------------------------------------- synthesis
function metaKey(u) { return voice() + "|" + u; }
function getMeta(u, prio = 0) {
  const k = metaKey(u);
  if (metaCache.has(k)) return Promise.resolve(metaCache.get(k));
  if (inflight.has(k) && prio > 0) return inflight.get(k);
  const vc = voice(), docId = D.id;
  const pr = api(`/api/para?doc=${docId}&i=${u}&voice=${vc}&prio=${prio}`).then((m) => {
    inflight.delete(k);
    if (!D || D.id !== docId) return m;
    m.sent = sentenceStarts(D.paras[paraOf(u)], m.words);
    metaCache.set(vc + "|" + u, m);
    if (vc === voice()) rerenderPara(paraOf(u));
    return m;
  }, (e) => { inflight.delete(k); throw e; });
  inflight.set(k, pr);
  return pr;
}
function sentenceStarts(p, words) {
  const out = [0];
  for (let k = 1; k < words.length; k++) {
    const prev = p.text.slice(words[k - 1][0], words[k][0]);
    if (/[.?!][”’)\]]*\s/.test(prev) && !/\b(?:e\.g|i\.e|al|U\.S|Mr|Dr|vs|Fig|No)\.\s*$/.test(prev)) out.push(k);
  }
  return out;
}
async function getBlob(m) {
  const clean = S.settings.clean ? 1 : 0, k = m.key + "|" + clean;
  if (blobCache.has(k)) return blobCache.get(k);
  const b = await (await fetch(`/audio/${m.key}.wav?clean=${clean}`)).blob();
  const url = URL.createObjectURL(b);
  blobCache.set(k, url);
  if (blobCache.size > 40) {
    const [oldKey, oldUrl] = blobCache.entries().next().value;
    if (oldUrl !== audio.src && oldUrl !== spare.src && oldUrl !== captionRun?.clip.src) URL.revokeObjectURL(oldUrl);
    blobCache.delete(oldKey);
  }
  return url;
}
function queueOnly(u, prio) {
  const k = metaKey(u);
  if (metaCache.has(k) || inflight.has(k)) return;
  fetch(`/api/para?doc=${D.id}&i=${u}&voice=${voice()}&prefetch=${prio}`).catch(() => {});
}
function prefetch(u) {
  getMeta(u, 1).catch(() => {});
  let j = u;
  const ahead = 6;
  for (let n = 1; n <= ahead && (j = nextUnit(j, 1)) != null; n++) {
    if (n <= 2) getMeta(j, n + 1).then(getBlob).then(() => { if (n === 1) primeNext(); }).catch(() => {});
    else queueOnly(j, n + 1);   // further ahead: just queue the work (an open request each would block the app)
  }
}

// ---------------------------------------------------------------- playback
function tellMac(on) {
  try {
    if (!window.pywebview?.api?.now_playing || !D) return;
    const sec = cur >= 0 ? sectionFor(paraOf(cur)) : null;
    window.pywebview.api.now_playing(sec ? sec.title : D.title, D.title, !!on);
  } catch (_) {}
}
function setPlaying(on) {
  tellMac(on);
  $("#playIcon").innerHTML = on ? '<path d="M6 4h4v16H6zM14 4h4v16h-4z"/>' : '<path d="M7 4.5v15l13-7.5z"/>';
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = on ? "playing" : "paused";
}
function setLoading(on) { $("#play").classList.toggle("loading", on); setStatus(on ? (S.ready === false ? "Loading voice…" : "Preparing…") : ""); }

async function startAt(u, t = 0, wordIndex = null, charPos = null) {
  if (!D || u == null) return;
  cancelFigureCaption();
  const my = ++token;
  sample.pause();
  const paraChanged = cur < 0 || paraOf(cur) !== paraOf(u);
  cur = u; markCurrent(); updateInfo();
  audio.pause();
  let m = metaCache.get(metaKey(u));
  if (!m) {
    setLoading(true); setPlaying(true);
    for (let tries = 0; ; tries++) {
      try { m = await getMeta(u, 0); break; }
      catch (e) { if (tries >= 2 || my !== token) { if (my === token) { setLoading(false); setPlaying(false); toast("Speech failed: " + e.message, 4000); } return; } await sleep(300); }
    }
  }
  if (my !== token) return;
  if (charPos != null) wordIndex = wordAtChar(m, charPos);
  if (wordIndex != null && m.words[wordIndex]) t = Math.max(0, m.words[wordIndex][2] - 0.05);
  const url = await getBlob(m);
  if (my !== token) return;
  setLoading(false);
  if (audio.src !== url) audio.src = url;
  audio.playbackRate = S.settings.rate;
  try { audio.currentTime = Math.min(t, Math.max(0, m.dur - 0.2)); } catch (_) {}
  if (paraChanged || wordIndex != null) { userScrolled = false; $("#back").classList.remove("show"); }
  try { await audio.play(); } catch (e) { if (my === token) setPlaying(false); return; }
  if (my !== token) return;
  setPlaying(true); updateMediaSession(); prefetch(u); tick(); primeNext();
}
function pause() { cancelFigureCaption(); token++; audio.pause(); sample.pause(); setLoading(false); setPlaying(false); saveProgress(true); }
function toggle() {
  if (!D) return;
  if (isBusy()) { pause(); return; }
  const m = metaCache.get(metaKey(cur));
  if (m && audio.src && audio.currentTime > 0 && audio.currentTime < m.dur - 0.05) {
    const my = ++token; audio.play().then(() => { if (my === token) { setPlaying(true); tick(); } });
  } else { startAt(cur, resumeT); resumeT = 0; }
}
function restartCurrent() {
  const busy = captionRun?.resume ?? isBusy();
  if (cancelFigureCaption()) setPlaying(false);
  spareFor = null; spare.removeAttribute("src");
  for (const u of blobCache.values()) URL.revokeObjectURL(u);
  blobCache.clear();
  if (!D) return;
  const t = audio.currentTime;
  const sameVoiceMeta = metaCache.get(metaKey(cur));
  const k = sameVoiceMeta ? curWordIndex() : null;
  D.paras.forEach((p, i) => { if (p.units?.length) rerenderPara(i); });
  api("/api/drop_prefetch", {}).catch(() => {});
  if (busy) startAt(cur, sameVoiceMeta ? t : 0, null);
  else { audio.removeAttribute("src"); prefetch(cur); }
}
function onEnded() {
  if (captionRun) return;
  const n = nextUnit(cur, 1);
  if (n == null) { setPlaying(false); playNextInQueue(); return; }
  if (spareFor && spareFor.u === n && spareFor.voice === voice() && spare.src === spareFor.url && spare.readyState >= 2) {
    const my = ++token;
    window.__swaps = (window.__swaps || 0) + 1;
    const old = audio;
    [audio, spare] = [spare, audio]; spareFor = null;
    setTimeout(() => { if (old !== audio) old.pause(); }, 450);   // let the old clip's silence finish
    cur = n; markCurrent(); updateInfo();
    audio.playbackRate = S.settings.rate;
    try { audio.currentTime = 0; } catch (_) {}
    audio.play().then(() => {
      if (my !== token) return;
      setPlaying(true); updateMediaSession(); prefetch(n); tick(); primeNext();
    }, () => startAt(n, 0));
    return;
  }
  startAt(n, 0);
}
async function primeNext() {
  if (!D || cur < 0) return;
  const n = nextUnit(cur, 1), m = n == null ? null : metaCache.get(metaKey(n));
  if (!m) return;
  const url = await getBlob(m).catch(() => null);
  if (!url || nextUnit(cur, 1) !== n) return;
  if (spare.src !== url) { spare.src = url; spare.load(); }
  spare.playbackRate = S.settings.rate;
  spareFor = { u: n, url, voice: voice() };
}

function jumpTo(u, wordIndex = null) {
  if (u == null) return;
  if (Math.abs(u - cur) > 6) api("/api/drop_prefetch", {}).catch(() => {});
  resumeT = 0;
  if (isBusy() || wordIndex != null) startAt(u, 0, wordIndex);
  else { cur = u; markCurrent(); updateInfo(); prefetch(u); saveProgress(true); }
}
function curWordIndex() {
  const m = metaCache.get(metaKey(cur)); if (!m) return -1;
  const t = audio.currentTime, w = m.words;
  let lo = 0, hi = w.length - 1, k = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (w[mid][2] <= t + 0.02) { k = mid; lo = mid + 1; } else hi = mid - 1; }
  return k;
}
function sentence(dir) {
  if (!D) return;
  const m = metaCache.get(metaKey(cur));
  if (!m || !m.words.length) { const n = nextUnit(cur, dir); if (n != null) jumpTo(n); return; }
  const k = Math.max(0, curWordIndex());
  let si = 0; for (let x = 0; x < m.sent.length; x++) if (m.sent[x] <= k) si = x;
  if (dir > 0) {
    if (si + 1 < m.sent.length) seekWord(m, m.sent[si + 1]);
    else { const n = nextUnit(cur, 1); if (n != null) startAt(n, 0); }
  } else {
    const startT = m.words[m.sent[si]][2];
    if (audio.currentTime - startT > 1.2) seekWord(m, m.sent[si]);
    else if (si > 0) seekWord(m, m.sent[si - 1]);
    else {
      const p = nextUnit(cur, -1);
      if (p != null) { const pm = metaCache.get(metaKey(p)); startAt(p, 0, pm && pm.sent.length ? pm.sent[pm.sent.length - 1] : null); }
    }
  }
}
function wordAtChar(m, c) {
  let k = m.words.findIndex((w) => w[1] > c);
  return k < 0 ? null : k;
}
function seekWord(m, k) {
  cancelFigureCaption();
  audio.currentTime = Math.max(0, m.words[k][2] - 0.05);
  if (audio.paused) { const my = ++token; audio.play().then(() => { if (my === token) { setPlaying(true); tick(); } }); }
  userScrolled = false; $("#back").classList.remove("show");
}

// ---------------------------------------------------------------- figure previews
let shownFigure = null, lastFigureReference = null;
const figurePreview = $("#figurePreview");
const figureArt = $("#figureArt"), figureImage = $("#figureImage"), figureRead = $("#figureRead");

function trackFigureReference(k) {
  if (audio.paused || S.settings.figure_previews === false || k < 0) return;
  const p = D.paras[paraOf(cur)];
  const word = p.figure_refs && metaCache.get(metaKey(cur))?.words[k];
  const ref = word ? p.figure_refs.find(([s, e]) => word[0] < e && word[1] > s) ?? null : null;
  if (ref === lastFigureReference) return;
  lastFigureReference = ref;
  if (!ref || figurePreview.classList.contains("expanded")) return;
  const [, , i, panel] = ref;
  if (shownFigure?.i === i && shownFigure.panel === panel) return;

  const figure = D.paras[i].figure;
  shownFigure = { i, panel };
  const [n, x0, y0, x1, y1] = figure.crop;
  $("#figureTitle").textContent = `Figure ${figure.number}${panel}`;
  $("#figurePage").textContent = `Page ${n + 1}`;
  $("#figureCaption").textContent = figure.caption;
  figureRead.hidden = !figure.caption;
  const outline = $("#figureOutline"), box = figure.panels[panel];
  outline.hidden = !box;
  if (box) {
    const [left, top, width, height] = box.map((v) => `${v * 100}%`);
    Object.assign(outline.style, { left, top, width, height });
  }
  const src = cropUrl(figure.crop, Math.min(2400, Math.round((x1 - x0) * 4)));
  if (figureImage.getAttribute("src") !== src) {
    figureArt.style.setProperty("--figure-ratio", (x1 - x0) / (y1 - y0));
    figureImage.src = src;
  }
  figurePreview.hidden = false;
}
figureImage.onload = () => {
  if (figureImage.naturalHeight) figureArt.style.setProperty("--figure-ratio", figureImage.naturalWidth / figureImage.naturalHeight);
};
figureImage.onerror = () => {
  if (figurePreview.hidden) return;
  figureImage.removeAttribute("src");
  dismissFigurePreview();
  toast("Couldn’t load the figure");
};

function expandFigurePreview(expanded) {
  figurePreview.classList.toggle("expanded", expanded);
  const zoom = $("#figureZoom"), label = expanded ? "Reduce figure" : "Enlarge figure";
  zoom.setAttribute("aria-expanded", String(expanded));
  zoom.setAttribute("aria-label", label);
  zoom.title = label;
  $("#figureZoomIcon").textContent = expanded ? "⤡" : "⤢";
}
function dismissFigurePreview() {
  finishFigureCaption();
  expandFigurePreview(false);
  figurePreview.hidden = true;
  shownFigure = null;
}
function resetFigurePreview() {
  dismissFigurePreview();
  lastFigureReference = null;
}
$("#figureClose").onclick = dismissFigurePreview;
$("#figureZoom").onclick = () => expandFigurePreview(!figurePreview.classList.contains("expanded"));

function cancelFigureCaption() {
  const run = captionRun;
  if (!run) return null;
  captionRun = null;
  run.clip.onended = null;
  run.clip.pause();
  figureRead.textContent = "Read caption";
  figureRead.removeAttribute("aria-busy");
  return run;
}
function finishFigureCaption(expected = captionRun) {
  if (!expected || expected !== captionRun) return;
  const run = cancelFigureCaption();
  setPlaying(false);
  if (run.resume && D === run.doc && cur === run.unit) startAt(run.unit, run.time);
}
figureRead.onclick = async () => {
  if (captionRun) { finishFigureCaption(); return; }
  if (!D || cur < 0 || !shownFigure) return;
  const word = audio.paused ? null : metaCache.get(metaKey(cur))?.words[curWordIndex()];
  const time = word ? Math.max(0, word[2] - 0.05) : (audio.paused ? 0 : audio.currentTime);
  const run = { doc: D, unit: cur, time, resume: isBusy(), clip: new Audio() };
  const { i } = shownFigure;
  pause();
  captionRun = run;
  figureRead.textContent = run.resume ? "Back to reading" : "Stop caption";
  figureRead.setAttribute("aria-busy", "true");
  setPlaying(true);
  try {
    const meta = await api(`/api/figure-caption?doc=${D.id}&i=${i}&voice=${encodeURIComponent(voice())}`);
    if (captionRun !== run) return;
    const src = await getBlob(meta);
    if (captionRun !== run) return;
    run.clip.src = src;
    run.clip.playbackRate = S.settings.rate;
    run.clip.onended = () => finishFigureCaption(run);
    await run.clip.play();
    if (captionRun === run) figureRead.removeAttribute("aria-busy");
  } catch (e) {
    if (captionRun !== run) return;
    toast("Couldn’t read the caption: " + e.message, 3200);
    finishFigureCaption(run);
  }
};

// ---------------------------------------------------------------- highlighting & follow
let markedPara = -1;
function markCurrent() {
  const p = paraOf(cur);
  if (markedPara !== p) {
    if (els[markedPara]) els[markedPara].classList.remove("cur");
    if (els[p]) els[p].classList.add("cur");
    markedPara = p;
  }
  if (wordEl) wordEl.classList.remove("on"); wordEl = null; wordK = -1;
  pdfUnit();
  const s = sectionFor(p);
  const a = s ? document.querySelector(`#toc a[data-p="${s.para}"]`) : $("#toc a");
  if (a && !a.classList.contains("on")) {
    document.querySelectorAll("#toc a.on").forEach((x) => x.classList.remove("on"));
    a.classList.add("on"); a.scrollIntoView({ block: "nearest" });
  }
}
let raf = 0, tickTimer = 0;
function tick() {
  // A steady timer drives the highlight; animation frames add smoothness when the
  // window is visible (macOS stops them for hidden windows, which froze the highlight).
  cancelAnimationFrame(raf); clearInterval(tickTimer);
  tickTimer = setInterval(() => { if (audio.paused) clearInterval(tickTimer); else step(); }, 25);
  const frame = () => { if (audio.paused) return; step(); raf = requestAnimationFrame(frame); };
  raf = requestAnimationFrame(frame);
}
// seconds the Mac's audio output needs to get a new clip going; when the window is hidden
// macOS checks less often, so hand over a little earlier (still inside the clip's own pause)
const startLag = () => (document.hidden ? 0.45 : 0.3);
function step() {
  {
    if (!D || cur < 0) return;
    // hand over to the preloaded next passage during this one's trailing pause
    if (!audio.paused && spareFor && audio.duration && audio.duration - audio.currentTime <= startLag() * audio.playbackRate
        && nextUnit(cur, 1) === spareFor.u) { window.__early = (window.__early || 0) + 1; onEnded(); return; }
    const k = curWordIndex();
    if (k !== wordK || (k >= 0 && !wordEl)) {
      if (wordEl) wordEl.classList.remove("on");
      wordK = k;
      const pe = els[paraOf(cur)];
      wordEl = k >= 0 && pe ? pe.querySelector(`.w[data-u="${cur}"][data-k="${k}"]`) : null;
      if (wordEl) { wordEl.classList.add("on"); if (textView()) follow(wordEl); }
      pdfWord(k);
    }
    trackFigureReference(k);
    saveProgress(false);
  }
}
function follow(el, force) {
  if (!force && (!S.settings.follow || userScrolled)) return;
  const R = $("#reader"), rr = R.getBoundingClientRect(), r = el.getBoundingClientRect();
  if (force || r.top < rr.top + rr.height * 0.15 || r.bottom > rr.top + rr.height * 0.65) {
    autoScrollUntil = performance.now() + 800;
    R.scrollTo({ top: R.scrollTop + r.top - rr.top - rr.height * 0.33, behavior: "smooth" });
  }
}
function scrollToPara(i, smooth) {
  pdfScrollToPara(i, smooth);
  const el = els[i]; if (!el) return;
  const R = $("#reader");
  autoScrollUntil = performance.now() + 800;
  R.scrollTo({ top: el.offsetTop - R.clientHeight * 0.28, behavior: smooth ? "smooth" : "auto" });
}
function userScroll() {
  if (performance.now() < autoScrollUntil || !isBusy()) return;
  userScrolled = true; $("#back").classList.add("show");
}
$("#reader").addEventListener("wheel", userScroll, { passive: true });
$("#reader").addEventListener("touchmove", userScroll, { passive: true });
$("#pdfview").addEventListener("wheel", userScroll, { passive: true });
$("#back").onclick = () => {
  userScrolled = false; $("#back").classList.remove("show");
  if (wordEl) follow(wordEl, true); else scrollToPara(paraOf(cur), true);
  pdfFollow(true);
};

$("#text").addEventListener("click", (e) => {
  if (String(window.getSelection())) return;
  const pe = e.target.closest("[data-i]"); if (!pe || pe.classList.contains("skip")) return;
  const i = +pe.dataset.i, w = e.target.closest(".w");
  if (w) {
    const u = +w.dataset.u, k = +w.dataset.k;
    if (u === cur && !audio.paused) seekWord(metaCache.get(metaKey(u)), k);
    else startAt(u, 0, k);
    return;
  }
  const u = D.paras[i].units?.[0];
  if (u == null) return;
  if (paraOf(cur) === i && !isBusy()) toggle(); else startAt(u, 0);
});


// ---------------------------------------------------------------- PDF view
let pgEls = [];        // page elements
let pageIdx = [];      // page -> [[x0, y0, x1, y1, para, charStart]]
let wdEls = [];        // current word highlight(s)
let urEls = [];        // current unit highlight boxes
const hasPdf = () => !!(D && D.pages && D.pages.length);
// web pages remember their own view (they open as the page itself); documents keep yours
const curView = () => (D && D.web ? (S.settings.web_view || "pdf") : (S.settings.view || "split"));
const pdfView = () => hasPdf() && curView() !== "text";
const textView = () => !hasPdf() || curView() !== "pdf";

function applyView() {
  const v = hasPdf() ? curView() : "text";
  document.body.classList.remove("view-text", "view-split", "view-pdf");
  document.body.classList.add("view-" + v);
  document.body.classList.toggle("nopdf", !hasPdf());
  document.querySelectorAll("#viewSeg button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  const pb = document.querySelector('#viewSeg button[data-v="pdf"]'); if (pb) pb.textContent = D && D.web ? "Page" : "PDF";
  document.body.classList.toggle("webdoc", !!(D && D.web));
  $("#zoomVal").textContent = S.settings.pdf_zoom == 1 ? "Fit" : Math.round(S.settings.pdf_zoom * 100) + "%";
  if (hasPdf()) layoutPdf();
}
function setView(v) {
  saveSettings(D && D.web ? { web_view: v } : { view: v }); applyView();
  requestAnimationFrame(() => { pdfFollow(true); if (wordEl && textView()) follow(wordEl, true); else if (textView()) scrollToParaText(paraOf(cur)); });
}
document.querySelectorAll("#viewSeg button").forEach((b) => (b.onclick = () => setView(b.dataset.v)));

function setZoom(z) {
  z = Math.round(Math.min(3, Math.max(0.5, z)) * 100) / 100;
  if (Math.abs(z - 1) < 0.04) z = 1;
  saveSettings({ pdf_zoom: z }); applyView(); pdfFollow(true);
}
$("#zoomIn").onclick = () => setZoom(S.settings.pdf_zoom * 1.15);
$("#zoomOut").onclick = () => setZoom(S.settings.pdf_zoom / 1.15);
$("#zoomVal").onclick = () => setZoom(1);
(() => {
  let saveT = 0, raf = 0, pend = null, base = null;
  const over = (e) => e.target.closest?.("#pdfview") ? "pdf" : e.target.closest?.("#reader") ? "text" : null;
  function apply(kind, factor, cx, cy) {
    if (kind === "pdf") {
      const v = $("#pdfview"), r = v.getBoundingClientRect();
      const fx = (v.scrollLeft + cx - r.left) / Math.max(1, v.scrollWidth), fy = (v.scrollTop + cy - r.top) / Math.max(1, v.scrollHeight);
      let z = Math.round(Math.min(3, Math.max(0.5, S.settings.pdf_zoom * factor)) * 100) / 100;
      if (Math.abs(z - 1) < 0.03) z = 1;
      if (z === S.settings.pdf_zoom) return;
      S.settings.pdf_zoom = z; applyView();
      v.scrollLeft = fx * v.scrollWidth - (cx - r.left); v.scrollTop = fy * v.scrollHeight - (cy - r.top);
      clearTimeout(saveT); saveT = setTimeout(() => saveSettings({ pdf_zoom: S.settings.pdf_zoom }), 400);
    } else {
      const R = $("#reader"), r = R.getBoundingClientRect(), fy = (R.scrollTop + cy - r.top) / Math.max(1, R.scrollHeight);
      const f = Math.round(Math.min(28, Math.max(15, S.settings.font * factor)) * 4) / 4;
      if (f === S.settings.font) return;
      S.settings.font = f; applyLook(); $("#optFont").value = Math.round(f);
      R.scrollTop = fy * R.scrollHeight - (cy - r.top);
      clearTimeout(saveT); saveT = setTimeout(() => saveSettings({ font: S.settings.font }), 400);
    }
  }
  const queue = (kind, factor, cx, cy) => {
    if (pend && pend.kind === kind) { pend.factor *= factor; pend.cx = cx; pend.cy = cy; } else pend = { kind, factor, cx, cy };
    if (!raf) { const run = () => { if (!raf) return; raf = 0; const q = pend; pend = null; if (q) apply(q.kind, q.factor, q.cx, q.cy); }; raf = 1; requestAnimationFrame(run); setTimeout(run, 40); }
  };
  // trackpad pinch arrives as ctrl+wheel in most engines…
  addEventListener("wheel", (e) => {
    if (!e.ctrlKey || !D) return;
    const k = over(e); if (!k) return;
    e.preventDefault();
    queue(k, Math.exp(-e.deltaY * 0.01), e.clientX, e.clientY);
  }, { passive: false });
  // …and as gesture events in WebKit (ALOUD's window)
  addEventListener("gesturestart", (e) => { const k = D && over(e); if (!k) return; e.preventDefault(); base = { k, s: 1 }; }, { passive: false });
  addEventListener("gesturechange", (e) => {
    if (!base) return; e.preventDefault();
    const f = e.scale / base.s; base.s = e.scale;
    queue(base.k, f, e.clientX, e.clientY);
  }, { passive: false });
  addEventListener("gestureend", (e) => { if (base) e.preventDefault(); base = null; }, { passive: false });
})();

function pct(div, x0, y0, x1, y1, W, H, pad = 0) {
  div.style.left = ((x0 - pad) / W * 100) + "%"; div.style.top = ((y0 - pad) / H * 100) + "%";
  div.style.width = ((x1 - x0 + 2 * pad) / W * 100) + "%"; div.style.height = ((y1 - y0 + 2 * pad) / H * 100) + "%";
  return div;
}
function addBox(n, cls, x0, y0, x1, y1, pad) {
  const [W, H] = D.pages[n];
  const d = pct(document.createElement("div"), x0, y0, x1, y1, W, H, pad);
  d.className = cls; pgEls[n].ov.appendChild(d); return d;
}
function inNull(p, s) { return (p.spans || []).some(([a, b, v]) => v === null && s >= a && s < b); }

function renderPdf() {
  const view = $("#pdfview"); view.innerHTML = ""; pgEls = []; pageIdx = []; wdEls = []; urEls = [];
  if (!hasPdf()) return;
  if (D.web) return renderWeb(view);
  D.pages.forEach(([W, H], n) => {
    const pg = document.createElement("div"); pg.className = "pg"; pg.dataset.n = n;
    pg.innerHTML = `<img alt="" draggable="false"><div class="ov"></div><span class="pn">${n + 1}</span>`;
    pg.ov = pg.querySelector(".ov"); pg.img = pg.querySelector("img");
    view.appendChild(pg); pgEls.push(pg); pageIdx.push([]);
  });
  // what isn't read: page furniture, footnotes / equations / tables, citations
  (D.furn || []).forEach((rs, n) => rs.forEach((r) => addBox(n, "nr", ...r, 1)));
  D.paras.forEach((p, i) => {
    if (p.kind === "skip") (p.rects || []).forEach(([n, ...r]) => addBox(n, "nr", ...r, 1));
    for (const b of p.boxes || []) {
      pageIdx[b[2]].push([b[3], b[4], b[5], b[6], i, b[0]]);
      if (inNull(p, b[0])) addBox(b[2], "nr", b[3], b[4], b[5], b[6], 1);
    }
  });
}
function wantWidth(pg) { return Math.min(3000, Math.ceil(pg.clientWidth * (window.devicePixelRatio || 2) / 300) * 300); }
function loadPage(n) {
  const pg = pgEls[n]; if (!pg) return;
  const w = wantWidth(pg);
  if ((pg.loadedW || 0) >= w) return;
  pg.loadedW = w;
  pg.img.src = `/api/page?doc=${D.id}&n=${n}&w=${w}`;
}
function layoutPdf() {
  if (D && D.web) return;
  const view = $("#pdfview");
  if (!pgEls.length || !view.clientWidth) return;
  const frac = view.scrollHeight ? view.scrollTop / view.scrollHeight : 0;
  const size = () => {
    const w = Math.round((view.clientWidth - 36) * S.settings.pdf_zoom);
    pgEls.forEach((pg, n) => { const [W, H] = D.pages[n]; pg.style.width = w + "px"; pg.style.height = Math.round(w * H / W) + "px"; });
  };
  size(); size();   // second pass once the vertical scrollbar has taken its space
  view.scrollTop = frac * view.scrollHeight;
  pgEls.forEach((pg, n) => { if (pg.loadedW) pg.loadedW = 1; });
  loadVisible();
}
function loadVisible() {
  const view = $("#pdfview");
  if (!pgEls.length || !view.clientHeight) return;
  const a = view.scrollTop - 1500, b = view.scrollTop + view.clientHeight + 1500;
  pgEls.forEach((pg, n) => { if (pg.offsetTop + pg.offsetHeight > a && pg.offsetTop < b) loadPage(n); });
}
let lvRaf = 0;
$("#pdfview").addEventListener("scroll", () => { cancelAnimationFrame(lvRaf); lvRaf = requestAnimationFrame(loadVisible); }, { passive: true });
let rsz = 0;
window.addEventListener("resize", () => { clearTimeout(rsz); rsz = setTimeout(() => { if (hasPdf()) layoutPdf(); }, 120); });

function boxesIn(p, s, e) { return (p.boxes || []).filter((b) => b[0] < e && b[1] > s); }
function pdfWord(k) {
  if (!hasPdf()) return;
  if (D.web) return webWord(k);
  const m = metaCache.get(metaKey(cur)), p = D.paras[paraOf(cur)];
  const w = k >= 0 && m ? m.words[k] : null;
  const bs = w ? boxesIn(p, w[0], w[1]) : [];
  while (wdEls.length > bs.length) wdEls.pop().remove();
  bs.forEach((b, i) => {
    let d = wdEls[i];
    if (!d) { d = document.createElement("div"); d.className = "wd"; wdEls.push(d); }
    if (d.parentNode !== pgEls[b[2]].ov) pgEls[b[2]].ov.appendChild(d);
    pct(d, b[3], b[4], b[5], b[6], ...D.pages[b[2]], 1.5);
  });
  if (bs.length) pdfFollow(false);
}
function pdfUnit() {
  if (D && D.web) return webUnit();
  urEls.forEach((d) => d.remove()); urEls = [];
  wdEls.forEach((d) => d.remove()); wdEls = [];
  if (!hasPdf() || cur < 0) return;
  const u = D.units[cur], p = D.paras[u.p];
  const lines = [];
  for (const b of boxesIn(p, u.s, u.e)) {
    if (inNull(p, b[0])) continue;
    const L = lines[lines.length - 1], cy = (b[4] + b[6]) / 2;
    if (L && L[0] === b[2] && Math.abs(cy - (L[2] + L[4]) / 2) < (L[4] - L[2]) * 0.6 && b[3] - L[3] < 40 && b[3] >= L[1] - 2) {
      L[3] = Math.max(L[3], b[5]); L[2] = Math.min(L[2], b[4]); L[4] = Math.max(L[4], b[6]);
    } else lines.push([b[2], b[3], b[4], b[5], b[6]]);
  }
  for (const [n, ...r] of lines) urEls.push(addBox(n, "ur", ...r, 2));
}
function pdfScrollTo(n, y, smooth, force, x0, x1) {
  const view = $("#pdfview"), pg = pgEls[n];
  if (!pg || !pdfView() || !view.clientHeight) return;
  const [W, H] = D.pages[n];
  const top = pg.offsetTop + y / H * pg.offsetHeight;
  const rel = top - view.scrollTop;
  if (force || rel < view.clientHeight * 0.12 || rel > view.clientHeight * 0.7) {
    autoScrollUntil = performance.now() + 800;
    const o = { top: top - view.clientHeight * 0.33, behavior: smooth ? "smooth" : "auto" };
    if (x0 != null && view.scrollWidth > view.clientWidth + 4) {
      const l = pg.offsetLeft + x0 / W * pg.offsetWidth, r = pg.offsetLeft + x1 / W * pg.offsetWidth;
      if (l < view.scrollLeft + 20 || r > view.scrollLeft + view.clientWidth - 20) o.left = l - 40;
    }
    view.scrollTo(o);
  }
}
function pdfFollow(force) {
  if (!hasPdf() || cur < 0) return;
  if (D.web) return webFollow(force);
  if (!force && (!S.settings.follow || userScrolled)) return;
  const p = D.paras[paraOf(cur)], u = D.units[cur];
  const m = metaCache.get(metaKey(cur)), w = m && wordK >= 0 ? m.words[wordK] : null;
  const b = (w && boxesIn(p, w[0], w[1])[0]) || boxesIn(p, u.s, u.e)[0];
  if (b) pdfScrollTo(b[2], b[4], true, force, b[3], b[5]);
}
function pdfScrollToPara(i, smooth) {
  if (!hasPdf()) return;
  if (D.web) return webScrollToPara(i, smooth);
  const p = D.paras[i]; if (!p) return;
  const b = (p.boxes || [])[0];
  if (b) return pdfScrollTo(b[2], b[4], smooth, true);
  const r = (p.rects || [])[0];
  if (r) pdfScrollTo(r[0], r[2], smooth, true);
}
function scrollToParaText(i) { const el = els[i]; if (!el) return; const R = $("#reader"); R.scrollTo({ top: el.offsetTop - R.clientHeight * 0.28 }); }

$("#pdfview").addEventListener("click", (e) => {
  const pg = e.target.closest(".pg"); if (!pg || !D) return;
  if (e.ctrlKey || e.button !== 0) return;              // a secondary click: the context-menu handler takes it
  const n = +pg.dataset.n, [W, H] = D.pages[n], r = pg.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width * W, y = (e.clientY - r.top) / r.height * H;
  let best = null, bd = 1e9;
  for (const b of pageIdx[n]) {
    const dx = Math.max(b[0] - x, 0, x - b[2]), dy = Math.max(b[1] - y, 0, y - b[3]);
    const d = dx + dy * 3;
    if (d < bd) { bd = d; best = b; }
  }
  if (!best || bd > 8) {
    const hit = [...pg.ov.querySelectorAll(".nr")].some((d) => { const q = d.getBoundingClientRect(); return e.clientX >= q.left - 3 && e.clientX <= q.right + 3 && e.clientY >= q.top - 3 && e.clientY <= q.bottom + 3; });
    if (hit) toast("That part isn't read aloud (footnote, page number, equation or table)");
    return;
  }
  const [, , , , i, c] = best, p = D.paras[i];
  if (inNull(p, c)) {
    const ct = citeAt(i, c);
    if (ct) { showCite(ct); return; }                   // clicking a citation shows what it refers to
    toast("That part isn't read aloud");
  }
  const u = (p.units || []).find((uu) => D.units[uu].s <= c && c < D.units[uu].e) ?? p.units?.[0];
  if (u == null) return;
  const m = metaCache.get(metaKey(u));
  if (m && u === cur && !audio.paused) { const k = wordAtChar(m, c); if (k != null) seekWord(m, k); return; }
  userScrolled = false; $("#back").classList.remove("show");
  startAt(u, 0, null, c);
});

// ---------------------------------------------------------------- warm what's on screen
// While browsing (not listening), quietly prepare the first passage of each paragraph
// in view, so clicking one starts speaking almost at once.
let warmT = 0;
function warmVisible() {
  clearTimeout(warmT);
  warmT = setTimeout(() => {
    if (!D || isBusy()) return;
    const want = new Set();
    if (textView()) {
      const R = $("#reader"), top = R.scrollTop, bot = top + R.clientHeight;
      let lo = 0, hi = els.length - 1;
      while (lo < hi) { const m = (lo + hi) >> 1; if (els[m].offsetTop + els[m].offsetHeight < top) lo = m + 1; else hi = m; }
      for (let i = lo; i < els.length && els[i].offsetTop < bot && want.size < 6; i++) if (D.paras[i].units?.length) want.add(D.paras[i].units[0]);
    }
    if (pdfView()) {
      const V = $("#pdfview"), top = V.scrollTop, bot = top + V.clientHeight;
      pgEls.forEach((pg, n) => {
        if (pg.offsetTop + pg.offsetHeight < top || pg.offsetTop > bot) return;
        for (const b of pageIdx[n]) {
          const yy = pg.offsetTop + b[1] / D.pages[n][1] * pg.offsetHeight;
          if (yy > top && yy < bot && D.paras[b[4]].units?.length && want.size < 8) want.add(D.paras[b[4]].units[0]);
        }
      });
    }
    let n = 0;
    for (const u of want) getMeta(u, 20 + n++).catch(() => {});
  }, 500);
}
$("#reader").addEventListener("scroll", warmVisible, { passive: true });
$("#pdfview").addEventListener("scroll", warmVisible, { passive: true });

// ---------------------------------------------------------------- pronunciations
// Right-click a word (in the text or on the PDF) to tell ALOUD how to say it.
function wordAt(text, c) {
  let a = c, b = c;
  while (a > 0 && /[\w\-’'.]/.test(text[a - 1])) a--;
  while (b < text.length && /[\w\-’'.]/.test(text[b])) b++;
  return text.slice(a, b).replace(/^[.'’-]+|[.'’-]+$/g, "");
}
async function editPronunciation(word) {
  if (!word) return;
  const lex = { ...(S.settings.lexicon || {}) };
  const said = prompt(`How should ALOUD say "${word}"?\n\nType it the way it sounds (e.g. "lock three D"). Leave empty to skip the word entirely. Press Cancel to keep it as it is${lex[word] !== undefined ? ", or clear the box and type a single dash (-) to remove your custom pronunciation" : ""}.`, lex[word] ?? word);
  if (said === null) return;
  if (said.trim() === "-") delete lex[word]; else lex[word] = said.trim();
  const r = await saveSettings({ lexicon: lex });
  buildLexicon();
  toast(said.trim() === "-" ? `Back to the normal pronunciation of "${word}"` : said.trim() ? `"${word}" will be read as "${said.trim()}"` : `"${word}" will be skipped`, 2600);
  if (r.reload && D) { const p = paraOf(cur), was = isBusy(); await openDoc({ id: D.id }, p, was); }
}
// the citation ("[26]", "(Smith, 2020)", "Smith et al. (2020)") at a character of a paragraph, if any
function citeAt(i, pos) {
  const p = D && D.paras[i]; if (!p) return null;
  for (const [a, b] of citeSpans(i)) if (pos >= a - 1 && pos <= b) return p.text.slice(a, b);
  const t = p.text;
  for (const rx of [/\[[^\[\]]{1,40}\]/g, /\((?:see |e\.g\.,? |cf\. )?[A-Z][^()]{0,160}?(?:19|20)\d{2}[a-z]?\)/g,
                    /[A-Z][\w'’\-]+(?:\s+(?:et al\.?|and|&)\s*(?:[A-Z][\w'’\-]+)?)?\s*\((?:19|20)\d{2}[a-z]?\)/g]) {
    let m; rx.lastIndex = 0;
    while ((m = rx.exec(t))) {
      if (pos >= m.index - 1 && pos <= m.index + m[0].length && /\d/.test(m[0])) return m[0];
      if (m.index > pos + 2) break;
    }
  }
  return null;
}
$("#text").addEventListener("contextmenu", (e) => {
  const pe = e.target.closest("[data-i]"); if (!pe || !D) return;
  const cs = e.target.closest(".cite");
  if (cs && /\d/.test(cs.textContent)) {
    e.preventDefault();
    const t = D.paras[+pe.dataset.i].text, a = +cs.dataset.s;
    let q = t.slice(a, +cs.dataset.e);
    // "Brandeis (1914)": the year alone is skipped; the author's name just before it goes with it
    if (!/[A-Za-z]{2}/.test(q)) { const m = t.slice(Math.max(0, a - 80), a).match(/([A-Z][\w'’\-]+(?:\s+(?:et al\.?|and|&)(?:\s+[A-Z][\w'’\-]+)?)?)\s*$/); if (m) q = m[1] + " " + q.trim(); }
    showCite(q); return;
  }
  const cr = document.caretRangeFromPoint ? document.caretRangeFromPoint(e.clientX, e.clientY) : null;
  if (cr && pe.contains(cr.startContainer)) {
    const pre = document.createRange(); pre.setStart(pe, 0); pre.setEnd(cr.startContainer, cr.startOffset);
    const c = citeAt(+pe.dataset.i, pre.toString().length);
    if (c) { e.preventDefault(); showCite(c); return; }
  }
  const sel = String(window.getSelection()).trim();
  let word = sel && sel.length < 40 ? sel : null;
  if (!word) {
    const r = document.caretRangeFromPoint ? document.caretRangeFromPoint(e.clientX, e.clientY) : null;
    if (r) { const pre = document.createRange(); pre.setStart(pe, 0); pre.setEnd(r.startContainer, r.startOffset); word = wordAt(D.paras[+pe.dataset.i].text, pre.toString().length); }
  }
  if (word) { e.preventDefault(); editPronunciation(word); }
});
$("#pdfview").addEventListener("contextmenu", (e) => {
  const pg = e.target.closest(".pg"); if (!pg || !D) return;
  const n = +pg.dataset.n, [W, H] = D.pages[n], r = pg.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width * W, y = (e.clientY - r.top) / r.height * H;
  e.preventDefault();                                  // the page image's own menu is of no use here
  // nearest words to the pointer (like a normal click); a citation among the closest ones wins
  const near = [];
  for (const b of pageIdx[n] || []) {
    const dx = Math.max(b[0] - x, 0, x - b[2]), dy = Math.max(b[1] - y, 0, y - b[3]);
    const d = dx + dy * 3; if (d < 10) near.push([d, b]);
  }
  near.sort((p, q) => p[0] - q[0]);
  if (!near.length) return;
  for (const [d, nb] of near.slice(0, 4)) { if (d > near[0][0] + 4) break; const c = citeAt(nb[4], nb[5]); if (c) { showCite(c); return; } }
  const b = near[0][1];
  const c = citeAt(b[4], b[5]);
  if (c) showCite(c); else editPronunciation(wordAt(D.paras[b[4]].text, b[5]));
});
function buildLexicon() {
  const box = $("#lexicon"); if (!box) return;
  const lex = S.settings.lexicon || {}, keys = Object.keys(lex).sort((a, b) => a.localeCompare(b));
  box.innerHTML = keys.length ? "" : '<small style="color:var(--muted)">None yet — right-click any word to add one.</small>';
  for (const k of keys) {
    const row = document.createElement("div"); row.className = "lexrow";
    row.innerHTML = `<span><b>${esc(k)}</b> → ${lex[k] ? esc(lex[k]) : "<i>skipped</i>"}</span><button class="icon" title="Remove">✕</button>`;
    row.querySelector("button").onclick = async () => { const l = { ...lex }; delete l[k]; const r = await saveSettings({ lexicon: l }); buildLexicon(); if (r.reload && D) { const p = paraOf(cur), was = isBusy(); await openDoc({ id: D.id }, p, was); } };
    row.querySelector("span").onclick = () => editPronunciation(k);
    box.appendChild(row);
  }
}

// ---------------------------------------------------------------- search
let finds = [], findI = -1, findBoxes = [];
function openFind() { if (!D) return; $("#find").classList.add("show"); const q = $("#findQ"); q.focus(); q.select(); if (q.value) runFind(); }
function closeFind() { $("#find").classList.remove("show"); clearFindMarks(); }
function clearFindMarks() {
  if (window.CSS && CSS.highlights) { CSS.highlights.delete("find"); CSS.highlights.delete("findcur"); }
  findBoxes.forEach((d) => d.remove()); findBoxes = [];
}
function rangeIn(el, s, e) {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let pos = 0, n, r = document.createRange(), started = false;
  while ((n = walker.nextNode())) {
    const len = n.nodeValue.length;
    if (!started && s < pos + len) { r.setStart(n, s - pos); started = true; }
    if (started && e <= pos + len) { r.setEnd(n, e - pos); return r; }
    pos += len;
  }
  return null;
}
function runFind(quiet) {
  const q = $("#findQ").value.trim().toLowerCase();
  finds = []; findI = -1; clearFindMarks();
  if (!q || !D) { $("#findN").textContent = ""; return; }
  D.paras.forEach((p, i) => { const t = p.text.toLowerCase(); let k = t.indexOf(q); while (k >= 0) { finds.push({ p: i, s: k, e: k + q.length }); k = t.indexOf(q, k + q.length); } });
  if (window.CSS && CSS.highlights && window.Highlight) {
    const rs = finds.slice(0, 2000).map((f) => els[f.p] && rangeIn(els[f.p], f.s, f.e)).filter(Boolean);
    CSS.highlights.set("find", new Highlight(...rs));
  }
  // first match at or after the reading position
  const here = cur >= 0 ? paraOf(cur) : 0;
  findI = finds.length ? Math.max(0, finds.findIndex((f) => f.p >= here)) : -1;
  if (!quiet) showFind();
}
function showFind() {
  $("#findN").textContent = finds.length ? `${findI + 1} of ${finds.length}` : "No matches";
  findBoxes.forEach((d) => d.remove()); findBoxes = [];
  if (findI < 0) return;
  const f = finds[findI];
  const r = els[f.p] && rangeIn(els[f.p], f.s, f.e);
  if (r && window.CSS && CSS.highlights && window.Highlight) CSS.highlights.set("findcur", new Highlight(r));
  if (textView() && els[f.p]) { const R = $("#reader"); const rr = (r || els[f.p]).getBoundingClientRect(), Rr = R.getBoundingClientRect(); autoScrollUntil = performance.now() + 800; R.scrollTo({ top: R.scrollTop + rr.top - Rr.top - R.clientHeight * 0.33, behavior: "smooth" }); }
  if (hasPdf()) {
    const bs = boxesIn(D.paras[f.p], f.s, f.e);
    bs.forEach((b) => findBoxes.push(addBox(b[2], "fh", b[3], b[4], b[5], b[6], 1.5)));
    if (bs[0]) pdfScrollTo(bs[0][2], bs[0][4], true, true, bs[0][3], bs[0][5]);
    else pdfScrollToPara(f.p, true);
  }
  if (isBusy()) { userScrolled = true; $("#back").classList.add("show"); }
}
function showFindQuiet() {
  $("#findN").textContent = finds.length ? `${findI + 1} of ${finds.length}` : "No matches";
  const f = finds[findI]; if (!f) return;
  const r = els[f.p] && rangeIn(els[f.p], f.s, f.e);
  if (r && window.CSS && CSS.highlights && window.Highlight) CSS.highlights.set("findcur", new Highlight(r));
}
function findStep(d) { if (!finds.length) return; findI = (findI + d + finds.length) % finds.length; showFind(); }
$("#findQ").addEventListener("input", () => { clearTimeout(runFind._h); runFind._h = setTimeout(() => runFind(), 150); });
$("#findQ").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); if (e.metaKey) readFind(); else findStep(e.shiftKey ? -1 : 1); }
  if (e.key === "Escape") { e.preventDefault(); closeFind(); }
});
$("#findNext").onclick = () => findStep(1); $("#findPrev").onclick = () => findStep(-1);
$("#findClose").onclick = closeFind;
function readFind() {
  if (findI < 0) return; const f = finds[findI], p = D.paras[f.p];
  let u = (p.units || []).find((uu) => D.units[uu].s <= f.s && f.s < D.units[uu].e);
  if (u == null) u = unitAtPara(f.p);
  userScrolled = false; $("#back").classList.remove("show");
  startAt(u, 0, null, f.s);
}
$("#findRead").onclick = readFind;

// ---------------------------------------------------------------- export as audio
let exportPoll = 0;
$("#exportBtn").onclick = async () => {
  if (!D) return;
  const st = await api("/api/status");
  if (st.export.running) {
    if (confirm("Stop saving the audio files?")) await api("/api/export", { cancel: true });
    return;
  }
  const mins = Math.round(D.units.reduce((n, u) => n + u.e - u.s, 0) / 15.5 / 60);
  if (!confirm(`Save "${D.title}" as audio files (one per section) in your Downloads folder?\n\nAbout ${mins} minutes of audio; it is prepared in the background (roughly ${Math.max(1, Math.round(mins / 10))} min) and live reading always takes priority.`)) return;
  try { await api("/api/export", { id: D.id, voice: voice() }); } catch (e) { toast(e.message, 3000); return; }
  toast("Saving audio files in the background…");
  clearInterval(exportPoll);
  exportPoll = setInterval(async () => {
    const x = (await api("/api/status")).export;
    $("#exportBtn").title = x.running ? `Saving audio… ${Math.round(100 * x.done / Math.max(1, x.total))}% (click to stop)` : "Save as audio files";
    $("#exportBtn").style.color = x.running ? "var(--accent)" : "";
    if (!x.running) {
      clearInterval(exportPoll);
      if (x.error === "cancelled") toast("Audio export stopped");
      else if (x.error) toast("Audio export failed: " + x.error, 5000);
      else { toast("Audio files saved in Downloads", 4000); if (window.pywebview?.api) window.pywebview.api.reveal(x.folder); }
    }
  }, 1500);
};

// ---------------------------------------------------------------- sleep timer
const SLEEP_STEPS = [0, 15, 30, 45, 60, -1];   // minutes; -1 = end of this section
let sleepIdx = 0, sleepAt = 0, sleepSection = null, sleepTick = 0;
function setSleep(i) {
  sleepIdx = i % SLEEP_STEPS.length; const m = SLEEP_STEPS[sleepIdx];
  sleepAt = m > 0 ? Date.now() + m * 60000 : 0;
  sleepSection = m === -1 && D && cur >= 0 ? sectionFor(paraOf(cur)) : null;
  $("#sleepBtn").classList.toggle("on", m !== 0);
  clearInterval(sleepTick);
  if (m !== 0) sleepTick = setInterval(checkSleep, 1000);
  updateSleepLabel();
  toast(m === 0 ? "Sleep timer off" : m === -1 ? "Stopping at the end of this section" : `Stopping in ${m} minutes`);
}
function updateSleepLabel() {
  const m = SLEEP_STEPS[sleepIdx];
  $("#sleepVal").textContent = m === 0 ? "Off" : m === -1 ? "End of section" : Math.max(1, Math.ceil((sleepAt - Date.now()) / 60000)) + " min";
}
function checkSleep() {
  const m = SLEEP_STEPS[sleepIdx]; if (!m) return;
  updateSleepLabel();
  const sectionDone = m === -1 && D && cur >= 0 && sectionFor(paraOf(cur)) !== sleepSection;
  if ((m > 0 && Date.now() >= sleepAt) || sectionDone) {
    // fade out gently, then pause
    let v = 1; const a = audio, f = setInterval(() => { v -= 0.05; a.volume = Math.max(0, v); if (v <= 0) { clearInterval(f); pause(); a.volume = 1; spare.volume = 1; } }, 150);
    sleepIdx = 0; clearInterval(sleepTick); $("#sleepBtn").classList.remove("on"); $("#sleepVal").textContent = "Off";
  }
}
$("#sleepBtn").onclick = () => setSleep(sleepIdx + 1);

// ---------------------------------------------------------------- info / progress
function updateInfo() {
  if (!D || cur < 0) return;
  $("#fill").style.width = (100 * cur / Math.max(1, D.units.length - 1)) + "%";
  const p = paraOf(cur), s = sectionFor(p);
  $("#nowSec").textContent = s ? s.title : D.title;
  let chars = 0; for (let x = cur; x < D.units.length; x++) chars += D.units[x].e - D.units[x].s;
  const secs = chars / 15.5 / S.settings.rate;
  const h = Math.floor(secs / 3600), mnt = Math.round((secs % 3600) / 60);
  $("#nowInfo").textContent = `Page ${D.paras[p].page || "–"} · ${h ? h + " h " : ""}${mnt} min left`;
}
$("#progress").onclick = (e) => {
  if (!D) return;
  const r = e.currentTarget.getBoundingClientRect();
  const f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const u = Math.round(f * (D.units.length - 1));
  jumpTo(u); scrollToPara(paraOf(u), true);
};
let lastSave = 0;
function saveProgress(force) {
  if (!D || cur < 0) return; const now = Date.now();
  if (!force && now - lastSave < 4000) return; lastSave = now;
  api("/api/progress", { id: D.id, para: paraOf(cur), unit: cur, t: audio.currentTime || 0 }).catch(() => {});
}
window.addEventListener("beforeunload", () => saveProgress(true));

// ---------------------------------------------------------------- media keys & keyboard
function updateMediaSession() {
  tellMac(!audio.paused);
  if (!("mediaSession" in navigator) || !D) return;
  const s = sectionFor(paraOf(cur));
  try { navigator.mediaSession.metadata = new MediaMetadata({ title: s ? s.title : D.title, artist: D.title, album: "ALOUD" }); } catch (_) {}
}
if ("mediaSession" in navigator) {
  const h = (a, f) => { try { navigator.mediaSession.setActionHandler(a, f); } catch (_) {} };
  h("play", () => toggle()); h("pause", () => pause());
  h("previoustrack", () => sentence(-1)); h("nexttrack", () => sentence(1));
  h("seekbackward", () => sentence(-1)); h("seekforward", () => sentence(1));
}
function paraNav(dir) { const n = nextParaUnit(cur, dir); if (n != null) { jumpTo(n); scrollToPara(paraOf(n), true); } }
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  if (e.metaKey && e.key.toLowerCase() === "o") { e.preventDefault(); chooseFile(); return; }
  if (e.metaKey && e.key.toLowerCase() === "f") { e.preventDefault(); openFind(); return; }
  if (e.metaKey && e.key.toLowerCase() === "g") { e.preventDefault(); findStep(e.shiftKey ? -1 : 1); return; }
  if (e.metaKey && e.shiftKey && e.key.toLowerCase() === "v") { e.preventDefault(); showPaste(); return; }
  if (e.metaKey && e.key === ",") { e.preventDefault(); $("#settings").classList.toggle("show"); return; }
  if (e.metaKey && e.key === "\\") { e.preventDefault(); document.body.classList.toggle("notoc"); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "Escape") {
    if (!$("#settings").classList.contains("show") && !$("#wiki").classList.contains("show") && !figurePreview.hidden) {
      if (figurePreview.classList.contains("expanded")) expandFigurePreview(false);
      else dismissFigurePreview();
    }
    $("#settings").classList.remove("show"); closeWiki(); return;
  }
  if (e.key === "?" || (e.key === "/" && e.shiftKey)) { e.preventDefault(); openWiki(); return; }
  if (!D || document.body.classList.contains("lib")) return;
  if (e.key === " " && e.target.closest("#figurePreview button")) return;
  if (e.key === " " || e.key === "k") { e.preventDefault(); toggle(); }
  else if (e.key === "ArrowRight") { e.preventDefault(); e.shiftKey ? paraNav(1) : sentence(1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); e.shiftKey ? paraNav(-1) : sentence(-1); }
  else if (e.key === "v" || e.key === "V") { if (hasPdf()) setView({ text: "split", split: "pdf", pdf: "text" }[curView()] || "split"); }
  else if (e.key === "d" || e.key === "D") setTheme(isDark() ? "light" : "dark");
  else if (e.key === "c" || e.key === "C") citeNearHere();
  else if (e.key === "m" || e.key === "M") addMark();
  else if (e.key === "q" || e.key === "Q") toggleQueue();
  else if (e.key === "n" || e.key === "N") { e.preventDefault(); noteBox(); }
  else if (e.key === "+" || e.key === "=") setRate(S.settings.rate + 0.1);
  else if (e.key === "-" || e.key === "_") setRate(S.settings.rate - 0.1);
});
$("#play").onclick = toggle;
$("#nextSent").onclick = () => sentence(1); $("#prevSent").onclick = () => sentence(-1);
$("#nextPara").onclick = () => paraNav(1); $("#prevPara").onclick = () => paraNav(-1);

// ---------------------------------------------------------------- boot
(async function boot() {
  const st = await api("/api/state");
  S.settings = st.settings; S.voices = st.voices; S.ready = st.ready; S.defaultVoice = st.default_voice || {}; S.queue = st.queue || []; S.docs = st.docs || [];
  applyLook(); buildSettings();
  if (!st.ready) (async () => { while (!(await api("/api/state")).ready) await sleep(1000); S.ready = true; })();
  const m = location.hash.match(/open=([^&]+)/);
  if (m) { history.replaceState(null, "", "/"); return openDoc({ path: decodeURIComponent(m[1]) }); }
  if (st.last && st.docs.some((d) => d.id === st.last)) return openDoc({ id: st.last });
  showLibrary();
})();

function sendBrowserPage(port, key) {
  const html = document.documentElement.outerHTML;
  const status = document.createElement("div");
  status.textContent = "Sending to ALOUD…";
  status.style.cssText = "position:fixed;z-index:2147483647;top:16px;right:16px;padding:10px 14px;border-radius:10px;background:#2f6fde;color:#fff;font:600 14px -apple-system,sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.25)";
  document.body.append(status);
  fetch(`http://127.0.0.1:${port}/send`, {
    method: "POST",
    headers: { "Content-Type": "text/plain" },
    body: JSON.stringify({ key, url: location.href, title: document.title, html })
  }).then(async (response) => {
    const result = await response.json();
    status.textContent = response.ok ? "Reading in ALOUD ✓" : (result.error || "ALOUD could not read this page");
  }).catch(() => { status.textContent = "Open ALOUD first"; })
    .finally(() => setTimeout(() => status.remove(), 2600));
}
api("/api/bookmarklet").then(({ port, key }) => {
  const code = `(${sendBrowserPage.toString()})(${port},${JSON.stringify(key)})`;
  $("a.bm").setAttribute("href", "javascript:" + encodeURIComponent(code));
  $("#bmCopy").disabled = false;
}).catch(() => {});

$("#bmCopy").onclick = async () => {
  const code = document.querySelector("a.bm").getAttribute("href");
  try { await navigator.clipboard.writeText(code); toast("Copied. Make a new bookmark and paste this as its address.", 4000); }
  catch (e) { prompt("Copy this, then make a new bookmark with it as the address:", code); }
};

// ---------------------------------------------------------------- citations: what is "[26]"?
const CITE_RX = /^\s*\[[^\]]{1,40}\]\s*$|^\s*\(?[A-Z][^()]{0,120}(19|20)\d{2}[a-z]?\)?\s*$/;
function citeSpans(i) {
  const p = D.paras[i];
  return (p?.spans || []).filter(([a, b, v]) => v === null && CITE_RX.test(p.text.slice(a, b)) && /\d/.test(p.text.slice(a, b)));
}
function citeNearHere() {
  if (!D) return;
  const pi = cur >= 0 ? paraOf(cur) : 0;
  const m = metaCache.get(voice() + "|" + cur);
  const here = m && wordK >= 0 && m.words[wordK] ? m.words[wordK][0] : (D.units[cur]?.s ?? 0);
  // the last citation already read in this paragraph, else the next one coming up, else the previous paragraph's
  let spans = citeSpans(pi), pick = null, pIdx = pi;
  for (const sp of spans) if (sp[0] <= here + 2) pick = sp;
  if (!pick && spans.length) pick = spans.find((sp) => sp[0] > here);
  for (let j = pi - 1; !pick && j >= Math.max(0, pi - 3); j--) { const sp = citeSpans(j); if (sp.length) { pick = sp[sp.length - 1]; pIdx = j; } }
  if (!pick) return toast("No citation nearby");
  showCite(D.paras[pIdx].text.slice(pick[0], pick[1]));
}
async function showCite(q) {
  let box = $("#citeCard");
  if (!box) { box = document.createElement("div"); box.id = "citeCard"; document.body.appendChild(box); }
  box.innerHTML = `<div class="cc-q">${esc(q.trim())}</div><div class="cc-body">Looking it up…</div>`;
  box.classList.add("show");
  clearTimeout(showCite._t);
  try {
    const r = await api(`/api/cite?doc=${D.id}&q=${encodeURIComponent(q)}`);
    if (!r.refs.length) { box.querySelector(".cc-body").textContent = r.have_list ? "Not found in the reference list." : "This document has no reference list I can read."; }
    else box.querySelector(".cc-body").innerHTML = r.refs.map((x) => `
      <div class="cc-ref">
        <a href="#" data-url="${esc(x.link)}" class="cc-title">${esc(x.title)}</a>
        <div class="cc-meta">${esc(x.author || "")}${x.year ? " · " + esc(x.year) : ""}</div>
        <div class="cc-links"><a href="#" data-url="${esc(x.link)}">Open</a>${x.alphaxiv && x.alphaxiv !== x.link ? `<a href="#" data-url="${esc(x.alphaxiv)}">alphaXiv</a>` : ""}<a href="#" data-url="${esc(x.scholar)}">Google Scholar</a></div>
      </div>`).join("");
  } catch (e) { box.querySelector(".cc-body").textContent = "Couldn't look that up."; }
  showCite._t = setTimeout(() => box.classList.remove("show"), 15000);
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("#citeCard a[data-url]");
  if (a) { e.preventDefault(); const u = a.dataset.url; if (window.pywebview?.api?.open_url) window.pywebview.api.open_url(u); else window.open(u, "_blank"); return; }
  if (!e.target.closest("#citeCard")) $("#citeCard")?.classList.remove("show");
  const c = e.target.closest("#text .cite");
  if (c && D) { const i = +c.closest("[data-i]").dataset.i; const t = D.paras[i].text.slice(+c.dataset.s, +c.dataset.e); if (CITE_RX.test(t)) { e.stopPropagation(); showCite(t); } }
}, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#citeCard")?.classList.remove("show"); });

// ---------------------------------------------------------------- notes while listening
let NOTES = [], notesFile = null;
function hereRange() {
  // the sentence being read (or the current passage when nothing is playing yet)
  const u = cur >= 0 ? cur : 0, unit = D.units[u], p = D.paras[unit.p];
  const m = metaCache.get(voice() + "|" + u);
  let s = unit.s, e = unit.e;
  if (m && m.words.length) {
    const k = Math.max(0, wordK);
    const starts = m.sent || [0];
    let a = 0; for (const x of starts) if (x <= k) a = x;
    const nb = starts.find((x) => x > a);
    s = m.words[a][0]; e = m.words[(nb ?? m.words.length) - 1][1];
  }
  const pg = p.boxes?.find((b) => b[0] >= s)?.[2];
  return { unit: u, para: unit.p, s, e, quote: p.text.slice(s, e).trim().slice(0, 400), section: sectionFor(unit.p)?.title || null, page: pg != null ? pg + 1 : (p.page || null) };
}
async function saveNote(n) {
  const r = await api("/api/note", { doc: D.id, ...n });
  NOTES = r.notes; notesFile = r.file; renderNotes();
}
async function addMark() {
  if (!D) return;
  await saveNote({ ...hereRange(), kind: "mark", text: "" });
  toast("⭐ Marked — saved to your notes", 1800);
}
function noteBox() {
  if (!D) return;
  const wasPlaying = captionRun?.resume ?? !audio.paused;
  if (wasPlaying || captionRun) pause();
  const here = hereRange();
  let box = $("#noteBox");
  if (!box) { box = document.createElement("div"); box.id = "noteBox"; document.body.appendChild(box); }
  box.innerHTML = `<div class="nb-q">${esc(here.quote.slice(0, 160))}${here.quote.length > 160 ? "…" : ""}</div>
    <textarea placeholder="Your thought… (type, or dictate: press 🎤 or Fn twice)"></textarea>
    <div class="nb-acts"><small>Enter saves · Shift+Enter new line · Esc cancels</small><button class="pill-btn nb-x">Cancel</button><button class="primary nb-ok">Save note</button></div>`;
  box.classList.add("show");
  const ta = box.querySelector("textarea");
  setTimeout(() => ta.focus(), 20);
  const close = (resume) => { box.classList.remove("show"); if (resume && wasPlaying) toggle(); };
  const save = async () => { const t = ta.value.trim(); close(true); if (t) { await saveNote({ ...here, kind: "note", text: t }); toast("📝 Note saved", 1500); } };
  box.querySelector(".nb-ok").onclick = save;
  box.querySelector(".nb-x").onclick = () => close(true);
  ta.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); } else if (e.key === "Escape") { e.preventDefault(); close(true); } e.stopPropagation(); };
}
async function loadNotes() {
  NOTES = []; notesFile = null;
  try { const r = await api(`/api/notes?doc=${D.id}`); NOTES = r.notes; notesFile = r.file; } catch (e) { /* none */ }
  renderNotes();
}
function renderNotes() {
  els.forEach((el) => el && el.classList.remove("noted"));
  for (const n of NOTES) els[n.para]?.classList.add("noted");
  let box = $("#notesList");
  if (!box) { box = document.createElement("div"); box.id = "notesList"; }
  $("#toc").appendChild(box);
  if (!NOTES.length) { box.innerHTML = `<div class="nl-h">Notes</div><div class="nl-empty">Press <b>M</b> to mark a sentence, <b>N</b> to write or dictate a note.</div>`; return; }
  box.innerHTML = `<div class="nl-h">Notes <span>${NOTES.length}</span></div>` +
    [...NOTES].sort((a, b) => a.unit - b.unit).map((n) => `<div class="nl-i" data-u="${n.unit}" data-id="${n.id}">
      <span class="ic">${n.kind === "mark" ? "⭐" : "📝"}</span><span class="tx">${esc(n.kind === "mark" ? n.quote : n.text)}</span><button class="rm" title="Delete">×</button></div>`).join("") +
    (notesFile ? `<a class="nl-file" href="#">Open notes file</a>` : "");
  box.onclick = async (e) => {
    if (e.target.closest(".nl-file")) { e.preventDefault(); if (window.pywebview?.api?.open_file) window.pywebview.api.open_file(notesFile); return; }
    const it = e.target.closest(".nl-i"); if (!it) return;
    if (e.target.closest(".rm")) { const r = await api("/api/note", { doc: D.id, delete: it.dataset.id }); NOTES = r.notes; notesFile = r.file; renderNotes(); return; }
    const u = +it.dataset.u; jumpTo(u); scrollToPara(paraOf(u), true);
  };
}
window.aloudJump = async (doc, unit) => {
  if (!D || D.id !== doc) await openDoc({ id: doc });
  if (!D || !D.units[unit]) return;
  jumpTo(unit); scrollToPara(paraOf(unit), true);
};

// ---------------------------------------------------------------- up next (queue)
async function refreshQueue() {
  try { const st = await api("/api/state"); S.queue = st.queue || []; S.docs = st.docs || []; } catch (e) { /* keep */ }
  updateQueueBtn();
}
function updateQueueBtn() {
  const n = (S.queue || []).length;
  $("#queueN").textContent = n ? `Up next · ${n}` : "Up next";
}
function docInfo(id) { return (S.docs || []).find((d) => d.id === id) || { id, title: "Untitled" }; }
async function toggleQueue() {
  let pop = $("#queuePop");
  if (!pop) { pop = document.createElement("div"); pop.id = "queuePop"; document.body.appendChild(pop); }
  if (pop.classList.contains("show")) { pop.classList.remove("show"); return; }
  await refreshQueue(); renderQueue(); pop.classList.add("show");
}
function renderQueue() {
  const pop = $("#queuePop"); if (!pop) return;
  const q = S.queue || [];
  pop.innerHTML = `<h4>Up next ${q.length ? '<a id="qClear">Clear</a>' : ""}</h4>` + (q.length ? q.map((id, i) => {
    const d = docInfo(id);
    return `<div class="q-i" data-id="${id}"><img src="/api/cover?id=${encodeURIComponent(id)}" alt="" onerror="this.style.visibility='hidden'">
      <div class="t" data-open="1">${esc(d.title || "Untitled")}<span>${i === 0 ? "Plays when this one ends" : "#" + (i + 1)}</span></div>
      <button data-mv="-1" title="Move up">↑</button><button data-mv="1" title="Move down">↓</button><button data-rm="1" title="Remove">×</button></div>`;
  }).join("") : `<div class="q-empty">Nothing queued. In the library, hover a document and press <b>+ Up next</b>. When a document ends, the next one starts automatically, each from where you left it.</div>`);
  pop.onclick = async (e) => {
    if (e.target.id === "qClear") { S.queue = (await api("/api/queue", { op: "clear" })).queue; renderQueue(); updateQueueBtn(); return; }
    const it = e.target.closest(".q-i"); if (!it) return;
    const id = it.dataset.id, i = q.indexOf(id);
    if (e.target.dataset.rm) S.queue = (await api("/api/queue", { op: "remove", id })).queue;
    else if (e.target.dataset.mv) S.queue = (await api("/api/queue", { op: "move", id, to: i + +e.target.dataset.mv })).queue;
    else if (e.target.closest("[data-open]")) { pop.classList.remove("show"); return openDoc({ id }, null, true); }
    renderQueue(); updateQueueBtn();
  };
}
async function playNextInQueue() {
  await refreshQueue();
  const next = (S.queue || []).find((id) => !D || id !== D.id);
  if (!next) { toast("End of document"); return; }
  toast(`Up next: ${docInfo(next).title}`, 2600);
  await sleep(2200);
  if (!audio.paused) return;                         // the listener started something else meanwhile
  openDoc({ id: next }, null, true);
}
$("#queueBtn").onclick = (e) => { e.stopPropagation(); toggleQueue(); };
document.addEventListener("click", (e) => { if (!e.target.closest("#queuePop, #queueBtn")) $("#queuePop")?.classList.remove("show"); });
refreshQueue();

window.aloudRead = (path) => { openDoc({ path }, null, true); };   // text sent from another app: read it now

// ---------------------------------------------------------------- guide (wiki)
let wikiLoaded = false;
async function openWiki(section) {
  const w = $("#wiki");
  if (!wikiLoaded) {
    try { $("#wkBody").innerHTML = await (await fetch("/help.html")).text(); wikiLoaded = true; }
    catch (e) { $("#wkBody").textContent = "Couldn't load the guide."; }
    const toc = $("#wkToc"); toc.innerHTML = "";
    $("#wkBody").querySelectorAll("section").forEach((sec) => {
      const a = document.createElement("a"); a.textContent = sec.querySelector("h2").textContent; a.dataset.id = sec.id;
      a.onclick = () => { sec.scrollIntoView({ behavior: "smooth", block: "start" }); };
      toc.appendChild(a);
    });
    $("#wkBody").addEventListener("scroll", wikiSpy);
  }
  w.classList.add("show"); $("#settings").classList.remove("show");
  if (section) document.getElementById(section)?.scrollIntoView({ block: "start" });
  setTimeout(() => $("#wkFind").focus(), 30); wikiSpy();
}
function closeWiki() { $("#wiki").classList.remove("show"); }
function wikiSpy() {
  const body = $("#wkBody"), top = body.getBoundingClientRect().top + 40;
  let on = null;
  body.querySelectorAll("section:not(.hide)").forEach((sec) => { if (sec.getBoundingClientRect().top <= top) on = sec.id; });
  on = on || body.querySelector("section:not(.hide)")?.id;
  $("#wkToc").querySelectorAll("a").forEach((a) => a.classList.toggle("on", a.dataset.id === on));
}
$("#wkFind").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase(), body = $("#wkBody");
  body.querySelectorAll("mark").forEach((m) => m.replaceWith(m.textContent));
  body.normalize();
  let shown = 0;
  body.querySelectorAll("section").forEach((sec) => {
    const hit = !q || sec.textContent.toLowerCase().includes(q);
    sec.classList.toggle("hide", !hit); if (hit) shown++;
    $("#wkToc").querySelector(`a[data-id="${sec.id}"]`)?.classList.toggle("dim", !hit);
    if (hit && q.length > 1) {   // highlight matches
      const walk = document.createTreeWalker(sec, NodeFilter.SHOW_TEXT); const nodes = [];
      while (walk.nextNode()) nodes.push(walk.currentNode);
      for (const n of nodes) {
        const i = n.nodeValue.toLowerCase().indexOf(q); if (i < 0) continue;
        const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + q.length);
        const m = document.createElement("mark"); r.surroundContents(m);
      }
    }
  });
  body.querySelector(".wk-none")?.remove();
  if (!shown) body.insertAdjacentHTML("beforeend", `<p class="wk-none">Nothing about “${esc(e.target.value)}” in the guide.</p>`);
  body.scrollTop = 0; wikiSpy();
});
$("#wkFind").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); closeWiki(); } e.stopPropagation(); });
$("#helpBtn").onclick = () => openWiki();
$("#wkClose").onclick = closeWiki;
$("#wiki").addEventListener("mousedown", (e) => { if (e.target.id === "wiki") closeWiki(); });

// ---------------------------------------------------------------- web pages, shown as they are
// The article is read from its clean text; the page itself is shown as the site made it (pictures,
// GIFs, videos) and the voice is highlighted on its own text, using the CSS Custom Highlight API.
let WEB = null;   // { win, doc, norm, map, paras: {i: [start, end]}, user }
const NORM_MAP = { "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " ",
  "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "…": "..." };
function normStr(t) {
  let out = "", prevSpace = true;
  for (const ch of t) {
    let c = NORM_MAP[ch] ?? ch;
    if (/[­​-‍﻿•◦▪]/.test(c)) continue;
    if (/\s/.test(c)) { if (!prevSpace) out += " "; prevSpace = true; continue; }
    out += c.toLowerCase(); prevSpace = false;
  }
  return out;
}
function renderWeb(view) {
  WEB = null;
  view.classList.add("web");
  view.innerHTML = `<iframe id="webframe" title="Web page" sandbox="allow-same-origin allow-scripts allow-popups allow-presentation allow-popups-to-escape-sandbox"
    src="/api/webpage?doc=${encodeURIComponent(D.id)}"></iframe>`;
  const fr = view.querySelector("iframe");
  fr.addEventListener("load", () => { try { indexWeb(fr); } catch (e) { console.warn("web index", e); } });
}
function indexWeb(fr) {
  const win = fr.contentWindow, doc = fr.contentDocument;
  if (!doc || !doc.body) return;
  // the page's text, whitespace-normalised, with a map back to (text node, offset)
  const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT, { acceptNode(n) {
    const t = n.parentNode && n.parentNode.nodeName;
    if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|TEXTAREA)$/.test(t)) return NodeFilter.FILTER_REJECT;
    if (n.parentElement && n.parentElement.closest(".katex-mathml, [aria-hidden='true'] .katex-mathml")) return NodeFilter.FILTER_REJECT;
    return NodeFilter.FILTER_ACCEPT; } });
  let norm = "", prevSpace = true; const mapN = [], mapO = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const v = n.nodeValue;
    for (let i = 0; i < v.length; i++) {
      let c = NORM_MAP[v[i]] ?? v[i];
      if (/[­​-‍﻿]/.test(c)) continue;
      if (/\s/.test(c)) { if (!prevSpace) { norm += " "; mapN.push(n); mapO.push(i); } prevSpace = true; continue; }
      c = c.toLowerCase();
      for (const cc of c) { norm += cc; mapN.push(n); mapO.push(i); }
      prevSpace = false;
    }
    if (!prevSpace) { norm += " "; mapN.push(n); mapO.push(v.length); prevSpace = true; }   // block boundaries
  }
  // where each paragraph of the article sits in the page, in order
  const paras = {};
  let from = 0;
  D.paras.forEach((p, i) => {
    if ((p.kind !== "p" && p.kind !== "h") || !p.text) return;
    const t = normStr(p.text);
    let at = -1;
    for (const len of [60, 32, 18]) {
      const key = t.slice(0, Math.min(len, t.length)).trim();
      if (key.length < 6) break;
      at = norm.indexOf(key, from);
      if (at < 0) at = norm.indexOf(key);
      if (at >= 0) break;
    }
    let shift = 0;
    if (at < 0) {
      // the paragraph opens with a formula (LaTeX here, typeset on the page): anchor on its longest
      // formula-free stretch instead, and place the paragraph start relative to that
      const runs = [...t.matchAll(/[^$\\{}^_]{14,}/g)].sort((a, b) => b[0].length - a[0].length);
      for (const r of runs.slice(0, 3)) {
        const key = r[0].trim().slice(0, 40);
        if (key.length < 12) continue;
        let k = norm.indexOf(key, from); if (k < 0) k = norm.indexOf(key);
        if (k >= 0) { at = Math.max(0, k - Math.round(r.index * 0.6)); shift = 1; break; }
      }
    }
    if (at >= 0) { paras[i] = [at, at + t.length]; if (at >= from) from = at + Math.max(1, Math.floor(t.length * (shift ? 0.3 : 0.6))); }
  });
  WEB = { win, doc, norm, mapN, mapO, paras, user: 0 };
  win.addEventListener("scroll", () => { if (performance.now() > (WEB?.auto || 0)) WEB.user = performance.now(); }, { passive: true });
  doc.addEventListener("click", onWebClick, true);
  for (const n of NOTES || []) markWebNote(n);
  webUnit(); webFollow(true);
}
function webRange(i, s, e) {
  // page Range for characters [s, e) of paragraph i (searching near where they should be)
  if (!WEB || !WEB.paras[i]) return null;
  const p = D.paras[i], [ps] = WEB.paras[i];
  const piece = normStr(p.text.slice(s, e)).trim();
  if (!piece) return null;
  const guess = ps + normStr(p.text.slice(0, s)).length;
  if (/[$\\]/.test(piece)) {
    // LaTeX here, typeset on the page: anchor on the plain-text parts around the formulas
    const runs = [...piece.matchAll(/[^$\\{}^_]+/g)].map((m) => m[0]).filter((r) => r.trim().length >= 6);
    if (!runs.length) return null;
    const first = runs[0].trim().slice(0, 24), last = runs[runs.length - 1].trim().slice(-24);
    let h = WEB.norm.indexOf(first, Math.max(ps - 10, guess - 80));
    if (h < 0 || h > guess + 500) h = WEB.norm.indexOf(first, ps - 10);
    if (h < 0) return null;
    let t = runs.length > 1 ? WEB.norm.indexOf(last, h + first.length) : h + first.length - last.length;
    if (t < 0 || t > h + piece.length + 400) t = h + first.length - Math.min(first.length, last.length);
    const end = Math.max(h, t + last.length - 1);
    try {
      const r = WEB.doc.createRange();
      r.setStart(WEB.mapN[h], WEB.mapO[h]);
      r.setEnd(WEB.mapN[end], Math.min(WEB.mapO[end] + 1, WEB.mapN[end].nodeValue.length));
      return r;
    } catch (e3) { return null; }
  }
  if (piece.length > 48) {
    // long spans (a sentence): anchor its first and last few words separately, so one small
    // difference in the middle (a formula, a footnote mark) doesn't lose the whole sentence
    const head = piece.slice(0, 22), tail = piece.slice(-22);
    let h = WEB.norm.indexOf(head, Math.max(ps, guess - 60));
    if (h < 0 || h > guess + 400) h = WEB.norm.indexOf(head, ps);
    if (h < 0) return null;
    let t = WEB.norm.indexOf(tail, h + Math.floor(piece.length * 0.5));
    if (t < 0 || t > h + piece.length + 300) t = WEB.norm.indexOf(tail, h);
    const end = t >= 0 ? t + tail.length - 1 : Math.min(WEB.norm.length - 1, h + piece.length - 1);
    try {
      const r = WEB.doc.createRange();
      r.setStart(WEB.mapN[h], WEB.mapO[h]);
      r.setEnd(WEB.mapN[end], Math.min(WEB.mapO[end] + 1, WEB.mapN[end].nodeValue.length));
      return r;
    } catch (e2) { return null; }
  }
  let at = WEB.norm.indexOf(piece, Math.max(ps, guess - 60));
  if (at < 0 || at > guess + 400) {
    const back = WEB.norm.lastIndexOf(piece, guess + 40);
    at = back >= ps - 5 ? back : at;
  }
  if (at < 0) return null;
  const end = at + piece.length - 1;
  try {
    const r = WEB.doc.createRange();
    r.setStart(WEB.mapN[at], WEB.mapO[at]);
    r.setEnd(WEB.mapN[end], Math.min(WEB.mapO[end] + 1, WEB.mapN[end].nodeValue.length));
    return r;
  } catch (e) { return null; }
}
function webSet(name, range) {
  if (!WEB || !WEB.win.CSS || !WEB.win.CSS.highlights) return;
  if (range) WEB.win.CSS.highlights.set(name, new WEB.win.Highlight(range));
  else WEB.win.CSS.highlights.delete(name);
}
function webUnit() {
  if (!WEB || cur < 0) return;
  const u = D.units[cur];
  WEB.unit = webRange(u.p, u.s, u.e);
  webSet("nr-unit", WEB.unit); webSet("nr-word", null);
}
function webWord(k) {
  if (!WEB) return;
  const m = metaCache.get(metaKey(cur)), w = k >= 0 && m ? m.words[k] : null;
  WEB.word = w ? webRange(paraOf(cur), w[0], w[1]) : null;
  webSet("nr-word", WEB.word);
  if (WEB.word) webFollow(false);
}
function webScrollTo(range, force, smooth = true) {
  if (!WEB || !range) return;
  const r = range.getBoundingClientRect(), h = WEB.win.innerHeight;
  if (!r.height && !r.width) return;
  if (force || r.top < h * 0.12 || r.bottom > h * 0.72) {
    WEB.auto = performance.now() + 900;
    WEB.win.scrollTo({ top: WEB.win.scrollY + r.top - h * 0.33, behavior: smooth ? "smooth" : "auto" });
  }
}
function webFollow(force) {
  if (!WEB) return;
  if (!force && (!S.settings.follow || performance.now() - WEB.user < 4000)) return;
  webScrollTo(WEB.word || WEB.unit, force);
}
function webScrollToPara(i, smooth) {
  if (!WEB) return;
  const p = D.paras[i]; if (!p) return;
  webScrollTo(webRange(i, 0, Math.min(p.text.length, 80)), true, smooth);
}
function onWebClick(e) {
  const a = e.target.closest && e.target.closest("a[href]");
  if (a) {                                    // links open in your browser, the page stays here
    e.preventDefault(); e.stopPropagation();
    const u = a.href; if (window.pywebview?.api?.open_url) window.pywebview.api.open_url(u); else window.open(u, "_blank");
    return;
  }
  const doc = WEB.doc, pos = doc.caretRangeFromPoint ? doc.caretRangeFromPoint(e.clientX, e.clientY) : null;
  if (!pos) return;
  // which paragraph (and roughly where in it) was clicked
  for (const [i, [a0, a1]] of Object.entries(WEB.paras)) {
    const r = doc.createRange();
    try { r.setStart(WEB.mapN[a0], WEB.mapO[a0]); const e1 = Math.min(a1, WEB.mapN.length - 1); r.setEnd(WEB.mapN[e1], WEB.mapO[e1]); } catch (_) { continue; }
    if (r.isPointInRange(pos.startContainer, pos.startOffset)) {
      const pre = doc.createRange(); pre.setStart(r.startContainer, r.startOffset); pre.setEnd(pos.startContainer, pos.startOffset);
      const frac = Math.min(1, normStr(pre.toString()).length / Math.max(1, a1 - a0));
      const p = D.paras[+i], ch = Math.floor(frac * p.text.length);
      const u = (p.units || []).reduce((best, uu) => (D.units[uu].s <= ch ? uu : best), p.units?.[0]);
      if (u != null) { e.preventDefault(); jumpTo(u); }
      return;
    }
  }
}
function markWebNote(n) { /* notes on the page: reserved */ }

// ---------------------------------------------------------------- folder fan (like a Dock stack)
let fanTimer = 0, fanHide = 0, fanFor = null;
function fanSoon(tile, fid) {
  clearTimeout(fanHide); clearTimeout(fanTimer);
  if (dragging || tile.classList.contains("editing")) return;
  if (fanFor === fid && $("#fan")?.classList.contains("show")) return;
  fanTimer = setTimeout(() => showFan(tile, fid), 260);
}
function fanLeave() { clearTimeout(fanTimer); clearTimeout(fanHide); fanHide = setTimeout(closeFan, 220); }
function closeFan() {
  const f = $("#fan"); if (!f) return;
  f.classList.remove("show"); fanFor = null;
  f.querySelectorAll(".fan-i").forEach((el) => { el.style.transform = el.dataset.from; el.style.opacity = 0; });
  setTimeout(() => { if (!f.classList.contains("show")) f.innerHTML = ""; }, 260);
}
function showFan(tile, fid) {
  const subs = Object.entries(S.folders).filter(([, f]) => (f.parent || null) === fid).sort((a, b) => a[1].name.localeCompare(b[1].name));
  const docs = S.docs.filter((d) => (d.folder || null) === fid);       // already most recent first
  const items = [...subs.map(([id, f]) => ({ kind: "folder", id, name: f.name })), ...docs.map((d) => ({ kind: "doc", id: d.id, name: d.title || "Untitled" }))];
  if (!items.length) return;
  const MAX = 9, shown = items.slice(0, MAX), more = items.length - shown.length;
  if (more > 0) shown.push({ kind: "more", id: fid, name: `${more} more in “${S.folders[fid].name}”` });
  let f = $("#fan"); if (!f) { f = document.createElement("div"); f.id = "fan"; document.body.appendChild(f);
    f.addEventListener("mouseenter", () => clearTimeout(fanHide)); f.addEventListener("mouseleave", fanLeave); }
  fanFor = fid;
  const r = tile.querySelector(".cover").getBoundingClientRect();
  const STEP = 76, n = shown.length;
  const up = r.top - n * STEP > 60;                        // fan upwards when there's room, else downwards
  const ox = r.left + r.width / 2, oy = up ? r.top + 18 : r.bottom - 18;
  f.innerHTML = shown.map((it, k) => {
    const thumb = it.kind === "doc" ? `<img src="/api/cover?id=${encodeURIComponent(it.id)}" alt="" draggable="false">`
      : it.kind === "folder" ? `<span class="fan-folder">${FOLDER_SVG}</span>` : `<span class="fan-more">⋯</span>`;
    return `<button class="fan-i ${it.kind}" data-kind="${it.kind}" data-id="${it.id}" style="--k:${k}"><span class="fan-label">${esc(it.name)}</span><span class="fan-thumb">${thumb}</span></button>`;
  }).join("");
  f.classList.add("show");
  [...f.querySelectorAll(".fan-i")].forEach((el, k) => {
    // a gentle arc: each item a step further out, drifting sideways and tilting a little more
    const t = k + 1, dy = (up ? -1 : 1) * t * STEP, dx = 6 * t + 1.6 * t * t, rot = (up ? -1 : 1) * (1.2 * t);
    const from = `translate(${ox}px, ${oy}px) translate(-100%, -50%) scale(.35) rotate(0deg)`;
    el.dataset.from = from;
    el.style.transform = from; el.style.opacity = 0;
    el.style.transitionDelay = `${k * 22}ms`;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.style.transform = `translate(${ox + dx}px, ${oy + dy}px) translate(-100%, -50%) scale(1) rotate(${rot}deg)`;
      el.style.opacity = 1;
    }));
    el.onclick = (e) => {
      e.stopPropagation(); const { kind, id } = el.dataset; closeFan();
      if (kind === "doc") openDoc({ id }); else goFolder(id);
    };
  });
}
document.addEventListener("scroll", closeFan, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeFan(); });
