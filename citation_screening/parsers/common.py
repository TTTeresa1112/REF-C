import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .nlm_xml import parse_nlm_xml
from .word import parse_word


def normalize_rid(value: str) -> str:
    value = str(value or "").strip()
    match = re.search(r"(?:B)?(\d+)", value, re.IGNORECASE)
    return f"B{int(match.group(1))}" if match else value


def split_reference_lines(text: str) -> Dict[str, Dict[str, Any]]:
    refs: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate((text or "").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        label_match = re.match(r"^\s*(?:\[\s*)?(\d+)(?:\s*\])?[.、)\s]+", raw)
        label = label_match.group(1) if label_match else str(index)
        rid = f"B{int(label)}"
        refs[rid] = {
            "rid": rid,
            "label": label,
            "raw_reference": raw,
            "doi": "",
            "pmid": "",
            "pmcid": "",
            "title": "",
        }
    return refs


def parse_manuscript(
    file_bytes: bytes, filename: str, pasted_references: str = ""
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".docx":
        sentences = parse_word(file_bytes)
        references = split_reference_lines(pasted_references)
        if not references:
            raise ValueError("Word 稿件需要粘贴参考文献列表（每行一条）。")
        return sentences, references
    if suffix == ".xml":
        sentences, references = parse_nlm_xml(file_bytes)
        pasted = split_reference_lines(pasted_references)
        for rid, ref in pasted.items():
            if rid in references:
                references[rid]["raw_reference"] = ref["raw_reference"]
            else:
                references[rid] = ref
        return sentences, references
    raise ValueError("仅支持 .docx 和 .xml 文件。")
