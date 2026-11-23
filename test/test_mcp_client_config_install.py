"""Installing the MCP entry must not silently destroy comments in client configs."""

import _bootstrap  # noqa: F401

import json

import pytest

from services.mcp_service import McpService


@pytest.fixture
def service(tmp_path):
    svc = McpService(root_dir=str(tmp_path))
    yield svc
    svc.shutdown()


def test_jsonc_config_is_backed_up_before_rewriting(service, tmp_path):
    target = tmp_path / "mcp.json"
    original = (
        "{\n"
        "  // keep this note\n"
        '  "mcpServers": {"other": {"command": "echo"},},\n'
        "}\n"
    )
    target.write_text(original, encoding="utf-8")

    service.install_mcp_config("cursor", config_path=target, port=9123)

    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["mcpServers"]["other"] == {"command": "echo"}
    assert written["mcpServers"]["manga-translator"] == {"url": "http://127.0.0.1:9123/mcp"}
    backups = list(tmp_path.glob("mcp.json.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_plain_json_config_needs_no_backup(service, tmp_path):
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    service.install_mcp_config("cursor", config_path=target, port=9123)

    assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["manga-translator"]
    assert list(tmp_path.glob("*.bak")) == []
