import React, { useState } from "react";
import { apiFetch } from "../api/client";
import { classifyTranscriptStatus } from "../speechStatus";

export interface VoiceAttemptItem {
  provider?: string;
  model?: string;
  language?: string | null;
  quality?: string;
  quality_tier?: string;
  confidence?: number | null;
  confidence_available?: boolean;
  language_probability?: number | null;
  score?: number;
  detected_language?: string;
  text?: string;
  reason?: string;
  flags?: string[];
  error?: string;
}

export interface VoiceTranscriptItem {
  id: string;
  audio_id?: string | null;
  original_transcript: string;
  corrected_transcript: string;
  detected_language: string;
  source_language?: string;
  language_probability?: number | null;
  language_probability_available?: boolean;
  confidence?: number | null;
  confidence_available?: boolean;
  translated_text?: string | null;
  target_language?: string;
  translation_engine?: string | null;
  model?: string;
  recognition_quality?: string;
  review_needed?: boolean;
  seconds?: number;
  created_at?: number;
  status?: string;
  quality_flags?: string[];
  selection_reason?: string;
  selected_candidate?: {
    provider?: string;
    model?: string;
    reason?: string;
    score?: number;
    quality_tier?: string;
  } | null;
  attempts?: VoiceAttemptItem[];
  provider_status?: {
    requested?: string;
    available?: boolean;
    state?: string;
    reason?: string;
    expected_dir?: string;
    expected_dirs?: string[];
  };
  provider_errors?: Array<{
    provider?: string;
    model?: string;
    error?: string;
  }>;
  audio_diagnostics?: {
    sample_rate?: number;
    channels?: number;
    duration_s?: number;
    rms_level?: number;
    snr_db?: number;
    clipping_percent?: number;
    clipped?: boolean;
    trimmed?: boolean;
    profile?: string;
    warnings?: string[];
  };
  translation_verification?: {
    passed?: boolean;
    uncertain?: boolean;
    warnings?: string[];
    score?: number;
    structural_check?: boolean;
    semantic_equivalence_proven?: boolean;
    previous_translation_preserved?: boolean;
    check_method?: string;
    note?: string;
  } | null;
  eval_consent?: boolean;
}

interface SpeechInspectorPanelProps {
  records: VoiceTranscriptItem[];
  onClose: () => void;
  onRecordsChange: (updater: (prev: VoiceTranscriptItem[]) => VoiceTranscriptItem[]) => void;
  onNotify: (msg: string) => void;
}

const LANG_OPTIONS = [
  ["auto", "Auto"],
  ["en", "English"],
  ["ml", "Malayalam"],
  ["mixed", "ML+EN Mixed"],
  ["hi", "Hindi"],
  ["ta", "Tamil"],
];

const MODEL_OPTIONS = ["small.en", "medium.en", "small", "base", "tiny", "malayalam_whisper", "indic_conformer"];
const QUALITY_OPTIONS = ["accurate", "high", "balanced", "fast"];
const PREP_OPTIONS = ["light", "standard", "off"];

function computeLevenshtein(a: string[], b: string[]): number {
  const m = a.length;
  const n = b.length;
  const d: number[][] = Array.from({ length: m + 1 }, () => Array(n + 1).fill(0));
  for (let i = 0; i <= m; i++) d[i][0] = i;
  for (let j = 0; j <= n; j++) d[0][j] = j;
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      const cost = a[i - 1].toLowerCase() === b[j - 1].toLowerCase() ? 0 : 1;
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost);
    }
  }
  return d[m][n];
}

function calculateMetrics(ref: string, hyp: string): { wer: number; cer: number } | null {
  const cleanRef = ref.trim();
  const cleanHyp = hyp.trim();
  if (!cleanRef || cleanRef === cleanHyp) return { wer: 0, cer: 0 };
  const refWords = cleanRef.split(/\s+/).filter(Boolean);
  const hypWords = cleanHyp.split(/\s+/).filter(Boolean);
  const wordDist = computeLevenshtein(refWords, hypWords);
  const wer = refWords.length > 0 ? (wordDist / refWords.length) * 100 : 0;

  const refChars = cleanRef.replace(/\s+/g, "").split("");
  const hypChars = cleanHyp.replace(/\s+/g, "").split("");
  const charDist = computeLevenshtein(refChars, hypChars);
  const cer = refChars.length > 0 ? (charDist / refChars.length) * 100 : 0;
  return { wer: Math.round(wer * 10) / 10, cer: Math.round(cer * 10) / 10 };
}

