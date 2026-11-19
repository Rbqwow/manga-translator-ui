"""Qt service bridging ConfigService, MainAppLogic, LogService, McpJobManager, and McpHttpServer."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import tomllib
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from manga_translator.mcp import (
    find_api_backend_conflicts,
    format_mcp_endpoint_url,
)
from services.log_service import get_active_log_file_path

if TYPE_CHECKING:
    from manga_translator.mcp import McpHttpServer, McpJobManager

logger = logging.getLogger("manga_translator.mcp.service")

MCP_SERVER_NAME = "manga-translator"
AGENTS_STANDARD_SKILL_RELPATH = ".agents/skills/manga-translate/SKILL.md"


@dataclass(frozen=True)
class McpClientProfile:
    """Declarative integration profile for a supported MCP coding harness."""

    key: str
    display_name: str
    config_format: Literal["json", "toml"]
    root_key: str
    default_config_relpath: Optional[str]
    default_skill_relpath: str
    supports_write: bool = True
    requires_custom_config_path: bool = False

    def default_config_path(self) -> Optional[Path]:
        if not self.default_config_relpath:
            return None
        return Path.home() / Path(self.default_config_relpath)

    def default_skill_path(self) -> Path:
        return Path.home() / Path(self.default_skill_relpath)


MCP_CLIENT_PROFILES: Dict[str, McpClientProfile] = {
    "generic": McpClientProfile(
        key="generic",
        display_name="Generic Streamable HTTP (Copy Only)",
        config_format="json",
        root_key="mcpServers",
        default_config_relpath=None,
        default_skill_relpath=AGENTS_STANDARD_SKILL_RELPATH,
        supports_write=False,
    ),
    "claude_code": McpClientProfile(
        key="claude_code",
        display_name="Claude Code",
        config_format="json",
        root_key="mcpServers",
        default_config_relpath=".claude.json",
        default_skill_relpath=".claude/skills/manga-translate/SKILL.md",
    ),
    "codex": McpClientProfile(
        key="codex",
        display_name="Codex CLI",
        config_format="toml",
        root_key="mcp_servers",
        default_config_relpath=".codex/config.toml",
        default_skill_relpath=AGENTS_STANDARD_SKILL_RELPATH,
    ),
    "cursor": McpClientProfile(
        key="cursor",
        display_name="Cursor",
        config_format="json",
        root_key="mcpServers",
        default_config_relpath=".cursor/mcp.json",
        default_skill_relpath=".cursor/skills/manga-translate/SKILL.md",
    ),
    "opencode": McpClientProfile(
        key="opencode",
        display_name="OpenCode",
        config_format="json",
        root_key="mcp",
        default_config_relpath=".config/opencode/opencode.json",
        default_skill_relpath=".config/opencode/skills/manga-translate/SKILL.md",
    ),
    "antigravity": McpClientProfile(
        key="antigravity",
        display_name="Google Antigravity",
        config_format="json",
        root_key="mcpServers",
        default_config_relpath=".gemini/config/mcp_config.json",
        default_skill_relpath=".gemini/config/skills/manga-translate/SKILL.md",
    ),
    "windsurf": McpClientProfile(
        key="windsurf",
        display_name="Windsurf",
        config_format="json",
        root_key="mcpServers",
        default_config_relpath=".codeium/windsurf/mcp_config.json",
        default_skill_relpath=".codeium/windsurf/skills/manga-translate/SKILL.md",
    ),
    "vscode": McpClientProfile(
        key="vscode",
        display_name="VS Code / Copilot",
        config_format="json",
        root_key="servers",
        default_config_relpath=None,
        default_skill_relpath=AGENTS_STANDARD_SKILL_RELPATH,
        supports_write=True,
        requires_custom_config_path=True,
    ),
    "stdio_bridge": McpClientProfile(
        key="stdio_bridge",
        display_name="Stdio Bridge (mcp-remote)",
        config_format="json",
        root_key="mcpServers",
        default_config_relpath=None,
        default_skill_relpath=AGENTS_STANDARD_SKILL_RELPATH,
        supports_write=False,
    ),
}

_CODEX_SECTION_RE = re.compile(
    r"""^\s*\[\s*mcp_servers\s*\.\s*(?:manga-translator|"manga-translator"|'manga-translator')\s*\]\s*(?:#.*)?$"""
)
_CODEX_INLINE_TABLE_RE = re.compile(r"^\s*mcp_servers\s*=\s*\{", re.MULTILINE)
_TOML_TABLE_HEADER_RE = re.compile(r"^\s*\[")
_TOML_URL_LINE_RE = re.compile(r"^(\s*)url\s*=")
_TOML_ASSIGNMENT_KEY_RE = re.compile(r"""^\s*(?:([A-Za-z0-9_-]+)|"([^"]+)"|'([^']+)')\s*=(.*)$""")


