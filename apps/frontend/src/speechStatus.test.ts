import assert from "node:assert/strict";
import { test } from "node:test";
import { classifyTranscriptStatus } from "./speechStatus.ts";

test("speech inspector status distinguishes high, review-needed, low, provider-unavailable, and translation-uncertain", () => {
  const high = classifyTranscriptStatus({
    recognition_quality: "high",
    review_needed: false,
    status: "completed",
  });
  assert.equal(high.qualityLabel, "High quality");
  assert.equal(high.qualityTone, "ok");

  const review = classifyTranscriptStatus({
    recognition_quality: "medium",
    review_needed: true,
    status: "completed",
  });
  assert.equal(review.qualityLabel, "Needs review");
  assert.equal(review.qualityTone, "warn");

  const low = classifyTranscriptStatus({
    recognition_quality: "low",
    review_needed: true,
    status: "uncertain",
  });
  assert.equal(low.qualityLabel, "Low quality");
  assert.equal(low.qualityTone, "danger");

  const unavailableOnly = classifyTranscriptStatus({
    recognition_quality: "high",
    review_needed: true,
    confidence: null,
    confidence_available: false,
    provider_status: {
      requested: "malayalam_whisper",
      available: false,
      state: "unavailable",
      reason: "Model directory not installed",
      expected_dir: "models/asr/whisper-small-malayalam-v2-ct2",
    },
    provider_errors: [],
  });
  assert.equal(unavailableOnly.providerUnavailable, true);
  assert.equal(unavailableOnly.hasProviderErrors, false);
  assert.equal(unavailableOnly.confidenceAvailable, false);
  assert.equal(unavailableOnly.qualityLabel, "Needs review");

  const trUncertain = classifyTranscriptStatus({
    recognition_quality: "high",
    status: "uncertain",
    confidence: 0.84,
    translation_verification: { passed: false, uncertain: true, warnings: ["missing_number:8080"] },
  });
  assert.equal(trUncertain.translationUncertain, true);
  assert.equal(trUncertain.confidenceAvailable, true);
  assert.equal(trUncertain.qualityTone, "warn");
});

