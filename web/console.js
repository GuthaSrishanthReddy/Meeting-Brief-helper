'use strict';
/* The CLI in a page: pick a command, fill the form, run the real assist.py, read the result. */
(function () {
  const R = window.R3, h = R.h, fmt = R.fmt;
  const $ = function (id) { return document.getElementById(id); };
  const EXAMPLES = [
    'Who owns billing for NB?',
    'When is the tms data pipeline for the London account due?',
    'Is the Friday thing done?',
    'What did Meera say about me?',
    'Which Arjun is that?',
    'When is my Dubai trip?',
  ];
  const HISTORY_KEY = 'r3-history-v1', PERSON_KEY = 'r3-person', MAX_HISTORY = 25;
  const state = { info: null, people: {}, cmd: 'brief', running: false, raw: '', titles: {} };

  function stored(key, value) {
    try {
      if (value === undefined) return JSON.parse(localStorage.getItem(key));
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* storage may be unavailable: the page still works */ }
    return null;
  }
  function addDays(iso, n) {
    const d = new Date(iso + 'T00:00:00Z');
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }
  function quote(a) { return /[\s"']/.test(a) ? '"' + a.replace(/"/g, '\\"') + '"' : a; }

  /* ------------------------------------------------------------ the form */
  function fillPeople() {
    const sel = R.clear($('person'));
    const byOrg = {};
    state.info.people.forEach(function (p) { state.people[p.id] = p; (byOrg[p.org] = byOrg[p.org] || []).push(p); });
    Object.keys(byOrg).forEach(function (org) {
      const group = h('optgroup', { label: state.info.orgs[org] || org });
      byOrg[org].forEach(function (p) {
        group.appendChild(h('option', { value: p.id }, p.id + ' · ' + p.name + ' (' + p.role + ')'));
      });
      sel.appendChild(group);
    });
    const saved = stored(PERSON_KEY);
    if (saved && state.people[saved]) sel.value = saved;
  }

  function defaults() {
    const info = state.info;
    if (info.day1 && info.last_day > 0) {
      $('brief-date').value = addDays(info.day1, info.last_day);        // the day after the last ingested day
      $('ask-at').value = addDays(info.day1, info.last_day - 1) + 'T18:00';
    }
    const max = info.days_in_pack.length ? Math.max.apply(null, info.days_in_pack) : info.last_day + 1;
    $('ingest-day').value = Math.max(1, Math.min(info.last_day + 1, max));
    $('empty-note').hidden = info.last_day > 0;
    $('ingest-info').textContent = 'The pack has days ' + (info.days_in_pack.length ? info.days_in_pack[0] + '–' + max : 'none')
      + '. Last ingested: ' + info.last_day + '. Ingesting a day that is already loaded does nothing.';
    const ex = $('ask-examples');
    EXAMPLES.forEach(function (t) {
      ex.appendChild(h('button', { class: 'chip', type: 'button', onclick: function () { $('ask-text').value = t; updateCli(); $('ask-text').focus(); } }, t));
    });
  }

  let meetingsSeq = 0;   // only the newest request may draw, or overlapping loads would append twice
  async function loadMeetings() {
    const seq = ++meetingsSeq;
    const box = R.clear($('brief-meetings'));
    if (state.cmd !== 'brief') return;
    try {
      const data = await R.api('/api/meetings?person=' + encodeURIComponent($('person').value));
      if (seq !== meetingsSeq) return;
      data.meetings.forEach(function (m) { state.titles[$('person').value + '/' + m.event_id] = m.title + ' · ' + fmt.day(m.date); });
      const target = $('brief-date').value;
      const near = data.meetings.slice().sort(function (a, b) {
        return Math.abs(new Date(a.date) - new Date(target || a.date)) - Math.abs(new Date(b.date) - new Date(target || b.date));
      }).slice(0, 10).sort(function (a, b) { return a.start < b.start ? -1 : 1; });
      if (!near.length) { box.appendChild(h('span', { class: 'small muted' }, 'This person has no meetings in the data.')); return; }
      near.forEach(function (m) {
        box.appendChild(h('button', { class: 'chip', type: 'button', title: m.event_id, onclick: function () { $('brief-date').value = m.date; updateCli(); } },
          fmt.day(m.date) + ' · ' + m.title));
      });
    } catch (e) { if (seq === meetingsSeq) box.appendChild(h('span', { class: 'small muted' }, e.message)); }
  }

  function draft() {   // what the command line would be right now, without validating
    const person = $('person').value;
    if (state.cmd === 'brief') return ['brief', '--person', person, '--date', $('brief-date').value || '…'].concat($('brief-summary').checked ? ['--summary'] : []);
    if (state.cmd === 'ask') {
      const at = $('ask-at').value;
      return ['ask', '--person', person, '--at', at ? (at.length === 16 ? at + ':00' : at) + 'Z' : '…', '--ask', $('ask-text').value.trim() || '…'];
    }
    if (state.cmd === 'forget') return ['forget', '--person', person, '--what', $('forget-text').value.trim() ? '(your text)' : '…'];
    return ['ingest', '--day', $('ingest-day').value || '…'];
  }
  function updateCli() {
    $('cli').textContent = 'python assist.py ' + draft().map(quote).join(' ');
    $('ingest-confirm-row').hidden = !(state.cmd === 'ingest' && parseInt($('ingest-day').value, 10) === 1);
  }

  function readRequest() {
    const person = $('person').value;
    if (state.cmd === 'brief') {
      if (!$('brief-date').value) throw new Error('Pick a meeting date.');
      return { cmd: 'brief', person: person, date: $('brief-date').value, summary: $('brief-summary').checked };
    }
    if (state.cmd === 'ask') {
      let at = $('ask-at').value;
      if (!at) throw new Error('Pick the time the question is asked.');
      if (at.length === 16) at += ':00';
      const ask = $('ask-text').value.trim();
      if (!ask) throw new Error('Type a question.');
      return { cmd: 'ask', person: person, at: at + 'Z', ask: ask };
    }
    if (state.cmd === 'forget') {
      const what = $('forget-text').value.trim();
      if (!what) throw new Error('Type what to forget.');
      return { cmd: 'forget', person: person, what: what };
    }
    const day = parseInt($('ingest-day').value, 10);
    if (!(day >= 1)) throw new Error('Enter a day number, 1 or more.');
    const body = { cmd: 'ingest', day: day };
    if (day === 1) {
      if (!$('ingest-confirm').checked) throw new Error('Tick the box to confirm that day 1 erases the current store.');
      body.confirm_reset = true;
    }
    return body;
  }

  function setCmd(cmd) {
    state.cmd = cmd;
    document.querySelectorAll('.tab[data-cmd]').forEach(function (t) { t.setAttribute('aria-selected', String(t.dataset.cmd === cmd)); });
    ['brief', 'ask', 'forget', 'ingest'].forEach(function (c) { $('f-' + c).hidden = c !== cmd; });
    $('person-field').hidden = cmd === 'ingest';
    updateCli();
    loadMeetings();
  }

  /* ------------------------------------------------------------ rendering a result */
  function srcChip(id, person) {
    return h('button', { class: 'chip', type: 'button', title: 'Open this record as ' + person, onclick: function () { openRecord(person, id); } }, id);
  }
  function srcs(ids, person) { return h('span', { class: 'srcs' }, (ids || []).map(function (i) { return srcChip(i, person); })); }
  function section(title) { return h('div', { class: 'sect' }, h('h4', null, title)); }
  function personName(id) { const p = state.people[id]; return p ? p.name : id; }

  function usageChips(u) {
    if (!u) return null;
    const box = h('div', { class: 'row small muted', style: 'margin-top:10px' });
    box.appendChild(R.chip('tokens in ' + fmt.num(u.input_tokens)));
    box.appendChild(R.chip('out ' + fmt.num(u.output_tokens)));
    box.appendChild(R.chip(fmt.cost(u.cost_usd)));
    box.appendChild(R.chip('command ' + fmt.ms(u.ms)));
    return box;
  }

  function renderBrief(list, body) {
    const out = h('div');
    if (!list.length) {
      out.appendChild(h('div', { class: 'note' }, personName(body.person) + ' has no meeting on ' + body.date + '.'));
      return out;
    }
    list.forEach(function (b) {
      const card = h('div', { class: 'card' });
      const title = state.titles[body.person + '/' + b.meeting_id];
      card.appendChild(h('h3', null, title ? h('span', null, title) : null, h('span', { class: title ? 'mono small muted' : 'mono' }, b.meeting_id), R.badge(b.status === 'brief' ? 'brief' : 'nothing new', b.status === 'brief' ? 'info' : 'mute')));
      if ('summary' in b) {
        const s = section('Summary');
        if (b.summary) {
          s.appendChild(h('div', { class: 'summary-box' }, b.summary.text, ' ', srcs(b.summary.sources, body.person)));
        } else {
          s.appendChild(h('div', { class: 'note' }, b.status === 'brief'
            ? 'No summary: the model is off, over its budget or unreachable, or nothing could be said safely. The brief below is complete.'
            : 'Nothing new to summarise.'));
        }
        card.appendChild(s);
      }
      if ((b.changed || []).length) {
        const s = section('What changed');
        s.appendChild(h('ul', { class: 'lines' }, b.changed.map(function (c) {
          return h('li', null, h('span', { class: 'txt' }, c.text), srcs(c.sources, body.person));
        })));
        card.appendChild(s);
      }
      if ((b.promises || []).length) {
        const s = section('Open promises');
        const rows = b.promises.map(function (p) {
          const overdue = p.due && p.due < body.date;
          return h('tr', null, h('td', null, personName(p.owner)), h('td', null, p.what),
            h('td', null, p.due, ' ', overdue ? R.badge('overdue', 'warn') : null), h('td', null, srcs(p.sources, body.person)));
        });
        s.appendChild(h('table', null, h('thead', null, h('tr', null, h('th', null, 'Owner'), h('th', null, 'What'), h('th', null, 'Due'), h('th', null, 'Sources'))), h('tbody', null, rows)));
        card.appendChild(s);
      }
      if ((b.open_these || []).length) {
        const s = section('Worth opening');
        s.appendChild(h('ul', { class: 'lines' }, b.open_these.map(function (o) {
          return h('li', null, srcChip(o.record, body.person), h('span', { class: 'txt' }, o.why));
        })));
        card.appendChild(s);
      }
      if (b.question_to_ask) {
        const s = section('Question to ask');
        s.appendChild(h('div', null, b.question_to_ask.text, ' ', srcs(b.question_to_ask.sources, body.person)));
        card.appendChild(s);
      }
      card.appendChild(usageChips(b.usage));
      out.appendChild(card);
    });
    return out;
  }

  function renderAsk(o, body) {
    const card = h('div', { class: 'card' });
    if (o.refused) {
      card.appendChild(h('h3', null, 'Refused', R.badge('private', 'bad')));
      card.appendChild(h('div', null, 'This answer is private to someone else, so nothing about it is shown. Reason: ' + (o.refused.reason || 'private') + '.'));
    } else if (o.clarify) {
      card.appendChild(h('h3', null, 'Needs clarification', R.badge('ask back', 'warn')));
      card.appendChild(h('div', { class: 'answer' }, o.clarify.question));
      const s = section('Options');
      s.appendChild(h('ul', { class: 'lines' }, (o.clarify.options || []).map(function (opt) {
        return h('li', null, R.chip(opt.id, ''), h('span', { class: 'txt' }, opt.text),
          h('button', { class: 'btn small', type: 'button', onclick: function () {
            $('ask-text').value = 'What is the status of ' + opt.text + '?'; updateCli(); $('ask-text').focus(); } }, 'Ask about this'));
      })));
      card.appendChild(s);
    } else if (o.answer) {
      const nothing = o.answer.text === 'I have nothing on that.';
      card.appendChild(h('h3', null, 'Answer', nothing ? R.badge('nothing found', 'mute') : R.badge('answered', 'ok')));
      card.appendChild(h('div', { class: 'answer' }, o.answer.text));
      if ((o.answer.sources || []).length) {
        const s = section('Sources');
        s.appendChild(srcs(o.answer.sources, body.person));
        card.appendChild(s);
      }
    }
    if ((o.matched || []).length) {
      const s = section('Matched');
      s.appendChild(h('div', { class: 'row' }, o.matched.map(function (m) {
        return R.chip((m.phrase ? m.phrase + ' → ' : '') + m.id, 'info');
      })));
      card.appendChild(s);
    }
    card.appendChild(usageChips(o.usage));
    return card;
  }

  function renderPlain(o) {
    const dl = h('dl', { class: 'kv' });
    Object.keys(o || {}).forEach(function (k) {
      dl.appendChild(h('dt', null, k));
      dl.appendChild(h('dd', null, typeof o[k] === 'object' ? JSON.stringify(o[k]) : String(o[k])));
    });
    return h('div', { class: 'card' }, h('h3', null, o && o.ok === false ? R.badge('failed', 'bad') : R.badge('done', 'ok')), dl);
  }

  function showResult(entry) {
    const res = entry.res, body = entry.body;
    const out = R.clear($('out-formatted'));
    const errBox = $('out-error');
    errBox.hidden = true;
    const chips = R.clear($('res-chips'));
    chips.appendChild(R.chip(res.exit === 0 ? 'exit 0' : 'exit ' + res.exit, res.exit === 0 ? 'ok' : 'bad'));
    chips.appendChild(R.chip(fmt.ms(res.ms)));
    chips.appendChild(R.chip(res.valid_json ? 'valid JSON' : 'not JSON', res.valid_json ? 'ok' : 'bad'));
    state.raw = res.valid_json ? JSON.stringify(res.result, null, 2) : '(the command printed no valid JSON)';
    $('raw-pre').textContent = state.raw;
    const se = $('stderr-box');
    se.hidden = !res.stderr;
    $('stderr-pre').textContent = res.stderr || '';
    if (res.exit !== 0) {
      errBox.hidden = false;
      errBox.textContent = res.exit === 'timeout' ? 'The command did not finish in time.' : 'The command exited with code ' + res.exit + '.';
    }
    if (!res.valid_json) {
      out.appendChild(h('div', { class: 'note bad' }, 'The command did not print valid JSON. See the messages below.'));
      se.open = true;
      return;
    }
    const r = res.result;
    if (body.cmd === 'brief' && Array.isArray(r)) out.appendChild(renderBrief(r, body));
    else if (body.cmd === 'ask' && r && typeof r === 'object') out.appendChild(renderAsk(r, body));
    else out.appendChild(renderPlain(r));
  }

  /* ------------------------------------------------------------ the record viewer */
  async function openRecord(person, id) {
    const dlg = $('record-dialog'), body = R.clear($('record-body'));
    $('record-title').textContent = 'Record ' + id;
    body.appendChild(h('div', { class: 'muted' }, 'Loading…'));
    if (!dlg.open) dlg.showModal();
    try {
      const r = await R.api('/api/record?person=' + encodeURIComponent(person) + '&id=' + encodeURIComponent(id));
      R.clear(body);
      const rows = [['Type', r.type], ['Time', fmt.time(r.at)], ['Author', (r.author_name || '') + ' (' + r.author + ')'],
        ['Client', r.client || 'none'], ['Visibility', r.visibility], ['Kind', r.kind], ['Ingest day', r.day]];
      if (r.title) rows.splice(1, 0, ['Title', r.title]);
      const dl = h('dl', { class: 'kv' });
      rows.forEach(function (row) { dl.appendChild(h('dt', null, row[0])); dl.appendChild(h('dd', null, String(row[1]))); });
      body.appendChild(dl);
      body.appendChild(h('pre', null, r.text + (r.truncated ? '\n… (shortened)' : '')));
      body.appendChild(h('div', { class: 'note' }, 'Shown through the same visibility gate as the CLI, as ' + personName(person) + ' (' + person + ').'));
    } catch (e) {
      R.clear(body);
      body.appendChild(h('div', { class: 'note bad' }, e.message));
      body.appendChild(h('div', { class: 'small muted' }, 'Either the record does not exist, or ' + personName(person) + ' is not allowed to see it.'));
    }
  }

  /* ------------------------------------------------------------ running, history */
  function history() { return stored(HISTORY_KEY) || []; }
  function pushHistory(entry) {
    const e = JSON.parse(JSON.stringify(entry));
    if (e.body.cmd === 'forget') {            // what a person asked to forget is never kept in this page
      e.body = { cmd: 'forget', person: e.body.person };
      e.res.cli = 'python assist.py forget --person ' + e.body.person + ' --what "(not saved)"';
    }
    if (JSON.stringify(e).length > 60000) { e.res.result = null; e.res.valid_json = false; e.trimmed = true; }
    const list = [e].concat(history()).slice(0, MAX_HISTORY);
    stored(HISTORY_KEY, list);
    renderHistory();
  }
  function renderHistory() {
    const box = R.clear($('history'));
    const list = history();
    if (!list.length) { box.appendChild(h('div', { class: 'muted small' }, 'Nothing yet.')); return; }
    list.forEach(function (e) {
      box.appendChild(h('button', { class: 'history-item', type: 'button', title: 'Show this result again', onclick: function () { loadEntry(e); } },
        h('span', { class: 'small muted' }, fmt.time(e.at)), R.badge(e.body.cmd, 'info'), h('code', null, e.res.cli),
        R.chip(fmt.ms(e.res.ms))));
    });
  }
  function loadEntry(e) {
    const b = e.body;
    setCmd(b.cmd);
    if (b.person && state.people[b.person]) $('person').value = b.person;
    if (b.cmd === 'brief') { $('brief-date').value = b.date; $('brief-summary').checked = !!b.summary; }
    if (b.cmd === 'ask') { $('ask-at').value = b.at.replace(/(:\d\d)?Z$/, ''); $('ask-text').value = b.ask; }
    if (b.cmd === 'ingest') $('ingest-day').value = b.day;
    updateCli();
    if (e.trimmed) { $('out-error').hidden = false; $('out-error').textContent = 'This result was too large to keep. Run the command again.'; return; }
    showResult(e);
  }

  async function run() {
    if (state.running) return;
    const status = $('run-status');
    let body;
    try { body = readRequest(); } catch (e) { status.textContent = e.message; return; }
    state.running = true;
    $('run').disabled = true;
    R.clear(status).appendChild(h('span', { class: 'spinner', 'aria-hidden': 'true' }));
    status.appendChild(document.createTextNode(' Running…'));
    try {
      stored(PERSON_KEY, $('person').value);
      const res = await R.api('/api/run', body);
      const entry = { at: new Date().toISOString(), body: body, res: res };
      showResult(entry);
      pushHistory(entry);
      status.textContent = 'Done in ' + fmt.ms(res.ms) + '.';
      if (body.cmd === 'ingest' || body.cmd === 'forget') R.loadStatus($('status')).then(function (info) { if (info) { state.info = info; $('empty-note').hidden = info.last_day > 0; } });
    } catch (e) {
      status.textContent = '';
      const box = $('out-error');
      box.hidden = false;
      box.textContent = e.message;
    } finally {
      state.running = false;
      $('run').disabled = false;
    }
  }

  /* ------------------------------------------------------------ start */
  function applyHash() {
    const p = new URLSearchParams(location.hash.replace(/^#/, ''));
    const cmd = p.get('cmd');
    if (!cmd || ['brief', 'ask', 'forget', 'ingest'].indexOf(cmd) < 0) return;
    setCmd(cmd);
    if (p.get('person') && state.people[p.get('person')]) $('person').value = p.get('person');
    if (p.get('date')) $('brief-date').value = p.get('date');
    if (p.get('summary') === '1') $('brief-summary').checked = true;
    if (p.get('at')) $('ask-at').value = p.get('at').replace(/(:\d\d)?Z$/, '');
    if (p.get('q')) $('ask-text').value = p.get('q');
    if (p.get('day')) $('ingest-day').value = p.get('day');
    updateCli();
    loadMeetings();
    // Only read-only commands ever run from a link; forget and ingest always wait for a click.
    if (p.get('run') === '1' && (cmd === 'brief' || cmd === 'ask')) run();
  }

  async function init() {
    renderHistory();
    const info = await R.loadStatus($('status'));
    if (!info) { $('out-error').hidden = false; $('out-error').textContent = 'Could not load the server information.'; return; }
    state.info = info;
    fillPeople();
    defaults();
    document.querySelectorAll('.tab[data-cmd]').forEach(function (t) { t.addEventListener('click', function () { setCmd(t.dataset.cmd); }); });
    document.querySelectorAll('.tab[data-view]').forEach(function (t) {
      t.addEventListener('click', function () {
        document.querySelectorAll('.tab[data-view]').forEach(function (x) { x.setAttribute('aria-selected', String(x === t)); });
        $('out-formatted').hidden = t.dataset.view !== 'formatted';
        $('out-raw').hidden = t.dataset.view !== 'raw';
      });
    });
    $('person').addEventListener('change', function () { stored(PERSON_KEY, $('person').value); updateCli(); loadMeetings(); });
    ['brief-date', 'brief-summary', 'ask-at', 'ask-text', 'forget-text', 'ingest-day'].forEach(function (id) { $(id).addEventListener('input', updateCli); });
    $('brief-date').addEventListener('change', loadMeetings);
    $('ask-eod').addEventListener('click', function () {
      if (state.info.day1 && state.info.last_day > 0) { $('ask-at').value = addDays(state.info.day1, state.info.last_day - 1) + 'T18:00'; updateCli(); }
    });
    $('ask-now').addEventListener('click', function () { $('ask-at').value = new Date().toISOString().slice(0, 16); updateCli(); });
    $('form').addEventListener('submit', function (e) { e.preventDefault(); run(); });
    document.addEventListener('keydown', function (e) { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); run(); } });
    $('copy-cli').addEventListener('click', function () { R.copyText($('cli').textContent); });
    $('copy-raw').addEventListener('click', function () { R.copyText(state.raw); });
    $('clear-history').addEventListener('click', function () { stored(HISTORY_KEY, []); renderHistory(); });
    $('record-close').addEventListener('click', function () { $('record-dialog').close(); });
    $('record-dialog').addEventListener('click', function (e) { if (e.target === $('record-dialog')) $('record-dialog').close(); });
    setCmd('brief');
    applyHash();
  }
  init();
})();
