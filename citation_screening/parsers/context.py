import re
from typing import Any, Dict, List


_ABBREVIATION = re.compile(
    r"\b(?:et al|e\.g|i\.e|Fig|Figs|Eq|Eqs|Dr|Mr|Mrs|Prof|vs|No)\.",
    re.IGNORECASE,
)


def split_candidate_sentences(text: str) -> List[str]:
    """Best-effort boundaries; the full paragraph is retained as a safety net."""
    protected = _ABBREVIATION.sub(lambda m: m.group(0).replace(".", "<DOT>"), text)
    protected = re.sub(r"(?<=\d)\.(?=\d)", "<DOT>", protected)
    parts = re.split(r"(?<=[.!?。！？])\s+(?=[A-Z0-9(\[\"“])|\n+", protected)
    return [part.replace("<DOT>", ".").strip() for part in parts if part.strip()]


def add_context(
    item: Dict[str, Any], sentences: List[str], target_index: int, full_block: str
) -> Dict[str, Any]:
    item["context_before"] = sentences[target_index - 1] if target_index > 0 else ""
    item["context_after"] = sentences[target_index + 1] if target_index + 1 < len(sentences) else ""
    item["full_block"] = re.sub(r"\s+", " ", full_block).strip()
    item["boundary_confidence"] = "candidate"
    return item
