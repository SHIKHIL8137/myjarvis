import test from "node:test";
import assert from "node:assert/strict";
import {
  hotkeyError,
  meterPercent,
  missingItems,
  mcpTogglePatch,
  mcpTone,
  nextStep,
  parseBlockedTitles,
  prevStep,
  WIZARD_STEPS,
  type HealthCheck,
} from "./setupLogic.ts";

const chk = (id: string, status: HealthCheck["status"], action = ""): HealthCheck => ({ id, label: id, status, detail: "", action });

test("missing items are only failed checks that carry an exact manual instruction", () => {
  const items = missingItems([chk("ollama", "fail", "ollama serve"), chk("wake", "fail"), chk("disk", "ok"), chk("mic", "warn", "x")]);
  assert.deepEqual(items.map((i) => i.id), ["ollama"]);
});

test("wizard steps advance and clamp at both ends", () => {
  assert.equal(nextStep("welcome"), "llm");
  assert.equal(nextStep("finish"), "finish");
  assert.equal(prevStep("welcome"), "welcome");
  assert.equal(prevStep("finish"), "wake");
  assert.equal(WIZARD_STEPS.length, 8);
});

test("meter percent clamps and ignores garbage", () => {
  assert.equal(meterPercent(0.5), 50);
  assert.equal(meterPercent(7), 100);
  assert.equal(meterPercent(-1), 0);
  assert.equal(meterPercent("x"), 0);
  assert.equal(meterPercent(NaN), 0);
});

test("hotkey validation mirrors the backend rules", () => {
  assert.equal(hotkeyError("ctrl+alt+space"), "");
  assert.equal(hotkeyError("Ctrl + Shift + F9"), "");
  assert.match(hotkeyError("space"), /modifiers/);
  assert.match(hotkeyError("a+b"), /modifier/);
  assert.match(hotkeyError("ctrl+nonsense"), /Unknown key/);
});

test("mcp tone reflects state and disabled servers are neutral", () => {
  const base = { name: "x", enabled: true, tool_count: 0, restarts: 0, last_error: "", network: false };
  assert.equal(mcpTone({ ...base, state: "ready" }), "ok");
  assert.equal(mcpTone({ ...base, state: "crashed" }), "danger");
  assert.equal(mcpTone({ ...base, state: "starting" }), "warn");
  assert.equal(mcpTone({ ...base, state: "ready", enabled: false }), "");
});

test("toggle patch keeps other servers and never touches env", () => {
  const servers = { a: { command: "x", env: { K: "${K}" }, enabled: false }, b: { command: "y", enabled: true } };
  const p = mcpTogglePatch(servers, "a", true);
  assert.equal((p.mcp.servers.a as { enabled: boolean }).enabled, true);
  assert.deepEqual((p.mcp.servers.a as { env: object }).env, { K: "${K}" });
  assert.equal((p.mcp.servers.b as { enabled: boolean }).enabled, true);
});

test("blocked title parsing normalises, de-duplicates and drops blanks", () => {
  assert.deepEqual(parseBlockedTitles("Bank, bank\n  Password  \n\n"), ["bank", "password"]);
});
