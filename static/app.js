/* NE8000 Manager front end. Plain JS, no build step, no external libraries. */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const attr = s => esc(JSON.stringify(s));
let NAVSEQ = 0, NAVCTL = new AbortController();   // the page being shown (see go)

async function api(path, opts = {}, retried = false) {
  // reads belong to the page being shown: leaving the page cancels them (changes, POSTs, always finish)
  // (opts.global: a read for the whole app, like the top bar, that a page change must not cancel)
  const page = !opts.method && !opts.signal && !opts.global ? { signal: NAVCTL.signal } : {};
  if (opts.global) { opts = { ...opts }; delete opts.global; }
  let res;
  try { res = await fetch(path, { headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'ne8000' }, credentials: 'same-origin', ...page, ...opts }); }
  catch (e) { if (e.name === 'AbortError') { const x = new Error('Left the page.'); x.stale = true; throw x; } throw e; }
  const data = await res.json().catch(() => ({ error: 'Bad response from server (' + res.status + ')' }));
  if (res.status === 401 && data.login && path !== '/api/auth/me') { showAppLogin(); }
  // connected over SNMP and this needs the router CLI: ask for an SSH login once, then try again
  if (!res.ok && data.ssh_required && !retried && !path.startsWith('/api/connect') && await sshLogin(data.error)) return api(path, opts, true);
  if (!res.ok) { const err = new Error(data.error || res.statusText); err.data = data; throw err; }
  return data;
}
let sshAsk = null, sshCancel = null;
function sshLogin(why) {
  if (sshAsk) return sshAsk;   // several requests at once share one prompt
  sshAsk = new Promise(done => {
    sshCancel = () => { sshAsk = null; sshCancel = null; done(false); };   // closed by Esc or a click outside: same as Not now
    let last = ''; try { last = localStorage.getItem('ne8000.router') || ''; } catch (e) {}
    const m = $('#modal'), r = (ROUTER_LIST || []).find(x => x.id === last);
    const mt = VENDOR === 'mikrotik';
    m.innerHTML = `<h2>${mt ? 'MikroTik login needed' : 'SSH login needed'}</h2><p class="muted">${esc(why || 'This needs the router command line.')} Monitoring keeps working over SNMP without it.</p>
      <div class="form"><label>Router username</label><input id="sshU" name="router-user" autocomplete="off" value="${esc(r && r.username || '')}">
      <label>Router password (not your app password)</label><input id="sshP" name="router-pass" type="password" autocomplete="new-password"></div>
      <div class="err"></div><div class="actions"><button class="btn" data-a="c">Not now</button><button class="btn primary" data-a="s">Sign in</button></div>`;
    $('#modalBg').classList.remove('hidden'); ($('#sshU').value ? $('#sshP') : $('#sshU')).focus();
    const finish = ok => { sshCancel = null; closeModal(); sshAsk = null; done(ok); };
    const submit = async () => {
      try { const x = await post('/api/connect/ssh', { username: $('#sshU').value, password: $('#sshP').value }); VIA = x.via; topbar().catch(() => {}); toast(mt ? 'MikroTik API connected' : 'SSH connected'); finish(true); }
      catch (e) { m.querySelector('.err').textContent = e.message; }
    };
    $('#sshP').onkeydown = e => { if (e.key === 'Enter') submit(); };
    m.querySelector('.actions').onclick = e => { if (e.target.dataset.a === 'c') finish(false); if (e.target.dataset.a === 's') submit(); };
  });
  return sshAsk;
}
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });

function toast(msg, err) {
  const d = document.createElement('div'); d.textContent = msg; if (err) d.className = 'e';
  $('#toast').appendChild(d); setTimeout(() => d.remove(), err ? 9000 : 4000);
}

