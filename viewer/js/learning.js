/* Brain & Learning Dashboard + Memory/Workspace Drawer (Phases 2, 3–8, 11, 13, 14) */
import { api, el, $, toast, on, emit } from "./api.js";
import { openDrawer } from "./panels.js";

let currentTab = "overview";
let currentWsTab = "memory";
let refreshTimer = null;

function pill(text, cls = "") {
  return el("span", `l-pill ${cls}`.trim(), text);
}

function kvCard(title, value, sub = "", badges = []) {
  const card = el("div", "meter l-card");
  const top = el("div", "top");
  top.append(el("span", null, title), el("b", null, value));
  card.append(top);
  if (badges.length) {
    const row = el("div", "l-badges");
    badges.forEach((b) => row.append(b));
    card.append(row);
  }
  if (sub) card.append(el("div", "sub", sub));
  return card;
}

function fmtTs(ts) {
  if (!ts) return "never";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return String(ts);
  }
}

async function renderLearning() {
  const host = $("#l-body");
  if (!host) return;
  try {
    const ov = await api("/learning/overview");
    if (currentTab === "overview") renderOverview(host, ov);
    else if (currentTab === "knowledge") renderKnowledge(host, ov);
    else if (currentTab === "datasets") await renderDatasets(host, ov);
    else if (currentTab === "training") await renderTraining(host, ov);
    else if (currentTab === "evaluation") renderEvaluation(host, ov);
    else if (currentTab === "registry") renderRegistry(host, ov);
  } catch (e) {
    host.replaceChildren(el("p", "dim", `Failed to load learning dashboard: ${e.message}`));
  }
}

function renderOverview(host, ov) {
  const reg = ov.registry || {};
  const kf = ov.knowledge || {};
  const fb = (ov.feedback && ov.feedback.by_status) || {};
  const hw = ov.hardware || {};
  const caps = hw.capabilities || {};

  const grid = el("div", "l-grid");
  grid.append(
    kvCard("ACTIVE MODEL", reg.active?.ollama_tag || "baseline", `version: ${reg.active_version_id || "—"}`),
    kvCard("KNOWLEDGE INDEX", `${kf.kb_chunks ?? 0} chunks`, `last sync: ${fmtTs(kf.last_successful_update)} · ${ov.knowledge_sources_count ?? 0} sources`),
    kvCard("FEEDBACK LESSONS", `${fb.approved ?? 0} approved`, `${fb.pending ?? 0} pending review · ${fb.rejected ?? 0} rejected`),
    kvCard("HARDWARE BUDGET", `${hw.gpu_name || "CPU only"}`, `${hw.free_ram_gb ?? "?"} GB free RAM · ${hw.total_vram_gb ?? 0} GB VRAM`),
    kvCard("TRAINING RUNTIME", caps.ready_for_peft_lora ? "PEFT Ready" : "RAG + Feedback Mode", caps.ready_for_peft_lora ? "Optional LoRA packages available" : `Missing optional packages: ${(caps.missing_required || []).join(", ")}`),
    kvCard("LATEST EVALUATION", ov.evaluations?.[0]?.decision || "not run", ov.evaluations?.[0] ? `score ${ov.evaluations[0].baseline_score}` : "Run 12-category benchmark"),
  );

  const actions = el("div", "l-actions");
  const syncBtn = el("button", null, "Sync Knowledge Now");
  syncBtn.onclick = async () => {
    try {
      const res = await api("/learning/knowledge/sync", { method: "POST", body: {} });
      toast(`Synced: ${res.new_documents_found || 0} updated, ${res.failed_documents || 0} failed.`);
      renderLearning();
    } catch (e) { toast(e.message); }
  };
  const prepBtn = el("button", null, "Build Dataset from Approved Feedback");
  prepBtn.onclick = async () => {
    try {
      const r = await api("/learning/datasets/prepare-feedback", { method: "POST" });
      toast(`Prepared ${r.build?.build_id} (${r.build?.train_count} train rows).`);
      renderLearning();
    } catch (e) { toast(e.message); }
  };
  const evalBtn = el("button", null, "Run 12-Category Evaluation");
  evalBtn.onclick = async () => {
    evalBtn.disabled = true;
    evalBtn.textContent = "Evaluating…";
    try {
      const r = await api("/learning/evaluate", { method: "POST", body: {} });
      toast(`Evaluation ${r.decision} (score ${r.baseline_score})`);
      renderLearning();
    } catch (e) { toast(e.message); }
    finally { evalBtn.disabled = false; evalBtn.textContent = "Run 12-Category Evaluation"; }
  };
  actions.append(syncBtn, prepBtn, evalBtn);

  const note = el("p", "dim l-note", "Note: Knowledge updates update retrieved RAG chunks immediately without altering model weights. Fine-tuning requires explicit approval, validated datasets, evaluation, and runtime compatibility.");
  host.replaceChildren(grid, actions, note);
}

