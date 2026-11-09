/* Bambu X2D OBS overlay — renders server state (SSE) into one of several layouts. */
(() => {
  'use strict';

  const stage = document.getElementById('stage');
  const DEFAULTS = { layout: 'glass', accent: 'auto', position: 'auto', side: 'left', scale: 1, idle: 'show',
    name: 'file', ams: true, credit: false, byline: false, build: true, meter: true, viewers: true, demo: false,
    socialTwitch: true, socialTiktok: false, socialX: false, socialInstagram: false, socialYoutube: false,
    tiktokHandle: '', xHandle: '', instagramHandle: '', youtubeHandle: '',
    musicCredit: false, musicLabel: 'Music', musicText: '', musicInput: '',
    socialEvery: 120 };
  const DEFAULT_POS = { glass: 'bl', orb: 'tl', minimal: 'br' };
  const FALLBACK_ACCENT = '#4fe3c1';

  // URL parameters override server-side settings, e.g. /?layout=hud&accent=ff8800&scale=1.2
  const params = new URLSearchParams(location.search);
  const overrides = {};
  for (const [k, def] of Object.entries(DEFAULTS)) {
    if (!params.has(k)) continue;
    const v = params.get(k);
    if (typeof def === 'number') overrides[k] = parseFloat(v) || def;
    else if (typeof def === 'boolean' && k !== 'demo') overrides[k] = !/^(0|false|no|off)$/i.test(v);
    else overrides[k] = v;
  }
  if (overrides.accent && /^[0-9a-f]{6}$/i.test(overrides.accent)) overrides.accent = '#' + overrides.accent;

  let view = null;
  let serverDownSince = Date.now();
  // lite: static thumbnails (control-panel gallery): no animations, no meter loop, re-render every 5 s
  const LITE = params.has('lite');
  if (LITE) stage.classList.add('lite');
  let assetsSeen = null;
  function checkAssets(v) {   // web files changed on the server -> reload (OBS browser sources cache otherwise)
    if (!v?.assets) return;
    if (assetsSeen && v.assets !== assetsSeen) location.reload();
    assetsSeen = v.assets;
  }
  let mounted = null;

  // ------------------------------------------------------------------ connection
  function connect() {
    const es = new EventSource('/api/events');
    es.onmessage = (e) => { view = JSON.parse(e.data); serverDownSince = 0; checkAssets(view); render(); };
    es.addEventListener('meter', (e) => onMeter(JSON.parse(e.data)));
    es.onerror = () => { if (!serverDownSince) serverDownSince = Date.now(); };
  }

  // ------------------------------------------------------------------ helpers
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec));
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
    return m ? `${m}m` : '<1m';
  }
  const pad = (n) => String(n).padStart(2, '0');
  const fmtClockDur = (sec) => `${pad(Math.floor(sec / 3600))}:${pad(Math.floor(sec % 3600 / 60))}`;
  const fmtClockDurSec = (sec) => `${fmtClockDur(sec)}:${pad(Math.floor(sec % 60))}`;
  const fmtCount = (n) => (n < 1000 ? String(n) : n < 10000 ? `${(n / 1000).toFixed(1).replace(/\.0$/, '')}K` : `${Math.round(n / 1000)}K`);
  // No wall-clock times anywhere: viewers are in other time zones, durations read the same for everyone.

  function hexToHsl(hex) {
    const n = parseInt(hex.slice(1, 7), 16);
    const r = (n >> 16 & 255) / 255, g = (n >> 8 & 255) / 255, b = (n & 255) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
    if (max === min) return [0, 0, l];
    const d = max - min, s = l > .5 ? d / (2 - max - min) : d / (max + min);
    const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [h * 60, s, l];
  }
  function hslToHex(h, s, l) {
    const f = (n) => {
      const k = (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
      return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1)))).toString(16).padStart(2, '0');
    };
    return `#${f(0)}${f(8)}${f(4)}`;
  }
  // Turn a filament colour into a readable accent: keep the hue, force it bright enough for a dark overlay.
  function accentFrom(hex) {
    if (!hex || !/^#[0-9a-f]{6}$/i.test(hex)) return null;
    const [h, s, l] = hexToHsl(hex);
    if (s < 0.18 || l > 0.9 || l < 0.06) return null; // white / black / grey filament -> default accent
    return hslToHex(h, Math.max(s, 0.62), clamp(l, 0.58, 0.68));
  }

  function settings() {
    return { ...DEFAULTS, ...(view?.settings || {}), ...overrides };
  }
  function demoMode(s, real) {
    const d = s.demo;
    if (d === true || /^(1|true|on|yes)$/i.test(String(d))) return true;
    if (String(d) === 'auto') return !real || !real.online || !real.job?.hasJob;
    return false;
  }

  // ------------------------------------------------------------------ demo data
  const DEMO_COVER = 'data:image/svg+xml,' + encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"><g stroke="#3a2400" stroke-width="1.5" stroke-linejoin="round">
    <path d="M100 38 160 72 100 106 40 72Z" fill="#b37a1f"/><path d="M40 72 100 106 100 168 40 134Z" fill="#7c4b00"/>
    <path d="M160 72 100 106 100 168 160 134Z" fill="#5d3800"/><path d="M100 58 132 76 100 94 68 76Z" fill="#8f5a0c"/></g></svg>`);

  function demoView(real) {
    const t = Date.now() / 1000, cycle = 300, ph = (t % cycle) / cycle;
    let state = 'RUNNING', stage = null, step = null, progress;
    const prepSteps = ['Heatbed preheating', 'Homing toolhead', 'Auto bed leveling', 'Heating hotend', 'Cleaning nozzle tip'];
    if (ph < 0.1) { state = 'PREPARE'; step = Math.floor(ph / 0.02); stage = prepSteps[step]; progress = 0; }
    else if (ph > 0.93) { state = 'FINISH'; progress = 100; }
    else progress = Math.round((ph - 0.1) / 0.83 * 100);
    const total = 326, remaining = state === 'RUNNING' ? Math.round((100 - progress) * 2.39) : 0;
    const nozL = state === 'PREPARE' ? Math.round(140 + (ph / 0.1) * 80) : (state === 'FINISH' ? 96 : 220);
    const now = Date.now() / 1000;
    const tray = (slot, name, color, remain, active = false, type = 'PLA') =>
      ({ slot, type, name, color, alpha: 1, colors: [], remain, empty: false, active });
    return {
      v: 0, now, online: true, hasData: true, settings: real?.settings || {},
      twitch: real?.twitch || null,   // real viewer numbers only: demo mode can be on stream
      printer: { ip: 'demo', serialTail: '', wifi: '-52dBm', status: 'demo', media: 'demo' },
      job: {
        state, label: { PREPARE: 'Preparing', RUNNING: 'Printing', FINISH: 'Print complete' }[state], stage,
        step: step != null ? step + 1 : null, steps: step != null ? prepSteps.length : null,
        hasJob: true, active: state !== 'FINISH', task: 'Double_Stroller(2)_1', fileName: 'Double Stroller(2) 1',
        title: 'Baby Stroller v1', designer: 'Conceptual3D', license: 'BY-NC', makerworld: 'makerworld.com/models/835950',
        maker: { handle: 'Conceptual3D', name: 'Conceptual3D', remix: false },
        plate: 1, plateCount: 4, progress, remainingMin: remaining, layer: Math.round(total * progress / 100), totalLayers: total,
        startedAt: now - 600 - progress * 143, finishedAt: state === 'FINISH' ? now - (ph - 0.93) * cycle : null, durationSec: state === 'FINISH' ? 14580 : null,
        predictionSec: 14370, weightG: 73.5, bedType: 'Textured PEI', objects: 4,
        speed: { level: 2, name: 'Standard', pct: 100 }, prepPercent: 100, printType: 'cloud', error: null,
        cover: real?.job?.cover || DEMO_COVER,
        filaments: [{ type: 'PLA', name: 'PLA Basic', color: '#7C4B00', colors: [], usedG: 73.5, usedM: 24.25, slot: 'A3', remain: 62, active: state === 'RUNNING' }],
      },
      temps: {
        nozzles: [
          { id: 1, side: 'L', label: 'Nozzle L', cur: nozL, tgt: state === 'FINISH' ? 0 : 220, active: true, diameter: 0.4, type: 'Hardened steel' },
          { id: 0, side: 'R', label: 'Nozzle R', cur: 38, tgt: 0, active: false, diameter: 0.4, type: 'Hardened steel' }],
        bed: { cur: state === 'PREPARE' ? Math.round(30 + ph / 0.1 * 25) : 55, tgt: state === 'FINISH' ? 0 : 55 },
        chamber: { cur: 31, tgt: 0 },
      },
      fans: { part: 60, aux: 30, chamber: 20 },
      ams: [{ id: 0, name: 'A', humidity: 43, temp: 26.1, trays: [
        tray('A1', 'PLA Basic', '#F7E6DE', 100), tray('A2', 'PLA Pure', '#FFFFFF', 38),
        tray('A3', 'PLA Basic', '#7C4B00', 62, state === 'RUNNING'), tray('A4', 'PLA', '#F72323', null)] }],
      external: [tray('Ext-R', 'PLA', '#161616', null)],
      loaded: [], accentSource: '#7C4B00',
    };
  }

  // ------------------------------------------------------------------ view -> display model
  let demoActive = false;
  function model(d, s) {
    const j = d.job, st = j.state, now = Date.now() / 1000;
    const m = {};
    m.state = d.online ? st.toLowerCase() : 'offline';
    m.isJob = j.hasJob;
    m.showProgress = j.hasJob;
    m.isDone = st === 'FINISH';
    m.label = d.online ? (j.hasJob ? j.label : (st === 'IDLE' || st === 'FINISH' ? 'Idle' : j.label)) : (d.hasData ? 'Reconnecting' : 'Printer offline');
    let stage = d.online && (j.active || j.finishing) ? (j.stage || '') : '';
    if (st === 'PREPARE' && j.prepPercent < 100) stage = `Downloading ${j.prepPercent}%`;
    if (st === 'PREPARE' && j.step && stage) stage += ` · ${j.step}/${j.steps}`;
    if (j.error && (st === 'FAILED' || st === 'PAUSE')) stage = `Error ${j.error}`;
    m.stage = stage;
    m.statusTxt = stage ? `${m.label} · ${stage}` : m.label;
    m.statusUpper = m.statusTxt.toUpperCase();
    m.name = j.hasJob ? ((s.name === 'title' && j.title) || j.fileName || 'Untitled') : 'Bambu Lab X2D';
    const mk = j.hasJob ? j.maker : null;   // the original maker when the model is a remix
    m.maker = mk ? (mk.handle || mk.name || '') : '';   // usernames are shown without "@"
    m.byline = s.byline && m.maker ? `by ${m.maker}` : '';
    m.designer = s.credit && j.hasJob ? (m.maker || j.designer || '') : '';
    m.designerStatus = m.byline ? '' : m.designer;   // lower third: no second "by" when the byline shows it
    m.designerUpper = m.designer.toUpperCase();
    const tw = d.twitch;
    m.viewersTxt = s.viewers !== false && tw?.live && tw.viewers != null ? fmtCount(tw.viewers) : '';
    m.socials = socialGroups(d, s);
    m.mw = s.credit && j.hasJob ? (j.makerworld || '') : '';
    m.creditLine = m.designer || m.mw;
    m.pct = j.progress;
    m.pctOrZero = j.hasJob ? j.progress : 0;
    const rem = j.remainingMin * 60;
    m.left = j.active && st !== 'PREPARE' ? (rem > 0 ? fmtDur(rem) : '<1m') : '';
    m.leftClock = m.left ? fmtClockDur(rem) : '';
    const elapsedSec = j.active && j.startedAt ? Math.max(0, now - j.startedAt) : null;
    const took = !j.active && j.durationSec ? fmtDur(j.durationSec) : '';
    m.el = elapsedSec != null ? fmtDur(elapsedSec) : took;   // elapsed while printing, total once done
    m.elLabel = elapsedSec != null || !took ? 'Elapsed' : 'Took';
    m.elLower = m.elLabel.toLowerCase();
    m.elUpper = m.elLabel.toUpperCase();
    m.elClock = elapsedSec != null ? fmtClockDurSec(elapsedSec) : '';
    m.elOrDash = m.el || '—';
    m.doneTxt = st === 'FINISH' ? (took ? `Done in ${took}` : 'Complete') : '';
    const total = elapsedSec != null && st === 'RUNNING' ? elapsedSec + rem : (j.durationSec || j.predictionSec || 0);
    m.totalLabel = elapsedSec != null || !j.durationSec ? 'Est. total' : 'Total';
    m.totalOrDash = total ? fmtDur(total) : '—';
    m.leftOrDash = m.left || (m.isDone ? 'Done' : '—');
    m.layerTxt = j.totalLayers ? `${j.layer}/${j.totalLayers}` : '—';
    m.plateTxt = j.hasJob && j.plateCount > 1 ? `Plate ${j.plate}/${j.plateCount}` : '';
    m.speedTxt = j.speed?.name && j.active ? `${j.speed.name} ${j.speed.pct}%` : '';
    m.speedUpper = m.speedTxt.toUpperCase();
    m.weightTxt = j.hasJob && j.weightG ? `${j.weightG} g` : '';
    m.cover = j.hasJob ? (j.cover || '') : '';
    // how much of the model is "printed": layer height is closer to physical height than time-based %
    let frac = j.totalLayers ? j.layer / j.totalLayers : j.progress / 100;
    if (st === 'PREPARE') frac = 0;
    if (st === 'FINISH') frac = 1;
    m.buildFrac = clamp(frac, 0, 1);
    m.buildOn = s.build !== false;
    m.building = st === 'RUNNING' || st === 'PAUSE';
    m.meterOn = s.meter !== false && (demoActive || d.obs?.meter === 'live');
    m.showAms = s.ams && ((j.hasJob && (j.filaments || []).length) || (d.ams || []).length > 0 || (d.external || []).length > 0);
    return m;
  }

  // The streamer's socials, in this order; platforms that share a handle are shown once with all their icons.
  const SOCIALS = [['tiktok', 'socialTiktok', 'tiktokHandle'], ['twitch', 'socialTwitch', null],
    ['youtube', 'socialYoutube', 'youtubeHandle'], ['x', 'socialX', 'xHandle'], ['instagram', 'socialInstagram', 'instagramHandle']];
  function socialGroups(d, s) {
    const groups = [];
    for (const [net, on, key] of SOCIALS) {
      const handle = String((key ? s[key] : (s.twitchChannel || d.twitch?.envChannel)) || '').replace(/^@/, '');
      if (!s[on] || !handle) continue;
      const g = groups.find((x) => x.handle.toLowerCase() === handle.toLowerCase());
      if (g) g.nets.push(net); else groups.push({ handle, nets: [net] });
    }
    return groups;
  }

  // ------------------------------------------------------------------ list renderers
  function tempItems(d) {
    const t = d.temps, items = [];
    const dual = t.nozzles.length > 1;
    for (const n of t.nozzles) items.push({ icon: 'nozzle', label: n.label, side: n.side, mono: n.side ? `NOZ.${n.side}` : 'NOZZLE', cur: n.cur, tgt: n.tgt, max: 300, active: dual && n.active });
    if (t.bed) items.push({ icon: 'bed', label: 'Bed', side: '', mono: 'BED', cur: t.bed.cur, tgt: t.bed.tgt, max: 120 });
    if (t.chamber) items.push({ icon: 'chamber', label: 'Chamber', side: '', mono: 'CHMB', cur: t.chamber.cur, tgt: t.chamber.tgt, max: 70 });
    for (const it of items) {
      it.heating = it.tgt > 0 && it.cur < it.tgt - 3;
      it.holding = it.tgt > 0 && !it.heating;
      it.off = !it.tgt && !it.active;
      it.cls = [it.heating && 'heating', it.holding && 'holding', it.active && 'active', it.off && 'off'].filter(Boolean).join(' ');
    }
    return items;
  }

  const icon = (name) => `<svg class="ic"><use href="#i-${name}"/></svg>`;
  function swatch(f, extra = '') {
    if (!f || f.empty || !f.color) return `<i class="sw empty ${extra}"></i>`;
    if (f.colors && f.colors.length > 1) return `<i class="sw multi ${extra}" style="--c1:${f.colors[0]};--c2:${f.colors[1]}"></i>`;
    return `<i class="sw ${extra}" style="--c:${f.color}"></i>`;
  }
  function allTrays(d) {
    const out = [];
    for (const u of d.ams || []) out.push(...u.trays);
    for (const e of d.external || []) out.push(e);
    return out;
  }

  const LISTS = {
    temps(d, m, variant) {
      const items = tempItems(d);
      switch (variant) {
        case 'chip':
        case 'mini':
          return items.map((t) => `<span class="chip ${t.cls}">${icon(t.icon)}${t.side ? `<em>${t.side}</em>` : ''}<b>${t.cur}°</b>${t.tgt && variant === 'chip' ? `<i>/${t.tgt}</i>` : ''}</span>`).join('');
        case 'cell': {
          const noz = items.filter((t) => t.icon === 'nozzle');
          const rest = items.filter((t) => t.icon !== 'nozzle');
          const nozCell = noz.length > 1
            ? `<div class="t-cell"><label>${icon('nozzle')}Nozzles</label><b class="num">${noz.map((t) => `<span class="${t.cls}">${t.cur}°<i>${t.side}</i></span>`).join(' ')}</b></div>`
            : noz.map((t) => `<div class="t-cell ${t.cls}"><label>${icon('nozzle')}Nozzle</label><b class="num">${t.cur}°${t.tgt ? `<i>/${t.tgt}</i>` : ''}</b></div>`).join('');
          return nozCell + rest.map((t) => `<div class="t-cell ${t.cls}"><label>${icon(t.icon)}${esc(t.label)}</label><b class="num">${t.cur}°${t.tgt ? `<i>/${t.tgt}</i>` : ''}</b></div>`).join('');
        }
        case 'gauge':
          return items.map((t) => `<div class="gauge ${t.cls}"><span class="lbl ${t.active ? 'act' : ''}">${t.mono}</span><span class="trk"><i style="--p:${clamp(t.cur / t.max * 100, 0, 100).toFixed(1)}"></i>${t.tgt ? `<u style="--t:${clamp(t.tgt / t.max * 100, 0, 100).toFixed(1)}"></u>` : ''}</span><span class="val">${t.cur}<small>/${t.tgt || '--'}°C</small></span></div>`).join('');
        case 'row':
          return items.map((t) => `<div class="trow ${t.cls}">${icon(t.icon)}<span>${esc(t.label)}</span><b>${t.cur}°</b><i>${t.tgt ? `→ ${t.tgt}°` : 'off'}</i></div>`).join('');
      }
      return '';
    },
    filaments(d, m, variant) {
      const fil = d.job.hasJob ? d.job.filaments || [] : [];
      if (!fil.length) return '';
      const main = fil.find((f) => f.active) || fil[0];
      const totalG = fil.reduce((a, f) => a + (f.usedG || 0), 0);
      switch (variant) {
        case 'line': {
          const extra = fil.length > 1 ? fil.filter((f) => f !== main).slice(0, 4).map((f) => swatch(f)).join('') : '';
          return `<div class="fil-line">${swatch(main)}${extra}<b>${esc(main.name || main.type)}</b>${main.slot ? `<span class="slot">${esc(main.slot)}</span>` : ''}${totalG ? `<span>${totalG.toFixed(1)} g</span>` : ''}</div>`;
        }
        case 'cell': {
          const label = `<label>${icon('cube')}Filament${fil.length > 1 ? 's' : ''}${totalG ? ` · ${totalG.toFixed(0)} g` : ''}</label>`;
          if (fil.length === 1) return `<div class="t-cell">${label}<b>${swatch(main)}${esc(main.name || main.type)}</b></div>`;
          // several filaments (both nozzles, or AMS colours): a line each, the one printing now first and bright;
          // the planned nozzle (L/R) when the job uses both
          const order = [main, ...fil.filter((f) => f !== main).sort((a, b) => (b.usedG || 0) - (a.usedG || 0))];
          const twoNozzles = new Set(order.map((f) => f.side).filter(Boolean)).size > 1;
          const shown = order.slice(0, 2), more = order.length - shown.length;
          const lines = shown.map((f, i) => `<span class="fl${f.active ? ' on' : ''}">${swatch(f)}<span>${esc(f.name || f.type)}</span>`
            + `${twoNozzles && f.side ? `<i>${f.side}</i>` : ''}${i === 1 && more > 0 ? `<em>+${more}</em>` : ''}</span>`).join('');
          return `<div class="t-cell multi">${label}<b class="fls">${lines}</b></div>`;
        }
        case 'mono':
          return fil.slice(0, 3).map((f) => `<div class="mono-fil">${swatch(f)}<b>${esc((f.name || f.type || '').toUpperCase())}</b><span>${f.slot ? `[${esc(f.slot)}]` : ''} ${f.usedG ? f.usedG.toFixed(1) + 'G' : ''}</span></div>`).join('');
      }
      return '';
    },
    ams(d, m, variant) {
      const units = d.ams || [];
      const ext = d.external || [];
      if (!units.length && !ext.length) return '';
      switch (variant) {
        case 'dots':
          return `<div class="amsdots">${units.map((u) => u.trays.map((t) => swatch(t, t.active ? 'on' : '')).join('')).join('<i class="gap"></i>')}${ext.length ? '<i class="gap"></i>' + ext.map((t) => swatch(t, t.active ? 'on' : '')).join('') : ''}</div>`;
        case 'mono':
          return units.map((u) => `<div class="mono-ams">AMS-${esc(u.name)} <span class="sq">${u.trays.map((t) => swatch(t, t.active ? 'on' : '')).join('')}</span>${u.humidity != null ? ` RH${u.humidity}%` : ''}${u.temp != null ? ` ${u.temp.toFixed(0)}°C` : ''}</div>`).join('');
        case 'spools':
          return units.map((u) => `<div class="ams-head"><span>AMS ${esc(u.name)}</span><span>${u.humidity != null ? `${icon('drop')}${u.humidity}%` : ''}${u.temp != null ? ` · ${u.temp.toFixed(0)}°C` : ''}</span></div>
            <div class="spools">${u.trays.map((t) => `<div class="spool ${t.active ? 'on' : ''}"><span class="ring ${t.remain == null ? 'unk' : ''}" style="--r:${t.remain ?? 0}">${swatch(t)}</span><span class="lbl">${esc(t.empty ? 'Empty' : (t.name || t.type))}</span><span class="slot">${esc(t.slot)}${t.remain != null ? ` · ${t.remain}%` : ''}</span></div>`).join('')}</div>`).join('');
      }
      return '';
    },
    segments(d, m) {
      const n = 40, on = Math.floor(m.pctOrZero / 100 * n);
      let html = '';
      for (let i = 0; i < n; i++) html += `<i class="${i < on ? 'on' : (i === on && d.job.state === 'RUNNING' ? 'edge' : '')}"></i>`;
      return html;
    },
  };

  // Vertical extent of the model inside the (transparent) plate render, in % of the image height,
  // so the build-up fill maps layers onto the model rather than onto the empty padding around it.
  const bounds = new Map();
  function modelBounds(src) {
    if (bounds.has(src)) return bounds.get(src);
    bounds.set(src, null);
    const img = new Image();
    img.onload = () => {
      let result = [0, 100, 6, 6];
      try {
        const n = 128, c = document.createElement('canvas');
        c.width = c.height = n;
        const ctx = c.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, n, n);
        const px = ctx.getImageData(0, 0, n, n).data;
        let top = -1, bottom = -1, left = n, right = -1;
        for (let y = 0; y < n; y++) {
          for (let x = 0; x < n; x++) {
            if (px[(y * n + x) * 4 + 3] > 24) {
              if (top < 0) top = y;
              bottom = y;
              left = Math.min(left, x);
              right = Math.max(right, x);
            }
          }
        }
        if (top >= 0) result = [top / n * 100, (bottom + 1) / n * 100, left / n * 100, (n - 1 - right) / n * 100];
      } catch (e) { /* tainted canvas etc.: fall back to the full image */ }
      bounds.set(src, result);
      render();
    };
    img.onerror = () => bounds.set(src, [0, 100, 6, 6]);
    img.src = src;
    return null;
  }

  // ------------------------------------------------------------------ VU meter (OBS InputVolumeMeters)
  // Two segmented L/R bars, Winamp style: instant attack, smooth release, peak caps that hold then fall.
  // It's a beat visualiser rather than a studio meter: bars are centred on the running average loudness and
  // scaled by its recent variation, so loud masters (always near 0 dBFS) and quiet music both dance.
  const vu = { target: [-Infinity, -Infinity], level: [-90, -90], mean: -40, mad: 3,
               peak: [0, 0], hold: [0, 0], lastData: 0, lastFrame: 0 };
  let vuEls = [];
  const toDb = (x) => (x > 0 ? 20 * Math.log10(x) : -Infinity);
  function onMeter(levels) {
    if (!levels) { vu.lastData = 0; return; }
    vu.target = [toDb(levels[0]), toDb(levels[1])];   // RMS magnitude per channel (OBS mixer's main bar)
    vu.lastData = performance.now();
    vuKick();
  }
  // The loop only runs while a meter is on screen and something moves: 24 fps (OBS sources render at ~30),
  // stopped entirely when hidden, silent and settled, or when the page itself is in the background.
  const VU_FRAME_MS = 1000 / 24;
  let vuRunning = false, vuVisible = false;
  function vuKick() {
    if (vuRunning || !vuVisible || LITE || document.hidden) return;
    vuRunning = true;
    vu.lastFrame = performance.now();
    setTimeout(vuFrame, VU_FRAME_MS);
  }
  document.addEventListener('visibilitychange', vuKick);
  function vuBuild(el) {
    const n = +el.dataset.segs || 16;
    const seg = (i) => {
      const f = i / (n - 1);
      const c = f < 0.6 ? 'var(--g)' : f < 0.8 ? 'var(--y)' : f < 0.93 ? 'var(--o)' : 'var(--r)';
      return `<i class="vu-seg" style="--c:${c}"></i>`;
    };
    const bar = `<div class="vu-ch">${Array.from({ length: n }, (_, i) => seg(i)).join('')}</div>`;
    el.innerHTML = el.dataset.vu === 'h'
      ? `<span class="vu-lbl">L</span>${bar}<span class="vu-lbl">R</span>${bar}`
      : `${bar}${bar}<span class="vu-lbl">L</span><span class="vu-lbl">R</span>`;
    el._segs = [...el.querySelectorAll('.vu-ch')].map((ch) => [...ch.children]);
    el._state = [[-1, -1], [-1, -1]];
  }
  function demoBeat(now) {
    const t = now / 1000, beat = (t * 2.05) % 1;                 // ~123 bpm
    const kick = Math.exp(-beat * 7), wob = Math.sin(t * 9.3) * 0.5 + Math.sin(t * 23.1) * 0.5;
    return [-22 + kick * 12 + wob * 2 + Math.random() * 1.5, -23 + kick * 11 + wob * 2.5 + Math.random() * 1.5];
  }
  function vuFrame() {
    const now = performance.now();
    if (!vuVisible || document.hidden) { vuRunning = false; return; }
    const dt = Math.min(0.1, (now - vu.lastFrame) / 1000 || 0);
    vu.lastFrame = now;
    const live = now - vu.lastData < 1200;
    const target = live ? vu.target : (demoActive ? demoBeat(now) : [-Infinity, -Infinity]);
    const t = [Math.max(target[0], -90), Math.max(target[1], -90)];
    const avg = (t[0] + t[1]) / 2;
    if (Math.abs(avg - vu.mean) > 18) vu.mean = avg;                               // start / after silence: re-centre now
    vu.mean += (avg - vu.mean) * (1 - Math.exp(-dt / 1.5));                       // ~1.5 s average
    vu.mad += (Math.abs(avg - vu.mean) - vu.mad) * (1 - Math.exp(-dt / 4));     // ~4 s spread
    const frac = [0, 0];
    for (let c = 0; c < 2; c++) {
      vu.level[c] = t[c] >= vu.level[c] ? t[c] : Math.max(t[c], vu.level[c] - 42 * dt);
      frac[c] = vu.mean < -50 ? 0 : clamp(0.52 + 0.2 * (vu.level[c] - vu.mean) / Math.max(vu.mad, 1.2), 0, 1);
      if (frac[c] >= vu.peak[c]) { vu.peak[c] = frac[c]; vu.hold[c] = now + 650; }
      else if (now > vu.hold[c]) vu.peak[c] = Math.max(0, vu.peak[c] - 0.55 * dt);
    }
    // silence and everything has fallen to zero: stop until the next meter frame (onMeter kicks it again)
    const settled = !demoActive && frac[0] === 0 && frac[1] === 0 && vu.peak[0] === 0 && vu.peak[1] === 0;
    for (const el of vuEls) {
      if (!el._segs || !el.offsetParent) continue;
      const n = el._segs[0].length;
      for (let c = 0; c < 2; c++) {
        const lit = Math.round(frac[c] * n);
        const pk = Math.round(vu.peak[c] * n) - 1;
        const [pl, pp] = el._state[c];
        if (lit === pl && pk === pp) continue;
        el._segs[c].forEach((seg, i) => {
          seg.classList.toggle('on', i < lit);
          seg.classList.toggle('pk', i === pk && pk >= lit && pk > 0);
        });
        el._state[c] = [lit, pk];
      }
    }
    if (settled && !live) { vuRunning = false; return; }
    setTimeout(vuFrame, VU_FRAME_MS);
  }

  // ------------------------------------------------------------------ mount + bind
  function mount(layout) {
    const tpl = document.getElementById(`tpl-${layout}`) || document.getElementById('tpl-glass');
    stage.replaceChildren(tpl.content.cloneNode(true));
    for (const img of stage.querySelectorAll('img')) img.addEventListener('error', () => { img.hidden = true; });
    vuEls = [...stage.querySelectorAll('[data-vu]')];
    vuEls.forEach(vuBuild);
    mounted = layout;
  }

  function bind(m, d) {
    for (const el of stage.querySelectorAll('[data-t]')) {
      const v = m[el.dataset.t];
      const txt = v == null ? '' : String(v);
      if (el.textContent !== txt) el.textContent = txt;
    }
    // toggleAttribute (not .hidden) so it also works on <svg> elements
    for (const el of stage.querySelectorAll('[data-if]')) el.toggleAttribute('hidden', !m[el.dataset.if]);
    for (const el of stage.querySelectorAll('[data-ifnot]')) el.toggleAttribute('hidden', !!m[el.dataset.ifnot]);
    for (const el of stage.querySelectorAll('[data-p]')) el.style.setProperty('--p', m[el.dataset.p] ?? 0);
    for (const el of stage.querySelectorAll('[data-dash]')) el.style.strokeDasharray = `${m[el.dataset.dash] ?? 0} 100`;
    for (const el of stage.querySelectorAll('[data-src]')) {
      const v = m[el.dataset.src];
      if (v) { if (el.getAttribute('src') !== v) { el.hidden = false; el.src = v; } }
      else { el.removeAttribute('src'); el.hidden = true; }
    }
    for (const el of stage.querySelectorAll('[data-build]')) {
      const b = m.cover ? modelBounds(m.cover) : null;
      if (m.cover && !b) continue;  // wait for the model bounds so the first fill animates from empty
      const [top, bottom, left, right] = b || [0, 100, 6, 6];
      const cut = m.buildOn && m.cover ? bottom - m.buildFrac * (bottom - top) : 0;
      el.style.setProperty('--cut', cut.toFixed(2));
      el.style.setProperty('--l', left.toFixed(1));
      el.style.setProperty('--r', right.toFixed(1));
      el.classList.toggle('building', m.buildOn && m.building && m.buildFrac > 0 && m.buildFrac < 1);
      el.classList.toggle('full', !m.buildOn || m.buildFrac >= 1);
    }
    for (const el of stage.querySelectorAll('[data-list]')) {
      const html = LISTS[el.dataset.list](d, m, el.dataset.variant);
      if (el._html !== html) { el.innerHTML = html; el._html = html; }
    }
  }

  // ------------------------------------------------------------------ socials (+ music credit): roll in next to the print name
  // Every `socialEvery` seconds each social group flips in (at the right end of the name line), a glint runs
  // over it, and it flips back out; then the next group. Only these few seconds animate; the rest of the time it's idle.
  const SOC_FIRST_MS = 8000, SOC_SOON_MS = 1500, SOC_SHOW_MS = 10600, SOC_SWAP_MS = 600;   // show: roll in + 3 glints
  // Items of a round: each social group ("Follow" + logos + username), then an optional music credit ("Music" + note +
  // text), shown only if the OBS input it follows is playing when its turn comes.
  const soc = { key: null, groups: [], music: null, every: 120, timer: 0, audible: [], round: [] };
  function socHtml(it) {
    const icons = it.icons.map((n, i) => `<svg class="ic${n === 'instagram' ? ' line' : ''}${n === 'note' ? ' note' : ''}" style="--i:${i}"><use href="#i-${n}"/></svg>`).join('');
    const letters = [...it.text].map((c, i) => `<span class="ch" style="--i:${i}">${esc(c)}</span>`).join('');
    return `<span class="soc in"><span class="soc-cta">${esc(it.label)}</span><span class="soc-ics">${icons}</span><b>${letters}</b></span>`;
  }
  function socRound() {
    const items = soc.groups.map((g) => ({ label: 'Follow', icons: g.nets, text: g.handle }));
    if (soc.music) items.push({ label: soc.music.label, icons: ['note'], text: soc.music.text, input: soc.music.input });
    return items;
  }
  function socLater(ms, fn) { clearTimeout(soc.timer); soc.timer = setTimeout(fn, ms); }
  function socShow(i) {
    const box = stage.querySelector('.t-soc');
    if (!box) return;
    if (i === 0) soc.round = socRound();
    if (soc.round[i]?.input && !soc.audible.includes(soc.round[i].input)) return socShow(i + 1);   // music not playing
    if (i >= soc.round.length) {   // hidden until the next round
      box.replaceChildren();
      return socLater(soc.every * 1000, () => socShow(0));
    }
    box.innerHTML = socHtml(soc.round[i]);
    socLater(SOC_SHOW_MS, () => {
      box.firstElementChild?.classList.add('out');
      socLater(SOC_SWAP_MS, () => socShow(i + 1));
    });
  }
  function updateSocials(m, s, d) {
    soc.audible = d.obs?.meterAudible || [];   // checked when the music credit's turn comes; never cuts an item short
    const groups = LITE ? [] : m.socials;   // gallery thumbnails stay still
    const music = !LITE && s.musicCredit && String(s.musicText || '').trim()
      ? { label: String(s.musicLabel || 'Music').trim() || 'Music', text: String(s.musicText).trim(), input: s.musicInput || '' } : null;
    const every = clamp(+s.socialEvery || 120, 20, 1800);
    const key = JSON.stringify([groups, music, every, mounted]);
    if (key === soc.key) return;
    const first = soc.key === null;
    soc.key = key; soc.groups = groups; soc.music = music; soc.every = every;
    clearTimeout(soc.timer);
    const box = stage.querySelector('.t-soc');
    box?.replaceChildren();
    // soon after a settings change (a preview of the new set), a little later after the page loads
    if (box && (groups.length || music)) socLater(first ? SOC_FIRST_MS : SOC_SOON_MS, () => socShow(0));
  }

  function fit() {
    const k = Math.min(innerWidth / 1280, innerHeight / 720);
    stage.style.transform = `translate(${(innerWidth - 1280 * k) / 2}px, ${(innerHeight - 720 * k) / 2}px) scale(${k})`;
  }

  function render() {
    const s = settings();
    const demo = demoMode(s, view);
    demoActive = demo;
    const d = demo ? demoView(view) : view;
    const layout = ['glass', 'ticker', 'hud', 'sidebar', 'orb', 'minimal', 'none'].includes(s.layout) ? s.layout : 'glass';
    if (layout !== mounted) mount(layout);
    stage.style.setProperty('--ui', clamp(+s.scale || 1, 0.5, 2));

    const pos = s.position === 'auto' || !s.position ? (DEFAULT_POS[layout] || 'bl') : s.position;
    for (const el of stage.querySelectorAll('[data-at]')) el.dataset.pos = pos;
    for (const el of stage.querySelectorAll('[data-side]')) el.dataset.side = s.side === 'right' ? 'right' : 'left';

    const serverDown = !demo && serverDownSince && Date.now() - serverDownSince > 8000;
    // nothing trustworthy to show: no data yet, or the printer has been unreachable for a while
    const stale = !demo && d && !d.online && (!d.hasData || Date.now() / 1000 - (d.printer?.lastReport || 0) > 90);
    if (!d || serverDown || stale) { stage.classList.add('hidden'); return; }

    const m = model(d, s);
    stage.dataset.state = m.state;
    const accent = m.state === 'pause' ? '#fbbf24' : m.state === 'failed' ? '#f87171'
      : /^#[0-9a-f]{6}$/i.test(s.accent) ? s.accent : (accentFrom(d.accentSource) || FALLBACK_ACCENT);
    stage.style.setProperty('--accent', accent);
    stage.classList.toggle('hidden', layout === 'none' || (s.idle === 'hide' && !d.job.hasJob && !demo));
    bind(m, d);
    updateSocials(m, s, d);
    vuVisible = !stage.classList.contains('hidden') && vuEls.some((el) => el.offsetParent);
    vuKick();
    if (!stage.classList.contains('ready')) requestAnimationFrame(() => stage.classList.add('ready'));
  }

  addEventListener('resize', fit);
  fit();
  render();
  if (params.has('embed')) {
    // Inside the control panel: the parent relays state, so previews don't each hold an SSE connection
    // (browsers allow only ~6 connections per host).
    addEventListener('message', (e) => {
      if (e.origin !== location.origin) return;
      if (e.data?.type === 'bambu-meter') return onMeter(e.data.levels);
      if (e.data?.type !== 'bambu-state') return;
      const first = !view;
      view = e.data.view;
      serverDownSince = view ? 0 : (serverDownSince || Date.now());
      if (!LITE || first) render();
    });
  } else {
    connect();
  }
  setInterval(render, LITE ? 5000 : 1000);
})();
