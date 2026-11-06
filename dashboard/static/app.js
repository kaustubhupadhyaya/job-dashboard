'use strict';
/* The board: polls /api/status every 30 s, redraws ages every 15 s, draws every lane on one
   00:00-24:00 axis. Rows are judged on the server from saved rows only; this file only draws. */

const POLL_MS = 30000, TICK_MS = 15000, STALE_MS = 90000;
const S = { data: null, lastOk: 0, open: new Set(), signed: new Set(), first: true, justOpened: null };
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pad = (n) => String(n).padStart(2, '0');
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function nowMin() { const d = new Date(); return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60; }
function hm(m) { if (m == null) return '--:--'; return `${pad(Math.floor(m / 60))}:${pad(Math.floor(m % 60))}`; }
function dayLabel(d) { return `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`; }
function sameDay(a, b) { return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); }
function dur(min) {
  min = Math.round(min);
  if (min < 1) return 'just now';
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60), r = min % 60;
  if (h >= 10) return `${h} h`;
  return r ? `${h} h ${r} min` : `${h} h`;
}
function age(iso) {
  if (!iso) return { time: '--:--', age: 'never' };
  const t = new Date(iso), now = new Date();
  const time = `${pad(t.getHours())}:${pad(t.getMinutes())}`;
  if (sameDay(t, now)) {
    const m = (now - t) / 60000;
    return { time, age: m < 1 ? 'just now' : `${dur(m)} ago` };
  }
  const y = new Date(now); y.setDate(now.getDate() - 1);
  return { time, age: sameDay(t, y) ? 'yesterday' : `${t.getDate()} ${MONTHS[t.getMonth()]}` };
}
function siteDate(iso) {
  if (!iso) return { text: '--', old: true };
  const [y, m, d] = iso.split('-').map(Number);
  const dt = new Date(y, m - 1, d);
  return { text: `${d} ${MONTHS[m - 1]}`, old: !sameDay(dt, new Date()) };
}
function sinceLabel(r) {
  const last = r.last ? new Date(r.last) : null;
  return last && sameDay(last, new Date()) ? `since ${pad(last.getHours())}:${pad(last.getMinutes())}` : 'today';
}