function renderKnowledge(host, ov) {
  const kf = ov.knowledge || {};
  const top = el("div", "l-actions");
  const syncBtn = el("button", null, "Sync All Enabled Sources");
  syncBtn.onclick = async () => {
    try {
      const res = await api("/learning/knowledge/sync", { method: "POST", body: {} });
      toast(`Knowledge sync: ${res.sources_checked} checked, ${res.new_documents_found} updated.`);
      renderLearning();
    } catch (e) { toast(e.message); }
  };
  top.append(syncBtn, pill(`Chunks: ${kf.kb_chunks ?? 0}`), pill(`Last sync: ${fmtTs(kf.last_successful_update)}`));

  // Manual ingest form
  const form = el("div", "box form l-form");
  const sid = el("input"); sid.placeholder = "Source ID (e.g. local-guides)";
  const title = el("input"); title.placeholder = "Document Title";
  const topic = el("input"); topic.placeholder = "Topic (e.g. architecture)";
  const body = el("textarea"); body.rows = 3; body.placeholder = "Paste trusted local documentation or release notes…";
  const addBtn = el("button", null, "Ingest Local Knowledge Document");
  addBtn.onclick = async () => {
    if (!sid.value.trim() || !body.value.trim()) return toast("Enter source ID and document text.");
    try {
      const res = await api("/learning/knowledge/ingest", {
        method: "POST",
        body: { source_id: sid.value.trim(), name: title.value.trim() || sid.value.trim(), topic: topic.value.trim() || "general", text: body.value },
      });
      toast(`Indexed version ${res.version} (${res.chunks} chunks).`);
      body.value = "";
      renderLearning();
    } catch (e) { toast(e.message); }
  };
  form.append(sid, title, topic, body, addBtn);

  // Search box
  const sRow = el("div", "inline-row");
  const sIn = el("input"); sIn.placeholder = "Search indexed knowledge chunks…";
  const sBtn = el("button", null, "Search KB");
  const sOut = el("div", "l-list");
  sBtn.onclick = async () => {
    try {
      const r = await api(`/learning/knowledge/search?q=${encodeURIComponent(sIn.value.trim())}&k=5`);
      sOut.replaceChildren(...(r.results?.length ? r.results.map((x) => kvCard(`${x.title} (v${x.version})`, `score ${x.score}`, x.content.slice(0, 180), [pill(x.trust), pill(x.language)])) : [el("p", "dim", "No indexed chunks matched.")]));
    } catch (e) { toast(e.message); }
  };
  sRow.append(sIn, sBtn);

  const srcList = el("div", "l-list");
  (ov.knowledge_sources || []).forEach((s) => {
    const row = kvCard(s.name || s.id, s.kind, `${s.location} · license: ${s.license}`, [pill(s.trust_classification || "user_provided")]);
    const rb = el("button", "ghost", "Rollback Version");
    rb.onclick = async () => {
      try {
        const res = await api(`/learning/knowledge/${encodeURIComponent(s.id)}/rollback`, { method: "POST" });
        toast(`Rolled back ${s.id} to v${res.active_version}.`);
        renderLearning();
      } catch (e) { toast(e.message); }
    };
    row.append(rb);
    srcList.append(row);
  });

  host.replaceChildren(top, form, sRow, sOut, el("h2", null, "Registered Knowledge Sources"), srcList);
}

