import assert from "node:assert/strict";
import { test } from "node:test";
import {
  getMediaCapabilities,
  mapMediaError,
  queryBrowserPermissionState,
  safeEnumerateMediaDevices,
  stopMediaStream,
} from "./mediaDevices.ts";

test("safeEnumerateMediaDevices handles missing navigator.mediaDevices without throwing", async () => {
  const res = await safeEnumerateMediaDevices({ mediaDevices: null });
  assert.equal(res.supported, false);
  assert.equal(res.enumerateSupported, false);
  assert.equal(res.getUserMediaSupported, false);
  assert.deepEqual(res.audioInputs, []);
  assert.deepEqual(res.videoInputs, []);
  assert.match(res.error || "", /navigator\.mediaDevices/i);
});

test("safeEnumerateMediaDevices handles missing getUserMedia and rejected enumerateDevices promises", async () => {
  const capsOnlyEnum = getMediaCapabilities({
    mediaDevices: {
      enumerateDevices: async () => [],
    },
  });
  assert.equal(capsOnlyEnum.supported, true);
  assert.equal(capsOnlyEnum.enumerateSupported, true);
  assert.equal(capsOnlyEnum.getUserMediaSupported, false);

  const rejected = await safeEnumerateMediaDevices({
    mediaDevices: {
      enumerateDevices: async () => {
        const err = new Error("Permission blocked");
        err.name = "NotAllowedError";
        throw err;
      },
      getUserMedia: async () => {
        throw new Error("must not be called during enumeration");
      },
    },
  });
  assert.equal(rejected.supported, true);
  assert.match(rejected.error || "", /Permission denied/i);
});

test("safeEnumerateMediaDevices separates mics/speakers/cameras and never calls getUserMedia", async () => {
  let gumCalled = false;
  const inv = await safeEnumerateMediaDevices({
    mediaDevices: {
      enumerateDevices: async () => [
        { kind: "audioinput", deviceId: "mic-1", groupId: "g1", label: "" },
        { kind: "videoinput", deviceId: "cam-1", groupId: "g2", label: "Integrated Webcam" },
        { kind: "audiooutput", deviceId: "spk-1", groupId: "g1", label: "Headphones" },
      ],
      getUserMedia: async () => {
        gumCalled = true;
        throw new Error("should not call getUserMedia automatically");
      },
    },
  });

  assert.equal(gumCalled, false);
  assert.equal(inv.error, null);
  assert.equal(inv.labelsHidden, true);
  assert.equal(inv.audioInputs.length, 1);
  assert.equal(inv.audioInputs[0].labelHidden, true);
  assert.match(inv.audioInputs[0].label, /permission needed/i);
  assert.equal(inv.videoInputs.length, 1);
  assert.equal(inv.videoInputs[0].label, "Integrated Webcam");
  assert.equal(inv.audioOutputs.length, 1);
});

test("mapMediaError distinguishes denied, no device, busy, and security errors clearly", () => {
  assert.equal(mapMediaError({ name: "NotAllowedError" }).code, "permission_denied");
  assert.equal(mapMediaError({ name: "NotFoundError" }).code, "no_device");
  assert.equal(mapMediaError({ name: "NotReadableError" }).code, "device_busy");
  assert.equal(mapMediaError({ name: "SecurityError" }).code, "insecure_context");
  assert.match(mapMediaError({ name: "NotReadableError" }).message, /busy|in use/i);
});

test("stopMediaStream stops every track cleanly and queryBrowserPermissionState never throws", async () => {
  let stopped = 0;
  stopMediaStream({
    getTracks: () => [
      { stop: () => { stopped += 1; } },
      { stop: () => { throw new Error("already ended"); } },
      { stop: () => { stopped += 1; } },
    ],
  });
  assert.equal(stopped, 2);

  const permUnknown = await queryBrowserPermissionState("camera", { permissions: null });
  assert.equal(permUnknown, "unknown");
});

