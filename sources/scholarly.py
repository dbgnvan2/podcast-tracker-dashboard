"""Scholarly (literature) source adapter.

Phase-1: a self-contained EuropePMC client (keyless, biomedical/life-sciences;
returns title, abstract, authors, DOI, citation count, date) over stdlib urllib.
This is the seam where biorx's broader multi-source clients (PubMed, OpenAlex,
bioRxiv/medRxiv, Crossref, Unpaywall) plug in behind the same interface.

Owns DOI dedup (decision §12): normalize DOIs, collapse duplicates.
"""
import math
import json
import time
import urllib.parse
import urllib.request

from .base import SourceAdapter, make_document

EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def _norm_doi(doi):
    if not doi:
        return ""
    d = doi.strip().lower()
    for pre in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pre):
            d = d[len(pre):]
    return d


def _europepmc_search(query, page_size=25, retries=3):
    params = urllib.parse.urlencode({
        "query": query, "format": "json", "resultType": "core", "pageSize": page_size,
    })
    url = f"{EUROPEPMC}?{params}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.loads(r.read())
            return data.get("resultList", {}).get("result", [])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            return []
        except (urllib.error.URLError, TimeoutError, ValueError):
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            return []
    return []


class ScholarlyAdapter(SourceAdapter):
    name = "scholarly"

    def discover(self, arm):
        if not arm or not arm.get("enabled", True):
            return []
        queries = arm.get("queries", [])
        since = arm.get("since_date", "")
        min_cites = int(arm.get("min_citations", 0) or 0)
        per = int(arm.get("max_results_per_source", 25) or 25)
        authors = arm.get("authors", []) or []

        # Author-follow queries get their own pass (always pulled — the literature
        # analog of a monitored channel).
        all_queries = list(queries) + [f'AUTH:"{a}"' for a in authors]

        by_doi, no_doi = {}, []
        for q in all_queries:
            for rec in _europepmc_search(q, per):
                doc = self._to_document(rec)
                if not doc:
                    continue
                if since and doc["published_date"] and doc["published_date"] < since:
                    continue
                if (doc["raw"].get("citations", 0) or 0) < min_cites:
                    # keep preprints (0 cites) only if min_cites==0
                    if min_cites > 0:
                        continue
                key = _norm_doi(doc["raw"].get("doi"))
                if key:
                    # dedup by DOI; prefer the record with more citations / non-preprint
                    prev = by_doi.get(key)
                    if not prev or (doc["raw"].get("citations", 0) > prev["raw"].get("citations", 0)):
                        by_doi[key] = doc
                else:
                    no_doi.append(doc)
            time.sleep(0.3)
        return list(by_doi.values()) + no_doi

    def authority(self, doc):
        cites = doc["raw"].get("citations", 0) or 0
        # log-scaled; ~300+ citations saturates. Preprints (0) rely on recency/venue.
        return min(math.log10(cites + 1) / 2.5, 1.0)

    def _to_document(self, rec):
        title = rec.get("title")
        abstract = rec.get("abstractText")
        if not title or not abstract:
            return None
        doi = rec.get("doi", "")
        pid = doi or f"{rec.get('source','')}:{rec.get('id','')}"
        date = (rec.get("firstPublicationDate") or "")[:10]
        cites = rec.get("citedByCount", 0) or 0
        journal = (rec.get("journalInfo", {}) or {}).get("journal", {}).get("title", "")
        url = f"https://doi.org/{doi}" if doi else \
              f"https://europepmc.org/article/{rec.get('source','MED')}/{rec.get('id','')}"
        doc = make_document(
            id=_norm_doi(doi) or pid, source_type="literature", source="europepmc",
            title=title, byline=rec.get("authorString", ""), url=url,
            published_date=date,
            text=_strip_html(abstract),
            velocity=0.0,
            raw={"doi": doi, "citations": cites, "venue": journal,
                 "pmid": rec.get("pmid", ""), "is_preprint": rec.get("source") == "PPR"},
        )
        doc["authority"] = self.authority(doc)
        return doc


def _strip_html(text):
    import re
    return re.sub(r"<[^>]+>", "", text or "").strip()
