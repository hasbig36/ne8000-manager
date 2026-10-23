/* Network plans: draw the network, list the changes, execute them, keep the record. */
const NODE_META = {
  ne8000: { label: 'NE8000 router', tag: 'NE8000', color: '--accent' }, router: { label: 'Router', tag: 'ROUTER', color: '--s1' },
  switch: { label: 'Switch', tag: 'SWITCH', color: '--s3' }, olt: { label: 'OLT', tag: 'OLT', color: '--s1' },
  reseller: { label: 'Reseller', tag: 'RESELLER', color: '--s2' }, customer: { label: 'Customer', tag: 'CUSTOMER', color: '--s2' },
  upstream: { label: 'Upstream / IX', tag: 'UPSTREAM', color: '--muted' }, server: { label: 'Server', tag: 'SERVER', color: '--muted' },
  ap: { label: 'Tower / AP', tag: 'TOWER', color: '--s3' }, note: { label: 'Note', tag: 'NOTE', color: '--faint' },
};
const NODE_FIELDS = {
  _: [{ k: 'mgmt_ip', label: 'Management IP' }, { k: 'location', label: 'Site / location' }, { k: 'notes', label: 'Notes' }],
  ne8000: [{ k: 'mgmt_ip', label: 'Management IP' }, { k: 'local_as', label: 'BGP local AS' }, { k: 'location', label: 'Site' }, { k: 'notes', label: 'Notes' }],
  reseller: [{ k: 'splynx_id', label: 'Splynx customer ID' }, { k: 'bgp_as', label: 'BGP AS (if peering)' }, { k: 'peer_ip', label: 'BGP peer IP' }, { k: 'location', label: 'Location' }, { k: 'notes', label: 'Notes' }],
};
NODE_FIELDS.customer = NODE_FIELDS.reseller;
const LINK_FIELDS = [{ k: 'label', label: 'Label', ph: 'VLAN 310 / 10G fibre' }, { k: 'router_port', label: 'NE8000 port', ph: 'GigabitEthernet0/1/6' },
  { k: 'vlan', label: 'VLAN ID', ph: '310' }, { k: 'address', label: 'Router IP/prefix', ph: '102.216.117.9/30' }, { k: 'rate_kbps', label: 'Rate limit (kbit/s)', ph: '300000' },
  { k: 'vpn', label: 'VPN instance', ph: 'optional' }, { k: 'notes', label: 'Notes' }];

const ifList = () => ifCache.map(i => i.name);
const TASK_TYPES = {
  'subif.create': { label: 'Create dot1q sub-interface', fields: () => [{ k: 'parent', label: 'Parent port', list: parentList() }, { k: 'vlan', label: 'VLAN ID', type: 'number' }, { k: 'number', label: 'Sub-interface no.', type: 'number', ph: 'same as VLAN' },
    { k: 'encap', label: 'Encapsulation', type: 'select', options: [['vlan-type', 'vlan-type dot1q'], ['termination', 'dot1q termination'], ['qinq', 'QinQ termination']] }, { k: 'inner_vlan', label: 'Inner VLAN (QinQ)', type: 'number' },
    { k: 'address', label: 'IP/prefix', ph: '10.0.0.1/30' }, { k: 'vpn', label: 'VPN instance' }, { k: 'description', label: 'Description' }] },
  'interface.set': { label: 'Change interface', fields: () => [{ k: 'interface', label: 'Interface', list: ifList() }, { k: 'enabled', label: 'State', type: 'select', options: [['', 'leave as is'], ['true', 'enable (undo shutdown)'], ['false', 'shutdown']] }, { k: 'description', label: 'Description' }, { k: 'mtu', label: 'MTU', type: 'number' }] },
  'interface.ip_add': { label: 'Add IP address', fields: () => [{ k: 'interface', label: 'Interface', list: ifList() }, { k: 'address', label: 'IP/prefix' }, { k: 'secondary', label: 'Secondary', type: 'check' }] },
  'loopback.create': { label: 'Create loopback', fields: () => [{ k: 'number', label: 'Number', type: 'number' }, { k: 'address', label: 'IP/prefix' }, { k: 'description', label: 'Description' }] },
  'vlan.create': { label: 'Create VLAN (+ VLANIF)', fields: () => [{ k: 'vlan', label: 'VLAN ID', type: 'number' }, { k: 'description', label: 'Description' }, { k: 'address', label: 'VLANIF IP/prefix' }] },
  'vlan.port': { label: 'Switch port mode', fields: () => [{ k: 'interface', label: 'Port', list: parentList() }, { k: 'mode', label: 'Mode', type: 'select', options: [['trunk', 'Trunk: add VLANs'], ['trunk_remove', 'Trunk: remove VLANs'], ['access', 'Access'], ['routed', 'Routed']] }, { k: 'vlans', label: 'VLANs' }, { k: 'vlan', label: 'Access VLAN', type: 'number' }] },
  'qos.car': { label: 'Rate limit (CAR)', fields: () => [{ k: 'interface', label: 'Interface', list: ifList() }, { k: 'direction', label: 'Direction', type: 'select', options: ['outbound', 'inbound'] }, { k: 'cir', label: 'Max rate (kbit/s)', type: 'number' }, { k: 'pir', label: 'Burst (kbit/s)', type: 'number' }] },
  'qos.shaping': { label: 'Port shaping', fields: () => [{ k: 'interface', label: 'Port', list: parentList() }, { k: 'mbps', label: 'Rate (Mbit/s)', type: 'number' }] },
  'route.static_add': { label: 'Static route', fields: () => [{ k: 'prefix', label: 'Destination' }, { k: 'next_hop', label: 'Gateway' }, { k: 'interface', label: 'Out interface', list: ifList() }, { k: 'preference', label: 'Preference', type: 'number' }, { k: 'vpn', label: 'VPN instance' }, { k: 'description', label: 'Description' }] },
  'vpn.create': { label: 'VPN instance', fields: () => [{ k: 'name', label: 'Name' }, { k: 'rd', label: 'RD' }, { k: 'import_rt', label: 'Import RT' }, { k: 'export_rt', label: 'Export RT' }] },
  'bgp.peer_add': { label: 'BGP peer', fields: () => [{ k: 'local_as', label: 'Local AS' }, { k: 'peer', label: 'Peer IP' }, { k: 'remote_as', label: 'Remote AS' }, { k: 'description', label: 'Description' }, { k: 'connect_interface', label: 'Update source' }, { k: 'route_limit', label: 'Max prefixes', type: 'number' }, { k: 'route_policy_in', label: 'Import policy' }, { k: 'route_policy_out', label: 'Export policy' }], note: 'MD5 passwords are not stored in plans; add them on the BGP page.' },
  'bgp.network_add': { label: 'BGP network', fields: () => [{ k: 'local_as', label: 'Local AS' }, { k: 'prefix', label: 'Network' }] },
  'ospf.network_add': { label: 'OSPF network', fields: () => [{ k: 'process', label: 'Process', type: 'number', value: 1 }, { k: 'area', label: 'Area', value: '0.0.0.0' }, { k: 'prefix', label: 'Network' }] },
  'custom.lines': { label: 'Custom configuration', fields: () => [{ k: 'lines', label: 'Config lines', type: 'textarea', ph: 'interface GigabitEthernet0/1/7\n description New uplink\n undo shutdown' }] },
};
const taskLabel = a => (TASK_TYPES[a] || {}).label || a;
const STATUS_PILL = { draft: 'p-off', approved: 'p-warn', checked: 'p-warn', executed: 'p-ok', failed: 'p-bad', cancelled: 'p-off' };
const statusPill = s => `<span class="pill ${STATUS_PILL[s] || 'p-off'}">${esc(s)}</span>`;
const fmtDate = t => new Date(t * 1000).toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const editor = () => { if (ME) return ME.username; try { return localStorage.getItem('planner') || ''; } catch (e) { return ''; } };
const uid = () => Math.random().toString(36).slice(2, 10);

