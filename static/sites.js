/* NOC "Major sites": how Google, YouTube, Facebook and the other big sites are reached, on a map of the region.
   Nairobi, the Mombasa landing, the submarine cables up the coast to Djibouti and on to Europe, the Gulf and
   south to Johannesburg; a glowing route to each site with traffic moving along it, coloured by its delay.
   Where a site is served from is estimated from its delay (light in fibre: about 1 ms per 100 km of round trip);
   the side panel shows the real path (each hop, and the upstream it leaves by). */
const SW = { data: null, sel: null, z: 1, cx: null, cy: null, at: 0 };
const SW_PLACES = {
  NBO: { ll: [-1.29, 36.82], label: 'NBO' }, MBA: { ll: [-4.04, 39.67], label: 'MBA' }, JIB: { ll: [11.59, 43.15], label: 'JIB' },
  JNB: { ll: [-26.2, 28.05], label: 'JNB' }, FJR: { ll: [25.12, 56.33], label: 'FUJAIRAH' }, EU: { ll: [30.6, 32.3], label: 'TO EUROPE' },
};
// the main submarine cable routes, simplified ([lat, lon])
const SW_CABLES = {
  north: [[-4.04, 39.67], [-3.0, 40.6], [-1.5, 42.4], [1.5, 45.6], [5.5, 49.2], [9.5, 51.6], [11.9, 51.5], [12.3, 47.5], [11.8, 44.6], [11.59, 43.15]],
  redsea: [[11.59, 43.15], [12.7, 43.3], [15.2, 41.9], [19.0, 39.3], [23.0, 37.0], [27.0, 34.4], [29.9, 32.6], [30.6, 32.3]],
  gulf: [[12.3, 47.5], [13.6, 51.5], [16.0, 55.5], [20.5, 59.2], [23.6, 59.6], [25.12, 56.33]],
  south: [[-4.04, 39.67], [-6.8, 39.6], [-10.3, 40.6], [-15.0, 41.0], [-20.0, 36.0], [-25.0, 33.6], [-25.97, 32.6]],
};
const SW_ROUTES = {   // NBO to each place: overland to the landing, then along the cables
  NBO: [[-1.29, 36.82]], MBA: [[-1.29, 36.82], [-2.7, 38.3], [-4.04, 39.67]],
  JIB: null, JNB: null, FJR: null, EU: null,
};
SW_ROUTES.JIB = SW_ROUTES.MBA.concat(SW_CABLES.north.slice(1));
SW_ROUTES.EU = SW_ROUTES.JIB.concat(SW_CABLES.redsea.slice(1));
SW_ROUTES.FJR = SW_ROUTES.MBA.concat(SW_CABLES.north.slice(1, 8), SW_CABLES.gulf.slice(1));
SW_ROUTES.JNB = SW_ROUTES.MBA.concat(SW_CABLES.south.slice(1), [[-26.2, 28.05]]);
const swWhere = ms => ms == null ? null : ms <= 12 ? 'NBO' : ms <= 35 ? 'MBA' : ms <= 75 ? 'JNB' : ms <= 115 ? 'FJR' : 'EU';
const SW_WHERE_TXT = { NBO: 'in Nairobi (a cache or the exchange point)', MBA: 'at the coast (Mombasa)', JNB: 'in South Africa (about 50-75 ms away)',
  FJR: 'in the Gulf (about 75-115 ms away)', EU: 'in Europe (over 115 ms away)' };
const swXY = ll => [(ll[1] - KENYA_MAP.lon0) * KENYA_MAP.k, (KENYA_MAP.lat0 - ll[0]) * KENYA_MAP.k];
const swPath = pts => pts.map((p, i) => (i ? 'L' : 'M') + swXY(p).map(v => v.toFixed(0)).join(' ')).join('');
const swState = s => s.avg == null || s.loss === 100 ? 'down' : s.loss >= 20 || s.normal && s.avg > s.normal * 1.5 + 15 ? 'bad' : 'ok';

