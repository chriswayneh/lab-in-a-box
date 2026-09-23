import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const outDir = path.join(root, 'screenshots');
fs.mkdirSync(outDir, { recursive: true });

function envValue(name, fallback = '') {
  try {
    const text = fs.readFileSync(path.join(root, '.env'), 'utf8');
    const line = text.split(/\r?\n/).find((x) => new RegExp(`^${name}=`).test(x));
    return line ? line.slice(name.length + 1).trim().replace(/^['"]|['"]$/g, '') : fallback;
  } catch { return fallback; }
}
function fileValue(rel, fallback = '') {
  try { return fs.readFileSync(path.join(root, rel), 'utf8').trim(); } catch { return fallback; }
}
const adminUser = envValue('KEYCLOAK_ADMIN', 'admin');
const adminPass = envValue('KEYCLOAK_ADMIN_PASSWORD');
const demoPass = envValue('DEMO_USER_PASSWORD', 'LabDemoPass1!');
const grafanaUser = envValue('GRAFANA_ADMIN_USER', 'admin');
const grafanaPass = fileValue('secrets/grafana_admin_password.txt');
const vaultToken = envValue('VAULT_DEV_ROOT_TOKEN');

const browser = await chromium.launch({ headless: true });
const results = [];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function settle(page, ms = 2500) {
  await page.waitForLoadState('domcontentloaded', { timeout: 15000 }).catch(() => {});
  await sleep(ms);
}
async function fillFirst(page, selectors, value) {
  for (const selector of selectors) {
    const loc = page.locator(selector).first();
    if (await loc.count() && await loc.isVisible().catch(() => false)) {
      await loc.fill(value);
      return true;
    }
  }
  return false;
}
async function submitFirst(page) {
  for (const selector of ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("Sign in")', 'button:has-text("Log in")', 'button:has-text("Login")']) {
    const loc = page.locator(selector).first();
    if (await loc.count() && await loc.isVisible().catch(() => false)) {
      await loc.click();
      return true;
    }
  }
  return false;
}
async function keycloakLoginIfShown(page, username, password) {
  const userOk = await fillFirst(page, ['#username', 'input[name="username"]', 'input[autocomplete="username"]'], username);
  if (!userOk) return false;
  await fillFirst(page, ['#password', 'input[name="password"]', 'input[type="password"]'], password);
  await submitFirst(page);
  await settle(page, 3500);
  return true;
}
async function screenshotPage(page, name) {
  const target = path.join(outDir, name);
  await page.screenshot({ path: target, fullPage: false, animations: 'disabled' });
  const size = fs.statSync(target).size;
  results.push({ file: name, size, url: page.url(), ok: true });
  console.log(`OK|${name}|${size}|${page.url()}`);
}
async function capture(name, url, setup) {
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
    viewport: { width: 1920, height: 1080 },
    colorScheme: 'dark',
    deviceScaleFactor: 1,
    locale: 'en-US',
  });
  const page = await context.newPage();
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await settle(page);
    if (setup) await setup(page);
    await screenshotPage(page, name);
  } catch (error) {
    results.push({ file: name, ok: false, url: page.url(), error: String(error).split('\n')[0] });
    console.log(`FAIL|${name}|${page.url()}|${String(error).split('\n')[0]}`);
  } finally {
    await context.close();
  }
}

await capture('landing-page-live.png', 'https://lab.localhost', async (page) => {
  await settle(page, 4500);
});
await capture('keycloak-login-desktop.png', 'https://keycloak.lab.localhost/admin/', async (page) => {
  await settle(page, 2500);
});
await capture('open-webui-live.png', 'https://chat.lab.localhost', async (page) => {
  await settle(page, 5000);
});

await capture('grafana-overview-live.png', 'https://grafana.lab.localhost', async (page) => {
  if (grafanaPass) {
    await fillFirst(page, ['input[name="user"]', 'input[name="username"]', 'input[placeholder*="email" i]'], grafanaUser);
    await fillFirst(page, ['input[name="password"]', 'input[type="password"]'], grafanaPass);
    await submitFirst(page);
    await settle(page, 5000);
  }
  if (/\/login/i.test(page.url())) throw new Error('Grafana login did not succeed');
  // The provisioned home dashboard is Lab Overview and contains live panels.
  await settle(page, 3500);
});
await capture('traefik-dashboard.png', 'https://traefik.lab.localhost/dashboard/', async (page) => {
  // The protected host redirects to Keycloak; the fragment is restored after login.
  if (/404 page not found/i.test(await page.locator('body').innerText().catch(() => ''))) {
    await sleep(12000);
    await page.goto('https://traefik.lab.localhost/dashboard/', { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
    await settle(page, 2500);
  }
  if (/keycloak|oauth/i.test(page.url()) || await page.locator('#username, input[name="username"]').count()) {
    await keycloakLoginIfShown(page, 'alice', demoPass);
  }
  await settle(page, 6500);
  await page.goto('https://traefik.lab.localhost/dashboard/#/http/routers', { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
  await settle(page, 5000);
  const body = await page.locator('body').innerText().catch(() => '');
  if (/404 page not found/i.test(body) || !/HTTP Routers|Routers/i.test(body)) {
    throw new Error('Traefik dashboard did not load routers view');
  }
});
await capture('vault-policies.png', 'https://vault.lab.localhost/ui/vault/policies/acl', async (page) => {
  if (vaultToken) {
    let tokenFilled = await fillFirst(page, ['input[name="token"]', '#token', 'input[placeholder*="token" i]'], vaultToken);
    if (!tokenFilled) {
      const tokenTab = page.getByText('Token', { exact: true }).first();
      if (await tokenTab.count() && await tokenTab.isVisible().catch(() => false)) {
        await tokenTab.click().catch(() => {});
        tokenFilled = await fillFirst(page, ['input[name="token"]', '#token', 'input[placeholder*="token" i]'], vaultToken);
      }
    }
    if (tokenFilled) {
      await submitFirst(page);
      await settle(page, 4500);
    }
    if (!/policies/i.test(page.url())) {
      await page.goto('https://vault.lab.localhost/ui/vault/policies/acl', { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
      await settle(page, 3500);
    }
  }
  const dismiss = page.getByRole('button', { name: 'Dismiss' }).first();
  if (await dismiss.count() && await dismiss.isVisible().catch(() => false)) await dismiss.click().catch(() => {});
  // Do not expose the token; only the policies view is captured.
});

await browser.close();
console.log('SUMMARY');
for (const item of results) console.log(JSON.stringify(item));
