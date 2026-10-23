/* Tiny dependency-free time-series chart (canvas) with crosshair tooltip. One y-axis per chart. */
const Fmt = {
  bps(v) { if (v == null) return '-'; const u = ['bps', 'kbps', 'Mbps', 'Gbps', 'Tbps']; let i = 0; while (Math.abs(v) >= 1000 && i < u.length - 1) { v /= 1000; i++; } return (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)) + ' ' + u[i]; },
  pct(v) { return v == null ? '-' : Math.round(v) + '%'; },
  temp(v) { return v == null ? '-' : Math.round(v) + ' °C'; },
  num(v) { return v == null ? '-' : Number(v).toLocaleString(); },
  bytes(v) { if (v == null) return '-'; const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']; let i = 0; while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; } return v.toFixed(i ? 1 : 0) + ' ' + u[i]; },
  time(t) { const d = new Date(t * 1000); return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); },
};

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

class TimeChart {
  /* series: [{name, color: '--s1', values: [[t, v], ...]}]; opts: {fmt, max, min} */
  constructor(host, opts = {}) {
    this.host = host; this.opts = opts; this.series = [];
    host.classList.add('chart'); if (opts.small) host.classList.add('sm');
    host.innerHTML = '<canvas></canvas><div class="tip hidden"></div>';
    this.canvas = host.querySelector('canvas'); this.tip = host.querySelector('.tip');
    this.canvas.addEventListener('mousemove', e => this.hover(e));
    this.canvas.addEventListener('mouseleave', () => { this.hx = null; this.tip.classList.add('hidden'); this.draw(); });
    this.ro = new ResizeObserver(() => this.draw()); this.ro.observe(host);
  }
  set(series) { this.series = series; this.draw(); }
  geom() {
    const r = this.host.getBoundingClientRect(); const pad = { l: 64, r: 12, t: 10, b: 24 };
    return { w: r.width, h: r.height, pad, pw: r.width - pad.l - pad.r, ph: r.height - pad.t - pad.b };
  }
  domain() {
    let t0 = Infinity, t1 = -Infinity, vmax = this.opts.max ?? 0, vmin = this.opts.min ?? 0;
    for (const s of this.series) for (const [t, v] of s.values) { t0 = Math.min(t0, t); t1 = Math.max(t1, t); if (this.opts.max == null && v > vmax) vmax = v; }
    if (!isFinite(t0)) { t1 = Date.now() / 1000; t0 = t1 - 300; }
    if (t1 - t0 < 60) t0 = t1 - 60;
    if (this.opts.max == null) vmax = niceMax(vmax || 1);
    return { t0, t1, vmin, vmax };
  }
  draw() {
    const { w, h, pad, pw, ph } = this.geom(); if (w <= 0 || h <= 0) return;
    const dpr = window.devicePixelRatio || 1; const c = this.canvas; c.width = w * dpr; c.height = h * dpr;
    const g = c.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, w, h);
    const { t0, t1, vmin, vmax } = this.d = this.domain();
    const X = t => pad.l + (t - t0) / (t1 - t0) * pw, Y = v => pad.t + ph - (v - vmin) / (vmax - vmin) * ph;
    this.X = X; this.Y = Y;
    const fmt = this.opts.fmt || Fmt.num, font = '11px system-ui,sans-serif';
    g.font = font; g.fillStyle = cssVar('--muted'); g.strokeStyle = cssVar('--grid'); g.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const v = vmin + (vmax - vmin) * i / 4, y = Math.round(Y(v)) + .5;
      g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke();
      g.textAlign = 'right'; g.textBaseline = 'middle'; g.fillText(fmt(v), pad.l - 8, y);
    }
    g.textAlign = 'center'; g.textBaseline = 'top';
    const ticks = Math.max(2, Math.floor(pw / 110));
    for (let i = 0; i <= ticks; i++) { const t = t0 + (t1 - t0) * i / ticks; g.fillText(Fmt.time(t).slice(0, 5 + (t1 - t0 < 900 ? 3 : 0)), X(t), h - pad.b + 6); }
    for (const s of this.series) {
      if (!s.values.length) continue;
      const col = cssVar(s.color);
      if (s.fill !== false && this.series.length === 1) {
        g.beginPath(); g.moveTo(X(s.values[0][0]), Y(vmin));
        for (const [t, v] of s.values) g.lineTo(X(t), Y(v));
        g.lineTo(X(s.values[s.values.length - 1][0]), Y(vmin)); g.closePath();
        g.fillStyle = col + '22'; g.fill();
      }
      if (s.values.length === 1) { g.beginPath(); g.arc(X(s.values[0][0]), Y(s.values[0][1]), 4, 0, 7); g.fillStyle = col; g.fill(); continue; }
      g.beginPath(); g.strokeStyle = col; g.lineWidth = 2; g.lineJoin = 'round';
      s.values.forEach(([t, v], i) => i ? g.lineTo(X(t), Y(v)) : g.moveTo(X(t), Y(v))); g.stroke();
    }
    if (this.hx != null) {
      g.strokeStyle = cssVar('--faint'); g.lineWidth = 1; g.beginPath(); g.moveTo(this.hx + .5, pad.t); g.lineTo(this.hx + .5, pad.t + ph); g.stroke();
      for (const s of this.series) { const p = nearest(s.values, this.ht); if (!p) continue;
        g.beginPath(); g.arc(X(p[0]), Y(p[1]), 4, 0, 7); g.fillStyle = cssVar(s.color); g.fill(); g.lineWidth = 2; g.strokeStyle = cssVar('--panel'); g.stroke(); }
    }
    if (!this.series.some(s => s.values.length)) { g.fillStyle = cssVar('--faint'); g.textAlign = 'center'; g.textBaseline = 'middle'; g.font = '12px system-ui'; g.fillText('Collecting samples…', pad.l + pw / 2, pad.t + ph / 2); }
  }
  hover(e) {
    const r = this.canvas.getBoundingClientRect(); const { pad, pw } = this.geom(); const x = e.clientX - r.left;
    if (x < pad.l || x > pad.l + pw || !this.d) { this.tip.classList.add('hidden'); return; }
    const t = this.d.t0 + (x - pad.l) / pw * (this.d.t1 - this.d.t0); this.ht = t; this.hx = x;
    const fmt = this.opts.fmt || Fmt.num; let html = '', tt = null;
    for (const s of this.series) { const p = nearest(s.values, t); if (!p) continue; tt = p[0];
      html += `<div><i style="background:${cssVar(s.color)}"></i>${s.name}: <b>${fmt(p[1])}</b></div>`; }
    if (!html) return;
    this.tip.innerHTML = `<div class="faint">${Fmt.time(tt)}</div>` + html; this.tip.classList.remove('hidden');
    const tw = this.tip.offsetWidth; this.tip.style.left = (x + 14 + tw > r.width ? x - tw - 14 : x + 14) + 'px'; this.tip.style.top = '8px';
    this.draw();
  }
  destroy() { this.ro.disconnect(); }
}
function nearest(vals, t) { let best = null, bd = Infinity; for (const p of vals) { const d = Math.abs(p[0] - t); if (d < bd) { bd = d; best = p; } } return best; }
function niceMax(v) { const e = Math.pow(10, Math.floor(Math.log10(v))); for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * e >= v * 1.05) return m * e; return 10 * e; }
