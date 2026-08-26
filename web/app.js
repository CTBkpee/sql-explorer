import { startAnalysisRuntime } from "./boot.js";

const status = document.querySelector("#status");
const input = document.querySelector("#sql");
const button = document.querySelector("#analyze");
const output = document.querySelector("#output");

try {
  const analyze = await startAnalysisRuntime((message) => { status.textContent = message; });
  globalThis.analyze = analyze;
  input.disabled = false;
  button.disabled = false;
  button.addEventListener("click", async () => {
    status.textContent = "Analyzing…";
    output.textContent = JSON.stringify(await analyze(input.value), null, 2);
    status.textContent = "Analysis complete.";
  });
} catch (error) {
  status.textContent = `Runtime failed to load: ${error.message}`;
}
