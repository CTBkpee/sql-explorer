const palette = ["#dcebd9", "#d9e7f1", "#f1e1c9", "#e7dcee", "#d9ece7", "#ecd9d8", "#e5e5c7"];

function escapeHtml(value) {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
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

export function renderHighlight(document, script, selectedCteId = null) {
  const ranges = cteRanges(document, script).sort((left, right) => left.start - right.start || right.end - left.end);
  let html = "";
  let cursor = 0;
  for (const range of ranges) {
    if (range.start < cursor) continue;
    html += escapeHtml(script.slice(cursor, range.start));
    const selected = range.cte.id === selectedCteId ? " is-selected" : "";
    const colour = palette[range.cte.ordinal % palette.length];
    html += `<mark class="cte-token${selected}" data-cte-id="${range.cte.id}" data-start="${range.start}" data-end="${range.end}" style="--cte-colour:${colour}">${escapeHtml(script.slice(range.start, range.end))}</mark>`;
    cursor = range.end;
  }
  html += escapeHtml(script.slice(cursor));
  return { html, ranges };
}

export function cteAtOffset(ranges, offset) {
  return ranges.find((range) => range.start <= offset && offset <= range.end) || null;
}

export function invalidBatchRanges(document) {
  const bad = new Set(document.diagnostics.filter((item) => item.level === "error").map((item) => item.batch));
  return document.batches.filter((batch) => bad.has(batch.index));
}
