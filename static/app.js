(() => {
const $ = s => document.querySelector(s);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const EXAMPLES = ['I want gyms in Koramangala Bangalore', 'Find cafes in Indiranagar', 'Best pharmacies near me', 'Hospitals in Hyderabad'];
const newId = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '') : String(Date.now()) + Math.random().toString(16).slice(2));
// Every API call goes through auth.js, which adds the signed-in account's token. The API may live on another site (config.js).
// The server files chats under the account, so a signed-in person only ever sees their own history.
const api = (path, options) => Auth.fetch(path, options);
let threadId = newId(), waiting = false, busy = false, typingEl = null, locked = false;
// Live events are revealed one at a time so a person can follow them; reopening a saved chat skips the pacing.
const REDUCED = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
const PAUSE = { thought: 900, tool_call: 1100, tool_result: 1100, sub: 950, interrupt: 300, message: 300, error: 300, done: 0 };
const sleep = ms => new Promise(r => setTimeout(r, ms));
let live = false, queue = [], draining = null;

// History: each chat keeps its messages and the server's events, so opening it replays the same screen.
// The server stores them as files in its data folder; `chats` holds only the summaries for the sidebar.
const MAX_LOG = 300;
let chats = [], current = null;
async function loadChats() { try { const r = await api('/api/chats'); const list = r.ok ? await r.json() : []; chats = Array.isArray(list) ? list : []; } catch (e) { chats = []; } renderHistory(); }
function save() {
  if (!current) return;
  api('/api/chats/' + current.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: current.title, updated: current.updated, log: current.log }) }).catch(() => { /* offline: the chat stays on screen but is not saved */ });
}
function ago(t) { const m = Math.round((Date.now() - t) / 60000); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : new Date(t).toLocaleDateString(); }
function record(entry, title) {
  if (!current) { current = { id: threadId, title: short(title || 'New chat', 48), updated: Date.now(), log: [] }; chats.unshift({ id: current.id, title: current.title, updated: current.updated }); }
  current.log.push(entry); if (current.log.length > MAX_LOG) current.log.splice(0, current.log.length - MAX_LOG);
  current.updated = Date.now();
  const summary = chats.find(c => c.id === current.id); if (summary) summary.updated = current.updated;
  syncSession();
}
function bucket(t) {
  const day = 86400000, start = new Date().setHours(0, 0, 0, 0);
  return t >= start ? 'Today' : t >= start - day ? 'Yesterday' : t >= start - 6 * day ? 'Previous 7 days' : 'Older';
}
function renderHistory() {
  const box = $('#hist'); box.replaceChildren();
  const q = $('#hist-search').value.trim().toLowerCase();
  const list = chats.filter(c => c.title.toLowerCase().includes(q)).sort((a, b) => b.updated - a.updated);
  if (!list.length) { box.append(el('p', chats.length ? 'No chats match your search.' : 'Your chats will be saved here.', 'hist-empty')); return; }
  let group = '';
  list.forEach(c => { const g = bucket(c.updated); if (g !== group) { group = g; box.append(el('div', g, 'gh')); } box.append(historyRow(c)); });
}
function historyRow(c) {
  const row = el('div', undefined, 'hrow' + (c.id === threadId ? ' on' : '')), open = el('button', undefined, 'open'); open.type = 'button';
  open.title = 'Session ID: ' + c.id;
  open.append(el('span', c.title), el('small', ago(c.updated))); open.addEventListener('click', () => openChat(c.id));
  const edit = el('button', '✎', 'act'); edit.type = 'button'; edit.title = 'Rename'; edit.setAttribute('aria-label', 'Rename chat ' + c.title);
  edit.addEventListener('click', () => beginRename(row, c));
  const del = el('button', '×', 'act del'); del.type = 'button'; del.title = 'Delete chat'; del.setAttribute('aria-label', 'Delete chat ' + c.title);
  del.addEventListener('click', () => removeChat(c.id));
  row.append(open, edit, del); return row;
}
function beginRename(row, c) {
  const input = el('input', undefined, 'rename'); input.value = c.title; input.maxLength = 80; input.setAttribute('aria-label', 'Chat name');
  row.replaceChildren(input); input.focus(); input.select();
  let done = false;
  const finish = async apply => { if (done) return; done = true; if (apply) await renameChat(c.id, input.value); renderHistory(); };
  input.addEventListener('keydown', e => { if (e.key === 'Enter') finish(true); else if (e.key === 'Escape') finish(false); });
  input.addEventListener('blur', () => finish(true));
}
async function renameChat(id, title) {
  title = title.trim().slice(0, 80); if (!title) return;
  try {
    const chat = current && current.id === id ? current : await (await api('/api/chats/' + id)).json();
    chat.title = title;
    const r = await api('/api/chats/' + id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title, updated: chat.updated, log: chat.log }) });
    if (!r.ok) return;
    const summary = chats.find(c => c.id === id); if (summary) summary.title = title;
  } catch (e) { /* the old name stays */ }
}
function setDrawer(open) {
  $('#drawer').classList.toggle('open', open); $('#drawer').inert = !open; $('#scrim').classList.toggle('show', open);
  $('#open-history').setAttribute('aria-expanded', String(open));
  if (open) { $('#hist-search').value = ''; renderHistory(); $('#hist-search').focus(); } else $('#open-history').focus();
}
async function removeChat(id) {
  try { await api('/api/chats/' + id, { method: 'DELETE' }); } catch (e) { return; }
  chats = chats.filter(c => c.id !== id); if (id === threadId) reset(); else renderHistory();
}
async function openChat(id) {
  if (busy) return;
  let chat; try { const r = await api('/api/chats/' + id); if (!r.ok) throw Error(); chat = await r.json(); } catch (e) { return; }
  clearScreen(); threadId = id; current = chat; started(); syncSession(); setDrawer(false);
  chat.log.forEach(e => { if (e.u !== undefined) { addUser(e.u); thinkReset(); think('Reading your message', short(e.u, 120)); } else handle(e.ev); });
  thinkDone(); hideTyping();
  try { const s = await (await api('/api/thread/' + id)).json(); locked = !s.exists; waiting = !!s.waiting; } catch (e) { locked = true; }
  setBusy(false); renderHistory();
}

