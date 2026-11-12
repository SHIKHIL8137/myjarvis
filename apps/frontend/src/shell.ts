/** Contracts between the app shell and its modules. */
export type View =
  | "overview" | "chat" | "assistant" | "decision" | "workforce" | "projects" | "skills"
  | "memory" | "graph" | "gateway" | "learning" | "recovery" | "settings";

/** What the right-hand inspector shows: a live graph node, or any record a module wants to explain. */
export type Inspectable =
  | { kind: "node"; id: string }
  | { kind: "detail"; title: string; subtitle?: string; fields: Record<string, unknown>; text?: string };

export interface ShellProps {
  onNotify: (msg: string) => void;
  onInspect?: (item: Inspectable | null) => void;
  onNavigate?: (view: View, arg?: string) => void;
}

export const fmtTime = (epochSeconds?: number | null): string =>
  epochSeconds ? new Date(epochSeconds * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—";

/** Workforce run state -> the six states the UI distinguishes + its status tone. */
export function runStatus(state: string): { label: string; tone: "ok" | "warn" | "danger" | "info" | "" } {
  if (state === "completed") return { label: "Completed", tone: "ok" };
  if (state === "running") return { label: "Running", tone: "info" };
  if (state === "awaiting_approval") return { label: "Approval required", tone: "warn" };
  if (state === "cancelling") return { label: "Waiting", tone: "warn" };
  if (state === "cancelled") return { label: "Cancelled", tone: "" };
  return { label: `Failed (${state})`, tone: "danger" };
}