def _find_toml_assignment_end(lines: List[str], start_idx: int) -> int:
    """Return inclusive line index where a TOML key = value assignment ends."""
    in_basic = False
    in_ml_basic = False
    in_literal = False
    in_ml_literal = False
    bracket_depth = 0

    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        if idx == start_idx:
            match = _TOML_ASSIGNMENT_KEY_RE.match(line)
            segment = match.group(4) if match else ""
        else:
            segment = line

        pos = 0
        length = len(segment)
        while pos < length:
            ch = segment[pos]
            if in_ml_basic:
                if segment.startswith('"""', pos):
                    in_ml_basic = False
                    pos += 3
                    continue
                if ch == "\\" and pos + 1 < length:
                    pos += 2
                    continue
                pos += 1
                continue
            if in_ml_literal:
                if segment.startswith("'''", pos):
                    in_ml_literal = False
                    pos += 3
                    continue
                pos += 1
                continue
            if in_basic:
                if ch == "\\":
                    pos += 2
                    continue
                if ch == '"':
                    in_basic = False
                pos += 1
                continue
            if in_literal:
                if ch == "'":
                    in_literal = False
                pos += 1
                continue

            if ch == "#":
                break
            if segment.startswith('"""', pos):
                in_ml_basic = True
                pos += 3
                continue
            if segment.startswith("'''", pos):
                in_ml_literal = True
                pos += 3
                continue
            if ch == '"':
                in_basic = True
                pos += 1
                continue
            if ch == "'":
                in_literal = True
                pos += 1
                continue
            if ch in "[{":
                bracket_depth += 1
            elif ch in "]}":
                bracket_depth = max(0, bracket_depth - 1)
            pos += 1

        if not (in_basic or in_ml_basic or in_literal or in_ml_literal) and bracket_depth == 0:
            return idx
    return len(lines) - 1


def _strip_toml_section_keys(section_lines: List[str], keys_to_remove: set[str]) -> List[str]:
    """Remove specified top-level key assignments (including multi-line values) from section lines."""
    kept: List[str] = []
    idx = 0
    while idx < len(section_lines):
        match = _TOML_ASSIGNMENT_KEY_RE.match(section_lines[idx])
        if match:
            key = next((g for g in match.groups()[:3] if g is not None), "")
            if key in keys_to_remove:
                idx = _find_toml_assignment_end(section_lines, idx) + 1
                continue
        kept.append(section_lines[idx])
        idx += 1
    return kept


def _build_client_server_entry(client: str, url: str) -> Dict[str, Any]:
    if client in {"claude_code", "vscode"}:
        return {"type": "http", "url": url}
    if client == "opencode":
        return {"type": "remote", "url": url}
    if client == "windsurf":
        return {"serverUrl": url}
    if client == "stdio_bridge":
        return {"command": "npx", "args": ["-y", "mcp-remote", url]}
    return {"url": url}


def _parse_json_or_jsonc(raw_text: str, target: Path) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        try:
            import json5

            parsed = json5.loads(raw_text)
        except Exception as exc:
            raise ValueError(f"Failed to parse JSON config {target}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object in {target}")
    return parsed


