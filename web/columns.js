// The work decision applies to the whole document, including query outputs.
export const EAGER_WORK_LIMIT = 30000;
export const EAGER_COLUMN_LIMIT = 24;
export const EAGER_TIME_BUDGET_MS = 750;
const later = () => new Promise((resolve) => setTimeout(resolve, 0));
export const columnKey = (column) => JSON.stringify([column.owner, column.name]);

export function createColumnLineage(call, onResult) {
  let generation = 0;
  let current = null;
  function cancel() { generation += 1; current = null; }
  function begin(document, sql) {
    cancel();
    const eligible = document.columns.filter((column) => column.resolved);
    current = { generation, sql, cache: new Map(), eager: eligible.length <= EAGER_COLUMN_LIMIT && eligible.length * sql.length <= EAGER_WORK_LIMIT, done: false };
    const session = current;
    async function get(column) {
      if (current !== session) return null;
      const key = columnKey(column);
      if (session.cache.has(key)) return session.cache.get(key);
      // Store the promise before entering Python so a click and the pass share it.
      const pending = Promise.resolve().then(async () => {
        let result;
        try { result = await call(sql, column.owner, column.name); }
        catch (error) { result = { nodes: [], edges: [], resolved: false, error: error.message }; }
        if (current !== session) return null;
        session.cache.set(key, result);
        onResult(column, result);
        return result;
      });
      session.cache.set(key, pending);
      return pending;
    }
    session.get = get;
    session.start = () => {
      const started = performance.now();
      // Two frames guarantee that the rendered document can paint before Python.
      requestAnimationFrame(() => requestAnimationFrame(async () => {
        if (current !== session || !session.eager) return;
        for (const column of eligible) {
          await later();
          if (current !== session) return;
          if (performance.now() - started >= EAGER_TIME_BUDGET_MS) return;
          await get(column);
        }
        if (current === session) session.done = true;
      }));
    };
    return session;
  }
  return { begin, cancel, get current() { return current; } };
}

function nameKey(value) {
  return value.replace(/^\[|\]$/g, "").replace(/^"|"$/g, "").replaceAll("]]", "]").replaceAll('""', '"').toLocaleLowerCase();
}
function identifierForms(name) {
  return [name, `[${name.replaceAll("]", "]]")}]`, `"${name.replaceAll('"', '""')}"`];
}
function nodeName(value, names) {
  const label = value.toLocaleLowerCase();
  // Known identifiers may contain dots; match their complete names first.
  const known = [...new Set(names.map(nameKey))].sort((a, b) => b.length - a.length);
  for (const name of known) {
    for (const form of identifierForms(name)) {
      if (label === form) return { qualifier: "", name };
      if (label.endsWith(`.${form}`)) return { qualifier: nameKey(value.slice(0, -form.length - 1)), name };
    }
  }
  const dot = value.lastIndexOf(".");
  return { qualifier: dot < 0 ? "" : nameKey(value.slice(0, dot)), name: nameKey(value.slice(dot + 1)) };
}

