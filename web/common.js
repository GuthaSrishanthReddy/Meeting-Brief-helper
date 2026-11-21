'use strict';
/* Shared helpers for the console and metrics pages. The DOM is built with textContent only: record text
   is untrusted and must never be parsed as HTML. */
(function () {
  const meta = document.querySelector('meta[name="r3-token"]');
  const TOKEN = meta ? meta.content : '';

  async function api(path, body) {
    const init = { headers: { 'X-R3-Token': TOKEN }, cache: 'no-store' };
    if (body !== undefined) {
      init.method = 'POST';
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try { res = await fetch(path, init); }
    catch (e) { throw new Error('Cannot reach the server. Is webapp.py still running?'); }
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) throw new Error((data && data.error) || ('Request failed (' + res.status + ')'));
    return data;
  }

  function add(el, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) kid.forEach(function (k) { add(el, k); });
    else if (kid.nodeType) el.appendChild(kid);
    else el.appendChild(document.createTextNode(String(kid)));
  }
  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      const v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.slice(0, 2) === 'on' && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    });
    for (let i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  const SVGNS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs) {
    const el = document.createElementNS(SVGNS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === 'text') el.textContent = attrs[k];
      else if (attrs[k] !== null && attrs[k] !== undefined) el.setAttribute(k, attrs[k]);
    });
    for (let i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

  const fmt = {
    ms: function (n) {
      if (n === null || n === undefined) return '–';
      if (n < 1000) return Math.round(n) + ' ms';
      if (n < 60000) return (n / 1000).toFixed(n < 10000 ? 2 : 1) + ' s';
      return Math.floor(n / 60000) + ' min ' + Math.round((n % 60000) / 1000) + ' s';
    },
    num: function (n) { return n === null || n === undefined ? '–' : Number(n).toLocaleString('en-US'); },
    cost: function (n) {
      if (n === null || n === undefined) return '–';
      if (n === 0) return '$0';
      return '$' + (n < 0.01 ? n.toFixed(5) : n.toFixed(4));
    },
    pct: function (x) { return x === null || x === undefined ? '–' : Math.round(x * 100) + '%'; },
    plural: function (n, word, many) { return fmt.num(n) + ' ' + (n === 1 ? word : (many || word + 's')); },
    time: function (iso) {
      if (!iso) return '–';
      const d = new Date(iso);
      return isNaN(d) ? String(iso) : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    },
    day: function (iso) {
      const d = new Date(iso + 'T12:00:00Z');
      return isNaN(d) ? iso : d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
    },
  };

  function chip(text, cls) { return h('span', { class: 'chip' + (cls ? ' ' + cls : '') }, text); }
  function badge(text, cls) { return h('span', { class: 'badge ' + (cls || 'mute') }, text); }

  const CMD_COLOR = { brief: 'var(--s-brief)', ask: 'var(--s-ask)', ingest: 'var(--s-ingest)', forget: 'var(--s-forget)' };
  function colorFor(cmd) { return CMD_COLOR[cmd] || 'var(--s-other)'; }

  function legend(items) {
    return h('div', { class: 'legend' }, items.map(function (it) {
      return h('span', null, h('i', { style: 'background:' + it.color }), it.name);
    }));
  }

  /* Horizontal bars. rows: [{label, segs: [{v, color, title}]}]. Grouped by default, stacked with opts.stacked. */
  function barChart(rows, opts) {
    opts = opts || {};
    const W = opts.width || 560, labelW = opts.labelW || 120, valW = 64, padR = 6, barH = 13, gap = 3, rowGap = 12;
    const fmtv = opts.fmt || fmt.num;
    const nSegs = Math.max.apply(null, [1].concat(rows.map(function (r) { return r.segs.length; })));
    const rowH = opts.stacked ? barH : nSegs * (barH + gap) - gap;
    const total = function (r) { return r.segs.reduce(function (a, s) { return a + (s.v || 0); }, 0); };
    const max = opts.max || Math.max.apply(null, [1].concat(rows.map(function (r) {
      return opts.stacked ? total(r) : Math.max.apply(null, [0].concat(r.segs.map(function (s) { return s.v || 0; })));
    })));
    const plotW = W - labelW - valW - padR;
    const H = rows.length * (rowH + rowGap) + 4;
    const root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, class: 'chart', role: 'img', 'aria-label': opts.label || 'bar chart' });
    rows.forEach(function (r, i) {
      const y = 2 + i * (rowH + rowGap);
      root.appendChild(svg('text', { x: labelW - 8, y: y + Math.min(rowH, 20) / 2 + 4, 'text-anchor': 'end', class: 'lbl', text: r.label }));
      let x = labelW;
      r.segs.forEach(function (s, j) {
        const w = Math.max(0, (s.v || 0) / max * plotW);
        const sy = opts.stacked ? y : y + j * (barH + gap);
        if (w > 0 || !opts.stacked) {
          root.appendChild(svg('rect', { x: opts.stacked ? x : labelW, y: sy, width: Math.max(w, 1), height: barH, rx: 3,
            style: 'fill:' + (s.color || 'var(--accent)') }, svg('title', { text: (s.title || r.label) + ': ' + fmtv(s.v || 0) })));
        }
        if (!opts.stacked) root.appendChild(svg('text', { x: labelW + w + 5, y: sy + barH - 3, class: 'val', text: fmtv(s.v || 0) }));
        if (opts.stacked) x += w;
      });
      if (opts.stacked) root.appendChild(svg('text', { x: x + 5, y: y + barH - 3, class: 'val', text: fmtv(total(r)) }));
    });
    return root;
  }

  /* "10 ms", "1 s", "100 s": compact labels for the decade ticks of a log axis. */
  function tickMs(v) { return v < 1000 ? v + ' ms' : (v / 1000) + ' s'; }

  /* Dots over sequence on a log axis (one slow outlier must not flatten everything else).
     points: [{y, color, title, ring}], lines: [{y, label}]. */
  function scatterChart(points, opts) {
    opts = opts || {};
    const W = 720, H = opts.height || 230, ml = 56, mr = 12, mt = 10, mb = 24;
    const root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, class: 'chart', role: 'img', 'aria-label': opts.label || 'scatter chart' });
    const lines = opts.lines || [];
    const vals = points.map(function (p) { return Math.max(1, p.y); }).concat(lines.map(function (l) { return Math.max(1, l.y); }));
    const loExp = Math.floor(Math.log10(Math.min.apply(null, vals)));
    let hiExp = Math.ceil(Math.log10(Math.max.apply(null, vals)));
    if (hiExp <= loExp) hiExp = loExp + 1;
    const X = function (i) { return ml + (points.length < 2 ? (W - ml - mr) / 2 : i / (points.length - 1) * (W - ml - mr)); };
    const Y = function (v) { return mt + (1 - (Math.log10(Math.max(1, v)) - loExp) / (hiExp - loExp)) * (H - mt - mb); };
    for (let e = loExp; e <= hiExp; e++) {
      const y = Y(Math.pow(10, e));
      root.appendChild(svg('line', { x1: ml, x2: W - mr, y1: y, y2: y, class: 'grid' }));
      root.appendChild(svg('text', { x: ml - 6, y: y + 4, 'text-anchor': 'end', text: tickMs(Math.pow(10, e)) }));
    }
    root.appendChild(svg('line', { x1: ml, x2: W - mr, y1: H - mb, y2: H - mb, class: 'axis' }));
    root.appendChild(svg('text', { x: ml, y: H - 6, text: 'oldest' }));
    root.appendChild(svg('text', { x: W - mr, y: H - 6, 'text-anchor': 'end', text: 'newest (log scale)' }));
    let lastLabelY = null;
    lines.slice().sort(function (a, b) { return Y(a.y) - Y(b.y); }).forEach(function (l) {
      const ly = Y(l.y);
      root.appendChild(svg('line', { x1: ml, x2: W - mr, y1: ly, y2: ly, class: 'ref' }));
      let ty = ly - 3;
      if (lastLabelY !== null && ty - lastLabelY < 13) ty = lastLabelY + 13;
      lastLabelY = ty;
      root.appendChild(svg('text', { x: W - mr - 2, y: ty, 'text-anchor': 'end', class: 'refl', text: l.label }));
    });
    points.forEach(function (p, i) {
      root.appendChild(svg('circle', { cx: X(i), cy: Y(p.y), r: p.ring ? 4.5 : 3.2,
        style: 'fill:' + p.color + ';opacity:.9' + (p.ring ? ';stroke:var(--bad);stroke-width:2' : '') },
        svg('title', { text: p.title })));
    });
    return root;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    const ta = h('textarea', { style: 'position:fixed;opacity:0' });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
    return Promise.resolve();
  }

  /* The status chips in the top bar: pack, ingested day, model. */
  async function loadStatus(box) {
    clear(box);
    try {
      const info = await api('/api/info');
      const llm = info.llm || {};
      box.appendChild(chip('day ' + info.last_day + ' ingested' + (info.days_in_pack.length ? ' of ' + info.days_in_pack.length : ''),
        info.last_day ? 'info' : 'warn'));
      box.appendChild(chip(llm.enabled ? (llm.provider + ' · ' + llm.model) : (llm.off_by_switch ? 'model off (R3_NO_LLM)' : 'model off (no key)'),
        llm.enabled ? 'ok' : 'warn'));
      const dot = box.firstChild;
      dot.title = info.pack;
      return info;
    } catch (e) {
      box.appendChild(chip(e.message, 'bad'));
      return null;
    }
  }

  window.R3 = { api: api, h: h, svg: svg, clear: clear, fmt: fmt, chip: chip, badge: badge, colorFor: colorFor, legend: legend,
    barChart: barChart, scatterChart: scatterChart, copyText: copyText, loadStatus: loadStatus, CMD_COLOR: CMD_COLOR };
})();
