/* Manager pages: my account, routers (admin), adopt devices (admin), app users (admin) and the audit log (admin). */
const MANAGER_PAGES = ['account', 'routers', 'adopt', 'appusers', 'audit'];
const ROLE_HELP = { admin: 'everything, including app users', operator: 'everything except app users', viewer: 'read only' };
const when = t => t ? new Date(t * 1000).toLocaleString() : '';

PAGES.account = async c => {
  c.innerHTML = head('My account', ME.username + ' · ' + ME.role) + `<div class="card" style="max-width:520px"><h3>Change password</h3>
    <div class="form"><label>Current password</label><input id="acCur" type="password" autocomplete="current-password"><label>New password</label><input id="acNew" type="password" autocomplete="new-password" placeholder="at least 10 characters">
    <label>New password again</label><input id="acNew2" type="password" autocomplete="new-password"></div><div class="err" id="acErr"></div>
    <div class="actions"><button class="btn primary" id="acSave">Change password</button></div></div>`;
  $('#acSave').onclick = async () => {
    $('#acErr').textContent = '';
    if ($('#acNew').value !== $('#acNew2').value) return $('#acErr').textContent = 'The new passwords do not match.';
    try { await post('/api/auth/password', { current: $('#acCur').value, new: $('#acNew').value }); toast('Password changed'); ['#acCur', '#acNew', '#acNew2'].forEach(s => $(s).value = ''); }
    catch (e) { $('#acErr').textContent = e.message; }
  };
};

PAGES.appusers = async c => {
  const { users, roles } = await api('/api/users');
  c.innerHTML = head('App users', 'who can sign in to NE8000 Manager', `<button class="btn primary" id="auAdd">+ App user</button>`) +
    `<div class="card"><div id="auT"></div><p class="faint" style="font-size:12.5px;margin-bottom:0">${roles.map(r => `<b>${r}</b>: ${ROLE_HELP[r]}`).join(' · ')}. Router SSH logins are separate: each person still connects with their own router account.</p></div>`;
  table($('#auT'), [{ t: 'Username', k: 'username', f: u => `<b>${esc(u.username)}</b>${u.username === ME.username ? ' <span class="faint">(you)</span>' : ''}` }, { t: 'Role', k: 'role' },
    { t: 'Created', k: 'created', f: u => when(u.created) }, { t: 'Last sign-in', k: 'last_login', f: u => when(u.last_login) || '<span class="faint">never</span>' },
    { t: '', sort: false, cls: 'acts', f: u => `<button class="btn sm" data-edit="${esc(u.username)}">Edit</button>${u.username === ME.username ? '' : `<button class="btn sm danger" data-del="${esc(u.username)}">Delete</button>`}` }], users, { filter: false });
  const form = u => {
    const m = $('#modal');
    m.innerHTML = `<h2>${u ? 'Edit ' + esc(u.username) : 'New app user'}</h2><div class="form">${u ? '' : '<label>Username</label><input id="fuName" autocomplete="off">'}
      <label>Role</label><select id="fuRole">${roles.map(r => `<option ${u && u.role === r ? 'selected' : ''} value="${r}">${r}: ${ROLE_HELP[r]}</option>`).join('')}</select>
      <label>${u ? 'New password' : 'Password'}</label><input id="fuPw" type="password" autocomplete="new-password" placeholder="${u ? 'leave empty to keep' : 'at least 10 characters'}"></div>
      <div class="err"></div><div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">${u ? 'Save' : 'Add user'}</button></div>`;
    $('#modalBg').classList.remove('hidden');
    m.querySelector('.actions').onclick = async e => {
      if (e.target.dataset.a === 'c') return closeModal(); if (e.target.dataset.a !== 's') return;
      try {
        if (u) await api('/api/users/' + encodeURIComponent(u.username), { method: 'PUT', body: JSON.stringify({ role: $('#fuRole').value, password: $('#fuPw').value }) });
        else await post('/api/users', { username: $('#fuName').value, role: $('#fuRole').value, password: $('#fuPw').value });
        closeModal(); toast('Saved'); PAGES.appusers(c);
      } catch (err) { m.querySelector('.err').textContent = err.message; }
    };
  };
  $('#auAdd').onclick = () => form(null);
  $('#auT').onclick = async e => {
    const ed = e.target.dataset.edit, del = e.target.dataset.del;
    if (ed) form(users.find(u => u.username === ed));
    if (del && confirm(`Delete the app user ${del}? They are signed out at once.`)) { try { await api('/api/users/' + encodeURIComponent(del), { method: 'DELETE' }); toast('Deleted'); PAGES.appusers(c); } catch (err) { toast(err.message, true); } }
  };
};

