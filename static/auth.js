// Sign-in: the screen people see first, the session token, and the one fetch every API call goes through.
// The server hands back a session token after a sign-in; it is kept in this browser and sent as `Authorization: Bearer`.
// Chats are saved under the account on the server, so another account (or another browser's visitor) never sees them.
(() => {
const $ = s => document.querySelector(s);
const API_BASE = (window.SCOUT_API_BASE || '').replace(/\/$/, '');
const KEY = 'scout_token', PROFILE = 'scout_profile';
let token = null, user = null, mode = 'signin', config = { google_client_id: null, min_password: 8 }, handlers = {}, googleReady = false, configLoaded = false, googleFailed = false, googleNote = false;
try { token = localStorage.getItem(KEY); } catch (e) { /* storage blocked: the person signs in again next visit */ }
function keep(value, person) {
  token = value;
  try { if (value) { localStorage.setItem(KEY, value); localStorage.setItem(PROFILE, JSON.stringify(person || null)); } else { localStorage.removeItem(KEY); localStorage.removeItem(PROFILE); } } catch (e) { /* not saved */ }
}
function remembered() { try { return JSON.parse(localStorage.getItem(PROFILE)); } catch (e) { return null; } } // only to draw the screen at once; the server decides who the token belongs to

// Every API call. A 401 means the session ended (or never was): back to the sign-in screen.
async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = 'Bearer ' + token;
  const response = await fetch(API_BASE + path, { ...options, headers });
  if (response.status === 401 && token && !path.startsWith('/api/auth/')) leave('Your session ended. Please sign in again.');
  return response;
}
const post = (path, body) => request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

function show(state) { document.body.classList.remove('booting', 'guest', 'authed'); document.body.classList.add(state); }
function error(message) { const box = $('#auth-error'); box.textContent = message || ''; box.hidden = !message; }
function busy(on) { $('#auth-submit').disabled = on; $('#auth-submit').textContent = on ? 'One moment…' : mode === 'signup' ? 'Create account' : 'Sign in'; }

function setMode(next) {
  mode = next; const up = mode === 'signup';
  $('#auth-title').textContent = up ? 'Create your account' : 'Welcome back';
  $('#auth-sub').textContent = up ? 'Sign up to save your searches. Your chat history stays private to your account.' : 'Sign in to pick up your chats. Your history is private to your account.';
  $('#name-row').hidden = !up;
  $('#auth-pass').autocomplete = up ? 'new-password' : 'current-password';
  $('#auth-submit').textContent = up ? 'Create account' : 'Sign in';
  $('#switch-text').textContent = up ? 'Already have an account?' : 'New to Scout?';
  $('#auth-switch').textContent = up ? 'Sign in' : 'Create an account';
  $('#google-fallback') && ($('#google-fallback span').textContent = up ? 'Sign up with Google' : 'Continue with Google');
  error(''); renderGoogle();
}

// ---- Google. The page gets a signed ID token from Google; the server checks it and answers like any other sign-in.
function loadGoogleScript() {
  return new Promise((resolve, reject) => {
    if (window.google && google.accounts) return resolve();
    const tag = document.createElement('script'); tag.src = 'https://accounts.google.com/gsi/client'; tag.async = true;
    tag.onload = resolve; tag.onerror = () => { tag.remove(); reject(new Error('Could not load Google sign-in.')); }; document.head.append(tag); // removed on failure so a retry can load it again
  });
}
function renderGoogle() {
  const slot = $('#google-slot');
  if (!config.google_client_id) { // not set up on this server: keep the button, say why when it is pressed
    if (!$('#google-fallback')) slot.replaceChildren(fallbackButton()); return;
  }
  loadGoogleScript().then(() => {
    if (!googleReady) { google.accounts.id.initialize({ client_id: config.google_client_id, callback: r => finish(post('/api/auth/google', { credential: r.credential })) }); googleReady = true; }
    slot.replaceChildren(); googleFailed = false; if (googleNote) { googleNote = false; error(''); }
    google.accounts.id.renderButton(slot, { type: 'standard', theme: 'outline', size: 'large', shape: 'pill', logo_alignment: 'left', width: 320, text: mode === 'signup' ? 'signup_with' : 'continue_with' });
  }).catch(e => { googleFailed = true; slot.replaceChildren(fallbackButton()); note(e.message); });
}
function fallbackButton() {
  const b = document.createElement('button'); b.type = 'button'; b.id = 'google-fallback'; b.className = 'gbtn';
  b.innerHTML = '<svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.5 5.4 2.6 13.2l7.9 6.1C12.4 13.6 17.7 9.5 24 9.5z"/><path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.7c-.6 3-2.3 5.5-4.8 7.2l7.6 5.9c4.4-4.1 7-10.1 7-17.6z"/><path fill="#FBBC05" d="M10.5 28.7a14.5 14.5 0 0 1 0-9.4l-7.9-6.1a24 24 0 0 0 0 21.6l7.9-6.1z"/><path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.6-5.9c-2.1 1.4-4.9 2.3-8.3 2.3-6.3 0-11.6-4.2-13.5-9.9l-7.9 6.1C6.5 42.6 14.6 48 24 48z"/></svg><span></span>';
  b.querySelector('span').textContent = mode === 'signup' ? 'Sign up with Google' : 'Continue with Google';
  // Say the truth about why Google is not there. "Not set up" is only true when the server answered and has no client id.
  b.addEventListener('click', () => {
    if (!configLoaded) { note('Still connecting to the server (it may be waking up). Trying again, tap Google in a few seconds.'); loadConfig(); return; }
    if (googleFailed) { note('Could not load Google sign-in. Check your connection or content blocker. Trying again…'); renderGoogle(); return; }
    note('Google sign-in is not set up on this server yet. Use your email and password.');
  });
  return b;
}
function note(text) { googleNote = !!text; error(text); } // a message about Google, cleared once Google's button is up

// The settings (Google client id) come from the API. A host that sleeps answers late or fails the first time: ask again a few times.
function loadConfig(tries = 0) {
  return fetch(API_BASE + '/api/auth/config', { cache: 'no-store' }).then(r => r.json()).then(c => {
    config = { ...config, ...c }; configLoaded = true;
    if (document.body.classList.contains('guest')) renderGoogle();
  }).catch(() => { if (tries < 6) setTimeout(() => loadConfig(tries + 1), 3000 * (tries + 1)); });
}

// ---- the two forms
async function finish(pending) {
  busy(true); error('');
  // a host that sleeps when idle needs up to a minute to wake: say so instead of looking stuck
  const slow = setTimeout(() => { $('#auth-submit').textContent = 'Waking the server, this can take a minute…'; }, 4000);
  try {
    const response = await pending, body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || 'Something went wrong. Please try again.');
    keep(body.token, body.user); enter(body.user);
  } catch (e) { error(e.message === 'Failed to fetch' ? 'Could not reach the server. Check your connection and try again.' : e.message); }
  clearTimeout(slow); busy(false);
}
function submit(e) {
  e.preventDefault();
  const email = $('#auth-email').value.trim(), password = $('#auth-pass').value;
  if (!email || !password) return error('Enter your email and password.');
  if (mode === 'signup' && password.length < config.min_password) return error(`Choose a password of at least ${config.min_password} characters.`);
  finish(post(mode === 'signup' ? '/api/auth/signup' : '/api/auth/login', { email, password, name: $('#auth-name').value.trim() }));
}

