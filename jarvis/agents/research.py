"""P10.22 / P10.22.1 Multi-Stage Verified Deep Research Engine.

Implements:
PLAN -> INVESTIGATE -> REVIEW -> WRITE -> VERIFY -> REVISE (bounded <= 2 cycles)
with SHA-256 snapshot persistence & reload verification, strict separation of
citation validity / quote matching / claim support / contradictions, unique
canonical-URL corroboration, Local Mode KB-only fallback, zero personal-memory
exfiltration, and decoupled Markdown/PDF/DOCX export outcome reporting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import logging
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from jarvis.departments.assistant.documents import DocumentService
from jarvis.security.outcomes import (
    ToolOutcome,
    ToolStatus,
    VerificationCheck,
    verify_file_created,
)
from jarvis.security.untrusted import UNTRUSTED_DATA_POLICY, wrap_untrusted

from .task_runner import TaskExecutionResult, TaskRunContext

log = logging.getLogger("jarvis.research")

MAX_SUBQUERIES = 6
MAX_PAGES_PER_QUERY = 2
MAX_TOTAL_PAGES = 10
MAX_REVISION_CYCLES = 2


def normalize_for_quote_match(text: str) -> str:
    """Normalize Unicode (NFKC), quotes/dashes, and whitespace for resilient quote checks."""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = (
        s.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u00a0", " ")
    )
    return " ".join(s.split()).casefold()


def canonicalize_url(raw_url: str) -> str:
    """Strip fragments and trailing slash noise for URL deduplication."""
    raw = (raw_url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))
    except Exception:
        return raw


@dataclass
class ResearchSnapshot:
    source_id: str
    title: str
    url: str
    canonical_url: str
    source_type: str  # "web_page" | "knowledge_chunk" | "web_snippet"
    sha256: str
    fetched_at: float
    snapshot_path: str
    content: str
    snippet: str = ""
    query: str = ""

    def to_metadata_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "canonical_url": self.canonical_url,
            "source_type": self.source_type,
            "sha256": self.sha256,
            "fetched_at": self.fetched_at,
            "snapshot_path": self.snapshot_path,
            "content_chars": len(self.content),
            "snippet": self.snippet[:320],
            "query": self.query,
        }


@dataclass
class ResearchClaim:
    statement: str
    kind: str = "verified_fact"  # "verified_fact" | "reported_claim" | "disputed" | "inference" | "unverified"
    citations: list[str] = field(default_factory=list)
    quote: str = ""
    contradicted: bool = False
    verification_note: str = ""
    citation_valid: bool = False
    quote_verified: bool = False
    quote_spans: list[dict[str, Any]] = field(default_factory=list)
    provenance: str = "synthesized_claim"  # "extracted_quote" | "synthesized_claim" | "inference"
    evidence_supports_claim: bool = False
    evidence_contradicts_claim: bool = False
    verification_status: str = "unverified"  # "verified" | "supported_reported" | "disputed" | "inference" | "unverified"
    independent_source_count: int = 0
    assessment_method: str = "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchPlan:
    question: str
    subqueries: list[str]
    expected_source_types: list[str]
    key_claims_to_verify: list[str]
    budgets: dict[str, int] = field(
        default_factory=lambda: {
            "max_subqueries": MAX_SUBQUERIES,
            "max_pages_per_query": MAX_PAGES_PER_QUERY,
            "max_total_pages": MAX_TOTAL_PAGES,
            "max_revision_cycles": MAX_REVISION_CYCLES,
        }
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationReport:
    passed: bool
    overall_status: str = "verified"  # "verified" | "verified_with_reported" | "disputed" | "unverified" | "integrity_warning"
    missing_citations: list[int] = field(default_factory=list)
    invalid_citations: list[str] = field(default_factory=list)
    quote_mismatches: list[dict[str, Any]] = field(default_factory=list)
    missing_quotes: list[int] = field(default_factory=list)
    unsupported_claims: list[int] = field(default_factory=list)
    contradicted_claims: list[int] = field(default_factory=list)
    contradictions_found: list[str] = field(default_factory=list)
    duplicate_urls: list[str] = field(default_factory=list)
    integrity_warnings: list[str] = field(default_factory=list)
    independent_source_count: int = 0
    unverified_claim_count: int = 0
    verified_claim_count: int = 0
    reported_claim_count: int = 0
    disputed_claim_count: int = 0
    inference_claim_count: int = 0
    revision_cycle: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SynthesizerFn = Callable[[str, list[dict[str, Any]], list[str]], Awaitable[dict[str, Any]] | dict[str, Any]]
ClaimAssessorFn = Callable[[dict[str, Any], list[dict[str, Any]]], Awaitable[dict[str, Any]] | dict[str, Any]]


def verify_checkpoint_snapshots(
    task_id: str,
    checkpoint: dict[str, Any],
    *,
    data_dir: Path,
) -> tuple[list[ResearchSnapshot], list[dict[str, Any]], list[str]]:
    """Recompute SHA-256 and validate storage root on checkpoint snapshot reload."""
    allowed_root = (Path(data_dir) / "tasks" / task_id / "pages").resolve()
    valid: list[ResearchSnapshot] = []
    rejected: list[dict[str, Any]] = []
    warnings: list[str] = []

    for meta in checkpoint.get("snapshots") or []:
        sid = str(meta.get("source_id") or "?")
        raw_path = str(meta.get("snapshot_path") or "").strip()
        expected_sha = str(meta.get("sha256") or "").strip().lower()

        if not raw_path:
            reason = "missing snapshot_path"
            warnings.append(f"Rejected snapshot {sid}: {reason}")
            rejected.append({"source_id": sid, "snapshot_path": raw_path, "reason": reason})
            continue

        try:
            sp = Path(raw_path).resolve()
            sp.relative_to(allowed_root)
        except Exception:
            reason = "snapshot path outside task storage root"
            warnings.append(f"Rejected snapshot {sid}: {reason} ({raw_path})")
            rejected.append({"source_id": sid, "snapshot_path": raw_path, "reason": reason})
            continue

        if not expected_sha:
            reason = "missing expected sha256 digest"
            warnings.append(f"Rejected snapshot {sid}: {reason}")
            rejected.append({"source_id": sid, "snapshot_path": str(sp), "reason": reason})
            continue

        if not (sp.exists() and sp.is_file()):
            reason = "snapshot file missing on disk"
            warnings.append(f"Rejected snapshot {sid}: {reason} ({sp.name})")
            rejected.append(
                {
                    "source_id": sid,
                    "snapshot_path": str(sp),
                    "expected_sha256": expected_sha,
                    "reason": reason,
                }
            )
            continue

        try:
            raw_bytes = sp.read_bytes()
        except OSError as exc:
            reason = f"unreadable snapshot file: {exc}"
            warnings.append(f"Rejected snapshot {sid}: {reason}")
            rejected.append({"source_id": sid, "snapshot_path": str(sp), "reason": reason})
            continue

        actual_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
        if actual_sha != expected_sha:
            reason = f"sha256 mismatch (expected {expected_sha[:12]}, got {actual_sha[:12]})"
            warnings.append(f"Rejected corrupted/truncated snapshot {sid}: {reason}")
            rejected.append(
                {
                    "source_id": sid,
                    "snapshot_path": str(sp),
                    "expected_sha256": expected_sha,
                    "actual_sha256": actual_sha,
                    "reason": reason,
                }
            )
            continue

        content = raw_bytes.decode("utf-8", errors="replace")
        valid.append(
            ResearchSnapshot(
                source_id=sid,
                title=str(meta.get("title") or ""),
                url=str(meta.get("url") or ""),
                canonical_url=str(meta.get("canonical_url") or canonicalize_url(str(meta.get("url") or ""))),
                source_type=str(meta.get("source_type") or "web_page"),
                sha256=expected_sha,
                fetched_at=float(meta.get("fetched_at") or 0.0),
                snapshot_path=str(sp),
                content=content,
                snippet=str(meta.get("snippet") or ""),
                query=str(meta.get("query") or ""),
            )
        )
    return valid, rejected, warnings


def _extract_subqueries(question: str, steer_notes: list[str] | None = None, *, max_queries: int = MAX_SUBQUERIES) -> list[str]:
    base = re.sub(r"^(?:deep\s+research|research|investigate|compare|analyze)\s+(?:on|about)?\s*", "", (question or "").strip(), flags=re.I).strip()
    if not base:
        base = (question or "").strip() or "overview"

    candidates: list[str] = [base]
    for part in re.split(r"\b(?:and|vs\.?|versus|compared to|,)\b", base, flags=re.I):
        cleaned = part.strip(" ?.!\"'")
        if len(cleaned) >= 4 and cleaned.lower() != base.lower():
            candidates.append(f"{cleaned} documentation benchmark specifications")
    candidates.append(f"{base} official documentation")
    candidates.append(f"{base} limitations risks trade-offs")

    if steer_notes:
        for note in steer_notes[-2:]:
            n = note.strip()
            if n:
                candidates.insert(1, f"{base} {n}")

    deduped: list[str] = []
    seen: set[str] = set()
    for q in candidates:
        norm = " ".join(q.split()).lower()
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(" ".join(q.split())[:200])
        if len(deduped) >= max(1, min(max_queries, MAX_SUBQUERIES)):
            break
    return deduped


_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "with",
        "that",
        "this",
        "from",
        "have",
        "has",
        "had",
        "been",
        "will",
        "into",
        "over",
        "under",
        "also",
        "however",
        "about",
        "which",
        "when",
        "where",
        "they",
        "their",
        "there",
        "than",
        "then",
        "uses",
        "using",
        "used",
        "report",
        "reports",
        "source",
        "claims",
        "claim",
        "certain",
        "legacy",
        "builds",
        "officially",
        "introduces",
        "according",
        "details",
    }
)

_POLARITY_NEUTRAL_TERMS = frozenset(
    {
        "supported",
        "unsupported",
        "deprecated",
        "disabled",
        "enabled",
        "recommended",
        "optional",
        "required",
        "available",
        "unavailable",
        "removed",
        "never",
        "always",
        "cannot",
        "false",
        "true",
        "default",
        "longer",
        "without",
    }
)

_NEG_PATTERNS = re.compile(
    r"\b(?:not\s+supported|unsupported|deprecated|disabled\s+by\s+default|is\s+disabled|"
    r"no\s+longer|does\s+not\s+support|cannot|never|unavailable|removed)\b",
    re.I,
)
_POS_PATTERNS = re.compile(
    r"\b(?:is\s+supported|are\s+supported|officially\s+supported|supported\s+by\s+default|"
    r"enabled\s+by\s+default|is\s+enabled|recommended|always\s+enabled|supports)\b",
    re.I,
)


def _content_terms(text: str, *, include_polarity: bool = False) -> set[str]:
    norm = normalize_for_quote_match(text)
    toks = re.findall(r"[a-z0-9]{3,}(?:[._-][a-z0-9]+)*", norm)
    out: set[str] = set()
    for t in toks:
        if t in _STOPWORDS:
            continue
        if not include_polarity and t in _POLARITY_NEUTRAL_TERMS:
            continue
        out.add(t)
    return out


def _numeric_tokens(text: str) -> set[str]:
    norm = normalize_for_quote_match(text)
    return set(re.findall(r"\b\d+(?:\.\d+)?%?\b", norm))


def _line_polarity(text: str) -> int:
    """Return +1 (affirmative), -1 (negated/deprecated), or 0 (neutral/ambiguous)."""
    has_neg = bool(_NEG_PATTERNS.search(text))
    has_pos = bool(_POS_PATTERNS.search(text))
    if has_neg and not has_pos:
        return -1
    if has_pos and not has_neg:
        return 1
    if has_neg and has_pos:
        # Sentences like "does not support ... by default" -> negative wins
        return -1
    return 0


def _detect_contradiction_records(snapshots: list[ResearchSnapshot]) -> list[dict[str, Any]]:
    """Detect polarity divergences across distinct canonical URLs only (duplicates never corroborate/contradict)."""
    records: list[dict[str, Any]] = []
    pos_items: list[tuple[str, str, set[str], str]] = []
    neg_items: list[tuple[str, str, set[str], str]] = []

    for snap in snapshots:
        canon = snap.canonical_url or canonicalize_url(snap.url) or snap.source_id
        for line in re.split(r"[\r\n]+|(?<=[.!?])\s+", snap.content):
            clean = line.strip()
            if len(clean) < 12:
                continue
            pol = _line_polarity(clean)
            if pol == 0:
                continue
            terms = _content_terms(clean, include_polarity=False)
            if len(terms) < 2:
                continue
            if pol < 0:
                neg_items.append((snap.source_id, canon, terms, clean[:160]))
            else:
                pos_items.append((snap.source_id, canon, terms, clean[:160]))

    seen_pairs: set[tuple[str, str, tuple[str, ...]]] = set()
    for p_sid, p_canon, p_terms, p_line in pos_items:
        for n_sid, n_canon, n_terms, n_line in neg_items:
            if p_sid == n_sid or p_canon == n_canon:
                continue
            overlap = sorted(p_terms.intersection(n_terms))
            if len(overlap) >= 2:
                sig = (min(p_sid, n_sid), max(p_sid, n_sid), tuple(overlap[:4]))
                if sig not in seen_pairs:
                    seen_pairs.add(sig)
                    topic = ", ".join(overlap[:4])
                    records.append(
                        {
                            "pos_source": p_sid,
                            "neg_source": n_sid,
                            "pos_line": p_line,
                            "neg_line": n_line,
                            "terms": set(overlap),
                            "summary": f"Divergent statements on '{topic}': ({p_sid}: {p_line}) vs ({n_sid}: {n_line})",
                        }
                    )
    return records[:8]


def _detect_contradictions(snapshots: list[ResearchSnapshot]) -> list[str]:
    """Identify negation/support divergences across distinct canonical sources sharing subject terms."""
    return [r["summary"] for r in _detect_contradiction_records(snapshots)]


def _assess_deterministic_support(
    claim: ResearchClaim,
    cited_snaps: list[ResearchSnapshot],
    all_snaps: list[ResearchSnapshot],
    contradiction_records: list[dict[str, Any]],
) -> tuple[bool, bool, str]:
    """Deterministically verify whether cited evidence supports the claim and whether any source contradicts it.

    Returns (supports_claim, contradicts_claim, detail_reason).
    """
    claim_terms = _content_terms(claim.statement, include_polarity=False)
    claim_nums = _numeric_tokens(claim.statement)
    claim_pol = _line_polarity(claim.statement)
    nq_claim = normalize_for_quote_match(claim.quote)

    cited_sids = {s.source_id for s in cited_snaps}

    # 1) Check contradictions: either detected cross-source conflict touching the claim polarity/quote, or direct polarity conflict
    contradicts = False
    contra_reason = ""
    for rec in contradiction_records:
        if rec["pos_source"] in cited_sids or rec["neg_source"] in cited_sids:
            if len(claim_terms.intersection(rec["terms"])) >= 2:
                quote_hits_conflict = bool(
                    nq_claim
                    and (
                        nq_claim in normalize_for_quote_match(rec.get("pos_line", ""))
                        or nq_claim in normalize_for_quote_match(rec.get("neg_line", ""))
                    )
                )
                if claim_pol != 0 or quote_hits_conflict:
                    contradicts = True
                    contra_reason = rec["summary"]
                    break

    if not contradicts and claim_pol != 0 and len(claim_terms) >= 2:
        for snap in all_snaps:
            for sent in re.split(r"[\r\n]+|(?<=[.!?])\s+", snap.content):
                s_pol = _line_polarity(sent)
                if s_pol != 0 and s_pol != claim_pol:
                    overlap = claim_terms.intersection(_content_terms(sent, include_polarity=False))
                    if len(overlap) >= 2:
                        contradicts = True
                        contra_reason = f"Contradicted by source {snap.source_id}: {sent.strip()[:140]}"
                        break
            if contradicts:
                break

    # 2) Check topical & numeric support strictly inside cited snapshots (NOT uncited snapshots)
    combined_cited_text = "\n".join(s.content for s in cited_snaps)
    cited_terms = _content_terms(combined_cited_text, include_polarity=False)
    cited_nums = _numeric_tokens(combined_cited_text)

    if not claim_terms:
        return False, contradicts, "Claim contains no verifiable factual terms"

    shared = claim_terms.intersection(cited_terms)
    coverage = len(shared) / max(1, len(claim_terms))
    min_shared = 1 if len(claim_terms) == 1 else 2

    if len(shared) < min_shared or coverage < 0.35:
        return (
            False,
            contradicts,
            f"Cited source(s) do not sufficiently support claim topic (shared={len(shared)}/{len(claim_terms)}, coverage={coverage:.0%})",
        )

    if claim_nums and not claim_nums.issubset(cited_nums):
        missing_nums = sorted(claim_nums - cited_nums)
        return (
            False,
            contradicts,
            f"Claim numeric values {missing_nums} not present in cited source snapshot(s)",
        )

    # Check sentence-level polarity agreement in cited snapshots when claim states affirmative/negative status
    if claim_pol != 0:
        cited_disagrees_only = False
        cited_agrees = False
        for snap in cited_snaps:
            for sent in re.split(r"[\r\n]+|(?<=[.!?])\s+", snap.content):
                if len(claim_terms.intersection(_content_terms(sent, include_polarity=False))) >= min_shared:
                    spol = _line_polarity(sent)
                    if spol == claim_pol:
                        cited_agrees = True
                    elif spol == -claim_pol:
                        cited_disagrees_only = True
        if cited_disagrees_only and not cited_agrees:
            contradicts = True
            return False, True, "Cited source polarity directly opposes the claim statement"

    # If a verbatim quote is attached, make sure the quote window itself relates topically OR snapshot supports claim
    if claim.quote and claim.quote.strip():
        quote_terms = _content_terms(claim.quote, include_polarity=False)
        quote_nums = _numeric_tokens(claim.quote)
        # At least either the quote shares claim topic/numbers OR the surrounding snapshot clearly supports it cleanly
        if quote_terms and len(claim_terms.intersection(quote_terms)) == 0 and not (claim_nums and claim_nums.intersection(quote_nums)):
            # Allow when quote comes from an explicitly supporting sentence in the same snapshot only if shared snapshot topic is strong (>= 0.65)
            if coverage < 0.65:
                return (
                    False,
                    contradicts,
                    "Attached quotation does not relate to the claim subject in cited source",
                )

    if contradicts:
        return True, True, contra_reason
    return True, False, "Deterministic citation and evidence support verified"


def _locate_quote_span(content: str, quote: str, source_id: str) -> dict[str, Any] | None:
    """Locate start/end character offsets for a verified excerpt inside raw snapshot content."""
    clean_q = str(quote or "").strip()
    if not clean_q or not content:
        return None
    idx = content.find(clean_q)
    if idx >= 0:
        return {
            "source_id": source_id,
            "start_offset": idx,
            "end_offset": idx + len(clean_q),
            "matched_text": content[idx : idx + len(clean_q)][:240],
        }
    low_idx = content.lower().find(clean_q.lower())
    if low_idx >= 0:
        return {
            "source_id": source_id,
            "start_offset": low_idx,
            "end_offset": low_idx + len(clean_q),
            "matched_text": content[low_idx : low_idx + len(clean_q)][:240],
        }
    words = [re.escape(w) for w in clean_q.split() if w]
    if words:
        pattern = r"\s+".join(words[:18])
        m = re.search(pattern, content, flags=re.IGNORECASE)
        if m:
            return {
                "source_id": source_id,
                "start_offset": m.start(),
                "end_offset": m.end(),
                "matched_text": m.group(0)[:240],
            }
    return None


def build_freshness_and_limitations(
    snapshots: list[ResearchSnapshot],
    verification: VerificationReport,
    *,
    online_allowed: bool,
    warnings: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Compute evidence freshness metadata and explicit research gaps/limitations."""
    timestamps = [float(s.fetched_at) for s in snapshots if getattr(s, "fetched_at", None)]
    freshness = {
        "source_count": len(snapshots),
        "independent_source_count": verification.independent_source_count,
        "oldest_fetched_at": min(timestamps) if timestamps else None,
        "newest_fetched_at": max(timestamps) if timestamps else None,
        "online_allowed": bool(online_allowed),
    }
    limitations: list[str] = []
    if not online_allowed:
        limitations.append("Offline Local Mode active: live external web search was disabled; only approved local/KB evidence used.")
    if not snapshots:
        limitations.append("Zero verified source snapshots were retained for this query.")
    elif verification.independent_source_count < 2:
        limitations.append("Fewer than 2 independent canonical sources corroborated the findings.")
    if verification.unverified_claim_count > 0:
        limitations.append(f"{verification.unverified_claim_count} claim(s) lacked full verified citation/quote support.")
    if verification.disputed_claim_count > 0 or verification.contradictions_found:
        limitations.append(f"{len(verification.contradictions_found)} cross-source contradiction(s) detected and flagged.")
    for w in warnings or []:
        if "denied" in w.lower() or "integrity" in w.lower() or "unavailable" in w.lower():
            if w not in limitations:
                limitations.append(w)
    return freshness, limitations