/* ------------------------------------------------------------------ verdict */
function needPhrase(r) {
  const who = r.kind === 'refresh' ? `${r.name}'s refresh` : r.name;
  switch (r.state) {
    case 'failing': return `${who} is failing`;
    case 'stalled': return r.count ? `${who} has saved nothing ${sinceLabel(r)}` : `${who} has applied nothing today`;
    case 'logged_out': return `${who} is logged out`;
    case 'stopped': return `${who} has stopped`;
    case 'challenge': return `${who} hit the site's bot check`;
    case 'cant_read': return `the board can't read ${r.name}'s file`;
    case 'missed': return `${r.name} missed today's resume refresh`;
    case 'login': return `${r.name}'s resume refresh needs your login`;
    case 'unconfirmed': return `${r.name}'s refresh isn't confirmed on the site`;
    default: return `${who} needs you`;
  }
}
function verdict(d, stale) {
  if (stale) {
    const at = S.lastOk ? new Date(S.lastOk) : null;
    return { text: `This board stopped updating${at ? ` at ${pad(at.getHours())}:${pad(at.getMinutes())}` : ''}. Start it again with ${d ? d.start_command : 'job-tracker\\dashboard.cmd'}.`, tab: 'Not updating', sign: true };
  }
  if (!d) return { text: "Reading the bots' files.", tab: "Reading the bots' files", sign: false, loading: true };
  const rows = [...d.pipelines, ...d.refresh];
  const needs = rows.filter((r) => r.sign);
  const tabNames = needs.map((r) => (r.kind === 'refresh' ? `${r.name} refresh` : r.name));
  if (needs.length === 1) {
    const r = needs[0];
    const why = r.state === 'stalled' ? (r.count ? `nothing saved ${sinceLabel(r)}` : 'nothing saved today')
      : r.state === 'failing' ? `failing, nothing saved ${sinceLabel(r)}` : null;
    const text = why ? `${r.kind === 'refresh' ? `${r.name}'s refresh` : r.name} needs you: ${why}.` : `${cap(needPhrase(r))}.`;
    return { text, tab: `Needs you: ${tabNames[0]}`, sign: false };
  }
  if (needs.length === 2) return { text: `${cap(needPhrase(needs[0]))} and ${needPhrase(needs[1])}.`, tab: `Needs you: ${tabNames.join(', ')}`, sign: false };
  if (needs.length > 2) {
    const names = needs.map((r) => (r.kind === 'refresh' ? `${r.name}'s refresh` : r.name));
    return { text: `${needs.length} need you: ${names.slice(0, -1).join(', ')} and ${names.at(-1)}.`, tab: `Needs you: ${tabNames.join(', ')}`, sign: false };
  }
  const live = d.pipelines.filter((r) => !['switched_off', 'not_built', 'watching'].includes(r.state));
  const win = d.window;
  if (win && win.configured && !win.inside && live.length && live.every((r) => r.state === 'resting')) return { text: `Scheduled rest: the appliers run ${win.start} to ${win.end} and start again at ${win.start}.`, tab: 'Scheduled rest', sign: false };
  if (live.length && live.every((r) => r.state === 'resting')) return { text: 'Every bot is resting at its cap until midnight.', tab: 'All resting', sign: false };
  const quiet = live.filter((r) => r.state === 'applying').map((r) => {
    const last = r.last ? new Date(r.last) : null;
    const from = last && sameDay(last, new Date()) ? last : new Date(new Date().setHours(0, 0, 0, 0));
    return { r, m: (Date.now() - from) / 60000 };
  }).sort((a, b) => b.m - a.m)[0];
  return { text: quiet ? `Nothing needs you. The longest quiet is ${quiet.r.name}, ${dur(quiet.m)}.` : 'Nothing needs you.', tab: 'Nothing needs you', sign: false };
}
function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

function favicon(sign) {
  const svg = sign
    ? "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><rect x='1.5' y='1.5' width='13' height='13' fill='#F6C400' stroke='#1B2230' stroke-width='3'/></svg>"
    : "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><rect x='1.5' y='1.5' width='13' height='13' fill='none' stroke='#1B2230' stroke-width='3'/></svg>";
  $('#favicon').href = 'data:image/svg+xml,' + encodeURIComponent(svg);
}

/* ------------------------------------------------------------------ lanes (inline SVG, one shared axis) */
function laneApp(r, W, H, now) {
  const top = 4, zero = H - 16, railY = H - 9, tickTop = H - 13, tickBot = H - 5;
  const x = (m) => (Math.max(0, Math.min(1440, m)) / 1440) * W;
  const cap = r.cap || 1;
  const y = (c) => zero - (Math.min(c, cap) / cap) * (zero - top);
  let s = '';
  for (let h = 0; h <= 24; h += 3) s += `<line class="g-grid" x1="${x(h * 60)}" x2="${x(h * 60)}" y1="${top}" y2="${zero}"/>`;
  if (r.state === 'resting' && r.rest_from != null && now > r.rest_from) {
    s += `<rect class="g-rested" x="${x(r.rest_from)}" y="${top}" width="${x(now) - x(r.rest_from)}" height="${zero - top}"/>`;
  }
  s += `<line class="g-zero" x1="0" x2="${W}" y1="${zero}" y2="${zero}"/>`;
  if (r.points) {
    let d = `M0 ${zero}`, c = 0;
    for (const p of r.points) { c += 1; d += ` H${x(p).toFixed(1)} V${y(c).toFixed(1)}`; }
    d += ` H${x(now).toFixed(1)}`;
    s += `<path class="g-line" d="${d}"/>`;
    if (r.cap_reached_at != null) s += `<rect class="g-term" x="${x(r.cap_reached_at) - 1.5}" y="${top}" width="3" height="${zero - top}"/>`;
    if (r.count > cap) s += `<rect class="g-notch" x="${x(r.points.at(-1)) - 1}" y="${top - 4}" width="2" height="4"/>`;
  }
  for (const [a, b] of r.alive || []) {
    if (a >= now) continue;
    s += `<line class="g-rail" x1="${x(a)}" x2="${x(Math.min(b, now))}" y1="${railY}" y2="${railY}"/>`;
  }
  for (const t of r.starts || []) s += `<line class="g-tick" x1="${x(t)}" x2="${x(t)}" y1="${tickTop}" y2="${tickBot}"/>`;
  s += `<line class="g-now" x1="${x(now)}" x2="${x(now)}" y1="0" y2="${H}"/>`;
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">${s}</svg>`;
}

