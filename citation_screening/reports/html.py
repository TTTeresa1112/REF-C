from collections import Counter
from datetime import datetime
from html import escape
from typing import Any, Dict, List


COLORS = {"匹配": "#15803d", "存疑": "#b45309", "领域不符": "#b91c1c"}


def build_html_report(filename: str, results: List[Dict[str, Any]]) -> str:
    counts = Counter(item.get("result", "存疑") for item in results)
    cards = "".join(
        f'<div class="card"><strong style="color:{COLORS[label]}">{counts[label]}</strong><span>{label}</span></div>'
        for label in ("匹配", "存疑", "领域不符")
    )
    rows = []
    for item in results:
        result = item.get("result", "存疑")
        doi = item.get("doi", "")
        pmid = item.get("pmid", "")
        identifiers = []
        if doi:
            identifiers.append(f'<a href="https://doi.org/{escape(doi)}">DOI: {escape(doi)}</a>')
        if pmid:
            identifiers.append(f'<a href="https://pubmed.ncbi.nlm.nih.gov/{escape(pmid)}/">PMID: {escape(pmid)}</a>')
        rows.append(f"""
        <article class="result">
          {f'<div class="context"><b>前文：</b>{escape(item.get("context_before", ""))}</div>' if item.get('context_before') else ''}
          <div class="sentence">{escape(item.get('sentence_text', ''))}</div>
          {f'<div class="context"><b>后文：</b>{escape(item.get("context_after", ""))}</div>' if item.get('context_after') else ''}
          <div class="meta"><b>Ref. {escape(str(item.get('label', '')))}</b> · {escape(item.get('title') or '未获取到题名')}</div>
          <div><span class="badge" style="background:{COLORS.get(result, COLORS['存疑'])}">{escape(result)}</span>
          <span class="reason">{escape(item.get('reason', ''))}</span></div>
          <div class="source">数据来源：{escape(item.get('metadata_source') or '未获取')} {' · '.join(identifiers)}</div>
        </article>""")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>REF-C 引用内容初筛报告</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1f2937;background:#f8fafc;margin:0}}
main{{max-width:1000px;margin:36px auto;padding:0 24px}} h1{{margin-bottom:6px}} .muted,.source{{color:#64748b;font-size:13px}}
.cards{{display:flex;gap:12px;margin:24px 0}} .card{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:16px 24px;min-width:110px}}
.card strong{{font-size:28px;display:block}} .card span{{font-size:14px}} .result{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:18px;margin:12px 0;break-inside:avoid}}
.sentence{{font-size:16px;line-height:1.6;margin-bottom:10px}} .meta{{margin-bottom:12px}} .badge{{color:white;border-radius:999px;padding:3px 10px;font-size:13px;font-weight:600;margin-right:8px}}
.sentence:before{{content:"目标引用句：";font-weight:700}} .context{{font-size:14px;color:#64748b;line-height:1.55;margin:5px 0}}
.reason{{line-height:1.6}} .source{{margin-top:10px}} a{{color:#2563eb;text-decoration:none}} .notice{{margin-top:28px;padding:14px;border-left:4px solid #94a3b8;background:#f1f5f9}}
@media print{{body{{background:white}} main{{margin:0;max-width:none}}}}
</style></head><body><main>
<h1>REF-C 引用内容初筛报告</h1>
<div class="muted">稿件：{escape(filename)} · 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
<div class="cards">{cards}</div>
{''.join(rows) if rows else '<p>没有可显示的筛查结果。</p>'}
<div class="notice"><b>使用限制：</b>本报告仅根据公开数据库中的题名和摘要进行自动初筛，不等同于全文证据核查，也不应替代人工学术编辑判断。</div>
</main></body></html>"""
