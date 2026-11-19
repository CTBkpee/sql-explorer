import { startAnalysisRuntime } from "./boot.js";
import { createEditor } from "./editor.js";
import { cteAtOffset, invalidBatchRanges, renderHighlight } from "./highlight.js";
import { renderGraph } from "./graph.js";
import { renderMarkers } from "./markers.js";
import { createColumnLineage, columnKey, columnSelection } from "./columns.js";

const status = document.querySelector("#status");
const textarea = document.querySelector("#sql");
const code = document.querySelector("#code");
const output = document.querySelector("#output");
const stale = document.querySelector("#stale");
const diagnostics = document.querySelector("#diagnostics");
const pane = document.querySelector(".source-pane");
const graph = document.querySelector("#graph");
const markers = document.querySelector("#markers");
const columnPanel = document.createElement("section");
columnPanel.id = "columns";
columnPanel.className = "marker-panel column-panel";
graph.closest(".analysis-pane").append(columnPanel);
code.removeAttribute("aria-hidden");

export const sourcePaneState = { selectedCteId: null, selectedColumnId: null };
globalThis.sqlExplorerSourcePane = sourcePaneState;
let latestDocument = null;
let lastGoodDocument = null;
let analyzedSql = null;
let displayedRanges = [];
let displayedColumns = [];
let selectedLineage = null;
let analysisGeneration = 0;
let editor;
let lineage;

function hasParseError(result) { return result.diagnostics.some((item) => item.level === "error"); }
function isCurrent() { return latestDocument && analyzedSql === textarea.value && !hasParseError(latestDocument); }
function escapeText(value) { return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"); }
function columnById(id) { return latestDocument?.columns.find((column) => column.id === id); }

function renderColumns(visible) {
  const heading = document.createElement("h2");
  heading.textContent = "Columns";
  const rows = [heading];
  const owners = [...visible.ctes, ...(visible.terminal || [])];
  for (const owner of owners) {
    const group = document.createElement("div");
    group.className = "column-group";
    const label = document.createElement("h3");
    label.textContent = owner.name || `Query ${owner.batch + 1}.${owner.statement_index + 1}`;
    group.append(label);
    for (const column of visible.columns.filter((item) => item.owner === owner.id)) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.columnId = column.id;
      button.textContent = column.name || "Unnamed output";
      button.setAttribute("aria-pressed", String(sourcePaneState.selectedColumnId === column.id));
      const cached = lineage?.current?.cache.get(columnKey(column));
      button.dataset.lineageState = cached instanceof Promise ? "pending" : cached ? (cached.resolved ? "ready" : "unknown") : "idle";
      if (!column.resolved || (cached && !(cached instanceof Promise) && !cached.resolved)) button.textContent += " — could not tell";
      button.disabled = !column.resolved;
      button.addEventListener("click", () => selectColumn(column));
      group.append(button);
    }
    rows.push(group);
  }
  columnPanel.replaceChildren(...rows);
}

function render(result, isStale) {
  const active = document.activeElement;
  const focusPane = code.contains(active) ? code : columnPanel.contains(active) ? columnPanel : graph.contains(active) ? graph : null;
  const focusId = active?.dataset.columnId || active?.dataset.nodeId;
  const visible = isStale && lastGoodDocument ? lastGoodDocument : result;
  if (isStale) {
    // Old offsets never index freshly edited source, even in a partially valid batch.
    displayedRanges = [];
    displayedColumns = [];
    editor.setMarkup(escapeText(textarea.value));
  } else {
    const marked = renderHighlight(visible, textarea.value, sourcePaneState.selectedCteId, selectedLineage);
    displayedRanges = marked.ranges;
    displayedColumns = marked.columnRanges;
    editor.setMarkup(marked.html);
  }
  renderGraph(graph, visible, sourcePaneState.selectedCteId, selectCte, isStale ? null : selectedLineage);
  renderMarkers(markers, visible);
  renderColumns(visible);
  output.textContent = JSON.stringify(result, null, 2);
  pane.classList.toggle("is-stale", isStale);
  graph.closest(".analysis-pane").classList.toggle("is-stale", isStale);
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
  if (focusPane && focusId) {
    const target = [...focusPane.querySelectorAll("[data-column-id], [data-node-id]")].find((item) => (item.dataset.columnId || item.dataset.nodeId) === focusId);
    target?.focus({ preventScroll: true });
  }
}