function laneRef(r, W, H, now) {
  const x = (m) => (Math.max(0, Math.min(1440, m)) / 1440) * W;
  const toMin = (t) => { const [h, m] = t.split(':').map(Number); return h * 60 + m; };
  const wo = toMin(r.window[0]), wc = toMin(r.window[1]);
  const by = 10, zero = H - 1;
  let s = '';
  for (let h = 0; h <= 24; h += 3) s += `<line class="g-grid" x1="${x(h * 60)}" x2="${x(h * 60)}" y1="2" y2="${zero}"/>`;
  s += `<line class="g-zero" x1="0" x2="${W}" y1="${zero}" y2="${zero}"/>`;
  s += `<path class="g-bracket" d="M${x(wo)} ${by + 6} V${by} H${x(wc)} V${by + 6}"/>`;
  for (const a of r.attempts || []) s += `<line class="g-tick" x1="${x(a)}" x2="${x(a)}" y1="${by + 9}" y2="${by + 15}"/>`;
  if (r.verified_at != null) s += `<circle class="g-dot${r.confirmed ? '' : ' hollow'}" cx="${x(r.verified_at)}" cy="${by}" r="3.5"/>`;
  s += `<line class="g-now" x1="${x(now)}" x2="${x(now)}" y1="0" y2="${H}"/>`;
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">${s}</svg>`;
}

function histBars(r, W, H) {
  const bw = W >= 96 ? 5 : 4, gap = 2, total = 14 * bw + 13 * gap, x0 = Math.max(0, W - total), base = H - 2;
  const avail = H - 8;
  let s = '';
  (r.history || []).forEach((h, i) => {
    if (!h.exists) return;
    const xi = x0 + i * (bw + gap);
    const ht = Math.min(h.count / (r.cap || 1), 1) * avail;
    const [yy, mm, dd] = h.date.split('-').map(Number);
    const dt = new Date(yy, mm - 1, dd);
    const title = `<title>${dayLabel(dt)}: ${h.count} of ${r.cap_label}</title>`;
    const today = i === 13;
    if (today) s += `<rect class="g-bar today" x="${xi + 0.5}" y="${base - Math.max(ht, 1) + 0.5}" width="${bw - 1}" height="${Math.max(ht, 1)}">${title}</rect>`;
    else if (h.count === 0 && h.ran) s += `<rect class="g-bar" x="${xi}" y="${base - 2}" width="${bw}" height="2">${title}</rect>`;
    else s += `<rect class="g-bar" x="${xi}" y="${base - ht}" width="${bw}" height="${ht}">${title}</rect>`;
    if (h.capped && !today) s += `<rect class="g-bar" x="${xi - 1}" y="${base - ht - 4}" width="${bw + 2}" height="2"/>`;
  });
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">${s}</svg>`;
}

