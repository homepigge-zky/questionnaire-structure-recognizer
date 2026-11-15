from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field


PARSER_SCHEMA_VERSION = "source-blocks-v1"


class SourceBlock(BaseModel):
    block_id: str
    kind: Literal[
        "paragraph", "table_cell", "ocr_text", "image", "form_field", "header", "footer", "unsupported"
    ]
    text: str = ""
    order: int
    location: dict[str, Any]
    parent_id: str | None = None
    style: dict[str, Any] = Field(default_factory=dict)
    structure: dict[str, Any] = Field(default_factory=dict)
    controls: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ExtractedDocument(BaseModel):
    schema_version: str = PARSER_SCHEMA_VERSION
    document_id: str
    source_hash: str
    filename: str
    file_type: str
    parser: str
    parser_version: str
    extraction_status: Literal["complete", "partial", "failed"]
    source_blocks: list[SourceBlock]
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    unread_regions: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def stable_block_id(kind: str, location: dict, text: str) -> str:
    material = json.dumps(
        {"kind": kind, "location": location, "text": text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "src-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def make_block(kind: str, text: str, order: int, location: dict, **extra) -> SourceBlock:
    return SourceBlock(
        block_id=stable_block_id(kind, location, text),
        kind=kind,
        text=text,
        order=order,
        location=location,
        **extra,
    )