def _merge_codex_toml_config(existing_text: str, url: str) -> str:
    existing_text = existing_text.lstrip("\ufeff")
    if existing_text.strip():
        try:
            tomllib.loads(existing_text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid TOML syntax: {exc}") from exc

    url_literal = json.dumps(url)
    lines = existing_text.splitlines()
    section_start: Optional[int] = None
    for idx, line in enumerate(lines):
        if _CODEX_SECTION_RE.match(line):
            section_start = idx
            break

    if section_start is not None:
        section_end = len(lines)
        for idx in range(section_start + 1, len(lines)):
            if _TOML_TABLE_HEADER_RE.match(lines[idx]):
                section_end = idx
                break

        section_body = _strip_toml_section_keys(
            lines[section_start + 1 : section_end],
            {"command", "args"},
        )
        lines = lines[: section_start + 1] + section_body + lines[section_end:]
        section_end = section_start + 1 + len(section_body)

        url_idx: Optional[int] = None
        for idx in range(section_start + 1, section_end):
            if _TOML_URL_LINE_RE.match(lines[idx]):
                url_idx = idx
                break

        if url_idx is not None:
            indent = _TOML_URL_LINE_RE.match(lines[url_idx]).group(1)  # type: ignore[union-attr]
            lines[url_idx] = f"{indent}url = {url_literal}"
        else:
            insert_at = section_end
            while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, f"url = {url_literal}")

        merged = "\n".join(lines).rstrip("\n") + "\n"
    else:
        block = f"[mcp_servers.{MCP_SERVER_NAME}]\nurl = {url_literal}\n"
        if existing_text.strip():
            merged = existing_text.rstrip() + "\n\n" + block
        else:
            merged = block

    try:
        validated = tomllib.loads(merged)
    except tomllib.TOMLDecodeError as exc:
        if _CODEX_INLINE_TABLE_RE.search(existing_text):
            raise ValueError(
                "Detected inline table syntax 'mcp_servers = { ... }' in Codex config.toml, "
                "which cannot be combined with a [mcp_servers.manga-translator] section. "
                "Please rewrite mcp_servers as standard [mcp_servers.<name>] sections first."
            ) from exc
        raise ValueError(f"Merged Codex TOML is invalid: {exc}") from exc

    servers = validated.get("mcp_servers")
    if not isinstance(servers, dict) or not isinstance(servers.get(MCP_SERVER_NAME), dict):
        raise ValueError("Merged Codex TOML is missing [mcp_servers.manga-translator]")
    entry = servers[MCP_SERVER_NAME]
    if entry.get("url") != url:
        raise ValueError("Merged Codex TOML url verification failed")
    if "command" in entry or "args" in entry:
        raise ValueError("Merged Codex TOML still contains conflicting stdio keys")
    return merged


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
    api_conflicts_changed = pyqtSignal(list)
    models_unload_finished = pyqtSignal(bool, str)
    job_cancel_finished = pyqtSignal(bool, str)

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
        self._server_op_lock = threading.Lock()
        self._job_manager: Optional[Any] = None
        self._http_server: Optional[Any] = None

        if self._config_service is not None and hasattr(self._config_service, "config_changed"):
            try:
                self._config_service.config_changed.connect(self._on_config_changed)
            except Exception:
                pass

    @property
    def job_manager(self) -> "McpJobManager":
        with self._lock:
            if self._job_manager is None:
                from manga_translator.mcp import McpJobManager

                self._job_manager = McpJobManager(
                    root_dir=self.root_dir,
                    get_config=self._get_config,
                    get_gui_snapshot=self._get_gui_snapshot,
                    is_gui_busy=self._is_gui_busy,
                    file_expander=self._expand_inputs,
                    on_progress=self._on_job_progress,
                    on_job_finished=self._on_job_finished,
                )
            return self._job_manager

    @property
    def http_server(self) -> "McpHttpServer":
        with self._lock:
            if self._http_server is None:
                from manga_translator.mcp import McpHttpServer

                self._http_server = McpHttpServer(self.job_manager)
            return self._http_server

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
            if self.job_manager.should_update_ui_for_job(summary.get("job_id")):
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
        if self._http_server is not None and self._http_server.is_running:
            return self._http_server.endpoint_url
        host, port = self._configured_host_port()
        return format_mcp_endpoint_url(host, port)

    def is_server_running(self) -> bool:
        return bool(self._http_server is not None and self._http_server.is_running)

    def start_server(self, port: Optional[int] = None) -> tuple[bool, str, str]:
        with self._server_op_lock:
            host, cfg_port = self._configured_host_port()
            target_port = int(port) if port is not None else cfg_port
            try:
                from manga_translator.mcp import configure_mcp_transport_logging

                configure_mcp_transport_logging()
                url = self.http_server.start(host=host, port=target_port)
                if not self._shutdown_done:
                    try:
                        self.server_state_changed.emit(True, url, "")
                    except RuntimeError:
                        pass
                return True, url, ""
            except Exception as exc:
                err = str(exc)
                logger.error(f"Failed to start MCP server: {err}")
                url = format_mcp_endpoint_url(host, target_port)
                if not self._shutdown_done:
                    try:
                        self.server_state_changed.emit(False, url, err)
                    except RuntimeError:
                        pass
                return False, url, err

    def stop_server(self, *, cancel_active_job: bool = True) -> tuple[bool, str, str]:
        with self._server_op_lock:
            try:
                if cancel_active_job and self.has_active_job():
                    try:
                        self.job_manager.cancel_job(join_timeout=3.0)
                    except KeyError:
                        pass
                if self._http_server is not None:
                    self._http_server.stop()
                url = self.endpoint_url()
                if not self._shutdown_done:
                    try:
                        self.server_state_changed.emit(False, url, "")
                    except RuntimeError:
                        pass
                return True, url, ""
            except Exception as exc:
                err = str(exc)
                logger.error(f"Failed to stop MCP server: {err}")
                url = self.endpoint_url()
                if not self._shutdown_done:
                    try:
                        self.server_state_changed.emit(self.is_server_running(), url, err)
                    except RuntimeError:
                        pass
                return False, url, err

    def start_server_async(self, port: Optional[int] = None) -> None:
        threading.Thread(
            target=self.start_server,
            kwargs={"port": port},
            name="mcp-server-start",
            daemon=True,
        ).start()

    def stop_server_async(self, *, cancel_active_job: bool = True) -> None:
        threading.Thread(
            target=self.stop_server,
            kwargs={"cancel_active_job": cancel_active_job},
            name="mcp-server-stop",
            daemon=True,
        ).start()

    def maybe_auto_start(self) -> None:
        cfg = self._get_config()
        mcp_cfg = getattr(cfg, "mcp", None)
        if bool(getattr(mcp_cfg, "auto_start", False)) and not self.is_server_running():
            self.start_server()

    def has_active_job(self) -> bool:
        return bool(self._job_manager is not None and self._job_manager.has_active_job())

    def get_current_job_status(self) -> Optional[Dict[str, Any]]:
        if self._job_manager is None:
            return None
        try:
            return self._job_manager.get_job_status()
        except KeyError:
            return None

    def cancel_active_job(self) -> Optional[Dict[str, Any]]:
        if not self.has_active_job():
            return None
        try:
            return self._job_manager.cancel_job()  # type: ignore[union-attr]
        except KeyError:
            return None

    def cancel_active_job_async(
        self,
        on_done: Optional[Callable[[Optional[Dict[str, Any]]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        def _worker() -> None:
            try:
                result = self.cancel_active_job()
                if not self._shutdown_done:
                    try:
                        self.job_cancel_finished.emit(True, "")
                    except RuntimeError:
                        pass
                if callable(on_done):
                    on_done(result)
            except Exception as exc:
                err = str(exc)
                if not self._shutdown_done:
                    try:
                        self.job_cancel_finished.emit(False, err)
                    except RuntimeError:
                        pass
                if callable(on_error):
                    on_error(err)

        threading.Thread(target=_worker, name="mcp-cancel-job", daemon=True).start()

    def unload_models_async(
        self,
        on_done: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        def _worker() -> None:
            try:
                result = self.job_manager.unload_models()
                if not self._shutdown_done:
                    try:
                        self.models_unload_finished.emit(True, "")
                    except RuntimeError:
                        pass
                if callable(on_done):
                    on_done(result)
            except Exception as exc:
                err = str(exc)
                if not self._shutdown_done:
                    try:
                        self.models_unload_finished.emit(False, err)
                    except RuntimeError:
                        pass
                if callable(on_error):
                    on_error(err)

        threading.Thread(target=_worker, name="mcp-unload-models", daemon=True).start()

    def get_api_backend_conflicts(self) -> List[Dict[str, str]]:
        cfg = self._get_config()
        return [c.model_dump() for c in find_api_backend_conflicts(cfg)]

    # ------------------------------------------------------------------
    # Multi-harness MCP client integration helpers
    # ------------------------------------------------------------------
    @staticmethod
    def list_mcp_client_profiles() -> List[McpClientProfile]:
        return list(MCP_CLIENT_PROFILES.values())

    @staticmethod
    def get_mcp_client_profile(client: str) -> McpClientProfile:
        key = str(client or "").strip()
        profile = MCP_CLIENT_PROFILES.get(key)
        if profile is None:
            supported = ", ".join(MCP_CLIENT_PROFILES.keys())
            raise ValueError(f"Unsupported MCP client '{client}'. Supported: {supported}")
        return profile

    def _resolved_endpoint_url(self, port: Optional[int] = None) -> str:
        host, cfg_port = self._configured_host_port()
        resolved_port = int(port) if port is not None else cfg_port
        return format_mcp_endpoint_url(host, resolved_port)

    def get_mcp_client_config_snippet(
        self,
        client: str = "generic",
        port: Optional[int] = None,
    ) -> str:
        profile = self.get_mcp_client_profile(client)
        url = self._resolved_endpoint_url(port=port)
        if profile.config_format == "toml":
            return f"[mcp_servers.{MCP_SERVER_NAME}]\nurl = {json.dumps(url)}\n"
        payload = {
            profile.root_key: {
                MCP_SERVER_NAME: _build_client_server_entry(profile.key, url),
            }
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def install_mcp_config(
        self,
        client: str,
        config_path: Optional[str | Path] = None,
        *,
        port: Optional[int] = None,
    ) -> str:
        profile = self.get_mcp_client_profile(client)
        if not profile.supports_write:
            raise ValueError(
                f"Client '{profile.display_name}' is copy-only and does not support automatic config writing."
            )

        if config_path is not None:
            target = Path(config_path)
        else:
            default_path = profile.default_config_path()
            if default_path is None:
                raise ValueError(
                    f"A target config file path must be specified for '{profile.display_name}'."
                )
            target = default_path

        url = self._resolved_endpoint_url(port=port)

        if profile.config_format == "toml":
            existing_text = target.read_text(encoding="utf-8-sig") if target.exists() else ""
            merged_toml = _merge_codex_toml_config(existing_text, url)
            _atomic_write_text(target, merged_toml)
        else:
            existing_data: Dict[str, Any] = {}
            if target.exists():
                raw_text = target.read_text(encoding="utf-8-sig").strip()
                if raw_text:
                    existing_data = _parse_json_or_jsonc(raw_text, target)

            servers = existing_data.get(profile.root_key)
            if servers is None:
                servers = {}
            elif not isinstance(servers, dict):
                raise ValueError(f"'{profile.root_key}' in {target} must be a JSON object")

            servers = dict(servers)
            servers[MCP_SERVER_NAME] = _build_client_server_entry(profile.key, url)
            existing_data[profile.root_key] = servers

            _atomic_write_text(target, json.dumps(existing_data, indent=2, ensure_ascii=False) + "\n")

        logger.info(f"[MCP] Updated {profile.display_name} MCP config at {target}")
        return str(target)

    def install_skill(
        self,
        target: str = "agents_standard",
        skill_path: Optional[str | Path] = None,
    ) -> str:
        if skill_path is not None:
            dest = Path(skill_path)
        elif str(target or "").strip() == "agents_standard":
            dest = Path.home() / Path(AGENTS_STANDARD_SKILL_RELPATH)
        else:
            profile = self.get_mcp_client_profile(target)
            dest = profile.default_skill_path()

        template_path = Path(self.root_dir) / "manga_translator" / "mcp" / "skill_template.md"
        if not template_path.exists():
            import manga_translator.mcp as mcp_pkg

            template_path = Path(mcp_pkg.__file__).resolve().parent / "skill_template.md"
        content = template_path.read_text(encoding="utf-8")
        _atomic_write_text(dest, content)
        logger.info(f"[MCP] Installed manga-translate skill at {dest}")
        return str(dest)

    def get_log_directory(self) -> str:
        active_path = get_active_log_file_path()
        if active_path:
            return os.path.dirname(active_path)
        return os.path.normpath(os.path.abspath(os.path.join(self.root_dir, "result")))

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        try:
            if self._job_manager is not None:
                self._job_manager.shutdown()
        except Exception:
            logger.debug("Error shutting down McpJobManager", exc_info=True)
        try:
            if self._http_server is not None and self._http_server.is_running:
                self._http_server.stop()
        except Exception:
            logger.debug("Error stopping McpHttpServer", exc_info=True)
