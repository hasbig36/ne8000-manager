/* IP plan: the address blocks, split into segments by purpose and region, the addresses booked from them, and what is
   on the router now. "I need addresses" books the next free, aligned subnet of the size asked for, in the right
   segment, and can put it on an interface (through the usual Review and commit). The advice says what could be better
   in the addresses as they are, and a renumbering plan shows where each would go; nothing changes by itself. */
const IPM = { d: null, pick: null };
const IPM_COLORS = { infra: '#8b5cf6', p2p: '#0ea5e9', upstream: '#f59e0b', customers: '#10b981', mgmt: '#64748b', reserved: '#a3a3a3' };
const IPM_SIZES = [[32, '1 address (a loopback)'], [31, '/31: 2 addresses, a point-to-point link'], [30, '/30: 2 usable, a link (old style)'], [29, '/29: 6 usable'],
  [28, '/28: 14 usable'], [27, '/27: 30 usable'], [26, '/26: 62 usable'], [25, '/25: 126 usable'], [24, '/24: 254 usable']];
const ip2n = s => s.split('.').reduce((a, x) => a * 256 + +x, 0);
const n2ip = n => [24, 16, 8, 0].map(k => Math.floor(n / 2 ** k) % 256).join('.');
const cidr = s => { const [a, l] = String(s).split('/'); const len = +l, size = 2 ** (32 - len), lo = Math.floor(ip2n(a) / size) * size; return { lo, hi: lo + size - 1, len, size }; };
const inC = (s, b) => { const x = cidr(s), y = cidr(b); return x.lo >= y.lo && x.hi <= y.hi; };
const ovC = (s, b) => { const x = cidr(s), y = cidr(b); return x.lo <= y.hi && y.lo <= x.hi; };
// the town a description names (for grouping by region)
const ipmTown = desc => { const u = String(desc || '').toUpperCase(); return Object.keys(typeof KE_TOWNS === 'object' ? KE_TOWNS : {}).find(t => u.includes(t)) || ''; };

