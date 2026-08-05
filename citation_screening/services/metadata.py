import os
import re
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import Any, Dict
from urllib.parse import quote

import requests


DEFAULT_CONTACT = os.getenv("MY_EMAIL", "you@example.com")
HEADERS = {"User-Agent": f"REF-C/1.0 (mailto:{DEFAULT_CONTACT})"}


def _empty(ref: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": ref.get("title", ""),
        "abstract": "",
        "authors": [],
        "doi": ref.get("doi", ""),
        "pmcid": ref.get("pmcid", ""),
        "metadata_source": "",
        "metadata_error": "",
    }


def _pubmed(pmid: str) -> Dict[str, Any]:
    params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
    if os.getenv("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    response = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params=params,
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    title_node = root.find(".//ArticleTitle")
    abstract_nodes = root.findall(".//AbstractText")
    authors = []
    for author in root.findall(".//Author"):
        family = author.findtext("LastName", "")
        given = author.findtext("ForeName", "") or author.findtext("Initials", "")
        if family:
            authors.append(f"{family} {given}".strip())
    article_ids = {
        (node.get("IdType") or "").lower(): (node.text or "").strip()
        for node in root.findall(".//ArticleId")
    }
    return {
        "title": "".join(title_node.itertext()).strip() if title_node is not None else "",
        "abstract": " ".join("".join(n.itertext()).strip() for n in abstract_nodes),
        "authors": authors,
        "doi": article_ids.get("doi", ""),
        "pmcid": article_ids.get("pmc", ""),
        "metadata_source": "PubMed",
        "metadata_error": "",
    }


def _semantic_scholar(identifier: str, id_type: str) -> Dict[str, Any]:
    fields = "title,abstract,authors"
    if id_type == "doi":
        url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(identifier, safe='')}"
        params = {"fields": fields}
    else:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {"query": identifier, "limit": 1, "fields": fields}
    headers = dict(HEADERS)
    if os.getenv("SEMANTIC_SCHOLAR_API_KEY"):
        headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
    response = requests.get(url, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    data = response.json()
    if id_type == "title":
        hits = data.get("data", [])
        if not hits:
            return {}
        data = hits[0]
        score = SequenceMatcher(None, identifier.lower(), data.get("title", "").lower()).ratio()
        if score < 0.82:
            return {}
    return {
        "title": data.get("title") or "",
        "abstract": data.get("abstract") or "",
        "authors": [a.get("name", "") for a in data.get("authors", []) if a.get("name")],
        "metadata_source": "Semantic Scholar",
        "metadata_error": "",
    }


def _crossref(doi: str) -> Dict[str, Any]:
    response = requests.get(
        f"https://api.crossref.org/works/{quote(doi, safe='')}",
        params={"mailto": os.getenv("MY_EMAIL", DEFAULT_CONTACT)},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    item = response.json().get("message", {})
    titles = item.get("title", [])
    abstract = re.sub(r"<[^>]+>", " ", item.get("abstract", ""))
    return {
        "title": titles[0] if titles else "",
        "abstract": re.sub(r"\s+", " ", abstract).strip(),
        "authors": [
            f"{a.get('family', '')} {a.get('given', '')}".strip()
            for a in item.get("author", [])
            if a.get("family")
        ],
        "metadata_source": "Crossref",
        "metadata_error": "",
    }


def fetch_metadata(ref: Dict[str, Any]) -> Dict[str, Any]:
    result = _empty(ref)
    attempts = []
    if ref.get("pmid"):
        attempts.append(lambda: _pubmed(str(ref["pmid"])))
    if ref.get("doi"):
        attempts.extend([
            lambda: _semantic_scholar(str(ref["doi"]), "doi"),
            lambda: _crossref(str(ref["doi"])),
        ])
    title = ref.get("title", "")
    if title:
        attempts.append(lambda: _semantic_scholar(title, "title"))

    errors = []
    for attempt in attempts:
        try:
            found = attempt()
            if not found:
                continue
            result.update(found)
            if found.get("abstract"):
                return result
            if not result.get("metadata_source"):
                result["metadata_source"] = found.get("metadata_source", "")
        except (requests.RequestException, ET.ParseError, ValueError, KeyError) as exc:
            errors.append(str(exc))
    if errors and not result.get("metadata_source"):
        result["metadata_error"] = errors[-1][:240]
    return result
