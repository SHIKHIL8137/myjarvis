// Small, safe Markdown renderer for chat and notes. Everything is HTML-escaped FIRST; only our own tags are added after.
// Links are never navigable (the app is offline): external URLs render as plain text, [[wikilinks]] become buttons.
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])_([^_\s][^_]*)_/g, "$1<em>$2</em>")
    .replace(/\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]/g, (_, t) => `<button class="wl" data-note="${t.trim()}">${t.trim()}</button>`)
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
}

export function renderMarkdown(src) {
  const out = [];
  const blocks = esc(src.replace(/\r/g, "")).split(/```/);
  blocks.forEach((b, i) => {
    if (i % 2) { out.push(`<pre><code>${b.replace(/^[\w+-]*\n/, "")}</code></pre>`); return; }  // fenced code: verbatim
    let list = null, para = [];
    const flushP = () => { if (para.length) out.push(`<p>${inline(para.join(" "))}</p>`); para = []; };
    const flushL = () => { if (list) out.push(`</${list}>`); list = null; };
    for (const ln of b.split("\n")) {
      let m;
      if (!ln.trim()) { flushP(); flushL(); }
      else if ((m = ln.match(/^\s*#{1,6}\s+(.*)/))) { flushP(); flushL(); out.push(`<h4>${inline(m[1])}</h4>`); }
      else if ((m = ln.match(/^\s*([-*+]|\d+[.)])\s+(.*)/))) {
        flushP(); const kind = /\d/.test(m[1]) ? "ol" : "ul";
        if (list !== kind) { flushL(); out.push(`<${kind}>`); list = kind; }
        out.push(`<li>${inline(m[2])}</li>`);
      } else { flushL(); para.push(ln.trim()); }
    }
    flushP(); flushL();
  });
  return out.join("");
}

/** Render into an element and wire [[wikilink]] buttons to onNote(title). */
export function mdInto(node, src, onNote) {
  node.classList.add("md");
  node.innerHTML = renderMarkdown(src);  // safe: source was escaped above and only fixed tags were added
  if (onNote) node.querySelectorAll(".wl").forEach((b) => (b.onclick = () => onNote(b.dataset.note)));
}
