export interface BrowserMediaDeviceItem {
  deviceId: string;
  groupId: string;
  kind: "audioinput" | "audiooutput" | "videoinput";
  label: string;
  labelHidden: boolean;
}

export interface BrowserMediaInventory {
  supported: boolean;
  enumerateSupported: boolean;
  getUserMediaSupported: boolean;
  audioInputs: BrowserMediaDeviceItem[];
  audioOutputs: BrowserMediaDeviceItem[];
  videoInputs: BrowserMediaDeviceItem[];
  labelsHidden: boolean;
  error: string | null;
}

export interface MappedMediaError {
  code: string;
  message: string;
}

export interface MediaDevicesLike {
  enumerateDevices?: () => Promise<Array<Partial<MediaDeviceInfo>>>;
  getUserMedia?: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  addEventListener?: (type: string, listener: EventListenerOrEventListenerObject) => void;
  removeEventListener?: (type: string, listener: EventListenerOrEventListenerObject) => void;
}

export interface NavigatorLike {
  mediaDevices?: MediaDevicesLike | null;
  permissions?: {
    query?: (desc: { name: string }) => Promise<{ state: string }>;
  } | null;
}

/** Format raw WebRTC / getUserMedia DOMException names into plain-English guidance. */
export function mapMediaError(err: unknown): MappedMediaError {
  const rec = (err && typeof err === "object" ? err : {}) as { name?: unknown; message?: unknown };
  const name = typeof rec.name === "string" ? rec.name : "";
  const rawMsg = typeof rec.message === "string" ? rec.message : typeof err === "string" ? err : "";

  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    return {
      code: "permission_denied",
      message: "Permission denied by the browser. Click the lock/site-settings icon in the address bar to allow access, then retry.",
    };
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return {
      code: "no_device",
      message: "No matching microphone or camera device was found. Connect a device and click Refresh Devices.",
    };
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return {
      code: "device_busy",
      message: "The selected device is busy or already in use by another application or browser tab.",
    };
  }
  if (name === "OverconstrainedError" || name === "ConstraintNotSatisfiedError") {
    return {
      code: "overconstrained",
      message: "The requested audio or video constraint is not supported by the selected device. Try the default device.",
    };
  }
  if (name === "SecurityError") {
    return {
      code: "insecure_context",
      message: "Browser media access requires a secure context (HTTPS or localhost / 127.0.0.1).",
    };
  }
  return {
    code: name || "media_error",
    message: rawMsg || "Browser media request failed.",
  };
}

export function getMediaCapabilities(nav: NavigatorLike | undefined | null): {
  supported: boolean;
  enumerateSupported: boolean;
  getUserMediaSupported: boolean;
} {
  const md = nav?.mediaDevices ?? null;
  return {
    supported: Boolean(md),
    enumerateSupported: typeof md?.enumerateDevices === "function",
    getUserMediaSupported: typeof md?.getUserMedia === "function",
  };
}

/**
 * Safely enumerate browser microphones, speakers, and cameras without ever calling getUserMedia()
 * and without throwing on unsupported contexts or rejected promises.
 */
export async function safeEnumerateMediaDevices(
  nav: NavigatorLike | undefined | null = typeof navigator !== "undefined" ? (navigator as unknown as NavigatorLike) : null
): Promise<BrowserMediaInventory> {
  const caps = getMediaCapabilities(nav);
  if (!caps.supported) {
    return {
      ...caps,
      audioInputs: [],
      audioOutputs: [],
      videoInputs: [],
      labelsHidden: false,
      error: "navigator.mediaDevices is unavailable in this browser or non-secure context (requires HTTPS or localhost).",
    };
  }
  if (!caps.enumerateSupported || !nav?.mediaDevices?.enumerateDevices) {
    return {
      ...caps,
      audioInputs: [],
      audioOutputs: [],
      videoInputs: [],
      labelsHidden: false,
      error: "Browser device enumeration (enumerateDevices) is not supported here.",
    };
  }

  try {
    const rawList = (await nav.mediaDevices.enumerateDevices()) || [];
    const audioInputs: BrowserMediaDeviceItem[] = [];
    const audioOutputs: BrowserMediaDeviceItem[] = [];
    const videoInputs: BrowserMediaDeviceItem[] = [];
    let labelsHidden = false;

    for (const d of rawList) {
      if (!d || (d.kind !== "audioinput" && d.kind !== "audiooutput" && d.kind !== "videoinput")) {
        continue;
      }
      const rawLabel = typeof d.label === "string" ? d.label.trim() : "";
      const hidden = rawLabel.length === 0;
      if (hidden) labelsHidden = true;

      const idx =
        d.kind === "audioinput"
          ? audioInputs.length + 1
          : d.kind === "audiooutput"
          ? audioOutputs.length + 1
          : videoInputs.length + 1;
      const prefix =
        d.kind === "audioinput" ? "Microphone" : d.kind === "audiooutput" ? "Speaker" : "Camera";

      const item: BrowserMediaDeviceItem = {
        deviceId: String(d.deviceId || `${d.kind}-${idx}`),
        groupId: String(d.groupId || ""),
        kind: d.kind,
        label: rawLabel || `${prefix} #${idx} (permission needed for name)`,
        labelHidden: hidden,
      };
      if (d.kind === "audioinput") audioInputs.push(item);
      else if (d.kind === "audiooutput") audioOutputs.push(item);
      else videoInputs.push(item);
    }

    return {
      ...caps,
      audioInputs,
      audioOutputs,
      videoInputs,
      labelsHidden,
      error: null,
    };
  } catch (err) {
    const mapped = mapMediaError(err);
    return {
      ...caps,
      audioInputs: [],
      audioOutputs: [],
      videoInputs: [],
      labelsHidden: false,
      error: mapped.message,
    };
  }
}

/** Query browser mic/camera permission states passively without triggering a permission prompt. */
export async function queryBrowserPermissionState(
  kind: "microphone" | "camera",
  nav: NavigatorLike | undefined | null = typeof navigator !== "undefined" ? (navigator as unknown as NavigatorLike) : null
): Promise<"granted" | "denied" | "prompt" | "unknown"> {
  try {
    if (typeof nav?.permissions?.query !== "function") return "unknown";
    const status = await nav.permissions.query({ name: kind });
    if (status?.state === "granted" || status?.state === "denied" || status?.state === "prompt") {
      return status.state;
    }
    return "unknown";
  } catch {
    return "unknown";
  }
}

/** Stop every track on a MediaStream safely so mic/camera hardware lights turn off immediately. */
export function stopMediaStream(stream: { getTracks?: () => Array<{ stop?: () => void }> } | null | undefined): void {
  if (!stream || typeof stream.getTracks !== "function") return;
  try {
    for (const track of stream.getTracks()) {
      try {
        track?.stop?.();
      } catch {
        // continue stopping remaining tracks
      }
    }
  } catch {
    // ignore malformed stream objects during cleanup
  }
}

