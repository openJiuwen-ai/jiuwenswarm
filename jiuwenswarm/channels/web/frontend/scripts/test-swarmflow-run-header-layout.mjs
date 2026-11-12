// Layout regression check for the Swarmflow run header.
//
// Renders the real header markup together with the real SwarmflowTreeView.css at
// every container width and every combination that occurs in practice, then asserts
// that nothing overlaps and nothing is painted outside the card.
//
//   ended    — no control buttons (run finished, the common case)
//   running  — with pause / stop buttons
//   nobudget — budget fields absent
//
// Why this exists: the header is a 4-column grid that collapses to 2 rows at 52rem.
// A plain unit test cannot see layout, and eyeballing screenshots missed two real
// defects during development (metrics cluster squeezed inside its cell at 736-800px;
// budget pills overflowing the card below 272px). This script catches both.
//
// Requires a local Chrome/Edge. Override with CHROME_PATH.
// Run: node scripts/test-swarmflow-run-header-layout.mjs
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(resolve(here, '../src/components/teamArea/SwarmflowTreeView.css'), 'utf8');
const tsx = readFileSync(resolve(here, '../src/components/teamArea/SwarmflowTreeView.tsx'), 'utf8');

// Contract check: this script renders a hand-written replica of the header markup,
// so it can only prove the CSS correct if the replica and the component agree on the
// class names that wire the layout together. Assert them against the real source.
// (This guard is not hypothetical: the first version passed while the component was
// missing `team-area-swarmflow-run` on the card root, so `container-type` had nothing
// to attach to and every size rule silently did nothing.)
const CONTRACT = [
  // card root carries the query container
  [/className="team-area-swarmflow-run[ "]/],
  // header grid + its four slots
  [/team-area-swarmflow-run-header/],
  [/swarmflow-run-header__chev/],
  [/swarmflow-run-header__status/],
  [/swarmflow-run-nametrack/],
  [/swarmflow-run-metrics/],
  [/swarmflow-run-budgets/],
  // progressive-disclosure hooks
  [/swarmflow-run-executor-name/],
  [/swarmflow-run-progress-track/],
  [/team-area-swarmflow-run-status-text/],
  [/swarmflow-run-unbounded/],
];
const missing = CONTRACT.filter(([re]) => !re.test(tsx)).map(([re]) => re.source);
if (missing.length > 0) {
  console.error('FAIL: SwarmflowTreeView.tsx no longer provides these layout hooks:');
  for (const m of missing) console.error('  ' + m);
  console.error('The CSS in SwarmflowTreeView.css depends on them; update both together.');
  process.exit(1);
}

