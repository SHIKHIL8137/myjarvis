import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";

interface SkillItem {
  skill_id: string;
  name: string;
  description: string;
  category: string;
  version: string;
  source_repo: string;
  source_commit: string;
  content_sha256: string;
  license: string;
  supported_runtimes: string[];
  compatible_roles: string[];
  required_permissions: string[];
  execution_mode: string;
  enabled: boolean;
  verification_status: string;
  last_used_at?: number | null;
  runtime_compat?: Record<string, { compatible: boolean; mode: string; reason: string }>;
  assignments?: Array<{ id: string; agent_role: string; priority: number }>;
  metrics?: {
    invocations: number;
    success_rate?: number;
    qa_improvement_delta?: number | null;
    quality_assessment?: string;
  };
}

interface CatalogAuditItem {
  source_id: string;
  repo: string;
  pinned_commit: string;
  license: string;
  tracked_files: number;
  supported_runtimes: string[];
  dependencies: string[];
  scripts_found: string[];
  security_findings: string[];
  jarvis_compatibility: string;
  recommendation: string;
  policy_verdict: string;
}

interface SkillAuditEntry {
  id: string;
  actor: string;
  decision: string;
  target: string;
  created_at: number;
}

const AGENT_ROLES = [
  "coder_agent",
  "architect_agent",
  "qa_agent",
  "research_agent",
  "document_producer",
  "assistant_coordinator",
];

