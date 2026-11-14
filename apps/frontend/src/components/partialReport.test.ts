import test from "node:test";
import assert from "node:assert/strict";
import { partialReportText } from "./partialReport.ts";

test("partial_report renders as text for every shape the API can send (regression: Tasks tab crashed on {})", () => {
  assert.equal(partialReportText({}), "");
  assert.equal(partialReportText(null), "");
  assert.equal(partialReportText(undefined), "");
  assert.equal(partialReportText("half done"), "half done");
  assert.equal(partialReportText({ summary: "step 2 of 5" }), "step 2 of 5");
  assert.equal(partialReportText({ steps: 2 }), '{"steps":2}');
  assert.equal(partialReportText(42), "");
});
