// ALOUD: small touches layered on app.js (focus mode, sweep, ribbon, ambient colour,
// library shelf/peek/parallax/folder styles, waveform, chapter ticks, speed dial,
// compact mode, ⌘K palette, flick-to-close, Dock ring, sounds, empty states, time-of-day).
// app.js's top-level functions are globals, so they are wrapped here.
(() => {
  const on = (k) => S.settings[k] !== false;
  const wrap = (name, after, before) => {
    const orig = window[name]; if (typeof orig !== "function") return;
    window[name] = function (...a) { before && before.apply(this, a); const r = orig.apply(this, a); after && after.apply(this, [r, ...a]); return r; };
  };
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };

  // ---------------------------------------------------------------- sounds (very quiet)
  let AC = null;
  function blip(kind) {
    if (!on("sounds")) return;
    try {
      AC = AC || new (window.AudioContext || window.webkitAudioContext)();
      const tones = { play: [[660, 0], [880, 0.05]], pause: [[740, 0], [520, 0.05]], mark: [[988, 0], [1318, 0.06]],
        note: [[784, 0], [988, 0.05], [1175, 0.1]], tick: [[1500, 0]], open: [[523, 0], [784, 0.04]] }[kind] || [[700, 0]];
      const t0 = AC.currentTime + 0.01;
      for (const [f, dt] of tones) {
        const o = AC.createOscillator(), g = AC.createGain();
        o.type = "sine"; o.frequency.value = f;
        const v = kind === "tick" ? 0.018 : 0.035;
        g.gain.setValueAtTime(0, t0 + dt); g.gain.linearRampToValueAtTime(v, t0 + dt + 0.006);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + (kind === "tick" ? 0.03 : 0.11));
        o.connect(g).connect(AC.destination); o.start(t0 + dt); o.stop(t0 + dt + 0.14);
      }
    } catch (e) { /* no audio */ }
  }
  window.aloudBlip = blip;

  // ---------------------------------------------------------------- reading ribbon + Dock ring
  const ribbon = el("div"); ribbon.id = "ribbon"; document.body.appendChild(ribbon);
  function docFrac() {
    if (!D || cur < 0 || !D.units.length) return 0;
    let f = 0;
    const m = metaCache.get(metaKey(cur));
    if (m && m.dur && !audio.paused) f = Math.min(1, audio.currentTime / m.dur);
    return Math.min(1, (cur + f) / Math.max(1, D.units.length - 1));
  }
  let dockAt = 0, dockLast = -1;
  function dock(force) {
    const api_ = window.pywebview?.api; if (!api_?.dock_progress) return;
    const now = performance.now(); if (!force && now - dockAt < 1500) return; dockAt = now;
    const lib = document.body.classList.contains("lib");
    const f = !D || lib ? null : Math.round(docFrac() * 200) / 200;
    const key = f + "|" + isBusy(); if (key === dockLast && !force) return; dockLast = key;
    try { api_.dock_progress(f, !audio.paused); } catch (e) { /* older backend */ }
  }
  function drawRibbon() {
    const lib = document.body.classList.contains("lib");
    ribbon.style.transform = `scaleX(${lib || !D ? 0 : docFrac()})`;
  }

  // ---------------------------------------------------------------- focus mode
  let calmT = 0;
  // "reading" survives the short gaps between passages (clip swaps, loading), so the dimming doesn't flicker
  let busyAt = 0;
  function focusUpdate() {
    const lib = document.body.classList.contains("lib");
    if (isBusy()) busyAt = performance.now();
    const reading = performance.now() - busyAt < 2500;
    const want = on("focus") && D && !lib && reading && performance.now() > calmT;
    document.body.classList.toggle("focusing", !!want);
    document.body.classList.toggle("focusmode", !!(on("focus") && D && !lib && !document.body.classList.contains("compact")));
  }
  addEventListener("mousemove", () => { calmT = performance.now() + 1500; focusUpdate(); }, { passive: true });
  addEventListener("wheel", () => { calmT = performance.now() + 1500; focusUpdate(); }, { passive: true });
  setInterval(focusUpdate, 400);
  // the PDF: a spotlight on the lines being read, everything else dimmed
  let spot = null;
  function pdfSpot() {
    if (!urEls.length || !urEls[0].parentNode) { spot?.remove(); return; }
    const ov = urEls[0].parentNode;
    let l = 1e9, t = 1e9, r = -1e9, b = -1e9;
    for (const e of urEls) {
      if (e.parentNode !== ov) continue;
      const L = parseFloat(e.style.left), T = parseFloat(e.style.top), W = parseFloat(e.style.width), H = parseFloat(e.style.height);
      l = Math.min(l, L); t = Math.min(t, T); r = Math.max(r, L + W); b = Math.max(b, T + H);
    }
    if (!(r > l)) return;
    if (!spot) { spot = document.createElement("div"); spot.className = "spot"; }
    if (spot.parentNode !== ov) ov.appendChild(spot);
    Object.assign(spot.style, { left: (l - 1.5) + "%", top: (t - 0.8) + "%", width: (r - l + 3) + "%", height: (b - t + 1.6) + "%" });
  }
  wrap("pdfUnit", pdfSpot);
  const fBtn = el("button", "icon", `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8V5a2 2 0 0 1 2-2h3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M8 21H5a2 2 0 0 1-2-2v-3"/><path d="M7 10h10M7 14h6"/></svg>`);
  fBtn.id = "focusBtn";
  const setFocusBtn = () => { fBtn.classList.toggle("on", on("focus")); fBtn.title = `Focus mode is ${on("focus") ? "on" : "off"} (F): while reading, everything but the current passage fades`; };
  function toggleFocus() {
    saveSettings({ focus: !on("focus") }); setFocusBtn(); syncOpts();
    calmT = 0; focusUpdate();
    toast(on("focus") ? "Focus mode on · Space plays and pauses · F to leave" : "Focus mode off", 2200);
  }
  fBtn.onclick = toggleFocus;
  $("#themeBtn")?.before(fBtn);
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.target.matches?.("input, select, textarea")) return;
    if ((e.key === "f" || e.key === "F") && D && !document.body.classList.contains("lib")) { e.preventDefault(); toggleFocus(); }
  });

  // ---------------------------------------------------------------- karaoke sweep
  function sweep() {
    if (!wordEl) return;
    if (!on("sweep")) { wordEl.style.removeProperty("--sw"); return; }
    const m = metaCache.get(metaKey(cur)), w = m && wordK >= 0 ? m.words[wordK] : null;
    if (!w) return;
    const t0 = w[2], t1 = w[3] > t0 ? w[3] : (m.words[wordK + 1]?.[2] ?? t0 + 0.3);
    const f = Math.max(0, Math.min(1, (audio.currentTime - t0) / Math.max(0.05, t1 - t0)));
    wordEl.style.setProperty("--sw", (f * 100).toFixed(1) + "%");
  }

  // ---------------------------------------------------------------- mini waveform of the passage
  const wave = el("canvas"); wave.id = "wave"; wave.width = 280; wave.height = 40;
  $("#player .now")?.appendChild(wave);
  let waveSrc = "", peaks = null;
  async function loadWave() {
    const src = audio.src; if (!src || src === waveSrc) return; waveSrc = src; peaks = null;
    try {
      AC = AC || new (window.AudioContext || window.webkitAudioContext)();
      const buf = await AC.decodeAudioData(await (await fetch(src)).arrayBuffer());
      if (src !== waveSrc) return;
      const d = buf.getChannelData(0), N = 70, per = Math.floor(d.length / N) || 1, out = [];
      for (let i = 0; i < N; i++) { let m = 0; for (let j = i * per; j < (i + 1) * per && j < d.length; j += 8) m = Math.max(m, Math.abs(d[j])); out.push(m); }
      const mx = Math.max(...out, 0.01); peaks = out.map((v) => Math.pow(v / mx, 0.7));
    } catch (e) { peaks = null; }
    drawWave();
  }
  function drawWave() {
    const c = wave.getContext("2d"), W = wave.width, H = wave.height;
    c.clearRect(0, 0, W, H);
    if (!peaks || !D) { wave.classList.remove("show"); return; }
    wave.classList.add("show");
    const cs = getComputedStyle(document.documentElement);
    const acc = cs.getPropertyValue("--accent").trim() || "#0a84ff", dim = cs.getPropertyValue("--line").trim() || "#ccc";
    const f = audio.duration ? audio.currentTime / audio.duration : 0, bw = W / peaks.length;
    peaks.forEach((p, i) => {
      const h = Math.max(3, p * (H - 6));
      c.fillStyle = (i + 0.5) / peaks.length <= f ? acc : dim;
      const x = i * bw + bw * 0.2, w = bw * 0.6, y = (H - h) / 2;
      c.beginPath(); c.roundRect ? c.roundRect(x, y, w, h, w / 2) : c.rect(x, y, w, h); c.fill();
    });
  }

  // ---------------------------------------------------------------- hooks into playback
  let lastFrameDraw = 0;
  wrap("step", () => {
    sweep();
    const now = performance.now();
    if (now - lastFrameDraw > 120) { lastFrameDraw = now; drawRibbon(); drawWave(); loadWave(); }
    dock(false);
  });
  wrap("setPlaying", (r, playing) => {
    if (setPlaying._was !== playing && setPlaying._was !== undefined) blip(playing ? "play" : "pause");
    setPlaying._was = playing;
    if (playing) busyAt = performance.now();
    focusUpdate(); dock(true); drawRibbon();
  });
  wrap("updateInfo", () => { drawRibbon(); chapterTicks(); });
  wrap("addMark", () => blip("mark"));
  wrap("saveNote", () => blip("note"));

  // ---------------------------------------------------------------- chapter ticks
  let ticksFor = null;
  const bubble = el("div"); bubble.id = "progTip"; $("#progress")?.appendChild(bubble);
  function chapterTicks() {
    if (!D || ticksFor === D) return; ticksFor = D;
    const tr = $("#progress .track"); if (!tr) return;
    tr.querySelectorAll(".tick").forEach((t) => t.remove());
    const n = Math.max(1, D.units.length - 1);
    for (const s of (D.sections || []).slice(0, 80)) {
      const u = unitAtPara(s.para); if (u == null || u <= 0) continue;
      const t = el("i", "tick"); t.style.left = (100 * u / n) + "%"; tr.appendChild(t);
    }
  }
  $("#progress")?.addEventListener("mousemove", (e) => {
    if (!D) return;
    const r = e.currentTarget.getBoundingClientRect(), f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    const u = Math.round(f * (D.units.length - 1)), s = sectionFor(paraOf(u));
    bubble.textContent = s ? s.title : D.title;
    bubble.style.left = Math.min(r.width - 20, Math.max(20, e.clientX - r.left)) + "px";
    bubble.classList.add("show");
  });
  $("#progress")?.addEventListener("mouseleave", () => bubble.classList.remove("show"));

  // ---------------------------------------------------------------- speed dial (drag the speed)
  (() => {
    const out = $("#rate"); if (!out) return;
    out.title = "Drag sideways to change speed · click for 1×";
    let x0 = 0, r0 = 1, moved = false, lastSnap = null;
    out.addEventListener("pointerdown", (e) => {
      x0 = e.clientX; r0 = S.settings.rate; moved = false; lastSnap = null; out.setPointerCapture(e.pointerId); out.classList.add("dial");
    });
    out.addEventListener("pointermove", (e) => {
      if (!out.classList.contains("dial")) return;
      const dx = e.clientX - x0; if (Math.abs(dx) > 3) moved = true; if (!moved) return;
      let r = Math.min(3, Math.max(0.6, r0 + dx / 90));
      const snap = [1, 1.25, 1.5, 2].find((s) => Math.abs(r - s) < 0.05);
      if (snap) { r = snap; if (lastSnap !== snap) blip("tick"); }
      lastSnap = snap || null;
      setRate(r);
    });
    const end = () => { out.classList.remove("dial"); };
    out.addEventListener("pointerup", end); out.addEventListener("pointercancel", end);
    out.addEventListener("click", (e) => { if (moved) { e.stopImmediatePropagation(); moved = false; } }, true);
  })();

  // ---------------------------------------------------------------- ambient colour
  const ambCache = new Map();
  function coverColour(id) {
    if (ambCache.has(id)) return ambCache.get(id);
    const p = new Promise((res) => {
      const img = new Image();
      img.onload = () => {
        try {
          const c = document.createElement("canvas"); c.width = c.height = 16;
          const g = c.getContext("2d"); g.drawImage(img, 0, 0, 16, 16);
          const d = g.getImageData(0, 0, 16, 16).data;
          let r = 0, gg = 0, b = 0, w = 0;
          for (let i = 0; i < d.length; i += 4) {
            const mx = Math.max(d[i], d[i + 1], d[i + 2]), mn = Math.min(d[i], d[i + 1], d[i + 2]);
            const sat = mx ? (mx - mn) / mx : 0, wt = 0.15 + sat * sat * 4;     // colourful pixels count more than white paper
            r += d[i] * wt; gg += d[i + 1] * wt; b += d[i + 2] * wt; w += wt;
          }
          res([r / w, gg / w, b / w].map(Math.round));
        } catch (e) { res(null); }
      };
      img.onerror = () => res(null);
      img.src = `/api/cover?id=${encodeURIComponent(id)}`;
    });
    ambCache.set(id, p); return p;
  }
  async function ambient() {
    const root = document.documentElement;
    if (!on("ambient") || !D || document.body.classList.contains("lib")) { root.style.removeProperty("--amb"); document.body.classList.remove("ambient"); return; }
    const c = await coverColour(D.id);
    if (!c || !D) return;
    root.style.setProperty("--amb", c.join(","));
    document.body.classList.add("ambient");
  }

  // ---------------------------------------------------------------- time of day (library) + gentle theme fade
  function timeOfDay() {
    const h = new Date().getHours();
    const tod = h < 6 ? "night" : h < 11 ? "morning" : h < 17 ? "day" : h < 21 ? "evening" : "night";
    document.body.dataset.tod = tod;
  }
  timeOfDay(); setInterval(timeOfDay, 10 * 60 * 1000);
  let lastDark = null;
  wrap("applyLook", () => {
    const d = isDark();
    if (lastDark !== null && d !== lastDark) {
      document.documentElement.classList.add("fade");
      clearTimeout(applyLook._f); applyLook._f = setTimeout(() => document.documentElement.classList.remove("fade"), 1300);
      drawWave();
    }
    lastDark = d;
  });

  // ---------------------------------------------------------------- library: shelf, folder styles, FLIP, parallax, empty states
  wrap("docCard", (card, d) => { card.dataset.id = d.id; });
  const TAGS = [["Red", "#ff5f57"], ["Orange", "#ff9f0a"], ["Yellow", "#ffd426"], ["Green", "#30d158"], ["Blue", "#0a84ff"], ["Purple", "#bf5af2"], ["Grey", "#98989d"]];
  const EMOJI = ["📄", "📚", "🧠", "🔬", "📈", "⚖️", "💡", "🧪", "🎓", "🌍", "💻", "🏛️", "📰", "✍️", "⭐", "❤️"];
  async function styleFolder(fid, patch) {
    try { const r = await api("/api/folder", { op: "style", id: fid, ...patch }); S.folders = r.folders || S.folders; }
    catch (e) { Object.assign(S.folders[fid], patch); }
    renderLibrary();
  }
  function decorateFolders() {
    document.querySelectorAll("#grid .card.folder").forEach((t) => {
      const fid = t.dataset.f, f = S.folders[fid]; if (!f) return;
      if (f.color) { t.style.setProperty("--folder", f.color); t.style.setProperty("--folder-back", `color-mix(in srgb, ${f.color} 72%, #000)`); }
      if (f.emoji) t.querySelector(".cover").appendChild(el("span", "f-emoji", esc(f.emoji)));
      const base = t.oncontextmenu;
      t.oncontextmenu = (e) => {
        e.preventDefault();
        menuAt(e, [["Open", () => goFolder(fid)], ["Rename…", () => renameFolder(t, fid)],
          ["Colour", null, TAGS.map(([n, c]) => [(f.color === c ? "✓ " : "● ") + n, () => styleFolder(fid, { color: c })]).concat([["-"], ["No colour", () => styleFolder(fid, { color: null })]])],
          ["Emoji", null, EMOJI.map((x) => [x + (f.emoji === x ? "  ✓" : ""), () => styleFolder(fid, { emoji: x })]).concat([["-"], ["No emoji", () => styleFolder(fid, { emoji: null })]])],
          ["Move to…", null, moveTargets([], [fid])], ["-"], ["Delete folder", () => deleteFolder(fid)]]);
      };
      void base;
    });
  }
  // colour the dots in the Colour submenu
  const ctxObs = new MutationObserver(() => {
    document.querySelectorAll("#ctxMenu button").forEach((b) => {
      const t = TAGS.find(([n]) => b.textContent.trim().endsWith(" " + n) && /^[●✓]/.test(b.textContent.trim()));
      if (t && !b.querySelector(".dot")) b.innerHTML = `<i class="dot" style="background:${t[1]}"></i>${esc(t[0])}${b.textContent.trim().startsWith("✓") ? " ✓" : ""}`;
    });
  });
  const hookCtx = () => { const m = $("#ctxMenu"); if (m && !m._obs) { m._obs = 1; ctxObs.observe(m, { childList: true }); } };
  document.addEventListener("contextmenu", () => setTimeout(hookCtx, 0), true);
  const ring = (pct) => { const R = 15, C = 2 * Math.PI * R; return `<svg class="ring" viewBox="0 0 36 36"><circle cx="18" cy="18" r="${R}" class="bg"/><circle cx="18" cy="18" r="${R}" class="fg" stroke-dasharray="${C}" stroke-dashoffset="${C * (1 - pct / 100)}"/></svg>`; };
  function shelf() {
    const q = ($("#libFind")?.value || "").trim();
    if (libFolder || q) return;
    const docs = (S.docs || []).filter((d) => d.units > 1 && d.pos && d.pos.unit > 0 && d.pos.unit < d.units - 2).slice(0, 4);
    if (!docs.length) return;
    const s = el("div", "shelf", `<div class="shelf-h">Continue listening</div><div class="shelf-row"></div>`);
    const row = s.querySelector(".shelf-row");
    docs.forEach((d, i) => {
      const pct = Math.round(100 * d.pos.unit / Math.max(1, d.units - 1));
      const c = el("button", "sh-i", `<div class="sh-cov"><img alt="" src="/api/cover?id=${encodeURIComponent(d.id)}" onerror="this.style.visibility='hidden'"></div>
        <div class="sh-t"><b>${esc(d.title || "Untitled")}</b><span>${esc(timeLeft(d) || "")}</span></div>
        <div class="sh-play">${ring(pct)}<svg class="pl" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l11-6.5z"/></svg></div>`);
      c.style.setProperty("--i", i);
      c.title = `Resume “${d.title}” (${pct}%)`;
      c.onclick = () => { blip("open"); openDoc({ id: d.id }, null, true); };
      coverColour(d.id).then((col) => col && c.style.setProperty("--sh", col.join(",")));
      row.appendChild(c);
    });
    $("#recent .lib-head")?.after(s);
  }
  // FLIP: cards glide to their new places when the library re-renders
  let flipFrom = null;
  const keyOf = (c) => c.dataset.id ? "d" + c.dataset.id : c.dataset.f ? "f" + c.dataset.f : null;
  function flipBefore() {
    flipFrom = new Map();
    document.querySelectorAll("#grid .card").forEach((c) => { const k = keyOf(c); if (k) flipFrom.set(k, c.getBoundingClientRect()); });
  }
  function flipAfter() {
    if (!flipFrom || !flipFrom.size) return;
    const cards = [...document.querySelectorAll("#grid .card")];
    let moved = 0;
    for (const c of cards) {
      const a = flipFrom.get(keyOf(c)); if (!a) continue;
      const b = c.getBoundingClientRect(), dx = a.left - b.left, dy = a.top - b.top;
      c.style.animation = "none";                         // no entry animation for cards that were already there
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
      moved++;
      c.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 420, easing: "cubic-bezier(.22,1,.36,1)" });
    }
    flipFrom = null;
  }
  function parallax() {
    document.querySelectorAll("#grid .card .cover, .shelf .sh-cov").forEach((cv) => {
      if (cv._px) return; cv._px = 1;
      cv.addEventListener("mousemove", (e) => {
        const r = cv.getBoundingClientRect(), x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
        cv.style.setProperty("--rx", (-y * 7).toFixed(2) + "deg"); cv.style.setProperty("--ry", (x * 9).toFixed(2) + "deg");
        cv.style.setProperty("--gx", ((x + 0.5) * 100).toFixed(0) + "%"); cv.style.setProperty("--gy", ((y + 0.5) * 100).toFixed(0) + "%");
        cv.classList.add("tilt");
      });
      cv.addEventListener("mouseleave", () => { cv.classList.remove("tilt"); cv.style.setProperty("--rx", "0deg"); cv.style.setProperty("--ry", "0deg"); });
    });
  }
  const ART = {
    folder: `<svg viewBox="0 0 120 90" class="empty-art"><path d="M14 22a6 6 0 0 1 6-6h24l7 7h48a6 6 0 0 1 6 6v43a6 6 0 0 1-6 6H20a6 6 0 0 1-6-6z" fill="var(--folder-back, #9cc3f5)" opacity=".55"/><path d="M14 34a5 5 0 0 1 5-5h82a5 5 0 0 1 5 5v38a6 6 0 0 1-6 6H20a6 6 0 0 1-6-6z" fill="var(--folder, #b9d6fa)" opacity=".8"/><path class="float" d="M50 8l20 0 0 26-20 0z" fill="var(--panel, #fff)" stroke="var(--line, #ccc)"/><path class="float" d="M54 14h12M54 19h12M54 24h8" stroke="var(--muted, #999)" stroke-linecap="round"/></svg>`,
    notes: `<svg viewBox="0 0 120 80" class="empty-art"><rect x="30" y="10" width="54" height="62" rx="7" fill="var(--panel, #fff)" stroke="var(--line, #ccc)"/><path d="M40 26h34M40 34h34M40 42h22" stroke="var(--muted, #aaa)" stroke-linecap="round" stroke-width="2.5"/><path class="float" d="M78 44l14-14 6 6-14 14-8 2z" fill="#ffd426" stroke="#c9a400" stroke-linejoin="round"/></svg>`,
    queue: `<svg viewBox="0 0 120 80" class="empty-art"><rect x="22" y="22" width="44" height="50" rx="6" fill="var(--line, #ddd)" opacity=".7" transform="rotate(-8 44 47)"/><rect x="38" y="16" width="44" height="52" rx="6" fill="var(--panel, #fff)" stroke="var(--line, #ccc)"/><circle class="float" cx="88" cy="22" r="11" fill="var(--accent, #0a84ff)"/><path class="float" d="M84 22h8M88 18v8" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></svg>`,
    lib: `<svg viewBox="0 0 140 90" class="empty-art"><rect x="30" y="18" width="20" height="62" rx="3" fill="#ff9f0a" opacity=".85"/><rect x="53" y="10" width="18" height="70" rx="3" fill="var(--accent, #0a84ff)" opacity=".85"/><rect x="74" y="24" width="16" height="56" rx="3" fill="#30d158" opacity=".8"/><rect class="float" x="94" y="22" width="18" height="58" rx="3" fill="#bf5af2" opacity=".8" transform="rotate(10 103 51)"/><path d="M20 81h100" stroke="var(--line, #ccc)" stroke-width="3" stroke-linecap="round"/></svg>`,
  };
  wrap("renderLibrary", () => {
    decorateFolders(); shelf(); flipAfter(); parallax();
    const e = $("#grid .lib-empty"); if (e && !e.querySelector(".empty-art")) e.insertAdjacentHTML("afterbegin", libFolder ? ART.folder : ART.lib);
    ambient(); dock(true); drawRibbon();
  }, flipBefore);
  wrap("renderNotes", () => { const e = $("#notesList .nl-empty"); if (e && !e.querySelector(".empty-art")) e.insertAdjacentHTML("afterbegin", ART.notes); });
  wrap("renderQueue", () => { const e = $("#queuePop .q-empty"); if (e && !e.querySelector(".empty-art")) e.insertAdjacentHTML("afterbegin", ART.queue); });

  // ambient colour follows the open document
  let ambFor = null;
  setInterval(() => { const k = (D && D.id) + "|" + document.body.classList.contains("lib"); if (k !== ambFor) { ambFor = k; ambient(); drawRibbon(); dock(true); ticksFor = null; chapterTicks(); } }, 400);

  // ---------------------------------------------------------------- peek (Space on a hovered card)
  let hoverCard = null;
  document.addEventListener("mouseover", (e) => { hoverCard = e.target.closest?.("#grid .card:not(.folder)") || (e.target.closest?.("#peek") ? hoverCard : null); });
  const peek = el("div"); peek.id = "peek"; document.body.appendChild(peek);
  async function showPeek(id) {
    const d = (S.docs || []).find((x) => x.id === id); if (!d) return;
    const pct = d.units && d.pos ? Math.round(100 * (d.pos.unit || 0) / Math.max(1, d.units - 1)) : 0;
    const inQ = (S.queue || []).includes(id);
    peek.innerHTML = `<div class="pk-box"><div class="pk-cov"><img alt="" src="/api/cover?id=${encodeURIComponent(id)}" onerror="this.style.visibility='hidden'"></div>
      <div class="pk-info"><span class="pk-kind">${esc(kindOf(d.path))}</span><h3>${esc(d.title || "Untitled")}</h3>
      <dl><dt>Length</dt><dd>${d.pages > 1 ? d.pages + " pages · " : ""}${esc(timeLeft(d) || "")}</dd>
      <dt>Progress</dt><dd>${pct ? pct + "% listened" : "Not started"}</dd><dt>Notes</dt><dd class="pk-notes">…</dd></dl>
      <div class="pk-btns"><button class="pk-open">${pct ? "Resume" : "Open"}</button><button class="pk-q">${inQ ? "✓ Up next" : "+ Up next"}</button></div>
      <p class="pk-hint">Space or Esc to close</p></div></div>`;
    peek.classList.add("show");
    coverColour(id).then((c) => c && peek.style.setProperty("--pk", c.join(",")));
    peek.querySelector(".pk-open").onclick = () => { closePeek(); openDoc({ id }, null, !!pct); };
    peek.querySelector(".pk-q").onclick = async (e) => {
      const was = (S.queue || []).includes(id);
      S.queue = (await api("/api/queue", { op: was ? "remove" : "add", id })).queue;
      e.target.textContent = was ? "+ Up next" : "✓ Up next";
      const b = document.querySelector(`#grid .card[data-id="${CSS.escape(id)}"] .qadd`); if (b) { b.classList.toggle("on", !was); b.textContent = e.target.textContent; }
    };
    try { const r = await api(`/api/notes?doc=${encodeURIComponent(id)}`); const n = (r.notes || []).length; peek.querySelector(".pk-notes").textContent = n ? `${n} mark${n === 1 ? "" : "s"} & notes` : "None"; }
    catch (e) { peek.querySelector(".pk-notes").textContent = "None"; }
  }
  const closePeek = () => peek.classList.remove("show");
  peek.addEventListener("mousedown", (e) => { if (e.target === peek) closePeek(); });

  // ---------------------------------------------------------------- compact mode
  function compact(onoff) {
    const want = onoff ?? !document.body.classList.contains("compact");
    if (want && !D) { toast("Open something to read first"); return; }
    document.body.classList.toggle("compact", want);
    try { window.pywebview?.api?.compact?.(want); } catch (e) { /* browser */ }
    if (!want) toast("Full window", 1200);
  }
  const cBtn = el("button", "icon", `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/></svg>`);
  cBtn.id = "compactBtn"; cBtn.title = "Compact player: a small strip that stays on top (⌘⇧M)"; cBtn.onclick = () => compact();
  $("#player .right")?.appendChild(cBtn);

  // ---------------------------------------------------------------- ⌘K command palette
  const pal = el("div"); pal.id = "palette";
  pal.innerHTML = `<div class="pl-box"><input id="plQ" placeholder="Search documents, sections, folders, voices, commands…" autocomplete="off" spellcheck="false"><div class="pl-list" id="plList"></div><div class="pl-foot"><span>↑↓ to move · ↩ to choose · esc to close</span></div></div>`;
  document.body.appendChild(pal);
  let plItems = [], plSel = 0;
  function commands() {
    const c = [];
    const cmd = (label, fn, hint = "") => c.push({ kind: "Command", icon: "⌘", label, fn, hint });
    cmd(isDark() ? "Light theme" : "Dark theme", () => setTheme(isDark() ? "light" : "dark"), "D");
    for (const t of ["light", "sepia", "dark", "auto"]) cmd(`Theme: ${t[0].toUpperCase() + t.slice(1)}`, () => setTheme(t));
    cmd("Library", () => showLibrary(), "");
    cmd("Open a file…", () => chooseFile(), "⌘O");
    cmd("Paste text or a link…", () => showPaste(), "⌘⇧V");
    cmd("Voice & settings", () => $("#settings").classList.add("show"), "⌘,");
    cmd("Guide & shortcuts", () => openWiki(), "?");
    cmd("Up next", () => toggleQueue(), "Q");
    cmd("Compact player", () => compact(), "⌘⇧M");
    cmd("New folder", () => { if (!document.body.classList.contains("lib")) showLibrary().then(() => newFolder()); else newFolder(); });
    const tog = (k, name) => cmd(`${on(k) ? "Turn off" : "Turn on"} ${name}`, () => { saveSettings({ [k]: !on(k) }); syncOpts(); toast(`${name} ${on(k) ? "on" : "off"}`); focusUpdate(); ambient(); });
    cmd(`${on("focus") ? "Turn off" : "Turn on"} focus mode`, () => toggleFocus(), "F"); tog("sweep", "smooth highlight"); tog("ambient", "ambient colour"); tog("sounds", "sounds");
    if (D) {
      if (hasPdf()) for (const [v, n] of [["text", "Text"], ["split", "Both"], ["pdf", D.web ? "Page" : "PDF"]]) cmd(`View: ${n}`, () => setView(v));
      cmd("Save as audio", () => $("#exportBtn")?.click());
      for (const r of [1, 1.25, 1.5, 1.75, 2]) cmd(`Speed ${r}×`, () => setRate(r));
    }
    return c;
  }
  function allItems() {
    const out = [];
    if (D) for (const s of D.sections || []) out.push({ kind: "Section", icon: "§", label: s.title, fn: () => { const u = unitAtPara(s.para); jumpTo(u); scrollToPara(s.para, true); } });
    for (const d of S.docs || []) out.push({ kind: "Document", icon: "📄", label: d.title || "Untitled", hint: d.folder && S.folders?.[d.folder] ? S.folders[d.folder].name : "", fn: () => openDoc({ id: d.id }) });
    for (const [fid, f] of Object.entries(S.folders || {})) out.push({ kind: "Folder", icon: f.emoji || "📁", label: f.name, fn: async () => { if (!document.body.classList.contains("lib")) await showLibrary(); goFolder(fid); } });
    for (const [id, label, grade] of S.voices || []) out.push({ kind: "Voice", icon: "🗣", label: `Voice: ${label}`, hint: grade || "", fn: () => chooseVoice(id) });
    return out.concat(commands());
  }
  function score(q, s) {
    s = s.toLowerCase(); if (!q) return 1;
    const i = s.indexOf(q); if (i >= 0) return 100 - i - s.length * 0.05 + (i === 0 || s[i - 1] === " " ? 20 : 0);
    let j = 0, sc = 0, last = -2;
    for (let k = 0; k < s.length && j < q.length; k++) if (s[k] === q[j]) { sc += last === k - 1 ? 3 : 1; last = k; j++; }
    return j === q.length ? sc - s.length * 0.05 : -1;
  }
  function plRender() {
    const q = $("#plQ").value.toLowerCase().trim();
    let items = allItems().map((it) => ({ it, s: score(q, it.label) })).filter((x) => x.s > 0);
    const ORDER = ["Section", "Document", "Folder", "Command", "Voice"];
    if (!q) { items = items.filter((x) => x.it.kind !== "Voice"); items.sort((a, b) => ORDER.indexOf(a.it.kind) - ORDER.indexOf(b.it.kind)); }
    else items.sort((a, b) => b.s - a.s);
    plItems = items.slice(0, 60).map((x) => x.it);
    plSel = Math.min(plSel, Math.max(0, plItems.length - 1));
    $("#plList").innerHTML = plItems.length ? plItems.map((it, i) => `<div class="pl-i${i === plSel ? " sel" : ""}" data-i="${i}"><span class="pl-ic">${esc(it.icon)}</span><span class="pl-l">${esc(it.label)}</span><span class="pl-k">${esc(it.hint || it.kind)}</span></div>`).join("")
      : `<div class="pl-none">Nothing found</div>`;
    $("#plList .sel")?.scrollIntoView({ block: "nearest" });
  }
  function openPalette() { pal.classList.add("show"); $("#plQ").value = ""; plSel = 0; plRender(); setTimeout(() => $("#plQ").focus(), 20); }
  function closePalette() { pal.classList.remove("show"); }
  function plRun(i) { const it = plItems[i]; closePalette(); if (it) setTimeout(() => it.fn(), 30); }
  $("#plQ").addEventListener("input", () => { plSel = 0; plRender(); });
  $("#plQ").addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "ArrowDown") { e.preventDefault(); plSel = Math.min(plItems.length - 1, plSel + 1); plRender(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); plSel = Math.max(0, plSel - 1); plRender(); }
    else if (e.key === "Enter") { e.preventDefault(); plRun(plSel); }
    else if (e.key === "Escape" || (e.metaKey && e.key.toLowerCase() === "k")) { e.preventDefault(); closePalette(); }
  });
  $("#plList").addEventListener("click", (e) => { const r = e.target.closest(".pl-i"); if (r) plRun(+r.dataset.i); });
  $("#plList").addEventListener("mousemove", (e) => { const r = e.target.closest(".pl-i"); if (r && +r.dataset.i !== plSel) { plSel = +r.dataset.i; $("#plList .sel")?.classList.remove("sel"); r.classList.add("sel"); } });
  pal.addEventListener("mousedown", (e) => { if (e.target === pal) closePalette(); });

  // keys: ⌘K palette, ⌘⇧M compact, Space peek (capture phase, before app.js)
  document.addEventListener("keydown", (e) => {
    if (e.metaKey && !e.shiftKey && e.key.toLowerCase() === "k") { e.preventDefault(); e.stopImmediatePropagation(); pal.classList.contains("show") ? closePalette() : openPalette(); return; }
    if (e.metaKey && e.shiftKey && e.key.toLowerCase() === "m") { e.preventDefault(); e.stopImmediatePropagation(); compact(); return; }
    if (e.target.matches?.("input, select, textarea")) return;
    if (peek.classList.contains("show") && (e.key === " " || e.key === "Escape")) { e.preventDefault(); e.stopImmediatePropagation(); closePeek(); return; }
    if (e.key === " " && !e.metaKey && document.body.classList.contains("lib") && hoverCard?.dataset.id) { e.preventDefault(); e.stopImmediatePropagation(); showPeek(hoverCard.dataset.id); }
  }, true);

  // ---------------------------------------------------------------- flick panels closed (spring physics)
  function flickable(sel, close, handleSel) {
    document.addEventListener("pointerdown", (e) => {
      const p = e.target.closest(sel); if (!p || e.button !== 0) return;
      if (e.target.closest("input, textarea, select, button, a, .voices, [contenteditable], .switch, .pl-list, .wk-body")) return;
      if (handleSel && !e.target.closest(handleSel)) return;
      const y0 = e.clientY, t0 = performance.now(); let dy = 0, lastY = y0, lastT = t0, v = 0, dragging = false;
      const move = (ev) => {
        const raw = ev.clientY - y0;
        if (!dragging && Math.abs(raw) < 6) return;
        dragging = true; p.classList.add("flicking");
        dy = raw > 0 ? raw : -Math.pow(-raw, 0.6) * 2;               // rubber-band against dragging up
        const now = performance.now(); v = (ev.clientY - lastY) / Math.max(1, now - lastT); lastY = ev.clientY; lastT = now;
        p.style.translate = `0 ${dy}px`; p.style.opacity = String(Math.max(0.35, 1 - Math.max(0, dy) / 500));
      };
      const up = () => {
        removeEventListener("pointermove", move); removeEventListener("pointerup", up);
        p.classList.remove("flicking");
        if (!dragging) return;
        if (dy > 110 || v > 0.7) {
          p.animate([{ translate: `0 ${dy}px`, opacity: p.style.opacity }, { translate: `0 ${dy + 160}px`, opacity: 0 }], { duration: 220, easing: "cubic-bezier(.4,0,1,1)" })
            .finished.then(() => { close(p); p.style.translate = ""; p.style.opacity = ""; });
        } else {
          p.animate([{ translate: `0 ${dy}px` }, { translate: "0 -6px", offset: 0.55 }, { translate: "0 0" }], { duration: 480, easing: "cubic-bezier(.2,.9,.22,1.04)" });
          p.style.translate = ""; p.style.opacity = "";
        }
      };
      addEventListener("pointermove", move); addEventListener("pointerup", up);
    });
  }
  flickable("#settings.show", (p) => p.classList.remove("show"));
  flickable("#queuePop.show", (p) => p.classList.remove("show"));
  flickable("#citeCard.show", (p) => p.classList.remove("show"));
  flickable("#wiki.show .wk-box", () => closeWiki(), ".wk-head, .wk-box > header, h2:first-child");
  flickable("#peek.show .pk-box", () => closePeek());
  flickable("#palette.show .pl-box", () => closePalette(), ".pl-foot");

  // ---------------------------------------------------------------- settings switches
  const OPTS = { optFocus: "focus", optSweep: "sweep", optAmbient: "ambient", optSounds: "sounds" };
  function syncOpts() { try { setFocusBtn(); } catch (e) {} for (const [id, k] of Object.entries(OPTS)) { const x = document.getElementById(id); if (x) x.checked = on(k); } document.body.classList.toggle("sweep", on("sweep")); }
  for (const [id, k] of Object.entries(OPTS)) {
    const x = document.getElementById(id);
    if (x) x.onchange = (e) => { saveSettings({ [k]: e.target.checked }); syncOpts(); focusUpdate(); ambient(); if (k === "sounds" && e.target.checked) blip("mark"); };
  }
  wrap("buildSettings", syncOpts);
  syncOpts();
})();
