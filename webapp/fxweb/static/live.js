// Live page (signal analysis + recording) and Takes page. Plain JS, no dependencies.
// The box does the analysis; this file only draws the frames it streams
// (Server-Sent Events, ~30 per second) and drives recording.
// Uses api(), toast(), busy() from app.js.
"use strict";

(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (tag, props = {}, ...kids) => {
    const n = Object.assign(document.createElement(tag), props);
    for (const k of kids) n.append(k);
    return n;
  };
  const NOTE_NAMES = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"];

  const L = {
    view: null,
    es: null,
    fps: 30,
    frozen: false,
    frame: null,          // latest frame being displayed
    channels: [],
    sources: [],
    status: null,
    lastT: null,          // source time of the previous frame
    framesSeen: 0,
    rateWindow: [],
    rec: null,
    recAt: 0,             // performance.now() when rec status arrived (to interpolate the timer)
    meters: [],           // per channel {peak, rms, hold, holdAt, clipLatched}
    spec: null,           // smoothed dB per channel (Float32Array)
    specPeak: null,
    onsetHist: [],        // [{t, db}]
    onsets: [],           // [{t, strength}]
    tunerCents: 0,
    noteSeenAt: 0,
    sg: null,             // spectrogram offscreen canvas
    visible: new Set(),
    drawPending: false,
  };

  // ---------------------------------------------------------------- utils

  function b64bytes(s) {
    const bin = atob(s);
    const a = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
    return a;
  }
  const fmtTime = (s, tenths = true) => {
    const m = Math.floor(s / 60), r = s - m * 60;
    return `${m}:${(tenths ? r.toFixed(1) : String(Math.floor(r))).padStart(tenths ? 4 : 2, "0")}`;
  };
  const fmtBytes = (b) => b > 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round(b / 1e3)} kB`;
  const fmtHz = (f) => f >= 1000 ? `${(f / 1000).toFixed(f >= 10000 ? 0 : 1)}k` : `${Math.round(f)}`;
  const noteOf = (f) => {
    const m = Math.round(69 + 12 * Math.log2(f / 440));
    return `${NOTE_NAMES[((m % 12) + 12) % 12]}${Math.floor(m / 12) - 1}`;
  };
  const dbStr = (v) => (v == null || v <= -119 ? "−∞" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)}`);

  let C = {};
  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    C = { text: v("--text"), muted: v("--muted"), line: v("--line"), surface: v("--surface"),
      surface2: v("--surface-2"), ok: v("--ok"), warn: v("--warn"), bad: v("--bad"),
      ch: [v("--ch0"), v("--ch1"), v("--ch2"), v("--ch3")] };
  }

  function fit(cv) {
    const r = cv.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, r.width, r.height);
    ctx.font = "11px system-ui, -apple-system, sans-serif";
    return { ctx, w: r.width, h: r.height };
  }

  function chColor(c, alpha = 1) {
    const base = C.ch[c % C.ch.length] || "#888";
    return alpha >= 1 ? base : `color-mix(in srgb, ${base} ${Math.round(alpha * 100)}%, transparent)`;
  }

  // ---------------------------------------------------------------- stream

  function wantStream() {
    return L.view === "live" && document.visibilityState === "visible";
  }

  function syncStream() {
    if (wantStream() && !L.es) {
      L.es = new EventSource(`/api/live/stream?fps=${L.fps}`);
      L.es.onmessage = (e) => onFrame(JSON.parse(e.data));
      L.es.onerror = () => setState("waiting", "Reconnecting to the box…");
    } else if (!wantStream() && L.es) {
      L.es.close();
      L.es = null;
    }
  }
  function restartStream() {
    if (L.es) { L.es.close(); L.es = null; }
    syncStream();
  }

  function onFrame(f) {
    const now = performance.now();
    L.rateWindow.push(now);
    while (L.rateWindow.length && L.rateWindow[0] < now - 2000) L.rateWindow.shift();
    if (f.source) renderSource(f.source);
    renderRec(f.rec ?? null);
    if (f.error) { $("#live-error").hidden = false; $("#live-error").textContent = f.error; return; }
    if (f.idle || !f.levels) return;
    $("#live-error").hidden = !(f.source && f.source.error && f.source.state !== "live");
    if (L.frozen) return;
    if (JSON.stringify(f.channels) !== JSON.stringify(L.channels)) setChannels(f.channels, f);
    const dt = L.lastT == null ? 1 / L.fps : Math.max(0, Math.min(0.5, f.t - L.lastT));
    if (L.lastT != null && f.t < L.lastT) resetHistories();  // source restarted
    L.lastT = f.t;
    L.frame = f;
    ingest(f, dt);
    if (!L.drawPending) {
      L.drawPending = true;
      requestAnimationFrame(() => { L.drawPending = false; draw(); });
    }
  }

  function resetHistories() {
    L.onsetHist = []; L.onsets = []; L.spec = null; L.specPeak = null; L.sg = null;
  }

  function setChannels(names, f) {
    L.channels = names;
    resetHistories();
    L.meters = names.map(() => ({ peak: -120, rms: -120, hold: -120, holdAt: 0, clipLatched: false, clips: 0 }));
    const m = $("#meters");
    m.replaceChildren(...names.map((n, c) => {
      const row = el("div", { className: "meter" });
      row.innerHTML = `<span class="m-name"></span>
        <div class="m-bar"><div class="m-rms"></div><div class="m-peak"></div><div class="m-hold"></div></div>
        <button type="button" class="m-clip" title="Clip light: tap to reset">CLIP</button>
        <span class="m-num"><b>—</b><small>—</small></span>`;
      $(".m-name", row).textContent = n;
      $(".m-name", row).style.color = chColor(c);
      $(".m-rms", row).style.background = chColor(c, 0.55);
      $(".m-peak", row).style.background = chColor(c);
      $(".m-clip", row).addEventListener("click", () => { L.meters[c].clipLatched = false; });
      return row;
    }), el("div", { className: "meter scale-row" }, el("span"),
      el("div", { className: "m-scale", innerHTML: [-60, -48, -36, -24, -12, -6, 0]
        .map((d) => `<span style="left:${dbPct(d)}%">${d}</span>`).join("") })));
    const sg = $("#sg-ch");
    sg.replaceChildren(...names.map((n, c) => el("option", { value: c, textContent: n })));
    $("#spec-legend").replaceChildren(...names.map((n, c) => {
      const s = el("span", { textContent: n });
      s.style.setProperty("--c", chColor(c));
      return s;
    }));
    $("#panel-response").hidden = names.length < 2;
    $("#tuner-ch").textContent = `on “${names[0]}”`;
  }

  // Meter scale: -60..0 dBFS, linear in dB.
  const dbPct = (d) => Math.max(0, Math.min(100, (d + 60) / 60 * 100));

  function ingest(f, dt) {
    // meters (ballistics: fast attack, slow fall, 1.5 s peak hold)
    const now = performance.now();
    f.levels.forEach((lv, c) => {
      const m = L.meters[c];
      if (!m) return;
      m.peak = Math.max(lv.peak_db, m.peak - 26 * dt);
      m.rms = lv.rms_db > m.rms ? lv.rms_db : m.rms + (lv.rms_db - m.rms) * Math.min(1, dt / 0.3);
      if (lv.peak_db >= m.hold || now - m.holdAt > 1500) { m.hold = lv.peak_db; m.holdAt = now; }
      if (lv.clip) m.clipLatched = true;
      m.clips = lv.clips;
      m.max10 = lv.max10_db;
      m.raw = lv;
    });

    // spectrum smoothing (release 40 dB/s) and peak hold (falls 8 dB/s)
    const sp = f.spectrum;
    const cur = sp.data.map((d) => {
      const b = b64bytes(d);
      const out = new Float32Array(b.length);
      for (let i = 0; i < b.length; i++) out[i] = b[i] / 2 + sp.floor;
      return out;
    });
    if (!L.spec || L.spec.length !== cur.length || L.spec[0].length !== cur[0].length) {
      L.spec = cur.map((a) => a.slice());
      L.specPeak = cur.map((a) => a.slice());
    } else {
      cur.forEach((a, c) => {
        const s = L.spec[c], p = L.specPeak[c];
        for (let i = 0; i < a.length; i++) {
          s[i] = a[i] > s[i] ? a[i] : Math.max(a[i], s[i] - 40 * dt);
          p[i] = a[i] > p[i] ? a[i] : Math.max(a[i], p[i] - 8 * dt);
        }
      });
    }
    L.cur = cur;
    pushSpectrogram(cur);

    // onset strip
    L.onsetHist.push({ t: f.t, db: f.levels[0].rms_db, pk: f.levels[0].peak_db });
    while (L.onsetHist.length && L.onsetHist[0].t < f.t - 8) L.onsetHist.shift();
    for (const o of f.onsets) L.onsets.push(o);
    while (L.onsets.length && L.onsets[0].t < f.t - 8) L.onsets.shift();

    // tuner smoothing
    if (f.pitch) {
      L.tunerCents += (f.pitch.cents - L.tunerCents) * Math.min(1, dt / 0.08);
      L.noteSeenAt = now;
    }
  }

  // ---------------------------------------------------------------- spectrogram

  const LUT = (() => {
    // Dark-to-bright perceptual-ish ramp (black, indigo, magenta, orange, pale yellow).
    const stops = [[0, [8, 8, 16]], [0.25, [52, 20, 104]], [0.5, [156, 36, 112]], [0.72, [236, 104, 46]], [1, [252, 238, 160]]];
    const lut = new Uint8ClampedArray(256 * 4);
    for (let i = 0; i < 256; i++) {
      const x = i / 255;
      let k = 0;
      while (k < stops.length - 2 && x > stops[k + 1][0]) k++;
      const [x0, c0] = stops[k], [x1, c1] = stops[k + 1];
      const u = (x - x0) / (x1 - x0);
      for (let j = 0; j < 3; j++) lut[i * 4 + j] = c0[j] + (c1[j] - c0[j]) * u;
      lut[i * 4 + 3] = 255;
    }
    return lut;
  })();

  function pushSpectrogram(cur) {
    const cols = Math.round(20 * L.fps);
    const bins = cur[0].length;
    if (!L.sg || L.sg.width !== cols || L.sg.height !== bins) {
      L.sg = el("canvas", { width: cols, height: bins });
      const g = L.sg.getContext("2d");
      g.fillStyle = "rgb(8,8,16)";
      g.fillRect(0, 0, cols, bins);
      L.sgCol = g.createImageData(1, bins);
    }
    const g = L.sg.getContext("2d");
    g.drawImage(L.sg, -1, 0);
    const ch = Math.min(Number($("#sg-ch").value) || 0, cur.length - 1);
    const d = cur[ch], px = L.sgCol.data;
    for (let i = 0; i < bins; i++) {
      const v = Math.max(0, Math.min(255, Math.round((d[i] + 100) / 90 * 255)));  // -100..-10 dBFS
      const row = bins - 1 - i;  // low frequencies at the bottom
      px.set(LUT.subarray(v * 4, v * 4 + 4), row * 4);
    }
    g.putImageData(L.sgCol, cols - 1, 0);
  }

  // ---------------------------------------------------------------- drawing

  function draw() {
    const f = L.frame;
    if (!f) return;
    // Only panels on screen are drawn; one failing panel must not blank the rest.
    const panels = { levels: drawMeters, tuner: drawTuner, scope: drawScope, spectrum: drawSpectrum,
      spectrogram: drawSpectrogram, harmony: drawHarmony, harmonics: drawHarmonics,
      dynamics: drawDynamics, response: (fr) => fr.response && drawResponse(fr) };
    for (const [k, fn] of Object.entries(panels)) {
      if (!L.visible.has(k)) continue;
      try { fn(f); } catch (e) {
        if (!L.drawErr?.[k]) console.error(`live panel ${k}:`, e);
        (L.drawErr ??= {})[k] = true;
      }
    }
    const n = L.rateWindow.length;
    $("#live-lag").textContent = n > 1 ? `${Math.round((n - 1) / ((L.rateWindow[n - 1] - L.rateWindow[0]) / 1000))} frames/s` : "";
  }

  function drawMeters() {
    const rows = document.querySelectorAll("#meters .meter");
    let worst = null;
    L.meters.forEach((m, c) => {
      const row = rows[c];
      if (!row) return;
      $(".m-rms", row).style.width = `${dbPct(m.rms)}%`;
      $(".m-peak", row).style.width = `${dbPct(m.peak)}%`;
      $(".m-hold", row).style.left = `${dbPct(m.hold)}%`;
      $(".m-hold", row).classList.toggle("hot", m.hold > -3);
      $(".m-clip", row).classList.toggle("on", m.clipLatched);
      $(".m-num b", row).textContent = dbStr(m.raw?.peak_db);
      $(".m-num small", row).textContent = `rms ${dbStr(m.raw?.rms_db)}`;
      if (m.max10 != null && (worst == null || m.max10 > worst.v)) worst = { v: m.max10, c };
    });
    if (worst) {
      const name = L.channels[worst.c];
      const clips = L.meters.reduce((a, m) => a + (m.clips || 0), 0);
      $("#headroom-cap").textContent = worst.v <= -100 ? "Silence." :
        `Loudest in the last 10 s: ${dbStr(worst.v)} dBFS on “${name}” (${worst.v >= -0.1 ? "no" : (-worst.v).toFixed(1) + " dB of"} headroom).` +
        (clips ? ` Clipped ${clips}× since the page opened.` : " Aim for peaks around −12 to −6.");
    }
  }

  function drawTuner(f) {
    const p = f.pitch;
    const fresh = performance.now() - L.noteSeenAt < 800;
    const box = $(".tuner");
    box.classList.toggle("stale", !p);
    if (p) {
      $("#tuner-name").textContent = p.note;
      $("#tuner-oct").textContent = p.octave;
      $("#tuner-cents").textContent = `${p.cents > 0 ? "+" : p.cents < 0 ? "−" : "±"}${Math.abs(p.cents).toFixed(0)} cents`;
      $("#tuner-hz").textContent = `${p.hz.toFixed(2)} Hz`;
      $("#tuner-conf").textContent = p.conf >= 0.9 ? "steady" : "unsteady";
    } else if (!fresh) {
      $("#tuner-name").textContent = "–";
      $("#tuner-oct").textContent = "";
      $("#tuner-cents").textContent = "play one string";
      $("#tuner-hz").textContent = "— Hz";
      $("#tuner-conf").textContent = "";
    }
    const c = Math.max(-50, Math.min(50, L.tunerCents));
    const needle = $("#tuner-needle");
    needle.style.left = `${50 + c}%`;
    needle.classList.toggle("in", !!p && Math.abs(p.cents) < 3);
  }

  function drawScope(f) {
    const { ctx, w, h } = fit($("#cv-scope"));
    const s = f.scope;
    ctx.strokeStyle = C.line;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
    for (let q = 1; q < 4; q++) {
      ctx.globalAlpha = 0.5;
      ctx.beginPath(); ctx.moveTo(w * q / 4, 0); ctx.lineTo(w * q / 4, h); ctx.stroke();
    }
    ctx.globalAlpha = 1;
    s.data.forEach((d, c) => {
      const v = new Int8Array(b64bytes(d).buffer);
      ctx.strokeStyle = chColor(c, c ? 0.8 : 1);
      ctx.lineWidth = c ? 1.2 : 1.6;
      ctx.beginPath();
      for (let i = 0; i < v.length; i++) {
        const x = i / (v.length - 1) * w, y = h / 2 - v[i] / 127 * h * 0.45;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.stroke();
    });
    ctx.fillStyle = C.muted;
    const peakDb = 20 * Math.log10(Math.max(s.scale, 1e-6));
    ctx.fillText(`zoomed to peak ${dbStr(peakDb)} dBFS · ${s.ms.toFixed(0)} ms wide${s.triggered ? "" : " · free-running"}`, 6, 14);
  }

  function freqX(fr, fmin, fmax, w) { return Math.log(fr / fmin) / Math.log(fmax / fmin) * w; }

  function freqGrid(ctx, w, h, fmin, fmax, bottomPad) {
    ctx.strokeStyle = C.line; ctx.fillStyle = C.muted; ctx.lineWidth = 1;
    for (const fr of [50, 100, 200, 500, 1000, 2000, 5000, 10000]) {
      if (fr < fmin || fr > fmax) continue;
      const x = freqX(fr, fmin, fmax, w);
      ctx.globalAlpha = 0.6;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h - bottomPad); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillText(fmtHz(fr), x + 2, h - 3);
    }
  }

  function drawSpectrum(f) {
    const { ctx, w, h } = fit($("#cv-spectrum"));
    const sp = f.spectrum, bins = sp.bins, pad = 14;
    const lo = -100, hi = 0, ph = h - pad;
    const y = (d) => (hi - Math.max(lo, Math.min(hi, d))) / (hi - lo) * ph;
    const binX = (i) => (i + 0.5) / bins * w;
    freqGrid(ctx, w, h, sp.fmin, sp.fmax, pad);
    ctx.fillStyle = C.muted;
    for (let d = -80; d <= -20; d += 20) {
      ctx.globalAlpha = 0.35; ctx.strokeStyle = C.line;
      ctx.beginPath(); ctx.moveTo(0, y(d)); ctx.lineTo(w, y(d)); ctx.stroke();
      ctx.globalAlpha = 1; ctx.fillText(`${d}`, w - 24, y(d) - 2);
    }
    L.spec.forEach((s, c) => {
      ctx.beginPath();
      for (let i = 0; i < bins; i++) i ? ctx.lineTo(binX(i), y(s[i])) : ctx.moveTo(binX(i), y(s[i]));
      if (c === 0) {
        ctx.lineTo(w, ph); ctx.lineTo(0, ph); ctx.closePath();
        ctx.fillStyle = chColor(0, 0.22); ctx.fill();
        ctx.beginPath();
        for (let i = 0; i < bins; i++) i ? ctx.lineTo(binX(i), y(s[i])) : ctx.moveTo(binX(i), y(s[i]));
      }
      ctx.strokeStyle = chColor(c); ctx.lineWidth = c ? 1.2 : 1.5; ctx.stroke();
      // peak hold, dotted
      const p = L.specPeak[c];
      ctx.setLineDash([2, 3]); ctx.lineWidth = 1; ctx.strokeStyle = chColor(c, 0.6);
      ctx.beginPath();
      for (let i = 0; i < bins; i++) i ? ctx.lineTo(binX(i), y(p[i])) : ctx.moveTo(binX(i), y(p[i]));
      ctx.stroke(); ctx.setLineDash([]);
    });
    // Label the three strongest peaks of channel 0 with frequency and note.
    const s = L.cur[0];
    const cand = [];
    for (let i = 2; i < bins - 2; i++) {
      if (s[i] > -75 && s[i] >= s[i - 1] && s[i] >= s[i + 1] && s[i] >= s[i - 2] && s[i] >= s[i + 2]) cand.push(i);
    }
    cand.sort((a, b) => s[b] - s[a]);
    const picked = [];
    for (const i of cand) {
      if (picked.every((j) => Math.abs(binX(j) - binX(i)) > 70)) picked.push(i);  // labels mustn't collide
      if (picked.length === 3) break;
    }
    ctx.fillStyle = C.text;
    for (const i of picked) {
      const fr = sp.fmin * Math.pow(sp.fmax / sp.fmin, (i + 0.5) / bins);
      const label = `${fmtHz(fr)} ${noteOf(fr)}`;
      const x = Math.min(w - ctx.measureText(label).width - 2, Math.max(2, binX(i) - 14));
      ctx.fillText(label, x, Math.max(11, y(s[i]) - 5));
    }
  }

  function drawSpectrogram(f) {
    const { ctx, w, h } = fit($("#cv-spectrogram"));
    if (!L.sg) return;
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(L.sg, 0, 0, w, h);
    const sp = f.spectrum;
    ctx.fillStyle = "rgba(255,255,255,.75)";
    for (const fr of [100, 200, 500, 1000, 2000, 5000, 10000]) {
      const yy = h - Math.log(fr / sp.fmin) / Math.log(sp.fmax / sp.fmin) * h;
      ctx.fillRect(0, yy, 5, 1);
      ctx.fillText(fmtHz(fr), 7, yy + 4);
    }
    ctx.fillText("now", w - 24, h - 4);
    ctx.fillText("−20 s", w - 24 - (w - 30), h - 4);
  }

  function drawHarmony(f) {
    const ch = f.chroma;
    $("#chord-name").textContent = f.chord ? f.chord.name : (f.pitch ? f.pitch.note : "–");
    $("#chord-score").textContent = f.chord ? `match ${Math.round(f.chord.score * 100)}%` : f.pitch ? "single note" : "";
    const { ctx, w, h } = fit($("#cv-chroma"));
    const bw = w / 12, pad = 14;
    for (let i = 0; i < 12; i++) {
      const v = ch[i];
      const bh = v * (h - pad - 2);
      ctx.fillStyle = v > 0.6 ? chColor(0) : chColor(0, 0.35 + 0.4 * v);
      ctx.fillRect(i * bw + 2, h - pad - bh, bw - 4, bh);
      ctx.fillStyle = v > 0.6 ? C.text : C.muted;
      const t = NOTE_NAMES[i];
      ctx.fillText(t, i * bw + bw / 2 - ctx.measureText(t).width / 2, h - 2);
    }
  }

  function drawHarmonics(f) {
    const { ctx, w, h } = fit($("#cv-harmonics"));
    const hs = f.harmonics;
    const pad = 14, n = 10, bw = w / n;
    ctx.strokeStyle = C.line; ctx.fillStyle = C.muted;
    for (const d of [-20, -40]) {
      const yy = (-d / 60) * (h - pad);
      ctx.globalAlpha = 0.4; ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(w, yy); ctx.stroke();
      ctx.globalAlpha = 1; ctx.fillText(`${d} dB`, w - 38, yy - 2);
    }
    if (!hs || !hs.length) {
      $("#harm-cap").textContent = "Play a single note and let it ring.";
      for (let k = 0; k < n; k++) ctx.fillText(String(k + 1), k * bw + bw / 2 - 3, h - 2);
      return;
    }
    hs.forEach((d, k) => {
      const v = Math.max(0, (d + 60) / 60);
      const bh = Math.min(1, v) * (h - pad);
      ctx.fillStyle = k === 0 ? chColor(0) : chColor(0, 0.6);
      ctx.fillRect(k * bw + 3, h - pad - bh, bw - 6, bh);
    });
    ctx.fillStyle = C.muted;
    for (let k = 0; k < n; k++) ctx.fillText(String(k + 1), k * bw + bw / 2 - 3, h - 2);
    const f0 = f.pitch.hz;
    const strongest = hs.slice(1).reduce((b, d, i) => (d > b.d ? { d, k: i + 2 } : b), { d: -999, k: 0 });
    $("#harm-cap").textContent = `Fundamental ${f0.toFixed(1)} Hz (${f.pitch.note}${f.pitch.octave}). ` +
      (strongest.k ? `Strongest overtone: #${strongest.k} at ${dbStr(strongest.d)} dB${strongest.d > 0 ? ", louder than the fundamental (common on piezos)" : ""}.` : "");
  }

  function drawDynamics(f) {
    const { ctx, w, h } = fit($("#cv-onsets"));
    const t1 = f.t, t0 = t1 - 8, lo = -70, hi = 0;
    const x = (t) => (t - t0) / 8 * w, y = (d) => (hi - Math.max(lo, Math.min(hi, d))) / (hi - lo) * h;
    const ft = f.features;
    if (ft.tempo_bpm && L.onsets.length) {  // faint beat grid through the latest attack
      const beat = 60 / ft.tempo_bpm, last = L.onsets[L.onsets.length - 1].t;
      ctx.strokeStyle = C.line; ctx.globalAlpha = 0.5;
      for (let t = last; t > t0; t -= beat) { ctx.beginPath(); ctx.moveTo(x(t), 0); ctx.lineTo(x(t), h); ctx.stroke(); }
      ctx.globalAlpha = 1;
    }
    if (ft.noise_floor_db != null) {
      ctx.strokeStyle = C.muted; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(0, y(ft.noise_floor_db)); ctx.lineTo(w, y(ft.noise_floor_db)); ctx.stroke();
      ctx.setLineDash([]);
    }
    const hist = L.onsetHist;
    if (hist.length > 1) {
      ctx.beginPath(); ctx.moveTo(x(hist[0].t), h);
      for (const p of hist) ctx.lineTo(x(p.t), y(p.pk));
      ctx.lineTo(x(hist[hist.length - 1].t), h); ctx.closePath();
      ctx.fillStyle = chColor(0, 0.18); ctx.fill();
      ctx.beginPath();
      hist.forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.db)) : ctx.moveTo(x(p.t), y(p.db))));
      ctx.strokeStyle = chColor(0); ctx.lineWidth = 1.5; ctx.stroke();
    }
    ctx.fillStyle = C.text;
    for (const o of L.onsets) ctx.fillRect(x(o.t) - 1, 0, 2, Math.min(h * 0.35, 6 + o.strength / 4));
    ctx.fillStyle = C.muted;
    ctx.fillText("−8 s", 2, h - 3);
    ctx.fillText("now", w - 22, h - 3);

    const tile = (k, text, level = "") => {
      const t = $(`#feats .tile[data-k="${k}"]`);
      $("dd", t).textContent = text;
      t.classList.toggle("warn", level === "warn");
    };
    tile("tempo", ft.tempo_bpm ? `${Math.round(ft.tempo_bpm)} BPM` : "—");
    tile("attacks", String(ft.onsets_8s));
    tile("bright", ft.centroid_hz ? `${fmtHz(ft.centroid_hz)}Hz` : "—");
    tile("crest", ft.crest_db != null ? `${ft.crest_db.toFixed(0)} dB` : "—");
    tile("floor", ft.noise_floor_db != null ? `${ft.noise_floor_db.toFixed(0)} dB` : "—", ft.noise_floor_db > -50 ? "warn" : "");
    tile("hum", ft.hum_db != null ? `${ft.hum_db.toFixed(0)} dB` : "—", ft.hum_prominent ? "warn" : "");
    $('#feats .tile[data-k="hum"]').title = ft.hum_prominent
      ? `${ft.mains_hz} Hz mains hum stands out above the noise. Check cables and grounding.` : "No obvious mains hum.";
  }

  function drawResponse(f) {
    const r = f.response;
    const { ctx, w, h } = fit($("#cv-response"));
    const fmin = r.freqs[0], fmax = r.freqs[r.freqs.length - 1], pad = 14, ph = h - pad;
    const vals = r.mag_db.filter((v) => v != null);
    const span = vals.length ? [12, 24, 48].find((s) => vals.every((v) => Math.abs(v) <= s * 0.9)) ?? 60 : 12;
    const y = (d) => ph / 2 - Math.max(-span, Math.min(span, d)) / span * ph / 2;
    freqGrid(ctx, w, h, fmin, fmax, pad);
    ctx.fillStyle = C.muted; ctx.strokeStyle = C.line;
    for (const d of [-span / 2, 0, span / 2]) {
      ctx.globalAlpha = d === 0 ? 1 : 0.35;
      ctx.beginPath(); ctx.moveTo(0, y(d)); ctx.lineTo(w, y(d)); ctx.stroke();
      ctx.globalAlpha = 1; ctx.fillText(`${d > 0 ? "+" : ""}${d} dB`, w - 40, y(d) - 2);
    }
    // Segments fade out where coherence is low: there the ratio isn't trustworthy.
    ctx.lineWidth = 2;
    for (let i = 1; i < r.freqs.length; i++) {
      const a = r.mag_db[i - 1], b = r.mag_db[i];
      if (a == null || b == null) continue;
      const coh = Math.min(r.coherence[i - 1], r.coherence[i]);
      ctx.strokeStyle = chColor(1, 0.15 + 0.85 * coh * coh);
      ctx.beginPath();
      ctx.moveTo(freqX(r.freqs[i - 1], fmin, fmax, w), y(a));
      ctx.lineTo(freqX(r.freqs[i], fmin, fmax, w), y(b));
      ctx.stroke();
    }
    const [a, b] = r.names;
    $("#resp-cap").textContent = `“${b}” ÷ “${a}”: what happens between the two`;
    const coh = r.coherence.reduce((s, v) => s + v, 0) / r.coherence.length;
    const delay = r.delay_sharpness >= 8 ? `${r.delay_ms >= 0 ? "" : "−"}${Math.abs(r.delay_ms).toFixed(2)} ms` : "unclear";
    $("#resp-read").textContent = `Delay ${b} after ${a}: ${delay}. Average coherence ${coh.toFixed(2)} ` +
      `(1 = “${b}” is fully explained by “${a}”; echoes, reverb and noise pull it down). Faint parts of the curve are unreliable.`;
  }

  // ---------------------------------------------------------------- source + recording

  function setState(state, text) {
    const s = $("#live-state");
    s.dataset.state = state;
    $("span", s).textContent = text;
  }

  function renderSource(src) {
    L.status = src;
    const ch = (src.channels || []).join(", ");
    const dropped = src.dropped ? ` · ${(src.dropped / (src.sample_rate || 48000)).toFixed(2)} s lost` : "";
    if (src.state === "live") setState("live", `live · ${(src.sample_rate / 1000).toFixed(0)} kHz · ${ch}${dropped}`);
    else if (src.state === "starting") setState("waiting", "starting…");
    else setState(src.state === "error" ? "error" : "waiting", src.error || src.state);
    if (src.error && src.state !== "live") { $("#live-error").hidden = false; $("#live-error").textContent = src.error; }
    if (src.retro_max_s) {
      $("#retro-max").textContent = Math.round(src.retro_max_s);
      for (const o of $("#retro-secs").options) o.disabled = Number(o.value) > src.retro_max_s;
    }
  }

  function renderRec(rec) {
    const was = !!L.rec;
    L.rec = rec;
    L.recAt = performance.now();
    if (typeof renderRecPill === "function") renderRecPill(rec);  // app.js's topbar pill
    const btn = $("#rec-btn");
    btn.classList.toggle("on", !!rec);
    btn.setAttribute("aria-pressed", String(!!rec));
    $(".rec-label", btn).textContent = rec ? "Stop" : "Record";
    $("#mark-btn").disabled = !rec;
    $("#live-source").disabled = !!rec;
    $("#rec-card").classList.toggle("recording", !!rec);
    if (rec) {
      const pk = rec.peak_dbfs.map(dbStr).join(" / ");
      $("#rec-sub").textContent = `${fmtBytes(rec.bytes)} · peak ${pk} dBFS` +
        (rec.markers.length ? ` · ${rec.markers.length} marker${rec.markers.length > 1 ? "s" : ""}` : "") +
        (rec.backlog_s > 0.5 ? ` · disk behind by ${rec.backlog_s.toFixed(1)} s` : "") +
        (rec.error ? ` · ${rec.error}` : "");
      $("#rec-time").textContent = fmtTime(rec.elapsed_s);
    } else {
      if (was) loadStatus();  // it stopped (maybe automatically: disk full)
      $("#rec-time").textContent = "0:00.0";
      renderDisk();
    }
  }

  function tickRecTimer() {
    if (L.rec && L.view === "live") {
      const t = L.rec.elapsed_s + (performance.now() - L.recAt) / 1000;
      $("#rec-time").textContent = fmtTime(Math.min(t, L.rec.elapsed_s + 1));
    }
    requestAnimationFrame(tickRecTimer);
  }

  function renderDisk() {
    const st = L.statusAll;
    if (!st || L.rec) return;
    const d = st.disk;
    const mins = d.free_minutes;
    $("#rec-sub").textContent = `${fmtBytes(d.free_bytes)} free on the card ≈ ${mins >= 120 ? Math.floor(mins / 60) + " h" : mins + " min"} of recording`;
  }

  async function loadStatus() {
    try {
      L.statusAll = await api("GET", "/api/live");
      renderSource(L.statusAll.source);
      renderRec(L.statusAll.recording);
      renderDisk();
    } catch (e) { setState("error", e.message); }
  }

  function specKey(s) {
    return JSON.stringify([s.kind, s.device ?? null]);
  }

  async function loadSources() {
    const r = await api("GET", "/api/live/sources");
    L.sources = r.sources;
    const sel = $("#live-source");
    const cur = r.current.spec || {};
    sel.replaceChildren(...r.sources.map((s, i) => {
      const o = el("option", { value: i, textContent: s.label + (s.kind === "tap" && !s.present ? " (engine not running)" : "") });
      if (specKey(s) === specKey(cur)) o.selected = true;
      return o;
    }));
  }

  async function switchSource() {
    const s = L.sources[Number($("#live-source").value)];
    if (!s) return;
    const spec = { kind: s.kind };
    if (s.device) spec.device = s.device;
    if (s.channels) spec.channels = s.channels;
    if (s.label && s.kind === "alsa") spec.label = s.label;
    if (s.kind === "tap" && s.path) spec.path = s.path;
    await api("POST", "/api/live/source", spec);
    L.channels = [];
    L.frame = null;
    toast(`Listening to ${s.label}`);
    await loadStatus();
  }

  async function toggleRecord() {
    if (L.rec) {
      const m = await api("POST", "/api/live/record/stop");
      renderRec(null);
      toast(`Saved “${m.name}” (${fmtTime(m.duration_s, false)})${m.dropped_frames ? " — some audio was lost, see Takes" : ""}`);
      $("#take-name").value = "";
      $("#take-notes").value = "";
    } else {
      const body = {};
      if ($("#take-name").value.trim()) body.name = $("#take-name").value.trim();
      if ($("#take-notes").value.trim()) body.notes = $("#take-notes").value.trim();
      renderRec(await api("POST", "/api/live/record/start", body));
    }
    loadStatus();
  }

  // ---------------------------------------------------------------- takes

  async function renderTakes() {
    const r = await api("GET", "/api/recordings");
    const ul = $("#take-list");
    ul.replaceChildren();
    $("#takes-disk").textContent = `${fmtBytes(r.disk.free_bytes)} free`;
    if (!r.recordings.length) {
      ul.append(el("li", { className: "empty", textContent: "No takes yet. Record one on the Live page." }));
      return;
    }
    for (const m of r.recordings) ul.append(takeItem(m));
    requestAnimationFrame(() => document.querySelectorAll(".take").forEach((li) => drawWave(li)));
  }

  function takeItem(m) {
    const li = $("#tpl-take").content.firstElementChild.cloneNode(true);
    li._meta = m;
    const name = $(".take-title", li);
    name.value = m.name;
    const kind = $(".kind", li);
    if (m.kind === "retro") { kind.hidden = false; kind.textContent = "saved after"; }
    if (m.status === "recording") { kind.hidden = false; kind.textContent = "recording…"; kind.classList.add("warn"); }
    const created = new Date(m.created);
    const parts = [
      created.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
      m.duration_s != null ? fmtTime(m.duration_s, false) : "…",
      (m.channels || []).join(" + "),
    ];
    if (m.preset?.name) parts.push(`preset ${m.preset.name}${m.preset.modified ? " (edited)" : ""}`);
    if (m.peak_dbfs) parts.push(`peak ${m.peak_dbfs.map(dbStr).join(" / ")} dBFS`);
    if (m.size_bytes) parts.push(fmtBytes(m.size_bytes));
    $(".take-meta", li).textContent = parts.join(" · ");
    const warn = [];
    (m.clipped_samples || []).forEach((n, c) => { if (n) warn.push(`clipped ${n.toLocaleString()} samples on “${m.channels[c]}”`); });
    if (m.dropped_frames) warn.push(`${(m.dropped_frames / m.sample_rate).toFixed(2)} s of audio lost (the box fell behind)`);
    if (m.status === "interrupted") warn.push("cut short by a crash or power loss; the audio up to then is intact");
    if (m.stop_reason) warn.push(`stopped: ${m.stop_reason}`);
    if (m.error) warn.push(m.error);
    if (m.markers?.length) warn.push(`markers at ${m.markers.map((t) => fmtTime(t)).join(", ")}`);
    $(".take-warn", li).textContent = warn.join(" · ");
    $(".take-warn", li).hidden = !warn.length;

    const sel = $(".take-ch", li), audio = $("audio", li);
    (m.channels || []).forEach((n, c) => sel.append(el("option", { value: c, textContent: `▶ ${n}` })));
    const setSrc = () => { audio.src = `/api/recordings/${encodeURIComponent(m.id)}/audio?ch=${sel.value}`; };
    sel.addEventListener("change", () => { const play = !audio.paused; setSrc(); if (play) audio.play(); });
    if (m.status !== "recording") setSrc(); else $(".take-play", li).hidden = true;
    audio.addEventListener("timeupdate", () => drawWave(li, audio.currentTime));
    $(".take-dl", li).href = `/api/recordings/${encodeURIComponent(m.id)}/audio`;
    const notes = $(".take-notes", li);
    notes.value = m.notes || "";
    const save = (body) => api("PATCH", `/api/recordings/${encodeURIComponent(m.id)}`, body)
      .then(() => toast("Saved")).catch((e) => toast(e.message, true));
    name.addEventListener("change", () => name.value.trim() && save({ name: name.value.trim() }));
    notes.addEventListener("change", () => save({ notes: notes.value }));
    $(".take-del", li).addEventListener("click", (e) => busy(e.currentTarget, async () => {
      if (!confirm(`Delete “${m.name}”? The WAV file is removed from the box.`)) return;
      await api("DELETE", `/api/recordings/${encodeURIComponent(m.id)}`);
      toast("Deleted");
      renderTakes();
    }));
    return li;
  }

  function drawWave(li, playhead = null) {
    const m = li._meta, ov = m.overview || [];
    const { ctx, w, h } = fit($(".take-wave", li));
    if (!ov.length) return;
    const lanes = ov.length, lh = h / lanes;
    ov.forEach((row, c) => {
      const mid = lh * c + lh / 2;
      ctx.fillStyle = chColor(c, 0.8);
      const bw = w / row.length;
      row.forEach((v, i) => {
        const a = Math.max(1, Math.pow(v, 0.5) * lh * 0.46);  // sqrt so quiet parts stay visible
        ctx.fillRect(i * bw, mid - a, Math.max(1, bw - 0.5), 2 * a);
      });
      ctx.fillStyle = C.muted;
      ctx.fillText(m.channels?.[c] ?? "", 3, lh * c + 11);
    });
    if (m.duration_s) {
      ctx.fillStyle = C.warn;
      for (const t of m.markers || []) ctx.fillRect(t / m.duration_s * w - 1, 0, 2, h);
      if (playhead != null) { ctx.fillStyle = C.text; ctx.fillRect(playhead / m.duration_s * w, 0, 1.5, h); }
    }
  }

  // ---------------------------------------------------------------- wiring

  function onView(view) {
    L.view = view;
    if (view === "live") {
      loadSources().catch((e) => toast(e.message, true));
      loadStatus();
      requestAnimationFrame(draw);
    }
    if (view === "takes") renderTakes().catch((e) => toast(e.message, true));
    syncStream();
  }

  function init() {
    readColors();
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readColors(); draw(); });
    document.addEventListener("fx:view", (e) => onView(e.detail));
    document.addEventListener("visibilitychange", syncStream);
    const io = new IntersectionObserver((entries) => {
      for (const en of entries) {
        const k = en.target.dataset.panel;
        en.isIntersecting ? L.visible.add(k) : L.visible.delete(k);
      }
    });
    document.querySelectorAll(".panel").forEach((p) => io.observe(p));
    window.addEventListener("resize", () => requestAnimationFrame(draw));

    $("#rec-btn").addEventListener("click", (e) => busy(e.currentTarget, toggleRecord));
    $("#mark-btn").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const r = await api("POST", "/api/live/record/mark");
      toast(`Marker at ${fmtTime(r.t)}`);
    }));
    $("#retro-btn").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const body = { seconds: Number($("#retro-secs").value) };
      if ($("#take-name").value.trim()) body.name = $("#take-name").value.trim();
      if ($("#take-notes").value.trim()) body.notes = $("#take-notes").value.trim();
      const m = await api("POST", "/api/live/record/retro", body);
      toast(`Saved the last ${fmtTime(m.duration_s, false)} as “${m.name}”`);
      loadStatus();
    }));
    $("#live-source").addEventListener("change", (e) => busy(e.currentTarget, switchSource));
    $("#freeze-btn").addEventListener("click", (e) => {
      L.frozen = !L.frozen;
      e.currentTarget.setAttribute("aria-pressed", String(L.frozen));
      e.currentTarget.textContent = L.frozen ? "Frozen — tap to resume" : "Freeze";
      e.currentTarget.classList.toggle("primary", L.frozen);
    });
    $("#live-fps").addEventListener("change", (e) => {
      L.fps = Number(e.target.value);
      L.sg = null;
      restartStream();
    });
    $("#sg-ch").addEventListener("change", () => { L.sg = null; });
    setInterval(() => { if (L.view === "live" && !L.rec) loadStatus(); }, 10000);
    requestAnimationFrame(tickRecTimer);
  }

  init();
})();
