const pickerView = (() => {
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) =>
    String(value).replace(
      /[&<>"']/g,
      (character) =>
        ({
          '&': '&amp;',
          '<': '&lt;',
          '>': '&gt;',
          '"': '&quot;',
          "'": '&#39;',
        })[character],
    );

  const roles = {
    '1;4;97;48;5;23': 'native-selected',
    90: 'native-hint',
    36: 'native-heading',
    37: 'native-action',
    '1;97': 'native-key',
    92: 'green',
    '1;96': 'cyan',
    33: 'amber',
    91: 'red',
    95: 'purple',
    96: 'cyan',
    94: 'native-agent-codex',
    '38;5;215': 'native-agent-claude',
  };

  function ansiText(text, keys, expanded) {
    const parts = text.split(/\x1b\[([0-9;]+)m/);
    let result = esc(parts[0]);
    for (let index = 1; index < parts.length; index += 2) {
      const content = parts[index + 1];
      if (!content) continue;
      const role = roles[parts[index]];
      const match = keys && content.match(/^([⌃⌥])([A-Z]) /);
      const shortcut = match && (match[1] === '⌃' ? 'Control+' : 'Alt+') + match[2];
      const action = shortcut && keys[shortcut];
      if (action) {
        const disclosure =
          action === 'expand-filters'
            ? ' aria-controls="filter-options" aria-expanded="' + expanded + '"'
            : '';
        result +=
          '<button type="button" data-action="' +
          action +
          '" aria-keyshortcuts="' +
          shortcut +
          '"' +
          disclosure +
          '>' +
          esc(content) +
          '</button>';
      } else {
        result += role ? '<span class="' + role + '">' + esc(content) + '</span>' : esc(content);
      }
    }
    return result;
  }

  function measurePicker() {
    const container = $('#native-picker-controls');
    const terminal = $('#terminal');
    const charWidth = parseFloat(getComputedStyle(container, '::after').width) / 10;
    const rowHeight = parseFloat(getComputedStyle(terminal).lineHeight);
    const columns = Math.max(16, Math.floor(container.clientWidth / charWidth));
    const short = Math.floor(terminal.clientHeight / rowHeight) <= 14;
    const compact = short || columns <= 40;
    container.dataset.columns = columns;
    container.dataset.compact = String(compact);
    return { columns, compact, short };
  }

  function renderPickerGroup(id, table, { columns, compact }, keys, expanded = false, prefix = '') {
    const group = document.getElementById(id);
    group.hidden = !table;
    if (!table) return;
    const variants = table[Number(compact)];
    const [, rows] = variants.findLast(([limit]) => columns >= limit);
    const html =
      prefix +
      rows
        .map((line) => '<div class="native-picker-line">' + ansiText(line, keys, expanded) + '</div>')
        .join('');
    if (group.innerHTML !== html) group.innerHTML = html;
  }

  // Let the CSS frame reserve result space, then release gaps in native order.
  function fitPickerHeader(controls, { short }) {
    const header = $('#header');
    for (const id of controls.gapPriority) document.getElementById(id).hidden = short;
    $('#legend').hidden = short;
    $('#sort-gap').hidden = short || $('#legend').hidden;
    for (const id of [...controls.gapPriority, ...controls.contentPriority]) {
      if (header.scrollHeight <= header.clientHeight + 1) break;
      document.getElementById(id).hidden = true;
    }
  }

  return { $, esc, ansiText, measurePicker, renderPickerGroup, fitPickerHeader };
})();
