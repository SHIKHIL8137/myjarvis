/** Pure helpers for the setup wizard, health page and integrations panel (unit-tested with node --test). */

export interface HealthCheck {
  id: string;
  label: string;
  status: "ok" | "warn" | "fail";
  detail: string;
  action: string;
}

export const WIZARD_STEPS = ["welcome", "llm", "stt", "voice", "mic", "speaker", "wake", "finish"] as const;
export type WizardStep = (typeof WIZARD_STEPS)[number];

/** Checks that need a manual step from the owner (model downloads, starting Ollama...). The app never downloads anything itself. */
export const missingItems = (checks: HealthCheck[]): HealthCheck[] =>
  checks.filter((c) => c.status === "fail" && c.action.trim().length > 0);

/** The wizard can always be skipped forward; only the Ollama step is blocked from "finish" claims. */
export const stepIndex = (s: WizardStep): number => WIZARD_STEPS.indexOf(s);
export const nextStep = (s: WizardStep): WizardStep => WIZARD_STEPS[Math.min(stepIndex(s) + 1, WIZARD_STEPS.length - 1)];
export const prevStep = (s: WizardStep): WizardStep => WIZARD_STEPS[Math.max(stepIndex(s) - 1, 0)];

/** Server returns 0..1 meter values; clamp for the progress bar. */
export const meterPercent = (level: unknown): number => {
  const n = typeof level === "number" && Number.isFinite(level) ? level : 0;
  return Math.round(Math.min(1, Math.max(0, n)) * 100);
};

const MODIFIERS = new Set(["ctrl", "control", "alt", "shift", "win"]);
const KEY = /^([a-z0-9]|f([1-9]|1[0-9]|2[0-4])|space|enter|tab|esc|backspace)$/;

/** Mirrors jarvis/ptt.py parse_hotkey: at least one modifier plus exactly valid keys. Returns an error message or "" when valid. */
export function hotkeyError(raw: string): string {
  const parts = raw.toLowerCase().replace(/\s+/g, "").split("+").filter(Boolean);
  if (parts.length < 2) return "Use modifiers plus a key, e.g. ctrl+alt+space";
  if (!parts.some((p) => MODIFIERS.has(p))) return "Include at least one modifier (ctrl, alt, shift or win)";
  const bad = parts.filter((p) => !MODIFIERS.has(p) && !KEY.test(p));
  return bad.length ? `Unknown key: ${bad.join(", ")}` : "";
}

export interface McpServerStatus {
  name: string;
  enabled: boolean;
  state: string;
  tool_count: number;
  restarts: number;
  last_error: string;
  network: boolean;
}

export const mcpTone = (s: McpServerStatus): "ok" | "warn" | "danger" | "" => {
  if (!s.enabled) return "";
  if (s.state === "ready") return "ok";
  if (s.state === "failed" || s.state === "crashed") return "danger";
  return "warn";
};

/** Settings patch for toggling a server; env credentials are never edited here (they are ${ENV_VAR} references). */
export const mcpTogglePatch = (servers: Record<string, unknown>, name: string, enabled: boolean) => ({
  mcp: { servers: { ...servers, [name]: { ...(servers[name] as object), enabled } } },
});

export const parseBlockedTitles = (text: string): string[] =>
  Array.from(new Set(text.split(/[\n,]/).map((s) => s.trim().toLowerCase()).filter(Boolean)));