export const SkillsPanel: React.FC<{ onNotify: (msg: string) => void }> = ({ onNotify }) => {
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [catalog, setCatalog] = useState<CatalogAuditItem[]>([]);
  const [audit, setAudit] = useState<SkillAuditEntry[]>([]);
  const [selected, setSelected] = useState<SkillItem | null>(null);
  const [history, setHistory] = useState<Array<{ id: number; version: string; action: string; content_sha256: string; created_at: number }>>([]);
  const [srcDir, setSrcDir] = useState("");
  const [category, setCategory] = useState("general");
  const [assignRole, setAssignRole] = useState("coder_agent");
  const [validationPreview, setValidationPreview] = useState<Record<string, unknown> | null>(null);
  const [taskPreview, setTaskPreview] = useState("");
  const [previewResult, setPreviewResult] = useState<Record<string, unknown> | null>(null);

  const refresh = async () => {
    try {
      const res = await apiFetch<{ skills: SkillItem[]; catalog: CatalogAuditItem[]; audit: SkillAuditEntry[] }>("/v2/skills");
      setSkills(res.skills || []);
      setCatalog(res.catalog || []);
      setAudit(res.audit || []);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const inspectDetails = async (sk: SkillItem) => {
    setSelected(sk);
    try {
      const res = await apiFetch<{ history: typeof history }>(`/v2/skills/${sk.skill_id}/history`);
      setHistory(res.history || []);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  const handleValidate = async () => {
    if (!srcDir.trim()) return;
    try {
      const res = await apiFetch<Record<string, unknown>>("/v2/skills/validate", {
        method: "POST",
        body: { source_dir: srcDir.trim(), category },
      });
      setValidationPreview(res);
      onNotify("Validation report ready. Review before confirming installation.");
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  const handleInstall = async () => {
    if (!srcDir.trim()) return;
    if (!window.confirm(`Confirm installing skill from "${srcDir.trim()}" under category "${category}"? Scripts remain quarantined unless explicitly permitted.`)) {
      return;
    }
    try {
      await apiFetch("/v2/skills/install", {
        method: "POST",
        body: { source_dir: srcDir.trim(), category, approved: true },
      });
      setSrcDir("");
      setValidationPreview(null);
      onNotify("Skill validated, pinned, and installed.");
      refresh();
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  return (
    <div className="scroll-body">
      <div className="card">
        <h3>VALIDATE & INSTALL SKILL FROM LOCAL / AUDITED PATH</h3>
        <div className="grid-2" style={{ marginBottom: 8 }}>
          <input value={srcDir} onChange={(e) => setSrcDir(e.target.value)} placeholder="Local skill directory containing SKILL.md" />
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="general">general</option>
            <option value="security_audit">security_audit</option>
            <option value="testing_api">testing_api</option>
            <option value="code_review">code_review</option>
            <option value="debugging">debugging</option>
            <option value="architecture">architecture</option>
            <option value="refactoring">refactoring</option>
            <option value="react_frontend">react_frontend</option>
            <option value="ui_review">ui_review</option>
            <option value="technical_explanation">technical_explanation</option>
          </select>
        </div>
        <div className="row" style={{ marginBottom: 8 }}>
          <button className="ghost" onClick={handleValidate}>Validate Security & Checksum</button>
          <button onClick={handleInstall}>Confirm & Install Skill</button>
        </div>
        {validationPreview && <pre className="diff">{JSON.stringify(validationPreview, null, 2)}</pre>}
      </div>

      <div className="card">
        <h3>INSTALLED SKILLS & CAPABILITY ASSIGNMENTS ({skills.length})</h3>
        {skills.map((sk) => (
          <div key={sk.skill_id} style={{ padding: "8px 0", borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
            <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
              <div>
                <strong>{sk.name}</strong> <span className="dim">({sk.skill_id} v{sk.version})</span>{" "}
                <span className="pill">{sk.category}</span>{" "}
                <span className={`pill ${sk.verification_status === "verified" ? "ok" : sk.verification_status === "blocked" ? "danger" : "warn"}`}>
                  {sk.verification_status}
                </span>{" "}
                <span className={`pill ${sk.enabled ? "ok" : "danger"}`}>{sk.enabled ? "ENABLED" : "DISABLED"}</span>
              </div>
              <div className="row" style={{ flexWrap: "wrap" }}>
                <button className="ghost" onClick={() => inspectDetails(sk)}>Details</button>
                <button
                  className={sk.enabled ? "warn" : "ok"}
                  onClick={async () => {
                    await apiFetch(`/v2/skills/${sk.skill_id}/${sk.enabled ? "disable" : "enable"}`, { method: "POST" });
                    refresh();
                  }}
                >
                  {sk.enabled ? "Disable" : "Enable"}
                </button>
                <button
                  className="ghost"
                  onClick={async () => {
                    try {
                      await apiFetch(`/v2/skills/${sk.skill_id}/assign`, { method: "POST", body: { agent_role: assignRole, priority: 20 } });
                      onNotify(`Assigned ${sk.skill_id} -> ${assignRole}`);
                      refresh();
                    } catch (e) {
                      onNotify((e as Error).message);
                    }
                  }}
                >
                  Assign ({assignRole})
                </button>
                <button
                  className="danger"
                  onClick={async () => {
                    if (!window.confirm(`Uninstall skill "${sk.skill_id}" permanently?`)) return;
                    await apiFetch(`/v2/skills/${sk.skill_id}`, { method: "DELETE" });
                    if (selected?.skill_id === sk.skill_id) setSelected(null);
                    refresh();
                  }}
                >
                  Uninstall
                </button>
              </div>
            </div>
            <div className="dim" style={{ marginTop: 4 }}>{sk.description}</div>
            <div className="dim" style={{ fontSize: 12, marginTop: 2 }}>
              License: {sk.license} | Runtimes: {sk.supported_runtimes.join(", ") || "none"} | Mode: {sk.execution_mode} | Perms: {sk.required_permissions.join(", ") || "none"}
            </div>
            <div className="dim" style={{ fontSize: 12 }}>
              SHA-256: <code>{sk.content_sha256.slice(0, 16)}…</code> | Assigned: {(sk.assignments || []).map((a) => a.agent_role).join(", ") || "auto-role"} | Uses: {sk.metrics?.invocations ?? 0} ({sk.metrics?.quality_assessment || "inconclusive"})
              {sk.last_used_at ? ` | Last used: ${new Date(sk.last_used_at * 1000).toLocaleString()}` : ""}
            </div>
          </div>
        ))}
        {!skills.length && <p className="dim">No skills installed yet. Inspect and approve from a validated local folder or the audited catalog below.</p>}
        <div className="row" style={{ marginTop: 8 }}>
          <span className="dim">Target Role for Assignment:</span>
          <select value={assignRole} onChange={(e) => setAssignRole(e.target.value)}>
            {AGENT_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </div>
      </div>

      {selected && (
        <div className="card">
          <h3>SKILL DETAILS & VERSION HISTORY: {selected.skill_id}</h3>
          <div className="row" style={{ marginBottom: 8 }}>
            <button
              className="ghost"
              onClick={async () => {
                if (!window.confirm(`Roll back "${selected.skill_id}" to its previous verified snapshot?`)) return;
                try {
                  await apiFetch(`/v2/skills/${selected.skill_id}/rollback`, { method: "POST" });
                  onNotify("Rolled back cleanly.");
                  refresh();
                  inspectDetails(selected);
                } catch (e) {
                  onNotify((e as Error).message);
                }
              }}
            >
              Rollback Previous Version
            </button>
            <button className="ghost" onClick={() => setSelected(null)}>Close Details</button>
          </div>
          <pre className="diff">{JSON.stringify({ manifest: selected, history }, null, 2)}</pre>
        </div>
      )}

      <div className="card">
        <h3>AGENT CAPABILITY ROUTER PREVIEW</h3>
        <div className="row" style={{ marginBottom: 8 }}>
          <input value={taskPreview} onChange={(e) => setTaskPreview(e.target.value)} placeholder="Describe a task to test dynamic skill selection…" />
          <button
            onClick={async () => {
              if (!taskPreview.trim()) return;
              const r = await apiFetch<Record<string, unknown>>("/v2/skills/select-preview", {
                method: "POST",
                body: { task_prompt: taskPreview.trim(), agent_role: assignRole, department: "dev", runtime: "jarvis_native" },
              });
              setPreviewResult(r);
            }}
          >
            Preview Routing
          </button>
        </div>
        {previewResult && <pre className="diff">{JSON.stringify(previewResult, null, 2)}</pre>}
      </div>

      <div className="card">
        <h3>AUDITED EXTERNAL SKILL SOURCES ({catalog.length})</h3>
        {catalog.map((c) => (
          <div key={c.source_id} style={{ padding: "8px 0", borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
            <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
              <div>
                <strong>{c.source_id}</strong> <span className="pill">{c.license.toUpperCase()}</span>{" "}
                <span className={`pill ${c.policy_verdict === "do_not_install_framework" ? "danger" : c.policy_verdict.includes("approve") ? "ok" : "warn"}`}>
                  {c.policy_verdict}
                </span>
              </div>
              <span className="dim">Commit: <code>{c.pinned_commit.slice(0, 12)}</code> ({c.tracked_files} files)</span>
            </div>
            <div className="dim" style={{ fontSize: 12, marginTop: 2 }}>{c.repo}</div>
            <div style={{ marginTop: 4 }}>{c.recommendation}</div>
            <div className="dim" style={{ fontSize: 12, marginTop: 2 }}>
              Compatibility: {c.jarvis_compatibility}
            </div>
            {c.security_findings.length > 0 && (
              <div className="dim" style={{ fontSize: 12, marginTop: 2 }}>Security Notes: {c.security_findings.join(" | ")}</div>
            )}
          </div>
        ))}
      </div>

      <div className="card">
        <h3>SKILL AUDIT HISTORY</h3>
        {audit.slice(0, 12).map((a) => (
          <div key={a.id} className="dim" style={{ fontSize: 12 }}>
            [{new Date(a.created_at * 1000).toLocaleTimeString()}] {a.decision} <strong>{a.target}</strong> by {a.actor}
          </div>
        ))}
      </div>
    </div>
  );
};