/* ---------- list ---------- */
PAGES.plans = async c => {
  const { plans } = await api('/api/plans');
  const by = s => plans.filter(p => p.status === s).length;
  c.innerHTML = head('Network plans', 'design, execute and keep a record of every change', `<button class="btn primary" onclick="newPlan()">+ New plan</button>`) +
    `<div class="tiles">${[['Plans', plans.length], ['Draft', by('draft')], ['Approved / checked', by('approved') + by('checked')], ['Executed', by('executed')], ['Failed', by('failed')]].map(([l, v]) => `<div class="tile"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('')}</div>
    <div class="card"><div id="pT"></div></div>`;
  table($('#pT'), [{ t: 'Plan', k: 'name', cls: 'wrap', f: p => `<b>${esc(p.name)}</b><div class="faint" style="font-size:12px">${esc(p.description.slice(0, 140))}</div>` },
    { t: 'Status', k: 'status', f: p => statusPill(p.status) }, { t: 'Devices / links', k: 'nodes', f: p => `${p.nodes} / ${p.links}` }, { t: 'Tasks', k: 'tasks', num: true },
    { t: 'Versions', k: 'revisions', num: true }, { t: 'Last execution', k: 'last_run', f: p => p.last_run ? `${p.last_run.commit ? 'commit' : 'check'} on ${esc(p.last_run.host)} · ${p.last_run.ok ? 'ok' : 'failed'}<div class="faint" style="font-size:12px">${fmtDate(p.last_run.at)}</div>` : '<span class="faint">never</span>' },
    { t: 'Updated', k: 'updated', f: p => fmtDate(p.updated) }, { t: 'By', k: 'author' }], plans,
    { sort: 'updated', dir: -1, onRow: p => location.hash = 'plan/' + p.id, empty: 'No plans yet' });
};
function newPlan() {
  const m = $('#modal');
  m.innerHTML = `<h2>New network plan</h2><div class="form"><label>Name</label><input id="npName" placeholder="Connect reseller Kilele Net"><label>Description</label><input id="npDesc" placeholder="what this plan builds and why"></div><div class="err"></div><div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Create</button></div>`;
  $('#modalBg').classList.remove('hidden'); $('#npName').focus();
  m.querySelector('.actions').onclick = async e => {
    if (e.target.dataset.a === 'c') return closeModal();
    if (e.target.dataset.a !== 's') return;
    try { const p = await post('/api/plans', { name: $('#npName').value, description: $('#npDesc').value, author: editor(),
      diagram: { nodes: [{ id: 'n1', type: 'ne8000', label: 'BNG-NDOVU1 (NE8000 M8)', x: 360, y: 160, props: {} }], links: [] } });
      closeModal(); location.hash = 'plan/' + p.id; } catch (err) { m.querySelector('.err').textContent = err.message; }
  };
}

/* ---------- editor ---------- */
let P = null, PD = { dirty: false, tab: 'design', sel: null, connect: false, from: null, lastRun: null, preview: null };
window.planGuard = page => {
  if (PD.dirty && P && page !== 'plan/' + P.id && !confirm('This plan has unsaved changes. Leave without saving?')) { history.replaceState(null, '', '#plan/' + P.id); return false; }
  if (page !== 'plan/' + (P && P.id)) PD.dirty = false;
  return true;
};
window.addEventListener('beforeunload', e => { if (PD.dirty) { e.preventDefault(); e.returnValue = ''; } });
function markDirty() { PD.dirty = true; const d = $('#plDirty'); if (d) d.textContent = '● unsaved changes'; }

PAGES.plan = async (c, id) => {
  await ensureIfs();
  if (!P || P.id !== id || !PD.dirty) { const other = !P || P.id !== id; P = await api('/api/plans/' + id); PD = { ...PD, tab: other ? 'design' : PD.tab, connect: other ? false : PD.connect, dirty: false, sel: null, from: null, lastRun: P.runs.at(-1) || null, preview: null }; }
  c.innerHTML = `${DEMO ? '<div class="banner">Demo mode: plans are saved for real, executions run against the simulated router.</div>' : ''}
    <div class="bar"><a class="btn sm" href="#plans">← All plans</a><span class="sp"></span><span id="plDirty" class="dirty">${PD.dirty ? '● unsaved changes' : ''}</span>
      <input id="plNote" placeholder="what changed? (optional)" style="width:220px">
      <button class="btn primary" id="plSave">Save</button><button class="btn" id="plDup">Duplicate</button><button class="btn" id="plPrint">Print / PDF record</button><button class="btn danger" id="plDel">Delete</button></div>
    <div class="card"><div class="form" style="grid-template-columns:110px 1fr 90px 160px"><label>Plan name</label><input id="plName" value="${esc(P.name)}"><label>Status</label>
      <select id="plStatus">${['draft', 'approved', 'checked', 'executed', 'failed', 'cancelled'].map(s => `<option ${s === P.status ? 'selected' : ''}>${s}</option>`).join('')}</select>
      <label>Description</label><input id="plDesc" value="${esc(P.description)}" style="grid-column:2/5"></div>
      <div class="faint" style="font-size:12px;margin-top:8px">Created ${fmtDate(P.created)}${P.author ? ' by ' + esc(P.author) : ''} · last saved ${fmtDate(P.updated)} · ${P.revisions.length} versions · ${P.runs.length} executions</div></div>
    <div class="tabs" id="plTabs">${[['design', 'Design'], ['tasks', `Tasks (${P.tasks.length})`], ['record', 'Record']].map(([k, l]) => `<button data-t="${k}" class="${PD.tab === k ? 'on' : ''}">${l}</button>`).join('')}</div>
    <div id="plBody"></div>`;
  $('#plTabs').onclick = e => { if (e.target.dataset.t) { PD.tab = e.target.dataset.t; PAGES.plan(c, id); } };
  ['plName', 'plDesc', 'plStatus'].forEach(k => $('#' + k).oninput = () => { P[{ plName: 'name', plDesc: 'description', plStatus: 'status' }[k]] = $('#' + k).value; markDirty(); });
  $('#plSave').onclick = () => savePlan(c);
  $('#plDup').onclick = async () => { if (PD.dirty && !confirm('Duplicate the last saved version? Unsaved changes stay here.')) return; const d = await post(`/api/plans/${P.id}/duplicate`, { author: editor() }); PD.dirty = false; toast('Duplicated'); location.hash = 'plan/' + d.id; };
  $('#plDel').onclick = async () => { if (!confirm(`Delete the plan "${P.name}" and its whole record? This cannot be undone.`)) return; await api('/api/plans/' + P.id, { method: 'DELETE' }); PD.dirty = false; P = null; toast('Plan deleted'); location.hash = 'plans'; };
  $('#plPrint').onclick = printRecord;
  ({ design: renderDesign, tasks: renderTasks, record: renderRecord })[PD.tab]($('#plBody'), c);
};
async function savePlan(c) {
  try {
    P = await api('/api/plans/' + P.id, { method: 'PUT', body: JSON.stringify({ name: P.name, description: P.description, status: P.status, diagram: P.diagram, tasks: P.tasks, note: $('#plNote').value, author: editor() }) });
    PD.dirty = false; toast('Plan saved (version ' + P.revisions.length + ')'); PAGES.plan(c, P.id);
  } catch (e) { toast(e.message, true); }
}

/* ----- design canvas ----- */
const NW = 190, NH = 58;
function renderDesign(host, c) {
  host.innerHTML = `<div class="planwrap"><div>
    <div class="bar"><div class="palette">${Object.entries(NODE_META).map(([k, m]) => `<button class="btn" data-add="${k}">+ ${m.label}</button>`).join('')}</div></div>
    <div class="bar"><button class="btn ${PD.connect ? 'primary' : ''}" id="dConn">${PD.connect ? 'Connecting: click two devices' : 'Connect devices'}</button>
      <button class="btn" id="dGen" title="create tasks from links on the NE8000">Generate tasks from design</button><span class="sp"></span><span class="faint" style="font-size:12px">Drag to move · click to edit · Delete key removes the selection</span></div>
    <div class="canvas ${PD.connect ? 'connect' : ''}" id="dCanvas"></div></div>
    <div class="card" id="dProps"></div></div>`;
  host.querySelectorAll('[data-add]').forEach(b => b.onclick = () => {
    const n = { id: 'n' + uid(), type: b.dataset.add, label: NODE_META[b.dataset.add].label, ...freeSpot(), props: {} };
    P.diagram.nodes.push(n); PD.sel = { kind: 'node', id: n.id }; markDirty(); drawCanvas(); drawProps();
  });
  $('#dConn').onclick = () => { PD.connect = !PD.connect; PD.from = null; renderDesign(host, c); };
  $('#dGen').onclick = generateTasks;
  drawCanvas(); drawProps();
}
document.addEventListener('keydown', e => {
  if (e.key !== 'Delete' && e.key !== 'Backspace') return;
  if (!$('#dCanvas') || !PD.sel || e.target.closest('input,textarea,select')) return;
  deleteSel(); e.preventDefault();
});
function freeSpot() {
  const busy = (x, y) => P.diagram.nodes.some(n => Math.abs(n.x - x) < NW + 20 && Math.abs(n.y - y) < NH + 30);
  for (let y = 40; y < 2000; y += NH + 40) for (let x = 40; x < 1000; x += NW + 40) if (!busy(x, y)) return { x, y };
  return { x: 40, y: 40 };
}
function deleteSel() {
  if (!PD.sel) return;
  if (PD.sel.kind === 'node') { P.diagram.nodes = P.diagram.nodes.filter(n => n.id !== PD.sel.id); P.diagram.links = P.diagram.links.filter(l => l.from !== PD.sel.id && l.to !== PD.sel.id); }
  else P.diagram.links = P.diagram.links.filter(l => l.id !== PD.sel.id);
  PD.sel = null; markDirty(); drawCanvas(); drawProps();
}
function drawCanvas() {
  const host = $('#dCanvas'); if (!host) return;
  const nodes = P.diagram.nodes, byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const W = Math.max(1100, ...nodes.map(n => n.x + NW + 60)), H = Math.max(580, ...nodes.map(n => n.y + NH + 60));
  const links = P.diagram.links.filter(l => byId[l.from] && byId[l.to]).map(l => {
    const a = byId[l.from], b = byId[l.to], x1 = a.x + NW / 2, y1 = a.y + NH / 2, x2 = b.x + NW / 2, y2 = b.y + NH / 2;
    const lab = [l.props.label, l.props.vlan && !String(l.props.label || '').includes(l.props.vlan) ? 'VLAN ' + l.props.vlan : '', l.props.rate_kbps ? Fmt.bps(l.props.rate_kbps * 1000) : ''].filter(Boolean).join(' · ');
    return `<g class="link ${PD.sel && PD.sel.id === l.id ? 'sel' : ''}" data-l="${l.id}"><line class="vis" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/><line class="hit" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>
      ${lab ? `<text x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 6}" text-anchor="middle">${esc(lab)}</text>` : ''}</g>`;
  }).join('');
  const ns = nodes.map(n => { const m = NODE_META[n.type] || NODE_META.router; const sub = n.props.mgmt_ip || n.props.splynx_id && ('Splynx ' + n.props.splynx_id) || n.props.location || '';
    return `<g class="node ${PD.sel && PD.sel.id === n.id ? 'sel' : ''} ${PD.from === n.id ? 'sel' : ''}" data-n="${n.id}" transform="translate(${n.x},${n.y})">
      <rect width="${NW}" height="${NH}" rx="8" style="stroke:var(${m.color})"/><rect width="5" height="${NH}" rx="2" style="fill:var(${m.color});stroke:none"/>
      <text class="tag" x="14" y="17" style="fill:var(${m.color})">${m.tag}</text><text x="14" y="34">${esc(trunc(n.label, 22))}</text><text class="sub" x="14" y="50">${esc(trunc(sub, 26))}</text></g>`; }).join('');
  host.innerHTML = `<svg width="${W}" height="${H}" id="dSvg"><defs><pattern id="grid" width="20" height="20" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" style="fill:var(--grid)"/></pattern></defs>
    <rect width="100%" height="100%" fill="url(#grid)" data-bg="1"/>${links}${ns}</svg>`;
  const svg = $('#dSvg'); let drag = null;
  const pt = e => { const r = svg.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  svg.onpointerdown = e => {
    const g = e.target.closest('[data-n]'), l = e.target.closest('[data-l]');
    if (g) {
      const n = byId[g.dataset.n];
      if (PD.connect) {
        if (!PD.from) { PD.from = n.id; drawCanvas(); return; }
        if (PD.from !== n.id) { const link = { id: 'l' + uid(), from: PD.from, to: n.id, props: {} }; P.diagram.links.push(link); PD.sel = { kind: 'link', id: link.id }; markDirty(); }
        PD.from = null; drawCanvas(); drawProps(); return;
      }
      const p = pt(e); drag = { n, dx: p.x - n.x, dy: p.y - n.y, moved: false }; svg.setPointerCapture(e.pointerId);
      PD.sel = { kind: 'node', id: n.id }; drawProps(); highlight();
    } else if (l) { PD.sel = { kind: 'link', id: l.dataset.l }; drawCanvas(); drawProps(); }
    else { PD.sel = null; PD.from = null; drawCanvas(); drawProps(); }
  };
  svg.onpointermove = e => { if (!drag) return; const p = pt(e); drag.n.x = Math.max(0, Math.round((p.x - drag.dx) / 10) * 10); drag.n.y = Math.max(0, Math.round((p.y - drag.dy) / 10) * 10); drag.moved = true;
    const g = svg.querySelector(`[data-n="${drag.n.id}"]`); g.setAttribute('transform', `translate(${drag.n.x},${drag.n.y})`); redrawLinks(); };
  svg.onpointerup = () => { if (drag && drag.moved) { markDirty(); drawCanvas(); } drag = null; };
  const highlight = () => svg.querySelectorAll('.node').forEach(g => g.classList.toggle('sel', PD.sel && g.dataset.n === PD.sel.id));
  const redrawLinks = () => svg.querySelectorAll('[data-l]').forEach(g => { const l = P.diagram.links.find(x => x.id === g.dataset.l); const a = byId[l.from], b = byId[l.to];
    g.querySelectorAll('line').forEach(x => { x.setAttribute('x1', a.x + NW / 2); x.setAttribute('y1', a.y + NH / 2); x.setAttribute('x2', b.x + NW / 2); x.setAttribute('y2', b.y + NH / 2); });
    const t = g.querySelector('text'); if (t) { t.setAttribute('x', (a.x + b.x + NW) / 2); t.setAttribute('y', (a.y + b.y + NH) / 2 - 6); } });
}
const trunc = (s, n) => (s = String(s || '')).length > n ? s.slice(0, n - 1) + '…' : s;
function drawProps() {
  const host = $('#dProps'); if (!host) return;
  if (!PD.sel) { const d = P.diagram; host.innerHTML = `<h3>Design</h3><div class="kv"><div><span>Devices</span>${d.nodes.length}</div><div><span>Links</span>${d.links.length}</div></div>
    <p class="muted" style="font-size:12.5px">Add devices from the toolbar, then use <b>Connect devices</b> to draw links. Give a link from the NE8000 a port, VLAN, IP and rate, then press <b>Generate tasks from design</b> to turn it into router changes.</p>`; return; }
  if (PD.sel.kind === 'node') {
    const n = P.diagram.nodes.find(x => x.id === PD.sel.id); if (!n) return;
    const fields = NODE_FIELDS[n.type] || NODE_FIELDS._;
    host.innerHTML = `<h3>${esc(NODE_META[n.type].label)}</h3><div class="form" style="grid-template-columns:1fr"><label>Name</label><input data-f="label" value="${esc(n.label)}">
      <label>Type</label><select data-f="type">${Object.entries(NODE_META).map(([k, m]) => `<option value="${k}" ${k === n.type ? 'selected' : ''}>${m.label}</option>`).join('')}</select>
      ${fields.map(f => `<label>${f.label}</label><input data-p="${f.k}" value="${esc(n.props[f.k] || '')}">`).join('')}</div>
      <div class="actions"><button class="btn danger" id="dDel">Delete device</button></div>`;
    host.querySelectorAll('[data-f]').forEach(el => el.oninput = () => { n[el.dataset.f] = el.value; markDirty(); drawCanvas(); if (el.dataset.f === 'type') drawProps(); });
    host.querySelectorAll('[data-p]').forEach(el => el.oninput = () => { n.props[el.dataset.p] = el.value; markDirty(); drawCanvas(); });
  } else {
    const l = P.diagram.links.find(x => x.id === PD.sel.id); if (!l) return;
    const name = id => (P.diagram.nodes.find(n => n.id === id) || {}).label || '?';
    host.innerHTML = `<h3>Link</h3><div class="muted" style="margin-bottom:8px">${esc(name(l.from))} ↔ ${esc(name(l.to))}</div><div class="form" style="grid-template-columns:1fr">
      ${LINK_FIELDS.map(f => `<label>${f.label}</label><input data-p="${f.k}" value="${esc(l.props[f.k] || '')}" placeholder="${esc(f.ph || '')}" ${f.k === 'router_port' ? 'list="dlPorts"' : ''}>`).join('')}
      <datalist id="dlPorts">${parentList().map(x => `<option value="${esc(x)}">`).join('')}</datalist></div>
      <div class="actions"><button class="btn danger" id="dDel">Delete link</button></div>`;
    host.querySelectorAll('[data-p]').forEach(el => el.oninput = () => { l.props[el.dataset.p] = el.value; markDirty(); drawCanvas(); });
  }
  $('#dDel').onclick = deleteSel;
}
function generateTasks() {
  const byId = Object.fromEntries(P.diagram.nodes.map(n => [n.id, n])); let added = 0;
  const router = P.diagram.nodes.find(n => n.type === 'ne8000');
  const has = src => P.tasks.some(t => t.source === src);
  for (const l of P.diagram.links) {
    const a = byId[l.from], b = byId[l.to]; if (!a || !b) continue;
    const pr = l.props; const other = a.type === 'ne8000' ? b : a;
    if (!pr.router_port || !pr.vlan) continue;
    const sub = `${pr.router_port}.${pr.vlan}`; const desc = other.label + (other.props.splynx_id ? ' ' + other.props.splynx_id : '');
    if (!has('subif:' + l.id)) { P.tasks.push({ id: 't' + uid(), source: 'subif:' + l.id, action: 'subif.create', title: `Sub-interface ${sub} for ${other.label}`,
      params: clean({ parent: pr.router_port, vlan: pr.vlan, encap: 'vlan-type', address: pr.address, vpn: pr.vpn, description: desc }) }); added++; }
    if (pr.rate_kbps) for (const dir of ['outbound', 'inbound']) if (!has(`car-${dir}:` + l.id)) {
      P.tasks.push({ id: 't' + uid(), source: `car-${dir}:` + l.id, action: 'qos.car', title: `Limit ${other.label} to ${Fmt.bps(pr.rate_kbps * 1000)} (${dir === 'outbound' ? 'download' : 'upload'})`,
        params: { interface: sub, direction: dir, cir: pr.rate_kbps } }); added++; }
  }
  for (const n of P.diagram.nodes) if (n.props.bgp_as && n.props.peer_ip && !has('bgp:' + n.id)) {
    P.tasks.push({ id: 't' + uid(), source: 'bgp:' + n.id, action: 'bgp.peer_add', title: `BGP peer ${n.label}`,
      params: clean({ local_as: router && router.props.local_as || '', peer: n.props.peer_ip, remote_as: n.props.bgp_as, description: n.label }) }); added++; }
  if (added) { markDirty(); PD.preview = null; toast(`${added} task${added === 1 ? '' : 's'} added from the design. Review them on the Tasks tab.`); }
  else toast('No new tasks: give links from the NE8000 a port and VLAN (and optionally IP and rate), or resellers a BGP AS and peer IP.', true);
  const t = $('#plTabs [data-t=tasks]'); if (t) t.textContent = `Tasks (${P.tasks.length})`;
}
const clean = o => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== '' && v != null));

/* ----- tasks ----- */
async function renderTasks(host, c) {
  if (!PD.preview && !PD.dirty) PD.preview = (await api(`/api/plans/${P.id}/preview`)).tasks;
  const pv = Object.fromEntries((PD.preview || []).map(x => [x.id, x]));
  const res = PD.lastRun ? Object.fromEntries(PD.lastRun.results.map(r => [r.id, r])) : {};
  host.innerHTML = `<div class="card"><div class="bar"><button class="btn" id="tAdd">+ Task</button><span class="sp"></span>
      <button class="btn" id="tCheck" ${PD.dirty ? 'disabled title="save first"' : ''}>Check on router (no commit)</button><button class="btn primary" id="tExec" ${PD.dirty ? 'disabled title="save first"' : ''}>Execute &amp; commit</button></div>
    ${PD.dirty ? '<div class="banner">Save the plan to preview the commands and execute it.</div>' : ''}
    ${PD.lastRun ? `<div class="muted" style="font-size:12.5px;margin-bottom:8px">Results below are from the last ${PD.lastRun.commit ? 'commit' : 'check'} on ${esc(PD.lastRun.host)} at ${fmtDate(PD.lastRun.at)}: ${PD.lastRun.ok ? '<b style="color:var(--ok)">all tasks ok</b>' : '<b style="color:var(--bad)">failed</b>'}</div>` : ''}
    <div id="tList"></div></div>`;
  const list = $('#tList');
  list.innerHTML = P.tasks.length ? P.tasks.map((t, i) => { const p = pv[t.id], r = res[t.id];
    return `<div class="card" style="margin-bottom:10px;padding:12px 14px"><div class="bar" style="margin-bottom:6px"><b>${i + 1}. ${esc(t.title || taskLabel(t.action))}</b><span class="pill p-off">${esc(taskLabel(t.action))}</span>
      ${r ? `<span class="pill ${r.ok ? 'p-ok' : 'p-bad'}">${r.ok ? 'ok' : 'failed'}</span>` : ''}<span class="sp"></span>
      <button class="btn sm" data-up="${i}" ${i ? '' : 'disabled'}>↑</button><button class="btn sm" data-down="${i}" ${i < P.tasks.length - 1 ? '' : 'disabled'}>↓</button>
      <button class="btn sm" data-edit="${i}">Edit</button><button class="btn sm danger" data-del="${i}">Remove</button></div>
      ${p ? (p.ok ? `<pre>${esc(p.lines.join('\n'))}</pre>` : `<div class="err">${esc(p.error)}</div>`) : '<div class="faint" style="font-size:12px">save to preview commands</div>'}
      ${r && !r.ok ? `<div class="err">${esc(r.output)}</div>` : ''}</div>`; }).join('') : '<div class="muted">No tasks yet. Add them here, or draw links on the Design tab and press “Generate tasks from design”.</div>';
  const move = (i, d) => { const t = P.tasks.splice(i, 1)[0]; P.tasks.splice(i + d, 0, t); markDirty(); PD.preview = null; renderTasks(host, c); };
  list.querySelectorAll('[data-up]').forEach(b => b.onclick = () => move(+b.dataset.up, -1));
  list.querySelectorAll('[data-down]').forEach(b => b.onclick = () => move(+b.dataset.down, 1));
  list.querySelectorAll('[data-del]').forEach(b => b.onclick = () => { P.tasks.splice(+b.dataset.del, 1); markDirty(); PD.preview = null; renderTasks(host, c); $('#plTabs [data-t=tasks]').textContent = `Tasks (${P.tasks.length})`; });
  list.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => taskForm(P.tasks[+b.dataset.edit], host, c));
  $('#tAdd').onclick = () => taskForm(null, host, c);
  $('#tCheck').onclick = () => runPlan(false, c); $('#tExec').onclick = () => runPlan(true, c);
}
function taskForm(task, host, c) {
  const m = $('#modal'); let action = task ? task.action : 'subif.create';
  const draw = () => {
    const tt = TASK_TYPES[action]; const vals = task && task.action === action ? task.params : {};
    m.innerHTML = `<h2>${task ? 'Edit task' : 'New task'}</h2><div class="form"><label>Type</label><select id="tfType">${Object.entries(TASK_TYPES).map(([k, v]) => `<option value="${k}" ${k === action ? 'selected' : ''}>${v.label}</option>`).join('')}</select>
      <label>Title</label><input id="tfTitle" value="${esc(task ? task.title : '')}" placeholder="${esc(tt.label)}">
      ${tt.fields().map(f => field({ ...f, value: vals[f.k] ?? f.value })).join('')}${tt.note ? `<div class="help">${esc(tt.note)}</div>` : ''}</div>
      <div class="preview hidden"><div class="lbl">Commands:</div><pre class="cli"></pre></div><div class="err"></div>
      <div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn" data-a="p">Preview commands</button><button class="btn primary" data-a="s">${task ? 'Update task' : 'Add task'}</button></div>`;
    $('#tfType').onchange = () => { action = $('#tfType').value; draw(); };
  };
  draw(); $('#modalBg').classList.remove('hidden');
  const params = () => { const p = {}; m.querySelectorAll('.form [name]').forEach(el => { const v = el.type === 'checkbox' ? el.checked : el.value.trim(); if (v !== '' && v !== false) p[el.name] = v; }); return p; };
  m.onclick = async e => {
    const a = e.target.dataset.a; if (!a) return;
    if (a === 'c') { m.onclick = null; return closeModal(); }
    try { const r = await post('/api/action', { action, params: params(), preview: true });
      if (a === 'p') { m.querySelector('.cli').textContent = r.lines.join('\n'); m.querySelector('.preview').classList.remove('hidden'); m.querySelector('.err').textContent = ''; return; }
      const t = { id: task ? task.id : 't' + uid(), action, params: params(), title: $('#tfTitle').value.trim() || TASK_TYPES[action].label, source: task ? task.source : '' };
      if (task) P.tasks[P.tasks.indexOf(task)] = t; else P.tasks.push(t);
      m.onclick = null; closeModal(); markDirty(); PD.preview = null; renderTasks(host, c); $('#plTabs [data-t=tasks]').textContent = `Tasks (${P.tasks.length})`;
    } catch (err) { m.querySelector('.err').textContent = err.message; }
  };
}
async function runPlan(commit, c) {
  if (!P.tasks.length) return toast('No tasks to run', true);
  if (commit && !confirm(`Execute ${P.tasks.length} task(s) from "${P.name}" on ${$('#tName').textContent} and commit? It stops at the first failure.`)) return;
  toast(commit ? 'Executing plan…' : 'Checking plan on the router…');
  try { const r = await post(`/api/plans/${P.id}/execute`, { commit, author: editor() }); P = r.plan; PD.lastRun = r.run;
    toast(r.run.ok ? (commit ? 'Plan executed and committed' : 'Check passed: nothing was committed') : 'Plan stopped: a task failed (see the Tasks tab)', !r.run.ok); PAGES.plan(c, P.id); }
  catch (e) { toast(e.message, true); }
}

