"""McpJobManager and dual-queue GPU/Render orchestration for MCP translation."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple

from .batch_adapter import (
    apply_batch_post_processing,
    build_mcp_engine_config,
    build_page_region_items,
    create_mcp_translator,
    find_api_backend_conflicts,
    load_job_guidelines_and_glossary,
    preprocess_page,
    render_and_save_page,
    render_cropped_region_preview,
    render_numbered_page_preview,
    resolve_batch_image_delivery,
    run_page_inpaint,
    validate_batch_translations,
    write_preview_bytes_to_disk,
)
from .protocol_models import (
    BatchPayload,
    CreateJobResponse,
    EffectiveMode,
    JobProgressSummary,
    JobStatusResponse,
    PageBatchItem,
    PagePreviewResponse,
    SubmitBatchResponse,
    TranslationSubmissionItem,
    WorkspaceInfoResponse,
)

logger = logging.getLogger("manga_translator.mcp")


def _new_worker_event_loop() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def _run_coro_sync(coro):
    loop = _new_worker_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()


def _natural_sort_key(path: str):
    import re

    normalized = path.replace("\\", "/")
    parts = []
    for part in re.split(r"(\d+)", normalized):
        if part.isdigit():
            parts.append((False, int(part)))
        elif part:
            parts.append((True, part.lower()))
    return parts


def _default_expand_inputs(
    source_files: Sequence[str],
    excluded_subfolders: Sequence[str],
    excluded_files: Sequence[str],
    output_base_dir: str,
    overwrite_extract: bool,
) -> Tuple[List[str], Dict[str, Optional[str]], Dict[str, str]]:
    """Fallback file/folder/archive expander when no external expander is injected."""
    from manga_translator.image_formats import SUPPORTED_IMAGE_EXTENSIONS

    try:
        from desktop_qt_ui.utils.archive_extractor import (
            check_output_extract_conflict,
            clear_output_extract_root,
            extract_images_from_archive,
            get_output_extract_dir,
            is_archive_file,
            write_output_extract_marker,
        )
    except Exception:
        def is_archive_file(_p: str) -> bool:
            return False

        extract_images_from_archive = None
        check_output_extract_conflict = None
        clear_output_extract_root = None
        get_output_extract_dir = None
        write_output_extract_marker = None

    def _norm_key(p: str) -> str:
        return os.path.normcase(os.path.abspath(os.path.normpath(p)))

    def _is_within(p: str, folder: str) -> bool:
        try:
            return os.path.commonpath([_norm_key(p), _norm_key(folder)]) == _norm_key(folder)
        except ValueError:
            return False

    excluded_file_keys = {_norm_key(p) for p in excluded_files}
    excluded_folders = list(excluded_subfolders)

    def _is_excluded(p: str) -> bool:
        if _norm_key(p) in excluded_file_keys:
            return True
        return any(_is_within(p, f) for f in excluded_folders)

    resolved: List[str] = []
    file_to_folder_map: Dict[str, Optional[str]] = {}
    archive_to_temp_map: Dict[str, str] = {}
    processed_archives: set[str] = set()

    folders: List[str] = []
    individual_files: List[str] = []
    archive_files: List[str] = []

    for path in source_files:
        if not path:
            continue
        if os.path.isdir(path):
            folders.append(path)
        elif os.path.isfile(path):
            if is_archive_file(path):
                archive_files.append(path)
            elif os.path.splitext(path)[1].lower() in SUPPORTED_IMAGE_EXTENSIONS:
                individual_files.append(path)

    def _extract_archive(archive_path: str, scan_root: Optional[str] = None) -> None:
        if extract_images_from_archive is None:
            return
        norm_arch = _norm_key(archive_path)
        if norm_arch in processed_archives:
            return
        processed_archives.add(norm_arch)

        archive_output_base = ""
        if output_base_dir and os.path.isdir(output_base_dir):
            if not scan_root:
                archive_output_base = output_base_dir
            else:
                try:
                    rel_parent = os.path.relpath(
                        os.path.normpath(os.path.dirname(archive_path)),
                        os.path.normpath(scan_root),
                    )
                    nested = os.path.join(output_base_dir, os.path.basename(os.path.normpath(scan_root)))
                    if rel_parent != ".":
                        nested = os.path.join(nested, rel_parent)
                    archive_output_base = os.path.normpath(nested)
                except ValueError:
                    archive_output_base = output_base_dir

        if archive_output_base:
            if check_output_extract_conflict(archive_output_base, archive_path):
                if not overwrite_extract:
                    return
                clear_output_extract_root(archive_output_base, archive_path)
            extract_dir = get_output_extract_dir(archive_output_base, archive_path)
            images, extracted_dir = extract_images_from_archive(archive_path, extract_dir)
            if images:
                write_output_extract_marker(archive_output_base, archive_path)
        else:
            images, extracted_dir = extract_images_from_archive(archive_path)

        if images:
            archive_to_temp_map[archive_path] = extracted_dir
            for img_path in images:
                resolved.append(img_path)
                file_to_folder_map[img_path] = archive_path

    for arch in archive_files:
        _extract_archive(arch)

    folders.sort(key=_natural_sort_key)
    for folder in folders:
        folder_images: List[str] = []
        folder_archives: List[str] = []
        for root, dirs, files in os.walk(folder):
            if "manga_translator_work" in dirs:
                dirs.remove("manga_translator_work")
            dirs.sort(key=_natural_sort_key)
            cur_imgs: List[str] = []
            cur_archs: List[str] = []
            for fname in files:
                fpath = os.path.join(root, fname)
                if not os.path.isfile(fpath) or _is_excluded(fpath):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext in SUPPORTED_IMAGE_EXTENSIONS:
                    cur_imgs.append(fpath)
                elif is_archive_file(fpath):
                    cur_archs.append(fpath)
            cur_imgs.sort(key=_natural_sort_key)
            cur_archs.sort(key=_natural_sort_key)
            folder_images.extend(cur_imgs)
            folder_archives.extend(cur_archs)

        for arch in folder_archives:
            _extract_archive(arch, folder)
        resolved.extend(folder_images)
        for img_path in folder_images:
            file_to_folder_map[img_path] = folder

    individual_files.sort(key=_natural_sort_key)
    for fpath in individual_files:
        resolved.append(fpath)
        file_to_folder_map[fpath] = None

    unique_files = list(dict.fromkeys(resolved))
    return unique_files, file_to_folder_map, archive_to_temp_map


@dataclass
class McpPageState:
    """Lifecycle state for one pending page inside an MCP job."""

    page_index: int
    source_order: int
    image_path: str
    filename: str
    ctx: Any = None
    text_order: List[int] = field(default_factory=list)
    low_confidence_ids: List[int] = field(default_factory=list)
    width: int = 0
    height: int = 0
    batch_id: Optional[str] = None
    preprocessed: bool = False
    is_textless: bool = False
    committed: bool = False
    inpaint_in_progress: bool = False
    inpaint_done: bool = False
    pending_redo: bool = False
    render_queued: bool = False
    rendered: bool = False
    failed: bool = False
    error: Optional[str] = None
    output_path: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    preview_cache: Optional[Tuple[bytes, str, int, int]] = None
    preview_error: Optional[str] = None
    preview_ready: threading.Event = field(default_factory=threading.Event)


@dataclass
class McpBatchState:
    """Lease and validation state for one batch of pages."""

    batch_id: str
    batch_num: int
    page_indices: List[int]
    status: Literal["READY", "LEASED", "COMMITTED", "DONE", "FAILED"] = "READY"
    worker_id: Optional[str] = None
    leased_at: Optional[float] = None
    submitting: bool = False
    retry_count: int = 0
    preview_future: Optional[Future] = None
    preview_files: List[str] = field(default_factory=list)


class McpTranslationJob:
    """Coordinates GpuPriorityWorker, CPU preview pool, RenderWorker, and MCP leases."""

    def __init__(
        self,
        *,
        job_id: str,
        config: Any,
        translator: Any,
        mcp_settings: Dict[str, Any],
        save_info: Dict[str, Any],
        plan: Any,
        archive_to_temp_map: Dict[str, str],
        preview_dir: str,
        guidelines: str,
        glossary: Dict[str, Any],
        prompt_file_path: Optional[str],
        effective_mode: EffectiveMode,
        retranslate_json: bool,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_finished: Optional[Callable[["McpTranslationJob"], None]] = None,
    ):
        from manga_translator.utils.batch_skip import input_path, normalize_path

        self.job_id = job_id
        self.config = config
        self.translator = translator
        self.mcp_settings = dict(mcp_settings)
        self.save_info = dict(save_info)
        self.plan = plan
        self.archive_to_temp_map = dict(archive_to_temp_map)
        self.preview_dir = preview_dir
        self.guidelines = guidelines
        self.glossary = glossary
        self.prompt_file_path = prompt_file_path
        self.effective_mode: EffectiveMode = effective_mode
        self.retranslate_json = bool(retranslate_json)
        self._on_progress = on_progress
        self._on_finished = on_finished

        self.batch_size = max(1, int(getattr(translator, "batch_size", 3) or 3))
        self.context_size = max(0, int(getattr(translator, "context_size", 0) or 0))
        self.batch_concurrent = bool(getattr(translator, "batch_concurrent", True))
        self.max_parallel_batches = max(1, min(4, int(self.mcp_settings.get("max_parallel_batches", 1) or 1)))
        self.lease_timeout_seconds = max(0.05, float(self.mcp_settings.get("lease_timeout_seconds", 180) or 180))
        self.image_delivery = str(self.mcp_settings.get("image_delivery", "auto") or "auto")
        self.inline_image_budget_mb = float(self.mcp_settings.get("inline_image_budget_mb", 2.0) or 2.0)
        self.preview_max_size = int(self.mcp_settings.get("preview_max_size", 1024) or 1024)
        self.preview_format = str(self.mcp_settings.get("preview_format", "jpeg") or "jpeg")
        self.preview_quality = int(self.mcp_settings.get("preview_quality", 82) or 82)

        self.prefetch_batches = 1 if self.batch_concurrent else 0
        self.max_inflight_pages = max(
            self.batch_size,
            self.batch_size * (self.max_parallel_batches + self.prefetch_batches + 1),
        )

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self.cancel_event = threading.Event()
        self._finished_event = threading.Event()

        self.total_pages = len(plan.source_items)
        self.skipped_pages = len(plan.skipped_contexts)
        self.pending_pages_count = len(plan.pending_items)

        self.pages: List[McpPageState] = []
        for idx, item in enumerate(plan.pending_items):
            img_path = input_path(item)
            source_order = plan.source_order.get(normalize_path(img_path), idx)
            self.pages.append(
                McpPageState(
                    page_index=idx,
                    source_order=source_order,
                    image_path=img_path,
                    filename=os.path.basename(img_path),
                )
            )

        # Partition pending indices into contiguous segments split at skipped-page context boundaries.
        skipped_orders = {order for order, _path, _entries in plan.resume_pages}
        self._segments: List[List[int]] = []
        current_seg: List[int] = []
        for idx, page in enumerate(self.pages):
            if current_seg:
                prev_order = self.pages[current_seg[-1]].source_order
                if any(prev_order < s_order < page.source_order for s_order in skipped_orders):
                    self._segments.append(current_seg)
                    current_seg = []
            current_seg.append(idx)
        if current_seg:
            self._segments.append(current_seg)

        self._segment_cursor = 0
        self._item_cursor = 0
        self._building_batch_pages: List[int] = []
        self._preprocessing_done = len(self.pages) == 0

        self._inflight_page_indices: set[int] = set()
        self._batches: Dict[str, McpBatchState] = {}
        self._batch_order: List[str] = []
        self._batch_seq = 0
        self._render_queue: List[int] = []

        # Out-of-order safe cross-page context history keyed by source_order.
        self._translation_history: Dict[int, List[Dict[str, Any]]] = {}
        for order, _path, entries in plan.resume_pages:
            self._translation_history[order] = list(entries)

        # Glossary increment tracking for parallel/relay workers.
        self._glossary_version = 0
        self._glossary_events: List[Tuple[int, List[Dict[str, Any]]]] = []
        self._worker_glossary_seen: Dict[str, int] = {}

        self.on_demand_image_requests = 0
        self.output_paths: List[str] = []
        self.completed_output_sources: Dict[str, str] = {}
        self.status: str = "running" if self.pages else "completed"
        self.error: Optional[str] = None

        self._preview_pool: Optional[ThreadPoolExecutor] = None
        self._gpu_thread: Optional[threading.Thread] = None
        self._render_thread: Optional[threading.Thread] = None
        self._finalized = False

        if not self.pages:
            self._finalize_job_locked()

    def start(self) -> None:
        if not self.pages:
            return
        self._preview_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"mcp-preview-{self.job_id[:6]}")
        self._render_thread = threading.Thread(
            target=self._run_render_worker,
            name=f"mcp-render-{self.job_id[:6]}",
            daemon=True,
        )
        self._gpu_thread = threading.Thread(
            target=self._run_gpu_worker,
            name=f"mcp-gpu-{self.job_id[:6]}",
            daemon=True,
        )
        self._render_thread.start()
        self._gpu_thread.start()
        self._emit_progress()

    def _emit_progress(self) -> None:
        if self._on_progress is None:
            return
        try:
            self._on_progress(self.get_status_dict())
        except Exception:
            logger.debug("MCP progress callback failed", exc_info=True)

    def _cleanup_temp_artifacts(self) -> None:
        if self.preview_dir and os.path.isdir(self.preview_dir):
            shutil.rmtree(self.preview_dir, ignore_errors=True)
        if self.archive_to_temp_map:
            try:
                from desktop_qt_ui.utils.archive_extractor import cleanup_archive_temp

                for archive_path in list(self.archive_to_temp_map):
                    try:
                        cleanup_archive_temp(archive_path)
                    except Exception:
                        pass
            except Exception:
                pass
            self.archive_to_temp_map.clear()

    def _delete_batch_preview_files(self, batch: McpBatchState) -> None:
        for file_path in batch.preview_files:
            try:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
            except OSError:
                pass
        batch.preview_files.clear()

    def _reclaim_expired_leases_locked(self) -> None:
        now = time.monotonic()
        reclaimed = False
        for batch_id in self._batch_order:
            batch = self._batches[batch_id]
            if batch.status == "LEASED" and not batch.submitting and batch.leased_at is not None:
                if now - batch.leased_at > self.lease_timeout_seconds:
                    logger.warning(
                        f"[MCP] Reclaiming expired lease for {batch.batch_id} "
                        f"(worker={batch.worker_id}, elapsed={now - batch.leased_at:.1f}s)"
                    )
                    batch.status = "READY"
                    batch.worker_id = None
                    batch.leased_at = None
                    reclaimed = True
        if reclaimed:
            self._cond.notify_all()

    def _progress_summary_locked(self) -> JobProgressSummary:
        preprocessed = sum(1 for p in self.pages if p.preprocessed)
        committed = sum(1 for p in self.pages if p.committed and not p.is_textless and not p.failed)
        rendered = sum(1 for p in self.pages if p.rendered and not p.failed)
        failed = sum(1 for p in self.pages if p.failed)
        textless = sum(1 for p in self.pages if p.is_textless)
        return JobProgressSummary(
            total_pages=self.total_pages,
            pending_pages=self.pending_pages_count,
            skipped_pages=self.skipped_pages,
            preprocessed_pages=preprocessed,
            committed_pages=committed,
            rendered_pages=rendered,
            failed_pages=failed,
            textless_pages=textless,
            on_demand_image_requests=self.on_demand_image_requests,
        )

    def _build_completion_summary_locked(self) -> Dict[str, Any]:
        summary = self._progress_summary_locked().model_dump()
        summary.update(
            {
                "job_id": self.job_id,
                "status": self.status,
                "output_dir": self.save_info.get("output_folder", ""),
                "output_paths": list(self.output_paths),
                "failed_details": [
                    {"page_index": p.page_index, "filename": p.filename, "error": p.error or ""}
                    for p in self.pages
                    if p.failed
                ],
            }
        )
        return summary

    def _check_all_done_locked(self) -> bool:
        if not self._preprocessing_done:
            return False
        if self._building_batch_pages:
            return False
        if any(b.status in ("READY", "LEASED") for b in self._batches.values()):
            return False
        return all(p.rendered or p.failed for p in self.pages)

    def _finalize_job_locked(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self.cancel_event.is_set():
            self.status = "cancelled"
        elif self.error or any(p.failed for p in self.pages):
            self.status = "failed" if self.error and not self.output_paths else "completed_with_errors"
        else:
            self.status = "completed"
        self._cleanup_temp_artifacts()
        self._finished_event.set()
        self._cond.notify_all()
        if self._on_finished is not None:
            callback = self._on_finished
            threading.Thread(
                target=callback,
                args=(self,),
                name=f"mcp-finish-{self.job_id[:6]}",
                daemon=True,
            ).start()

    def _all_batches_committed_or_done_locked(self) -> bool:
        if not self._preprocessing_done or self._building_batch_pages:
            return False
        return not any(b.status in ("READY", "LEASED") for b in self._batches.values())

    def _has_next_unpreprocessed_locked(self) -> bool:
        while self._segment_cursor < len(self._segments):
            if self._item_cursor < len(self._segments[self._segment_cursor]):
                return True
            self._segment_cursor += 1
            self._item_cursor = 0
        return False

    def _seal_building_batch_locked(self) -> Optional[McpBatchState]:
        if not self._building_batch_pages:
            return None
        self._batch_seq += 1
        batch_id = f"batch_{self._batch_seq:04d}"
        page_indices = list(self._building_batch_pages)
        self._building_batch_pages.clear()

        global_text_index = 1
        for p_idx in page_indices:
            page = self.pages[p_idx]
            page.batch_id = batch_id
            regions = list(getattr(page.ctx, "text_regions", None) or [])
            _items, text_order, low_conf_ids = build_page_region_items(page.ctx, global_text_index)
            page.text_order = text_order
            page.low_confidence_ids = low_conf_ids
            global_text_index += len(regions)

        batch = McpBatchState(
            batch_id=batch_id,
            batch_num=self._batch_seq,
            page_indices=page_indices,
            status="READY",
        )
        self._batches[batch_id] = batch
        self._batch_order.append(batch_id)

        if self.effective_mode == "hq" and self._preview_pool is not None:
            for p_idx in page_indices:
                page = self.pages[p_idx]
                page.preview_ready.clear()
            batch.preview_future = self._preview_pool.submit(self._render_batch_previews, batch)

        self._cond.notify_all()
        return batch

    def _render_batch_previews(self, batch: McpBatchState) -> None:
        for p_idx in batch.page_indices:
            page = self.pages[p_idx]
            if self.cancel_event.is_set():
                page.preview_ready.set()
                continue
            try:
                encoded = render_numbered_page_preview(
                    page.ctx,
                    page.text_order,
                    max_size=self.preview_max_size,
                    fmt=self.preview_format,
                    quality=self.preview_quality,
                )
                page.preview_cache = encoded
            except Exception as exc:
                page.preview_error = str(exc)
                logger.warning(f"[MCP] Preview render failed for {page.filename}: {exc}")
            finally:
                page.preview_ready.set()

    # ------------------------------------------------------------------
    # Background worker threads
    # ------------------------------------------------------------------
    def _pick_gpu_action_locked(self) -> Optional[Tuple[str, Any]]:
        """Choose highest-priority GPU task (P0 > P1 > P2 > P3)."""
        if self.cancel_event.is_set() or self._finalized:
            return None

        # P0-a: pending_redo pages (highest priority so filtered pages drain to render)
        for p in self.pages:
            if p.pending_redo and not p.inpaint_in_progress and not p.failed:
                return ("inpaint_redo", p.page_index)

        # P0-b: committed pages waiting for first inpaint when window is nearly full or preprocessing finished
        high_watermark = max(1, self.max_inflight_pages - self.batch_size)
        if len(self._inflight_page_indices) >= high_watermark or self._preprocessing_done:
            for p in self.pages:
                if (
                    p.committed
                    and not p.is_textless
                    and not p.inpaint_done
                    and not p.inpaint_in_progress
                    and not p.failed
                ):
                    return ("inpaint_first", p.page_index)

        # P1: keep READY batches stocked up to max_parallel_batches (critical path!)
        ready_count = sum(1 for b in self._batches.values() if b.status == "READY")
        if (
            ready_count < self.max_parallel_batches
            and not self._preprocessing_done
            and len(self._inflight_page_indices) < self.max_inflight_pages
        ):
            if self._has_next_unpreprocessed_locked():
                p_idx = self._segments[self._segment_cursor][self._item_cursor]
                return ("preprocess", (self._segment_cursor, self._item_cursor, p_idx))
            self._preprocessing_done = True
            self._seal_building_batch_locked()

        # P2: normal inpaint for committed pages, or speculative pre-inpaint for leased pages
        for p in self.pages:
            if (
                p.committed
                and not p.is_textless
                and not p.inpaint_done
                and not p.inpaint_in_progress
                and not p.failed
            ):
                return ("inpaint_first", p.page_index)

        if self.batch_concurrent:
            for batch_id in self._batch_order:
                batch = self._batches[batch_id]
                if batch.status != "LEASED" or batch.submitting:
                    continue
                for p_idx in batch.page_indices:
                    p = self.pages[p_idx]
                    if (
                        not p.is_textless
                        and not p.inpaint_done
                        and not p.inpaint_in_progress
                        and not p.failed
                    ):
                        return ("inpaint_first", p.page_index)

        # P3: far prefetch if window allows and batch_concurrent is enabled
        if (
            self.batch_concurrent
            and ready_count < self.max_parallel_batches + self.prefetch_batches
            and not self._preprocessing_done
            and len(self._inflight_page_indices) < self.max_inflight_pages
        ):
            if self._has_next_unpreprocessed_locked():
                p_idx = self._segments[self._segment_cursor][self._item_cursor]
                return ("preprocess", (self._segment_cursor, self._item_cursor, p_idx))
            self._preprocessing_done = True
            self._seal_building_batch_locked()

        return None

    def _run_gpu_worker(self) -> None:
        loop = _new_worker_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self.cancel_event.is_set():
                with self._cond:
                    action = self._pick_gpu_action_locked()
                    if action is None:
                        if self._check_all_done_locked():
                            self._finalize_job_locked()
                            break
                        if (
                            self._preprocessing_done
                            and not self._building_batch_pages
                            and not any(
                                (p.pending_redo or (p.committed and not p.inpaint_done and not p.is_textless))
                                and not p.failed
                                for p in self.pages
                            )
                            and not (
                                self.batch_concurrent
                                and any(
                                    b.status == "LEASED"
                                    and any(not self.pages[i].inpaint_done and not self.pages[i].failed for i in b.page_indices)
                                    for b in self._batches.values()
                                )
                            )
                        ):
                            # Nothing left for GPU worker unless a batch gets committed or redo added; wait.
                            pass
                        self._cond.wait(timeout=0.25)
                        continue

                    kind, payload = action
                    if kind == "preprocess":
                        seg_idx, item_idx, p_idx = payload
                        self._item_cursor += 1
                        at_segment_end = self._item_cursor >= len(self._segments[seg_idx])
                        self._inflight_page_indices.add(p_idx)
                    elif kind in ("inpaint_first", "inpaint_redo"):
                        p_idx = payload
                        self.pages[p_idx].inpaint_in_progress = True

                if kind == "preprocess":
                    page = self.pages[p_idx]
                    try:
                        ctx = loop.run_until_complete(
                            preprocess_page(
                                self.translator,
                                page.image_path,
                                self.config,
                                retranslate_json=self.retranslate_json,
                            )
                        )
                        img_rgb = getattr(ctx, "img_rgb", None)
                        h, w = img_rgb.shape[:2] if img_rgb is not None else (0, 0)
                        if (w == 0 or h == 0) and getattr(ctx, "original_size", None):
                            w, h = ctx.original_size

                        with self._cond:
                            page.ctx = ctx
                            page.width = int(w)
                            page.height = int(h)
                            page.preprocessed = True

                            if getattr(ctx, "translation_error", None):
                                page.failed = True
                                page.error = str(ctx.translation_error)
                                self._inflight_page_indices.discard(p_idx)
                                self.translator._cleanup_context_memory(ctx, keep_result=False)
                            elif not getattr(ctx, "text_regions", None):
                                page.is_textless = True
                                page.committed = True
                                page.inpaint_done = True
                                page.render_queued = True
                                self._render_queue.append(p_idx)
                            else:
                                self._building_batch_pages.append(p_idx)
                                if (
                                    len(self._building_batch_pages) >= self.batch_size
                                    or at_segment_end
                                    or len(self._inflight_page_indices) >= self.max_inflight_pages
                                ):
                                    self._seal_building_batch_locked()

                            if not self._has_next_unpreprocessed_locked():
                                self._preprocessing_done = True
                                self._seal_building_batch_locked()
                            self._cond.notify_all()
                    except Exception as exc:
                        logger.error(f"[MCP] Preprocessing failed for {page.filename}: {exc}", exc_info=True)
                        with self._cond:
                            page.preprocessed = True
                            page.failed = True
                            page.error = str(exc)
                            self._inflight_page_indices.discard(p_idx)
                            if page.ctx is not None:
                                try:
                                    self.translator._cleanup_context_memory(page.ctx, keep_result=False)
                                except Exception:
                                    pass
                            if at_segment_end:
                                self._seal_building_batch_locked()
                            if not self._has_next_unpreprocessed_locked():
                                self._preprocessing_done = True
                                self._seal_building_batch_locked()
                            self._cond.notify_all()
                    finally:
                        self._emit_progress()

                elif kind in ("inpaint_first", "inpaint_redo"):
                    page = self.pages[p_idx]
                    is_redo = kind == "inpaint_redo"
                    try:
                        loop.run_until_complete(
                            run_page_inpaint(
                                self.translator,
                                self.config,
                                page.ctx,
                                is_redo=is_redo,
                            )
                        )
                        with self._cond:
                            page.inpaint_in_progress = False
                            page.inpaint_done = True
                            if is_redo:
                                page.pending_redo = False
                            if (
                                page.committed
                                and not page.pending_redo
                                and not page.render_queued
                                and not page.failed
                            ):
                                page.render_queued = True
                                self._render_queue.append(p_idx)
                            self._cond.notify_all()
                    except Exception as exc:
                        logger.error(f"[MCP] Inpainting failed for {page.filename}: {exc}", exc_info=True)
                        with self._cond:
                            page.inpaint_in_progress = False
                            page.pending_redo = False
                            page.failed = True
                            page.error = str(exc)
                            self._inflight_page_indices.discard(p_idx)
                            if page.committed and page.ctx is not None:
                                try:
                                    self.translator._cleanup_context_memory(page.ctx, keep_result=False)
                                except Exception:
                                    pass
                            self._cond.notify_all()
                        self._emit_progress()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            asyncio.set_event_loop(None)
            loop.close()

    def _run_render_worker(self) -> None:
        loop = _new_worker_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self.cancel_event.is_set():
                with self._cond:
                    while not self._render_queue and not self.cancel_event.is_set():
                        if self._check_all_done_locked():
                            self._finalize_job_locked()
                            return
                        self._cond.wait(timeout=0.25)

                    if self.cancel_event.is_set():
                        break
                    if not self._render_queue:
                        continue
                    p_idx = self._render_queue.pop(0)

                page = self.pages[p_idx]
                try:
                    res = loop.run_until_complete(
                        render_and_save_page(
                            self.translator,
                            self.config,
                            page.ctx,
                            self.save_info,
                        )
                    )
                    with self._cond:
                        page.rendered = True
                        page.output_path = res.get("output_path")
                        page.skipped = bool(res.get("skipped"))
                        page.skip_reason = res.get("skip_reason")
                        if not res.get("success") and not page.skipped:
                            page.failed = True
                            page.error = res.get("error") or "Render/save failed"
                        elif page.output_path:
                            self.output_paths.append(page.output_path)
                            self.completed_output_sources[os.path.normpath(page.output_path)] = os.path.normpath(
                                page.image_path
                            )
                        self._inflight_page_indices.discard(p_idx)
                        if page.batch_id and page.batch_id in self._batches:
                            batch = self._batches[page.batch_id]
                            if all(self.pages[i].rendered or self.pages[i].failed for i in batch.page_indices):
                                batch.status = "DONE"
                                self._delete_batch_preview_files(batch)
                        if self._check_all_done_locked():
                            self._finalize_job_locked()
                        self._cond.notify_all()
                except Exception as exc:
                    logger.error(f"[MCP] Render failed for {page.filename}: {exc}", exc_info=True)
                    with self._cond:
                        page.rendered = True
                        page.failed = True
                        page.error = str(exc)
                        self._inflight_page_indices.discard(p_idx)
                        if page.ctx is not None:
                            try:
                                self.translator._cleanup_context_memory(page.ctx, keep_result=False)
                            except Exception:
                                pass
                        if page.batch_id and page.batch_id in self._batches:
                            batch = self._batches[page.batch_id]
                            if all(self.pages[i].rendered or self.pages[i].failed for i in batch.page_indices):
                                batch.status = "FAILED"
                                self._delete_batch_preview_files(batch)
                        if self._check_all_done_locked():
                            self._finalize_job_locked()
                        self._cond.notify_all()
                finally:
                    self._emit_progress()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            asyncio.set_event_loop(None)
            loop.close()

    # ------------------------------------------------------------------
    # Lease & tool operations
    # ------------------------------------------------------------------
    def _build_prev_context_locked(self, batch: McpBatchState) -> List[Dict[str, Any]]:
        if self.context_size <= 0:
            return []
        batch_orders = {self.pages[idx].source_order for idx in batch.page_indices}
        eligible_orders = [
            order
            for order in sorted(self._translation_history)
            if order not in batch_orders and self._translation_history.get(order)
        ]
        selected = eligible_orders[-self.context_size :]
        return [
            {"page_index": order, "items": self._translation_history[order]}
            for order in selected
        ]

    def _collect_glossary_delta_locked(self, worker_key: str) -> Tuple[List[Dict[str, Any]], int]:
        seen_ver = self._worker_glossary_seen.get(worker_key, 0)
        delta: List[Dict[str, Any]] = []
        for ver, terms in self._glossary_events:
            if ver > seen_ver:
                delta.extend(terms)
        self._worker_glossary_seen[worker_key] = self._glossary_version
        return delta, self._glossary_version

    def next_translation_batch(
        self,
        worker_id: Optional[str] = None,
        wait_timeout: float = 25.0,
    ) -> Tuple[BatchPayload, List[Tuple[bytes, str]]]:
        worker_key = str(worker_id or "_default")
        deadline = time.monotonic() + max(0.5, float(wait_timeout))

        selected_batch: Optional[McpBatchState] = None
        while True:
            with self._cond:
                if self.cancel_event.is_set():
                    return (
                        BatchPayload(
                            done=True,
                            job_id=self.job_id,
                            progress=self._progress_summary_locked(),
                            summary=self._build_completion_summary_locked(),
                        ),
                        [],
                    )

                self._reclaim_expired_leases_locked()

                leased_count = sum(1 for b in self._batches.values() if b.status == "LEASED")
                ready_batches = [
                    self._batches[bid]
                    for bid in self._batch_order
                    if self._batches[bid].status == "READY"
                ]

                if ready_batches and leased_count < self.max_parallel_batches:
                    selected_batch = ready_batches[0]
                    selected_batch.status = "LEASED"
                    selected_batch.worker_id = worker_key
                    selected_batch.leased_at = time.monotonic()
                    self._cond.notify_all()
                    break

                if self._all_batches_committed_or_done_locked():
                    # No more translation batches remain; wait for RenderWorker to drain & finalize.
                    break

                if self._preprocessing_done and not self._building_batch_pages and not ready_batches and leased_count > 0:
                    return (
                        BatchPayload(
                            done=False,
                            waiting=True,
                            waiting_reason="waiting_for_leased_batches",
                            job_id=self.job_id,
                            progress=self._progress_summary_locked(),
                        ),
                        [],
                    )

                if leased_count >= self.max_parallel_batches:
                    return (
                        BatchPayload(
                            done=False,
                            waiting=True,
                            waiting_reason="max_parallel_batches_leased",
                            job_id=self.job_id,
                            progress=self._progress_summary_locked(),
                        ),
                        [],
                    )

                # Preprocessing is still producing the next READY batch.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return (
                        BatchPayload(
                            done=False,
                            waiting=True,
                            waiting_reason="preprocessing_in_progress",
                            job_id=self.job_id,
                            progress=self._progress_summary_locked(),
                        ),
                        [],
                    )
                self._cond.wait(timeout=min(remaining, 0.5))

        if selected_batch is None:
            # Wait for GpuPriorityWorker & RenderWorker to finish saving final outputs.
            self._finished_event.wait(timeout=120.0)
            with self._cond:
                summary = self._build_completion_summary_locked()
                progress = self._progress_summary_locked()
            return (
                BatchPayload(
                    done=True,
                    job_id=self.job_id,
                    progress=progress,
                    summary=summary,
                ),
                [],
            )

        # Wait for CPU preview generation if HQ mode.
        encoded_pages: Dict[int, Tuple[bytes, str, int, int, str]] = {}
        if self.effective_mode == "hq":
            for p_idx in selected_batch.page_indices:
                page = self.pages[p_idx]
                page.preview_ready.wait(timeout=30.0)
                if page.preview_cache is None and not page.preview_error:
                    try:
                        page.preview_cache = render_numbered_page_preview(
                            page.ctx,
                            page.text_order,
                            max_size=self.preview_max_size,
                            fmt=self.preview_format,
                            quality=self.preview_quality,
                        )
                    except Exception as exc:
                        page.preview_error = str(exc)
                if page.preview_cache is not None:
                    raw_bytes, mime, pw, ph = page.preview_cache
                    encoded_pages[page.page_index] = (raw_bytes, mime, pw, ph, page.filename)

        delivery_map, hints = resolve_batch_image_delivery(
            encoded_pages,
            delivery=self.image_delivery,
            budget_mb=self.inline_image_budget_mb,
            preview_dir=self.preview_dir,
            batch_id=selected_batch.batch_id,
            fmt=self.preview_format,
        )

        inline_images: List[Tuple[bytes, str]] = []
        page_items: List[PageBatchItem] = []

        with self._cond:
            for info in delivery_map.values():
                if info.get("file_path"):
                    selected_batch.preview_files.append(info["file_path"])

            for p_idx in selected_batch.page_indices:
                page = self.pages[p_idx]
                regions, _order, _low = build_page_region_items(page.ctx, page.text_order[0] if page.text_order else 1)
                d_info = delivery_map.get(page.page_index)
                if d_info is not None:
                    mode = d_info["mode"]
                    preview_path = d_info.get("file_path")
                    preview_mime = d_info.get("mime_type")
                    if mode == "mcp_image" and d_info.get("bytes") is not None:
                        inline_images.append((d_info["bytes"], preview_mime))
                else:
                    mode = "none"
                    preview_path = None
                    preview_mime = None

                page_items.append(
                    PageBatchItem(
                        page_index=page.page_index,
                        filename=page.filename,
                        source_path=page.image_path,
                        width=page.width,
                        height=page.height,
                        regions=regions,
                        low_confidence_ids=list(page.low_confidence_ids),
                        image_delivery_mode=mode,
                        preview_path=preview_path,
                        preview_mime=preview_mime,
                    )
                )

            prev_context = self._build_prev_context_locked(selected_batch)
            glossary_delta, glossary_ver = self._collect_glossary_delta_locked(worker_key)
            progress = self._progress_summary_locked()

        if self.effective_mode == "text_on_demand":
            hints.append(
                "Text-only batch with on-demand vision enabled: inspect low_confidence_ids or "
                "ambiguous lines via get_page_preview(job_id, page_index, region_ids=...)."
            )
        elif self.effective_mode == "text_only":
            hints.append("Strict text-only mode: get_page_preview is disabled.")

        return (
            BatchPayload(
                done=False,
                waiting=False,
                job_id=self.job_id,
                batch_id=selected_batch.batch_id,
                lease_expires_in=max(1, int(round(self.lease_timeout_seconds))),
                progress=progress,
                prev_context=prev_context,
                glossary_delta=glossary_delta,
                glossary_version=glossary_ver,
                pages=page_items,
                hints=hints,
            ),
            inline_images,
        )

    def get_page_preview(
        self,
        page_index: int,
        *,
        region_ids: Optional[List[int]] = None,
        high_res: bool = False,
    ) -> Tuple[PagePreviewResponse, Optional[bytes]]:
        if self.effective_mode == "text_only":
            raise PermissionError(
                "On-demand page preview is disabled in strict text-only mode "
                "(translation_mode='text', allow_on_demand_image=False)."
            )

        with self._cond:
            if page_index < 0 or page_index >= len(self.pages):
                raise ValueError(f"Invalid page_index={page_index}; valid range is 0..{len(self.pages) - 1}")
            page = self.pages[page_index]
            if not page.preprocessed or page.ctx is None:
                raise RuntimeError(f"Page {page_index} has not been preprocessed yet")
            if page.rendered:
                raise RuntimeError(f"Page {page_index} has already been rendered and its buffers released")

            self.on_demand_image_requests += 1
            lease_renewed = False
            if page.batch_id and page.batch_id in self._batches:
                batch = self._batches[page.batch_id]
                if batch.status == "LEASED":
                    batch.leased_at = time.monotonic()
                    lease_renewed = True
            ctx = page.ctx
            text_order = list(page.text_order)
            filename = page.filename
            batch_id = page.batch_id or "ondemand"

        max_size = max(self.preview_max_size, 1600) if high_res else self.preview_max_size
        quality = max(self.preview_quality, 90) if high_res else self.preview_quality

        if region_ids:
            raw_bytes, mime, pw, ph = render_cropped_region_preview(
                ctx,
                text_order,
                region_ids,
                max_size=max_size,
                fmt=self.preview_format,
                quality=quality,
            )
            stem = f"{batch_id}_p{page_index}_crop_{'_'.join(str(x) for x in region_ids[:6])}"
        else:
            raw_bytes, mime, pw, ph = render_numbered_page_preview(
                ctx,
                text_order,
                max_size=max_size,
                fmt=self.preview_format,
                quality=quality,
            )
            stem = f"{batch_id}_p{page_index}_full"

        budget_bytes = max(1, int(self.inline_image_budget_mb * 1024 * 1024))
        use_file_path = self.image_delivery == "file_path" or (
            self.image_delivery == "auto" and len(raw_bytes) > budget_bytes
        )

        self._emit_progress()

        if use_file_path:
            file_path = write_preview_bytes_to_disk(raw_bytes, self.preview_dir, stem, self.preview_format)
            with self._cond:
                if batch_id in self._batches:
                    self._batches[batch_id].preview_files.append(file_path)
            return (
                PagePreviewResponse(
                    job_id=self.job_id,
                    page_index=page_index,
                    filename=filename,
                    region_ids=region_ids,
                    high_res=bool(high_res),
                    delivery_mode="file_path",
                    file_path=file_path,
                    mime_type=mime,
                    width=pw,
                    height=ph,
                    lease_renewed=lease_renewed,
                    note="Preview written to file_path; inspect file_path using your local file/image viewing tool.",
                ),
                None,
            )

        return (
            PagePreviewResponse(
                job_id=self.job_id,
                page_index=page_index,
                filename=filename,
                region_ids=region_ids,
                high_res=bool(high_res),
                delivery_mode="mcp_image",
                file_path=None,
                mime_type=mime,
                width=pw,
                height=ph,
                lease_renewed=lease_renewed,
                note="Inline MCP image attached.",
            ),
            raw_bytes,
        )

    def submit_translation_batch(
        self,
        batch_id: str,
        translations: Sequence[TranslationSubmissionItem | Dict[str, Any]],
        *,
        new_terms: Optional[List[Dict[str, Any]]] = None,
        return_next: bool = True,
    ) -> Tuple[SubmitBatchResponse, List[Tuple[bytes, str]]]:
        with self._cond:
            batch = self._batches.get(batch_id)
            if batch is None:
                return (
                    SubmitBatchResponse(
                        status="error",
                        job_id=self.job_id,
                        batch_id=batch_id,
                        message=f"Unknown batch_id '{batch_id}'.",
                    ),
                    [],
                )
            if batch.status in ("COMMITTED", "DONE", "FAILED"):
                return (
                    SubmitBatchResponse(
                        status="error",
                        job_id=self.job_id,
                        batch_id=batch_id,
                        message=f"Batch '{batch_id}' is already {batch.status}.",
                    ),
                    [],
                )
            if batch.status != "LEASED":
                return (
                    SubmitBatchResponse(
                        status="error",
                        job_id=self.job_id,
                        batch_id=batch_id,
                        message=(
                            f"Batch '{batch_id}' is in status '{batch.status}' (lease expired or released). "
                            "Call next_translation_batch to acquire a new lease."
                        ),
                    ),
                    [],
                )

            if batch.submitting:
                return (
                    SubmitBatchResponse(
                        status="error",
                        job_id=self.job_id,
                        batch_id=batch_id,
                        message=f"Batch '{batch_id}' is already being submitted.",
                    ),
                    [],
                )

            batch.submitting = True
            pages_in_batch = [self.pages[i] for i in batch.page_indices]
            retry_count = batch.retry_count
            worker_id = batch.worker_id

        try:
            with self._cond:
                # Wait briefly if speculative P2 inpaint is currently active on any page in this batch.
                while any(self.pages[i].inpaint_in_progress for i in batch.page_indices):
                    if self.cancel_event.is_set():
                        raise RuntimeError("Job cancelled while submitting batch")
                    self._cond.wait(timeout=0.1)

            is_valid, errors, cleaned_map = _run_coro_sync(
                validate_batch_translations(
                    self.translator,
                    self.config,
                    pages_in_batch,
                    translations,
                    retry_count=retry_count,
                )
            )
            if not is_valid:
                with self._cond:
                    batch.retry_count += 1
                    if batch.status == "LEASED":
                        batch.leased_at = time.monotonic()
                return (
                    SubmitBatchResponse(
                        status="retry_needed",
                        job_id=self.job_id,
                        batch_id=batch_id,
                        errors=errors,
                        message="Validation failed; fix only the reported region IDs and resubmit.",
                    ),
                    [],
                )

            redo_pages, glossary_modified = _run_coro_sync(
                apply_batch_post_processing(
                    self.translator,
                    self.config,
                    pages_in_batch,
                    cleaned_map,
                    new_terms=new_terms,
                    prompt_file_path=self.prompt_file_path,
                )
            )
            redo_set = set(redo_pages)

            with self._cond:
                for page in pages_in_batch:
                    page.committed = True
                    page.preview_cache = None  # Free preview bytes immediately after commit.
                    if getattr(page.ctx, "text_regions", None):
                        entries = self.translator._build_page_context_entries(page.ctx)
                        if entries:
                            self._translation_history[page.source_order] = entries

                    if page.page_index in redo_set:
                        page.pending_redo = True
                        page.inpaint_done = False
                        page.render_queued = False
                    elif page.inpaint_done and not page.render_queued and not page.failed:
                        page.render_queued = True
                        self._render_queue.append(page.page_index)

                merged_terms_count = 0
                if new_terms and glossary_modified:
                    merged_terms_count = len(new_terms)
                    self._glossary_version += 1
                    self._glossary_events.append((self._glossary_version, list(new_terms)))
                    if worker_id:
                        self._worker_glossary_seen[worker_id] = self._glossary_version

                batch.status = "COMMITTED"
                batch.leased_at = None
                self._cond.notify_all()
        finally:
            with self._cond:
                batch.submitting = False
                self._cond.notify_all()

        self._emit_progress()

        if not return_next:
            return (
                SubmitBatchResponse(
                    status="accepted",
                    job_id=self.job_id,
                    batch_id=batch_id,
                    merged_terms_count=merged_terms_count,
                    redo_pages=redo_pages,
                    done=False,
                    message="Batch accepted and queued for background inpainting/rendering.",
                ),
                [],
            )

        next_payload, inline_images = self.next_translation_batch(worker_id=worker_id)
        return (
            SubmitBatchResponse(
                status="accepted",
                job_id=self.job_id,
                batch_id=batch_id,
                merged_terms_count=merged_terms_count,
                redo_pages=redo_pages,
                next_batch=next_payload,
                done=next_payload.done,
                summary=next_payload.summary,
                message=(
                    "All batches completed and saved."
                    if next_payload.done
                    else "Batch accepted; next batch attached."
                ),
            ),
            inline_images,
        )

    def release_batch(self, batch_id: str) -> Dict[str, Any]:
        with self._cond:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise ValueError(f"Unknown batch_id '{batch_id}'")
            if batch.status != "LEASED":
                return {
                    "released": False,
                    "batch_id": batch_id,
                    "status": batch.status,
                    "message": f"Batch '{batch_id}' is not leased (status={batch.status}).",
                }
            batch.status = "READY"
            batch.worker_id = None
            batch.leased_at = None
            self._cond.notify_all()
            return {
                "released": True,
                "batch_id": batch_id,
                "status": batch.status,
                "message": f"Batch '{batch_id}' returned to READY queue.",
            }

    def get_status_dict(self) -> Dict[str, Any]:
        with self._cond:
            summary = self._progress_summary_locked()
            ready_count = sum(1 for b in self._batches.values() if b.status == "READY")
            leased_list = [
                {
                    "batch_id": b.batch_id,
                    "worker_id": b.worker_id,
                    "age_seconds": round(time.monotonic() - b.leased_at, 1) if b.leased_at else 0.0,
                    "page_indices": list(b.page_indices),
                }
                for b in self._batches.values()
                if b.status == "LEASED"
            ]
            redo_count = sum(1 for p in self.pages if p.pending_redo)
            render_q = len(self._render_queue)
            failed_details = [
                {"page_index": str(p.page_index), "filename": p.filename, "error": p.error or ""}
                for p in self.pages
                if p.failed
            ]
            return JobStatusResponse(
                job_id=self.job_id,
                status=self.status,
                effective_mode=self.effective_mode,
                output_dir=self.save_info.get("output_folder", ""),
                total_pages=summary.total_pages,
                pending_pages=summary.pending_pages,
                skipped_pages=summary.skipped_pages,
                preprocessed_pages=summary.preprocessed_pages,
                committed_pages=summary.committed_pages,
                rendered_pages=summary.rendered_pages,
                failed_pages=summary.failed_pages,
                textless_pages=summary.textless_pages,
                on_demand_image_requests=summary.on_demand_image_requests,
                queues={
                    "ready_batches": ready_count,
                    "leased_batches": len(leased_list),
                    "pending_redo": redo_count,
                    "render_queue": render_q,
                },
                leases=leased_list,
                failed_details=failed_details,
                output_paths=list(self.output_paths),
                error=self.error,
            ).model_dump()

    def cancel(self, join_timeout: float = 5.0) -> Dict[str, Any]:
        self.cancel_event.set()
        with self._cond:
            self._cond.notify_all()
        if self._gpu_thread and self._gpu_thread.is_alive():
            self._gpu_thread.join(timeout=join_timeout)
        if self._render_thread and self._render_thread.is_alive():
            self._render_thread.join(timeout=join_timeout)
        if self._preview_pool is not None:
            self._preview_pool.shutdown(wait=False, cancel_futures=True)
        with self._cond:
            for p in self.pages:
                if p.ctx is not None:
                    try:
                        self.translator._cleanup_context_memory(p.ctx, keep_result=False)
                    except Exception:
                        pass
                p.preview_cache = None
                p.preview_ready.set()
            self._finalize_job_locked()
        self._emit_progress()
        return self.get_status_dict()


class McpJobManager:
    """Process-wide manager owning MCP jobs, GPU mutual exclusion, and idle TTL cleanup."""

    def __init__(
        self,
        *,
        root_dir: str,
        get_config: Callable[[], Any],
        get_gui_snapshot: Optional[Callable[[], Dict[str, Any]]] = None,
        is_gui_busy: Optional[Callable[[], bool]] = None,
        file_expander: Optional[Callable[..., Any]] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_job_finished: Optional[Callable[[Dict[str, Any]], None]] = None,
        translator_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        self.root_dir = root_dir
        self._get_config = get_config
        self._get_gui_snapshot = get_gui_snapshot
        self._is_gui_busy = is_gui_busy
        self._file_expander = file_expander
        self._on_progress = on_progress
        self._on_job_finished = on_job_finished
        self._translator_factory = translator_factory or create_mcp_translator

        self._lock = threading.RLock()
        self._jobs: Dict[str, McpTranslationJob] = {}
        self._active_job_id: Optional[str] = None
        self._latest_job_id: Optional[str] = None
        self._idle_timer: Optional[threading.Timer] = None

    def _cancel_idle_timer_locked(self) -> None:
        if self._idle_timer is not None:
            try:
                self._idle_timer.cancel()
            except Exception:
                pass
            self._idle_timer = None

    def _schedule_idle_cleanup_locked(self) -> None:
        self._cancel_idle_timer_locked()
        cfg = self._get_config()
        cfg_dict = cfg.model_dump() if hasattr(cfg, "model_dump") else dict(cfg or {})
        mcp_cfg = cfg_dict.get("mcp") or {}
        app_cfg = cfg_dict.get("app") or {}
        ttl = int(mcp_cfg.get("models_idle_ttl", 300) or 0)
        if ttl > 0:
            timer = threading.Timer(ttl, self._idle_timer_fired)
            timer.daemon = True
            timer.start()
            self._idle_timer = timer
        elif bool(app_cfg.get("unload_models_after_translation", True)):
            threading.Thread(target=self._safe_unload_if_idle, name="mcp-idle-unload", daemon=True).start()

    def _idle_timer_fired(self) -> None:
        self._safe_unload_if_idle()

    def _safe_unload_if_idle(self) -> None:
        with self._lock:
            if self.has_active_job() or (self._is_gui_busy and self._is_gui_busy()):
                return
        try:
            self.unload_models()
        except Exception:
            logger.debug("MCP idle model unload skipped/failed", exc_info=True)

    def has_active_job(self) -> bool:
        with self._lock:
            if not self._active_job_id:
                return False
            job = self._jobs.get(self._active_job_id)
            if job is None:
                self._active_job_id = None
                return False
            if job.status in ("running",):
                return True
            self._active_job_id = None
            return False

    def _get_job(self, job_id: Optional[str] = None) -> McpTranslationJob:
        with self._lock:
            target_id = job_id or self._active_job_id or self._latest_job_id
            if not target_id or target_id not in self._jobs:
                raise KeyError(f"Translation job not found: {job_id or '<active>'}")
            return self._jobs[target_id]

    def _handle_job_finished(self, job: McpTranslationJob) -> None:
        with self._lock:
            if self._active_job_id == job.job_id:
                self._active_job_id = None
            self._schedule_idle_cleanup_locked()
            summary = job.get_status_dict()
            summary["completed_output_sources"] = dict(job.completed_output_sources)

        if self._on_job_finished is not None:
            try:
                self._on_job_finished(summary)
            except Exception:
                logger.debug("MCP job finished callback failed", exc_info=True)

    def get_workspace_info(self) -> WorkspaceInfoResponse:
        cfg = self._get_config()
        cfg_dict = cfg.model_dump() if hasattr(cfg, "model_dump") else dict(cfg or {})
        app_cfg = cfg_dict.get("app") or {}
        trans_cfg = cfg_dict.get("translator") or {}
        render_cfg = cfg_dict.get("render") or {}
        cli_cfg = cfg_dict.get("cli") or {}
        mcp_cfg = cfg_dict.get("mcp") or {}

        mode = str(mcp_cfg.get("translation_mode", "hq") or "hq")
        allow_on_demand = bool(mcp_cfg.get("allow_on_demand_image", True))
        if mode == "hq":
            effective: EffectiveMode = "hq"
        elif allow_on_demand:
            effective = "text_on_demand"
        else:
            effective = "text_only"

        gui_snap = self._get_gui_snapshot() if self._get_gui_snapshot else {}
        gui_files = list(gui_snap.get("source_files") or ())
        output_dir = str(gui_snap.get("output_dir") or app_cfg.get("last_output_path") or "")

        conflicts = find_api_backend_conflicts(cfg_dict)
        conflict_warning = None
        if conflicts:
            conflict_warning = "; ".join(c.message for c in conflicts)

        gui_busy = bool(self._is_gui_busy and self._is_gui_busy())
        mcp_busy = self.has_active_job()
        busy = gui_busy or mcp_busy
        busy_reason = None
        if gui_busy:
            busy_reason = "GUI translation task is currently running."
        elif mcp_busy:
            busy_reason = f"MCP job {self._active_job_id} is currently running."

        return WorkspaceInfoResponse(
            preset=str(app_cfg.get("current_preset") or "默认"),
            target_lang=str(trans_cfg.get("target_lang") or "CHS"),
            translation_mode="hq" if mode == "hq" else "text",
            allow_on_demand_image=allow_on_demand,
            effective_mode=effective,
            image_delivery=str(mcp_cfg.get("image_delivery", "auto") or "auto"),
            inline_image_budget_mb=float(mcp_cfg.get("inline_image_budget_mb", 2.0) or 2.0),
            preview_max_size=int(mcp_cfg.get("preview_max_size", 1024) or 1024),
            preview_format=str(mcp_cfg.get("preview_format", "jpeg") or "jpeg"),
            preview_quality=int(mcp_cfg.get("preview_quality", 82) or 82),
            max_parallel_batches=int(mcp_cfg.get("max_parallel_batches", 1) or 1),
            batch_size=int(cli_cfg.get("batch_size", 3) or 3),
            context_size=int(cli_cfg.get("context_size", 3) or 3),
            ai_line_break_enabled=bool(render_cfg.get("disable_auto_wrap", False)),
            check_br_and_retry=bool(render_cfg.get("check_br_and_retry", False)),
            extract_glossary=bool(trans_cfg.get("extract_glossary", False)),
            high_quality_prompt_path=trans_cfg.get("high_quality_prompt_path"),
            gui_file_count=len(gui_files),
            gui_file_samples=[os.path.basename(p) for p in gui_files[:10]],
            output_dir=output_dir,
            use_gui_files_by_default=bool(mcp_cfg.get("use_gui_files_by_default", True)),
            busy=busy,
            busy_reason=busy_reason,
            active_job_id=self._active_job_id,
            api_conflicts=conflicts,
            api_conflict_warning=conflict_warning,
        )

    def create_translation_job(
        self,
        *,
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
    ) -> CreateJobResponse:
        with self._lock:
            if self._is_gui_busy and self._is_gui_busy():
                raise RuntimeError("A GUI translation task is currently running. Wait for it to finish or stop it first.")
            if self.has_active_job():
                raise RuntimeError(f"MCP translation job '{self._active_job_id}' is already running.")
            self._cancel_idle_timer_locked()

        cfg = self._get_config()
        cfg_dict = cfg.model_dump() if hasattr(cfg, "model_dump") else dict(cfg or {})
        conflicts = find_api_backend_conflicts(cfg_dict)
        if conflicts:
            details = "\n".join(f"- {c.section}.{c.setting} = {c.value}" for c in conflicts)
            raise RuntimeError(
                "0-API policy violation: switch the following settings to local models before starting an MCP job:\n"
                + details
            )

        mcp_cfg = dict(cfg_dict.get("mcp") or {})
        mode = translation_mode or str(mcp_cfg.get("translation_mode", "hq") or "hq")
        allow_on_demand = (
            bool(allow_on_demand_image)
            if allow_on_demand_image is not None
            else bool(mcp_cfg.get("allow_on_demand_image", True))
        )
        if mode == "hq":
            effective_mode: EffectiveMode = "hq"
        elif allow_on_demand:
            effective_mode = "text_on_demand"
        else:
            effective_mode = "text_only"

        gui_snap = self._get_gui_snapshot() if self._get_gui_snapshot else {}
        use_gui = (
            bool(use_gui_files)
            if use_gui_files is not None
            else bool(mcp_cfg.get("use_gui_files_by_default", True))
        )

        if inputs:
            raw_sources = [str(p) for p in inputs if str(p).strip()]
            excluded_subfolders: Sequence[str] = ()
            excluded_files: Sequence[str] = ()
        elif use_gui:
            raw_sources = list(gui_snap.get("source_files") or ())
            excluded_subfolders = list(gui_snap.get("excluded_subfolders") or ())
            excluded_files = list(gui_snap.get("excluded_files") or ())
        else:
            raw_sources = []
            excluded_subfolders = ()
            excluded_files = ()

        if not raw_sources:
            raise ValueError("No input files provided and GUI file list is empty.")

        resolved_output_dir = str(
            output_dir
            if output_dir is not None and str(output_dir).strip()
            else (gui_snap.get("output_dir") or (cfg_dict.get("app") or {}).get("last_output_path") or "")
        ).strip()
        save_to_source_dir = bool((cfg_dict.get("cli") or {}).get("save_to_source_dir", False))
        if not save_to_source_dir:
            if not resolved_output_dir:
                raise ValueError("Output directory is empty. Provide output_dir or set Output Directory in GUI.")
            os.makedirs(resolved_output_dir, exist_ok=True)

        engine_config, translator_params = build_mcp_engine_config(
            cfg_dict,
            self.root_dir,
            target_lang=target_lang,
            overwrite=overwrite,
            batch_size=batch_size,
            context_size=context_size,
        )

        overwrite_flag = bool(engine_config.cli.overwrite)
        if self._file_expander is not None:
            resolved_files, file_to_folder_map, archive_to_temp_map = self._file_expander(
                raw_sources,
                excluded_subfolders,
                excluded_files,
                resolved_output_dir,
                overwrite_flag,
            )
        else:
            resolved_files, file_to_folder_map, archive_to_temp_map = _default_expand_inputs(
                raw_sources,
                excluded_subfolders,
                excluded_files,
                resolved_output_dir,
                overwrite_flag,
            )

        if not resolved_files:
            raise ValueError("No supported image files found in the provided inputs.")

        input_folders = {
            os.path.normpath(folder)
            for folder in file_to_folder_map.values()
            if folder
        }
        output_format = getattr(engine_config.cli, "format", None)
        if not output_format or output_format == "不指定":
            output_format = None

        save_info = {
            "output_folder": resolved_output_dir,
            "format": output_format,
            "overwrite": overwrite_flag,
            "input_folders": input_folders,
            "save_to_source_dir": save_to_source_dir,
        }

        translator = self._translator_factory(translator_params)
        guidelines, glossary, resolved_prompt_path = _run_coro_sync(
            load_job_guidelines_and_glossary(translator, engine_config, self.root_dir)
        )

        from manga_translator.utils.batch_skip import plan_batch_inputs

        items = [(fpath, engine_config) for fpath in resolved_files]
        plan = plan_batch_inputs(translator, items, save_info)

        job_id = uuid.uuid4().hex[:12]
        preview_base = resolved_output_dir or os.path.dirname(resolved_files[0])
        preview_dir = os.path.join(preview_base, "manga_translator_work", "mcp_preview", job_id)

        job = McpTranslationJob(
            job_id=job_id,
            config=engine_config,
            translator=translator,
            mcp_settings=mcp_cfg,
            save_info=save_info,
            plan=plan,
            archive_to_temp_map=archive_to_temp_map,
            preview_dir=preview_dir,
            guidelines=guidelines,
            glossary=glossary,
            prompt_file_path=resolved_prompt_path,
            effective_mode=effective_mode,
            retranslate_json=bool(retranslate_json),
            on_progress=self._on_progress,
            on_finished=self._handle_job_finished,
        )

        with self._lock:
            self._jobs[job_id] = job
            self._latest_job_id = job_id
            if job.pages:
                self._active_job_id = job_id

        if job.pages:
            job.start()

        return CreateJobResponse(
            job_id=job_id,
            status=job.status,
            total_pages=job.total_pages,
            pending_pages=job.pending_pages_count,
            skipped_pages=job.skipped_pages,
            effective_mode=effective_mode,
            image_delivery=job.image_delivery,
            max_parallel_batches=job.max_parallel_batches,
            batch_size=job.batch_size,
            context_size=job.context_size,
            target_lang=engine_config.translator.target_lang,
            ai_line_break_enabled=bool(engine_config.render.disable_auto_wrap),
            extract_glossary=bool(engine_config.translator.extract_glossary),
            guidelines=guidelines,
            glossary=glossary,
            glossary_version=0,
            output_dir=resolved_output_dir,
        )

    def next_translation_batch(
        self,
        job_id: str,
        worker_id: Optional[str] = None,
    ) -> Tuple[BatchPayload, List[Tuple[bytes, str]]]:
        job = self._get_job(job_id)
        return job.next_translation_batch(worker_id=worker_id)

    def get_page_preview(
        self,
        job_id: str,
        page_index: int,
        *,
        region_ids: Optional[List[int]] = None,
        high_res: bool = False,
    ) -> Tuple[PagePreviewResponse, Optional[bytes]]:
        job = self._get_job(job_id)
        return job.get_page_preview(page_index, region_ids=region_ids, high_res=high_res)

    def submit_translation_batch(
        self,
        job_id: str,
        batch_id: str,
        translations: Sequence[TranslationSubmissionItem | Dict[str, Any]],
        *,
        new_terms: Optional[List[Dict[str, Any]]] = None,
        return_next: bool = True,
    ) -> Tuple[SubmitBatchResponse, List[Tuple[bytes, str]]]:
        job = self._get_job(job_id)
        return job.submit_translation_batch(
            batch_id,
            translations,
            new_terms=new_terms,
            return_next=return_next,
        )

    def release_batch(self, job_id: str, batch_id: str) -> Dict[str, Any]:
        job = self._get_job(job_id)
        return job.release_batch(batch_id)

    def get_job_status(self, job_id: Optional[str] = None) -> Dict[str, Any]:
        job = self._get_job(job_id)
        return job.get_status_dict()

    def cancel_job(self, job_id: Optional[str] = None) -> Dict[str, Any]:
        job = self._get_job(job_id)
        status = job.cancel()
        with self._lock:
            if self._active_job_id == job.job_id:
                self._active_job_id = None
            self._schedule_idle_cleanup_locked()
        return status

    def unload_models(self) -> Dict[str, Any]:
        with self._lock:
            if self.has_active_job():
                raise RuntimeError("Cannot unload GPU models while an MCP translation job is running.")
            if self._is_gui_busy and self._is_gui_busy():
                raise RuntimeError("Cannot unload GPU models while a GUI translation task is running.")
            self._cancel_idle_timer_locked()

        from desktop_qt_ui.utils.memory_cleanup import full_memory_cleanup

        return _run_coro_sync(full_memory_cleanup(unload_models=True))

    def shutdown(self) -> None:
        with self._lock:
            self._cancel_idle_timer_locked()
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.status == "running":
                try:
                    job.cancel(join_timeout=3.0)
                except Exception:
                    logger.debug("Error cancelling job during shutdown", exc_info=True)
            else:
                try:
                    job._cleanup_temp_artifacts()
                except Exception:
                    pass
