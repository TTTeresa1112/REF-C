import io
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import fitz
import requests
from rank_bm25 import BM25Okapi


DEFAULT_CONTACT = os.getenv("MY_EMAIL", "you@example.com")
HEADERS = {"User-Agent": f"REF-C/1.0 (mailto:{DEFAULT_CONTACT})"}
MAX_PDF_BYTES = 25 * 1024 * 1024


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean(text: str) -> str:
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _xml_paragraphs(content: bytes) -> List[Dict[str, Any]]:
    root = ET.fromstring(content)
    body = next((node for node in root.iter() if _local(node.tag) == "body"), None)
    if body is None:
        return []
    paragraphs: List[Dict[str, Any]] = []

    def walk(node, section: str = "正文") -> None:
        current_section = section
        for child in list(node):
            tag = _local(child.tag)
            if tag == "title" and _local(node.tag) == "sec":
                current_section = _clean("".join(child.itertext())) or section
                break
        for child in list(node):
            tag = _local(child.tag)
            if tag == "p":
                text = _clean("".join(child.itertext()))
                if len(text) >= 40:
                    paragraphs.append({"section": current_section, "text": text, "page": None})
            elif tag in {"sec", "boxed-text", "disp-quote"}:
                walk(child, current_section)
            elif tag == "table-wrap":
                text = _clean("".join(child.itertext()))
                if len(text) >= 40:
                    paragraphs.append({"section": f"{current_section} · 表格", "text": text, "page": None})

    walk(body)
    return paragraphs


def _pdf_paragraphs(content: bytes) -> List[Dict[str, Any]]:
    document = fitz.open(stream=content, filetype="pdf")
    paragraphs: List[Dict[str, Any]] = []
    current_section = "正文"
    for page_index, page in enumerate(document, 1):
        for block in page.get_text("blocks"):
            text = _clean(block[4])
            if len(text) < 40:
                continue
            if len(text) <= 120 and not text.endswith(('.', '?', '!', '。', '？', '！')):
                current_section = text
                continue
            paragraphs.append({"section": current_section, "text": text, "page": page_index})
    document.close()
    return paragraphs


def _download_pdf(url: str) -> Optional[bytes]:
    response = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True, stream=True)
    response.raise_for_status()
    content = io.BytesIO()
    for chunk in response.iter_content(64 * 1024):
        content.write(chunk)
        if content.tell() > MAX_PDF_BYTES:
            raise ValueError("开放 PDF 超过 25 MB 上限")
    data = content.getvalue()
    if not data.startswith(b"%PDF"):
        return None
    return data


def _pmc_xml(pmcid: str) -> Optional[Dict[str, Any]]:
    pmcid = pmcid if str(pmcid).upper().startswith("PMC") else f"PMC{pmcid}"
    urls = [
        f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
    ]
    for url in urls:
        try:
            params = None
            if "efetch.fcgi" in url:
                params = {"db": "pmc", "id": pmcid, "retmode": "xml"}
                if os.getenv("NCBI_API_KEY"):
                    params["api_key"] = os.environ["NCBI_API_KEY"]
            response = requests.get(url, params=params, headers=HEADERS, timeout=25)
            response.raise_for_status()
            paragraphs = _xml_paragraphs(response.content)
            if paragraphs:
                return {"source": "PMC/Europe PMC XML", "source_url": url, "paragraphs": paragraphs}
        except (requests.RequestException, ET.ParseError, ValueError):
            continue
    return None


def _oa_pdf_urls(doi: str) -> List[str]:
    urls: List[str] = []
    email = os.getenv("MY_EMAIL", "you@example.com")
    try:
        response = requests.get(
            f"https://api.unpaywall.org/v2/{quote(doi, safe='')}",
            params={"email": email}, headers=HEADERS, timeout=20,
        )
        if response.ok:
            location = response.json().get("best_oa_location") or {}
            if location.get("url_for_pdf"):
                urls.append(location["url_for_pdf"])
    except requests.RequestException:
        pass
    try:
        response = requests.get(
            f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='')}",
            params={"mailto": email}, headers=HEADERS, timeout=20,
        )
        if response.ok:
            location = response.json().get("best_oa_location") or {}
            if location.get("pdf_url"):
                urls.append(location["pdf_url"])
    except requests.RequestException:
        pass
    return list(dict.fromkeys(urls))


def fetch_open_fulltext(ref: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if ref.get("pmcid"):
        found = _pmc_xml(str(ref["pmcid"]))
        if found:
            return found
    if ref.get("doi"):
        for url in _oa_pdf_urls(str(ref["doi"])):
            try:
                pdf = _download_pdf(url)
                if not pdf:
                    continue
                paragraphs = _pdf_paragraphs(pdf)
                if paragraphs:
                    return {"source": "开放获取 PDF", "source_url": url, "paragraphs": paragraphs}
            except (requests.RequestException, ValueError, RuntimeError):
                continue
    return None


def _tokens(text: str) -> List[str]:
    lowered = (text or "").lower()
    words = re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)?", lowered)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.extend(chinese[i:i + 2] for i in range(max(0, len(chinese) - 1)))
    return words or [lowered[:80]]


def rank_paragraphs(
    query: str, paragraphs: List[Dict[str, Any]], limit: int = 8
) -> List[Dict[str, Any]]:
    if not paragraphs:
        return []
    corpus = [_tokens(f"{item.get('section', '')} {item.get('text', '')}") for item in paragraphs]
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(_tokens(query))
    section_bonus = {"result": 1.25, "conclusion": 1.15, "abstract": 1.1, "结果": 1.25, "结论": 1.15}
    ranked = []
    for index, (item, score) in enumerate(zip(paragraphs, scores)):
        section = item.get("section", "").lower()
        multiplier = next((value for key, value in section_bonus.items() if key in section), 1.0)
        ranked.append({**item, "rank_score": float(score) * multiplier, "original_index": index})
    ranked.sort(key=lambda item: (-item["rank_score"], item["original_index"]))
    return ranked[:max(1, min(limit, len(ranked)))]
