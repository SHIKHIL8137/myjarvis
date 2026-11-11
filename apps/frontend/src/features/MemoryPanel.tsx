import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";

export const MemoryPanel: React.FC<{ onNotify: (msg: string) => void }> = ({ onNotify }) => {
  const [sub, setSub] = useState<"personal" | "experience" | "knowledge" | "learning">("personal");
  const [personal, setPersonal] = useState<Array<{ id: string; category: string; content: string }>>([]);
  const [experience, setExperience] = useState<Array<{ id: string; scope: string; trigger_context: string; lesson_after: string }>>([]);
  const [overview, setOverview] = useState<Record<string, unknown>>({});
  const [memInput, setMemInput] = useState("");
  const [memCat, setMemCat] = useState("explicit");

  const refresh = async () => {
    try {
      const [p, ex, ov] = await Promise.all([
        apiFetch<{ items: typeof personal }>("/v2/memory/personal"),
        apiFetch<{ items: typeof experience }>("/v2/memory/experience"),
        apiFetch<Record<string, unknown>>("/learning/overview"),
      ]);
      setPersonal(p.items || []);
      setExperience(ex.items || []);
      setOverview(ov);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="scroll-body">
      <div className="row">
        <button className={sub === "personal" ? "" : "ghost"} onClick={() => setSub("personal")}>Personal</button>
        <button className={sub === "experience" ? "" : "ghost"} onClick={() => setSub("experience")}>Experience</button>
        <button className={sub === "knowledge" ? "" : "ghost"} onClick={() => setSub("knowledge")}>Knowledge Base</button>
        <button className={sub === "learning" ? "" : "ghost"} onClick={() => setSub("learning")}>Training & Registry</button>
        <button className="ghost" onClick={async () => { const r = await apiFetch<{ imported: Record<string, number> }>("/v2/memory/migrate-legacy", { method: "POST" }); onNotify(`Imported: ${JSON.stringify(r.imported)}`); refresh(); }}>
          Sync Legacy → DB
        </button>
      </div>

      {sub === "personal" && (
        <div className="card">
          <h3>PERSONAL MEMORY (USER FACTS & PREFERENCES)</h3>
          <div className="row" style={{ marginBottom: 8 }}>
            <select value={memCat} onChange={(e) => setMemCat(e.target.value)} style={{ width: 150 }}>
              <option value="explicit">explicit</option>
              <option value="preference">preference</option>
              <option value="temporary">temporary</option>
            </select>
            <input value={memInput} onChange={(e) => setMemInput(e.target.value)} placeholder="Save personal preference or fact…" />
            <button onClick={async () => { if (!memInput.trim()) return; await apiFetch("/v2/memory/personal", { method: "POST", body: { content: memInput, category: memCat } }); setMemInput(""); refresh(); }}>Save</button>
          </div>
          {personal.map((m) => (
            <div key={m.id} style={{ padding: "5px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
              <span className="pill">{m.category}</span> <span>{m.content}</span>
            </div>
          ))}
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

      {(sub === "knowledge" || sub === "learning") && (
        <div className="card">
          <h3>{sub === "knowledge" ? "EXTERNAL KNOWLEDGE INDEX" : "TRAINING REGISTRY & EVALUATION"}</h3>
          <pre className="diff">{JSON.stringify(overview, null, 2)}</pre>
        </div>
      )}
    </div>
  );
};

