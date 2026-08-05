import hashlib
import io
import re
from typing import Any, Dict, List

from docx import Document
from docx.oxml.ns import qn

from .context import add_context, split_candidate_sentences


_CITATION_GROUP = re.compile(r"\[(\s*\d+(?:\s*[-–—,;]\s*\d+)*)\]")


def _ancestor(node, tag: str):
    current = node
    while current is not None:
        if current.tag == tag:
            return current
        current = current.getparent()
    return None


def _is_superscript_text(text_node) -> bool:
    run = _ancestor(text_node, qn("w:r"))
    if run is None:
        return False
    vert_align = run.find("./w:rPr/w:vertAlign", namespaces=run.nsmap)
    return vert_align is not None and vert_align.get(qn("w:val")) == "superscript"


def _paragraph_text(paragraph) -> str:
    """Read displayed OOXML, including fields, hyperlinks and content controls."""
    node = getattr(paragraph, "_element", paragraph)
    chunks: List[str] = []
    superscript_buffer = ""
    field_display_stack: List[bool] = []

    def flush_superscript() -> None:
        nonlocal superscript_buffer
        if superscript_buffer:
            chunks.append(f"[{superscript_buffer.strip()}]")
            superscript_buffer = ""

    for element in node.iter():
        if element.tag == qn("w:fldChar"):
            field_type = element.get(qn("w:fldCharType"))
            if field_type == "begin":
                field_display_stack.append(False)
            elif field_type == "separate" and field_display_stack:
                field_display_stack[-1] = True
            elif field_type == "end" and field_display_stack:
                field_display_stack.pop()
            continue
        if element.tag == qn("w:instrText"):
            continue
        if element.tag == qn("w:t"):
            if field_display_stack and not field_display_stack[-1]:
                continue
            if _ancestor(element, qn("w:del")) is not None:
                continue
            text = element.text or ""
        elif element.tag == qn("w:tab"):
            text = "\t"
        elif element.tag in (qn("w:br"), qn("w:cr")):
            text = "\n"
        else:
            continue
        is_citation_superscript = bool(
            _is_superscript_text(element) and re.fullmatch(r"[\d\s,;\-–—]+", text)
        )
        if is_citation_superscript:
            superscript_buffer += text
            continue
        flush_superscript()
        chunks.append(text)
    flush_superscript()
    return "".join(chunks).strip()


def _expand_group(group: str) -> List[str]:
    values: List[int] = []
    for part in re.split(r"\s*[,;]\s*", group.strip()):
        range_match = re.fullmatch(r"(\d+)\s*[-–—]\s*(\d+)", part)
        if range_match:
            start, end = map(int, range_match.groups())
            if 0 < start <= end and end - start <= 100:
                values.extend(range(start, end + 1))
        elif part.strip().isdigit():
            values.append(int(part.strip()))
    return [f"B{value}" for value in dict.fromkeys(values)]


def _citation_rids(text: str) -> List[str]:
    rids: List[str] = []
    for match in _CITATION_GROUP.finditer(text):
        rids.extend(_expand_group(match.group(1)))
    return list(dict.fromkeys(rids))


def _make_item(text: str, source: str, location: Dict[str, int]) -> Dict[str, Any]:
    rids = _citation_rids(text)
    return {
        "sentence_id": hashlib.md5(f"{source}:{location}:{text}".encode("utf-8")).hexdigest(),
        "sentence_text": re.sub(r"\s+", " ", text).strip(),
        "source": source,
        "location": location,
        "citations": [{"rid": rid} for rid in rids],
    }


def parse_word(file_bytes: bytes) -> List[Dict[str, Any]]:
    try:
        document = Document(io.BytesIO(file_bytes))
    except Exception as exc:
        raise ValueError(f"无法读取 Word 文件：{exc}") from exc

    results: List[Dict[str, Any]] = []
    body = document.element.body
    body_paragraphs = [
        paragraph for paragraph in body.iter(qn("w:p"))
        if _ancestor(paragraph, qn("w:tbl")) is None
    ]
    for paragraph_index, paragraph in enumerate(body_paragraphs, 1):
        text = _paragraph_text(paragraph)
        if re.fullmatch(r"\s*(references|bibliography|参考文献)\s*[:：]?\s*", text, re.IGNORECASE):
            break
        sentence_parts = split_candidate_sentences(text)
        for sentence_index, sentence in enumerate(sentence_parts):
            if _citation_rids(sentence):
                item = _make_item(sentence, "body_text", {"paragraph": paragraph_index})
                results.append(add_context(item, sentence_parts, sentence_index, text))

    for table_index, table in enumerate(body.iter(qn("w:tbl")), 1):
        for row_index, row in enumerate(table.iter(qn("w:tr")), 1):
            cells = []
            for cell in row.findall(qn("w:tc")):
                cell_text = " ".join(
                    filter(None, (_paragraph_text(p) for p in cell.iter(qn("w:p"))))
                ).strip()
                if cell_text:
                    cells.append(cell_text)
            text = " | ".join(cells)
            if _citation_rids(text):
                item = _make_item(text, "table_content", {"table": table_index, "row": row_index})
                item.update({"context_before": "", "context_after": "", "full_block": text, "boundary_confidence": "table_row"})
                results.append(item)
    return results
