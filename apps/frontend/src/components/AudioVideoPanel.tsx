import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  mapMediaError,
  queryBrowserPermissionState,
  safeEnumerateMediaDevices,
  stopMediaStream,
  type BrowserMediaInventory,
} from "../mediaDevices";

export interface HostAudioInventory {
  available?: boolean;
  input?: string | null;
  output?: string | null;
  inputs?: Array<{ index: number; name: string }>;
  outputs?: Array<{ index: number; name: string }>;
  error?: string | null;
}

interface AudioVideoPanelProps {
  hostAudio?: HostAudioInventory;
  onRefreshHostDevices?: () => Promise<void> | void;
  onNotify?: (msg: string) => void;
}

interface BoundaryProps {
  title: string;
  children: React.ReactNode;
}

interface BoundaryState {
  error: Error | null;
}

/**
 * Surfaces real render errors inside a Settings section with full message and Retry
 * instead of blanking the whole UI or silently hiding the failure.
 */
export class SettingsErrorBoundary extends React.Component<BoundaryProps, BoundaryState> {
  constructor(props: BoundaryProps) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  override componentDidCatch(error: Error, info: React.ErrorInfo): void {
    console.error(`[Settings:${this.props.title}]`, error, info.componentStack);
  }

  override render(): React.ReactNode {
    if (this.state.error) {
      return (
        <div className="card" role="alert">
          <h3>{this.props.title} failed to render</h3>
          <p className="err-text">{this.state.error.message || "Unexpected rendering error"}</p>
          <p className="dim" style={{ fontSize: 12, marginTop: 6 }}>
            Jarvis isolated this error so the rest of Settings stays usable. Check the browser console for full stack details.
          </p>
          <button
            className="ghost"
            style={{ marginTop: 10 }}
            onClick={() => this.setState({ error: null })}
          >
            Retry Section
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

export const AudioVideoPanel: React.FC<AudioVideoPanelProps> = ({
  hostAudio,
  onRefreshHostDevices,
  onNotify,
}) => {
  const [inventory, setInventory] = useState<BrowserMediaInventory>(() => ({
    supported: typeof navigator !== "undefined" && Boolean(navigator.mediaDevices),
    enumerateSupported:
      typeof navigator !== "undefined" && typeof navigator.mediaDevices?.enumerateDevices === "function",
    getUserMediaSupported:
      typeof navigator !== "undefined" && typeof navigator.mediaDevices?.getUserMedia === "function",
    audioInputs: [],
    audioOutputs: [],
    videoInputs: [],
    labelsHidden: false,
    error: null,
  }));
  const [micPerm, setMicPerm] = useState<"granted" | "denied" | "prompt" | "unknown">("unknown");
  const [camPerm, setCamPerm] = useState<"granted" | "denied" | "prompt" | "unknown">("unknown");
  const [refreshing, setRefreshing] = useState(false);

  const [selectedMicId, setSelectedMicId] = useState<string>("");
  const [selectedCamId, setSelectedCamId] = useState<string>("");

  const [micTesting, setMicTesting] = useState(false);
  const [micLevel, setMicLevel] = useState(0);
  const [micError, setMicError] = useState<string | null>(null);

  const [camTesting, setCamTesting] = useState(false);
  const [camError, setCamError] = useState<string | null>(null);

  const micStreamRef = useRef<MediaStream | null>(null);
  const camStreamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const rafRef = useRef<number | null>(null);
  const videoElRef = useRef<HTMLVideoElement | null>(null);

  const stopMicTest = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    stopMediaStream(micStreamRef.current);
    micStreamRef.current = null;
    if (audioCtxRef.current) {
      const ctx = audioCtxRef.current;
      audioCtxRef.current = null;
      ctx.close().catch(() => undefined);
    }
    setMicTesting(false);
    setMicLevel(0);
  }, []);

  const stopCamTest = useCallback(() => {
    stopMediaStream(camStreamRef.current);
    camStreamRef.current = null;
    if (videoElRef.current) {
      try {
        videoElRef.current.srcObject = null;
      } catch {
        // ignore detached video element cleanup
      }
    }
    setCamTesting(false);
  }, []);

  const refreshDevices = useCallback(async (alsoRefreshHost = false) => {
    setRefreshing(true);
    try {
      const [inv, mp, cp] = await Promise.all([
        safeEnumerateMediaDevices(),
        queryBrowserPermissionState("microphone"),
        queryBrowserPermissionState("camera"),
      ]);
      setInventory(inv);
      setMicPerm(mp);
      setCamPerm(cp);
      if (alsoRefreshHost && onRefreshHostDevices) {
        await onRefreshHostDevices();
      }
    } catch (e) {
      const msg = (e as Error)?.message || "Device refresh failed.";
      setInventory((prev) => ({ ...prev, error: msg }));
    } finally {
      setRefreshing(false);
    }
  }, [onRefreshHostDevices]);

  // Passive enumeration only on open (never requests mic/camera permissions automatically).
  useEffect(() => {
    void refreshDevices(false);
    const md = typeof navigator !== "undefined" ? navigator.mediaDevices : undefined;
    const onDeviceChange = () => {
      void refreshDevices(false);
    };
    try {
      md?.addEventListener?.("devicechange", onDeviceChange);
    } catch {
      // some browsers lack addEventListener on mediaDevices
    }
    const onPageHide = () => {
      stopMicTest();
      stopCamTest();
    };
    window.addEventListener("pagehide", onPageHide);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
      try {
        md?.removeEventListener?.("devicechange", onDeviceChange);
      } catch {
        // ignore
      }
      stopMicTest();
      stopCamTest();
    };
  }, [refreshDevices, stopMicTest, stopCamTest]);

  // Attach camera MediaStream when the preview element mounts.
  useEffect(() => {
    if (camTesting && videoElRef.current && camStreamRef.current) {
      videoElRef.current.srcObject = camStreamRef.current;
    }
  }, [camTesting]);

  const startMicTest = async () => {
    setMicError(null);
    if (micTesting) {
      stopMicTest();
      return;
    }
    if (!inventory.supported || !inventory.getUserMediaSupported || !navigator?.mediaDevices?.getUserMedia) {
      setMicError("Microphone testing requires browser navigator.mediaDevices.getUserMedia support (HTTPS or localhost).");
      return;
    }
    if (inventory.enumerateSupported && inventory.audioInputs.length === 0) {
      setMicError("No browser microphone device detected. Connect a microphone and click Refresh Devices.");
      return;
    }
    try {
      const audioConstraint: MediaTrackConstraints | boolean = selectedMicId
        ? { deviceId: { exact: selectedMicId } }
        : true;
      const stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraint, video: false });
      micStreamRef.current = stream;
      setMicTesting(true);
      onNotify?.("Microphone permission granted for live level check.");
      void refreshDevices(false);

      const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (AudioCtx) {
        const ctx = new AudioCtx();
        audioCtxRef.current = ctx;
        const src = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        src.connect(analyser);
        const buf = new Uint8Array(analyser.frequencyBinCount);
        const tick = () => {
          if (!micStreamRef.current) return;
          analyser.getByteTimeDomainData(buf);
          let sq = 0;
          for (let i = 0; i < buf.length; i += 1) {
            const norm = (buf[i] - 128) / 128;
            sq += norm * norm;
          }
          const rms = Math.sqrt(sq / buf.length);
          setMicLevel(Math.min(100, Math.round(rms * 350)));
          rafRef.current = requestAnimationFrame(tick);
        };
        rafRef.current = requestAnimationFrame(tick);
      }
    } catch (err) {
      stopMicTest();
      const mapped = mapMediaError(err);
      setMicError(mapped.message);
      void refreshDevices(false);
    }
  };

  const startCamTest = async () => {
    setCamError(null);
    if (camTesting) {
      stopCamTest();
      return;
    }
    if (!inventory.supported || !inventory.getUserMediaSupported || !navigator?.mediaDevices?.getUserMedia) {
      setCamError("Camera testing requires browser navigator.mediaDevices.getUserMedia support (HTTPS or localhost).");
      return;
    }
    if (inventory.enumerateSupported && inventory.videoInputs.length === 0) {
      setCamError("No camera device is connected. Video input is optional — Jarvis voice and chat work without a camera.");
      return;
    }
    try {
      const videoConstraint: MediaTrackConstraints | boolean = selectedCamId
        ? { deviceId: { exact: selectedCamId } }
        : true;
      const stream = await navigator.mediaDevices.getUserMedia({ video: videoConstraint, audio: false });
      camStreamRef.current = stream;
      setCamTesting(true);
      onNotify?.("Camera permission granted for local preview test.");
      void refreshDevices(false);
    } catch (err) {
      stopCamTest();
      const mapped = mapMediaError(err);
      setCamError(mapped.message);
      void refreshDevices(false);
    }
  };

  const hostInputs = hostAudio?.inputs || [];
  const hostOutputs = hostAudio?.outputs || [];

  return (
    <div className="card">
      <div className="row between wrap" style={{ gap: 8 }}>
        <div>
          <h3>Audio &amp; Video Devices (Diagnostics &amp; Permissions)</h3>
          <p className="field-hint" style={{ marginTop: 4 }}>
            Inspects desktop audio endpoints and browser microphones/cameras without requesting permission automatically.
            Permissions are only requested when you click a Test button, and streams stop immediately on Stop or leaving this view.
          </p>
        </div>
        <button
          type="button"
          className="ghost"
          disabled={refreshing}
          onClick={() => refreshDevices(true)}
        >
          {refreshing ? "Refreshing…" : "Refresh Devices"}
        </button>
      </div>

      {/* Host Server Audio Inventory */}
      <div className="perm-row" style={{ marginTop: 10 }}>
        <div className="row between wrap">
          <strong>Desktop Host Audio Hardware (Backend Voice Loop)</strong>
          <span className={`pill ${hostAudio?.available === false ? "warn" : "ok"}`}>
            {hostAudio?.available === false
              ? "No Host Audio Devices"
              : `${hostInputs.length} inputs · ${hostOutputs.length} outputs`}
          </span>
        </div>
        <div className="dim" style={{ fontSize: 12 }}>
          Default Input: <span className="mono">{hostAudio?.input || "None detected"}</span> · Default Output:{" "}
          <span className="mono">{hostAudio?.output || "None detected"}</span>
        </div>
        {hostAudio?.error && (
          <div className="err-text" style={{ fontSize: 12 }}>Host audio probe note: {hostAudio.error}</div>
        )}
      </div>

      {/* Browser Media API Status */}
      <div className="perm-row" style={{ marginTop: 10 }}>
        <div className="row between wrap" style={{ gap: 6 }}>
          <strong>Browser MediaDevices &amp; Permissions</strong>
          <div className="row wrap" style={{ gap: 6 }}>
            <span className={`pill ${micPerm === "granted" ? "ok" : micPerm === "denied" ? "danger" : "info"}`}>
              Mic perm: {micPerm}
            </span>
            <span className={`pill ${camPerm === "granted" ? "ok" : camPerm === "denied" ? "danger" : "info"}`}>
              Cam perm: {camPerm}
            </span>
          </div>
        </div>

        {inventory.error && (
          <div className="err-text" style={{ fontSize: 12 }}>{inventory.error}</div>
        )}
        {!inventory.error && !inventory.getUserMediaSupported && (
          <div className="dim" style={{ fontSize: 12 }}>
            Live browser testing (<code>getUserMedia</code>) is unsupported in this browser context.
          </div>
        )}
        {inventory.labelsHidden && (
          <div className="dim" style={{ fontSize: 12 }}>
            Browser security hides exact device names until you grant permission via Test Microphone or Test Camera below.
          </div>
        )}

        <div className="grid-2" style={{ marginTop: 6 }}>
          <div>
            <small className="dim">Browser Microphones ({inventory.audioInputs.length})</small>
            {inventory.audioInputs.length === 0 ? (
              <p className="dim" style={{ fontSize: 12, marginTop: 4 }}>No browser microphones reported.</p>
            ) : (
              <select
                aria-label="Browser microphone to test"
                value={selectedMicId}
                onChange={(e) => setSelectedMicId(e.target.value)}
                style={{ width: "100%", marginTop: 4 }}
              >
                <option value="">Default Browser Microphone</option>
                {inventory.audioInputs.map((d) => (
                  <option key={d.deviceId} value={d.deviceId}>{d.label}</option>
                ))}
              </select>
            )}
            <div className="row wrap" style={{ marginTop: 8, gap: 8 }}>
              <button
                type="button"
                className={micTesting ? "danger" : "ghost"}
                disabled={!inventory.getUserMediaSupported}
                onClick={startMicTest}
              >
                {micTesting ? "Stop Mic Test" : "Test Microphone"}
              </button>
              {micTesting && (
                <span className="mono dim" style={{ fontSize: 12 }}>
                  Live input level: {micLevel}%
                </span>
              )}
            </div>
            {micTesting && (
              <div
                aria-label="Live microphone test meter"
                style={{
                  marginTop: 6,
                  height: 6,
                  borderRadius: 4,
                  background: "rgba(15, 23, 42, 0.8)",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    width: `${micLevel}%`,
                    height: "100%",
                    background: "var(--accent, #22d3ee)",
                    transition: "width 60ms linear",
                  }}
                />
              </div>
            )}
            {micError && <div className="err-text" style={{ fontSize: 12, marginTop: 6 }}>{micError}</div>}
          </div>

          <div>
            <small className="dim">Browser Cameras ({inventory.videoInputs.length})</small>
            {inventory.videoInputs.length === 0 ? (
              <p className="dim" style={{ fontSize: 12, marginTop: 4 }}>
                No camera connected. Video input is optional for Jarvis.
              </p>
            ) : (
              <select
                aria-label="Browser camera to preview"
                value={selectedCamId}
                onChange={(e) => setSelectedCamId(e.target.value)}
                style={{ width: "100%", marginTop: 4 }}
              >
                <option value="">Default Camera</option>
                {inventory.videoInputs.map((d) => (
                  <option key={d.deviceId} value={d.deviceId}>{d.label}</option>
                ))}
              </select>
            )}
            <div className="row wrap" style={{ marginTop: 8, gap: 8 }}>
              <button
                type="button"
                className={camTesting ? "danger" : "ghost"}
                disabled={!inventory.getUserMediaSupported}
                onClick={startCamTest}
              >
                {camTesting ? "Stop Camera Test" : "Test Camera"}
              </button>
              <span className="pill warn">Camera AI Vision: Not Implemented Yet</span>
            </div>
            <p className="field-hint" style={{ marginTop: 4 }}>
              Camera preview runs strictly locally inside your browser to test device access; video is not sent to Jarvis AI models yet.
            </p>
            {camTesting && (
              <video
                ref={videoElRef}
                autoPlay
                muted
                playsInline
                style={{
                  marginTop: 8,
                  width: "100%",
                  maxHeight: 160,
                  borderRadius: 6,
                  border: "1px solid var(--border)",
                  background: "#020617",
                  objectFit: "cover",
                }}
              />
            )}
            {camError && <div className="err-text" style={{ fontSize: 12, marginTop: 6 }}>{camError}</div>}
          </div>
        </div>
      </div>
    </div>
  );
};