async function renderDatasets(host, ov) {
  const [ds, fb] = await Promise.all([api("/learning/datasets"), api("/learning/feedback")]);
  const fbList = el("div", "l-list");
  (fb.records || []).slice(0, 20).forEach((rec) => {
    const card = kvCard(
      `[${rec.category}] ${rec.topic || "feedback"}`,
      rec.approval_status.toUpperCase(),
      `Q: ${rec.input_text.slice(0, 90)} → ${rec.corrected_response.slice(0, 120)}`,
      [pill(`score ${rec.quality_score}`)],
    );
    const act = el("div", "l-actions");
    if (rec.approval_status !== "approved") {
      const aBtn = el("button", null, "Approve");
      aBtn.onclick = async () => { await api(`/learning/feedback/${rec.id}/approve`, { method: "POST" }); renderLearning(); };
      act.append(aBtn);
    }
    if (rec.approval_status !== "rejected") {
      const rBtn = el("button", "ghost", "Reject");
      rBtn.onclick = async () => { await api(`/learning/feedback/${rec.id}/reject`, { method: "POST" }); renderLearning(); };
      act.append(rBtn);
    }
    const dBtn = el("button", "ghost danger", "Delete");
    dBtn.onclick = async () => { await api(`/learning/feedback/${rec.id}`, { method: "DELETE" }); renderLearning(); };
    act.append(dBtn);
    card.append(act);
    fbList.append(card);
  });
  if (!fb.records?.length) fbList.append(el("p", "dim", "No feedback records yet."));

  const form = el("div", "box form l-form");
  const did = el("input"); did.placeholder = "Dataset ID (e.g. python-qa)";
  const dpath = el("input"); dpath.placeholder = "Local path to .jsonl / .json / .zip";
  const dlic = el("input"); dlic.placeholder = "License (e.g. mit, apache-2.0)"; dlic.value = "mit";
  const impBtn = el("button", null, "Import & Hash Local Dataset");
  impBtn.onclick = async () => {
    try {
      const r = await api("/learning/datasets/import-local", {
        method: "POST",
        body: { dataset_id: did.value.trim(), path: dpath.value.trim(), license: dlic.value.trim() },
      });
      toast(`Imported v${r.snapshot.version} (sha256: ${r.snapshot.sha256.slice(0, 10)}…)`);
      renderLearning();
    } catch (e) { toast(e.message); }
  };
  form.append(did, dpath, dlic, impBtn);

  const hfRow = el("div", "inline-row");
  const hfIn = el("input"); hfIn.placeholder = "Search Hugging Face public datasets (Online Mode only)…";
  const hfBtn = el("button", null, "Discover HF");
  const hfOut = el("div", "l-list");
  hfBtn.onclick = async () => {
    try {
      const r = await api(`/learning/datasets/discover?q=${encodeURIComponent(hfIn.value.trim() || "instruction")}`);
      hfOut.replaceChildren(...(r.results || []).map((item) => kvCard(item.repo_id, item.license, `Downloads: ${item.downloads} · Likes: ${item.likes}`)));
    } catch (e) { toast(e.message); }
  };
  hfRow.append(hfIn, hfBtn);

  const list = el("div", "l-list");
  (ds.datasets || []).forEach((d) => {
    const v = d.versions?.[String(d.latest_version)];
    const val = v?.validation;
    const card = kvCard(
      `${d.name} (v${d.latest_version})`,
      v?.license || "unknown",
      `SHA: ${(v?.sha256 || "").slice(0, 14)}… · ${val ? `valid ${val.valid_records}/${val.total_records}` : "unvalidated"}`,
    );
    const proc = el("button", null, "Validate, Clean & Build Splits");
    proc.onclick = async () => {
      try {
        const res = await api("/learning/datasets/process", { method: "POST", body: { dataset_id: d.id, version: d.latest_version } });
        toast(res.approved ? `Built ${res.build.build_id} (0 leakage)` : `Rejected: ${(res.validation.reasons || []).join("; ")}`);
        renderLearning();
      } catch (e) { toast(e.message); }
    };
    card.append(proc);
    list.append(card);
  });

  const builds = el("div", "l-list");
  (ds.builds || []).forEach((b) => {
    builds.append(kvCard(b.build_id, b.target_format, `train=${b.train_count} · val=${b.val_count} · test=${b.test_count} · leakage=${b.leakage_overlaps}`));
  });

  host.replaceChildren(el("h2", null, "Feedback Review & Approval Queue"), fbList, el("h2", null, "Dataset Import & Validation"), form, hfRow, hfOut, el("h2", null, "Dataset Versions"), list, el("h2", null, "Prepared Split Builds"), builds);
}

