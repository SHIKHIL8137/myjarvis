"""P10.22 P0.3 Multi-Stage Verified Deep Research Engine.

Implements:
PLAN -> INVESTIGATE -> REVIEW -> WRITE -> VERIFY -> REVISE (bounded <= 2 cycles)
with SHA-256 snapshot persistence, normalized quote matching, strict untrusted
boundaries, Local Mode KB-only fallback, zero personal-memory exfiltration,
and verified Markdown/PDF/DOCX export via DocumentService.
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
from jarvis.security.outcomes import verify_file_created
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
    kind: str = "verified_fact"  # "verified_fact" | "reported_claim" | "inference" | "unverified"
    citations: list[str] = field(default_factory=list)
    quote: str = ""
    contradicted: bool = False
    verification_note: str = ""

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
    missing_citations: list[int] = field(default_factory=list)
    invalid_citations: list[str] = field(default_factory=list)
    quote_mismatches: list[dict[str, Any]] = field(default_factory=list)
    contradictions_found: list[str] = field(default_factory=list)
    duplicate_urls: list[str] = field(default_factory=list)
    unverified_claim_count: int = 0
    verified_claim_count: int = 0
    revision_cycle: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SynthesizerFn = Callable[[str, list[dict[str, Any]], list[str]], Awaitable[dict[str, Any]] | dict[str, Any]]


def _extract_subqueries(question: str, steer_notes: list[str] | None = None, *, max_queries: int = MAX_SUBQUERIES) -> list[str]:
    base = re.sub(r"^(?:deep\s+research|research|investigate|compare|analyze)\s+(?:on|about)?\s*", "", (question or "").strip(), flags=re.I).strip()
    if not base:
        base = (question or "").strip() or "overview"

    candidates: list[str] = [base]
    # Split compound comparison / multi-part clauses deterministically
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


_POLARITY_STOPWORDS = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "have",
        "been",
        "will",
        "also",
        "however",
        "supported",
        "deprecated",
        "false",
        "true",
        "recommended",
        "enabled",
        "default",
        "longer",
        "without",
        "officially",
        "introduces",
    }
)


def _detect_contradictions(snapshots: list[ResearchSnapshot]) -> list[str]:
    """Identify negation/support divergences across distinct retrieved sources sharing subject terms."""
    contradictions: list[str] = []
    pos_items: list[tuple[str, set[str], str]] = []
    neg_items: list[tuple[str, set[str], str]] = []
    for snap in snapshots:
        for line in snap.content.splitlines():
            low = line.lower()
            terms = {t for t in re.findall(r"[a-z0-9]{4,}", low) if t not in _POLARITY_STOPWORDS}
            if len(terms) < 2:
                continue
            if "deprecated" in low or "not supported" in low or "disabled by default" in low or "no longer" in low:
                neg_items.append((snap.source_id, terms, line.strip()[:140]))
            elif "supported" in low or "recommended" in low or "enabled by default" in low:
                pos_items.append((snap.source_id, terms, line.strip()[:140]))

    seen_pairs: set[tuple[str, str]] = set()
    for p_sid, p_terms, p_line in pos_items:
        for n_sid, n_terms, n_line in neg_items:
            if p_sid == n_sid:
                continue
            overlap = sorted(p_terms.intersection(n_terms))
            if len(overlap) >= 2:
                pair = (p_sid, n_sid)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    topic = ", ".join(overlap[:3])
                    contradictions.append(
                        f"Divergent statements on '{topic}': ({p_sid}: {p_line}) vs ({n_sid}: {n_line})"
                    )
    return contradictions[:5]


def verify_claims_against_snapshots(
    claims: list[ResearchClaim],
    snapshots: dict[str, ResearchSnapshot],
    contradictions: list[str],
    *,
    revision_cycle: int = 0,
) -> VerificationReport:
    """Deterministic quote and citation verification against saved full-text snapshots."""
    missing: list[int] = []
    invalid: list[str] = []
    mismatches: list[dict[str, Any]] = []
    normalized_snaps = {sid: normalize_for_quote_match(s.content) for sid, s in snapshots.items()}

    # Duplicate URL detection across cited source IDs
    seen_canon: dict[str, str] = {}
    dup_urls: list[str] = []
    for sid, snap in snapshots.items():
        c_url = snap.canonical_url or snap.url
        if c_url in seen_canon:
            dup_urls.append(f"{sid} duplicates {seen_canon[c_url]} ({c_url})")
        else:
            seen_canon[c_url] = sid

    for idx, cl in enumerate(claims):
        if cl.kind in ("inference", "unverified"):
            continue
        if not cl.citations:
            missing.append(idx)
            cl.verification_note = "No source citation provided"
            continue
        valid_cits = []
        for cid in cl.citations:
            if cid not in snapshots or not snapshots[cid].content.strip():
                if cid not in invalid:
                    invalid.append(cid)
            else:
                valid_cits.append(cid)
        if not valid_cits:
            missing.append(idx)
            cl.verification_note = f"All cited sources invalid/empty: {cl.citations}"
            continue

        if cl.quote and cl.quote.strip():
            nq = normalize_for_quote_match(cl.quote)
            matched = any(nq in normalized_snaps[cid] for cid in valid_cits)
            if not matched:
                mismatches.append({"claim_index": idx, "citations": valid_cits, "quote": cl.quote[:160]})
                cl.verification_note = "Cited quote not found verbatim in retrieved snapshot text"
                continue
        cl.verification_note = "Verified against retrieved source snapshot"

    passed = not missing and not invalid and not mismatches
    verified_count = sum(1 for i, c in enumerate(claims) if c.kind in ("verified_fact", "reported_claim") and i not in missing and not any(m["claim_index"] == i for m in mismatches))
    unverified_count = sum(1 for c in claims if c.kind == "unverified")
    return VerificationReport(
        passed=passed,
        missing_citations=missing,
        invalid_citations=invalid,
        quote_mismatches=mismatches,
        contradictions_found=list(contradictions),
        duplicate_urls=dup_urls,
        unverified_claim_count=unverified_count,
        verified_claim_count=verified_count,
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
        online_allowed_fn: Callable[[], bool] | None = None,
        synthesizer_fn: SynthesizerFn | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.search_service = search_service
        self.knowledge_pipeline = knowledge_pipeline
        self.document_service = document_service or DocumentService(self.data_dir / "outputs" / "documents")
        self.online_allowed_fn = online_allowed_fn or (lambda: False)
        self.synthesizer_fn = synthesizer_fn

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
        # Post-condition check: confirm saved file on disk matches digest
        verify_res = verify_file_created(snap_file, tool_name="research_snapshot_write", min_bytes=0, expected_sha256=digest)
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

    def _load_snapshots_from_checkpoint(self, checkpoint: dict[str, Any]) -> list[ResearchSnapshot]:
        out: list[ResearchSnapshot] = []
        for meta in checkpoint.get("snapshots") or []:
            sp = Path(str(meta.get("snapshot_path") or ""))
            content = sp.read_text(encoding="utf-8", errors="replace") if sp.exists() and sp.is_file() else str(meta.get("snippet") or "")
            out.append(
                ResearchSnapshot(
                    source_id=str(meta["source_id"]),
                    title=str(meta.get("title") or ""),
                    url=str(meta.get("url") or ""),
                    canonical_url=str(meta.get("canonical_url") or canonicalize_url(meta.get("url", ""))),
                    source_type=str(meta.get("source_type") or "web_page"),
                    sha256=str(meta.get("sha256") or ""),
                    fetched_at=float(meta.get("fetched_at") or 0.0),
                    snapshot_path=str(sp),
                    content=content,
                    snippet=str(meta.get("snippet") or ""),
                    query=str(meta.get("query") or ""),
                )
            )
        return out

    async def _investigate_query(
        self,
        task_id: str,
        subquery: str,
        *,
        online_allowed: bool,
        snapshots: list[ResearchSnapshot],
        seen_urls: set[str],
        max_pages: int = MAX_PAGES_PER_QUERY,
    ) -> list[str]:
        warnings: list[str] = []
        if len(snapshots) >= MAX_TOTAL_PAGES:
            return warnings

        # 1) Always consult local knowledge base if available (isolated from personal memory/notes!)
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

        # 2) External web search/fetch strictly ONLY in Online Assisted Mode
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

            seen_urls.add(canon)
            seen_urls.add(canonicalize_url(url))
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

        # Deterministic structured grounded fallback (no hallucinated facts)
        fallback_claims: list[ResearchClaim] = []
        if not snapshots:
            mode_note = (
                "No matching local knowledge base documents were found while operating strictly in offline Local Mode."
                if not online_allowed
                else "No readable web or local knowledge sources were retrieved."
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
                    statement=f"Source {s.title} reports: {lead_quote}",
                    kind="reported_claim",
                    citations=[s.source_id],
                    quote=lead_quote,
                    contradicted=bool(contradictions),
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

    def _revise_claims(
        self,
        claims: list[ResearchClaim],
        report: VerificationReport,
        snapshots_by_id: dict[str, ResearchSnapshot],
    ) -> list[ResearchClaim]:
        """Deterministically downgrade or fix invalid/ungrounded citations in revision cycles."""
        bad_quotes = {m["claim_index"] for m in report.quote_mismatches}
        missing = set(report.missing_citations)
        invalid_ids = set(report.invalid_citations)

        revised: list[ResearchClaim] = []
        for idx, cl in enumerate(claims):
            # Prune non-existent cited IDs
            cl.citations = [c for c in cl.citations if c not in invalid_ids and c in snapshots_by_id]
            if idx in missing or not cl.citations or idx in bad_quotes:
                if cl.kind != "inference":
                    cl.kind = "unverified"
                    if "[unverified]" not in cl.statement:
                        cl.statement = f"{cl.statement} [unverified]"
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
            f"- **Sources Retrieved**: {len(snapshots)}",
            f"- **Verified Claims**: {verification.verified_claim_count}",
            f"- **Unverified / Marked Claims**: {verification.unverified_claim_count}",
            f"- **Verification Status**: {'PASSED' if verification.passed else 'UNVERIFIED ITEMS FLAGGED'} (cycles: {verification.revision_cycle})",
            "",
            "## Research Plan",
            "",
        ]
        for q in plan.subqueries:
            lines.append(f"- Query: `{q}`")
        lines.extend(["", "## Key Findings", ""])

        verified_facts = [c for c in claims if c.kind in ("verified_fact", "reported_claim")]
        inferences = [c for c in claims if c.kind == "inference"]
        unverified = [c for c in claims if c.kind == "unverified"]

        lines.append("### Grounded Facts & Reported Claims")
        lines.append("")
        if not verified_facts:
            lines.append("- None verified from available sources.")
        else:
            for c in verified_facts:
                cites = " ".join(f"[{cid}]" for cid in c.citations)
                tag = "**Fact**" if c.kind == "verified_fact" else "Reported"
                lines.append(f"- ({tag}) {c.statement} {cites}")
                if c.quote:
                    lines.append(f"  > Quote: \"{c.quote}\"")

        if inferences:
            lines.extend(["", "### Inferences & Cross-Source Synthesis", ""])
            for c in inferences:
                lines.append(f"- (Inference) {c.statement}")

        if unverified:
            lines.extend(["", "### Unverified Claims & Gaps", ""])
            for c in unverified:
                lines.append(f"- (Unverified) {c.statement}")

        if verification.contradictions_found:
            lines.extend(["", "## Contradictions & Conflicting Evidence", ""])
            for ct in verification.contradictions_found:
                lines.append(f"- {ct}")

        if warnings:
            lines.extend(["", "## Retrieval Limitations & Notices", ""])
            for w in warnings:
                lines.append(f"- {w}")

        lines.extend(["", "## Bibliography & Snapshot Hashes", ""])
        if not snapshots:
            lines.append("- No external or local source snapshots were captured.")
        else:
            for s in snapshots:
                lines.append(
                    f"- **[{s.source_id}]** {s.title} — {s.url} (`{s.source_type}`, sha256=`{s.sha256[:16]}...`, snapshot=`{s.snapshot_path}`)"
                )
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
    ) -> dict[str, Any]:
        """Run or resume full PLAN -> INVESTIGATE -> REVIEW -> WRITE -> VERIFY -> REVISE pipeline."""
        online_allowed = bool(self.online_allowed_fn())
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

        # STAGE 2: INVESTIGATE (resume incremental snapshots per subquery!)
        snapshots: list[ResearchSnapshot] = self._load_snapshots_from_checkpoint(checkpoint)
        seen_urls: set[str] = {canonicalize_url(s.url) or s.canonical_url for s in snapshots if (s.url or s.canonical_url)}
        warnings: list[str] = list(checkpoint.get("warnings") or [])

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
            )
            warnings.extend(w)
            if ctx is not None:
                await ctx.complete_step(
                    step_key,
                    checkpoint_patch={
                        "snapshots": [s.to_metadata_dict() for s in snapshots],
                        "warnings": warnings,
                    },
                    current_step=f"Investigated query {idx + 1}/{len(plan.subqueries)} ({len(snapshots)} sources)",
                    delta_tools=1,
                )

        # STAGE 3: REVIEW (corroboration + contradiction check + bounded follow-up)
        if "step_review" in done_steps and "contradictions" in checkpoint:
            contradictions = list(checkpoint["contradictions"])
        else:
            contradictions = _detect_contradictions(snapshots)
            # Bounded follow-up if <2 sources and online and budget allows
            if len(snapshots) < 2 and len(plan.subqueries) < MAX_SUBQUERIES and online_allowed:
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

        # STAGE 5 & 6: VERIFY -> REVISE (bounded <= MAX_REVISION_CYCLES)
        snaps_by_id = {s.source_id: s for s in snapshots}
        cycle = 0
        verification = verify_claims_against_snapshots(claims, snaps_by_id, contradictions, revision_cycle=cycle)
        while not verification.passed and cycle < MAX_REVISION_CYCLES:
            cycle += 1
            claims = self._revise_claims(claims, verification, snaps_by_id)
            verification = verify_claims_against_snapshots(claims, snaps_by_id, contradictions, revision_cycle=cycle)

        markdown = self.render_markdown(
            question=question,
            plan=plan,
            claims=claims,
            snapshots=snapshots,
            verification=verification,
            online_allowed=online_allowed,
            warnings=warnings,
        )

        # STAGE 7: VERIFIED EXPORT (when requested, defaults to Markdown artifact)
        generated_files: list[str] = []
        export_outcome: dict[str, Any] | None = None
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
                                f"- [{c.kind}] {c.statement} {' '.join('[' + cid + ']' for cid in c.citations)}"
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
            gen = self.document_service.generate(
                fmt_clean,
                filename=f"research-{task_id[:8]}",
                title=f"Deep Research: {question[:90]}",
                payload=payload_doc,
            )
            export_outcome = gen.get("outcome")
            if gen.get("ok") and gen.get("path"):
                generated_files.append(str(gen["path"]))

        if ctx is not None:
            await ctx.complete_step(
                "step_verify_export",
                checkpoint_patch={
                    "claims": [c.to_dict() for c in claims],
                    "verification": verification.to_dict(),
                    "generated_files": generated_files,
                },
                current_step=f"Verified report ({verification.verified_claim_count} grounded, {verification.unverified_claim_count} unverified)",
            )

        return {
            "ok": True,
            "task_id": task_id,
            "question": question,
            "mode": "online" if online_allowed else "local_kb_only",
            "plan": plan.to_dict(),
            "sources": [s.to_metadata_dict() for s in snapshots],
            "claims": [c.to_dict() for c in claims],
            "verification": verification.to_dict(),
            "markdown": markdown,
            "summary": summary,
            "generated_files": generated_files,
            "outcome": export_outcome,
            "untrusted_policy": UNTRUSTED_DATA_POLICY,
        }


async def run_deep_research_background_task(
    ctx: TaskRunContext,
    task: dict[str, Any],
    *,
    engine: DeepResearchEngine,
) -> TaskExecutionResult:
    """Adapter running DeepResearchEngine cleanly inside BackgroundTaskRunner."""
    payload = dict(task.get("payload") or {})
    export_fmt = str(payload.get("export_format") or "md")
    res = await engine.run(
        task_id=task["task_id"],
        question=str(payload.get("question") or task.get("user_request") or ""),
        ctx=ctx,
        export_format=export_fmt if payload.get("export_artifact", True) else None,
    )
    return TaskExecutionResult(
        summary=f"Deep research finished ({res['verification']['verified_claim_count']} verified claims, {len(res['sources'])} sources)",
        result=res["markdown"],
        generated_files=res["generated_files"],
        outcome=res.get("outcome"),
        meta_patch={
            "research_verification": res["verification"],
            "research_sources": res["sources"],
            "research_mode": res["mode"],
        },
    )