// The session id is the chat's thread id. It is also the Langfuse session id, so it finds the chat's traces there.
// The button shows only once a chat exists, because Langfuse has no session before the first message.
function syncSession() {
  const b = $('#copy-session'); b.hidden = !current; b.title = current ? 'Langfuse session ID: ' + current.id : '';
}
async function copySession() {
  if (!current) return;
  const id = current.id, b = $('#copy-session');
  try { await navigator.clipboard.writeText(id); }
  catch (e) { const t = el('textarea'); t.value = id; document.body.append(t); t.select(); try { document.execCommand('copy'); } catch (e2) { /* nothing more to try */ } t.remove(); }
  b.textContent = 'Copied ✓'; b.classList.add('copied');
  setTimeout(() => { b.textContent = 'Copy session ID'; b.classList.remove('copied'); }, 1500);
}

function scrollDown() { window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }); }
// The right-hand column shows "what Scout can do" only on a fresh page. The first message (or opening a saved chat) swaps it for the
// "Coordinator is thinking" card, and that card stays for the rest of the chat. A new chat brings "what Scout can do" back.
function started() { $('.chatcol').classList.add('started'); $('.tracecol').classList.add('thinking'); }

function addUser(text) { started(); const m = el('div', undefined, 'msg user'); m.append(el('div', text, 'bubble')); $('#thread').append(m); scrollDown(); }
function addBot(text, kind) {
  started(); const m = el('div', undefined, 'msg bot' + (kind ? ' ' + kind : ''));
  const b = el('div', undefined, 'bubble'), body = kind === 'ask' ? el('div') : b;
  if (kind === 'ask') b.append(el('div', 'needs your input', 'tag'), body);
  m.append(el('div', 'Coordinator', 'who'), b); $('#thread').append(m); scrollDown();
  if (!live || REDUCED) { body.textContent = text; return Promise.resolve(); }
  return new Promise(done => { // type the reply out, like someone writing it
    let i = 0; const step = Math.max(1, Math.ceil(text.length / 90));
    const timer = setInterval(() => { i = Math.min(text.length, i + step); body.textContent = text.slice(0, i); scrollDown(); if (i >= text.length) { clearInterval(timer); done(); } }, 22);
  });
}
function showTyping() { hideTyping(); typingEl = el('div', undefined, 'msg bot'); const t = el('div', undefined, 'bubble'); const dots = el('span', undefined, 'typing'); dots.append(el('i'), el('i'), el('i')); t.append(dots); typingEl.append(el('div', 'Coordinator', 'who'), t); $('#thread').append(typingEl); scrollDown(); }
function hideTyping() { if (typingEl) { typingEl.remove(); typingEl = null; } }

