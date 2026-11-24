import _bootstrap  # noqa: F401

import os
from types import SimpleNamespace

import pytest

from manga_translator.utils import curl_cffi_transport
from manga_translator.utils.curl_cffi_transport import _encode_curl_file_path


@pytest.mark.skipif(os.name != "nt", reason="Windows ACP behavior only")
@pytest.mark.parametrize(
    ("acp", "path"),
    [
        (936, r"C:\测试目录\cacert.pem"),
        (1252, r"C:\café\cacert.pem"),
    ],
)
def test_curl_file_path_uses_windows_acp_under_utf8_mode(monkeypatch, acp, path):
    encoding = f"cp{acp}"
    expected = path.encode(encoding)
    assert expected != path.encode("utf-8")

    monkeypatch.setattr(
        curl_cffi_transport,
        "ctypes",
        SimpleNamespace(
            windll=SimpleNamespace(kernel32=SimpleNamespace(GetACP=lambda: acp))
        ),
    )

    encoded = _encode_curl_file_path(path)

    assert isinstance(encoded, str)
    assert encoded == path
    assert encoded.encode("utf-8") == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows ACP behavior only")
def test_curl_file_path_raises_when_getacp_returns_zero(monkeypatch):
    monkeypatch.setattr(
        curl_cffi_transport,
        "ctypes",
        SimpleNamespace(
            windll=SimpleNamespace(kernel32=SimpleNamespace(GetACP=lambda: 0))
        ),
    )

    with pytest.raises(OSError, match="GetACP"):
        _encode_curl_file_path(r"C:\cacert.pem")
