if (new URLSearchParams(location.search).get('view') !== 'resume') {
  const { $, esc, measurePicker, renderPickerGroup, fitPickerHeader } = pickerView;
  const pickerControls = JSON.parse($('#native-picker-data').textContent);
  const { modelStates, memoryStates } = pickerControls;
  const stateChoices = ['all', ...Object.keys(modelStates), 'not ready'];

  function modelState(pair) {
    if (pair.ready) return 'ready';
    if (pair.verified) return 'verified';
    return pair.installed ? 'downloaded' : 'available';
  }

  function statusMarker(pair) {
    const { label, icon, color } = modelStates[modelState(pair)];
    return (
      '<span class="' + color + '" title="' + label + '" aria-label="' + label + '">' + icon + '</span>'
    );
  }

  function statusLegend() {
    return (
      '<b class="legend-label">Status</b>' +
      Object.values(modelStates)
        .map(
          ({ label, icon, color }) =>
            '<span class="' + color + '">' + icon + ' ' + label.toLowerCase() + '</span>',
        )
        .join('')
    );
  }

  const DEFAULT_FIT = 'not blocked';
  const fitChoices = [DEFAULT_FIT, 'all', 'fits', 'blocked', 'unknown'];
  let data = [],
    catalog = [],
    screen = 'library',
    focus = 0,
    chosen = null,
    pair = null,
    stack = [],
    events = [],
    query = '',
    filters = true,
    state = 'all',
    runtime = 'all',
    fit = DEFAULT_FIT,
    sorting = true,
    sort = 'name',
    timestamp = new Date(),
    scenario = 'normal',
    lastResult = '',
    runtimeInstalled = {},
    loaded = '',
    saved,
    draft;
  const benchmarkColumns = pickerControls.benchmarks;
  const benchmarkNames = new Map(benchmarkColumns);
  const refreshTime = new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });
  const sortNames = [
    'name',
    'size',
    'swe-bench-pro',
    'readiness',
    ...benchmarkColumns.map(([key]) => key).filter((key) => key !== 'swe-bench-pro'),
  ].filter((key) => key in pickerControls.sort);
  function heading(column, text, sortKey) {
    const key = sortKey ? ' data-sort-key="' + sortKey + '"' : '';
    return (
      '<span role="columnheader" class="cell ' +
      column +
      '"' +
      key +
      '><span>' +
      esc(text) +
      '</span></span>'
    );
  }
  function benchmarkCell(pair, key, title) {
    const cell = '<span class="cell scorecell score-' + key + '"';
    if (!pair) return heading('scorecell score-' + key, title.toUpperCase(), key);
    const entry = pair.benchmarks[key];
    const summary = entry
      ? [title, entry.version, entry.kind].filter(Boolean).join(' · ')
      : title + ' · unavailable';
    const score = entry ? entry.score.toFixed(1) + (key === 'livebench-coding' ? '' : '%') : '—';
    return cell + ' title="' + esc(summary) + '">' + score + '</span>';
  }
  const providerNames = { lmstudio: 'LM Studio', 'mlx-vlm': 'mlx-vlm', ollama: 'Ollama' };
  const pname = (p) => providerNames[p] || p;
  const pid = (p) => p.model + ' / ' + p.provider;
  const label = (p) => p.model + ' · ' + pname(p.provider);
  const reviewExamples = {
    available: { label: 'Available · install', model: 'athene-v2:72b', action: 'install' },
    downloaded: {
      label: 'Downloaded · verify offline',
      model: 'lmstudio-community/Qwen3-Coder-Next-MLX-6bit',
      action: 'verify',
    },
    verified: {
      label: 'Verified · retest at 32K',
      model: 'mlx-community/Qwen3.5-122B-A10B-4bit',
      action: 'verify',
      runtime: 'lmstudio',
    },
    ready: {
      label: 'Ready · verify again',
      model: 'mlx-community/Qwen3.8-27B-8bit',
      action: 'verify',
      runtime: 'lmstudio',
    },
    loaded: {
      label: 'Ready · loaded in memory',
      model: 'mlx-community/Qwen3.8-27B-8bit',
      action: 'verify',
      runtime: 'lmstudio',
      loaded: true,
    },
    alternate: {
      label: 'Multiple compatible runtimes',
      model: 'mlx-community/Qwen3.8-27B-8bit',
      action: 'verify',
    },
    'missing-runtime': {
      label: 'Runtime missing · install both',
      model: 'athene-v2:72b',
      action: 'install',
      sample: 'missing',
    },
    'missing-runtime-only': {
      label: 'Runtime missing · weights present',
      model: 'mlx-community/Qwen3.8-27B-8bit',
      action: 'install',
      sample: 'missing',
      runtime: 'lmstudio',
    },
    'needs-weights': { label: 'Verify · weights needed', model: 'athene-v2:72b', action: 'verify' },
    'unknown-fit': {
      label: 'Unknown memory fit',
      model: 'nace-ai/Qwen3.8-Flash-Next-MLX-4bit',
      action: 'verify',
      runtime: 'lmstudio',
    },
    fits: { label: 'Fits in memory', model: 'qwen3.6:27b', action: 'install' },
    blocked: { label: 'Blocked · insufficient memory', model: 'deepseek-r1:671b', action: 'install' },
    passed: {
      label: 'Offline check passed',
      model: 'mlx-community/Qwen3.8-27B-8bit',
      action: 'verify',
      runtime: 'lmstudio',
      showResult: true,
    },
    failed: {
      label: 'Offline check failed',
      model: 'athene-v2:72b',
      action: 'install',
      sample: 'error',
      showResult: true,
    },
  };

  for (const [key, example] of Object.entries(reviewExamples)) {
    $('#review-examples').append(new Option(example.label, 'review-' + key));
  }

  // Choose real snapshot rows so each status and display variation appears on the first screen.
  const exampleArtifacts = [
    ['athene-v2:72b'],
    ['lmstudio-community/Qwen3-Coder-Next-MLX-6bit', 'mlx-vlm'],
    ['mlx-community/Qwen3.5-122B-A10B-4bit'],
    ['mlx-community/Qwen3.8-27B-8bit'],
    ['qwen3.6:27b'],
    ['deepseek-r1:671b'],
    ['nace-ai/Qwen3.8-Flash-Next-MLX-4bit'],
  ];
  function sampleRows() {
    if (scenario === 'empty') return [];
    const source =
      scenario === 'examples'
        ? exampleArtifacts.flatMap(([artifact, provider]) =>
            catalog.filter(
              (model) => model.model === artifact && (!provider || model.provider === provider),
            ),
          )
        : catalog;
    return source.map((model) => {
      const priorContextOnly =
        scenario === 'examples' &&
        model.model === 'mlx-community/Qwen3.5-122B-A10B-4bit' &&
        model.provider === 'lmstudio';
      return {
        ...model,
        verified: model.ready,
        ready: model.ready && !priorContextOnly && scenario !== 'missing',
        priorContextOnly,
      };
    });
  }

  // The showcase includes blocked rows to make the memory states visible together.
  function initialFit() {
    return scenario === 'examples' ? 'all' : DEFAULT_FIT;
  }

  function reset() {
    const selection = $('#scenario').value;
    const example = reviewExamples[selection.slice(7)];
    scenario = example ? example.sample || 'examples' : selection;
    data = sampleRows();
    const runtimeAvailable = scenario !== 'missing';
    runtimeInstalled = {
      lmstudio: runtimeAvailable,
      'mlx-vlm': runtimeAvailable,
      ollama: runtimeAvailable,
    };
    const defaultPair = data.find((candidate) => candidate.ready);
    saved = { enabled: true, default: defaultPair ? pid(defaultPair) : '', mode: 'Pause and retry' };
    draft = { ...saved };
    loaded = example?.loaded
      ? pid(pairs(example.model).find((candidate) => candidate.provider === example.runtime))
      : '';
    events = [];
    query = '';
    state = 'all';
    runtime = example?.runtime || 'all';
    fit = initialFit();
    filters = true;
    sorting = scenario !== 'examples';
    sort = 'name';
    screen = 'library';
    stack = [];
    chosen = pair = lastFocus = null;
    editor = false;
    pan = 0;
    notice = '';
    focus = example ? visible().indexOf(example.model) : 0;
    draw();

    if (example) {
      $('#scenario').blur();
      document.body.classList.remove('tools-active');
      start(example.model, example.action);
      if (example.showResult) run();
    }
  }
  const dirty = () => JSON.stringify(saved) !== JSON.stringify(draft);
  const models = () => [...new Set(data.map((p) => p.model))];
  const pairs = (m) => data.filter((p) => p.model === m);
  const ready = () => data.filter((p) => p.ready);
  const installed = (m) => pairs(m).some((p) => p.installed);
  function matchesMemoryFit(pair) {
    if (fit === DEFAULT_FIT) return pair.fit !== false;
    if (fit === 'all') return true;
    if (fit === 'fits') return pair.fit === true;
    if (fit === 'blocked') return pair.fit === false;
    return pair.fit === null;
  }

  function eligible(model) {
    return pairs(model).filter((pair) => {
      if (!filters) return true;
      const matchesState =
        state === 'all' || state === modelState(pair) || (state === 'not ready' && !pair.ready);
      return matchesState && (runtime === 'all' || pair.provider === runtime) && matchesMemoryFit(pair);
    });
  }
  function activePair(m) {
    return eligible(m)[0] || pairs(m)[0];
  }
  function visible() {
    let list = models().filter(
      (m) => m.toLowerCase().includes(query.toLowerCase()) && eligible(m).length,
    );
    if (sorting)
      list.sort((a, b) => {
        let x = activePair(a),
          y = activePair(b);
        if (sort === 'size') return (x.bytes ?? Infinity) - (y.bytes ?? Infinity) || a.localeCompare(b);
        if (benchmarkNames.has(sort))
          return (
            (y.benchmarks[sort]?.score ?? -1) - (x.benchmarks[sort]?.score ?? -1) || a.localeCompare(b)
          );
        if (sort === 'readiness')
          return (
            modelStates[modelState(y)].priority - modelStates[modelState(x)].priority ||
            a.localeCompare(b)
          );
        return a.localeCompare(b);
      });
    return list;
  }
  function open(s) {
    stack.push({ screen, focus });
    screen = s;
    focus = 0;
    draw();
  }
  function back() {
    if (screen === 'library') {
      screen = 'hub';
      focus = 0;
      draw();
      return;
    }
    if (screen === 'hub') {
      open(dirty() ? 'discard' : 'closed');
      return;
    }
    let p = stack.pop();
    if (p) {
      screen = p.screen;
      focus = p.focus;
      draw();
    } else {
      screen = 'library';
      draw();
    }
  }
  let reviewMode = 'install';
  function start(model, action) {
    chosen = model;
    pair = activePair(model);
    if (action === 'details') {
      open('details');
      return;
    }
    reviewMode = action;
    open('review');
  }

  function reviewAction() {
    if (pair.fit === false) return null;
    if (reviewMode === 'verify' && !pair.installed) {
      return row('Review install', () => {
        reviewMode = 'install';
        draw();
      });
    }
    let label = 'Install & verify';
    if (pair.installed) {
      label = runtimeInstalled[pair.provider] ? 'Run offline verification' : 'Prepare runtime & verify';
    }
    return row(label, run, 'Simulated');
  }
  const row = (label, act, value = '') => ({ label, act, value });

  function reviewRows() {
    const choices = eligible(chosen).filter((candidate) => candidate.fit !== false);
    const primary = reviewAction();
    const actions = primary ? [primary] : [];
    if (choices.some((candidate) => candidate !== pair)) {
      actions.push(
        row('Change runtime', () => {
          pair = choices[(choices.indexOf(pair) + 1) % choices.length];
          draw();
        }),
      );
    }
    return [...actions, row('Back', back)];
  }

  function resultRows() {
    if (lastResult === 'passed') return [row('Return to library', returnList)];
    return [
      row('Retry verification', () => {
        reviewMode = 'verify';
        screen = 'review';
        focus = 0;
        draw();
      }),
      row('Back', returnList),
    ];
  }

  function rows() {
    if (screen === 'hub')
      return [
        row(
          'Library',
          () => {
            screen = 'library';
            draw();
          },
          models().length + ' artifacts · ' + data.length + ' pairs',
        ),
        row(
          'Local inference',
          () => {
            draft.enabled = !draft.enabled;
            draw();
          },
          draft.enabled ? 'On' : 'Off',
        ),
        row('Default model', () => open('default'), draft.default || 'None ready'),
        row('When cloud drops', () => open('fallback'), draft.mode),
        ...(dirty() ? [row('Review & save', () => open('settingsReview'))] : []),
        row('Exit preview', back),
      ];
    if (screen === 'review') return reviewRows();
    if (screen === 'result') return resultRows();
    if (screen === 'details')
      return [
        row(pair.installed ? 'Verify' : 'Install', () => {
          reviewMode = pair.installed ? 'verify' : 'install';
          open('review');
        }),
        row('Back', back),
      ];
    if (screen === 'default')
      return ready()
        .map((p) =>
          row(
            label(p),
            () => {
              draft.default = pid(p);
              back();
            },
            draft.default === pid(p) ? '✓' : '',
          ),
        )
        .concat([row('Back', back)]);
    if (screen === 'fallback')
      return ['Pause and retry', 'Ask which local model', 'Continue automatically']
        .map((m) =>
          row(
            m,
            () => {
              draft.mode = m;
              back();
            },
            draft.mode === m ? '✓' : '',
          ),
        )
        .concat([row('Back', back)]);
    if (screen === 'settingsReview')
      return [
        row('Save changes', () => {
          saved = { ...draft };
          events.push('Saved settings (simulated)');
          back();
        }),
        row('Back', back),
      ];
    if (screen === 'discard')
      return [
        row('Keep editing', back),
        row('Discard changes', () => {
          draft = { ...saved };
          screen = 'closed';
          draw();
        }),
      ];
    return [
      row('Reopen Models', () => {
        screen = 'hub';
        draw();
      }),
    ];
  }
  function run() {
    let reused = installed(pair.model);
    pairs(pair.model).forEach((p) => (p.installed = true));
    events.push((reused ? 'Reused weights: ' : 'Downloaded weights: ') + pair.model);
    if (!runtimeInstalled[pair.provider]) {
      runtimeInstalled[pair.provider] = true;
      events.push('Prepared ' + pname(pair.provider));
    }
    pair.ready = scenario !== 'error';
    if (pair.ready) {
      pair.verified = true;
      pair.priorContextOnly = false;
    }
    lastResult = pair.ready ? 'passed' : 'failed';
    events.push('Offline start + response generation ' + lastResult + ': ' + label(pair));
    loaded = '';
    screen = 'result';
    focus = 0;
    draw();
  }
  function returnList() {
    stack = [];
    screen = 'library';
    lastFocus = chosen;
    focus = Math.max(0, visible().indexOf(chosen));
    draw();
    focusQuery();
  }
  let pan = 0,
    editor = false,
    notice = '',
    refreshing = false,
    lastFocus = null;
  function draw() {
    const list = visible();
    if (screen === 'library') {
      if (lastFocus && list.includes(lastFocus)) focus = list.indexOf(lastFocus);
      focus = Math.max(0, Math.min(focus, list.length - 1));
      lastFocus = list[focus] || null;
    }
    const compact = innerWidth <= 620 || innerHeight <= 560;
    document.body.classList.toggle('compact', compact);
    $('#command').textContent =
      't models' + (screen === 'library' ? ' → Library' : screen === 'hub' ? '' : ' → ' + screen);
    $('#query').value = query;
    const total = models();
    const beforeSearch = total.filter((model) => eligible(model).length).length;
    const hidden = total.length - beforeSearch;
    $('#counts').textContent =
      list.length + '/' + beforeSearch + ' artifacts · ' + data.length + ' pairs';
    $('#hidden-count').textContent = hidden + ' hidden';
    $('#hidden-count').dataset.count = hidden;
    $('#refresh-status').textContent = refreshing
      ? 'Refreshing…'
      : 'Last refresh ' + refreshTime.format(timestamp);
    $('#refresh-status').className = refreshing ? 'cyan bright' : '';

    $('#marker-legend').innerHTML = statusLegend();
    $('#columns').innerHTML = cells(null, true);
    const previous = $('.column-row [aria-sort]');
    if (previous) previous.removeAttribute('aria-sort');
    if (sorting) {
      const column = document.querySelector('[data-sort-key="' + sort + '"]');
      column.setAttribute('aria-sort', ['name', 'size'].includes(sort) ? 'ascending' : 'descending');
    }
    const scroll = $('#rows').scrollTop;
    $('#rows').innerHTML = list.length
      ? list
          .map((m) => {
            const p = activePair(m);
            return (
              '<div class="result-row' +
              (m === lastFocus ? ' selected' : '') +
              '" role="row" data-model="' +
              esc(m) +
              '" aria-selected="' +
              (m === lastFocus) +
              '"><div class="fixed-gutter"><span class="cyan">' +
              (m === lastFocus ? '▸' : '') +
              '</span>' +
              statusMarker(p) +
              '</div><div class="clip"><div class="table-data">' +
              cells(m) +
              '</div></div></div>'
            );
          })
          .join('')
      : '<div class="empty">' +
        (models().length ? 'No matching models.' : 'No models in this sample catalog.') +
        '<br><span class="hint">Edit search, ⌃X reset filters, or switch sample scenario.</span></div>';
    $('#rows').scrollTop = scroll;
    $('#notice').textContent = notice || 'Benchmarks from the catalog · — unavailable';
    $('#table').setAttribute('aria-rowcount', list.length);
    renderOverlay();
    layout();
    setPan(pan);
  }
  $('#memory-legend').innerHTML =
    '<b class="legend-label">Memory</b>' +
    ['true', 'false', 'null']
      .map((key) => {
        const { icon, label, color } = memoryStates[key];
        return (
          '<span class="' +
          color +
          '">' +
          icon +
          ' ' +
          (key === 'null' ? 'unknown' : label.toLowerCase()) +
          '</span>'
        );
      })
      .join('');

  function renderPickerControls() {
    const dimensions = measurePicker();
    const keys = pickerControls.keys;
    const filterKey = filters ? [state, runtime, fit].join('|') : 'paused';
    renderPickerGroup('filter', pickerControls.filter[filterKey], dimensions);
    renderPickerGroup(
      'filter-disclosure',
      pickerControls.disclosure[Number(editor)],
      dimensions,
      keys,
      editor,
    );
    renderPickerGroup(
      'filter-options',
      editor ? pickerControls.filterOptions[Number(filters)] : null,
      dimensions,
      keys,
    );
    renderPickerGroup('sort', pickerControls.sort[sorting ? sort : 'paused'], dimensions);
    renderPickerGroup('sort-options', pickerControls.sortOptions[Number(sorting)], dimensions, keys);
    fitPickerHeader(pickerControls, dimensions);
  }

  function cells(model, head = false) {
    if (head)
      return (
        heading('modelcell', 'MODEL / ARTIFACT', 'name') +
        heading('runtimecell', 'RUNTIME') +
        heading('sizecell', 'SIZE GB', 'size') +
        heading('fitcell', 'MEMORY') +
        benchmarkColumns.map(([key, title]) => benchmarkCell(null, key, title)).join('')
      );
    const pair = activePair(model);
    const count = eligible(model).length;
    const name =
      '<button class="model-detail" data-details="' +
      esc(model) +
      '" title="' +
      esc(model) +
      '">' +
      esc(model) +
      '</button>';
    return (
      '<span class="cell modelcell">' +
      name +
      '</span>' +
      '<span class="cell runtimecell">' +
      esc(pname(pair.provider)) +
      (count > 1 ? ' +' + (count - 1) : '') +
      '</span>' +
      '<span class="cell sizecell">' +
      (pair.bytes ? (pair.bytes / 1e9).toFixed(1) : '—') +
      '</span>' +
      '<span class="cell fitcell">' +
      memoryIcon(pair.fit) +
      '</span>' +
      benchmarkColumns.map(([key, title]) => benchmarkCell(pair, key, title)).join('')
    );
  }

  function memoryIcon(fit) {
    const { icon, label, color } = memoryStates[fit ?? 'null'];
    return (
      '<span class="' + color + '" title="' + label + '" aria-label="' + label + '">' + icon + '</span>'
    );
  }

  function modelDetails() {
    const runtime = [
      pname(pair.provider),
      runtimeInstalled[pair.provider] ? 'installed' : 'missing',
    ].join(' · ');
    const otherRuntimes =
      screen === 'details'
        ? pairs(chosen)
            .filter((candidate) => candidate !== pair)
            .map((candidate) => pname(candidate.provider))
        : [];
    const memory = memoryStates[pair.fit ?? 'null'];
    const size = pair.bytes ? (pair.bytes / 1e9).toFixed(1) + ' GB' : 'Unknown size';
    const values = [
      ['Runtime', runtime + (otherRuntimes.length ? ' · also ' + otherRuntimes.join(', ') : '')],
      ['Memory', memory.label, memory.color],
      ['Size', size],
      ['State', modelStates[modelState(pair)].label + (loaded === pid(pair) ? ' · loaded' : '')],
    ];
    if (pair.priorContextOnly) values.push(['Verification', 'Passed at 8K · retest at 32K']);
    if (screen === 'review' && pair.fit !== false) {
      const tasks = [];
      if (!runtimeInstalled[pair.provider]) tasks.push('prepare the runtime');
      if (!pair.installed) tasks.push('download ' + size);
      tasks.push('check offline generation');
      values.push(['Plan', tasks.join(' → ')]);
    }
    return facts(values);
  }

  function facts(values) {
    return (
      '<dl class="model-facts">' +
      values
        .map(
          ([key, value, color]) =>
            '<dt>' +
            esc(key) +
            '</dt><dd' +
            (color ? ' class="' + color + '"' : '') +
            '>' +
            esc(value) +
            '</dd>',
        )
        .join('') +
      '</dl>'
    );
  }

  function renderOverlay() {
    const modal = $('#overlay');
    modal.hidden = screen === 'library';
    $('#picker').inert = !modal.hidden;
    if (modal.hidden) return;
    const titles = {
      hub: 'Models',
      review: reviewMode === 'verify' ? 'Review offline verification' : 'Review install',
      result: lastResult === 'failed' ? 'Offline check failed' : 'Ready for offline use',
      default: 'Default model',
      fallback: 'When cloud drops',
      settingsReview: 'Review settings',
      discard: 'Unsaved settings',
      closed: 'Models closed',
    };
    let info = '';
    if (screen === 'hub')
      info =
        (saved.enabled ? 'Local inference on' : 'Local inference off') +
        ' · ' +
        ready().length +
        ' ready pairs\nDefault: ' +
        (saved.default || 'None') +
        '\nLoaded now: ' +
        (loaded || 'None');
    if (screen === 'default')
      info = ready().length
        ? 'Choose an artifact and runtime marked Ready.'
        : 'No ready pair. Install or verify a model in Library.';
    if (screen === 'fallback') info = 'Choose what happens when the cloud connection drops.';
    if (screen === 'settingsReview')
      info = Object.keys(saved)
        .filter((k) => saved[k] !== draft[k])
        .map((k) => k + ': ' + saved[k] + ' → ' + draft[k])
        .join('\n');
    if (screen === 'discard')
      info = 'Discard staged settings? Completed simulated library actions are retained.';
    if (screen === 'closed') info = 'Preview closed. No real files, settings or runtimes changed.';
    const hasModelFacts = screen === 'review' || screen === 'details';
    const title = screen === 'details' ? chosen : titles[screen] || screen;
    const subheading = '<p class="review-artifact">' + esc(chosen) + '</p>';
    let body = '<p>' + esc(info) + '</p>';
    if (hasModelFacts) body = (screen === 'review' ? subheading : '') + modelDetails();
    if (screen === 'result') {
      body = subheading + facts([['Runtime', pname(pair.provider)]]);
      if (lastResult === 'failed') body += '<p>Generation timed out. Weights saved.</p>';
    }
    const actions = rows();
    focus = Math.max(0, Math.min(focus, actions.length - 1));
    const buttons = actions
      .map((action, index) => {
        const selected = focus === index;
        const backAction = ['Back', 'Return to library'].includes(action.label) ? ' data-back' : '';
        const description = action.value
          ? '<span class="modal-value">' + esc(action.value) + '</span>'
          : '';
        return (
          '<button class="' +
          (selected ? 'selected' : '') +
          '" data-index="' +
          index +
          '"' +
          backAction +
          '><span>' +
          (selected ? '▸ ' : '  ') +
          esc(action.label) +
          '</span>' +
          description +
          '</button>'
        );
      })
      .join('');
    const headingClass = screen === 'result' ? ' class="result-' + lastResult + '"' : '';
    const movementHint = actions.length > 1 ? '↑ ↓ move · ' : '';
    modal.querySelector('.modal-content').innerHTML =
      '<h2' +
      headingClass +
      '>' +
      esc(title) +
      '</h2>' +
      body +
      '<div class="modal-actions">' +
      buttons +
      '</div>' +
      '<div class="modal-hint">' +
      movementHint +
      '↩ Return select · ⎋ Esc back</div>';
    modal.querySelectorAll('[data-index]').forEach(
      (button) =>
        (button.onclick = () => {
          focus = Number(button.dataset.index);
          actions[focus].act();
        }),
    );
  }
  function ensureVisible() {
    const box = $('#rows'),
      row = box.children[focus];
    if (!row || !box.clientHeight) return;
    const step = row.offsetHeight,
      capacity = Math.max(1, Math.floor(box.clientHeight / step)),
      first = Math.round(box.scrollTop / step);
    box.scrollTop = Math.max(focus - capacity + 1, Math.min(focus, first)) * step;
  }
  function setPan(value) {
    const viewport = $('#columns').parentElement.clientWidth,
      width = $('#columns').scrollWidth,
      max = Math.max(0, width - viewport);
    pan = Math.max(0, Math.min(value, max));
    $('#table').style.setProperty('--pan', pan);
    $('#table').classList.toggle('has-left', pan > 0);
    $('#table').classList.toggle('has-right', pan < max - 1);
  }
  function layout() {
    requestAnimationFrame(() => {
      const terminal = $('#terminal');
      const top = terminal.getBoundingClientRect().top;
      terminal.style.height = Math.max(180, innerHeight - top - 10) + 'px';
      renderPickerControls();
      if (screen === 'library') ensureVisible();
    });
  }
  function focusQuery() {
    if (screen === 'library' && !document.body.classList.contains('tools-active'))
      $('#query').focus({ preventScroll: true });
  }
  function action(a) {
    if (a === 'expand-filters') editor = !editor;
    else if (a === 'refresh') {
      refreshing = true;
      draw();
      setTimeout(() => {
        refreshing = false;
        timestamp = new Date();
        notice = 'Refreshed sample snapshot · search, filters, sort and focus retained.';
        draw();
      }, 500);
      return;
    } else if (a === 'toggle-filters') filters = !filters;
    else if (a === 'reset-filters') {
      state = runtime = 'all';
      fit = initialFit();
      filters = true;
    } else if (a === 'toggle-sort') sorting = !sorting;
    else if (a === 'next-sort') {
      sort = sortNames[(sortNames.indexOf(sort) + 1) % sortNames.length];
      sorting = true;
    } else if (['state', 'runtime', 'fit'].includes(a)) {
      const lists = {
        state: stateChoices,
        runtime: ['all', ...new Set(data.map((p) => p.provider))],
        fit: fitChoices,
      };
      let value = { state, runtime, fit }[a],
        next = lists[a][(lists[a].indexOf(value) + 1) % lists[a].length];
      if (a === 'state') state = next;
      if (a === 'runtime') runtime = next;
      if (a === 'fit') fit = next;
      filters = true;
    } else if (a === 'cancel') {
      back();
      return;
    } else if (['install', 'verify', 'details'].includes(a)) {
      const m = visible()[focus];
      if (m) {
        lastFocus = m;
        start(m, a);
      }
      return;
    }
    draw();
    focusQuery();
  }
  function toggleTools() {
    document.body.classList.toggle('tools-active');
    if (document.body.classList.contains('tools-active')) $('#scenario').focus();
    else focusQuery();
    layout();
  }
  $('#controls-toggle').onclick = toggleTools;
  $('#query').oninput = (e) => {
    query = e.target.value;
    focus = 0;
    lastFocus = null;
    draw();
    ensureVisible();
  };
  $('#terminal').onclick = (e) => {
    const b = e.target.closest('[data-action]');
    if (b) action(b.dataset.action);
  };
  $('#rows').onclick = (e) => {
    const r = e.target.closest('.result-row');
    if (!r) return;
    focus = visible().indexOf(r.dataset.model);
    lastFocus = r.dataset.model;
    if (e.target.closest('[data-details]')) start(r.dataset.model, 'details');
    else {
      draw();
      focusQuery();
    }
  };
  $('#table').addEventListener(
    'wheel',
    (e) => {
      if (screen !== 'library' || e.ctrlKey) return;
      const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      if (d) {
        e.preventDefault();
        setPan(pan + d * (e.deltaMode === 1 ? 18 : 1));
      }
    },
    { passive: false },
  );
  window.addEventListener('resize', () => {
    draw();
    layout();
    ensureVisible();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'F6') {
      e.preventDefault();
      toggleTools();
      return;
    }
    if (e.target.closest('.preview-tools') || e.target === $('#controls-toggle')) return;
    if (e.metaKey) return;
    if (e.target.tagName === 'BUTTON' && !e.ctrlKey && !e.altKey && (e.key === 'Enter' || e.key === ' '))
      return;
    if (screen !== 'library') {
      if (e.key === 'Escape') {
        e.preventDefault();
        back();
        return;
      }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        focus = (focus + (e.key === 'ArrowDown' ? 1 : -1) + rows().length) % rows().length;
        renderOverlay();
        $('#overlay .selected')?.scrollIntoView({ block: 'nearest' });
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        rows()[focus]?.act();
        return;
      }
      return;
    }
    let k = e.key.toLowerCase();
    if (e.altKey) {
      const resultActions = { KeyI: 'install', KeyV: 'verify' };
      const selected = pickerControls.keys['Alt+' + e.code.slice(3)] || resultActions[e.code];
      if (selected) {
        e.preventDefault();
        action(selected);
        return;
      }
    }
    if (e.ctrlKey) {
      const resultActions = { u: 'refresh', '/': 'details', _: 'details' };
      const selected = pickerControls.keys['Control+' + k.toUpperCase()] || resultActions[k];
      if (selected || e.code === 'Slash') {
        e.preventDefault();
        action(selected || 'details');
        return;
      }
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'PageDown' || e.key === 'PageUp') {
      e.preventDefault();
      let step = e.key.startsWith('Page')
        ? Math.max(1, Math.floor($('#rows').clientHeight / 23) - 1)
        : 1;
      if (e.key === 'ArrowUp' || e.key === 'PageUp') step *= -1;
      focus = Math.max(0, Math.min(visible().length - 1, focus + step));
      lastFocus = visible()[focus];
      draw();
      focusQuery();
      ensureVisible();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      action('details');
    } else if (e.key === 'Escape') {
      e.preventDefault();
      action('cancel');
    } else if (e.target !== $('#query') && e.key.length === 1 && !e.ctrlKey && !e.altKey) {
      query += e.key;
      focus = 0;
      lastFocus = null;
      draw();
      focusQuery();
    }
  });
  $('#scenario').onchange = () => {
    lastFocus = null;
    editor = false;
    reset();
    focusQuery();
  };
  $('#run').onclick = () => {
    lastFocus = null;
    reset();
    focusQuery();
  };
  $('#reset').onclick = () => {
    $('#scenario').value = openingScenario;
    lastFocus = null;
    editor = false;
    pan = 0;
    reset();
    focusQuery();
  };
  const baseBack = back;
  back = function () {
    baseBack();
    if (screen === 'library') {
      lastFocus = chosen && visible().includes(chosen) ? chosen : visible()[focus];
      draw();
      focusQuery();
    }
  };
  const url = new URLSearchParams(location.search);
  const requestedExample = url.get('example');
  const openingScenario = Object.hasOwn(reviewExamples, requestedExample)
    ? 'review-' + requestedExample
    : url.get('view') === 'full'
      ? 'normal'
      : 'examples';
  catalog = JSON.parse($('#sample-catalog').textContent);
  $('#scenario').value = openingScenario;
  reset();
  focusQuery();
  window.modelPreview = {
    state: () => ({
      screen,
      query,
      state,
      runtime,
      fit,
      filters,
      sort,
      sorting,
      focus,
      focused: visible()[focus],
      visible: visible().length,
      ready: ready().map(pid),
      events: [...events],
      draft: { ...draft },
      saved: { ...saved },
    }),
    action,
  };
}
