const palette = ["#dcebd9", "#d9e7f1", "#f1e1c9", "#e7dcee", "#d9ece7", "#ecd9d8", "#e5e5c7"];

function escapeHtml(value) {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

function identifierRanges(script, batch, name) {
  const ranges = [];
  const wanted = name.toLocaleLowerCase();
  let cursor = batch.start;
  while (cursor < batch.end) {
    const character = script[cursor];
    if (character === "'" || (character === "N" && script[cursor + 1] === "'")) {
      cursor += character === "N" ? 2 : 1;
      while (cursor < batch.end) {
        if (script[cursor] === "'" && script[cursor + 1] === "'") { cursor += 2; continue; }
        if (script[cursor++] === "'") break;
      }
      continue;
    }
    if (script.slice(cursor, cursor + 2) === "--") {
      const newline = script.indexOf("\n", cursor + 2);
      cursor = newline < 0 ? batch.end : newline + 1;
      continue;
    }
    if (script.slice(cursor, cursor + 2) === "/*") {
      const close = script.indexOf("*/", cursor + 2);
      cursor = close < 0 ? batch.end : close + 2;
      continue;
    }
    if (character === "[") {
      const close = script.indexOf("]", cursor + 1);
      if (close >= 0) {
        if (script.slice(cursor + 1, close).toLocaleLowerCase() === wanted) ranges.push([cursor, close + 1]);
        cursor = close + 1;
        continue;
      }
    }
    if (/[A-Za-z_#@]/.test(character)) {
      const start = cursor++;
      while (cursor < batch.end && /[A-Za-z0-9_#$@]/.test(script[cursor])) cursor += 1;
      if (script.slice(start, cursor).toLocaleLowerCase() === wanted) ranges.push([start, cursor]);
      continue;
    }
    cursor += 1;
  }
  return ranges;
}

export function cteRanges(document, script) {
  const batches = new Map(document.batches.map((batch) => [batch.index, batch]));
  return document.ctes.flatMap((cte) => {
    const batch = batches.get(cte.batch);
    if (!batch || !cte.resolved) return [];
    const seen = new Set();
    const ranges = [[cte.start, cte.end], ...identifierRanges(script, batch, cte.name)]
      .filter(([start, end]) => {
        const key = `${start}:${end}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    return ranges.map(([start, end]) => ({ start, end, cte }));
  });
}

function columnRanges(document) {
  const byId = new Map((document.columns || []).map((column) => [column.id, column]));
  const ranges = new Map();
  // Output names take precedence when a bare projection is also a reference.
  for (const column of document.columns || []) {
    if (column.end > column.start) ranges.set(`${column.start}:${column.end}`, {
      start: column.start, end: column.end, column: column.resolved ? column : null,
      columnId: column.id,
    });
  }
  for (const reference of document.references || []) {
    const key = `${reference.start}:${reference.end}`;
    if (ranges.has(key)) { ranges.get(key).bindsTo = reference.binds_to; continue; }
    const column = byId.get(reference.binds_to);
    ranges.set(key, { start: reference.start, end: reference.end,
      column: column?.resolved ? column : null, columnId: null, bindsTo: reference.binds_to });
  }
  return [...ranges.values()];
}

export function renderHighlight(document, script, selectedCteId = null, selection = null) {
  const ranges = cteRanges(document, script).sort((left, right) => left.start - right.start || right.end - left.end);
  const columns = columnRanges(document);
  const contributing = new Set((selection?.spans || []).map(({ start, end }) => `${start}:${end}`));
  const marks = [
    ...ranges.map((range) => ({ ...range, kind: "cte" })),
    ...columns.map((range) => ({ ...range, kind: "column" })),
  ].filter((range) => range.start >= 0 && range.end <= script.length && range.end > range.start);
  const events = new Map();
  for (const mark of marks) {
    if (!events.has(mark.start)) events.set(mark.start, { open: [], close: [] });
    if (!events.has(mark.end)) events.set(mark.end, { open: [], close: [] });
    events.get(mark.start).open.push(mark);
    events.get(mark.end).close.push(mark);
  }
  const active = new Set();
  let html = "";
  let cursor = 0;
  function segment(end) {
    if (end <= cursor) return;
    let content = escapeHtml(script.slice(cursor, end));
    for (const range of [...active].reverse()) {
      if (range.kind === "cte") {
        const selected = range.cte.id === selectedCteId ? " is-selected" : "";
        const colour = palette[range.cte.ordinal % palette.length];
        content = `<mark class="cte-token${selected}" data-cte-id="${escapeHtml(range.cte.id)}" data-start="${range.start}" data-end="${range.end}" style="--cte-colour:${colour}">${content}</mark>`;
      } else {
        const selected = contributing.has(`${range.start}:${range.end}`) || selection?.columnIds?.has(range.columnId);
        const button = range.column ? ` role="button" tabindex="0" aria-label="Trace column ${escapeHtml(range.column.name)}" data-column-id="${escapeHtml(range.column.id)}"` : "";
        content = `<mark class="column-token${selected ? " is-contributing" : ""}" data-start="${range.start}" data-end="${range.end}"${button}>${content}</mark>`;
      }
    }
    html += content;
    cursor = end;
  }
  for (const [offset, event] of [...events].sort(([left], [right]) => left - right)) {
    segment(offset);
    for (const mark of event.close) active.delete(mark);
    for (const mark of event.open) active.add(mark);
  }
  segment(script.length);
  return { html, ranges, columnRanges: columns.filter((range) => range.column) };
}

export function cteAtOffset(ranges, offset) {
  return ranges.find((range) => range.start <= offset && offset <= range.end) || null;
}

export function invalidBatchRanges(document) {
  const bad = new Set(document.diagnostics.filter((item) => item.level === "error").map((item) => item.batch));
  return document.batches.filter((batch) => bad.has(batch.index));
}
