const svgNs = ["http", "://www.w3.org/2000/svg"].join("");

function svgElement(name, attributes = {}) {
  const node = document.createElementNS(svgNs, name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

function sourceColour(id) {
  const mark = document.querySelector(`mark.cte-token[data-cte-id="${CSS.escape(id)}"]`);
  return mark ? getComputedStyle(mark).getPropertyValue("--cte-colour").trim() : "#e8e2d5";
}

function labelFor(cteById, id) { return cteById.get(id)?.name || id; }

export function renderGraph(container, documentData, selectedCteId, onSelect, selection = null) {
  const cteById = new Map(documentData.ctes.map((cte) => [cte.id, cte]));
  const terminalById = new Map((documentData.terminal || []).map((terminal) => [terminal.id, terminal]));
  const layerById = new Map();
  documentData.layers.forEach((layer, tier) => layer.ctes.forEach((id) => layerById.set(id, tier)));
  const tableNames = [...new Set(documentData.edges.filter((edge) => edge.kind === "table").map((edge) => edge.target))].sort();
  const groups = new Map([["base", tableNames]]);
  for (const cte of documentData.ctes) {
    const tier = layerById.get(cte.id);
    const key = tier === undefined ? "tail" : String(tier);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(cte.id);
  }
  for (const ids of groups.values()) ids.sort((left, right) => labelFor(cteById, left).localeCompare(labelFor(cteById, right)));

  if (terminalById.size) groups.set("terminal", [...terminalById.keys()]);
  const keys = ["base", ...[...groups.keys()].filter((key) => key !== "base" && key !== "tail" && key !== "terminal").sort((a, b) => Number(a) - Number(b)), ...(groups.has("tail") ? ["tail"] : []), ...(groups.has("terminal") ? ["terminal"] : [])];
  const positions = new Map();
  // Lay out top-to-bottom by tier (tall, not wide) so the long axis matches the narrow
  // analysis aside. Base tables sit at the top, downstream CTEs flow down; sibling nodes
  // within a tier spread left-to-right and are centred. The SVG then carries intrinsic
  // px width/height (below) instead of being scaled to fit, so labels render at full size.
  const nodeWidth = 124;
  const nodeHeight = 44;
  const rowGap = 88;
  const colGap = 152;
  const marginX = 20;
  const marginY = 24;
  const maxRow = Math.max(1, ...keys.map((key) => groups.get(key).length));
  const width = Math.max(nodeWidth + marginX * 2, marginX * 2 + (maxRow - 1) * colGap + nodeWidth);
  const height = marginY * 2 + (keys.length - 1) * rowGap + nodeHeight;
  keys.forEach((key, row) => {
    const ids = groups.get(key);
    const rowWidth = (ids.length - 1) * colGap + nodeWidth;
    const startX = (width - rowWidth) / 2 + nodeWidth / 2;
    const y = marginY + nodeHeight / 2 + row * rowGap;
    ids.forEach((id, column) => positions.set(id, { x: startX + column * colGap, y, tier: key }));
  });

  const svg = svgElement("svg", { class: "dependency-graph", viewBox: `0 0 ${width} ${height}`, width, height, role: "group", "aria-label": "Query dependency graph" });
  const edgeGroup = svgElement("g", { class: "graph-edges" });
  for (const edge of documentData.edges) {
    const from = positions.get(edge.source);
    const to = positions.get(edge.target);
    if (!from || !to) continue;
    const line = svgElement("line", { x1: from.x, y1: from.y, x2: to.x, y2: to.y, class: `graph-edge graph-edge-${edge.kind}${selection?.edgeKeys?.has(JSON.stringify([edge.source, edge.target])) ? " is-contributing" : ""}`, "data-source": edge.source, "data-target": edge.target, "data-kind": edge.kind });
    edgeGroup.append(line);
  }
  svg.append(edgeGroup);

  const nodeGroup = svgElement("g", { class: "graph-nodes" });
  for (const [id, point] of positions) {
    const cte = cteById.get(id);
    const terminal = terminalById.get(id);
    const isCte = Boolean(cte);
    const selectable = cte || terminal;
    const label = cte?.name || (terminal ? `Query ${terminal.batch + 1}.${terminal.statement_index + 1}` : id);
    const kind = isCte ? "cte" : terminal ? "terminal" : "table";
    const node = svgElement("g", {
      class: `graph-node graph-${kind}${id === selectedCteId ? " is-selected" : ""}${selection?.nodeIds?.has(id) ? " is-contributing" : ""}`,
      transform: `translate(${point.x} ${point.y})`,
      "data-graph-node": "true",
      "data-node-id": id,
      "data-node-kind": kind,
      "data-tier": point.tier,
      role: selectable ? "button" : "img",
      tabindex: selectable ? "0" : "-1",
      "aria-label": selectable ? `Select ${label}` : label,
    });
    const shape = isCte
      ? svgElement("rect", { x: -62, y: -19, width: 124, height: 38, rx: 8, fill: sourceColour(id), class: "graph-node-shape" })
      : terminal ? svgElement("path", { d: "M0,-28 L68,0 L0,28 L-68,0 Z", class: "graph-node-shape" })
      : svgElement("path", { d: "M-62,-13 L-50,-22 L50,-22 L62,-13 L62,13 L50,22 L-50,22 L-62,13 Z", class: "graph-node-shape" });
    const text = svgElement("text", { x: 0, y: 5, "text-anchor": "middle", class: "graph-node-label" });
    text.textContent = label;
    node.append(shape, text);
    if (selectable) {
      node.addEventListener("click", () => onSelect(selectable));
      node.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(selectable); }
      });
    }
    nodeGroup.append(node);
  }
  svg.append(nodeGroup);
  container.replaceChildren(svg);
}
