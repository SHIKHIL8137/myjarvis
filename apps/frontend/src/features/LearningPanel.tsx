import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";

export const LearningPanel: React.FC<{ onNotify: (msg: string) => void }> = ({ onNotify }) => {
  const [overview, setOverview] = useState<Record<string, unknown>>({});
  const [sources, setSources] = useState<Array<{ id: string; name: string; kind: string; license: string; state?: { active_version?: number } }>>([]);
  const [jobs, setJobs] = useState<Array<{ job_id: string; state: string; approved: boolean }>>([]);
  const [versions, setVersions] = useState<Array<{ version_id: string; base_model: string; status: string; evaluation_passed: boolean }>>([]);

  const refresh = async () => {
    try {
      const ov = await apiFetch<Record<string, unknown>>("/learning/overview");
      setOverview(ov);
      setSources((ov.knowledge_sources as typeof sources) || []);
      setJobs((ov.training_jobs as typeof jobs) || []);
      const reg = (ov.registry as { versions?: typeof versions }) || {};
      setVersions(reg.versions || []);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="scroll-body">
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3>KNOWLEDGE BASE & RAG UPDATES (SEPARATE FROM MODEL TRAINING)</h3>
          <button onClick={async () => { try { await apiFetch("/learning/knowledge/sync", { method: "POST", body: {} }); onNotify("Knowledge sync finished"); refresh(); } catch (e) { onNotify((e as Error).message); } }}>Sync Knowledge</button>
        </div>
        {sources.map((s) => (
          <div key={s.id} style={{ padding: "5px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
            <strong>{s.name}</strong> <span className="pill">{s.kind}</span> <span className="pill ok">lic: {s.license}</span>{" "}
            <span className="dim">v{s.state?.active_version ?? 0}</span>
          </div>
        ))}
        {!sources.length && <p className="dim">No knowledge sources registered yet.</p>}
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3>MODEL TRAINING JOBS & EVALUATION GATE</h3>
          <button className="ghost" onClick={refresh}>Refresh</button>
        </div>
        {jobs.map((j) => (
          <div key={j.job_id} className="row" style={{ justifyContent: "space-between", padding: "5px 0" }}>
            <span><strong>{j.job_id}</strong> <span className="pill">{j.state}</span></span>
            {!j.approved && (
              <button className="ok" onClick={async () => { await apiFetch(`/learning/training/jobs/${j.job_id}/approve`, { method: "POST", body: {} }); refresh(); }}>Approve Job</button>
            )}
          </div>
        ))}
        {!jobs.length && <p className="dim">No training jobs scheduled (auto_training disabled by default).</p>}
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3>MODEL VERSION REGISTRY & ROLLBACK</h3>
          <button className="danger" onClick={async () => { try { await apiFetch("/learning/registry/rollback", { method: "POST", body: {} }); onNotify("Rolled back model version"); refresh(); } catch (e) { onNotify((e as Error).message); } }}>Rollback Active</button>
        </div>
        {versions.map((v) => (
          <div key={v.version_id} className="row" style={{ justifyContent: "space-between", padding: "5px 0" }}>
            <div>
              <strong>{v.version_id}</strong> <span className="dim">({v.base_model})</span>{" "}
              <span className={`pill ${v.status === "baseline" || v.status === "promoted" ? "ok" : "warn"}`}>{v.status}</span>
            </div>
            <div className="row">
              <span className={`pill ${v.evaluation_passed ? "ok" : "danger"}`}>eval:{v.evaluation_passed ? "PASS" : "FAIL"}</span>
              {v.evaluation_passed && v.status === "candidate" && (
                <button className="ok" onClick={async () => { await apiFetch(`/learning/registry/${v.version_id}/promote`, { method: "POST", body: {} }); refresh(); }}>Promote</button>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>LEARNING POLICY & EVALUATION SNAPSHOT</h3>
        <pre className="diff">{JSON.stringify({ policy: overview.policy, knowledge: overview.knowledge, latest_evaluation: overview.latest_evaluation }, null, 2)}</pre>
      </div>
    </div>
  );
};

