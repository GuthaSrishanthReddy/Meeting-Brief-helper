'use strict';
/* Logs and performance: the command log, the model-call log, latency and token metrics, harness runs. */
(function () {
  const R = window.R3, h = R.h, fmt = R.fmt;
  const $ = function (id) { return document.getElementById(id); };
  const AUTO_KEY = 'r3-auto-refresh';
  const state = { timer: null, stream: 'commands', metrics: null, searchTimer: null };
  const AREA_NAMES = { briefs: 'Right briefs', answers: 'Right answers', honesty: 'Honesty', memory: 'Memory', privacy: 'Privacy',
    better: 'Getting better', cost: 'Cost' };
  const C = { p50: 'var(--ok)', p90: 'var(--warn)', max: 'var(--bad)' };

  function tile(label, value, sub, cls) {
    return h('div', { class: 'panel tile' + (cls ? ' ' + cls : '') }, h('div', { class: 'label' }, label), h('div', { class: 'value' }, value),
      h('div', { class: 'sub' }, sub || ' '));
  }
  function empty(text) { return h('div', { class: 'empty' }, text); }
  function setError(msg) { const e = $('error'); e.hidden = !msg; e.textContent = msg || ''; }
  function th(label, num) { return h('th', { class: num ? 'num' : null }, label); }
  function td(content, num) { return h('td', { class: num ? 'num' : null }, content); }
  function table(box, head, rows, emptyText) {
    R.clear(box);
    if (!rows.length) { box.appendChild(h('tbody', null, h('tr', null, h('td', { class: 'muted' }, emptyText || 'Nothing to show.')))); return; }
    box.appendChild(h('thead', null, h('tr', null, head)));
    box.appendChild(h('tbody', null, rows));
  }

  /* ------------------------------------------------------------ live metrics */
  function renderTiles(m) {
    const c = m.commands, md = m.model, box = R.clear($('tiles'));
    const by = Object.keys(c.by_cmd).map(function (k) { return k + ' ' + c.by_cmd[k].n; }).join(' · ');
    const bad = (c.status.fallback || 0) + (c.status.watchdog || 0);
    box.appendChild(tile('Commands', fmt.num(c.n), by));
    box.appendChild(tile('Median latency', fmt.ms(c.ms.p50), 'p90 ' + fmt.ms(c.ms.p90) + ' · max ' + fmt.ms(c.ms.max)));
    box.appendChild(tile('Fallbacks', fmt.num(bad), (c.status.watchdog || 0) + ' watchdog · ' + fmt.plural(c.status.fallback || 0, 'error'), bad ? 'bad' : (c.n ? 'ok' : '')));
    box.appendChild(tile('Cache hit rate', fmt.pct(c.cache.rate), c.cache.hits + ' of ' + c.cache.lookups + ' lookups'));
    box.appendChild(tile('Model calls', fmt.num(md.n), md.ok + ' ok · ' + md.fail + ' failed'));
    box.appendChild(tile('Model failures', fmt.num(md.fail), md.n ? fmt.pct(md.fail / md.n) + ' of calls' : 'no calls yet', md.fail ? 'warn' : ''));
    box.appendChild(tile('Model latency', fmt.ms(md.ms.p50), 'p90 ' + fmt.ms(md.ms.p90) + ' · max ' + fmt.ms(md.ms.max)));
    box.appendChild(tile('Tokens', fmt.num(md.tokens_in + md.tokens_out), fmt.num(md.tokens_in) + ' in · ' + fmt.num(md.tokens_out) + ' out · ' + fmt.cost(md.cost_usd)));
    $('empty').hidden = c.n > 0;
  }

  function renderTimeline(m) {
    const tl = m.commands.timeline, box = R.clear($('chart-timeline')), lg = R.clear($('legend-timeline'));
    if (!tl.length) { box.appendChild(empty('No commands yet.')); return; }
    const pts = tl.map(function (t) {
      return { y: t.ms || 0, color: R.colorFor(t.cmd), ring: t.status !== 'ok',
        title: t.cmd + ' · ' + fmt.ms(t.ms) + ' · ' + t.status + (t.cache ? ' · cache ' + t.cache : '') + ' · ' + fmt.time(t.ts) };
    });
    box.appendChild(R.scatterChart(pts, { label: 'Command latency over time',
      lines: [{ y: m.commands.ms.p50, label: 'median ' + fmt.ms(m.commands.ms.p50) }, { y: m.commands.ms.p90, label: 'p90 ' + fmt.ms(m.commands.ms.p90) }] }));
    const seen = {};
    tl.forEach(function (t) { seen[t.cmd] = true; });
    lg.appendChild(R.legend(Object.keys(seen).map(function (k) { return { name: k, color: R.colorFor(k) }; })));
  }

  function renderByCmd(m) {
    const box = R.clear($('chart-bycmd')), lg = R.clear($('legend-bycmd'));
    const keys = Object.keys(m.commands.by_cmd);
    if (!keys.length) { box.appendChild(empty('No commands yet.')); return; }
    box.appendChild(R.barChart(keys.map(function (k) {
      const s = m.commands.by_cmd[k].ms;
      return { label: k + ' (' + m.commands.by_cmd[k].n + ')', segs: [{ v: s.p50, color: C.p50, title: 'median' }, { v: s.p90, color: C.p90, title: 'p90' }, { v: s.max, color: C.max, title: 'slowest' }] };
    }), { fmt: fmt.ms, label: 'Latency by command' }));
    lg.appendChild(R.legend([{ name: 'median', color: C.p50 }, { name: 'p90', color: C.p90 }, { name: 'slowest', color: C.max }]));
  }

  function renderOutcomes(m) {
    const box = R.clear($('chart-outcomes')), lg = R.clear($('legend-outcomes')), chips = R.clear($('outcome-chips'));
    const keys = Object.keys(m.commands.by_cmd);
    if (!keys.length) { box.appendChild(empty('No commands yet.')); return; }
    box.appendChild(R.barChart(keys.map(function (k) {
      const d = m.commands.by_cmd[k];
      return { label: k, segs: [{ v: d.ok, color: 'var(--ok)', title: 'ok' }, { v: d.fallback, color: 'var(--warn)', title: 'internal-error fallback' },
        { v: d.watchdog, color: 'var(--bad)', title: 'watchdog' }] };
    }), { stacked: true, label: 'Outcomes by command' }));
    lg.appendChild(R.legend([{ name: 'ok', color: 'var(--ok)' }, { name: 'fallback after an error', color: 'var(--warn)' }, { name: 'watchdog fired', color: 'var(--bad)' }]));
    const k = m.commands.ask_kinds;
    Object.keys(k).forEach(function (name) { chips.appendChild(R.chip('ask · ' + name + ' ' + k[name], name === 'refused' ? 'warn' : 'info')); });
    const b = m.commands.briefs;
    if (b.meetings) chips.appendChild(R.chip('briefs · ' + fmt.plural(b.meetings, 'meeting') + ', ' + b.with_changes + ' with changes, ' + fmt.plural(b.changes, 'change line'), 'info'));
    if (b.summaries) chips.appendChild(R.chip('summaries · ' + b.summaries, 'info'));
    const via = m.commands.via;
    Object.keys(via).forEach(function (v) { chips.appendChild(R.chip('from ' + v + ' ' + via[v])); });
  }

  function renderModel(m) {
    const md = m.model, chart = R.clear($('chart-model')), lg = R.clear($('legend-model')), errs = R.clear($('model-errors'));
    $('model-sub').textContent = md.n ? fmt.plural(md.n, 'call') : '';
    $('model-empty').hidden = md.n > 0;
    const keys = Object.keys(md.by_purpose);
    if (!keys.length) { R.clear($('model-table')); return; }
    chart.appendChild(R.barChart(keys.map(function (k) {
      const s = md.by_purpose[k].ms;
      return { label: k + ' (' + md.by_purpose[k].n + ')', segs: [{ v: s.p50, color: C.p50, title: 'median' }, { v: s.p90, color: C.p90, title: 'p90' }, { v: s.max, color: C.max, title: 'slowest' }] };
    }), { fmt: fmt.ms, label: 'Model latency by purpose' }));
    lg.appendChild(R.legend([{ name: 'median', color: C.p50 }, { name: 'p90', color: C.p90 }, { name: 'slowest', color: C.max }]));
    table($('model-table'), [th('Purpose'), th('Calls', 1), th('Failed', 1), th('Tokens in', 1), th('Tokens out', 1), th('Cost', 1)],
      keys.map(function (k) {
        const d = md.by_purpose[k];
        return h('tr', { class: d.fail ? 'warn' : null }, td(k), td(d.n, 1), td(d.fail, 1), td(fmt.num(d.tokens_in), 1), td(fmt.num(d.tokens_out), 1), td(fmt.cost(d.cost_usd), 1));
      }));
    const models = Object.keys(md.by_model);
    if (models.length) errs.appendChild(h('div', { class: 'small muted' }, 'Models used: ' + models.map(function (k) { return k + ' (' + md.by_model[k].n + ')'; }).join(', ')));
    if (md.errors.length) {
      errs.appendChild(h('div', { class: 'sect' }, h('h4', null, 'Most common failures'),
        h('ul', { class: 'lines' }, md.errors.map(function (e) { return h('li', null, h('span', { class: 'txt mono' }, e.error), R.chip('× ' + e.n, 'bad')); }))));
    }
  }

  async function refreshMetrics() {
    try {
      const m = await R.api('/api/metrics');
      state.metrics = m;
      setError('');
      renderTiles(m); renderTimeline(m); renderByCmd(m); renderOutcomes(m); renderModel(m);
      fillKinds();
      $('updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
    } catch (e) { setError(e.message); }
  }

  /* ------------------------------------------------------------ the log tables */
  function fillKinds() {
    const m = state.metrics, kind = $('f-kind'), keep = kind.value;
    const names = state.stream === 'commands' ? Object.keys(m.commands.by_cmd) : Object.keys(m.model.by_purpose);
    R.clear(kind);
    kind.appendChild(h('option', { value: '' }, state.stream === 'commands' ? 'All commands' : 'All purposes'));
    names.forEach(function (n) { kind.appendChild(h('option', { value: n }, n)); });
    kind.value = names.indexOf(keep) >= 0 ? keep : '';
    const st = $('f-status'), keepS = st.value;
    R.clear(st);
    const opts = state.stream === 'commands' ? [['', 'Any outcome'], ['ok', 'ok'], ['fallback', 'fallback'], ['watchdog', 'watchdog']] : [['', 'Any result'], ['ok', 'ok'], ['fail', 'failed']];
    opts.forEach(function (o) { st.appendChild(h('option', { value: o[0] }, o[1])); });
    st.value = opts.some(function (o) { return o[0] === keepS; }) ? keepS : '';
  }

  function detail(r) {
    const bits = [];
    if (r.date) bits.push(r.date);
    if (r.at) bits.push('at ' + r.at.replace('T', ' ').replace('Z', ''));
    if (r.day !== undefined) bits.push('day ' + r.day);
    if (r.ask_len) bits.push('question of ' + r.ask_len + ' characters (' + r.ask_id + ')' + (r.ask ? ': ' + r.ask : ''));
    if (r.summary) bits.push('with summary');
    return bits.join(' · ');
  }
  function outcome(r) {
    const bits = [];
    if (r.kind) bits.push(r.kind + (r.sources ? ', ' + fmt.plural(r.sources, 'source') : ''));
    if (r.meetings !== undefined) bits.push(fmt.plural(r.meetings, 'meeting') + ', ' + fmt.plural(r.changes || 0, 'change') + (r.summaries !== undefined ? ', ' + fmt.plural(r.summaries, 'summary', 'summaries') : ''));
    if (r.records !== undefined) bits.push(fmt.plural(r.records, 'record') + (r.skipped ? ' (already ingested)' : '') + (r.pending ? ', ' + r.pending + ' pending model work' : ''));
    if (r.ok !== undefined) bits.push(r.ok ? 'done' : 'not done');
    return bits.join(' · ');
  }

  async function loadLogs() {
    const q = new URLSearchParams({ stream: state.stream, limit: $('f-limit').value });
    if ($('f-kind').value) q.set('cmd', $('f-kind').value);
    if ($('f-status').value) q.set('status', $('f-status').value);
    if ($('f-q').value.trim()) q.set('q', $('f-q').value.trim());
    const box = $('log-table');
    try {
      const data = await R.api('/api/logs?' + q.toString());
      if (state.stream === 'commands') {
        table(box, [th('Time'), th('Command'), th('From'), th('Person'), th('Detail'), th('Outcome'), th('Cache'), th('Time taken', 1), th('Tokens in/out', 1)],
          data.rows.map(function (r) {
            const bad = r.status !== 'ok';
            return h('tr', { class: bad ? 'bad' : null }, td(fmt.time(r.ts)), td(h('span', { style: 'color:' + R.colorFor(r.cmd) + ';font-weight:600' }, r.cmd)), td(r.via),
              td(r.person || ''), td(detail(r)), td([bad ? R.badge(r.status, 'bad') : null, bad ? ' ' : null, outcome(r)]),
              td(r.cache || ''), td(fmt.ms(r.ms), 1), td(r.tokens_in !== undefined ? fmt.num(r.tokens_in) + ' / ' + fmt.num(r.tokens_out) : '', 1));
          }), 'No commands match.');
      } else {
        table(box, [th('Time'), th('Purpose'), th('Model'), th('Result'), th('In', 1), th('Out', 1), th('Cost', 1), th('Time taken', 1), th('Error')],
          data.rows.map(function (r) {
            return h('tr', { class: r.ok ? null : 'warn' }, td(fmt.time(r.ts)), td(r.purpose), td((r.provider || '') + ' / ' + (r.model || '')),
              td(r.ok ? R.badge('ok', 'ok') : R.badge('failed', 'bad')), td(fmt.num(r.input_tokens), 1), td(fmt.num(r.output_tokens), 1),
              td(fmt.cost(r.cost_usd), 1), td(fmt.ms(r.ms), 1), td(r.error ? h('span', { class: 'mono' }, r.error) : ''));
          }), 'No model calls match.');
      }
      $('log-note').textContent = 'Showing ' + data.rows.length + ' of the last ' + data.scanned + ' lines, newest first.';
    } catch (e) { setError(e.message); }
  }

  /* ------------------------------------------------------------ harness runs */
  function areaRows(areas) {
    return Object.keys(areas || {}).map(function (k) {
      const a = areas[k];
      return h('tr', null, td(AREA_NAMES[k] || k), td(a.score === null ? 'n/a' : fmt.pct(a.score)),
        td(a.score === null ? '' : h('div', { class: 'bar-score' }, h('i', { style: 'width:' + Math.round(a.score * 100) + '%' }))),
        td(a.points === null ? '–' : a.points + ' / ' + a.weight, 1), td(a.checks, 1));
    });
  }

  function renderRun(d) {
    const box = R.clear($('run-body'));
    const calls = d.calls, sc = d.score;
    const tiles = h('div', { class: 'tiles' });
    if (sc) {
      tiles.appendChild(tile('Score', sc.total + ' / ' + sc.max_scored, sc.privacy_capped ? 'capped at 50: privacy leak' : 'usefulness ' + fmt.pct(sc.usefulness) + ', gate ' + sc.gate, sc.privacy_capped ? 'bad' : 'ok'));
    }
    const failed = Object.keys(calls.by_kind).reduce(function (a, k) { return a + calls.by_kind[k].failed; }, 0);
    tiles.appendChild(tile('Calls', fmt.num(calls.n), Object.keys(calls.by_kind).map(function (k) { return k + ' ' + calls.by_kind[k].n; }).join(' · ')));
    tiles.appendChild(tile('Failed calls', fmt.num(failed), 'non-zero exit or not JSON', failed ? 'bad' : 'ok'));
    tiles.appendChild(tile('Median call', fmt.ms(calls.ms.p50), 'p90 ' + fmt.ms(calls.ms.p90) + ' · max ' + fmt.ms(calls.ms.max)));
    const total = Object.keys(calls.per_day_ms).reduce(function (a, k) { return a + calls.per_day_ms[k]; }, 0);
    tiles.appendChild(tile('Total time', fmt.ms(total), 'all calls added up'));
    box.appendChild(tiles);
    if (sc && sc.privacy_leaks.length) {
      box.appendChild(h('div', { class: 'note bad', style: 'margin-bottom:12px' }, 'Privacy leaks: ' + sc.privacy_leaks.map(function (l) { return l.item + ' (' + l.why + ')'; }).join('; ')));
    }
    const left = h('div', { class: 'stack' }), right = h('div', { class: 'stack' });
    if (sc) {
      left.appendChild(h('div', null, h('h3', { style: 'margin-bottom:6px' }, 'Score by area'),
        h('table', null, h('thead', null, h('tr', null, th('Area'), th('Score'), th(''), th('Points', 1), th('Checks', 1))), h('tbody', null, areaRows(sc.areas)))));
      const days = Object.keys(sc.per_day || {});
      if (days.length) {
        left.appendChild(h('div', null, h('h3', { style: 'margin-bottom:6px' }, 'Score by day'),
          R.barChart(days.map(function (k) { return { label: 'day ' + k, segs: [{ v: sc.per_day[k], color: 'var(--accent)', title: 'day score' }] }; }),
            { max: 1, fmt: fmt.pct, label: 'Score by day' })));
      }
    }
    const kinds = Object.keys(calls.by_kind);
    right.appendChild(h('div', null, h('h3', { style: 'margin-bottom:6px' }, 'Call latency by kind'),
      R.barChart(kinds.map(function (k) {
        const s = calls.by_kind[k].ms;
        return { label: k + ' (' + calls.by_kind[k].n + ')', segs: [{ v: s.p50, color: C.p50, title: 'median' }, { v: s.p90, color: C.p90, title: 'p90' }, { v: s.max, color: C.max, title: 'slowest' }] };
      }), { fmt: fmt.ms, label: 'Call latency by kind' }),
      R.legend([{ name: 'median', color: C.p50 }, { name: 'p90', color: C.p90 }, { name: 'slowest', color: C.max }])));
    right.appendChild(h('div', null, h('h3', { style: 'margin-bottom:6px' }, 'Slowest calls'),
      h('div', { class: 'scroll' }, h('table', null, h('thead', null, h('tr', null, th('Day'), th('Kind'), th('Call'), th('Time', 1))),
        h('tbody', null, calls.slowest.map(function (s) { return h('tr', null, td(s.day), td(s.kind), td(h('span', { class: 'mono small' }, s.args)), td(fmt.ms(s.ms), 1)); }))))));
    box.appendChild(h('div', { class: 'cols-2' }, left, right));
    if (calls.failures.length) {
      box.appendChild(h('div', { class: 'sect' }, h('h4', null, 'Failed calls'),
        h('table', null, h('thead', null, h('tr', null, th('Day'), th('Kind'), th('Exit'), th('Messages'))),
          h('tbody', null, calls.failures.map(function (f) { return h('tr', { class: 'bad' }, td(f.day), td(f.kind), td(String(f.exit)), td(h('span', { class: 'mono small' }, f.stderr_tail || '')));  })))));
    }
    if (sc && sc.worst.length) {
      const weak = sc.worst.filter(function (w) { return (w.score !== null && w.score < 1) || (w.honesty !== null && w.honesty < 1); });
      box.appendChild(h('div', { class: 'sect' }, h('h4', null, 'Items that lost points'),
        weak.length
          ? h('div', { class: 'scroll' }, h('table', null, h('thead', null, h('tr', null, th('Day'), th('Item'), th('Person'), th('Score', 1), th('Honesty', 1), th('Why'))),
            h('tbody', null, weak.map(function (w) {
              return h('tr', { class: 'warn' }, td(w.day), td(h('span', { class: 'mono' }, w.id)), td(w.person), td(w.score === null ? '' : w.score.toFixed(2), 1),
                td(w.honesty === null ? '' : w.honesty.toFixed(1), 1), td(w.why || '–'));
            }))))
          : h('div', { class: 'note ok' }, 'Every scored item got full marks and full honesty.')));
    }
  }

  async function loadRun(name) {
    const box = R.clear($('run-body'));
    if (!name) { box.appendChild(empty('No harness runs found. Run tools/run_days.py with --out runs/<name> and score it, and it will show up here.')); return; }
    box.appendChild(empty('Loading…'));
    try { renderRun(await R.api('/api/run-detail?name=' + encodeURIComponent(name))); }
    catch (e) { R.clear(box).appendChild(h('div', { class: 'note bad' }, e.message)); }
  }

  async function loadRuns() {
    const sel = R.clear($('run-select'));
    try {
      const data = await R.api('/api/runs');
      $('runs-dir').textContent = data.dir;
      data.runs.forEach(function (r) {
        sel.appendChild(h('option', { value: r.name }, r.name + (r.total !== undefined && r.total !== null ? '  ·  ' + r.total + '/' + r.max_scored : '')));
      });
      const best = data.runs.filter(function (r) { return r.total !== undefined; })[0] || data.runs[0];
      if (best) sel.value = best.name;
      loadRun(best ? best.name : null);
    } catch (e) { R.clear($('run-body')).appendChild(h('div', { class: 'note bad' }, e.message)); }
  }

  /* ------------------------------------------------------------ start */
  function setAuto(on) {
    clearInterval(state.timer);
    state.timer = on ? setInterval(function () { refreshMetrics().then(loadLogs); }, 5000) : null;
    try { localStorage.setItem(AUTO_KEY, on ? '1' : '0'); } catch (e) { /* ignore */ }
  }

  async function init() {
    R.loadStatus($('status'));
    document.querySelectorAll('.tab[data-stream]').forEach(function (t) {
      t.addEventListener('click', function () {
        state.stream = t.dataset.stream;
        document.querySelectorAll('.tab[data-stream]').forEach(function (x) { x.setAttribute('aria-selected', String(x === t)); });
        $('f-q').value = '';
        if (state.metrics) fillKinds();
        loadLogs();
      });
    });
    ['f-kind', 'f-status', 'f-limit'].forEach(function (id) { $(id).addEventListener('change', loadLogs); });
    $('f-q').addEventListener('input', function () { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(loadLogs, 250); });
    $('refresh').addEventListener('click', function () { refreshMetrics().then(loadLogs); });
    $('auto').addEventListener('change', function () { setAuto($('auto').checked); });
    $('run-select').addEventListener('change', function () { loadRun($('run-select').value); });
    await refreshMetrics();
    await loadLogs();
    loadRuns();
    let on = false;
    try { on = localStorage.getItem(AUTO_KEY) === '1'; } catch (e) { /* ignore */ }
    $('auto').checked = on;
    setAuto(on);
  }
  init();
})();
