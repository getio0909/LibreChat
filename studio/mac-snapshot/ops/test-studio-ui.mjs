import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { chromium } from '/home/wangtinghu/.npm/_npx/9833c18b2d85bc59/node_modules/playwright/index.mjs';

const root = new URL('./', import.meta.url);
const live = process.argv.includes('--deployed');
const origin = process.env.ZYB_HTTP_URL || 'http://127.0.0.1:18188';
const get = async name => {
  const response = await fetch(origin + '/zyb/studio/' + name);
  assert(response.ok);
  return response.json();
};
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const service = await get('status');
const listing = await get('tasks');
const originals = await Promise.all(listing.tasks.map(row => get('tasks/' + row.id)));
const before = hash(originals);
const sample = originals.find(row => row.is_demo && row.selected && row.concepts.length >= 3 &&
  row.images.filter(image => image.concept_id === row.selected).length >= 2);
assert(sample, 'A saved demonstration design with versions is required');
const evidence = new URL(process.env.ZYB_UI_EVIDENCE || './evidence/ui-20260922/', root);
await fs.mkdir(evidence, { recursive: true, mode: 0o700 });
const browser = await chromium.connectOverCDP(process.env.ZYB_CDP_URL || 'http://127.0.0.1:19222');
const page = await browser.contexts()[0].newPage();
const errors = [], writes = [], checks = [];
let responseMode = 'normal';
const ok = name => checks.push(name);
page.on('pageerror', error => errors.push(error.message));
const hook = '\nglobalThis.__studioQA = { refresh: render, show(value, step, options) { ++navigation; clearTimeout(pollTimer); task = value; stage = step; status = {...status, ...options}; planning = false; render(); } };';
await page.route('**/extensions/zyb_obsidian_design/**', async route => {
  const name = new URL(route.request().url()).pathname.split('/').at(-1);
  if (!['studio.js', 'studio.css'].includes(name)) return route.continue();
  let body = await fs.readFile(new URL('zyb_obsidian_design/web/' + name, root), 'utf8');
  if (live) {
    const deployed = await fetch(origin + '/extensions/zyb_obsidian_design/' + name).then(r => r.text());
    assert.equal(deployed, body, 'Deployed frontend must match validated source');
    body = deployed;
  }
  await route.fulfill({ status: 200, body: body + (name === 'studio.js' ? hook : ''), contentType: name.endsWith('.js') ? 'text/javascript; charset=utf-8' : 'text/css; charset=utf-8' });
});
await page.route('**/zyb/studio/**', async route => {
  const request = route.request();
  if (request.method() !== 'GET') {
    writes.push({ url: request.url(), body: request.postDataJSON() });
    return route.fulfill({ status: 400, json: { error: 'DeepSeek 模型密钥无效' } });
  }
  if (responseMode === 'html') return route.fulfill({ status: 502, contentType: 'text/html', body: '<h1>Unavailable</h1>' });
  if (responseMode === 'offline') return route.abort();
  return route.continue();
});
const show = (value, step, options = {}) => page.evaluate(({value, step, options}) => window.__studioQA.show(value, step, options), {value, step, options});
const clean = async () => assert(!/DeepSeek|Seedream|Doubao|ComfyUI|Qwen|Flux|\bAPI\b|模型|收费|费用|计费|付费|扣费|余额|额度|^null$/im.test(await page.locator('.zs-studio').innerText()));
const screenshot = name => page.screenshot({ path: new URL(name + (live ? '-deployed' : '') + '.png', evidence).pathname });
try {
  await page.setViewportSize({width: 1500, height: 1050});
  await page.goto('http://127.0.0.1:8188/?zyb=studio', {waitUntil: 'domcontentloaded'});
  await page.locator('.zs-main').waitFor({timeout: 45000});
  await page.getByRole('button', {name: '+ 新建设计', exact: true}).click();
  await clean();
  assert.equal(await page.title(), '庄严宝刹 · 设计工作台');
  await screenshot('01-new-design');
  ok('home and browser title');
  await page.getByText('更多要求', {exact:false}).first().click();
  await page.locator('.zs-studio [name=culture]').fill('图案含义需核对');
  await page.locator('.zs-studio [name=process]').fill('木作连接待确认');
  await page.evaluate(() => window.__studioQA.refresh());
  assert.equal(await page.locator('.zs-studio [name=culture]').inputValue(), '图案含义需核对');
  assert.equal(await page.locator('.zs-studio [name=process]').inputValue(), '木作连接待确认');
  ok('brief culture and process drafts survive without restoring review approvals');
  await page.locator('.zs-help-menu > summary').click();
  await clean();
  await page.locator('.zs-help-menu > summary').click();
  assert.equal(await page.locator('.zs-form-card').evaluate(n => getComputedStyle(n).borderRadius), '24px');
  assert.equal(await page.locator('.zs-btn.primary').first().evaluate(n => getComputedStyle(n).borderRadius), '24px');
  ok('rounded surfaces and help copy');

  await show(sample, 3);
  await page.locator('[name=change]').fill('将底座改为深色，保留其余细节。');
  const refine = page.getByRole('button', {name: '生成修改版', exact: true});
  assert(await refine.isDisabled());
  await page.locator('[name=cloud_consent]').check();
  assert(await refine.isEnabled());
  await page.evaluate(() => window.__studioQA.refresh());
  assert.equal(await page.locator('[name=change]').inputValue(), '将底座改为深色，保留其余细节。');
  assert(!(await page.locator('[name=cloud_consent]').isChecked()));
  ok('text draft survives render without restoring consent');
  await page.locator('.zs-output-options > summary').click();
  assert.deepEqual(await page.locator('[name=size] option').evaluateAll(rows => rows.map(x => x.value)), ['1K', '2K']);
  await page.locator('[name=size]').selectOption('1K');
  assert.match(await page.locator('[data-native-size]').innerText(), /1024|1152|864/);
  await page.locator('.zs-output-options > summary').click();
  const images = sample.images.filter(row => row.concept_id === sample.selected);
  for (const row of images) {
    await page.locator('[name=image_id]').selectOption(row.id);
    if (row.id !== images.at(-1).id) {
      assert.equal(await page.locator('[name=change]').inputValue(), '');
      assert(!(await page.locator('[name=cloud_consent]').isChecked()));
    }
    const link = page.getByRole('link', {name: '下载当前原图', exact: true});
    assert((await link.getAttribute('href')).endsWith(row.id));
    assert(!(await link.getAttribute('download')).includes(row.id));
    assert.equal(await page.locator('.zs-thumb[aria-current=true]').getAttribute('data-image-id'), row.id);
  }
  assert.equal(await page.locator('.zs-vision button').count(), 0);
  await clean();
  await screenshot('02-image-editor');
  ok('sizes, version preview, download and historical checks');
  await page.locator('[name=cloud_consent]').check();
  await refine.click();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].body.image_id, images.at(-1).id);
  assert.equal(writes[0].body.cloud_consent, true);
  assert(!(await page.locator('[name=cloud_consent]').isChecked()));
  assert(await refine.isDisabled());
  await clean();
  assert.match(await page.locator('.zs-notice').innerText(), /联系管理员/);
  await page.getByRole('button', {name: '关闭提示', exact: true}).click();
  assert(await page.locator('.zs-notice').isHidden());
  ok('paid submission gated, consent reset and friendly errors');

  await show(sample, 2);
  assert.equal(await page.locator('.zs-concept').count(), Math.min(sample.concepts.length, 3));
  await page.locator('.zs-batch > summary').click();
  await clean();
  await page.locator('[name=cloud_consent]').check();
  page.once('dialog', dialog => {
    assert(!/收费|费用|计费|付费|扣费|余额|额度/.test(dialog.message()));
    dialog.dismiss();
  });
  await page.getByRole('button', {name: '生成 3 张效果图', exact: true}).click();
  assert.equal(writes.length, 1);
  await screenshot('03-concepts');
  await show(sample, 4);
  await clean();
  assert(await page.getByRole('button', {name: '保存到资料库', exact: true}).isVisible());
  assert.equal(await page.locator('.zs-archives a').count(), sample.archives.length);
  await screenshot('04-review');
  ok('concepts and archive entry retained');

  const historical = structuredClone(sample);
  historical.images = [];
  historical.visual_checks = [];
  await show(historical, 3);
  assert.equal(await page.getByRole('button', {name: '生成效果图', exact: true}).count(), 1);
  assert.equal(await page.locator('.zs-vision button').count(), 0);
  await show({...sample, id: 'b'.repeat(32)}, 3);
  assert.equal(await page.locator('[name=change]').inputValue(), '');
  ok('image-less selected design reachable and task drafts isolated');

  await show({...sample, job: {action: 'generate', status: 'uncertain', message: 'Seedream internal'}}, 3);
  await clean();
  await page.locator('[name=cloud_consent]').check();
  page.once('dialog', dialog => {
    assert(!/收费|费用|计费|付费|扣费|余额|额度/.test(dialog.message()));
    dialog.dismiss();
  });
  await page.getByRole('button', {name: '生成修改版', exact: true}).click();
  assert.equal(writes.length, 1);
  await show({...sample, job: {action: 'generate', status: 'running', message: 'Seedream 5.0 Pro', total: 3, completed: 1}}, 3);
  await clean();
  assert.match(await page.locator('.zs-job').innerText(), /2\/3/);
  ok('uncertain charge guard and provider-free progress');
  for (const message of ['余额不足', 'API 按张计费', 'Payment required', 'Quota exceeded']) {
    await show({...sample, job: {action: 'generate', status: 'failed', message}}, 3);
    await clean();
    assert.match(await page.locator('.zs-job').innerText(), /联系管理员/);
  }
  await show(sample, 1);
  await page.locator('.zs-privacy > summary').click();
  await clean();
  ok('privacy consent retained and billing errors not displayed');

  await show(sample, 3);
  await page.setViewportSize({width: 390, height: 844});
  await page.getByRole('button', {name: '我的设计', exact: true}).click();
  assert(await page.locator('.zs-sidebar').isVisible());
  await page.locator('.zs-task').first().click();
  assert(await page.locator('.zs-sidebar').isHidden());
  assert.equal(await page.locator('.zs-main').evaluate(n => n.scrollWidth > n.clientWidth), false);
  await clean();
  await screenshot('05-mobile');
  ok('mobile project switching and no horizontal overflow');

  for (const mode of ['html', 'offline']) {
    responseMode = mode;
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.getByRole('button', {name: '重新连接', exact: true}).waitFor({timeout: 30000});
    await clean();
  }
  responseMode = 'normal';
  await page.getByRole('button', {name: '重新连接', exact: true}).click();
  await page.locator('.zs-task').first().waitFor({state: 'attached'});
  ok('offline and HTML failures have retry');
  assert.deepEqual(errors, []);
  const after = await Promise.all(listing.tasks.map(row => get('tasks/' + row.id)));
  assert.equal(hash(after), before);
  assert.equal((await get('tasks')).tasks.length, listing.tasks.length);
  console.log(JSON.stringify({passed: checks, javascript_errors: errors, intercepted_posts: writes.length,
    real_mutations: 0, task_count: listing.tasks.length, image_count: originals.reduce((n, row) => n + row.images.length, 0), unchanged: true, deployed: live}, null, 2));
} finally {
  await page.unrouteAll({behavior: 'wait'});
  await page.close();
  await browser.close();
}
