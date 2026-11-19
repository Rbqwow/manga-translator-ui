"""Universal Streamable HTTP MCP integration package (lazy-imports heavy CV/torch/ASGI modules)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .batch_adapter import find_api_backend_conflicts
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
    format_mcp_endpoint_url,
)

if TYPE_CHECKING:
    from .job_manager import McpJobManager
    from .server_app import (
        McpHttpServer,
        configure_mcp_transport_logging,
        create_fastmcp_server,
    )

_LAZY_EXPORTS = {
    "McpJobManager": ("manga_translator.mcp.job_manager", "McpJobManager"),
    "McpHttpServer": ("manga_translator.mcp.server_app", "McpHttpServer"),
    "configure_mcp_transport_logging": (
        "manga_translator.mcp.server_app",
        "configure_mcp_transport_logging",
    ),
    "create_fastmcp_server": ("manga_translator.mcp.server_app", "create_fastmcp_server"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    from importlib import import_module

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


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
    "configure_mcp_transport_logging",
    "create_fastmcp_server",
    "find_api_backend_conflicts",
    "format_mcp_endpoint_url",
]
