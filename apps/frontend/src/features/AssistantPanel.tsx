import React, { useEffect, useState } from "react";
import { apiFetch } from "../api/client";

export const AssistantPanel: React.FC<{ onNotify: (msg: string) => void }> = ({ onNotify }) => {
  const [docs, setDocs] = useState<Array<{ name: string; ext: string; bytes: number }>>([]);
  const [reminders, setReminders] = useState<Array<{ id: string; title: string; due_at: number; status: string }>>([]);
  const [fmt, setFmt] = useState("pdf");
  const [docTitle, setDocTitle] = useState("Project Status Brief");
  const [docBody, setDocBody] = useState("Generated locally inside Jarvis sandbox.");
  const [remTitle, setRemTitle] = useState("");

  const refresh = async () => {
    try {
      const [d, r] = await Promise.all([
        apiFetch<{ documents: typeof docs }>("/v2/documents"),
        apiFetch<{ reminders: typeof reminders }>("/v2/reminders"),
      ]);
      setDocs(d.documents || []);
      setReminders(r.reminders || []);
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const createDoc = async () => {
    try {
      const payload =
        fmt === "pptx"
          ? { slides: [{ title: docTitle, bullets: docBody.split("\n").filter(Boolean) }] }
          : fmt === "xlsx"
            ? { sheets: [{ name: "Summary", rows: [["Title", "Notes"], [docTitle, docBody]] }] }
            : { sections: [{ heading: "Summary", body: docBody }] };
      const res = await apiFetch<{ name: string }>("/v2/documents/generate", {
        method: "POST",
        body: { format: fmt, filename: docTitle.toLowerCase().replace(/\s+/g, "-"), title: docTitle, payload },
      });
      onNotify(`Generated ${res.name}`);
      refresh();
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  const addReminder = async () => {
    if (!remTitle.trim()) return;
    try {
      await apiFetch("/v2/reminders", {
        method: "POST",
        body: { title: remTitle.trim(), due_at: Math.floor(Date.now() / 1000) + 3600 },
      });
      setRemTitle("");
      refresh();
    } catch (e) {
      onNotify((e as Error).message);
    }
  };

  return (
    <div className="scroll-body">
      <div className="card">
        <h3>DOCUMENT ENGINE (PDF / DOCX / PPTX / XLSX)</h3>
        <div className="grid-2" style={{ marginBottom: 8 }}>
          <select value={fmt} onChange={(e) => setFmt(e.target.value)}>
            <option value="pdf">PDF (ReportLab)</option>
            <option value="docx">Word (.docx)</option>
            <option value="pptx">PowerPoint (.pptx)</option>
            <option value="xlsx">Excel (.xlsx)</option>
          </select>
          <input value={docTitle} onChange={(e) => setDocTitle(e.target.value)} placeholder="Document title" />
        </div>
        <textarea rows={3} value={docBody} onChange={(e) => setDocBody(e.target.value)} placeholder="Content / bullets" />
        <div className="row" style={{ marginTop: 8 }}>
          <button onClick={createDoc}>Generate Document</button>
        </div>
        <div style={{ marginTop: 10 }}>
          {docs.map((d) => (
            <div key={d.name} className="row" style={{ justifyContent: "space-between", padding: "4px 0" }}>
              <span className="dim">{d.name} ({d.bytes} B)</span>
              <a href={`/api/v2/documents/download/${encodeURIComponent(d.name)}`} target="_blank" rel="noreferrer">
                <button className="ghost">Download</button>
              </a>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h3>REMINDERS (+1h QUICK SCHEDULE)</h3>
        <div className="row" style={{ marginBottom: 8 }}>
          <input value={remTitle} onChange={(e) => setRemTitle(e.target.value)} placeholder="Remind me to review architecture…" />
          <button onClick={addReminder}>Schedule</button>
        </div>
        {reminders.map((r) => (
          <div key={r.id} className="row" style={{ justifyContent: "space-between", padding: "4px 0" }}>
            <span>{r.title} <span className="dim">[{r.status}]</span></span>
            {r.status !== "completed" && (
              <button className="ok" onClick={async () => { await apiFetch(`/v2/reminders/${r.id}/complete`, { method: "POST" }); refresh(); }}>
                Done
              </button>
            )}
          </div>
        ))}
        {!reminders.length && <p className="dim">No scheduled reminders.</p>}
      </div>
    </div>
  );
};