// Lineage ids are local to a Python result. Reconcile its arcs with the frozen
// reference bindings, never with a scan of SQL text or with ids from another call.
export function columnSelection(document, column, result) {
  const selected = { columnIds: new Set([column.id]), spans: [], nodeIds: new Set([column.owner]), edgeKeys: new Set() };
  if (!result?.resolved) return selected;
  const byId = new Map(document.columns.map((item) => [item.id, item]));
  const ctes = new Map(document.ctes.map((item) => [item.id, item]));
  const nodes = new Map(result.nodes.map((node) => [node.id, node]));
  const children = new Map();
  const targets = new Set(result.edges.map((edge) => edge.target));
  for (const edge of result.edges) {
    if (!children.has(edge.source)) children.set(edge.source, []);
    children.get(edge.source).push(edge.target);
  }
  const refs = (owner) => document.references.filter((ref) => ref.owner === owner);
  function link(from, to) {
    if (from === to) return;
    if (document.edges.some((edge) => edge.source === from && edge.target === to)) {
      selected.nodeIds.add(to);
      selected.edgeKeys.add(JSON.stringify([from, to]));
    }
  }
  const visited = new Set();
  function visit(id, output, probe = false) {
    const key = JSON.stringify([id, output.owner, output.name, probe]);
    if (visited.has(key)) return;
    visited.add(key);
    if (output.id) selected.columnIds.add(output.id);
    selected.nodeIds.add(output.owner);
    for (const childId of children.get(id) || []) {
      const child = nodes.get(childId);
      if (!child) continue;
      const ownRefs = refs(output.owner);
      const reads = document.edges.filter((edge) => edge.source === output.owner);
      const part = nodeName(child.name, [output.name, ...ownRefs.map((ref) => ref.name),
        ...reads.flatMap((edge) => ctes.get(edge.target)?.columns || [])]);
      const self = ctes.get(output.owner);
      if (probe && self && part.qualifier === nameKey(self.name) && part.name === nameKey(output.name)) {
        visit(childId, output);
        continue;
      }
      let matches = ownRefs.filter((ref) => {
        if (nameKey(ref.name) !== part.name) return false;
        if (ref.qualifier) return nameKey(ref.qualifier) === part.qualifier;
        if (!part.qualifier) return true;
        const bound = byId.get(ref.binds_to);
        if (bound) return nameKey(ctes.get(bound.owner)?.name || "") === part.qualifier;
        return reads.some((edge) => nameKey(ctes.get(edge.target)?.name || edge.target) === part.qualifier);
      });
      // Set-query branch nodes have ordinal names. A direct output reference
      // supplies its binding without interpreting the branch's SQL expression.
      if (!matches.length && /^\d+$/.test(part.name)) {
        matches = ownRefs.filter((ref) => ref.start === output.start && ref.end === output.end);
        if (!matches.length) {
          const byOrdinal = ownRefs.filter((ref) => byId.get(ref.binds_to)?.ordinal === Number(part.name));
          if (new Set(byOrdinal.map((ref) => ref.binds_to)).size === 1) matches = byOrdinal;
        }
      }
      for (const ref of matches) selected.spans.push({ start: ref.start, end: ref.end });
      const upstream = [...new Set(matches.map((ref) => ref.binds_to).filter(Boolean))].map((target) => byId.get(target)).filter(Boolean);
      if (upstream.length) {
        for (const next of upstream) { link(output.owner, next.owner); visit(childId, next); }
      } else {
        // A CTE star has no projected column id. A named read edge and the
        // analyzer's expanded output list still identify its place in the chain.
        const cteReads = reads.filter((edge) => edge.kind === "cte");
        const named = cteReads.filter((edge) => nameKey(ctes.get(edge.target)?.name || "") === part.qualifier);
        const candidates = (named.length ? named : cteReads).filter((edge) => {
          const cte = ctes.get(edge.target);
          return cte?.columns.some((name) => nameKey(name) === part.name) &&
            (named.length || matches.some((ref) => !ref.binds_to));
        });
        if (candidates.length === 1) {
          const owner = candidates[0].target;
          const next = document.columns.find((item) => item.owner === owner && nameKey(item.name) === part.name);
          link(output.owner, owner);
          for (const star of document.columns.filter((item) => item.owner === owner && item.name === "*")) selected.spans.push({ start: star.start, end: star.end });
          visit(childId, next || { id: null, owner, name: part.name, start: -1, end: -1 });
          continue;
        }
        // Match physical leaf labels against known read names and aliases.
        // These are comparisons with lineage output, never a source-text scan.
        for (const edge of document.edges) {
          if (edge.source !== output.owner || edge.kind !== "table") continue;
          const expression = child.expression.toLocaleLowerCase();
          const leafMatches = identifierForms(edge.target).some((table) => identifierForms(part.qualifier).some((alias) => {
            const label = `${table} AS ${alias}`.toLocaleLowerCase();
            return expression === label || expression.endsWith(`.${label}`);
          }));
          if (!(children.get(childId)?.length) && leafMatches) link(output.owner, edge.target);
        }
        visit(childId, output);
      }
    }
  }
  for (const root of result.nodes.filter((node) => !targets.has(node.id))) visit(root.id, column, ctes.has(column.owner));
  return selected;
}