def verify_claims_against_snapshots(
    claims: list[ResearchClaim],
    snapshots: dict[str, ResearchSnapshot],
    contradictions: list[str],
    *,
    revision_cycle: int = 0,
    integrity_warnings: list[str] | None = None,
    assessor_results: dict[int, dict[str, Any]] | None = None,
) -> VerificationReport:
    """Verify citation validity, quote matching, claim support, unique-URL corroboration, and contradictions."""
    missing: list[int] = []
    invalid: list[str] = []
    mismatches: list[dict[str, Any]] = []
    missing_quotes: list[int] = []
    unsupported: list[int] = []
    contradicted_idxs: list[int] = []
    int_warns = list(integrity_warnings or [])

    normalized_snaps = {sid: normalize_for_quote_match(s.content) for sid, s in snapshots.items()}
    snap_list = list(snapshots.values())
    contra_records = _detect_contradiction_records(snap_list)
    effective_contradictions = list(dict.fromkeys([*contradictions, *[r["summary"] for r in contra_records]]))

    # Duplicate canonical URL detection across snapshots
    seen_canon: dict[str, str] = {}
    dup_urls: list[str] = []
    for sid, snap in snapshots.items():
        c_url = snap.canonical_url or canonicalize_url(snap.url) or snap.url or sid
        if c_url in seen_canon:
            dup_urls.append(f"{sid} duplicates {seen_canon[c_url]} ({c_url})")
        else:
            seen_canon[c_url] = sid

    for idx, cl in enumerate(claims):
        if cl.kind == "inference":
            cl.citation_valid = False
            cl.quote_verified = False
            cl.quote_spans = []
            cl.provenance = "inference"
            cl.evidence_supports_claim = True
            cl.evidence_contradicts_claim = bool(cl.contradicted)
            cl.verification_status = "inference"
            if not cl.verification_note:
                cl.verification_note = "Clearly labeled analytical inference"
            continue

        if cl.kind == "unverified":
            cl.citation_valid = False
            cl.quote_verified = False
            cl.quote_spans = []
            cl.provenance = "synthesized_claim"
            cl.evidence_supports_claim = False
            cl.verification_status = "unverified"
            continue

        # 1) Citation presence and snapshot existence check
        if not cl.citations:
            missing.append(idx)
            cl.citation_valid = False
            cl.quote_verified = False
            cl.evidence_supports_claim = False
            cl.verification_status = "unverified"
            cl.verification_note = "No source citation provided"
            continue

        valid_cits: list[str] = []
        for cid in cl.citations:
            if cid not in snapshots or not snapshots[cid].content.strip():
                if cid not in invalid:
                    invalid.append(cid)
            else:
                valid_cits.append(cid)

        if not valid_cits:
            missing.append(idx)
            cl.citation_valid = False
            cl.quote_verified = False
            cl.evidence_supports_claim = False
            cl.verification_status = "unverified"
            cl.verification_note = f"All cited sources invalid or unavailable: {cl.citations}"
            continue

        cl.citation_valid = True
        cited_snaps = [snapshots[cid] for cid in valid_cits]
        unique_canon = {s.canonical_url or canonicalize_url(s.url) or s.url or s.source_id for s in cited_snaps}
        cl.independent_source_count = len(unique_canon)

        # 2) Verbatim normalized quote verification + offset span provenance
        has_quote = bool(cl.quote and cl.quote.strip())
        cl.quote_spans = []
        if has_quote:
            nq = normalize_for_quote_match(cl.quote)
            quote_snaps = [snapshots[cid] for cid in valid_cits if nq in normalized_snaps[cid]]
            cl.quote_verified = bool(quote_snaps)
            if cl.quote_verified:
                cl.provenance = "extracted_quote"
                for qsnap in quote_snaps:
                    span = _locate_quote_span(qsnap.content, cl.quote, qsnap.source_id)
                    if span is not None:
                        cl.quote_spans.append(span)
            else:
                cl.provenance = "synthesized_claim"
                mismatches.append({"claim_index": idx, "citations": valid_cits, "quote": cl.quote[:160]})
                cl.evidence_supports_claim = False
                cl.verification_status = "unverified"
                cl.verification_note = "Cited quote not found verbatim in retrieved snapshot text"
                continue
        else:
            cl.quote_verified = False
            cl.provenance = "synthesized_claim"
            quote_snaps = cited_snaps
            missing_quotes.append(idx)

        # 3) Claim-support and contradiction verification against cited + full source pool
        det_supports, det_contradicts, detail_reason = _assess_deterministic_support(
            cl,
            quote_snaps,
            snap_list,
            contra_records,
        )

        # Bounded optional semantic assessor can veto/flag contradictions, NEVER override failed deterministic checks.
        if assessor_results and idx in assessor_results:
            ares = assessor_results[idx]
            cl.assessment_method = "deterministic+model"
            if ares.get("contradicts") is True:
                det_contradicts = True
                if ares.get("reason"):
                    detail_reason = f"{detail_reason}; assessor: {ares['reason']}"
            if ares.get("supports") is False:
                det_supports = False
                if ares.get("reason"):
                    detail_reason = f"{detail_reason}; assessor: {ares['reason']}"

        cl.evidence_supports_claim = det_supports
        cl.evidence_contradicts_claim = bool(det_contradicts or cl.contradicted)
        cl.contradicted = cl.evidence_contradicts_claim

        if not det_supports:
            unsupported.append(idx)
            cl.verification_status = "unverified"
            cl.verification_note = detail_reason
            continue

        if cl.evidence_contradicts_claim:
            contradicted_idxs.append(idx)
            cl.verification_status = "disputed"
            cl.verification_note = detail_reason or "Claim contradicted by conflicting retrieved source evidence"
            continue

        if not cl.quote_verified:
            # Valid supporting citation without verbatim excerpt MUST NOT be verified_fact.
            cl.verification_status = "supported_reported"
            cl.verification_note = (
                "Valid supporting citation present without verbatim excerpt; cannot remain verified_fact"
                if cl.kind == "verified_fact"
                else "Supported reported claim without verbatim quotation"
            )
            continue

        cl.verification_status = "verified" if cl.kind == "verified_fact" else "supported_reported"
        cl.verification_note = "Verified citation, quotation, and supporting evidence"

    verified_count = sum(
        1
        for i, c in enumerate(claims)
        if c.kind == "verified_fact"
        and c.citation_valid
        and c.quote_verified
        and c.evidence_supports_claim
        and not c.evidence_contradicts_claim
        and i not in missing
        and i not in unsupported
        and i not in contradicted_idxs
    )
    reported_count = sum(
        1
        for i, c in enumerate(claims)
        if c.kind == "reported_claim"
        and c.citation_valid
        and c.evidence_supports_claim
        and not c.evidence_contradicts_claim
        and i not in missing
        and i not in unsupported
        and i not in contradicted_idxs
    )
    disputed_count = sum(1 for c in claims if c.evidence_contradicts_claim or c.kind == "disputed")
    inference_count = sum(1 for c in claims if c.kind == "inference")
    unverified_count = sum(1 for c in claims if c.kind == "unverified")

    has_disputes = bool(contradicted_idxs or disputed_count or effective_contradictions)
    has_ungrounded = bool(missing or invalid or mismatches or unsupported or unverified_count)

    if int_warns and len(snapshots) == 0:
        overall_status = "integrity_warning"
    elif has_disputes:
        overall_status = "disputed"
    elif has_ungrounded:
        overall_status = "unverified"
    elif reported_count > 0 and verified_count == 0:
        overall_status = "verified_with_reported"
    elif reported_count > 0:
        overall_status = "verified_with_reported"
    else:
        overall_status = "verified"

    passed = (
        not missing
        and not invalid
        and not mismatches
        and not unsupported
        and not contradicted_idxs
        and not effective_contradictions
        and not int_warns
        and disputed_count == 0
        and unverified_count == 0
        and not any(c.kind == "verified_fact" and not c.quote_verified for c in claims)
    )

    return VerificationReport(
        passed=passed,
        overall_status=overall_status,
        missing_citations=missing,
        invalid_citations=invalid,
        quote_mismatches=mismatches,
        missing_quotes=missing_quotes,
        unsupported_claims=unsupported,
        contradicted_claims=contradicted_idxs,
        contradictions_found=effective_contradictions,
        duplicate_urls=dup_urls,
        integrity_warnings=int_warns,
        independent_source_count=len(seen_canon),
        unverified_claim_count=unverified_count,
        verified_claim_count=verified_count,
        reported_claim_count=reported_count,
        disputed_claim_count=disputed_count,
        inference_claim_count=inference_count,
        revision_cycle=revision_cycle,
    )