async function swLoad() { try { SW.data = await api('/api/sites'); SW.at = Date.now(); } catch (e) { } if (NM.sites) nmDraw(); }
// the upstream a path leaves by: a hop that is one of the router's BGP peers (or in the same /30 as one)
function swUpstream(hops) {
  const b = NM.bgpView; if (!b || !hops) return '';
  const desc = {}; Object.values(b.config || {}).flat().forEach(l => { const m = String(l).match(/^peer (\S+) description (.+)$/); if (m) desc[m[1]] = m[2]; });
  for (const h of hops) {
    const x = nmIp4(h.ip); if (x == null) continue;
    const p = (b.peers || []).find(q => { const y = nmIp4(q.peer); return y != null && (y === x || (y >>> 2) === (x >>> 2)); });
    if (p) return `${desc[p.peer] || 'AS' + (p.as || '?')} (${p.peer})`;
  }
  return '';
}
function sitesDraw(host) {
  host.classList.add('geo', 'swmap'); host.classList.remove('nogeo');
  const RB = KENYA_MAP.region_box, hw = host.clientWidth || 800, hh = host.clientHeight || 600;
  if (SW.cx == null) { const c = swXY([11, 42]); SW.cx = c[0]; SW.cy = c[1]; }
  const base = Math.max(3300 / hw, 4300 / hh), px = base / SW.z;
  const vb = () => `${SW.cx - hw * px / 2} ${SW.cy - hh * px / 2} ${hw * px} ${hh * px}`;
  const sites = (SW.data && SW.data.sites) || [];
  // where each site sits: around its place, fanned out so the labels do not sit on each other
  const groups = {}; sites.forEach(s => { const w = swWhere(s.avg) || 'NBO'; (groups[w] = groups[w] || []).push(s); });
  const pos = {};
  // each place's sites in a column beside it (left of Nairobi, right of the others), one label per row
  Object.entries(groups).forEach(([w, list]) => {
    const [x, y] = swXY(SW_PLACES[w].ll), left = w === 'NBO', dx = (left ? -150 : 150) * px, n = list.length;
    list.forEach((s, i) => { pos[s.host] = [x + dx, y + (w === 'EU' ? i + 1 : i - (n - 1) / 2) * 24 * px, w, left]; });
  });
  const cable = Object.values(SW_CABLES).map(c => [0, 5, -5, 10].map(o => `<path class="swcab" d="${swPath(c)}" transform="translate(${o},${o / 2})"/>`).join('')).join('');
  const subs = [[-1.5, 42.4], [5.5, 49.2], [12.3, 47.5], [19.0, 39.3], [16.0, 55.5], [-15.0, 41.0], [-20.0, 36.0], [9.5, 51.6]].map(ll => { const [x, y] = swXY(ll);
    return `<g class="swsub" transform="translate(${x + 18},${y}) scale(${0.9 * px})"><path d="M-9 0 Q-9 -4 0 -4 Q9 -4 10 0 Q9 4 0 4 Q-9 4 -9 0Z"/><rect x="-2" y="-8" width="4" height="5" rx="1"/></g>`; }).join('');
  const routes = sites.map((s, i) => {
    const w = pos[s.host] ? pos[s.host][2] : 'NBO', st = swState(s), pts = SW_ROUTES[w].map(swXY), end = pos[s.host];
    const d = 'M' + pts.concat([[end[0], end[1]]]).map(p => p.map(v => v.toFixed(0)).join(' ')).join('L'), id = 'swr' + i, sel = SW.sel === s.host;
    const dots = st === 'down' ? '' : [0, 1, 2].map(k => `<circle class="swdot" r="${(sel ? 9 : 6.5) * px}"><animateMotion dur="${(4 + i % 4).toFixed(1)}s" begin="-${(k * (4 + i % 4) / 3).toFixed(2)}s" repeatCount="indefinite"><mpath href="#${id}"/></animateMotion></circle>`).join('');
    return `<g class="swroute ${st}${SW.sel && !sel ? ' dim' : ''}${sel ? ' sel' : ''}" data-site="${esc(s.host)}"><path id="${id}" class="vis" d="${d}"/><path class="hit" d="${d}" style="stroke-width:${14 * px}"/>${dots}</g>`;
  }).join('');
  const pins = sites.map(s => { const [x, y] = pos[s.host], st = swState(s), ms = s.avg != null ? Math.round(s.avg) + ' ms' : 'no reply';
    return `<g class="swsite ${st}${SW.sel && SW.sel !== s.host ? ' dim' : ''}" data-site="${esc(s.host)}"><circle cx="${x}" cy="${y}" r="${6 * px}"/>
      <text x="${pos[s.host][3] ? x - 10 * px : x + 10 * px}" y="${y + 4 * px}" text-anchor="${pos[s.host][3] ? 'end' : 'start'}" style="font-size:${12 * px}px">${esc(s.name.toUpperCase())} <tspan class="ms">${esc(ms)}</tspan></text>
      ${st !== 'ok' ? nmBadge([{ severity: st === 'down' ? 'critical' : 'warning', title: st, detail: '' }], x - 12 * px, y - 12 * px, px * 1.1) : ''}</g>`; }).join('');
  const places = Object.entries(SW_PLACES).map(([k, p]) => { const [x, y] = swXY(p.ll);
    return `<g class="swplace${k === 'NBO' ? ' hub' : ''}"><circle cx="${x}" cy="${y}" r="${(k === 'NBO' ? 9 : 6) * px}"/><rect x="${x + 9 * px}" y="${y - 10 * px}" width="${(p.label.length * 8.6 + 10) * px}" height="${20 * px}" rx="${2 * px}"/>
      <text x="${x + 14 * px}" y="${y + 5 * px}" style="font-size:${13.5 * px}px">${p.label}</text></g>`; }).join('');
  host.innerHTML = `<svg id="swSvg" viewBox="${vb()}" width="${hw}" height="${hh}" preserveAspectRatio="xMidYMid meet">
    <defs><radialGradient id="swGlow"><stop offset="0" stop-color="#9fb4ff"/><stop offset=".35" stop-color="#2d5bff" stop-opacity=".9"/><stop offset="1" stop-color="#1030ff" stop-opacity="0"/></radialGradient>
      <radialGradient id="swSea" cx="80%" cy="60%" r="60%"><stop offset="0" stop-color="#1b1d22"/><stop offset="1" stop-color="#050506"/></radialGradient></defs>
    <rect x="${RB[0] - 4000}" y="${RB[1] - 4000}" width="${RB[2] + 8000}" height="${RB[3] + 8000}" fill="url(#swSea)" data-bg="1"/>
    <g class="swland">${KENYA_MAP.region.map(c => `<path d="${c.d}"><title>${esc(c.n)}</title></path>`).join('')}</g>
    <path class="swke" d="${KENYA_MAP.kenya}"/>
    <g class="swcables">${cable}${subs}</g>${routes}${places}${pins}
    <text class="gcredit" x="${SW.cx + hw * px / 2 - 8 * px}" y="${SW.cy + hh * px / 2 - 8 * px}" style="font-size:${9.5 * px}px">Places by delay · cables simplified · map: Natural Earth</text></svg>`;
  const svg = $('#swSvg');
  let pan = null;
  svg.onpointerdown = e => { pan = { x: e.clientX, y: e.clientY, cx: SW.cx, cy: SW.cy, moved: false }; svg.setPointerCapture(e.pointerId); };
  svg.onpointermove = e => { if (!pan) return; const dx = e.clientX - pan.x, dy = e.clientY - pan.y; if (Math.abs(dx) + Math.abs(dy) > 4) pan.moved = true;
    if (pan.moved) { SW.cx = pan.cx - dx * px; SW.cy = pan.cy - dy * px; svg.setAttribute('viewBox', vb()); } };
  svg.onpointerup = e => { const p = pan; pan = null; if (!p || p.moved) return;
    const el = document.elementFromPoint(e.clientX, e.clientY) || e.target, s = el.closest('[data-site]');
    SW.sel = s ? s.dataset.site : null; swSide(); nmDraw(); };
  svg.onwheel = e => { e.preventDefault(); swZoom(e.deltaY < 0 ? 1.2 : 1 / 1.2, e); };
}
function swZoom(f, e) {
  const svg = $('#swSvg'), z0 = SW.z, z1 = Math.max(0.6, Math.min(10, z0 * f)); if (z1 === z0) return;
  if (e && svg) { const r = svg.getBoundingClientRect(), RB = KENYA_MAP.region_box, b = Math.max(3300 / r.width, 4300 / r.height), p0 = b / z0, p1 = b / z1;
    SW.cx += (e.clientX - r.left - r.width / 2) * (p0 - p1); SW.cy += (e.clientY - r.top - r.height / 2) * (p0 - p1); }
  SW.z = z1; nmDraw();
}
function swFit() { SW.z = 1; SW.cx = null; SW.cy = null; nmDraw(); }
// the side panel: every site, or the one picked with its path and its day
function swSide() {
  const el = $('#ncS'); if (!el) return;
  const sites = (SW.data && SW.data.sites) || [], s = sites.find(x => x.host === SW.sel);
  const ms = v => v == null ? '-' : Math.round(v) + ' ms', cls = x => ({ ok: 'okc', bad: 'warnc', down: 'bad' })[swState(x)];
  if (!s) {
    el.innerHTML = `<h3>Major sites</h3><p class="muted" style="font-size:12px">Pinged and traced from the app's server every 5 minutes; its traffic goes out through the router. Click a site for its path.</p>
      ${sites.length ? `<table><tbody>${sites.map(x => `<tr data-h="${esc(x.host)}" style="cursor:pointer"><td><b>${esc(x.name)}</b><div class="faint" style="font-size:11.5px">${esc(swUpstream(x.hops) || x.host)}</div></td>
        <td style="text-align:right;white-space:nowrap" class="${cls(x)}"><b>${ms(x.avg)}</b>${x.loss ? `<div style="font-size:11.5px">${x.loss}% lost</div>` : ''}${x.normal ? `<div class="faint" style="font-size:11.5px">normally ${ms(x.normal)}</div>` : ''}</td></tr>`).join('')}</tbody></table>`
        : `<p class="muted">${SW.data ? 'The first check runs a minute after the app starts.' : 'Loading…'}</p>`}
      <button class="btn sm" id="swEdit" style="margin-top:8px">Edit the list</button>`;
    el.querySelectorAll('[data-h]').forEach(tr => tr.onclick = () => { SW.sel = tr.dataset.h; swSide(); nmDraw(); });
    $('#swEdit').onclick = swEdit;
    return;
  }
  const w = swWhere(s.avg), up = swUpstream(s.hops);
  el.innerHTML = `<h3>${esc(s.name)} <span class="muted" style="font-weight:500">${esc(s.host)}</span></h3>
    <div class="row" style="gap:18px;margin-bottom:8px"><div><div class="muted">Now</div><div class="big ${cls(s)}">${ms(s.avg)}</div></div><div><div class="muted">Normally</div><div class="big">${ms(s.normal)}</div></div><div><div class="muted">Lost</div><div class="big">${s.loss == null ? '-' : s.loss + '%'}</div></div></div>
    <div id="swG" style="height:110px"></div>
    <table><tbody><tr><td>Address</td><td>${esc(s.ip || '-')}</td></tr><tr><td>Leaves by</td><td>${esc(up || 'not one of the BGP peers')}</td></tr>
      <tr><td>Served from</td><td>${esc(w ? SW_WHERE_TXT[w] : '-')} <span class="faint">(by delay)</span></td></tr>${s.error ? `<tr><td>Note</td><td>${esc(s.error)}</td></tr>` : ''}</tbody></table>
    <h3 style="margin-top:10px">Path</h3>${s.hops.length ? `<table><tbody>${s.hops.map(h => `<tr><td>${h.hop}</td><td>${esc(h.ip || '*')}</td><td style="text-align:right">${h.ms != null ? h.ms + ' ms' : ''}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">No trace yet.</p>'}
    ${s.events.length ? `<h3 style="margin-top:10px">Changes</h3>${s.events.map(e => `<div class="witem ${e.kind === 'path' ? 'warn' : 'crit'}"><b>${esc(e.text)}</b> <span class="faint">${esc(ago(e.ts))}</span></div>`).join('')}` : ''}
    <button class="btn sm" id="swAll" style="margin-top:8px">← All sites</button>`;
  $('#swAll').onclick = () => { SW.sel = null; swSide(); nmDraw(); };
  if (s.series.length) new TimeChart($('#swG'), { fmt: v => Math.round(v) + ' ms', small: true }).set([{ name: 'Delay', color: '--s1', values: s.series.map(v => [v[0], v[1]]) }]);
}
function swEdit() {
  const m = $('#modal'), rows = ((SW.data && SW.data.sites) || []).map(s => `${s.name}, ${s.host}`).join('\n');
  m.innerHTML = `<h2>Major sites</h2><p class="muted">One per line: a name, a comma, and the host name or address.</p><textarea id="swT" style="width:100%;min-height:260px;font-family:monospace">${esc(rows)}</textarea><div class="err"></div>
    <div class="actions"><button class="btn" data-a="c">Cancel</button><button class="btn primary" data-a="s">Save</button></div>`;
  $('#modalBg').classList.remove('hidden');
  m.querySelector('.actions').onclick = async e => {
    if (e.target.dataset.a === 'c') return closeModal();
    if (e.target.dataset.a !== 's') return;
    const list = $('#swT').value.split('\n').map(l => l.trim()).filter(Boolean).map(l => { const i = l.lastIndexOf(','); return i > 0 ? { name: l.slice(0, i).trim(), host: l.slice(i + 1).trim() } : { name: l, host: l }; });
    try { await post('/api/sites', { sites: list }); closeModal(); toast('Saved. The new sites are checked now.'); swLoad(); } catch (err) { m.querySelector('.err').textContent = err.message; }
  };
}