function axisHead(W, H, now) {
  const x = (m) => (m / 1440) * W;
  let s = '';
  for (let h = 0; h <= 24; h += 3) {
    const anchor = h === 0 ? 'start' : h === 24 ? 'end' : 'middle';
    s += `<text class="axis-label" x="${x(h * 60)}" y="${H - 2}" text-anchor="${anchor}">${pad(h)}</text>`;
  }
  const nx = x(now), anchor = nx < 30 ? 'start' : nx > W - 30 ? 'end' : 'middle';
  s += `<text class="now-label" x="${nx}" y="${H - 18}" text-anchor="${anchor}">${hm(now)}</text>`;
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">${s}</svg>`;
}

function laneLabel(r) {
  if (r.kind === 'refresh') {
    return r.verified_at != null ? `Verified today at ${hm(r.verified_at)}.` : `Not verified today. Window ${r.window[0]} to ${r.window[1]}.`;
  }
  const a = age(r.last);
  const n = (r.starts || []).length;
  if (r.today_unit) return `${r.count ?? 'unknown'} ${r.today_unit} today.${n ? ` Scanned ${n} time${n > 1 ? 's' : ''} today.` : ''}`;
  return `${r.count ?? 'unknown'} of ${r.cap_label === '~35' ? 'about 35' : r.cap_label} today. Last saved application ${a.age === 'yesterday' ? 'yesterday' : a.age} at ${a.time}.${n ? ` Started ${n} time${n > 1 ? 's' : ''} today.` : ''}`;
}

/* ------------------------------------------------------------------ rows */
function causeHtml(parts) {
  return (parts || []).map((p) => (p.code ? `<code>${esc(p.code)}</code>` : p.link ? `<a href="${esc(p.href)}" target="_blank" rel="noopener">${esc(p.link)}</a>` : esc(p.t))).join('');
}

function rowHtml(r) {
  const isRef = r.kind === 'refresh';
  const a = age(r.last);
  const open = S.open.has(r.key);
  const off = ['switched_off', 'not_built'].includes(r.state);
  let today;
  if (isRef) {
    const sd = siteDate(r.site_date);
    today = `<span class="${sd.old ? 'old' : ''}">${sd.text}</span>`;
  } else {
    today = r.count == null ? '--' : `${r.count} <small>${r.today_unit ? esc(r.today_unit) : `of ${esc(r.cap_label)}`}</small>`;
  }
  const detail = r.detail || '';
  const cls = ['entry', isRef ? 'ref' : 'app', r.sign ? 'sign' : '', off ? 'off' : '',
    r.sign && !S.signed.has(r.key) && !S.first ? 'unroll' : ''].filter(Boolean).join(' ');
  const ageText = document.body.classList.contains('stale') && S.lastOk ? `as of ${hm(new Date(S.lastOk).getHours() * 60 + new Date(S.lastOk).getMinutes())}` : a.age;
  let h = `<div class="${cls}" data-key="${esc(r.key)}">
  <div class="row${isRef ? ' ref' : ''}" role="button" tabindex="0" aria-expanded="${open}" data-key="${esc(r.key)}">
    <div class="cell c-name"><div class="name">${esc(r.name)}</div><div class="sub">${esc(r.identity)}</div></div>
    <div class="cell c-last last"><span class="spine">${esc(a.time)}</span><span class="sub">${esc(ageText)}</span></div>
    <div class="cell c-today today">${today}</div>
    <div class="cell c-state"><div class="word">${esc(r.word)}</div><div class="sub">${off && r.off_hint ? causeHtml(r.off_hint) : esc(detail)}</div></div>
    <div class="cell lane" data-lane="${esc(r.key)}" role="img" aria-label="${esc(laneLabel(r))}"></div>
    <div class="cell hist" data-hist="${esc(r.key)}"></div>
  </div>`;
  if (r.sign && r.cause) h += `<p class="cause">${causeHtml(r.cause)}</p>`;
  if (open) h += moreHtml(r);
  return h + '</div>';
}

