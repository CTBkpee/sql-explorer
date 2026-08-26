const analyzerFiles = [
  "__init__.py", "api.py", "ctes.py", "deadcode.py", "lineage.py", "split.py", "unions.py",
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
def analyze_json(sql):
    return json.dumps(_analyze(sql))
`);
  const analyzeJson = pyodide.globals.get("analyze_json");
  onProgress("Analysis runtime ready.");
  return async (sql) => JSON.parse(await analyzeJson(sql));
}
