import json
import os
import sys
import time
import requests
import re
import argparse
from typing import Dict, Any, Optional

# Constants
NCBI_API_KEY = os.getenv("NCBI_API_KEY")
BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
USER_AGENT = "RefListGenerator/1.0 (mailto:teresa.l@explorationpub.com)"

def clean_title(title: str) -> str:
    """
    Cleans the title by removing prefixes like 'BOOK_TITLE:', 'SEARCH_QUERY:', etc.
    and removing grouping quotes.
    """
    if not title:
        return ""
    
    # Remove common prefixes from AI output
    prefixes = ["BOOK_TITLE:", "TITLE:", "CHAPTER:", "SEARCH_QUERY:", "PUBLISHER:"]
    for prefix in prefixes:
        if title.upper().startswith(prefix):
            title = title[len(prefix):].strip()
            
    # Remove surrounding quotes
    title = title.strip('"\'')
    
    return title

def search_pubmed(query: str) -> Optional[str]:
    """
    Searches PubMed for a given query (title) and returns the first PMID found.
    """
    if not query:
        return None
        
    url = f"{BASE_URL}esearch.fcgi"
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": 1,
        "tool": "RefListGen",
        "email": "teresa.l@explorationpub.com"
    }
    
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
        
    try:
        response = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        id_list = data.get("esearchresult", {}).get("idlist", [])
        if id_list:
            return id_list[0]
            
    except Exception as e:
        print(f"Error searching PubMed for '{query[:30]}...': {e}")
        
    return None

def get_pubmed_details(pmid: str) -> Dict[str, str]:
    """
    Retrieves details (DOI, PMCID, Title) for a given PMID.
    """
    if not pmid:
        return {}
        
    url = f"{BASE_URL}esummary.fcgi"
    params = {
        "db": "pubmed",
        "id": pmid,
        "retmode": "json",
        "tool": "RefListGen",
        "email": "teresa.l@explorationpub.com"
    }
    
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
        
    result = {
        "doi": "",
        "pmcid": "",
        "article_title": ""
    }
    
    try:
        response = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        uid_data = data.get("result", {}).get(pmid, {})
        
        if not uid_data:
            return result
            
        # Get Article Title
        result["article_title"] = uid_data.get("title", "")
        
        # Get DOI and PMCID from articleids
        for article_id in uid_data.get("articleids", []):
            id_type = article_id.get("idtype", "")
            value = article_id.get("value", "")
            
            if id_type == "doi":
                result["doi"] = value
            elif id_type == "pmc":
                result["pmcid"] = value
                
    except Exception as e:
        print(f"Error getting details for PMID {pmid}: {e}")
        
    return result

def process_file(filepath: str):
    """
    Main processing function.
    """
    print(f"Processing file: {filepath}")
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Failed to load JSON file: {e}")
        return

    output_list = []
    
    results = data.get("results", [])
    total = len(results)
    
    print(f"Found {total} references to process.")
    
    for idx, item in enumerate(results, 1):
        print(f"[{idx}/{total}] Processing reference...")
        
        label = str(idx)
        rid = f"B{idx}"
        match_status = item.get("match_status", "")
        
        # Defaults
        final_doi = ""
        final_pmid = ""
        final_pmcid = ""
        final_title = ""
        
        if match_status == "match":
            # For matched entries: directly use the data from generate_json.py
            # These were already queried via Crossref + NLM
            final_pmid = item.get("pmid", "")
            final_pmcid = item.get("pmcid", "")
            final_doi = item.get("api_doi", "") or item.get("extracted_doi", "")
            final_title = item.get("title", "")
            
            if final_pmid:
                print(f"    -> Using cached data: PMID={final_pmid}, PMCID={final_pmcid or 'N/A'}")
            else:
                print(f"    -> Match found but no PMID in cache (DOI: {final_doi or 'N/A'})")
        else:
            # For non-match entries: query NLM using AI extracted title
            query_title = clean_title(item.get("ai_extracted_title", ""))
            
            if query_title:
                # Step 1: Search for PMID
                pmid = search_pubmed(query_title)
                
                if pmid:
                    final_pmid = pmid
                    # Step 2: Get Details
                    details = get_pubmed_details(pmid)
                    final_doi = details.get("doi", "")
                    final_pmcid = details.get("pmcid", "")
                    final_title = details.get("article_title", "")
                    
                    print(f"    -> Found PMID: {pmid}")
                else:
                    print(f"    -> No match in NLM for title: {query_title[:50]}...")
                    # Keep the AI extracted title as fallback
                    final_title = query_title
            else:
                print(f"    -> No query title available for non-match entry")
        
        # Construct output entry
        entry = {
            "rid": rid,
            "label": label,
            "doi": final_doi,
            "pmid": final_pmid,
            "pmcid": final_pmcid,
            "article_title": final_title
        }
        
        output_list.append(entry)
        
        # Rate limiting (only for non-match entries that query NLM)
        if match_status != "match":
            if not NCBI_API_KEY:
                time.sleep(0.34) # Max 3 req/s without key -> sleep ~340ms to be safe
            else:
                time.sleep(0.1) # Max 10 req/s with key
            
    # Save output
    base_name = os.path.splitext(os.path.basename(filepath))[0]
    # Remove _cache suffix if present to prevent stacking names like _cache_RefList
    if base_name.endswith("_cache"):
        base_name = base_name[:-6] # keeping _cache might be intended if input is strictly cache file?
        # User said: "{输入文件名}_RefList". If input is "X_cache.json", output "X_cache_RefList.json".
        # Let's stick to strict filename append.
        base_name = os.path.splitext(os.path.basename(filepath))[0]
        
    output_filename = f"{base_name}_RefList.json"
    output_path = os.path.join(os.path.dirname(filepath), output_filename)
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output_list, f, indent=4, ensure_ascii=False)
        print(f"\nSaved Reference List to: {output_path}")
    except Exception as e:
        print(f"Error saving output file: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate NLM enriched reference list from cache JSON.")
    parser.add_argument("filepath", help="Path to the input JSON file")
    args = parser.parse_args()
    
    if os.path.exists(args.filepath):
        process_file(args.filepath)
    else:
        print(f"File not found: {args.filepath}")
