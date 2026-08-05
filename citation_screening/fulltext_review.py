from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any, Callable, Dict, Optional

from .reports import build_html_report
from .services.fulltext import fetch_open_fulltext, rank_paragraphs
from .services.fulltext_deepseek import check_fulltext_paragraph


ProgressCallback = Optional[Callable[[int, str], None]]


def _report(callback: ProgressCallback, percent: int, message: str) -> None:
    if callback:
        callback(percent, message)


def prepare_fulltext_review(
    screening_result: Dict[str, Any],
    progress_callback: ProgressCallback = None,
    max_paragraphs: int = 8,
    max_workers: int = 3,
) -> Dict[str, Any]:
    base_results = deepcopy(screening_result.get("results", []))
    doubtful_indices = [index for index, item in enumerate(base_results) if item.get("result") == "存疑"]
    if not doubtful_indices:
        raise ValueError("当前结果中没有需要全文复核的“存疑”文献。")

    refs = {}
    for index in doubtful_indices:
        item = base_results[index]
        refs.setdefault(item.get("rid") or f"row-{index}", item)
    fulltexts = {}
    _report(progress_callback, 5, "正在查找合法开放全文…")
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(refs)))) as executor:
        futures = {executor.submit(fetch_open_fulltext, ref): rid for rid, ref in refs.items()}
        completed = 0
        for future in as_completed(futures):
            rid = futures[future]
            try:
                fulltexts[rid] = future.result()
            except Exception:
                fulltexts[rid] = None
            completed += 1
            _report(progress_callback, 5 + int(70 * completed / len(refs)), f"正在获取开放全文（{completed}/{len(refs)}）…")

    review_items = []
    found_refs = set()
    for index in doubtful_indices:
        claim = base_results[index]
        rid = claim.get("rid") or f"row-{index}"
        document = fulltexts.get(rid)
        if not document:
            claim["fulltext_review_status"] = "未取得开放全文"
            continue
        query = f"{claim.get('sentence_text', '')} {claim.get('title', '')}"
        candidates = rank_paragraphs(query, document["paragraphs"], limit=max_paragraphs)
        if not candidates:
            claim["fulltext_review_status"] = "全文无法提取有效段落"
            continue
        found_refs.add(rid)
        review_items.append({
            "result_index": index,
            "source": document["source"],
            "source_url": document.get("source_url", ""),
            "candidates": candidates,
        })

    estimated_calls = sum(len(item["candidates"]) for item in review_items)
    _report(progress_callback, 100, "全文候选段落准备完成，尚未调用 DeepSeek。")
    return {
        "filename": screening_result.get("filename", "稿件"),
        "base_results": base_results,
        "review_items": review_items,
        "doubtful_count": len(doubtful_indices),
        "fulltexts_found": len(found_refs),
        "unavailable_count": len(doubtful_indices) - len(review_items),
        "estimated_calls": estimated_calls,
        "max_paragraphs": max_paragraphs,
    }


def _review_one(base_result: Dict[str, Any], review_item: Dict[str, Any]):
    updated = deepcopy(base_result)
    calls = 0
    checked = 0
    for paragraph in review_item["candidates"]:
        decision = check_fulltext_paragraph(updated, paragraph)
        calls += int(decision.get("api_called", False))
        checked += 1
        if decision.get("decision") == "支持":
            updated.update({
                "result": "匹配",
                "reason": f"全文复核找到明确支持：{decision.get('reason', '')}",
                "fulltext_review_status": "找到明确支持",
                "fulltext_source": review_item["source"],
                "fulltext_source_url": review_item.get("source_url", ""),
                "evidence_section": paragraph.get("section", "正文"),
                "evidence_page": paragraph.get("page"),
                "evidence_text": paragraph.get("text", "")[:600],
                "fulltext_paragraphs_checked": checked,
            })
            return updated, calls
    updated.update({
        "fulltext_review_status": "未找到明确支持，保持存疑",
        "fulltext_source": review_item["source"],
        "fulltext_source_url": review_item.get("source_url", ""),
        "fulltext_paragraphs_checked": checked,
        "reason": f"{updated.get('reason', '')}；全文复核了 {checked} 个高相关段落，仍未找到明确支持。".strip("；"),
    })
    return updated, calls


def execute_fulltext_review(
    prepared: Dict[str, Any],
    progress_callback: ProgressCallback = None,
    max_workers: int = 3,
) -> Dict[str, Any]:
    results = deepcopy(prepared["base_results"])
    review_items = prepared["review_items"]
    actual_calls = 0
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(review_items) or 1))) as executor:
        futures = {
            executor.submit(_review_one, results[item["result_index"]], item): item
            for item in review_items
        }
        completed = 0
        for future in as_completed(futures):
            item = futures[future]
            updated, calls = future.result()
            results[item["result_index"]] = updated
            actual_calls += calls
            completed += 1
            _report(progress_callback, int(98 * completed / max(1, len(review_items))), f"正在全文复核（{completed}/{len(review_items)}）…")

    counts = {label: sum(1 for item in results if item.get("result") == label) for label in ("匹配", "存疑", "领域不符")}
    _report(progress_callback, 100, "全文复核完成。")
    return {
        "filename": prepared["filename"],
        "statistics": {"total": len(results), **counts},
        "estimated_calls": prepared["estimated_calls"],
        "actual_calls": actual_calls,
        "review_stage": "fulltext",
        "results": results,
        "html": build_html_report(prepared["filename"], results),
    }
