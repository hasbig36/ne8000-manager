/* NOC "Major sites": how Google, YouTube, Facebook and the other big sites are reached, on a map of the world.
   Nairobi, the Mombasa landing, and the main submarine cables: up the coast to Djibouti, the Red Sea and the
   Mediterranean to Marseille and over the Atlantic to Virginia, to the Gulf, to Mumbai and Singapore, and south to
   Johannesburg. A glowing route to each site with traffic moving along it, coloured by its state.
   Where a site is served from is estimated from its delay (light in fibre: about 1 ms per 100 km of round trip);
   the side panel shows the real path (each hop, and the upstream it leaves by) and what is wrong, with advice.
   Any other site can be checked on demand (the search) and added to the list. */
const SW = { data: null, sel: null, z: 1, cx: null, cy: null, at: 0, off: 0, extra: [], tick: null, polled: 0 };
const SW_PLACES = {
  NBO: { ll: [-1.29, 36.82], label: 'NBO', side: -1 }, MBA: { ll: [-4.04, 39.67], label: 'MBA', side: 1, col: [110, 46] }, JIB: { ll: [11.59, 43.15], label: 'JIB', side: 1 },
  JNB: { ll: [-26.2, 28.05], label: 'JNB', side: -1 }, FJR: { ll: [25.12, 56.33], label: 'FUJAIRAH', side: 1 }, BOM: { ll: [19.07, 72.88], label: 'MUMBAI', side: 1 },
  SIN: { ll: [1.35, 103.8], label: 'SINGAPORE', side: 1 }, EU: { ll: [43.3, 5.37], label: 'MARSEILLE', side: -1, col: [-120, -34] }, US: { ll: [39.04, -77.49], label: 'ASHBURN', side: -1 },
};
// the main submarine cable routes, simplified ([lat, lon])
const SW_CABLES = {
  north: [[-4.04, 39.67], [-3.0, 40.6], [-1.5, 42.4], [1.5, 45.6], [5.5, 49.2], [9.5, 51.6], [11.9, 51.5], [12.3, 47.5], [11.8, 44.6], [11.59, 43.15]],
  redsea: [[11.59, 43.15], [12.7, 43.3], [15.2, 41.9], [19.0, 39.3], [23.0, 37.0], [27.0, 34.4], [29.9, 32.6], [31.5, 32.3], [32.8, 29.0], [33.8, 24.0],
    [35.4, 18.0], [37.3, 11.6], [38.5, 8.0], [41.5, 6.5], [43.3, 5.37]],
  atlantic: [[43.3, 5.37], [41.8, 4.5], [38.0, 0.8], [36.3, -2.5], [35.95, -5.6], [36.4, -10.0], [38.5, -30.0], [39.8, -55.0], [37.5, -70.0], [36.85, -75.98], [39.04, -77.49]],
  gulf: [[9.5, 51.6], [13.0, 54.5], [16.0, 57.0], [20.5, 59.2], [22.7, 60.2], [24.6, 57.6], [25.12, 56.33]],
  asia: [[9.5, 51.6], [12.5, 57.0], [15.5, 64.0], [18.6, 71.5], [19.07, 72.88], [15.0, 72.6], [8.0, 76.0], [5.7, 80.5], [6.0, 90.0], [6.3, 94.5], [3.2, 100.6], [1.35, 103.8]],
  south: [[-4.04, 39.67], [-6.8, 39.6], [-10.3, 40.6], [-15.0, 41.0], [-20.0, 36.0], [-25.0, 33.6], [-25.97, 32.6]],
};
const SW_ROUTES = { NBO: [[-1.29, 36.82]], MBA: [[-1.29, 36.82], [-2.7, 38.3], [-4.04, 39.67]] };   // NBO to each place: overland to the landing, then the cables
SW_ROUTES.JIB = SW_ROUTES.MBA.concat(SW_CABLES.north.slice(1));
SW_ROUTES.EU = SW_ROUTES.JIB.concat(SW_CABLES.redsea.slice(1));
SW_ROUTES.US = SW_ROUTES.EU.concat(SW_CABLES.atlantic.slice(1));
SW_ROUTES.FJR = SW_ROUTES.MBA.concat(SW_CABLES.north.slice(1, 6), SW_CABLES.gulf.slice(1));
SW_ROUTES.SIN = SW_ROUTES.MBA.concat(SW_CABLES.north.slice(1, 6), SW_CABLES.asia.slice(1));
SW_ROUTES.JNB = SW_ROUTES.MBA.concat(SW_CABLES.south.slice(1), [[-26.2, 28.05]]);
const swWhere = ms => ms == null ? null : ms <= 12 ? 'NBO' : ms <= 35 ? 'MBA' : ms <= 70 ? 'JNB' : ms <= 100 ? 'FJR' : ms <= 210 ? 'EU' : ms <= 290 ? 'US' : 'SIN';
const SW_WHERE_TXT = { NBO: 'in Nairobi (a cache or the exchange point)', MBA: 'at the coast (Mombasa) or close by', JNB: 'in South Africa (about 40-70 ms away)',
  FJR: 'in the Gulf (about 70-100 ms away)', EU: 'in Europe (about 100-210 ms away)', US: 'in North America (about 210-290 ms away)', SIN: 'in Asia or further (over 290 ms away)' };
