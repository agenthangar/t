if (new URLSearchParams(location.search).get('view') === 'resume') {
  const { $, esc, ansiText, measurePicker, renderPickerGroup, fitPickerHeader } = pickerView;
  const fixture = JSON.parse($('#resume-picker-data').textContent);
  const results = $('#rows');
  const query = $('#query');
  const modal = $('#overlay');
  const ids = Object.keys(fixture.rows);
  const marks = new Set();
  const sorts = Object.keys(fixture.order);
  let sort = sorts[0];
  let sorting = true;
  let filtering = true;
  let editor = false;
  let live = false;
  let merged = false;
  let facets = {};
  let focused = null;
  let shown = [];
  let pan = 0;

  document.body.classList.add('resume-reference');
  $('.orientation strong').textContent = 't resume · Design reference';
  $('.orientation p').textContent = 'Sample conversations. Resume and attach actions are simulated.';
  $('#other-preview').href = '?view=full';
  $('#other-preview').textContent = 'Model library reference';
  $('#scenario').parentElement.hidden = true;
  $('#run').hidden = true;
  $('#terminal').setAttribute('aria-label', 't resume terminal reference');
  $('#table').setAttribute('aria-label', 'Saved conversations');
  $('.query-prefix').textContent = 'resume (30d)> ';
  query.setAttribute('aria-label', 'Search conversations');
  $('.marker-heading').innerHTML = '<span class="resume-state">STATUS</span>';
  $('#columns').innerHTML = esc(fixture.columns.slice(fixture.statusWidth));

  function eligible(id) {
    const row = fixture.rows[id];
    return (
      !filtering ||
      ((live || !row.attached) &&
        (!merged || row.pr !== 'MERGED') &&
        Object.entries(facets).every(([name, value]) => !value || row[name] === value))
    );
  }

  function setPan(next) {
    const table = $('#table');
    const content = $('#columns');
    const max = Math.max(0, content.scrollWidth - content.parentElement.clientWidth);
    pan = Math.min(max, Math.max(0, next));
    table.style.setProperty('--pan', pan);
    table.classList.toggle('has-left', pan > 0);
    table.classList.toggle('has-right', pan < max - 1);
  }

  function rowMarkup(id) {
    const row = fixture.rows[id];
    const state = ansiText(row.status);
    const rest = ansiText(row.display);
    const active = id === focused;
    return (
      '<div class="result-row' +
      (active ? ' selected' : '') +
      '" role="row" data-session="' +
      esc(id) +
      '" aria-selected="' +
      active +
      '"><div class="fixed-gutter">' +
      '<span class="cyan">' +
      (active ? '▸' : ' ') +
      '</span><span class="green">' +
      (marks.has(id) ? '✓' : ' ') +
      '</span>' +
      state +
      '</div><div class="clip"><div class="table-data">' +
      rest +
      '</div></div></div>'
    );
  }

  function draw() {
    const eligibleRows = ids.filter(eligible);
    const previousIndex = Math.max(0, shown.indexOf(focused));
    const phrase = query.value.toLowerCase();
    shown = (sorting ? fixture.order[sort] : ids).filter(
      (id) => eligible(id) && fixture.rows[id].search.toLowerCase().includes(phrase),
    );
    for (const id of marks) if (!shown.includes(id)) marks.delete(id);
    if (!shown.includes(focused)) focused = shown[Math.min(previousIndex, shown.length - 1)];
    $('#counts').textContent = shown.length + '/' + eligibleRows.length + ' (' + marks.size + ')';
    $('#hidden-count').textContent = ids.length - eligibleRows.length + ' hidden';
    $('#hidden-count').dataset.count = ids.length - eligibleRows.length;
    $('#command').textContent = 't resume';
    $('#picker').inert = !modal.hidden;

    const terminal = $('#terminal');
    terminal.style.height =
      Math.max(180, innerHeight - terminal.getBoundingClientRect().top - 10) + 'px';
    const dimensions = measurePicker();
    const filterKey = filtering
      ? [Number(live), Number(merged), facets.agent || '', facets.host || '', facets.repo || ''].join(
          '|',
        )
      : 'paused';
    const attached = Number(eligibleRows.some((id) => fixture.rows[id].attached));
    renderPickerGroup('filter', fixture.filter[filterKey], dimensions);
    renderPickerGroup(
      'filter-disclosure',
      fixture.disclosure[Number(editor)],
      dimensions,
      fixture.keys,
      editor,
    );
    renderPickerGroup(
      'filter-options',
      editor ? fixture.filterOptions[Number(live)][Number(filtering)] : null,
      dimensions,
      fixture.keys,
    );
    renderPickerGroup('sort', fixture.sort[sorting ? sort : 'paused'], dimensions);
    renderPickerGroup('sort-options', fixture.sortOptions[Number(sorting)], dimensions, fixture.keys);
    renderPickerGroup(
      'legend',
      fixture.legend[attached],
      dimensions,
      null,
      false,
      '<div class="native-picker-line cyan">LEGEND</div>',
    );
    renderPickerGroup('result-actions', fixture.actions[attached], dimensions);
    fitPickerHeader(fixture, dimensions);

    const previousScroll = results.scrollTop;
    results.innerHTML = shown.length
      ? shown.map(rowMarkup).join('')
      : '<div class="empty">No matching conversations</div>';
    results.scrollTop = previousScroll;
    const focusedRow = $('.result-row.selected');
    if (focusedRow) {
      const top = focusedRow.offsetTop - results.firstElementChild.offsetTop;
      results.scrollTop = Math.max(
        top + focusedRow.offsetHeight - results.clientHeight,
        Math.min(top, results.scrollTop),
      );
    }
    $('#table').setAttribute('aria-rowcount', shown.length);
    setPan(pan);
  }

  function showDetail(id, accept = false, exact = false) {
    const picked =
      accept && !exact && marks.size
        ? shown.filter((candidate) => marks.has(candidate))
        : id
          ? [id]
          : [];
    if (!picked.length) return;
    const detail = picked
      .map((candidate) => {
        const row = fixture.rows[candidate];
        const title = row.search.trim().replace(/ {2,}/g, '  ');
        return accept ? title : title + '\nWork status  ' + row.workLabel;
      })
      .join('\n');
    modal.querySelector('.modal-content').innerHTML =
      '<h2>' +
      (accept ? 'Resume selection' : 'Conversation details') +
      '</h2><p>' +
      esc(detail) +
      '</p>' +
      '<button class="back-sample" type="button">Back</button>';
    modal.hidden = false;
    $('#picker').inert = true;
    $('.back-sample').onclick = closeDetail;
    $('.back-sample').focus();
  }

  function closeDetail() {
    modal.hidden = true;
    $('#picker').inert = false;
    query.focus();
  }

  function action(name) {
    if (name === 'expand-filters') editor = !editor;
    if (name === 'toggle-filters') filtering = !filtering;
    if (name === 'state') {
      live = !live;
      filtering = true;
    }
    if (name === 'merged') {
      merged = !(filtering && merged);
      filtering = true;
    }
    if (name === 'reset-filters') {
      filtering = true;
      live = merged = false;
      facets = {};
    }
    if (name === 'toggle-sort') sorting = !sorting;
    if (name === 'next-sort') {
      sort = sorts[(sorts.indexOf(sort) + 1) % sorts.length];
      sorting = true;
    }
    if (['agent', 'repo', 'host'].includes(name)) {
      facets[name] = facets[name] ? null : fixture.rows[focused]?.[name];
      filtering = true;
    }
    draw();
    query.focus({ preventScroll: true });
  }

  function reset() {
    editor = live = merged = false;
    filtering = sorting = true;
    facets = {};
    marks.clear();
    pan = 0;
    sort = sorts[0];
    focused = null;
    shown = [];
    query.value = '';
    $('#refresh-status').textContent = 'Last refresh 09:20';
    closeDetail();
    draw();
  }

  $('#reset').onclick = reset;
  $('#controls-toggle').onclick = () => document.body.classList.toggle('tools-active');
  $('#terminal').onclick = (event) => {
    const target = event.target.closest('[data-action], [data-session]');
    if (!target) return;
    if (target.dataset.action === 'refresh') {
      $('#refresh-status').textContent = 'Last refresh 09:21';
      return;
    }
    if (target.dataset.action) action(target.dataset.action);
    else {
      focused = target.dataset.session;
      draw();
      query.focus();
    }
  };
  $('#table').addEventListener(
    'wheel',
    (event) => {
      event.preventDefault();
      const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
      setPan(pan + delta * (event.deltaMode === 1 ? 18 : 1));
    },
    { passive: false },
  );
  query.oninput = draw;
  window.addEventListener('resize', draw);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'F6') {
      event.preventDefault();
      document.body.classList.toggle('tools-active');
      if (!modal.hidden) return;
      if (document.body.classList.contains('tools-active')) $('#other-preview').focus();
      else query.focus();
      return;
    }
    if (event.target.closest('.preview-tools') || event.metaKey) return;
    if (!modal.hidden) {
      if (event.key === 'Escape') closeDetail();
      return;
    }
    const key = event.code.replace(/^Key/, '');
    const shortcut = event.altKey ? 'Alt+' + key : event.ctrlKey ? 'Control+' + key : '';
    const control = fixture.keys[shortcut];
    if (control) {
      event.preventDefault();
      action(control);
    } else if (event.ctrlKey && event.code === 'KeyB') {
      event.preventDefault();
      showDetail(
        fixture.order.closed.find((id) => fixture.rows[id].closed !== '-'),
        true,
        true,
      );
    } else if (event.ctrlKey && event.code === 'KeyU') {
      event.preventDefault();
      $('#refresh-status').textContent = 'Last refresh 09:21';
    } else if (event.ctrlKey && event.code === 'Slash') {
      event.preventDefault();
      showDetail(focused);
    } else if (['ArrowUp', 'ArrowDown', 'Tab'].includes(event.key)) {
      event.preventDefault();
      if (event.key === 'Tab' && focused) {
        if (marks.has(focused)) marks.delete(focused);
        else marks.add(focused);
      }
      const direction = event.key === 'ArrowUp' || event.shiftKey ? -1 : 1;
      focused = shown[Math.max(0, Math.min(shown.length - 1, shown.indexOf(focused) + direction))];
      draw();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      showDetail(focused, true);
    } else if (event.key === 'Escape') reset();
  });
  reset();
}
