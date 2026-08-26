import { startAnalysisRuntime } from "./boot.js";
import { createEditor } from "./editor.js";
import { cteAtOffset, invalidBatchRanges, renderHighlight } from "./highlight.js";

const status = document.querySelector("#status");
const textarea = document.querySelector("#sql");
const output = document.querySelector("#output");
const stale = document.querySelector("#stale");
const diagnostics = document.querySelector("#diagnostics");
const pane = document.querySelector(".source-pane");

export const sourcePaneState = { selectedCteId: null };
globalThis.sqlExplorerSourcePane = sourcePaneState;
let latestDocument = null;
let lastGoodDocument = null;
let displayedRanges = [];
let editor;

function hasParseError(result) { return result.diagnostics.some((item) => item.level === "error"); }

function render(result, isStale) {
  const visible = result.ctes.length || !lastGoodDocument ? result : lastGoodDocument;
  const marked = renderHighlight(visible, textarea.value, sourcePaneState.selectedCteId);
  displayedRanges = marked.ranges;
  editor.setMarkup(marked.html);
  output.textContent = JSON.stringify(result, null, 2);
  pane.classList.toggle("is-stale", isStale);
  stale.hidden = !isStale;
  diagnostics.replaceChildren(...result.diagnostics.map((item) => {
    const row = document.createElement("li");
    row.textContent = `Batch ${item.batch}: ${item.level} — ${item.reason}`;
    return row;
  }));
  for (const batch of invalidBatchRanges(result)) {
    const marker = document.createElement("span");
    marker.hidden = true;
    marker.dataset.invalidBatch = String(batch.index);
    diagnostics.append(marker);
  }
}

function selectCte(range) {
  if (!range) return;
  sourcePaneState.selectedCteId = range.cte.id;
  render(latestDocument || lastGoodDocument, hasParseError(latestDocument || { diagnostics: [] }));
  editor.scrollTo(range.cte.start);
}

async function analyzeScript(analyze, sql) {
  status.textContent = "Analyzing…";
  const result = await analyze(sql);
  latestDocument = result;
  const invalid = hasParseError(result);
  if (!invalid) lastGoodDocument = result;
  render(result, invalid);
  status.textContent = invalid ? "Analysis is stale." : "Analysis complete.";
}

try {
  const analyze = await startAnalysisRuntime((message) => { status.textContent = message; });
  globalThis.analyze = analyze;
  editor = createEditor({
    textarea,
    code: document.querySelector("#code"),
    gutter: document.querySelector("#gutter"),
    onChange: (sql) => analyzeScript(analyze, sql).catch((error) => { status.textContent = `Analysis failed: ${error.message}`; }),
    onTokenClick: (offset) => selectCte(cteAtOffset(displayedRanges, offset)),
  });
  textarea.disabled = false;
  status.textContent = "Analysis runtime ready. Paste T-SQL to begin.";
  editor.focus();
} catch (error) {
  status.textContent = `Runtime failed to load: ${error.message}`;
}