function moreHtml(r) {
  const apps = (r.apps || []).map((x) => `<li><span class="t">${esc(x.t)}</span><span>${esc(x.company || 'company not stored')}</span></li>`).join('');
  const evs = (r.events || []).map((x) => `<li><span class="t">${esc(x.t)}</span><span>${esc(x.text)}</span></li>`).join('');
  const links = (r.links || []).map((l) => `<a href="${esc(l.href)}" target="_blank" rel="noopener">${esc(l.label)}</a>`).join('');
  const left = r.kind === 'refresh'
    ? `<div><h3>Last verified</h3><p class="${r.last ? '' : 'empty'}">${r.last ? `${esc(age(r.last).time)}, ${esc(age(r.last).age)}; the site shows ${esc(siteDate(r.site_date).text)}` : 'Never'}</p></div>`
    : `<div><h3>Saved today, newest first</h3>${apps ? `<ol>${apps}</ol>` : '<p class="empty">None yet</p>'}</div>`;
  const opening = S.justOpened === r.key ? ' opening' : '';
  return `<div class="more${opening}">${left}<div><h3>Bot events today, newest first</h3>${evs ? `<ol>${evs}</ol>` : '<p class="empty">None today</p>'}</div><div class="links">${links}</div></div>`;
}

function headHtml(kind, withAxis) {
  if (kind === 'apply') {
    return `<div class="head"><div class="sect">Applications</div><div class="num">Last applied</div><div class="num">Today</div><div class="statehead">State</div>
      <div class="lanehead" ${withAxis ? 'data-axis="1"' : ''}></div><div class="histhead">Last 14 days</div></div>`;
  }
  const w = S.data ? S.data.refresh_window : ['09:00', '09:55'];
  return `<div class="head"><div class="sect">Resume refresh</div><div class="num">Last refresh</div><div class="num">On the site</div><div class="statehead">State</div>
    <div class="lanehead">Daily window ${esc(w[0])}–${esc(w[1])}</div><div></div></div>`;
}

/* ------------------------------------------------------------------ render */
function drawLanes() {
  const d = S.data;
  if (!d) return;
  const now = nowMin();
  const byKey = Object.fromEntries([...d.pipelines, ...d.refresh].map((r) => [r.key, r]));
  document.querySelectorAll('[data-lane]').forEach((el) => {
    const r = byKey[el.dataset.lane];
    const W = Math.floor(el.clientWidth), H = Math.floor(el.clientHeight);
    if (!r || W < 10) return;
    el.innerHTML = r.kind === 'refresh' ? laneRef(r, W, H, now) : laneApp(r, W, H, now);
  });
  document.querySelectorAll('[data-hist]').forEach((el) => {
    const r = byKey[el.dataset.hist];
    const W = Math.floor(el.clientWidth), H = Math.floor(el.clientHeight);
    if (!r || r.kind === 'refresh' || W < 10) return;
    el.innerHTML = histBars(r, W, H);
  });
  document.querySelectorAll('[data-axis]').forEach((el) => {
    el.innerHTML = axisHead(Math.floor(el.clientWidth), Math.floor(el.clientHeight), now);
  });
}

function render() {
  const d = S.data;
  const stale = S.lastOk && Date.now() - S.lastOk > STALE_MS;
  document.body.classList.toggle('stale', !!stale);
  const v = verdict(d, stale);
  const vEl = $('#verdict');
  if (vEl.textContent !== v.text) vEl.textContent = v.text;
  vEl.classList.toggle('signed', !!v.sign);
  vEl.classList.toggle('loading', !!v.loading);
  document.title = v.tab;
  const anySign = v.sign || (d && [...d.pipelines, ...d.refresh].some((r) => r.sign));
  favicon(anySign);
  renderStamp();
  if (!d) return;
  const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null;
  $('#board').innerHTML = `<section aria-label="Applications">${headHtml('apply', true)}${d.pipelines.map(rowHtml).join('')}</section>
    <section aria-label="Resume refresh">${headHtml('refresh')}${d.refresh.map(rowHtml).join('')}</section>`;
  [...d.pipelines, ...d.refresh].forEach((r) => (r.sign ? S.signed.add(r.key) : S.signed.delete(r.key)));
  S.first = false;
  drawLanes();
  renderFoot();
  if (focused) {
    const el = document.querySelector(`.row[data-key="${CSS.escape(focused)}"]`);
    if (el) el.focus({ preventScroll: true });
  }
}