function selectCte(owner) {
  if (!owner || !latestDocument) return;
  if (!isCurrent()) { status.textContent = "Fix the parse error first to select a column."; return; }
  sourcePaneState.selectedCteId = owner.id;
  sourcePaneState.selectedColumnId = null;
  selectedLineage = null;
  render(latestDocument, false);
  editor.scrollTo(owner.start);
}

async function selectColumn(column) {
  if (!isCurrent()) {
    status.textContent = "Fix the parse error first to select a column.";
    return;
  }
  if (!column?.resolved) return;
  const session = lineage.current;
  if (!session) return;
  sourcePaneState.selectedCteId = column.owner;
  sourcePaneState.selectedColumnId = column.id;
  selectedLineage = columnSelection(latestDocument, column, null);
  const cached = session.cache.has(columnKey(column));
  status.textContent = cached ? "Column selected." : "Tracing column…";
  render(latestDocument, false);
  editor.scrollTo(column.start);
  // Let the pending status paint before an uncached synchronous Python call.
  if (!cached) await new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)));
  if (!isCurrent() || lineage.current !== session || sourcePaneState.selectedColumnId !== column.id) return;
  const result = await session.get(column);
  if (!result || !isCurrent() || lineage.current !== session || sourcePaneState.selectedColumnId !== column.id) return;
  selectedLineage = columnSelection(latestDocument, column, result);
  render(latestDocument, false);
  status.textContent = result.resolved ? `Column ${column.name} selected.` : `Column ${column.name}: could not tell.`;
}

async function analyzeScript(analyze, sql) {
  const generation = ++analysisGeneration;
  lineage.cancel();
  status.textContent = "Analyzing…";
  const result = await analyze(sql);
  if (generation !== analysisGeneration || sql !== textarea.value) return;
  latestDocument = result;
  analyzedSql = sql;
  const invalid = hasParseError(result);
  selectedLineage = null;
  sourcePaneState.selectedColumnId = null;
  if (![...result.ctes, ...result.terminal].some((owner) => owner.id === sourcePaneState.selectedCteId)) sourcePaneState.selectedCteId = null;
  if (!invalid) lastGoodDocument = result;
  const session = invalid ? null : lineage.begin(result, sql);
  render(result, invalid);
  status.textContent = invalid ? "Analysis is stale. Fix the parse error first to select a column." : "Analysis complete.";
  session?.start();
}

try {
  const analyze = await startAnalysisRuntime((message) => { status.textContent = message; });
  globalThis.analyze = analyze;
  globalThis.column_lineage = (...args) => analyze.column_lineage(...args);
  globalThis.add_column_plan = (...args) => analyze.add_column_plan(...args);
  lineage = createColumnLineage((...args) => globalThis.column_lineage(...args), (column, result) => {
    const button = [...columnPanel.querySelectorAll("button")].find((item) => item.dataset.columnId === column.id);
    if (button) {
      button.dataset.lineageState = result.resolved ? "ready" : "unknown";
      if (!result.resolved) button.textContent = `${column.name} — could not tell`;
    }
  });
  editor = createEditor({
    textarea, code,
    gutter: document.querySelector("#gutter"),
    onChange: (sql) => analyzeScript(analyze, sql).catch((error) => { status.textContent = `Analysis failed: ${error.message}`; }),
    onTokenClick: (offset) => {
      if (!isCurrent() && latestDocument) { status.textContent = "Fix the parse error first to select a column."; return; }
      const token = displayedColumns.find((range) => range.start <= offset && offset < range.end);
      if (token) selectColumn(token.column);
      else selectCte(cteAtOffset(displayedRanges, offset)?.cte);
    },
  });
  textarea.addEventListener("input", () => {
    analysisGeneration += 1;
    lineage.cancel();
    sourcePaneState.selectedColumnId = null;
    selectedLineage = null;
    status.textContent = "Waiting for analysis…";
    if (latestDocument) render(latestDocument, true);
  });
  code.addEventListener("scroll", () => {
    // Keyboard focus can scroll a token into view in the visible source layer.
    if (textarea.scrollTop !== code.scrollTop) textarea.scrollTop = code.scrollTop;
    if (textarea.scrollLeft !== code.scrollLeft) textarea.scrollLeft = code.scrollLeft;
  });
  code.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const token = event.target.closest("[data-column-id]");
    if (token) { event.preventDefault(); selectColumn(columnById(token.dataset.columnId)); }
  });
  textarea.disabled = false;
  status.textContent = "Analysis runtime ready. Paste T-SQL to begin.";
  editor.focus();
} catch (error) {
  status.textContent = `Runtime failed to load: ${error.message}`;
}