function kvRow(label, value) { const r = el('div', undefined, 'kv'); r.append(el('span', label), el('b', value)); return r; }
// The scout agent's report. When it found places, the results come first and the report folds away under them.
function addResult(data) {
  const a = data.args || {}, failed = data.status === 'failed', found = !failed && data.search && data.search.id;
  const card = el(found ? 'details' : 'div', undefined, found ? 'report' : 'result' + (failed ? ' warn' : ''));
  if (found) card.append(el('summary', 'How the scout agent built this search'));
  else card.append(el('div', failed ? 'scout agent could not finish' : 'scout agent report', 'tag'), el('h3', failed ? 'Search request' : a.query));
  if (failed) card.append(el('p', data.problem || 'Unknown problem.', 'problem'));
  card.append(kvRow('business', data.business_type || '-'), kvRow('area', data.area || '-'), kvRow('city', data.city || '-'));
  if (!failed && a.query) {
    card.append(el('div', 'tool arguments', 'args-title'), el('pre', JSON.stringify(a, null, 2), 'args'));
    const parts = ['gmaps', '-fast-mode', '-json', '-c 1', `-geo "${a.lat},${a.lng}"`];
    if (a.radius_m != null) parts.push('-radius ' + a.radius_m);
    if (a.lang && a.lang !== 'en') parts.push('-lang ' + a.lang);
    parts.push('-input q.txt');
    card.append(el('div', 'as a command', 'args-title'), el('pre', parts.join(' ') + '\n# q.txt: ' + a.query, 'cmd'));
  }
  if (!found) { $('#thread').append(card); scrollDown(); return Promise.resolve(); }
  const host = el('div', undefined, 'places-host'); $('#thread').append(host, card);
  return window.Places.render(host, data.search.id).then(scrollDown);
}

