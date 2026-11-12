export interface TranscriptStatusInput {
  recognition_quality?: string;
  review_needed?: boolean;
  status?: string;
  provider_status?: {
    requested?: string;
    available?: boolean;
    reason?: string;
  };
  provider_errors?: Array<{
    provider?: string;
    model?: string;
    error?: string;
  }>;
  translation_verification?: {
    passed?: boolean;
    uncertain?: boolean;
    warnings?: string[];
  } | null;
}

export interface TranscriptStatusSummary {
  qualityLabel: "High quality" | "Needs review" | "Low quality";
  qualityTone: "ok" | "warn" | "danger";
  providerUnavailable: boolean;
  hasProviderErrors: boolean;
  translationUncertain: boolean;
}

export function classifyTranscriptStatus(rec: TranscriptStatusInput | null | undefined): TranscriptStatusSummary {
  const recQuality = rec?.recognition_quality || "medium";
  const providerUnavailable = rec?.provider_status?.available === false;
  const hasProviderErrors = Boolean(rec?.provider_errors && rec.provider_errors.length > 0);
  const translationUncertain = Boolean(rec?.translation_verification?.uncertain);

  let qualityLabel: "High quality" | "Needs review" | "Low quality" = "Needs review";
  let qualityTone: "ok" | "warn" | "danger" = "warn";

  if (recQuality === "low") {
    qualityLabel = "Low quality";
    qualityTone = "danger";
  } else if (
    recQuality === "high" &&
    !rec?.review_needed &&
    rec?.status !== "uncertain" &&
    !providerUnavailable &&
    !hasProviderErrors &&
    !translationUncertain
  ) {
    qualityLabel = "High quality";
    qualityTone = "ok";
  }

  return {
    qualityLabel,
    qualityTone,
    providerUnavailable,
    hasProviderErrors,
    translationUncertain,
  };
}