async function renderTraining(host, ov) {
  const data = await api("/learning/training/jobs");
  const hw = data.hardware || {};
  const caps = hw.capabilities || {};
  const banner = kvCard(
    `HARDWARE READINESS (${hw.gpu_name || "CPU"})`,
    caps.ready_for_peft_lora ? "PEFT Ready" : "Packages Missing",
    `Free RAM ${hw.free_ram_gb}/${hw.total_ram_gb} GB · VRAM ${hw.free_vram_gb}/${hw.total_vram_gb} GB · Ollama ${hw.ollama_loaded_vram_gb} GB`,
    caps.missing_required?.length ? [pill(`Missing: ${caps.missing_required.join(", ")}`, "warn")] : [pill("torch/peft present", "ok")],
  );

  const form = el("div", "inline-row");
  const sel = el("select");
  (ov.dataset_builds || []).forEach((b) => sel.add(new Option(`${b.build_id} (${b.train_count} train)`, b.build_id)));
  const reqBtn = el("button", null, "Create Approval-Gated Training Job");
  reqBtn.onclick = async () => {
    if (!sel.value) return toast("Prepare a dataset build first.");
    try {
      const j = await api("/learning/training/jobs", { method: "POST", body: { dataset_build_id: sel.value } });
      toast(`Created job ${j.job_id} (${j.state})`);
      renderLearning();
    } catch (e) { toast(e.message); }
  };
  form.append(sel, reqBtn);

  const jobsWrap = el("div", "l-list");
  (data.jobs || []).forEach((j) => {
    const est = j.estimate || {};
    const prog = j.progress || {};
    const card = kvCard(
      j.job_id,
      j.state.toUpperCase(),
      `est VRAM ${est.estimated_vram_gb} GB · step ${prog.step || 0}/${prog.total_steps || 0}${prog.error ? ` · ${prog.error}` : ""}`,
      (est.blockers || []).slice(0, 2).map((b) => pill(b, "warn")),
    );
    const bar = el("div", "l-actions");
    const act = (lbl, path) => {
      const b = el("button", "ghost", lbl);
      b.onclick = async () => {
        try { await api(path, { method: "POST" }); renderLearning(); } catch (e) { toast(e.message); }
      };
      return b;
    };
    if (!j.approved) bar.append(act("Approve", `/learning/training/jobs/${j.job_id}/approve`));
    if (j.approved && ["approved", "queued"].includes(j.state)) bar.append(act("Start Worker", `/learning/training/jobs/${j.job_id}/start`));
    if (j.state === "running") bar.append(act("Pause", `/learning/training/jobs/${j.job_id}/pause`));
    if (j.state === "paused") bar.append(act("Resume", `/learning/training/jobs/${j.job_id}/resume`));
    if (!["completed", "cancelled", "blocked", "failed"].includes(j.state)) bar.append(act("Cancel", `/learning/training/jobs/${j.job_id}/cancel`));
    if (j.log_tail?.length) card.append(el("pre", "l-log", j.log_tail.join("\n")));
    card.append(bar);
    jobsWrap.append(card);
  });

  host.replaceChildren(banner, form, el("h2", null, "Training Jobs"), jobsWrap);
}

function renderEvaluation(host, ov) {
  const act = el("div", "l-actions");
  const runBtn = el("button", null, "Run Fixed 12-Category Evaluation");
  runBtn.onclick = async () => {
    runBtn.disabled = true;
    runBtn.textContent = "Running Benchmark…";
    try {
      const r = await api("/learning/evaluate", { method: "POST", body: {} });
      toast(`Result: ${r.decision} (${r.baseline_score})`);
      renderLearning();
    } catch (e) { toast(e.message); }
    finally { runBtn.disabled = false; runBtn.textContent = "Run Fixed 12-Category Evaluation"; }
  };
  act.append(runBtn);

  const reports = el("div", "l-list");
  (ov.evaluations || []).forEach((rep) => {
    const card = kvCard(
      rep.eval_id,
      rep.decision,
      `Baseline: ${rep.baseline_score} (${rep.baseline_avg_ms} ms)${rep.candidate_score != null ? ` · Candidate: ${rep.candidate_score}` : ""} · Regressions: ${rep.regressions} · Safety failures: ${rep.safety_failures}`,
      (rep.reasons || []).map((r) => pill(r)),
    );
    const cats = el("div", "l-cat-grid");
    Object.entries(rep.categories || {}).forEach(([k, v]) => {
      const ok = v.baseline?.passed;
      cats.append(pill(`${k}: ${ok ? "PASS" : "FAIL"}`, ok ? "ok" : "bad"));
    });
    card.append(cats);
    reports.append(card);
  });
  host.replaceChildren(act, reports);
}