function short(value, n = 150) { const s = typeof value === 'string' ? value : JSON.stringify(value); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
// "Coordinator is thinking": a running timeline of decisions in plain words. The newest item pulses until the next one arrives.
const FIELD_NAMES = { business_type: 'the type of place', area: 'the area', city: 'the city' };
const THINK_IDLE = 'Send a message and the coordinator\'s decisions show up here, step by step.';
function think(title, detail, cls) {
  const list = $('#think'); list.querySelectorAll('li.idle').forEach(li => li.remove());
  list.querySelectorAll('li.active').forEach(li => li.classList.remove('active'));
  const li = el('li', undefined, 'active ' + (cls || '')); li.append(el('i'));
  const d = el('div'); d.append(el('b', title)); if (detail) d.append(el('span', detail)); li.append(d); list.append(li); list.scrollTop = list.scrollHeight;
}
function thinkDone() { $('#think').querySelectorAll('li.active').forEach(li => li.classList.remove('active')); }
function thinkReset() { const li = el('li', undefined, 'idle'); li.append(el('i')); const d = el('div'); d.append(el('span', THINK_IDLE)); li.append(d); $('#think').replaceChildren(li); }

function explainCall(ev) {
  if (ev.name === 'scout_agent') return ['I\'ll ask the scout agent what the search tool needs', short(ev.args.request, 140)];
  if (ev.name === 'ask_user') return ['The scout agent needs more from you, so I\'m asking you', short(ev.args.question, 140)];
  return [`Calling ${ev.name}`, short(ev.args, 120)];
}
function explainScout(data) {
  if (data.status === 'ready') {
    const n = data.search && data.search.count, best = data.search && data.search.highlights && data.search.highlights.best;
    return n ? [`Scout agent searched Google Maps and found ${n} places`, best ? `best overall: ${best.name} (${best.rating} ★, ${best.reviews} reviews)` : data.args && data.args.query]
             : ['Scout agent says it is ready and built the search arguments', data.args && data.args.query];
  }
  if (data.status === 'needs_input') return ['Scout agent says it is missing ' + ((data.missing || []).map(m => FIELD_NAMES[m] || m).join(' and ') || 'a detail'), 'I\'ll relay its question to you'];
  return ['Scout agent could not finish', data.problem];
}

function handle(ev) {
  switch (ev.type) {
    case 'thought':
      think(ev.text, undefined, 'note'); break;
    case 'tool_call': {
      const [title, detail] = explainCall(ev); think(title, detail, ev.name === 'ask_user' ? 'ask' : ''); break;
    }
    case 'tool_result':
      if (ev.name === 'scout_agent' && ev.data && typeof ev.data === 'object') {
        const [title, detail] = explainScout(ev.data); think(title, detail, ev.data.status === 'failed' ? 'bad' : '');
        if (ev.data.status !== 'needs_input') return addResult(ev.data);
      } else if (ev.name === 'ask_user') think('Got your answer', short(ev.data, 100));
      break;
    case 'sub':
      if (ev.kind === 'tool_call' && ev.name === 'search_places') think('Scout agent is searching Google Maps', ev.args.query + (ev.args.radius_m ? ` within ${ev.args.radius_m / 1000} km` : ''));
      else if (ev.kind === 'tool_call') think('Scout agent is looking up the map pin', (ev.args.area ? `${ev.args.area}, ${ev.args.city}` : short(ev.args, 100)));
      else if (ev.kind === 'tool_result' && ev.name === 'search_places') {
        const n = /"count":\s*(\d+)/.exec(ev.content || '');
        if (n) think(`Google Maps returned ${n[1]} places`); else think('The Google Maps search failed', undefined, 'bad');
      } else if (ev.kind === 'tool_result' && /error/.test(ev.content || '')) think('Map lookup failed, trying again or asking', undefined, 'bad');
      break;
    case 'interrupt':
      hideTyping(); waiting = true; think('Paused. Waiting for your answer', undefined, 'ask'); return addBot(ev.question, 'ask');
    case 'message':
      hideTyping(); think('Done. Replying to you'); thinkDone(); return addBot(ev.text);
    case 'error':
      hideTyping(); started(); { const m = el('div', undefined, 'msg err'); m.append(el('div', ev.message, 'bubble')); $('#thread').append(m); } think('Something went wrong', ev.message, 'bad'); thinkDone(); break;
    case 'done':
      hideTyping(); waiting = !!ev.waiting; thinkDone(); break;
  }
}

function setBusy(on) {
  busy = on; $('#send').disabled = on || locked; $('#input').disabled = on || locked;
  const hint = $('#composer-hint'); hint.hidden = !((waiting || locked) && !on);
  hint.textContent = locked ? 'This chat has ended. Start a new chat to continue' : 'The coordinator is waiting for your answer';
  $('#input').placeholder = locked ? 'Start a new chat to ask again…' : waiting ? 'Type your answer…' : 'Ask the coordinator to find a place…';
  if (!on && !locked) $('#input').focus();
}

// Events are saved at once but shown through a queue, one step at a time.
function onEvent(ev) { record({ ev }); queue.push(ev); if (!draining) draining = drain(); }
async function drain() {
  live = true;
  while (queue.length) {
    const ev = queue.shift();
    if (ev.type !== 'done' && ev.type !== 'error' && ev.type !== 'interrupt' && ev.type !== 'message') showTyping();
    await sleep(REDUCED ? 0 : (ev.type === 'interrupt' || ev.type === 'message') ? 700 : 350);  // a beat of "thinking" before each step
    hideTyping(); await handle(ev);
    await sleep(REDUCED ? 0 : PAUSE[ev.type] || 0);
  }
  live = false; draining = null;
}

async function send(text) {
  if (busy || locked || !text.trim()) return;
  record({ u: text }, text); renderHistory();
  addUser(text); $('#input').value = ''; autosize(); setBusy(true); showTyping();
  if (waiting) think('You answered, so I\'m handing it back to the scout agent', short(text, 100));
  else { thinkReset(); think('Reading your message', short(text, 120)); }
  try {
    const resp = await api('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ thread_id: threadId, message: text }) });
    if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw Error(e.error || 'The server rejected the message.'); }
    const reader = resp.body.getReader(), dec = new TextDecoder(); let buf = '';
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); const parts = buf.split('\n\n'); buf = parts.pop();
      parts.forEach(p => { const line = p.split('\n').find(l => l.startsWith('data: ')); if (line) { try { onEvent(JSON.parse(line.slice(6))); } catch (e) { /* ignore a malformed event */ } } });
    }
  } catch (e) { onEvent({ type: 'error', message: e.message || 'Could not reach the server.' }); onEvent({ type: 'done', waiting: false }); }
  while (draining) await draining;  // let the last steps finish appearing before the box unlocks
  save(); renderHistory(); setBusy(false);
}

