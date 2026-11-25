// Exercise the exact standalone reference that is linked from DESIGN.md.
const assert = require('node:assert/strict');
const { resolve } = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

const target = pathToFileURL(resolve(process.argv[2])).href;
const examples = {
  available: [/State\s+Available/, /Install & verify/],
  fits: [/Memory\s+Fits/, /qwen3.6:27b/],
  blocked: [/Memory\s+Blocked/, /deepseek-r1:671b/],
  'unknown-fit': [/Memory\s+Unknown fit/, /Run offline verification/],
  downloaded: [/State\s+Downloaded/, /Run offline verification/],
  verified: [/State\s+Verified/, /retest at 32K/],
  ready: [/State\s+Ready/, /Run offline verification/],
  loaded: [/State\s+Ready · loaded/, /Run offline verification/],
  alternate: [/LM Studio · installed/, /Change runtime/],
  'missing-runtime': [/Ollama · missing/, /prepare the runtime/],
  'missing-runtime-only': [/LM Studio · missing/, /Prepare runtime & verify/],
  'needs-weights': [/Review offline verification/, /Review install/],
  passed: [/Ready for offline use/, /Runtime\s+LM Studio/],
  failed: [/Offline check failed/, /Generation timed out\. Weights saved\./, /Retry verification/],
};

async function verifySpacing(page, expanded) {
  await page.locator(expanded ? '#filter-options' : '#filter-disclosure').waitFor();
  const spacing = await page.evaluate((open) => {
    const bottom = (selector) => document.querySelector(selector).getBoundingClientRect().bottom;
    const top = (selector) => document.querySelector(selector).getBoundingClientRect().top;
    const row = parseFloat(getComputedStyle(document.querySelector('#terminal')).lineHeight);
    const edges = [
      ['.refresh-row', '#filter'],
      [open ? '#filter-options' : '#filter-disclosure', '#sort'],
      ['#sort-options', '#legend'],
      ['#legend', '#result-actions'],
      ['#result-actions', '.column-row'],
      ['.column-row', '.result-row'],
    ];
    return edges.map(([before, after]) => Math.round(((top(after) - bottom(before)) / row) * 100) / 100);
  }, expanded);
  assert.deepEqual(spacing, [1, 1, 1, 1, 1, 0]);
}

async function resumeReference(page, compact) {
  await page.goto(target + '?view=resume');
  const focused = page.locator('.result-row.selected');
  assert.equal(await focused.getAttribute('data-session'), 'web-3');
  assert.match(await focused.locator('.fixed-gutter').innerText(), /↶ ↻/);
  assert.match(await page.locator('#filter').innerText(), /Saved only/);
  if (!compact) await verifySpacing(page, false);
  await page.keyboard.press('Tab');
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('Alt+f');
  assert.equal(await focused.getAttribute('data-session'), 'web-3');
  assert.match(await focused.locator('.fixed-gutter').innerText(), /✓/);
  if (!compact) await verifySpacing(page, true);
  assert.match(await page.locator('#filter-options').innerText(), /⌃F/);
  await page.keyboard.press('Control+y');
  assert.equal(await focused.getAttribute('data-session'), 'web-3');
  assert.match(await page.locator('#sort').innerText(), /Last turn/);
  await page.keyboard.press('Control+s');
  assert.match(await page.locator('#sort').innerText(), /Sort paused/);
  await page.keyboard.press('Alt+f');
  assert.equal(await page.locator('#filter-options').isVisible(), false);
  await page.locator('#query').fill('responsive navigation');
  assert.equal(await focused.getAttribute('data-session'), 'web-1');
  await page.keyboard.press('Control+x');
  assert.equal(await page.locator('#query').inputValue(), 'responsive navigation');
  await page.keyboard.press('Enter');
  await page.keyboard.press('Escape');
  assert.equal(await focused.getAttribute('data-session'), 'web-1');
  if (compact) {
    for (const id of ['refresh-gap', 'filter-gap', 'sort-gap', 'legend-gap', 'results-gap']) {
      assert.equal(await page.locator('#' + id).isVisible(), false);
    }
    assert.equal(await page.locator('#right-edge').isVisible(), true);
    const before = await focused.locator('.fixed-gutter').boundingBox();
    await page.locator('#table').hover();
    await page.mouse.wheel(0, 210);
    await page.locator('#left-edge').waitFor({ state: 'visible' });
    assert.equal((await focused.locator('.fixed-gutter').boundingBox()).x, before.x);
  }
}

