import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";

interface PersonalItem {
  id: string;
  category?: string;
  memory_type?: string;
  project_id?: string | null;
  source?: string;
  privacy_level?: string;
  confidence?: number;
  expires_at?: number | null;
  content: string;
  score?: number;
}

export const MemoryPanel: React.FC<{ onNotify: (msg: string) => void }> = ({ onNotify }) => {
  const [sub, setSub] = useState<"personal" | "experience" | "documents">("personal");
  const [personal, setPersonal] = useState<PersonalItem[]>([]);
  const [experience, setExperience] = useState<Array<{ id: string; scope: string; trigger_context: string; lesson_after: string }>>([]);
  const [summary, setSummary] = useState<Record<string, unknown>>({});
  const [memInput, setMemInput] = useState("");
  const [memCat, setMemCat] = useState("explicit");
  const [query, setQuery] = useState("");
  const [projFilter, setProjFilter] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");

  const refresh = async (qOverride?: string) => {
    try {
      const qVal = (qOverride ?? query).trim();
      const qs = new URLSearchParams();
      if (qVal) qs.set("q", qVal);
      if (projFilter.trim()) qs.set("project_id", projFilter.trim());
      const [p, ex, sum] = await Promise.all([
        apiFetch<{ items: PersonalItem[] }>(`/v2/memory/personal${qs.toString() ? "?" + qs.toString() : ""}`),
        apiFetch<{ items: typeof experience }>(`/v2/memory/experience${projFilter.trim() ? "?project_id=" + encodeURIComponent(projFilter.trim()) : ""}`),
        apiFetch<Record<string, unknown>>("/v2/memory/summary"),
      ]);
      setPersonal(p.items || []);
      setExperience(ex.items || []);
      setSummary(sum);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="scroll-body">
      <div className="row" style={{ flexWrap: "wrap" }}>
        <button className={sub === "personal" ? "" : "ghost"} onClick={() => setSub("personal")}>Personal & Project</button>
        <button className={sub === "experience" ? "" : "ghost"} onClick={() => setSub("experience")}>Experience Lessons</button>
        <button className={sub === "documents" ? "" : "ghost"} onClick={() => setSub("documents")}>5-Way Backend Info</button>
        <button className="ghost" onClick={async () => { const r = await apiFetch<{ sql_purged: number; vectors_purged: number }>("/v2/memory/purge-expired", { method: "POST" }); onNotify(`Purged ${r.sql_purged} SQL / ${r.vectors_purged} vectors`); refresh(); }}>
          Purge Expired
        </button>
        <button className="ghost" onClick={async () => { const r = await apiFetch<{ imported: Record<string, number> }>("/v2/memory/migrate-legacy", { method: "POST" }); onNotify(`Imported: ${JSON.stringify(r.imported)}`); refresh(); }}>
          Sync Legacy → DB
        </button>
      </div>

      {sub === "personal" && (
        <div className="card">
          <h3>PERSONAL & PROJECT MEMORY STORE</h3>
          <div className="row" style={{ marginBottom: 8 }}>
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search memory semantically…" />
            <input value={projFilter} onChange={(e) => setProjFilter(e.target.value)} placeholder="Project ID filter (optional)" style={{ width: 180 }} />
            <button className="ghost" onClick={() => refresh()}>Search</button>
          </div>
          <div className="row" style={{ marginBottom: 10 }}>
            <select value={memCat} onChange={(e) => setMemCat(e.target.value)} style={{ width: 140 }}>
              <option value="explicit">explicit</option>
              <option value="preference">preference</option>
              <option value="temporary">temporary</option>
            </select>
            <input value={memInput} onChange={(e) => setMemInput(e.target.value)} placeholder="Save personal preference or fact…" />
            <button onClick={async () => { if (!memInput.trim()) return; await apiFetch("/v2/memory/personal", { method: "POST", body: { content: memInput, category: memCat } }); setMemInput(""); refresh(); }}>Save</button>
          </div>
          {personal.map((m) => (
            <div key={m.id} style={{ padding: "6px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
              <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
                <div className="row">
                  <span className="pill">{m.category || m.memory_type || "memory"}</span>
                  {m.privacy_level && <span className="pill warn">{m.privacy_level}</span>}
                  {m.source && <span className="dim">src:{m.source}</span>}
                  {m.expires_at && <span className="dim">exp:{new Date(m.expires_at * 1000).toLocaleDateString()}</span>}
                  {m.score !== undefined && <span className="dim">score:{m.score}</span>}
                </div>
                <div className="row">
                  <button className="ghost" onClick={() => { setEditingId(m.id); setEditText(m.content); }}>Correct</button>
                  <button className="ghost" onClick={async () => { await apiFetch(`/v2/memory/personal/${m.id}/archive`, { method: "POST" }); refresh(); }}>Archive</button>
                  <button className="danger" onClick={async () => { await apiFetch(`/v2/memory/personal/${m.id}`, { method: "DELETE" }); refresh(); }}>Delete</button>
                </div>
              </div>
              {editingId === m.id ? (
                <div className="row" style={{ marginTop: 6 }}>
                  <input value={editText} onChange={(e) => setEditText(e.target.value)} />
                  <button className="ok" onClick={async () => { await apiFetch(`/v2/memory/personal/${m.id}`, { method: "PUT", body: { content: editText } }); setEditingId(null); refresh(); }}>Save Fix</button>
                  <button className="ghost" onClick={() => setEditingId(null)}>Cancel</button>
                </div>
              ) : (
                <div style={{ marginTop: 4 }}>{m.content}</div>
              )}
            </div>
          ))}
          {!personal.length && <p className="dim">No matching active memories.</p>}
        </div>
      )}

      {sub === "experience" && (
        <div className="card">
          <h3>EXPERIENCE & WORKFLOW LESSONS</h3>
          {experience.map((e) => (
            <div key={e.id} style={{ padding: "6px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
              <span className="pill ok">{e.scope}</span> <strong>{e.trigger_context}</strong>
              <div className="dim">Lesson: {e.lesson_after}</div>
            </div>
          ))}
          {!experience.length && <p className="dim">No experience lessons recorded yet.</p>}
        </div>
      )}

      {sub === "documents" && (
        <div className="card">
          <h3>5-WAY MEMORY & VECTOR BACKEND SUMMARY</h3>
          <pre className="diff">{JSON.stringify(summary, null, 2)}</pre>
        </div>
      )}
    </div>
  );
};
