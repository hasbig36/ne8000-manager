/* Sites: every place the network is built in, fully registered (where it is, who to call, how to get in, the power),
   and what is watched there: the devices, the power, access control and CCTV. Items with an address are pinged every
   minute; power, door and camera systems post their alarms to /api/site-events. A device can only be added at a
   site that is fully registered. */
const SR = { d: null, open: null };
const SR_ICON = { power: '⚡', access: '🔐', cctv: '📹', device: '🖧', env: '🌡' };
const SR_LABEL = { power: 'Power', access: 'Access control', cctv: 'CCTV', device: 'Devices', env: 'Environment' };
const SR_FIELDS = [['name', 'Name', 'MARSABIT POP'], ['code', 'Site code', 'MBT-01'], ['town', 'Town', 'MARSABIT'], ['address', 'Address and directions', 'Plot 12, behind the county offices; 2nd floor'],
  ['contact_name', 'Site contact / caretaker', 'John Doe'], ['contact_phone', 'Contact phone', '+254 7…'], ['access', 'How to get in', 'Keys with the caretaker; gate code; call before going']];
async function srLoad() { SR.d = await api('/api/siteregistry'); return SR.d; }
// the fully registered sites, for the pickers where a device is added
async function srComplete() { const d = await srLoad().catch(() => SR.d || { sites: [] }); return (d.sites || []).filter(s => !s.missing.length); }
const srPicker = (sites, sel, id) => `<select id="${id}"><option value="">${sites.length ? 'pick the site' : 'no fully registered site yet (Sites)'}</option>${sites.map(s => `<option value="${esc(s.id)}" ${s.id === sel ? 'selected' : ''}>${esc(s.code)} · ${esc(s.name)}</option>`).join('')}</select>`;