function renderStamp() {
  const now = new Date();
  const upd = S.lastOk ? dur((Date.now() - S.lastOk) / 60000) : null;
  const ago = !S.lastOk ? 'not read yet' : upd === 'just now' ? `updated ${Math.max(1, Math.round((Date.now() - S.lastOk) / 1000))} s ago` : `updated ${upd} ago`;
  $('#stamp').textContent = `${dayLabel(now)}, ${ago}`;
}

function renderFoot() {
  const d = S.data;
  if (!d) return;
  const sm = d.stall_minutes;
  const read = new Date(d.generated);
  const files = d.files.map((f) => `<li>${esc(f.path)}${f.ok ? '' : ` (can't read: ${esc(f.error || '')})`}</li>`).join('');
  $('#foot').innerHTML = `
    <p class="key">The line counts today's saved applications; the top of each lane is the cap.</p>
    <p class="key">Ticks mark bot starts, the rail a running bot, shading a resting bot.</p>
    ${d.pipelines.some((r) => r.earned) ? '<p class="key">A portal that earned its row states what its last run found; Not armed means live applying is locked.</p>' : ''}
    <p id="rule">Nothing saved for ${sm.linkedin} min under the cap raises a sign (Naukri 1: ${sm.naukri1}, Naukri 2: ${sm.naukri2} min); Instahyre only on a failed run.</p>
    <p class="read">Read every 30 s from the bots' files, last at ${pad(read.getHours())}:${pad(read.getMinutes())}:${pad(read.getSeconds())}.
      <details><summary>List the files</summary><ul>${files}</ul></details></p>`;
}

async function poll() {
  try {
    const res = await fetch('/api/status', { cache: 'no-store' });
    if (!res.ok) throw new Error(res.status);
    S.data = await res.json();
    S.lastOk = Date.now();
  } catch (e) { /* the stale check below turns a dead server into the sign */ }
  render();
}

/* ------------------------------------------------------------------ interaction */
function toggle(key) {
  if (S.open.has(key)) { S.open.delete(key); S.justOpened = null; } else { S.open.add(key); S.justOpened = key; }
  render();
  S.justOpened = null;
}
document.addEventListener('click', (e) => {
  const row = e.target.closest('.row[data-key]');
  if (row && !e.target.closest('a')) toggle(row.dataset.key);
});
document.addEventListener('keydown', (e) => {
  const row = e.target.closest && e.target.closest('.row[data-key]');
  if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggle(row.dataset.key); }
  if (e.key === 'Escape' && S.open.size) {
    const key = row ? row.dataset.key : null;
    if (key && S.open.has(key)) S.open.delete(key); else S.open.clear();
    render();
  }
});

function effectiveTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) return set;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
function syncThemeButton() { $('#theme').textContent = effectiveTheme() === 'dark' ? 'Use light mode' : 'Use dark mode'; }
(function initTheme() {
  try { const t = localStorage.getItem('board-theme'); if (t === 'dark' || t === 'light') document.documentElement.dataset.theme = t; } catch (e) { /* private window */ }
  syncThemeButton();
  $('#theme').addEventListener('click', () => {
    const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('board-theme', next); } catch (e) { /* ignore */ }
    syncThemeButton();
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncThemeButton);
})();

new ResizeObserver(() => drawLanes()).observe($('#board'));
setInterval(poll, POLL_MS);
setInterval(() => { render(); }, TICK_MS);
poll();