export const SpeechInspectorPanel: React.FC<SpeechInspectorPanelProps> = ({
  records,
  onClose,
  onRecordsChange,
  onNotify,
}) => {
  const [selectedId, setSelectedId] = useState<string>("");
  const [draftCorrection, setDraftCorrection] = useState<string>("");
  const [draftTargetLang, setDraftTargetLang] = useState<string>("none");
  const [retryModel, setRetryModel] = useState<string>("small");
  const [retryLang, setRetryLang] = useState<string>("auto");
  const [retryQuality, setRetryQuality] = useState<string>("high");
  const [retryPrep, setRetryPrep] = useState<string>("light");
  const [retryTarget, setRetryTarget] = useState<string>("none");
  const [busy, setBusy] = useState<boolean>(false);
  const [showAttempts, setShowAttempts] = useState<boolean>(false);

  const active = records.find((r) => r.id === selectedId) || records[0] || null;

  const selectRecord = (rec: VoiceTranscriptItem) => {
    setSelectedId(rec.id);
    setDraftCorrection(rec.corrected_transcript || rec.original_transcript);
    setDraftTargetLang(rec.target_language || "none");
    setRetryLang(rec.detected_language === "unknown" ? "auto" : rec.detected_language || "auto");
    setRetryTarget(rec.target_language || "none");
    setShowAttempts(false);
  };

  const copyText = async (label: string, txt: string) => {
    try {
      await navigator.clipboard.writeText(txt);
      onNotify(`${label} copied.`);
    } catch {
      onNotify("Clipboard unavailable.");
    }
  };

  const saveCorrection = async () => {
    if (!active) return;
    const textToSave = (draftCorrection || active.corrected_transcript || active.original_transcript).trim();
    if (!textToSave) return;
    setBusy(true);
    try {
      const updated = await apiFetch<VoiceTranscriptItem>("/voice/correct", {
        method: "POST",
        body: {
          id: active.id,
          corrected_transcript: textToSave,
          target_language: draftTargetLang,
        },
      });
      onRecordsChange((prev) => [updated, ...prev.filter((r) => r.id !== updated.id)]);
      setDraftCorrection(updated.corrected_transcript);
      onNotify("Correction saved (original preserved).");
    } catch (e) {
      onNotify((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const runRetry = async () => {
    if (!active?.audio_id) {
      onNotify("Original audio buffer not available for this record.");
      return;
    }
    setBusy(true);
    try {
      const res = await apiFetch<{ transcript_record: VoiceTranscriptItem }>("/voice/retry", {
        method: "POST",
        body: {
          audio_id: active.audio_id,
          transcript_id: active.id,
          model: retryModel,
          language: retryLang,
          quality: retryQuality,
          preprocessing_profile: retryPrep,
          target_language: retryTarget === "none" ? undefined : retryTarget,
        },
      });
      if (res.transcript_record) {
        const nextRec = res.transcript_record;
        onRecordsChange((prev) => [nextRec, ...prev.filter((r) => r.id !== nextRec.id)]);
        selectRecord(nextRec);
        onNotify(`Re-transcribed with ${nextRec.model} (${nextRec.detected_language}).`);
      }
    } catch (e) {
      onNotify((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveConsentSample = async () => {
    if (!active) return;
    setBusy(true);
    try {
      const res = await apiFetch<{ ok: boolean; exported_path?: string | null }>("/voice/eval-consent", {
        method: "POST",
        body: { id: active.id, consent: true, include_audio: true },
      });
      if (res.ok) {
        onRecordsChange((prev) =>
          prev.map((r) => (r.id === active.id ? { ...r, eval_consent: true } : r))
        );
        onNotify("Saved consented evaluation sample locally.");
      }
    } catch (e) {
      onNotify((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const deleteRecord = async (recId: string) => {
    setBusy(true);
    try {
      await apiFetch(`/voice/transcripts/${recId}?delete_audio=true`, { method: "DELETE" });
      onRecordsChange((prev) => prev.filter((r) => r.id !== recId));
      onNotify("Transcript and audio deleted.");
    } catch (e) {
      if ((e as Error).message !== "Cancelled.") onNotify((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const diag = active?.audio_diagnostics;
  const confAvailable = active?.confidence != null && active?.confidence_available !== false;
  const confPct = confAvailable ? Math.round((active?.confidence || 0) * 100) : null;
  const recQuality = active?.recognition_quality || "medium";
  const statusSummary = classifyTranscriptStatus(active);
  const qualityBadge = { label: statusSummary.qualityLabel, cls: statusSummary.qualityTone };
  const currentDraft =
    selectedId === active?.id ? draftCorrection : active?.corrected_transcript || active?.original_transcript || "";

  return (
    <div className="contextual-tasks-panel" aria-label="Speech and Translation Inspector">
      <div className="row between" style={{ marginBottom: 8 }}>
        <div className="row wrap">
          <span className="eyebrow">Speech &amp; Translation Inspector</span>
          <span className="chip static">{records.length} recent</span>
        </div>
        <button className="ghost icon" onClick={onClose} aria-label="Close speech inspector">✕</button>
      </div>

      {!active ? (
        <p className="dim">No voice transcripts recorded yet. Speak via Host Mic or browser Speak/PTT.</p>
      ) : (
        <div className="speech-inspector-body" style={{ display: "grid", gap: 8 }}>
          {records.length > 1 && (
            <div className="row wrap" style={{ gap: 6 }}>
              {records.slice(0, 6).map((r) => {
                const rConfKnown = r.confidence != null && r.confidence_available !== false;
                const rConfLabel = rConfKnown ? `${Math.round((r.confidence || 0) * 100)}%` : "Conf N/A";
                return (
                  <button
                    key={r.id}
                    className={r.id === active.id ? "active" : "ghost"}
                    onClick={() => selectRecord(r)}
                    style={{ fontSize: 11.5, padding: "4px 8px" }}
                  >
                    {r.detected_language.toUpperCase()} · {rConfLabel} · {(r.corrected_transcript || r.original_transcript || "(empty)").slice(0, 22)}
                  </button>
                );
              })}
            </div>
          )}

          <div className="row wrap between" style={{ gap: 6 }}>
            <div className="row wrap" style={{ gap: 6 }}>
              <span className="pill info">Lang: {active.detected_language}</span>
              <span className={`pill ${qualityBadge.cls}`}>{qualityBadge.label}</span>
              {confPct !== null ? (
                <span className={`pill ${confPct >= 75 ? "ok" : confPct >= 50 ? "warn" : "danger"}`}>
                  Conf: {confPct}%
                </span>
              ) : (
                <span className="pill warn">Conf: unavailable</span>
              )}
              {active.model && <span className="chip static">{active.model} · {recQuality}</span>}
              {active.status === "uncertain" && <span className="pill warn">Uncertain</span>}
              {active.provider_status?.available === false && (
                <span className="pill warn">
                  Provider unavailable: {active.provider_status.requested || "specialist"}
                </span>
              )}
              {active.quality_flags?.map((f) => (
                <span key={f} className="pill warn">{f}</span>
              ))}
            </div>
            <button
              className="ghost"
              disabled={busy}
              onClick={() => deleteRecord(active.id)}
              style={{ fontSize: 11 }}
            >
              Delete Audio &amp; Record
            </button>
          </div>

          {((active.provider_errors && active.provider_errors.length > 0) || active.provider_status?.available === false) && (
            <div className="card" style={{ padding: "6px 10px", borderColor: "var(--warn, #d97706)" }}>
              <small className="dim">Provider Status &amp; Fallback Diagnostics</small>
              <div className="mono" style={{ fontSize: 11.5, marginTop: 2 }}>
                {active.provider_status?.available === false && (
                  <div>
                    Unavailable ({active.provider_status.requested || "specialist"}): {active.provider_status.reason || "Not installed locally; fell back to Whisper."}
                    {active.provider_status.expected_dir ? ` · Expected dir: ${active.provider_status.expected_dir}` : ""}
                  </div>
                )}
                {active.provider_errors?.map((pe, idx) => (
                  <div key={idx}>Runtime error ({pe.provider || pe.model || "provider"}): {pe.error}</div>
                ))}
              </div>
            </div>
          )}

          {active.audio_id && (
            <div className="row wrap between" style={{ gap: 8, alignItems: "center" }}>
              <audio
                controls
                preload="none"
                src={`/api/voice/audio/${encodeURIComponent(active.audio_id)}`}
                style={{ height: 32, flex: "1 1 220px" }}
              />
              {diag && (
                <span className="dim mono" style={{ fontSize: 11 }}>
                  {diag.sample_rate || 16000}Hz {diag.channels || 1}ch · SNR {diag.snr_db ?? "—"} dB · {diag.profile || "light"}
                  {diag.clipped ? " · CLIPPED" : ""}
                  {diag.trimmed ? " · trimmed" : ""}
                </span>
              )}
            </div>
          )}

          <div className="card" style={{ padding: "8px 10px" }}>
            <div className="row between">
              <small className="dim">Original Transcript (immutable)</small>
              <button className="ghost" style={{ fontSize: 11, padding: "2px 6px" }} onClick={() => copyText("Original transcript", active.original_transcript)}>
                Copy Original
              </button>
            </div>
            <p className="mono" style={{ fontSize: 12.5, margin: "4px 0 0" }}>
              {active.original_transcript || "(empty)"}
            </p>
          </div>

          <div className="card" style={{ padding: "8px 10px" }}>
            <div className="row between" style={{ marginBottom: 4 }}>
              <div className="row" style={{ gap: 6 }}>
                <small className="dim">Corrected / Verified Reference</small>
                {active.original_transcript && currentDraft && currentDraft !== active.original_transcript && (() => {
                  const m = calculateMetrics(currentDraft, active.original_transcript);
                  return m ? (
                    <span className={`pill ${m.wer === 0 ? "ok" : m.wer <= 20 ? "warn" : "danger"}`} style={{ fontSize: 10 }}>
                      WER: {m.wer}% · CER: {m.cer}% vs Original
                    </span>
                  ) : null;
                })()}
              </div>
              <button className="ghost" style={{ fontSize: 11, padding: "2px 6px" }} onClick={() => copyText("Corrected transcript", currentDraft)}>
                Copy Corrected
              </button>
            </div>
            <textarea
              rows={2}
              value={currentDraft}
              onChange={(e) => {
                if (selectedId !== active.id) setSelectedId(active.id);
                setDraftCorrection(e.target.value);
              }}
              style={{ width: "100%", fontSize: 12.5 }}
            />
            <div className="row wrap between" style={{ marginTop: 6, gap: 6 }}>
              <div className="row wrap" style={{ gap: 6 }}>
                <select
                  aria-label="Target translation language"
                  value={selectedId === active.id ? draftTargetLang : active.target_language || "none"}
                  onChange={(e) => {
                    if (selectedId !== active.id) setSelectedId(active.id);
                    setDraftTargetLang(e.target.value);
                  }}
                >
                  <option value="none">No translation</option>
                  <option value="en">Translate → English</option>
                  <option value="ml">Translate → Malayalam</option>
                  <option value="hi">Translate → Hindi</option>
                  <option value="ta">Translate → Tamil</option>
                </select>
                <button disabled={busy} onClick={saveCorrection}>Save Correction</button>
              </div>
              <div className="row wrap" style={{ gap: 6 }}>
                <button
                  className="ghost"
                  disabled={busy}
                  onClick={() => {
                    setDraftCorrection(`[INCORRECT] ${active.original_transcript}`);
                    saveCorrection();
                  }}
                  title="Mark transcript as inaccurate"
                  style={{ fontSize: 11 }}
                >
                  Mark Inaccurate
                </button>
                <button
                  className="ghost"
                  disabled={busy || Boolean(active.eval_consent)}
                  onClick={async () => {
                    await saveCorrection();
                    await saveConsentSample();
                  }}
                  title="Save verified ground truth reference for local STT benchmarks"
                  style={{ fontSize: 11 }}
                >
                  {active.eval_consent ? "✓ Verified Benchmark Ref" : "Save as Benchmark Ref"}
                </button>
              </div>
            </div>
          </div>

          {active.translated_text && (
            <div className="card" style={{ padding: "8px 10px" }}>
              <div className="row wrap between" style={{ gap: 6 }}>
                <small className="dim">
                  Translated ({active.target_language}) · {active.translation_engine || "qwen"}
                </small>
                <div className="row wrap" style={{ gap: 6 }}>
                  {active.translation_verification?.previous_translation_preserved && (
                    <span className="pill warn">Previous valid translation preserved</span>
                  )}
                  {active.translation_verification && (
                    <span
                      className={`pill ${active.translation_verification.uncertain ? "warn" : "info"}`}
                      title={active.translation_verification.note || "Structural anchor check only"}
                    >
                      {active.translation_verification.uncertain
                        ? `Translation uncertain: ${(active.translation_verification.warnings || []).join(", ")}`
                        : "Structural check passed (not semantic proof)"}
                    </span>
                  )}
                </div>
              </div>
              <p style={{ margin: "4px 0 0", fontSize: 13 }}>{active.translated_text}</p>
              {active.translation_verification?.note && (
                <small className="dim" style={{ display: "block", marginTop: 4 }}>
                  {active.translation_verification.note}
                </small>
              )}
            </div>
          )}

          {active.audio_id && (
            <div className="card" style={{ padding: "8px 10px" }}>
              <div className="row between" style={{ marginBottom: 6 }}>
                <small className="dim">Re-run Transcription on Original Audio</small>
                {active.attempts && active.attempts.length > 0 && (
                  <button className="ghost" style={{ fontSize: 11, padding: "2px 6px" }} onClick={() => setShowAttempts((s) => !s)}>
                    {showAttempts ? "Hide passes" : `Compare passes (${active.attempts.length})`}
                  </button>
                )}
              </div>
              <div className="row wrap" style={{ gap: 6 }}>
                <select aria-label="Retry model" value={retryModel} onChange={(e) => setRetryModel(e.target.value)}>
                  {MODEL_OPTIONS.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
                <select aria-label="Retry language" value={retryLang} onChange={(e) => setRetryLang(e.target.value)}>
                  {LANG_OPTIONS.map(([code, lbl]) => <option key={code} value={code}>{lbl}</option>)}
                </select>
                <select aria-label="Retry quality" value={retryQuality} onChange={(e) => setRetryQuality(e.target.value)}>
                  {QUALITY_OPTIONS.map((q) => <option key={q} value={q}>{q}</option>)}
                </select>
                <select aria-label="Retry preprocessing" value={retryPrep} onChange={(e) => setRetryPrep(e.target.value)}>
                  {PREP_OPTIONS.map((p) => <option key={p} value={p}>prep:{p}</option>)}
                </select>
                <select aria-label="Retry translate" value={retryTarget} onChange={(e) => setRetryTarget(e.target.value)}>
                  <option value="none">no translate</option>
                  <option value="en">→ en</option>
                  <option value="ml">→ ml</option>
                  <option value="hi">→ hi</option>
                  <option value="ta">→ ta</option>
                </select>
                <button disabled={busy} onClick={runRetry}>{busy ? "Running…" : "Retry Pass"}</button>
              </div>
              {showAttempts && active.attempts && (
                <div style={{ marginTop: 6, display: "grid", gap: 4 }}>
                  {active.selection_reason && (
                    <div className="dim mono" style={{ fontSize: 11 }}>
                      Winner reason: {active.selection_reason}
                      {active.selected_candidate
                        ? ` (${active.selected_candidate.provider || "fw"}/${active.selected_candidate.model || "?"}, score=${active.selected_candidate.score ?? "?"})`
                        : ""}
                    </div>
                  )}
                  {active.attempts.map((a, idx) => {
                    const aConfKnown = a.confidence != null && a.confidence_available !== false;
                    const aConfText = aConfKnown ? `${Math.round((a.confidence || 0) * 100)}%` : "unavailable";
                    return (
                      <div key={idx} className="dim mono" style={{ fontSize: 11 }}>
                        #{idx + 1} [{a.provider || "fw"}/{a.model}/{a.quality_tier || a.quality || "?"} {a.reason ? `(${a.reason}) ` : ""}lang={a.language || a.detected_language}] score={a.score} conf={aConfText}: {a.error ? `ERROR: ${a.error}` : a.text}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