function autosize() { const t = $('#input'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 140) + 'px'; }
function clearScreen() {
  waiting = false; busy = false; locked = false; queue.length = 0; hideTyping();
  $('#thread').replaceChildren();
  $('.chatcol').classList.remove('started'); $('.tracecol').classList.remove('thinking'); thinkReset();
}
function reset() {
  clearScreen(); threadId = newId(); current = null; syncSession(); setBusy(false); renderHistory(); window.scrollTo({ top: 0 });
}

EXAMPLES.forEach(t => { const c = el('button', t, 'chip'); c.type = 'button'; c.addEventListener('click', () => send(t)); $('#chips').append(c); });
$('#composer').addEventListener('submit', e => { e.preventDefault(); send($('#input').value); });
$('#input').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send($('#input').value); } });
$('#input').addEventListener('input', autosize);
$('#new-chat').addEventListener('click', reset);
$('#copy-session').addEventListener('click', copySession);
$('#open-history').addEventListener('click', () => setDrawer(true));
$('#close-history').addEventListener('click', () => setDrawer(false));
$('#scrim').addEventListener('click', () => setDrawer(false));
$('#drawer-new').addEventListener('click', () => { reset(); setDrawer(false); });
$('#hist-search').addEventListener('input', renderHistory);
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || e.target.classList.contains('rename')) return;
  if (!$('#profile-menu').hidden) setProfileMenu(false); else if ($('#drawer').classList.contains('open')) setDrawer(false);
});

// ---- the signed-in person: profile in the history drawer, and what happens when they sign in or out
const METHODS = { password: 'Email and password', google: 'Google' };
function renderProfile(person) {
  const initial = (person.name || person.email || '?').trim().charAt(0);
  $('#avatar').textContent = initial; $('#pname').textContent = person.name; $('#pemail').textContent = person.email;
  $('#pm-name').textContent = person.name; $('#pm-email').textContent = person.email;
  $('#pm-method').textContent = (person.methods || []).map(m => METHODS[m] || m).join(' + ') || '-';
  $('#pm-since').textContent = new Date((person.created || 0) * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}
function setProfileMenu(open) { $('#profile-menu').hidden = !open; $('#profile-btn').setAttribute('aria-expanded', String(open)); }
function signedIn(person) { // a fresh screen for this account, so nothing from a previous account can show
  renderProfile(person); setProfileMenu(false); setDrawer(false); chats = []; current = null; threadId = newId();
  clearScreen(); syncSession(); setBusy(false); loadChats(); window.scrollTo({ top: 0 });
}
function signedOut() { chats = []; current = null; threadId = newId(); clearScreen(); syncSession(); setDrawer(false); setProfileMenu(false); $('#hist').replaceChildren(); }
$('#profile-btn').addEventListener('click', () => setProfileMenu($('#profile-menu').hidden));
$('#logout').addEventListener('click', () => Auth.logout());
document.addEventListener('click', e => { if (!$('#profile-menu').hidden && !$('#profile').contains(e.target)) setProfileMenu(false); });
setBusy(false); Auth.init({ onSignIn: signedIn, onSignOut: signedOut, onProfile: renderProfile });
})();