PAGES.ipplan = async c => {
  c.innerHTML = head('IP plan', '', `<button class="btn" id="ipBlk">+ Block</button><button class="btn" id="ipSeg">+ Segment</button><button class="btn" id="ipLay">Suggest a layout</button>`) +
    `<div id="ipBody"><div class="card muted">Loading…</div></div>`;
  $('#ipBlk').onclick = () => ipmBlock(); $('#ipSeg').onclick = () => ipmSegment(); $('#ipLay').onclick = () => ipmLayout();
  await ipmLoad();
};
async function ipmLoad() { IPM.d = await api('/api/ipam'); ipmDraw(); }
async function ipmSave(blocks, pools, msg) {
  await post('/api/ipam', { blocks, pools }); toast(msg || 'IP plan saved'); await ipmLoad();
}
function ipmDraw() {
  const el = $('#ipBody'); if (!el || !IPM.d) return;
  const { plan, used, stats, findings, suggested, purposes } = IPM.d;
  const poolOf = s => plan.pools.find(p => inC(s, p.cidr));
  const reg = ipmRegionAdvice();
  el.innerHTML = `${suggested && plan.blocks.length ? `<div class="banner" style="margin-bottom:12px">Found ${plan.blocks.map(b => `<b>${esc(b.cidr)}</b>`).join(', ')} on the router. <button class="btn sm primary" id="ipKeep">Keep it as my block</button> or add your blocks with <b>+ Block</b>.</div>` : ''}
    ${!plan.blocks.length ? `<div class="card"><p>No public addresses found on the router yet. Add the blocks you were given (from AFRINIC or your upstream) with <b>+ Block</b>.</p></div>` : ''}
    <div class="grid g2">${stats.map(st => ipmBlockCard(st, plan)).join('')}</div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><h3>I need addresses</h3>
        <div class="form"><label>How many</label><select id="ipSz">${IPM_SIZES.map(([l, t]) => `<option value="${l}" ${l === 31 ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
          <label>From</label><select id="ipWh"></select>
          <label>For</label><input id="ipNm" placeholder="customer, site or link name, e.g. MARSABIT-BTS-02">
          <label>Note</label><input id="ipNo" placeholder="optional: circuit ID, ticket…">
          <label>On the router</label><select id="ipDo"><option value="">Book only</option><option value="sub">Configure a new sub-interface</option><option value="if">Add to an existing interface</option></select>
          <label class="ipD sub">Port</label><select class="ipD sub" id="ipPt"></select>
          <label class="ipD sub">VLAN</label><input class="ipD sub" id="ipVl" type="number" min="1" max="4094" placeholder="e.g. 310">
          <label class="ipD if">Interface</label><select class="ipD if" id="ipIf"></select>
          <label class="ipD sub">VPN instance</label><input class="ipD sub" id="ipVp" placeholder="optional"></div>
        <div class="row" style="margin-top:8px"><button class="btn primary" id="ipGo">Find and book</button><span class="sp"></span></div>
        <div class="faint ipD sub if" style="font-size:11.5px;margin-top:4px">Books the addresses, then shows the commands for the router: press Apply &amp; commit to put them on.</div><div class="err" id="ipErr"></div><div id="ipRes"></div></div>
      <div class="card"><h3>Best practice for the addresses you have</h3>
        ${findings.length || reg ? `<div class="wlist">${findings.concat(reg ? [reg] : []).map(f => `<div class="witem ${f.severity === 'critical' ? 'crit' : f.severity === 'info' ? 'info' : 'warn'}"><b>${esc(f.title)}</b><div>${esc(f.detail)}</div><div class="muted">${esc(f.advice)}</div></div>`).join('')}</div>` : '<p class="okc">Nothing to improve: no overlaps, no waste found.</p>'}
        <div class="muted" style="font-size:12.5px;margin-top:6px"><b>How to arrange them:</b> keep loopbacks together in one small range; give point-to-point links /31s from one segment; keep upstreams and IXP in their own; give each region its own aligned segment, so its routes summarise and new customers land next to their neighbours; keep about a quarter back for growth. Book every address here before it goes on the router.</div>
        <button class="btn sm" id="ipRen" style="margin-top:8px" ${plan.pools.length ? '' : 'disabled title="split the block into segments first"'}>Renumbering plan</button><div id="ipRenR"></div></div></div>
    <div class="card" style="margin-top:14px"><h3>Segments</h3>${plan.pools.length ? `<table class="tbl"><thead><tr><th>Segment</th><th>Range</th><th>Purpose</th><th>Region</th><th>In use</th><th></th></tr></thead><tbody>${plan.pools.map(p => {
      const n = cidr(p.cidr).size, u = used.filter(x => inC(x.net, p.cidr)).reduce((a, x) => a + cidr(x.net).size, 0) + plan.allocs.filter(a => inC(a.cidr, p.cidr) && !used.some(x => ovC(x.net, a.cidr))).reduce((a, x) => a + cidr(x.cidr).size, 0);
      return `<tr><td><i class="swsw" style="background:${IPM_COLORS[p.purpose]}"></i><b>${esc(p.name)}</b></td><td class="mono">${esc(p.cidr)}</td><td>${esc(purposes[p.purpose] || p.purpose)}</td><td>${esc(p.region || '')}</td>
        <td>${u} / ${n} <span class="faint">(${Math.round(u * 100 / n)}%)</span></td><td class="acts"><button class="btn sm" data-pe="${esc(p.id)}">Edit</button><button class="btn sm danger" data-pd="${esc(p.id)}">Delete</button></td></tr>`; }).join('')}</tbody></table>`
      : '<p class="muted">The blocks are not split yet. <b>Suggest a layout</b> splits one the usual way (loopbacks, links, upstreams, management, one segment per region, a reserve); or add segments one by one.</p>'}</div>
    <div class="card" style="margin-top:14px"><h3>Booked</h3>${plan.allocs.length ? `<table class="tbl"><thead><tr><th>Subnet</th><th>For</th><th>Segment</th><th>Status</th><th>Booked</th><th></th></tr></thead><tbody>${plan.allocs.slice().reverse().map(a => {
      const on = used.find(x => ovC(x.net, a.cidr));
      return `<tr><td class="mono"><b>${esc(a.cidr)}</b></td><td>${esc(a.name)}${a.note ? `<div class="faint" style="font-size:11.5px">${esc(a.note)}</div>` : ''}</td><td>${esc((plan.pools.find(p => p.id === a.pool) || {}).name || '-')}</td>
        <td>${on ? `<span class="okc">on ${esc(on.iface)}</span>` : '<span class="faint">not on the router yet</span>'}</td><td class="faint">${esc(a.by || '')} ${esc(ago(a.at))}</td><td class="acts"><button class="btn sm danger" data-af="${esc(a.id)}">Free</button></td></tr>`; }).join('')}</tbody></table>` : '<p class="muted">Nothing booked yet.</p>'}</div>
    <div class="card" style="margin-top:14px"><h3>On the router now</h3><div id="ipUsed"></div></div>`;
  // what to hand out from: the segments (the right one first for the size), then each block
  const fillWhere = () => { const l = +$('#ipSz').value, want = l >= 31 ? (l === 32 ? 'infra' : 'p2p') : 'customers';
    const opts = plan.pools.filter(p => p.purpose !== 'reserved').sort((a, b) => (b.purpose === want) - (a.purpose === want)).map(p => [p.id, `${p.name} (${p.cidr})`]).concat(plan.blocks.map(b => [b.cidr, `${b.cidr} ${b.name ? '· ' + b.name : ''} (outside the segments)`]));
    $('#ipWh').innerHTML = opts.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join(''); };
  fillWhere(); $('#ipSz').onchange = fillWhere;
  $('#ipGo').onclick = ipmAllocate;
  const doSel = () => { const v = $('#ipDo').value; el.querySelectorAll('.ipD').forEach(x => x.style.display = v && x.classList.contains(v) ? '' : 'none'); $('#ipGo').textContent = v ? 'Book and configure' : 'Find and book'; };
  $('#ipDo').onchange = doSel; doSel();
  // the next free VLAN on the port (from its sub-interfaces), and a used one refused before anything is booked
  const vlans = () => new Set(ifCache.filter(i => i.name.startsWith($('#ipPt').value + '.')).map(i => +i.name.split('.').pop()).concat(ifCache.filter(i => i.name.startsWith($('#ipPt').value + '.') && i.vlan).map(i => +i.vlan)));
  const nextVlan = () => { const u = vlans(); let v = Math.max(100, ...[...u].filter(x => x < 4000)) + 1; if (!u.size) v = 100; while (u.has(v)) v++; $('#ipVl').value = v; $('#ipVl').title = u.size ? 'in use on this port: ' + [...u].sort((a, b) => a - b).join(', ') : ''; };
  IPM.vlans = vlans;
  ensureIfs().then(() => { if (!$('#ipPt')) return; $('#ipPt').innerHTML = parentList().map(n => `<option>${esc(n)}</option>`).join('');
    $('#ipPt').onchange = nextVlan; nextVlan();
    $('#ipIf').innerHTML = ifCache.filter(i => i.type !== 'null' && !i.l2).map(i => `<option value="${esc(i.name)}">${esc(i.name)}${i.description ? ' · ' + esc(i.description) : ''}</option>`).join(''); }).catch(() => {});
  if ($('#ipKeep')) $('#ipKeep').onclick = () => ipmSave(plan.blocks, plan.pools, 'Block kept in the plan');
  el.querySelectorAll('[data-pe]').forEach(b => b.onclick = () => ipmSegment(plan.pools.find(p => p.id === b.dataset.pe)));
  el.querySelectorAll('[data-pd]').forEach(b => b.onclick = () => { const p = plan.pools.find(x => x.id === b.dataset.pd); if (confirm(`Delete the segment ${p.name} (${p.cidr})? The addresses in it stay as they are.`)) ipmSave(plan.blocks, plan.pools.filter(x => x !== p), 'Segment deleted'); });
  el.querySelectorAll('[data-af]').forEach(b => b.onclick = async () => { if (!confirm('Free this booking? Nothing changes on the router.')) return; await api('/api/ipam/allocs/' + b.dataset.af, { method: 'DELETE' }); toast('Booking freed'); ipmLoad(); });
  el.querySelectorAll('[data-bd]').forEach(b => b.onclick = () => { const x = plan.blocks.find(y => y.cidr === b.dataset.bd); if (confirm(`Remove the block ${x.cidr} and its segments from the plan? Nothing changes on the router.`)) ipmSave(plan.blocks.filter(y => y !== x), plan.pools.filter(p => !inC(p.cidr, x.cidr)), 'Block removed'); });
  el.querySelectorAll('.ipgrid').forEach(g => g.onclick = e => { const c = e.target.closest('[data-c]'); if (c) ipmCell(g.dataset.b, +c.dataset.c); });
  $('#ipRen').onclick = ipmRenumber;
  table($('#ipUsed'), [{ t: 'Subnet', k: 'net', cls: 'mono', f: r => `<b>${esc(r.net)}</b>` }, { t: 'Address', k: 'ip', cls: 'mono' }, { t: 'Interface', k: 'iface' }, { t: 'Description', k: 'desc' },
    { t: 'Segment', k: 'seg' }, { t: 'Region', k: 'town' }],
    used.map(u => ({ ...u, seg: (poolOf(u.net) || {}).name || '', town: ipmTown(u.desc) })));
}
// one block: its numbers, and a map of it (each square a few addresses), coloured by segment and use
function ipmBlockCard(st, plan) {
  const b = cidr(st.cidr), cellLen = Math.min(32, b.len + 8), cell = 2 ** (32 - cellLen), n = b.size / cell, used = IPM.d.used;
  const cells = [];
  for (let i = 0; i < n; i++) {
    const lo = b.lo + i * cell, s = `${n2ip(lo)}/${cellLen}`, pool = plan.pools.find(p => inC(s, p.cidr) || ovC(s, p.cidr));
    const u = used.find(x => ovC(x.net, s)), a = !u && plan.allocs.find(x => ovC(x.cidr, s)), col = pool ? IPM_COLORS[pool.purpose] : '#3b82f6';
    cells.push(`<i data-c="${i}" class="${u ? 'u' : a ? 'a' : 'f'}" style="${u ? `background:${col}` : a ? `background:repeating-linear-gradient(45deg,${col}55 0 3px,transparent 3px 6px);border-color:${col}` : pool ? `border-color:${col}66` : ''}"
      title="${esc(n2ip(lo))}${cell > 1 ? '/' + cellLen : ''}${pool ? ' · ' + pool.name : ''}${u ? ' · ' + u.iface + ' ' + u.desc : a ? ' · booked: ' + a.name : ' · free'}"></i>`);
  }
  const pct = v => (v * 100 / st.size).toFixed(1);
  const name = (plan.blocks.find(x => x.cidr === st.cidr) || {}).name;
  return `<div class="card"><h3 class="row"><span>${esc(st.cidr)} ${name ? `<span class="muted" style="font-weight:500">${esc(name)}</span>` : ''}</span><span class="sp"></span><button class="btn sm danger" data-bd="${esc(st.cidr)}" title="not our space? take it off the plan">Remove</button></h3>
    <div class="ipbar"><i style="width:${pct(st.used)}%;background:#3b82f6"></i><i style="width:${pct(st.booked)}%;background:repeating-linear-gradient(45deg,#3b82f6aa 0 3px,transparent 3px 6px)"></i></div>
    <div style="font-size:12.5px;margin:6px 0 10px;display:flex;flex-wrap:wrap;gap:6px 16px"><span><b>${st.used}</b> in use</span><span><b>${st.booked}</b> booked</span><span class="okc"><b>${st.free}</b> free of ${st.size}</span>
      <span class="faint">${st.gaps.length} free gap${st.gaps.length === 1 ? '' : 's'}${st.gaps.length ? `, largest ${st.gaps[0].size} at ${esc(st.gaps[0].from)}` : ''}</span></div>
    <div class="ipgrid" data-b="${esc(st.cidr)}" style="grid-template-columns:repeat(${Math.min(32, n)},1fr)">${cells.join('')}</div>
    <div class="faint" style="font-size:11.5px;margin-top:6px">Each square is ${cell === 1 ? 'one address' : cell + ' addresses'}. Solid: on the router · striped: booked · outlined: free in a segment. Click a square.</div>
    <div class="legend" style="margin-top:4px">${Object.entries(IPM_COLORS).map(([k, c]) => `<span><i style="background:${c}"></i>${esc(IPM.d.purposes[k])}</span>`).join('')}</div><div class="ipcell" id="ipC_${st.cidr.replace(/\W/g, '_')}"></div></div>`;
}
function ipmCell(block, i) {
  const b = cidr(block), cellLen = Math.min(32, b.len + 8), cell = 2 ** (32 - cellLen), lo = b.lo + i * cell, s = `${n2ip(lo)}/${cellLen}`;
  const { plan, used } = IPM.d, u = used.filter(x => ovC(x.net, s)), a = plan.allocs.filter(x => ovC(x.cidr, s)), p = plan.pools.find(x => ovC(s, x.cidr));
  const box = $('#ipC_' + block.replace(/\W/g, '_')); if (!box) return;
  box.innerHTML = `<div class="witem info" style="margin-top:8px"><b class="mono">${esc(n2ip(lo))}${cell > 1 ? ' – ' + esc(n2ip(lo + cell - 1)) : ''}</b>${p ? ` · ${esc(p.name)} (${esc(p.cidr)})` : ' · in no segment'}
    ${u.map(x => `<div>On the router: <b class="mono">${esc(x.net)}</b> on ${esc(x.iface)} ${esc(x.desc)}</div>`).join('')}${a.map(x => `<div>Booked: <b class="mono">${esc(x.cidr)}</b> for ${esc(x.name)}</div>`).join('')}${!u.length && !a.length ? '<div>Free.</div>' : ''}</div>`;
}
async function ipmAllocate() {
  const err = $('#ipErr'); err.textContent = '';
  try {
    const how = $('#ipDo').value, port = $('#ipPt').value, vlan = $('#ipVl').value.trim(), itf = $('#ipIf').value, vpn = $('#ipVp').value.trim();
    if (how === 'sub' && (!port || !/^\d+$/.test(vlan) || +vlan < 1 || +vlan > 4094)) throw new Error('Pick the port and a VLAN (1-4094) for the sub-interface.');
    if (how === 'sub' && IPM.vlans && IPM.vlans().has(+vlan)) throw new Error(`VLAN ${vlan} is already used on ${port}: pick another.`);
    if (how === 'if' && !itf) throw new Error('Pick the interface.');
    const r = await post('/api/ipam/allocate', { where: $('#ipWh').value, plen: +$('#ipSz').value, name: $('#ipNm').value, note: [$('#ipNo').value, how === 'sub' ? `${port}.${vlan}` : how === 'if' ? itf : ''].filter(Boolean).join(' · ') });
    const a = r.alloc, l = cidr(a.cidr).len, h = r.hosts;
    await ipmLoad();
    $('#ipRes').innerHTML = `<div class="witem info" style="margin-top:10px"><b>Booked ${esc(a.cidr)}</b> for ${esc(a.name)}.
      <div>${h.length > 1 ? `Router: <b class="mono">${esc(h[0])}/${l}</b> · far end: <b class="mono">${esc(h[1])}</b>${l < 30 ? ` · usable ${esc(h[0])} – ${esc(n2ip(cidr(a.cidr).hi - 1))}` : ''}` : `Address: <b class="mono">${esc(h[0])}/32</b>`}</div>
      <div class="row" style="gap:6px;margin-top:6px"><button class="btn sm" id="ipSub">Put it on a new sub-interface…</button><button class="btn sm" id="ipAdd">Add it to an interface…</button></div>
      <div class="faint" style="font-size:11.5px">Both open the usual form: you review the commands before anything is sent to the router.</div></div>`;
    const addr = `${h[0]}/${l}`;
    $('#ipSub').onclick = () => subifForm(undefined, { address: addr, description: a.name });
    $('#ipAdd').onclick = async () => { await ensureIfs(); addrForm({ address: addr }); };
    // configure it now: the usual change form, filled in; Review shows the commands, Apply & commit puts them on the router
    if (how === 'sub') subifForm(port, { address: addr, description: a.name, vlan, vpn });
    else if (how === 'if') { await ensureIfs(); addrForm({ address: addr, interface: itf, secondary: !!(ifCache.find(i => i.name === itf) || {}).ip }); }
  } catch (e) { err.textContent = e.message; }
}
function ipmBlock() {
  const m = $('#modal'), plan = IPM.d.plan;
  m.innerHTML = `<h2>Add a block</h2><p class="muted">An address block you were given (AFRINIC, an upstream) or a private range you use.</p>
    <div class="form"><label>Block</label><input id="ibC" placeholder="102.216.116.0/23"><label>Name</label><input id="ibN" placeholder="AFRINIC allocation"></div><div class="err"></div>
    <div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Add</button></div>`;
  $('#modalBg').classList.remove('hidden');
  m.querySelector('.actions').onclick = async e => { if (e.target.dataset.a === 'c') return closeModal(); if (e.target.dataset.a !== 's') return;
    try { await ipmSave(plan.blocks.concat([{ cidr: $('#ibC').value.trim(), name: $('#ibN').value }]), plan.pools, 'Block added'); closeModal(); } catch (err) { m.querySelector('.err').textContent = err.message; } };
}
// add or change a segment: by size (the next free range of that size is found) or a range typed in
function ipmSegment(seg) {
  const m = $('#modal'), plan = IPM.d.plan, purposes = IPM.d.purposes;
  if (!plan.blocks.length) return toast('Add a block first', true);
  const opt = (v, t, s) => `<option value="${esc(v)}" ${v === s ? 'selected' : ''}>${esc(t)}</option>`;
  m.innerHTML = `<h2>${seg ? 'Change the segment' : 'New segment'}</h2><p class="muted">A part of a block kept for one purpose or region. Only the plan changes.</p>
    <div class="form"><label>Name</label><input id="isN" value="${esc(seg ? seg.name : '')}" placeholder="Customers – Marsabit">
      <label>Purpose</label><select id="isP">${Object.entries(purposes).map(([k, t]) => opt(k, t, seg ? seg.purpose : 'customers')).join('')}</select>
      <label>Region</label><input id="isR" value="${esc(seg ? seg.region : '')}" placeholder="Marsabit" list="isRL"><datalist id="isRL">${Object.keys(KE_TOWNS).sort().map(t => `<option value="${esc(t)}">`).join('')}</datalist>
      <label>Block</label><select id="isB">${plan.blocks.map(b => opt(b.cidr, b.cidr, seg ? (plan.blocks.find(x => inC(seg.cidr, x.cidr)) || {}).cidr : '')).join('')}</select>
      <label>Size</label><select id="isS">${opt('', seg ? 'keep ' + seg.cidr : 'pick a size', '')}${[22, 23, 24, 25, 26, 27, 28, 29].map(l => opt(String(l), `/${l} (${2 ** (32 - l)} addresses)`, '')).join('')}</select>
      <label>or the range</label><input id="isC" value="${esc(seg ? seg.cidr : '')}" placeholder="102.216.117.0/25"></div><div class="err"></div>
    <div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Save</button></div>`;
  $('#modalBg').classList.remove('hidden');
  // a size picked: the first free range of that size in the block, clear of the other segments
  $('#isS').onchange = () => { const l = +$('#isS').value; if (!l) return; const b = cidr($('#isB').value), size = 2 ** (32 - l);
    for (let lo = b.lo; lo + size - 1 <= b.hi; lo += size) { const s = `${n2ip(lo)}/${l}`; if (!plan.pools.some(p => p !== seg && ovC(s, p.cidr))) { $('#isC').value = s; return; } }
    m.querySelector('.err').textContent = `No free /${l} left in that block outside the other segments.`; };
  m.querySelector('.actions').onclick = async e => { if (e.target.dataset.a === 'c') return closeModal(); if (e.target.dataset.a !== 's') return;
    const row = { id: seg ? seg.id : '', name: $('#isN').value, purpose: $('#isP').value, region: $('#isR').value, cidr: $('#isC').value.trim() };
    try { await ipmSave(plan.blocks, plan.pools.filter(p => p !== seg).concat([row]), seg ? 'Segment changed' : 'Segment added'); closeModal(); } catch (err) { m.querySelector('.err').textContent = err.message; } };
}
// the towns the router's addresses serve (from the interface descriptions): one customer segment each
const ipmRegions = () => [...new Set(IPM.d.used.map(u => ipmTown(u.desc)).filter(Boolean))].map(t => t[0] + t.slice(1).toLowerCase());
async function ipmLayout() {
  const m = $('#modal'), plan = IPM.d.plan;
  if (!plan.blocks.length) return toast('Add a block first', true);
  m.innerHTML = `<h2>Suggest a layout</h2><p class="muted">Splits a block the usual way: loopbacks first, links as /31, upstreams and IXP, management, one segment per region, and a quarter kept for growth. Each piece is aligned. Nothing on the router changes; addresses already in use stay where they are (the renumbering plan shows where they would go).</p>
    <div class="form"><label>Block</label><select id="ilB">${plan.blocks.map(b => `<option>${esc(b.cidr)}</option>`).join('')}</select>
      <label>Regions</label><input id="ilR" value="${esc(ipmRegions().join(', '))}" placeholder="Marsabit, Moyale, Nairobi"><div class="help">One customer segment each, from the towns in your interface descriptions. Change as you like.</div></div>
    <div id="ilP" style="margin-top:8px"></div><div class="err"></div>
    <div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn" data-a="p">Preview</button><button class="btn primary" data-a="s" disabled>Use this layout</button></div>`;
  $('#modalBg').classList.remove('hidden'); m.classList.add('wide');
  let pools = null;
  const preview = async () => { try { m.querySelector('.err').textContent = '';
      pools = (await post('/api/ipam/layout', { block: $('#ilB').value, regions: $('#ilR').value.split(',').map(x => x.trim()).filter(Boolean) })).pools;
      $('#ilP').innerHTML = `<table class="tbl"><tbody>${pools.map(p => `<tr><td><i class="swsw" style="background:${IPM_COLORS[p.purpose]}"></i>${esc(p.name)}</td><td class="mono">${esc(p.cidr)}</td><td>${cidr(p.cidr).size} addresses</td></tr>`).join('')}</tbody></table>`;
      m.querySelector('[data-a=s]').disabled = false; } catch (err) { m.querySelector('.err').textContent = err.message; } };
  $('#ilB').onchange = preview; preview();
  m.querySelector('.actions').onclick = async e => { const a = e.target.dataset.a; if (a === 'c') return closeModal(); if (a === 'p') return preview(); if (a !== 's' || !pools) return;
    const blk = $('#ilB').value, old = plan.pools.filter(p => inC(p.cidr, blk));
    if (old.length && !confirm(`Replace the ${old.length} segments already in ${blk}?`)) return;
    try { await ipmSave(plan.blocks, plan.pools.filter(p => !inC(p.cidr, blk)).concat(pools), 'Layout saved'); closeModal(); } catch (err) { m.querySelector('.err').textContent = err.message; } };
}
// regions mixed together: the subnets of one town spread over the block, between other towns' subnets
function ipmRegionAdvice() {
  const { used, plan } = IPM.d, rows = used.filter(u => plan.blocks.some(b => inC(u.net, b.cidr)) && ipmTown(u.desc)).sort((a, b) => cidr(a.net).lo - cidr(b.net).lo);
  const towns = rows.map(u => ipmTown(u.desc)), split = [...new Set(towns)].filter(t => { const at = towns.map((x, i) => x === t ? i : -1).filter(i => i >= 0); return at.length > 1 && at[at.length - 1] - at[0] + 1 > at.length; });
  if (!split.length) return null;
  return { severity: 'warning', title: `Regions are mixed together (${split.length})`, detail: `${split.slice(0, 6).map(t => t[0] + t.slice(1).toLowerCase()).join(', ')}: their subnets are spread across the block, between other regions'.`,
    advice: 'Give each region its own aligned segment and hand out its new addresses from there; the routes for a region then summarise into one, and a problem shows up in one place.' };
}
// where each address would go in the plan: a list for planned windows, nothing changes by itself
async function ipmRenumber() {
  const { used, plan } = IPM.d, moves = {};
  used.filter(u => plan.blocks.some(b => inC(u.net, b.cidr))).forEach(u => {
    const t = ipmTown(u.desc), up = /upstream|transit|ixp|kixp|peer|isp/i.test(u.desc);
    const target = u.kind === 'loopback' ? plan.pools.find(p => p.purpose === 'infra') : up ? plan.pools.find(p => p.purpose === 'upstream')
      : t ? plan.pools.find(p => p.purpose === 'customers' && p.region.toUpperCase() === t) : cidr(u.net).len >= 30 ? plan.pools.find(p => p.purpose === 'p2p') : null;
    if (target && !inC(u.net, target.cidr)) moves[u.net] = target.id;
  });
  const box = $('#ipRenR');
  if (!Object.keys(moves).length) { box.innerHTML = '<p class="okc" style="margin-top:8px">Every address is already in its segment (or has no clear one).</p>'; return; }
  const r = await post('/api/ipam/renumber', { moves });
  box.innerHTML = `<p class="muted" style="font-size:12.5px;margin-top:8px">A plan only: nothing is booked or changed. Move one at a time in a maintenance window: add the new address as a secondary, move the far end, then remove the old.</p>
    <table class="tbl"><thead><tr><th>Now</th><th></th><th>Would be</th><th>Segment</th></tr></thead><tbody>${r.moves.map(x => { const u = used.find(y => y.net === x.from) || {};
      return `<tr><td class="mono">${esc(x.from)}<div class="faint" style="font-size:11px">${esc(u.iface || '')} ${esc(u.desc || '')}</div></td><td>→</td><td class="mono">${x.to ? `<b>${esc(x.to)}</b>` : '<span class="warnc">no room</span>'}</td><td>${esc(x.pool)}</td></tr>`; }).join('')}</tbody></table>`;
}
