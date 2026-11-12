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

  const missingProvider = classifyTranscriptStatus({
    recognition_quality: "high",
    provider_status: { requested: "malayalam_whisper", available: false, reason: "missing" },
    provider_errors: [{ provider: "malayalam_whisper", error: "missing" }],
  });
  assert.equal(missingProvider.providerUnavailable, true);
  assert.equal(missingProvider.hasProviderErrors, true);
  assert.equal(missingProvider.qualityLabel, "Needs review");

  const trUncertain = classifyTranscriptStatus({
    recognition_quality: "high",
    status: "uncertain",
    translation_verification: { passed: false, uncertain: true, warnings: ["missing_number:8080"] },
  });
  assert.equal(trUncertain.translationUncertain, true);
  assert.equal(trUncertain.qualityTone, "warn");
});

