import React from "react";

// Small, safe Markdown renderer (ported from viewer/js/md.js). Everything is HTML-escaped FIRST; only our own tags are added after.
const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);

function safeLink(label: string, rawUrl: string) {
  const u = (rawUrl || "").trim();
  return u.startsWith("https://") && !/[\s<>"'`]/.test(u) ? `<a href="${u}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
}

function inline(s: string) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])_([^_\s][^_]*)_/g, "$1<em>$2</em>")
    .replace(/\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]/g, (_, t: string) => `<button type="button" class="wl" data-note="${t.trim()}">${t.trim()}</button>`)
    .replace(/\[([^\]]+)\]\(([^)]*)\)/g, (_, label: string, url: string) => safeLink(label, url));
}

export function renderMarkdown(src: string): string {
  const out: string[] = [];
  esc(src.replace(/\r/g, "")).split(/```/).forEach((b, i) => {
    if (i % 2) {
      out.push(`<pre><button type="button" class="copy-code" data-copy>Copy</button><code>${b.replace(/^[\w+-]*\n/, "")}</code></pre>`);
      return;
    }
    let list: string | null = null;
    let para: string[] = [];
    const flushP = () => { if (para.length) out.push(`<p>${inline(para.join(" "))}</p>`); para = []; };
    const flushL = () => { if (list) out.push(`</${list}>`); list = null; };
    for (const ln of b.split("\n")) {
      let m: RegExpMatchArray | null;
      if (!ln.trim()) { flushP(); flushL(); }
      else if ((m = ln.match(/^\s*#{1,6}\s+(.*)/))) { flushP(); flushL(); out.push(`<h4>${inline(m[1])}</h4>`); }
      else if ((m = ln.match(/^\s*([-*+]|\d+[.)])\s+(.*)/))) {
        flushP();
        const kind = /\d/.test(m[1]) ? "ol" : "ul";
        if (list !== kind) { flushL(); out.push(`<${kind}>`); list = kind; }
        out.push(`<li>${inline(m[2])}</li>`);
      } else { flushL(); para.push(ln.trim()); }
    }
    flushP();
    flushL();
  });
  return out.join("");
}

/** Rendered Markdown. Code blocks get a working Copy button; [[wikilinks]] call onNote(title). */
export const Markdown: React.FC<{ text: string; onNote?: (title: string) => void }> = React.memo(({ text, onNote }) => (
  <div
    className="md"
    onClick={(e) => {
      const t = e.target as HTMLElement;
      if (t.dataset.copy !== undefined) {
        navigator.clipboard?.writeText(t.nextElementSibling?.textContent || "").then(() => { t.textContent = "Copied"; }, () => { t.textContent = "Copy failed"; });
      } else if (t.dataset.note && onNote) onNote(t.dataset.note);
    }}
    // safe: the source is escaped above and only fixed tags are added
    dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
  />
));