async function modelReference(page, compact) {
  await page.goto(target + '?view=full');
  if (!compact) await verifySpacing(page, false);
  const columns = page.locator('[role="columnheader"]');
  const titles = await columns.allTextContents();
  const benchmarks = await page.locator('#native-picker-data').textContent();
  for (const [, title] of JSON.parse(benchmarks).benchmarks) {
    assert.ok(titles.includes(title.toUpperCase()), title);
  }
  assert.ok(!titles.includes('ACTION'));
  assert.match(await page.locator('#result-actions').innerText(), /⌥I Install.*⌥V Verify/s);
  assert.match(
    await page.locator('#legend').textContent(),
    /● ready.*◇ verified.*↓ downloaded.*○ available/s,
  );
  assert.doesNotMatch(
    await page.locator('#native-picker-controls').innerText(),
    /Hide models that won’t fit|All models|Name ↑/,
  );
  const unknown = page.locator('[data-model*="Flash-Next"] .fitcell');
  assert.equal(await unknown.textContent(), '?');
  assert.equal(await page.locator('[data-model="deepseek-r1:671b"]').count(), 0);
  await page.keyboard.press('Control+f');
  assert.equal(await page.locator('[data-model="deepseek-r1:671b"]').count(), 1);
  const before = await page.locator('#query').inputValue();
  await page.keyboard.press('Control+s');
  assert.equal(await page.locator('[aria-sort]').count(), 0);
  assert.doesNotMatch(await page.locator('#sort').innerText(), /Snapshot order/);
  assert.equal(await page.locator('#query').inputValue(), before);
  await page.keyboard.press('Control+y');
  assert.equal(await page.locator('[data-sort-key="size"]').getAttribute('aria-sort'), 'ascending');
  assert.deepEqual(await columns.allTextContents(), titles);
  const select = page.locator('#scenario');
  const modal = page.locator('#overlay');
  const actions = modal.locator('.modal-actions');
  assert.equal(await select.locator('#review-examples option').count(), Object.keys(examples).length);
  for (const [name, patterns] of Object.entries(examples)) {
    if (compact) await page.keyboard.press('F6');
    await select.selectOption('review-' + name);
    for (const expected of patterns) assert.match(await modal.innerText(), expected, name);
    assert.doesNotMatch(await actions.innerText(), /Details|Load this pair|Use as default/i);
    assert.equal(
      await modal
        .locator('.modal-hint')
        .evaluate(
          (hint) =>
            parseFloat(getComputedStyle(hint).paddingTop) ===
            parseFloat(getComputedStyle(hint).getPropertyValue('--line')),
        ),
      true,
    );
    if (name === 'blocked') {
      const markers = [
        modal.locator('dd.red'),
        page.locator('[data-model="deepseek-r1:671b"] .fitcell .red'),
        page.locator('#memory-legend .red'),
      ];
      for (const marker of markers) {
        assert.equal(await marker.evaluate((el) => getComputedStyle(el).color), 'rgb(243, 141, 152)');
      }
      assert.equal(await actions.locator('button').count(), 1);
    } else if (name === 'fits') {
      assert.equal(await modal.locator('dd.green').innerText(), 'Fits');
      assert.equal(await page.locator('[data-model="qwen3.6:27b"] .fitcell').textContent(), '✓');
    } else if (name === 'unknown-fit') {
      assert.equal(await modal.locator('dd.hint').innerText(), 'Unknown fit');
      assert.equal(await unknown.textContent(), '?');
    }
    if (name === 'alternate') {
      await actions.getByRole('button', { name: 'Change runtime' }).click();
      assert.match(await modal.innerText(), /mlx-vlm · installed/);
    }
    if (name === 'failed') {
      await page.keyboard.press('Enter');
      assert.match(await modal.innerText(), /Review offline verification/);
    }
    await page.keyboard.press('Escape');
    assert.equal(await modal.isVisible(), false);
  }
  await page.goto(target + '?example=available');
  assert.match(await modal.innerText(), /State\s+Available/);
  await page.keyboard.press('Enter');
  assert.match(await modal.innerText(), /Ready for offline use/);
  assert.equal(await actions.locator('button').count(), 1);
  await page.keyboard.press('Enter');
  assert.equal(await modal.isVisible(), false);
  assert.equal(await page.locator('.result-row.selected').getAttribute('data-model'), 'athene-v2:72b');
}

(async () => {
  const browser = await chromium.launch();
  try {
    for (const [width, height] of [
      [1360, 900],
      [320, 330],
    ]) {
      const page = await browser.newPage({ viewport: { width, height } });
      const errors = [];
      const network = [];
      page.on('pageerror', (error) => errors.push(error.message));
      await page.route(/https?:\/\//, (route) => {
        network.push(route.request().url());
        return route.abort();
      });
      await resumeReference(page, width < 600);
      if (width < 600) await page.setViewportSize({ width, height: 440 });
      await modelReference(page, width < 600);
      assert.deepEqual(errors, []);
      assert.deepEqual(network, []);
      await page.close();
    }
    const tall = await browser.newPage({ viewport: { width: 360, height: 980 } });
    for (const view of ['resume', 'full']) {
      await tall.goto(target + '?view=' + view);
      await verifySpacing(tall, false);
      await tall.keyboard.press('Alt+f');
      await verifySpacing(tall, true);
    }
    await tall.close();
    console.log('Picker reference passed at normal, narrow and short sizes');
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
