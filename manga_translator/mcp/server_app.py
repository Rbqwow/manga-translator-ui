"""FastMCP server registration and embedded uvicorn Streamable HTTP lifecycle."""

from __future__ import annotations

import asyncio
import base64
import logging
import sys
import threading
from typing import Any, Dict, List, Literal, Optional

import anyio
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import uvicorn

from .job_manager import McpJobManager
from .protocol_models import TranslationSubmissionItem

logger = logging.getLogger("manga_translator.mcp")


def _encode_inline_images(inline_images: List[tuple[bytes, str]]) -> List[types.ImageContent]:
    return [
        types.ImageContent(
            type="image",
            data=base64.b64encode(raw_bytes).decode("ascii"),
            mimeType=mime_type,
        )
        for raw_bytes, mime_type in inline_images
    ]


def create_fastmcp_server(
    job_manager: McpJobManager,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> FastMCP:
    """Create and configure the FastMCP server with all 9 translation workflow tools."""
    mcp = FastMCP(
        name="manga-translator",
        instructions=(
            "Local CV/GPU/Qt manga translation server. Follow the workflow: "
            "get_workspace_info -> create_translation_job -> next_translation_batch -> "
            "submit_translation_batch(..., return_next=True) until done=true."
        ),
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "127.0.0.1:*",
                "localhost:*",
                "[::1]:*",
                "127.0.0.1",
                "localhost",
                "[::1]",
            ],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
                "https://127.0.0.1:*",
                "https://localhost:*",
            ],
        ),
    )

    @mcp.tool(
        name="get_workspace_info",
        description=(
            "Return read-only workspace configuration, effective MCP mode (hq/text_on_demand/text_only), "
            "GUI file list count/samples, output directory, busy state, and any 0-API backend conflicts."
        ),
    )
    async def get_workspace_info() -> Dict[str, Any]:
        resp = await anyio.to_thread.run_sync(job_manager.get_workspace_info)
        return resp.model_dump()

    @mcp.tool(
        name="create_translation_job",
        description=(
            "Create a local CV/GPU/Qt manga translation job using the current settings (0 external LLM APIs). "
            "Returns job_id, skip/pending counts, and one-time guidelines + initial glossary."
        ),
    )
    async def create_translation_job(
        inputs: Optional[List[str]] = None,
        output_dir: Optional[str] = None,
        use_gui_files: Optional[bool] = None,
        translation_mode: Optional[Literal["hq", "text"]] = None,
        allow_on_demand_image: Optional[bool] = None,
        target_lang: Optional[str] = None,
        batch_size: Optional[int] = None,
        context_size: Optional[int] = None,
        overwrite: Optional[bool] = None,
        retranslate_json: bool = False,
    ) -> Dict[str, Any]:
        def _run():
            return job_manager.create_translation_job(
                inputs=inputs,
                output_dir=output_dir,
                use_gui_files=use_gui_files,
                translation_mode=translation_mode,
                allow_on_demand_image=allow_on_demand_image,
                target_lang=target_lang,
                batch_size=batch_size,
                context_size=context_size,
                overwrite=overwrite,
                retranslate_json=retranslate_json,
            )

        resp = await anyio.to_thread.run_sync(_run)
        return resp.model_dump()

    @mcp.tool(
        name="next_translation_batch",
        description=(
            "Lease the next preprocessed translation batch. Returns JSON metadata/OCR regions/prev_context/"
            "glossary_delta and, in HQ mode, numbered page images (inline ImageContent or file_path)."
        ),
        structured_output=False,
    )
    async def next_translation_batch(
        job_id: str,
        worker_id: Optional[str] = None,
    ) -> List[types.TextContent | types.ImageContent]:
        payload, inline_images = await anyio.to_thread.run_sync(
            lambda: job_manager.next_translation_batch(job_id, worker_id=worker_id)
        )
        blocks: List[types.TextContent | types.ImageContent] = [
            types.TextContent(type="text", text=payload.model_dump_json())
        ]
        blocks.extend(_encode_inline_images(inline_images))
        return blocks

    @mcp.tool(
        name="get_page_preview",
        description=(
            "Fetch a full-page or region-cropped numbered preview image for a page in hq or text_on_demand mode. "
            "Automatically renews the batch lease."
        ),
        structured_output=False,
    )
    async def get_page_preview(
        job_id: str,
        page_index: int,
        region_ids: Optional[List[int]] = None,
        high_res: bool = False,
    ) -> List[types.TextContent | types.ImageContent]:
        resp, raw_bytes = await anyio.to_thread.run_sync(
            lambda: job_manager.get_page_preview(
                job_id,
                page_index,
                region_ids=region_ids,
                high_res=high_res,
            )
        )
        blocks: List[types.TextContent | types.ImageContent] = [
            types.TextContent(type="text", text=resp.model_dump_json())
        ]
        if raw_bytes is not None:
            blocks.extend(_encode_inline_images([(raw_bytes, resp.mime_type)]))
        return blocks

    @mcp.tool(
        name="submit_translation_batch",
        description=(
            "Validate and submit translations [{id, translation}] and optional new_terms for a leased batch. "
            "When return_next=True, immediately leases and returns the next batch (or waits for final save when done)."
        ),
        structured_output=False,
    )
    async def submit_translation_batch(
        job_id: str,
        batch_id: str,
        translations: List[TranslationSubmissionItem],
        new_terms: Optional[List[Dict[str, Any]]] = None,
        return_next: bool = True,
    ) -> List[types.TextContent | types.ImageContent]:
        resp, inline_images = await anyio.to_thread.run_sync(
            lambda: job_manager.submit_translation_batch(
                job_id,
                batch_id,
                translations,
                new_terms=new_terms,
                return_next=return_next,
            )
        )
        blocks: List[types.TextContent | types.ImageContent] = [
            types.TextContent(type="text", text=resp.model_dump_json())
        ]
        blocks.extend(_encode_inline_images(inline_images))
        return blocks

    @mcp.tool(
        name="release_batch",
        description="Release a currently leased batch back to the READY queue so another worker can take it.",
    )
    async def release_batch(job_id: str, batch_id: str) -> Dict[str, Any]:
        return await anyio.to_thread.run_sync(lambda: job_manager.release_batch(job_id, batch_id))

    @mcp.tool(
        name="get_job_status",
        description="Inspect progress, queue depths, active leases, failures, and saved output paths for an MCP job.",
    )
    async def get_job_status(job_id: Optional[str] = None) -> Dict[str, Any]:
        return await anyio.to_thread.run_sync(lambda: job_manager.get_job_status(job_id))

    @mcp.tool(
        name="cancel_job",
        description="Cancel the active or specified MCP translation job and clean up temporary preview files.",
    )
    async def cancel_job(job_id: Optional[str] = None) -> Dict[str, Any]:
        return await anyio.to_thread.run_sync(lambda: job_manager.cancel_job(job_id))

    @mcp.tool(
        name="unload_models",
        description="Immediately unload cached GPU models and trim process working set when no job is running.",
    )
    async def unload_models() -> Dict[str, Any]:
        return await anyio.to_thread.run_sync(job_manager.unload_models)

    return mcp


