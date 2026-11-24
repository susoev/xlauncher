"""Интерфейс лаунчера: одна страница без сборки, открывается в окне pywebview."""

INDEX_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>XLauncher</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {
  color-scheme: light dark;
  --ink:#1c2230; --ink2:#5b6478; --ink3:#8a92a6;
  --paper:#f4f6fb; --panel:#ffffff; --line:#e1e5ef; --hover:#eef1f8; --sel:#e7e9ff;
  --brand:#4f5bff; --brand-ink:#ffffff;
  --run:#12a37a; --runbg:#e3f6ef; --alarm:#d6334a; --alarmbg:#fdecee; --caution:#b97d00; --cautionbg:#fff5dc;
  --radius-s:6px; --radius-m:10px;
  --font: "Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
  --font-display: "Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root { --ink:#e8ebf3; --ink2:#a3abbf; --ink3:#6f778b; --paper:#13161d; --panel:#1a1e27; --line:#2a303d;
          --hover:#212634; --sel:#262b4a; --brand:#7d86ff; --run:#3ccf9f; --runbg:#13302a; --alarm:#ff6b7f;
          --alarmbg:#3a1820; --caution:#e2b13c; --cautionbg:#33290f; }
}
* { box-sizing:border-box; }
html, body { height:100%; }
body { margin:0; font:14px/1.45 var(--font); color:var(--ink); background:var(--paper); display:flex; flex-direction:column; }
button { font:inherit; color:inherit; background:var(--panel); border:1px solid var(--line); border-radius:var(--radius-s);
         padding:6px 12px; cursor:pointer; }
button:hover:not(:disabled) { border-color:var(--brand); }
button:disabled { opacity:.45; cursor:default; }
button.primary { background:var(--brand); border-color:var(--brand); color:var(--brand-ink); }
button.ghost { border-color:transparent; background:transparent; }
button.ghost:hover:not(:disabled) { background:var(--hover); border-color:transparent; }
button.danger { color:var(--alarm); }
:focus-visible { outline:2px solid var(--brand); outline-offset:2px; }
input, textarea, select { font:inherit; color:var(--ink); background:var(--paper); border:1px solid var(--line);
         border-radius:var(--radius-s); padding:7px 10px; width:100%; }
textarea { resize:vertical; min-height:64px; }
.num { font-variant-numeric:tabular-nums; }
.muted { color:var(--ink2); } .faint { color:var(--ink3); } .small { font-size:12.5px; }
.spacer { flex:1; }

/* шапка */
header { display:flex; align-items:center; gap:10px; padding:10px 16px; background:var(--panel); border-bottom:1px solid var(--line); }
.brand { display:flex; align-items:center; gap:9px; font:600 16px var(--font-display); letter-spacing:-.01em; margin-right:6px; }
.brand svg { width:22px; height:22px; }
.search { position:relative; width:320px; }
.search input { padding-left:30px; background:var(--paper); }
.search svg { position:absolute; left:9px; top:50%; transform:translateY(-50%); width:15px; height:15px; color:var(--ink3); }
#updState { font-size:12.5px; color:var(--ink3); white-space:nowrap; }

/* баннеры */
#banners:empty { display:none; }
#banners { padding:10px 16px 0; display:flex; flex-direction:column; gap:8px; }
.banner { display:flex; gap:12px; align-items:flex-start; padding:10px 14px; border-radius:var(--radius-m); background:var(--panel);
          border:1px solid var(--line); }
.banner.alarm { background:var(--alarmbg); border-color:transparent; }
.banner.caution { background:var(--cautionbg); border-color:transparent; }
.banner .text { flex:1; } .banner .actions { display:flex; gap:6px; flex-wrap:wrap; }
.progress { height:4px; background:var(--line); border-radius:2px; overflow:hidden; margin-top:6px; }
.progress i { display:block; height:100%; background:var(--brand); }

/* основная область */
.main { flex:1; display:grid; grid-template-columns:minmax(360px, 440px) 1fr; min-height:0; }
.list { border-right:1px solid var(--line); display:flex; flex-direction:column; min-height:0; background:var(--panel); }
.filters { display:flex; gap:4px; padding:10px 12px; border-bottom:1px solid var(--line); }
.filters button { padding:4px 10px; border-radius:20px; font-size:12.5px; border-color:transparent; background:transparent; color:var(--ink2); }
.filters button[aria-pressed="true"] { background:var(--sel); color:var(--ink); }
.filters .count { color:var(--ink3); margin-left:5px; }
#rows { overflow:auto; flex:1; padding-bottom:16px; }
.group { display:flex; align-items:baseline; gap:8px; padding:14px 14px 4px; font-weight:600; font-size:13px; color:var(--ink2); }
.group .n { font-weight:400; color:var(--ink3); }
.row { display:grid; grid-template-columns:34px 1fr auto; gap:10px; align-items:center; padding:7px 12px 7px 10px;
       margin:0 6px; border-radius:var(--radius-s); cursor:pointer; border-left:3px solid transparent; }
