from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from .parsers import parse_manuscript
from .reports import build_html_report
from .services import fetch_metadata, screen_pair


ProgressCallback = Optional[Callable[[int, str], None]]
ReferenceResolver = Optional[Callable[[str], Dict[str, Any]]]


def _report(callback: ProgressCallback, percent: int, message: str) -> None:
    if callback:
        callback(percent, message)


def _merge_ref_result(ref: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(ref)
    merged["doi"] = result.get("api_doi") or result.get("extracted_doi") or merged.get("doi", "")
    merged["pmid"] = result.get("pmid") or merged.get("pmid", "")
    merged["pmcid"] = result.get("pmcid") or merged.get("pmcid", "")
    merged["title"] = result.get("title") or merged.get("title", "")
    return merged


def prepare_screening(
    file_bytes: bytes,
    filename: str,
    pasted_references: str = "",
    reference_resolver: ReferenceResolver = None,
    progress_callback: ProgressCallback = None,
    max_workers: int = 3,
) -> Dict[str, Any]:
    """Parse, resolve references and fetch abstracts without calling DeepSeek."""
    _report(progress_callback, 5, "正在解析稿件和引用标记…")
    sentences, references = parse_manuscript(file_bytes, filename, pasted_references)
    if not sentences:
        raise ValueError("没有识别到包含参考文献编号的正文内容。")

    cited_rids = list(dict.fromkeys(
        citation["rid"] for sentence in sentences for citation in sentence.get("citations", [])
    ))
    _report(progress_callback, 18, f"识别到 {len(sentences)} 个引用位置、{len(cited_rids)} 篇被引文献。")
    for index, rid in enumerate(cited_rids, 1):
        ref = references.get(rid)
        if not ref:
            references[rid] = {
                "rid": rid, "label": rid.lstrip("B"), "raw_reference": "",
                "doi": "", "pmid": "", "pmcid": "", "title": "",
                "mapping_error": "正文引用编号未在参考文献列表中找到",
            }
            continue
        if reference_resolver and ref.get("raw_reference"):
            try:
                references[rid] = _merge_ref_result(ref, reference_resolver(ref["raw_reference"]))
            except Exception as exc:
                references[rid]["mapping_error"] = str(exc)[:240]
        _report(progress_callback, 18 + int(30 * index / max(1, len(cited_rids))), f"正在匹配 Ref. {ref.get('label', index)}…")

    metadata_by_rid: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(cited_rids)))) as executor:
        futures = {executor.submit(fetch_metadata, references[rid]): rid for rid in cited_rids}
        completed = 0
        for future in as_completed(futures):
            rid = futures[future]
            try:
                metadata_by_rid[rid] = future.result()
            except Exception as exc:
                metadata_by_rid[rid] = {"title": references[rid].get("title", ""), "abstract": "", "authors": [], "metadata_source": "", "metadata_error": str(exc)[:240]}
            completed += 1
            _report(progress_callback, 48 + int(50 * completed / max(1, len(cited_rids))), f"正在获取题名和摘要（{completed}/{len(cited_rids)}）…")

    pairs: List[Dict[str, Any]] = []
    for sentence_order, sentence in enumerate(sentences):
        for citation in sentence.get("citations", []):
            rid = citation["rid"]
            ref = references[rid]
            metadata = metadata_by_rid.get(rid, {})
            requires_ai = bool(metadata.get("abstract"))
            pairs.append({
                "sentence_order": sentence_order,
                "sentence_id": sentence["sentence_id"],
                "sentence_text": sentence["sentence_text"],
                "context_before": sentence.get("context_before", ""),
                "context_after": sentence.get("context_after", ""),
                "full_block": sentence.get("full_block", sentence["sentence_text"]),
                "source": sentence.get("source", "body_text"),
                "location": sentence.get("location", {}),
                "rid": rid,
                "label": ref.get("label") or rid.lstrip("B"),
                "raw_reference": ref.get("raw_reference", ""),
                "doi": metadata.get("doi") or ref.get("doi", ""),
                "pmid": ref.get("pmid", ""),
                "pmcid": metadata.get("pmcid") or ref.get("pmcid", ""),
                "title": metadata.get("title") or ref.get("title", ""),
                "abstract": metadata.get("abstract", ""),
                "metadata_source": metadata.get("metadata_source", ""),
                "requires_ai": requires_ai,
            })
    estimated_calls = sum(1 for pair in pairs if pair["requires_ai"])
    _report(progress_callback, 100, "稿件准备完成，尚未调用 DeepSeek。")
    return {
        "filename": filename,
        "citation_locations": len(sentences),
        "cited_references": len(cited_rids),
        "total_pairs": len(pairs),
        "direct_doubt": len(pairs) - estimated_calls,
        "estimated_calls": estimated_calls,
        "pairs": pairs,
    }


def execute_screening(
    prepared: Dict[str, Any], progress_callback: ProgressCallback = None, max_workers: int = 3
) -> Dict[str, Any]:
    """Call DeepSeek only for prepared pairs with abstracts."""
    pairs = prepared["pairs"]
    results: List[Dict[str, Any]] = []
    ai_pairs = [pair for pair in pairs if pair["requires_ai"]]
    for pair in pairs:
        if not pair["requires_ai"]:
            results.append({**pair, "result": "存疑", "reason": "未获取到文献摘要，无法可靠判断支持程度。"})

    actual_calls = 0
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(ai_pairs) or 1))) as executor:
        futures = {
            executor.submit(
                screen_pair, pair["sentence_text"], pair,
                pair["context_before"], pair["context_after"], pair["full_block"],
            ): pair for pair in ai_pairs
        }
        completed = 0
        for future in as_completed(futures):
            pair = futures[future]
            decision = future.result()
            actual_calls += int(decision.get("api_called", False))
            results.append({**pair, "result": decision["result"], "reason": decision["reason"]})
            completed += 1
            _report(progress_callback, int(98 * completed / max(1, len(ai_pairs))), f"DeepSeek 正在初筛（{completed}/{len(ai_pairs)}）…")

    results.sort(key=lambda x: (x["sentence_order"], int(x["label"]) if str(x["label"]).isdigit() else 999999))
    counts = {label: sum(1 for item in results if item["result"] == label) for label in ("匹配", "存疑", "领域不符")}
    _report(progress_callback, 100, "引用内容初筛完成。")
    return {
        "filename": prepared["filename"],
        "statistics": {"total": len(results), **counts},
        "estimated_calls": prepared["estimated_calls"],
        "actual_calls": actual_calls,
        "results": results,
        "html": build_html_report(prepared["filename"], results),
    }


def run_screening(*args, **kwargs) -> Dict[str, Any]:
    """Backward-compatible one-shot wrapper used by tests and programmatic callers."""
    prepared = prepare_screening(*args, **kwargs)
    callback = kwargs.get("progress_callback")
    return execute_screening(prepared, progress_callback=callback, max_workers=kwargs.get("max_workers", 3))
