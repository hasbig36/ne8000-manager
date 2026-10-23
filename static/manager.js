/* Manager pages: my account, app users (admin) and the audit log (admin). */
const MANAGER_PAGES = ['account', 'appusers', 'audit'];
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

const AUDIT_TEXT = {
  login: 'signed in', login_failed: 'failed sign-in', logout: 'signed out', password_changed: 'changed their password', user_added: 'added app user', user_changed: 'changed app user',
  user_deleted: 'deleted app user', router_connect: 'connected to router', router_disconnect: 'disconnected from router', action: 'change', config: 'config editor', save: 'saved config',
  billing_settings: 'billing settings', billing_sync_apply: 'billing sync (apply)', billing_map: 'billing port mapping', plan_execute: 'ran plan', plan_deleted: 'deleted plan',
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
