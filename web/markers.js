function markerGlyph(state) { return state === "dead" ? "✱" : "?"; }

function addMarker(list, marker, cteById) {
  if (marker.state === "live") return;
  const row = document.createElement("li");
  row.className = `analysis-marker marker-${marker.state}`;
  row.dataset.markerState = marker.state;
  row.dataset.cteId = marker.cte;
  row.dataset.column = marker.column || "";
  const glyph = document.createElement("span");
  glyph.className = "marker-glyph";
  glyph.setAttribute("aria-hidden", "true");
  glyph.textContent = markerGlyph(marker.state);
  const cte = cteById.get(marker.cte);
  const text = document.createElement("span");
  const item = marker.column ? `${cte?.name || marker.cte}.${marker.column}` : (cte?.name || marker.cte);
  text.textContent = `${item} — ${marker.state === "dead" ? "not read downstream" : "analysis could not resolve this item"}`;
  row.title = text.textContent;
  row.append(glyph, text);
  list.append(row);
}

function addUnion(list, warning) {
  const row = document.createElement("li");
  row.className = "union-warning";
  row.dataset.unionKind = warning.kind;
  row.dataset.batch = String(warning.batch);
  if (warning.kind === "column_count") {
    row.textContent = `Batch ${warning.batch}: UNION branches have different column counts (${warning.counts.join(", ")}).`;
  } else {
    row.textContent = `Batch ${warning.batch}: UNION column ${warning.position + 1} aliases differ (${warning.aliases.join(", ")}).`;
  }
  list.append(row);
}

export function renderMarkers(container, documentData) {
  const cteById = new Map(documentData.ctes.map((cte) => [cte.id, cte]));
  const markerList = document.createElement("ul");
  markerList.className = "marker-list";
  for (const marker of documentData.dead.ctes) addMarker(markerList, marker, cteById);
  for (const marker of documentData.dead.columns) addMarker(markerList, marker, cteById);
  const unionList = document.createElement("ul");
  unionList.className = "union-list";
  for (const warning of documentData.unions) addUnion(unionList, warning);
  const markerHeading = document.createElement("h2");
  markerHeading.textContent = "Read markers";
  const unionHeading = document.createElement("h2");
  unionHeading.textContent = "UNION alignment";
  const markerEmpty = document.createElement("p");
  markerEmpty.className = "empty-note";
  markerEmpty.textContent = "No dead or unresolved items reported.";
  const unionEmpty = document.createElement("p");
  unionEmpty.className = "empty-note";
  unionEmpty.textContent = "No UNION alignment warnings.";
  container.replaceChildren(markerHeading, markerList, markerList.children.length ? document.createDocumentFragment() : markerEmpty, unionHeading, unionList, unionList.children.length ? document.createDocumentFragment() : unionEmpty);
}