.row:hover { background:var(--hover); }
.row.sel { background:var(--sel); }
.row.st-run { border-left-color:var(--run); } .row.st-alarm { border-left-color:var(--alarm); } .row.st-wait { border-left-color:var(--caution); }
.row .name { font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.row .sub { font-size:12.5px; color:var(--ink2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.row .sub .alarmtxt { color:var(--alarm); } .row .sub .runtxt { color:var(--run); } .row .sub .waittxt { color:var(--caution); }
.row .go { width:30px; height:30px; padding:0; display:grid; place-items:center; border-radius:50%; }
.row .go svg { width:14px; height:14px; }
.row .go.stop { color:var(--alarm); }
.empty { padding:40px 24px; color:var(--ink2); text-align:center; }

/* карточка */
.detail { overflow:auto; min-height:0; }
.detail .inner { max-width:780px; padding:24px 28px 40px; }
.dhead { display:grid; grid-template-columns:72px 1fr auto; gap:18px; align-items:center; }
.dhead h1 { font:600 24px/1.2 var(--font-display); letter-spacing:-.015em; margin:0; }
.dhead .group-name { color:var(--ink2); margin-top:2px; }
.status { display:inline-flex; align-items:center; gap:7px; margin-top:8px; font-size:13px; }
.status i { width:8px; height:8px; border-radius:50%; background:var(--ink3); }
.status.run i { background:var(--run); animation:pulse 2s ease-in-out infinite; } .status.run { color:var(--run); }
.status.alarm i { background:var(--alarm); } .status.alarm { color:var(--alarm); }
.status.wait i { background:var(--caution); } .status.wait { color:var(--caution); }
@keyframes pulse { 50% { opacity:.35; } }
@media (prefers-reduced-motion: reduce) { .status.run i { animation:none; } }
.bigbtn { padding:10px 22px; font-size:15px; font-weight:600; border-radius:var(--radius-m); }
.alert { margin-top:16px; padding:12px 14px; border-radius:var(--radius-m); display:flex; gap:12px; align-items:flex-start; }
.alert.alarm { background:var(--alarmbg); } .alert.caution { background:var(--cautionbg); }
.alert .text { flex:1; } .alert .actions { display:flex; gap:6px; }
section { margin-top:28px; }
section h2 { font:600 15px var(--font-display); margin:0 0 10px; display:flex; align-items:baseline; gap:10px; }
section h2 .aside { font:400 12.5px var(--font); color:var(--ink3); }
dl.kv { display:grid; grid-template-columns:150px 1fr; gap:7px 16px; margin:0; }
dl.kv dt { color:var(--ink2); } dl.kv dd { margin:0; min-width:0; overflow-wrap:anywhere; }
.timeline { list-style:none; margin:0; padding:0; }
.timeline li { display:grid; grid-template-columns:130px 140px 1fr; gap:12px; padding:7px 0; border-bottom:1px solid var(--line); font-size:13px; }
.timeline li:last-child { border-bottom:0; }
.timeline .flag { color:var(--caution); font-weight:600; }
.timeline.log li { grid-template-columns:150px 1fr; }
.timeline.log .details { display:block; margin-top:1px; }
.tools { display:flex; gap:8px; flex-wrap:wrap; margin-top:28px; padding-top:18px; border-top:1px solid var(--line); }
.notes { white-space:pre-wrap; color:var(--ink); }

/* диалоги и уведомления */
dialog { border:1px solid var(--line); border-radius:14px; padding:0; width:min(560px, 94vw); background:var(--panel); color:var(--ink);
         box-shadow:0 24px 60px rgba(20,25,40,.25); }
dialog::backdrop { background:rgba(15,18,28,.4); }
.dlg { padding:22px 24px; }
.dlg h3 { font:600 18px var(--font-display); margin:0 0 6px; }
.dlg label { display:block; font-size:12.5px; color:var(--ink2); margin:14px 0 5px; }
.dlg .btns { display:flex; gap:8px; justify-content:flex-end; margin-top:20px; }
.dlg .hint { font-size:12.5px; color:var(--ink3); margin-top:5px; }
.inline { display:flex; gap:6px; }
.inline input { flex:1; }
#checkResult { font-size:13px; margin-top:7px; }
.ok { color:var(--run); } .bad { color:var(--alarm); }
#toasts { position:fixed; right:16px; bottom:16px; display:flex; flex-direction:column; gap:8px; z-index:20; max-width:420px; }
.toast { background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--brand); border-radius:var(--radius-s);
         padding:10px 13px; box-shadow:0 8px 24px rgba(20,25,40,.18); font-size:13px; cursor:pointer; }
.toast.err { border-left-color:var(--alarm); } .toast.ok { border-left-color:var(--run); }
@media (max-width: 860px) { .main { grid-template-columns:1fr; } .detail { display:none; } .main.show-detail .list { display:none; }
  .main.show-detail .detail { display:block; } .search { width:auto; flex:1; } }
</style></head>
<body>
<header>
  <div class="brand"><svg viewBox="0 0 32 32" aria-hidden="true"><defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#286eff"/><stop offset="1" stop-color="#6e3ceb"/></linearGradient></defs>
    <rect x="1" y="1" width="30" height="30" rx="7.5" fill="url(#bg)"/>
    <path d="M10.5 10.5l11 11M21.5 10.5l-11 11" stroke="#fff" stroke-width="4.4" stroke-linecap="round"/></svg>XLauncher</div>
  <div class="search"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="7" cy="7" r="5"/><path d="M11 11l3.5 3.5"/></svg>
    <input id="q" type="search" placeholder="Найти профиль, клиента, IP" aria-label="Поиск профилей"></div>
  <span class="spacer"></span>
  <span id="updState"></span>
  <button class="ghost" onclick="checkUpdates()" title="Проверить обновления">Обновления</button>
  <button class="ghost" onclick="startAudit()" title="Проверить, не совпадают ли отпечатки профилей">Отпечатки</button>
  <button class="ghost" onclick="openSettings()">Настройки</button>
  <button class="primary" onclick="openForm()">Новый профиль</button>
</header>
<div id="banners"></div>
<div class="main" id="main">
  <div class="list">
    <div class="filters" role="group" aria-label="Фильтр">
      <button data-f="all" aria-pressed="true">Все<span class="count" id="c_all"></span></button>
      <button data-f="run" aria-pressed="false">Запущены<span class="count" id="c_run"></span></button>
      <button data-f="alarm" aria-pressed="false">Требуют внимания<span class="count" id="c_alarm"></span></button>
    </div>
    <div id="rows" tabindex="0" aria-label="Профили"></div>
  </div>
  <div class="detail" id="detail"></div>
</div>

<dialog id="form"><form class="dlg" method="dialog" onsubmit="return false">
  <h3 id="formTitle">Новый профиль</h3>
  <label for="f_name">Имя</label><input id="f_name" placeholder="Например: Анатолий — Instagram" required>
  <label for="f_group">Клиент</label><input id="f_group" list="groups" placeholder="Группа в списке"><datalist id="groups"></datalist>
  <label for="f_proxy">Прокси</label>
  <div class="inline"><input id="f_proxy" placeholder="socks5://user:pass@host:port" autocomplete="off" spellcheck="false">
    <button type="button" onclick="checkProxy()">Проверить</button></div>
  <div class="hint">Также подходят форматы host:port:user:pass и http://user:pass@host:port</div>
  <div id="checkResult"></div>
  <label for="f_urls">Открывать при запуске</label><textarea id="f_urls" placeholder="https://www.instagram.com/"></textarea>
  <div class="hint">По одному адресу в строке</div>
  <label for="f_notes">Заметки</label><textarea id="f_notes" placeholder="Логин, статус прогрева, что важно помнить"></textarea>
  <div class="btns"><button type="button" onclick="$('form').close()">Отмена</button>
    <button type="button" class="primary" id="f_save" onclick="saveForm()">Создать профиль</button></div>
</form></dialog>

<dialog id="settings"><div class="dlg">
  <h3>Настройки</h3>
  <label for="s_monitor">Проверять IP открытого профиля каждые, минут</label><input id="s_monitor" type="number" min="1" max="120">
  <section style="margin-top:20px"><dl class="kv small" id="s_info"></dl></section>
  <div class="btns"><button type="button" onclick="api('POST','/api/open-root').catch(e => toast(e.message))">Открыть папку лаунчера</button><span class="spacer"></span>
    <button type="button" onclick="$('settings').close()">Закрыть</button><button type="button" class="primary" onclick="saveSettings()">Сохранить</button></div>
</div></dialog>

<dialog id="ask"><form class="dlg" method="dialog" onsubmit="return false">
  <h3 id="askTitle"></h3><div id="askText" class="muted"></div>
  <input id="askInput" style="margin-top:14px; display:none">
  <div class="btns"><button type="button" id="askNo">Отмена</button><button type="button" class="primary" id="askYes">ОК</button></div>
</form></dialog>
<div id="toasts" aria-live="polite"></div>

<script>
const $ = id => document.getElementById(id);
// Перерисовываем только при изменениях: иначе сбиваются фокус, выделение и прокрутка.
const setHTML = (el, html) => { if (el._html !== html) { el._html = html; el.innerHTML = html; } };
const cap = s => { s = String(s ?? ''); return s.charAt(0).toUpperCase() + s.slice(1); };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(method, url, body) {
  const r = await fetch(url, {method, cache:'no-store', headers:{'Content-Type':'application/json'}, body: body ? JSON.stringify(body) : undefined});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || ('HTTP ' + r.status));
  return data;
}

// ---------- состояние
let profiles = [], dupes = [], status = {}, selected = null, filter = 'all', query = '', auditDismissed = false, editing = null;
let detailCache = {id: null, history: [], log: [], at: 0};

// ---------- глиф отпечатка: кольца с разрывами из seed
const glyphCache = {};
function glyph(seed, size = 32) {
  const key = seed + ':' + size;
  if (glyphCache[key]) return glyphCache[key];
  let x = (Number(seed) >>> 0) || 1;
  const rnd = () => { x ^= x << 13; x >>>= 0; x ^= x >>> 17; x ^= x << 5; x >>>= 0; return x / 4294967296; };
  const hue = Math.floor(rnd() * 360), c = 16;
  let paths = '';
  for (let i = 0; i < 5; i++) {
    const r = 3.2 + i * 2.55, start = rnd() * Math.PI * 2, gap = 0.5 + rnd() * 1.4;
    const a1 = start + gap, a2 = start + Math.PI * 2 - 0.0001;
    const p = (a) => (c + r * Math.cos(a)).toFixed(2) + ' ' + (c + r * Math.sin(a)).toFixed(2);
    const large = (a2 - a1) > Math.PI ? 1 : 0;
    paths += `<path d="M${p(a1)} A${r} ${r} 0 ${large} 1 ${p(a2)}" />`;
  }
  const svg = `<svg width="${size}" height="${size}" viewBox="0 0 32 32" aria-hidden="true">
    <circle cx="16" cy="16" r="15.5" fill="hsl(${hue} 70% 94%)"/>
    <g fill="none" stroke="hsl(${hue} 62% 42%)" stroke-width="1.55" stroke-linecap="round">${paths}</g>
    <circle cx="16" cy="16" r="1.4" fill="hsl(${hue} 62% 42%)"/></svg>`;
  return glyphCache[key] = svg;
}

// ---------- статусы
function view(p) {
  const st = p.state || {};
  const s = st.status;
  if (s === 'running') return {cls:'run', text: st.warning ? 'запущен, есть предупреждение' : 'запущен', active:true, alarm: !!st.warning};
  if (s === 'starting') return {cls:'wait', text:'запускается', active:true};
  if (s === 'checking') return {cls:'wait', text:'проверяю прокси', active:true};
  if (s === 'stopping') return {cls:'wait', text:'закрывается', active:true};
  if (s === 'needs_confirmation') return {cls:'alarm', text:'сменилась страна IP', alarm:true};
  if (s === 'error') return {cls:'alarm', text:'не запустился', alarm:true};
  if (s === 'crashed') return {cls:'alarm', text:'аварийно завершился', alarm:true};
  if (p.duplicate) return {cls:'alarm', text:'есть копия с тем же id', alarm:true};
  if (!p.proxy_masked) return {cls:'alarm', text:'не задан прокси', alarm:true};
  return {cls:'', text:'не запущен'};
}
const since = ts => { if (!ts) return ''; const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 1 ? 'меньше минуты' : m < 60 ? `${m} мин` : `${Math.floor(m / 60)} ч ${m % 60} мин`; };

// ---------- список
function matches(p) {
  if (filter === 'run' && !view(p).active) return false;
  if (filter === 'alarm' && !view(p).alarm) return false;
  if (!query) return true;
  const ip = (p.state && p.state.ip) || (p.last_ip && p.last_ip.ip) || '';
  return [p.name, p.group, ip, p.proxy_masked, p.notes].join(' ').toLowerCase().includes(query);
}
function renderList() {
  $('c_all').textContent = profiles.length || '';
  $('c_run').textContent = profiles.filter(p => view(p).active).length || '';
  $('c_alarm').textContent = profiles.filter(p => view(p).alarm).length || '';
  const shown = profiles.filter(matches);
  if (!profiles.length) { setHTML($('rows'), '<div class="empty">Профилей пока нет.<br><br><button class="primary" onclick="openForm()">Создать первый профиль</button></div>'); return; }
  if (!shown.length) { setHTML($('rows'), '<div class="empty">Ничего не найдено. Измени запрос или фильтр.</div>'); return; }
  const groups = {};
  for (const p of shown) (groups[p.group || 'Без клиента'] ||= []).push(p);
  let html = '';
  for (const g of Object.keys(groups).sort((a, b) => a.localeCompare(b, 'ru'))) {
    html += `<div class="group">${esc(g)}<span class="n">${groups[g].length}</span></div>`;
    for (const p of groups[g].sort((a, b) => a.name.localeCompare(b.name, 'ru'))) {
      const v = view(p), st = p.state || {};
      const ip = st.status === 'running' ? st.ip : (p.last_ip && p.last_ip.ip);
      const cc = st.status === 'running' ? (st.geo || {}).country : (p.last_ip || {}).country;
      const sub = v.alarm ? `<span class="alarmtxt">${esc(v.text)}</span>`
                : v.active ? `<span class="${v.cls === 'run' ? 'runtxt' : 'waittxt'}">${esc(v.text)}</span> <span class="num">${esc(ip || '')}</span>`
                : ip ? `<span class="num">${esc(ip)}</span> ${esc(cc || '')}` : '<span class="faint">ещё не запускался</span>';
      const btn = v.active
        ? `<button class="go stop ghost" title="Остановить" onclick="event.stopPropagation(); act('${p.id}','stop')"><svg viewBox="0 0 14 14"><rect x="2" y="2" width="10" height="10" rx="1.5" fill="currentColor"/></svg></button>`
        : `<button class="go ghost" title="Запустить" ${p.duplicate ? 'disabled' : ''} onclick="event.stopPropagation(); act('${p.id}','start')"><svg viewBox="0 0 14 14"><path d="M3.5 2l8.5 5-8.5 5z" fill="currentColor"/></svg></button>`;
      html += `<div class="row st-${v.cls || 'none'} ${p.id === selected ? 'sel' : ''}" data-id="${p.id}" onclick="select('${p.id}')">
        ${glyph(p.fingerprint.seed)}<div style="min-width:0"><div class="name">${esc(p.name)}</div><div class="sub">${sub}</div></div>${btn}</div>`;
    }
  }
  setHTML($('rows'), html);
}

// ---------- карточка
async function loadDetailExtras(id, force) {
  if (!force && detailCache.id === id && Date.now() - detailCache.at < 10000) return;
  try {
    const [h, l] = await Promise.all([api('GET', `/api/profiles/${id}/ip-history`), api('GET', `/api/profiles/${id}/launch-log`)]);
    detailCache = {id, history: h.history.reverse(), log: l.log, at: Date.now()};
    if (!document.querySelector('dialog[open]')) renderDetail();
  } catch (e) {}
}
function renderDetail() {
  const p = profiles.find(x => x.id === selected);
  if (!p) { setHTML($('detail'), profiles.length ? '<div class="empty">Выбери профиль слева</div>' : ''); return; }
  const v = view(p), st = p.state || {}, fp = p.fingerprint || {}, pr = fp.probe;
  const geo = st.status === 'running' ? (st.geo || {}) : (p.last_ip || {});
  const ip = st.status === 'running' ? st.ip : (p.last_ip && p.last_ip.ip);
  let alert = '';
  if (st.status === 'error' || st.status === 'crashed')
    alert = `<div class="alert alarm"><div class="text">${esc(cap(st.error))}</div><div class="actions"><button onclick="act('${p.id}','dismiss')">Понятно</button></div></div>`;
  else if (st.status === 'needs_confirmation')
    alert = `<div class="alert caution"><div class="text"><b>${esc(cap(st.error))}.</b><br>Площадки замечают вход из другой страны. Запускай, только если смена прокси была намеренной.</div>
      <div class="actions"><button onclick="act('${p.id}','dismiss')">Не запускать</button><button class="primary" onclick="act('${p.id}','start',{confirm_country:true})">Запустить</button></div></div>`;
  else if (st.warning)
    alert = `<div class="alert caution"><div class="text">${esc(cap(st.warning))}</div></div>`;
  if (p.duplicate)
    alert += `<div class="alert alarm"><div class="text">У этого профиля есть копия с тем же id. Реши, что это за копия: подсказка вверху окна.</div></div>`;
  const big = st.status === 'needs_confirmation' ? '<span></span>'
    : v.active
    ? `<button class="bigbtn" onclick="act('${p.id}','stop')" ${st.status === 'stopping' ? 'disabled' : ''}>Остановить</button>`
    : `<button class="bigbtn primary" onclick="act('${p.id}','start')" ${p.duplicate ? 'disabled' : ''}>Запустить</button>`;
  const statusLine = st.status === 'running' ? `${v.text}, работает ${since(st.started)}${st.agent ? ', управляет агент' : ''}` : v.text;
  const hist = detailCache.id === p.id ? detailCache.history : [];
  const log = detailCache.id === p.id ? detailCache.log : [];
  const SRC = {launch:'при запуске', monitor:'во время работы', create:'при создании'};
  setHTML($('detail'), `<div class="inner">
    <div class="dhead">${glyph(fp.seed, 72)}<div><h1>${esc(p.name)}</h1>
      <div class="group-name">${esc(p.group || 'Без клиента')}</div>
      <div class="status ${v.cls}"><i></i>${esc(statusLine)}</div></div>${big}</div>
    ${alert}
    <section><h2>Сеть</h2><dl class="kv">
      <dt>Прокси</dt><dd class="num">${p.proxy_masked ? esc(p.proxy_masked) : '<span class="bad">не задан, профиль не запустится</span>'}</dd>
      <dt>Внешний IP</dt><dd class="num">${ip ? esc(ip) : '<span class="faint">появится после запуска</span>'}</dd>
      <dt>Где</dt><dd>${esc([geo.country, geo.city].filter(Boolean).join(', ')) || '—'}${geo.asn ? `<div class="small muted">${esc(geo.asn)}</div>` : ''}</dd>
      ${st.status === 'running' ? `<dt>Часовой пояс</dt><dd>${esc(st.timezone || '—')}</dd><dt>IP проверен</dt><dd>${since(st.last_ip_check)} назад</dd>` : ''}
    </dl></section>
    <section><h2>Отпечаток</h2><dl class="kv">
      <dt>Язык</dt><dd>${esc(fp.locale)} <span class="faint small">${esc(fp.accept_language)}</span></dd>
      <dt>Процессор</dt><dd>${esc(fp.hardware_concurrency)} ядер</dd>
      <dt>Часовой пояс</dt><dd>${fp.timezone === 'auto' ? 'по IP прокси' : esc(fp.timezone)}</dd>
      <dt>Видеокарта</dt><dd class="small">${pr ? esc(pr.webgl) : '<span class="faint">не проверялась</span>'}</dd>
      <dt>Уникальность</dt><dd>${pr ? 'проверена на этом компьютере' : '<span class="faint">не проверялась, нажми «Отпечатки» в шапке</span>'}</dd>
      <dt>Seed</dt><dd class="num faint">${esc(fp.seed)}</dd>
    </dl></section>
    <section><h2>История IP<span class="aside">новая строка появляется, только когда IP сменился</span></h2>
      ${hist.length ? `<ul class="timeline">${hist.map(r => `<li><span class="num muted">${esc(r.time)}</span><span class="num">${esc(r.ip)}</span>
        <span>${esc([r.country, r.city].filter(Boolean).join(', '))} <span class="faint">${esc(SRC[r.source] || r.source)}</span>
        ${r.flag ? '<span class="flag">смена страны</span>' : ''}</span></li>`).join('')}</ul>` : '<div class="faint">Пока пусто</div>'}</section>
    <section><h2>Журнал запусков</h2>
      ${log.length ? `<ul class="timeline log">${log.slice(0, 15).map(r => `<li><span class="num muted">${esc(r.time)}</span><span>${esc(r.event)}
        ${r.details.filter(Boolean).length ? `<span class="details small muted num">${esc(r.details.filter(Boolean).join('   '))}</span>` : ''}</span></li>`).join('')}</ul>`
        : '<div class="faint">Профиль ещё не запускался</div>'}</section>
    ${p.notes ? `<section><h2>Заметки</h2><div class="notes">${esc(p.notes)}</div></section>` : ''}
    ${(p.start_urls || []).length ? `<section><h2>Открывать при запуске</h2><div class="small">${p.start_urls.map(esc).join('<br>')}</div></section>` : ''}
    <div class="tools">
      <button onclick="openForm('${p.id}')" ${v.active ? 'disabled title="Сначала останови профиль"' : ''}>Изменить</button>
      <button onclick="cloneP('${p.id}')" ${p.duplicate ? 'disabled' : ''}>Клонировать</button>
      <button onclick="api('POST','/api/profiles/${p.id}/open-folder').catch(e => toast(e.message))">Открыть папку</button>
      <span class="spacer"></span>
      <button class="danger" onclick="delP('${p.id}')" ${v.active ? 'disabled title="Сначала останови профиль"' : ''}>Удалить</button>
    </div></div>`);
}
function select(id) {
  selected = id; renderList(); renderDetail(); loadDetailExtras(id, true);
  $('main').classList.add('show-detail');
}

// ---------- баннеры
function renderBanners() {
  const s = status, e = s.engine || {}, au = s.audit || {};
  let b = '';
  if (s._connError) b += `<div class="banner alarm"><div class="text">Нет связи с лаунчером: ${esc(s._connError)}. Данные на экране могут быть устаревшими.</div></div>`;
  if (e.version && !e.installed) {
    const ins = e.installer || {};
    if (ins.status === 'downloading') {
      const pct = ins.total_mb ? Math.round(100 * ins.done_mb / ins.total_mb) : 0;
      b += `<div class="banner"><div class="text">Скачиваю браузер ${esc(e.version)}: ${ins.done_mb} из ${ins.total_mb ?? '?'} МБ
        <div class="progress"><i style="width:${pct}%"></i></div></div></div>`;
    } else if (ins.status === 'error')
      b += `<div class="banner alarm"><div class="text">Браузер не скачался: ${esc(ins.error)}</div><div class="actions"><button onclick="installEngine()">Повторить</button></div></div>`;
    else
      b += `<div class="banner caution"><div class="text">Чтобы запускать профили, нужен браузер ${esc(e.version)}, около 190 МБ.</div><div class="actions"><button class="primary" onclick="installEngine()">Скачать браузер</button></div></div>`;
  }
  if (au.status === 'running')
    b += `<div class="banner"><div class="text">Проверяю отпечатки: ${au.done} из ${au.total || '…'}<div class="progress"><i style="width:${au.total ? Math.round(100 * au.done / au.total) : 0}%"></i></div></div></div>`;
  else if (au.status === 'error')
    b += `<div class="banner alarm"><div class="text">Проверка отпечатков не удалась: ${esc(au.error)}</div></div>`;
  else if (au.status === 'done' && !auditDismissed) {
    const issues = au.collisions.map(c => `${c.kind === 'audio' ? 'Аудио' : 'Canvas'} совпадает: ${c.profiles.map(esc).join(', ')}`);
    if (au.baseline_hits.length) issues.push('Как у браузера без подмены: ' + au.baseline_hits.map(esc).join(', '));
    b += issues.length
      ? `<div class="banner caution"><div class="text"><b>Найдены совпадения отпечатков</b> среди ${au.checked} профилей<br>${issues.join('<br>')}
          <div class="small muted" style="margin-top:4px">Лаунчер не меняет отпечатки используемых профилей. Если в профиле ещё не входили в аккаунты, проще удалить его и создать заново.</div></div>
          <div class="actions"><button onclick="auditDismissed=true;renderBanners()">Скрыть</button></div></div>`
      : `<div class="banner"><div class="text">Отпечатки всех ${au.checked} профилей различаются.</div><div class="actions"><button onclick="auditDismissed=true;renderBanners()">Скрыть</button></div></div>`;
  }
  for (const d of dupes)
    b += `<div class="banner alarm"><div class="text"><b>Две папки с одним и тем же профилем:</b> ${d.slugs.map(x => esc(x)).join(' и ')}.<br>
      Если это перенос, удали лишнюю папку в profiles. Если копию сделали ради нового профиля, нажми у неё кнопку: ей выдадут новый отпечаток и очистят куки.</div>
      <div class="actions">${d.slugs.map(x => `<button data-slug="${esc(x)}" onclick="newIdentity(this.dataset.slug)">Сделать «${esc(x)}» новым профилем</button>`).join('')}</div></div>`;
  setHTML($('banners'), b);
}

// ---------- загрузка
async function load() {
  try {
    const [s, l] = await Promise.all([api('GET', '/api/status'), api('GET', '/api/profiles')]);
    status = s; profiles = l.profiles; dupes = l.duplicates;
    const u = s.updater || {};
    $('updState').textContent = u.last_error ? 'обновления: ошибка проверки' : `версия ${s.version}`;
    $('updState').title = u.last_error || (u.last_check ? 'Последняя проверка обновлений: ' + u.last_check : '');
    setHTML($('groups'), [...new Set(profiles.map(p => p.group).filter(Boolean))].map(g => `<option value="${esc(g)}">`).join(''));
    if (selected && !profiles.find(p => p.id === selected)) selected = null;
    if (!selected && profiles.length) { selected = profiles.slice().sort((a, b) => a.name.localeCompare(b.name, 'ru'))[0].id; loadDetailExtras(selected, true); }
  } catch (e) { status._connError = e.message; }
  renderBanners(); renderList();
  if (!document.querySelector('dialog[open]')) renderDetail();
  if (selected) loadDetailExtras(selected, false);
}

// ---------- действия
function toast(msg, kind = 'err', ms = 7000) {
  const t = document.createElement('div'); t.className = 'toast ' + kind; t.textContent = msg;
  t.onclick = () => t.remove(); $('toasts').appendChild(t); setTimeout(() => t.remove(), ms);
}
function ask(title, text, {input = null, ok = 'ОК', danger = false} = {}) {
  return new Promise(resolve => {
    $('askTitle').textContent = title; $('askText').textContent = text || '';
    const inp = $('askInput'); inp.style.display = input === null ? 'none' : 'block'; inp.value = input ?? '';
    const yes = $('askYes'); yes.textContent = ok; yes.className = danger ? 'danger' : 'primary';
    const done = v => { $('ask').close(); resolve(v); };
    yes.onclick = () => done(input === null ? true : inp.value);
    $('askNo').onclick = () => done(input === null ? false : null);
    inp.onkeydown = e => { if (e.key === 'Enter') yes.onclick(); };
    $('ask').showModal(); if (input !== null) inp.select();
  });
}
async function act(id, what, body) {
  try { const r = await api('POST', `/api/profiles/${id}/${what}`, body || {}); if (r.ok === false && r.error) toast(r.error); }
  catch (e) { toast(e.message); }
  load(); setTimeout(() => loadDetailExtras(id, true), 1500);
}
function uniqueToast(u) {
  if (!u) return;
  if (!u.checked) toast('Отпечаток не проверен: ' + u.reason, 'err', 9000);
  else if (u.unique) toast(`Отпечаток уникален среди ${u.compared_with} профилей`, 'ok', 4000);
  else toast('Полностью уникальный отпечаток подобрать не удалось: ' + u.issues.join('; '), 'err', 12000);
}
async function installEngine() { try { await api('POST', '/api/engine/install'); } catch (e) { toast(e.message); } load(); }
async function checkUpdates() {
  try { await api('POST', '/api/update/check'); toast('Проверяю обновления. Если найдётся новая версия, окно перезапустится само.', 'ok', 5000); }
  catch (e) { toast(e.message); }
}
async function startAudit() {
  auditDismissed = false;
  try { await api('POST', '/api/fingerprints/audit'); } catch (e) { toast(e.message); }
  load();
}
async function newIdentity(slug) {
  if (!await ask(`Сделать «${slug}» новым профилем?`, 'Копии выдадут новый отпечаток, куки и история будут удалены.', {ok:'Сделать новым', danger:true})) return;
  try { const r = await api('POST', `/api/duplicates/${encodeURIComponent(slug)}/new-identity`); uniqueToast(r.uniqueness); } catch (e) { toast(e.message); }
  load();
}
async function cloneP(id) {
  const p = profiles.find(x => x.id === id);
  const name = await ask('Клонировать профиль', 'У копии будет новый отпечаток, без кук и истории. Расширения и закладки сохранятся.', {input:(p?.name || '') + ' (копия)', ok:'Клонировать'});
  if (name === null) return;
  toast('Создаю копию и подбираю отпечаток…', 'ok', 4000);
  try { const r = await api('POST', `/api/profiles/${id}/clone`, {name}); uniqueToast(r.uniqueness); selected = r.profile.id; } catch (e) { toast(e.message); }
  load(); if (selected) loadDetailExtras(selected, true);
}
async function delP(id) {
  const p = profiles.find(x => x.id === id);
  if (!await ask(`Удалить «${p?.name}»?`, 'Папка профиля переедет в backups, её можно вернуть вручную.', {ok:'Удалить', danger:true})) return;
  try { await api('DELETE', `/api/profiles/${id}`); toast('Профиль перенесён в backups', 'ok'); selected = null; } catch (e) { toast(e.message); }
  load();
}
async function openForm(id) {
  editing = id || null;
  $('formTitle').textContent = id ? 'Изменить профиль' : 'Новый профиль';
  $('f_save').textContent = id ? 'Сохранить' : 'Создать профиль';
  $('checkResult').textContent = '';
  for (const f of ['name', 'group', 'proxy', 'urls', 'notes']) $('f_' + f).value = '';
  if (id) {
    const p = await api('GET', `/api/profiles/${id}?with_proxy=true`);
    $('f_name').value = p.name; $('f_group').value = p.group || ''; $('f_proxy').value = p.proxy || '';
    $('f_urls').value = (p.start_urls || []).join('\n'); $('f_notes').value = p.notes || '';
  } else {
    const cur = profiles.find(x => x.id === selected); if (cur && cur.group) $('f_group').value = cur.group;
  }
  $('form').showModal(); $('f_name').focus();
}
async function checkProxy() {
  $('checkResult').innerHTML = '<span class="muted">Проверяю…</span>';
  try {
    const r = await api('POST', '/api/proxy/check', {proxy: $('f_proxy').value});
    $('checkResult').innerHTML = r.ok
      ? `<span class="ok">Работает: <span class="num">${esc(r.ip)}</span>, ${esc([r.geo.country, r.geo.city].filter(Boolean).join(', '))}, ${esc(r.geo.timezone)}, ${r.latency_s} с</span>`
      : `<span class="bad">${esc(r.error)}</span>`;
  } catch (e) { $('checkResult').innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
}
async function saveForm() {
  const body = {name:$('f_name').value.trim(), group:$('f_group').value.trim(), proxy:$('f_proxy').value.trim(),
    start_urls:$('f_urls').value.split('\n').map(s => s.trim()).filter(Boolean), notes:$('f_notes').value};
  if (!body.name) { toast('Укажи имя профиля'); $('f_name').focus(); return; }
  const btn = $('f_save'); btn.disabled = true;
  try {
    if (editing) { await api('PATCH', `/api/profiles/${editing}`, body); toast('Сохранено', 'ok', 3000); }
    else {
      btn.textContent = 'Подбираю отпечаток…';
      const r = await api('POST', '/api/profiles', body);
      if (r.check && !r.check.ok) toast('Профиль создан, но прокси не прошёл проверку: ' + r.check.error, 'err', 12000);
      uniqueToast(r.uniqueness); selected = r.profile.id;
    }
    $('form').close();
  } catch (e) { toast(e.message); }
  btn.disabled = false; btn.textContent = editing ? 'Сохранить' : 'Создать профиль';
  load(); if (selected) loadDetailExtras(selected, true);
}
async function openSettings() {
  const st = await api('GET', '/api/settings');
  $('s_monitor').value = st.monitor_interval_min;
  const e = status.engine || {};
  $('s_info').innerHTML = `<dt>Папка</dt><dd>${esc(status.root)}</dd><dt>Версия</dt><dd class="num">${esc(status.version)}</dd>
    <dt>Загрузчик</dt><dd>${esc(status.bootstrap_version ?? '—')}</dd><dt>Браузер</dt><dd class="num">${esc(e.version)} ${e.installed ? '' : '(не скачан)'}</dd>
    <dt>Обновления</dt><dd>${esc((status.updater || {}).last_check || '—')}</dd>`;
  $('settings').showModal();
}
async function saveSettings() {
  try { await api('PATCH', '/api/settings', {monitor_interval_min: Number($('s_monitor').value)}); toast('Настройки сохранены', 'ok', 3000); $('settings').close(); }
  catch (e) { toast(e.message); }
}

// ---------- клавиатура и фильтры
document.querySelectorAll('.filters button').forEach(b => b.onclick = () => {
  filter = b.dataset.f; document.querySelectorAll('.filters button').forEach(x => x.setAttribute('aria-pressed', x === b)); renderList();
});
$('q').oninput = () => { query = $('q').value.trim().toLowerCase(); renderList(); };
document.addEventListener('keydown', e => {
  if (document.querySelector('dialog[open]')) return;
  const typing = ['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName);
  if (e.key === '/' && !typing) { e.preventDefault(); $('q').focus(); return; }
  if (e.key === 'Escape' && typing) { document.activeElement.blur(); return; }
  if (typing && !['ArrowDown', 'ArrowUp', 'Enter'].includes(e.key)) return;
  const ids = [...document.querySelectorAll('#rows .row')].map(r => r.dataset.id);
  const i = ids.indexOf(selected);
  if (e.key === 'ArrowDown' && ids.length) { e.preventDefault(); select(ids[Math.min(ids.length - 1, i + 1)]); scrollSel(); }
  else if (e.key === 'ArrowUp' && ids.length) { e.preventDefault(); select(ids[Math.max(0, i - 1)]); scrollSel(); }
  else if (e.key === 'Enter' && selected && document.activeElement.tagName !== 'BUTTON') {
    const p = profiles.find(x => x.id === selected); if (!p) return;
    e.preventDefault(); act(p.id, view(p).active ? 'stop' : 'start');
  }
});
function scrollSel() { const r = document.querySelector('#rows .row.sel'); if (r) r.scrollIntoView({block:'nearest'}); }

load(); setInterval(load, 2000);
window.addEventListener('focus', load);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
setInterval(() => fetch('/api/ui-ping', {method:'POST', cache:'no-store'}).catch(() => {}), 5000);
</script></body></html>
"""