PAGES.routers = async c => {
  const { routers } = await api('/api/routers');
  c.innerHTML = head('Routers', 'the routers people can connect to', `<button class="btn primary" id="rtAdd">+ Router</button>`) +
    `<div class="card"><div id="rtT"></div><p class="faint" style="font-size:12.5px;margin-bottom:0">Once at least one router is saved, the app only connects to saved routers and the connect screen shows them as a list.
    Router SSH passwords are never stored: each person types their own. An optional read-only SNMPv3 login is kept on this server (never shown here) so people can connect and monitor without one.</p></div>`;
  table($('#rtT'), [{ t: 'Name', k: 'name', f: r => `<b>${esc(r.name)}</b>` }, { t: 'Address', k: 'host', f: r => `<span class="mono">${esc(r.host)}:${r.port}</span>` },
    { t: 'Default username', k: 'username', f: r => esc(r.username) || '<span class="faint">none</span>' },
    { t: 'SNMP', k: 'snmp', f: r => r.snmp ? `v3 <span class="mono">${esc(r.snmp.user)}</span>` : '<span class="faint">off (SSH only)</span>' }, { t: 'Notes', k: 'notes', cls: 'wrap', f: r => esc(r.notes) },
    { t: '', sort: false, cls: 'acts', f: r => `${r.snmp ? `<button class="btn sm" data-test="${esc(r.id)}">Test SNMP</button>` : ''}<button class="btn sm" data-edit="${esc(r.id)}">Edit</button><button class="btn sm danger" data-del="${esc(r.id)}">Delete</button>` }],
    routers, { filter: false, empty: 'No routers saved yet. Add one so people can pick it when connecting.' });
  const form = r => {
    const m = $('#modal'), v = k => r ? esc(r[k] ?? '') : '';
    m.innerHTML = `<h2>${r ? 'Edit ' + esc(r.name) : 'New router'}</h2><div class="form">
      <label>Name</label><input id="frName" value="${v('name')}" placeholder="BNG-NDOVU1" autocomplete="off">
      <label>Management IP / hostname</label><input id="frHost" value="${v('host')}" placeholder="192.168.0.10" autocomplete="off">
      <label>SSH port</label><input id="frPort" type="number" value="${r ? r.port : 22}">
      <label>Default username</label><input id="frUser" value="${v('username')}" placeholder="optional, filled in on the connect screen" autocomplete="off">
      <label>Notes</label><input id="frNotes" value="${v('notes')}" placeholder="optional" autocomplete="off">
      <h3 style="margin:14px 0 2px">SNMP (optional)</h3><div class="faint" style="font-size:12.5px">A read-only SNMPv3 login. With it, Connect uses SNMP first and SSH only when needed or if SNMP fails. Leave the user empty for SSH only.</div>
      <label>SNMPv3 user</label><input id="fsUser" value="${r && r.snmp ? esc(r.snmp.user) : ''}" placeholder="e.g. ne8000mon" autocomplete="off">
      <label>Authentication</label><select id="fsAuth">${['sha256', 'sha384', 'sha512', 'sha224', 'sha', 'md5'].map(p => `<option ${r && r.snmp && r.snmp.auth_proto === p ? 'selected' : ''} value="${p}">${p.toUpperCase()}</option>`).join('')}</select>
      <label>Authentication password</label><input id="fsAuthKey" type="password" autocomplete="new-password" placeholder="${r && r.snmp ? 'leave empty to keep' : '8 to 64 characters'}">
      <label>Privacy (encryption)</label><select id="fsPriv">${['aes128', 'aes192', 'aes256', 'des'].map(p => `<option ${r && r.snmp && r.snmp.priv_proto === p ? 'selected' : ''} value="${p}">${p.toUpperCase()}</option>`).join('')}</select>
      <label>Privacy password</label><input id="fsPrivKey" type="password" autocomplete="new-password" placeholder="${r && r.snmp ? 'leave empty to keep' : '8 to 64 characters'}">
      <label>SNMP port</label><input id="fsPort" type="number" value="${r && r.snmp ? r.snmp.port : 161}"></div>
      <div class="err"></div><div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">${r ? 'Save' : 'Add router'}</button></div>`;
    $('#modalBg').classList.remove('hidden'); $('#frName').focus();
    m.querySelector('.actions').onclick = async e => {
      if (e.target.dataset.a === 'c') return closeModal(); if (e.target.dataset.a !== 's') return;
      const body = { name: $('#frName').value, host: $('#frHost').value, port: $('#frPort').value, username: $('#frUser').value, notes: $('#frNotes').value,
        snmp_user: $('#fsUser').value, snmp_auth_proto: $('#fsAuth').value, snmp_auth_key: $('#fsAuthKey').value, snmp_priv_proto: $('#fsPriv').value,
        snmp_priv_key: $('#fsPrivKey').value, snmp_port: $('#fsPort').value };
      try {
        if (r) await api('/api/routers/' + encodeURIComponent(r.id), { method: 'PUT', body: JSON.stringify(body) });
        else await post('/api/routers', body);
        closeModal(); toast('Saved'); PAGES.routers(c);
      } catch (err) { m.querySelector('.err').textContent = err.message; }
    };
  };
  $('#rtAdd').onclick = () => form(null);
  $('#rtT').onclick = async e => {
    const ed = e.target.dataset.edit, del = e.target.dataset.del, ts = e.target.dataset.test;
    if (ts) { e.target.disabled = true; try { const x = await post('/api/routers/' + encodeURIComponent(ts) + '/test-snmp'); toast(`SNMP OK: ${x.sysname}, up ${x.uptime_days} days`); } catch (err) { toast(err.message, true); } finally { e.target.disabled = false; } }
    if (ed) form(routers.find(r => r.id === ed));
    const d = del && routers.find(r => r.id === del);
    if (d && confirm(`Remove ${d.name} (${d.host}) from the saved routers? People already connected stay connected.`)) { try { await api('/api/routers/' + encodeURIComponent(del), { method: 'DELETE' }); toast('Removed'); PAGES.routers(c); } catch (err) { toast(err.message, true); } }
  };
};

