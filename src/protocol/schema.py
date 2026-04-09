"""Protocol schema — placeholder.

The actual fields, types, and validators are deferred pending the
`source_data` column-schema discussion (see SYSTEM_DESIGN.md §9). This
module exists only to reserve the import path and to host the protocol
version constant.

Current sketch (non-binding):

    class ProvenanceField(BaseModel):
        value: float | int | str | None
        quote: str                         # verbatim, <= 30 words
        locator: str                       # canonical page id, e.g. "p42"
        source_doc_hash: str               # SHA-256 hex
        extraction_type: Literal["direct", "inferred", "derived"]
        confidence: float | None           # advisory only
        protocol_version: str
        extracting_model: str

    class ExtractionRecord(BaseModel):
        company: str
        period: str                        # e.g. "2025-Q4"
        filing_type: str                   # e.g. "10-Q"
        metrics: dict[str, ProvenanceField]

Do not import this module in production code until the schema is ratified.
"""
from __future__ import annotations

from . import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION"]
