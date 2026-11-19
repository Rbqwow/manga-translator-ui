"""Universal Streamable HTTP MCP integration package (lazy-imports heavy CV/torch modules)."""

from __future__ import annotations

from .batch_adapter import find_api_backend_conflicts
from .job_manager import McpJobManager
from .protocol_models import (
    ApiConflictItem,
    BatchPayload,
    CreateJobResponse,
    JobProgressSummary,
    JobStatusResponse,
    PageBatchItem,
    PagePreviewResponse,
    RegionItem,
    SubmitBatchResponse,
    TranslationSubmissionItem,
    ValidationErrorItem,
    WorkspaceInfoResponse,
)
from .server_app import McpHttpServer, create_fastmcp_server

__all__ = [
    "ApiConflictItem",
    "BatchPayload",
    "CreateJobResponse",
    "JobProgressSummary",
    "JobStatusResponse",
    "McpHttpServer",
    "McpJobManager",
    "PageBatchItem",
    "PagePreviewResponse",
    "RegionItem",
    "SubmitBatchResponse",
    "TranslationSubmissionItem",
    "ValidationErrorItem",
    "WorkspaceInfoResponse",
    "create_fastmcp_server",
    "find_api_backend_conflicts",
]
