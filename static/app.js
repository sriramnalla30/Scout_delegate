(() => {
const $ = s => document.querySelector(s);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const AVATAR = '<svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="#18aa7e"/><circle cx="16" cy="16" r="6" fill="none" stroke="#fff" stroke-width="2.5"/><circle cx="16" cy="16" r="1.8" fill="#fff"/></svg>';
const EXAMPLES = ['I want gyms in Koramangala Bangalore', 'Find cafes in Indiranagar', 'Best pharmacies near me', 'Hospitals in Hyderabad'];
const newId = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '') : String(Date.now()) + Math.random().toString(16).slice(2));
let threadId = newId(), waiting = false, busy = false, typingEl = null, locked = false;

// History: each chat keeps its messages and the server's events, so opening it replays the same screen.
// The server stores them as files in its data folder; `chats` holds only the summaries for the sidebar.
const MAX_LOG = 300;
let chats = [], current = null;
async function loadChats() { try { chats = await (await fetch('/api/chats')).json(); } catch (e) { chats = []; } renderHistory(); }
function save() {
  if (!current) return;
  fetch('/api/chats/' + current.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: current.title, updated: current.updated, log: current.log }) }).catch(() => { /* offline: the chat stays on screen but is not saved */ });
}
function ago(t) { const m = Math.round((Date.now() - t) / 60000); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : new Date(t).toLocaleDateString(); }
function record(entry, title) {
  if (!current) { current = { id: threadId, title: short(title || 'New chat', 48), updated: Date.now(), log: [] }; chats.unshift({ id: current.id, title: current.title, updated: current.updated }); }
  current.log.push(entry); if (current.log.length > MAX_LOG) current.log.splice(0, current.log.length - MAX_LOG);
  current.updated = Date.now();
  const summary = chats.find(c => c.id === current.id); if (summary) summary.updated = current.updated;
}
function renderHistory() {
  const ul = $('#hist'); ul.replaceChildren();
  if (!chats.length) { const li = el('li'); li.append(el('p', 'Your chats will be saved here.', 'hist-empty')); ul.append(li); return; }
  [...chats].sort((a, b) => b.updated - a.updated).forEach(c => {
    const li = el('li', undefined, c.id === threadId ? 'on' : ''), open = el('button', undefined, 'open'); open.type = 'button';
    open.append(el('span', c.title), el('small', ago(c.updated))); open.addEventListener('click', () => openChat(c.id));
    const del = el('button', '×', 'del'); del.type = 'button'; del.title = 'Delete chat'; del.setAttribute('aria-label', 'Delete chat ' + c.title);
    del.addEventListener('click', () => removeChat(c.id)); li.append(open, del); ul.append(li);
  });
}
async function removeChat(id) {
  try { await fetch('/api/chats/' + id, { method: 'DELETE' }); } catch (e) { return; }
  chats = chats.filter(c => c.id !== id); if (id === threadId) reset(); else renderHistory();
}
async function openChat(id) {
  if (busy) return;
  let chat; try { const r = await fetch('/api/chats/' + id); if (!r.ok) throw Error(); chat = await r.json(); } catch (e) { return; }
  clearScreen(); threadId = id; current = chat; started(); $('#histcol').classList.remove('show');
  chat.log.forEach(e => { if (e.u !== undefined) { addUser(e.u); thinkReset(); think('Reading your message', short(e.u, 120)); } else handle(e.ev); });
  thinkDone(); hideTyping();
  try { const s = await (await fetch('/api/thread/' + id)).json(); locked = !s.exists; waiting = !!s.waiting; } catch (e) { locked = true; }
  setBusy(false); renderHistory();
}

function scrollDown() { window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }); }
function started() { $('.chatcol').classList.add('started'); }

function addUser(text) { started(); const m = el('div', undefined, 'msg user'); m.append(el('div', text, 'bubble')); $('#thread').append(m); scrollDown(); }
function addBot(text, kind) {
  started(); const m = el('div', undefined, 'msg bot' + (kind ? ' ' + kind : ''));
  const av = el('div', undefined, 'avatar'); av.innerHTML = AVATAR;
  const b = el('div', undefined, 'bubble');
  if (kind === 'ask') b.append(el('div', 'needs your input', 'tag'), el('div', text));
  else b.textContent = text;
  m.append(av, b); $('#thread').append(m); scrollDown();
}
function showTyping() { hideTyping(); typingEl = el('div', undefined, 'msg bot'); const av = el('div', undefined, 'avatar'); av.innerHTML = AVATAR; const t = el('div', undefined, 'bubble'); const dots = el('span', undefined, 'typing'); dots.append(el('i'), el('i'), el('i')); t.append(dots); typingEl.append(av, t); $('#thread').append(typingEl); scrollDown(); }
function hideTyping() { if (typingEl) { typingEl.remove(); typingEl = null; } }

