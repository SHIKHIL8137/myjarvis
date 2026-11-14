/** partial_report arrives as {} / {summary,...} / string depending on the task; always render text, never call string methods on it. */
export function partialReportText(pr: unknown): string {
  if (typeof pr === "string") return pr;
  if (pr && typeof pr === "object") {
    const o = pr as Record<string, unknown>;
    if (typeof o.summary === "string" && o.summary) return o.summary;
    return Object.keys(o).length ? JSON.stringify(o) : "";
  }
  return "";
}