/* Adopt: find new Huawei routers and switches (subnet scan, LLDP), push a reviewed initial configuration, save them as routers. */
let ADOPT = { devices: [] };
PAGES.adopt = async c => {
  c.innerHTML = head('Adopt devices', 'find new Huawei routers and switches and give them their first configuration') +
    `<div class="card"><h3>Find devices</h3>
      <div class="form" style="grid-template-columns:auto 1fr auto 90px auto;align-items:center">
        <label>Subnet</label><input id="adNet" placeholder="10.10.0.0/24 (up to a /22)" autocomplete="off">
        <label>SSH port</label><input id="adPort" type="number" value="22">
        <button class="btn primary" id="adScan">Scan</button></div>
      <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap"><button class="btn" id="adLldp">Check LLDP neighbours of saved routers</button><button class="btn" id="adManual">Adopt by address…</button></div>
      <p class="faint" style="font-size:12.5px;margin-bottom:0">The scan only opens the SSH port and reads the greeting each device sends; nothing is logged in to or changed. LLDP is read over SNMP from every saved router that has an SNMP login.</p>
      <div id="adMsg" class="muted" style="margin-top:8px"></div><div id="adT" style="margin-top:8px"></div></div>
    <div class="card hidden" id="adW"></div>`;
  const show = (rows, note) => {
    ADOPT.devices = rows;
    $('#adMsg').innerHTML = note;
    table($('#adT'), [{ t: 'Address', k: 'host', f: d => `<b class="mono">${esc(d.host || '(no management address)')}</b>` }, { t: 'Name', k: 'name' },
      { t: 'Vendor', k: 'vendor', f: d => d.huawei ? '<span class="pill p-ok">Huawei</span>' : esc(d.vendor || 'unknown') }, { t: 'Found by', k: 'source', f: d => d.source === 'lldp' ? 'LLDP · ' + esc(d.via) : 'scan' },
      { t: 'Greeting / description', k: 'banner', cls: 'wrap faint', f: d => esc(d.banner) },
      { t: '', sort: false, cls: 'acts', f: (d) => d.saved ? '<span class="faint">already saved</span>' : !d.host ? '<span class="faint">no address</span>' : d.huawei ? `<button class="btn sm primary" data-adopt="${esc(d.host)}">Adopt</button>` : `<span class="faint">not Huawei</span>` }],
      rows, { empty: 'Nothing found.' });
  };
  $('#adScan').onclick = async e => {
    const net = $('#adNet').value.trim(); if (!net) return $('#adNet').focus();
    e.target.disabled = true; $('#adMsg').textContent = 'Scanning ' + net + '…';
    try { const r = await post('/api/adopt/scan', { subnet: net, port: $('#adPort').value }); show(r.devices, `${r.devices.length} address(es) answered on SSH, ${r.devices.filter(d => d.huawei && !d.saved).length} new Huawei device(s).${r.demo ? ' <b>Demo:</b> simulated devices.' : ''}`); }
    catch (err) { $('#adMsg').innerHTML = `<span class="err">${esc(err.message)}</span>`; } finally { e.target.disabled = false; }
  };
  $('#adNet').onkeydown = e => { if (e.key === 'Enter') $('#adScan').click(); };
  $('#adLldp').onclick = async e => {
    e.target.disabled = true; $('#adMsg').textContent = 'Reading LLDP neighbours…';
    try { const r = await api('/api/adopt/discover'); show(r.devices, `${r.devices.length} LLDP neighbour(s), ${r.devices.filter(d => d.huawei && !d.saved && d.host).length} new Huawei device(s) with a management address.` + (r.errors.length ? ` <span class="err">${esc(r.errors.join('; '))}</span>` : '') + (r.demo ? ' <b>Demo:</b> simulated neighbours.' : '')); }
    catch (err) { $('#adMsg').innerHTML = `<span class="err">${esc(err.message)}</span>`; } finally { e.target.disabled = false; }
  };
  $('#adManual').onclick = () => adoptWizard({ host: '', port: 22, name: '' });
  $('#adT').onclick = e => { const h = e.target.dataset.adopt; if (h) adoptWizard(ADOPT.devices.find(d => d.host === h)); };
};

