"""Страница интерфейса (этап 3 — рабочий минимум; полноценный интерфейс — этап 4)."""

INDEX_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>xlauncher</title>
<style>
  :root { color-scheme: light dark; --fg:#1d1d1f; --mut:#6e6e73; --bg:#f5f5f7; --card:#fff; --line:#e5e5ea;
          --acc:#0a66ff; --ok:#1a7f37; --warn:#9a6700; --warnbg:#fff8c5; --err:#cf222e; --errbg:#ffebe9; }
  @media (prefers-color-scheme: dark) { :root { --fg:#f2f2f7; --mut:#98989f; --bg:#111113; --card:#1c1c1f; --line:#2c2c30;
          --warnbg:#3a2f00; --errbg:#3d1214; --ok:#3fb950; --warn:#d29922; --err:#f85149; } }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.45 "Segoe UI", system-ui, sans-serif; color:var(--fg); background:var(--bg); }
  header { display:flex; align-items:center; gap:12px; padding:14px 20px; border-bottom:1px solid var(--line); background:var(--card); position:sticky; top:0; z-index:5; }
  header h1 { font-size:17px; margin:0; } .mut { color:var(--mut); } .sp { flex:1; }
  main { padding:16px 20px 40px; max-width:1100px; margin:0 auto; }
  button { font:inherit; padding:6px 12px; border-radius:7px; border:1px solid var(--line); background:var(--card); color:var(--fg); cursor:pointer; }
  button:hover { border-color:var(--acc); } button.pri { background:var(--acc); border-color:var(--acc); color:#fff; }
  button.danger { color:var(--err); } button:disabled { opacity:.5; cursor:default; }
  .banner { padding:10px 14px; border-radius:9px; margin-bottom:12px; border:1px solid var(--line); background:var(--card); }
  .banner.warn { background:var(--warnbg); } .banner.err { background:var(--errbg); }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--mut); margin:22px 0 8px; }
  .row { display:grid; grid-template-columns: 1.4fr 1.3fr 1.4fr 120px auto; gap:12px; align-items:center;
         background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; margin-bottom:6px; }
  .row .name { font-weight:600; } .small { font-size:12px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:20px; font-size:12px; border:1px solid var(--line); white-space:nowrap; }
  .badge.run { color:var(--ok); border-color:var(--ok); } .badge.err { color:var(--err); border-color:var(--err); }
  .badge.wait { color:var(--warn); border-color:var(--warn); }
  .acts { display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
  .alert { grid-column:1/-1; padding:8px 10px; border-radius:7px; font-size:13px; display:flex; gap:10px; align-items:center; }
  .alert.err { background:var(--errbg); } .alert.warn { background:var(--warnbg); }
  dialog { border:1px solid var(--line); border-radius:12px; padding:0; width:min(560px, 92vw); background:var(--card); color:var(--fg); }
  dialog::backdrop { background:rgba(0,0,0,.35); }
  .dlg { padding:18px 20px; } .dlg h3 { margin:0 0 14px; font-size:16px; }
  label { display:block; font-size:12px; color:var(--mut); margin:10px 0 4px; }
  input, textarea { width:100%; font:inherit; padding:7px 9px; border-radius:7px; border:1px solid var(--line); background:var(--bg); color:var(--fg); }
  textarea { min-height:60px; resize:vertical; }
  .dlg .btns { display:flex; gap:8px; justify-content:flex-end; margin-top:16px; }
  table { width:100%; border-collapse:collapse; font-size:13px; } td, th { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); }
  .empty { text-align:center; padding:50px 0; color:var(--mut); }
  #toasts { position:fixed; right:16px; bottom:16px; display:flex; flex-direction:column; gap:8px; z-index:50; max-width:420px; }
  .toast { background:var(--card); border:1px solid var(--line); border-left:4px solid var(--acc); border-radius:8px; padding:10px 12px;
           box-shadow:0 4px 16px rgba(0,0,0,.15); font-size:13px; cursor:pointer; }
  .toast.err { border-left-color:var(--err); } .toast.ok { border-left-color:var(--ok); }
</style></head>
<body>
<header>
  <h1>xlauncher</h1><span id="ver" class="mut small"></span><span class="sp"></span>
  <span id="upd" class="mut small"></span>
  <button onclick="checkUpdates()" title="Проверить обновления сейчас">⟳</button>
  <button onclick="startAudit()" title="Проверить, не совпадают ли отпечатки профилей">Проверить отпечатки</button>
  <button class="pri" onclick="openForm()">+ Новый профиль</button>
</header>
<main>
  <div id="conn" class="banner err" style="display:none"></div>
  <div id="banners"></div>
  <div id="list"></div>
</main>

<dialog id="form"><form class="dlg" method="dialog" onsubmit="return false">
  <h3 id="formTitle">Новый профиль</h3>
  <label>Имя</label><input id="f_name" required placeholder="Клиент — Instagram">
  <label>Группа (клиент)</label><input id="f_group" list="groups" placeholder="Клиент"><datalist id="groups"></datalist>
  <label>Прокси</label>
  <div style="display:flex; gap:6px"><input id="f_proxy" placeholder="socks5://user:pass@host:port или host:port:user:pass" autocomplete="off"><button type="button" onclick="checkProxy()">Проверить</button></div>
  <div id="f_check" class="small" style="margin-top:6px"></div>
  <label>Стартовые страницы (по одной в строке)</label><textarea id="f_urls" placeholder="https://www.instagram.com/"></textarea>
  <label>Заметки</label><textarea id="f_notes"></textarea>
  <div class="btns"><button type="button" onclick="form.close()">Отмена</button><button type="button" class="pri" id="f_save" onclick="saveForm()">Создать</button></div>
</form></dialog>

<dialog id="ask"><form class="dlg" method="dialog" onsubmit="return false">
  <h3 id="askTitle"></h3><div id="askText" class="small"></div>
  <input id="askInput" style="margin-top:10px; display:none">
  <div class="btns"><button type="button" id="askNo">Отмена</button><button type="button" class="pri" id="askYes">ОК</button></div>
</form></dialog>
<div id="toasts"></div>

<dialog id="hist"><div class="dlg">
  <h3 id="histTitle">История IP</h3><div id="histBody"></div>
  <div class="btns"><button onclick="hist.close()">Закрыть</button></div>
</div></dialog>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(method, url, body) {
  const r = await fetch(url, {method, cache: 'no-store', headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || ('HTTP ' + r.status));
  return data;
}
let editing = null, profilesCache = [], auditDismissed = false;

// Свои уведомления и диалоги вместо alert/confirm/prompt: системные окна во встроенном
// браузере модальны и могут открыться невидимыми, блокируя окно лаунчера.
function toast(msg, kind = 'err', ms = 7000) {
  const t = document.createElement('div');
  t.className = 'toast ' + kind; t.textContent = msg; t.onclick = () => t.remove();
  $('toasts').appendChild(t); setTimeout(() => t.remove(), ms);
}
function ask(title, text, {input = null, ok = 'ОК', danger = false} = {}) {
  return new Promise(resolve => {
    $('askTitle').textContent = title; $('askText').textContent = text || '';
    const inp = $('askInput'); inp.style.display = input === null ? 'none' : 'block'; inp.value = input ?? '';
    const yes = $('askYes'); yes.textContent = ok; yes.className = danger ? 'danger' : 'pri';
    const done = v => { $('ask').close(); resolve(v); };
    yes.onclick = () => done(input === null ? true : inp.value);
    $('askNo').onclick = () => done(input === null ? false : null);
    inp.onkeydown = e => { if (e.key === 'Enter') yes.onclick(); };
    $('ask').showModal(); if (input !== null) inp.focus();
  });
}

const STATUS = {
  starting: ['запуск…', 'wait'], checking: ['проверка прокси…', 'wait'], running: ['запущен', 'run'],
  stopping: ['закрывается…', 'wait'], error: ['ошибка', 'err'], crashed: ['сбой', 'err'],
  needs_confirmation: ['нужно решение', 'wait'],
};

function rowHtml(p) {
  const st = p.state || {}; const s = STATUS[st.status] || ['не запущен', ''];
  const active = ['starting','checking','running','stopping'].includes(st.status);
  const ip = st.status === 'running' ? st.ip : (p.last_ip && p.last_ip.ip);
  const geo = st.status === 'running' ? (st.geo || {}) : (p.last_ip || {});
  let alert = '';
  if (st.status === 'error' || st.status === 'crashed')
    alert = `<div class="alert err">⚠ ${esc(st.error)}<span class="sp"></span><button onclick="act('${p.id}','dismiss')">ОК</button></div>`;
  else if (st.status === 'needs_confirmation')
    alert = `<div class="alert warn">⚠ ${esc(st.error)}. Запустить с новой страной?<span class="sp"></span>
      <button onclick="act('${p.id}','start',{confirm_country:true})">Запустить</button><button onclick="act('${p.id}','dismiss')">Отмена</button></div>`;
  else if (st.warning)
    alert = `<div class="alert warn">⚠ ${esc(st.warning)}</div>`;
  if (p.duplicate) alert += `<div class="alert err">У этого профиля есть копия с тем же id — см. предупреждение вверху.</div>`;
  return `<div class="row">
    <div><div class="name">${esc(p.name)}</div><div class="mut small">${esc(p.fingerprint?.locale)} · ${esc(p.fingerprint?.hardware_concurrency)} ядер</div></div>
    <div class="small mut">${esc(p.proxy_masked) || '<span style="color:var(--err)">прокси не задан</span>'}</div>
    <div class="small">${ip ? `${esc(ip)} <span class="mut">${esc(geo.country)} ${esc(geo.city)}</span>` : '<span class="mut">—</span>'}</div>
    <div><span class="badge ${s[1]}">${s[0]}</span></div>
    <div class="acts">
      ${active ? `<button onclick="act('${p.id}','stop')">Остановить</button>`
               : `<button class="pri" ${p.duplicate ? 'disabled' : ''} onclick="act('${p.id}','start')">Запустить</button>`}
      <button onclick="showHistory('${p.id}')">IP</button>
      <button ${active ? 'disabled' : ''} onclick="openForm('${p.id}')">Изменить</button>
      <button ${p.duplicate ? 'disabled' : ''} onclick="cloneP('${p.id}')">Клон</button>
      <button class="danger" ${active ? 'disabled' : ''} onclick="delP('${p.id}')">Удалить</button>
    </div>${alert}</div>`;
}

async function load() {
  try {
    const [s, l] = await Promise.all([api('GET', '/api/status'), api('GET', '/api/profiles')]);
    $('ver').textContent = s.version;
    const u = s.updater || {};
    $('upd').textContent = u.last_error ? 'обновления: ошибка' : (u.state === 'up-to-date' ? 'актуальная версия' : (u.state || ''));
    $('upd').title = u.last_error || (u.last_check ? 'последняя проверка: ' + u.last_check : '');
    let b = '';
    const e = s.engine;
    if (!e.installed) {
      const ins = e.installer || {};
      if (ins.status === 'downloading')
        b += `<div class="banner">Скачиваю браузер ${esc(e.version)}: ${ins.done_mb} / ${ins.total_mb ?? '?'} МБ…</div>`;
      else if (ins.status === 'error')
        b += `<div class="banner err">Не удалось скачать браузер: ${esc(ins.error)} <button onclick="api('POST','/api/engine/install').then(load)">Повторить</button></div>`;
      else
        b += `<div class="banner warn">Браузер ${esc(e.version)} ещё не скачан (~190 МБ). <button class="pri" onclick="api('POST','/api/engine/install').then(load)">Скачать</button></div>`;
    }
    const au = s.audit || {};
    if (au.status === 'running')
      b += `<div class="banner">Проверяю отпечатки: ${au.done} / ${au.total || '…'}</div>`;
    else if (au.status === 'error')
      b += `<div class="banner err">Проверка отпечатков: ${esc(au.error)}</div>`;
    else if (au.status === 'done' && !auditDismissed) {
      const issues = au.collisions.map(c => `${c.kind === 'audio' ? 'аудио' : 'canvas'}: ${c.profiles.map(esc).join(', ')}`);
      if (au.baseline_hits.length) issues.push('как у браузера без подмены: ' + au.baseline_hits.map(esc).join(', '));
      b += issues.length
        ? `<div class="banner warn"><b>Найдены совпадения отпечатков</b> (проверено ${au.checked}):<br>${issues.join('<br>')}<br>
           <span class="small mut">Отпечатки уже используемых профилей лаунчер не меняет. Если в профиле ещё не входили в аккаунты — проще удалить его и создать заново.</span>
           <button onclick="auditDismissed=true;load()">Скрыть</button></div>`
        : `<div class="banner">✓ Отпечатки всех ${au.checked} профилей различаются. <button onclick="auditDismissed=true;load()">ОК</button></div>`;
    }
    for (const d of l.duplicates)
      b += `<div class="banner err"><b>Найдены копии одного профиля:</b> папки ${d.slugs.map(x => '<code>' + esc(x) + '</code>').join(', ')}.<br>
        Если это перенос — удали лишнюю папку в <code>profiles\\</code>. Если копию сделали, чтобы получить новый профиль, — нажми у неё «Это новый профиль»: ей выдадут новый отпечаток и очистят куки.<br>
        ${d.slugs.map(x => `<button data-slug="${esc(x)}" onclick="newIdentity(this.dataset.slug)">«${esc(x)}» — это новый профиль</button>`).join(' ')}</div>`;
    $('banners').innerHTML = b;
    profilesCache = l.profiles;
    $('groups').innerHTML = [...new Set(l.profiles.map(p => p.group).filter(Boolean))].map(g => `<option value="${esc(g)}">`).join('');
    if (!l.profiles.length) { $('list').innerHTML = '<div class="empty">Профилей пока нет. Нажми «+ Новый профиль».</div>'; return; }
    const groups = {};
    for (const p of l.profiles) (groups[p.group || 'Без группы'] ||= []).push(p);
    $('list').innerHTML = Object.keys(groups).sort().map(g => `<h2>${esc(g)}</h2>` + groups[g].map(rowHtml).join('')).join('');
    lastOk = Date.now(); $('conn').style.display = 'none';
  } catch (e) {
    console.error(e);
    $('conn').style.display = 'block';
    $('conn').textContent = 'Нет связи с лаунчером (' + e.message + '). Данные на экране могут быть устаревшими.';
  }
}
let lastOk = 0;

async function startAudit() {
  auditDismissed = false;
  try { await api('POST', '/api/fingerprints/audit'); toast('Проверяю отпечатки — это пара секунд на профиль', 'ok', 4000); } catch (e) { toast(e.message); }
  load();
}
function uniqueToast(u) {
  if (!u) return;
  if (!u.checked) toast('Отпечаток не проверен: ' + u.reason, 'err', 9000);
  else if (u.unique) toast(`Отпечаток уникален (сравнён с ${u.compared_with} проф., попыток: ${u.tried})`, 'ok', 5000);
  else toast('Полностью уникальный отпечаток подобрать не удалось: ' + u.issues.join('; '), 'err', 12000);
}
async function checkUpdates() {
  try { await api('POST', '/api/update/check'); toast('Проверяю обновления… Если найдётся новая версия, окно перезапустится само.', 'ok', 5000); }
  catch (e) { toast(e.message); }
}
async function act(id, what, body) {
  try { const r = await api('POST', `/api/profiles/${id}/${what}`, body || {}); if (r.ok === false && r.error) toast(r.error); }
  catch (e) { toast(e.message); }
  load();
}
async function newIdentity(slug) {
  if (!await ask('Это новый профиль?', `Профилю «${slug}» будет выдан новый отпечаток, куки и история будут удалены.`, {ok: 'Выдать новый отпечаток', danger: true})) return;
  try { const r = await api('POST', `/api/duplicates/${encodeURIComponent(slug)}/new-identity`); uniqueToast(r.uniqueness); } catch (e) { toast(e.message); }
  load();
}
async function cloneP(id) {
  const p = profilesCache.find(x => x.id === id);
  const name = await ask('Клонировать профиль', 'Копия получит новый отпечаток и не унаследует куки и историю. Расширения и закладки сохранятся.', {input: (p?.name || '') + ' (копия)', ok: 'Клонировать'});
  if (name === null) return;
  toast('Создаю клон и подбираю отпечаток…', 'ok', 4000);
  try { const r = await api('POST', `/api/profiles/${id}/clone`, {name}); uniqueToast(r.uniqueness); } catch (e) { toast(e.message); }
  load();
}
const nameOf = id => (profilesCache.find(x => x.id === id) || {}).name || '';
async function delP(id) {
  const name = nameOf(id);
  if (!await ask(`Удалить «${name}»?`, 'Папка профиля будет перенесена в backups\\ — её можно вернуть вручную.', {ok: 'Удалить', danger: true})) return;
  try { await api('DELETE', `/api/profiles/${id}`); toast('Профиль перенесён в backups', 'ok'); } catch (e) { toast(e.message); }
  load();
}
async function showHistory(id) {
  const name = nameOf(id);
  $('histTitle').textContent = 'История IP — ' + name;
  $('histBody').innerHTML = 'Загрузка…'; hist.showModal();
  const h = (await api('GET', `/api/profiles/${id}/ip-history`)).history.reverse();
  $('histBody').innerHTML = h.length ? `<table><tr><th>Когда</th><th>IP</th><th>Где</th><th>Источник</th></tr>${h.map(r =>
    `<tr><td>${esc(r.time)}</td><td>${esc(r.ip)}</td><td>${esc(r.country)} ${esc(r.city)}<div class="mut small">${esc(r.asn)}</div></td>
     <td class="small">${esc({launch:'запуск', monitor:'во время работы', create:'создание'}[r.source] || r.source)}${r.flag ? ' <b style="color:var(--warn)">смена страны</b>' : ''}</td></tr>`).join('')}</table>`
    : '<div class="mut">Пока пусто — история появится после первого запуска.</div>';
}
async function openForm(id) {
  editing = id || null;
  $('formTitle').textContent = id ? 'Профиль' : 'Новый профиль';
  $('f_save').textContent = id ? 'Сохранить' : 'Создать';
  $('f_check').textContent = '';
  for (const f of ['name','group','proxy','urls','notes']) $('f_' + f).value = '';
  if (id) {
    const p = await api('GET', `/api/profiles/${id}?with_proxy=true`);
    $('f_name').value = p.name; $('f_group').value = p.group || ''; $('f_proxy').value = p.proxy || '';
    $('f_urls').value = (p.start_urls || []).join('\n'); $('f_notes').value = p.notes || '';
  }
  form.showModal();
}
async function checkProxy() {
  $('f_check').textContent = 'Проверяю…';
  const r = await api('POST', '/api/proxy/check', {proxy: $('f_proxy').value});
  $('f_check').innerHTML = r.ok ? `<span style="color:var(--ok)">✓ ${esc(r.ip)} — ${esc(r.geo.country)} ${esc(r.geo.city)}, ${esc(r.geo.timezone)}, ${esc(r.geo.asn)} (${r.latency_s} с)</span>`
                                : `<span style="color:var(--err)">✗ ${esc(r.error)}</span>`;
}
async function saveForm() {
  const body = {name: $('f_name').value.trim(), group: $('f_group').value.trim(), proxy: $('f_proxy').value.trim(),
    start_urls: $('f_urls').value.split('\n').map(s => s.trim()).filter(Boolean), notes: $('f_notes').value};
  if (!body.name) { toast('Укажи имя'); return; }
  $('f_save').disabled = true;
  try {
    if (editing) await api('PATCH', `/api/profiles/${editing}`, body);
    else {
      $('f_save').textContent = 'Подбираю отпечаток…';
      const r = await api('POST', '/api/profiles', body);
      if (r.check && !r.check.ok) toast('Профиль создан, но прокси не прошёл проверку: ' + r.check.error, 'err', 12000);
      uniqueToast(r.uniqueness);
    }
    form.close();
  } catch (e) { toast(e.message); }
  $('f_save').disabled = false; $('f_save').textContent = editing ? 'Сохранить' : 'Создать'; load();
}
load(); setInterval(load, 2000);
setInterval(() => fetch('/api/ui-ping', {method: 'POST', cache: 'no-store'}).catch(() => {}), 5000);
window.addEventListener('focus', load);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
</script></body></html>
"""
