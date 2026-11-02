import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';

const variant = process.argv[process.argv.indexOf('--variant') + 1];
if (!['baseline', 'candidate', 'contrast'].includes(variant)) throw new Error('Expected --variant baseline|candidate|contrast');
const browser = process.env.BEHAVIOR_BROWSER ?? [
  '/Applications/Helium.app/Contents/MacOS/Helium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].find(path => Bun.file(path).size > 0) ?? Bun.which('chromium') ?? Bun.which('google-chrome');
if (!browser) {
  console.error('A local Chromium browser is required. No browser is installed automatically.');
  process.exit(127);
}
const temporary = await mkdtemp(join(tmpdir(), 'focus-behavior-'));
try {
  const url = new URL('./fixture.html', import.meta.url);
  url.searchParams.set('variant', variant);
  const args = [browser, '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    '--disable-sync', '--disable-background-networking', '--disable-component-update',
    `--user-data-dir=${join(temporary, 'profile')}`, '--dump-dom', '--virtual-time-budget=1000'];
  if (process.env.BEHAVIOR_SCREENSHOT) args.push(`--screenshot=${process.env.BEHAVIOR_SCREENSHOT}`, '--window-size=800,600');
  args.push(url.href);
  const child = spawn(args[0], args.slice(1), {stdio: ['ignore', 'pipe', 'pipe'], detached: true});
  const terminate = () => { if (!Number.isInteger(child.pid)) return; try { process.kill(-child.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; } };
  const timer = setTimeout(terminate, 30000);
  async function collect(stream) {
    let text = '';
    for await (const chunk of stream) {
      text += chunk.toString();
      if (text.length > 1024 * 1024) { terminate(); throw new Error('Browser output exceeded fixture limit'); }
    }
    return text;
  }
  let html, code;
  try {
    [html, , code] = await Promise.all([collect(child.stdout), collect(child.stderr),
      new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', resolve); })]);
  } finally { clearTimeout(timer); terminate(); }
  if (code !== 0) throw new Error(`Browser did not complete successfully (exit ${code}); no behavior result established.`);
  const encoded = html.match(/<pre id="results">([^<]+)<\/pre>/)?.[1];
  if (!encoded) throw new Error('Browser did not return completed DOM checks');
  const result = JSON.parse(encoded.replaceAll('&quot;', '"').replaceAll('&amp;', '&').replaceAll('&lt;', '<').replaceAll('&gt;', '>'));
  if (result.variant !== variant || !Array.isArray(result.checks) || result.checks.length !== 3 ||
      result.checks.some(c => typeof c.passed !== 'boolean') || result.passed !== result.checks.every(c => c.passed)) throw new Error('Invalid browser result');
  console.log(JSON.stringify(result));
  process.exitCode = result.passed ? 0 : 1;
} finally {
  await rm(temporary, {recursive: true, force: true});
}
