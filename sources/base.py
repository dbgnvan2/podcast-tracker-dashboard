"""SourceAdapter — the seam that makes the engine source-agnostic.

A `document` is the universal currency (see DESIGN-multisource.md §4):

    {
      "id":             str,    # youtube id | normalized DOI | source:accession
      "source_type":    str,    # "youtube" | "literature"
      "source":         str,    # "youtube" | "europepmc" | "pubmed" | "openalex" | ...
      "title":          str,
      "byline":         str,    # channel name | author list
      "url":            str,
      "published_date": str,    # YYYY-MM-DD
      "text":           str,    # transcript | abstract (+ full text if fetched)
      "authority":      float,  # 0..1 normalized
      "velocity":       float,  # views/day | citations/year
      "raw":            dict,   # source-specific (views/likes | citations/venue/doi/oa)
    }
"""


def make_document(id, source_type, source, title, byline="", url="",
                  published_date="", text="", authority=0.0, velocity=0.0, raw=None):
    return {
        "id": id, "source_type": source_type, "source": source,
        "title": title or "", "byline": byline or "", "url": url or "",
        "published_date": published_date or "", "text": text or "",
        "authority": float(authority or 0.0), "velocity": float(velocity or 0.0),
        "raw": raw or {},
    }


class SourceAdapter:
    """Subclass per modality. Fault-tolerant by contract: on a transient failure,
    return what you have and let the caller record a retryable status — never
    record a terminal negative from a transient error (see LEARNINGS.md P1/P2)."""

    name = "base"

    def discover(self, arm):
        """arm: the profile's source-arm dict (queries + channels/sources/authors +
        filters). Return a list of `document` dicts (text may be empty until
        fetch_content). Must not raise on a single-query failure — skip and continue."""
        raise NotImplementedError

    def fetch_content(self, doc):
        """Return the analyzable text for a document (transcript | abstract/full text),
        or None on genuine failure. Default: whatever discover already attached."""
        return doc.get("text") or None

    def authority(self, doc):
        """0..1 normalized source authority. Default: none."""
        return float(doc.get("authority") or 0.0)