class DeepResearchEngine:
    """Isolated, resumable multi-stage deep research orchestrator.

    Privacy boundary: accepts ONLY `search_service` (online web), `knowledge_pipeline`
    (local indexed docs), and `document_service` (output export). It explicitly has NO
    reference to personal memory, personal notes, email, shell, or host filesystem.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        search_service=None,
        knowledge_pipeline=None,
        document_service: DocumentService | None = None,
        fs_access: Any = None,
        online_allowed_fn: Callable[[], bool] | None = None,
        synthesizer_fn: SynthesizerFn | None = None,
        claim_assessor_fn: ClaimAssessorFn | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.search_service = search_service
        self.knowledge_pipeline = knowledge_pipeline
        self.document_service = document_service or DocumentService(self.data_dir / "outputs" / "documents")
        self.fs_access = fs_access
        self.online_allowed_fn = online_allowed_fn or (lambda: False)
        self.synthesizer_fn = synthesizer_fn
        self.claim_assessor_fn = claim_assessor_fn

    def _pages_dir(self, task_id: str) -> Path:
        p = self.data_dir / "tasks" / task_id / "pages"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _store_snapshot(
        self,
        task_id: str,
        *,
        source_id: str,
        title: str,
        url: str,
        source_type: str,
        content: str,
        snippet: str = "",
        query: str = "",
    ) -> ResearchSnapshot:
        clean_text = str(content or "").strip()
        raw_bytes = clean_text.encode("utf-8", errors="replace")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        snap_file = self._pages_dir(task_id) / f"{digest}.txt"
        snap_file.write_bytes(raw_bytes)
        verify_res = verify_file_created(
            snap_file,
            tool_name="research_snapshot_write",
            min_bytes=0,
            expected_sha256=digest,
            root=self._pages_dir(task_id),
        )
        if not verify_res.ok:
            raise RuntimeError(f"Failed verifying research snapshot persistence: {verify_res.error}")
        return ResearchSnapshot(
            source_id=source_id,
            title=title or url or source_id,
            url=url,
            canonical_url=canonicalize_url(url),
            source_type=source_type,
            sha256=digest,
            fetched_at=round(time.time(), 3),
            snapshot_path=str(snap_file),
            content=clean_text,
            snippet=snippet or clean_text[:280],
            query=query,
        )

    def _load_snapshots_from_checkpoint(
        self,
        task_id: str,
        checkpoint: dict[str, Any],
    ) -> tuple[list[ResearchSnapshot], list[dict[str, Any]], list[str]]:
        return verify_checkpoint_snapshots(task_id, checkpoint, data_dir=self.data_dir)

    async def _investigate_local_folders(
        self,
        task_id: str,
        subquery: str,
        local_folders: list[str],
        *,
        snapshots: list[ResearchSnapshot],
        seen_urls: set[str],
    ) -> list[str]:
        warnings: list[str] = []
        if not local_folders or len(snapshots) >= MAX_TOTAL_PAGES:
            return warnings
        if self.fs_access is None:
            warnings.append("Local folder access denied: filesystem permission manager unavailable")
            return warnings

        from ..departments.assistant.documents import parse_document_untrusted

        allowed_exts = {".md", ".txt", ".py", ".json", ".csv", ".rst", ".yaml", ".yml", ".toml", ".pdf", ".docx", ".xlsx"}
        q_terms = _content_terms(subquery, include_polarity=False)
        added = 0

        for raw_dir in local_folders:
            if len(snapshots) >= MAX_TOTAL_PAGES or added >= 4:
                break
            perm = await self.fs_access.authorize(raw_dir, "list_directory", task_id=task_id)
            if not getattr(perm, "allowed", False):
                warnings.append(f"Local folder access denied ({getattr(perm, 'reason', 'permission required')}): {raw_dir}")
                continue
            folder = Path(getattr(perm, "canonical_path", raw_dir) or raw_dir)
            if not folder.exists() or not folder.is_dir():
                warnings.append(f"Local folder not found: {raw_dir}")
                continue

            candidates: list[Path] = []
            for p in sorted(folder.rglob("*")):
                if len(candidates) >= 24:
                    break
                if p.is_file() and p.suffix.lower() in allowed_exts and p.stat().st_size <= 512_000:
                    candidates.append(p)
            # Rank by subquery term overlap cleanly
            candidates.sort(
                key=lambda fp: (
                    len(q_terms.intersection(_content_terms(fp.name, include_polarity=False))),
                    fp.name,
                ),
                reverse=True,
            )
            for fp in candidates:
                if len(snapshots) >= MAX_TOTAL_PAGES or added >= 4:
                    break
                fperm = await self.fs_access.authorize(fp, "read_file", task_id=task_id)
                if not getattr(fperm, "allowed", False):
                    continue
                canon = f"file://{fp.resolve().as_posix()}"
                if canon in seen_urls:
                    continue
                try:
                    parsed = parse_document_untrusted(fp, max_chars=8000)
                    text = str(parsed.get("raw_text") or parsed.get("text") or "").strip()
                    if not text:
                        text = fp.read_text(encoding="utf-8", errors="ignore").strip()
                except Exception:
                    try:
                        text = fp.read_text(encoding="utf-8", errors="ignore").strip()
                    except Exception:
                        text = ""
                if not text:
                    continue
                seen_urls.add(canon)
                sid = f"S{len(snapshots) + 1}"
                snapshots.append(
                    self._store_snapshot(
                        task_id,
                        source_id=sid,
                        title=fp.name,
                        url=canon,
                        source_type="local_file",
                        content=text[:8000],
                        query=subquery,
                    )
                )
                added += 1
        return warnings

    async def _investigate_query(
        self,
        task_id: str,
        subquery: str,
        *,
        online_allowed: bool,
        snapshots: list[ResearchSnapshot],
        seen_urls: set[str],
        local_folders: list[str] | None = None,
        max_pages: int = MAX_PAGES_PER_QUERY,
    ) -> list[str]:
        warnings: list[str] = []
        if len(snapshots) >= MAX_TOTAL_PAGES:
            return warnings

        if local_folders:
            warnings.extend(
                await self._investigate_local_folders(
                    task_id,
                    subquery,
                    local_folders,
                    snapshots=snapshots,
                    seen_urls=seen_urls,
                )
            )

        if self.knowledge_pipeline is not None:
            try:
                kb_hits = self.knowledge_pipeline.search(subquery, k=2)
                for h in kb_hits:
                    if len(snapshots) >= MAX_TOTAL_PAGES:
                        break
                    loc = str(h.get("location") or h.get("source_id") or "local-kb")
                    canon = canonicalize_url(loc) or f"kb:{h.get('source_id', '')}:{loc}"
                    if canon in seen_urls:
                        continue
                    seen_urls.add(canon)
                    sid = f"S{len(snapshots) + 1}"
                    snapshots.append(
                        self._store_snapshot(
                            task_id,
                            source_id=sid,
                            title=str(h.get("title") or loc),
                            url=loc,
                            source_type="knowledge_chunk",
                            content=str(h.get("content") or ""),
                            query=subquery,
                        )
                    )
            except Exception as e:
                warnings.append(f"Knowledge base search skipped: {e}")

        if not online_allowed:
            return warnings
        if self.search_service is None:
            warnings.append("Web search service unavailable despite Online Mode enabled.")
            return warnings

        try:
            search_res = await self.search_service.search(subquery, max_results=max_pages + 2)
        except Exception as e:
            warnings.append(f"Search error for '{subquery}': {e}")
            return warnings

        fetched_for_query = 0
        for item in search_res.get("results") or []:
            if len(snapshots) >= MAX_TOTAL_PAGES or fetched_for_query >= max_pages:
                break
            url = str(item.get("url") or "").strip()
            canon = canonicalize_url(url)
            if not url or not canon or canon in seen_urls:
                continue

            full_text = ""
            title = str(item.get("title") or url)
            src_type = "web_snippet"
            try:
                if getattr(self.search_service.spec, "allow_page_fetch", True):
                    page = await self.search_service.fetch_page(url, max_chars=None)
                    full_text = str(page.get("content") or "").strip()
                    title = str(page.get("title") or title)
                    url = str(page.get("url") or url)
                    src_type = "web_page"
            except Exception as e:
                warnings.append(f"Page fetch fallback to snippet for {url}: {e}")

            if not full_text:
                full_text = str(item.get("snippet") or "").strip()
            if not full_text:
                continue

            eff_canon = canonicalize_url(url)
            if canon in seen_urls or (eff_canon and eff_canon in seen_urls):
                continue
            seen_urls.add(canon)
            if eff_canon:
                seen_urls.add(eff_canon)
            fetched_for_query += 1
            sid = f"S{len(snapshots) + 1}"
            snapshots.append(
                self._store_snapshot(
                    task_id,
                    source_id=sid,
                    title=title,
                    url=url,
                    source_type=src_type,
                    content=full_text,
                    snippet=str(item.get("snippet") or full_text[:280]),
                    query=subquery,
                )
            )
        return warnings

    async def _write_draft(
        self,
        question: str,
        snapshots: list[ResearchSnapshot],
        contradictions: list[str],
        steer_notes: list[str],
        *,
        online_allowed: bool,
    ) -> tuple[str, list[ResearchClaim]]:
        """Synthesize cited research draft strictly from untrusted-wrapped snapshots."""
        wrapped_sources = [
            {
                "source_id": s.source_id,
                "url": s.url,
                "title": s.title,
                "sha256": s.sha256,
                "source_type": s.source_type,
                "wrapped_content": wrap_untrusted(
                    s.content,
                    source_type="web_page" if s.source_type == "web_page" else "knowledge_chunk",
                    source_id=s.source_id,
                    url=s.url,
                    title=s.title,
                    sha256=s.sha256,
                    max_chars=6000,
                ),
            }
            for s in snapshots
        ]

        if self.synthesizer_fn is not None and snapshots:
            raw = self.synthesizer_fn(question, wrapped_sources, steer_notes)
            if hasattr(raw, "__await__"):
                raw = await raw
            if isinstance(raw, dict):
                claims = [
                    ResearchClaim(
                        statement=str(c.get("statement") or ""),
                        kind=str(c.get("kind") or "reported_claim"),
                        citations=[str(x) for x in (c.get("citations") or [])],
                        quote=str(c.get("quote") or ""),
                    )
                    for c in (raw.get("claims") or [])
                    if isinstance(c, dict) and c.get("statement")
                ]
                summary = str(raw.get("summary") or "")
                if claims:
                    return summary, claims

        fallback_claims: list[ResearchClaim] = []
        if not snapshots:
            mode_note = (
                "No valid local knowledge base documents remained while operating strictly in offline Local Mode."
                if not online_allowed
                else "No valid web or local knowledge sources were available or verified."
            )
            fallback_claims.append(ResearchClaim(statement=mode_note, kind="unverified", citations=[]))
            return mode_note, fallback_claims

        for s in snapshots:
            lines = [ln.strip() for ln in s.content.splitlines() if len(ln.strip()) >= 15]
            lead_quote = lines[0][:240] if lines else s.content[:240].strip()
            if not lead_quote:
                continue
            fallback_claims.append(
                ResearchClaim(
                    statement=lead_quote,
                    kind="reported_claim",
                    citations=[s.source_id],
                    quote=lead_quote,
                )
            )
        if contradictions:
            fallback_claims.append(
                ResearchClaim(
                    statement=f"Conflicting details detected across retrieved sources: {contradictions[0]}",
                    kind="inference",
                    citations=[],
                )
            )
        summary = f"Synthesized {len(fallback_claims)} grounded findings across {len(snapshots)} retrieved source snapshots."
        return summary, fallback_claims

    async def _run_bounded_assessor(
        self,
        claims: list[ResearchClaim],
        snaps_by_id: dict[str, ResearchSnapshot],
    ) -> dict[int, dict[str, Any]]:
        if self.claim_assessor_fn is None:
            return {}
        out: dict[int, dict[str, Any]] = {}
        for idx, cl in enumerate(claims[:20]):
            if cl.kind in ("inference", "unverified") or not cl.citations:
                continue
            wrapped_ev = [
                {
                    "source_id": sid,
                    "title": snaps_by_id[sid].title,
                    "url": snaps_by_id[sid].url,
                    "wrapped_content": wrap_untrusted(
                        snaps_by_id[sid].content,
                        source_type="web_page" if snaps_by_id[sid].source_type == "web_page" else "knowledge_chunk",
                        source_id=sid,
                        max_chars=2400,
                    ),
                }
                for sid in cl.citations
                if sid in snaps_by_id
            ]
            if not wrapped_ev:
                continue
            try:
                res = self.claim_assessor_fn(cl.to_dict(), wrapped_ev)
                if hasattr(res, "__await__"):
                    res = await res
                if isinstance(res, dict):
                    out[idx] = res
            except Exception as exc:
                log.debug("Bounded claim assessor skipped for claim %s: %s", idx, exc)
        return out

    def _revise_claims(
        self,
        claims: list[ResearchClaim],
        report: VerificationReport,
        snapshots_by_id: dict[str, ResearchSnapshot],
    ) -> list[ResearchClaim]:
        """Deterministically downgrade disputed, unsupported, unquoted verified_fact, or bad-quote claims."""
        bad_quotes = {m["claim_index"] for m in report.quote_mismatches}
        missing = set(report.missing_citations)
        invalid_ids = set(report.invalid_citations)
        unsupported = set(report.unsupported_claims)
        contradicted = set(report.contradicted_claims)
        missing_q = set(report.missing_quotes)

        revised: list[ResearchClaim] = []
        for idx, cl in enumerate(claims):
            if cl.kind == "inference":
                cl.citations = [c for c in cl.citations if c not in invalid_ids and c in snapshots_by_id]
                cl.verification_status = "inference"
                revised.append(cl)
                continue

            cl.citations = [c for c in cl.citations if c not in invalid_ids and c in snapshots_by_id]
            if idx in missing or not cl.citations or idx in bad_quotes or idx in unsupported:
                cl.kind = "unverified"
                cl.verification_status = "unverified"
                if "[unverified]" not in cl.statement:
                    cl.statement = f"{cl.statement} [unverified]"
            elif idx in contradicted or cl.evidence_contradicts_claim:
                cl.kind = "disputed"
                cl.contradicted = True
                cl.evidence_contradicts_claim = True
                cl.verification_status = "disputed"
                if "[disputed]" not in cl.statement:
                    cl.statement = f"{cl.statement} [disputed]"
            elif idx in missing_q and cl.kind == "verified_fact":
                # Verified fact without verbatim excerpt cannot remain verified_fact -> downgrade to reported_claim.
                cl.kind = "reported_claim"
                cl.verification_status = "supported_reported"
            revised.append(cl)
        return revised

    def render_markdown(
        self,
        *,
        question: str,
        plan: ResearchPlan,
        claims: list[ResearchClaim],
        snapshots: list[ResearchSnapshot],
        verification: VerificationReport,
        online_allowed: bool,
        warnings: list[str],
    ) -> str:
        lines: list[str] = [
            f"# Deep Research Report: {question}",
            "",
            f"- **Mode**: {'Online Assisted (web + local KB)' if online_allowed else 'Local Mode (local knowledge base only)'}",
            f"- **Sources Verified & Loaded**: {len(snapshots)} (unique canonical URLs: {verification.independent_source_count})",
            f"- **Verified Claims (with Quote & Support)**: {verification.verified_claim_count}",
            f"- **Reported Claims**: {verification.reported_claim_count}",
            f"- **Disputed / Contradicted Claims**: {verification.disputed_claim_count}",
            f"- **Inferences**: {verification.inference_claim_count}",
            f"- **Unverified Claims**: {verification.unverified_claim_count}",
            f"- **Overall Verification Status**: `{verification.overall_status}` ({'PASSED' if verification.passed else 'ITEMS FLAGGED'}) (cycles: {verification.revision_cycle})",
            "",
            "## Research Plan",
            "",
        ]
        for q in plan.subqueries:
            lines.append(f"- Query: `{q}`")
        lines.extend(["", "## Key Findings", ""])

        verified_facts = [c for c in claims if c.kind == "verified_fact"]
        reported = [c for c in claims if c.kind == "reported_claim"]
        disputed = [c for c in claims if c.kind == "disputed" or c.evidence_contradicts_claim]
        inferences = [c for c in claims if c.kind == "inference"]
        unverified = [c for c in claims if c.kind == "unverified"]

        def _claim_flags(c: ResearchClaim) -> str:
            return (
                f"Citation valid: {'Yes' if c.citation_valid else 'No'} | "
                f"Quotation verified: {'Yes' if c.quote_verified else 'No'} | "
                f"Evidence supports claim: {'Yes' if c.evidence_supports_claim else 'No'} | "
                f"Evidence contradicts claim: {'Yes' if c.evidence_contradicts_claim else 'No'}"
            )

        lines.append("### Verified Facts & Grounded Reported Claims")
        lines.append("")
        if not verified_facts and not reported:
            lines.append("- None verified from available sources.")
        else:
            for c in [*verified_facts, *reported]:
                cites = " ".join(f"[{cid}]" for cid in c.citations)
                tag = "**Fact**" if c.kind == "verified_fact" else "Reported"
                lines.append(f"- ({tag}) {c.statement} {cites}")
                lines.append(f"  - Verification: {_claim_flags(c)} (unique sources: {c.independent_source_count})")
                if c.quote:
                    lines.append(f"  > Quote: \"{c.quote}\"")

        if disputed:
            lines.extend(["", "### Disputed & Contradicted Claims", ""])
            for c in disputed:
                cites = " ".join(f"[{cid}]" for cid in c.citations)
                lines.append(f"- (Disputed) {c.statement} {cites}")
                lines.append(f"  - Verification: {_claim_flags(c)} — {c.verification_note}")

        if inferences:
            lines.extend(["", "### Inferences & Cross-Source Synthesis", ""])
            for c in inferences:
                lines.append(f"- (Inference) {c.statement}")

        if unverified:
            lines.extend(["", "### Unverified Claims & Gaps", ""])
            for c in unverified:
                cites = " ".join(f"[{cid}]" for cid in c.citations) if c.citations else ""
                lines.append(f"- (Claim Unverified) {c.statement} {cites}".rstrip())
                lines.append(f"  - Verification: {_claim_flags(c)} — {c.verification_note or 'Insufficient source support'}")

        if verification.contradictions_found:
            lines.extend(["", "## Contradictions & Conflicting Evidence", ""])
            for ct in verification.contradictions_found:
                lines.append(f"- {ct}")

        if verification.duplicate_urls:
            lines.extend(["", "## Duplicate URL Notice", ""])
            for d in verification.duplicate_urls:
                lines.append(f"- {d} (excluded from independent corroboration count)")

        if verification.integrity_warnings or warnings:
            lines.extend(["", "## Snapshot Integrity & Retrieval Notices", ""])
            for iw in verification.integrity_warnings:
                lines.append(f"- **Integrity Warning**: {iw}")
            for w in warnings:
                if w not in verification.integrity_warnings:
                    lines.append(f"- {w}")

        lines.extend(["", "## Bibliography & Snapshot Hashes", ""])
        if not snapshots:
            lines.append("- No valid external or local source snapshots were captured or retained.")
        else:
            for s in snapshots:
                lines.append(
                    f"- **[{s.source_id}]** {s.title} — {s.url} (`{s.source_type}`, sha256=`{s.sha256[:16]}...`, snapshot=`{s.snapshot_path}`)"
                )
        freshness, limitations = build_freshness_and_limitations(
            snapshots,
            verification,
            online_allowed=online_allowed,
            warnings=warnings,
        )
        lines.extend(
            [
                "",
                "## Freshness, Coverage & Known Limitations",
                "",
                f"- **Sources captured**: {freshness['source_count']} ({freshness['independent_source_count']} independent canonical sources)",
            ]
        )
        for lim in limitations:
            lines.append(f"- **Limitation**: {lim}")
        lines.append("")
        return "\n".join(lines)

    async def run(
        self,
        *,
        task_id: str,
        question: str,
        ctx: TaskRunContext | None = None,
        export_format: str | None = "md",
        steer_notes: list[str] | None = None,
        local_folders: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run or resume full PLAN -> INVESTIGATE -> REVIEW -> WRITE -> VERIFY -> REVISE pipeline."""
        online_allowed = bool(self.online_allowed_fn())
        eff_local_folders = [str(f).strip() for f in (local_folders or []) if str(f).strip()]
        checkpoint = ctx.checkpoint if ctx is not None else {}
        done_steps = ctx.completed_step_keys if ctx is not None else set()
        effective_steer = list(steer_notes or [])
        if ctx is not None:
            effective_steer.extend(await ctx.latest_steer_instructions())

        # STAGE 1: PLAN
        if "step_plan" in done_steps and checkpoint.get("plan"):
            p_raw = checkpoint["plan"]
            plan = ResearchPlan(**p_raw)
        else:
            subqueries = _extract_subqueries(question, effective_steer, max_queries=MAX_SUBQUERIES)
            plan = ResearchPlan(
                question=question,
                subqueries=subqueries,
                expected_source_types=(["web_page", "knowledge_chunk"] if online_allowed else ["knowledge_chunk"]),
                key_claims_to_verify=[f"Core evidence regarding: {q}" for q in subqueries[:3]],
            )
            if ctx is not None:
                await ctx.complete_step(
                    "step_plan",
                    checkpoint_patch={"plan": plan.to_dict(), "online_allowed": online_allowed},
                    current_step=f"Planned {len(plan.subqueries)} research queries",
                )

        # STAGE 2: INVESTIGATE (always reverify reloaded checkpoint snapshots against SHA-256)
        snapshots, rejected_snaps, int_warnings = self._load_snapshots_from_checkpoint(task_id, checkpoint)
        seen_urls: set[str] = {canonicalize_url(s.url) or s.canonical_url for s in snapshots if (s.url or s.canonical_url)}
        warnings: list[str] = list(checkpoint.get("warnings") or [])
        integrity_warnings: list[str] = list(dict.fromkeys([*(checkpoint.get("integrity_warnings") or []), *int_warnings]))
        for iw in int_warnings:
            if iw not in warnings:
                warnings.append(iw)

        for idx, subq in enumerate(plan.subqueries):
            step_key = f"step_investigate_{idx}"
            if step_key in done_steps:
                continue
            if ctx is not None:
                steer_now = await ctx.latest_steer_instructions()
                effective_steer = list(dict.fromkeys([*effective_steer, *steer_now]))
            w = await self._investigate_query(
                task_id,
                subq,
                online_allowed=online_allowed,
                snapshots=snapshots,
                seen_urls=seen_urls,
                local_folders=eff_local_folders,
            )
            warnings.extend(w)
            if ctx is not None:
                await ctx.complete_step(
                    step_key,
                    checkpoint_patch={
                        "snapshots": [s.to_metadata_dict() for s in snapshots],
                        "warnings": warnings,
                        "integrity_warnings": integrity_warnings,
                        "rejected_snapshots": rejected_snaps,
                    },
                    current_step=f"Investigated query {idx + 1}/{len(plan.subqueries)} ({len(snapshots)} sources)",
                    delta_tools=1,
                )

        # If snapshots were corrupted/rejected on reload, force re-review and re-write using only remaining valid evidence
        if int_warnings:
            done_steps = set(done_steps) - {"step_review", "step_write"}

        # STAGE 3: REVIEW
        if "step_review" in done_steps and "contradictions" in checkpoint:
            contradictions = list(checkpoint["contradictions"])
        else:
            contradictions = _detect_contradictions(snapshots)
            if len(snapshots) < 2 and len(plan.subqueries) < MAX_SUBQUERIES and online_allowed and not rejected_snaps:
                followup_q = f"{question} overview specification"
                if followup_q.lower() not in {q.lower() for q in plan.subqueries}:
                    plan.subqueries.append(followup_q)
                    warnings.extend(
                        await self._investigate_query(
                            task_id,
                            followup_q,
                            online_allowed=online_allowed,
                            snapshots=snapshots,
                            seen_urls=seen_urls,
                        )
                    )
                    contradictions = _detect_contradictions(snapshots)
            if ctx is not None:
                await ctx.complete_step(
                    "step_review",
                    checkpoint_patch={
                        "snapshots": [s.to_metadata_dict() for s in snapshots],
                        "contradictions": contradictions,
                        "warnings": warnings,
                        "integrity_warnings": integrity_warnings,
                        "rejected_snapshots": rejected_snaps,
                    },
                    current_step=f"Reviewed {len(snapshots)} sources ({len(contradictions)} contradictions)",
                )

        # STAGE 4: WRITE
        if "step_write" in done_steps and checkpoint.get("claims"):
            claims = [ResearchClaim(**c) for c in checkpoint["claims"]]
            summary = str(checkpoint.get("summary") or "")
        else:
            summary, claims = await self._write_draft(
                question,
                snapshots,
                contradictions,
                effective_steer,
                online_allowed=online_allowed,
            )
            if ctx is not None:
                await ctx.complete_step(
                    "step_write",
                    checkpoint_patch={
                        "summary": summary,
                        "claims": [c.to_dict() for c in claims],
                    },
                    current_step=f"Drafted {len(claims)} cited claims",
                )

        # Reverify snapshots once more before final VERIFY/REVISE pass so mid-run disk corruption is caught
        if checkpoint.get("snapshots") and not snapshots:
            pass
        else:
            for snap in list(snapshots):
                sp = Path(snap.snapshot_path)
                if not sp.exists() or hashlib.sha256(sp.read_bytes()).hexdigest().lower() != snap.sha256.lower():
                    msg = f"Snapshot integrity failure detected prior to claim verification for {snap.source_id}"
                    if msg not in integrity_warnings:
                        integrity_warnings.append(msg)
                    if msg not in warnings:
                        warnings.append(msg)
                    snapshots.remove(snap)

        # STAGE 5 & 6: VERIFY -> REVISE (bounded <= MAX_REVISION_CYCLES)
        snaps_by_id = {s.source_id: s for s in snapshots}
        assessor_results = await self._run_bounded_assessor(claims, snaps_by_id)
        cycle = 0
        verification = verify_claims_against_snapshots(
            claims,
            snaps_by_id,
            contradictions,
            revision_cycle=cycle,
            integrity_warnings=integrity_warnings,
            assessor_results=assessor_results,
        )
        while not verification.passed and cycle < MAX_REVISION_CYCLES:
            needs_revision = bool(
                verification.missing_citations
                or verification.invalid_citations
                or verification.quote_mismatches
                or verification.unsupported_claims
                or verification.contradicted_claims
                or any(cl.kind == "verified_fact" and i in verification.missing_quotes for i, cl in enumerate(claims))
            )
            if not needs_revision:
                break
            cycle += 1
            claims = self._revise_claims(claims, verification, snaps_by_id)
            verification = verify_claims_against_snapshots(
                claims,
                snaps_by_id,
                contradictions,
                revision_cycle=cycle,
                integrity_warnings=integrity_warnings,
                assessor_results=assessor_results,
            )

        markdown = self.render_markdown(
            question=question,
            plan=plan,
            claims=claims,
            snapshots=snapshots,
            verification=verification,
            online_allowed=online_allowed,
            warnings=warnings,
        )

        # STAGE 7: VERIFIED EXPORT (isolated error handling so export failure NEVER destroys research results)
        generated_files: list[str] = []
        export_outcome: dict[str, Any] | None = None
        export_ok = True
        export_error = ""
        if export_format:
            fmt_clean = export_format.lower().strip()
            payload_doc = (
                {"content": markdown, "markdown": markdown}
                if fmt_clean in ("md", "markdown")
                else {
                    "sections": [
                        {"heading": "Summary", "body": summary},
                        {
                            "heading": "Key Findings",
                            "body": "\n".join(
                                f"- [{c.kind}/{c.verification_status}] {c.statement} {' '.join('[' + cid + ']' for cid in c.citations)}"
                                for c in claims
                            ),
                        },
                        {
                            "heading": "Sources",
                            "body": "\n".join(f"- [{s.source_id}] {s.title}: {s.url} (sha256={s.sha256[:12]})" for s in snapshots),
                        },
                    ]
                }
            )
            try:
                gen = self.document_service.generate(
                    fmt_clean,
                    filename=f"research-{task_id[:8]}",
                    title=f"Deep Research: {question[:90]}",
                    payload=payload_doc,
                )
                export_outcome = gen.get("outcome")
                if gen.get("ok") and gen.get("path"):
                    generated_files.append(str(gen["path"]))
                else:
                    export_ok = False
                    export_error = str((export_outcome or {}).get("error") or (export_outcome or {}).get("summary") or f"{fmt_clean} generation unverified")
                    warnings.append(f"Artifact export failed ({fmt_clean}): {export_error}")
            except Exception as exc:
                export_ok = False
                export_error = f"{type(exc).__name__}: {exc}"
                warnings.append(f"Artifact export failed ({fmt_clean}): {export_error}")
                export_outcome = ToolOutcome(
                    tool_name=f"generate_{fmt_clean}",
                    status=ToolStatus.FAILED.value,
                    summary=f"Failed exporting {fmt_clean} report: {exc}",
                    checks=[VerificationCheck("document_generated", False, export_error).to_dict()],
                    error=export_error,
                ).to_dict()

        if ctx is not None:
            await ctx.complete_step(
                "step_verify_export",
                checkpoint_patch={
                    "claims": [c.to_dict() for c in claims],
                    "verification": verification.to_dict(),
                    "generated_files": generated_files,
                    "integrity_warnings": integrity_warnings,
                    "rejected_snapshots": rejected_snaps,
                    "export_outcome": export_outcome,
                },
                current_step=f"Verified report ({verification.verified_claim_count} verified, {verification.disputed_claim_count} disputed, {verification.unverified_claim_count} unverified)",
            )

        freshness, limitations = build_freshness_and_limitations(
            snapshots,
            verification,
            online_allowed=online_allowed,
            warnings=warnings,
        )
        return {
            "ok": True,
            "task_id": task_id,
            "question": question,
            "mode": "online" if online_allowed else "local_kb_only",
            "plan": plan.to_dict(),
            "sources": [s.to_metadata_dict() for s in snapshots],
            "rejected_snapshots": rejected_snaps,
            "integrity_warnings": integrity_warnings,
            "claims": [c.to_dict() for c in claims],
            "verification": verification.to_dict(),
            "contradictions": list(verification.contradictions_found),
            "freshness": freshness,
            "limitations": limitations,
            "warnings": warnings,
            "markdown": markdown,
            "summary": summary,
            "generated_files": generated_files,
            "outcome": export_outcome,
            "export_ok": export_ok,
            "export_error": export_error,
            "untrusted_policy": UNTRUSTED_DATA_POLICY,
        }


