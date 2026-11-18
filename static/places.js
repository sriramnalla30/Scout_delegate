// The results of one search: a summary, sort and filter controls, and one card per place with everything Google returned.
// It reads one stored search by id (GET /api/searches/<id>), so a saved chat shows the same results again, and any other
// view (a map, charts) can read the same document. Nothing here talks to a model.
(() => {
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = text; if (cls) e.className = cls; return e; };
const add = (parent, ...kids) => { kids.forEach(k => k && parent.append(k)); return parent; };
const num = n => Number(n).toLocaleString('en-IN');
const dist = m => m == null ? '' : m < 1000 ? m + ' m' : (m / 1000).toFixed(m < 10000 ? 1 : 0) + ' km';
const human = key => key.replace(/_/g, ' ');
function ago(sec) { const m = Math.round(sec / 60); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : Math.round(m / 1440) + ' d ago'; }

const SORTS = {
  best: ['Best overall', (a, b) => (b.score || 0) - (a.score || 0) || b.reviews - a.reviews],
  rating: ['Top rated', (a, b) => (b.rating || 0) - (a.rating || 0) || b.reviews - a.reviews],
  reviews: ['Most reviews', (a, b) => b.reviews - a.reviews],
  near: ['Closest to the pin', (a, b) => (a.distance_m ?? 1e12) - (b.distance_m ?? 1e12)],
  google: ['Google\'s order', (a, b) => a.rank - b.rank],
};
const FILTERS = { open: ['Open now', p => p.live && p.live.open === true], phone: ['Has a phone', p => !!p.phone], web: ['Has a website', p => !!p.website] };
const PICKS = { best: 'Best overall', most_reviewed: 'Most reviewed', closest: 'Closest' };
const FLAG_TEXT = {
  few_reviews: p => `Only ${p.reviews} review${p.reviews === 1 ? '' : 's'}, so the rating is not reliable yet`,
  shared_phone: p => `Same phone as ${p.phone.shared - 1} other listing${p.phone.shared === 2 ? '' : 's'}: likely one owner or chain`,
  no_hours: () => 'Opening hours not listed',
};

function stars(rating) {
  const s = el('span', '★★★★★', 'pl-stars'); s.style.setProperty('--fill', (rating / 5 * 100).toFixed(0) + '%');
  s.setAttribute('role', 'img'); s.setAttribute('aria-label', rating + ' out of 5'); return s;
}
function link(href, text, cls) {
  const a = el('a', text, cls); if (/^https?:\/\//i.test(href)) { a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; } return a;
}
function today(tz) { try { return new Intl.DateTimeFormat('en-US', { weekday: 'long', timeZone: tz || 'Asia/Kolkata' }).format(new Date()); } catch (e) { return ''; } }

// every field Google returned that has a value, whatever the field is: new scraper fields show up without a code change
function value(v) {
  if (Array.isArray(v)) return v.map(value).filter(Boolean).join(', ');
  if (v && typeof v === 'object') return Object.entries(v).map(([k, x]) => value(x) && `${human(k)}: ${value(x)}`).filter(Boolean).join(' · ');
  return v === null || v === undefined || v === '' || v === 0 ? '' : String(v);
}
function allFields(raw) {
  const box = el('dl', undefined, 'pl-fields');
  Object.entries(raw).forEach(([k, v]) => {
    const text = value(v); if (!text) return;
    const dd = /^https?:\/\//i.test(text) ? link(text, text) : el('span', text);
    add(box, el('dt', human(k)), add(el('dd'), dd));
  });
  return box;
}

function placeCard(p, tags, position) {
  const c = el('article', undefined, 'pl-card'); c.dataset.id = p.id;
  const live = p.live || {}, head = el('div', undefined, 'pl-head');
  const title = el('div', undefined, 'pl-title'); add(title, el('h4', p.name), tags.length && add(el('div', undefined, 'pl-tags'), ...tags.map(t => el('span', t, 'pl-tag'))));
  add(head, el('span', String(position), 'pl-rank'), title, el('span', live.text || 'Hours not listed', 'pl-live ' + (live.open === true ? 'open' : live.open === false ? 'closed' : 'unknown')));
  add(c, head);
  if (p.categories.length) add(c, add(el('div', undefined, 'pl-cats'), ...p.categories.map(t => el('span', t, 'pl-cat'))));

  const rate = el('div', undefined, 'pl-rate');
  if (p.rating) add(rate, stars(p.rating), el('b', p.rating.toFixed(1)), el('span', `(${num(p.reviews)} reviews)`), p.score ? el('span', 'weighted ' + p.score.toFixed(2), 'pl-score') : null);
  else add(rate, el('span', 'No rating yet', 'pl-muted'));
  add(c, rate);
  if (p.business_status) add(c, el('div', p.business_status, 'pl-warn'));
  add(c, el('p', p.address || 'No address', 'pl-addr'));

  const facts = el('div', undefined, 'pl-facts');
  if (p.distance_m != null) add(facts, el('span', dist(p.distance_m) + ' from the pin'));
  if (p.phone) add(facts, el('span', `${p.phone.display} · ${p.phone.kind}`));
  if (p.price_range) add(facts, el('span', p.price_range));
  add(c, facts);
  p.flags.filter(f => FLAG_TEXT[f]).forEach(f => add(c, el('div', FLAG_TEXT[f](p), 'pl-note')));
  if (p.description) add(c, el('p', p.description, 'pl-desc'));

  const acts = el('div', undefined, 'pl-acts');
  if (p.phone) add(acts, link('tel:' + p.phone.e164, 'Call', 'pl-btn primary'));
  add(acts, link(p.directions_url, 'Directions', 'pl-btn'), link(p.maps_url, 'Google Maps', 'pl-btn'), p.website ? link(p.website, 'Website', 'pl-btn') : null);
  c.append(acts);

  const more = el('details', undefined, 'pl-more'); more.append(el('summary', 'Hours and all details'));
  const grid = el('div', undefined, 'pl-two');
  const hours = el('table', undefined, 'pl-hours'), now = today(p.timezone);
  Object.entries(p.hours).forEach(([day, text]) => { const tr = el('tr', undefined, day === now ? 'now' : ''); add(tr, el('th', day), el('td', text || 'Closed')); hours.append(tr); });
  const ids = el('dl', undefined, 'pl-fields');
  [['plus code', p.plus_code], ['coordinates', p.lat != null ? `${p.lat}, ${p.lng}` : ''], ['time zone', p.timezone], ['phone (international)', p.phone && p.phone.e164], ['google order', '#' + p.rank], ['google id', p.id]]
    .filter(([, v]) => v).forEach(([k, v]) => add(ids, el('dt', k), el('dd', v)));
  add(grid, Object.keys(p.hours).length ? hours : el('p', 'Opening hours are not listed.', 'pl-muted'), ids);
  add(more, grid, el('div', 'Everything Google returned', 'pl-sub'), allFields(p.raw));
  c.append(more);
  return c;
}

function csv(doc) {
  const cols = ['rank', 'name', 'categories', 'rating', 'reviews', 'weighted_score', 'distance_m', 'address', 'phone', 'phone_kind', 'website', 'lat', 'lng', 'plus_code', 'maps_url'];
  const cell = v => { const s = v == null ? '' : String(v); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const rows = doc.places.map(p => [p.rank, p.name, p.categories.join('; '), p.rating, p.reviews, p.score, p.distance_m, p.address, p.phone && p.phone.e164, p.phone && p.phone.kind, p.website, p.lat, p.lng, p.plus_code, p.maps_url]);
  return '﻿' + [cols, ...rows].map(r => r.map(cell).join(',')).join('\r\n');
}
function download(name, mime, text) {
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text], { type: mime })); a.download = name; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function coverage(doc) {
  const total = doc.places.length, entries = Object.entries(doc.coverage || {});
  const box = el('details', undefined, 'pl-cov'); box.append(el('summary', 'What Google returned for these places'));
  const grid = el('div', undefined, 'pl-covgrid');
  entries.filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]).forEach(([k, n]) => {
    const bar = el('i'); bar.style.width = (n / total * 100) + '%';
    add(grid, el('span', human(k)), add(el('span', undefined, 'pl-bar'), bar), el('b', `${n}/${total}`));
  });
  box.append(grid);
  const empty = entries.filter(([, n]) => n === 0).map(([k]) => human(k));
  if (empty.length) box.append(el('p', 'Empty for every place in this search: ' + empty.join(', ') + '.', 'pl-muted'));
  return box;
}

