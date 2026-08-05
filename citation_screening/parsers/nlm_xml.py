import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Tuple

from .context import add_context, split_candidate_sentences


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(node, name: str):
    return [child for child in node.iter() if _local(child.tag) == name]


def _text(node) -> str:
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip() if node is not None else ""


def _ref_ids(raw: str) -> List[str]:
    return [value for value in re.split(r"\s+", raw or "") if value]


def _flatten(node) -> Tuple[str, List[str]]:
    chunks: List[str] = []
    rids: List[str] = []
    if node.text:
        chunks.append(node.text)
    for child in node:
        if _local(child.tag) == "xref" and child.get("ref-type") == "bibr":
            child_rids = _ref_ids(child.get("rid", ""))
            rids.extend(child_rids)
            chunks.append(f"{_text(child)}@@@{'|'.join(child_rids)}@@@")
        else:
            child_text, child_rids = _flatten(child)
            chunks.append(child_text)
            rids.extend(child_rids)
        if child.tail:
            chunks.append(child.tail)
    return "".join(chunks), list(dict.fromkeys(rids))


def _markers(text: str) -> List[str]:
    found: List[str] = []
    for marker in re.findall(r"@@@([^@]+)@@@", text):
        found.extend(value for value in marker.split("|") if value)
    return list(dict.fromkeys(found))


def _clean_markers(text: str) -> str:
    return re.sub(r"@@@[^@]+@@@", "", text)


def _pub_id(ref, kind: str) -> str:
    for node in _children(ref, "pub-id"):
        if (node.get("pub-id-type") or "").lower() == kind:
            return _text(node)
    return ""


def parse_nlm_xml(file_bytes: bytes) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    try:
        root = ET.fromstring(file_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"NLM XML 格式错误：{exc}") from exc

    references: Dict[str, Dict[str, Any]] = {}
    for ref in _children(root, "ref"):
        rid = ref.get("id", "").strip()
        if not rid:
            continue
        label_nodes = [n for n in list(ref) if _local(n.tag) == "label"]
        title_nodes = _children(ref, "article-title")
        references[rid] = {
            "rid": rid,
            "label": _text(label_nodes[0]) if label_nodes else re.sub(r"\D", "", rid),
            "raw_reference": _text(ref),
            "doi": _pub_id(ref, "doi"),
            "pmid": _pub_id(ref, "pmid"),
            "pmcid": _pub_id(ref, "pmcid"),
            "title": _text(title_nodes[0]) if title_nodes else "",
        }

    body_nodes = _children(root, "body")
    if not body_nodes:
        raise ValueError("XML 中未找到 <body> 正文。")
    body = body_nodes[0]
    table_paragraphs = {
        id(p)
        for table in _children(body, "table-wrap")
        for p in _children(table, "p")
    }
    results: List[Dict[str, Any]] = []
    paragraph_index = 0
    for paragraph in _children(body, "p"):
        if id(paragraph) in table_paragraphs:
            continue
        paragraph_index += 1
        text, paragraph_rids = _flatten(paragraph)
        if not paragraph_rids:
            continue
        sentence_parts = split_candidate_sentences(text)
        for sentence_index, sentence in enumerate(sentence_parts):
            sentence_rids = _markers(sentence)
            if not sentence_rids:
                continue
            clean_sentence_parts = [_clean_markers(value) for value in sentence_parts]
            item = {
                "sentence_id": hashlib.md5(f"xml:p:{paragraph_index}:{sentence}".encode("utf-8")).hexdigest(),
                "sentence_text": re.sub(r"\s+", " ", _clean_markers(sentence)).strip(),
                "source": "body_text",
                "location": {"paragraph": paragraph_index},
                "citations": [{"rid": rid} for rid in dict.fromkeys(sentence_rids)],
            }
            results.append(add_context(item, clean_sentence_parts, sentence_index, _clean_markers(text)))

    for table_index, table in enumerate(_children(body, "table"), 1):
        for row_index, row in enumerate(_children(table, "tr"), 1):
            text, rids = _flatten(row)
            if rids:
                clean = re.sub(r"\s+", " ", _clean_markers(text)).strip()
                results.append({
                    "sentence_id": hashlib.md5(f"xml:t:{table_index}:{row_index}:{clean}".encode("utf-8")).hexdigest(),
                    "sentence_text": clean,
                    "source": "table_content",
                    "location": {"table": table_index, "row": row_index},
                    "citations": [{"rid": rid} for rid in dict.fromkeys(rids)],
                    "context_before": "",
                    "context_after": "",
                    "full_block": clean,
                    "boundary_confidence": "table_row",
                })
    return results, references
