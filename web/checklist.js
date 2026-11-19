// Match the source pane's CTE palette by the analyzer's ordinal.
const palette = ["#dcebd9", "#d9e7f1", "#f1e1c9", "#e7dcee", "#d9ece7", "#ecd9d8", "#e5e5c7"];

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

export function createChecklist({ container, call, scrollTo }) {
  const picker = container.querySelector("select");
  const copy = container.querySelector("#checklist-copy");
  const status = container.querySelector("#checklist-status");
  const routes = container.querySelector("#checklist-routes");
  let current = null;
  let generation = 0;
  let plainText = "";
  let planStatus = "";

  function clear(message) {
    generation += 1;
    routes.replaceChildren();
    status.textContent = message;
    plainText = "";
    planStatus = "";
    copy.disabled = true;
  }

  function ownerLabel(owner) {
    const cte = current.document.ctes.find((item) => item.id === owner);
    if (cte) return cte.name;
    const terminal = current.document.terminal.find((item) => item.id === owner);
    return terminal ? `Query ${terminal.batch + 1}.${terminal.statement_index + 1}` : owner;
  }

  function render(plan, target) {
    routes.replaceChildren();
    if (!plan.visible) { status.textContent = plan.reason; return; }
    const summary = plan.truncated ? `showing ${plan.paths.length} of ${plan.path_count} paths` : `${plan.path_count} ${plan.path_count === 1 ? "path" : "paths"}`;
    status.textContent = plan.reason || (plan.paths.length ? summary : "No route to a terminal query.");
    planStatus = status.textContent;
    const text = [`Add a column: ${ownerLabel(target.owner)} · ${target.name}`, status.textContent];
    for (const path of plan.paths) {
      const section = element("section", "", "checklist-route");
      section.append(element("h3", path.label));
      const list = element("ol", "", "checklist-hops");
      text.push("", path.label);
      for (const [index, hop] of path.hops.entries()) {
        const caveats = new Set(hop.caveats);
        const unresolved = caveats.has("unresolved");
        const projected = caveats.has("already_projected");
        const state = unresolved ? "unknown" : projected ? "projected" : hop.needs_edit ? "edit" : "automatic";
        const row = element("li", "", `checklist-hop checklist-${state}`);
        row.dataset.owner = hop.owner;
        row.dataset.state = state;
        const name = element("span", ownerLabel(hop.owner), "checklist-owner");
        const cte = current.document.ctes.find((item) => item.id === hop.owner);
        if (cte) name.style.backgroundColor = palette[cte.ordinal % palette.length];
        row.append(name);
        const notes = [];
        if (unresolved) notes.push("could not tell");
        else if (projected) notes.push("a column with this name is already projected here; check it is the one you mean");
        else if (!hop.needs_edit) notes.push("flows through automatically — no edit needed");
        else {
          notes.push("Add the column to this projection.");
          if (caveats.has("already_projected_partial")) notes.push("Some UNION branches already project it; only the branches that need the column added are listed below.");
        }
        if (hop.needs_edit && caveats.has("group_by")) notes.push("also add to GROUP BY, or wrap in an aggregate");
        for (const note of notes) row.append(element("p", note));
        text.push(`${index + 1}. ${ownerLabel(hop.owner)} — ${notes.join(" ")}`);
        // Unknown, name-matched and automatic hops never claim a source line.
        if (state === "edit") {
          const branches = hop.anchors.length > 1 || caveats.has("already_projected_partial");
          if (branches) {
            const count = `${hop.anchors.length} branch ${hop.anchors.length === 1 ? "anchor" : "anchors"} needing an edit`;
            row.append(element("p", count));
            text.push(`   ${count}`);
          }
          const anchors = element("ul", "", "checklist-anchors");
          for (const anchor of hop.anchors) {
            const label = `${branches ? "Branch at line" : "Line"} ${anchor.line}`;
            const link = element("button", label);
            link.type = "button";
            link.dataset.start = String(anchor.start);
            link.addEventListener("click", () => scrollTo(anchor.start));
            const item = element("li");
            item.append(link);
            anchors.append(item);
            text.push(`   ${label}`);
          }
          row.append(anchors);
        }
        list.append(row);
      }
      section.append(list);
      if (!path.hops.some((hop) => hop.needs_edit)) {
        const note = "The column already reaches this terminal.";
        section.append(element("p", note, "checklist-complete"));
        text.push(note);
      }
      routes.append(section);
    }
    plainText = text.join("\n");
    copy.disabled = false;
  }

  picker.addEventListener("change", async () => {
    const target = current?.targets[Number(picker.value)];
    clear(target && picker.value !== "" ? "Finding routes…" : "Pick a target column.");
    if (!current || current.stale || picker.value === "" || !target) return;
    const request = generation;
    try {
      const plan = await call(current.sql, target.owner, target.name);
      if (request === generation) render(plan, target);
    } catch (error) {
      if (request === generation) status.textContent = `could not tell: ${error.message}`;
    }
  });

  copy.addEventListener("click", async () => {
    const request = generation;
    try {
      await navigator.clipboard.writeText(plainText);
      if (request === generation) status.textContent = `${planStatus} Checklist copied.`;
    } catch {
      if (request === generation) status.textContent = `${planStatus} Could not copy. Select the checklist text and copy it manually.`;
    }
  });

  function update(documentData, sql, stale) {
    if (current?.document === documentData && current.sql === sql && current.stale === stale) return;
    clear(stale ? "Waiting for valid analysis. Pick a target after fixing the script." : "Pick a target column.");
    const targets = [];
    const seen = new Set();
    for (const record of [...documentData.columns, ...documentData.references.filter((ref) => ref.qualifier)]) {
      const key = JSON.stringify([record.owner, record.name]);
      if (!record.name || seen.has(key)) continue;
      seen.add(key);
      targets.push(record);
    }
    current = { document: documentData, sql, stale, targets };
    const placeholder = element("option", stale ? "Waiting for valid analysis" : targets.length ? "Choose a visible column" : "No visible columns");
    placeholder.value = "";
    picker.replaceChildren(placeholder, ...targets.map((target, index) => {
      const option = element("option", `${ownerLabel(target.owner)} · ${target.qualifier ? `${target.qualifier}.` : ""}${target.name}`);
      option.value = String(index);
      option.dataset.owner = target.owner;
      option.dataset.name = target.name;
      return option;
    }));
    picker.disabled = stale || !targets.length;
  }
  return { update };
}