function enter(person) {
  user = person; $('#auth-pass').value = ''; error(''); show('authed');
  if (handlers.onSignIn) handlers.onSignIn(person);
}
function leave(message) { // signed out: here, or because the session ended
  keep(null); user = null; show('guest'); setMode('signin'); error(message || '');
  if (handlers.onSignOut) handlers.onSignOut();
}
async function logout() {
  try { await post('/api/auth/logout', {}); } catch (e) { /* the token is dropped here either way */ }
  leave('');
}

function init(h) {
  handlers = h || {};
  $('#auth-form').addEventListener('submit', submit);
  $('#auth-switch').addEventListener('click', () => setMode(mode === 'signup' ? 'signin' : 'signup'));
  $('#pw-toggle').addEventListener('click', () => { const p = $('#auth-pass'), hide = p.type === 'text'; p.type = hide ? 'password' : 'text'; $('#pw-toggle').textContent = hide ? 'Show' : 'Hide'; });
  // Nothing waits for the server: a hosted API may be asleep for a minute. The screen is drawn at once; Google's button appears when the
  // settings arrive, and a saved sign-in opens the app straight away (the first call that gets a 401 sends the person back here).
  loadConfig(); // if it never arrives the form still works
  const person = token && remembered();
  if (person) { enter(person); request('/api/auth/me').then(r => r.ok ? r.json() : null).then(b => { if (b && b.user) { user = b.user; keep(token, b.user); if (handlers.onProfile) handlers.onProfile(b.user); } }).catch(() => { /* offline: the saved profile stays */ }); return; }
  if (token) keep(null); // a token without a profile (an older page): ask for a fresh sign-in
  show('guest'); setMode('signin');
}

window.Auth = { init, fetch: request, logout, user: () => user };
})();