function kvRow(label, value) { const r = el('div', undefined, 'kv'); r.append(el('span', label), el('b', value)); return r; }
function addResult(data) {
  const a = data.args || {}, failed = data.status === 'failed';
  const card = el('div', undefined, 'result' + (failed ? ' warn' : ''));
  card.append(el('div', failed ? 'parser could not finish' : 'arguments ready for the search tool', 'tag'), el('h3', failed ? 'Search request' : a.query));
  if (failed) card.append(el('p', data.problem || 'Unknown problem.', 'problem'));
  card.append(kvRow('business', data.business_type || '-'), kvRow('area', data.area || '-'), kvRow('city', data.city || '-'));
  if (!failed) {
    card.append(el('div', 'tool arguments', 'args-title'), el('pre', JSON.stringify(a, null, 2), 'args'));
    const parts = ['gmaps', '-fast-mode', '-json', '-c 1', `-geo "${a.lat},${a.lng}"`];
    if (a.radius_m != null) parts.push('-radius ' + a.radius_m);
    if (a.lang && a.lang !== 'en') parts.push('-lang ' + a.lang);
    parts.push('-input q.txt');
    card.append(el('div', 'as a command', 'args-title'), el('pre', parts.join(' ') + '\n# q.txt: ' + a.query, 'cmd'));
  }
  card.append(el('p', 'The Google Maps scraper is not connected in this demo, so nothing was searched.', 'note'));
  $('#thread').append(card); scrollDown();
}