PAGES.sites = async c => {
  c.innerHTML = head('Sites', 'power, access, CCTV and devices at each site', `<button class="btn primary" id="srAdd">+ Site</button>`) + '<div id="srBody"><div class="card muted">Loading…</div></div>';
  $('#srAdd').onclick = () => srForm(null);
  SR.open = null; await srLoad(); srDraw();
  every(30000, async () => { await srLoad(); if (!document.querySelector('#modalBg:not(.hidden)')) srDraw(); });
};
function srDraw() {
  const el = $('#srBody'); if (!el || !SR.d) return;
  const sites = SR.d.sites, s = sites.find(x => x.id === SR.open);
  if (s) return srSite(el, s);
  const bad = sites.filter(x => x.status !== 'ok'), part = sites.filter(x => x.missing.length);
  const kindHtml = (x, k) => { const items = x.assets.filter(a => a.kind === k), al = x.alarms.filter(a => a.kind === k), down = items.filter(a => a.up === false).length;
    const n = k === 'device' ? items.length + x.routers.length : items.length;
    return `<span class="srk ${al.some(a => a.severity === 'critical') ? 'crit' : al.length ? 'warn' : n ? 'ok' : 'none'}" title="${esc(SR_LABEL[k])}${al.length ? ': ' + esc(al.map(a => a.detail || a.event).join('; ')) : ''}">${SR_ICON[k]} ${n ? (down ? `${n - down}/${n}` : n) : '-'}</span>`; };
  el.innerHTML = `<div class="tiles">
      <div class="tile"><div class="l">Sites</div><div class="v">${sites.length}</div></div>
      <div class="tile"><div class="l">Need a look</div><div class="v ${bad.length ? 'warnc' : 'okc'}">${bad.length}</div></div>
      <div class="tile"><div class="l">Not fully registered</div><div class="v ${part.length ? 'warnc' : 'okc'}">${part.length}</div></div>
      <div class="tile"><div class="l">Routers with no site</div><div class="v ${SR.d.unassigned.length ? 'warnc' : 'okc'}">${SR.d.unassigned.length}</div></div></div>
    ${SR.d.unassigned.length ? `<div class="card" style="margin-bottom:14px"><h3>Routers that need a site</h3><p class="muted" style="font-size:12.5px">Saved before sites existed. Pick each one's site (fully registered sites only).</p>
      <table class="tbl"><tbody>${SR.d.unassigned.map(r => `<tr><td><b>${esc(r.name)}</b> <span class="faint mono">${esc(r.host)}</span></td><td>${srPicker(sites.filter(x => !x.missing.length), '', 'srU_' + r.id)}</td><td class="acts"><button class="btn sm" data-ua="${esc(r.id)}">Save</button></td></tr>`).join('')}</tbody></table></div>` : ''}
    ${sites.length ? `<div class="srgrid">${sites.map(x => `<div class="card srcard ${x.status}" data-s="${esc(x.id)}">
      <div class="row"><b>${esc(x.name)}</b><span class="sp"></span><span class="pill">${esc(x.code)}</span></div>
      <div class="faint" style="font-size:12px">${esc(SR.d.types[x.type] || 'type?')} · ${esc([x.town, x.county].filter(Boolean).join(', ') || 'where?')}</div>
      <div class="row" style="gap:6px;margin-top:8px;flex-wrap:wrap">${['power', 'access', 'cctv', 'device'].map(k => kindHtml(x, k)).join('')}</div>
      ${x.missing.length ? `<div class="warnc" style="font-size:12px;margin-top:6px">⚠ Not fully registered: ${esc(x.missing.join(', '))}</div>` : x.alarms.length ? `<div class="${x.status === 'critical' ? 'bad' : 'warnc'}" style="font-size:12px;margin-top:6px">⚠ ${esc(x.alarms[0].detail || x.alarms[0].event.replace(/_/g, ' '))}${x.alarms.length > 1 ? ` (+${x.alarms.length - 1})` : ''}</div>` : '<div class="okc" style="font-size:12px;margin-top:6px">All normal</div>'}</div>`).join('')}</div>`
      : '<div class="card"><p>No sites yet. Register each place the network is built in with <b>+ Site</b>: core, PoPs, towers, customer hubs. Devices are then added at a site.</p></div>'}`;
  el.querySelectorAll('[data-s]').forEach(x => x.onclick = () => { SR.open = x.dataset.s; srDraw(); });
  el.querySelectorAll('[data-ua]').forEach(b => b.onclick = async () => {
    const r = SR.d.unassigned.find(x => x.id === b.dataset.ua), site = $('#srU_' + r.id).value; if (!site) return toast('Pick a site', true);
    const body = { name: r.name, host: r.host, port: r.port, username: r.username, notes: r.notes, site, snmp_user: r.snmp ? r.snmp.user : '', snmp_auth_proto: r.snmp ? r.snmp.auth_proto : '', snmp_priv_proto: r.snmp ? r.snmp.priv_proto : '', snmp_port: r.snmp ? r.snmp.port : 161 };
    try { await api('/api/routers/' + encodeURIComponent(r.id), { method: 'PUT', body: JSON.stringify(body) }); toast(`${r.name} attached`); await srLoad(); srDraw(); } catch (err) { toast(err.message, true); } });
}
function srSite(el, s) {
  const d = SR.d, row = (k, v) => v ? `<tr><td class="faint">${esc(k)}</td><td>${v}</td></tr>` : '';
  const panel = k => { const items = s.assets.filter(a => a.kind === k), al = s.alarms.filter(a => a.kind === k);
    const extra = k === 'device' ? s.routers.map(r => `<tr><td>🖧 <b>${esc(r.name)}</b><div class="faint" style="font-size:11.5px">saved router · ${esc(r.host)}</div></td><td class="faint">Manager › Routers</td><td></td></tr>`).join('') : '';
    return `<div class="card"><h3 class="row"><span>${SR_ICON[k]} ${esc(SR_LABEL[k])}</span><span class="sp"></span><button class="btn sm" data-add="${k}">+ Add</button></h3>
      ${al.length ? nmWarnHtml(al.map(a => ({ severity: a.severity, title: a.event.replace(/_/g, ' '), detail: `${[a.asset, a.detail].filter(Boolean).join(': ')}`, since: a.ts }))) : ''}
      ${items.length || extra ? `<table class="tbl"><tbody>${items.map(a => `<tr><td><b>${esc(a.name)}</b><div class="faint" style="font-size:11.5px">${esc([a.model, a.ip, a.note].filter(Boolean).join(' · '))}</div></td>
        <td>${!a.ip ? '<span class="faint">not watched (no address)</span>' : a.up == null ? '<span class="faint">checking…</span>' : a.up ? `<span class="okc">● up</span> <span class="faint">${a.ms != null ? a.ms.toFixed(1) + ' ms' : ''}</span>` : `<span class="bad">● down</span> <span class="faint">${esc(ago(a.since))}</span>`}</td>
        <td class="acts"><button class="btn sm" data-ae="${esc(a.id)}">Edit</button><button class="btn sm danger" data-ad="${esc(a.id)}">Remove</button></td></tr>`).join('')}${extra}</tbody></table>`
        : `<p class="muted" style="font-size:12.5px">${k === 'power' ? 'Add the UPS, rectifier, generator controller or smart meter (its address is pinged; its alarms can be posted here).' : k === 'access' ? 'Add the door controller (ZKTeco, Hikvision, Suprema…).' : k === 'cctv' ? 'Add the NVR and cameras.' : 'Add switches, radios, OLTs; routers are added under Manager › Routers.'}</p>`}</div>`; };
  el.innerHTML = `<div class="row" style="margin-bottom:10px"><button class="btn sm" id="srBack">← All sites</button><span class="sp"></span><button class="btn sm" id="srEv">Log an event</button><button class="btn sm" id="srEd">Edit</button><button class="btn sm danger" id="srDel">Delete</button></div>
    <div class="grid g2"><div class="card"><h3>${esc(s.name)} <span class="pill">${esc(s.code)}</span></h3>
      ${s.missing.length ? `<div class="witem warn"><b>⚠ Not fully registered</b><div>Missing: ${esc(s.missing.join(', '))}.</div><div class="muted">Nothing can be added here until it is. Press Edit.</div></div>` : ''}
      <table class="tbl"><tbody>${row('Type', esc(d.types[s.type] || ''))}${row('Where', esc([s.town, s.county].filter(Boolean).join(', ')))}${row('GPS', s.lat != null ? `<a href="https://www.google.com/maps?q=${s.lat},${s.lon}" target="_blank" rel="noopener">${s.lat}, ${s.lon}</a>` : '')}
        ${row('Address', esc(s.address))}${row('Contact', esc([s.contact_name, s.contact_phone].filter(Boolean).join(' · ')))}${row('Getting in', esc(s.access))}
        ${row('Power', esc((s.power || []).map(p => d.power[p]).join(', ')))}${row('UPS', s.ups_kva ? esc(s.ups_kva + ' kVA') + (s.battery_hours ? ` · ${esc(s.battery_hours)} h on battery` : '') : '')}
        ${row('Landlord', esc([s.landlord, s.landlord_phone].filter(Boolean).join(' · ')))}${row('Lease ends', esc(s.lease_expiry))}${row('Notes', esc(s.notes))}</tbody></table></div>
      <div class="card"><h3>Now</h3>${s.alarms.length ? nmWarnHtml(s.alarms.map(a => ({ severity: a.severity, title: `${SR_ICON[a.kind] || ''} ${a.event.replace(/_/g, ' ')}`, detail: `${[a.asset, a.detail].filter(Boolean).join(': ')}`, since: a.ts }))) : '<p class="okc">All normal.</p>'}
        <h3 style="margin-top:10px">Recent events</h3><div id="srEvL" class="muted">Loading…</div></div></div>
    <div class="grid g2" style="margin-top:14px">${['power', 'access', 'cctv', 'device'].map(panel).join('')}</div>
    <div class="card" style="margin-top:14px"><h3>Connecting the power, door and camera systems</h3><p class="muted" style="font-size:12.5px">Each item with an address is pinged every minute from this server. Alarms (mains failed, on battery, door forced, camera offline, disk full…) can be posted by the system or a small script to <span class="mono">POST /api/site-events</span> with the header <span class="mono">Authorization: Bearer &lt;NE8000_SITES_TOKEN&gt;</span> and a body like <span class="mono">{"site": "${esc(s.code)}", "kind": "power", "event": "mains_fail", "asset": "UPS-1", "detail": "battery 82%"}</span>. Known events: ${Object.entries(d.events).map(([k, v]) => `<b>${esc(k)}</b> ${esc(Object.keys(v).join(', '))}`).join('; ')}.</p></div>`;
  $('#srBack').onclick = () => { SR.open = null; srDraw(); };
  $('#srEd').onclick = () => srForm(s);
  $('#srEv').onclick = () => srEvent(s);
  $('#srDel').onclick = async () => { if (!confirm(`Delete the site ${s.name}? Its items and events go too. Nothing changes on any device.`)) return;
    try { await api('/api/siteregistry/' + s.id, { method: 'DELETE' }); toast('Site deleted'); SR.open = null; await srLoad(); srDraw(); } catch (err) { toast(err.message, true); } };
  el.querySelectorAll('[data-add]').forEach(b => b.onclick = () => srItem(s, { kind: b.dataset.add }));
  el.querySelectorAll('[data-ae]').forEach(b => b.onclick = () => srItem(s, s.assets.find(a => a.id === b.dataset.ae)));
  el.querySelectorAll('[data-ad]').forEach(b => b.onclick = async () => { if (!confirm('Remove this item from the site?')) return; await api(`/api/siteregistry/${s.id}/assets/${b.dataset.ad}`, { method: 'DELETE' }); await srLoad(); srDraw(); });
  api(`/api/siteregistry/${s.id}/events`).then(r => { const l = $('#srEvL'); if (!l) return;
    l.innerHTML = r.events.length ? `<table class="tbl"><tbody>${r.events.slice(0, 15).map(e => `<tr><td class="faint" style="white-space:nowrap">${esc(ago(e.ts))}</td><td>${SR_ICON[e.kind] || ''} <b class="${e.severity === 'critical' ? 'bad' : e.severity === 'warning' ? 'warnc' : e.severity === 'ok' ? 'okc' : ''}">${esc(e.event.replace(/_/g, ' '))}</b> ${esc(e.asset || '')} <span class="faint">${esc(e.detail || '')}</span></td></tr>`).join('')}</tbody></table>` : 'None yet.'; }).catch(() => {});
}
// register or change a site: every field marked * is needed before devices can be added there
function srForm(s) {
  const m = $('#modal'), d = SR.d, v = k => esc(s && s[k] != null ? s[k] : '');
  const counties = (typeof KENYA_MAP === 'object' ? KENYA_MAP.counties.map(c => c.n) : []).sort();
  m.classList.add('wide');
  m.innerHTML = `<h2>${s ? 'Change ' + esc(s.name) : 'Register a site'}</h2><p class="muted">Fields marked * are needed before devices can be added at the site.</p>
    <div class="grid g2"><div class="form">
      ${SR_FIELDS.slice(0, 2).map(([k, l, ph]) => `<label>${l} *</label><input id="sf_${k}" value="${v(k)}" placeholder="${esc(ph)}">`).join('')}
      <label>Type *</label><select id="sf_type"><option value="">pick</option>${Object.entries(d.types).map(([k, t]) => `<option value="${k}" ${s && s.type === k ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
      <label>County *</label><input id="sf_county" value="${v('county')}" list="sfCL" placeholder="Marsabit"><datalist id="sfCL">${counties.map(c => `<option value="${esc(c)}">`).join('')}</datalist>
      <label>Town *</label><input id="sf_town" value="${v('town')}" list="sfTL" placeholder="MARSABIT"><datalist id="sfTL">${Object.keys(KE_TOWNS).sort().map(t => `<option value="${esc(t)}">`).join('')}</datalist>
      <label>GPS latitude *</label><input id="sf_lat" value="${v('lat')}" placeholder="2.3346"><label>GPS longitude *</label><input id="sf_lon" value="${v('lon')}" placeholder="37.9899">
      <div class="help">Picking a known town fills the GPS roughly; put the exact point when you have it.</div></div>
    <div class="form">${SR_FIELDS.slice(3).map(([k, l, ph]) => `<label>${l} *</label>${k === 'access' || k === 'address' ? `<textarea id="sf_${k}" rows="2" placeholder="${esc(ph)}">${v(k)}</textarea>` : `<input id="sf_${k}" value="${v(k)}" placeholder="${esc(ph)}">`}`).join('')}
      <label>Power source *</label><div>${Object.entries(d.power).map(([k, t]) => `<label style="display:inline-flex;gap:4px;margin-right:10px;font-weight:400"><input type="checkbox" class="sfP" value="${k}" ${s && (s.power || []).includes(k) ? 'checked' : ''} style="width:auto">${esc(t)}</label>`).join('')}</div>
      <label>UPS (kVA)</label><input id="sf_ups_kva" value="${v('ups_kva')}" placeholder="3"><label>Battery time (hours)</label><input id="sf_battery_hours" value="${v('battery_hours')}" placeholder="4">
      <label>Landlord</label><input id="sf_landlord" value="${v('landlord')}"><label>Landlord phone</label><input id="sf_landlord_phone" value="${v('landlord_phone')}">
      <label>Lease ends</label><input id="sf_lease_expiry" type="date" value="${v('lease_expiry')}"><label>Notes</label><input id="sf_notes" value="${v('notes')}"></div></div>
    <div class="err"></div><div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">${s ? 'Save' : 'Register'}</button></div>`;
  $('#modalBg').classList.remove('hidden');
  $('#sf_town').onchange = () => { const t = KE_TOWNS[$('#sf_town').value.trim().toUpperCase()]; if (t && !$('#sf_lat').value) { $('#sf_lat').value = t[0]; $('#sf_lon').value = t[1]; } };
  m.querySelector('.actions').onclick = async e => {
    const a = e.target.dataset.a; if (a === 'c') { m.classList.remove('wide'); return closeModal(); } if (a !== 's') return;
    const body = Object.fromEntries(['name', 'code', 'type', 'county', 'town', 'lat', 'lon', 'address', 'contact_name', 'contact_phone', 'access', 'ups_kva', 'battery_hours', 'landlord', 'landlord_phone', 'lease_expiry', 'notes'].map(k => [k, $('#sf_' + k).value]));
    body.power = [...m.querySelectorAll('.sfP:checked')].map(x => x.value);
    try { const r = s ? await api('/api/siteregistry/' + s.id, { method: 'PUT', body: JSON.stringify(body) }) : await post('/api/siteregistry', body);
      m.classList.remove('wide'); closeModal(); toast(r.missing.length ? `Saved. Still missing: ${r.missing.join(', ')}` : 'Site registered'); SR.open = r.site.id; await srLoad(); srDraw(); }
    catch (err) { m.querySelector('.err').textContent = err.message; }
  };
}
function srItem(s, a) {
  const m = $('#modal'), d = SR.d;
  m.innerHTML = `<h2>${a && a.id ? 'Change ' + esc(a.name) : 'Add at ' + esc(s.name)}</h2>
    <div class="form"><label>What it is</label><select id="si_kind">${Object.entries(d.kinds).map(([k, t]) => `<option value="${k}" ${a && a.kind === k ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
      <label>Name</label><input id="si_name" value="${esc((a && a.name) || '')}" placeholder="UPS-1, NVR-1, Door controller, SW-MBT-01">
      <label>Address (to watch it)</label><input id="si_ip" value="${esc((a && a.ip) || '')}" placeholder="10.50.1.20 (optional)">
      <label>Make and model</label><input id="si_model" value="${esc((a && a.model) || '')}" placeholder="APC Smart-UPS 3000, Hikvision DS-7608…">
      <label>Note</label><input id="si_note" value="${esc((a && a.note) || '')}"></div><div class="err"></div>
    <div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Save</button></div>`;
  $('#modalBg').classList.remove('hidden');
  m.querySelector('.actions').onclick = async e => { const x = e.target.dataset.a; if (x === 'c') return closeModal(); if (x !== 's') return;
    try { await post(`/api/siteregistry/${s.id}/assets`, { id: a && a.id, kind: $('#si_kind').value, name: $('#si_name').value, ip: $('#si_ip').value, model: $('#si_model').value, note: $('#si_note').value });
      closeModal(); toast('Saved; it is checked within a minute'); await srLoad(); srDraw(); } catch (err) { m.querySelector('.err').textContent = err.message; } };
}
// note an event by hand (a power cut someone phoned in, a door left open), the same as a system would post it
function srEvent(s) {
  const m = $('#modal'), d = SR.d;
  m.innerHTML = `<h2>Log an event at ${esc(s.name)}</h2><div class="form"><label>Area</label><select id="se_kind">${Object.keys(d.events).map(k => `<option value="${k}">${esc(SR_LABEL[k] || k)}</option>`).join('')}</select>
    <label>Event</label><select id="se_event"></select><label>Item</label><input id="se_asset" placeholder="UPS-1 (optional)" list="seAL"><datalist id="seAL">${s.assets.map(a => `<option value="${esc(a.name)}">`).join('')}</datalist>
    <label>Detail</label><input id="se_detail" placeholder="e.g. KPLC outage in the area, battery 80%"></div><div class="err"></div>
    <div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Log it</button></div>`;
  $('#modalBg').classList.remove('hidden');
  const fill = () => { $('#se_event').innerHTML = Object.entries(d.events[$('#se_kind').value]).map(([k, sv]) => `<option value="${k}">${esc(k.replace(/_/g, ' '))} (${sv})</option>`).join(''); };
  $('#se_kind').onchange = fill; fill();
  m.querySelector('.actions').onclick = async e => { const x = e.target.dataset.a; if (x === 'c') return closeModal(); if (x !== 's') return;
    try { await post('/api/site-events', { site: s.id, kind: $('#se_kind').value, event: $('#se_event').value, asset: $('#se_asset').value, detail: $('#se_detail').value, source: 'by hand' });
      closeModal(); toast('Event logged'); await srLoad(); srDraw(); } catch (err) { m.querySelector('.err').textContent = err.message; } };
}