class _SignalingUvicornServer(uvicorn.Server):
    """Uvicorn server subclass that signals bind readiness and skips OS signal hooks off main thread."""

    def __init__(self, config: uvicorn.Config, ready_event: threading.Event):
        super().__init__(config)
        self._ready_event = ready_event
        self.bound_port: int = int(config.port)
        self.startup_error: Optional[BaseException] = None

    def install_signal_handlers(self) -> None:
        return None

    async def startup(self, sockets=None) -> None:
        try:
            await super().startup(sockets=sockets)
        except SystemExit as exc:
            self.startup_error = OSError(f"Failed to bind MCP HTTP server on {self.config.host}:{self.config.port}")
            self._ready_event.set()
            raise exc
        except BaseException as exc:
            self.startup_error = exc
            self._ready_event.set()
            raise

        if getattr(self, "servers", None):
            try:
                sock = self.servers[0].sockets[0]
                self.bound_port = int(sock.getsockname()[1])
            except Exception:
                pass
        self._ready_event.set()


class McpHttpServer:
    """Lifecycle controller running FastMCP's Streamable HTTP ASGI app inside a background uvicorn thread."""

    def __init__(self, job_manager: McpJobManager):
        self.job_manager = job_manager
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[_SignalingUvicornServer] = None
        self._ready_event: Optional[threading.Event] = None
        self._host: str = "127.0.0.1"
        self._port: int = 8765
        self._last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return (
                self._thread is not None
                and self._thread.is_alive()
                and self._server is not None
                and not self._server.should_exit
            )

    @property
    def host(self) -> str:
        with self._lock:
            return self._host

    @property
    def port(self) -> int:
        with self._lock:
            return self._port

    @property
    def endpoint_url(self) -> str:
        with self._lock:
            return f"http://{self._host}:{self._port}/mcp"

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    def start(self, host: str = "127.0.0.1", port: int = 8765, timeout: float = 5.0) -> str:
        """Start the Streamable HTTP server thread and return the active /mcp endpoint URL."""
        with self._lock:
            if self.is_running:
                if self._host == host and (port == 0 or self._port == port):
                    return self.endpoint_url
                self.stop()

            self._last_error = None
            ready_event = threading.Event()
            self._ready_event = ready_event
            self._host = host
            self._port = int(port)

            fastmcp = create_fastmcp_server(self.job_manager, host=host, port=int(port))
            starlette_app = fastmcp.streamable_http_app()
            config = uvicorn.Config(
                starlette_app,
                host=host,
                port=int(port),
                log_config=None,
                access_log=False,
                lifespan="on",
            )
            server = _SignalingUvicornServer(config, ready_event)
            self._server = server

            def _serve() -> None:
                loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(server.serve())
                except SystemExit:
                    pass
                except Exception as exc:
                    if server.startup_error is None:
                        server.startup_error = exc
                    logger.error(f"[MCP] HTTP server exited with error: {exc}", exc_info=True)
                finally:
                    ready_event.set()
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:
                        pass
                    asyncio.set_event_loop(None)
                    loop.close()

            thread = threading.Thread(target=_serve, name="mcp-http-server", daemon=True)
            self._thread = thread
            thread.start()

        if not ready_event.wait(timeout=timeout):
            self.stop()
            raise TimeoutError(f"MCP HTTP server failed to bind {host}:{port} within {timeout:.1f}s")

        with self._lock:
            if server.startup_error is not None:
                err = server.startup_error
                self._last_error = str(err)
                self._cleanup_stopped_refs_locked()
                raise RuntimeError(f"Failed to start MCP HTTP server on {host}:{port}: {err}") from err
            if not thread.is_alive() or not server.started:
                self._last_error = "MCP HTTP server stopped unexpectedly during startup"
                self._cleanup_stopped_refs_locked()
                raise RuntimeError(self._last_error)
            self._port = server.bound_port
            url = f"http://{self._host}:{self._port}/mcp"

        logger.info(f"[MCP] Streamable HTTP server listening on {url}")
        return url

    def _cleanup_stopped_refs_locked(self) -> None:
        self._server = None
        self._thread = None
        self._ready_event = None

    def stop(self, timeout: float = 5.0) -> None:
        """Gracefully shut down uvicorn and join its background thread."""
        with self._lock:
            server = self._server
            thread = self._thread
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._cleanup_stopped_refs_locked()
        logger.info("[MCP] Streamable HTTP server stopped")
