const analyzerFiles = [
  "__init__.py", "api.py", "ctes.py", "deadcode.py", "lineage.py", "split.py", "unions.py",
  "columns.py", "checklist.py",
];
const wheelUrl = "./vendor/sqlglot/sqlglot-30.17.0-py3-none-any.whl";

async function fetchBytes(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Could not load ${url}: ${response.status}`);
  return new Uint8Array(await response.arrayBuffer());
}

export async function startAnalysisRuntime(onProgress) {
  onProgress("Starting local Python runtime…");
  const pyodide = await globalThis.loadPyodide({ indexURL: "./vendor/pyodide/" });
  onProgress("Loading local SQL parser…");
  pyodide.FS.writeFile("/tmp/sqlglot-30.17.0-py3-none-any.whl", await fetchBytes(wheelUrl));
  pyodide.FS.mkdirTree("/home/pyodide/analyzer");
  for (const name of analyzerFiles) {
    pyodide.FS.writeFile(`/home/pyodide/analyzer/${name}`, await fetchBytes(`./analyzer/${name}`));
  }
  await pyodide.runPythonAsync(`
import json
import sys
sys.path.insert(0, "/tmp/sqlglot-30.17.0-py3-none-any.whl")
sys.path.insert(0, "/home/pyodide")
from analyzer.api import analyze as _analyze
from analyzer.columns import column_lineage as _column_lineage
from analyzer.checklist import add_column_plan as _add_column_plan
def analyze_json(sql):
    return json.dumps(_analyze(sql))
def column_lineage_json(sql, owner_id, column):
    return json.dumps(_column_lineage(sql, owner_id, column))
def add_column_plan_json(sql, owner_id, column):
    return json.dumps(_add_column_plan(sql, owner_id, column))
`);
  const analyzeJson = pyodide.globals.get("analyze_json");
  onProgress("Analysis runtime ready.");
  const lineageJson = pyodide.globals.get("column_lineage_json");
  const planJson = pyodide.globals.get("add_column_plan_json");
  const analyze = async (sql) => JSON.parse(await analyzeJson(sql));
  analyze.column_lineage = async (sql, owner, column) => JSON.parse(await lineageJson(sql, owner, column));
  analyze.add_column_plan = async (sql, owner, column) => JSON.parse(await planJson(sql, owner, column));
  return analyze;
}
