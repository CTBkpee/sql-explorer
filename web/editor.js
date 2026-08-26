export function createEditor({ textarea, code, gutter, onChange, onTokenClick }) {
  let timer = null;
  const delay = 400;
  function lineNumbers() { gutter.textContent = Array.from({ length: textarea.value.split("\n").length }, (_, index) => index + 1).join("\n"); }
  function syncScroll() { code.scrollTop = textarea.scrollTop; code.scrollLeft = textarea.scrollLeft; gutter.scrollTop = textarea.scrollTop; }
  function schedule() { clearTimeout(timer); timer = setTimeout(() => onChange(textarea.value), delay); }
  textarea.addEventListener("input", () => { lineNumbers(); schedule(); });
  textarea.addEventListener("scroll", syncScroll);
  textarea.addEventListener("click", () => onTokenClick(textarea.selectionStart));
  textarea.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    event.preventDefault();
    const start = textarea.selectionStart;
    textarea.setRangeText("  ", start, textarea.selectionEnd, "end");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  lineNumbers();
  return {
    get value() { return textarea.value; }, focus() { textarea.focus(); }, setMarkup(html) { code.innerHTML = html || " "; },
    scrollTo(offset) { const line = textarea.value.slice(0, offset).split("\n").length - 1; const lineHeight = Number.parseFloat(getComputedStyle(textarea).lineHeight) || 22; textarea.scrollTop = Math.max(0, line * lineHeight - textarea.clientHeight / 3); syncScroll(); },
  };
}