/* A clear result after a change: green ✓ committed, blue ✓ checked only, red ✗ nothing committed. Stays until closed. */
function done(o) {
  let d = $('#done'); if (!d) { d = document.createElement('div'); d.id = 'done'; document.body.appendChild(d); }
  const kind = o.fail ? 'fail' : o.check ? 'check' : 'ok';
  const when = new Date((o.time || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const head = o.head || (o.fail ? 'Not applied: nothing was committed' : o.check ? 'Checked: nothing was committed' : 'Applied successfully');
  d.className = kind; d.setAttribute('role', 'status');
  d.innerHTML = `<div class="hd"><div class="tick">${o.fail ? '✗' : '✓'}</div><div><div class="t">${esc(head)}</div><div class="s">${esc(o.title || '')}${o.title ? ' · ' : ''}${esc(when)}${o.commit_id ? ' · commit ' + esc(o.commit_id) : ''}${$('#tName') ? ' · ' + esc($('#tName').textContent) : ''}</div></div><button class="x" title="close" aria-label="close">×</button></div>` +
    (o.lines && o.lines.length ? `<pre>${esc(o.lines.join('\n'))}</pre>` : o.text ? `<pre>${esc(o.text)}</pre>` : '');
  d.querySelector('.x').onclick = () => { d.remove(); clearTimeout(done.t); };
  // the card lets clicks through to the page; after a while it shrinks to a small note in the top bar (click it to see it again)
  const old = $('#doneMini'); if (old) old.remove();
  clearTimeout(done.t); done.t = setTimeout(() => { d.remove();
    const b = document.createElement('button'); b.id = 'doneMini'; b.className = 'btn sm done-mini ' + kind; b.title = head + (o.title ? ': ' + o.title : '');
    b.textContent = (o.fail ? '✗ ' : '✓ ') + (o.fail ? 'Not applied' : o.check ? 'Checked' : 'Applied') + ' ' + when.slice(0, 5); b.onclick = () => { b.remove(); done(o); };
    const r = $('#top .right'); if (r) r.prepend(b); }, o.fail ? 30000 : 12000);
}

function pill(state) {
  const s = String(state ?? '').toLowerCase();
  let c = 'p-bad';
  if (!s) return '';
  if (s.startsWith('*') || s.includes('admin') || s === 'idle(admin)') c = 'p-off';
  else if (s.startsWith('up') || ['full', 'established', 'operational', 'normal', 'active', 'enable', 'supply', 'present', 'registered', 'p-2-p', 'dr', 'bdr', 'drother', 'synchronized'].includes(s)) c = 'p-ok';
  else if (['connect', 'opensent', 'openconfirm', 'init', '2-way', 'exstart', 'exchange', 'loading', 'minor', 'warning'].includes(s)) c = 'p-warn';
  return `<span class="pill ${c}">${esc(state)}</span>`;
}
function bgpPill(state) {
  const s = String(state || ''); const c = s === 'Established' ? 'p-ok' : /admin/i.test(s) ? 'p-off' : /^(Connect|OpenSent|OpenConfirm)$/.test(s) ? 'p-warn' : 'p-bad';
  return `<span class="pill ${c}">${esc(s)}</span>`;
}
function ubar(pct) {
  const v = Math.max(0, Math.min(100, +pct || 0)); const col = v > 85 ? 'var(--bad)' : v > 65 ? 'var(--warn)' : 'var(--s1)';
  return `<span class="ubar"><div style="width:${Math.max(v, v ? 2 : 0)}%;background:${col}"></div></span>${(+pct || 0).toFixed(pct < 10 ? 2 : 0)}%`;
}

/* ---------- generic table with sort + filter ---------- */
let GOQ = null;      // a filter from the search box, given to the first table the opened page draws
function table(host, cols, rows, opts = {}) {
  const st = host._t || (host._t = { sort: opts.sort || null, dir: opts.dir || 1, q: '' });
  if (GOQ && opts.filter !== false && Date.now() - GOQ.at < 8000) { st.q = GOQ.q; GOQ = null; }
  host.innerHTML = (opts.filter !== false ? `<div class="bar">${opts.toolbar || ''}<span class="sp"></span><input placeholder="Filter" class="tf" value="${esc(st.q)}"></div>` : (opts.toolbar ? `<div class="bar">${opts.toolbar}</div>` : '')) +
    `<div class="tw"><table><thead><tr>${cols.map(c => `<th class="${c.sort === false ? 'nosort' : ''} ${c.num ? 'num' : ''}" data-k="${c.k || ''}">${esc(c.t)}${st.sort === c.k ? (st.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('')}</tr></thead><tbody></tbody></table></div><div class="count"></div>`;
  const body = host.querySelector('tbody');
  const render = () => {
    let list = rows;
    if (st.q) { const q = st.q.toLowerCase(); list = list.filter(r => JSON.stringify(r).toLowerCase().includes(q)); }
    if (st.sort) list = [...list].sort((a, b) => { const x = a[st.sort], y = b[st.sort]; return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x ?? '').localeCompare(String(y ?? ''), undefined, { numeric: true })) * st.dir; });
    const shown = list.slice(0, opts.limit || 3000);
    body.innerHTML = shown.map((r, i) => `<tr data-i="${rows.indexOf(r)}" class="${opts.onRow ? 'click' : ''} ${opts.rowClass ? opts.rowClass(r) : ''}">${cols.map(c => `<td class="${c.num ? 'num' : ''} ${c.cls || ''}">${c.f ? c.f(r) : esc(r[c.k])}</td>`).join('')}</tr>`).join('')
      || `<tr><td colspan="${cols.length}" class="muted">${esc(opts.empty || 'Nothing here')}</td></tr>`;
    host.querySelector('.count').textContent = `${list.length} item${list.length === 1 ? '' : 's'}` + (list.length > shown.length ? `, showing first ${shown.length}` : '') + (st.q ? ` matching “${st.q}”` : '');
  };
  host.querySelectorAll('th[data-k]').forEach(th => th.onclick = () => { const k = th.dataset.k; if (!k || th.classList.contains('nosort')) return; st.dir = st.sort === k ? -st.dir : 1; st.sort = k; table(host, cols, rows, opts); });
  const tf = host.querySelector('.tf'); if (tf) tf.oninput = () => { st.q = tf.value; render(); };
  if (opts.onRow) body.onclick = e => { if (e.target.closest('button,a,input')) return; const tr = e.target.closest('tr[data-i]'); if (tr) opts.onRow(rows[+tr.dataset.i]); };
  render();
}

/* ---------- change flow: form -> preview CLI -> commit / check ---------- */
function closeModal() { $('#modalBg').classList.add('hidden'); $('#modal').innerHTML = ''; $('#modal').classList.remove('wide'); if (sshCancel) sshCancel(); if (cropCancel) cropCancel(); }
$('#modalBg').addEventListener('mousedown', e => { if (e.target.id === 'modalBg') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') { if (String(current).split('/')[0] === 'noc' && $('#modalBg').classList.contains('hidden')) { nocLeave(); location.hash = 'netmap'; } closeModal(); closeDrawer(); } });

function field(f) {
  const v = f.value ?? '';
  let input;
  if (f.type === 'select') input = `<select name="${f.k}">${f.options.map(o => { const [val, lab] = Array.isArray(o) ? o : [o, o]; return `<option value="${esc(val)}" ${String(val) === String(v) ? 'selected' : ''}>${esc(lab)}</option>`; }).join('')}</select>`;
  else if (f.type === 'textarea') input = `<textarea name="${f.k}" style="min-height:140px" placeholder="${esc(f.ph || '')}">${esc(v)}</textarea>`;
  else if (f.type === 'check') input = `<input type="checkbox" name="${f.k}" ${v ? 'checked' : ''} style="width:auto">`;
  else input = `<input name="${f.k}" type="${f.type || 'text'}" value="${esc(v)}" placeholder="${esc(f.ph || '')}" ${f.list ? `list="dl-${f.k}"` : ''} autocomplete="off">` +
    (f.list ? `<datalist id="dl-${f.k}">${f.list.map(o => `<option value="${esc(o)}">`).join('')}</datalist>` : '');
  return `<label>${esc(f.label)}</label>${input}${f.help ? `<div class="help">${esc(f.help)}</div>` : ''}`;
}

/* change({title, action, fields, params, danger, after}) */
function change(o) {
  const m = $('#modal');
  m.innerHTML = `<h2>${esc(o.title)}</h2>${o.intro ? `<p class="muted" style="margin-top:-4px">${esc(o.intro)}</p>` : ''}
    <form class="form" onsubmit="return false">${[...(o.fields || []), ...(o.confirm ? [{ k: '_confirm', label: `Type ${o.confirm} to confirm`, ph: o.confirm }] : [])].map(field).join('')}</form>
    <div class="preview hidden"><div class="lbl">Commands that will be sent:</div><pre class="cli"></pre></div>
    <div class="err"></div>
    <div class="actions"><button class="btn" data-a="cancel">${o.block ? 'Close' : 'Cancel'}</button>${o.block ? '' : '<button class="btn" data-a="check" title="send the lines, then abort instead of commit">Check only</button><button class="btn primary" data-a="preview">Review</button>'}</div>`;
  $('#modalBg').classList.remove('hidden');
  const form = m.querySelector('form'), err = m.querySelector('.err'), pv = m.querySelector('.preview');
  const first = form.querySelector('input,select'); if (first) first.focus();
  const params = () => { const p = { ...(o.params || {}) }; form.querySelectorAll('[name]').forEach(el => p[el.name] = el.type === 'checkbox' ? el.checked : el.value); return p; };
  let reviewed = false;
  form.addEventListener('input', e => { if (e.target.name === '_confirm') return; reviewed = false; pv.classList.add('hidden'); setPrimary('Review'); });
  const setPrimary = t => { const b = m.querySelector('[data-a=preview]'); b.textContent = t; b.classList.toggle('danger', false); };
  const run = async (commit) => {
    err.textContent = '';
    try {
      const sent = params();
      const r = await post(o.url || '/api/action', { action: o.action, params: sent, commit });
      closeModal(); toast((commit ? 'Applied: ' : 'Syntax checked (not committed): ') + o.title);
      if (commit) done({ title: o.title, lines: r.lines, commit_id: r.commit_id, time: r.time, head: r.committed === false ? 'Done successfully' : '' });
      if (!commit) showOutput('Check result: ' + o.title, r.output);
      if (o.after) o.after(commit, sent); else refresh();
    } catch (e) { err.textContent = '✗ ' + e.message; if (commit) done({ fail: true, title: o.title, text: e.message }); }
  };
  let busy = false;   // one request at a time: a second click while the router is still answering would send the change twice
  const lock = on => { busy = on; m.querySelectorAll('.actions [data-a=check],.actions [data-a=preview]').forEach(b => b.disabled = on); };
  m.querySelector('.actions').onclick = async e => {
    const a = e.target.dataset.a; if (!a) return;
    if (a === 'cancel') return closeModal();
    if (o.block || busy) return;
    lock(true);
    try { await step(a); } finally { if (m.contains(e.target)) lock(false); }
  };
  const step = async a => {
    if (a === 'check') return run(false);
    if (!reviewed) {
      err.textContent = '';
      try { const r = await post('/api/action', { action: o.action, params: params(), preview: true });
        m.querySelector('.cli').textContent = r.lines.join('\n') + (r.mode === 'config' ? '\ncommit' : '');
        pv.classList.remove('hidden'); reviewed = true; setPrimary(r.mode === 'exec' ? 'Run' : 'Apply & commit');
        const w = o.warn && o.warn(params()); if (w) err.textContent = '⚠ ' + w;
      } catch (e2) { err.textContent = e2.message; }
      return;
    }
    const typed = form.querySelector('[name=_confirm]');
    if (typed && typed.value.trim() !== o.confirm) { err.textContent = `Type ${o.confirm} in the box to confirm.`; return; }
    return run(true);
  };
  if (o.block) err.textContent = '⚠ ' + o.block;
  else if (!(o.fields || []).length && !o.confirm) m.querySelector('[data-a=preview]').click();
}
function showOutput(title, text) {
  const m = $('#modal'); m.innerHTML = `<h2>${esc(title)}</h2><pre>${esc(text)}</pre><div class="actions"><button class="btn primary">Close</button></div>`;
  m.querySelector('button').onclick = closeModal; $('#modalBg').classList.remove('hidden');
}

/* ---------- navigation ---------- */
const I = {
  bell: '<path d="M4 11V7a4 4 0 0 1 8 0v4l1.5 2h-11zM6.5 14a1.5 1.5 0 0 0 3 0"/>',
  site: '<path d="M2 14h12M3 14V6l5-3 5 3v8M6 14v-3h4v3M6 8h1M9 8h1"/>',
  dash: '<path d="M2 2h5v6H2zM9 2h5v3H9zM9 7h5v7H9zM2 10h5v4H2z"/>', health: '<path d="M1 8h3l2-4 3 8 2-4h4"/>', ifs: '<path d="M2 4h12v8H2zM5 7v2M8 7v2M11 7v2"/>', vlan: '<path d="M2 3h12M2 8h12M2 13h12M5 3v10M11 3v10"/>',
  ip: '<circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c2 2 2 10 0 12M8 2c-2 2-2 10 0 12"/>', route: '<path d="M3 13V8a3 3 0 013-3h7M10 2l3 3-3 3"/>', q: '<path d="M2 4h9M2 8h12M2 12h6"/>',
  sys: '<circle cx="8" cy="8" r="2.5"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M3 13l1.5-1.5M11.5 4.5L13 3"/>', tool: '<path d="M10 2a3 3 0 00-3 4L2 11l3 3 5-5a3 3 0 004-3l-2 1-2-2 1-2z"/>',
  graph: '<path d="M2 13l4-5 3 3 5-7"/>', log: '<path d="M3 2h10v12H3zM5 5h6M5 8h6M5 11h4"/>', hw: '<path d="M3 3h10v10H3zM6 6h4v4H6zM1 6h2M1 10h2M13 6h2M13 10h2"/>', user: '<circle cx="8" cy="5" r="3"/><path d="M2 14c1-3 3-4 6-4s5 1 6 4"/>',
  term: '<path d="M2 3h12v10H2zM4 6l2 2-2 2M8 10h4"/>', rtr: '<path d="M2 9h12v4H2zM5 11h.01M8 11h.01M4 9l2-5h4l2 5"/>', adopt: '<path d="M2 9h8v4H2zM12 3v6M9 6h6"/>', plan: '<path d="M2 3l4-1 4 2 4-1v11l-4 1-4-2-4 1zM6 2v11M10 4v11"/>', bill: '<path d="M3 2h10v12l-2-1-2 1-2-1-2 1-2-1zM5.5 5h5M5.5 8h5M5.5 11h3"/>',
};
const NAV = [
  [null, [['dashboard', 'Dashboard', I.dash], ['health', 'Health check', I.health], ['alerts', 'Alerts', I.bell], ['sites', 'Sites', I.site], ['changes', 'Change log', I.log]]],
  ['Network', [['interfaces', 'Interfaces', I.ifs], ['switching', 'Switching (ports, MACs, PoE)', I.vlan], ['graphs', 'Graphs', I.graph], ['netflow', 'Traffic per address', I.graph], ['vlan', 'VLAN', I.vlan], ['queues', 'Queues / QoS', I.q]]],
  ['IP', [['ipplan', 'IP plan', I.plan], ['addresses', 'Addresses', I.ip], ['routes', 'Routes', I.route], ['arp', 'ARP', I.ip], ['vpn', 'VPN instances', I.ip], ['neighbors', 'Neighbors (LLDP)', I.ip]]],
  ['Planning', [['netmap', 'Network map', I.plan], ['plans', 'Network plans', I.plan]]],
  ['Billing', [['billing', 'Splynx billing', I.bill]]],
  ['Routing', [['bgp', 'BGP', I.route], ['policies', 'Route policies', I.route], ['ospf', 'OSPF', I.route], ['isis', 'IS-IS', I.route], ['mpls', 'MPLS / LDP', I.route]]],
  ['System', [['server', 'Server health', I.health], ['resources', 'Resources', I.graph], ['hardware', 'Hardware', I.hw], ['users', 'Router users', I.user], ['settings', 'Settings', I.sys], ['logs', 'Logs & alarms', I.log]]],
  ['Manager', [['account', 'My account', I.user], ['routers', 'Routers', I.rtr], ['adopt', 'Adopt devices', I.adopt], ['appusers', 'App users', I.user], ['audit', 'Audit log', I.log]]],
  ['Tools', [['traffic', 'Traffic monitor', I.graph], ['ping', 'Ping / Traceroute', I.tool], ['console', 'Console', I.term], ['config', 'Config editor', I.term], ['backup', 'Backup', I.tool]]],
];
$('#side').innerHTML = NAV.map(([g, items]) => (g ? `<div class="grp">${g}</div>` : '') + items.map(([k, t, ic]) =>
  `<a data-p="${k}"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">${ic}</svg>${t}</a>`).join('')).join('');
// Search and NOC live at the top of the left menu (moved, so their handlers stay)
{ const st = document.createElement('div'); st.className = 'sideTop'; const nb = $('#nocBtn'); st.append($('#gs'), nb); nb.innerHTML = '▣ NOC view'; $('#side').prepend(st); }
$('#side').onclick = e => { const a = e.target.closest('a[data-p]'); if (a) { location.hash = a.dataset.p; $('#side').classList.remove('open'); } };
$('#menuBtn').onclick = () => $('#side').classList.toggle('open');

let MODEL = '';
let timers = [], charts = [], current = null, DEMO = false, VIA = '', VENDOR = 'huawei', ME = null, AUTH = {}, ROUTER_LIST = [];
function every(ms, fn) { const my = typeof NAVSEQ === 'undefined' ? 0 : NAVSEQ; timers.push(setInterval(() => { if (!document.hidden && my === NAVSEQ) fn().catch(e => { if (!e.stale) console.warn(e); }); }, ms)); }
function cleanup() { timers.forEach(clearInterval); timers = []; charts.forEach(c => c.destroy()); charts = []; }
function chart(host, opts) { const c = new TimeChart(host, opts); charts.push(c); return c; }
function refresh() { if (current) go(current, true); }

/* Each click gets its own page box and its own request signal. Clicking on before a slow page has loaded cancels that
   page's requests and leaves it drawing into a box nobody sees, so it can never cover the page you went to. */
async function go(page, keepScroll) {
  const [base, arg] = (page || '').split('/');
  if (!PAGES[base]) page = 'dashboard';
  const key = PAGES[base] ? base : 'dashboard';
  if (window.planGuard && !window.planGuard(page)) return;
  if (base !== 'noc') nocLeave();
  cleanup(); closeDrawer(); current = page;
  NAVCTL.abort(); NAVCTL = new AbortController(); const my = ++NAVSEQ;
  const navKey = key === 'plan' ? 'plans' : key;
  document.querySelectorAll('#side a').forEach(a => a.classList.toggle('active', a.dataset.p === navKey));
  const host = $('#content'), y = host.scrollTop, old = [...host.querySelectorAll(':scope > .pagebox')];
  const c = document.createElement('div'); c.className = 'pagebox';
  // page state kept on the container (filters, chosen VPN) carries over to the new box
  for (const src of [host, ...old]) for (const k of Object.keys(src)) if (k.startsWith('_') && k !== '_t' && k !== '_p' && k !== '_n') c[k] = src[k];
  if (keepScroll && old.length) { c.style.display = 'none'; host.prepend(c); }   // build the refreshed page behind the old one: no flash
  else { c.innerHTML = '<div class="muted">Loading…</div>'; host.replaceChildren(c); }
  const show = () => { if (my !== NAVSEQ) { c.remove(); return; } for (const o of host.querySelectorAll(':scope > .pagebox')) if (o !== c) o.remove(); c.style.display = ''; };
  if (!keepScroll && ME) { const nav = NAV.flatMap(g => g[1]).find(x => x[0] === navKey); post('/api/audit/view', { page, title: nav ? nav[1] : ({ noc: 'NOC view', plan: 'Network plan' }[key] || page) }).catch(() => {}); }
  try { await PAGES[key](c, arg); show(); if (keepScroll && my === NAVSEQ) host.scrollTop = y; }
  catch (e) { if (my !== NAVSEQ || e.stale) { c.remove(); return; } show(); c.innerHTML = `<div class="card"><div class="err">${esc(e.message)}</div><button class="btn" onclick="refresh()">Retry</button></div>`; }
}
window.addEventListener('hashchange', () => go(location.hash.slice(1)));

function head(title, sub, buttons = '') { return `${DEMO ? '<div class="banner">Demo mode: a simulated router with sample data. Changes apply to the simulation only.</div>' : ''}${ME && ME.role === 'viewer' ? '<div class="banner">Your account is read-only: you can look at everything but changes are refused.</div>' : ''}<h1>${esc(title)}${sub ? ` <span class="sub">${esc(sub)}</span>` : ''}<span style="flex:1"></span>${buttons}</h1>`; }
const legend = items => `<div class="legend">${items.map(([n, c, v]) => `<span><i style="background:var(${c})"></i>${esc(n)}${v != null ? ` <b data-v="${esc(n)}">${v}</b>` : ''}</span>`).join('')}</div>`;

/* ---------- top bar ---------- */
async function topbar() {
  const o = await api('/api/view/overview', { global: true });
  MODEL = o.model || MODEL;
  $('#tName').textContent = o.sysname; $('#tHost').textContent = o.host + (o.via ? ' · ' + o.via : ''); $('#tCpu').textContent = Fmt.pct(o.cpu);
  $('#tMem').textContent = Fmt.pct(o.memory.percent); const up = (o.uptime || '').replace(/ minutes?/, 'm').replace(/ hours?,?/, 'h').replace(/ days?,?/, 'd'); $('#tUp').textContent = up.split(' ').slice(0, 2).join(' '); $('#tUp').parentNode.title = 'Up ' + (o.uptime || '');
  $('#tCpu').parentNode.className = o.cpu >= 85 ? 'hi' : o.cpu >= 70 ? 'mid' : ''; $('#tMem').parentNode.className = o.memory.percent >= 90 ? 'hi' : o.memory.percent >= 80 ? 'mid' : '';
  if ($('#tSwitch')) $('#tSwitch').title = `${o.sysname} · ${o.host}${o.via ? ' · ' + o.via : ''} (switch to another saved router)`;
  document.title = `${o.sysname} · NE8000 Manager`;
  return o;
}

/* ================= pages ================= */
const PAGES = {};

PAGES.dashboard = async c => {
  const [o, res] = await Promise.all([topbar(), api('/api/resources')]);
  const t = (l, v, s, m) => `<div class="tile"><div class="l">${l}</div><div class="v">${v}</div>${s ? `<div class="s">${s}</div>` : ''}${m != null ? `<div class="meter"><div style="width:${m}%;background:${m > 85 ? 'var(--bad)' : m > 65 ? 'var(--warn)' : 'var(--s1)'}"></div></div>` : ''}</div>`;
  c.innerHTML = head(o.sysname, `${o.model} · VRP ${o.vrp_version} · ${o.software}`) + (o.poll_error ? `<div class="banner">Poller: ${esc(o.poll_error)}</div>` : '') +
    `<div class="tiles">${t('CPU', Fmt.pct(o.cpu), '', o.cpu)}${t('Memory', Fmt.pct(o.memory.percent), o.memory.total_kb ? `${Fmt.bytes(o.memory.used_kb * 1024)} of ${Fmt.bytes(o.memory.total_kb * 1024)}` : '', o.memory.percent)}
    ${t('Uptime', esc(o.uptime.replace(/ days?,?/, 'd').replace(/ hours?,?/, 'h').replace(/ minutes?/, 'm')), esc(o.patch ? 'Patch ' + o.patch : ''))}${t('Max temperature', Fmt.temp(o.max_temp), 'hottest sensor')}
    ${t('Ports up', `${o.interfaces.up} / ${o.interfaces.total}`, `${o.interfaces.admin_down} admin down · ${o.interfaces.all} interfaces total`)}
    ${t('BGP established', `${o.bgp.established ?? 0} / ${o.bgp.total ?? 0}`, `AS ${esc(o.bgp.local_as || '-')} · RID ${esc(o.bgp.router_id || '-')}`)}
    ${o.alarms.total == null ? t('Active alarms', '-', '<a href="#logs">sign in with SSH to read alarms</a>') : t('Active alarms', o.alarms.total, `${o.alarms.critical} major or critical`)}
    <a class="tile" href="#health" id="dHealth" style="text-decoration:none;color:inherit"><div class="l">Health check</div><div class="v">…</div></a></div>
    <div class="card"><h3>Ports<span class="sp"></span><a href="#interfaces" class="btn sm">Interfaces</a></h3><div id="dPorts"><span class="muted">Loading…</span></div></div>
    <div class="grid g2"><div class="card"><h3>CPU <span class="sp"></span><a href="#resources" class="btn sm">Resources</a></h3><div id="cCpu"></div></div>
    <div class="card"><h3>Memory</h3><div id="cMem"></div></div></div>
    <div class="grid g2"><div class="card"><h3>Busiest interfaces<span class="sp"></span><a href="#interfaces" class="btn sm">All interfaces</a></h3><div id="dIf"></div></div>
    <div class="card"><h3>BGP peers<span class="sp"></span><a href="#bgp" class="btn sm">BGP</a></h3><div id="dBgp"></div></div></div>`;
  const cpu = chart($('#cCpu'), { fmt: Fmt.pct, max: 100, small: true }), mem = chart($('#cMem'), { fmt: Fmt.pct, max: 100, small: true });
  const draw = r => { cpu.set([{ name: 'CPU', color: '--s1', values: r.cpu }]); mem.set([{ name: 'Memory', color: '--s1', values: r.memory }]); };
  draw(res);
  const tops = () => {
    table($('#dIf'), [{ t: 'Interface', k: 'name', f: r => `<b>${esc(r.name)}</b>` }, { t: 'In', k: 'in_util', f: r => ubar(r.in_util) }, { t: 'Out', k: 'out_util', f: r => ubar(r.out_util) }], o.top_interfaces, { filter: false, onRow: r => interfaceDrawer(r.name) });
    table($('#dBgp'), [{ t: 'Peer', k: 'peer', f: r => `<b>${esc(r.peer)}</b>` }, { t: 'AS', k: 'as' }, { t: 'State', k: 'state', f: r => bgpPill(r.state) }, { t: 'Up/Down', k: 'up_down' }, { t: 'Prefixes', k: 'prefixes', num: true }], o.bgp.peers, { filter: false, empty: 'BGP not configured' });
  };
  tops();
  const hl = async () => { const h = await api('/api/health'), f = h.findings || [], cr = f.filter(x => x.severity === 'critical').length, el = $('#dHealth'); if (!el) return;
    el.innerHTML = `<div class="l">Health check</div><div class="v" style="color:${cr ? 'var(--bad)' : f.length ? 'var(--warn)' : 'var(--ok)'}">${f.length ? f.length + ' problem' + (f.length > 1 ? 's' : '') : 'All good'}</div><div class="s">${f.length ? `${cr} critical · ${f.filter(x => x.slow).length} slowing customers` : h.status && h.status.time ? 'nothing wrong found' : 'first check in a few minutes'}</div>`; };
  let hw = null;
  const pv = async () => { const rows = await api('/api/view/interfaces'); ifCache = rows; await arpLoad(); if (!hw) [hw] = await Promise.all([api('/api/view/hardware').catch(() => ({})), panelLoad()]); portPanel($('#dPorts'), withClients(rows), { hw }); };
  pv().catch(e => { const el = $('#dPorts'); if (el) el.innerHTML = `<span class="muted">${esc(e.message)}</span>`; });
  every(5000, pv);
  hl().catch(() => { const el = $('#dHealth'); if (el) el.remove(); });
  every(60000, hl);
  every(5000, async () => draw(await api('/api/resources')));
  every(10000, async () => { Object.assign(o, await topbar()); tops(); });
};

/* ----- smart health check: what is wrong or unusual, and what to do about it ----- */
const HK = { errors: 'Errors', out_drops: 'Drops', in_drops: 'Drops', congestion: 'Full link', optics: 'SFP light', down: 'Link down', flap: 'Flapping', latency: 'Latency', loss: 'Loss', distance: 'Distance', capacity: 'Filling up', otdr: 'Fibre (OTDR)', cpu: 'CPU', memory: 'Memory', temp: 'Temperature' };
function ago(t) { if (!t) return '-'; const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' days ago'; }
PAGES.health = async c => {
  const h = await api('/api/health').catch(e => ({ note: 'Router checks: ' + (e.message || e) + ' Server health below still works.' })); const f = h.findings || [], st = h.status || {}, lat = h.latency || [];
  const cr = f.filter(x => x.severity === 'critical').length, slow = f.filter(x => x.slow).length;
  const muted = h.muted || [], canMute = !DEMO && ME && ME.role !== 'viewer' && !h.note;
  const t = (l, v, s, col) => `<div class="tile"><div class="l">${l}</div><div class="v" ${col ? `style="color:var(${col})"` : ''}>${v}</div>${s ? `<div class="s">${s}</div>` : ''}</div>`;
  const notes = [h.note, st.error, /^unavailable/.test(st.ping || '') ? 'Latency checks: ' + st.ping.replace(/^unavailable: /, '') + '. The server needs to be allowed to ping (Docker sets this by default).' : ''].filter(Boolean);
  c.innerHTML = head('Health check', `checks every ${Math.round((h.interval || 300) / 60)} minutes and learns each link's normal levels`, `<button class="btn" id="hRun">Check now</button>`) +
    notes.map(n => `<div class="banner">${esc(n)}</div>`).join('') +
    `<div class="tiles">${t('Critical', cr, 'needs action now', cr ? '--bad' : '--ok')}${t('Warnings', f.length - cr, 'worth a look', f.length - cr ? '--warn' : '--ok')}
    ${t('Slowing customers', slow, 'errors, drops, full links, latency', slow ? '--bad' : '--ok')}
    ${t('Checked', `${st.interfaces ?? 0} links`, `${st.optics ?? 0} SFPs · ${st.targets ?? 0} ping targets · ${st.time ? ago(st.time) : 'not yet'}`)}</div>
    <div class="card"><h3>Problems found</h3>${st.time || st.demo ? '' : `<p class="muted">The first check runs within 5 minutes of the server starting. "Unusual" levels need about ${h.learn_hours || 2} hours of history first; fixed limits (errors, full links, SFP alarm levels) apply straight away.</p>`}<div id="hF"></div></div>
    ${muted.length ? `<div class="card"><h3>Muted <span class="muted" style="font-weight:400;font-size:12px">known problems that do not count and send no alerts</span></h3>${muted.map(x => `<div class="row" style="gap:8px;align-items:center;padding:4px 0;border-top:1px solid var(--line)"><span class="pill">${esc(x.severity)}</span><b>${esc(x.title)}</b> ${esc(x.target)}<span class="faint">${esc(x.muted.by)}${x.muted.note ? ': ' + esc(x.muted.note) : ''}${x.muted.until ? ' · until ' + esc(new Date(x.muted.until * 1000).toLocaleString()) : ''}</span><span class="sp"></span>${canMute ? `<button class="btn sm" onclick='nmMute(${attr(x.id)}, false)'>Unmute</button>` : ''}</div>`).join('')}</div>` : ''}
    <div class="card"><h3>Latency and loss <span class="muted" style="font-weight:400;font-size:12px">pinged from this server</span><span class="sp"></span><button class="btn sm" id="hTg">Ping targets…</button></h3><div id="hL"></div></div>
    <div class="card" id="hSrv"><h3>Server health <span class="muted" style="font-weight:400;font-size:12px">the machine this app runs on</span></h3><p class="muted">Reading the server…</p></div>`;
  serverHealth();
  table($('#hF'), [{ t: 'Severity', k: 'severity', f: x => `<span class="pill ${x.severity === 'critical' ? 'p-bad' : 'p-warn'}">${esc(x.severity)}</span>` },
    { t: 'Problem', k: 'title', cls: 'wrap', f: x => `<b>${esc(x.title)}</b> <span class="faint">${esc(HK[x.kind] || '')}</span>${x.slow ? ' <span class="pill p-bad" title="likely to make customers slow">slows customers</span>' : ''}<div class="muted">${esc(x.detail)}</div>` },
    { t: 'Where', k: 'target', f: x => `<b>${esc(x.target)}</b>` }, { t: 'Customer / description', k: 'description' },
    { t: 'Since', k: 'since', f: x => ago(x.since) }, { t: 'What to do', sort: false, cls: 'wrap muted', f: x => esc(x.advice) },
    { t: '', sort: false, cls: 'acts', f: x => (/\//.test(x.target) ? `<button class="btn sm" onclick='showGraphs(${attr(x.target)})'>Graph</button>` : '') + (canMute ? `<button class="btn sm" title="A known problem: keep it listed below but stop counting it and stop alerts" onclick='event.stopPropagation();nmMute(${attr(x.id)}, true)'>Mute</button>` : '') }],
    f, { empty: st.time || st.demo ? 'No problems found. Everything is within its normal levels.' : 'Waiting for the first check.', onRow: x => /\//.test(x.target) && interfaceDrawer(x.target) });
  table($('#hL'), [{ t: 'Address', k: 'ip', cls: 'mono', f: x => `<b>${esc(x.ip)}</b>` }, { t: 'Name', k: 'label' },
    { t: 'Average', k: 'avg', num: true, f: x => x.error ? `<span class="faint">${esc(x.error)}</span>` : x.avg == null ? '<span class="faint">no reply</span>' : x.avg.toFixed(1) + ' ms' },
    { t: 'Normal', k: 'normal', num: true, f: x => x.normal == null ? '<span class="faint">learning</span>' : x.normal.toFixed(1) + ' ms' },
    { t: 'Min / max', sort: false, num: true, f: x => x.min == null ? '' : `${x.min.toFixed(1)} / ${x.max.toFixed(1)} ms` },
    { t: 'Loss', k: 'loss', num: true, f: x => x.loss == null ? '' : `<span style="color:var(${x.loss >= 20 ? '--bad' : x.loss ? '--warn' : '--ok'})">${x.loss}%</span>` }],
    lat, { empty: 'Nothing pinged yet. Add your customers\' CPEs with Ping targets.' });
  $('#hRun').onclick = async () => { await post('/api/health/run'); toast('Checking now; results in about a minute.'); setTimeout(() => current === 'health' && refresh(), 45000); };
  $('#hTg').onclick = () => healthTargets(h.targets || []);
  every(60000, async () => { if ($('#modalBg').classList.contains('hidden')) refresh(); });
};
PAGES.server = async c => {
  c.innerHTML = head('Server health', 'the machine this app is hosted on: CPU, memory, disk and uptime') +
    `<div class="card" id="hSrv"><h3>Server health</h3><p class="muted">Reading the server…</p></div>`;
  await serverHealth();
  every(60000, () => serverHealth());
};
/* Server health card on the Health page: the machine the app is hosted on, its use now and over 24 h. */
async function serverHealth() {
  const el = $('#hSrv'); if (!el) return;
  let r; try { r = await api('/api/server'); } catch (e) { el.innerHTML = '<h3>Server health</h3><p class="muted">Could not read the server: ' + esc(e.message || e) + '</p>'; return; }
  if (!$('#hSrv')) return;
  const h = r.host, c = r.cpu, m = r.memory, d = r.disk, a = r.app, cm = r.container_memory;
  const pct = (u, t) => t ? 100 * u / t : null, col = (v, w, b) => v == null ? '' : v >= b ? '--bad' : v >= w ? '--warn' : '--ok';
  const mp = pct(m.used, m.total), dp = d ? pct(d.used, d.used + d.free) : null, lp = c.cores ? 100 * c.load[1] / c.cores : null;
  const dur = s => { if (s == null) return '-'; s = Math.round(s); const dd = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), mm = Math.floor(s % 3600 / 60); return dd ? `${dd} d ${hh} h` : hh ? `${hh} h ${mm} min` : `${mm} min`; };
  const bar = v => `<div class="srvbar"><i style="width:${Math.min(100, Math.max(0, v || 0)).toFixed(1)}%;background:var(${col(v, 80, 92) || '--s1'})"></i></div>`;
  const tile = (l, v, s, colr, b) => `<div class="tile"><div class="l">${l}</div><div class="v" ${colr ? `style="color:var(${colr})"` : ''}>${v}</div>${b || ''}${s ? `<div class="s">${s}</div>` : ''}</div>`;
  const where = [h.vendor, h.product].filter(Boolean).join(' ') || (h.hypervisor ? h.hypervisor.toUpperCase() : '');
  const kind = (h.virtual ? 'Virtual machine' : 'Physical server') + (where ? ' · ' + where : '');
  const os = h.container ? `${h.host_os ? h.host_os + ' host, ' : ''}kernel ${h.kernel}` : `${h.os}, kernel ${h.kernel}`;
  const rows = [['Machine', kind], ['Hostname', h.hostname + (h.container ? ' <span class="faint">(the app\'s container)</span>' : '')], ['Operating system', esc(os)],
    ['Processor', `${esc(c.model || h.arch)} · ${c.cores} core${c.cores === 1 ? '' : 's'} · ${esc(h.arch)}`],
    ['Memory', `${Fmt.bytes(m.total)}${m.swap_total ? ` · swap ${Fmt.bytes(m.swap_used)} of ${Fmt.bytes(m.swap_total)} used` : ' · no swap'}`],
    ['Disk (app data)', d ? `${Fmt.bytes(d.used)} used of ${Fmt.bytes(d.used + d.free)} · ${Fmt.bytes(d.free)} free` : '-'],
    ['App data folder', `${Fmt.bytes(r.data.size)} in ${Fmt.num(r.data.files)}${r.data.more ? '+' : ''} files`],
    ['Server up for', dur(r.uptime)], ['App running for', `${dur(a.uptime)} <span class="faint">since ${new Date(a.started * 1000).toLocaleString()}</span>`],
    ['App process', `${Fmt.bytes(a.rss)} memory · ${a.threads} threads · Python ${esc(a.python)}${cm ? ` · container uses ${Fmt.bytes(cm.used)}${cm.limit ? ' of ' + Fmt.bytes(cm.limit) : ''}` : ''}`],
    ['Run in', h.container ? 'Docker container' : 'Directly on the server'], ['Server time', `${new Date(r.time * 1000).toLocaleString()} <span class="faint">${esc(h.timezone)}</span>`]]
    .concat(r.version ? [['App version', esc(r.version)]] : []);
  el.innerHTML = `<h3>Server health <span class="muted" style="font-weight:400;font-size:12px">the machine this app runs on</span><span class="sp"></span><span class="faint" style="font-weight:400;font-size:12px">updated ${Fmt.time(r.time)}</span></h3>` +
    (r.warnings || []).map(w => `<div class="witem${w.severity === 'critical' ? ' crit' : ''}">${w.severity === 'critical' ? '⛔' : '⚠'} ${esc(w.text)}</div>`).join('') +
    `<div class="tiles">${tile('CPU', Fmt.pct(c.percent), `load ${c.load.map(x => x.toFixed(2)).join(' · ')} on ${c.cores} cores`, col(lp, 70, 100), bar(c.percent))}
    ${tile('Memory', Fmt.pct(mp), `${Fmt.bytes(m.used)} of ${Fmt.bytes(m.total)}`, col(mp, 80, 90), bar(mp))}
    ${tile('Disk', Fmt.pct(dp), d ? `${Fmt.bytes(d.free)} free of ${Fmt.bytes(d.used + d.free)}` : '', col(dp, 80, 92), bar(dp))}
    ${tile('Up for', dur(r.uptime), `app restarted ${ago(a.started)}`)}</div>
    <div class="srvgrid"><table class="kv">${rows.map(([k, v]) => `<tr><td class="muted">${k}</td><td>${v}</td></tr>`).join('')}</table>
    <div><div class="muted" style="font-size:12px;margin-bottom:4px">Last 24 hours (one point a minute)</div><div id="hSrvC" style="height:180px"></div>
    <div class="srvleg"><span><i style="background:var(--s1)"></i>CPU</span><span><i style="background:var(--s2)"></i>Memory</span><span><i style="background:var(--s3)"></i>Disk</span></div>
    <div class="faint" style="font-size:12px">${r.history.length < 2 ? 'Points are kept from when the app started; the graph fills in over the next minutes.' : 'Each line is % of the whole. Hover for the values at any minute.'}</div></div></div>`;
  const hs = r.history || [];
  new TimeChart($('#hSrvC'), { fmt: Fmt.pct, max: 100 }).set([
    { name: 'CPU', color: '--s1', values: hs.filter(x => x[1] != null).map(x => [x[0], x[1]]) },
    { name: 'Memory', color: '--s2', values: hs.filter(x => x[2] != null).map(x => [x[0], x[2]]) },
    { name: 'Disk', color: '--s3', values: hs.filter(x => x[3] != null).map(x => [x[0], x[3]]) }]);
}

async function healthTargets(cur) {
  const m = $('#modal');
  m.innerHTML = `<h2>Ping targets</h2><p class="muted">This server pings each address 5 times every 5 minutes, learns its normal latency and warns when it rises or packets are lost. The router's own address is always included. One per line: address, then a name.</p>
    <div class="form"><label>Addresses</label><textarea id="hTx" rows="12" class="mono" placeholder="102.216.117.26 DONNET">${esc(cur.map(x => `${x.ip} ${x.label}`).join('\n'))}</textarea></div>
    <div class="err"></div><div class="actions"><button class="btn" data-a="arp" title="every client the router sees (ARP) behind an interface with a description">Add customers' CPEs</button><span class="sp"></span><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Save</button></div>`;
  $('#modalBg').classList.remove('hidden');
  m.querySelector('.actions').onclick = async e => {
    const a = e.target.dataset.a; if (!a) return;
    if (a === 'c') return closeModal();
    if (a === 'arp') {
      ifCache = await api('/api/view/interfaces'); arpCache.at = 0; await arpLoad();
      const have = new Set($('#hTx').value.split('\n').map(l => l.trim().split(/\s+/)[0]).filter(Boolean)), add = [];
      for (const r of ifCache) if (r.description && r.ip) for (const n of clientsOf(r)) if (!/no answer/.test(n.mac) && !have.has(n.ip)) { have.add(n.ip); add.push(`${n.ip} ${r.description}`); }
      $('#hTx').value = ($('#hTx').value.trim() ? $('#hTx').value.trim() + '\n' : '') + add.join('\n');
      return toast(add.length ? `Added ${add.length} CPE address${add.length > 1 ? 'es' : ''}.` : 'No new CPE addresses found in ARP.');
    }
    const rows = $('#hTx').value.split('\n').map(l => l.trim()).filter(Boolean).map(l => { const [ip, ...rest] = l.split(/\s+/); return { ip, label: rest.join(' ') }; });
    try { await post('/api/health/targets', { targets: rows }); closeModal(); toast(`Saved ${rows.length} ping target${rows.length === 1 ? '' : 's'}; checking now.`); post('/api/health/run').catch(() => {}); refresh(); }
    catch (err) { m.querySelector('.err').textContent = err.message; }
  };
}

PAGES.resources = async c => {
  const r = await api('/api/resources');
  c.innerHTML = head('Resources', `sampled every ${r.poll_seconds}s, last hour kept while connected`) +
    `<div class="grid g2"><div class="card"><h3>CPU usage</h3>${legend([['CPU', '--s1', Fmt.pct(r.cpu.at(-1)?.[1])]])}<div id="rCpu"></div></div>
    <div class="card"><h3>Memory usage</h3>${legend([['Memory', '--s1', Fmt.pct(r.memory.at(-1)?.[1])]])}<div id="rMem"></div></div>
    <div class="card"><h3>Hottest sensor</h3>${legend([['Temperature', '--s1', Fmt.temp(r.temperature.at(-1)?.[1])]])}<div id="rTemp"></div></div>
    <div class="card"><h3>Memory</h3><div class="kv" id="rMemKv"></div><h3 style="margin-top:16px">Temperature sensors</h3><div id="rTemps"></div></div></div>`;
  const cc = chart($('#rCpu'), { fmt: Fmt.pct, max: 100 }), mc = chart($('#rMem'), { fmt: Fmt.pct, max: 100 }), tc = chart($('#rTemp'), { fmt: Fmt.temp });
  const draw = r => {
    cc.set([{ name: 'CPU', color: '--s1', values: r.cpu }]); mc.set([{ name: 'Memory', color: '--s1', values: r.memory }]); tc.set([{ name: 'Temperature', color: '--s1', values: r.temperature }]);
    c.querySelector('[data-v=CPU]').textContent = Fmt.pct(r.cpu.at(-1)?.[1]); c.querySelector('[data-v=Memory]').textContent = Fmt.pct(r.memory.at(-1)?.[1]);
    c.querySelector('[data-v=Temperature]').textContent = Fmt.temp(r.temperature.at(-1)?.[1]);
    const m = r.memory_detail; $('#rMemKv').innerHTML = `<div><span>Total</span>${Fmt.bytes(m.total_kb * 1024)}</div><div><span>Used</span>${Fmt.bytes(m.used_kb * 1024)}</div><div><span>Free</span>${Fmt.bytes((m.total_kb - m.used_kb) * 1024)}</div><div><span>Used %</span>${Fmt.pct(m.percent)}</div>`;
    table($('#rTemps'), [{ t: 'Slot', k: 'slot' }, { t: 'Board', k: 'board' }, { t: 'Sensor', k: 'sensor' }, { t: 'Status', k: 'status', f: x => pill(x.status) }, { t: 'Now', k: 'temp', num: true, f: x => Fmt.temp(x.temp) }, { t: 'Major at', k: 'major', num: true, f: x => Fmt.temp(x.major) }], r.temps, { filter: false });
  };
  draw(r); every(5000, async () => draw(await api('/api/resources')));
};

/* ----- port view: each physical port with its sub-interfaces (customer VLANs) as tiles ----- */
/* port class from the name: 100GE0/4/1 -> 100G; GigabitEthernet0/0/0 -> 1G */
function portSpeed(r) {
  const m = r.speed_mbps; if (m && m < 4e6) return m >= 1000 ? (m % 1000 ? (m / 1000).toFixed(1) : m / 1000) + 'G' : m + 'M';
  const n = String(r.name || ''); const x = /^(?:\d+\|)?(\d+)GE/i.exec(n);
  return x ? x[1] + 'G' : /^(gigabitethernet|ge)/i.test(n) ? '1G' : /^(xgigabitethernet|10ge)/i.test(n) ? '10G' : /^ethernet/i.test(n) ? '100M' : '';
}
const portLoad = r => Math.max(+r.in_util || 0, +r.out_util || 0);
function portState(r) { return r.admin_down ? 'off' : String(r.phy || '').toLowerCase().startsWith('up') ? (String(r.protocol || '').toLowerCase().startsWith('up') ? 'up' : 'half') : 'down'; }
const PORT_STATE = { up: 'Up', half: 'Link up, protocol down', down: 'Down', off: 'Disabled (admin down)' };
const PORT_CHIPS = [['all', 'All'], ['up', 'Up'], ['down', 'Down'], ['busy', 'Busy (65%+)'], ['err', 'Errors']];
function portTip(r) {
  const n = r._nb || [];
  return [r.name, r.description, PORT_STATE[portState(r)], `In ${(+r.in_util || 0).toFixed(1)}% · Out ${(+r.out_util || 0).toFixed(1)}%`,
    r.ip ? 'IP ' + r.ip + ((r.secondary || []).length ? ` +${r.secondary.length}` : '') : '', r.vlan ? 'VLAN ' + r.vlan : '',
    n.length ? 'Client ' + n[0].ip + ' ' + macColon(n[0].mac) : '', r.in_errors + r.out_errors ? `Errors ${r.in_errors}/${r.out_errors}` : ''].filter(Boolean).join('\n');
}
/* ----- the router drawn as its front panel: every slot by number, the boards the router reports, and each port in its cage ----- */
/* optic type from the wavelength when the part number does not say: 850 nm is multimode (SR), 1310 single-mode (LR), 1550 long reach (ER) */
function opticKind(o, speed) {
  const w = +o.wavelength, lanes = /^(40|100|200|400)G$/.test(speed) ? '4' : '';
  if (!w) return null;
  return [null, w < 900 ? 'SR' + lanes : w < 1400 ? 'LR' + lanes : 'ER' + lanes];
}
/* the board's real model from the electronic labels (display elabel brief), e.g. CR81E2NBC0 2x100GE-QSFP28 */
function boardModel(hw, kind, slot) {
  const lines = String((hw && (hw.boards_elabel || hw.elabel)) || '').split('\n');
  for (const l of lines) {
    const t = l.trim().split(/\s+/);   // "PIC 4   CR81E2NBC0   034KEN10QC100528   2x100GE-QSFP28"
    if (t[1] !== String(slot) || !(kind === 'pic' ? /^(PIC|LPU|CARD)$/i : /^(IPU|MPU)$/i).test(t[0])) continue;
    const code = t.find(x => /^[A-Z]{2}\d\w{5,}$/.test(x) && !/^\d+$/.test(x)), desc = (/\d+\s*x\s*\d+G\w*(?:-[\w]+)?/i.exec(l) || [])[0];  // e.g. 2x100GE-QSFP28
    if (code || desc) return [code, desc].filter(Boolean).join(' ');
  }
  return '';
}
const BOARD_KIND = t => /IPU|MPU|SRU|MMB/i.test(t) ? 'ipu' : /PWR|POWER|PAC|PDC/i.test(t) ? 'pwr' : /FAN/i.test(t) ? 'fan' : /CLK|CLOCK/i.test(t) ? 'clk' : 'pic';
function chassisHtml(rows, hw) {
  const boards = (hw && hw.boards) || [], optics = (hw && hw.optics) || {};
  const phys = rows.filter(r => ['ethernet', 'management'].includes(r.type) && !r.name.includes('.'));
  const pos = r => { const m = /(\d+)\/(\d+)\/(\d+)$/.exec(r.name); return m ? { slot: +m[2], port: +m[3], sub: +m[1] } : null; };
  const by = k => boards.filter(b => BOARD_KIND(b.type) === k);
  const portSlots = [...new Set(phys.map(pos).filter(x => x && x.slot > 0).map(x => x.slot))];
  const m8 = /M8\b/i.test(MODEL);
  // only the card slots the router reports (a board or a port in them): slot numbers are shared with power supplies on some models
  const pics = [...new Set([...by('pic').map(b => +b.slot), ...portSlots])].filter(n => n > 0).sort((a, b) => a - b);
  if (!pics.length) return '';
  const subsN = p => rows.filter(r => r.name.startsWith(p.name + '.')).length;
  const led = st => `<i class="cled ${st}"></i>`;
  const bstate = b => !b ? 'off' : /absent/i.test(b.online) ? 'off' : /abnormal|fault|fail/i.test(b.status) ? 'bad' : 'ok';
  const cage = p => { const s = portState(p), o = optics[p.name] || {}, x = pos(p), mod = /(LR4|SR4|ER4|ZR4|CWDM4|PSM4|LR|SR|ER|ZR|LX|SX|AOC|DAC)/i.exec(o.part || '') || opticKind(o, portSpeed(p));
    return `<button class="cage ps-${s}" data-p="${esc(p.name)}" title="${esc(portTip(p) + noteTip(p.name) + (o.part ? `\nOptic ${[o.vendor, o.part].filter(Boolean).join(' ')}` : '') + (o.rx_dbm != null ? `\nRx ${o.rx_dbm} dBm · Tx ${o.tx_dbm} dBm` : '') + (subsN(p) ? `\n${subsN(p)} sub-interfaces` : ''))}">
      <span class="cport">${x.port}${notesFor(p.name).length ? ' 📝' : ''}</span><span class="chole"><span class="cfill"></span></span><span class="cmod">${esc(mod ? mod[1].toUpperCase() : portSpeed(p))}</span>${p.in_errors + p.out_errors ? '<span class="pe"></span>' : ''}</button>`; };
  const portsIn = slot => phys.filter(r => { const x = pos(r); return x && x.slot === slot; }).sort((a, b) => pos(a).port - pos(b).port);
  const pic = n => { const b = boards.find(x => +x.slot === n && BOARD_KIND(x.type) === 'pic'), ps = portsIn(n);
    if (!b && !ps.length) return `<div class="cslot cempty"><span class="cnum">${n}</span><span class="clab">empty</span></div>`;
    return `<div class="cslot"><span class="cnum">${n}</span>${led(bstate(b || { online: 'Present' }))}<span class="clab" title="${esc(boardModel(hw, 'pic', n))}">${esc(boardModel(hw, 'pic', n) || (b ? b.type : 'PIC'))}${ps.length ? ` · ${ps.length} × ${esc(portSpeed(ps[0]))}` : ''}</span><div class="cages">${ps.map(cage).join('')}</div></div>`; };
  const mgmt = phys.filter(r => { const x = pos(r); return x && x.slot === 0; });
  const ipus = by('ipu').length ? by('ipu') : (m8 ? [{ slot: '9', type: 'IPU' }, { slot: '10', type: 'IPU' }] : []);
  const ipu = (b, i) => `<div class="cslot cipu"><span class="cnum">${esc(b.slot)}</span>${led(bstate(b))}<span class="clab">${esc(b.type)}${boardModel(hw, 'ipu', b.slot) ? ' · ' + esc(boardModel(hw, 'ipu', b.slot)) : ''}${b.primary ? ' · ' + esc(b.primary) : b.role && b.role !== 'NA' ? ' · ' + esc(b.role) : ''}</span>${i === 0 && mgmt.length ? `<div class="cages">${mgmt.map(cage).join('')}<span class="faint" style="font-size:10px;align-self:center">MGMT</span></div>` : ''}</div>`;
  const small = k => by(k).map(b => `<div class="cmini" title="${esc(`${b.type} slot ${b.slot}: ${b.online || ''} ${b.status || ''}`)}">${led(bstate(b))}<b>${esc(k.toUpperCase())} ${esc(b.slot)}</b>${b.type.toUpperCase() !== k.toUpperCase() ? `<span>${esc(b.type)}</span>` : ''}${b.primary ? `<span>${esc(b.primary)}</span>` : ''}</div>`).join('');
  const half = Math.ceil(pics.length / 2);
  return `<div class="chassis"><div class="cear"><i></i><i></i></div><div class="cbody">
    <div class="ctop"><b>${esc(MODEL || 'Router')}</b><span>front panel, laid out by slot number</span></div>
    <div class="cpics">${pics.map(pic).join('')}</div>
    ${ipus.length ? `<div class="cipus">${ipus.map(ipu).join('')}</div>` : ''}
    ${by('pwr').length + by('fan').length + by('clk').length ? `<div class="cminis">${small('pwr')}${small('fan')}${small('clk')}</div>` : ''}
  </div><div class="cear"><i></i><i></i></div></div>`;
}
/* ----- front panel photo: the router's own photo with each port marked on it, coloured live ----- */
let PANEL = null;    // {photo, version, marks: {port: [x, y]}, size} for the connected router
async function panelLoad() { try { PANEL = await api('/api/panel'); } catch (e) { if (e.stale) throw e; PANEL = { error: e.message, marks: {} }; }
  if (!PANEL.photo && !PANEL.removed && !PANEL.error && VENDOR === 'mikrotik' && !DEMO && ME && ME.role !== 'viewer') await makerPhoto(false).catch(() => {});
  await notesLoad(); return PANEL; }
/* no photo yet: use the maker's product photo of this model (fetched once by the server from the maker's site, kept on it) */
const MAKER_TRIED = new Set();
async function makerPhoto(manual) {
  const key = (localStorage.getItem('ne8000.router') || '') + '|' + VENDOR;
  if (!manual && MAKER_TRIED.has(key)) return; MAKER_TRIED.add(key);
  const res = await fetch('/api/panel/maker-photo', { headers: { 'X-Requested-With': 'ne8000' }, credentials: 'same-origin' });
  if (!res.ok) { const d = await res.json().catch(() => ({})); if (manual) throw new Error(d.error || 'No photo from the maker.'); return; }
  const source = res.headers.get('X-Photo-Source') || '', blob = await res.blob(), img = new Image();
  const url = await new Promise((ok, bad) => { const f = new FileReader(); f.onload = () => ok(f.result); f.onerror = () => bad(new Error('The maker\'s photo could not be read.')); f.readAsDataURL(blob); });
  await new Promise((ok, bad) => { img.onload = ok; img.onerror = () => bad(new Error('The maker\'s photo could not be opened.')); img.src = url; });
  const k = Math.min(1, 2000 / Math.max(img.naturalWidth, img.naturalHeight)), c = document.createElement('canvas');
  c.width = Math.round(img.naturalWidth * k); c.height = Math.round(img.naturalHeight * k);
  const g = c.getContext('2d'); g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height); g.drawImage(img, 0, 0, c.width, c.height);
  PANEL = await post('/api/panel/photo', { image: c.toDataURL('image/jpeg', 0.9), source });
  return PANEL;
}
/* ----- crop and straighten the front photo: find the router's front automatically, adjust the 4 corners, warp to a rectangle ----- */
async function photoCanvas(file, max = 2000) {
  const url = await new Promise((ok, bad) => { const f = new FileReader(); f.onload = () => ok(f.result); f.onerror = () => bad(new Error('Could not read that file.')); f.readAsDataURL(file); }), img = new Image();
  await new Promise((ok, bad) => { img.onload = ok; img.onerror = () => bad(new Error('That file is not a photo the browser can open. Use a JPEG or PNG.')); img.src = url; });
  const k = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight)), c = document.createElement('canvas');
  c.width = Math.round(img.naturalWidth * k); c.height = Math.round(img.naturalHeight * k);
  c.getContext('2d').drawImage(img, 0, 0, c.width, c.height); img.src = '';
  return c;
}
/* the busiest band of rows, then of columns: a router front is dense with edges (ports, LEDs, labels, screws), walls and racks are not */
function detectFront(src) {
  const W = 360, k = W / src.width, H = Math.max(8, Math.round(src.height * k)), c = document.createElement('canvas');
  c.width = W; c.height = H; const g = c.getContext('2d'); g.drawImage(src, 0, 0, W, H);
  const d = g.getImageData(0, 0, W, H).data, L = new Float32Array(W * H);
  for (let i = 0; i < W * H; i++) L[i] = d[i * 4] * 0.3 + d[i * 4 + 1] * 0.59 + d[i * 4 + 2] * 0.11;
  const E = new Float32Array(W * H);
  for (let y = 1; y < H - 1; y++) for (let x = 1; x < W - 1; x++) { const i = y * W + x; E[i] = Math.abs(L[i + 1] - L[i - 1]) + Math.abs(L[i + W] - L[i - W]); }
  const band = (prof, n) => {
    const sm = prof.map((_, i) => { let t = 0, m = 0; for (let j = Math.max(0, i - 3); j <= Math.min(n - 1, i + 3); j++) { t += prof[j]; m++; } return t / m; });
    const mx = Math.max(...sm), mn = Math.min(...sm), th = mn + 0.3 * (mx - mn), gap = Math.max(2, Math.round(n * 0.03));
    let best = [0, n - 1], bestScore = -1, a = -1, last = -1, score = 0;
    for (let i = 0; i <= n; i++) {
      if (i < n && sm[i] >= th) { if (a < 0) { a = i; score = 0; } last = i; score += sm[i]; }
      else if (a >= 0 && (i === n || i - last > gap)) { if (score > bestScore) { bestScore = score; best = [a, last]; } a = -1; }
    }
    return best;
  };
  const rows = Array.from({ length: H }, (_, y) => { let t = 0; for (let x = 0; x < W; x++) t += E[y * W + x]; return t; });
  let [y0, y1] = band(rows, H);
  const cols = Array.from({ length: W }, (_, x) => { let t = 0; for (let y = y0; y <= y1; y++) t += E[y * W + x]; return t; });
  let [x0, x1] = band(cols, W);
  // the edges mark the ports; the plain faceplate around them has the same colour: grow out to where the colour changes
  const med = a => { const b = Float32Array.from(a).sort(); return b[b.length >> 1] || 0; };
  const span = (ya, yb, xa, xb) => { const v = []; for (let y = ya; y <= yb; y += 2) for (let x = xa; x <= xb; x += 2) v.push(L[y * W + x]); return med(v); };
  const face = span(y0, y1, x0, x1), close = v => Math.abs(v - face) < 22;
  const rowMed = y => { const v = []; for (let x = x0; x <= x1; x++) v.push(L[y * W + x]); return med(v); };
  const colMed = x => { const v = []; for (let y = y0; y <= y1; y++) v.push(L[y * W + x]); return med(v); };
  const growY = Math.round((y1 - y0 + 1) * 0.6), growX = Math.round((x1 - x0 + 1) * 0.25);
  for (let n = 0; n < growY && y0 > 0 && close(rowMed(y0 - 1)); n++) y0--;
  for (let n = 0; n < growY && y1 < H - 1 && close(rowMed(y1 + 1)); n++) y1++;
  for (let n = 0; n < growX && x0 > 0 && close(colMed(x0 - 1)); n++) x0--;
  for (let n = 0; n < growX && x1 < W - 1 && close(colMed(x1 + 1)); n++) x1++;
  const px = Math.round(W * 0.006), py = Math.round(H * 0.01);
  let b = [Math.max(0, x0 - px), Math.max(0, y0 - py), Math.min(W - 1, x1 + px), Math.min(H - 1, y1 + py)];
  if ((b[2] - b[0]) * (b[3] - b[1]) < W * H * 0.04) b = [W * 0.05, H * 0.05, W * 0.95, H * 0.95];   // nothing clear found
  const s = 1 / k;
  return [[b[0] * s, b[1] * s], [b[2] * s, b[1] * s], [b[2] * s, b[3] * s], [b[0] * s, b[3] * s]];
}
/* projective map from the unit square (u, v) to the quad: x = (a u + b v + c) / (g u + h v + 1), y = (d u + e v + f) / (...) */
function quadMatrix(q) {
  const [[x0, y0], [x1, y1], [x2, y2], [x3, y3]] = q, sx = x0 - x1 + x2 - x3, sy = y0 - y1 + y2 - y3;
  if (Math.abs(sx) < 1e-6 && Math.abs(sy) < 1e-6) return [x1 - x0, x3 - x0, x0, y1 - y0, y3 - y0, y0, 0, 0];
  const dx1 = x1 - x2, dx2 = x3 - x2, dy1 = y1 - y2, dy2 = y3 - y2, den = dx1 * dy2 - dx2 * dy1;
  const g = (sx * dy2 - dx2 * sy) / den, h = (dx1 * sy - sx * dy1) / den;
  return [x1 - x0 + g * x1, x3 - x0 + h * x3, x0, y1 - y0 + g * y1, y3 - y0 + h * y3, y0, g, h];
}
/* where a point of the original photo lands in the cropped one (u, v from 0 to 1), so port marks survive a re-crop */
function quadInverse(q, x, y) {
  const [a, b, c, d, e, f, g, h] = quadMatrix(q), M = [[a, b, c], [d, e, f], [g, h, 1]];
  const det = M[0][0] * (M[1][1] * M[2][2] - M[1][2] * M[2][1]) - M[0][1] * (M[1][0] * M[2][2] - M[1][2] * M[2][0]) + M[0][2] * (M[1][0] * M[2][1] - M[1][1] * M[2][0]);
  const inv = [[M[1][1] * M[2][2] - M[1][2] * M[2][1], M[0][2] * M[2][1] - M[0][1] * M[2][2], M[0][1] * M[1][2] - M[0][2] * M[1][1]],
    [M[1][2] * M[2][0] - M[1][0] * M[2][2], M[0][0] * M[2][2] - M[0][2] * M[2][0], M[0][2] * M[1][0] - M[0][0] * M[1][2]],
    [M[1][0] * M[2][1] - M[1][1] * M[2][0], M[0][1] * M[2][0] - M[0][0] * M[2][1], M[0][0] * M[1][1] - M[0][1] * M[1][0]]].map(r => r.map(v => v / det));
  const z = inv[2][0] * x + inv[2][1] * y + inv[2][2];
  return [(inv[0][0] * x + inv[0][1] * y + inv[0][2]) / z, (inv[1][0] * x + inv[1][1] * y + inv[1][2]) / z];
}
/* the quad (top-left, top-right, bottom-right, bottom-left) warped to a straight rectangle */
function warpQuad(src, q, max = 1600) {
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  let w = Math.max(dist(q[0], q[1]), dist(q[3], q[2])), h = Math.max(dist(q[0], q[3]), dist(q[1], q[2]));
  const k = Math.min(1, max / Math.max(w, h)); w = Math.max(8, Math.round(w * k)); h = Math.max(8, Math.round(h * k));
  const [a, b, c, d, e, f, g, hh] = quadMatrix(q);
  const S = src.getContext('2d').getImageData(0, 0, src.width, src.height), sd = S.data, SW = src.width, SH = src.height;
  const out = document.createElement('canvas'); out.width = w; out.height = h;
  const O = out.getContext('2d').createImageData(w, h), od = O.data;
  for (let y = 0; y < h; y++) { const v = (y + 0.5) / h;
    for (let x = 0; x < w; x++) { const u = (x + 0.5) / w, z = g * u + hh * v + 1, X = (a * u + b * v + c) / z - 0.5, Y = (d * u + e * v + f) / z - 0.5;
      const xi = Math.max(0, Math.min(SW - 2, Math.floor(X))), yi = Math.max(0, Math.min(SH - 2, Math.floor(Y))), fx = Math.max(0, Math.min(1, X - xi)), fy = Math.max(0, Math.min(1, Y - yi));
      const i00 = (yi * SW + xi) * 4, i10 = i00 + 4, i01 = i00 + SW * 4, i11 = i01 + 4, o = (y * w + x) * 4;
      for (let ch = 0; ch < 3; ch++) od[o + ch] = (sd[i00 + ch] * (1 - fx) + sd[i10 + ch] * fx) * (1 - fy) + (sd[i01 + ch] * (1 - fx) + sd[i11 + ch] * fx) * fy;
      od[o + 3] = 255; } }
  out.getContext('2d').putImageData(O, 0, 0);
  return out;
}
let cropCancel = null;
/* shows the photo with 4 draggable corners on the detected front; resolves to {image (JPEG data URL), q (the corners)}, or null if cancelled */
function cropPhoto(src, startFull) {
  return new Promise(done => {
    const m = $('#modal'), q0 = detectFront(src), full = [[0, 0], [src.width, 0], [src.width, src.height], [0, src.height]];
    let q = (startFull ? full : q0).map(p => [...p]);
    m.classList.add('wide');
    m.innerHTML = `<h2>Crop and straighten</h2><p class="muted">${startFull ? 'Drag' : "The router's front was found automatically. Drag"} the four corners onto the corners of the front panel if needed; everything outside is cut away, and an angled photo is straightened.</p>
      <div class="cropwrap"><canvas class="cropimg"></canvas><svg class="cropsvg"></svg></div>
      <div class="actions"><button class="btn" data-a="auto">Find automatically</button><button class="btn" data-a="full">Whole photo</button><span class="sp" style="flex:1"></span><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="ok">Crop and save</button></div>`;
    $('#modalBg').classList.remove('hidden');
    const cv = m.querySelector('.cropimg'), svg = m.querySelector('.cropsvg');
    const vw = Math.min(m.clientWidth - 40, 880), k = Math.min(vw / src.width, (window.innerHeight * 0.6) / src.height, 1);
    cv.width = Math.round(src.width * k); cv.height = Math.round(src.height * k); cv.getContext('2d').drawImage(src, 0, 0, cv.width, cv.height);
    svg.setAttribute('width', cv.width); svg.setAttribute('height', cv.height); svg.setAttribute('viewBox', `0 0 ${cv.width} ${cv.height}`);
    const draw = () => { const p = q.map(([x, y]) => [x * k, y * k]);
      svg.innerHTML = `<path d="M0 0H${cv.width}V${cv.height}H0Z M${p.map(v => v.join(' ')).join(' L')}Z" fill="rgba(0,0,0,.55)" fill-rule="evenodd"/><polygon points="${p.map(v => v.join(',')).join(' ')}" fill="none" stroke="#3ccf7f" stroke-width="2"/>` +
        p.map((v, i) => `<circle data-i="${i}" cx="${v[0]}" cy="${v[1]}" r="11" fill="rgba(60,207,127,.35)" stroke="#fff" stroke-width="2" style="cursor:grab;touch-action:none"/>`).join(''); };
    draw(); window._cropQuad = () => q;
    let drag = -1;
    svg.onpointerdown = e => { const c = e.target.closest('circle'); if (!c) return; drag = +c.dataset.i; svg.setPointerCapture(e.pointerId); e.preventDefault(); };
    svg.onpointermove = e => { if (drag < 0) return; const r = svg.getBoundingClientRect();
      q[drag] = [Math.max(0, Math.min(src.width, (e.clientX - r.left) / k)), Math.max(0, Math.min(src.height, (e.clientY - r.top) / k))]; draw(); };
    svg.onpointerup = svg.onpointercancel = () => { drag = -1; };
    const finish = v => { cropCancel = null; m.classList.remove('wide'); closeModal(); done(v); };
    cropCancel = () => { cropCancel = null; m.classList.remove('wide'); done(null); };
    m.querySelector('.actions').onclick = e => { const a = e.target.dataset.a;
      if (a === 'auto') { q = q0.map(p => [...p]); draw(); }
      if (a === 'full') { q = full.map(p => [...p]); draw(); }
      if (a === 'c') finish(null);
      if (a === 'ok') finish({ image: warpQuad(src, q).toDataURL('image/jpeg', 0.88), q }); };
  });
}
/* the marker takes the shape of the port: a square RJ45 jack, a wide SFP cage, a wider QSFP cage */
function portShape(p, hw) {
  const n = String(p.name), o = ((hw && hw.optics) || {})[n], g = /^(?:\d+\|)?(\d+)GE/i.exec(n);
  const sp = g ? +g[1] * 1000 : /^(xgigabit|xg)/i.test(n) ? 10000 : (p.speed_mbps && p.speed_mbps < 4e6 ? p.speed_mbps : 0);
  if (/^qsfp/i.test(n) || sp >= 40000) return 'qsfp';
  if (o || /^(sfp|xgigabit|xg)/i.test(n) || sp >= 10000) return 'sfp';
  return 'rj45';       // ether1, MEth, GigabitEthernet without an optic: a copper jack
}
function photoHtml(phys, st, hw) {
  const P = PANEL || {}, edit = !(ME && ME.role === 'viewer'), m = st.mark;
  if (P.error) return `<div class="pnophoto muted">${esc(P.error)}</div>`;
  const pick = `<label class="btn primary">${P.photo ? 'New photo' : 'Take or upload a photo'}<input type="file" accept="image/*" capture="environment" class="pfile" hidden></label>`;
  const maker = edit && VENDOR === 'mikrotik' && !DEMO ? '<button class="btn" data-a="maker">Use the maker\'s photo</button>' : '';
  if (!P.photo) return `<div class="pnophoto"><b>Show your real router here.</b><span class="muted">Take a straight-on photo of the router's front with every port visible (on a phone this opens the camera), then tap where each port is, once. After that every port lights up on your photo with its live state.</span>${edit ? pick + maker : '<span class="muted">An operator or admin can add the photo.</span>'}</div>`;
  const credit = P.source ? `<div class="faint pcredit" style="font-size:11px;margin-top:4px">Product photo from <a href="${esc(P.source)}" target="_blank" rel="noopener noreferrer">${esc(new URL(P.source).hostname)}</a>. Replace it with your own photo for the real port layout and cabling.</div>` : '';
  const marks = m ? m.marks : P.marks || {}, size = m ? m.size : P.size || 4;
  const short = n => (/(\d+\/\d+\/\d+)$/.exec(n) || [, n])[1];
  const pins = phys.filter(p => marks[p.name]).map(p => { const [x, y] = marks[p.name];
    return `<button class="pmark pm-${portShape(p, hw)} ps-${portState(p)}${m && phys[m.i] && phys[m.i].name === p.name ? ' cur' : ''}" data-p="${esc(p.name)}" style="left:${x * 100}%;top:${y * 100}%" title="${esc(portTip(p) + noteTip(p.name))}"><span>${esc(short(p.name))}${notesFor(p.name).length ? ' 📝' : ''}</span></button>`; }).join('');
  const missing = phys.filter(p => !(P.marks || {})[p.name]);
  const cur = m && phys[m.i];
  const tools = m ? `<div class="pmarkbar"><span>${cur ? `Tap where <b>${esc(cur.name)}</b> is on the photo <span class="muted">(${m.i + 1} of ${phys.length}${cur.description ? ', ' + esc(cur.description) : ''})</span>` : '<b>All ports placed.</b> Save to keep them.'}</span><span class="sp"></span>
      <label class="muted" style="font-size:12px">Marker size <input type="range" class="psize" min="1.5" max="12" step="0.5" value="${size}"></label>
      <button class="btn sm" data-a="m-back" ${m.i ? '' : 'disabled'}>Back</button>${cur ? '<button class="btn sm" data-a="m-skip">Skip</button>' : ''}<button class="btn sm" data-a="m-cancel">Cancel</button><button class="btn sm primary" data-a="m-save">Save</button></div>`
    : `<div class="bar pphototools">${missing.length ? `<span class="muted" style="font-size:12px">Not marked yet: ${esc(missing.map(p => p.name).join(', '))}</span>` : `<span class="muted" style="font-size:12px">Hover over a port for its traffic and optic, and click it for details.</span>`}<span class="sp"></span>
      ${edit ? `<button class="btn sm${missing.length === phys.length ? ' primary' : ''}" data-a="mark">Mark ports</button><button class="btn sm" data-a="crop">Crop / straighten</button>${pick.replace('btn primary', 'btn sm')}<button class="btn sm ghost" data-a="del">Remove photo</button>` : ''}</div>`;
  return `<div class="pphoto${m ? ' marking' : ''}" style="--ms:${size}"><img src="/api/panel/photo?v=${P.version}" alt="Front of the router" draggable="false">${pins}</div>${credit}${tools}`;
}
/* ----- device notes: what engineers need to know about this router, beside its picture ----- */
let NOTES = null;
async function notesLoad() { try { NOTES = (await api('/api/notes')).notes; } catch (e) { if (e.stale) throw e; NOTES = []; } return NOTES; }
const notesFor = name => (NOTES || []).filter(n => n.port === name);
const noteTip = name => notesFor(name).map(n => `\n📝 ${n.text.split('\n')[0].slice(0, 120)}`).join('');
function noteWhen(t) { const d = new Date(t * 1000); return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric' }) + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }); }
/* draws into box and handles its own clicks, so the 15 s refresh of the ports never wipes a note being typed */
function notesPanel(box, phys, onChange) {
  const edit = !(ME && ME.role === 'viewer'), st = box._n || (box._n = { editing: null });
  const opts = sel => `<option value="">Whole device</option>${phys.map(p => `<option value="${esc(p.name)}" ${sel === p.name ? 'selected' : ''}>${esc(p.name)}${p.description ? ' · ' + esc(p.description) : ''}</option>`).join('')}`;
  const list = NOTES || [];
  box.innerHTML = `<h4>Device notes <span class="muted">${list.length || ''}</span></h4>
    ${edit ? `<div class="nadd"><textarea class="ntext" rows="3" placeholder="What should the next engineer know? e.g. 0/4/1 patched to ODF tray 3, do not move."></textarea>
      <div class="nrow"><select class="nport">${opts('')}</select><label class="muted"><input type="checkbox" class="npin"> Pin</label><button class="btn sm primary" data-n="add">Add note</button></div></div>` : ''}
    <div class="nlist">${list.map(n => st.editing === n.id ? `<div class="note${n.pinned ? ' pinned' : ''}" data-id="${n.id}"><textarea class="netext" rows="4">${esc(n.text)}</textarea>
        <div class="nrow"><select class="neport">${opts(n.port)}</select><button class="btn sm" data-n="cancel">Cancel</button><button class="btn sm primary" data-n="save">Save</button></div></div>`
      : `<div class="note${n.pinned ? ' pinned' : ''}" data-id="${n.id}">${n.pinned ? '<span class="npinned">📌 Pinned</span>' : ''}${n.port ? `<span class="nporttag">${esc(n.port)}</span>` : ''}<div class="ntxt">${esc(n.text)}</div>
        <div class="nmeta">${esc(n.by)} · ${noteWhen(n.at)}${n.edited_at ? ` · edited by ${esc(n.edited_by)} ${noteWhen(n.edited_at)}` : ''}${edit ? `<span class="sp"></span><button class="lnk" data-n="pin">${n.pinned ? 'Unpin' : 'Pin'}</button><button class="lnk" data-n="edit">Edit</button><button class="lnk" data-n="del">Delete</button>` : ''}</div></div>`).join('')
      || '<div class="muted nempty">No notes yet. Write down anything crucial: patching, spare slots, replaced parts, who to call.</div>'}</div>`;
  const done = x => { NOTES = x.notes; st.editing = null; notesPanel(box, phys, onChange); onChange && onChange(); };
  box.onclick = e => { const b = e.target.closest('[data-n]'); if (!b) return; const k = b.dataset.n, id = (b.closest('[data-id]') || {}).dataset?.id;
    const fail = x => toast(x.message, true);
    if (k === 'add') post('/api/notes', { text: box.querySelector('.ntext').value, port: box.querySelector('.nport').value, pinned: box.querySelector('.npin').checked }).then(done).catch(fail);
    if (k === 'edit') { st.editing = id; notesPanel(box, phys, onChange); box.querySelector('.netext').focus(); }
    if (k === 'cancel') { st.editing = null; notesPanel(box, phys, onChange); }
    if (k === 'save') post(`/api/notes/${id}/update`, { text: box.querySelector('.netext').value, port: box.querySelector('.neport').value }).then(done).catch(fail);
    if (k === 'pin') post(`/api/notes/${id}/update`, { pinned: !(NOTES.find(n => n.id === id) || {}).pinned }).then(done).catch(fail);
    if (k === 'del' && confirm('Delete this note?')) post(`/api/notes/${id}/delete`).then(done).catch(fail); };
}
/* opts.onPort(name): what a click on a physical port does (default: open its drawer) */
function portPanel(host, rows, opts = {}) {
  if (!host) return;
  const st = host._p || (host._p = { chip: 'all' });
  let saved = ''; try { saved = localStorage.getItem('ne8000.portview') || ''; } catch (x) {}
  const view = st.view || saved || (PANEL && PANEL.photo ? 'photo' : 'drawing');
  const phys = rows.filter(r => ['ethernet', 'management'].includes(r.type) && !r.name.includes('.'));
  const subsOf = p => rows.filter(r => r.name.startsWith(p.name + '.')).sort((a, b) => (+a.name.split('.')[1] || 0) - (+b.name.split('.')[1] || 0));
  const keep = r => st.chip === 'all' || (st.chip === 'up' ? portState(r) === 'up' : st.chip === 'down' ? portState(r) !== 'up' : st.chip === 'busy' ? portLoad(r) >= 65 : r.in_errors + r.out_errors > 0);
  const lc = v => v >= 85 ? 'var(--bad)' : v >= 65 ? 'var(--warn)' : 'var(--s1)';
  const bar = v => `<span class="pbar"><i style="width:${Math.min(100, Math.max(v ? 3 : 0, v))}%;background:${lc(v)}"></i></span>`;
  const tile = r => { const v = portLoad(r), s = portState(r);
    return `<button class="pt ps-${s}" data-n="${esc(r.name)}" title="${esc(portTip(r))}"><span class="pn">${esc(r.name.split('.')[1])}</span>${r.in_errors + r.out_errors ? '<span class="pe"></span>' : ''}${bar(v)}<span class="pd">${esc(r.description || '-')}</span></button>`; };
  /* the front panel: every physical port as one tile, like a switch */
  const face = p => { const v = portLoad(p), s = portState(p), n = subsOf(p).length, m = /(\d+\/\d+\/\d+)$/.exec(p.name);
    return `<button class="pt pfp ps-${s}" data-p="${esc(p.name)}" title="${esc(portTip(p) + (n ? `\n${n} sub-interfaces` : ''))}"><span class="pn"><span class="psp2">${esc(portSpeed(p))}</span> ${esc(m ? m[1] : p.name)}</span>${p.in_errors + p.out_errors ? '<span class="pe"></span>' : ''}${bar(v)}<span class="pd">${esc(p.description || (s === 'down' ? 'not connected' : '-'))}${n ? ` · ${n} subs` : ''}</span></button>`; };
  const port = p => { const subs = subsOf(p), shown = subs.filter(keep), s = portState(p), up = subs.filter(r => portState(r) === 'up').length;
    return `<div class="pport"><button class="pface ps-${s}" data-p="${esc(p.name)}" title="${esc(portTip(p))}"><span class="psp">${esc(portSpeed(p))}</span><b>${esc(p.name)}</b>
      <span class="pdesc">${esc(p.description || '')}</span><span class="pio">In ${bar(+p.in_util || 0)} ${(+p.in_util || 0).toFixed(1)}%</span><span class="pio">Out ${bar(+p.out_util || 0)} ${(+p.out_util || 0).toFixed(1)}%</span>
      <span class="psub">${PORT_STATE[s]}${subs.length ? ` · ${subs.length} sub-interfaces, ${up} up` : ''}${p.in_errors + p.out_errors ? ` · <span style="color:var(--bad)">errors ${p.in_errors}/${p.out_errors}</span>` : ''}</span></button>
      <div class="ptiles">${shown.map(tile).join('') || `<span class="faint" style="font-size:12px;padding:6px">${subs.length ? 'None match this filter.' : 'No sub-interfaces.'}</span>`}</div></div>`; };
  const count = k => k === 'all' ? '' : ` ${rows.filter(r => r.name.includes('.') && phys.some(p => r.name.startsWith(p.name + '.'))).filter(r => { const o = st.chip; st.chip = k; const y = keep(r); st.chip = o; return y; }).length}`;
  const keepNotes = host.querySelector('.pnotes');
  host.innerHTML = `<div class="bar"><div class="seg pchips">${PORT_CHIPS.map(([k, l]) => `<button data-c="${k}" class="${st.chip === k ? 'on' : ''}">${l}${count(k)}</button>`).join('')}</div><span class="sp"></span>
    <div class="legend plegend"><span><i class="ps-up"></i>Up</span><span><i class="ps-half"></i>Protocol down</span><span><i class="ps-down"></i>Down</span><span><i class="ps-off"></i>Disabled</span><span><i class="pe"></i>Errors</span><span>bar = traffic</span></div></div>
    <div class="bar pviewbar"><div class="seg pview">${[['drawing', 'Drawing'], ['photo', 'Photo']].map(([k, l]) => `<button data-v="${k}" class="${view === k ? 'on' : ''}">${l}</button>`).join('')}</div></div>
    <div class="pfront"><div class="pfview">${view === 'photo' ? photoHtml(phys, st, opts.hw) : (opts.hw && chassisHtml(rows, opts.hw)) || `<div class="pfplate">${phys.map(face).join('') || '<span class="muted">No physical ports found.</span>'}</div>`}</div><div class="pnotes"></div></div>
    <div class="pports">${phys.filter(p => subsOf(p).length).map(port).join('')}</div>`;
  host.querySelector('.pchips').onclick = e => { const b = e.target.closest('[data-c]'); if (b) { st.chip = b.dataset.c; portPanel(host, rows, opts); } };
  const again = () => portPanel(host, rows, opts);
  const slot = host.querySelector('.pnotes');
  if (keepNotes && keepNotes.dataset.ready) slot.replaceWith(keepNotes);
  else if (NOTES) { slot.dataset.ready = '1'; notesPanel(slot, phys, again); }
  const fail = e => toast(e.message, true);
  host.onclick = e => {
    const v = e.target.closest('.pview [data-v]');
    if (v) { st.view = v.dataset.v; try { localStorage.setItem('ne8000.portview', st.view); } catch (x) {} return again(); }
    const a = e.target.closest('[data-a]');
    if (a) {
      const m = st.mark, k = a.dataset.a;
      if (k === 'mark') { st.mark = { i: 0, marks: { ...(PANEL.marks || {}) }, size: PANEL.size || 4 }; return again(); }
      if (k === 'm-back') { m.i = Math.max(0, m.i - 1); return again(); }
      if (k === 'm-skip') { m.i++; return again(); }
      if (k === 'm-cancel') { st.mark = null; return again(); }
      if (k === 'm-save') { post('/api/panel/marks', { marks: m.marks, size: m.size }).then(x => { PANEL = x; st.mark = null; toast('Port positions saved'); again(); }).catch(fail); return; }
      if (k === 'crop') { (async () => {
        const img = new Image(); await new Promise((ok, bad) => { img.onload = ok; img.onerror = () => bad(new Error('Could not load the photo.')); img.src = '/api/panel/photo?v=' + PANEL.version; });
        const src = document.createElement('canvas'); src.width = img.naturalWidth; src.height = img.naturalHeight; src.getContext('2d').drawImage(img, 0, 0);
        const c = await cropPhoto(src, true); if (!c) return;
        const old = PANEL.marks || {}, size = PANEL.size || 4, marks = {};
        for (const [n, [x, y]] of Object.entries(old)) { const [u, v] = quadInverse(c.q, x * src.width, y * src.height); if (u >= 0 && u <= 1 && v >= 0 && v <= 1) marks[n] = [u, v]; }
        PANEL = await post('/api/panel/photo', { image: c.image, source: PANEL.source || '' });
        // the panel is now a smaller part of the picture, so the rings scale up with it
        const w0 = Math.hypot(c.q[1][0] - c.q[0][0], c.q[1][1] - c.q[0][1]);
        if (Object.keys(marks).length) PANEL = await post('/api/panel/marks', { marks, size: Math.min(12, size * src.width / Math.max(1, w0)) });
        const lost = Object.keys(old).length - Object.keys(marks).length;
        toast(lost ? `Cropped. ${lost} port mark${lost > 1 ? 's were' : ' was'} outside the crop; mark ${lost > 1 ? 'them' : 'it'} again.` : 'Cropped; port marks kept.'); again();
      })().catch(fail); return; }
      if (k === 'maker') { toast('Getting the maker\'s photo…'); makerPhoto(true).then(() => { st.view = 'photo'; again(); }).catch(fail); return; }
      if (k === 'del') { if (confirm('Remove the photo and the port positions?')) post('/api/panel/delete').then(x => { PANEL = x; st.mark = null; again(); }).catch(fail); return; }
      return;
    }
    const img = e.target.closest('.pphoto.marking img, .pphoto.marking .pmark');
    if (img && st.mark) { const m = st.mark, cur = phys[m.i]; if (!cur) return;
      const r = host.querySelector('.pphoto img').getBoundingClientRect();
      m.marks[cur.name] = [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
      m.i++; return again(); }
    const t = e.target.closest('.pports [data-n],.pports [data-p],.pfplate [data-p],.chassis [data-p],.pphoto:not(.marking) [data-p]'); if (!t) return;
    if (t.dataset.n) interfaceDrawer(t.dataset.n); else (opts.onPort || interfaceDrawer)(t.dataset.p); };
  host.onchange = async e => {
    if (!e.target.classList.contains('pfile') || !e.target.files[0]) return;
    const file = e.target.files[0]; e.target.value = '';
    try { const c = await cropPhoto(await photoCanvas(file)); if (!c) return;
      toast('Uploading the photo…'); PANEL = await post('/api/panel/photo', { image: c.image });
      st.mark = { i: 0, marks: {}, size: PANEL.size || 4 }; again(); } catch (x) { fail(x); }
  };
  host.oninput = e => { if (e.target.classList.contains('psize') && st.mark) { st.mark.size = +e.target.value; host.querySelector('.pphoto').style.setProperty('--ms', st.mark.size); } };
}

/* ----- interfaces ----- */
let ifCache = [];
PAGES.interfaces = async c => {
  const rows = ifCache = await api('/api/view/interfaces');
  await Promise.all([ipOffLoad(), arpLoad()]);
  const kind = c._kind || 'all';
  c.innerHTML = head('Interfaces', '', `<button class="btn" onclick="subifForm()">+ Sub-interface</button><button class="btn" onclick="loopbackForm()">+ Loopback</button>`) +
    `<div class="card"><h3>Ports <span class="muted" style="font-weight:400;font-size:12px">each physical port with its sub-interfaces. Click a tile for details, a port to list it below.</span></h3><div id="ifPorts"></div></div>
    <div class="card"><div class="bar"><div class="seg" id="ifKind">${[['all', 'All'], ['ethernet', 'Physical'], ['sub-interface', 'Sub-interfaces'], ['logical', 'Logical']].map(([k, l]) => `<button data-k="${k}" class="${k === kind ? 'on' : ''}">${l}</button>`).join('')}</div>
    <span class="muted" style="font-size:12px">Click a row for live traffic, counters and settings.</span></div><div id="ifTable"></div></div>`;
  $('#ifKind').onclick = e => { if (e.target.dataset.k) { c._kind = e.target.dataset.k; PAGES.interfaces(c); } };
  const pick = r => kind === 'all' || (kind === 'logical' ? ['loopback', 'vlanif', 'null', 'tunnel', 'virtual', 'eth-trunk'].includes(r.type) : kind === 'ethernet' ? ['ethernet', 'management'].includes(r.type) : r.type === kind);
  const draw = rows => table($('#ifTable'), [
    { t: 'Name', k: 'name', f: r => `<b>${esc(r.name)}</b>` },
    { t: 'Description', k: 'description', f: r => r.type === 'null' ? esc(r.description) : descCell(r.name, r.description) },
    { t: 'Type', k: 'type', f: r => `<span class="muted">${esc(r.type)}</span>` },
    { t: 'Physical', k: 'phy', f: r => pill(r.phy) }, { t: 'Protocol', k: 'protocol', f: r => pill(r.protocol) },
    { t: 'IP address', k: 'ip', f: ipCell }, { t: 'Client (ARP)', k: 'client', cls: 'mono nb', f: r => r.l2 || r.type === 'null' ? '' : nbHtml(r._nb || [], r.ip ? 'none in ARP' : '') }, { t: 'VPN', k: 'vpn', f: r => `<span class="muted">${esc(r.vpn)}</span>` }, { t: 'VLAN', k: 'vlan' },
    { t: 'In', k: 'in_util', f: r => ubar(r.in_util) }, { t: 'Out', k: 'out_util', f: r => ubar(r.out_util) },
    { t: 'Errors', k: 'in_errors', num: true, f: r => r.in_errors + r.out_errors ? `<span style="color:var(--bad)">${r.in_errors}/${r.out_errors}</span>` : '0' },
    { t: '', sort: false, cls: 'acts', f: r => r.type === 'null' ? '' : (r.admin_down ? `<button class="btn sm" onclick='ifToggle(${attr(r.name)},true)'>Enable</button>` : `<button class="btn sm" onclick='ifToggle(${attr(r.name)},false)'>Disable</button>`) + `<button class="btn sm" onclick='ifEdit(${attr(r.name)})'>Edit</button>` },
  ], withClients(rows).filter(pick), { onRow: r => interfaceDrawer(r.name), rowClass: r => r.admin_down ? 'off' : '' });
  let hw = null; Promise.all([api('/api/view/hardware').catch(() => null), panelLoad()]).then(([h]) => { hw = h; ports(ifCache); });
  const ports = rows => portPanel($('#ifPorts'), rows, { hw, onPort: name => { const t = $('#ifTable'); t._t.q = name; draw(ifCache); t.scrollIntoView({ behavior: 'smooth', block: 'start' }); } });
  draw(rows); ports(rows);
  every(5000, async () => { if (!$('#modalBg').classList.contains('hidden')) return; ifCache = await api('/api/view/interfaces'); await arpLoad(); draw(ifCache); ports(ifCache); });
};
/* adds the clients behind each row (for display) and their IP/MACs as text (so the filter box finds a MAC in any format) */
function withClients(rows) { return rows.map(r => { const n = r.ip ? clientsOf(r) : []; return Object.assign(r, { _nb: n, client: nbSearch(n) }); }); }
/* description text with a rename button; the button does not open the row's drawer */
function descCell(name, d) { return `<span class="dsc">${esc(d || '')}<button class="btn sm ghost dren" title="Rename the description of ${esc(name)}" onclick='event.stopPropagation();descForm(${attr(name)})'>✎</button></span>`; }
function descForm(name) {
  const r = ifCache.find(x => x.name === name) || {};
  change({ title: `Rename description of ${name}`, action: 'interface.set', params: { interface: name },
    intro: r.description ? `Now: ${r.description}` : 'This interface has no description yet.',
    fields: [{ k: 'description', label: 'New description', value: r.description || '', help: 'Leave empty to remove the description.' }] });
}
function ifToggle(name, on) { change({ title: `${on ? 'Enable' : 'Disable'} ${name}`, action: 'interface.set', params: { interface: name, enabled: on }, intro: on ? '' : 'Traffic on this interface will stop.' }); }
function ifEdit(name) {
  const r = ifCache.find(x => x.name === name) || {};
  change({ title: `Edit ${name}`, action: 'interface.set', params: { interface: name }, fields: [{ k: 'description', label: 'Description', value: r.description }, { k: 'mtu', label: 'MTU', type: 'number', value: r.mtu, ph: '1500', help: 'Leave empty to keep the current MTU.' }] });
}
function ifIpForm(name) {
  const r = ifCache.find(x => x.name === name) || {};
  change({ title: `Add IP address to ${name}`, action: 'interface.ip_add', params: { interface: name }, warn: p => ipWarn(p.address, name),
    intro: r.ip ? `${name} already has ${r.ip} as its primary address, so the new one is added as secondary (sub). To replace the primary, use Change on ${r.ip}.` : '',
    fields: [{ k: 'address', label: 'Address/prefix', ph: '10.0.0.1/30' }, { k: 'secondary', label: 'Secondary (sub)', type: 'check', value: !!r.ip }, { k: 'vpn', label: 'Bind VPN instance', ph: 'optional', help: 'Binding a VPN instance removes existing IP addresses on the interface.' }] });
}
/* ----- every address on one interface, in a dropdown: change, remove, add ----- */
let arpCache = { at: 0, rows: [] };    // the router's ARP table, to show the client (neighbour) behind each address
async function arpLoad() { if (Date.now() - arpCache.at < 30000) return arpCache.rows; try { arpCache = { at: Date.now(), rows: (await api('/api/view/arp')).entries || [] }; } catch (e) { if (e.stale) throw e; } return arpCache.rows; }
/* ARP entries in this address's subnet, other than the router's own address: the client side of the link */
function ipNeighbours(cidr, ifname) {
  const x = ip4(cidr); if (!x) return [];
  // the router's own addresses (CLI type I, SNMP type O) and the subnet's network/broadcast addresses are not clients
  const own = new Set(ifCache.flatMap(r => [r.ip, ...(r.secondary || [])]).filter(Boolean).map(c => c.split('/')[0]));
  own.add(cidr.split('/')[0]);
  const bcast = (x.net | (~x.mask >>> 0)) >>> 0;
  const list = arpCache.rows.filter(a => { const y = ip4(a.ip); return y && !own.has(a.ip) && a.type !== 'O' && !(x.p < 31 && (y.a === x.net || y.a === bcast)) &&
    ((y.a & x.mask) >>> 0) === x.net && (!ifname || !a.interface || a.interface === '--' || a.interface === ifname || a.interface.split('.')[0] === ifname.split('.')[0]); });
  const dead = a => /incomplete/i.test(a.mac || '') || !a.mac;
  return list.map(a => dead(a) ? { ...a, mac: 'no answer (Incomplete)' } : a).sort((p, q) => dead(p) - dead(q));
}
function nbText(list) { return list.map(a => `${a.ip} ${macColon(a.mac)}`).join(', '); }
/* 04f4-1c51-8e81 (router) -> 04:F4:1C:51:8E:81 (how most people write it); anything else is left alone */
function macColon(m) { const h = String(m || '').replace(/[-:.]/g, ''); return /^[0-9a-f]{12}$/i.test(h) ? h.match(/../g).join(':').toUpperCase() : String(m || ''); }
function nbHtml(n, none) {
  return n.length ? `<span title="${esc(n.map(a => `${a.ip} ${macColon(a.mac)}${macColon(a.mac) !== a.mac ? ` (${a.mac})` : ''}`).join('\n'))}">${esc(n[0].ip)} · <b>${esc(macColon(n[0].mac))}</b>${n.length > 1 ? ` <span class="faint">+${n.length - 1}</span>` : ''}</span>` : `<span class="faint">${none}</span>`;
}
/* every client behind an interface's IPv4 addresses (primary and secondaries) */
function clientsOf(r) { return [r.ip, ...(r.secondary || [])].filter(Boolean).flatMap(c => ipNeighbours(c, r.name)); }
/* searchable text: IP plus the MAC in colon, router and bare form, so the table filter finds any of them */
function nbSearch(n) { return n.map(a => { const h = String(a.mac || '').replace(/[-:.]/g, ''); return `${a.ip} ${macColon(a.mac)} ${a.mac} ${h}`; }).join(' '); }
let ipOff = {};    // addresses taken off with Disable, by interface: [{ip, sub, by, at, reason}]
async function ipOffLoad() { try { ipOff = await api('/api/ip/disabled'); } catch (e) { if (e.stale) throw e; ipOff = {}; } }
function ipsOf(r, withOff) {
  const on = [...(r.ip ? [{ ip: r.ip, sub: false }] : []), ...(r.secondary || []).map(ip => ({ ip, sub: true })), ...(r.ipv6 || []).map(ip => ({ ip, v6: true }))];
  const off = withOff ? (ipOff[r.name] || []).filter(x => !on.some(y => y.ip === x.ip)).map(x => ({ ...x, off: true })) : [];
  return [...on, ...off];
}
function ipCell(r) {
  const n = ipsOf(r, true).length, off = ipsOf(r, true).filter(x => x.off).length;
  if (n < 2 && !off) return `<span class="mono">${esc(r.ip || (r.ipv6 || [])[0] || '')}</span>`;
  return `<button class="ipdd" title="all ${n} addresses on ${esc(r.name)}" onclick='event.stopPropagation();ipMenu(this,${attr(r.name)})'><span class="mono">${esc(r.ip || (r.ipv6 || [])[0] || 'none active')}</span><span class="ipn">${n - off} IP${n - off === 1 ? '' : 's'}${off ? ` · ${off} off` : ''}</span>▾</button>`;
}
function ip4(s) {
  const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)(?:\/(\d+))?$/.exec(String(s || '').trim()); if (!m) return null;
  const a = ((+m[1] << 24) >>> 0) + (+m[2] << 16) + (+m[3] << 8) + +m[4], p = m[5] == null ? 32 : +m[5], mask = p ? (0xffffffff << (32 - p)) >>> 0 : 0;
  return { a, p, mask, net: (a & mask) >>> 0 };
}
function ipNet(cidr) { const x = ip4(cidr); if (!x) return ''; const n = x.net; return `${n >>> 24}.${n >> 16 & 255}.${n >> 8 & 255}.${n & 255}/${x.p}`; }
/* the new address overlaps one already on the router (on another interface, or another address on this one) */
function ipWarn(addr, name, except) {
  const x = ip4(addr); if (!x) return '';
  const hits = [];
  for (const r of ifCache) for (const { ip, v6 } of ipsOf(r)) {
    if (v6 || (r.name === name && ip === except)) continue;
    const y = ip4(ip); if (!y) continue;
    const m = x.p < y.p ? x.mask : y.mask; if (((x.a & m) >>> 0) === ((y.a & m) >>> 0)) hits.push(`${ip} on ${r.name}`);
  }
  return hits.length ? `Overlaps ${hits.slice(0, 4).join(', ')}${hits.length > 4 ? '…' : ''}. The router refuses overlapping subnets unless they are in different VPN instances.` : '';
}
async function ipMenu(btn, name) {
  ipMenuClose(); await ensureIfs(); await Promise.all([ipOffLoad(), arpLoad()]);
  const r = ifCache.find(x => x.name === name); if (!r) return;
  if (!(bgpState || {}).peers) try { bgpState = await api('/api/view/bgp'); } catch (e) { }
  const peers = ((bgpState || {}).peers || []).map(p => p.peer);
  const used = ip => { const x = ip4(ip); if (!x || x.p > 30 && x.p !== 31) return ''; const ps = peers.filter(p => ipNet(p + '/' + x.p) === ipNet(ip) && p !== ip.split('/')[0]); return ps.length ? 'BGP peer ' + ps.join(', ') : ''; };
  const pop = document.createElement('div'); pop.id = 'ipPop'; pop.className = 'ippop';
  const all = ipsOf(r, true), offN = all.filter(x => x.off).length;
  const acts = x => x.v6 ? '<span class="faint">use the Config editor</span>'
    : x.off ? `<button class="btn sm primary" data-en="${esc(x.ip)}" data-sub="${x.sub ? 1 : 0}">Enable</button>`
    : `<button class="btn sm" data-fg="${esc(ipNet(x.ip))}" title="traffic of ${esc(ipNet(x.ip))} from NetFlow">Graph</button><button class="btn sm" data-ch="${esc(x.ip)}" data-sub="${x.sub ? 1 : 0}">Change</button><button class="btn sm" data-dis="${esc(x.ip)}" data-sub="${x.sub ? 1 : 0}" data-use="${esc(used(x.ip))}">Disable</button><button class="btn sm danger" data-rm="${esc(x.ip)}" data-sub="${x.sub ? 1 : 0}" data-use="${esc(used(x.ip))}">Remove</button>`;
  pop.innerHTML = `<div class="iph"><b>${esc(name)}</b><span class="muted">${all.length - offN} active${offN ? ` · ${offN} disabled` : ''}</span><button class="ipx" data-close="1" title="Close">✕</button></div>
    <table class="tbl"><tbody>${all.map(x => `<tr class="${x.off ? 'ipoff' : ''}"><td><div class="mono ipa">${esc(x.ip)}</div><div class="muted ips">${x.off ? esc(`off since ${new Date(x.at * 1000).toLocaleDateString()} by ${x.by}${x.reason ? ' · ' + x.reason : ''}`) : esc(x.v6 ? '' : 'net ' + ipNet(x.ip)) + (used(x.ip) ? ` · ${esc(used(x.ip))}` : '')}</div></td><td><span class="pill ${x.v6 || x.sub || x.off ? '' : 'p-ok'}">${x.v6 ? 'IPv6' : (x.sub ? 'secondary' : 'primary') + (x.off ? ' · disabled' : '')}</span></td>
      <td class="mono nb">${x.off || x.v6 ? '' : nbHtml(ipNeighbours(x.ip, name), 'no neighbour in ARP')}</td>
      <td class="acts">${acts(x)}</td></tr>`).join('')}</tbody></table>
    <div class="ipf"><button class="btn sm" data-close="1">Close</button><span class="sp"></span><button class="btn sm" data-graph="1" title="the router counts traffic per interface, so this graph is shared by all the addresses above">Graph (whole interface)</button><button class="btn sm primary" data-add="1">+ Add address</button></div>`;
  document.body.appendChild(pop);
  const b = btn.getBoundingClientRect();
  pop.style.top = (b.bottom + window.scrollY + 4) + 'px'; pop.style.left = Math.max(8, Math.min(b.left + window.scrollX, window.innerWidth - pop.offsetWidth - 12)) + 'px';
  pop.onclick = e => {
    e.stopPropagation(); const t = e.target.closest('button'); if (!t) return; ipMenuClose();
    if (t.dataset.close) return;
    if (t.dataset.graph) { closeDrawer(); return showGraphs(name); }
    if (t.dataset.fg) { closeDrawer(); return showGraphs('flow:' + t.dataset.fg); }
    if (t.dataset.add) return ifIpForm(name);
    if (t.dataset.dis) return ipDisableForm(name, t.dataset.dis, t.dataset.sub === '1', t.dataset.use);
    if (t.dataset.en) return ipEnableForm(name, t.dataset.en, t.dataset.sub === '1');
    if (t.dataset.ch) return ipChangeForm(name, t.dataset.ch, t.dataset.sub === '1');
    if (t.dataset.rm) return ipRemoveForm(name, t.dataset.rm, t.dataset.sub === '1', t.dataset.use);
  };
  setTimeout(() => document.addEventListener('click', ipMenuClose, { once: true }), 0);
}
function ipMenuClose() { const p = $('#ipPop'); if (p) p.remove(); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') ipMenuClose(); });
function ipChangeForm(name, old, sub) {
  change({ title: `Change ${old} on ${name}`, action: 'interface.ip_change', params: { interface: name, old, secondary: sub }, warn: p => ipWarn(p.address, name, old),
    intro: sub ? 'The old secondary address is removed and the new one added in the same commit.' : 'The new primary address replaces the old one; the secondary addresses stay.',
    fields: [{ k: 'address', label: 'New address/prefix', value: old }] });
}
function ipDisableForm(name, ip, sub, use) {
  const r = ifCache.find(x => x.name === name) || {}, subs = (r.secondary || []).length;
  const notes = ['The router has no off switch for one address, so Disable takes it off the router and the manager keeps it listed here, so Enable can put it back.',
    use ? `${use} is in this subnet; that session will go down.` : '',
    ].filter(Boolean);
  change({ title: `Disable ${ip} on ${name}`, action: 'interface.ip_remove', url: '/api/ip/toggle', params: { interface: name, address: ip, secondary: sub }, intro: notes.join(' '), block: primaryBlock(name, ip, sub, subs),
    fields: [{ k: 'reason', label: 'Reason (optional)', ph: 'e.g. unpaid, migrated' }], after: ipAfter });
}
function ipEnableForm(name, ip, sub) {
  const r = ifCache.find(x => x.name === name) || {};
  change({ title: `Enable ${ip} on ${name}`, action: 'interface.ip_add', url: '/api/ip/toggle', params: { interface: name, address: ip, secondary: sub, replace_primary: !sub },
    intro: !sub && r.ip ? `This was the primary address. ${name} now has ${r.ip} as primary, which this replaces.` : `Puts ${ip} back on ${name} as ${sub ? 'a secondary' : 'the primary'} address.`, after: ipAfter });
}
function ipAfter() { ifCache = []; refresh(); }
function ipRemoveForm(name, ip, sub, use) {
  const r = ifCache.find(x => x.name === name) || {}, subs = (r.secondary || []).length;
  const notes = [use ? `${use} is in this subnet; that session will go down.` : '',
    ].filter(Boolean);
  change({ title: `Remove ${ip} from ${name}`, action: 'interface.ip_remove', params: { interface: name, address: ip, secondary: sub }, intro: notes.join(' '), block: primaryBlock(name, ip, sub, subs) });
}
function primaryBlock(name, ip, sub, subs) {
  return !sub && subs ? `${ip} is the primary address of ${name}, which still has ${subs} secondary address${subs > 1 ? 'es' : ''}. ` +
    `The router will not take off a primary while secondaries exist. To put a different address in its place, use Change on ${ip}: ` +
    `it swaps the primary in one step and keeps the secondaries. Otherwise remove the secondaries first.` : '';
}
function parentList() { return ifCache.filter(i => ['ethernet', 'eth-trunk'].includes(i.type)).map(i => i.name); }
async function ensureIfs() { if (!ifCache.length) ifCache = await api('/api/view/interfaces'); }
async function subifForm(parent, pre = {}) {
  await ensureIfs();
  change({ title: 'New dot1q sub-interface', action: 'subif.create', fields: [
    { k: 'parent', label: 'Parent port', type: 'select', options: parentList(), value: parent },
    { k: 'vlan', label: 'VLAN ID', type: 'number', ph: '100', value: pre.vlan }, { k: 'number', label: 'Sub-interface no.', type: 'number', ph: 'same as VLAN', help: 'Name will be <parent>.<number>', value: pre.vlan },
    { k: 'encap', label: 'Encapsulation', type: 'select', options: [['vlan-type', 'vlan-type dot1q (one VLAN)'], ['termination', 'dot1q termination vid'], ['qinq', 'QinQ termination']] },
    { k: 'inner_vlan', label: 'Inner VLAN (QinQ)', type: 'number', ph: 'only for QinQ' }, { k: 'address', label: 'IP address/prefix', ph: '10.20.100.1/24', value: pre.address },
    { k: 'vpn', label: 'VPN instance', ph: 'optional', value: pre.vpn }, { k: 'description', label: 'Description', value: pre.description }] });
}
function loopbackForm() { change({ title: 'New loopback', action: 'loopback.create', fields: [{ k: 'number', label: 'Number', type: 'number', ph: '1' }, { k: 'address', label: 'IP/prefix', ph: '1.1.1.2/32' }, { k: 'description', label: 'Description' }] }); }

function closeDrawer() { const d = $('#drawer'); if (d._stop) d._stop(); d.classList.add('hidden'); d.innerHTML = ''; }
async function interfaceDrawer(name) {
  post('/api/audit/view', { page: 'interface/' + name, title: 'Interface ' + name }).catch(() => {});
  closeDrawer(); const d = $('#drawer'); d.classList.remove('hidden'); d.innerHTML = '<div class="muted">Loading…</div>';
  let info = await api('/api/interface?name=' + encodeURIComponent(name));
  const isSub = name.includes('.') || /^(vlanif|loopback|tunnel)/i.test(name);
  d.innerHTML = `<h1>${esc(name)} <span class="sub" id="dState"></span><span style="flex:1"></span><button class="btn" onclick="closeDrawer()">Close ✕</button></h1>
    <div class="bar"><button class="btn" onclick='ifToggle(${attr(name)},${/down/i.test(info.state) && /admin/i.test(info.state)})'>${/admin/i.test(info.state) ? 'Enable' : 'Disable'}</button>
    <button class="btn" onclick='ifEdit(${attr(name)})'>Description / MTU</button><button class="btn" onclick='ifIpForm(${attr(name)})'>+ IP address</button>
    ${info.ip ? `<button class="btn" id="dIps" onclick='event.stopPropagation();ipMenu(this,${attr(name)})'>IP addresses ▾</button>` : ''}
    ${!isSub ? `<button class="btn" onclick='subifForm(${attr(name)})'>+ Sub-interface</button><button class="btn" onclick='location.hash="queues";setTimeout(()=>queueStats(${attr(name)}),300)'>Queues</button>` : ''}
    <button class="btn" onclick='change({title:"Reset counters on ${esc(name)}",action:"interface.reset_counters",params:{interface:${attr(name)}}})'>Reset counters</button>
    ${isSub ? `<button class="btn danger" onclick='change({title:"Delete ${esc(name)}",action:"interface.delete",params:{interface:${attr(name)}},after:()=>{closeDrawer();refresh()}})'>Delete</button>` : ''}</div>
    <div class="card"><h3>Traffic<span class="sp"></span><button class="btn sm" onclick='showGraphs(${attr(name)})'>Day / week / month / year</button></h3>${legend([['Rx', '--s1', '-'], ['Tx', '--s2', '-']])}<div id="dChart"></div></div>
    <div class="card"><h3>Status</h3><div class="kv" id="dKv"></div></div>
    <div class="card"><h3>Configuration</h3><pre id="dCfg"></pre></div>
    <div class="card"><h3>Raw output <span class="muted" style="font-weight:400">display interface ${esc(name)}</span></h3><pre id="dRaw"></pre></div>`;
  const ch = new TimeChart($('#dChart'), { fmt: Fmt.bps });
  const fill = () => {
    $('#dState').innerHTML = pill(info.state) + ' ' + (info.protocol ? 'protocol ' + pill(info.protocol) : '');
    const kv = [['Description', info.description], ['IP address', info.ip], ['MTU', info.mtu], ['MAC', info.mac], ['Speed', info.speed], ['Duplex', info.duplex], ['VLAN', info.vlan],
      ['Last up', info.last_up], ['Last down', info.last_down], ['Rx (300 s avg)', Fmt.bps(info.in_rate_bps)], ['Tx (300 s avg)', Fmt.bps(info.out_rate_bps)],
      ['Rx packets', Fmt.num(info.input_packets)], ['Tx packets', Fmt.num(info.output_packets)], ['Rx bytes', Fmt.bytes(info.input_bytes)], ['Tx bytes', Fmt.bytes(info.output_bytes)],
      ['CRC errors', info.crc_errors], ['Input discards', info.in_discards]];
    const op = info.optics; if (op) kv.push(['Optic Rx', op.rx_dbm != null ? op.rx_dbm + ' dBm' : ''], ['Optic Tx', op.tx_dbm != null ? op.tx_dbm + ' dBm' : ''], ['Optic', [op.vendor, op.part].filter(Boolean).join(' ')]);
    $('#dKv').innerHTML = kv.filter(x => x[1] !== '' && x[1] != null).map(([k, v]) => `<div><span>${k}</span>${esc(v)}</div>`).join('');
    $('#dCfg').textContent = info.config.length ? 'interface ' + name + '\n ' + info.config.join('\n ') : VIA && !VIA.includes('SSH') ? '(connected over SNMP: sign in with SSH to see the configuration)' : '(default configuration)';
    $('#dRaw').textContent = info.raw;
  };
  fill();
  const tick = async () => {
    const t = await api('/api/traffic?names=' + encodeURIComponent(name)); const s = t[name] || [];
    ch.set([{ name: 'Rx', color: '--s1', values: s.map(x => [x[0], x[1]]) }, { name: 'Tx', color: '--s2', values: s.map(x => [x[0], x[2]]) }]);
    if (s.length) { d.querySelector('[data-v=Rx]').textContent = Fmt.bps(s.at(-1)[1]); d.querySelector('[data-v=Tx]').textContent = Fmt.bps(s.at(-1)[2]); }
  };
  tick();
  const t1 = setInterval(() => tick().catch(() => {}), 5000);
  const t2 = setInterval(async () => { try { info = await api('/api/interface?name=' + encodeURIComponent(name)); fill(); } catch (e) {} }, 15000);
  d._stop = () => { clearInterval(t1); clearInterval(t2); ch.destroy(); d._stop = null; };
}

/* ----- VLAN ----- */
PAGES.vlan = async c => {
  const v = await api('/api/view/vlans'); await ensureIfs();
  c.innerHTML = head('VLAN', 'routed dot1q sub-interfaces, bridge VLANs, VLANIF and switch ports') +
    `<div class="card"><h3>Dot1q sub-interfaces (routed VLANs)</h3><div id="vSub"></div></div>
    <div class="grid g2"><div class="card"><h3>VLANs</h3><div id="vVlan"></div></div><div class="card"><h3>VLANIF interfaces</h3><div id="vVif"></div></div></div>
    <div class="card"><h3>Layer-2 (portswitch) ports</h3><div id="vL2"></div></div>`;
  table($('#vSub'), [{ t: 'Sub-interface', k: 'name', f: r => `<b>${esc(r.name)}</b>` }, { t: 'Parent', k: 'parent' }, { t: 'VLAN', k: 'vlan' }, { t: 'State', k: 'phy', f: r => pill(r.phy) },
    { t: 'IP address', k: 'ip', cls: 'mono' }, { t: 'VPN', k: 'vpn' }, { t: 'In', k: 'in_util', f: r => ubar(r.in_util) }, { t: 'Out', k: 'out_util', f: r => ubar(r.out_util) }, { t: 'Description', k: 'description' },
    { t: '', sort: false, cls: 'acts', f: r => `<button class="btn sm" onclick='ifEdit(${attr(r.name)})'>Edit</button><button class="btn sm danger" onclick='change({title:"Delete ${esc(r.name)}",action:"interface.delete",params:{interface:${attr(r.name)}}})'>Delete</button>` }],
    v.subinterfaces, { toolbar: `<button class="btn" onclick="subifForm()">+ Sub-interface</button>`, onRow: r => interfaceDrawer(r.name) });
  table($('#vVlan'), [{ t: 'VLAN', k: 'id', num: true, f: r => `<b>${r.id}</b>` }, { t: 'Status', k: 'status', f: r => pill(r.status) }, { t: 'Description', k: 'description' }, { t: 'Ports', k: 'ports', cls: 'wrap' },
    { t: '', sort: false, cls: 'acts', f: r => r.id === 1 ? '' : `<button class="btn sm danger" onclick='change({title:"Delete VLAN ${r.id}",action:"vlan.delete",fields:[{k:"with_vlanif",label:"Also delete Vlanif${r.id}",type:"check"}],params:{vlan:${r.id}}})'>Delete</button>` }],
    v.vlans, { toolbar: `<button class="btn" onclick="vlanForm()">+ VLAN</button>`, empty: 'No VLANs (or bridge VLANs not supported on this card)' });
  table($('#vVif'), [{ t: 'Interface', k: 'name', f: r => `<b>${esc(r.name)}</b>` }, { t: 'State', k: 'phy', f: r => pill(r.phy) }, { t: 'IP address', k: 'ip', cls: 'mono' }, { t: 'Description', k: 'description' }], v.vlanifs, { filter: false, onRow: r => interfaceDrawer(r.name), empty: 'No VLANIF interfaces' });
  table($('#vL2'), [{ t: 'Port', k: 'name', f: r => `<b>${esc(r.name)}</b>` }, { t: 'Configuration', k: 'config', cls: 'wrap mono', f: r => esc(r.config.join(' · ')) },
    { t: '', sort: false, cls: 'acts', f: r => `<button class="btn sm" onclick='portForm(${attr(r.name)})'>Change</button>` }],
    v.l2ports, { toolbar: `<button class="btn" onclick="portForm()">Set port mode</button>`, empty: 'No layer-2 ports' });
};
function vlanForm() { change({ title: 'New VLAN', action: 'vlan.create', fields: [{ k: 'vlan', label: 'VLAN ID', type: 'number', ph: '40' }, { k: 'description', label: 'Description' }, { k: 'address', label: 'VLANIF IP/prefix', ph: 'optional, creates VlanifN', help: 'Leave empty for a pure layer-2 VLAN.' }] }); }
function portForm(name) {
  change({ title: 'Switch port mode', action: 'vlan.port', fields: [{ k: 'interface', label: 'Port', type: 'select', options: parentList(), value: name },
    { k: 'mode', label: 'Mode', type: 'select', options: [['trunk', 'Trunk: add allowed VLANs'], ['trunk_remove', 'Trunk: remove allowed VLANs'], ['access', 'Access (one VLAN)'], ['routed', 'Back to routed port (undo portswitch)']] },
    { k: 'vlans', label: 'VLANs', ph: '10,20,30-35', help: 'For trunk modes.' }, { k: 'vlan', label: 'Access VLAN', type: 'number' }, { k: 'pvid', label: 'Native VLAN (PVID)', type: 'number', ph: 'optional' }] });
}

/* ----- Queues / QoS ----- */
PAGES.queues = async c => {
  const q = await api('/api/view/qos'); ifCache = await api('/api/view/interfaces');
  const ifl = q.interfaces;
  c.innerHTML = head('Queues / QoS', 'QoS profiles, rate limits, shaping, port queues and traffic policies') +
    `<div class="card"><h3>Rate limits and shaping</h3><div id="qRules"></div></div>
    <div class="card"><h3>QoS profiles</h3><div id="qProf"></div></div>
    <div class="grid g2"><div class="card"><h3>Port-queue scheduling</h3><div id="qQ"></div></div>
    <div class="card"><h3>Queue statistics <span class="sp"></span><select id="qIf">${ifl.filter(n => !n.includes('.')).sort((a, b) => busy(b) - busy(a)).map(n => `<option>${esc(n)}</option>`).join('')}</select></h3><div id="qStats" class="muted">Pick an interface.</div></div></div>
    <div class="card"><h3>Traffic policies <span class="sp"></span><button class="btn sm" onclick="simpleQueueForm()">+ Simple queue (policy)</button><button class="btn sm" onclick="bindForm()">Bind policy to interface</button></h3><pre>${esc(q.policies || '(none)')}</pre></div>`;
  window._qIfs = ifl; window._qProfiles = q.profiles || [];
  window._qBound = {}; q.rules.filter(r => r.kind === 'profile').forEach(r => { (window._qBound[r.interface] = window._qBound[r.interface] || {})[r.direction] = r.profile; });
  q.rules.forEach(r => { r.description = (ifCache.find(x => x.name === r.interface) || {}).description || ''; });    // which customer each rule is for
  table($('#qRules'), [{ t: 'Interface', k: 'interface', f: r => `<b>${esc(r.interface)}</b>` }, { t: 'Description', k: 'description', f: r => descCell(r.interface, r.description) }, { t: 'Type', k: 'kind', f: r => ({ 'rate-limit': 'Rate limit (CAR)', shaping: 'Port shaping', policy: 'Traffic policy ' + esc(r.policy), profile: 'QoS profile <b>' + esc(r.profile) + '</b>' }[r.kind]) },
    { t: 'Direction', k: 'direction' }, { t: 'Rate (CIR)', k: 'cir_kbps', num: true, f: r => r.cir_kbps ? Fmt.bps(r.cir_kbps * 1000) : '-' }, { t: 'Peak (PIR)', k: 'pir_kbps', num: true, f: r => r.pir_kbps ? Fmt.bps(r.pir_kbps * 1000) : '-' },
    { t: 'Current', sort: false, f: r => { const i = ifCache.find(x => x.name === r.interface); return i ? ubar(r.direction === 'inbound' ? i.in_util : i.out_util) : ''; } },
    { t: 'Command', k: 'line', cls: 'mono faint' },
    { t: '', sort: false, cls: 'acts', f: r => (r.kind === 'profile' ? `<button class="btn sm" onclick='profileForm(${attr(r.interface)})'>Change</button>` : '') + `<button class="btn sm danger" onclick='change(${attr(r.kind === 'profile' ? { title: 'Remove QoS profile ' + r.profile + ' (' + r.direction + ') from ' + r.interface, action: 'qos.profile_remove', params: { interface: r.interface, profile: r.profile, direction: r.direction } } : r.kind === 'shaping' ? { title: 'Remove shaping on ' + r.interface, action: 'qos.shaping_remove', params: { interface: r.interface } } : r.kind === 'policy' ? { title: 'Unbind policy from ' + r.interface, action: 'qos.policy_unbind', params: { interface: r.interface, direction: r.direction } } : { title: 'Remove rate limit on ' + r.interface, action: 'qos.car_remove', params: { interface: r.interface, direction: r.direction } })})'>Remove</button>` }],
    q.rules, { toolbar: `<button class="btn" onclick="profileForm()">+ Apply QoS profile</button><button class="btn" onclick="carForm()">+ Rate limit</button><button class="btn" onclick="shapeForm()">+ Port shaping</button>`, empty: 'No rate limits configured' });
  table($('#qProf'), [{ t: 'Profile', k: 'name', f: x => `<b>${esc(x.name)}</b>` }, { t: 'CIR (guaranteed)', k: 'cir_kbps', num: true, f: x => x.cir_kbps ? Fmt.bps(x.cir_kbps * 1000) : '-' },
    { t: 'PIR (max)', k: 'pir_kbps', num: true, f: x => x.pir_kbps ? Fmt.bps(x.pir_kbps * 1000) : '-' },
    { t: 'Used by', k: 'interfaces', cls: 'wrap', f: x => x.interfaces.length ? x.interfaces.map(esc).join(', ') : '<span class="faint">not used</span>' },
    { t: 'Command', k: 'lines', cls: 'mono faint wrap', f: x => esc(x.lines.join(' / ')) },
    { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm" onclick='profileForm(null, ${attr(x.name)})'>Apply to interface</button>` }],
    q.profiles || [], { toolbar: `<button class="btn" onclick="profileCreateForm()">+ QoS profile</button>`, empty: 'No QoS profiles defined' });
  table($('#qQ'), [{ t: 'Interface', k: 'interface' }, { t: 'Setting', k: 'line', cls: 'mono' }], q.queues, { toolbar: `<button class="btn" onclick="queueForm()">+ Queue scheduling</button>`, empty: 'Default scheduling on all ports' });
  $('#qIf').onchange = () => queueStats($('#qIf').value);
  queueStats($('#qIf').value);
};
async function queueStats(name) {
  if (!$('#qStats')) return; const sel = $('#qIf'); if (sel && sel.value !== name) sel.value = name;
  if (timers._q) clearInterval(timers._q);
  const draw = async () => {
    const r = await api('/api/queues?name=' + encodeURIComponent(name)); const host = $('#qStats'); if (!host) return;
    if (!r.queues.length) { host.innerHTML = `<pre>${esc(r.raw)}</pre>`; return; }
    const max = Math.max(1, ...r.queues.map(x => x.pass_bps));
    host.innerHTML = `<div class="muted" style="font-size:12px;margin-bottom:6px">Current pass rate per queue (outbound)</div>` + r.queues.map(x => `<div class="hbar"><b>${x.queue.toUpperCase()}</b><div class="t"><div style="width:${x.pass_bps / max * 100}%"></div></div><span class="num">${Fmt.bps(x.pass_bps)}</span></div>`).join('') +
      `<div class="tw" style="margin-top:10px"><table><thead><tr><th>Queue</th><th class="num">Passed</th><th class="num">Dropped</th><th class="num">Drop rate</th></tr></thead><tbody>${r.queues.map(x => `<tr><td>${x.queue.toUpperCase()}</td><td class="num">${Fmt.bytes(x.pass_bytes)}</td><td class="num" style="${x.discard_packets ? 'color:var(--bad)' : ''}">${Fmt.num(x.discard_packets)} pkts</td><td class="num">${Fmt.bps(x.discard_bps)}</td></tr>`).join('')}</tbody></table></div>`;
  };
  await draw(); const t = setInterval(() => { if (!document.hidden) draw().catch(() => {}); }, 5000); timers.push(t); timers._q = t;
}
const busy = n => { const i = ifCache.find(x => x.name === n); return i ? i.in_util + i.out_util : 0; };
const ifOpts = () => (window._qIfs || ifCache.map(i => i.name));
function carForm() { change({ title: 'Rate limit (CAR)', action: 'qos.car', intro: 'Polices traffic on an interface or sub-interface, like a MikroTik simple queue max-limit.', fields: [{ k: 'interface', label: 'Interface', type: 'select', options: ifOpts() }, { k: 'direction', label: 'Direction', type: 'select', options: [['inbound', 'Inbound (upload from customer)'], ['outbound', 'Outbound (download to customer)']] }, { k: 'cir', label: 'Max rate (kbit/s)', type: 'number', ph: '100000 = 100 Mbit/s' }, { k: 'pir', label: 'Burst rate (kbit/s)', type: 'number', ph: 'optional' }] }); }
const profLabel = x => `${x.name} (${x.pir_kbps && x.pir_kbps !== x.cir_kbps ? 'max ' + Fmt.bps(x.pir_kbps * 1000) + ', guaranteed ' + Fmt.bps(x.cir_kbps * 1000) : Fmt.bps((x.cir_kbps || 0) * 1000)})`;
function profileForm(ifn, prof) {
  const cur = ifn && window._qBound[ifn] || {};
  change({ title: ifn ? 'QoS profile on ' + ifn : 'Apply QoS profile', action: 'qos.profile_apply', params: { current: window._qBound },
    intro: 'Limits the interface to the profile\'s rate. A profile already on the interface in that direction is replaced.',
    fields: [{ k: 'interface', label: 'Interface', type: 'select', options: ifOpts(), value: ifn || '' },
      { k: 'profile', label: 'Profile', type: 'select', options: window._qProfiles.map(x => [x.name, profLabel(x)]), value: prof || cur.inbound || '' },
      { k: 'direction', label: 'Direction', type: 'select', options: [['both', 'Both (upload and download)'], ['inbound', 'Inbound (upload from customer)'], ['outbound', 'Outbound (download to customer)']] }] });
}
function profileCreateForm() { change({ title: 'New QoS profile', action: 'qos.profile_create', intro: 'A named speed limit you can apply to many interfaces. Rates in kbit/s (100 Mbit/s = 100000).', fields: [{ k: 'name', label: 'Name', ph: '250Mbps' }, { k: 'cir', label: 'CIR, guaranteed (kbit/s)', type: 'number', ph: '250000' }, { k: 'pir', label: 'PIR, maximum (kbit/s, optional)', type: 'number', ph: 'same as CIR' }] }); }
function shapeForm() { change({ title: 'Port shaping', action: 'qos.shaping', intro: 'Shapes all outbound traffic on a physical port.', fields: [{ k: 'interface', label: 'Port', type: 'select', options: ifOpts().filter(n => !n.includes('.')) }, { k: 'mbps', label: 'Rate (Mbit/s)', type: 'number', ph: '1000' }] }); }
function queueForm() { change({ title: 'Port-queue scheduling', action: 'qos.queue', fields: [{ k: 'interface', label: 'Port', type: 'select', options: ifOpts().filter(n => !n.includes('.')) }, { k: 'queue', label: 'Queue', type: 'select', options: ['be', 'af1', 'af2', 'af3', 'af4', 'ef', 'cs6', 'cs7'] }, { k: 'schedule', label: 'Scheduling', type: 'select', options: [['wfq', 'WFQ (weighted)'], ['pq', 'PQ (strict priority)'], ['lpq', 'LPQ (low priority)']] }, { k: 'weight', label: 'Weight', type: 'number', ph: '1-100, WFQ only' }, { k: 'shaping_pct', label: 'Shaping %', type: 'number', ph: 'optional, of port rate' }] }); }
function simpleQueueForm() { change({ title: 'Simple queue (traffic policy)', action: 'qos.simple_policy', intro: 'Creates an ACL, classifier, CAR behavior and policy. Bind it to an interface afterwards.', fields: [{ k: 'name', label: 'Name', ph: 'CUST-A-100M' }, { k: 'acl', label: 'ACL number', type: 'number', ph: '3000-3999' }, { k: 'source', label: 'Source prefix', ph: '10.20.100.0/24' }, { k: 'destination', label: 'Destination prefix', ph: 'optional' }, { k: 'cir', label: 'Max rate (kbit/s)', type: 'number', ph: '100000' }] }); }
function bindForm() { change({ title: 'Bind traffic policy', action: 'qos.policy_bind', fields: [{ k: 'interface', label: 'Interface', type: 'select', options: ifOpts() }, { k: 'policy', label: 'Policy name' }, { k: 'direction', label: 'Direction', type: 'select', options: ['inbound', 'outbound'] }] }); }

/* ----- IP ----- */
PAGES.addresses = async c => {
  const rows = ifCache = await api('/api/view/interfaces'); arpCache.at = 0; await arpLoad();
  c.innerHTML = head('IP addresses') + '<div class="card"><div id="aT"></div></div>';
  table($('#aT'), [{ t: 'Address', k: 'ip', cls: 'mono', f: r => `<b>${esc(r.ip)}</b>` }, { t: 'Interface', k: 'name' }, { t: 'Description', k: 'description', f: r => r.sub || r.v6 ? esc(r.description) : descCell(r.name, r.description) }, { t: 'Neighbour (client)', k: 'nb', cls: 'mono', f: r => r.v6 ? '' : nbHtml(r._nb, '-') },
    { t: 'VPN', k: 'vpn' }, { t: 'State', k: 'protocol', f: r => pill(r.protocol) }, 
    { t: '', sort: false, cls: 'acts', f: r => r.v6 ? '' : `<button class="btn sm" onclick='ipChangeForm(${attr(r.name)},${attr(r.ip)},${!!r.sub})'>Change</button><button class="btn sm danger" onclick='ipRemoveForm(${attr(r.name)},${attr(r.ip)},${!!r.sub},"")'>Remove</button>` }],
    rows.filter(r => r.ip || (r.ipv6 || []).length).flatMap(r => [...(r.ip ? [r] : []), ...(r.secondary || []).map(ip => ({ ...r, ip, sub: true, description: (r.description ? r.description + ' · ' : '') + 'secondary' })),
      ...(r.ipv6 || []).map(ip => ({ ...r, ip, v6: true, description: (r.description ? r.description + ' · ' : '') + 'IPv6' }))])
      .map(r => { const n = r.v6 ? [] : ipNeighbours(r.ip, r.name); return { ...r, _nb: n, nb: nbSearch(n) }; }), { toolbar: `<button class="btn" onclick="addrForm()">+ Address</button>` });
};
function addrForm(pre = {}) { change({ title: 'Add IP address', action: 'interface.ip_add', fields: [{ k: 'interface', label: 'Interface', type: 'select', options: ifCache.filter(i => i.type !== 'null' && !i.l2).map(i => i.name), value: pre.interface }, { k: 'address', label: 'Address/prefix', ph: '10.0.0.1/30', value: pre.address }, { k: 'secondary', label: 'Secondary (sub)', type: 'check', value: pre.secondary ?? true, help: 'Untick only on an interface with no address yet. The manager refuses to replace an existing primary from here.' }] }); }

PAGES.routes = async c => {
  const vpns = (await api('/api/view/vpn_instances')).instances.map(v => v.name);
  const vpn = c._vpn || '';
  const lk = c._lookup || '';
  const r = await api('/api/routes?' + new URLSearchParams({ ...(vpn ? { vpn } : {}), ...(lk ? { lookup: lk } : {}) }));
  c.innerHTML = head('Routes', r.summary, `<input id="rLk" placeholder="Lookup: 8.8.8.8 or 8.8.8.0/24" value="${esc(lk)}" style="max-width:230px"><button class="btn" id="rLkBtn">${lk ? 'Clear lookup' : 'Look up'}</button><select id="rVpn"><option value="">Public (_public_)</option>${vpns.map(v => `<option ${v === vpn ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select>`) +
    `<div class="card"><h3>Static routes</h3><div id="rS"></div></div><div class="card"><h3>Routing table</h3><div id="rT"></div></div>`;
  $('#rVpn').onchange = () => { c._vpn = $('#rVpn').value; PAGES.routes(c); };
  $('#rLk').oninput = () => $('#rLkBtn').textContent = $('#rLk').value.trim() && $('#rLk').value.trim() !== lk ? 'Look up' : lk ? 'Clear lookup' : 'Look up';
  $('#rLkBtn').onclick = () => { const v = $('#rLk').value.trim(); c._lookup = v && v !== lk ? v : ''; PAGES.routes(c).catch(e => { c._lookup = ''; toast(e.message, true); }); };
  $('#rLk').onkeydown = e => { if (e.key === 'Enter') $('#rLkBtn').click(); };
  table($('#rS'), [{ t: 'Destination', k: 'prefix', f: x => `<b>${esc(x.prefix)}/${esc(maskLen(x.mask))}</b>` }, { t: 'Gateway', k: 'next_hop' }, { t: 'Interface', k: 'interface' }, { t: 'Pref', k: 'preference', num: true }, { t: 'VPN', k: 'vpn' }, { t: 'Description', k: 'description' },
    { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm danger" onclick='change({title:"Remove static route",action:"route.static_remove",params:{line:${attr(x.line)}}})'>Remove</button>` }],
    r.static, { toolbar: `<button class="btn" onclick="routeForm()">+ Static route</button>`, empty: 'No static routes' });
  const proto = [...new Set(r.routes.map(x => x.protocol))];
  table($('#rT'), [{ t: 'Destination', k: 'destination', f: x => `<b>${esc(x.destination)}</b>` }, { t: 'Protocol', k: 'protocol' }, { t: 'Pref', k: 'preference', num: true }, { t: 'Cost', k: 'cost', num: true }, { t: 'Flags', k: 'flags' }, { t: 'Next hop', k: 'next_hop', cls: 'mono' }, { t: 'Interface', k: 'interface' }],
    r.routes, { empty: r.lookup ? 'No route for ' + r.lookup : 'No routes', toolbar: `<span class="muted" style="font-size:12px">${r.large ? 'Large table: BGP routes are not listed. ' : ''}${proto.map(p => `${esc(p)} ${r.routes.filter(x => x.protocol === p).length}`).join(' · ')}</span>` });
};
function maskLen(m) { return m.includes('.') ? m.split('.').reduce((a, o) => a + (+o).toString(2).split('1').length - 1, 0) : m; }
function routeForm() { change({ title: 'Add static route', action: 'route.static_add', fields: [{ k: 'prefix', label: 'Destination', ph: '0.0.0.0/0' }, { k: 'next_hop', label: 'Gateway', ph: '172.16.0.0' }, { k: 'interface', label: 'Out interface', ph: 'optional, e.g. NULL0', list: ifCache.map(i => i.name) }, { k: 'preference', label: 'Preference', type: 'number', ph: '60' }, { k: 'vpn', label: 'VPN instance', ph: 'optional' }, { k: 'description', label: 'Description' }] }); }

PAGES.arp = async c => {
  const r = await api('/api/view/arp');
  c.innerHTML = head('ARP') + `<div class="card"><h3>ARP table</h3><div id="arT"></div></div><div class="card"><h3>Static ARP</h3><div id="arS"></div></div>`;
  table($('#arT'), [{ t: 'IP', k: 'ip', cls: 'mono', f: x => `<b>${esc(x.ip)}</b>` }, { t: 'MAC', k: 'mac', cls: 'mono' }, { t: 'Type', k: 'type', f: x => ({ I: 'Interface', S: 'Static' }[x.type] || (x.type.startsWith('D') ? 'Dynamic' : esc(x.type))) }, { t: 'Expire (min)', k: 'expire', num: true }, { t: 'Interface', k: 'interface' }, { t: 'VLAN', k: 'vlan' }, { t: 'VPN', k: 'vpn' }], r.entries);
  table($('#arS'), [{ t: 'Entry', k: 'line', cls: 'mono', f: x => esc(x.line) }, { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm danger" onclick='change({title:"Remove static ARP",action:"arp.static_remove",params:{ip:${attr(x.line.split(" ")[2])}}})'>Remove</button>` }],
    r.static.map(line => ({ line })), { filter: false, toolbar: `<button class="btn" onclick="arpForm()">+ Static ARP</button>`, empty: 'None' });
};
function arpForm() { change({ title: 'Static ARP', action: 'arp.static_add', fields: [{ k: 'ip', label: 'IP', ph: '10.0.0.2' }, { k: 'mac', label: 'MAC', ph: '00e0-fc12-3456' }, { k: 'vpn', label: 'VPN instance', ph: 'optional' }] }); }

PAGES.switching = async c => {
  const r = await api('/api/view/switching');
  const tile = (l, v) => `<div class="tile"><div class="l">${l}</div><div class="v">${v}</div></div>`;
  const up = r.ports.filter(p => p.phy === 'up').length, poeOn = r.ports.filter(p => p.poe && p.poe.status === 'on');
  c.innerHTML = head('Switching', 'ports with their VLANs, the MAC addresses behind them, PoE and spanning tree') + (r.note ? `<div class="card muted">${esc(r.note)}</div>` : '') +
    `<div class="tiles">${tile('Ports up', `${up} / ${r.ports.length}`)}${tile('MAC addresses', r.macs.length)}${tile('VLANs', r.vlans.length)}${tile('PoE ports on', r.poe_budget ? `${poeOn.length} · ${r.poe_budget.used} of ${r.poe_budget.watts} W` : poeOn.length)}</div>
    <div class="card"><h3>Ports</h3><div id="swP"></div></div>
    <div class="card"><h3>MAC table</h3><div class="faint" style="font-size:12.5px;margin-bottom:6px">Type part of a MAC (aa:bb:cc…), a port or a VLAN to find where a device is.</div><div id="swMf" style="font-size:12.5px;margin-bottom:6px"></div><div id="swM"></div></div>
    <div class="card"><h3>VLANs</h3><div id="swV"></div></div>`;
  const vl = a => a.length ? a.map(esc).join(', ') : '<span class="faint">-</span>';
  table($('#swP'), [{ t: 'Port', k: 'name', f: p => `<b>${esc(p.name)}</b>` }, { t: 'State', k: 'phy', f: p => p.admin_down ? '<span class="faint">disabled</span>' : p.phy === 'up' ? '<span class="ok">up</span>' : '<span class="bad">down</span>' },
    { t: 'Speed', k: 'speed_mbps', num: true, f: p => p.speed_mbps ? (p.speed_mbps >= 1000 ? p.speed_mbps / 1000 + 'G' : p.speed_mbps + 'M') : '' },
    { t: 'Description', k: 'description', cls: 'wrap', f: p => esc(p.description) }, { t: 'Untagged', k: 'untagged', f: p => vl(p.untagged) }, { t: 'Tagged', k: 'tagged', cls: 'wrap', f: p => vl(p.tagged) },
    { t: 'MACs', k: 'macs', num: true, f: p => p.macs ? `<a href="#" data-mp="${esc(p.name)}">${p.macs}</a>` : '0' },
    { t: 'PoE', k: 'poe', f: p => p.poe ? `${esc(p.poe.status)}${p.poe.watts != null ? ` · ${p.poe.watts} W` : ''}` : '' }, { t: 'STP', k: 'stp', f: p => p.stp === 'blocking' ? '<span class="warnc">blocking</span>' : esc(p.stp) },
    { t: 'Errors in/out', k: 'in_errors', num: true, f: p => p.in_errors + p.out_errors ? `<span class="warnc">${p.in_errors} / ${p.out_errors}</span>` : '0' },
    { t: 'Neighbour', k: 'neighbor', f: p => esc(p.neighbor) }], r.ports, { empty: 'No switch ports reported' });
  const macs = port => { $('#swMf').innerHTML = port ? `Showing the MACs on <b>${esc(port)}</b> · <a href="#" data-mall>show all</a>` : '';
    table($('#swM'), [{ t: 'MAC', k: 'mac', cls: 'mono' }, { t: 'VLAN', k: 'vlan' }, { t: 'Port', k: 'port', f: m => `<b>${esc(m.port)}</b>` }, { t: 'Type', k: 'type' }], port ? r.macs.filter(m => m.port === port) : r.macs, { empty: 'No MAC addresses learnt' }); };
  macs('');
  table($('#swV'), [{ t: 'VLAN', k: 'id', num: true, f: v => `<b>${esc(v.id)}</b>` }, { t: 'Name', k: 'name' }, { t: 'Untagged ports', k: 'untagged', cls: 'wrap', f: v => vl(v.untagged) }, { t: 'Tagged ports', k: 'tagged', cls: 'wrap', f: v => vl(v.tagged) }], r.vlans, { empty: 'No VLANs reported' });
  $('#swP').onclick = e => { const a = e.target.closest('[data-mp]'); if (!a) return; e.preventDefault(); macs(a.dataset.mp); $('#swM').scrollIntoView({ block: 'center' }); };
  $('#swMf').onclick = e => { if (!e.target.closest('[data-mall]')) return; e.preventDefault(); macs(''); };
};

PAGES.vpn = async c => {
  const r = await api('/api/view/vpn_instances');
  c.innerHTML = head('VPN instances (VRF)') + `<div class="card"><div id="vT"></div></div>`;
  table($('#vT'), [{ t: 'Name', k: 'name', f: x => `<b>${esc(x.name)}</b>` }, { t: 'RD', k: 'rd', cls: 'mono' }, { t: 'Family', k: 'family' },
    { t: '', sort: false, cls: 'acts', f: x => x.name.startsWith('__') ? '' : `<button class="btn sm" onclick='location.hash="routes";document.querySelector("#content")._vpn=${attr(x.name)}'>Routes</button><button class="btn sm danger" onclick='change({title:"Delete VPN instance ${esc(x.name)}",action:"vpn.delete",params:{name:${attr(x.name)}},intro:"Interfaces bound to it lose their IP configuration."})'>Delete</button>` }],
    r.instances, { toolbar: `<button class="btn" onclick="vpnForm()">+ VPN instance</button>` });
};
function vpnForm() { change({ title: 'New VPN instance', action: 'vpn.create', fields: [{ k: 'name', label: 'Name', ph: 'CUST-E' }, { k: 'rd', label: 'Route distinguisher', ph: '65001:300' }, { k: 'import_rt', label: 'Import RT', ph: '65001:300' }, { k: 'export_rt', label: 'Export RT', ph: '65001:300' }] }); }

PAGES.neighbors = async c => {
  const r = await api('/api/view/neighbors');
  c.innerHTML = head('Neighbors', 'LLDP') + `<div class="card"><div id="nT"></div></div>`;
  table($('#nT'), [{ t: 'Local port', k: 'local', f: x => `<b>${esc(x.local)}</b>` }, { t: 'Neighbor', k: 'neighbor' }, { t: 'Neighbor port', k: 'neighbor_port' }, { t: 'Expires (s)', k: 'exptime', num: true }], r.lldp, { empty: 'No LLDP neighbors (is "lldp enable" configured?)' });
};

/* ----- Routing ----- */
let bgpState = {};
PAGES.bgp = async c => {
  const b = bgpState = await api('/api/view/bgp');
  const cfg = Object.values(b.config)[0] || [];
  const nets = cfg.filter(l => l.startsWith('network ')).map(l => l.split(' ').slice(1).join('/'));
  const desc = {}; cfg.forEach(l => { const m = l.match(/^peer (\S+) description (.+)$/); if (m) desc[m[1]] = m[2]; });
  const ign = new Set(cfg.filter(l => / ignore$/.test(l)).map(l => l.split(' ')[1]));
  const pol = {}; cfg.forEach(l => { const m = l.match(/^peer (\S+) route-policy (\S+) (import|export)$/); if (m) (pol[m[1]] = pol[m[1]] || {})[m[3]] = m[2]; });
  const [lat, sp] = await Promise.all([api('/api/changes/latest?kind=peer').catch(() => ({ now: {}, hour: {}, day: {} })), api('/api/changes/spark?kind=peer&range=day').catch(() => ({ series: {} }))]);
  b.peers.forEach(p => { p.description = desc[p.peer] || ''; p.ignored = ign.has(p.peer); p.pin = (pol[p.peer] || {}).import || ''; p.pout = (pol[p.peer] || {}).export || '';
    const n = lat.now[p.peer];   // the CLI summary has no active/advertised: use the tracker's last read if it is about the same table
    if (p.active == null && n && p.state === 'Established' && Math.abs(n[1] - p.prefixes) <= Math.max(10, p.prefixes / 100)) { p.active = n[2]; p.advertised = n[3]; }
    const was = (k, i) => lat[k][p.peer] && lat[k][p.peer][0] < (n ? n[0] : 1e12) ? lat[k][p.peer][i] : null;
    p.d1h = was('hour', 1) != null ? p.prefixes - was('hour', 1) : null; p.d24h = was('day', 1) != null ? p.prefixes - was('day', 1) : null; });
  const est = b.peers.filter(p => p.state === 'Established');
  c.innerHTML = head('BGP', `AS ${b.local_as || '-'} · router ID ${b.router_id || '-'}`) +
    `<div class="tiles"><div class="tile"><div class="l">Peers</div><div class="v">${b.peers.length}</div></div><div class="tile"><div class="l">Established</div><div class="v">${est.length}</div></div>
    <div class="tile"><div class="l">Down</div><div class="v" style="color:${b.peers.length - est.length ? 'var(--bad)' : ''}">${b.peers.length - est.length}</div></div><div class="tile"><div class="l">Prefixes received</div><div class="v">${Fmt.num(b.peers.reduce((a, p) => a + p.prefixes, 0))}</div></div></div>
    <div class="card"><h3>Peers</h3><div id="bP"></div></div>
    <div class="card"><h3>Session changes <span class="sp"></span><a class="btn sm" href="#changes" onclick="CHG.kind='sessions'">Full change log</a></h3><div id="bE" class="muted">Loading…</div></div>
    <div class="grid g2"><div class="card"><h3>Advertised networks</h3><div id="bN"></div></div><div class="card"><h3>Configuration</h3><pre>${esc(Object.entries(b.config).map(([k, v]) => 'bgp ' + k + '\n ' + v.join('\n ')).join('\n'))}</pre></div></div>
    <div class="card" id="bRcard"><h3 id="bRt">BGP routing table</h3><div class="row" style="gap:6px;margin-bottom:10px"><input id="bLk" placeholder="Lookup: 8.8.8.8 or 8.8.8.0/24" style="max-width:260px"><button class="btn sm" id="bLkBtn">Look up</button></div><div id="bR" class="muted">Pick a peer’s Received / Advertised, look up an address, or <button class="btn sm" onclick="bgpRoutes()">load the full BGP table</button></div></div>`;
  $('#bLkBtn').onclick = () => bgpRoutes(null, null, $('#bLk').value.trim()); $('#bLk').onkeydown = e => { if (e.key === 'Enter') $('#bLkBtn').click(); };
  table($('#bP'), [{ t: 'Peer', k: 'peer', f: p => `<b>${esc(p.peer)}</b>` }, { t: 'Description', k: 'description' }, { t: 'Remote AS', k: 'as' }, { t: 'Type', sort: false, f: p => p.as === b.local_as ? 'iBGP' : 'eBGP' },
    { t: 'State', k: 'state', f: p => bgpPill(p.state) }, { t: 'Up/Down', k: 'up_down' }, { t: 'Received', k: 'prefixes', num: true }, { t: 'Last 24 h', sort: false, f: p => `<a href="javascript:void 0" onclick='peerHistory(${attr(p.peer)},${attr(p.description)})' title="statistics and graphs">${spark(sp.series[p.peer])}</a>` }, { t: 'Active', k: 'active', num: true },
    { t: 'Advertised', k: 'advertised', num: true }, { t: 'Δ 1 h', k: 'd1h', num: true, f: p => delta(p.d1h) }, { t: 'Δ 24 h', k: 'd24h', num: true, f: p => delta(p.d24h) },
    { t: 'Policy in / out', k: 'pin', f: p => [p.pin, p.pout].map(x => x ? `<a href="#policies" onclick='POLSEL=${attr(x)}'>${esc(x)}</a>` : '<span class="faint">-</span>').join(' / ') },
    { t: 'Msg in/out', k: 'msg_rcvd', num: true, f: p => `${Fmt.num(p.msg_rcvd)} / ${Fmt.num(p.msg_sent)}` }, { t: 'OutQ', k: 'out_q', num: true },
    { t: '', sort: false, cls: 'acts', f: p => `<button class="btn sm" onclick='peerHistory(${attr(p.peer)},${attr(p.description)})'>Stats &amp; graphs</button><button class="btn sm" onclick='bgpRoutes(${attr(p.peer)},"received")'>Received</button><button class="btn sm" onclick='bgpRoutes(${attr(p.peer)},"advertised")'>Advertised</button>
      <button class="btn sm" onclick='change({title:"Reset BGP session ${esc(p.peer)}",action:"bgp.reset",fields:[{k:"soft",label:"Type",type:"select",options:[["","Hard reset (session drops)"],["import","Soft refresh inbound"],["export","Soft refresh outbound"]]}],params:{peer:${attr(p.peer)}}})'>Reset</button>
      <button class="btn sm" onclick='change({title:"${p.ignored ? 'Enable' : 'Disable'} peer ${esc(p.peer)}",action:"bgp.peer_enable",params:{local_as:${attr(b.local_as)},peer:${attr(p.peer)},enabled:${p.ignored}}})'>${p.ignored ? 'Enable' : 'Disable'}</button>
      <button class="btn sm danger" onclick='change({title:"Remove peer ${esc(p.peer)}",action:"bgp.peer_remove",params:{local_as:${attr(b.local_as)},peer:${attr(p.peer)}}})'>Remove</button>` }],
    b.peers, { toolbar: `<button class="btn" onclick="peerForm()">+ Peer</button>`, rowClass: p => p.ignored ? 'off' : '' });
  table($('#bN'), [{ t: 'Network', k: 'n', f: x => `<b>${esc(x.n)}</b>` }, { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm danger" onclick='change({title:"Stop advertising ${esc(x.n)}",action:"bgp.network_remove",params:{local_as:${attr(b.local_as)},prefix:${attr(x.n)}}})'>Remove</button>` }],
    nets.map(n => ({ n })), { filter: false, toolbar: `<button class="btn" onclick='change({title:"Advertise network",action:"bgp.network_add",params:{local_as:${attr(b.local_as)}},fields:[{k:"prefix",label:"Network",ph:"203.0.113.0/24"}]})'>+ Network</button>`, empty: 'None' });
  changeTable($('#bE'), { kind: 'bgp,peer', days: 30, limit: 50, compact: true });
  every(15000, async () => { if ($('#modalBg').classList.contains('hidden') && !$('#bR table')) PAGES.bgp(c); });
};
function delta(d) { return d == null ? '<span class="faint">-</span>' : d === 0 ? '<span class="faint">0</span>' : `<span style="color:${d < 0 ? 'var(--bad)' : 'var(--ok)'}">${d > 0 ? '+' : ''}${Fmt.num(d)}</span>`; }
function spark(v, w = 90, h = 22) {
  if (!v || v.length < 2) return '<span class="faint">-</span>';
  const ys = v.map(p => p[1]), lo = Math.min(...ys), hi = Math.max(...ys), t0 = v[0][0], t1 = v[v.length - 1][0] || t0 + 1;
  const X = t => (t - t0) / Math.max(1, t1 - t0) * (w - 2) + 1, Y = y => hi === lo ? h / 2 : h - 2 - (y - lo) / (hi - lo) * (h - 4);
  const last = ys[ys.length - 1], dropped = hi > 0 && last < hi * 0.8;
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="received routes, ${Fmt.num(lo)} to ${Fmt.num(hi)}"><title>${Fmt.num(lo)} – ${Fmt.num(hi)} routes</title><polyline fill="none" stroke="var(${dropped ? '--bad' : '--s1'})" stroke-width="1.5" points="${v.map(p => X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)).join(' ')}"/></svg>`;
}
/* One peer: its numbers now, routes over time, and when the session was up or down (and why). */
async function peerHistory(peer, desc) {
  const m = $('#modal'); m.classList.add('wide');
  const p = ((bgpState && bgpState.peers) || []).find(x => x.peer === peer) || { peer };
  const tile = (l, v, extra = '') => `<div class="tile"><div class="l">${l}</div><div class="v" ${extra}>${v}</div></div>`;
  m.innerHTML = `<h2>BGP peer ${esc(peer)}${desc ? ' · ' + esc(desc) : ''} ${bgpPill(p.state || '')}</h2>
    <div class="tiles sm">${tile('Remote AS', esc(p.as || '-'))}${tile('Up / down for', esc(p.up_down || '-'))}${tile('Received', Fmt.num(p.prefixes))}${tile('Active', p.active == null ? '-' : Fmt.num(p.active))}
      ${tile('Advertised', p.advertised == null ? '-' : Fmt.num(p.advertised))}${tile('Δ 1 h / 24 h', `${delta(p.d1h)} / ${delta(p.d24h)}`)}${tile('Messages in / out', `${Fmt.num(p.msg_rcvd)} / ${Fmt.num(p.msg_sent)}`)}
      ${tile('Policy in / out', `${esc(p.pin || '-')} / ${esc(p.pout || '-')}`, 'style="font-size:13px"')}</div>
    <div class="row" style="gap:10px;align-items:center;margin:4px 0 6px"><div class="seg" id="phR">${['hour', 'day', 'week', 'month', 'quarter'].map(k => `<button data-r="${k}">${{ hour: '1 hour', day: '1 day', week: '1 week', month: '1 month', quarter: '3 months' }[k]}</button>`).join('')}</div><span class="muted" id="phS"></span></div>
    <div class="lbl muted">Session up / down</div><div class="tl" id="phT"></div><div class="muted" id="phTx" style="font-size:12px"></div>
    <div class="lbl muted" style="margin-top:10px">Routes received and active (used)</div><div id="phC" style="height:220px"></div>
    <div class="lbl muted" style="margin-top:6px">Routes advertised to the peer</div><div id="phA" style="height:110px"></div><div class="muted" id="phN"></div>
    <div id="phE" style="margin-top:8px"></div>
    <div class="actions"><button class="btn" onclick='closeModal();bgpRoutes(${attr(peer)},"received")'>Received routes</button><button class="btn" onclick='closeModal();bgpRoutes(${attr(peer)},"advertised")'>Advertised routes</button><button class="btn primary" data-a="x">Close</button></div>`;
  $('#modalBg').classList.remove('hidden'); m.querySelector('[data-a=x]').onclick = closeModal;
  const show = async rng => {
    m.querySelectorAll('#phR button').forEach(b => b.classList.toggle('on', b.dataset.r === rng));
    const span = { hour: 3600, day: 86400, week: 604800, month: 2592000, quarter: 7776000 }[rng], now = Date.now() / 1000, t0 = now - span;
    const [r, ev] = await Promise.all([api(`/api/changes/series?kind=peer&key=${encodeURIComponent(peer)}&range=${rng}`),
      api(`/api/changes?kind=bgp&key=${encodeURIComponent(peer)}&days=${Math.max(span / 86400, 0.05)}&limit=1000`).catch(() => ({ events: [] }))]);
    if (!m.contains($('#phC'))) return;
    const pts = r.points, vals = pts.flatMap(x => [x[1], x[2]]).filter(v => v != null);
    const lo = vals.length ? Math.min(...vals) : 0, hi = vals.length ? Math.max(...vals) : 0;
    new TimeChart($('#phC'), { fmt: Fmt.num, span, min: lo > 1000 && hi - lo < lo * 0.2 ? Math.floor(lo * 0.98) : 0 }).set([
      { name: 'Received', color: '--s1', values: pts.map(x => [x[0], x[1]]) }, { name: 'Active', color: '--s3', values: pts.filter(x => x[2] != null).map(x => [x[0], x[2]]) }]);
    new TimeChart($('#phA'), { fmt: Fmt.num, span, small: true }).set([{ name: 'Advertised', color: '--s2', values: pts.filter(x => x[3] != null).map(x => [x[0], x[3]]) }]);
    $('#phN').textContent = pts.length ? '' : 'No numbers kept yet for this range. They are sampled about every 5 minutes, so the first points show up shortly.';
    // session timeline: walk the up/down events oldest first; the state before the first one is the opposite of it
    const es = (ev.events || []).filter(e => e.change === 'up' || e.change === 'down').sort((x, y) => x.ts - y.ts);
    const est = String(p.state || '').toLowerCase() === 'established';
    let st = es.length ? (es[0].change === 'up' ? 'd' : 'u') : (est ? 'u' : 'd'), at = t0; const segs = [];
    for (const e of es) { segs.push([st, Math.max(at, t0), e.ts]); st = e.change === 'up' ? 'u' : 'd'; at = e.ts; }
    segs.push([st, Math.max(at, t0), now]);
    const upT = segs.filter(x => x[0] === 'u').reduce((a, x) => a + x[2] - x[1], 0), downs = es.filter(e => e.change === 'down');
    $('#phT').innerHTML = segs.filter(x => x[2] > x[1]).map(([k, a, b]) => `<i class="${k}" style="width:${((b - a) / span * 100).toFixed(3)}%" title="${k === 'u' ? 'up' : 'down'} ${esc(new Date(a * 1000).toLocaleString())} – ${esc(new Date(b * 1000).toLocaleString())}"></i>`).join('');
    $('#phS').innerHTML = `Up <b>${(upT / span * 100).toFixed(upT === span ? 0 : 2)}%</b> of this range · <b>${downs.length}</b> drop${downs.length === 1 ? '' : 's'}`;
    $('#phTx').textContent = es.length ? '' : (est ? 'No drops recorded in this range.' : 'Down, and no change recorded in this range.');
    $('#phE').innerHTML = downs.length ? `<table class="tbl"><thead><tr><th>Went down</th><th>Reason</th><th>Back up</th></tr></thead><tbody>${downs.slice().reverse().slice(0, 20).map(d => { const u = es.find(e => e.change === 'up' && e.ts > d.ts);
      return `<tr><td>${esc(new Date(d.ts * 1000).toLocaleString())}</td><td>${esc(d.info || '-')}</td><td>${u ? esc(new Date(u.ts * 1000).toLocaleString()) + (u.info ? ' · ' + esc(u.info) : '') : '<span class="pill p-bad">still down</span>'}</td></tr>`; }).join('')}</tbody></table>` : '';
  };
  $('#phR').onclick = e => { const b = e.target.closest('button[data-r]'); if (b) show(b.dataset.r); };
  show('day');
}

/* ----- change log (ARP, MAC, routes, sessions, counts) ----- */
const CHG = { kind: 'all', days: 1, q: '' };
const CHG_KINDS = [['all', 'All', ''], ['arp', 'ARP', 'arp'], ['mac', 'MAC', 'mac'], ['route', 'Routes', 'route,routes'], ['sessions', 'Protocol sessions', 'bgp,ospf,isis,ldp'], ['counts', 'BGP route counts', 'peer,routes']];
const CHG_LABEL = { arp: 'ARP', mac: 'MAC', route: 'Route', routes: 'Route count', bgp: 'BGP', ospf: 'OSPF', isis: 'IS-IS', ldp: 'LDP', peer: 'BGP routes' };
function chgPill(c) {
  const cls = { added: 'p-ok', up: 'p-ok', back: 'p-ok', removed: 'p-bad', down: 'p-bad', dropped: 'p-bad', changed: 'p-warn', jumped: 'p-warn' }[c] || 'p-off';
  const words = { added: 'new', removed: 'gone', changed: 'changed', up: 'up', down: 'down', back: 'back', dropped: 'dropped', jumped: 'jumped' };
  return `<span class="pill ${cls}">${esc(words[c] || c)}</span>`;
}
function chgWhat(e) {
  if (e.kind === 'arp') return e.change === 'changed' ? 'MAC changed: possible moved host, IP clash or spoofing' : e.change === 'added' ? 'host appeared' : e.change === 'removed' ? 'host no longer answers' : '';
  if (e.kind === 'mac') return e.change === 'changed' ? 'moved to another port' : '';
  return '';
}
async function changeTable(host, o) {
  const r = await api('/api/changes?' + new URLSearchParams({ kind: o.kind || '', days: o.days || 1, q: o.q || '', limit: o.limit || 1000 })).catch(e => ({ error: e.message }));
  if (r.error) { host.innerHTML = `<div class="err">${esc(r.error)}</div>`; return r; }
  host.classList.remove('muted');
  table(host, [{ t: 'Time', k: 'ts', f: e => `<span title="${esc(new Date(e.ts * 1000).toLocaleString())}">${esc(new Date(e.ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }))}</span>` },
    ...(o.compact ? [] : [{ t: 'Table', k: 'kind', f: e => esc(CHG_LABEL[e.kind] || e.kind) }]),
    { t: 'Item', k: 'key', cls: 'mono', f: e => `<b>${esc(e.key)}</b>` }, { t: 'Change', k: 'change', f: e => chgPill(e.change) },
    { t: 'Before', k: 'old', cls: 'mono', f: e => esc(e.old || '') }, { t: 'After', k: 'new', cls: 'mono', f: e => esc(e.new || '') },
    { t: 'Details', k: 'info', cls: 'wrap', f: e => esc([e.info, chgWhat(e)].filter(Boolean).join(' · ')) }],
    r.events, { filter: !o.compact, empty: o.compact ? 'No session changes in the last 30 days.' : 'No changes in this period.' });
  return r;
}
PAGES.changes = async c => {
  c.innerHTML = head('Change log', 'what changed on the router, and when') + `<div id="chS"></div>
    <div class="card"><div class="bar"><div class="seg pchips" id="chK">${CHG_KINDS.map(([k, l]) => `<button data-k="${k}" class="${CHG.kind === k ? 'on' : ''}">${l}</button>`).join('')}</div><span class="sp"></span>
      <select id="chD">${[[1 / 24, 'Last hour'], [1, 'Last 24 hours'], [7, 'Last 7 days'], [30, 'Last 30 days'], [180, 'Last 6 months']].map(([d, l]) => `<option value="${d}" ${+CHG.days === d ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      <div id="chT" class="muted">Loading…</div></div>
    <div class="card"><h3>Routing table size <span class="sp"></span><span class="seg" id="chRR">${['day', 'week', 'month'].map(k => `<button data-r="${k}" class="${k === 'day' ? 'on' : ''}">${{ day: '1 day', week: '1 week', month: '1 month' }[k]}</button>`).join('')}</span></h3>
      <div class="muted" style="margin-bottom:8px">Routes per protocol, sampled about every 5 minutes. A full Internet table is counted, not listed route by route; a sudden drop or jump of 5% or more is logged above.</div><div id="chRC" style="height:220px"></div><div id="chRS" class="muted"></div></div>`;
  const kinds = () => (CHG_KINDS.find(x => x[0] === CHG.kind) || CHG_KINDS[0])[2];
  const load = async () => {
    const r = await changeTable($('#chT'), { kind: kinds(), days: CHG.days });
    if (r.error) return;
    const p = r.poller, tr = r.tracked || {}, n24 = r.counts || {};
    const watch = p.on ? `Watched around the clock over SNMP${p.last ? ' · last check ' + ago(p.last) : ''}.`
      : p.saved && p.snmp ? `The around-the-clock check could not read the router${p.error ? ': ' + p.error : ''}. Changes are still logged while this page or others read the router.`
      : 'Checked while someone has the app open on this router. For around-the-clock tracking, save it with an SNMP login under Manager › Routers.';
    $('#chS').innerHTML = `<div class="tiles">${[['arp', 'ARP entries'], ['mac', 'MAC entries'], ['route', 'Direct + static routes'], ['bgp', 'BGP sessions']].map(([k, l]) =>
      `<div class="tile"><div class="l">${l}</div><div class="v">${tr[k] != null ? Fmt.num(tr[k]) : '-'}</div><div class="muted" style="font-size:12px">${(n => `${Fmt.num(n)} change${n === 1 ? '' : 's'} in 24 h`)(k === 'route' ? (n24.route || 0) + (n24.routes || 0) : k === 'bgp' ? (n24.bgp || 0) + (n24.peer || 0) : n24[k] || 0)}</div></div>`).join('')}</div>
      <p class="muted" style="margin-top:-6px">${esc(watch)}${r.ssh ? '' : ' Route, MAC and route-policy changes need the router command line: sign in with SSH to include them.'}</p>`;
  };
  $('#chK').onclick = e => { const b = e.target.closest('button[data-k]'); if (!b) return; CHG.kind = b.dataset.k; c.querySelectorAll('#chK button').forEach(x => x.classList.toggle('on', x === b)); load(); };
  $('#chD').onchange = () => { CHG.days = +$('#chD').value; load(); };
  const rc = chart($('#chRC'), { fmt: Fmt.num, span: 86400 });
  const sizes = async rng => {
    const keys = ['BGP', 'DIRECT', 'STATIC', 'Total'], got = {};
    for (const k of keys) got[k] = (await api(`/api/changes/series?kind=routes&key=${k}&range=${rng}`)).points;
    rc.opts.span = { day: 86400, week: 604800, month: 2592000 }[rng];
    const main = got.BGP.length ? 'BGP' : 'Total', vals = got[main].map(p => p[1]);
    rc.opts.min = vals.length && Math.min(...vals) > 1000 ? Math.floor(Math.min(...vals) * 0.98) : 0;
    rc.set([{ name: main === 'BGP' ? 'BGP routes' : 'All routes', color: '--s1', values: got[main].map(p => [p[0], p[1]]) }]);
    const last = k => got[k].length ? Fmt.num(got[k][got[k].length - 1][1]) : '-';
    $('#chRS').innerHTML = got.Total.length || got.BGP.length ? `Now: total ${last('Total')} · BGP ${last('BGP')} · direct ${last('DIRECT')} · static ${last('STATIC')}` : 'No counts yet: they are sampled about every 5 minutes.';
  };
  $('#chRR').onclick = e => { const b = e.target.closest('button[data-r]'); if (!b) return; c.querySelectorAll('#chRR button').forEach(x => x.classList.toggle('on', x === b)); sizes(b.dataset.r); };
  await Promise.all([load(), sizes('day')]);
  every(30000, async () => { if ($('#modalBg').classList.contains('hidden')) await load(); });
};

/* ----- route policies: rules, counters, who uses them, and a prefix check ----- */
let POLSEL = '';
PAGES.policies = async c => {
  const [d, lat] = await Promise.all([api('/api/view/route_policies'), api('/api/changes/latest?kind=policy').catch(() => ({ now: {}, hour: {}, day: {} }))]);
  const users = {}; Object.entries(d.peers).forEach(([peer, x]) => [['import', 'in'], ['export', 'out']].forEach(([k, w]) => { if (x[k]) (users[x[k]] = users[x[k]] || []).push(`${peer} ${w}`); }));
  const delta = (k, key, now) => { const o = lat[k][key]; return o != null && now != null && (!lat.now[key] || o[0] < lat.now[key][0]) ? now - o[1] : null; };
  const pols = d.policies.slice().sort((a, b) => (b.name === POLSEL) - (a.name === POLSEL) || (!!users[b.name]) - (!!users[a.name]) || a.name.localeCompare(b.name));
  POLDATA = { d, users };
  c.innerHTML = head('Route policies', `${d.policies.length} route-policies · ${Object.keys(d.prefixes).length} prefix lists`,
    `<button class="btn" onclick="polRule()">+ New policy</button><button class="btn" onclick="plEntry()">+ Prefix list entry</button>`) +
    `<div class="card"><h3>What would a policy do to a prefix?</h3><div class="row" style="gap:6px;flex-wrap:wrap"><input id="ptP" placeholder="Prefix, e.g. 8.8.8.0/24" style="max-width:220px">
      <select id="ptN"><option value="">All policies in use</option>${d.policies.map(p => `<option ${p.name === POLSEL ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select><button class="btn" id="ptB">Check</button></div>
      <div class="muted" style="margin-top:6px">Worked out from the policy rules and prefix lists read from the router. Rules about AS path, communities and the like depend on the route itself, so they are shown as “depends”.</div><div id="ptR"></div></div>
    ${pols.map((p, i) => `<div class="card" id="pol-${i}"><h3>${esc(p.name)} <span class="sp"></span><span class="muted" style="font-weight:400;font-size:12px">${users[p.name] ? 'used by ' + users[p.name].map(u => `${esc(u)} <button class="btn sm" title="take it off this peer" onclick='polDetach(${attr(p.name)},${attr(u)})'>×</button>`).join(' ') : 'not used by a BGP peer'}</span>
      <button class="btn sm" onclick='polRule(${attr(p.name)})'>+ Rule</button><button class="btn sm" onclick='polAttach(${attr(p.name)})'>Attach to peer</button><button class="btn sm" onclick='polCopy(${attr(p.name)})'>Copy as…</button><button class="btn sm danger" onclick='polDelete(${attr(p.name)})'>Delete</button></h3><div class="polT"></div></div>`).join('')}
    <div class="card"><h3>Prefix lists</h3><div id="plT"></div></div>
    ${d.as_path ? `<div class="card"><h3>AS-path filters</h3><pre>${esc(d.as_path)}</pre></div>` : ''}${d.community ? `<div class="card"><h3>Community filters</h3><pre>${esc(d.community)}</pre></div>` : ''}`;
  pols.forEach((p, i) => table(c.querySelector(`#pol-${i} .polT`), [
    { t: 'Node', k: 'node', num: true }, { t: 'Action', k: 'action', f: n => `<span class="pill ${n.action === 'permit' ? 'p-ok' : 'p-bad'}">${esc(n.action)}</span>` },
    { t: 'Matches when', k: 'match', cls: 'mono wrap', f: n => n.match.length ? n.match.map(esc).join('<br>') : '<span class="faint">every route</span>' },
    { t: 'Then', k: 'apply', cls: 'mono wrap', f: n => n.action === 'deny' ? '<span class="faint">rejected</span>' : n.apply.length ? n.apply.map(esc).join('<br>') : '<span class="faint">accepted as is</span>' },
    { t: 'Matched (total)', k: 'matched', num: true, f: n => Fmt.num(n.matched) }, { t: 'Last hour', k: 'h', num: true, f: n => delta('hour', `${p.name} ${n.node}`, n.matched) == null ? '<span class="faint">-</span>' : Fmt.num(delta('hour', `${p.name} ${n.node}`, n.matched)) },
    { t: 'Last 24 h', k: 'd', num: true, f: n => delta('day', `${p.name} ${n.node}`, n.matched) == null ? '<span class="faint">-</span>' : Fmt.num(delta('day', `${p.name} ${n.node}`, n.matched)) },
    { t: '', sort: false, cls: 'acts', f: n => `<button class="btn sm" onclick='polRule(${attr(p.name)},${n.node})'>Edit</button><button class="btn sm danger" onclick='polNodeRemove(${attr(p.name)},${n.node})'>Remove</button>` }],
    p.nodes, { filter: false, empty: 'No nodes' }));
  const plUse = name => d.policies.filter(p => p.nodes.some(n => n.match.includes('if-match ip-prefix ' + name))).map(p => p.name);
  table($('#plT'), [{ t: 'Prefix list', k: 'name', f: x => `<b>${esc(x.name)}</b>` }, { t: 'Index', k: 'index', num: true }, { t: 'Action', k: 'action', f: x => `<span class="pill ${x.action === 'permit' ? 'p-ok' : 'p-bad'}">${esc(x.action)}</span>` },
    { t: 'Prefix', k: 'prefix', cls: 'mono', f: x => `${esc(x.prefix)}${x.ge != null ? ' ge ' + x.ge : ''}${x.le != null ? ' le ' + x.le : ''}` },
    { t: 'Used by', k: 'used', f: x => esc(plUse(x.name).join(', ') || '-') }, { t: 'Permitted / denied (list)', k: 'permitted', num: true, f: x => `${Fmt.num(x.permitted)} / ${Fmt.num(x.denied)}` },
    { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm" onclick='plEntry(${attr(x.name)},${x.index})'>Edit</button><button class="btn sm" onclick='plEntry(${attr(x.name)})'>+ Entry</button><button class="btn sm danger" onclick='plRemove(${attr(x.name)},${x.index})'>Remove</button>` }],
    Object.entries(d.prefixes).flatMap(([name, x]) => x.entries.map(e => ({ name, ...e, permitted: x.permitted, denied: x.denied, used: plUse(name).join(',') }))), { empty: 'No prefix lists' });
  const check = async () => {
    const pfx = $('#ptP').value.trim(); if (!pfx) return;
    const name = $('#ptN').value;
    let r; try { r = await api('/api/policy/test?' + new URLSearchParams({ prefix: pfx, ...(name ? { policy: name } : {}) })); } catch (e) { $('#ptR').innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
    const res = name ? r.results : r.results.filter(x => users[x.policy]);
    const word = { permit: ['p-ok', 'accepted'], deny: ['p-bad', 'rejected'], depends: ['p-warn', 'depends'] };
    $('#ptR').innerHTML = `<table class="tbl" style="margin-top:10px"><thead><tr><th>Policy</th><th>Used by</th><th>Result</th><th>How</th></tr></thead><tbody>${res.map(x => `<tr><td><b>${esc(x.policy)}</b></td><td>${esc((users[x.policy] || []).join(', ') || '-')}</td>
      <td><span class="pill ${word[x.result][0]}">${word[x.result][1]}</span>${x.node != null ? ` <span class="muted">node ${x.node}</span>` : ''}</td>
      <td class="wrap">${x.steps.map(s => `<div><span class="mono">${s.node} ${esc(s.action)}</span>: ${s.matches === true ? 'matches' : s.matches === false ? 'no' : 'maybe'}, ${esc(s.why)}</div>`).join('')}${x.apply.length ? `<div>then <span class="mono">${x.apply.map(esc).join('; ')}</span></div>` : ''}${x.note ? `<div class="muted">${esc(x.note)}</div>` : ''}</td></tr>`).join('')}</tbody></table>`;
  };
  $('#ptB').onclick = check; $('#ptP').onkeydown = e => { if (e.key === 'Enter') check(); };
  if (POLSEL) { c.querySelector('#pol-0')?.scrollIntoView({ block: 'start' }); POLSEL = ''; }
};

/* editing: every change goes through the usual Review / Check only / Apply dialog */
let POLDATA = { d: { policies: [], prefixes: {}, peers: {} }, users: {} };
function polUsers(name) { return (POLDATA.users[name] || []).map(u => { const [peer, w] = u.split(' '); return { peer, dir: w === 'in' ? 'import' : 'export' }; }); }
function polGuard(name) {   // a policy in use: say which peers, ask for the name, offer a soft refresh after the commit
  const us = polUsers(name);
  if (!us.length) return {};
  return { confirm: name, intro: `${name} is in use by BGP peer ${us.map(u => `${u.peer} (${u.dir})`).join(', ')}. The change applies to ${us.length > 1 ? 'those sessions' : 'that session'} straight away. Use Check only first.`,
    extra: [{ k: '_refresh', label: 'Refresh those peers\' routes after the commit (soft refresh: sessions stay up)', type: 'check', value: true }],
    after: async (commit, sent) => {
      if (commit && sent && sent._refresh) for (const u of us) {
        try { await post('/api/action', { action: 'bgp.reset', params: { peer: u.peer, soft: u.dir }, commit: true }); toast(`Refreshed ${u.peer} ${u.dir}`); } catch (e) { toast(`Refresh ${u.peer}: ${e.message}`, true); }
      }
      refresh();
    } };
}
function polRule(name, node) {
  const pol = POLDATA.d.policies.find(p => p.name === name), n = pol && node != null ? pol.nodes.find(x => x.node === node) : null;
  const next = pol && pol.nodes.length ? Math.max(...pol.nodes.map(x => x.node)) + 10 : 10;
  const g = name ? polGuard(name) : {}, lists = Object.keys(POLDATA.d.prefixes);
  change({ title: !name ? 'New route-policy' : n ? `Edit ${name} node ${node}` : `Add a rule to ${name}`, action: 'policy.node_set', params: { ...(name ? { name } : {}), ...(n ? { node, replace: true } : {}) },
    intro: [g.intro, n ? 'The rule is replaced as a whole (its old clauses are removed in the same commit).' : '', 'Rules are checked from the lowest node number up; the first rule that matches decides. A route no rule matches is rejected.'].filter(Boolean).join(' '),
    confirm: g.confirm, after: g.after,
    fields: [...(name ? [] : [{ k: 'name', label: 'Policy name', ph: 'e.g. ACE-IN' }]), ...(n ? [] : [{ k: 'node', label: 'Node (order)', type: 'number', value: next }]),
      { k: 'action', label: 'Action', type: 'select', options: [['permit', 'permit: accept (and apply the changes below)'], ['deny', 'deny: reject']], value: n ? n.action : 'permit' },
      { k: 'match', label: 'Matches when (one per line, all must match; empty = every route)', type: 'textarea', value: n ? n.match.map(x => x.replace(/^if-match /, '')).join('\n') : '',
        ph: `ip-prefix ${lists[0] || 'SAVENET'}\nas-path-filter 10\ncommunity-filter NAME\nlarge-community-filter NAME` },
      { k: 'apply', label: 'Then set (permit only, one per line)', type: 'textarea', value: n ? n.apply.map(x => x.replace(/^apply /, '')).join('\n') : '',
        ph: 'local-preference 200\ncost 50\ncommunity 329021:100 additive\nas-path 329021 329021 additive' }, ...(g.extra || [])] });
}
function polNodeRemove(name, node) {
  const g = polGuard(name), pol = POLDATA.d.policies.find(p => p.name === name);
  change({ title: `Remove node ${node} from ${name}`, action: 'policy.node_remove', params: { name, node }, confirm: g.confirm, after: g.after, fields: g.extra || [],
    intro: [g.intro, pol && pol.nodes.length <= 1 ? 'This is its only rule: removing it deletes the policy.' : ''].filter(Boolean).join(' ') });
}
function polDelete(name) {
  const us = polUsers(name);
  change({ title: `Delete route-policy ${name}`, action: 'policy.remove', params: { name }, confirm: name,
    block: us.length ? `${name} is used by BGP peer ${us.map(u => `${u.peer} (${u.dir})`).join(', ')}. Take it off with × next to each peer first.` : '' });
}
function polCopy(name) {
  const pol = POLDATA.d.policies.find(p => p.name === name);
  change({ title: `Copy ${name}`, action: 'policy.copy', params: { nodes: pol.nodes.map(n => ({ node: n.node, action: n.action, match: n.match, apply: n.apply })) },
    intro: `Makes a new policy with the same ${pol.nodes.length} rules. It is not used by any peer until you attach it.`, fields: [{ k: 'new_name', label: 'New policy name', ph: name.replace(/-(IN|OUT)$/, '-COPY-$1') }] });
}
async function polAttach(name) {
  const b = await api('/api/view/bgp');
  change({ title: `Attach ${name} to a BGP peer`, action: 'bgp.peer_policy', params: { policy: name, local_as: b.local_as },
    intro: 'Replaces the policy the peer has in that direction now. It applies to the session straight away.', confirm: name,
    fields: [{ k: 'peer', label: 'Peer', type: 'select', options: b.peers.map(p => [p.peer, `${p.peer} (AS ${p.as})${POLDATA.d.peers[p.peer] ? ' · now ' + ['import', 'export'].map(k => POLDATA.d.peers[p.peer][k] || '-').join(' / ') : ''}`]) },
      { k: 'direction', label: 'Direction', type: 'select', options: [['import', 'import: routes received from the peer'], ['export', 'export: routes sent to the peer']] }] });
}
async function polDetach(name, u) {
  const [peer, w] = u.split(' '), dir = w === 'in' ? 'import' : 'export', b = await api('/api/view/bgp');
  change({ title: `Take ${name} off ${peer} (${dir})`, action: 'bgp.peer_policy', params: { peer, direction: dir, old: name, local_as: b.local_as }, confirm: name,
    intro: `Without a policy, ${dir === 'import' ? 'every route the peer sends is accepted' : 'every best route is announced to the peer'}. Make sure that is what you want.` });
}
function plEntry(name, index) {
  const list = name ? POLDATA.d.prefixes[name] : null, e = list && index != null ? list.entries.find(x => x.index === index) : null;
  const next = list && list.entries.length ? Math.max(...list.entries.map(x => x.index)) + 10 : 10;
  const using = name ? POLDATA.d.policies.filter(p => p.nodes.some(n => n.match.includes('if-match ip-prefix ' + name))).map(p => p.name) : [];
  change({ title: e ? `Edit ${name} index ${index}` : name ? `Add an entry to ${name}` : 'New prefix list entry', action: 'prefix.entry_set',
    params: { ...(name ? { name } : {}), ...(e ? { index, replace: true } : {}) },
    intro: (using.length ? `Used by ${using.join(', ')}: the change applies to the BGP sessions using those policies straight away. ` : '') + 'Without ge / le only this exact prefix length matches.',
    fields: [...(name ? [] : [{ k: 'name', label: 'Prefix list name', ph: 'e.g. CUSTOMERS' }]), ...(e ? [] : [{ k: 'index', label: 'Index (order)', type: 'number', value: next }]),
      { k: 'action', label: 'Action', type: 'select', options: ['permit', 'deny'], value: e ? e.action : 'permit' }, { k: 'prefix', label: 'Prefix', ph: '102.216.116.0/23', value: e ? e.prefix : '' },
      { k: 'ge', label: 'Greater-equal (optional)', type: 'number', value: e && e.ge != null ? e.ge : '' }, { k: 'le', label: 'Less-equal (optional)', type: 'number', value: e && e.le != null ? e.le : '' }] });
}
function plRemove(name, index) {
  const list = POLDATA.d.prefixes[name];
  change({ title: `Remove ${name} index ${index}`, action: 'prefix.entry_remove', params: { name, index },
    intro: list && list.entries.length <= 1 ? 'This is its only entry: removing it deletes the list.' : '' });
}
function peerForm() {
  change({ title: 'New BGP peer', action: 'bgp.peer_add', params: { local_as: bgpState.local_as }, fields: [
    { k: 'peer', label: 'Peer address', ph: '10.10.3.2' }, { k: 'remote_as', label: 'Remote AS', ph: bgpState.local_as + ' for iBGP' }, { k: 'description', label: 'Description' },
    { k: 'connect_interface', label: 'Update source', ph: 'LoopBack0 (iBGP)', list: ['LoopBack0'] }, { k: 'ebgp_multihop', label: 'EBGP multihop', type: 'number', ph: 'optional' },
    { k: 'password', label: 'MD5 password', type: 'password', ph: 'optional' }, { k: 'route_policy_in', label: 'Import route-policy', ph: 'optional' }, { k: 'route_policy_out', label: 'Export route-policy', ph: 'optional' },
    { k: 'route_limit', label: 'Max prefixes', type: 'number', ph: 'optional' }, { k: 'next_hop_local', label: 'next-hop-local', type: 'check' }] });
}
async function bgpRoutes(peer, which, lookup) {
  $('#bRt').textContent = lookup ? `BGP paths for ${lookup}` : peer ? `Routes ${which} ${which === 'received' ? 'from' : 'to'} ${peer}` : 'BGP routing table';
  $('#bR').innerHTML = '<span class="muted">Loading…</span>'; $('#bRcard').scrollIntoView({ behavior: 'smooth' });
  let r; try { r = await api('/api/bgp/routes?' + new URLSearchParams(lookup ? { lookup } : peer ? { peer, which } : {})); } catch (e) { $('#bR').innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  if (!r.routes.length) { $('#bR').innerHTML = `<pre>${esc(r.raw)}</pre>`; return; }
  table($('#bR'), [{ t: 'Status', k: 'status', cls: 'mono' }, { t: 'Network', k: 'network', f: x => `<b>${esc(x.network)}</b>` }, { t: 'Next hop', k: 'next_hop', cls: 'mono' }, { t: 'MED', k: 'med', num: true }, { t: 'Local pref', k: 'local_pref', num: true }, { t: 'AS path / origin', k: 'path', cls: 'mono' }], r.routes);
}

PAGES.ospf = async c => {
  const o = await api('/api/view/ospf');
  c.innerHTML = head('OSPF', '', `<button class="btn" onclick='change({title:"Reset OSPF process",action:"ospf.reset",fields:[{k:"process",label:"Process",type:"number",value:1}],intro:"All adjacencies of the process will restart."})'>Reset process</button>`) +
    `<div class="card"><h3>Neighbors</h3><div id="oN"></div></div><div class="card"><h3>Interfaces</h3><div id="oI"></div></div>
    <div class="card"><h3>Networks <span class="sp"></span><button class="btn sm" onclick="ospfNet('ospf.network_add','Add OSPF network')">+ Network</button><button class="btn sm" onclick="ospfNet('ospf.network_remove','Remove OSPF network')">− Network</button></h3><div class="muted">Networks are listed in the configuration; use these to add or remove them.</div></div>
    <div class="card"><h3>Link-state database</h3><pre>${esc(o.lsdb)}</pre></div>`;
  table($('#oN'), [{ t: 'Neighbor ID', k: 'neighbor', f: x => `<b>${esc(x.neighbor)}</b>` }, { t: 'Process', k: 'process' }, { t: 'Area', k: 'area' }, { t: 'Interface', k: 'interface' }, { t: 'State', k: 'state', f: x => pill(x.state) }], o.peers, { empty: 'No OSPF neighbors' });
  table($('#oI'), [{ t: 'Interface', k: 'interface', f: x => `<b>${esc(x.interface)}</b>` }, { t: 'Area', k: 'area' }, { t: 'IP', k: 'ip', cls: 'mono' }, { t: 'Type', k: 'type' }, { t: 'State', k: 'state', f: x => pill(x.state) }, { t: 'Cost', k: 'cost', num: true }, { t: 'Priority', k: 'priority', num: true }, { t: 'DR', k: 'dr' }, { t: 'BDR', k: 'bdr' },
    { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm" onclick='change({title:"OSPF cost on ${esc(x.interface)}",action:"ospf.interface_cost",params:{interface:${attr(x.interface)}},fields:[{k:"cost",label:"Cost",type:"number",value:${x.cost}}]})'>Cost</button>` }], o.interfaces, { empty: 'No OSPF interfaces' });
  every(15000, async () => { if ($('#modalBg').classList.contains('hidden')) PAGES.ospf(c); });
};
function ospfNet(action, title) { change({ title, action, fields: [{ k: 'process', label: 'Process', type: 'number', value: 1 }, { k: 'area', label: 'Area', value: '0.0.0.0' }, { k: 'prefix', label: 'Network', ph: '172.16.0.4/31' }] }); }

PAGES.isis = async c => {
  const o = await api('/api/view/isis');
  let names = [];
  try { const v = await api('/api/view/interfaces'); names = (Array.isArray(v) ? v : v.interfaces || []).map(i => i.name); } catch (e) { }
  ISIS_IFS = names;
  const proc = (o.processes[0] || {}).process || '1';
  const btns = o.configured ? `<button class="btn" onclick="isisEnable(${attr(proc)})">Enable on interface</button><button class="btn" onclick='change({title:"Reset IS-IS adjacencies",action:"isis.reset",fields:[{k:"process",label:"Process",type:"number",value:${attr(proc)}}],intro:"All IS-IS adjacencies of the process restart. Traffic that depends on IS-IS routes is interrupted until they come back."})'>Reset process</button>` : '';
  if (!o.configured) {
    c.innerHTML = head('IS-IS', 'not configured', '') + `<div class="card"><h3>IS-IS is not running on this router</h3>
      <p class="muted">No IS-IS process, interface or neighbor was found. To start IS-IS: create a process with a network entity title (NET), then enable it on the core links and the loopback.
      Nothing is sent until you review the exact commands.</p>
      <button class="btn primary" onclick="isisProcess()">Create IS-IS process</button></div>`;
    return;
  }
  c.innerHTML = head('IS-IS', '', btns) +
    `<div class="card"><h3>Processes <span class="sp"></span><button class="btn sm" onclick="isisProcess()">+ Process</button></h3><div id="iP"></div></div>
    <div class="card"><h3>Neighbors</h3><div id="iN"></div></div>
    <div class="card"><h3>Interfaces</h3><div id="iI"></div></div>
    <div class="card"><h3>Routes learned</h3><div id="iR"></div></div>
    <div class="card"><h3>Link-state database</h3><pre>${esc(o.lsdb || '(empty)')}</pre></div>`;
  table($('#iP'), [{ t: 'Process', k: 'process', f: x => `<b>${esc(x.process)}</b>` }, { t: 'Level', k: 'level' }, { t: 'Cost style', k: 'cost_style' },
    { t: 'Network entity (NET)', k: 'nets', cls: 'mono', f: x => esc(x.nets.join(', ') || '(none)') }, { t: 'Hostname', k: 'is_name' },
    { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm" onclick='change({title:"IS-IS ${esc(x.process)} level",action:"isis.level",params:{process:${attr(x.process)}},fields:[{k:"level",label:"Level",type:"select",options:[["level-1","Level-1 (inside an area)"],["level-2","Level-2 (backbone)"],["level-1-2","Level-1-2 (both)"]],value:${attr(x.level)}}],intro:"Changing the level restarts adjacencies that no longer match."})'>Level</button><button class="btn sm" onclick='change({title:"Add NET to IS-IS ${esc(x.process)}",action:"isis.net_add",params:{process:${attr(x.process)}},fields:[{k:"net",label:"Network entity (NET)",ph:"49.0001.0010.0000.0001.00"}]})'>+ NET</button>` }], o.processes, { empty: 'No IS-IS process in the configuration' });
  table($('#iN'), [{ t: 'System ID', k: 'system_id', f: x => `<b>${esc(x.system_id)}</b>` }, { t: 'Process', k: 'process' }, { t: 'Interface', k: 'interface' }, { t: 'State', k: 'state', f: x => pill(x.state) }, { t: 'Hold', k: 'hold' }, { t: 'Type', k: 'type' }, { t: 'Priority', k: 'priority' }], o.peers, { empty: 'No IS-IS neighbors' });
  table($('#iI'), [{ t: 'Interface', k: 'interface', f: x => `<b>${esc(x.interface)}</b>` }, { t: 'Process', k: 'process' }, { t: 'IPv4 state', k: 'ipv4', f: x => x.ipv4 ? pill(x.ipv4) : '<span class="muted">not running</span>' },
    { t: 'Level', k: 'type' }, { t: 'Network', k: 'p2p', f: x => x.p2p ? 'Point-to-point' : (x.interface.startsWith('LoopBack') ? 'Loopback' : 'Broadcast') }, { t: 'Cost', k: 'cost', num: true, f: x => x.cost == null ? '<span class="muted">default</span>' : esc(x.cost + (x.cost_level ? ' (' + x.cost_level + ')' : '')) }, { t: 'MTU', k: 'mtu', num: true }, { t: 'DIS', k: 'dis' },
    { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm" onclick='change({title:"IS-IS cost on ${esc(x.interface)}",action:"isis.interface_cost",params:{interface:${attr(x.interface)}},fields:[{k:"cost",label:"Cost",type:"number",value:${x.cost || 10}},{k:"level",label:"Level",type:"select",options:[["","Both levels"],["level-1","Level-1 only"],["level-2","Level-2 only"]],value:${attr(x.cost_level)}}]})'>Cost</button><button class="btn sm danger" onclick='change({title:"Disable IS-IS on ${esc(x.interface)}",action:"isis.interface_disable",params:{interface:${attr(x.interface)}},intro:"The adjacency on this interface goes down and routes through it are withdrawn."})'>Disable</button>` }], o.interfaces, { empty: 'IS-IS is not enabled on any interface' });
  table($('#iR'), [{ t: 'Prefix', k: 'prefix', f: x => `<b>${esc(x.prefix)}</b>` }, { t: 'Level', k: 'level' }, { t: 'Cost', k: 'cost', num: true }, { t: 'Exit interface', k: 'interface' }, { t: 'Next hop', k: 'next_hop', cls: 'mono' }, { t: 'Flags', k: 'flags', cls: 'mono' }], o.routes, { empty: 'No IS-IS routes' });
  every(15000, async () => { if ($('#modalBg').classList.contains('hidden')) PAGES.isis(c); });
};
let ISIS_IFS = [];
function isisProcess() {
  change({ title: 'Create IS-IS process', action: 'isis.process_add', intro: 'The NET is the area followed by a 6-byte system ID, often built from the loopback address: 10.0.0.1 → 0100.0000.0001.',
    fields: [{ k: 'process', label: 'Process', type: 'number', value: 1 }, { k: 'net', label: 'Network entity (NET)', ph: '49.0001.0100.0000.0001.00' },
      { k: 'level', label: 'Level', type: 'select', options: [['level-2', 'Level-2 (backbone, usual for an ISP core)'], ['level-1', 'Level-1 (inside an area)'], ['level-1-2', 'Level-1-2 (both)']], value: 'level-2' },
      { k: 'cost_style', label: 'Cost style', type: 'select', options: [['wide', 'Wide (recommended, needed for MPLS TE)'], ['narrow', 'Narrow'], ['compatible', 'Compatible'], ['wide-compatible', 'Wide-compatible']], value: 'wide' },
      { k: 'is_name', label: 'Hostname shown to neighbors (optional)', ph: 'BNG-NDOVU1' }] });
}
function isisEnable(proc) {
  change({ title: 'Enable IS-IS on an interface', action: 'isis.interface_enable',
    fields: [{ k: 'interface', label: 'Interface', list: ISIS_IFS, ph: '100GE0/4/0' }, { k: 'process', label: 'Process', type: 'number', value: proc },
      { k: 'p2p', label: 'Point-to-point link (usual for router-to-router links)', type: 'check', value: true }, { k: 'cost', label: 'Cost (optional)', type: 'number', ph: '10' }] });
}

PAGES.mpls = async c => {
  const m = await api('/api/view/mpls');
  c.innerHTML = head('MPLS / LDP') + `<div class="card"><h3>LDP sessions</h3><div id="mL"></div></div><div class="card"><h3>LSPs</h3><pre>${esc(m.lsp)}</pre></div>`;
  table($('#mL'), [{ t: 'Peer', k: 'peer', f: x => `<b>${esc(x.peer)}</b>` }, { t: 'State', k: 'state', f: x => pill(x.state) }, { t: 'Label mode', k: 'lam' }, { t: 'Role', k: 'role' }, { t: 'Age (d:h:m)', k: 'age' }, { t: 'Keepalive sent/rcvd', k: 'keepalive' }], m.ldp, { empty: 'No LDP sessions' });
};

/* ----- System ----- */
PAGES.hardware = async c => {
  const h = await api('/api/view/hardware');
  c.innerHTML = head('Hardware') + `<div class="card"><h3>Boards</h3><div id="hB"></div></div><div class="grid g2"><div class="card"><h3>Temperature</h3><div id="hT"></div></div><div class="card"><h3>Serial numbers</h3><pre>${esc(h.elabel)}</pre></div>
    <div class="card"><h3>Power supplies</h3><pre>${esc(h.power)}</pre></div><div class="card"><h3>Fans</h3><pre>${esc(h.fan)}</pre></div></div>
    ${h.optics && Object.keys(h.optics).length ? '<div class="card"><h3>Optics</h3><div id="hO"></div></div>' : ''}`;
  if (h.optics && Object.keys(h.optics).length) {
    const dbm = (v, lo, hi) => v == null ? '-' : `<span class="${(lo != null && v < lo) || (hi != null && v > hi) ? 'p-bad' : ''}">${v} dBm</span>`;
    table($('#hO'), [{ t: 'Port', k: 'port', f: x => `<b>${esc(x.port)}</b>` }, { t: 'Rx', k: 'rx_dbm', num: true, f: x => dbm(x.rx_dbm, x.rx_low, x.rx_high) }, { t: 'Rx range', k: 'rx_low', f: x => x.rx_low != null ? `${x.rx_low} to ${x.rx_high}` : '' },
      { t: 'Tx', k: 'tx_dbm', num: true, f: x => dbm(x.tx_dbm, x.tx_low, x.tx_high) }, { t: 'Temp', k: 'temp', num: true, f: x => Fmt.temp(x.temp) }, { t: 'Module', k: 'part', f: x => esc([x.vendor, x.part].filter(Boolean).join(' ')) }, { t: 'nm', k: 'wavelength', num: true }],
      Object.entries(h.optics).map(([port, o]) => ({ port, ...o })), { filter: false });
  }
  table($('#hB'), [{ t: 'Slot', k: 'slot', num: true, f: x => `<b>${esc(x.slot)}</b>` }, { t: 'Type', k: 'type' }, { t: 'Online', k: 'online', f: x => pill(x.online) }, { t: 'Register', k: 'register' }, { t: 'Status', k: 'status', f: x => x.status === 'NA' ? '' : pill(x.status) }, { t: 'Role', k: 'role' }], h.boards, { filter: false });
  table($('#hT'), [{ t: 'Slot', k: 'slot' }, { t: 'Board', k: 'board' }, { t: 'Status', k: 'status', f: x => pill(x.status) }, { t: 'Now', k: 'temp', num: true, f: x => Fmt.temp(x.temp) }, { t: 'Minor', k: 'minor', num: true, f: x => Fmt.temp(x.minor) }, { t: 'Major', k: 'major', num: true, f: x => Fmt.temp(x.major) }], h.temperature, { filter: false });
};

PAGES.users = async c => {
  const s = await api('/api/view/system');
  c.innerHTML = head('Users') + `<div class="card"><h3>Local users</h3><div id="uT"></div></div><div class="card"><h3>Logged-in sessions</h3><pre>${esc(s.online)}</pre></div>`;
  table($('#uT'), [{ t: 'Name', k: 'name', f: x => `<b>${esc(x.name)}</b>` }, { t: 'State', k: 'state', f: x => pill(x.state) }, { t: 'Level', k: 'level', num: true }, { t: 'Auth', k: 'service' },
    { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm" onclick='change({title:"Password for ${esc(x.name)}",action:"user.password",params:{name:${attr(x.name)}},fields:[{k:"password",label:"New password",type:"password"}]})'>Password</button><button class="btn sm danger" onclick='change({title:"Delete user ${esc(x.name)}",action:"user.remove",params:{name:${attr(x.name)}}})'>Delete</button>` }],
    s.users, { toolbar: `<button class="btn" onclick='change({title:"New user",action:"user.add",fields:[{k:"name",label:"Username"},{k:"password",label:"Password",type:"password",help:"At least 8 characters."},{k:"level",label:"Level",type:"select",value:3,options:[[0,"0 visit"],[1,"1 monitor"],[2,"2 configure"],[3,"3 manage"]]}]})'>+ User</button>` });
};

PAGES.settings = async c => {
  const s = await api('/api/view/system');
  c.innerHTML = head('Settings') + `<div class="grid g2">
    <div class="card"><h3>Identity</h3><div class="bar"><b style="font-size:16px">${esc(s.sysname)}</b><span class="sp"></span><button class="btn" onclick='change({title:"Change identity (sysname)",action:"system.sysname",fields:[{k:"name",label:"Sysname",value:${attr(s.sysname)}}]})'>Change</button></div><h3 style="margin-top:14px">Clock</h3><pre>${esc(s.clock)}</pre></div>
    <div class="card"><h3>NTP</h3><div id="sNtp"></div><pre style="margin-top:10px">${esc(s.ntp)}</pre></div>
    <div class="card"><h3>SNMP <span class="sp"></span><button class="btn sm" onclick='change({title:"SNMP community",action:"system.snmp_community",fields:[{k:"community",label:"Community",type:"password"},{k:"access",label:"Access",type:"select",options:["read","write"]}]})'>+ Community</button></h3><pre>${esc(s.snmp || '(no communities)')}</pre></div>
    <div class="card"><h3>Syslog</h3><div class="muted" style="margin-bottom:8px">Send logs to a remote syslog server.</div><button class="btn" onclick='change({title:"Syslog server",action:"system.syslog_host",fields:[{k:"host",label:"Server IP",ph:"192.168.0.5"}]})'>+ Syslog server</button></div></div>`;
  table($('#sNtp'), [{ t: 'Server', k: 's', f: x => `<b>${esc(x.s)}</b>` }, { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm danger" onclick='change({title:"Remove NTP server",action:"system.ntp_remove",params:{server:${attr(x.s)}}})'>Remove</button>` }],
    s.ntp_servers.map(x => ({ s: x })), { filter: false, toolbar: `<button class="btn" onclick='change({title:"Add NTP server",action:"system.ntp_add",fields:[{k:"server",label:"Server IP"}]})'>+ NTP server</button>`, empty: 'No NTP servers' });
};

PAGES.logs = async c => {
  const l = await api('/api/view/logs');
  c.innerHTML = head('Logs & alarms', 'refreshes every 10 s') + `<div class="card"><h3>Active alarms</h3><div id="lA"></div></div><div class="card"><h3>Log buffer</h3><div id="lL"></div></div>`;
  const draw = l => {
    table($('#lA'), [{ t: 'Time', k: 'time' }, { t: 'Level', k: 'level', f: x => `<span class="pill ${/critical|major/i.test(x.level) ? 'p-bad' : 'p-warn'}">${esc(x.level)}</span>` }, { t: 'Alarm', k: 'name', f: x => `<b>${esc(x.name)}</b>` }, { t: 'Description', k: 'description', cls: 'wrap' }], l.alarms, { filter: false, empty: 'No active alarms' });
    if (l.log.length) table($('#lL'), [{ t: 'Time', k: 'time' }, { t: 'Sev', k: 'severity', num: true }, { t: 'Module', k: 'tag', f: x => `<span class="mono">${esc(x.tag.replace(/^%%\d+/, '').split('(')[0])}</span>` }, { t: 'Message', k: 'message', cls: 'wrap' }], l.log);
    else $('#lL').innerHTML = `<pre>${esc(l.log_raw)}</pre>`;
  };
  draw(l); every(10000, async () => draw(await api('/api/view/logs')));
};

/* ----- Billing (Splynx) ----- */
let billMeta = null;
PAGES.billing = async c => {
  const b = await api('/api/billing'); await ensureIfs();
  const cfg = b.config, last = b.last || {}, n = last.counts || {};
  if (!billMeta && (cfg.url || b.demo)) billMeta = await api('/api/billing/meta').catch(() => null);
  const name = (list, id) => ((list || []).find(x => +x.id === +id) || {}).name || ((list || []).find(x => +x.id === +id) || {}).title || id;
  const modeTxt = { off: 'Off', 'dry-run': 'Dry run (report only)', enforce: 'Enforcing' }[cfg.mode];
  const since = last.time ? `last sync ${Fmt.time(last.time)}` : 'not synced yet';
  c.innerHTML = head('Splynx billing', `${modeTxt} · ${since}`, `<button class="btn" id="bDry">Check now (dry run)</button><button class="btn primary" id="bApply">Sync &amp; apply now</button>`) +
    (last.error ? `<div class="banner">Splynx: ${esc(last.error)}</div>` : '') +
    `<div class="tiles">${[['Customers in scope', n.customers ?? '-', (cfg.partner_ids || []).map(x => name(billMeta?.partners, x)).join(', ') || 'all partners'],
      ['Matched to a port', n.matched ?? '-', 'by IP, description or manual'], ['Not matched', n.unmatched ?? '-', 'map them manually below'],
      ['Blocked in Splynx', n.blocked ?? '-', (cfg.block_statuses || []).join(' / ')], ['Ports shut by billing', Object.keys(b.shut_by_billing).length, 'only these are re-enabled']]
      .map(([l, v, s]) => `<div class="tile"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${esc(s)}</div></div>`).join('')}</div>
    <div class="card"><h3>Customers and their router ports</h3><div id="bT"></div></div>
    <div class="grid g2"><div class="card"><h3>Settings</h3><div id="bS"></div></div><div class="card"><h3>Activity log</h3><div id="bL"></div></div></div>`;
  $('#bDry').onclick = () => billSync(false); $('#bApply').onclick = () => billSync(true);
  billUsage();
  const rows = last.rows || [];
  table($('#bT'), [
    { t: 'ID', k: 'id', num: true }, { t: 'Customer', k: 'name', f: r => `<b>${esc(r.name)}</b>` },
    { t: 'Splynx status', k: 'status', f: r => `<span class="pill ${r.status === 'active' ? 'p-ok' : r.status === 'new' ? 'p-warn' : 'p-bad'}">${esc(r.status)}</span>` },
    { t: 'Plan', k: 'plans' }, { t: 'IP', k: 'ips', cls: 'mono' },
    { t: 'Router port and traffic', k: 'interface', f: r => r.interface ? `<b>${esc(r.interface)}</b> <span class="faint">${esc(r.match)}</span>${r.shared ? ' <span class="pill p-warn">shared</span>' : ''}<div data-use="${esc(r.interface)}" style="font-size:12px;line-height:1.5;margin-top:2px"></div>` : `<span class="faint">${r.match === 'excluded' ? 'excluded' : 'not matched'}</span>` },
    { t: 'Port', k: 'if_state', f: r => r.if_state ? pill(r.if_state) : '' },
    { t: 'Action', k: 'planned', cls: 'wrap', f: r => r.planned ? `<b style="color:${/shutdown$|shutdown \(/.test(r.planned) && !/undo/.test(r.planned) ? 'var(--bad)' : 'var(--ok)'}">${esc(r.planned)}</b>` : `<span class="faint">${esc(r.note || (r.interface ? 'in sync' : ''))}</span>` },
    { t: '', sort: false, cls: 'acts', f: r => (b.demo ? (r.status === 'active' ? `<button class="btn sm" onclick='billDemo(${r.id},"blocked")'>Simulate block</button>` : `<button class="btn sm" onclick='billDemo(${r.id},"active")'>Simulate payment</button>`) : '') +
      `<button class="btn sm" onclick='billMapForm(${r.id},${attr(r.name)},${attr(r.interface || "")})'>Map port</button>` +
      (r.match === 'manual' || r.match === 'excluded' ? `<button class="btn sm" onclick='billMap(${r.id},null)'>Auto</button>` : '') },
  ], rows, { empty: last.time ? 'No customers match the partner / location filter' : 'Press “Check now” to load customers from Splynx', rowClass: r => r.status !== 'active' ? 'off' : '' });
  table($('#bL'), [{ t: 'Time', k: 'time', f: x => Fmt.time(x.time) }, { t: 'Port', k: 'interface', f: x => `<b>${esc(x.interface)}</b>` }, { t: 'Action', k: 'action' },
    { t: 'Result', k: 'result', f: x => `<span class="pill ${x.result === 'ok' ? 'p-ok' : 'p-bad'}">${esc(x.result)}</span>` }, { t: 'Why', k: 'reason', cls: 'wrap' }], b.log, { filter: false, empty: 'No changes made yet' });
  const opts = (list, sel, key) => (list || []).map(x => `<label style="display:inline-flex;gap:5px;margin:0 12px 4px 0;font-size:12.5px"><input type="checkbox" data-${key}="${x.id}" ${sel.includes(+x.id) ? 'checked' : ''}>${esc(x.name || x.title)}</label>`).join('');
  $('#bS').innerHTML = `<div class="form">
    ${b.demo ? '<div class="help" style="grid-column:1/3;margin:0 0 6px">Demo mode uses sample Splynx data. On a real router, enter your Splynx URL and an API key (Splynx: Administration › API keys, with read access to customers, internet services, partners, locations and routers).</div>' : ''}
    <label>Splynx URL</label><input id="sUrl" value="${esc(cfg.url)}" placeholder="https://billing.example.com" ${b.demo ? 'disabled' : ''}>
    <label>API key</label><input id="sKey" value="${esc(cfg.api_key)}" ${b.demo ? 'disabled' : ''}>
    <label>API secret</label><input id="sSec" type="password" value="${esc(cfg.api_secret)}" ${b.demo ? 'disabled' : ''}>
    <label>Partners (resellers)</label><div id="sPart">${billMeta ? opts(billMeta.partners, cfg.partner_ids, 'p') : `<input id="sPartTxt" value="${esc(cfg.partner_ids.join(','))}" placeholder="partner IDs, e.g. 13">`}</div>
    <label>Locations</label><div id="sLoc">${billMeta ? opts(billMeta.locations, cfg.location_ids, 'l') : `<input id="sLocTxt" value="${esc(cfg.location_ids.join(','))}" placeholder="location IDs, e.g. 6">`}</div>
    <div class="help">Leave all unticked to include every partner / location.</div>
    <label>Splynx router</label><select id="sRouter"><option value="0">Any</option>${(billMeta?.routers || []).map(x => `<option value="${x.id}" ${+x.id === +cfg.router_id ? 'selected' : ''}>${esc(x.title)} ${x.model ? '(' + esc(x.model) + ')' : ''}</option>`).join('')}</select>
    <div class="help">Services on other routers are ignored. Services with no router set are always included.</div>
    <label>Shut port when status is</label><div>${['blocked', 'disabled', 'new'].map(x => `<label style="display:inline-flex;gap:5px;margin-right:12px"><input type="checkbox" data-st="${x}" ${cfg.block_statuses.includes(x) ? 'checked' : ''}>${x}</label>`).join('')}</div>
    <label>Match by description</label><label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="sDesc" ${cfg.match_description ? 'checked' : ''} style="width:auto"> interface description contains the Splynx customer ID or login</label>
    <label>Check every (s)</label><input id="sInt" type="number" value="${cfg.interval}">
    <label>Mode</label><div class="seg" id="sMode">${[['off', 'Off'], ['dry-run', 'Dry run'], ['enforce', 'Enforce']].map(([k, l]) => `<button data-m="${k}" class="${cfg.mode === k ? 'on' : ''}">${l}</button>`).join('')}</div>
    <div class="help">Dry run only reports what it would do. Enforce shuts and re-enables ports automatically.</div></div>
    <div class="err" id="sErr"></div><div class="actions"><button class="btn" id="sTest" ${b.demo ? 'disabled' : ''}>Test connection</button><button class="btn primary" id="sSave">Save settings</button></div>`;
  let mode = cfg.mode;
  $('#sMode').onclick = e => { if (e.target.dataset.m) { mode = e.target.dataset.m; $('#sMode').querySelectorAll('button').forEach(x => x.classList.toggle('on', x.dataset.m === mode)); } };
  const ids = (sel, txt) => billMeta ? [...c.querySelectorAll(sel)].filter(x => x.checked).map(x => +Object.values(x.dataset)[0]) : ($(txt).value.split(/[,\s]+/).filter(Boolean).map(Number));
  const collect = () => ({ url: $('#sUrl').value.trim(), api_key: $('#sKey').value.trim(), api_secret: $('#sSec').value, partner_ids: ids('[data-p]', '#sPartTxt'), location_ids: ids('[data-l]', '#sLocTxt'),
    router_id: $('#sRouter').value, block_statuses: [...c.querySelectorAll('[data-st]')].filter(x => x.checked).map(x => x.dataset.st), match_description: $('#sDesc').checked, interval: $('#sInt').value, mode });
  $('#sSave').onclick = async () => {
    if (mode === 'enforce' && cfg.mode !== 'enforce' && !confirm('Enforce mode will shut down router ports of blocked customers automatically. Continue?')) return;
    try { await post('/api/billing/config', collect()); toast('Billing settings saved'); billMeta = null; PAGES.billing(c); } catch (e) { $('#sErr').textContent = e.message; } };
  $('#sTest').onclick = async () => { $('#sErr').textContent = ''; try { await post('/api/billing/config', { ...collect(), mode: cfg.mode }); billMeta = await api('/api/billing/meta'); toast(`Connected to Splynx: ${billMeta.partners.length} partners, ${billMeta.locations.length} locations, ${billMeta.routers.length} routers`); PAGES.billing(c); } catch (e) { $('#sErr').textContent = e.message; } };
  every(10000, async () => { if ($('#modalBg').classList.contains('hidden') && !c.contains(document.activeElement)) PAGES.billing(c); });
};
async function billUsage() {
  // download = traffic the router sends out of the customer's port; upload = what comes in from them
  const u = await api('/api/billing/usage').catch(() => null); if (!u) return;
  document.querySelectorAll('[data-use]').forEach(el => {
    const p = u.ports[el.dataset.use]; if (!p) { el.textContent = ''; return; }
    if (p.tx_bps == null) { el.innerHTML = `↓ ${(+p.out_util || 0).toFixed(1)}% ↑ ${(+p.in_util || 0).toFixed(1)}% <span class="faint">of the port</span>`; return; }
    const pct = p.plan_bps ? ` <span class="faint">(${Math.round(p.tx_bps / p.plan_bps * 100)}% of ${Fmt.bps(p.plan_bps)})</span>` : '';
    el.innerHTML = `↓ <b>${Fmt.bps(p.tx_bps)}</b> ↑ ${Fmt.bps(p.rx_bps)}${pct}` +
      (p.window ? ` <span class="faint" title="peak ↓ ${Fmt.bps(p.tx_peak)} ↑ ${Fmt.bps(p.rx_peak)}">· last ${Math.max(1, Math.round(p.window / 60))} min ↓ ${Fmt.bytes(p.tx_bytes)} ↑ ${Fmt.bytes(p.rx_bytes)}</span>` : '');
  });
}
async function billSync(apply, quiet) {
  if (apply && !quiet && !confirm('Apply now: shut ports of blocked customers and re-enable ports of customers who paid?')) return;
  try { const r = await post('/api/billing/sync', { apply }); const p = r.last.planned || [];
    toast(p.length ? `${apply ? 'Applied' : 'Would apply'}: ` + p.map(a => `${a.action} ${a.interface}`).join(', ') : 'Everything is in sync'); refresh(); }
  catch (e) { toast(e.message, true); }
}
async function billMap(id, iface) { await post('/api/billing/map', { customer_id: id, interface: iface }); await post('/api/billing/sync', { apply: false }); refresh(); }
function billMapForm(id, name, cur) {
  const m = $('#modal');
  m.innerHTML = `<h2>Router port for ${esc(name)}</h2><div class="form"><label>Interface</label><select id="mIf"><option value="">(do not manage this customer)</option>${ifCache.filter(i => !['null', 'loopback'].includes(i.type)).map(i => `<option ${i.name === cur ? 'selected' : ''} value="${esc(i.name)}">${esc(i.name)}${i.description ? ' · ' + esc(i.description) : ''}</option>`).join('')}</select></div>
    <div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Save mapping</button></div>`;
  $('#modalBg').classList.remove('hidden');
  m.querySelector('.actions').onclick = async e => { if (e.target.dataset.a === 'c') return closeModal(); if (e.target.dataset.a === 's') { const v = $('#mIf').value; closeModal(); await billMap(id, v); toast(v ? `Mapped to ${v}` : 'Customer excluded'); } };
}
async function billDemo(id, status) { await post('/api/billing/demo-status', { customer_id: id, status }); toast(status === 'blocked' ? 'Splynx (demo): customer blocked' : 'Splynx (demo): payment received, customer active'); await billSync(true, true); }

/* ----- Tools ----- */
PAGES.traffic = async c => {
  await ensureIfs(); ifCache = await api('/api/view/interfaces');
  const up = ifCache.filter(i => i.phy === 'up' && !['null', 'loopback'].includes(i.type));
  const sel = c._sel || up.sort((a, b) => (b.in_util + b.out_util) - (a.in_util + a.out_util)).slice(0, 4).map(i => i.name);
  c.innerHTML = head('Traffic monitor', 'live Rx/Tx, sampled every 5 s') + `<div class="card"><div class="bar"><span class="muted">Interfaces (up to 6):</span>${ifCache.filter(i => !['null'].includes(i.type)).map(i => `<label style="font-size:12.5px;white-space:nowrap"><input type="checkbox" value="${esc(i.name)}" ${sel.includes(i.name) ? 'checked' : ''}> ${esc(i.name)}</label>`).join('')}</div></div>
    <div class="grid g2" id="tG">${sel.map((n, k) => `<div class="card"><h3>${esc(n)}<span class="sp"></span><button class="btn sm" onclick='interfaceDrawer(${attr(n)})'>Details</button></h3><div class="legend"><span><i style="background:var(--s1)"></i>Rx <b id="rx${k}">-</b></span><span><i style="background:var(--s2)"></i>Tx <b id="tx${k}">-</b></span></div><div id="tc${k}"></div></div>`).join('')}</div>`;
  c.querySelectorAll('input[type=checkbox]').forEach(cb => cb.onchange = () => { c._sel = [...c.querySelectorAll('input:checked')].map(x => x.value).slice(0, 6); PAGES.traffic(c); });
  const cs = sel.map((n, k) => chart($('#tc' + k), { fmt: Fmt.bps }));
  const tick = async () => {
    const t = await api('/api/traffic?names=' + encodeURIComponent(sel.join(',')));
    sel.forEach((n, k) => { const s = t[n] || []; cs[k].set([{ name: 'Rx', color: '--s1', values: s.map(x => [x[0], x[1]]) }, { name: 'Tx', color: '--s2', values: s.map(x => [x[0], x[2]]) }]);
      if (s.length) { $('#rx' + k).textContent = Fmt.bps(s.at(-1)[1]); $('#tx' + k).textContent = Fmt.bps(s.at(-1)[2]); } });
  };
  await tick(); every(5000, tick);
};

/* ----- Graphs: pick an interface on the left, see now / 1 day / 1 week / 1 month / 1 year ----- */
let graphSel = ''; try { graphSel = localStorage.getItem('ne8000.graph') || ''; } catch (e) {}
/* full screen for any panel: Esc or the button again leaves it */
function toggleFull(el) {
  if (document.fullscreenElement) return document.exitFullscreen();
  if (el.requestFullscreen) el.requestFullscreen().catch(() => el.classList.toggle('fsfake')); else el.classList.toggle('fsfake');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') document.querySelectorAll('.fsfake').forEach(x => x.classList.remove('fsfake')); });
function showGraphs(name) { graphSel = name; try { localStorage.setItem('ne8000.graph', name); } catch (e) {} closeDrawer(); location.hash = 'graphs'; if (current === 'graphs') refresh(); }
PAGES.graphs = async c => {
  ifCache = await api('/api/view/interfaces');
  let flows = []; try { flows = (await api('/api/flows')).subnets || []; } catch (e) { }
  const list = [...ifCache.filter(i => !['null'].includes(i.type)), ...flows.map(f => ({ name: 'flow:' + f.subnet, description: 'on ' + f.interface, iface: f.interface, phy: f.tx_bps || f.rx_bps ? 'up' : 'down', flow: true }))];
  if (!list.find(i => i.name === graphSel)) graphSel = (list.filter(i => i.phy === 'up').sort((a, b) => (b.in_util + b.out_util) - (a.in_util + a.out_util))[0] || list[0] || {}).name || '';
  const isFlow = graphSel.startsWith('flow:'), subnet = graphSel.slice(5);
  const R = [['now', isFlow ? 'Last hour (1-minute, NetFlow)' : 'Now (live, 5 s)'], ['day', '1 day (5-minute average)'], ['week', '1 week (30-minute average)'], ['month', '1 month (hourly average)'], ['year', '1 year (daily average)']];
  const sel = list.find(i => i.name === graphSel) || {};
  c.innerHTML = head('Graphs', 'interface traffic history') + `<div class="glay">
    <div class="card glist"><input id="gF" placeholder="Filter interfaces" autocomplete="off"><div id="gL">${list.map((i, k) => (i.flow && !list[k - 1]?.flow ? '<div class="glh">Addresses (NetFlow)</div>' : '') + `<a data-g="${esc(i.name)}" class="${i.name === graphSel ? 'on' : ''}" title="${esc(i.description || '')}"><span class="dot ${i.phy === 'up' ? 'ok' : 'off'}"></span><b>${esc(i.name)}</b><small>${esc(i.description || '')}</small></a>`).join('')}</div></div>
    <div id="gRight" class="fsbox"><button class="btn sm fsx" onclick="toggleFull($('#gRight'))">✕ Exit full screen</button><div class="card"><h3>${esc(graphSel)} <span class="faint" style="font-weight:400">${esc(sel.description || '')}</span><span class="sp"></span><button class="btn sm" onclick='interfaceDrawer(${attr(isFlow ? sel.iface : graphSel)})'>${isFlow ? 'Interface' : 'Details'}</button><button class="btn sm" onclick="toggleFull($('#gRight'))" title="full screen (Esc to leave)">⛶ Full screen</button></h3><div id="gNote" class="muted" style="font-size:12.5px">${isFlow ? 'Rx = from this subnet (upload), Tx = to it (download), from the router\'s sampled NetStream export.' : ''}</div></div>
    ${isFlow ? '<div class="card"><h3>Busiest addresses <span class="faint" style="font-weight:400">last 5-10 minutes</span></h3><div id="gHosts"></div></div>' : ''}
    ${R.map(([k, l]) => `<div class="card"><h3>${l}<span class="sp"></span><span class="legend" id="gS_${k}"></span></h3><div id="gC_${k}"></div></div>`).join('')}</div></div>`;
  $('#gL').onclick = e => { const a = e.target.closest('a[data-g]'); if (a) { graphSel = a.dataset.g; try { localStorage.setItem('ne8000.graph', graphSel); } catch (x) {} PAGES.graphs(c); } };
  $('#gF').oninput = () => { const q = $('#gF').value.toLowerCase(); c.querySelectorAll('#gL a').forEach(a => a.style.display = a.textContent.toLowerCase().includes(q) ? '' : 'none'); };
  c.querySelector('#gL a.on')?.scrollIntoView({ block: 'nearest' });
  if (!graphSel) return;
  const span = { day: 86400, week: 604800, month: 2592000, year: 31536000 };
  const cs = Object.fromEntries(R.map(([k]) => [k, chart($('#gC_' + k), { fmt: Fmt.bps, span: span[k] })]));
  const summary = (k, pts) => { if (!pts.length) { $('#gS_' + k).innerHTML = ''; return; }
    const avg = j => pts.reduce((a, p) => a + p[j], 0) / pts.length, max = j => Math.max(...pts.map(p => p[j] ?? p[j - 2]));
    $('#gS_' + k).innerHTML = `<span><i style="background:var(--s1)"></i>Rx avg <b>${Fmt.bps(avg(1))}</b> peak <b>${Fmt.bps(max(pts[0].length > 3 ? 3 : 1))}</b></span><span><i style="background:var(--s2)"></i>Tx avg <b>${Fmt.bps(avg(2))}</b> peak <b>${Fmt.bps(max(pts[0].length > 3 ? 4 : 2))}</b></span>`; };
  const put = (k, pts) => { cs[k].set([{ name: 'Rx', color: '--s1', values: pts.map(x => [x[0], x[1]]) }, { name: 'Tx', color: '--s2', values: pts.map(x => [x[0], x[2]]) }]); summary(k, pts); };
  const live = isFlow ? async () => { const t = await api('/api/flows/live?subnet=' + encodeURIComponent(subnet)); put('now', t.points);
      table($('#gHosts'), [{ t: 'Address', k: 'address', cls: 'mono' }, { t: 'Rx (from)', k: 'rx_bytes', num: true, f: h => Fmt.bytes(h.rx_bytes) }, { t: 'Tx (to)', k: 'tx_bytes', num: true, f: h => Fmt.bytes(h.tx_bytes) }], t.hosts, { filter: false }); }
    : async () => { const t = await api('/api/traffic?names=' + encodeURIComponent(graphSel)); put('now', t[graphSel] || []); };
  const hist = async () => { let note = '';
    for (const k of ['day', 'week', 'month', 'year']) { const g = await api(`/api/graph?name=${encodeURIComponent(graphSel)}&range=${k}`); put(k, g.points);
      note = g.note || (g.error ? 'History collector: ' + g.error : g.collecting && !g.points.length ? 'History is sampled every 5 minutes on the server; the first points appear about 10 minutes after it starts.' : ''); }
    $('#gNote').textContent = note; };
  await Promise.all([live(), hist()]); every(isFlow ? 60000 : 5000, live); every(300000, hist);
};

/* ----- traffic per address (NetStream / NetFlow) ----- */
PAGES.netflow = async c => {
  const [f, ov] = await Promise.all([api('/api/flows'), api('/api/view/overview').catch(() => ({}))]);
  ifCache = await api('/api/view/interfaces');
  const st = f.status || {}, ex = Object.entries(st.exporters || {});
  const ago = t => t ? Math.round(Date.now() / 1000 - t) + ' s ago' : 'never';
  c.innerHTML = head('Traffic per address', 'per subnet and per address, from the router\'s NetStream (NetFlow) export',
    `<button class="btn" id="nfSetup">Router setup…</button>`) +
    `<div class="card"><h3>Collector</h3><div class="kv">${st.demo ? '<div><span>Mode</span>demo: made-up traffic</div>' : `
      <div><span>Listening</span>${st.running ? `UDP ${esc(st.port)}` : 'off (set NE8000_NETFLOW_PORT)'}</div>
      <div><span>Flow packets</span>${Fmt.num(st.packets)} · last ${ago(st.last)}</div>
      <div><span>From this router</span>${ex.length ? ex.map(([ip, e]) => `${esc(ip)} (v${e.version}, 1:${Math.round(e.sampling)} sampling, ${Fmt.num(e.packets)} packets)`).join(', ') : 'nothing yet'}</div>
      <div><span>Not matched / dropped</span>${Fmt.num(st.unmatched)} flows / ${Fmt.num(st.dropped)} packets from unknown senders</div>`}</div>
      ${f.note ? `<p class="muted">${esc(f.note)}</p>` : !st.demo && !ex.length ? '<p class="muted">No flows from this router yet. Use <b>Router setup</b> to send its NetStream export here; graphs fill in about 5 minutes after the first flows arrive.</p>' : ''}</div>
    <div class="card"><div id="nfT"></div></div>`;
  table($('#nfT'), [{ t: 'Subnet', k: 'subnet', cls: 'mono', f: x => `<b>${esc(x.subnet)}</b>` }, { t: 'Interface', k: 'interface' },
    { t: 'Rx (from)', k: 'rx_bps', num: true, f: x => x.rx_bps == null ? '<span class="faint">-</span>' : Fmt.bps(x.rx_bps) },
    { t: 'Tx (to)', k: 'tx_bps', num: true, f: x => x.tx_bps == null ? '<span class="faint">-</span>' : Fmt.bps(x.tx_bps) },
    { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm" onclick='showGraphs(${attr('flow:' + x.subnet)})'>Graph</button>` }],
    f.subnets || [], { onRow: x => showGraphs('flow:' + x.subnet) });
  $('#nfSetup').onclick = () => netstreamForm(ov);
};
function netstreamForm(ov) {
  const host = /^\d+\.\d+\.\d+\.\d+$/.test(location.hostname) ? location.hostname : '';
  const ifs = ifCache.filter(i => i.ip && !/^(loopback|inloopback|null|virtual-template|meth|ethernet0\/0\/0$)/i.test(i.name) && !/OAM|MGMT/i.test(i.vpn || '') && i.type !== 'management').map(i => i.name);
  change({ title: 'Send NetStream to the manager', action: 'netstream.enable',
    intro: 'Samples 1 in N packets inbound on each interface (so each flow is counted once) and exports NetFlow v9 to this server every minute. It only counts; it does not block or limit traffic. Press Check only first: the router checks the lines and nothing is committed.',
    fields: [{ k: 'collector', label: 'This server\'s address (as the router reaches it)', value: host, ph: '102.216.116.66' },
      { k: 'port', label: 'UDP port', type: 'number', value: 2055 },
      { k: 'source', label: 'Export source (an address on the router)', value: ((ifCache.find(i => /^loopback0$/i.test(i.name)) || {}).ip || '').split('/')[0] || (ov && ov.host) || '', ph: 'e.g. the LoopBack0 address' },
      { k: 'sampling', label: 'Sampling, 1 in N packets', type: 'number', value: 1000, help: 'The manager multiplies by N. 1000 is light on the router and accurate for busy links.' },
      { k: 'slots', label: 'Slots that sample (self sampling)', ph: 'e.g. 9', help: 'The slot of the board that forwards (on an NE8000 M8 the IPU, usually 9). "display ip netstream statistics slot N" says "not self sampling" until this is set.' },
      { k: 'interfaces', label: `Interfaces (${ifs.length})`, type: 'textarea', value: ifs.join('\n'), help: 'Every interface that carries traffic: uplinks and customer ports. Remove any you want left out.' }],
    after: () => refresh() });
}

PAGES.ping = async c => {
  c.innerHTML = head('Ping / Traceroute') + `<div class="card"><div class="form" style="max-width:640px">
    <label>Target</label><input id="pT" placeholder="8.8.8.8"><label>Tool</label><select id="pK"><option value="ping">Ping</option><option value="tracert">Traceroute</option></select>
    <label>Count</label><input id="pC" type="number" value="5"><label>Packet size</label><input id="pS" type="number" placeholder="56">
    <label>Max hops (traceroute)</label><input id="pM" type="number" value="15"><label>Probes per hop (traceroute)</label><input id="pQ" type="number" value="1">
    <label>Source address</label><input id="pA" placeholder="optional"><label>VPN instance</label><input id="pV" placeholder="optional"></div>
    <div class="bar" style="margin-top:12px"><button class="btn primary" id="pGo">Start</button><span class="muted mono" id="pCmd"></span></div><pre id="pOut" class="hidden"></pre></div>`;
  const build = () => { const k = $('#pK').value, t = $('#pT').value.trim(); let cmd = k;
    if (k === 'ping') { if ($('#pC').value) cmd += ' -c ' + (+$('#pC').value); if ($('#pS').value) cmd += ' -s ' + (+$('#pS').value); }
    else { if ($('#pQ').value) cmd += ' -q ' + (+$('#pQ').value); if ($('#pM').value) cmd += ' -m ' + (+$('#pM').value); }   // keeps a silent path inside the time limit
    if ($('#pA').value.trim()) cmd += ' -a ' + $('#pA').value.trim(); if ($('#pV').value.trim()) cmd += ' -vpn-instance ' + $('#pV').value.trim();
    return cmd + ' ' + t; };
  c.querySelectorAll('input,select').forEach(e => e.oninput = () => $('#pCmd').textContent = build());
  $('#pT').onkeydown = e => { if (e.key === 'Enter') $('#pGo').click(); };
  $('#pGo').onclick = async () => { const o = $('#pOut');
    if (!$('#pT').value.trim()) { o.classList.remove('hidden'); o.textContent = 'Type a target address or name first, e.g. 8.8.8.8.'; $('#pT').focus(); return; } o.classList.remove('hidden'); o.textContent = 'Running ' + build() + ' …';
    try { o.textContent = (await post('/api/cli', { command: build() })).output; } catch (e) { o.textContent = 'Error: ' + e.message; } };
};

const QUICK_HW = ['display interface brief', 'display ip interface brief', 'display ip routing-table', 'display bgp peer', 'display ospf peer brief', 'display isis peer', 'display mpls ldp session', 'display arp all', 'display lldp neighbor brief', 'display alarm active', 'display device', 'display cpu-usage', 'display memory-usage', 'display optical-module brief', 'display logbuffer', 'display current-configuration'];
const QUICK_MT = ['/interface print', '/ip address print', '/ip route print count-only', '/routing bgp session print', '/routing ospf neighbor print', '/ip arp print', '/ip neighbor print', '/system resource print', '/system health print', '/log print', '/interface ethernet monitor sfp28-1 once', '/export'];
let cliHist = [];
PAGES.console = async c => {
  const QUICK = VENDOR === 'mikrotik' ? QUICK_MT : QUICK_HW, mt = VENDOR === 'mikrotik';
  c.innerHTML = head('Console', mt ? 'RouterOS print, monitor, export, ping and traceroute (read-only)' : 'display, ping and tracert. Use Config editor for changes.') + `<div class="card"><div class="bar"><input id="cIn" class="mono" style="flex:1" placeholder="${mt ? '/ip route print where dst-address=0.0.0.0/0' : 'display interface GigabitEthernet0/1/0'}" list="cQ"><datalist id="cQ">${QUICK.map(q => `<option value="${q}">`).join('')}</datalist><button class="btn primary" id="cGo">Run</button></div>
    <div class="bar">${QUICK.slice(0, 10).map(q => `<button class="btn sm" data-q="${q}">${q.replace('display ', '').replace(/^\//, '')}</button>`).join('')}</div><pre id="cOut" style="max-height:65vh">${esc(cliHist.at(-1)?.out || '')}</pre></div>`;
  let hi = cliHist.length;
  const run = async cmd => { if (!cmd) return; $('#cIn').value = cmd; $('#cOut').textContent = `<${$('#tName').textContent}>${cmd}\n…`;
    try { const r = await post('/api/cli', { command: cmd }); $('#cOut').textContent = `<${$('#tName').textContent}>${cmd}\n${r.output}`; cliHist.push({ cmd, out: $('#cOut').textContent }); hi = cliHist.length; }
    catch (e) { $('#cOut').textContent = 'Error: ' + e.message; } };
  $('#cGo').onclick = () => run($('#cIn').value.trim());
  $('#cIn').onkeydown = e => { if (e.key === 'Enter') run($('#cIn').value.trim());
    if (e.key === 'ArrowUp' && hi > 0) { hi--; $('#cIn').value = cliHist[hi].cmd; e.preventDefault(); }
    if (e.key === 'ArrowDown' && hi < cliHist.length - 1) { hi++; $('#cIn').value = cliHist[hi].cmd; e.preventDefault(); } };
  c.querySelectorAll('[data-q]').forEach(b => b.onclick = () => run(b.dataset.q));
  $('#cIn').focus();
};

PAGES.config = async c => {
  c.innerHTML = head('Config editor', 'lines run in system-view, then commit (VRP two-stage configuration)') +
    `<div class="card"><textarea id="eT" spellcheck="false" placeholder="interface GigabitEthernet0/1/6&#10; description To-Customer-E&#10; ip address 10.10.6.1 30&#10; undo shutdown">${esc(c._draft || '')}</textarea>
    <div class="bar" style="margin-top:10px"><button class="btn" id="eCheck">Check only (abort)</button><button class="btn primary" id="eApply">Apply &amp; commit</button><span class="sp"></span><button class="btn" id="eLoad">View running config</button></div><div class="err" id="eErr"></div><pre id="eOut" class="hidden" style="margin-top:10px"></pre></div>`;
  $('#eT').oninput = () => c._draft = $('#eT').value;
  const go = async commit => { const config = $('#eT').value; if (!config.trim()) return; if (commit && !confirm('Apply and commit these lines on ' + $('#tName').textContent + '?')) return;
    $('#eErr').textContent = ''; const o = $('#eOut'); o.classList.remove('hidden'); o.textContent = commit ? 'Applying…' : 'Checking…';
    try { const r = await post('/api/config', { config, commit }); o.textContent = r.output; toast(commit ? 'Committed' : 'Checked, nothing committed');
      done({ check: !commit, title: 'Config editor', commit_id: r.commit_id, time: r.time, lines: config.split('\n').filter(l => l.trim()) }); }
    catch (e) { o.classList.add('hidden'); $('#eErr').textContent = '✗ ' + e.message; if (commit) done({ fail: true, title: 'Config editor', text: e.message }); } };
  $('#eCheck').onclick = () => go(false); $('#eApply').onclick = () => go(true);
  $('#eLoad').onclick = async () => { const o = $('#eOut'); o.classList.remove('hidden'); o.textContent = 'Loading…'; o.textContent = (await api('/api/running-config')).output; };
};

PAGES.backup = async c => {
  const r = await api('/api/backups'); RESTORE_ON = !!r.restore;
  const KIND = { manual: 'by hand', daily: 'daily', 'after-change': 'after a change', 'after-save': 'after Save', 'before-restore': 'before a restore', upload: 'uploaded' };
  c.innerHTML = head('Backup', 'copies of the router configuration kept on this server', `<a class="btn" href="/api/running-config?download=1">Download running config</a><button class="btn" onclick="saveCfg()">Save configuration</button>`) +
    `<div class="card"><h3>Copies on the server</h3>
      <div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:8px"><input id="bkNote" placeholder="Note (optional), e.g. before ACE peering" style="max-width:320px"><button class="btn primary" id="bkTake">Back up now</button>
        <label class="btn" style="cursor:pointer">Upload a .cfg<input type="file" id="bkFile" accept=".cfg,.txt,.dat,text/plain" style="display:none"></label></div>
      <p class="muted" style="margin:0 0 10px">A copy is also kept automatically after every change and every Save made in this app, and once a day while someone is signed in with SSH. An automatic copy is only kept when the configuration really changed. Pinned, by-hand and uploaded copies are kept until you delete them; the newest 60 automatic ones are kept.${r.ssh ? '' : ' <b>Copying or restoring reads the router over SSH: you are asked for your SSH login the first time. The daily copy runs only while someone is signed in with SSH.</b>'}</p>
      <div id="bkT"></div></div><div class="card hidden" id="bkD"><h3 id="bkDt">Differences</h3><div id="bkDs"></div><pre id="bkDu" class="diff"></pre></div>`;
  table($('#bkT'), [{ t: 'Taken', k: 'time', f: m => `<b>${esc(new Date(m.time * 1000).toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }))}</b>${m.pinned ? ' 📌' : ''}` },
    { t: 'How', k: 'kind', f: m => esc(KIND[m.kind] || m.kind) }, { t: 'By', k: 'by' }, { t: 'Note', k: 'note', cls: 'wrap' }, { t: 'Lines', k: 'lines', num: true, f: m => Fmt.num(m.lines) },
    { t: '', sort: false, cls: 'acts', f: m => `<a class="btn sm" href="/api/backups/${m.id}/download">Download</a><button class="btn sm" onclick='bkDiff(${attr(m.id)})'>Compare with router</button>
      <button class="btn sm" onclick='bkPin(${attr(m.id)},${!m.pinned})'>${m.pinned ? 'Unpin' : 'Pin'}</button><button class="btn sm" onclick='bkNoteEdit(${attr(m.id)},${attr(m.note || '')})'>Note</button>
      ${RESTORE_ON ? `<button class="btn sm" onclick='bkRestore(${attr(m.id)})'>Restore…</button>` : ''}<button class="btn sm danger" onclick='bkDelete(${attr(m.id)})'>Delete</button>` }],
    r.backups, { empty: 'No copies yet. Press Back up now.' });
  $('#bkTake').onclick = async () => { $('#bkTake').disabled = true; try { await post('/api/backups', { note: $('#bkNote').value }); toast('Copy kept on the server'); PAGES.backup(c); } catch (e) { toast(e.message, true); $('#bkTake').disabled = false; } };
  $('#bkFile').onchange = async () => { const f = $('#bkFile').files[0]; if (!f) return;
    if (f.size > 8 * 1024 * 1024) return toast('That file is larger than 8 MB.', true);
    try { await post('/api/backups/upload', { text: await f.text(), name: f.name, note: $('#bkNote').value || f.name }); toast('Uploaded ' + f.name); PAGES.backup(c); } catch (e) { toast(e.message, true); } };
};
let RESTORE_ON = false;
/* Restore a server copy: compare, Check only on the router (it loads the file, lists its own changes, throws it away),
   then type the copy's id to commit. A copy of the router as it is now is kept first. Save stays manual. */
async function bkRestore(id) {
  const m = $('#modal');
  m.innerHTML = `<h2>Restore copy ${esc(id)}</h2>
    <p class="muted" style="margin-top:-4px">The router's whole running configuration is replaced by this copy (load configuration file … replace, then commit). A copy of the router as it is now is kept on the server first, so this can be undone the same way. The startup configuration is not touched: press Save afterwards to keep it over a reboot.</p>
    <div id="rsSum" class="muted">Comparing with the router…</div><pre id="rsOut" class="diff hidden" style="max-height:320px"></pre>
    <form class="form hidden" id="rsF" onsubmit="return false"><label>Type ${esc(id)} to confirm</label><input name="_confirm" placeholder="${esc(id)}" autocomplete="off"></form>
    <div class="err"></div>
    <div class="actions"><button class="btn" data-a="cancel">Cancel</button><button class="btn" data-a="check">Check only</button><button class="btn primary danger" data-a="apply" disabled>Restore &amp; commit</button></div>`;
  $('#modalBg').classList.remove('hidden');
  const err = m.querySelector('.err'), out = $('#rsOut');
  const paint = t => { out.classList.remove('hidden'); out.innerHTML = t.split('\n').map(l => `<span class="${l.startsWith('+') ? 'dadd' : l.startsWith('-') ? 'ddel' : ''}">${esc(l)}</span>`).join('\n'); };
  api(`/api/backups/${id}/diff?against=running`).then(d => { if (!m.contains($('#rsSum'))) return;
    $('#rsSum').textContent = d.added || d.removed ? `Restoring takes back ${d.added} line${d.added === 1 ? '' : 's'} that are on the router now and puts back ${d.removed} from the copy, in ${d.sections.length} section${d.sections.length === 1 ? '' : 's'}: ${d.sections.slice(0, 8).map(x => x.section).join(', ')}${d.sections.length > 8 ? ', …' : ''}. Press Check only to see what the router itself would change.` : 'This copy is the same as the router now; there is nothing to restore.';
  }).catch(e => { $('#rsSum').textContent = e.message; });
  let busy = false;
  m.querySelector('.actions').onclick = async e => {
    const a = e.target.dataset.a; if (!a) return;
    if (a === 'cancel') return closeModal();
    if (busy) return; err.textContent = '';
    if (a === 'apply' && m.querySelector('[name=_confirm]').value.trim() !== id) { err.textContent = `Type ${id} in the box to confirm.`; return; }
    busy = true; m.querySelectorAll('.actions [data-a=check],.actions [data-a=apply]').forEach(b => b.disabled = true);
    $('#rsSum').textContent = a === 'check' ? 'The router is loading the copy (nothing is committed)…' : 'Keeping a copy of the router as it is now, then restoring…';
    try {
      const r = await post(`/api/backups/${id}/restore`, a === 'check' ? { commit: false } : { commit: true, confirm: id });
      if (a === 'check') {
        $('#rsSum').textContent = 'Check only: the router loaded the copy and threw it away. This is what it would change (+ added, - removed):';
        paint(r.output || '(no changes listed)'); $('#rsF').classList.remove('hidden'); m.querySelector('[name=_confirm]').focus();
      } else { closeModal(); toast(`Restored copy ${id}. Press Save to keep it over a reboot.`); done({ head: 'Restored successfully', title: `Copy ${id} (press Save to keep it over a reboot)`, commit_id: r.commit_id, time: r.time }); showOutput('Restore result: ' + id, r.output); if (current === 'backup') refresh(); return; }
    } catch (e2) { err.textContent = '✗ ' + e2.message; $('#rsSum').textContent = ''; if (a === 'apply') done({ fail: true, title: 'Restore ' + id, text: e2.message }); }
    busy = false; m.querySelector('[data-a=check]').disabled = false; m.querySelector('[data-a=apply]').disabled = $('#rsF').classList.contains('hidden');
  };
}
async function bkPin(id, pinned) { try { await post('/api/backups/' + id, { pinned }); refresh(); } catch (e) { toast(e.message, true); } }
async function bkNoteEdit(id, note) { const v = prompt('Note for this copy', note); if (v == null) return; try { await post('/api/backups/' + id, { note: v }); refresh(); } catch (e) { toast(e.message, true); } }
async function bkDelete(id) { if (!confirm('Delete this copy from the server?')) return; try { await post('/api/backups/' + id, { delete: true }); refresh(); } catch (e) { toast(e.message, true); } }
async function bkDiff(id) {
  const box = $('#bkD'); box.classList.remove('hidden'); $('#bkDt').textContent = 'Comparing…'; $('#bkDs').innerHTML = ''; $('#bkDu').textContent = ''; box.scrollIntoView({ behavior: 'smooth' });
  let d; try { d = await api(`/api/backups/${id}/diff?against=running`); } catch (e) { $('#bkDt').textContent = e.message; return; }
  $('#bkDt').textContent = d.added || d.removed ? `Copy ${id} compared with the router now: ${d.removed} line${d.removed === 1 ? '' : 's'} only in the copy, ${d.added} only on the router` : `Copy ${id} is the same as the router now`;
  $('#bkDs').innerHTML = d.sections.length ? `<table class="tbl" style="margin-bottom:10px"><thead><tr><th>Section</th><th>Change</th><th>Only on the router</th><th>Only in the copy</th></tr></thead><tbody>${d.sections.map(x =>
    `<tr><td class="mono">${esc(x.section)}</td><td>${{ added: 'new on the router', removed: 'gone from the router', changed: 'changed' }[x.change]}</td><td class="num">${x.added}</td><td class="num">${x.removed}</td></tr>`).join('')}</tbody></table>` : '';
  $('#bkDu').innerHTML = d.unified.split('\n').map(l => `<span class="${l.startsWith('+') && !l.startsWith('+++') ? 'dadd' : l.startsWith('-') && !l.startsWith('---') ? 'ddel' : l.startsWith('@@') ? 'dhunk' : ''}">${esc(l)}</span>`).join('\n');
}
async function saveCfg() { if (!confirm('Save the running configuration to startup?')) return; try { const r = await post('/api/save'); toast(r.output.split('\n').filter(Boolean).pop() || 'Saved');
    done(r.saved === false ? { fail: true, head: 'Save may not have finished', title: 'Save configuration', text: r.output } : { head: 'Saved successfully', title: 'Running configuration saved to startup', time: r.time }); if (current === 'backup') setTimeout(refresh, 2500); } catch (e) { toast(e.message, true); } }
$('#saveBtn').onclick = saveCfg;

/* ---------- NOC: the live network map full screen, for a wall screen ---------- */
let NOC_THEME = null;
function nocLeave() {
  if (NOC_THEME === null) return;
  if (NOC_THEME) document.documentElement.dataset.theme = NOC_THEME; else delete document.documentElement.dataset.theme;
  NOC_THEME = null; NM.noc = null; NM.show = null; NM.focus = null; NM.geo = false; NM.sites = false; NM.userZoom = false; if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
}
$('#nocBtn').onclick = () => { if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {}); location.hash = 'noc'; };
PAGES.noc = async c => {
  if (typeof srLoad === 'function' && typeof SR === 'object' && !SR.d) srLoad().catch(() => {});   // registered sites name the regions
  if (NOC_THEME === null) NOC_THEME = document.documentElement.dataset.theme || '';
  document.documentElement.dataset.theme = 'dark';
  const view = decodeURIComponent((location.hash.split('/')[1] || ''));   // #noc/<view>: a wall screen can stay on one view
  c.innerHTML = `<div class="noc"><div class="nh"><span class="ttl">NOC · ${esc($('#tName').textContent)}</span>
      <span class="vsel"><select id="ncV" title="show the whole network, one port group, one region or one device"><option value="">Whole network</option></select></span>
      <span class="seg" id="ncM"><button data-m="d" title="the network as a diagram">Diagram</button><button data-m="g" title="the network on the map of Kenya">Kenya map</button><button data-m="s" title="how Google, YouTube, Facebook and the other big sites are reached">Major sites</button></span>
      <button class="btn sm" id="ncLine" title="draw a line the router cannot see by itself: pick the interface and where it goes">+ Line</button>
      <span class="seg"><button id="ncZo" title="zoom out (or the mouse wheel)">−</button><button id="ncZi" title="zoom in (or the mouse wheel)">+</button><button id="ncZf" title="fit everything on the screen">Fit</button></span>
      <span class="muted" id="ncInfo" style="font-size:12.5px">Reading the network…</span>
      <span class="clk" id="ncClk"></span><button class="btn sm sshb" id="ncSsh"></button><button class="btn sm" id="ncFs" title="full screen">⛶ Full screen</button><button class="btn sm" id="ncX" title="leave the NOC view (Esc)">✕ Exit</button></div>
    <div class="tiles"><div class="tile"><div class="l">Traffic in ▼ (upstreams)</div><div class="v" id="ncIn">-</div></div><div class="tile"><div class="l">Traffic out ▲ (upstreams)</div><div class="v" id="ncOut">-</div></div>
      <div class="tile"><div class="l">BGP sessions up</div><div class="v" id="ncB">-</div></div><div class="tile"><div class="l">Links down on the map</div><div class="v" id="ncD">-</div></div>
      <div class="tile"><div class="l">Interfaces down</div><div class="v" id="ncI">-</div></div><div class="tile"><div class="l">CPU · memory</div><div class="v" id="ncC">-</div></div>
      <div class="tile"><div class="l">Changes, last hour</div><div class="v" id="ncE">-</div></div>
      <div class="tile wt" id="ncWt" title="click for the list"><div class="l">Warnings ⚠</div><div class="v" id="ncW">-</div></div></div>
    <div class="body"><div class="mapw"><div class="canvas nmap" id="nmC"></div></div><div class="side" id="ncS"><h3>Click a line or a device</h3><p class="muted">One click follows it to the router and dims the rest. Double-click shows how it is built. Click empty space to clear.</p></div></div>
    <div class="tick"><div class="run" id="ncT">Loading the latest events…</div></div></div>`;
  $('#ncX').onclick = () => { nocLeave(); location.hash = 'netmap'; };
  $('#ncFs').onclick = () => { if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {}); };
  const sshBtn = () => { const b = $('#ncSsh'); if (!b) return; const on = /SSH/.test(VIA || $('#tHost').textContent);
    b.textContent = on ? 'SSH ✓' : 'Connect SSH'; b.classList.toggle('on', on); b.title = on ? 'signed in to the router over SSH' : 'traffic comes over SNMP; SSH adds configuration details (QoS, interface config)'; };
  sshBtn(); $('#ncSsh').onclick = async () => { if (/SSH/.test(VIA || $('#tHost').textContent)) return; if (await sshLogin('Sign in to the router over SSH to see configuration details (QoS, interface configuration).')) { sshBtn(); map(true); } };
  const clock = () => { const el = $('#ncClk'); if (el) el.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); };
  clock(); timers.push(setInterval(clock, 1000));
  NM.sel = null; NM.focus = null; NM.show = null; NM.lastClick = null; NM.userZoom = false; GEO.sel = null;
  let mode = 'd'; try { mode = { geo: 'g', sites: 's' }[localStorage.getItem('ne8000.nocmode')] || 'd'; } catch (e) { }
  NM.geo = mode === 'g'; NM.sites = mode === 's'; SW.sel = null;
  const modeBtns = () => $('#ncM').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.m === (NM.sites ? 's' : NM.geo ? 'g' : 'd')));
  modeBtns();
  $('#ncM').onclick = e => { const m = e.target.dataset.m; if (!m) return; NM.geo = m === 'g'; NM.sites = m === 's'; try { localStorage.setItem('ne8000.nocmode', { g: 'geo', s: 'sites', d: 'diagram' }[m]); } catch (x) { }
    modeBtns(); NM.focus = null; NM.sel = null; GEO.sel = null; SW.sel = null; side(null); if (m === 'd') { NM.userZoom = false; fit(); } if (NM.sites) { swSide(); if (!SW.data) swLoad(); } nmDraw(); };
  const zoomBy = (f, e) => {
    if (NM.sites) return swZoom(f, e);
    if (NM.geo) return geoZoom(f, e);
    const host = $('#nmC'), z0 = NM.zoom, z1 = Math.max(0.2, Math.min(3, z0 * f)); if (!host || z1 === z0) return;
    const r = host.getBoundingClientRect(), ox = e ? e.clientX - r.left : host.clientWidth / 2, oy = e ? e.clientY - r.top : host.clientHeight / 2;
    const sx = (host.scrollLeft + ox) / z0, sy = (host.scrollTop + oy) / z0;
    NM.zoom = z1; NM.userZoom = true; nmDraw(); host.scrollLeft = sx * z1 - ox; host.scrollTop = sy * z1 - oy; };
  $('#ncZi').onclick = () => zoomBy(1.25); $('#ncZo').onclick = () => zoomBy(1 / 1.25);
  $('#ncZf').onclick = () => { if (NM.sites) return swFit(); if (NM.geo) return geoFit(); NM.userZoom = false; fit(); nmDraw(); };
  let T = null, picked = null;
  // views: port groups (the devices right under the router), regions (a name many devices start with), single devices
  const views = () => {
    const d = NM.data.diagram, t = T, out = [];
    (t.kids[t.root] || []).map(id => d.nodes.find(n => n.id === id)).filter(n => n && n.type !== 'upstream').forEach(n => out.push(['n:' + n.id, 'Port group: ' + n.label]));
    if (d.nodes.some(n => n.type === 'upstream')) out.push(['up', 'Upstreams and peers']);
    // a region is a real place: a Kenyan town, or a registered site's town or name (not just any word devices start with)
    const known = new Set([...Object.keys(typeof KE_TOWNS === 'object' ? KE_TOWNS : {}), ...((typeof SR === 'object' && SR.d ? SR.d.sites : []).flatMap(x => [x.town, x.name, x.code].filter(Boolean).map(v => String(v).toUpperCase().split(/[\s-]/)[0])))]);
    const cnt = {}; d.nodes.forEach(n => { const w = (n.label.match(/^[A-Za-z]{3,}/) || [''])[0].toUpperCase(); if (w && known.has(w)) cnt[w] = (cnt[w] || 0) + 1; });
    Object.entries(cnt).filter(([, n]) => n >= 2).sort().forEach(([w, n]) => out.push(['r:' + w, `Region: ${w} (${n})`]));
    d.nodes.filter(n => n.id !== t.root).slice().sort((x, y) => x.label.localeCompare(y.label)).forEach(n => out.push(['d:' + n.id, 'Device: ' + n.label]));
    return out;
  };
  const applyView = v => {
    const d = NM.data.diagram, t = T; NM.show = null;
    if (v.startsWith('n:') || v.startsWith('d:')) { const id = v.slice(2); if (d.nodes.some(n => n.id === id)) NM.show = new Set([...t.up(id), ...t.down(id)]); }
    else if (v === 'up') NM.show = new Set(d.nodes.filter(n => n.type === 'upstream').flatMap(n => t.up(n.id)));
    else if (v.startsWith('r:')) { const w = v.slice(2); NM.show = new Set(d.nodes.filter(n => n.label.toUpperCase().startsWith(w)).flatMap(n => [...t.up(n.id), ...t.down(n.id)])); }
    if (NM.show) d.nodes.forEach(n => { if (!NM.show.has(n.id)) NM.show.delete(n.id); });
  };
  const fit = () => { const host = $('#nmC'); if (!host || !NM.data) return; const pos = nmLoadPos();
    const ns = NM.data.diagram.nodes.filter(n => !NM.show || NM.show.has(n.id)).map(n => pos[n.id] || [n.x, n.y]); if (!ns.length) return;
    const x0 = NM.show ? Math.max(0, Math.min(...ns.map(p => p[0])) - 40) : 0, y0 = NM.show ? Math.max(0, Math.min(...ns.map(p => p[1])) - 40) : 0;
    const W = Math.max(NM.show ? 300 : 1000, ...ns.map(p => p[0] - x0 + NW + 60)), H = Math.max(NM.show ? 200 : 560, ...ns.map(p => p[1] - y0 + NH + 60));
    NM.zoom = Math.max(0.3, Math.min(2, (host.clientWidth - 8) / W, (host.clientHeight - 8) / H)); };
  const rate = v => v == null ? '-' : Fmt.bps(v);
  const kv = rows => `<table><tbody>${rows.filter(([, v]) => v !== '' && v != null && v !== '-').map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v}</td></tr>`).join('')}</tbody></table>`;
  // one click: follow it to the router, dim the rest, show its numbers
  const side = async (x, kind) => {
    const el = $('#ncS'); if (!el) return;
    if (!x) { el.innerHTML = `<h3>Click a line or a device</h3><p class="muted">One click follows it to the router and dims the rest. Double-click shows how it is built. Click empty space to clear.</p>
      <p class="muted">A <span class="wdot warn">⚠</span> on a line, a device or a town is a warning: high or changed latency, a longer path, a port filling up, flapping, errors, falling light, a fibre event from the OTDR. Click it to see what and since when.</p>
      <p class="muted">Click a line, then <b>Edit</b> to correct its details, or <b>Delete</b> / <b>Hide</b> to take it off the map.</p><div id="ncHid"></div>`;
      nocHidden(); return; }
    const wh = nmWarnHtml((NM.warn || {})[x.id], 'map:' + x.id);
    if (kind === 'link') {
      const p = x.props, s = nmLinkState(x), t = NM.ifs[p.iface] || {};
      el.innerHTML = `<h3>${esc(p.label || p.iface || 'Line')}</h3><div class="row" style="gap:6px;margin:-2px 0 8px">${p.manual
          ? '<span class="faint" style="font-size:12px">Drawn by hand</span><span class="sp"></span><button class="btn sm" id="ncLE">✎ Edit</button><button class="btn sm danger" id="ncLD">Delete</button>'
          : `<span class="faint" style="font-size:12px">${p.edited ? 'Found by the app, corrected on the map' : 'Found by the app'}</span><span class="sp"></span><button class="btn sm" id="ncLE">✎ Edit</button><button class="btn sm danger" id="ncLD">Hide</button>`}</div>${wh}<div class="row" style="gap:18px;margin-bottom:8px"><div><div class="muted">Download ▼</div><div class="big">${rate(s.down || null)}</div></div><div><div class="muted">Upload ▲</div><div class="big">${rate(s.up || null)}</div></div></div>
        <div id="ncG" style="height:120px"></div>` + kv([['Interface', esc(p.iface || '')], ['State', esc(t.phy || p.state || '')], ['VLAN', esc(p.vlan || '')], ['Address', esc(p.address || '')],
          ['Note', esc(p.note || '')], ['Load', s.pct != null && p.iface ? Math.round(s.pct) + '%' : ''], ['QoS plan', p.plan_kbps ? Fmt.bps(p.plan_kbps * 1000) : ''], ['BGP', p.peers ? esc(p.peers.split(',').map(ip => `${ip} ${NM.peers[ip] || ''}`).join(', ')) : ''],
          ['Far end', p.manual && p.far_iface ? (() => { const f = (NM.far || {})[x.id]; return esc(`${p.far_name || ''} ${p.far_iface}`) + (f ? ` · ▼ ${Fmt.bps(f.rx)} ▲ ${Fmt.bps(f.tx)} <span class="faint">(${ago(f.ts)})</span>` : ' · <span class="faint">no data yet</span>'); })() : ''],
          ['Path', esc(T.up(x.to in T.parent && T.parent[x.to] === x.from ? x.to : x.from).filter(id => NM.data.diagram.nodes.some(n => n.id === id)).map(id => NM.data.diagram.nodes.find(n => n.id === id).label).reverse().join(' → '))]]) +
        nmRedHtml(p.iface) +
        `<p class="muted" style="font-size:12px;margin-top:8px">Double-click the line for how it is built.</p>` + (() => { const n = NM.data.diagram.nodes.find(y => y.id === (T.parent[x.to] === x.from ? x.to : x.from)); return n ? geoPlaceHtml(n) : ''; })();
      geoPlaceBind();
      if (p.manual) { $('#ncLE').onclick = () => nocLine(x.id.slice(3), () => map());
        $('#ncLD').onclick = async () => { if (!confirm('Delete this line from the map? Nothing changes on the router.')) return; await api('/api/netmap/links/' + x.id.slice(3), { method: 'DELETE' }); toast('Line deleted'); side(null); map(); }; }
      else { $('#ncLE').onclick = () => nocLineFix(x, () => map().then(() => { const l = NM.data.diagram.links.find(y => y.id === x.id); if (l) side(l, 'link'); }));
        $('#ncLD').onclick = async () => { if (!confirm('Hide this line (and what is only reached through it) from the map? Nothing changes on the router; you can bring it back.')) return;
          const far = NM.data.diagram.nodes.find(n => n.id === (T.parent[x.to] === x.from ? x.to : x.from));
          await post('/api/netmap/edits', { id: x.id, hidden: true, was: `${p.label || p.iface || ''}${far ? ' → ' + far.label : ''}` }); toast('Line hidden. Bring it back from the side panel.'); side(null); map(); }; }
      if (p.iface) { try { const r = await api('/api/traffic?names=' + encodeURIComponent(p.iface)), pts = r[p.iface] || [], dn = p.down === 'rx' ? 1 : 2;
        if ($('#ncG')) new TimeChart($('#ncG'), { fmt: Fmt.bps, small: true }).set([{ name: 'Download', color: '--s1', values: pts.map(v => [v[0], v[dn]]) }, { name: 'Upload', color: '--s2', values: pts.map(v => [v[0], v[3 - dn]]) }]); } catch (e) { } }
    } else {
      const kids = (T.kids[x.id] || []).length;
      el.innerHTML = `<h3>${esc(x.label)}</h3>${wh}` + kv([['Type', esc((NODE_META[x.type] || {}).label || x.type)], ...Object.entries(x.props).filter(([k]) => !['existing'].includes(k)).map(([k, v]) => [k.replace(/_/g, ' '), esc(v)]), ['Devices under it', kids || '']]) +
        `<p class="muted" style="font-size:12px;margin-top:8px">Double-click the device for how it is built.</p>` + geoPlaceHtml(x);
      geoPlaceBind();
    }
  };
  // every warning on the map, worst first; a click picks the line or the device
  const warnList = () => {
    const el = $('#ncS'), d = NM.data.diagram, W = NM.warn || {}; if (!el) return;
    const items = Object.entries(W).map(([id, ws]) => [d.links.find(l => l.id === id) || d.nodes.find(n => n.id === id), ws, d.links.some(l => l.id === id) ? 'link' : 'node']).filter(([x]) => x)
      .sort((a, b) => (!nmWorst(a[1]) - !nmWorst(b[1])) || (nmWorst(a[1]) !== 'crit') - (nmWorst(b[1]) !== 'crit'));
    el.innerHTML = `<h3>Warnings (${items.filter(([, ws]) => nmWorst(ws)).length}${items.some(([, ws]) => !nmWorst(ws)) ? ` <span class="faint" style="font-weight:400">+ ${items.filter(([, ws]) => !nmWorst(ws)).length} muted</span>` : ''})</h3>` + (items.length ? items.map(([x, ws, k], i) => `<div class="wpick" data-i="${i}"><b>${esc(k === 'link' ? x.props.label || x.props.iface || 'Line' : x.label)}</b>${nmWarnHtml(ws)}</div>`).join('') : '<p class="muted">Nothing wrong right now.</p>');
    el.querySelectorAll('.wpick').forEach(w => w.onclick = () => { const [x, , k] = items[+w.dataset.i]; NM.sel = x.id; NM.noc.pick(x, k); nmDraw(); });
  };
  $('#ncWt').onclick = warnList;
  NM.noc = {
    pick: (x, kind) => { picked = x ? [x, kind] : null; if (!x && NM.geo) GEO.sel = null;
      if (!x) NM.focus = null;
      else if (kind === 'link') { const below = T.parent[x.to] === x.from ? x.to : x.from; NM.focus = new Set([...T.up(below), ...T.down(below), x.id]); }
      else NM.focus = new Set([...T.up(x.id), ...T.down(x.id)]);
      side(x, kind); },
    dbl: (x, kind) => nocDesign(x, kind, T),
    zoom: zoomBy,
  };
  const map = async refresh => {
    try { NM.data = await api('/api/netmap' + (refresh ? '?refresh=1' : '')); T = nmTree();
      const sel = $('#ncV'); if (!sel) return; const cur = sel.value || view;
      sel.innerHTML = '<option value="">Whole network</option>' + views().map(([v, l]) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(l)}</option>`).join('');
      applyView(sel.value); $('#ncInfo').textContent = NM.data.summary.split(' Not available')[0]; $('#ncInfo').title = (NM.data.warnings || []).join('\n'); if (!NM.userZoom) fit();
    } catch (e) { if ($('#ncInfo')) $('#ncInfo').textContent = e.message; } };
  const traffic = async () => { await nmTraffic(); if (!$('#ncD')) return;
    const down = NM.data.diagram.links.filter(l => /nm-down|nm-bgpdown/.test(nmLinkState(l).cls)).length; $('#ncD').textContent = down; $('#ncD').className = 'v ' + (down ? 'bad' : 'okc');
    const ups = new Set(NM.data.diagram.nodes.filter(n => n.type === 'upstream').map(n => n.id)), seen = new Set(); let tin = 0, tout = 0, any = false;
    NM.data.diagram.links.filter(l => (ups.has(l.from) || ups.has(l.to)) && l.props.iface).forEach(l => { if (seen.has(l.props.iface)) return; seen.add(l.props.iface); const s = nmLinkState(l); if (s.down || s.up) any = true; tin += s.down || 0; tout += s.up || 0; });
    $('#ncIn').textContent = any ? Fmt.bps(tin) : '-'; $('#ncOut').textContent = any ? Fmt.bps(tout) : '-';
    if (picked && picked[1] === 'link') { const l = NM.data.diagram.links.find(x => x.id === picked[0].id); if (l) side(l, 'link'); }
    const all = Object.values(NM.warn || {}).filter(ws => nmWorst(ws)), crit = all.filter(ws => nmWorst(ws) === 'crit').length;
    $('#ncW').innerHTML = all.length ? `${all.length}${crit ? ` <span style="font-size:13px;font-weight:500">${crit} critical</span>` : ''}` : '0'; $('#ncW').className = 'v ' + (crit ? 'bad' : all.length ? 'warnc' : 'okc');
    if (NM.geo && GEO.sel && !picked) geoSide(); };
  const tiles = async () => {
    const [b, o, i, ev] = await Promise.all([api('/api/view/bgp').catch(() => null), api('/api/view/overview').catch(() => null), api('/api/view/interfaces').catch(() => null), api('/api/changes?days=1&limit=40').catch(() => null)]);
    if (!$('#ncB')) return; sshBtn();
    if (b) NM.bgpView = b;
    if (b) { const ign = new Set(Object.values(b.config || {}).flat().filter(l => / ignore$/.test(l)).map(l => l.split(' ')[1]));
      const live = b.peers.filter(p => !ign.has(p.peer) && !/admin/i.test(p.state)), up = live.filter(p => p.state === 'Established').length, off = b.peers.length - live.length;
      $('#ncB').innerHTML = `${up} / ${live.length}${off ? ` <span class="muted" style="font-size:13px;font-weight:500">+${off} disabled</span>` : ''}`; $('#ncB').className = 'v ' + (up < live.length ? 'bad' : 'okc'); }
    if (o) { const cpu = o.cpu, mem = o.memory && typeof o.memory === 'object' ? o.memory.percent : o.memory; $('#ncC').textContent = `${cpu != null ? cpu + '%' : '-'} · ${mem != null ? mem + '%' : '-'}`; }
    if (i) { const dn = i.filter(x => !x.admin_down && x.description && x.phy && x.phy !== 'up' && !String(x.phy).startsWith('*')).length; $('#ncI').textContent = dn; $('#ncI').className = 'v ' + (dn ? 'bad' : 'okc'); }
    if (ev) { const hr = ev.events.filter(e => e.ts > Date.now() / 1000 - 3600).length; $('#ncE').textContent = hr;
      $('#ncT').innerHTML = ev.events.length ? ev.events.slice(0, 25).map(e => `<span class="${['down', 'removed', 'dropped'].includes(e.change) ? 'bad' : ['up', 'added', 'back'].includes(e.change) ? 'okc' : ''}">${esc(new Date(e.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))} · ${esc((CHG_LABEL[e.kind] || e.kind) + ' ' + e.key + ' ' + e.change)}${e.info ? ' · ' + esc(e.info) : ''}</span>`).join('') : '<span>No changes in the last 24 hours.</span>'; }
  };
  $('#ncV').onchange = () => { const v = $('#ncV').value; history.replaceState(null, '', '#noc' + (v ? '/' + encodeURIComponent(v) : '')); NM.focus = null; NM.sel = null; GEO.sel = null; side(null); applyView(v); NM.userZoom = false; fit(); nmDraw(); };
  await map(); await traffic(); tiles(); nmHealth(); swLoad().then(() => { if (NM.sites && !picked) swSide(); });
  every(5000, traffic); every(10000, tiles); every(60000, () => map()); every(30000, nmHealth); every(60000, () => swLoad().then(() => { if (NM.sites && $('#ncS') && !SW.sel) swSide(); }));
  $('#ncLine').onclick = () => nocLine(null, () => map());
  let rt = null; const ro = new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => { if ($('#nmC')) { if (!NM.userZoom) fit(); nmDraw(); } else ro.disconnect(); }, 150); }); ro.observe($('#nmC'));
};
/* Draw a line by hand (or change one): the router interface it uses and where it goes. When the far end is a router
   the app monitors over SNMP, its interface too, so the line shows traffic from both ends. Only the app's map changes. */
async function nocLine(id, after) {
  const d = NM.data.diagram, m = $('#modal');
  const [ifv, rem, cur] = await Promise.all([api('/api/view/interfaces').catch(() => []), api('/api/netmap/remote').catch(() => ({ routers: [] })), api('/api/netmap/links').catch(() => ({ links: [] }))]);
  const row = id ? (cur.links || []).find(x => x.id === id) || {} : {};
  const root = (d.nodes.find(n => n.type === 'ne8000') || d.nodes[0] || {}).id, ifs = (Array.isArray(ifv) ? ifv : ifv.interfaces || []).map(i => i.name);
  const opt = (v, l, sel) => `<option value="${esc(v)}" ${v === sel ? 'selected' : ''}>${esc(l)}</option>`;
  const nodes = d.nodes.filter(n => !n.id.startsWith('m:')).slice().sort((a, b) => a.label.localeCompare(b.label));
  m.innerHTML = `<h2>${id ? 'Change the line' : 'Draw a line'}</h2><p class="muted">For a link the router cannot see by itself (no LLDP, no customer VLAN). Only the app's map changes, never the router.</p>
    <div class="form"><label>From</label><select id="lnF">${nodes.map(n => opt(n.id, n.label, row.from || root)).join('')}</select>
      <label>Router interface</label><input id="lnI" list="lnIL" value="${esc(row.iface || '')}" placeholder="GigabitEthernet0/1/6.310"><datalist id="lnIL">${ifs.map(n => `<option value="${esc(n)}">`).join('')}</datalist>
      <label>To</label><select id="lnT">${opt('', 'A new device…', row.to || '')}${nodes.map(n => opt(n.id, n.label, row.to)).join('')}</select>
      <label class="lnN">New device name</label><input class="lnN" id="lnN" value="${esc(row.to_label || '')}" placeholder="MARSABIT-BTS-01">
      <label class="lnN">Type</label><select class="lnN" id="lnY">${['router', 'switch', 'ap', 'olt', 'customer', 'reseller', 'server'].map(t => opt(t, (NODE_META[t] || {}).label || t, row.to_type || 'router')).join('')}</select>
      <label class="lnN">Site *</label><span class="lnN" id="lnSW">Loading…</span>
      <label class="lnN">Town</label><select class="lnN" id="lnW">${opt('', 'from the name', row.town || '')}${Object.keys(KE_TOWNS).sort().map(t => opt(t, t, row.town)).join('')}</select>
      <label>Far-end router (SNMP)</label><select id="lnR">${opt('', 'not monitored', row.far_router || '')}${(rem.routers || []).map(r => opt(r.id, `${r.name} (${r.host})${r.snmp ? '' : ' – no SNMP'}`, row.far_router)).join('')}</select>
      <label class="lnFI">Its interface</label><select class="lnFI" id="lnFI"><option value="">Loading…</option></select>
      <label>Label</label><input id="lnL" value="${esc(row.label || '')}" placeholder="Fibre to Marsabit (primary)"></div>
    <div class="err"></div><div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Save</button></div>`;
  $('#modalBg').classList.remove('hidden');
  const showNew = () => m.querySelectorAll('.lnN').forEach(e => e.style.display = $('#lnT').value ? 'none' : '');
  const loadFar = async () => { const rid = $('#lnR').value; m.querySelectorAll('.lnFI').forEach(e => e.style.display = rid ? '' : 'none'); if (!rid) return;
    const r = await api('/api/netmap/remote?rid=' + encodeURIComponent(rid)).catch(() => ({ interfaces: [] }));
    $('#lnFI').innerHTML = opt('', (r.interfaces || []).length ? 'pick its interface' : 'no SNMP data from that router yet', '') + (r.interfaces || []).map(i => opt(i.name, `${i.name}${i.rx != null ? ` · ▼ ${Fmt.bps(i.rx)} ▲ ${Fmt.bps(i.tx)}` : ''}`, row.far_iface)).join(''); };
  $('#lnT').onchange = showNew; $('#lnR').onchange = loadFar; showNew(); loadFar();
  srComplete().then(ss => { if ($('#lnSW')) $('#lnSW').outerHTML = srPicker(ss, row.site || '', 'lnS').replace('<select', '<select class="lnN"'); showNew(); });
  m.querySelector('.actions').onclick = async e => {
    if (e.target.dataset.a === 'c') return closeModal();
    if (e.target.dataset.a !== 's') return;
    try { await post('/api/netmap/links', { id, from: $('#lnF').value, iface: $('#lnI').value, to: $('#lnT').value, to_label: $('#lnN').value, to_type: $('#lnY').value, town: $('#lnW').value,
        far_router: $('#lnR').value, far_iface: $('#lnFI').value, label: $('#lnL').value, site: $('#lnS') ? $('#lnS').value : '' });
      closeModal(); toast('Line saved on the map'); after && after(); } catch (err) { m.querySelector('.err').textContent = err.message; }
  };
}
/* Correct a line the app found by itself: its label, a note, and the far device's name, type and town. Only the app's map
   changes, never the router; "Undo corrections" goes back to what the app found. */
async function nocLineFix(x, after) {
  const d = NM.data.diagram, m = $('#modal'), p = x.props;
  const root = (d.nodes.find(y => y.type === 'ne8000') || {}).id, far = d.nodes.find(n => n.id === (x.to === root ? x.from : x.to)) || {};
  const ed = ((await api('/api/netmap/edits').catch(() => ({ edits: {} }))).edits || {})[x.id] || {};
  const opt = (v, l, sel) => `<option value="${esc(v)}" ${v === sel ? 'selected' : ''}>${esc(l)}</option>`;
  m.innerHTML = `<h2>Correct this line</h2><p class="muted">Fix details the app got wrong. Only the app's map changes, never the router.</p>
    <div class="form"><label>Interface</label><input value="${esc(p.iface || '-')}" disabled>
      <label>Label</label><input id="lfL" value="${esc(ed.label || p.label || '')}" placeholder="Fibre to Marsabit (primary)">
      <label>Far device name</label><input id="lfN" value="${esc(ed.to_label || far.label || '')}">
      <label>Type</label><select id="lfY">${['router', 'switch', 'ap', 'olt', 'customer', 'reseller', 'server', 'upstream'].map(t => opt(t, (NODE_META[t] || {}).label || t, ed.to_type || far.type)).join('')}</select>
      <label>Town</label><select id="lfW">${opt('', 'from the name', ed.town || '')}${Object.keys(KE_TOWNS).sort().map(t => opt(t, t, ed.town || (far.props || {}).location)).join('')}</select>
      <label>Note</label><textarea id="lfO" rows="3" placeholder="e.g. leased from Safaricom, circuit ID 12345">${esc(ed.note || p.note || '')}</textarea></div>
    <div class="err"></div><div class="actions">${p.edited ? '<button class="btn" data-a="u">Undo corrections</button><span class="sp"></span>' : ''}<button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Save</button></div>`;
  $('#modalBg').classList.remove('hidden');
  m.querySelector('.actions').onclick = async e => {
    const a = e.target.dataset.a; if (!a) return;
    if (a === 'c') return closeModal();
    try {
      if (a === 'u') { await api('/api/netmap/edits/' + encodeURIComponent(x.id), { method: 'DELETE' }); closeModal(); toast('Back to what the app found'); return after && after(); }
      const body = { id: x.id, note: $('#lfO').value, was: `${p.label || p.iface || ''} → ${far.label || ''}` };
      if ($('#lfL').value.trim() !== (p.label || '')) body.label = $('#lfL').value;
      if ($('#lfN').value.trim() !== (far.label || '')) body.to_label = $('#lfN').value;
      if ($('#lfY').value !== far.type) body.to_type = $('#lfY').value;
      if ($('#lfW').value) body.town = $('#lfW').value;
      ['label', 'to_label', 'to_type', 'town'].forEach(k => { if (body[k] == null && ed[k]) body[k] = ed[k]; });     // keep earlier corrections
      await post('/api/netmap/edits', body); closeModal(); toast('Line corrected on the map'); after && after();
    } catch (err) { m.querySelector('.err').textContent = err.message; }
  };
}
// the side panel's list of hidden lines, to bring one back
async function nocHidden() {
  const r = await api('/api/netmap/edits').catch(() => null), el = $('#ncHid'); if (!r || !el) return;
  const hid = Object.entries(r.edits || {}).filter(([, e]) => e.hidden);
  if (!hid.length) return;
  el.innerHTML = `<h3 style="margin-top:10px">Hidden lines (${hid.length})</h3><table><tbody>${hid.map(([id, e]) => `<tr><td>${esc(e.was || id)}<div class="faint" style="font-size:11.5px">by ${esc(e.by || '?')} ${esc(ago(e.at))}</div></td>
    <td style="text-align:right"><button class="btn sm" data-r="${esc(id)}">Bring back</button></td></tr>`).join('')}</tbody></table>`;
  el.querySelectorAll('[data-r]').forEach(b => b.onclick = async () => { await api('/api/netmap/edits/' + encodeURIComponent(b.dataset.r), { method: 'DELETE' }); toast('Line back on the map'); location.hash === '#noc' && $('#ncM [data-m=d]') ? $('#ncM [data-m=d]').click() : 0; });
}
/* Double-click on the NOC map: how this line or device is built, from what the app knows (and the router's configuration with SSH). */
async function nocDesign(x, kind, T) {
  const m = $('#modal'); m.classList.add('wide');
  const d = NM.data.diagram, line = kind === 'link' ? x : d.links.find(l => l.id === T.via[x.id]), node = kind === 'node' ? x : d.nodes.find(n => n.id === (T.parent[x.to] === x.from ? x.to : x.from));
  const p = (line || {}).props || {}, iface = p.iface, kids = (T.kids[node.id] || []).map(id => [d.nodes.find(n => n.id === id), d.links.find(l => l.id === T.via[id])]).filter(([n]) => n);
  const kv = rows => `<table class="tbl"><tbody>${rows.filter(([, v]) => v !== '' && v != null).map(([k, v]) => `<tr><td class="faint" style="white-space:nowrap">${esc(k)}</td><td>${v}</td></tr>`).join('')}</tbody></table>`;
  m.innerHTML = `<h2>How it is built: ${esc(node.label)}</h2><div class="grid g2"><div><h3>Device</h3>${kv([['Type', esc((NODE_META[node.type] || {}).label || node.type)], ...Object.entries(node.props).filter(([k]) => k !== 'existing').map(([k, v]) => [k.replace(/_/g, ' '), esc(v)])])}
      ${line ? `<h3 style="margin-top:12px">Line to it</h3>${kv([['Label', esc(p.label || '')], ['Interface', esc(iface || '')], ['VLAN', esc(p.vlan || '')], ['Address', esc(p.address || '')], ['QoS plan', p.plan_kbps ? Fmt.bps(p.plan_kbps * 1000) : ''], ['BGP peers', esc(p.peers || '')]])}` : ''}
      ${kids.length ? `<h3 style="margin-top:12px">Under it (${kids.length})</h3>${kv(kids.slice(0, 40).map(([n, l]) => [n.label, esc([(l || {}).props && l.props.label, (l || {}).props && l.props.address].filter(Boolean).join(' · '))]))}` : ''}</div>
    <div><h3>On the router</h3><div id="ndI" class="muted">${iface ? 'Reading ' + esc(iface) + '…' : 'No router interface on this line.'}</div><div id="ndA" style="margin-top:10px"></div></div></div>
    <div class="actions"><button class="btn primary" data-a="x">Close</button></div>`;
  $('#modalBg').classList.remove('hidden'); m.querySelector('[data-a=x]').onclick = closeModal;
  const peers = (p.peers || '').split(',').filter(Boolean);
  const b = peers.length ? await api('/api/view/bgp').catch(() => null) : null;
  if (b && $('#ndA')) { const pol = {}; Object.values(b.config || {}).flat().forEach(l => { const mm = l.match(/^peer (\S+) route-policy (\S+) (import|export)$/); if (mm) (pol[mm[1]] = pol[mm[1]] || {})[mm[3]] = mm[2]; });
    $('#ndA').innerHTML = '<h3>BGP</h3>' + kv(peers.map(ip => { const q = (b.peers || []).find(x => x.peer === ip) || {}; return [ip, esc(`${q.state || ''} · AS ${q.as || '-'} · ${Fmt.num(q.prefixes || 0)} routes received · policy in ${(pol[ip] || {}).import || '-'} / out ${(pol[ip] || {}).export || '-'}`)]; })); }
  if (!iface) return;
  try {
    const r = await fetch('/api/interface?name=' + encodeURIComponent(iface)).then(x => x.json());
    if (!$('#ndI')) return;
    if (r.error) { $('#ndI').textContent = r.error; return; }
    const arp = await api('/api/view/arp').then(a => (a.entries || []).filter(e => e.interface === iface)).catch(() => []);
    $('#ndI').innerHTML = kv([['State', esc(`${r.state || ''} / ${r.protocol || ''}`)], ['Description', esc(r.description || '')], ['IP', esc(r.ip || '')], ['Speed', esc(r.speed || '')], ['MAC', esc(r.mac || '')],
        ['In / out now', r.in_rate_bps != null ? `${Fmt.bps(r.in_rate_bps)} / ${Fmt.bps(r.out_rate_bps)}` : ''], ['CRC errors', r.crc_errors != null ? Fmt.num(r.crc_errors) : ''], ['Optic', r.optics ? esc(`rx ${r.optics.rx_dbm} dBm · tx ${r.optics.tx_dbm} dBm`) : ''],
        ['Client MACs (ARP)', esc(arp.slice(0, 6).map(e => `${e.ip} ${e.mac}`).join(', '))]]) +
      (r.config && r.config.length ? `<div class="lbl muted" style="margin-top:8px">Configuration</div><pre style="max-height:220px">${esc(['interface ' + iface, ...r.config.map(l => ' ' + l)].join('\n'))}</pre>`
        : `<p class="muted" style="margin-top:8px">The configuration needs SSH. <button class="btn sm" onclick="sshLogin('Sign in to the router over SSH to see the interface configuration.').then(ok => ok && closeModal())">Connect SSH</button></p>`);
  } catch (e) { if ($('#ndI')) $('#ndI').textContent = e.message; }
}

/* ---------- search everything (top bar; press / or Ctrl+K) ---------- */
const GS = { res: [], on: -1, t: null, seq: 0 };
function gsPages(q) {
  const out = []; q = q.toLowerCase();
  NAV.forEach(([grp, items]) => items.forEach(([p, label]) => { if (label.toLowerCase().includes(q) || p.includes(q)) out.push({ group: 'Pages', title: label, sub: grp || '', go: { page: p } }); }));
  return out.slice(0, 6);
}
function gsShow() {
  const box = $('#gqR'); box.classList.remove('hidden');
  if (!GS.res.length) { box.innerHTML = `<div class="none">${$('#gq').value.trim().length < 2 ? 'Type at least 2 letters: a port, IP, MAC, peer, AS, VLAN, customer, policy…' : 'Nothing found.'}</div>`; return; }
  let last = '', html = '';
  GS.res.forEach((x, i) => { if (x.group !== last) { html += `<div class="g">${esc(x.group)}</div>`; last = x.group; } html += `<a data-i="${i}" class="${i === GS.on ? 'on' : ''}" role="option"><b>${esc(x.title)}</b>${x.sub ? `<span>${esc(x.sub)}</span>` : ''}</a>`; });
  box.innerHTML = html;
}
function gsHide() { $('#gqR').classList.add('hidden'); GS.on = -1; }
function gsOpen(x) {
  if (!x) return; gsHide(); $('#gq').blur(); const g = x.go || {};
  if (g.q) GOQ = { q: g.q, at: Date.now() };
  if (g.policy) POLSEL = g.policy;
  const after = g.peer ? () => { let n = 0; const t = setInterval(() => { if (++n > 30 || ($('#bP table') && current === 'bgp')) { clearInterval(t); if (n <= 30) peerHistory(g.peer, ((bgpState.peers || []).find(p => p.peer === g.peer) || {}).description || ''); } }, 300); } : null;
  if (location.hash.slice(1) === g.page) refresh(); else location.hash = g.page;
  if (after) after();
}
$('#gq').oninput = () => {
  clearTimeout(GS.t); const q = $('#gq').value.trim();
  GS.res = gsPages(q); GS.on = GS.res.length ? 0 : -1; gsShow();
  if (q.length < 2) return;
  GS.t = setTimeout(async () => { const my = ++GS.seq; try { const r = await api('/api/search?q=' + encodeURIComponent(q)); if (my !== GS.seq) return; GS.res = [...gsPages(q), ...r.results]; GS.on = GS.res.length ? 0 : -1; gsShow(); } catch (e) { } }, 180);
};
$('#gq').onfocus = () => { if ($('#gq').value.trim()) gsShow(); };
$('#gq').onkeydown = e => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (!GS.res.length) return; GS.on = (GS.on + (e.key === 'ArrowDown' ? 1 : -1) + GS.res.length) % GS.res.length; gsShow(); const a = $('#gqR a.on'); if (a) a.scrollIntoView({ block: 'nearest' }); }
  if (e.key === 'Enter') { e.preventDefault(); gsOpen(GS.res[GS.on] || GS.res[0]); }
  if (e.key === 'Escape') { gsHide(); $('#gq').blur(); }
};
$('#gqR').onmousedown = e => { const a = e.target.closest('a[data-i]'); if (a) { e.preventDefault(); gsOpen(GS.res[+a.dataset.i]); } };
document.addEventListener('mousedown', e => { if (!e.target.closest('#gs')) gsHide(); });
document.addEventListener('keydown', e => {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || document.activeElement.isContentEditable;
  if ((e.key === '/' && !typing) || (e.key.toLowerCase() === 'k' && (e.ctrlKey || e.metaKey))) { e.preventDefault(); $('#gq').focus(); $('#gq').select(); }
});

/* ---------- login ---------- */
async function connect(demo) {
  $('#loginError').textContent = ''; const b = $('#connectBtn'); b.disabled = true; b.textContent = 'Connecting…';
  const rid = !demo && ROUTER_LIST.length ? $('#routerSel').value : '';
  if (rid) try { localStorage.setItem('ne8000.router', rid); } catch (e) {}
  try { const x = await post('/api/connect', { demo, router_id: rid, host: $('#host').value, port: $('#port').value, username: $('#username').value, password: $('#password').value }); $('#password').value = ''; if (x.warning) toast(x.warning, true); await start(); }
  catch (e) { $('#loginError').textContent = e.message; } finally { b.disabled = false; b.textContent = 'Connect'; }
}
function routerPicker() {
  ROUTER_LIST = AUTH.routers || [];
  const has = ROUTER_LIST.length > 0;
  $('#pickRouter').classList.toggle('hidden', !has); $('#hostFields').classList.toggle('hidden', has);
  $('#noRoutersTip').style.display = !has && ME.role === 'admin' ? '' : 'none';
  if (!has) return;
  let last = ''; try { last = localStorage.getItem('ne8000.router') || ''; } catch (e) {}
  $('#routerSel').innerHTML = ROUTER_LIST.map(r => `<option value="${esc(r.id)}" ${r.id === last ? 'selected' : ''}>${esc(r.name)} (${esc(r.host)}:${r.port})</option>`).join('');
  const fill = () => {
    const r = ROUTER_LIST.find(x => x.id === $('#routerSel').value); if (r && r.username && !$('#username').value) $('#username').value = r.username;
    $('#snmpTip').style.display = r && r.snmp ? '' : 'none';
  };
  $('#routerSel').onchange = () => { $('#username').value = ''; fill(); }; fill();
}
$('#connectBtn').onclick = () => connect(false); $('#demoBtn').onclick = () => connect(true);
$('#password').onkeydown = e => { if (e.key === 'Enter') connect(false); };
$('#logoutBtn').onclick = async () => { await post('/api/disconnect'); location.hash = ''; location.reload(); };

/* ---------- switch between saved routers without disconnecting first ---------- */
function routerSwitcher(s) {
  const sel = $('#tSwitch'); if (!ROUTER_LIST.length) return;
  const cur = s.router_id || (ROUTER_LIST.find(r => r.host === s.host && !s.demo) || {}).id || '';
  sel.innerHTML = (cur ? '' : `<option value="">${esc(s.demo ? 'demo' : s.host)}</option>`) +
    ROUTER_LIST.map(r => `<option value="${esc(r.id)}" ${r.id === cur ? 'selected' : ''}>${esc(r.name)}</option>`).join('');
  $('#tSwitchW').style.display = '';
  sel.onchange = () => switchRouter(sel.value, cur);
}
async function switchRouter(id, back) {
  const r = ROUTER_LIST.find(x => x.id === id); if (!r) return;
  const done = () => { try { localStorage.setItem('ne8000.router', id); } catch (e) {} location.hash = location.hash || '#dashboard'; location.reload(); };
  const sel = $('#tSwitch'); sel.disabled = true; toast('Connecting to ' + r.name + '…');
  if (r.snmp) try { const x = await post('/api/connect', { router_id: id }); if (x.warning) toast(x.warning, true); return done(); }
  catch (e) {
    sel.disabled = false;
    if (!(e.data && e.data.ssh_required) && !/required|password|login/i.test(e.message)) { toast(e.message, true); sel.value = back; return; }
  }
  // no SNMP login saved for that router (or SNMP failed): ask for an SSH login
  const m = $('#modal');
  m.innerHTML = `<h2>Connect to ${esc(r.name)}</h2><p class="muted">${esc(r.host)}:${r.port} needs your SSH login.</p>
    <div class="form"><label>Router username</label><input id="swU" autocomplete="username" value="${esc(r.username || '')}">
    <label>Password</label><input id="swP" type="password" autocomplete="current-password"></div>
    <div class="err"></div><div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Connect</button></div>`;
  $('#modalBg').classList.remove('hidden'); ($('#swU').value ? $('#swP') : $('#swU')).focus();
  const go = async () => { try { const x = await post('/api/connect', { router_id: id, username: $('#swU').value, password: $('#swP').value }); if (x.warning) toast(x.warning, true); done(); } catch (e) { m.querySelector('.err').textContent = e.message; } };
  $('#swP').onkeydown = e => { if (e.key === 'Enter') go(); };
  m.querySelector('.actions').onclick = e => { if (e.target.dataset.a === 'c') { closeModal(); $('#tSwitch').value = back; } if (e.target.dataset.a === 's') go(); };
}

/* ---------- app sign-in ---------- */
function showAppLogin() {
  ['#login', '#shell'].forEach(s => $(s).classList.add('hidden')); $('#appLogin').classList.remove('hidden'); $('#auUser').focus();
}
async function appLogin() {
  $('#auError').textContent = ''; const b = $('#auBtn'); b.disabled = true;
  try { await post('/api/auth/login', { username: $('#auUser').value, password: $('#auPass').value }); $('#auPass').value = ''; location.reload(); }
  catch (e) { $('#auError').textContent = e.message; } finally { b.disabled = false; }
}
$('#auBtn').onclick = appLogin; $('#auPass').onkeydown = e => { if (e.key === 'Enter') appLogin(); };
document.querySelectorAll('.signOut').forEach(a => a.onclick = async e => { e.preventDefault(); await post('/api/auth/logout').catch(() => {}); location.hash = ''; location.reload(); });

async function start() {
  AUTH = await api('/api/auth/me'); ME = AUTH.user;
  if (!ME) return showAppLogin();
  $('#appLogin').classList.add('hidden');
  document.querySelectorAll('.meName').forEach(e => e.textContent = `${ME.username} (${ME.role})`);
  document.body.classList.toggle('ro', ME.role === 'viewer');
  document.querySelectorAll('#side a[data-p=appusers], #side a[data-p=audit], #side a[data-p=routers], #side a[data-p=adopt]').forEach(a => a.style.display = ME.role === 'admin' ? '' : 'none');
  $('#demoBtn').style.display = AUTH.allow_demo ? '' : 'none';
  routerPicker();
  if (AUTH.default_host && !$('#host').value) { $('#host').value = AUTH.default_host; $('#port').value = AUTH.default_port || 22; }
  const s = await api('/api/status');
  document.querySelectorAll('.adminOnly').forEach(e => e.style.display = ME.role === 'admin' ? '' : 'none');
  const want = location.hash.slice(1);
  if (!s.connected && !['account', 'routers', 'adopt', 'appusers', 'audit'].includes(want)) {
    $('#login').classList.remove('hidden'); $('#shell').classList.add('hidden'); (ROUTER_LIST.length || $('#host').value ? $('#username') : $('#host')).focus();
    window.onhashchange = () => { if (['account', 'routers', 'adopt', 'appusers', 'audit'].includes(location.hash.slice(1))) location.reload(); };
    return;
  }
  if (!s.connected) { $('#login').classList.add('hidden'); $('#shell').classList.remove('hidden'); $('#tHost').textContent = 'not connected'; $('#logoutBtn').textContent = 'Connect router'; $('#saveBtn').style.display = 'none';
    $('#logoutBtn').onclick = () => { location.hash = ''; location.reload(); }; return go(want); }
  window.onhashchange = null;
  DEMO = s.demo; VIA = s.via || ''; VENDOR = s.vendor || 'huawei'; document.body.dataset.vendor = VENDOR; document.body.dataset.kind = s.kind || 'router'; $('#login').classList.add('hidden'); $('#shell').classList.remove('hidden');
  routerSwitcher(s);
  topbar().catch(() => {}); setInterval(() => { if (!document.hidden) topbar().catch(() => {}); }, 30000);
  go(location.hash.slice(1) || 'dashboard');
}
window.addEventListener('DOMContentLoaded', start);
