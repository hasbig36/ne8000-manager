/* NOC geographic map: Kenya with its counties, the router's town as the hub, a glowing line to every town the network
   reaches (coloured by load and state, with traffic flowing), and warning icons on the towns and lines.
   A device's town comes from a place set by hand (shared, stored by the app), else a town name in its name or
   description (MARSABIT-…, …-KISUMU), else the town of the device it hangs off. */
const KE_TOWNS = {
  NAIROBI: [-1.286, 36.817], MOMBASA: [-4.043, 39.668], KISUMU: [-0.092, 34.768], NAKURU: [-0.303, 36.080], ELDORET: [0.514, 35.270],
  THIKA: [-1.033, 37.069], MALINDI: [-3.217, 40.117], KITALE: [1.016, 35.006], GARISSA: [-0.453, 39.646], KAKAMEGA: [0.282, 34.752],
  NYERI: [-0.420, 36.947], MERU: [0.047, 37.649], MACHAKOS: [-1.517, 37.263], KISII: [-0.681, 34.766], KERICHO: [-0.368, 35.284],
  EMBU: [-0.531, 37.450], NAIVASHA: [-0.717, 36.433], NANYUKI: [0.017, 37.074], ISIOLO: [0.354, 37.582], MARSABIT: [2.334, 37.990],
  MOYALE: [3.527, 39.056], LAISAMIS: [1.600, 37.800], LODWAR: [3.119, 35.597], LAMU: [-2.271, 40.902], WAJIR: [1.747, 40.057],
  MANDERA: [3.937, 41.856], BUSIA: [0.460, 34.111], MALABA: [0.636, 34.282], BUNGOMA: [0.563, 34.561], VIHIGA: [0.076, 34.722],
  AHERO: [-0.174, 34.919], 'HOMA BAY': [-0.527, 34.457], MIGORI: [-1.063, 34.473], NAROK: [-1.078, 35.871], KAJIADO: [-1.852, 36.776],
  VOI: [-3.396, 38.556], KITUI: [-1.367, 38.011], KILIFI: [-3.630, 39.850], DIANI: [-4.316, 39.578], UKUNDA: [-4.287, 39.567],
  NYAHURURU: [0.038, 36.363], KAPENGURIA: [1.239, 35.112], SIAYA: [0.061, 34.288], BOMET: [-0.782, 35.342], ITEN: [0.671, 35.508],
  KABARNET: [0.492, 35.743], MARALAL: [1.097, 36.698], MWINGI: [-0.934, 38.060], KERUGOYA: [-0.498, 37.280], MURANGA: [-0.721, 37.153],
  KIAMBU: [-1.171, 36.835], 'ATHI RIVER': [-1.456, 36.978], NAMANGA: [-2.545, 36.788], TAVETA: [-3.398, 37.677], WEBUYE: [0.607, 34.770],
  MUMIAS: [0.336, 34.488], KAPSABET: [0.204, 35.105], LITEIN: [-0.583, 35.190], NYAMIRA: [-0.567, 34.935], RONGO: [-0.758, 34.598],
  EMALI: [-2.084, 37.470], MAKINDU: [-2.277, 37.823], 'MTITO ANDEI': [-2.690, 38.166], HOLA: [-1.500, 40.030], GARSEN: [-2.276, 40.116],
  WATAMU: [-3.354, 40.024], RUMURUTI: [0.272, 36.538], GILGIL: [-0.500, 36.317], MOLO: [-0.249, 35.732], LIMURU: [-1.114, 36.642],
  RUIRU: [-1.146, 36.961], NGONG: [-1.361, 36.656], KITENGELA: [-1.473, 36.959], 'OL KALOU': [-0.270, 36.379], CHUKA: [-0.333, 37.646],
  MAUA: [0.233, 37.940], MERTI: [1.067, 38.666], SOLOLO: [3.548, 38.651], 'ARCHERS POST': [0.643, 37.680], LOIYANGALANI: [2.763, 36.717],
  TURBO: [0.636, 35.050], LONDIANI: [-0.166, 35.594], SOTIK: [-0.681, 35.117], KEROKA: [-0.776, 34.946], OYUGIS: [-0.508, 34.734],
  MBITA: [-0.433, 34.200], LOKICHOGGIO: [4.204, 34.348], KAKUMA: [3.712, 34.861], 'ELDAMA RAVINE': [0.050, 35.723],
  KIBWEZI: [-2.417, 37.967], NYANDARUA: [-0.180, 36.520], MWEA: [-0.690, 37.370], KANGUNDO: [-1.300, 37.350], WOTE: [-1.783, 37.633],
  BUTERE: [0.207, 34.490], BONDO: [-0.098, 34.274], USENGE: [-0.072, 34.059], ISEBANIA: [-1.243, 34.476],
};
Object.keys(KE_TOWNS).forEach(k => { if (!KE_TOWNS[k]) delete KE_TOWNS[k]; });
const GEO = { places: {}, key: null, z: 1, cx: null, cy: null, sel: null, last: null };
const geoXY = t => { const p = KE_TOWNS[t] || KE_TOWNS.NAIROBI; return [(p[1] - KENYA_MAP.lon0) * KENYA_MAP.k, (KENYA_MAP.lat0 - p[0]) * KENYA_MAP.k]; };
const TOWN_RE = Object.keys(KE_TOWNS).sort((a, b) => b.length - a.length).map(t => [t, new RegExp('(^|[^A-Z])' + t.replace(/ /g, '[ _-]?') + '([^A-Z]|$)')]);
function geoAutoTown(n) {
  const s = [n.label, n.props.location, n.props.description, n.props.notes].filter(Boolean).join(' ').toUpperCase().replace(/'/g, '');
  for (const [t, re] of TOWN_RE) if (re.test(s)) return t;
  return null;
}
async function geoLoadPlaces() { try { GEO.places = (await api('/api/netmap/places')).places || {}; } catch (e) { GEO.places = {}; } }
// device id -> town
function geoTowns(T) {
  const d = NM.data.diagram, byId = Object.fromEntries(d.nodes.map(n => [n.id, n])), out = {}, pl = id => KE_TOWNS[GEO.places[id]] ? GEO.places[id] : null;
  const hub = pl(T.root) || (byId[T.root] && geoAutoTown(byId[T.root])) || 'NAIROBI';
  Object.keys(T.parent).forEach(id => { out[id] = id === T.root ? hub : pl(id) || (byId[id] && geoAutoTown(byId[id])) || out[T.parent[id]] || hub; });
  d.nodes.forEach(n => { if (!out[n.id]) out[n.id] = pl(n.id) || geoAutoTown(n) || hub; });
  return { town: out, hub };
}
const STATE_RANK = { 'nm-down': 4, 'nm-bgpdown': 4, 'nm-bad': 3, 'nm-warn': 2, 'nm-ok': 1, '': 0 };

function geoDraw(host) {
  host.classList.add('geo');
  if (GEO.key !== NM.data.key) { GEO.key = NM.data.key; GEO.places = {}; geoLoadPlaces().then(() => { if (NM.geo) nmDraw(); }); }
  const d = NM.data.diagram, T = nmTree(), WN = NM.warn = nmWarnings(), { town, hub } = geoTowns(T), show = NM.show;
  const B = KENYA_MAP.box, W = B[2], H = B[3];
  if (GEO.cx == null) { GEO.cx = W / 2; GEO.cy = H / 2; }
  const hw = host.clientWidth || 800, hh = host.clientHeight || 600, z = GEO.z;
  const px = Math.max(W / z / hw, H / z / hh);           // map units per screen pixel
  const vb = () => `${GEO.cx - hw * px / 2} ${GEO.cy - hh * px / 2} ${hw * px} ${hh * px}`;
  // sites (towns) and arcs (town to town)
  const nodes = d.nodes.filter(n => !show || show.has(n.id)), sites = {};
  nodes.forEach(n => { const t = town[n.id], s = sites[t] = sites[t] || { t, nodes: [], links: [], ws: [], cls: '' }; s.nodes.push(n); (WN[n.id] || []).forEach(w => s.ws.push(w)); });
  const arcs = {};
  d.links.filter(l => !show || show.has(l.from) && show.has(l.to)).forEach(l => {
    const [pa, ch] = T.parent[l.to] === l.from ? [l.from, l.to] : [l.to, l.from], a = town[pa], b = town[ch], st = nmLinkState(l), ws = WN[l.id] || [];
    if (a === b) { const s = sites[a]; if (s) { s.links.push(l); s.ws.push(...ws); if (STATE_RANK[st.cls] > STATE_RANK[s.cls]) s.cls = st.cls; } return; }
    const k = a + '|' + b, A = arcs[k] = arcs[k] || { k, a, b, links: [], ws: [], down: 0, up: 0, cls: '' };
    A.links.push(l); A.ws.push(...ws); A.down += st.down || 0; A.up += st.up || 0; if (STATE_RANK[st.cls] > STATE_RANK[A.cls]) A.cls = st.cls;
  });
  GEO.last = { sites, arcs, town, hub, T };
  // what is lit when a town or a line is picked
  let lit = null;
  if (GEO.sel && GEO.sel.site && sites[GEO.sel.site]) { lit = new Set([GEO.sel.site]); Object.values(arcs).forEach(A => { if (A.a === GEO.sel.site || A.b === GEO.sel.site) { lit.add(A.k); lit.add(A.a); lit.add(A.b); } }); }
  if (GEO.sel && GEO.sel.arc && arcs[GEO.sel.arc]) { const A = arcs[GEO.sel.arc]; lit = new Set([A.k, A.a, A.b]); }
  const dim = id => lit && !lit.has(id) ? ' dim' : '';
  const sw = v => v ? Math.min(7, 1.6 + Math.max(0, Math.log10(v / 1e5))) : 1.6;
  const bends = geoRoute(Object.values(arcs), Object.keys(sites));
  const arcSvg = Object.values(arcs).map(A => {
    const [x1, y1] = geoXY(A.a), [x2, y2] = geoXY(A.b), dx = x2 - x1, dy = y2 - y1;
    const bend = bends[A.k], cx = (x1 + x2) / 2 - dy * bend, cy = (y1 + y2) / 2 + dx * bend;
    const path = `M${x1} ${y1} Q${cx} ${cy} ${x2} ${y2}`, rev = `M${x2} ${y2} Q${cx} ${cy} ${x1} ${y1}`, mx = 0.25 * x1 + 0.5 * cx + 0.25 * x2, my = 0.25 * y1 + 0.5 * cy + 0.25 * y2;
    const w = sw(A.down + A.up), spd = v => Math.max(0.5, 3.2 - Math.log10(Math.max(1, v / 1e6))).toFixed(2);
    const label = z >= 1.8 || GEO.sel && GEO.sel.arc === A.k ? `${A.links.length > 1 ? A.links.length + ' lines · ' : ''}${A.down || A.up ? `▼ ${Fmt.bps(A.down)} ▲ ${Fmt.bps(A.up)}` : ''}` : '';
    return `<g class="garc ${A.cls}${dim(A.k)}${GEO.sel && GEO.sel.arc === A.k ? ' sel' : ''}" data-arc="${esc(A.k)}">
      <path class="glow" d="${path}" style="stroke-width:${w * 2.6}"/><path class="vis" d="${path}" style="stroke-width:${w}"/>
      ${A.down ? `<path class="flow" d="${path}" style="animation-duration:${spd(A.down)}s"/>` : ''}${A.up > A.down * 0.05 && A.up ? `<path class="flow rev" d="${rev}" style="animation-duration:${spd(A.up)}s"/>` : ''}
      <path class="hit" d="${path}" style="stroke-width:${16 * px}"/>
      ${label ? `<text x="${mx}" y="${my - 8 * px}" style="font-size:${11.5 * px}px">${esc(label)}</text>` : ''}
      ${nmBadge(A.ws, mx, my, px * 1.3)}</g>`;
  }).join('');
  const zoomed = z >= 2.6;
  const siteSvg = Object.values(sites).sort((a, b) => (a.t === hub) - (b.t === hub)).map(S => {
    const [x, y] = geoXY(S.t), isHub = S.t === hub, r = (isHub ? 9 : 5.5) * px, cnt = S.nodes.length;
    const devs = zoomed && cnt > 1 ? S.nodes.slice(0, 24).map((n, i, a) => { const ang = -Math.PI / 2 + i * 2 * Math.PI / a.length, R = (isHub ? 44 : 34) * px, dx = x + Math.cos(ang) * R, dy = y + Math.sin(ang) * R, ws = WN[n.id];
      return `<g class="gdev ${nmWorst(ws)}" data-dev="${esc(n.id)}"><line x1="${x}" y1="${y}" x2="${dx}" y2="${dy}"/><circle cx="${dx}" cy="${dy}" r="${3.5 * px}"/>
        <text x="${dx + 6 * px}" y="${dy + 3.5 * px}" style="font-size:${10 * px}px">${esc(trunc(n.label, 26))}</text></g>`; }).join('') : '';
    return `<g class="gsite${isHub ? ' hub' : ''} ${S.cls}${dim(S.t)}${GEO.sel && GEO.sel.site === S.t ? ' sel' : ''}" data-site="${esc(S.t)}">${devs}
      <circle class="halo" cx="${x}" cy="${y}" r="${r * 2.6}"/><circle class="pin" cx="${x}" cy="${y}" r="${r}"/>
      <text x="${x + r + 5 * px}" y="${y + 4.5 * px}" style="font-size:${(isHub ? 15 : 12.5) * px}px">${esc(S.t)}${cnt > 1 ? ` <tspan class="n">${cnt}</tspan>` : ''}</text>
      ${nmBadge(S.ws, x - r - 11 * px, y - r - 8 * px, px * 1.3)}</g>`;
  }).join('');
  // a submarine cable out to sea from a coastal town where an upstream lands
  const sea = Object.values(sites).filter(S => ['MOMBASA', 'LAMU', 'MALINDI', 'DIANI', 'KILIFI'].includes(S.t) && S.nodes.some(n => n.type === 'upstream')).map(S => {
    const [x, y] = geoXY(S.t); return `<path class="gsea" d="M${x} ${y} C${x + 120} ${y + 40} ${x + 260} ${y - 30} ${x + 420} ${y + 60}"/><text class="gsea-l" x="${x + 150}" y="${y + 52}" style="font-size:${10.5 * px}px">submarine cable</text>`; }).join('');
  host.innerHTML = `<svg id="geoSvg" viewBox="${vb()}" width="${hw}" height="${hh}" preserveAspectRatio="xMidYMid meet">
    <defs><radialGradient id="gBg" cx="50%" cy="42%" r="70%"><stop offset="0" stop-color="#2b1650"/><stop offset=".55" stop-color="#160c2c"/><stop offset="1" stop-color="#07040f"/></radialGradient>
</defs>
    <rect x="-3000" y="-3000" width="${W + 6000}" height="${H + 6000}" fill="url(#gBg)" data-bg="1"/>
    <g class="gnear">${KENYA_MAP.near.map(c => `<path d="${c.d}"><title>${esc(c.n)}</title></path>`).join('')}</g>
    <path class="gland-glow" d="${KENYA_MAP.kenya}"/><path class="gland" d="${KENYA_MAP.kenya}"/>
    <g class="gcounty">${KENYA_MAP.counties.map(c => `<path d="${c.d}"><title>${esc(c.n)} County</title></path>`).join('')}</g>
    <g class="glake">${KENYA_MAP.lakes.map(p => `<path d="${p}"/>`).join('')}</g>
    ${sea}${arcSvg}${siteSvg}
    <text class="gcredit" x="${GEO.cx + hw * px / 2 - 8 * px}" y="${GEO.cy + hh * px / 2 - 8 * px}" style="font-size:${9.5 * px}px">Map: Natural Earth · geoBoundaries (CC BY 4.0)</text></svg>`;
  const svg = $('#geoSvg');
  // drag to pan, wheel or buttons to zoom, click a town or a line
  let pan = null;
  svg.onpointerdown = e => { pan = { x: e.clientX, y: e.clientY, cx: GEO.cx, cy: GEO.cy, moved: false }; svg.setPointerCapture(e.pointerId); };
  svg.onpointermove = e => { if (!pan) return; const dx = e.clientX - pan.x, dy = e.clientY - pan.y; if (Math.abs(dx) + Math.abs(dy) > 4) pan.moved = true;
    if (pan.moved) { GEO.cx = pan.cx - dx * px; GEO.cy = pan.cy - dy * px; svg.setAttribute('viewBox', vb()); } };
  svg.onpointerup = e => { const p = pan; pan = null; if (!p || p.moved) return;
    const el = document.elementFromPoint(e.clientX, e.clientY) || e.target, dv = el.closest('[data-dev]'), st = el.closest('[data-site]'), ar = el.closest('[data-arc]');
    const twice = id => { const now = Date.now(), r = NM.lastClick && NM.lastClick.id === id && now - NM.lastClick.t < 400; NM.lastClick = { id, t: now }; return r; };
    if (dv) { const n = d.nodes.find(x => x.id === dv.dataset.dev); if (twice(n.id)) return NM.noc && NM.noc.dbl(n, 'node'); GEO.sel = { site: town[n.id] }; NM.sel = n.id; if (NM.noc) NM.noc.pick(n, 'node'); nmDraw(); return; }
    if (st) { const t = st.dataset.site; if (twice('s:' + t)) { geoZoomTo(t); return; } GEO.sel = { site: t }; geoSide(); nmDraw(); return; }
    if (ar) { const k = ar.dataset.arc, A = arcs[k]; if (twice('a:' + k)) return NM.noc && NM.noc.dbl(A.links[0], 'link'); GEO.sel = { arc: k }; geoSide(); nmDraw(); return; }
    GEO.sel = null; if (NM.noc) NM.noc.pick(null); nmDraw();
  };
  svg.onwheel = e => { e.preventDefault(); geoZoom(e.deltaY < 0 ? 1.2 : 1 / 1.2, e); };
}
/* Give every line its own curve so that no two lines cross or run on top of each other and no line runs through
   another town: longest lines first, each takes the bend (left or right, more or less) that touches the lines
   already placed the least. */
function geoRoute(list, towns) {
  const N = 28, placed = [], out = {}, pins = towns.map(t => [t, geoXY(t)]);
  const curve = (A, b) => { const [x1, y1] = geoXY(A.a), [x2, y2] = geoXY(A.b), cx = (x1 + x2) / 2 - (y2 - y1) * b, cy = (y1 + y2) / 2 + (x2 - x1) * b, pts = [];
    for (let i = 0; i <= N; i++) { const t = i / N, u = 1 - t; pts.push([u * u * x1 + 2 * u * t * cx + t * t * x2, u * u * y1 + 2 * u * t * cy + t * t * y2]); } return pts; };
  const cross = (p, q, r, s2) => { const d = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    return d(p, q, r) * d(p, q, s2) < 0 && d(r, s2, p) * d(r, s2, q) < 0; };
  const near = (pt, ends) => ends.some(e => Math.hypot(pt[0] - e[0], pt[1] - e[1]) < 22);
  const len = A => { const [x1, y1] = geoXY(A.a), [x2, y2] = geoXY(A.b); return Math.hypot(x2 - x1, y2 - y1); };
  const cands = [0.14, -0.14, 0.2, -0.2, 0.28, -0.28, 0.36, -0.36, 0.45, -0.45, 0.55, -0.55, 0.05, -0.05, 0.7, -0.7];
  const score = (A, pts, others) => { let cost = 0;
    for (const P of others) {
      const shared = [A.a, A.b].filter(t => t === P.A.a || t === P.A.b).map(geoXY);
      for (let i = 0; i < N; i++) for (let j = 0; j < N; j++) {
        const a1 = pts[i], a2 = pts[i + 1], b1 = P.pts[j], b2 = P.pts[j + 1];
        if (shared.length && (near(a1, shared) || near(b1, shared))) continue;
        if (cross(a1, a2, b1, b2)) cost += 1000;
        else if (Math.hypot(a1[0] - b1[0], a1[1] - b1[1]) < 16) cost += 60;      // running on top of each other
      }
    }
    for (const [t, q] of pins) if (t !== A.a && t !== A.b && pts.some(pt => Math.hypot(pt[0] - q[0], pt[1] - q[1]) < 18)) cost += 600;   // through another town
    return cost; };
  const pick = (A, others) => { let best = null;
    for (const b of cands) { const pts = curve(A, b), cost = Math.abs(b) * 4 + score(A, pts, others); if (!best || cost < best.cost) best = { b, cost, pts }; }
    return best; };
  // longest lines first, then two passes where each line is placed again against all the others
  const order = list.slice().sort((a, b) => len(b) - len(a)), cur = new Map();
  order.forEach(A => { const r = pick(A, [...cur.values()]); cur.set(A.k, { A, pts: r.pts, b: r.b }); });
  for (let pass = 0; pass < 2 && order.length < 60; pass++)
    order.forEach(A => { const others = [...cur.values()].filter(P => P.A.k !== A.k), r = pick(A, others); cur.set(A.k, { A, pts: r.pts, b: r.b }); });
  cur.forEach((v, k) => { out[k] = v.b; });
  void placed;
  return out;
}
function geoZoom(f, e) {
  const host = $('#nmC'), svg = $('#geoSvg'); if (!host) return;
  const z0 = GEO.z, z1 = Math.max(0.8, Math.min(12, z0 * f)); if (z1 === z0) return;
  if (e && svg) {      // keep the point under the cursor where it is
    const r = svg.getBoundingClientRect(), B = KENYA_MAP.box, px0 = Math.max(B[2] / z0 / r.width, B[3] / z0 / r.height), px1 = px0 * z0 / z1;
    const ox = e.clientX - r.left - r.width / 2, oy = e.clientY - r.top - r.height / 2;
    GEO.cx += ox * (px0 - px1); GEO.cy += oy * (px0 - px1);
  }
  GEO.z = z1; nmDraw();
}
function geoZoomTo(t) { const [x, y] = geoXY(t); GEO.cx = x; GEO.cy = y; GEO.z = Math.max(GEO.z, 3.2); GEO.sel = { site: t }; geoSide(); nmDraw(); }
function geoFit() { GEO.z = 1; GEO.cx = null; GEO.cy = null; nmDraw(); }
// the side panel for a town or a line between two towns
function geoSide() {
  const el = $('#ncS'), L = GEO.last; if (!el || !L || !GEO.sel) return;
  const row = (l, n) => { const s = nmLinkState(l), ws = (NM.warn || {})[l.id] || [];
    return `<tr data-l="${esc(l.id)}" style="cursor:pointer"><td>${ws.length ? `<span class="wdot ${nmWorst(ws)}">⚠</span> ` : ''}${esc(n ? n.label : l.props.label || l.props.iface || '')}</td><td style="text-align:right;white-space:nowrap">${s.down || s.up ? `▼ ${Fmt.bps(s.down)}<br>▲ ${Fmt.bps(s.up)}` : esc(s.text || '')}</td></tr>`; };
  const uniq = ws => { const seen = new Set(); return ws.filter(w => { const k = w.kind + w.title + w.detail; if (seen.has(k)) return false; seen.add(k); return true; }); };
  const node = id => NM.data.diagram.nodes.find(n => n.id === id);
  const child = l => node(L.T.parent[l.to] === l.from ? l.to : l.from);
  if (GEO.sel.site) {
    const S = L.sites[GEO.sel.site]; if (!S) return;
    const into = Object.values(L.arcs).filter(A => A.a === S.t || A.b === S.t);
    el.innerHTML = `<h3>${esc(S.t)}${S.t === L.hub ? ' <span class="muted" style="font-weight:500">· hub</span>' : ''}</h3>${nmWarnHtml(uniq(S.ws.concat(into.filter(A => A.b === S.t).flatMap(A => A.ws))))}
      <div class="muted" style="margin:6px 0">${S.nodes.length} device${S.nodes.length === 1 ? '' : 's'} here${into.length ? ` · lines to ${esc(into.map(A => A.a === S.t ? A.b : A.a).join(', '))}` : ''}</div>
      <table><tbody>${S.nodes.map(n => { const l = NM.data.diagram.links.find(x => x.id === L.T.via[n.id]); return l ? row(l, n) : `<tr data-n="${esc(n.id)}" style="cursor:pointer"><td>${esc(n.label)}</td><td></td></tr>`; }).join('')}</tbody></table>
      <p class="muted" style="font-size:12px;margin-top:8px">Click a row for its details and to move it to another town. Double-click a town to zoom in on its devices.</p>
      <button class="btn sm" id="geoZ">Zoom in on ${esc(S.t)}</button>`;
    $('#geoZ').onclick = () => geoZoomTo(S.t);
  } else {
    const A = L.arcs[GEO.sel.arc]; if (!A) return;
    el.innerHTML = `<h3>${esc(A.a)} → ${esc(A.b)}</h3><div class="row" style="gap:18px;margin-bottom:8px"><div><div class="muted">Download ▼</div><div class="big">${Fmt.bps(A.down)}</div></div><div><div class="muted">Upload ▲</div><div class="big">${Fmt.bps(A.up)}</div></div></div>
      ${nmWarnHtml(uniq(A.ws))}<table><tbody>${A.links.map(l => row(l, child(l))).join('')}</tbody></table>
      <p class="muted" style="font-size:12px;margin-top:8px">Click a line for its graph. Double-click the line on the map for how it is built.</p>`;
  }
  el.querySelectorAll('[data-l],[data-n]').forEach(tr => tr.onclick = () => {
    if (!NM.noc) return;
    if (tr.dataset.l) { const l = NM.data.diagram.links.find(x => x.id === tr.dataset.l); NM.sel = l.id; NM.noc.pick(l, 'link'); }
    else { const n = node(tr.dataset.n); NM.sel = n.id; NM.noc.pick(n, 'node'); }
  });
}
// "Place on the Kenya map": a town for this device (and what hangs off it, unless placed itself)
function geoPlaceHtml(n) {
  const L = GEO.last, cur = GEO.places[n.id] || '', auto = L ? L.town[n.id] : '';
  return `<div class="gplace"><label class="muted" for="geoPl">Town on the Kenya map</label><select id="geoPl" data-n="${esc(n.id)}"><option value="">Automatic${auto && !cur ? ' (' + esc(auto) + ')' : ''}</option>
    ${Object.keys(KE_TOWNS).sort().map(t => `<option ${t === cur ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></div>`;
}
function geoPlaceBind() {
  const s = $('#geoPl'); if (!s) return;
  s.onchange = async () => {
    try { const r = await post('/api/netmap/places', { places: { [s.dataset.n]: s.value } }); GEO.places = r.places || {}; toast(s.value ? 'Placed at ' + s.value : 'Back to automatic'); nmDraw(); }
    catch (e) { toast(e.message, true); }
  };
}