const SW_WHERE_SHORT = { NBO: 'Nairobi', MBA: 'Mombasa', JNB: 'South Africa', FJR: 'the Gulf', EU: 'Europe', US: 'North America', SIN: 'Asia' };
// big content providers that have caches or peering in Nairobi: far away means something is wrong with the route
const SW_BIG = /(^|\.)(google|youtube|googlevideo|gstatic|facebook|fbcdn|instagram|whatsapp|netflix|nflxvideo|tiktok|microsoft|office|live|apple|icloud|akamai|amazon|cloudflare)\.[a-z.]+$/;
const swLocal = s => /\.ke$/.test(s.host || '');
const swXY = ll => [(ll[1] - KENYA_MAP.lon0) * KENYA_MAP.k, (KENYA_MAP.lat0 - ll[0]) * KENYA_MAP.k];
const swPath = pts => pts.map((p, i) => (i ? 'L' : 'M') + swXY(p).map(v => v.toFixed(0)).join(' ')).join('');
const swMs = v => v == null ? '-' : Math.round(v) + ' ms';
const swClock = ts => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '-';
const swNow = () => (Date.now() + SW.off) / 1000;      // the server's clock

async function swLoad() {
  try { const d = await api('/api/sites'); SW.data = d; SW.at = Date.now(); if (d.now) SW.off = d.now * 1000 - Date.now(); } catch (e) { }
  if (NM.sites) nmDraw();
}
// BGP peer descriptions, from the configuration
function swDesc() {
  const b = NM.bgpView, desc = {}; if (!b) return desc;
  Object.values(b.config || {}).flat().forEach(l => { const m = String(l).match(/^peer (\S+) description (.+)$/); if (m) desc[m[1]] = m[2]; });
  return desc;
}
// the upstream a path leaves by: a hop that is one of the router's BGP peers (or in the same /30 as one)
function swUpstream(hops) {
  const b = NM.bgpView; if (!b || !hops) return '';
  const desc = swDesc();
  for (const h of hops) {
    const x = nmIp4(h.ip); if (x == null) continue;
    const p = (b.peers || []).find(q => { const y = nmIp4(q.peer); return y != null && (y === x || (y >>> 2) === (x >>> 2)); });
    if (p) return `${desc[p.peer] || 'AS' + (p.as || '?')} (${p.peer})`;
  }
  return '';
}
// the KIXP session (a peer described as KIXP): 'up', 'down', 'none', or null when BGP is not known yet
function swKixp() {
  const b = NM.bgpView; if (!b) return null;
  const desc = swDesc(), k = (b.peers || []).filter(p => /kixp/i.test(desc[p.peer] || p.desc || ''));
  return !k.length ? 'none' : k.some(p => p.state === 'Established') ? 'up' : 'down';
}
// what is wrong with a site, and what to do about it (the same shape as the map's other warnings)
function swIssues(s) {
  const out = [], ms = swMs(s.avg), up = swUpstream(s.hops), w = swWhere(s.avg), kx = swKixp();
  const via = up ? ` through ${up}` : '', upName = up ? up.replace(/ \(.*$/, '') : 'your upstream';
  const kixpTxt = kx === 'down' ? ' Your KIXP session is down at the moment, so this cannot go over KIXP now.' : kx === 'none' ? ' The router has no KIXP session.' : '';
  if (s.avg == null || s.loss === 100) {
    out.push({ severity: 'critical', title: 'No reply', detail: s.error || `${s.host} did not answer the pings.`,
      advice: 'Some sites do not answer pings at all; if the site opens fine for customers this is not a fault. Otherwise look at the path for the last hop that answers.' });
    return out;
  }
  if (s.loss >= 20) out.push({ severity: 'warning', title: `${s.loss}% of pings lost`, detail: `Pings to ${s.host} are being lost on the way${via}. Customers get slow or stalling pages and calls.`,
    advice: 'Look at the path: the hop where the delay or loss starts is where the trouble is. If it is in the upstream, send them this trace.' });
  if (swLocal(s) && s.avg > 40) out.push({ severity: 'warning', title: 'Kenyan site reached the long way',
    detail: `${s.name} is hosted in Kenya, but it answers in ${ms}: traffic goes out to about ${SW_WHERE_SHORT[w] || 'far away'}${via} and comes back. Inside Kenya it should take under 10 ms.`,
    advice: `Get a local route: peer with them at KIXP, or ask ${upName} to reach ${s.name} inside Kenya.${kixpTxt}` });
  else if (SW_BIG.test(s.host) && s.avg > 60) out.push({ severity: 'warning', title: 'Served from far away',
    detail: `${s.name} has caches in Nairobi (Google Global Cache, Meta, Netflix Open Connect and others), but this answers in ${ms}, about ${SW_WHERE_TXT[w] || 'far away'}${via}. Customers get slower pages and video.`,
    advice: `Ask ${upName} why ${s.name} is not served from its Nairobi cache, or peer with ${s.name} at KIXP. From Nairobi it should take under 15 ms.${kixpTxt}` });
  if (s.normal && s.avg > s.normal * 1.5 + 15) out.push({ severity: 'warning', title: 'Slower than normal', detail: `${ms} now, normally ${swMs(s.normal)}.`,
    advice: 'Usually congestion on the upstream, or the site moved to a server further away. Compare the path with earlier and check the upstream\'s traffic.' });
  const pe = (s.events || []).find(e => e.kind === 'path' && swNow() - e.ts < 3600);
  if (pe) out.push({ severity: 'warning', title: 'The path changed', detail: `${pe.text} (${ago(pe.ts)}).`, advice: 'The upstream or the site changed the route. Nothing to do if the delay stayed the same.' });
  return out;
}
const swState = s => s.avg == null || s.loss === 100 ? 'down' : swIssues(s).length ? 'bad' : 'ok';
// the sites on the list, then the ones searched for this visit
function swAll() {
  const list = (SW.data && SW.data.sites) || [];
  return list.concat(SW.extra.filter(x => !list.some(y => y.host === x.host)));
}
const SW_VIEW = [-180, 76, 180, -50];     // the part of the world a fit shows: lon, lat top left to bottom right
const swBase = (w, h) => { const a = swXY([SW_VIEW[1], SW_VIEW[0]]), b = swXY([SW_VIEW[3], SW_VIEW[2]]); return Math.max((b[0] - a[0]) / w, (b[1] - a[1]) / h) * 1.02; };
function sitesDraw(host) {
  host.classList.add('geo', 'swmap'); host.classList.remove('nogeo');
  const WB = KENYA_MAP.world_box, hw = host.clientWidth || 800, hh = host.clientHeight || 600;
  if (SW.cx == null) { const a = swXY([SW_VIEW[1], SW_VIEW[0]]), b = swXY([SW_VIEW[3], SW_VIEW[2]]); SW.cx = (a[0] + b[0]) / 2; SW.cy = (a[1] + b[1]) / 2; }
  const px = swBase(hw, hh) / SW.z;
  const vb = () => `${SW.cx - hw * px / 2} ${SW.cy - hh * px / 2} ${hw * px} ${hh * px}`;
  const sites = swAll();
  // where each site sits: Kenyan sites by Nairobi, the rest by the place their delay points to, in a column beside it
  const groups = {}, far = {};
  sites.forEach(s => { const w = swWhere(s.avg) || 'NBO', g = swLocal(s) ? 'NBO' : w; if (swLocal(s) && !/NBO|MBA/.test(w)) far[s.host] = w; (groups[g] = groups[g] || []).push(s); });
  const pos = {};
  Object.entries(groups).forEach(([w, list]) => {
    const p = SW_PLACES[w], [x, y] = swXY(p.ll), n = list.length, c = p.col || [p.side * 150, 0];
    const y0 = c[1] < 0 ? c[1] - (n - 1) * 24 : c[1] > 0 ? c[1] : -(n - 1) * 12;      // a column above, below or beside its place
    list.forEach((s, i) => { pos[s.host] = [x + c[0] * px, y + (y0 + i * 24) * px, w, p.side < 0]; });
  });
  const grid = [];
  for (let lon = -180; lon <= 180; lon += 30) grid.push(swPath([[84, lon], [-58, lon]]));
  for (let lat = -40; lat <= 80; lat += 20) grid.push(swPath([[lat, -180], [lat, 180]]));
  const cable = Object.values(SW_CABLES).map(c => [0, 3, -3].map(o => `<path class="swcab" d="${swPath(c)}" transform="translate(${o * px},${o * px / 2})"/>`).join('')).join('');
  const subs = [[-1.5, 42.4], [5.5, 49.2], [19.0, 39.3], [16.0, 57.0], [-15.0, 41.0], [33.8, 24.0], [38.5, -30.0], [39.8, -55.0], [15.5, 64.0], [6.0, 90.0]].map(ll => { const [x, y] = swXY(ll);
    return `<g class="swsub" transform="translate(${x + 14 * px},${y}) scale(${0.8 * px})"><path d="M-9 0 Q-9 -4 0 -4 Q9 -4 10 0 Q9 4 0 4 Q-9 4 -9 0Z"/><rect x="-2" y="-8" width="4" height="5" rx="1"/></g>`; }).join('');
  const routes = sites.map((s, i) => {
    const w = pos[s.host][2], st = swState(s), out = SW_ROUTES[far[s.host] || w], sel = SW.sel === s.host;
    const ll = far[s.host] ? out.concat(out.slice(0, -1).reverse()) : out;      // a Kenyan site served the long way: out and back
    const pts = ll.map(swXY).concat([[pos[s.host][0], pos[s.host][1]]]);
    let L = 0; for (let k = 1; k < pts.length; k++) L += Math.hypot(pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1]);
    const dur = Math.min(16, Math.max(3, 2.5 + L / 1500)).toFixed(1), id = 'swr' + i;
    const d = 'M' + pts.map(p => p.map(v => v.toFixed(0)).join(' ')).join('L');
    const dots = st === 'down' ? '' : [0, 1, 2].map(k => `<circle class="swdot" r="${(sel ? 8 : 5.5) * px}"><animateMotion dur="${dur}s" begin="-${(k * dur / 3).toFixed(2)}s" repeatCount="indefinite"><mpath href="#${id}"/></animateMotion></circle>`).join('');
    return `<g class="swroute ${st}${SW.sel && !sel ? ' dim' : ''}${sel ? ' sel' : ''}" data-site="${esc(s.host)}"><path id="${id}" class="vis" d="${d}"/><path class="hit" d="${d}" style="stroke-width:${14 * px}"/>${dots}</g>`;
  }).join('');
  const pins = sites.map(s => { const [x, y, , left] = pos[s.host], st = swState(s), ms = s.avg != null ? Math.round(s.avg) + ' ms' : 'no reply', iss = swIssues(s);
    return `<g class="swsite ${st}${s.extra ? ' extra' : ''}${SW.sel && SW.sel !== s.host ? ' dim' : ''}" data-site="${esc(s.host)}"><circle cx="${x}" cy="${y}" r="${6 * px}"/>
      <text x="${left ? x - 10 * px : x + 10 * px}" y="${y + 4 * px}" text-anchor="${left ? 'end' : 'start'}" style="font-size:${12 * px}px">${esc(s.name.toUpperCase())} <tspan class="ms">${esc(ms)}</tspan></text>
      ${nmBadge(iss, left ? x + 13 * px : x - 13 * px, y - 2 * px, px * 1.05)}</g>`; }).join('');
  const places = Object.entries(SW_PLACES).map(([k, p]) => { const [x, y] = swXY(p.ll);
    return `<g class="swplace${k === 'NBO' ? ' hub' : ''}"><circle cx="${x}" cy="${y}" r="${(k === 'NBO' ? 7 : 4.5) * px}"/><rect x="${x + 8 * px}" y="${y - 9 * px}" width="${(p.label.length * 7.6 + 10) * px}" height="${17 * px}" rx="${2 * px}"/>
      <text x="${x + 13 * px}" y="${y + 4 * px}" style="font-size:${12 * px}px">${p.label}</text></g>`; }).join('');
  const nIss = sites.filter(s => swState(s) !== 'ok').length;
  host.innerHTML = `<svg id="swSvg" viewBox="${vb()}" width="${hw}" height="${hh}" preserveAspectRatio="xMidYMid meet">
    <defs><radialGradient id="swGlow"><stop offset="0" stop-color="#9fb4ff"/><stop offset=".35" stop-color="#2d5bff" stop-opacity=".9"/><stop offset="1" stop-color="#1030ff" stop-opacity="0"/></radialGradient>
      <radialGradient id="swSea" cx="55%" cy="55%" r="65%"><stop offset="0" stop-color="#0d1524"/><stop offset="1" stop-color="#04060a"/></radialGradient></defs>
    <rect x="${WB[0] - 40000}" y="${WB[1] - 40000}" width="${WB[2] + 80000}" height="${WB[3] + 80000}" fill="#04060a" data-bg="1"/>
    <rect x="${WB[0]}" y="${WB[1]}" width="${WB[2]}" height="${WB[3]}" fill="url(#swSea)"/>
    <g class="swgrid">${grid.map(d => `<path d="${d}"/>`).join('')}</g>
    <g class="swland">${KENYA_MAP.world.map(c => `<path d="${c.d}"><title>${esc(c.n)}</title></path>`).join('')}</g>
    <path class="swke" d="${KENYA_MAP.kenya}"><title>Kenya</title></path>
    <g class="swcables">${cable}${subs}</g>${routes}${places}${pins}</svg>
    <div class="swlegend"><span><i class="ok"></i>Normal</span><span><i class="bad"></i>Needs a look</span><span><i class="down"></i>No reply</span>
      <span>${nIss ? `<b class="warnc">⚠ ${nIss} site${nIss > 1 ? 's' : ''}: click for why</b>` : 'All sites normal'}</span>
      <span class="faint">Placed by delay · cables simplified · map: Natural Earth</span></div>`;
  const svg = $('#swSvg');
  let pan = null;
  svg.onpointerdown = e => { pan = { x: e.clientX, y: e.clientY, cx: SW.cx, cy: SW.cy, moved: false }; svg.setPointerCapture(e.pointerId); };
  svg.onpointermove = e => { if (!pan) return; const dx = e.clientX - pan.x, dy = e.clientY - pan.y; if (Math.abs(dx) + Math.abs(dy) > 4) pan.moved = true;
    if (pan.moved) { SW.cx = pan.cx - dx * px; SW.cy = pan.cy - dy * px; svg.setAttribute('viewBox', vb()); } };
  svg.onpointerup = e => { const p = pan; pan = null; if (!p || p.moved) return;
    const el = document.elementFromPoint(e.clientX, e.clientY) || e.target, s = el.closest('[data-site]');
    SW.sel = s ? s.dataset.site : null; swSide(true); nmDraw(); };
  svg.onwheel = e => { e.preventDefault(); swZoom(e.deltaY < 0 ? 1.2 : 1 / 1.2, e); };
}
function swZoom(f, e) {
  const svg = $('#swSvg'), z0 = SW.z, z1 = Math.max(0.8, Math.min(40, z0 * f)); if (z1 === z0) return;
  if (e && svg) { const r = svg.getBoundingClientRect(), b = swBase(r.width, r.height), p0 = b / z0, p1 = b / z1;
    SW.cx += (e.clientX - r.left - r.width / 2) * (p0 - p1); SW.cy += (e.clientY - r.top - r.height / 2) * (p0 - p1); }
  SW.z = z1; nmDraw();
}
function swFit() { SW.z = 1; SW.cx = null; SW.cy = null; nmDraw(); }
// the last check, and a live countdown to the next one
function swTimeHtml() {
  const d = SW.data || {}, iv = d.interval || 300;
  return `<div class="swtime"><div><span class="muted">Last check</span> <b>${swClock(d.last_at)}</b> <span class="faint" id="swAgo"></span></div>
    <div><span class="muted">Next check</span> <b id="swCd">-</b></div><div class="swbar"><i id="swBar"></i></div>
    <div class="row" style="gap:6px;margin-top:6px"><label class="muted" style="font-size:12px">Every <select id="swInt">${[1, 2, 3, 4, 5].map(m => `<option value="${m * 60}"${m * 60 === iv ? ' selected' : ''}>${m} min</option>`).join('')}</select></label>
      <button class="btn sm" id="swRun">Check now</button></div>
    <div class="faint" style="font-size:11px;margin-top:3px">Pings at this interval, paths traced every ${Math.round((d.trace_every || 300) / 60)} min or when the delay jumps.</div></div>`;
}
function swTick() {
  const d = SW.data; if (!d) return;
  const now = swNow(), cd = $('#swCd'), ag = $('#swAgo'), bar = $('#swBar');
  if (ag && d.last_at) ag.textContent = `(${Math.max(0, Math.round(now - d.last_at))} s ago)`;
  if (cd) {
    const iv = d.interval || 300, left = d.next_at ? (d.demo ? ((d.next_at - now) % iv + iv) % iv : d.next_at - now) : null;
    cd.textContent = left == null ? '-' : d.running || left <= 0 ? 'checking now…' : `in ${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, '0')}`;
    if (bar) bar.style.width = left == null ? '0' : Math.max(0, Math.min(100, 100 - left / iv * 100)) + '%';
  }
  // the check is due or running: fetch the new results (every 8 s until they are in)
  if (!d.demo && d.next_at && (d.running || now > d.next_at + 3) && Date.now() - SW.polled > 8000) {
    SW.polled = Date.now(); const before = d.last_at;
    swLoad().then(() => { if (SW.data && SW.data.last_at !== before && NM.sites) swSide(); });
  }
}
// the side panel: every site, or the one picked with what is wrong, its path and its day
function swSide(force) {
  const el = $('#ncS'); if (!el) return;
  const ae = document.activeElement;
  if (!force && ae && el.contains(ae) && /INPUT|SELECT|TEXTAREA/.test(ae.tagName)) return;     // someone is typing
  if (!SW.tick) SW.tick = setInterval(() => { if ($('#ncS') && NM.sites) swTick(); }, 1000);
  const sites = swAll(), s = sites.find(x => x.host === SW.sel);
  const cls = x => ({ ok: 'okc', bad: 'warnc', down: 'bad' })[swState(x)];
  if (!s) {
    const list = (SW.data && SW.data.sites) || [], bad = list.filter(x => swState(x) !== 'ok').length;
    const row = x => `<tr data-h="${esc(x.host)}" style="cursor:pointer"><td><b>${swState(x) !== 'ok' ? '<span class="warnc">⚠</span> ' : ''}${esc(x.name)}</b><div class="faint" style="font-size:11.5px">${esc(swUpstream(x.hops) || x.host)}</div></td>
        <td style="text-align:right;white-space:nowrap" class="${cls(x)}"><b>${swMs(x.avg)}</b>${x.loss ? `<div style="font-size:11.5px">${x.loss}% lost</div>` : ''}${x.normal ? `<div class="faint" style="font-size:11.5px">normally ${swMs(x.normal)}</div>` : ''}</td></tr>`;
    el.innerHTML = `<h3>Major sites</h3>${swTimeHtml()}
      <form id="swF" class="row" style="gap:6px;margin:8px 0"><input id="swQ" placeholder="Check any site, e.g. bbc.com" style="flex:1;min-width:0" autocomplete="off"><button class="btn sm primary">Check</button></form>
      <div id="swMsg" class="muted" style="font-size:12px"></div>
      ${bad ? `<p class="warnc" style="font-size:12.5px;margin:6px 0"><b>⚠ ${bad} site${bad > 1 ? 's need' : ' needs'} a look.</b> Click one for why and what to do.</p>` : ''}
      ${list.length ? `<table><tbody>${list.map(row).join('')}</tbody></table>` : `<p class="muted">${SW.data ? 'The first check runs a minute after the app starts.' : 'Loading…'}</p>`}
      ${SW.extra.length ? `<h3 style="margin-top:10px">Checked just now</h3><table><tbody>${SW.extra.map(row).join('')}</tbody></table>` : ''}
      <p class="muted" style="font-size:11.5px">Pinged and traced from the app's server; its traffic goes out through the router, so this is the way customers' traffic goes.</p>
      <button class="btn sm" id="swEdit" style="margin-top:4px">Edit the list</button>`;
    el.querySelectorAll('[data-h]').forEach(tr => tr.onclick = () => { SW.sel = tr.dataset.h; swSide(true); nmDraw(); });
    $('#swEdit').onclick = swEdit;
    $('#swF').onsubmit = e => { e.preventDefault(); swSearch($('#swQ').value); };
    swTimeBind(); swTick();
    return;
  }
  const w = swWhere(s.avg), up = swUpstream(s.hops), iss = swIssues(s);
  el.innerHTML = `<h3>${iss.length ? '<span class="warnc">⚠</span> ' : ''}${esc(s.name)} <span class="muted" style="font-weight:500">${esc(s.host)}</span></h3>
    ${nmWarnHtml(iss)}
    <div class="row" style="gap:18px;margin-bottom:8px"><div><div class="muted">Now</div><div class="big ${cls(s)}">${swMs(s.avg)}</div></div><div><div class="muted">Normally</div><div class="big">${swMs(s.normal)}</div></div><div><div class="muted">Lost</div><div class="big">${s.loss == null ? '-' : s.loss + '%'}</div></div></div>
    <div class="faint" style="font-size:12px;margin-bottom:6px">Checked ${swClock(s.ts)} (${esc(ago(s.ts))})${s.traced ? ` · path traced ${esc(ago(s.traced))}` : ''}${s.extra ? '' : ` · next check <b id="swCd">-</b>`}</div>
    ${s.series && s.series.length ? '<div id="swG" style="height:110px"></div>' : ''}
    <table><tbody><tr><td>Address</td><td>${esc(s.ip || '-')}</td></tr><tr><td>Leaves by</td><td>${esc(up || 'not one of the BGP peers')}</td></tr>
      <tr><td>Served from</td><td>${esc(w ? SW_WHERE_TXT[w] : '-')} <span class="faint">(by delay)</span></td></tr>${s.error ? `<tr><td>Note</td><td>${esc(s.error)}</td></tr>` : ''}</tbody></table>
    <h3 style="margin-top:10px">Path</h3>${s.hops && s.hops.length ? `<table><tbody>${s.hops.map(h => `<tr><td>${h.hop}</td><td>${esc(h.ip || '*')}</td><td style="text-align:right">${h.ms != null ? h.ms + ' ms' : ''}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">No trace yet.</p>'}
    ${s.events && s.events.length ? `<h3 style="margin-top:10px">Changes</h3>${s.events.map(e => `<div class="witem ${e.kind === 'path' ? 'warn' : 'crit'}"><b>${esc(e.text)}</b> <span class="faint">${esc(ago(e.ts))}</span></div>`).join('')}` : ''}
    ${s.extra ? `<div class="swadd"><h3 style="margin-top:10px">Keep checking it</h3><div class="row" style="gap:6px"><input id="swN" value="${esc(s.name)}" style="flex:1;min-width:0" title="the name shown on the map">
      <button class="btn sm primary" id="swAdd">Add to the list</button></div><div class="err" id="swAddE"></div><button class="btn sm" id="swRm" style="margin-top:6px">Remove from the map</button></div>` : ''}
    <button class="btn sm" id="swAll" style="margin-top:8px">← All sites</button>`;
  $('#swAll').onclick = () => { SW.sel = null; swSide(true); nmDraw(); };
  if (s.extra) {
    $('#swRm').onclick = () => { SW.extra = SW.extra.filter(x => x.host !== s.host); SW.sel = null; swSide(true); nmDraw(); };
    $('#swAdd').onclick = async () => {
      const list = ((SW.data && SW.data.sites) || []).map(x => ({ name: x.name, host: x.host })).concat([{ name: $('#swN').value.trim() || s.host, host: s.host }]);
      try { await post('/api/sites', { sites: list }); SW.extra = SW.extra.filter(x => x.host !== s.host); toast(`${s.host} added: it is checked with the others from now on.`); await swLoad(); swSide(true); }
      catch (err) { $('#swAddE').textContent = err.message; }
    };
  }
  if (s.series && s.series.length) new TimeChart($('#swG'), { fmt: v => Math.round(v) + ' ms', small: true }).set([{ name: 'Delay', color: '--s1', values: s.series.map(v => [v[0], v[1]]) }]);
  swTick();
}
function swTimeBind() {
  $('#swInt').onchange = async e => {
    try { await post('/api/sites/settings', { interval: +e.target.value }); toast(`Major sites are checked every ${e.target.value / 60} min.`); await swLoad(); swSide(true); }
    catch (err) { toast(err.message, true); swSide(true); }
  };
  $('#swRun').onclick = async () => {
    try { const r = await post('/api/sites/run', {}); toast(r.started ? 'Checking every site now: the results come in within a minute.' : 'A check ran less than 30 seconds ago, or one is running.');
      if (r.started && SW.data && !SW.data.demo) { SW.data.running = true; SW.polled = 0; } swTick(); }
    catch (err) { toast(err.message, true); }
  };
}
// check any site now: it is drawn on the map with the others, and can be added to the list
async function swSearch(q) {
  q = (q || '').trim(); if (!q) return;
  const msg = $('#swMsg'), known = swAll().find(x => x.host === q.toLowerCase().replace(/^https?:\/\//, '').split('/')[0]);
  if (known && !known.extra) { SW.sel = known.host; swSide(true); nmDraw(); return; }
  if (msg) msg.textContent = `Checking ${q}: pinging and tracing the path, this takes up to 20 seconds…`;
  const btn = $('#swF button'); if (btn) btn.disabled = true;
  try {
    const r = await post('/api/sites/check', { host: q });
    const s = Object.assign({ normal: null, series: [], events: [] }, r, { extra: true });
    SW.extra = SW.extra.filter(x => x.host !== s.host).concat([s]).slice(-8);
    SW.sel = s.host; swSide(true); nmDraw();
  } catch (err) { if (msg) msg.textContent = err.message; if (btn) btn.disabled = false; }
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