function renderRegistry(host, ov) {
  const reg = ov.registry || {};
  const top = el("div", "l-actions");
  const rbBtn = el("button", "danger", "Rollback Active Model");
  rbBtn.onclick = async () => {
    try {
      const r = await api("/learning/registry/rollback", { method: "POST", body: {} });
      toast(`Rolled back active model to ${r.active_version_id}.`);
      renderLearning();
    } catch (e) { toast(e.message); }
  };
  top.append(pill(`Active: ${reg.active_version_id || "—"}`, "ok"), pill(`Baseline: ${reg.baseline_version_id || "—"}`), rbBtn);

  const list = el("div", "l-list");
  (reg.versions || []).forEach((v) => {
    const isAct = v.version_id === reg.active_version_id;
    const card = kvCard(
      `${v.version_id} (${v.ollama_tag || v.base_model})`,
      isAct ? "ACTIVE" : v.status.toUpperCase(),
      `type=${v.artifact_type} · quant=${v.quantization} · eval=${v.evaluation_score ?? "not run"} (${v.evaluation_passed ? "passed" : "unverified"})`,
    );
    if (!isAct && v.status === "candidate") {
      const pBtn = el("button", null, "Promote (Requires Passed Eval)");
      pBtn.onclick = async () => {
        try {
          await api(`/learning/registry/${encodeURIComponent(v.version_id)}/promote`, { method: "POST" });
          toast(`Promoted ${v.version_id}.`);
          renderLearning();
        } catch (e) { toast(e.message); }
      };
      card.append(pBtn);
    }
    list.append(card);
  });
  host.replaceChildren(top, list);
}

// ---------------- Workspace Drawer: Memory, History, Safe Files & Actions -----------------------------
async function renderWorkspace() {
  const host = $("#w-body");
  if (!host) return;
  try {
    if (currentWsTab === "memory") await renderMemoryTab(host);
    else if (currentWsTab === "history") await renderHistoryTab(host);
    else if (currentWsTab === "files") await renderFilesTab(host);
    else if (currentWsTab === "actions") await renderActionsTab(host);
  } catch (e) {
    host.replaceChildren(el("p", "dim", `Workspace error: ${e.message}`));
  }
}

async function renderMemoryTab(host) {
  const bar = el("div", "inline-row");
  const cat = el("select");
  ["", "explicit", "preference", "project", "temporary", "feedback"].forEach((c) => cat.add(new Option(c || "All Categories", c)));
  const qIn = el("input"); qIn.placeholder = "Search personal memories…";
  const addIn = el("input"); addIn.placeholder = "Add new fact/preference…";
  const addBtn = el("button", null, "Remember");
  const bakBtn = el("button", "ghost", "Backup");
  bar.append(cat, qIn, addIn, addBtn, bakBtn);

  const list = el("div", "l-list");
  async function load() {
    const r = await api(`/memory/items?category=${encodeURIComponent(cat.value)}&q=${encodeURIComponent(qIn.value.trim())}`);
    list.replaceChildren(...(r.items?.length ? r.items.map((m) => {
      const card = kvCard(`[${m.category}] ${m.title}`, m.confirmed ? "CONFIRMED" : "PENDING", m.text);
      const act = el("div", "l-actions");
      if (!m.confirmed) {
        const cBtn = el("button", null, "Confirm");
        cBtn.onclick = async () => { await api(`/memory/items/${m.id}/confirm?approved=true`, { method: "POST" }); load(); };
        act.append(cBtn);
      }
      const dBtn = el("button", "ghost danger", "Delete");
      dBtn.onclick = async () => { await api(`/memory/items/${m.id}`, { method: "DELETE" }); load(); };
      act.append(dBtn);
      card.append(act);
      return card;
    }) : [el("p", "dim", "No stored personal memories match.")]));
  }
  cat.onchange = load;
  qIn.oninput = () => load().catch(() => {});
  addBtn.onclick = async () => {
    if (!addIn.value.trim()) return;
    try {
      await api("/memory/items", { method: "POST", body: { text: addIn.value.trim(), category: cat.value || null } });
      addIn.value = "";
      load();
    } catch (e) { toast(e.message); }
  };
  bakBtn.onclick = async () => {
    const r = await api("/memory/backup", { method: "POST" });
    toast(`Memory backed up: ${r.backup_file}`);
  };
  host.replaceChildren(bar, list);
  await load();
}

