from collections import Counter
from datetime import datetime
from html import escape
from typing import Any, Dict, List


STATUS_CLASS = {"匹配": "match", "存疑": "doubt", "领域不符": "mismatch"}


def build_html_report(filename: str, results: List[Dict[str, Any]]) -> str:
    counts = Counter(item.get("result", "存疑") for item in results)
    sources = sorted({item.get("metadata_source") or "未获取" for item in results})
    source_options = "".join(
        f'<option value="{escape(source, quote=True)}">{escape(source)}</option>' for source in sources
    )
    rows = []
    for index, item in enumerate(results, 1):
        result = item.get("result", "存疑")
        status_class = STATUS_CLASS.get(result, "doubt")
        source = item.get("metadata_source") or "未获取"
        doi = item.get("doi", "")
        pmid = item.get("pmid", "")
        identifiers = []
        if doi:
            identifiers.append(f'<a href="https://doi.org/{escape(doi, quote=True)}">DOI: {escape(doi)}</a>')
        if pmid:
            identifiers.append(f'<a href="https://pubmed.ncbi.nlm.nih.gov/{escape(pmid, quote=True)}/">PMID: {escape(pmid)}</a>')
        before = item.get("context_before", "")
        after = item.get("context_after", "")
        detail_parts = []
        if before:
            detail_parts.append(f'<div><b>前文：</b>{escape(before)}</div>')
        if after:
            detail_parts.append(f'<div><b>后文：</b>{escape(after)}</div>')
        if identifiers:
            detail_parts.append(f'<div><b>文献标识：</b>{" · ".join(identifiers)}</div>')
        details = (
            f'<details><summary>查看上下文与标识符</summary><div class="details-body">{"".join(detail_parts)}</div></details>'
            if detail_parts else ""
        )
        rows.append(f"""
        <tr data-status="{escape(result, quote=True)}" data-source="{escape(source, quote=True)}">
          <td class="number">{index}</td>
          <td class="status-cell"><span class="status {status_class}">{escape(result)}</span></td>
          <td class="ref">{escape(str(item.get('label', '')))}</td>
          <td class="claim"><div class="target">{escape(item.get('sentence_text', ''))}</div>{details}</td>
          <td class="paper"><div class="title">{escape(item.get('title') or '未获取到题名')}</div><div class="source">{escape(source)}</div></td>
          <td class="reason">{escape(item.get('reason', ''))}</td>
        </tr>""")

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>REF-C 引用内容初筛报告</title><style>
:root{{--line:#dfe3e8;--muted:#667085;--head:#f5f7f9;--green:#067647;--green-bg:#ecfdf3;--amber:#b54708;--amber-bg:#fffaeb;--red:#b42318;--red-bg:#fef3f2}}
*{{box-sizing:border-box}} body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#20262e;background:#fff;margin:0;font-size:14px}}
main{{width:min(1500px,calc(100% - 32px));margin:26px auto 50px}} h1{{font-size:24px;margin:0 0 5px}} .meta-line{{color:var(--muted);font-size:13px}}
.summary{{display:flex;align-items:center;gap:18px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:11px 0;margin:18px 0}}
.summary-item{{display:flex;gap:6px;align-items:baseline;color:var(--muted)}} .summary-item b{{font-size:18px;color:#20262e}} .summary-item.match b{{color:var(--green)}} .summary-item.doubt b{{color:var(--amber)}} .summary-item.mismatch b{{color:var(--red)}}
.toolbar{{display:grid;grid-template-columns:minmax(240px,1fr) 180px 190px auto;gap:10px;align-items:center;margin-bottom:12px}}
input,select,button{{height:38px;border:1px solid #cfd5dc;border-radius:6px;background:#fff;padding:0 10px;font:inherit;color:inherit}} input:focus,select:focus{{outline:2px solid #d6e4ff;border-color:#7aa2e3}} button{{cursor:pointer;background:#f8fafc}} button:hover{{background:#eef2f6}} .visible-count{{color:var(--muted);white-space:nowrap;text-align:right}}
.table-wrap{{border:1px solid var(--line);border-radius:7px;overflow:auto;max-height:72vh}} table{{width:100%;border-collapse:separate;border-spacing:0;min-width:1100px;table-layout:fixed}}
th{{position:sticky;top:0;z-index:2;background:var(--head);font-size:12px;text-align:left;color:#4b5563;border-bottom:1px solid var(--line);padding:10px 9px}}
td{{vertical-align:top;border-bottom:1px solid #e8ebee;padding:11px 9px;line-height:1.52;background:#fff;overflow-wrap:anywhere}} tbody tr:last-child td{{border-bottom:0}} tbody tr:hover td{{background:#fafbfd}} tr[hidden]{{display:none}}
.number{{width:46px;text-align:center;color:#98a2b3}} .status-cell{{width:90px}} .ref{{width:58px;text-align:center;font-weight:650}} .claim{{width:34%}} .paper{{width:25%}} .reason{{width:25%}}
.status{{display:inline-block;font-size:12px;font-weight:650;padding:3px 8px;border-radius:4px;white-space:nowrap}} .status.match{{color:var(--green);background:var(--green-bg)}} .status.doubt{{color:var(--amber);background:var(--amber-bg)}} .status.mismatch{{color:var(--red);background:var(--red-bg)}}
.target{{font-weight:550}} .title{{font-weight:550}} .source{{color:var(--muted);font-size:12px;margin-top:5px}} details{{margin-top:7px;color:var(--muted);font-size:12px}} summary{{cursor:pointer;user-select:none}} .details-body{{border-left:2px solid #d9dee5;margin-top:7px;padding-left:9px}} .details-body div+div{{margin-top:5px}} a{{color:#175cd3;text-decoration:none}} a:hover{{text-decoration:underline}}
.empty{{padding:34px;text-align:center;color:var(--muted)}} .notice{{color:var(--muted);font-size:12px;margin-top:16px;line-height:1.6}}
@media(max-width:800px){{main{{width:calc(100% - 18px);margin-top:16px}} .toolbar{{grid-template-columns:1fr 1fr}} .toolbar input{{grid-column:1/-1}} .visible-count{{text-align:left}} .summary{{gap:10px;flex-wrap:wrap}}}}
@media print{{main{{width:100%;margin:0}} .toolbar{{display:none}} .table-wrap{{max-height:none;overflow:visible;border:0}} table{{min-width:0}} th{{position:static}} tr[hidden]{{display:none}} .notice{{margin-top:10px}}}}
</style></head><body><main>
<h1>REF-C 引用内容初筛报告</h1>
<div class="meta-line">稿件：{escape(filename)}　生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
<div class="summary">
  <span class="summary-item"><b>{len(results)}</b> 总计</span>
  <span class="summary-item match"><b>{counts['匹配']}</b> 匹配</span>
  <span class="summary-item doubt"><b>{counts['存疑']}</b> 存疑</span>
  <span class="summary-item mismatch"><b>{counts['领域不符']}</b> 领域不符</span>
</div>
<div class="toolbar">
  <input id="keyword" type="search" placeholder="搜索正文句子、题名、理由或 Ref.…" autocomplete="off">
  <select id="statusFilter"><option value="">全部结果</option><option value="匹配">匹配</option><option value="存疑">存疑</option><option value="领域不符">领域不符</option></select>
  <select id="sourceFilter"><option value="">全部数据来源</option>{source_options}</select>
  <button id="reset" type="button">清除筛选</button>
  <div class="visible-count" id="visibleCount"></div>
</div>
<div class="table-wrap"><table>
  <colgroup><col style="width:46px"><col style="width:90px"><col style="width:58px"><col style="width:34%"><col style="width:25%"><col style="width:25%"></colgroup>
  <thead><tr><th>#</th><th>结果</th><th>Ref.</th><th>目标引用句</th><th>文献题名 / 来源</th><th>简要理由</th></tr></thead>
  <tbody id="resultRows">{''.join(rows) if rows else '<tr><td colspan="6" class="empty">没有可显示的筛查结果。</td></tr>'}</tbody>
</table></div>
<div class="notice"><b>使用限制：</b>本报告仅根据公开数据库中的题名和摘要进行自动初筛，不等同于全文证据核查，也不应替代人工学术编辑判断。</div>
</main><script>
const rows=[...document.querySelectorAll('#resultRows tr[data-status]')];
const keyword=document.getElementById('keyword');
const statusFilter=document.getElementById('statusFilter');
const sourceFilter=document.getElementById('sourceFilter');
const visibleCount=document.getElementById('visibleCount');
function applyFilters(){{
  const query=keyword.value.trim().toLocaleLowerCase();
  const status=statusFilter.value;
  const source=sourceFilter.value;
  let visible=0;
  rows.forEach(row=>{{
    const show=(!status||row.dataset.status===status)&&(!source||row.dataset.source===source)&&(!query||row.textContent.toLocaleLowerCase().includes(query));
    row.hidden=!show;if(show)visible++;
  }});
  visibleCount.textContent=`显示 ${{visible}} / ${{rows.length}} 条`;
}}
[keyword,statusFilter,sourceFilter].forEach(control=>control.addEventListener(control===keyword?'input':'change',applyFilters));
document.getElementById('reset').addEventListener('click',()=>{{keyword.value='';statusFilter.value='';sourceFilter.value='';applyFilters();keyword.focus();}});
applyFilters();
</script></body></html>"""