function adoptWizard(d) {
  const w = $('#adW'); w.classList.remove('hidden');
  const mgr = /^\d+\.\d+\.\d+\.\d+$/.test(location.hostname) ? location.hostname : '';
  const lab = (k, t, v = '', x = '') => `<label>${t}</label><input id="aw_${k}" value="${esc(v)}" autocomplete="off" ${x}>`;
  w.innerHTML = `<h3>Adopt ${esc(d.name || d.host || 'a device')}</h3>
    <div class="faint" style="font-size:12.5px">1. Log in with the device's current login (often the factory one) so the platform can be checked. 2. Fill in the template. 3. Review the exact commands, edit them if your software version needs it, then check or apply.</div>
    <h3 style="margin-top:14px">1. Current login</h3>
    <div class="form">${lab('host', 'Address', d.host, 'placeholder="192.168.1.253"')}${lab('port', 'SSH port', d.port || 22, 'type="number"')}${lab('user', 'Username', 'admin')}
      <label>Password</label><input id="aw_pass" type="password" autocomplete="new-password" placeholder="current password (not stored)"></div>
    <div class="actions" style="justify-content:flex-start"><button class="btn" id="awId">Log in and identify</button><span id="awIdR" class="muted" style="align-self:center"></span></div>
    <h3 style="margin-top:14px">2. Initial configuration</h3>
    <div class="form">
      <label>Platform</label><select id="aw_platform"><option value="">(log in and identify first)</option><option value="vrp8">NE / CE router (VRP8, two-stage commit)</option><option value="vrp5">S switch (VRP5, applies each line at once)</option></select>
      ${lab('hostname', 'Hostname', d.name && d.name !== 'HUAWEI' ? d.name : '', 'placeholder="SW-NDOVU-02"')}
      ${lab('admin_user', 'New admin user', 'noc')}<label>New admin password</label><input id="aw_admin_password" type="password" autocomplete="new-password" placeholder="8 to 64 characters, sent to the device only">
      <label>SSH only (telnet off)</label><input id="aw_ssh_only" type="checkbox" checked style="width:auto">
      ${lab('mgmt_interface', 'Management interface', '', 'placeholder="Vlanif10 or MEth0/0/0 (optional)"')}${lab('mgmt_vlan', 'Management VLAN', '', 'placeholder="optional, for a Vlanif"')}
      ${lab('mgmt_ip', 'Management IP / prefix', '', 'placeholder="10.10.0.21/24 (optional, applied last)"')}${lab('gateway', 'Default gateway', '', 'placeholder="optional"')}
      ${lab('snmp_user', 'SNMPv3 user (read-only)', 'ne8000mon')}
      <label>SNMP authentication</label><select id="aw_snmp_auth_proto"><option value="sha256">SHA-256</option><option value="sha">SHA</option><option value="md5">MD5</option></select>
      <label>SNMP authentication password</label><input id="aw_snmp_auth_key" type="password" autocomplete="new-password" placeholder="8 to 64 characters">
      <label>SNMP privacy</label><select id="aw_snmp_priv_proto"><option value="aes128">AES-128</option><option value="aes256">AES-256</option><option value="des">DES</option></select>
      <label>SNMP privacy password</label><input id="aw_snmp_priv_key" type="password" autocomplete="new-password" placeholder="8 to 64 characters">
      ${lab('snmp_acl_source', 'Allow SNMP only from', mgr, 'placeholder="this server\'s address"')}
      ${lab('ntp', 'NTP server(s)', '', 'placeholder="optional, comma separated"')}${lab('syslog', 'Syslog server(s)', '102.216.117.118', 'placeholder="optional"')}
      ${lab('banner', 'Login banner', 'Authorised access only. Activity is logged.')}</div>
    <div class="actions" style="justify-content:flex-start"><button class="btn primary" id="awBuild">Build commands</button></div>
    <div id="awS3" class="hidden"><h3 style="margin-top:14px">3. Review the commands</h3>
      <div class="faint" style="font-size:12.5px">These lines are sent in system-view. Lines after the "applied last" marker move the management address and are sent after the rest is committed and saved. Passwords appear here because they are part of the configuration; they are not stored by this app.</div>
      <textarea id="awText" spellcheck="false" style="min-height:360px;margin-top:6px"></textarea>
      <div class="err" id="awErr"></div>
      <div class="actions" style="justify-content:flex-start"><button class="btn" id="awCheck">Check only</button><button class="btn primary" id="awApply">Apply, save and add to Routers</button><button class="btn" id="awClose">Close</button></div>
      <pre id="awLog" class="hidden" style="max-height:320px;overflow:auto"></pre></div>`;
  w.scrollIntoView({ behavior: 'smooth' });
  const val = k => { const e = $('#aw_' + k); return e.type === 'checkbox' ? e.checked : e.value; };
  const fields = () => Object.fromEntries(['hostname', 'admin_user', 'admin_password', 'ssh_only', 'mgmt_interface', 'mgmt_vlan', 'mgmt_ip', 'gateway', 'snmp_user', 'snmp_auth_proto', 'snmp_auth_key', 'snmp_priv_proto', 'snmp_priv_key', 'snmp_acl_source', 'ntp', 'syslog', 'banner'].map(k => [k, val(k)]));
  const login = () => ({ host: val('host').trim(), port: val('port'), username: val('user'), password: val('pass') });
  $('#awId').onclick = async e => {
    e.target.disabled = true; $('#awIdR').textContent = 'Logging in…';
    try { const r = await post('/api/adopt/identify', login()); $('#aw_platform').value = r.platform; $('#awIdR').innerHTML = `<span class="pill p-ok">logged in</span> ${esc(r.model || 'Huawei')} · VRP ${esc(r.version)} · ${r.platform === 'vrp8' ? 'two-stage commit' : 'applies each line at once'}`; $('#awCheck').disabled = r.platform !== 'vrp8'; }
    catch (err) { $('#awIdR').innerHTML = `<span class="err">${esc(err.message)}</span>`; } finally { e.target.disabled = false; }
  };
  $('#awBuild').onclick = async () => {
    $('#awErr').textContent = '';
    try { const r = await post('/api/adopt/build', { fields: fields(), platform: val('platform') }); $('#awText').value = r.text; $('#awS3').classList.remove('hidden'); $('#awCheck').disabled = val('platform') !== 'vrp8'; $('#awText').scrollIntoView({ behavior: 'smooth' }); }
    catch (err) { $('#awS3').classList.remove('hidden'); $('#awText').value = ''; $('#awErr').textContent = err.message; }
  };
  const run = async commit => {
    $('#awErr').textContent = ''; const log = $('#awLog');
    if (commit && !confirm(`Send this configuration to ${val('host')} and save it? ${val('platform') === 'vrp5' ? 'This switch applies each line as it is sent.' : ''}`)) return;
    ['#awCheck', '#awApply'].forEach(s => $(s).disabled = true); log.classList.remove('hidden'); log.textContent = commit ? 'Applying…' : 'Checking…';
    try {
      const r = await post('/api/adopt/apply', { ...login(), platform: val('platform'), text: $('#awText').value, commit, fields: fields() });
      log.textContent = r.log;
      if (!commit) { r.ok ? toast('Check passed: the device accepted every line (nothing was committed).') : ($('#awErr').textContent = 'The device rejected:\n' + r.errors.join('\n')); }
      else {
        $('#awErr').innerHTML = (r.warnings || []).map(x => esc(x)).join('<br>');
        toast(r.saved ? `${r.saved.name} adopted and added to Routers at ${r.host}` : `Configured ${r.host}`);
        if (r.saved) { $('#awIdR').innerHTML = `<span class="pill p-ok">adopted</span> saved as <b>${esc(r.saved.name)}</b> (${esc(r.host)}). It now appears in the router switcher, and its graphs start within 10 minutes.`; }
      }
    } catch (err) { $('#awErr').textContent = err.message; log.textContent = ''; log.classList.add('hidden'); }
    finally { $('#awApply').disabled = false; $('#awCheck').disabled = val('platform') !== 'vrp8'; }
  };
  $('#awCheck').onclick = () => run(false);
  $('#awApply').onclick = () => run(true);
  $('#awClose').onclick = () => { w.classList.add('hidden'); w.innerHTML = ''; };
}

