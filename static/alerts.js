/* Alerts: where alerts go (Telegram, email, SMS, Slack, Teams, Discord, webhook), what is alerted (one rule per kind of
   problem: who gets it, from which severity, only/never for some links or sites, reminders), the user's own checks
   (ping with latency and loss limits, a TCP port, a web page), quiet hours, what is wrong now and what was sent. */
const AL = { d: null };
const AL_SEV = { info: 'info', warning: 'warning', critical: 'critical' };
const alSevPill = s => `<span class="pill ${s === 'critical' ? 'p-bad' : s === 'warning' ? 'p-warn' : ''}">${esc(s)}</span>`;
const alAdmin = () => ME && ME.role === 'admin';
const alDur = s => { s = Math.max(0, Math.round(s)); return s >= 86400 ? `${Math.floor(s / 86400)} d ${Math.floor(s % 86400 / 3600)} h` : s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min` : `${Math.max(1, Math.floor(s / 60))} min`; };
const AL_HELP = {
  telegram: 'Create a bot with @BotFather in Telegram and copy its token. Add the bot to your NOC group, send any message in the group, then open https://api.telegram.org/bot&lt;token&gt;/getUpdates to find the group\'s chat ID (it starts with -100…).',
  email: 'Any SMTP account. Gmail / Google Workspace: smtp.gmail.com, port 587, STARTTLS, your address and an app password (Google Account › Security › App passwords).',
  sms_at: 'Africa\'s Talking: your app\'s username and API key (Settings › API key). Numbers in +2547… form. Use the username "sandbox" to try it without sending real SMS.',
  slack: 'Slack › Apps › Incoming Webhooks › Add to a channel, then copy the webhook URL.',
  teams: 'In the Teams channel: ⋯ › Workflows › "Post to a channel when a webhook request is received" (or Connectors › Incoming Webhook), then copy the URL.',
  discord: 'Channel settings › Integrations › Webhooks › New webhook › Copy webhook URL.',
  webhook: 'Each alert is POSTed as JSON (subject, text and the alerts) to this URL: for your ticket system, n8n, or an SMS / WhatsApp gateway.',
};

async function alLoad() { AL.d = await api('/api/alerts'); return AL.d; }

PAGES.alerts = async c => {
  c.innerHTML = head('Alerts', 'messages to Telegram, email, SMS, Slack or Teams when something goes wrong',
    alAdmin() ? `<button class="btn" id="alRun">Check now</button><button class="btn primary" id="alAddC">+ Channel</button>` : '') + '<div id="alBody"><div class="card muted">Loading…</div></div>';
  await alLoad(); alDraw();
  if ($('#alAddC')) $('#alAddC').onclick = () => alChannelForm(null);
  if ($('#alRun')) $('#alRun').onclick = async () => { await post('/api/alerts/run'); toast('Checking now'); setTimeout(async () => { if (current === 'alerts') { await alLoad(); alDraw(); } }, 4000); };
  every(30000, async () => { if ($('#modalBg').classList.contains('hidden') && !alEditing()) { await alLoad(); alDraw(); } });
};
// someone is changing the rules or quiet hours: do not redraw under them
const alEditing = () => !!document.querySelector('#alRules.dirty, #alQuiet.dirty');

function alDraw() {
  const el = $('#alBody'), d = AL.d; if (!el || !d) return;
  const act = d.active, crit = act.filter(a => a.severity === 'critical').length, chans = d.channels, adm = alAdmin();
  const cname = id => (chans.find(c => c.id === id) || {}).name || '?';
  const chanTo = c => esc(c.type === 'telegram' ? 'chat ' + (c.config.chat_id || '') : c.type === 'email' ? c.config.to : c.type === 'sms_at' ? c.config.to : (c.config.url || ''));
  el.innerHTML = `<div class="tiles">
      <div class="tile"><div class="l">Wrong now</div><div class="v" style="color:var(${act.length ? crit ? '--bad' : '--warn' : '--ok'})">${act.length}</div><div class="s">${crit} critical</div></div>
      <div class="tile"><div class="l">Channels</div><div class="v" style="color:var(${chans.filter(c => c.enabled).length ? '--ok' : '--warn'})">${chans.filter(c => c.enabled).length}</div><div class="s">${chans.length ? chans.map(c => esc(c.name)).join(' · ') : 'none yet: add one to get alerts'}</div></div>
      <div class="tile"><div class="l">My checks</div><div class="v">${d.checks.length}</div><div class="s">${d.checks.filter(c => c.state && c.state.problem).length} failing</div></div>
      <div class="tile"><div class="l">Last check</div><div class="v" style="font-size:20px">${d.last_run ? ago(d.last_run) : 'starting'}</div><div class="s">every minute${d.quiet.on ? ` · quiet ${esc(d.quiet.from)}–${esc(d.quiet.to)}` : ''}</div></div></div>
    ${chans.length ? '' : `<div class="banner">No channel yet, so nothing is sent. ${adm ? 'Press <b>+ Channel</b> and add Telegram, email, SMS, Slack or Teams.' : 'An admin can add one.'}</div>`}
    <div class="card"><h3>Wrong now <span class="muted" style="font-weight:400;font-size:12px">sent once, reminded while it lasts, and "resolved" when it clears</span></h3><div id="alAct"></div></div>
    <div class="card"><h3>Channels <span class="muted" style="font-weight:400;font-size:12px">where alerts go</span></h3>
      ${chans.length ? `<table class="tbl"><thead><tr><th>Name</th><th>Kind</th><th>Sends to</th><th>From</th><th></th></tr></thead><tbody>${chans.map(c => `<tr>
        <td><b>${esc(c.name)}</b>${c.enabled ? '' : ' <span class="pill">off</span>'}</td><td>${esc(d.channel_types[c.type].label)}</td><td class="mono" style="font-size:12px">${chanTo(c)}</td>
        <td>${c.min === 'info' ? '<span class="faint">every alert</span>' : alSevPill(c.min) + ' and up'}</td>
        <td class="acts">${adm ? `<button class="btn sm" data-ct="${c.id}">Send test</button><button class="btn sm" data-ce="${c.id}">Edit</button><button class="btn sm danger" data-cd="${c.id}">Remove</button>` : ''}</td></tr>`).join('')}</tbody></table>`
        : '<p class="muted">Telegram is the quickest: free, instant on every phone, and good for a NOC group. Email, SMS (Africa\'s Talking), Slack, Teams, Discord and any webhook work too.</p>'}</div>
    <div class="card" id="alRules"><h3>What is alerted, and to whom <span class="sp"></span>${adm ? '<button class="btn sm primary" id="alRS">Save rules</button>' : ''}</h3>
      <p class="muted" style="font-size:12.5px">"Only if it mentions" / "never if it mentions" match the link, its description, the router, the site or the address: e.g. only <code>UPSTREAM, AlphaNet</code>, never <code>TEST</code>. Separate words with commas.</p>
      <div style="overflow-x:auto"><table class="tbl alrules"><thead><tr><th>On</th><th>When</th><th>From</th><th>Send to</th><th>Only if it mentions</th><th>Never if it mentions</th><th>Remind every</th></tr></thead><tbody>
      ${d.rules.map(r => `<tr data-k="${r.kind}"><td><input type="checkbox" class="arOn" ${r.enabled ? 'checked' : ''} ${adm ? '' : 'disabled'}></td>
        <td><b>${esc(d.kinds[r.kind].label)}</b><div class="faint" style="font-size:11.5px">${esc(d.kinds[r.kind].help)}</div></td>
        <td><select class="arMin" ${adm ? '' : 'disabled'}>${['warning', 'critical'].map(s => `<option ${r.min === s ? 'selected' : ''}>${s}</option>`).join('')}</select></td>
        <td class="arTo">${chans.length ? `<label><input type="checkbox" value="*" ${r.channels.includes('*') ? 'checked' : ''} ${adm ? '' : 'disabled'}> all</label>${chans.map(c => `<label><input type="checkbox" value="${c.id}" ${r.channels.includes(c.id) ? 'checked' : ''} ${adm ? '' : 'disabled'}> ${esc(c.name)}</label>`).join('')}` : '<span class="faint">add a channel</span>'}</td>
        <td><input class="arInc" value="${esc(r.include)}" placeholder="all" ${adm ? '' : 'disabled'}></td><td><input class="arExc" value="${esc(r.exclude)}" placeholder="—" ${adm ? '' : 'disabled'}></td>
        <td><select class="arRem" ${adm ? '' : 'disabled'}>${[[0, 'never'], [15, '15 min'], [30, '30 min'], [60, '1 h'], [120, '2 h'], [240, '4 h'], [720, '12 h']].map(([v, l]) => `<option value="${v}" ${+r.remind === v ? 'selected' : ''}>${l}</option>`).join('')}</select></td></tr>`).join('')}
      </tbody></table></div></div>
    <div class="card"><h3>My checks <span class="muted" style="font-weight:400;font-size:12px">anything else to watch: an address, a port, a web page</span><span class="sp"></span>${adm ? '<button class="btn sm" id="alAddK">+ Check</button>' : ''}</h3>
      ${d.checks.length ? `<table class="tbl"><thead><tr><th>Name</th><th>Checks</th><th>Limits</th><th>Now</th><th></th></tr></thead><tbody>${d.checks.map(k => { const s = k.state;
        return `<tr><td><b>${esc(k.name)}</b>${k.enabled ? '' : ' <span class="pill">off</span>'}</td><td>${esc(d.check_types[k.type])} <span class="mono faint">${esc(k.target)}${k.type === 'tcp' ? ':' + k.port : ''}</span></td>
        <td class="faint">${[k.ms != null ? `over ${k.ms} ms` : '', k.loss != null ? `${k.loss}% loss` : '', k.type === 'ping' ? 'no reply' : k.type === 'tcp' ? 'closed' : 'error page'].filter(Boolean).join(' · ')} → ${alSevPill(k.severity)}</td>
        <td>${!s ? '<span class="faint">checking…</span>' : s.problem ? `<span style="color:var(--bad)">● ${esc(s.problem)}</span>${s.fails < 2 ? ' <span class="faint">(alerts if it fails again)</span>' : ''}` : `<span style="color:var(--ok)">● ok</span> <span class="faint">${s.avg != null ? s.avg.toFixed(1) + ' ms' : ''}${s.status ? ' · ' + s.status : ''}</span>`}</td>
        <td class="acts">${adm ? `<button class="btn sm" data-ke="${k.id}">Edit</button><button class="btn sm danger" data-kd="${k.id}">Remove</button>` : ''}</td></tr>`; }).join('')}</tbody></table>`
        : '<p class="muted">Ping a customer\'s CPE, an upstream or a server with your own latency and loss limits, check that a port answers (SSH 22, Winbox 8291, RADIUS web, Splynx 443), or that a web page loads. Each is checked every minute and alerts after two failures in a row.</p>'}</div>
    <div class="card" id="alQuiet"><h3>Quiet hours <span class="sp"></span>${adm ? '<button class="btn sm" id="alQS">Save</button>' : ''}</h3>
      <div class="row" style="gap:10px;flex-wrap:wrap;align-items:center"><label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="alQon" ${d.quiet.on ? 'checked' : ''} ${adm ? '' : 'disabled'}> Hold back warnings</label>
      from <input id="alQf" type="time" value="${esc(d.quiet.from)}" style="width:110px" ${adm ? '' : 'disabled'}> to <input id="alQt" type="time" value="${esc(d.quiet.to)}" style="width:110px" ${adm ? '' : 'disabled'}>
      <select id="alTz" ${adm ? '' : 'disabled'}>${[-300, -240, 0, 60, 120, 180, 240, 330, 480].map(m => `<option value="${m}" ${d.tz === m ? 'selected' : ''}>UTC${m >= 0 ? '+' : '−'}${String(Math.floor(Math.abs(m) / 60)).padStart(2, '0')}:${String(Math.abs(m) % 60).padStart(2, '0')}${m === 180 ? ' (Nairobi)' : ''}</option>`).join('')}</select></div>
      <p class="muted" style="font-size:12.5px">Critical alerts always go out. Warnings that start in quiet hours are sent when they end, if they are still there.</p></div>
    <div class="card"><h3>Sent and cleared <span class="muted" style="font-weight:400;font-size:12px">the last 300</span></h3><div id="alHist"></div></div>`;
  table($('#alAct'), [{ t: 'Severity', k: 'severity', f: a => alSevPill(a.severity) }, { t: 'Problem', k: 'title', cls: 'wrap', f: a => `<b>${esc(a.title)}</b><div class="muted">${esc(a.detail || '')}</div>` },
    { t: 'Where', k: 'where' }, { t: 'Since', k: 'first', f: a => `${ago(a.first)} <span class="faint">${alDur(Date.now() / 1000 - a.first)}</span>` },
    { t: 'Sent to', sort: false, f: a => a.sent_at ? a.channels.map(cname).map(esc).join(', ') : `<span class="faint">${d.quiet.on && a.severity !== 'critical' ? 'held (quiet hours or no channel)' : 'no channel'}</span>` },
    { t: '', sort: false, cls: 'acts', f: a => a.acked_by ? `<span class="faint">acknowledged by ${esc(a.acked_by)}</span>` : ME && ME.role !== 'viewer' ? `<button class="btn sm" data-ack="${esc(a.key)}">Acknowledge</button>` : '' }],
    act, { empty: 'Nothing wrong right now.' });
  const HT = { problem: '🔴 sent', worse: '🔴 now critical', reminder: '⏰ reminder', resolved: '✅ resolved', opened: '• started', ack: '👤 acknowledged', test: '✉ test' };
  table($('#alHist'), [{ t: 'When', k: 'ts', f: h => `${new Date(h.ts * 1000).toLocaleString()}` }, { t: '', k: 'type', f: h => HT[h.type] || esc(h.type) },
    { t: 'Alert', k: 'title', cls: 'wrap', f: h => `${h.severity ? alSevPill(h.severity) + ' ' : ''}${esc(h.title || '')}${h.where ? ` <span class="faint">@ ${esc(h.where)}</span>` : ''}${h.after ? ` <span class="faint">after ${alDur(h.after)}</span>` : ''}${h.who ? ` <span class="faint">by ${esc(h.who)}</span>` : ''}` },
    { t: 'Delivery', sort: false, cls: 'wrap', f: h => (h.channels || []).map(c => c.ok ? `<span style="color:var(--ok)">✓ ${esc(c.name)}</span>` : `<span style="color:var(--bad)" title="${esc(c.error)}">✗ ${esc(c.name)}: ${esc((c.error || '').slice(0, 80))}</span>`).join(' ') }],
    d.history, { empty: 'Nothing sent yet.' });
  el.querySelectorAll('[data-ack]').forEach(b => b.onclick = async () => { try { AL.d = await post('/api/alerts/ack', { key: b.dataset.ack }); alDraw(); toast('Acknowledged: no more reminders for it'); } catch (e) { toast(e.message, true); } });
  el.querySelectorAll('[data-ct]').forEach(b => b.onclick = async () => { b.disabled = true; try { const r = await post('/api/alerts/test', { id: b.dataset.ct }); toast(r.ok ? 'Test sent. Check that it arrived.' : 'Not sent: ' + r.error, !r.ok); await alLoad(); alDraw(); } catch (e) { toast(e.message, true); } b.disabled = false; });
  el.querySelectorAll('[data-ce]').forEach(b => b.onclick = () => alChannelForm(chans.find(c => c.id === b.dataset.ce)));
  el.querySelectorAll('[data-cd]').forEach(b => b.onclick = async () => { const c = chans.find(x => x.id === b.dataset.cd); if (!confirm(`Remove the channel ${c.name}? Rules stop sending to it.`)) return;
    AL.d = await api('/api/alerts/channels/' + c.id, { method: 'DELETE' }); alDraw(); toast('Removed'); });
  el.querySelectorAll('[data-ke]').forEach(b => b.onclick = () => alCheckForm(d.checks.find(k => k.id === b.dataset.ke)));
  el.querySelectorAll('[data-kd]').forEach(b => b.onclick = async () => { AL.d = await api('/api/alerts/checks/' + b.dataset.kd, { method: 'DELETE' }); alDraw(); toast('Removed'); });
  if ($('#alAddK')) $('#alAddK').onclick = () => alCheckForm(null);
  const rules = $('#alRules'); rules.oninput = rules.onchange = e => { rules.classList.add('dirty');
    const all = e.target.closest('.arTo') && e.target.value === '*' && e.target.checked; if (all) e.target.closest('.arTo').querySelectorAll('input:not([value="*"])').forEach(x => x.checked = false);
    else if (e.target.closest('.arTo') && e.target.checked) { const a = e.target.closest('.arTo').querySelector('[value="*"]'); if (a) a.checked = false; } };
  if ($('#alRS')) $('#alRS').onclick = async () => {
    const rows = [...rules.querySelectorAll('tr[data-k]')].map(tr => ({ kind: tr.dataset.k, enabled: tr.querySelector('.arOn').checked, min: tr.querySelector('.arMin').value,
      channels: [...tr.querySelectorAll('.arTo input:checked')].map(x => x.value), include: tr.querySelector('.arInc').value, exclude: tr.querySelector('.arExc').value, remind: +tr.querySelector('.arRem').value }));
    try { AL.d = await post('/api/alerts/rules', { rules: rows }); rules.classList.remove('dirty'); alDraw(); toast('Rules saved'); } catch (e) { toast(e.message, true); } };
  const q = $('#alQuiet'); q.oninput = q.onchange = () => q.classList.add('dirty');
  if ($('#alQS')) $('#alQS').onclick = async () => { try { AL.d = await post('/api/alerts/settings', { quiet: { on: $('#alQon').checked, from: $('#alQf').value, to: $('#alQt').value }, tz: +$('#alTz').value });
    q.classList.remove('dirty'); alDraw(); toast('Saved'); } catch (e) { toast(e.message, true); } };
}

function alChannelForm(c) {
  const m = $('#modal'), types = AL.d.channel_types;
  let t = c ? c.type : 'telegram';
  const fields = () => types[t].fields.map(f => { const v = c && c.type === t ? c.config[f.key] || '' : '';
    if (f.key === 'security') return `<label>${esc(f.label)}</label><select id="acf_security">${[['starttls', 'STARTTLS (port 587)'], ['ssl', 'SSL/TLS (port 465)'], ['none', 'None (port 25)']].map(([k, l]) => `<option value="${k}" ${v === k ? 'selected' : ''}>${l}</option>`).join('')}</select>`;
    return `<label>${esc(f.label)}</label><input id="acf_${f.key}" value="${esc(v)}" ${f.secret ? 'type="password" autocomplete="new-password"' : ''} placeholder="${f.secret && c && c.type === t ? 'unchanged' : esc({ chat_id: '-1001234567890', host: 'smtp.gmail.com', port: '587', to: t === 'email' ? 'noc@example.co.ke, oncall@example.co.ke' : '+254711000000, +254722000000', username: 'sandbox', url: 'https://…', sender: 'NE8000 alerts <alerts@example.co.ke>', sender_id: 'SAVENET' }[f.key] || '')}">`; }).join('');
  const draw = () => {
    m.innerHTML = `<h2>${c ? 'Change ' + esc(c.name) : 'Add a channel'}</h2>
      <div class="form"><label>Kind</label><select id="acf_type" ${c ? 'disabled' : ''}>${Object.entries(types).map(([k, v]) => `<option value="${k}" ${k === t ? 'selected' : ''}>${esc(v.label)}</option>`).join('')}</select>
      <div class="help">${AL_HELP[t]}</div>
      <label>Name</label><input id="acf_name" value="${esc(c ? c.name : '')}" placeholder="${t === 'telegram' ? 'NOC group' : t === 'sms_at' ? 'On-call SMS' : t === 'email' ? 'NOC email' : types[t].label}">
      ${fields()}
      <label>Send</label><select id="acf_min">${[['info', 'every alert'], ['warning', 'warnings and critical'], ['critical', 'critical only']].map(([k, l]) => `<option value="${k}" ${(c ? c.min : 'info') === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <label style="display:flex;gap:6px;align-items:center;font-weight:400"><input type="checkbox" id="acf_on" ${!c || c.enabled ? 'checked' : ''}> On</label>
      <div class="help">Secrets (tokens, passwords, keys, webhook URLs) are kept on this server only and never shown again.</div></div>
      <div class="err"></div><div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn" data-a="t">Send test</button><button class="btn primary" data-a="s">Save</button></div>`;
    $('#acf_type').onchange = () => { t = $('#acf_type').value; draw(); };
    m.querySelector('.actions').onclick = async e => {
      const a = e.target.dataset.a; if (a === 'c') return closeModal(); if (!a) return;
      const body = { id: c ? c.id : '', type: t, name: $('#acf_name').value, min: $('#acf_min').value, enabled: $('#acf_on').checked,
        config: Object.fromEntries(types[t].fields.map(f => [f.key, ($('#acf_' + f.key) || {}).value || ''])) };
      m.querySelector('.err').textContent = '';
      try {
        if (a === 't') { e.target.disabled = true; const r = await post('/api/alerts/test', body); e.target.disabled = false;
          if (r.ok) toast('Test sent. Check that it arrived.'); else m.querySelector('.err').textContent = 'Not sent: ' + r.error; return; }
        AL.d = await post('/api/alerts/channels', body); closeModal(); alDraw(); toast('Channel saved. Press Send test to try it.');
      } catch (err) { e.target.disabled = false; m.querySelector('.err').textContent = err.message; }
    };
  };
  draw(); $('#modalBg').classList.remove('hidden');
}

function alCheckForm(k) {
  const m = $('#modal'); let t = k ? k.type : 'ping';
  const draw = () => {
    const v = f => esc(k && k[f] != null ? k[f] : '');
    m.innerHTML = `<h2>${k ? 'Change ' + esc(k.name) : 'Add a check'}</h2><div class="form">
      <label>Check</label><select id="akf_type">${Object.entries(AL.d.check_types).map(([x, l]) => `<option value="${x}" ${x === t ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>
      <label>Name</label><input id="akf_name" value="${v('name')}" placeholder="${t === 'ping' ? 'AlphaNet CPE' : t === 'tcp' ? 'Splynx SSH' : 'Customer portal'}">
      <label>${t === 'http' ? 'URL' : 'Address or name'}</label><input id="akf_target" value="${v('target')}" placeholder="${t === 'http' ? 'https://portal.example.co.ke' : '102.216.117.2'}">
      ${t === 'tcp' ? `<label>Port</label><input id="akf_port" value="${v('port') || ''}" placeholder="22">` : ''}
      <label>${t === 'ping' ? 'Alert when latency is over (ms)' : 'Alert when it takes over (ms)'}</label><input id="akf_ms" value="${v('ms')}" placeholder="${t === 'ping' ? '50' : 'leave empty for no limit'}">
      ${t === 'ping' ? `<label>Alert when loss is at least (%)</label><input id="akf_loss" value="${v('loss')}" placeholder="20">` : ''}
      <div class="help">${t === 'ping' ? 'No reply at all always alerts.' : t === 'tcp' ? 'A port that refuses or does not answer within 5 s alerts.' : 'An error page (400 and up) or no answer within 10 s alerts.'} It is checked every minute and alerts after two failures in a row.</div>
      <label>Severity</label><select id="akf_sev">${['critical', 'warning'].map(s => `<option ${(k ? k.severity : 'critical') === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
      <label style="display:flex;gap:6px;align-items:center;font-weight:400"><input type="checkbox" id="akf_on" ${!k || k.enabled ? 'checked' : ''}> On</label></div>
      <div class="err"></div><div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Save</button></div>`;
    $('#akf_type').onchange = () => { t = $('#akf_type').value; draw(); };
    m.querySelector('.actions').onclick = async e => {
      const a = e.target.dataset.a; if (a === 'c') return closeModal(); if (a !== 's') return;
      const g = id => ($('#' + id) || {}).value || '';
      try { AL.d = await post('/api/alerts/checks', { id: k ? k.id : '', type: t, name: g('akf_name'), target: g('akf_target'), port: g('akf_port'), ms: g('akf_ms'), loss: g('akf_loss'), severity: g('akf_sev'), enabled: $('#akf_on').checked });
        closeModal(); alDraw(); toast('Check saved; first result within a minute'); post('/api/alerts/run').catch(() => {}); }
      catch (err) { m.querySelector('.err').textContent = err.message; }
    };
  };
  draw(); $('#modalBg').classList.remove('hidden');
}
