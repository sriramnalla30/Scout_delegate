// Netlify build step: tells the page where the API is by writing static/config.js.
//   SCOUT_API_BASE   the API's address, for example https://scout-place-finder.onrender.com (set it in Netlify: Site configuration, Environment variables)
//   CONFIG_OUT       where to write the file (default static/config.js); used by the tests
// A production build without an address fails, so a site is never published pointing at nothing.
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const base = (process.env.SCOUT_API_BASE || '').trim().replace(/\/+$/, '');
if (process.env.CONTEXT === 'production' && !/^https:\/\/[^\s/]+$/.test(base)) {
  console.error(`SCOUT_API_BASE must be an https address such as https://scout-place-finder.onrender.com (got "${base}")`);
  process.exit(1);
}
const out = process.env.CONFIG_OUT || fileURLToPath(new URL('../static/config.js', import.meta.url));
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, `// Written by scripts/write-config.mjs at build time.\nwindow.SCOUT_API_BASE = ${JSON.stringify(base)};\n`);
console.log(`config.js: API address is ${base || '(the same site)'}`);