function build(host, doc) {
  const places = doc.places, s = doc.summary, a = doc.args, byId = Object.fromEntries(places.map(p => [p.id, p]));
  const state = { sort: 'best', filters: new Set(), category: null };
  const root = el('section', undefined, 'places'); root.setAttribute('aria-label', 'Search results');

  const radius = s.max_distance_m != null ? ` · spread over ${dist(s.max_distance_m)}` : '';
  add(root, add(el('div', undefined, 'pl-top'),
    el('div', 'GOOGLE MAPS · ' + places.length + ' PLACES', 'tag'),
    el('h3', a.query),
    el('p', `Around ${a.lat}, ${a.lng}${radius} · fetched ${ago(doc.viewed_at - doc.created_at)} · took ${(doc.elapsed_ms / 1000).toFixed(1)} s`, 'pl-sub')));

  const kpi = (label, v, hint) => add(el('div', undefined, 'pl-kpi'), el('b', v), el('span', label), hint ? el('small', hint) : null);
  add(root, add(el('div', undefined, 'pl-kpis'),
    kpi('average rating', s.avg_rating ? s.avg_rating.toFixed(2) + ' ★' : '-', 'from ' + places.filter(p => p.rating).length + ' rated places'),
    kpi('reviews in total', num(s.total_reviews)),
    kpi('open right now', String(s.open_now), s.with_hours + ' list their hours'),
    kpi('with a phone', `${s.with_phone}/${places.length}`, Object.entries(s.phone_kinds).map(([k, n]) => n + ' ' + k).join(' · ')),
    kpi('with a website', `${s.with_website}/${places.length}`)));

  const picks = el('div', undefined, 'pl-picks');
  Object.entries(PICKS).forEach(([key, label]) => {
    const p = byId[s.picks[key]]; if (!p) return;
    const b = el('button', undefined, 'pl-pick'); b.type = 'button';
    add(b, el('small', label), el('b', p.name), el('span', key === 'closest' ? dist(p.distance_m) : p.rating ? `${p.rating.toFixed(1)} ★ · ${num(p.reviews)} reviews` : 'no rating'));
    b.addEventListener('click', () => { state.filters.clear(); state.category = null; paint(); const card = list.querySelector(`[data-id="${CSS.escape(p.id)}"]`); if (card) { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); card.classList.add('flash'); setTimeout(() => card.classList.remove('flash'), 1600); } });
    picks.append(b);
  });
  if (picks.children.length) root.append(picks);

  const bar = el('div', undefined, 'pl-controls'), sel = el('select'); sel.setAttribute('aria-label', 'Sort places');
  Object.entries(SORTS).forEach(([k, [label]]) => { const o = el('option', 'Sort: ' + label); o.value = k; sel.append(o); });
  sel.addEventListener('change', () => { state.sort = sel.value; paint(); });
  bar.append(sel);
  const toggles = [];
  Object.entries(FILTERS).forEach(([k, [label]]) => {
    const b = el('button', label, 'pl-chip'); b.type = 'button'; b.setAttribute('aria-pressed', 'false');
    b.addEventListener('click', () => { state.filters.has(k) ? state.filters.delete(k) : state.filters.add(k); paint(); });
    toggles.push([k, b]); bar.append(b);
  });
  const cats = [];
  s.categories.slice(0, 6).forEach(({ name, count }) => {
    const b = el('button', `${name} · ${count}`, 'pl-chip cat'); b.type = 'button'; b.setAttribute('aria-pressed', 'false');
    b.addEventListener('click', () => { state.category = state.category === name ? null : name; paint(); });
    cats.push([name, b]); bar.append(b);
  });
  const shown = el('span', undefined, 'pl-shown'); bar.append(shown);
  root.append(bar);

  const list = el('div', undefined, 'pl-list'); root.append(list);
  const exp = el('div', undefined, 'pl-export');
  const csvBtn = el('button', 'Download CSV', 'pl-chip'), jsonBtn = el('button', 'Download JSON', 'pl-chip'); csvBtn.type = jsonBtn.type = 'button';
  const stem = 'places-' + (a.query || 'search').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  csvBtn.addEventListener('click', () => download(stem + '.csv', 'text/csv;charset=utf-8', csv(doc)));
  jsonBtn.addEventListener('click', () => download(stem + '.json', 'application/json', JSON.stringify(doc, null, 1)));
  add(exp, csvBtn, jsonBtn); add(root, exp, coverage(doc));

  const tagsFor = Object.fromEntries(places.map(p => [p.id, Object.entries(PICKS).filter(([k]) => s.picks[k] === p.id).map(([, l]) => l)]));
  function paint() {
    toggles.forEach(([k, b]) => b.setAttribute('aria-pressed', String(state.filters.has(k))));
    cats.forEach(([n, b]) => b.setAttribute('aria-pressed', String(state.category === n)));
    const rows = places.filter(p => [...state.filters].every(k => FILTERS[k][1](p)) && (!state.category || p.categories.includes(state.category))).sort(SORTS[state.sort][1]);
    shown.textContent = `Showing ${rows.length} of ${places.length}`;
    list.replaceChildren(...rows.map((p, i) => placeCard(p, tagsFor[p.id], i + 1)));
    if (!rows.length) {
      const none = add(el('div', 'No place matches these filters. ', 'pl-empty')), reset = el('button', 'Clear filters', 'pl-chip'); reset.type = 'button';
      reset.addEventListener('click', () => { state.filters.clear(); state.category = null; paint(); }); list.append(add(none, reset));
    }
  }
  paint();
  host.replaceChildren(root);
}

// Loads one stored search into `host`. Resolves when it is on screen.
async function render(host, searchId) {
  host.replaceChildren(el('div', 'Loading the results…', 'pl-loading'));
  try {
    const r = await fetch(((window.SCOUT_API_BASE || '').replace(/\/$/, '')) + '/api/searches/' + encodeURIComponent(searchId));
    if (!r.ok) throw Error(r.status === 404 ? 'These results are no longer stored on this machine.' : 'The results could not be loaded.');
    build(host, await r.json());
  } catch (e) { host.replaceChildren(el('div', e.message || 'The results could not be loaded.', 'pl-loading warn')); }
}
window.Places = { render };
})();
