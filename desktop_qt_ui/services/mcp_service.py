"""Qt service bridging ConfigService, MainAppLogic, LogService, McpJobManager, and McpHttpServer."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from manga_translator.mcp import McpHttpServer, McpJobManager, find_api_backend_conflicts
from services.log_service import subscribe_log_records, unsubscribe_log_records

logger = logging.getLogger("manga_translator.mcp.service")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass
        raise


class McpService(QObject):
    """Desktop lifecycle & configuration facade for the embedded Streamable HTTP MCP server."""

    server_state_changed = pyqtSignal(bool, str, str)
    job_progress_changed = pyqtSignal(dict)
    job_finished = pyqtSignal(dict)
    log_record_received = pyqtSignal(dict)
    api_conflicts_changed = pyqtSignal(list)

    def __init__(
        self,
        *,
        root_dir: str,
        config_service: Any = None,
        file_service: Any = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.root_dir = root_dir
        self._config_service = config_service
        self._file_service = file_service
        self._app_logic: Any = None
        self._shutdown_done = False
        self._lock = threading.RLock()

        self.job_manager = McpJobManager(
            root_dir=root_dir,
            get_config=self._get_config,
            get_gui_snapshot=self._get_gui_snapshot,
            is_gui_busy=self._is_gui_busy,
            file_expander=self._expand_inputs,
            on_progress=self._on_job_progress,
            on_job_finished=self._on_job_finished,
        )
        self.http_server = McpHttpServer(self.job_manager)

        subscribe_log_records(self._on_log_entry)
        if self._config_service is not None and hasattr(self._config_service, "config_changed"):
            try:
                self._config_service.config_changed.connect(self._on_config_changed)
            except Exception:
                pass

    def _get_config(self) -> Any:
        if self._config_service is not None:
            return self._config_service.get_config()
        from core.config_models import AppSettings

        return AppSettings()

    def _get_gui_snapshot(self) -> Dict[str, Any]:
        if self._app_logic is not None and hasattr(self._app_logic, "get_gui_state_snapshot"):
            return self._app_logic.get_gui_state_snapshot()
        cfg = self._get_config()
        return {
            "source_files": (),
            "excluded_subfolders": frozenset(),
            "excluded_files": frozenset(),
            "output_dir": str(getattr(getattr(cfg, "app", None), "last_output_path", "") or ""),
        }

    def _is_gui_busy(self) -> bool:
        if self._app_logic is not None and hasattr(self._app_logic, "is_gui_task_active"):
            return bool(self._app_logic.is_gui_task_active())
        return False

    def _expand_inputs(
        self,
        source_files,
        excluded_subfolders,
        excluded_files,
        output_base_dir: str,
        overwrite_extract: bool,
    ):
        if self._app_logic is not None and hasattr(self._app_logic, "expand_inputs_for_mcp"):
            return self._app_logic.expand_inputs_for_mcp(
                source_files,
                excluded_subfolders,
                excluded_files,
                output_base_dir,
                overwrite_extract,
            )
        from manga_translator.mcp.job_manager import _default_expand_inputs

        return _default_expand_inputs(
            source_files,
            excluded_subfolders,
            excluded_files,
            output_base_dir,
            overwrite_extract,
        )

    def attach_app_logic(self, app_logic: Any) -> None:
        self._app_logic = app_logic

    def _on_log_entry(self, entry: Dict[str, Any]) -> None:
        if self._shutdown_done:
            return
        try:
            self.log_record_received.emit(entry)
        except RuntimeError:
            pass

    def _on_config_changed(self, config_dict: dict) -> None:
        conflicts = [c.model_dump() for c in find_api_backend_conflicts(config_dict)]
        try:
            self.api_conflicts_changed.emit(conflicts)
        except RuntimeError:
            pass

    def _on_job_progress(self, status_dict: Dict[str, Any]) -> None:
        if self._shutdown_done:
            return
        try:
            self.job_progress_changed.emit(status_dict)
        except RuntimeError:
            pass

    def _on_job_finished(self, summary: Dict[str, Any]) -> None:
        if self._shutdown_done:
            return
        try:
            self.job_progress_changed.emit(summary)
            self.job_finished.emit(summary)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Server lifecycle & actions
    # ------------------------------------------------------------------
    def _configured_host_port(self) -> tuple[str, int]:
        cfg = self._get_config()
        mcp_cfg = getattr(cfg, "mcp", None)
        host = str(getattr(mcp_cfg, "host", "127.0.0.1") or "127.0.0.1")
        port = int(getattr(mcp_cfg, "port", 8765) or 8765)
        return host, port

    def endpoint_url(self) -> str:
        if self.http_server.is_running:
            return self.http_server.endpoint_url
        host, port = self._configured_host_port()
        return f"http://{host}:{port}/mcp"

    def is_server_running(self) -> bool:
        return self.http_server.is_running

    def start_server(self, port: Optional[int] = None) -> tuple[bool, str, str]:
        host, cfg_port = self._configured_host_port()
        target_port = int(port) if port is not None else cfg_port
        try:
            url = self.http_server.start(host=host, port=target_port)
            self.server_state_changed.emit(True, url, "")
            return True, url, ""
        except Exception as exc:
            err = str(exc)
            logger.error(f"Failed to start MCP server: {err}")
            url = f"http://{host}:{target_port}/mcp"
            self.server_state_changed.emit(False, url, err)
            return False, url, err

    def stop_server(self) -> tuple[bool, str, str]:
        try:
            self.http_server.stop()
            url = self.endpoint_url()
            self.server_state_changed.emit(False, url, "")
            return True, url, ""
        except Exception as exc:
            err = str(exc)
            logger.error(f"Failed to stop MCP server: {err}")
            url = self.endpoint_url()
            self.server_state_changed.emit(self.is_server_running(), url, err)
            return False, url, err

    def maybe_auto_start(self) -> None:
        cfg = self._get_config()
        mcp_cfg = getattr(cfg, "mcp", None)
        if bool(getattr(mcp_cfg, "auto_start", False)) and not self.is_server_running():
            self.start_server()

    def has_active_job(self) -> bool:
        return self.job_manager.has_active_job()

    def get_current_job_status(self) -> Optional[Dict[str, Any]]:
        try:
            return self.job_manager.get_job_status()
        except KeyError:
            return None

    def cancel_active_job(self) -> Optional[Dict[str, Any]]:
        if not self.has_active_job():
            return None
        return self.job_manager.cancel_job()

    def unload_models_async(
        self,
        on_done: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        def _worker() -> None:
            try:
                result = self.job_manager.unload_models()
                if callable(on_done):
                    on_done(result)
            except Exception as exc:
                if callable(on_error):
                    on_error(str(exc))

        threading.Thread(target=_worker, name="mcp-unload-models", daemon=True).start()

    def get_api_backend_conflicts(self) -> List[Dict[str, str]]:
        cfg = self._get_config()
        return [c.model_dump() for c in find_api_backend_conflicts(cfg)]

    # ------------------------------------------------------------------
    # Antigravity integration helpers
    # ------------------------------------------------------------------
    def get_mcp_client_config_json(self, port: Optional[int] = None) -> str:
        host, cfg_port = self._configured_host_port()
        resolved_port = int(port) if port is not None else cfg_port
        payload = {
            "mcpServers": {
                "manga-translator": {
                    "url": f"http://{host}:{resolved_port}/mcp",
                }
            }
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def install_antigravity_mcp_config(
        self,
        config_path: Optional[str | Path] = None,
        *,
        port: Optional[int] = None,
    ) -> str:
        target = (
            Path(config_path)
            if config_path is not None
            else Path.home() / ".gemini" / "config" / "mcp_config.json"
        )
        host, cfg_port = self._configured_host_port()
        resolved_port = int(port) if port is not None else cfg_port

        existing_data: Dict[str, Any] = {}
        if target.exists():
            raw_text = target.read_text(encoding="utf-8").strip()
            if raw_text:
                parsed = json.loads(raw_text)
                if not isinstance(parsed, dict):
                    raise ValueError(f"Expected JSON object in {target}")
                existing_data = parsed

        servers = existing_data.get("mcpServers")
        if servers is None:
            servers = {}
        elif not isinstance(servers, dict):
            raise ValueError(f"'mcpServers' in {target} must be a JSON object")

        servers = dict(servers)
        servers["manga-translator"] = {"url": f"http://{host}:{resolved_port}/mcp"}
        existing_data["mcpServers"] = servers

        _atomic_write_text(target, json.dumps(existing_data, indent=2, ensure_ascii=False) + "\n")
        logger.info(f"[MCP] Updated Antigravity MCP config at {target}")
        return str(target)

    def install_antigravity_skill(self, skill_path: Optional[str | Path] = None) -> str:
        target = (
            Path(skill_path)
            if skill_path is not None
            else Path.home() / ".gemini" / "config" / "skills" / "manga-translate" / "SKILL.md"
        )
        template_path = Path(self.root_dir) / "manga_translator" / "mcp" / "antigravity_skill_template.md"
        if not template_path.exists():
            import manga_translator.mcp as mcp_pkg

            template_path = Path(mcp_pkg.__file__).resolve().parent / "antigravity_skill_template.md"
        content = template_path.read_text(encoding="utf-8")
        _atomic_write_text(target, content)
        logger.info(f"[MCP] Installed Antigravity skill at {target}")
        return str(target)

    def get_log_directory(self) -> str:
        for handler in reversed(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler):
                path = str(getattr(handler, "baseFilename", "") or "").strip()
                if path:
                    return os.path.dirname(os.path.normpath(os.path.abspath(path)))
        return os.path.normpath(os.path.abspath(os.path.join(self.root_dir, "result")))

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        unsubscribe_log_records(self._on_log_entry)
        try:
            self.job_manager.shutdown()
        except Exception:
            logger.debug("Error shutting down McpJobManager", exc_info=True)
        try:
            if self.http_server.is_running:
                self.http_server.stop()
        except Exception:
            logger.debug("Error stopping McpHttpServer", exc_info=True)
