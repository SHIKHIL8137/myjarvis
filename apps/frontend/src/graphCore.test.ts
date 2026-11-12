// Run: npm test  (node --test, no extra dependency)
import assert from "node:assert/strict";
import { test } from "node:test";
import { applyDelta, applySnapshot, createGraph, degreeMap, topHubs } from "./graphCore.ts";

const n = (id: string, label = id) => ({ id, type: id.split(":")[0], label });
const l = (source: string, target: string) => ({ source, target, relation: "linked" });

test("duplicate events and duplicate sequence numbers never duplicate nodes or links", () => {
  const g = createGraph();
  const ev = { seq: 10, nodes: [n("memory:1"), n("note:a")], links: [l("memory:1", "note:a")] };
  assert.equal(applyDelta(g, ev), true);
  assert.equal(applyDelta(g, ev), false);
  assert.equal(applyDelta(g, { seq: 9, nodes: [n("memory:stale")] }), false); // out-of-order older seq ignored
  assert.equal(g.nodes.size, 2);
  assert.equal(g.links.size, 1);
  assert.equal(g.lastSeq, 10);
});

test("updates refresh metadata in place (object identity = preserved position)", () => {
  const g = createGraph();
  applyDelta(g, { seq: 1, nodes: [n("memory:1", "old")] });
  const ref = g.nodes.get("memory:1");
  applyDelta(g, { seq: 2, nodes: [n("memory:1", "new")] });
  assert.equal(g.nodes.get("memory:1"), ref);
  assert.equal(ref?.label, "new");
});

test("deleted nodes disappear together with their edges", () => {
  const g = createGraph();
  applyDelta(g, { nodes: [n("memory:1"), n("note:a")], links: [l("memory:1", "note:a")] });
  applyDelta(g, { removed: ["memory:1"] });
  assert.deepEqual([...g.nodes.keys()], ["note:a"]);
  assert.equal(g.links.size, 0);
});

test("snapshot resync drops stale server items but keeps client-loaded ones", () => {
  const g = createGraph();
  applyDelta(g, { nodes: [n("note:a"), n("note:gone"), { ...n("file:p:1"), local: true }], links: [l("note:a", "note:gone")] });
  applySnapshot(g, { seq: 42, nodes: [n("note:a"), n("note:b")], links: [l("note:a", "note:b")] });
  assert.deepEqual([...g.nodes.keys()].sort(), ["file:p:1", "note:a", "note:b"]);
  assert.deepEqual([...g.links.keys()], ["note:a|linked|note:b"]);
  assert.equal(g.lastSeq, 42);
});

test("topHubs ranks nodes strictly by real degree centrality", () => {
  const g = createGraph();
  applyDelta(g, {
    nodes: [n("project:p1", "Alpha"), n("note:n1", "N1"), n("note:n2", "N2"), n("memory:m1", "M1")],
    links: [l("project:p1", "note:n1"), l("project:p1", "note:n2"), l("project:p1", "memory:m1"), l("note:n1", "memory:m1")],
  });
  const deg = degreeMap(g);
  assert.equal(deg.get("project:p1"), 3);
  assert.equal(deg.get("memory:m1"), 2);
  const hubs = topHubs(g, 2);
  assert.equal(hubs[0].node.id, "project:p1");
  assert.equal(hubs[0].degree, 3);
  assert.equal(hubs[1].degree, 2);
});

test("scales cleanly for 1000+ synthetic nodes and edges under 50ms", () => {
  const g = createGraph();
  const nodes = Array.from({ length: 1200 }, (_, i) => n(`note:n${i}`, `Note ${i}`));
  const links = Array.from({ length: 2400 }, (_, i) => l(`note:n${i % 1200}`, `note:n${(i * 7 + 13) % 1200}`));
  const t0 = performance.now();
  applyDelta(g, { seq: 1, nodes, links });
  const hubs = topHubs(g, 10);
  const elapsed = performance.now() - t0;
  assert.equal(g.nodes.size, 1200);
  assert.ok(hubs.length === 10);
  assert.ok(elapsed < 150, `Expected <150ms merge+centrality, got ${elapsed.toFixed(1)}ms`);
});
