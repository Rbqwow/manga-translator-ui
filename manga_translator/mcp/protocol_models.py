"""Pydantic protocol models for Manga Translator UI MCP tools."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


EffectiveMode = Literal["hq", "text_on_demand", "text_only"]
ImageDeliveryMode = Literal["auto", "mcp_image", "file_path"]


class ApiConflictItem(BaseModel):
    """Describes a selected API-backed pipeline component incompatible with 0-API MCP mode."""

    section: str
    setting: str
    value: str
    message: str


class WorkspaceInfoResponse(BaseModel):
    """Read-only workspace summary returned by get_workspace_info()."""

    preset: str = "默认"
    target_lang: str = "CHS"
    translation_mode: Literal["hq", "text"] = "hq"
    allow_on_demand_image: bool = True
    effective_mode: EffectiveMode = "hq"
    image_delivery: ImageDeliveryMode = "auto"
    inline_image_budget_mb: float = 2.0
    preview_max_size: int = 1024
    preview_format: Literal["jpeg", "webp", "png"] = "jpeg"
    preview_quality: int = 82
    max_parallel_batches: int = 1
    batch_size: int = 3
    context_size: int = 3
    ai_line_break_enabled: bool = False
    check_br_and_retry: bool = False
    extract_glossary: bool = False
    high_quality_prompt_path: Optional[str] = None
    gui_file_count: int = 0
    gui_file_samples: List[str] = Field(default_factory=list)
    output_dir: str = ""
    use_gui_files_by_default: bool = True
    busy: bool = False
    busy_reason: Optional[str] = None
    active_job_id: Optional[str] = None
    api_conflicts: List[ApiConflictItem] = Field(default_factory=list)
    api_conflict_warning: Optional[str] = None


class CreateJobResponse(BaseModel):
    """Response returned when a new MCP translation job is created."""

    job_id: str
    status: str
    total_pages: int
    pending_pages: int
    skipped_pages: int
    effective_mode: EffectiveMode
    image_delivery: ImageDeliveryMode
    max_parallel_batches: int
    batch_size: int
    context_size: int
    target_lang: str
    ai_line_break_enabled: bool
    extract_glossary: bool
    guidelines: str
    glossary: Dict[str, Any] = Field(default_factory=dict)
    glossary_version: int = 0
    output_dir: str = ""


class RegionItem(BaseModel):
    """One merged text block in a page ready for LLM translation."""

    id: int
    text: str
    original_region_count: int = 1
    direction: str = "auto"
    prob: float = 1.0


class PageBatchItem(BaseModel):
    """Metadata and OCR regions for one page in a translation batch."""

    page_index: int
    filename: str
    source_path: str = ""
    width: int = 0
    height: int = 0
    regions: List[RegionItem] = Field(default_factory=list)
    low_confidence_ids: List[int] = Field(default_factory=list)
    image_delivery_mode: Literal["none", "mcp_image", "file_path"] = "none"
    preview_path: Optional[str] = None
    preview_mime: Optional[str] = None
    preview_error: Optional[str] = None


class JobProgressSummary(BaseModel):
    """Compact progress counters attached to batch payloads."""

    total_pages: int = 0
    pending_pages: int = 0
    skipped_pages: int = 0
    preprocessed_pages: int = 0
    committed_pages: int = 0
    rendered_pages: int = 0
    failed_pages: int = 0
    textless_pages: int = 0
    on_demand_image_requests: int = 0


class BatchPayload(BaseModel):
    """Batch lease payload returned by next_translation_batch and submit_translation_batch."""

    done: bool = False
    waiting: bool = False
    waiting_reason: Optional[str] = None
    job_id: str
    batch_id: Optional[str] = None
    lease_expires_in: Optional[int] = None
    progress: JobProgressSummary = Field(default_factory=JobProgressSummary)
    prev_context: List[Dict[str, Any]] = Field(default_factory=list)
    glossary_delta: List[Dict[str, Any]] = Field(default_factory=list)
    glossary_version: int = 0
    pages: List[PageBatchItem] = Field(default_factory=list)
    summary: Optional[Dict[str, Any]] = None
    hints: List[str] = Field(default_factory=list)


class TranslationSubmissionItem(BaseModel):
    """Single translated text block submitted by the MCP client."""

    id: int
    translation: str


class ValidationErrorItem(BaseModel):
    """Validation failure item instructing the MCP client which bubble IDs need fixing."""

    id: Optional[int] = None
    reason: str
    original_region_count: Optional[int] = None
    detail: str = ""


class SubmitBatchResponse(BaseModel):
    """Result of validating, committing, and optionally advancing to the next batch."""

    status: Literal["accepted", "retry_needed", "error"]
    job_id: str
    batch_id: str
    errors: List[ValidationErrorItem] = Field(default_factory=list)
    merged_terms_count: int = 0
    redo_pages: List[int] = Field(default_factory=list)
    next_batch: Optional[BatchPayload] = None
    done: bool = False
    summary: Optional[Dict[str, Any]] = None
    message: str = ""


class PagePreviewResponse(BaseModel):
    """Response for on-demand full-page or cropped-region preview inspection."""

    job_id: str
    page_index: int
    filename: str
    region_ids: Optional[List[int]] = None
    high_res: bool = False
    delivery_mode: Literal["mcp_image", "file_path"]
    file_path: Optional[str] = None
    mime_type: str
    width: int
    height: int
    lease_renewed: bool = False
    note: str = ""


class JobStatusResponse(BaseModel):
    """Full status snapshot for a running or finished MCP translation job."""

    job_id: str
    status: str
    effective_mode: str
    output_dir: str = ""
    total_pages: int = 0
    pending_pages: int = 0
    skipped_pages: int = 0
    preprocessed_pages: int = 0
    committed_pages: int = 0
    rendered_pages: int = 0
    failed_pages: int = 0
    textless_pages: int = 0
    on_demand_image_requests: int = 0
    queues: Dict[str, int] = Field(default_factory=dict)
    leases: List[Dict[str, Any]] = Field(default_factory=list)
    failed_details: List[Dict[str, str]] = Field(default_factory=list)
    output_paths: List[str] = Field(default_factory=list)
    error: Optional[str] = None
