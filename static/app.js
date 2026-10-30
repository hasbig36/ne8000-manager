/* NE8000 Manager front end. Plain JS, no build step, no external libraries. */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const attr = s => esc(JSON.stringify(s));

async function api(path, opts = {}, retried = false) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'ne8000' }, credentials: 'same-origin', ...opts });
  const data = await res.json().catch(() => ({ error: 'Bad response from server (' + res.status + ')' }));
  if (res.status === 401 && data.login && path !== '/api/auth/me') { showAppLogin(); }
  // connected over SNMP and this needs the router CLI: ask for an SSH login once, then try again
  if (!res.ok && data.ssh_required && !retried && !path.startsWith('/api/connect') && await sshLogin(data.error)) return api(path, opts, true);
  if (!res.ok) { const err = new Error(data.error || res.statusText); err.data = data; throw err; }
  return data;
}
let sshAsk = null;
function sshLogin(why) {
  if (sshAsk) return sshAsk;   // several requests at once share one prompt
  sshAsk = new Promise(done => {
    let last = ''; try { last = localStorage.getItem('ne8000.router') || ''; } catch (e) {}
    const m = $('#modal'), r = (ROUTER_LIST || []).find(x => x.id === last);
    m.innerHTML = `<h2>SSH login needed</h2><p class="muted">${esc(why || 'This needs the router command line.')} Monitoring keeps working over SNMP without it.</p>
      <div class="form"><label>Router username</label><input id="sshU" autocomplete="username" value="${esc(r && r.username || '')}">
      <label>Password</label><input id="sshP" type="password" autocomplete="current-password"></div>
      <div class="err"></div><div class="actions"><button class="btn" data-a="c">Not now</button><button class="btn primary" data-a="s">Sign in</button></div>`;
    $('#modalBg').classList.remove('hidden'); ($('#sshU').value ? $('#sshP') : $('#sshU')).focus();
    const finish = ok => { closeModal(); sshAsk = null; done(ok); };
    const submit = async () => {
      try { const x = await post('/api/connect/ssh', { username: $('#sshU').value, password: $('#sshP').value }); VIA = x.via; topbar().catch(() => {}); toast('SSH connected'); finish(true); }
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
function table(host, cols, rows, opts = {}) {
  const st = host._t || (host._t = { sort: opts.sort || null, dir: opts.dir || 1, q: '' });
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
function closeModal() { $('#modalBg').classList.add('hidden'); $('#modal').innerHTML = ''; }
$('#modalBg').addEventListener('mousedown', e => { if (e.target.id === 'modalBg') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeModal(); closeDrawer(); } });

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
    <form class="form" onsubmit="return false">${(o.fields || []).map(field).join('')}</form>
    <div class="preview hidden"><div class="lbl">Commands that will be sent:</div><pre class="cli"></pre></div>
    <div class="err"></div>
    <div class="actions"><button class="btn" data-a="cancel">Cancel</button><button class="btn" data-a="check" title="send the lines, then abort instead of commit">Check only</button><button class="btn primary" data-a="preview">Review</button></div>`;
  $('#modalBg').classList.remove('hidden');
  const form = m.querySelector('form'), err = m.querySelector('.err'), pv = m.querySelector('.preview');
  const first = form.querySelector('input,select'); if (first) first.focus();
  const params = () => { const p = { ...(o.params || {}) }; form.querySelectorAll('[name]').forEach(el => p[el.name] = el.type === 'checkbox' ? el.checked : el.value); return p; };
  let reviewed = false;
  form.addEventListener('input', () => { reviewed = false; pv.classList.add('hidden'); setPrimary('Review'); });
  const setPrimary = t => { const b = m.querySelector('[data-a=preview]'); b.textContent = t; b.classList.toggle('danger', false); };
  const run = async (commit) => {
    err.textContent = '';
    try {
      const r = await post(o.url || '/api/action', { action: o.action, params: params(), commit });
      closeModal(); toast((commit ? 'Applied: ' : 'Syntax checked (not committed): ') + o.title);
      if (!commit) showOutput('Check result: ' + o.title, r.output);
      if (o.after) o.after(commit); else refresh();
    } catch (e) { err.textContent = e.message; }
  };
  m.querySelector('.actions').onclick = async e => {
    const a = e.target.dataset.a; if (!a) return;
    if (a === 'cancel') return closeModal();
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
    run(true);
  };
  if (!(o.fields || []).length) m.querySelector('[data-a=preview]').click();
}
function showOutput(title, text) {
  const m = $('#modal'); m.innerHTML = `<h2>${esc(title)}</h2><pre>${esc(text)}</pre><div class="actions"><button class="btn primary">Close</button></div>`;
  m.querySelector('button').onclick = closeModal; $('#modalBg').classList.remove('hidden');
}

/* ---------- navigation ---------- */
const I = {
  dash: '<path d="M2 2h5v6H2zM9 2h5v3H9zM9 7h5v7H9zM2 10h5v4H2z"/>', ifs: '<path d="M2 4h12v8H2zM5 7v2M8 7v2M11 7v2"/>', vlan: '<path d="M2 3h12M2 8h12M2 13h12M5 3v10M11 3v10"/>',
  ip: '<circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c2 2 2 10 0 12M8 2c-2 2-2 10 0 12"/>', route: '<path d="M3 13V8a3 3 0 013-3h7M10 2l3 3-3 3"/>', q: '<path d="M2 4h9M2 8h12M2 12h6"/>',
  sys: '<circle cx="8" cy="8" r="2.5"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M3 13l1.5-1.5M11.5 4.5L13 3"/>', tool: '<path d="M10 2a3 3 0 00-3 4L2 11l3 3 5-5a3 3 0 004-3l-2 1-2-2 1-2z"/>',
  graph: '<path d="M2 13l4-5 3 3 5-7"/>', log: '<path d="M3 2h10v12H3zM5 5h6M5 8h6M5 11h4"/>', hw: '<path d="M3 3h10v10H3zM6 6h4v4H6zM1 6h2M1 10h2M13 6h2M13 10h2"/>', user: '<circle cx="8" cy="5" r="3"/><path d="M2 14c1-3 3-4 6-4s5 1 6 4"/>',
  term: '<path d="M2 3h12v10H2zM4 6l2 2-2 2M8 10h4"/>', rtr: '<path d="M2 9h12v4H2zM5 11h.01M8 11h.01M4 9l2-5h4l2 5"/>', adopt: '<path d="M2 9h8v4H2zM12 3v6M9 6h6"/>', plan: '<path d="M2 3l4-1 4 2 4-1v11l-4 1-4-2-4 1zM6 2v11M10 4v11"/>', bill: '<path d="M3 2h10v12l-2-1-2 1-2-1-2 1-2-1zM5.5 5h5M5.5 8h5M5.5 11h3"/>',
};
const NAV = [
  [null, [['dashboard', 'Dashboard', I.dash]]],
  ['Network', [['interfaces', 'Interfaces', I.ifs], ['graphs', 'Graphs', I.graph], ['netflow', 'Traffic per address', I.graph], ['vlan', 'VLAN', I.vlan], ['queues', 'Queues / QoS', I.q]]],
  ['IP', [['addresses', 'Addresses', I.ip], ['routes', 'Routes', I.route], ['arp', 'ARP', I.ip], ['vpn', 'VPN instances', I.ip], ['neighbors', 'Neighbors (LLDP)', I.ip]]],
  ['Planning', [['netmap', 'Network map', I.plan], ['plans', 'Network plans', I.plan]]],
  ['Billing', [['billing', 'Splynx billing', I.bill]]],
  ['Routing', [['bgp', 'BGP', I.route], ['ospf', 'OSPF', I.route], ['isis', 'IS-IS', I.route], ['mpls', 'MPLS / LDP', I.route]]],
  ['System', [['resources', 'Resources', I.graph], ['hardware', 'Hardware', I.hw], ['users', 'Router users', I.user], ['settings', 'Settings', I.sys], ['logs', 'Logs & alarms', I.log]]],
  ['Manager', [['account', 'My account', I.user], ['routers', 'Routers', I.rtr], ['adopt', 'Adopt devices', I.adopt], ['appusers', 'App users', I.user], ['audit', 'Audit log', I.log]]],
  ['Tools', [['traffic', 'Traffic monitor', I.graph], ['ping', 'Ping / Traceroute', I.tool], ['console', 'Console', I.term], ['config', 'Config editor', I.term], ['backup', 'Backup', I.tool]]],
];
$('#side').innerHTML = NAV.map(([g, items]) => (g ? `<div class="grp">${g}</div>` : '') + items.map(([k, t, ic]) =>
  `<a data-p="${k}"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">${ic}</svg>${t}</a>`).join('')).join('');
$('#side').onclick = e => { const a = e.target.closest('a[data-p]'); if (a) { location.hash = a.dataset.p; $('#side').classList.remove('open'); } };
$('#menuBtn').onclick = () => $('#side').classList.toggle('open');

let timers = [], charts = [], current = null, DEMO = false, VIA = '', ME = null, AUTH = {}, ROUTER_LIST = [];
function every(ms, fn) { timers.push(setInterval(() => { if (!document.hidden) fn().catch(e => console.warn(e)); }, ms)); }
function cleanup() { timers.forEach(clearInterval); timers = []; charts.forEach(c => c.destroy()); charts = []; }
function chart(host, opts) { const c = new TimeChart(host, opts); charts.push(c); return c; }
function refresh() { if (current) go(current, true); }

async function go(page, keepScroll) {
  const [base, arg] = (page || '').split('/');
  if (!PAGES[base]) page = 'dashboard';
  const key = PAGES[base] ? base : 'dashboard';
  if (window.planGuard && !window.planGuard(page)) return;
  cleanup(); closeDrawer(); current = page;
  const navKey = key === 'plan' ? 'plans' : key;
  document.querySelectorAll('#side a').forEach(a => a.classList.toggle('active', a.dataset.p === navKey));
  const c = $('#content'); const y = c.scrollTop;
  if (!keepScroll) c.innerHTML = '<div class="muted">Loading…</div>';
  try { await PAGES[key](c, arg); if (keepScroll) c.scrollTop = y; }
  catch (e) { c.innerHTML = `<div class="card"><div class="err">${esc(e.message)}</div><button class="btn" onclick="refresh()">Retry</button></div>`; }
}
window.addEventListener('hashchange', () => go(location.hash.slice(1)));

function head(title, sub, buttons = '') { return `${DEMO ? '<div class="banner">Demo mode: a simulated router with sample data. Changes apply to the simulation only.</div>' : ''}${ME && ME.role === 'viewer' ? '<div class="banner">Your account is read-only: you can look at everything but changes are refused.</div>' : ''}<h1>${esc(title)}${sub ? ` <span class="sub">${esc(sub)}</span>` : ''}<span style="flex:1"></span>${buttons}</h1>`; }
const legend = items => `<div class="legend">${items.map(([n, c, v]) => `<span><i style="background:var(${c})"></i>${esc(n)}${v != null ? ` <b data-v="${esc(n)}">${v}</b>` : ''}</span>`).join('')}</div>`;

/* ---------- top bar ---------- */
async function topbar() {
  const o = await api('/api/view/overview');
  $('#tName').textContent = o.sysname; $('#tHost').textContent = o.host + (o.via ? ' · ' + o.via : ''); $('#tCpu').textContent = Fmt.pct(o.cpu);
  $('#tMem').textContent = Fmt.pct(o.memory.percent); $('#tUp').textContent = (o.uptime || '').replace(/ minutes?/, 'm').replace(/ hours?,/, 'h').replace(/ days?,/, 'd');
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
    ${o.alarms.total == null ? t('Active alarms', '-', '<a href="#logs">sign in with SSH to read alarms</a>') : t('Active alarms', o.alarms.total, `${o.alarms.critical} major or critical`)}</div>
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
  every(5000, async () => draw(await api('/api/resources')));
  every(15000, async () => { Object.assign(o, await topbar()); tops(); });
};

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

/* ----- interfaces ----- */
let ifCache = [];
PAGES.interfaces = async c => {
  const rows = ifCache = await api('/api/view/interfaces');
  await Promise.all([ipOffLoad(), arpLoad()]);
  const kind = c._kind || 'all';
  c.innerHTML = head('Interfaces', '', `<button class="btn" onclick="subifForm()">+ Sub-interface</button><button class="btn" onclick="loopbackForm()">+ Loopback</button>`) +
    `<div class="card"><div class="bar"><div class="seg" id="ifKind">${[['all', 'All'], ['ethernet', 'Physical'], ['sub-interface', 'Sub-interfaces'], ['logical', 'Logical']].map(([k, l]) => `<button data-k="${k}" class="${k === kind ? 'on' : ''}">${l}</button>`).join('')}</div>
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
  draw(rows);
  every(10000, async () => { if (!$('#modalBg').classList.contains('hidden')) return; ifCache = await api('/api/view/interfaces'); await arpLoad(); draw(ifCache); });
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
    intro: r.ip ? `${name} already has ${r.ip} as its primary address, so the new one is added as secondary (sub). Untick it only to replace the primary.` : '',
    fields: [{ k: 'address', label: 'Address/prefix', ph: '10.0.0.1/30' }, { k: 'secondary', label: 'Secondary (sub)', type: 'check', value: !!r.ip }, { k: 'vpn', label: 'Bind VPN instance', ph: 'optional', help: 'Binding a VPN instance removes existing IP addresses on the interface.' }] });
}
/* ----- every address on one interface, in a dropdown: change, remove, add ----- */
let arpCache = { at: 0, rows: [] };    // the router's ARP table, to show the client (neighbour) behind each address
async function arpLoad() { if (Date.now() - arpCache.at < 30000) return arpCache.rows; try { arpCache = { at: Date.now(), rows: (await api('/api/view/arp')).entries || [] }; } catch (e) { } return arpCache.rows; }
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
async function ipOffLoad() { try { ipOff = await api('/api/ip/disabled'); } catch (e) { ipOff = {}; } }
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
    !sub && subs ? `${name} still has ${subs} secondary address${subs > 1 ? 'es' : ''}. The router refuses to take off the primary until they are disabled or removed.` : ''].filter(Boolean);
  change({ title: `Disable ${ip} on ${name}`, action: 'interface.ip_remove', url: '/api/ip/toggle', params: { interface: name, address: ip, secondary: sub }, intro: notes.join(' '),
    fields: [{ k: 'reason', label: 'Reason (optional)', ph: 'e.g. unpaid, migrated' }], after: ipAfter });
}
function ipEnableForm(name, ip, sub) {
  const r = ifCache.find(x => x.name === name) || {};
  change({ title: `Enable ${ip} on ${name}`, action: 'interface.ip_add', url: '/api/ip/toggle', params: { interface: name, address: ip, secondary: sub },
    intro: !sub && r.ip ? `This was the primary address. ${name} now has ${r.ip} as primary, which this replaces.` : `Puts ${ip} back on ${name} as ${sub ? 'a secondary' : 'the primary'} address.`, after: ipAfter });
}
function ipAfter() { ifCache = []; refresh(); }
function ipRemoveForm(name, ip, sub, use) {
  const r = ifCache.find(x => x.name === name) || {}, subs = (r.secondary || []).length;
  const notes = [use ? `${use} is in this subnet; that session will go down.` : '',
    !sub && subs ? `${name} still has ${subs} secondary address${subs > 1 ? 'es' : ''}. The router refuses to remove the primary until they are removed.` : ''].filter(Boolean);
  change({ title: `Remove ${ip} from ${name}`, action: 'interface.ip_remove', params: { interface: name, address: ip, secondary: sub }, intro: notes.join(' ') });
}
function parentList() { return ifCache.filter(i => ['ethernet', 'eth-trunk'].includes(i.type)).map(i => i.name); }
async function ensureIfs() { if (!ifCache.length) ifCache = await api('/api/view/interfaces'); }
async function subifForm(parent) {
  await ensureIfs();
  change({ title: 'New dot1q sub-interface', action: 'subif.create', fields: [
    { k: 'parent', label: 'Parent port', type: 'select', options: parentList(), value: parent },
    { k: 'vlan', label: 'VLAN ID', type: 'number', ph: '100' }, { k: 'number', label: 'Sub-interface no.', type: 'number', ph: 'same as VLAN', help: 'Name will be <parent>.<number>' },
    { k: 'encap', label: 'Encapsulation', type: 'select', options: [['vlan-type', 'vlan-type dot1q (one VLAN)'], ['termination', 'dot1q termination vid'], ['qinq', 'QinQ termination']] },
    { k: 'inner_vlan', label: 'Inner VLAN (QinQ)', type: 'number', ph: 'only for QinQ' }, { k: 'address', label: 'IP address/prefix', ph: '10.20.100.1/24' },
    { k: 'vpn', label: 'VPN instance', ph: 'optional' }, { k: 'description', label: 'Description' }] });
}
function loopbackForm() { change({ title: 'New loopback', action: 'loopback.create', fields: [{ k: 'number', label: 'Number', type: 'number', ph: '1' }, { k: 'address', label: 'IP/prefix', ph: '1.1.1.2/32' }, { k: 'description', label: 'Description' }] }); }

function closeDrawer() { const d = $('#drawer'); if (d._stop) d._stop(); d.classList.add('hidden'); d.innerHTML = ''; }
async function interfaceDrawer(name) {
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
function addrForm() { change({ title: 'Add IP address', action: 'interface.ip_add', fields: [{ k: 'interface', label: 'Interface', type: 'select', options: ifCache.filter(i => i.type !== 'null' && !i.l2).map(i => i.name) }, { k: 'address', label: 'Address/prefix', ph: '10.0.0.1/30' }, { k: 'secondary', label: 'Secondary (sub)', type: 'check' }] }); }

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
  b.peers.forEach(p => { p.description = desc[p.peer] || ''; p.ignored = ign.has(p.peer); });
  const est = b.peers.filter(p => p.state === 'Established');
  c.innerHTML = head('BGP', `AS ${b.local_as || '-'} · router ID ${b.router_id || '-'}`) +
    `<div class="tiles"><div class="tile"><div class="l">Peers</div><div class="v">${b.peers.length}</div></div><div class="tile"><div class="l">Established</div><div class="v">${est.length}</div></div>
    <div class="tile"><div class="l">Down</div><div class="v" style="color:${b.peers.length - est.length ? 'var(--bad)' : ''}">${b.peers.length - est.length}</div></div><div class="tile"><div class="l">Prefixes received</div><div class="v">${Fmt.num(b.peers.reduce((a, p) => a + p.prefixes, 0))}</div></div></div>
    <div class="card"><h3>Peers</h3><div id="bP"></div></div>
    <div class="grid g2"><div class="card"><h3>Advertised networks</h3><div id="bN"></div></div><div class="card"><h3>Configuration</h3><pre>${esc(Object.entries(b.config).map(([k, v]) => 'bgp ' + k + '\n ' + v.join('\n ')).join('\n'))}</pre></div></div>
    <div class="card" id="bRcard"><h3 id="bRt">BGP routing table</h3><div class="row" style="gap:6px;margin-bottom:10px"><input id="bLk" placeholder="Lookup: 8.8.8.8 or 8.8.8.0/24" style="max-width:260px"><button class="btn sm" id="bLkBtn">Look up</button></div><div id="bR" class="muted">Pick a peer’s Received / Advertised, look up an address, or <button class="btn sm" onclick="bgpRoutes()">load the full BGP table</button></div></div>`;
  $('#bLkBtn').onclick = () => bgpRoutes(null, null, $('#bLk').value.trim()); $('#bLk').onkeydown = e => { if (e.key === 'Enter') $('#bLkBtn').click(); };
  table($('#bP'), [{ t: 'Peer', k: 'peer', f: p => `<b>${esc(p.peer)}</b>` }, { t: 'Description', k: 'description' }, { t: 'Remote AS', k: 'as' }, { t: 'Type', sort: false, f: p => p.as === b.local_as ? 'iBGP' : 'eBGP' },
    { t: 'State', k: 'state', f: p => bgpPill(p.state) }, { t: 'Up/Down', k: 'up_down' }, { t: 'Prefixes', k: 'prefixes', num: true }, { t: 'Msg in/out', k: 'msg_rcvd', num: true, f: p => `${Fmt.num(p.msg_rcvd)} / ${Fmt.num(p.msg_sent)}` }, { t: 'OutQ', k: 'out_q', num: true },
    { t: '', sort: false, cls: 'acts', f: p => `<button class="btn sm" onclick='bgpRoutes(${attr(p.peer)},"received")'>Received</button><button class="btn sm" onclick='bgpRoutes(${attr(p.peer)},"advertised")'>Advertised</button>
      <button class="btn sm" onclick='change({title:"Reset BGP session ${esc(p.peer)}",action:"bgp.reset",fields:[{k:"soft",label:"Type",type:"select",options:[["","Hard reset (session drops)"],["import","Soft refresh inbound"],["export","Soft refresh outbound"]]}],params:{peer:${attr(p.peer)}}})'>Reset</button>
      <button class="btn sm" onclick='change({title:"${p.ignored ? 'Enable' : 'Disable'} peer ${esc(p.peer)}",action:"bgp.peer_enable",params:{local_as:${attr(b.local_as)},peer:${attr(p.peer)},enabled:${p.ignored}}})'>${p.ignored ? 'Enable' : 'Disable'}</button>
      <button class="btn sm danger" onclick='change({title:"Remove peer ${esc(p.peer)}",action:"bgp.peer_remove",params:{local_as:${attr(b.local_as)},peer:${attr(p.peer)}}})'>Remove</button>` }],
    b.peers, { toolbar: `<button class="btn" onclick="peerForm()">+ Peer</button>`, rowClass: p => p.ignored ? 'off' : '' });
  table($('#bN'), [{ t: 'Network', k: 'n', f: x => `<b>${esc(x.n)}</b>` }, { t: '', sort: false, cls: 'acts', f: x => `<button class="btn sm danger" onclick='change({title:"Stop advertising ${esc(x.n)}",action:"bgp.network_remove",params:{local_as:${attr(b.local_as)},prefix:${attr(x.n)}}})'>Remove</button>` }],
    nets.map(n => ({ n })), { filter: false, toolbar: `<button class="btn" onclick='change({title:"Advertise network",action:"bgp.network_add",params:{local_as:${attr(b.local_as)}},fields:[{k:"prefix",label:"Network",ph:"203.0.113.0/24"}]})'>+ Network</button>`, empty: 'None' });
  every(15000, async () => { if ($('#modalBg').classList.contains('hidden') && !$('#bR table')) PAGES.bgp(c); });
};
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
    <label>Source address</label><input id="pA" placeholder="optional"><label>VPN instance</label><input id="pV" placeholder="optional"></div>
    <div class="bar" style="margin-top:12px"><button class="btn primary" id="pGo">Start</button><span class="muted mono" id="pCmd"></span></div><pre id="pOut" class="hidden"></pre></div>`;
  const build = () => { const k = $('#pK').value, t = $('#pT').value.trim(); let cmd = k;
    if (k === 'ping') { if ($('#pC').value) cmd += ' -c ' + (+$('#pC').value); if ($('#pS').value) cmd += ' -s ' + (+$('#pS').value); }
    if ($('#pA').value.trim()) cmd += ' -a ' + $('#pA').value.trim(); if ($('#pV').value.trim()) cmd += ' -vpn-instance ' + $('#pV').value.trim();
    return cmd + ' ' + t; };
  c.querySelectorAll('input,select').forEach(e => e.oninput = () => $('#pCmd').textContent = build());
  $('#pT').onkeydown = e => { if (e.key === 'Enter') $('#pGo').click(); };
  $('#pGo').onclick = async () => { if (!$('#pT').value.trim()) return; const o = $('#pOut'); o.classList.remove('hidden'); o.textContent = 'Running ' + build() + ' …';
    try { o.textContent = (await post('/api/cli', { command: build() })).output; } catch (e) { o.textContent = 'Error: ' + e.message; } };
};

const QUICK = ['display interface brief', 'display ip interface brief', 'display ip routing-table', 'display bgp peer', 'display ospf peer brief', 'display isis peer', 'display mpls ldp session', 'display arp all', 'display lldp neighbor brief', 'display alarm active', 'display device', 'display cpu-usage', 'display memory-usage', 'display optical-module brief', 'display logbuffer', 'display current-configuration'];
let cliHist = [];
PAGES.console = async c => {
  c.innerHTML = head('Console', 'display, ping and tracert. Use Config editor for changes.') + `<div class="card"><div class="bar"><input id="cIn" class="mono" style="flex:1" placeholder="display interface GigabitEthernet0/1/0" list="cQ"><datalist id="cQ">${QUICK.map(q => `<option value="${q}">`).join('')}</datalist><button class="btn primary" id="cGo">Run</button></div>
    <div class="bar">${QUICK.slice(0, 10).map(q => `<button class="btn sm" data-q="${q}">${q.replace('display ', '')}</button>`).join('')}</div><pre id="cOut" style="max-height:65vh">${esc(cliHist.at(-1)?.out || '')}</pre></div>`;
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
    try { o.textContent = (await post('/api/config', { config, commit })).output; toast(commit ? 'Committed' : 'Checked, nothing committed'); } catch (e) { o.classList.add('hidden'); $('#eErr').textContent = e.message; } };
  $('#eCheck').onclick = () => go(false); $('#eApply').onclick = () => go(true);
  $('#eLoad').onclick = async () => { const o = $('#eOut'); o.classList.remove('hidden'); o.textContent = 'Loading…'; o.textContent = (await api('/api/running-config')).output; };
};

PAGES.backup = async c => {
  c.innerHTML = head('Backup') + `<div class="grid g2"><div class="card"><h3>Download running configuration</h3><p class="muted">Saves <span class="mono">display current-configuration</span> as a .cfg file on this computer.</p><a class="btn primary" href="/api/running-config?download=1">Download</a></div>
    <div class="card"><h3>Save to startup</h3><p class="muted">Runs <span class="mono">save</span> so the committed configuration survives a reboot.</p><button class="btn" onclick="saveCfg()">Save configuration</button></div></div>`;
};

async function saveCfg() { if (!confirm('Save the running configuration to startup?')) return; try { const r = await post('/api/save'); toast(r.output.split('\n').filter(Boolean).pop() || 'Saved'); } catch (e) { toast(e.message, true); } }
$('#saveBtn').onclick = saveCfg;

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
  DEMO = s.demo; VIA = s.via || ''; $('#login').classList.add('hidden'); $('#shell').classList.remove('hidden');
  routerSwitcher(s);
  topbar().catch(() => {}); setInterval(() => { if (!document.hidden) topbar().catch(() => {}); }, 30000);
  go(location.hash.slice(1) || 'dashboard');
}
window.addEventListener('DOMContentLoaded', start);