async function renderHistoryTab(host) {
  const row = el("div", "inline-row");
  const qIn = el("input"); qIn.placeholder = "Search full on-disk conversation archive…";
  const btn = el("button", null, "Search History");
  const list = el("div", "l-list");
  async function run() {
    const r = await api(`/history/search?q=${encodeURIComponent(qIn.value.trim())}&limit=25`);
    list.replaceChildren(...(r.results?.length ? r.results.map((h) => kvCard(h.role.toUpperCase(), fmtTs(h.t), h.content)) : [el("p", "dim", "No archived conversation turns found.")]));
  }
  btn.onclick = run;
  row.append(qIn, btn);
  host.replaceChildren(row, list);
  await run();
}

async function renderFilesTab(host) {
  const roots = await api("/files/roots");
  const info = el("p", "dim", `Allowlisted roots: ${(roots.roots || []).join(" · ")}`);
  const row = el("div", "inline-row");
  const qIn = el("input"); qIn.placeholder = "Search filenames inside allowed roots…";
  const sBtn = el("button", null, "Search Files");
  const iBtn = el("button", "ghost", "Inspect Project");
  row.append(qIn, sBtn, iBtn);
  const out = el("div", "l-list");
  const preview = el("pre", "l-log");
  sBtn.onclick = async () => {
    const r = await api(`/files/search?q=${encodeURIComponent(qIn.value.trim())}`);
    out.replaceChildren(...(r.matches || []).map((f) => {
      const card = kvCard(f.name, `${f.size} B`, f.path);
      const rBtn = el("button", null, "Read");
      rBtn.onclick = async () => {
        const doc = await api(`/files/read?path=${encodeURIComponent(f.path)}`);
        preview.textContent = doc.content || "";
      };
      card.append(rBtn);
      return card;
    }));
  };
  iBtn.onclick = async () => {
    const st = await api("/files/inspect");
    out.replaceChildren(kvCard("PROJECT SUMMARY", `${st.total_files} files`, Object.entries(st.languages || {}).map(([k, v]) => `${k}:${v}`).join("  ")));
  };
  host.replaceChildren(info, row, out, preview);
}

async function renderActionsTab(host) {
  const [pend, tasks] = await Promise.all([api("/actions/pending"), api("/tasks")]);
  const pWrap = el("div", "l-list");
  (pend.pending || []).forEach((p) => {
    const c = kvCard(p.action, p.level, p.description);
    const okBtn = el("button", null, "Approve");
    okBtn.onclick = async () => { try { await api(`/actions/${p.token}/confirm`, { method: "POST" }); renderWorkspace(); } catch (e) { toast(e.message); } };
    const noBtn = el("button", "ghost danger", "Reject");
    noBtn.onclick = async () => { try { await api(`/actions/${p.token}/reject`, { method: "POST" }); renderWorkspace(); } catch (e) { toast(e.message); } };
    const act = el("div", "l-actions");
    act.append(okBtn, noBtn);
    c.append(act);
    pWrap.append(c);
  });
  if (!pend.pending?.length) pWrap.append(el("p", "dim", "No sensitive actions pending user approval."));

  const tWrap = el("div", "l-list");
  (tasks.tasks || []).forEach((t) => {
    tWrap.append(kvCard(t.description || t.goal, t.status.toUpperCase(), (t.steps || []).join(" → ")));
  });
  host.replaceChildren(el("h2", null, "Pending Approvals"), pWrap, el("h2", null, "Tracked Tasks"), tWrap);
}

// Wire tab buttons cleanly
document.querySelectorAll("[data-ltab]").forEach((btn) => {
  btn.onclick = () => {
    currentTab = btn.dataset.ltab;
    document.querySelectorAll("[data-ltab]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    renderLearning();
  };
});
document.querySelectorAll("[data-wtab]").forEach((btn) => {
  btn.onclick = () => {
    currentWsTab = btn.dataset.wtab;
    document.querySelectorAll("[data-wtab]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    renderWorkspace();
  };
});

on("drawer", (d) => {
  clearInterval(refreshTimer);
  if (d === "learning") {
    renderLearning();
    refreshTimer = setInterval(() => !document.hidden && renderLearning(), 5000);
  } else if (d === "workspace") {
    renderWorkspace();
    refreshTimer = setInterval(() => !document.hidden && renderWorkspace(), 5000);
  }
});

// Expose quick open helpers
export { openDrawer, renderLearning, renderWorkspace };