function short(value, n = 150) { const s = typeof value === 'string' ? value : JSON.stringify(value); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
function step(text, cls, who, small) {
  const s = el('div', undefined, 'step ' + (cls || '')); if (who) s.append(el('span', who + ' ', 'who')); s.append(document.createTextNode(text));
  if (small) s.append(el('small', small)); const t = $('#trace'); if (t.querySelector('p.muted')) t.replaceChildren(); t.append(s); t.scrollTop = t.scrollHeight;
}
// "Scout is thinking": a running timeline of decisions in plain words. The newest item pulses until the next one arrives.
const FIELD_NAMES = { business_type: 'the type of place', area: 'the area', city: 'the city' };
const THINK_IDLE = 'Send a message and Scout\'s decisions show up here, step by step.';
function think(title, detail, cls) {
  const list = $('#think'); list.querySelectorAll('li.idle').forEach(li => li.remove());
  list.querySelectorAll('li.active').forEach(li => li.classList.remove('active'));
  const li = el('li', undefined, 'active ' + (cls || '')); li.append(el('i'));
  const d = el('div'); d.append(el('b', title)); if (detail) d.append(el('span', detail)); li.append(d); list.append(li); list.scrollTop = list.scrollHeight;
}
function thinkDone() { $('#think').querySelectorAll('li.active').forEach(li => li.classList.remove('active')); }
function thinkReset() { const li = el('li', undefined, 'idle'); li.append(el('i')); const d = el('div'); d.append(el('span', THINK_IDLE)); li.append(d); $('#think').replaceChildren(li); }

function explainCall(ev) {
  if (ev.name === 'parser_agent') return ['I\'ll ask the parser agent what the search tool needs', short(ev.args.request, 140)];
  if (ev.name === 'ask_user') return ['The parser needs more from you, so I\'m asking you', short(ev.args.question, 140)];
  return [`Calling ${ev.name}`, short(ev.args, 120)];
}
function explainParser(data) {
  if (data.status === 'ready') return ['Parser says it is ready and built the search arguments', data.args && data.args.query];
  if (data.status === 'needs_input') return ['Parser says it is missing ' + ((data.missing || []).map(m => FIELD_NAMES[m] || m).join(' and ') || 'a detail'), 'I\'ll relay its question to you'];
  return ['Parser could not finish', data.problem];
}

function handle(ev) {
  switch (ev.type) {
    case 'thought':
      think(ev.text, undefined, 'note'); break;
    case 'tool_call': {
      const [title, detail] = explainCall(ev); think(title, detail, ev.name === 'ask_user' ? 'ask' : '');
      step(`→ ${ev.name}(${short(ev.args)})`, '', 'scout'); break;
    }
    case 'tool_result':
      if (ev.name === 'parser_agent' && ev.data && typeof ev.data === 'object') {
        const [title, detail] = explainParser(ev.data); think(title, detail, ev.data.status === 'failed' ? 'bad' : '');
        if (ev.data.status !== 'needs_input') addResult(ev.data);
      } else if (ev.name === 'ask_user') think('Got your answer', short(ev.data, 100));
      step(`← ${ev.name}`, ev.data && ev.data.status === 'failed' ? 'fail' : '', 'scout', short(ev.data, 110)); break;
    case 'sub':
      if (ev.kind === 'tool_call') { think('Parser is looking up the map pin', short(ev.args.place || ev.args, 100)); step(`→ ${ev.name}(${short(ev.args, 100)})`, 'sub', 'parser'); }
      else if (ev.kind === 'tool_result') { const bad = /error/.test(ev.content || ''); if (bad) think('Map lookup failed, trying again or asking', undefined, 'bad'); step(`← ${ev.name}`, 'sub' + (bad ? ' fail' : ''), 'parser', short(ev.content, 100)); }
      else if (ev.kind === 'result') step('structured result ready', 'sub', 'parser');
      break;
    case 'interrupt':
      hideTyping(); waiting = true; think('Paused. Waiting for your answer', undefined, 'ask'); step('⏸ paused, waiting for your answer', 'pause', 'scout'); addBot(ev.question, 'ask'); break;
    case 'message':
      hideTyping(); think('Done. Replying to you'); thinkDone(); step('✓ replied', '', 'scout'); addBot(ev.text); break;
    case 'error':
      hideTyping(); started(); { const m = el('div', undefined, 'msg err'); m.append(el('div', ev.message, 'bubble')); $('#thread').append(m); } think('Something went wrong', ev.message, 'bad'); thinkDone(); step(ev.message, 'fail', 'error'); break;
    case 'done':
      hideTyping(); waiting = !!ev.waiting; if (waiting) thinkDone(); break;
  }
}

function setBusy(on) {
  busy = on; $('#send').disabled = on || locked; $('#input').disabled = on || locked;
  const hint = $('#composer-hint'); hint.hidden = !((waiting || locked) && !on);
  hint.textContent = locked ? 'This chat has ended. Start a new chat to continue' : 'Scout is waiting for your answer';
  $('#input').placeholder = locked ? 'Start a new chat to ask again…' : waiting ? 'Type your answer…' : 'Ask Scout to find a place…';
  if (!on && !locked) $('#input').focus();
}

function onEvent(ev) { record({ ev }); handle(ev); }

async function send(text) {
  if (busy || locked || !text.trim()) return;
  record({ u: text }, text); renderHistory();
  addUser(text); $('#input').value = ''; autosize(); setBusy(true); showTyping();
  if (waiting) think('You answered, so I\'m handing it back to the parser', short(text, 100));
  else { thinkReset(); think('Reading your message', short(text, 120)); }
  try {
    const resp = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ thread_id: threadId, message: text }) });
    if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw Error(e.error || 'The server rejected the message.'); }
    const reader = resp.body.getReader(), dec = new TextDecoder(); let buf = '';
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); const parts = buf.split('\n\n'); buf = parts.pop();
      parts.forEach(p => { const line = p.split('\n').find(l => l.startsWith('data: ')); if (line) { try { onEvent(JSON.parse(line.slice(6))); } catch (e) { /* ignore a malformed event */ } } });
    }
  } catch (e) { onEvent({ type: 'error', message: e.message || 'Could not reach the server.' }); onEvent({ type: 'done', waiting: false }); }
  save(); renderHistory(); setBusy(false);
}

function autosize() { const t = $('#input'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 140) + 'px'; }
function clearScreen() {
  waiting = false; busy = false; locked = false; hideTyping();
  $('#thread').replaceChildren(); $('#trace').replaceChildren(el('p', 'Steps appear here as the agents work.', 'muted'));
  $('.chatcol').classList.remove('started'); thinkReset();
}
function reset() {
  clearScreen(); threadId = newId(); current = null; setBusy(false); renderHistory(); window.scrollTo({ top: 0 });
}

EXAMPLES.forEach(t => { const c = el('button', t, 'chip'); c.type = 'button'; c.addEventListener('click', () => send(t)); $('#chips').append(c); });
$('#composer').addEventListener('submit', e => { e.preventDefault(); send($('#input').value); });
$('#input').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send($('#input').value); } });
$('#input').addEventListener('input', autosize);
$('#new-chat').addEventListener('click', reset);
$('#toggle-history').addEventListener('click', () => $('#histcol').classList.toggle('show'));
setBusy(false); loadChats();
})();