const AUDIT_TEXT = {
  login: 'signed in', login_failed: 'failed sign-in', logout: 'signed out', password_changed: 'changed their password', user_added: 'added app user', user_changed: 'changed app user',
  user_deleted: 'deleted app user', router_connect: 'connected to router', router_ssh_added: 'added SSH to an SNMP session', router_snmp_failed: 'SNMP connect failed', router_disconnect: 'disconnected from router', action: 'change', config: 'config editor', save: 'saved config',
  billing_settings: 'billing settings', billing_sync_apply: 'billing sync (apply)', billing_map: 'billing port mapping', router_added: 'added router', router_changed: 'changed router', router_deleted: 'removed router', adopt_scan: 'scanned for devices', adopt_checked: 'checked an adopt config', adopt_applied: 'adopted a device', adopt_failed: 'adopt failed', plan_execute: 'ran plan', plan_deleted: 'deleted plan',
};
PAGES.audit = async c => {
  const { events } = await api('/api/audit?n=500');
  c.innerHTML = head('Audit log', 'who did what, newest first', `<button class="btn" onclick="refresh()">Refresh</button>`) + '<div class="card"><div id="adT"></div></div>';
  const detail = e => {
    const { at, user, event, lines, ...rest } = e;
    const parts = Object.entries(rest).filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v]) => `${k}: ${v}`);
    return esc(parts.join(' · ')) + (Array.isArray(lines) ? `<pre class="mono" style="margin:4px 0 0;font-size:11.5px">${esc(lines.join('\n'))}</pre>` : '');
  };
  table($('#adT'), [{ t: 'When', k: 'at', f: e => when(e.at) }, { t: 'User', k: 'user' }, { t: 'Event', k: 'event', f: e => `<span class="pill ${e.event === 'login_failed' ? 'p-bad' : e.event === 'action' || e.event === 'plan_execute' || e.event === 'config' ? 'p-warn' : 'p-off'}">${esc(AUDIT_TEXT[e.event] || e.event)}</span>` },
    { t: 'Details', k: 'host', cls: 'wrap', f: detail }], events, { sort: 'at', dir: -1, empty: 'Nothing logged yet' });
};
