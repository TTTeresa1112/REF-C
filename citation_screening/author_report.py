import json
import math
import os
import re
from copy import deepcopy
from datetime import datetime
from html import escape
from typing import Any, Dict, List

import requests


BATCH_SIZE = 15
REPORTABLE = {"存疑", "领域不符"}


def _combined_context(item: Dict[str, Any]) -> str:
    parts = [item.get("context_before", ""), item.get("sentence_text", ""), item.get("context_after", "")]
    return " ".join(str(part).strip() for part in parts if str(part).strip())


def prepare_author_report(screening_result: Dict[str, Any]) -> Dict[str, Any]:
    items = []
    for index, result in enumerate(screening_result.get("results", [])):
        if result.get("result") not in REPORTABLE:
            continue
        items.append({
            "item_id": str(index),
            "reference_number": str(result.get("label", "")),
            "classification": result.get("result", ""),
            "relevant_text": _combined_context(result),
            "reason_zh": result.get("reason", ""),
            "title": result.get("title", ""),
        })
    if not items:
        raise ValueError("当前结果中没有需要反馈作者的“存疑”或“领域不符”条目。")
    return {
        "filename": screening_result.get("filename", "manuscript"),
        "items": items,
        "estimated_calls": math.ceil(len(items) / BATCH_SIZE),
        "doubt_count": sum(item["classification"] == "存疑" for item in items),
        "mismatch_count": sum(item["classification"] == "领域不符" for item in items),
    }


def _generate_batch(batch: List[Dict[str, Any]], totals: Dict[str, int]) -> Dict[str, Any]:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 DeepSeek API，无法生成英文作者报告。")
    payload = [{
        "item_id": item["item_id"],
        "reference_number": item["reference_number"],
        "classification": item["classification"],
        "reason_chinese": item["reason_zh"],
        "article_title": item["title"],
    } for item in batch]
    prompt = f"""You are a professional English-language academic editor. Convert the supplied citation-check JSON into concise, courteous feedback for manuscript authors.

Return JSON only:
{{"summary":"2-3 sentence English overview","items":[{{"item_id":"exact input ID","concern":"clear English explanation","suggested_action":"specific and courteous action for the author"}}]}}

Rules:
- Preserve every item_id exactly and return every input item once.
- Do not invent evidence, citations, findings, or manuscript wording.
- For 存疑, explain that the available evidence does not clearly support the claim and ask the author to verify, narrow, or replace the citation.
- For 领域不符, explain the concrete topic/population/intervention mismatch and ask for a more appropriate reference.
- Do not mention AI, DeepSeek, JSON, internal classifications, databases, or automated screening.
- Use formal, neutral English suitable for an editorial report.
- Overall totals: {totals['total']} items requiring attention ({totals['doubt']} uncertain; {totals['mismatch']} potentially mismatched).

INPUT:
{json.dumps(payload, ensure_ascii=False)}"""
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "messages": [{"role": "user", "content": prompt}],
            "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
            "temperature": 0.1, "stream": False,
        },
        timeout=120,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    match = re.search(r"\{[\s\S]*\}", content)
    data = json.loads(match.group(0) if match else content)
    returned = data.get("items", [])
    if {str(item.get("item_id")) for item in returned} != {item["item_id"] for item in batch}:
        raise ValueError("英文报告返回的条目与输入不一致")
    return data


def _html(filename: str, summary: str, items: List[Dict[str, Any]]) -> str:
    blocks = []
    for item in items:
        blocks.append(f"""<section>
<h2>Reference {escape(item['reference_number'])}</h2>
<div class="label">Relevant text</div><blockquote>{escape(item['relevant_text'])}</blockquote>
<div class="label">Concern</div><p>{escape(item['concern'])}</p>
<div class="label">Suggested action</div><p>{escape(item['suggested_action'])}</p>
</section>""")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reference Check Report</title><style>
body{{font-family:Arial,Helvetica,sans-serif;color:#202124;margin:0;background:#f6f7f9;line-height:1.6}}main{{max-width:900px;margin:32px auto;padding:34px 42px;background:#fff;border:1px solid #e1e4e8}}h1{{font-size:27px;margin:0 0 4px}}.meta{{color:#6b7280;font-size:13px;margin-bottom:24px}}.summary{{padding:15px 18px;background:#f3f6fa;border-left:4px solid #315f9b;margin-bottom:24px}}section{{padding:22px 0;border-top:1px solid #dfe3e8}}h2{{font-size:18px;margin:0 0 13px;color:#17365d}}.label{{font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:#667085;margin-top:12px}}p{{margin:4px 0}}blockquote{{margin:5px 0;padding:10px 14px;background:#fafafa;border-left:3px solid #c7cdd4;font-style:normal}}@media print{{body{{background:#fff}}main{{border:0;margin:0;max-width:none;padding:0}}section{{break-inside:avoid}}}}
</style></head><body><main><h1>Reference Check Report</h1><div class="meta">Manuscript: {escape(filename)} · Generated {datetime.now().strftime('%Y-%m-%d')}</div><div class="summary">{escape(summary)}</div>{''.join(blocks)}</main></body></html>"""


def execute_author_report(prepared: Dict[str, Any], progress_callback=None) -> Dict[str, Any]:
    items = deepcopy(prepared["items"])
    totals = {"total": len(items), "doubt": prepared["doubt_count"], "mismatch": prepared["mismatch_count"]}
    generated = {}
    summary = ""
    calls = 0
    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    for index, batch in enumerate(batches, 1):
        data = _generate_batch(batch, totals)
        calls += 1
        summary = summary or str(data.get("summary", "")).strip()
        generated.update({str(item["item_id"]): item for item in data["items"]})
        if progress_callback:
            progress_callback(int(index * 100 / len(batches)), f"正在生成英文报告（{index}/{len(batches)}）…")
    final_items = [{**item, **generated[item["item_id"]]} for item in items]
    return {
        "filename": prepared["filename"], "summary": summary, "items": final_items,
        "estimated_calls": prepared["estimated_calls"], "actual_calls": calls,
        "html": _html(prepared["filename"], summary, final_items),
    }