async def run_deep_research_background_task(
    ctx: TaskRunContext,
    task: dict[str, Any],
    *,
    engine: DeepResearchEngine,
) -> TaskExecutionResult:
    """Adapter running DeepResearchEngine cleanly with separate research vs artifact outcomes."""
    payload = dict(task.get("payload") or {})
    export_req = bool(payload.get("export_artifact", True))
    export_fmt = str(payload.get("export_format") or "md")
    res = await engine.run(
        task_id=task["task_id"],
        question=str(payload.get("question") or task.get("user_request") or ""),
        ctx=ctx,
        export_format=export_fmt if export_req else None,
        local_folders=list(payload.get("local_folders") or []),
    )
    ver = res["verification"]
    all_corrupted = bool(res.get("integrity_warnings")) and len(res.get("sources") or []) == 0
    if all_corrupted:
        task_outcome = ToolOutcome(
            tool_name="deep_research",
            status=ToolStatus.UNVERIFIED.value,
            summary="Research completed with zero surviving integrity-verified snapshots",
            target=task["task_id"],
            evidence={"integrity_warnings": res.get("integrity_warnings") or []},
            checks=[VerificationCheck("snapshot_integrity", False, "no surviving valid snapshots").to_dict()],
        )
    else:
        task_outcome = ToolOutcome(
            tool_name="deep_research",
            status=ToolStatus.DONE.value,
            summary=f"Deep research completed ({ver['verified_claim_count']} verified, {ver['disputed_claim_count']} disputed, {ver['unverified_claim_count']} unverified)",
            target=task["task_id"],
            evidence={
                "source_count": len(res["sources"]),
                "overall_status": ver["overall_status"],
                "integrity_warnings": len(res.get("integrity_warnings") or []),
            },
            checks=[
                VerificationCheck("research_completed", True, f"sources={len(res['sources'])}").to_dict(),
                VerificationCheck("claim_verification_ran", True, f"status={ver['overall_status']}").to_dict(),
            ],
        )

    return TaskExecutionResult(
        summary=f"Deep research finished ({ver['verified_claim_count']} verified claims, {len(res['sources'])} sources)",
        result=res["markdown"],
        generated_files=res["generated_files"],
        outcome=task_outcome.to_dict(),
        artifact_outcome=res.get("outcome") if export_req else None,
        meta_patch={
            "research_question": res["question"],
            "research_summary": res["summary"],
            "research_claims": res["claims"],
            "research_verification": res["verification"],
            "research_sources": res["sources"],
            "research_freshness": res["freshness"],
            "research_limitations": res["limitations"],
            "research_contradictions": res["contradictions"],
            "research_warnings": res["warnings"],
            "research_mode": res["mode"],
            "integrity_warnings": res.get("integrity_warnings") or [],
            "rejected_snapshots": res.get("rejected_snapshots") or [],
            "export_ok": res.get("export_ok", True),
            "export_error": res.get("export_error", ""),
        },
    )