/* ----- record ----- */
function renderRecord(host) {
  const revs = P.revisions.map((r, i) => ({ ...r, i })).reverse();
  host.innerHTML = `<div class="grid"><div class="card"><h3>Version history</h3><div id="rRev"></div></div>
    <div class="card"><h3>Executions on the router</h3><div id="rRuns">${P.runs.length ? '' : '<div class="muted">Not executed yet.</div>'}</div></div></div>`;
  table($('#rRev'), [{ t: 'When', k: 'at', f: r => fmtDate(r.at) }, { t: 'By', k: 'by' }, { t: 'Change', k: 'note', cls: 'wrap' },
    { t: '', sort: false, cls: 'acts', f: r => r.content ? `<button class="btn sm" data-view="${r.i}">View</button><button class="btn sm" data-restore="${r.i}">Restore</button>` : '' }], revs, { filter: false });
  $('#rRev').onclick = async e => {
    const v = e.target.dataset.view, rs = e.target.dataset.restore;
    if (v != null) { const cont = P.revisions[+v].content; showOutput(`Version before ${fmtDate(P.revisions[+v].at)}`,
      `Name: ${cont.name}\nStatus: ${cont.status}\nDescription: ${cont.description}\n\nDevices (${cont.diagram.nodes.length}):\n${cont.diagram.nodes.map(n => ' - ' + n.label + ' [' + n.type + ']').join('\n')}\n\nLinks (${cont.diagram.links.length})\n\nTasks (${cont.tasks.length}):\n${cont.tasks.map((t, i) => ` ${i + 1}. ${t.title} [${t.action}]`).join('\n')}`); }
    if (rs != null) { if (PD.dirty && !confirm('Discard unsaved changes and restore this version?')) return; if (!confirm('Restore this version? The current version stays in the history.')) return;
      P = await post(`/api/plans/${P.id}/restore`, { index: +rs, author: editor() }); PD.dirty = false; PD.preview = null; toast('Version restored'); PAGES.plan($('#content'), P.id); }
  };
  $('#rRuns').innerHTML += [...P.runs].reverse().map(r => `<details class="run"><summary><span class="pill ${r.ok ? 'p-ok' : 'p-bad'}">${r.ok ? 'ok' : 'failed'}</span><b>${r.commit ? 'Execute & commit' : 'Check only'}</b>
      <span class="muted">${fmtDate(r.at)} · ${esc(r.host)}${r.demo ? ' (demo)' : ''}${r.by ? ' · ' + esc(r.by) : ''}</span>${r.skipped ? `<span class="faint">${r.skipped} skipped</span>` : ''}</summary>
      <div class="body">${r.results.map((x, i) => `<div class="task-res"><div class="t">${i + 1}. ${esc(x.title)} <span class="pill ${x.ok ? 'p-ok' : 'p-bad'}">${x.ok ? 'ok' : 'failed'}</span></div><pre>${esc(x.output || x.lines.join('\n'))}</pre></div>`).join('')}</div></details>`).join('');
}
async function printRecord() {
  const w = window.open('', '_blank'); if (!w) return toast('Allow pop-ups to print the record', true);
  if (!PD.preview && !PD.dirty) try { PD.preview = (await api(`/api/plans/${P.id}/preview`)).tasks; } catch (e) {}
  let svg = $('#dSvg') ? $('#dSvg').outerHTML : '';
  if (!svg) { const tmp = document.createElement('div'); tmp.id = 'dCanvas'; tmp.style.cssText = 'position:absolute;left:-99999px;top:0'; document.body.appendChild(tmp);
    drawCanvas(); svg = tmp.querySelector('svg') ? tmp.querySelector('svg').outerHTML : ''; tmp.remove(); }
  const st = getComputedStyle(document.documentElement); const vars = ['--accent', '--s1', '--s2', '--s3', '--muted', '--faint', '--grid', '--panel', '--text'].map(v => `${v}:${st.getPropertyValue(v)}`).join(';');
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${esc(P.name)} – network plan record</title><style>:root{${vars};--panel:#fff;--text:#111;--muted:#555;--grid:#eee}
    body{font:13px system-ui,sans-serif;margin:24px;color:#111}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
    pre{background:#f5f5f5;padding:8px;border-radius:4px;white-space:pre-wrap;font:11.5px ui-monospace,monospace}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}
    .node rect{fill:#fff;stroke-width:2}.node text{font:12px system-ui;fill:#111}.node .sub{fill:#666;font-size:11px}.node .tag{font-size:10px;font-weight:700}.link line.vis{stroke:#999;stroke-width:2}.link .hit{display:none}.link text{font:11px system-ui;fill:#555}
    svg{max-width:100%;height:auto;border:1px solid #ddd}</style></head><body>
    <h1>${esc(P.name)}</h1><div>Status: <b>${esc(P.status)}</b> · created ${fmtDate(P.created)}${P.author ? ' by ' + esc(P.author) : ''} · last saved ${fmtDate(P.updated)} · printed ${fmtDate(Date.now() / 1000)}</div><p>${esc(P.description)}</p>
    <h2>Design</h2>${svg || '<p>(no drawing)</p>'}
    <table><tr><th>Device</th><th>Type</th><th>Details</th></tr>${P.diagram.nodes.map(n => `<tr><td>${esc(n.label)}</td><td>${esc(NODE_META[n.type].label)}</td><td>${esc(Object.entries(n.props).filter(([, v]) => v).map(([k, v]) => k + ': ' + v).join(', '))}</td></tr>`).join('')}</table>
    <h2>Tasks (${P.tasks.length})</h2>${(PD.preview || []).map((p, i) => `<div><b>${i + 1}. ${esc(P.tasks[i] ? P.tasks[i].title : '')}</b><pre>${esc(p.ok ? p.lines.join('\n') : p.error)}</pre></div>`).join('') || P.tasks.map((t, i) => `<div>${i + 1}. ${esc(t.title)}</div>`).join('')}
    <h2>Executions</h2>${P.runs.map(r => `<div><b>${fmtDate(r.at)} · ${r.commit ? 'execute & commit' : 'check only'} on ${esc(r.host)}${r.demo ? ' (demo)' : ''} · ${r.ok ? 'OK' : 'FAILED'}${r.by ? ' · ' + esc(r.by) : ''}</b>${r.results.map(x => `<div>${x.ok ? '✓' : '✗'} ${esc(x.title)}</div>`).join('')}</div>`).join('') || '<p>Not executed.</p>'}
    <h2>Version history</h2><table><tr><th>When</th><th>By</th><th>Change</th></tr>${[...P.revisions].reverse().map(r => `<tr><td>${fmtDate(r.at)}</td><td>${esc(r.by || '')}</td><td>${esc(r.note)}</td></tr>`).join('')}</table>
    <script>setTimeout(()=>print(),300)<\/script></body></html>`);
  w.document.close();
}