function findBrowser() {
  const candidates = [
    process.env.CHROME_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  const found = candidates.find((p) => existsSync(p));
  if (!found) {
    console.error('No Chrome/Edge found. Set CHROME_PATH to a Chromium-based browser.');
    process.exit(2);
  }
  return found;
}

const base = `
  * { box-sizing: border-box; }
  body { margin:0; font:400 14px/1.6 "Segoe UI",system-ui,sans-serif; background:#1e1e1c; }
  .card { border:1px solid #444; border-radius:8px; overflow:hidden; background:rgba(0,0,0,.2); margin:4px 0; }
  .hdr { padding:10px 12px; background:rgba(255,255,255,.04); }
  .chev { width:16px; height:16px; flex:none; }
  .stat { width:16px; height:16px; flex:none; border-radius:50%; border:2px solid #639922; }
  .nametrack { overflow:hidden; }
  .namerow { display:flex; flex-wrap:wrap; align-items:center; column-gap:8px; row-gap:4px; }
  .name { min-width:5rem; flex:1 1 0%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
          font-weight:600; color:#f1efe8; }
  .status { font-size:12px; color:#b4b2a9; flex:none; white-space:nowrap; }
  .exec { display:flex; flex:none; align-items:center; gap:4px; border-radius:999px;
          background:rgba(127,119,221,.15); padding:2px 6px; font-size:10px; color:#b4b2a9; }
  .exec .dot { width:14px; height:14px; border-radius:50%; background:#5f5e5a; flex:none; }
  .execname { max-width:6rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .metrics { display:flex; min-width:0; align-items:center; column-gap:8px; row-gap:4px; }
  .prog { display:flex; align-items:center; gap:8px; flex:none; }
  .progtrack { width:64px; height:6px; border-radius:999px; background:#5f5e5a; overflow:hidden; flex:none; }
  .progfill { height:100%; width:100%; background:#378add; border-radius:999px; }
  .progtext { font-size:12px; color:#b4b2a9; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .swarmflow-run-budgets { display:flex; align-items:center; gap:8px; }
  .pill { font-size:12px; flex:none; padding:2px 6px; border-radius:999px; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .pill-session { background:rgba(12,68,124,.55); color:#b5d4f4; }
  .pill-run { background:rgba(23,52,4,.55); color:#c0dd97; }
  .ctl { display:flex; align-items:center; gap:4px; flex:none; }
  .btn { width:28px; height:28px; border-radius:4px; background:transparent; border:0; color:#b4b2a9; }
`;

const variants = {
  ended: { budget: true, controls: false },
  running: { budget: true, controls: true },
  nobudget: { budget: false, controls: false },
};

function markup({ budget, controls }) {
  return `
<div class="hdr team-area-swarmflow-run-header">
  <div class="chev swarmflow-run-header__chev"></div>
  <div class="stat swarmflow-run-header__status"></div>
  <div class="nametrack swarmflow-run-nametrack">
    <div class="namerow">
      <span class="name">用 Python 编写冒泡排序脚本</span>
      <span class="status team-area-swarmflow-run-status-text">已完成</span>
      <span class="exec"><span class="dot"></span><span class="execname swarmflow-run-executor-name">python-dev</span></span>
    </div>
  </div>
  <div class="metrics swarmflow-run-metrics">
    <div class="prog"><div class="progtrack swarmflow-run-progress-track"><div class="progfill"></div></div><span class="progtext">3/3 (100%)</span></div>
    ${budget ? `<div class="swarmflow-run-budgets">
      <span class="pill pill-session">会话累计 1.1M<span class="swarmflow-run-unbounded"> · 无限制</span></span>
      <span class="pill pill-run">本次运行 255K<span class="swarmflow-run-unbounded"> · 无限制</span></span>
    </div>` : ''}
    ${controls ? `<div class="ctl"><button class="btn">P</button><button class="btn">S</button></div>` : ''}
  </div>
</div>`;
}

const widths = [
  1600, 1200, 1000, 960, 920, 880, 860, 840, 820, 800, 780, 760, 720, 680,
  640, 600, 560, 520, 480, 440, 400, 380, 360, 340, 320, 300, 280, 260, 250, 240, 220, 200,
];

const cases = [];
for (const v of Object.keys(variants)) {
  for (const w of widths) {
    cases.push(
      `<div class="case" data-w="${w}" data-v="${v}"><div class="card team-area-swarmflow-run" style="width:${w}px">${markup(variants[v])}</div></div>`
    );
  }
}

const measure = `
(() => {
  const rect = (el) => { const b = el.getBoundingClientRect(); return { l: b.left, r: b.right, t: b.top, b: b.bottom }; };
  const vis = (el) => {
    if (!el) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const b = el.getBoundingClientRect();
    return b.width > 0 && b.height > 0;
  };
  const rows = [];
  for (const c of document.querySelectorAll('.case')) {
    const hdr = c.querySelector('.hdr');
    const hb = hdr.getBoundingClientRect();
    const name = c.querySelector('.name');
    const prog = c.querySelector('.prog');
    const buds = c.querySelector('.swarmflow-run-budgets');
    const ctl = c.querySelector('.ctl');

    const parts = [name, c.querySelector('.status'), c.querySelector('.exec'), prog, buds, ctl];
    const painted = parts.filter(vis).map(rect);

    let overlaps = 0;
    for (let i = 0; i < painted.length; i++) {
      for (let j = i + 1; j < painted.length; j++) {
        const a = painted[i], b = painted[j];
        const vo = Math.min(a.b, b.b) - Math.max(a.t, b.t);
        const ho = Math.min(a.r, b.r) - Math.max(a.l, b.l);
        if (vo > 2 && ho > 2) overlaps++;
      }
    }

    const contentRight = hb.right - 12;
    const overflowing = parts
      .filter(vis)
      .filter((el) => el.getBoundingClientRect().right > contentRight + 0.6)
      .map((el) => el.className.split(' ')[0] + '+' + (el.getBoundingClientRect().right - contentRight).toFixed(1) + 'px');

    rows.push({
      w: +c.dataset.w,
      v: c.dataset.v,
      h: +hb.height.toFixed(1),
      ovl: overlaps,
      of: overflowing.length,
      over: overflowing.join(' '),
      trunc: name.scrollWidth > name.clientWidth + 1,
    });
  }
  const el = document.createElement('pre');
  el.id = 'RESULT';
  el.textContent = 'RESULT_JSON:' + JSON.stringify(rows);
  document.body.appendChild(el);
})();
`;

const work = mkdtempSync(join(tmpdir(), 'swarmflow-header-'));
const htmlPath = resolve(work, 'probe.html');
writeFileSync(
  htmlPath,
  `<!doctype html><html><head><meta charset="utf-8"><style>${base}\n${css}</style></head><body>${cases.join('\n')}<script>${measure}</script></body></html>`,
  'utf8'
);

const browser = findBrowser();

let dom;
try {
  dom = execFileSync(
    browser,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-sandbox',
      '--hide-scrollbars',
      '--virtual-time-budget=4000',
      '--dump-dom',
      pathToFileURL(htmlPath).href,
    ],
    { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] }
  );
} finally {
  rmSync(work, { recursive: true, force: true });
}

const match = dom.match(/RESULT_JSON:(\[[\s\S]*?\])<\/pre>/);
if (!match) {
  console.error('FAIL: the page produced no measurement payload');
  process.exit(1);
}

const rows = JSON.parse(match[1]);
const pad = (v, n) => String(v).padEnd(n);

for (const v of Object.keys(variants)) {
  const sub = rows.filter((r) => r.v === v);
  console.log(`\n${v}`);
  console.log(pad('W', 6) + pad('h', 7) + pad('overlap', 9) + pad('outside', 9) + 'name');
  for (const r of sub) {
    console.log(
      pad(r.w, 6) + pad(r.h, 7) + pad(r.ovl, 9) + pad(r.of, 9) + (r.trunc ? 'ellipsis' : 'full')
    );
  }
}

const bad = rows.filter((r) => r.ovl > 0 || r.of > 0);
if (bad.length > 0) {
  console.error('\nFAIL: overlapping or overflowing header at');
  for (const r of bad) console.error(`  ${r.v} @ ${r.w}px  overlap=${r.ovl} outside=[${r.over}]`);
  process.exit(1);
}

console.log(`\nPASS: ${rows.length} cases (${Object.keys(variants).length} variants x ${widths.length} widths), no overlap, nothing outside the card`);
