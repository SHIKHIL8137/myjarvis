"""Multilingual STT & ASR Benchmark Runner for Jarvis.
Calculates per-language and per-category WER, CER, substitution/deletion/insertion rates,
language detection accuracy, latency, RTF, model loading time, and peak RAM."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.multilingual.metrics import compute_text_metrics, memory_snapshot_mb, script_profile  # noqa: E402
from jarvis.config import STTSpec  # noqa: E402
from jarvis.stt import SR, Whisper, list_local_models, wav_to_float  # noqa: E402

DEFAULT_MANIFEST = Path(__file__).resolve().parent / "manifest.json"


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return {"samples": raw, "targets": {}}
    return raw


def _summarize_group(rows: list[dict]) -> dict:
    evaluated = [r for r in rows if r.get("metrics") is not None]
    lang_checked = [r for r in rows if r.get("lang_correct") is not None]
    rtfs = [r["rtf"] for r in rows if r.get("rtf") is not None]
    secs = [r["seconds"] for r in rows]
    script_mismatches = sum(1 for r in rows if "script_mismatch" in (r.get("quality_flags") or []))
    empties = sum(1 for r in rows if not (r.get("hypothesis") or "").strip())
    hallucinations = sum(
        1
        for r in rows
        if any(
            f in (r.get("quality_flags") or [])
            for f in ("repetition_hallucination", "short_audio_long_hallucination", "implausible_word_rate")
        )
    )
    low_q = sum(1 for r in rows if r.get("recognition_quality") == "low")
    retries = sum(1 for r in rows if r.get("retry_triggered"))
    return {
        "count": len(rows),
        "evaluated_count": len(evaluated),
        "mean_wer": round(sum(r["metrics"]["wer"] for r in evaluated) / len(evaluated), 4) if evaluated else None,
        "mean_cer": round(sum(r["metrics"]["cer"] for r in evaluated) / len(evaluated), 4) if evaluated else None,
        "mean_sub_rate": round(sum(r["metrics"]["substitution_rate"] for r in evaluated) / len(evaluated), 4) if evaluated else None,
        "mean_del_rate": round(sum(r["metrics"]["deletion_rate"] for r in evaluated) / len(evaluated), 4) if evaluated else None,
        "mean_ins_rate": round(sum(r["metrics"]["insertion_rate"] for r in evaluated) / len(evaluated), 4) if evaluated else None,
        "lang_detection_accuracy": (
            round(sum(1 for r in lang_checked if r["lang_correct"]) / len(lang_checked), 4)
            if lang_checked else None
        ),
        "script_mismatch_count": script_mismatches,
        "script_mismatch_rate": round(script_mismatches / len(rows), 4) if rows else 0.0,
        "empty_count": empties,
        "hallucination_or_loop_count": hallucinations,
        "low_quality_count": low_q,
        "fallback_triggered_count": retries,
        "mean_seconds": round(sum(secs) / len(secs), 3) if secs else 0.0,
        "mean_rtf": round(sum(rtfs) / len(rtfs), 3) if rtfs else None,
    }


def _expected_lang_hit(expected: str, detected: str, hyp_text: str) -> bool | None:
    exp = (expected or "").strip().lower()
    det = (detected or "").strip().lower()
    if exp in ("", "auto"):
        return None
    if exp == "mixed":
        # Mixed Malayalam-English can legitimately be labeled ml, en, or mixed; check whether detector stayed in {ml, en, mixed}
        # and note script balance separately.
        return det in ("ml", "en", "mixed")
    return det == exp


def run_benchmark(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    models: list[str] | None = None,
    qualities: list[str] | None = None,
    threads: int = 6,
    output_json: Path | None = None,
    output_md: Path | None = None,
) -> dict:
    manifest = load_manifest(manifest_path)
    base_dir = Path(manifest_path).resolve().parent
    samples_raw = manifest.get("samples", [])
    available = list_local_models()
    chosen_models = [m for m in (models or available) if m in available] or list(available)
    chosen_qualities = qualities or ["fast", "balanced", "accurate"]

    loaded_samples = []
    missing_samples = []
    for item in samples_raw:
        rel_audio = item.get("audio") or item.get("file") or ""
        audio_path = (base_dir / rel_audio).resolve() if rel_audio else None
        if not audio_path or not audio_path.exists():
            missing_samples.append(item.get("id", rel_audio))
            continue
        f32 = wav_to_float(audio_path.read_bytes())
        loaded_samples.append({
            "id": item["id"],
            "audio_path": str(audio_path),
            "language": (item.get("language") or "auto").lower(),
            "requested_language": (item.get("requested_language") or "auto").lower(),
            "category": item.get("category") or "normal_speech",
            "reference": item.get("reference") or "",
            "reference_verified": bool(item.get("reference_verified", bool(item.get("reference")))),
            "source": item.get("source", "user"),
            "audio": f32,
            "duration_s": round(len(f32) / SR, 3),
        })

    runs = []
    for model in chosen_models:
        for quality in chosen_qualities:
            mem0 = memory_snapshot_mb()
            spec = STTSpec(
                model=model,
                compute_type="int8",
                cpu_threads=threads,
                language="auto",
                quality=quality,
            )
            stt = Whisper(spec)
            t_load = time.perf_counter()
            stt.load()
            load_s = round(time.perf_counter() - t_load, 3)
            mem_after_load = memory_snapshot_mb()

            rows = []
            by_lang: dict[str, list[dict]] = defaultdict(list)
            by_cat: dict[str, list[dict]] = defaultdict(list)

            for s in loaded_samples:
                res = stt.transcribe(
                    s["audio"],
                    language=s.get("requested_language", "auto"),
                    save_audio=False,
                )
                hyp = (res.get("original_transcript") or res.get("text") or "").strip()
                has_ref = bool(s["reference"].strip()) and s["reference_verified"]
                metrics = compute_text_metrics(s["reference"], hyp) if has_ref else None
                lang_hit = _expected_lang_hit(s["language"], res.get("detected_language", ""), hyp)
                row = {
                    "id": s["id"],
                    "expected_language": s["language"],
                    "requested_language": s.get("requested_language", "auto"),
                    "category": s["category"],
                    "source": s["source"],
                    "reference_verified": s["reference_verified"],
                    "duration_s": s["duration_s"],
                    "seconds": float(res.get("seconds") or 0.0),
                    "rtf": round(float(res.get("seconds") or 0.0) / max(0.05, s["duration_s"]), 3),
                    "detected_language": res.get("detected_language"),
                    "language_probability": res.get("language_probability"),
                    "lang_correct": lang_hit,
                    "confidence": res.get("confidence"),
                    "avg_logprob": res.get("avg_logprob"),
                    "no_speech_prob": res.get("no_speech_prob"),
                    "compression_ratio": res.get("compression_ratio"),
                    "recognition_quality": res.get("recognition_quality"),
                    "quality_flags": list(res.get("quality_flags") or []),
                    "retry_triggered": bool(res.get("retry_triggered", False)),
                    "script": script_profile(hyp),
                    "reference": s["reference"],
                    "hypothesis": hyp,
                    "metrics": metrics,
                }
                rows.append(row)
                by_lang[s["language"]].append(row)
                by_cat[s["category"]].append(row)

            mem_peak = memory_snapshot_mb()
            run_summary = {
                "model": model,
                "quality": quality,
                "cpu_threads": threads,
                "load_seconds": load_s,
                "rss_before_mb": mem0["rss_mb"],
                "rss_after_load_mb": mem_after_load["rss_mb"],
                "peak_rss_mb": mem_peak["peak_rss_mb"],
                "overall": _summarize_group(rows),
                "by_language": {lang: _summarize_group(g) for lang, g in sorted(by_lang.items())},
                "by_category": {cat: _summarize_group(g) for cat, g in sorted(by_cat.items())},
                "cases": rows,
            }
            runs.append(run_summary)

    # Coverage audit: never claim Malayalam/Hindi/Tamil WER from English-only samples
    lang_counts: dict[str, int] = defaultdict(int)
    verified_lang_counts: dict[str, int] = defaultdict(int)
    for s in loaded_samples:
        lang_counts[s["language"]] += 1
        if s["reference_verified"] and s["reference"].strip():
            verified_lang_counts[s["language"]] += 1

    report = {
        "machine": "Ryzen 5 5600H / GTX 1650 4GB / 16GB RAM / CPU int8",
        "manifest": str(manifest_path),
        "targets": manifest.get("targets", {}),
        "available_models": available,
        "samples_loaded": len(loaded_samples),
        "missing_sample_files": missing_samples,
        "language_sample_counts": dict(sorted(lang_counts.items())),
        "verified_reference_counts": dict(sorted(verified_lang_counts.items())),
        "runs": runs,
    }

    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if output_md:
        output_md.parent.mkdir(parents=True, exist_ok=True)
        output_md.write_text(render_comparison_markdown(report), encoding="utf-8")
    return report


def render_comparison_markdown(report: dict) -> str:
    lines = [
        "# Multilingual ASR Benchmark Report",
        "",
        f"- **Machine**: {report.get('machine')}",
        f"- **Manifest**: `{report.get('manifest')}` ({report.get('samples_loaded', 0)} WAVs loaded)",
        f"- **Verified Reference Counts by Language**: `{report.get('verified_reference_counts')}`",
        "",
        "## Model & Preset Summary",
        "",
        "| Model | Preset | Load (s) | Peak RSS (MB) | Mean Latency (s) | Mean RTF | Lang Acc | Mean WER | Mean CER | Sub Rate | Del Rate | Ins Rate | Script Mismatch | Mismatch Rate | Empty | Hallucinations |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in report.get("runs", []):
        o = r["overall"]
        fmt_pct = lambda v: f"{v * 100:.1f}%" if isinstance(v, (int, float)) else "N/A"
        lines.append(
            f"| {r['model']} | {r['quality']} | {r['load_seconds']:.2f} | {r['peak_rss_mb']:.1f} | "
            f"{o['mean_seconds']:.2f} | {o['mean_rtf'] or 0.0:.2f}x | {fmt_pct(o['lang_detection_accuracy'])} | "
            f"{fmt_pct(o['mean_wer'])} | {fmt_pct(o['mean_cer'])} | {fmt_pct(o['mean_sub_rate'])} | "
            f"{fmt_pct(o['mean_del_rate'])} | {fmt_pct(o['mean_ins_rate'])} | "
            f"{o.get('script_mismatch_count', 0)} | {fmt_pct(o.get('script_mismatch_rate'))} | {o.get('empty_count', 0)} | {o.get('hallucination_or_loop_count', 0)} |"
        )
    lines.append("")
    lines.append("## Per-Language Breakdown")
    lines.append("")
    lines.append("> WER/CER are computed strictly on samples with `reference_verified: true`; unverified or missing references report `N/A`.")
    lines.append("")
    lines.append("| Model | Preset | Language | Samples | Evaluated | Lang Acc | WER | CER | Script Mismatch | Mismatch Rate | Empty |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in report.get("runs", []):
        for lang, g in r.get("by_language", {}).items():
            fmt_pct = lambda v: f"{v * 100:.1f}%" if isinstance(v, (int, float)) else "N/A"
            lines.append(
                f"| {r['model']} | {r['quality']} | `{lang}` | {g['count']} | {g['evaluated_count']} | "
                f"{fmt_pct(g['lang_detection_accuracy'])} | {fmt_pct(g['mean_wer'])} | {fmt_pct(g['mean_cer'])} | "
                f"{g.get('script_mismatch_count', 0)} | {fmt_pct(g.get('script_mismatch_rate'))} | {g.get('empty_count', 0)} |"
            )
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description="Jarvis Multilingual ASR Benchmark")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--models", nargs="*", default=None)
    p.add_argument("--qualities", nargs="*", default=["fast", "balanced", "accurate"])
    p.add_argument("--threads", type=int, default=6)
    p.add_argument("--output-json", type=Path, default=Path(__file__).resolve().parent / "results.json")
    p.add_argument("--output-md", type=Path, default=Path(__file__).resolve().parent / "REPORT.md")
    args = p.parse_args()
    rep = run_benchmark(
        manifest_path=args.manifest,
        models=args.models,
        qualities=args.qualities,
        threads=args.threads,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    print(render_comparison_markdown(rep))


if __name__ == "__main__":
    main()

