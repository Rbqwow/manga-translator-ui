import _bootstrap  # noqa: F401

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import time

import numpy as np
import pytest
from PIL import Image
import yaml

from manga_translator.manga_translator import MangaTranslator
from manga_translator.mcp.batch_adapter import (
    apply_batch_post_processing,
    build_mcp_engine_config,
    find_api_backend_conflicts,
)
from manga_translator.mcp.job_manager import McpJobManager
from manga_translator.utils.path_manager import get_inpainted_path, get_json_path
from manga_translator.utils.textblock import TextBlock
from services.log_service import (
    configure_queue_logging,
    shutdown_queue_logging,
)
from services.mcp_service import McpService


def _make_png(path: Path, size=(240, 320), color=(245, 245, 245)) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")
    return str(path)


def _make_block(
    text: str,
    *,
    num_lines: int = 1,
    prob: float = 0.95,
    x: int = 20,
    y: int = 20,
    w: int = 80,
    h: int = 24,
) -> TextBlock:
    lines = []
    for idx in range(num_lines):
        y0 = y + idx * (h + 4)
        y1 = y0 + h
        lines.append([(x, y0), (x + w, y0), (x + w, y1), (x, y1)])
    return TextBlock(
        lines=lines,
        texts=[text] * num_lines if num_lines > 1 else [text],
        prob=prob,
        font_size=18,
    )


def _base_config(output_dir: str, **mcp_overrides) -> dict:
    mcp_cfg = {
        "host": "127.0.0.1",
        "port": 8765,
        "auto_start": False,
        "translation_mode": "hq",
        "allow_on_demand_image": True,
        "image_delivery": "auto",
        "inline_image_budget_mb": 2.0,
        "preview_max_size": 1024,
        "preview_format": "jpeg",
        "preview_quality": 82,
        "max_parallel_batches": 1,
        "lease_timeout_seconds": 30,
        "models_idle_ttl": 0,
        "use_gui_files_by_default": False,
    }
    mcp_cfg.update(mcp_overrides)
    return {
        "app": {
            "last_output_path": output_dir,
            "current_preset": "默认",
            "unload_models_after_translation": False,
        },
        "translator": {
            "translator": "openai_hq",
            "target_lang": "CHS",
            "extract_glossary": False,
            "enable_post_translation_check": False,
            "post_check_max_retry_attempts": 3,
            "post_check_repetition_threshold": 6,
            "high_quality_prompt_path": None,
            "no_text_lang_skip": False,
        },
        "ocr": {
            "ocr": "48px",
            "secondary_ocr": "mocr",
            "use_hybrid_ocr": False,
            "min_text_length": 0,
            "prob": 0.1,
        },
        "detector": {
            "detector": "default",
            "detection_size": 1024,
            "text_threshold": 0.5,
            "box_threshold": 0.5,
            "unclip_ratio": 2.0,
        },
        "inpainter": {
            "inpainter": "lama_large",
            "inpainting_size": 1024,
            "inpainting_precision": "fp32",
        },
        "render": {
            "renderer": "default",
            "alignment": "auto",
            "direction": "auto",
            "disable_auto_wrap": False,
            "check_br_and_retry": False,
            "uppercase": False,
            "lowercase": False,
            "filter_text": None,
        },
        "colorizer": {"colorizer": "none"},
        "upscale": {"upscale_ratio": None},
        "cli": {
            "use_gpu": False,
            "verbose": False,
            "batch_size": 3,
            "context_size": 3,
            "batch_concurrent": False,
            "format": "png",
            "overwrite": True,
            "skip_no_text": True,
            "save_text": True,
        },
        "mcp": mcp_cfg,
    }


def _make_stub_factory(regions_by_filename, calls_log=None):
    calls = calls_log if calls_log is not None else {
        "prepare": 0,
        "detect": [],
        "ocr": [],
        "inpaint": [],
        "render_skip_font_scaling": [],
        "revert": [],
        "post_check_flags": [],
    }

    def factory(translator_params):
        translator = MangaTranslator(params=translator_params)
        translator._mcp_context_enabled = True
        translator._models_loaded = True
        translator.start_model_cleanup_job = False

        async def _forbid_prepare(*_a, **_kw):
            calls["prepare"] += 1
            raise AssertionError("prepare_translation must never be called in MCP mode")

        def _ctx_fname(ctx) -> str:
            img_name = (
                getattr(ctx, "image_name", None)
                or getattr(getattr(ctx, "input", None), "name", "")
                or ""
            )
            return os.path.basename(img_name)

        async def _detect(config, ctx):
            fname = _ctx_fname(ctx)
            calls["detect"].append(fname)
            blocks = [
                TextBlock(
                    lines=b.lines.copy(),
                    texts=list(b.texts),
                    prob=b.prob,
                    font_size=b.font_size,
                )
                for b in regions_by_filename.get(fname, [])
            ]
            if not blocks:
                return [], None, None
            h, w = ctx.img_rgb.shape[:2]
            mask = np.zeros((h, w), dtype=np.uint8)
            for b in blocks:
                x1, y1, x2, y2 = [int(v) for v in b.xyxy]
                mask[max(0, y1):min(h, y2), max(0, x1):min(w, x2)] = 255
            return blocks, mask, mask.copy()

        async def _ocr(config, ctx):
            fname = _ctx_fname(ctx)
            calls["ocr"].append(fname)
            return list(ctx.textlines or [])

        async def _merge(config, ctx):
            return list(ctx.textlines or [])

        async def _mask(config, ctx):
            h, w = ctx.img_rgb.shape[:2]
            mask = np.zeros((h, w), dtype=np.uint8)
            for r in (ctx.text_regions or []):
                x1, y1, x2, y2 = [int(v) for v in r.xyxy]
                mask[max(0, y1):min(h, y2), max(0, x1):min(w, x2)] = 255
            return mask

        async def _inpaint(config, ctx):
            fname = _ctx_fname(ctx)
            calls["inpaint"].append((fname, ctx.mask is None))
            return ctx.img_rgb.copy()

        async def _render(config, ctx, skip_font_scaling: bool = False, **_kwargs):
            calls.setdefault("render_skip_font_scaling", []).append(bool(skip_font_scaling))
            return ctx.img_inpainted.copy()

        orig_revert = translator._revert_upscale

        async def _revert(config, ctx):
            fname = _ctx_fname(ctx)
            calls["revert"].append(fname)
            return await orig_revert(config, ctx)

        orig_post = translator._apply_post_translation_processing

        async def _post(ctx, config):
            calls["post_check_flags"].append(bool(config.translator.enable_post_translation_check))
            return await orig_post(ctx, config)

        translator.prepare_translation = _forbid_prepare
        translator._run_detection = _detect
        translator._run_ocr = _ocr
        translator._run_textline_merge = _merge
        translator._run_mask_refinement = _mask
        translator._run_inpainting = _inpaint
        translator._run_text_rendering = _render
        translator._revert_upscale = _revert
        translator._apply_post_translation_processing = _post
        return translator

    return factory, calls


def test_closed_loop_validation_retries_and_sidecars(tmp_path):
    out_dir = tmp_path / "out"
    img_path = _make_png(tmp_path / "p1.png")
    regions_map = {
        "p1.png": [
            _make_block("一行目", num_lines=1, y=20),
            _make_block("二行目A", num_lines=2, y=60),
            _make_block("二行目B", num_lines=2, y=130),
            _make_block("四番目", num_lines=1, y=200),
        ]
    }
    factory, calls = _make_stub_factory(regions_map)

    cfg = _base_config(str(out_dir))
    cfg["render"]["disable_auto_wrap"] = True
    cfg["render"]["check_br_and_retry"] = True
    cfg["translator"]["enable_post_translation_check"] = True
    cfg["translator"]["post_check_repetition_threshold"] = 6

    mgr = McpJobManager(
        root_dir=str(_bootstrap.ROOT),
        get_config=lambda: cfg,
        translator_factory=factory,
    )
    try:
        job_resp = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        assert job_resp.total_pages == 1
        assert job_resp.pending_pages == 1
        assert job_resp.effective_mode == "hq"

        batch, inline_images = mgr.next_translation_batch(job_resp.job_id)
        assert batch.batch_id is not None
        assert len(inline_images) == 1
        assert len(batch.pages[0].regions) == 4

        # 1) missing_id + duplicate_id + unknown_id
        r1 = mgr.submit_translation_batch(
            job_resp.job_id,
            batch.batch_id,
            [
                {"id": 1, "translation": "第一句"},
                {"id": 1, "translation": "重复"},
                {"id": 2, "translation": "两[BR]行A"},
                {"id": 3, "translation": "两[BR]行B"},
                {"id": 99, "translation": "未知"},
            ],
            return_next=True,
        )[0]
        assert r1.status == "retry_needed"
        reasons_1 = {e.reason for e in r1.errors}
        assert {"missing_id", "duplicate_id", "unknown_id"} <= reasons_1

        # 2) missing_br_marker (2 out of 2 multi-line bubbles lack [BR], exceeding tolerance=1)
        r2 = mgr.submit_translation_batch(
            job_resp.job_id,
            batch.batch_id,
            [
                {"id": 1, "translation": "第一[BR]句"},
                {"id": 2, "translation": "缺断句A"},
                {"id": 3, "translation": "缺断句B"},
                {"id": 4, "translation": "第四句"},
            ],
            return_next=True,
        )[0]
        assert r2.status == "retry_needed"
        assert {e.reason for e in r2.errors} == {"missing_br_marker"}
        assert {e.id for e in r2.errors} == {2, 3}

        # 3) repetition_hallucination
        r3 = mgr.submit_translation_batch(
            job_resp.job_id,
            batch.batch_id,
            [
                {"id": 1, "translation": "第一[BR]句"},
                {"id": 2, "translation": "第二[BR]行A"},
                {"id": 3, "translation": "第三[BR]行B"},
                {"id": 4, "translation": "哈哈哈哈哈哈哈哈"},
            ],
            return_next=True,
        )[0]
        assert r3.status == "retry_needed"
        assert {e.reason for e in r3.errors} == {"repetition_hallucination"}
        assert r3.errors[0].id == 4

        # 4) valid submission; single-region id=1 still has extra [BR] cleaned automatically
        r4, imgs4 = mgr.submit_translation_batch(
            job_resp.job_id,
            batch.batch_id,
            [
                {"id": 1, "translation": "第一[BR]句"},
                {"id": 2, "translation": "第二[BR]行A"},
                {"id": 3, "translation": "第三[BR]行B"},
                {"id": 4, "translation": "第四句"},
            ],
            return_next=True,
        )
        assert r4.status == "accepted"
        assert r4.done is True
        assert imgs4 == []
        assert r4.summary["rendered_pages"] == 1

        out_file = out_dir / "p1.png"
        assert out_file.exists()
        json_file = Path(get_json_path(img_path, create_dir=False))
        assert json_file.exists()
        inpainted_file = Path(get_inpainted_path(img_path, create_dir=False))
        assert inpainted_file.exists()

        saved_json = json.loads(json_file.read_text(encoding="utf-8"))
        img_key = next(iter(saved_json))
        assert saved_json[img_key]["regions"][0]["translation"] == "第一句"

        assert calls["prepare"] == 0
        assert calls["revert"] == ["p1.png"]
        assert calls["post_check_flags"] == [False]
    finally:
        mgr.shutdown()


def test_vision_modes_hq_ondemand_and_text_only(tmp_path):
    img_path = _make_png(tmp_path / "p1.png", size=(400, 600))
    regions_map = {
        "p1.png": [
            _make_block("はっきり", prob=0.95, x=20, y=20, w=80, h=24),
            _make_block("ぼやけ", prob=0.35, x=140, y=180, w=90, h=30),
        ]
    }
    factory, _calls = _make_stub_factory(regions_map)

    # 1) text_on_demand
    out_td = tmp_path / "out_td"
    cfg_td = _base_config(str(out_td), translation_mode="text", allow_on_demand_image=True)
    mgr_td = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg_td, translator_factory=factory)
    try:
        job_td = mgr_td.create_translation_job(inputs=[img_path], output_dir=str(out_td))
        assert job_td.effective_mode == "text_on_demand"

        batch_td, inline_td = mgr_td.next_translation_batch(job_td.job_id)
        assert inline_td == []
        assert batch_td.pages[0].image_delivery_mode == "none"
        assert batch_td.pages[0].low_confidence_ids == [2]

        prev_resp, prev_bytes = mgr_td.get_page_preview(
            job_td.job_id, 0, region_ids=[2], high_res=True
        )
        assert prev_bytes is not None and len(prev_bytes) > 0
        assert prev_resp.delivery_mode == "mcp_image"
        assert prev_resp.lease_renewed is True
        assert prev_resp.width < 400 and prev_resp.height < 600
        assert mgr_td.get_job_status(job_td.job_id)["on_demand_image_requests"] == 1
    finally:
        mgr_td.shutdown()

    # 2) text_only forbids get_page_preview
    out_to = tmp_path / "out_to"
    cfg_to = _base_config(str(out_to), translation_mode="text", allow_on_demand_image=False)
    mgr_to = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg_to, translator_factory=factory)
    try:
        job_to = mgr_to.create_translation_job(inputs=[img_path], output_dir=str(out_to))
        assert job_to.effective_mode == "text_only"
        mgr_to.next_translation_batch(job_to.job_id)
        with pytest.raises(PermissionError):
            mgr_to.get_page_preview(job_to.job_id, 0)
    finally:
        mgr_to.shutdown()


def test_image_delivery_budget_fallback_and_cleanup(tmp_path):
    img_path = _make_png(tmp_path / "p1.png", size=(300, 400))
    regions_map = {"p1.png": [_make_block("台詞", prob=0.9)]}
    factory, _calls = _make_stub_factory(regions_map)

    out_dir = tmp_path / "out"
    cfg = _base_config(
        str(out_dir),
        translation_mode="hq",
        image_delivery="auto",
        inline_image_budget_mb=0.0001,  # 104 bytes -> guaranteed overflow
    )
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        batch, inline_images = mgr.next_translation_batch(job.job_id)
        assert inline_images == []
        page_item = batch.pages[0]
        assert page_item.image_delivery_mode == "file_path"
        assert page_item.preview_path is not None
        preview_file = Path(page_item.preview_path)
        assert preview_file.exists()
        assert "mcp_preview" in str(preview_file)

        prev_resp, prev_bytes = mgr.get_page_preview(job.job_id, 0, region_ids=[1])
        assert prev_bytes is None
        assert prev_resp.delivery_mode == "file_path"
        assert Path(prev_resp.file_path).exists()

        sub_resp, _ = mgr.submit_translation_batch(
            job.job_id,
            batch.batch_id,
            [{"id": 1, "translation": "台词"}],
            return_next=True,
        )
        assert sub_resp.done is True
        preview_dir = out_dir / "manga_translator_work" / "mcp_preview" / job.job_id
        assert not preview_dir.exists()

        # Verify cancel_job also deletes preview_dir
        job2 = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        b2, _ = mgr.next_translation_batch(job2.job_id)
        p2_file = Path(b2.pages[0].preview_path)
        assert p2_file.exists()
        mgr.cancel_job(job2.job_id)
        assert not p2_file.exists()
        assert not (out_dir / "manga_translator_work" / "mcp_preview" / job2.job_id).exists()
    finally:
        mgr.shutdown()


@pytest.mark.parametrize(
    ("section", "setting", "bad_value"),
    [
        ("ocr", "ocr", "openai_ocr"),
        ("ocr", "secondary_ocr", "gemini_ocr"),
        ("render", "renderer", "openai_renderer"),
        ("colorizer", "colorizer", "gemini_colorizer"),
    ],
)
def test_zero_api_enforcement_rejects_external_backends(tmp_path, section, setting, bad_value):
    img_path = _make_png(tmp_path / "p1.png")
    out_dir = tmp_path / "out"
    cfg = _base_config(str(out_dir))
    cfg[section][setting] = bad_value
    if section == "ocr" and setting == "secondary_ocr":
        cfg["ocr"]["use_hybrid_ocr"] = True

    conflicts = find_api_backend_conflicts(cfg)
    assert len(conflicts) == 1
    assert conflicts[0].section == section
    assert conflicts[0].setting == setting

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg)
    try:
        ws = mgr.get_workspace_info()
        assert len(ws.api_conflicts) == 1
        with pytest.raises(RuntimeError, match="0-API policy violation"):
            mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
    finally:
        mgr.shutdown()


def test_build_mcp_engine_config_isolation_and_cli_overrides(tmp_path):
    cfg = _base_config(str(tmp_path / "out"))
    cfg["translator"].update(
        {
            "translator": "openai_hq",
            "translator_chain": "deepl:CHS",
            "selective_translation": "sakura",
            "user_api_key": "sk-secret",
            "user_api_base": "https://example.invalid/v1",
            "user_api_model": "gpt-test",
        }
    )
    cfg["cli"]["load_text"] = True
    cfg["cli"]["translate_json_only"] = True

    engine_cfg, params = build_mcp_engine_config(
        cfg,
        str(_bootstrap.ROOT),
        target_lang="ENG",
        overwrite=False,
        batch_size=5,
        context_size=7,
    )
    assert engine_cfg.translator.translator.value == "none"
    assert engine_cfg.translator.translator_chain is None
    assert engine_cfg.translator.selective_translation is None
    assert engine_cfg.translator.user_api_key is None
    assert engine_cfg.translator.user_api_base is None
    assert engine_cfg.translator.user_api_model is None
    assert engine_cfg._allow_server_api_keys is False
    assert engine_cfg.translator.target_lang == "ENG"
    assert engine_cfg.cli.overwrite is False
    assert engine_cfg.cli.batch_size == 5
    assert engine_cfg.cli.context_size == 7
    assert engine_cfg.cli.translate_json_only is False
    assert params["load_text"] is False
    assert params["translate_json_only"] is False


def test_glossary_merge_lock_is_thread_safe(tmp_path):
    prompt_file = tmp_path / "prompt.yaml"
    prompt_file.write_text(
        yaml.safe_dump(
            {
                "system_prompt": "Translate manga naturally.",
                "glossary": {"Person": [{"original": "太郎", "translation": "太郎"}]},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    out_dir = tmp_path / "out"
    cfg_dict = _base_config(str(out_dir))
    cfg_dict["translator"]["extract_glossary"] = True
    cfg_dict["translator"]["high_quality_prompt_path"] = str(prompt_file)

    # Direct thread-safety stress test on apply_batch_post_processing glossary writes
    engine_cfg, params = build_mcp_engine_config(cfg_dict, str(_bootstrap.ROOT))
    translator = MangaTranslator(params=params)
    pages_dummy = [SimpleNamespace(failed=False, ctx=SimpleNamespace(text_regions=[]))]

    import asyncio

    def _merge_worker(idx: int):
        term_payload = {
            "category": "Item",
            "original": f"Term{idx}",
            "aliases": [
                {
                    "original": f"Term{idx}",
                    "translations": [{"text": f"道具{idx}", "context": "item"}],
                }
            ],
        }
        asyncio.run(
            apply_batch_post_processing(
                translator,
                engine_cfg,
                pages_dummy,
                {},
                new_terms=[term_payload],
                prompt_file_path=str(prompt_file),
            )
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_merge_worker, range(6)))

    parsed = yaml.safe_load(prompt_file.read_text(encoding="utf-8"))
    saved_terms = {item["original"] for item in parsed["glossary"].get("Item", [])}
    assert saved_terms == {f"Term{i}" for i in range(6)}


def test_lease_timeout_release_parallel_and_out_of_order_context(tmp_path):
    p1 = _make_png(tmp_path / "01.png")
    p2 = _make_png(tmp_path / "02.png")
    p3 = _make_png(tmp_path / "03.png")
    factory, _calls = _make_stub_factory(
        {
            "01.png": [_make_block("一页原文")],
            "02.png": [_make_block("二页原文")],
            "03.png": [_make_block("三页原文")],
        }
    )

    out_dir = tmp_path / "out"
    cfg = _base_config(
        str(out_dir),
        max_parallel_batches=2,
        lease_timeout_seconds=0.15,
    )
    cfg["cli"]["batch_size"] = 1
    cfg["cli"]["context_size"] = 2

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1, p2, p3], output_dir=str(out_dir))
        b1, _ = mgr.next_translation_batch(job.job_id, worker_id="w1")
        b2, _ = mgr.next_translation_batch(job.job_id, worker_id="w2")
        assert b1.batch_id != b2.batch_id

        # Both lease slots occupied -> non-blocking waiting response
        wait_resp, _ = mgr.next_translation_batch(job.job_id, worker_id="w3")
        assert wait_resp.waiting is True
        assert wait_resp.waiting_reason == "max_parallel_batches_leased"

        # Wait past lease_timeout_seconds (0.15s) -> expired leases reclaimed
        time.sleep(0.25)
        reclaimed, _ = mgr.next_translation_batch(job.job_id, worker_id="w3")
        assert reclaimed.waiting is False
        assert reclaimed.batch_id == b1.batch_id

        # Explicit release_batch returns it to READY
        rel = mgr.release_batch(job.job_id, b1.batch_id)
        assert rel["released"] is True

        # Re-lease both and submit out of order: b2 BEFORE b1
        b1_re, _ = mgr.next_translation_batch(job.job_id, worker_id="w1")
        b2_re, _ = mgr.next_translation_batch(job.job_id, worker_id="w2")

        mgr.submit_translation_batch(
            job.job_id,
            b2_re.batch_id,
            [{"id": 1, "translation": "第二页译文"}],
            return_next=False,
        )
        sub1, _ = mgr.submit_translation_batch(
            job.job_id,
            b1_re.batch_id,
            [{"id": 1, "translation": "第一页译文"}],
            return_next=True,
        )
        b3 = sub1.next_batch
        assert b3 is not None and not b3.done
        # Reading-order prev_context: [page 0, page 1]
        assert [entry["page_index"] for entry in b3.prev_context] == [0, 1]
        assert b3.prev_context[0]["items"][0]["translation"] == "第一页译文"
        assert b3.prev_context[1]["items"][0]["translation"] == "第二页译文"

        done_resp, _ = mgr.submit_translation_batch(
            job.job_id,
            b3.batch_id,
            [{"id": 1, "translation": "第三页译文"}],
            return_next=True,
        )
        assert done_resp.done is True
    finally:
        mgr.shutdown()


def test_resume_boundary_textless_bypass_and_retranslate_json(tmp_path):
    p1 = _make_png(tmp_path / "p1.png")
    p2 = _make_png(tmp_path / "p2.png")
    p3 = _make_png(tmp_path / "p3.png")
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    regions_map = {
        "p1.png": [_make_block("一页")],
        "p2.png": [_make_block("二页跳过")],
        "p3.png": [],  # textless!
    }
    factory, calls = _make_stub_factory(regions_map)

    # First write p2's existing output image + JSON sidecar so overwrite=False resumes cleanly.
    _make_png(out_dir / "p2.png")
    p2_json = Path(get_json_path(p2, create_dir=True))
    p2_json.write_text(
        json.dumps(
            {
                os.path.abspath(p2): {
                    "regions": [
                        {
                            "lines": [[[10, 10], [80, 10], [80, 30], [10, 30]]],
                            "texts": ["二页跳过"],
                            "text": "二页跳过",
                            "translation": "已有第二页译文",
                            "prob": 0.9,
                        }
                    ],
                    "original_width": 240,
                    "original_height": 320,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cfg = _base_config(str(out_dir))
    cfg["cli"]["overwrite"] = False
    cfg["cli"]["batch_size"] = 2

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1, p2, p3], output_dir=str(out_dir))
        assert job.total_pages == 3
        assert job.skipped_pages == 1
        assert job.pending_pages == 2

        b1, _ = mgr.next_translation_batch(job.job_id)
        # p1 is leased; p3 is textless so bypassed automatically!
        assert [pg.filename for pg in b1.pages] == ["p1.png"]
        sub1, _ = mgr.submit_translation_batch(
            job.job_id, b1.batch_id, [{"id": 1, "translation": "第一页译文"}], return_next=True
        )
        assert sub1.done is True
        assert (out_dir / "p1.png").exists()
        assert (out_dir / "p3.png").exists()
    finally:
        mgr.shutdown()

    # Now verify retranslate_json=True on p2 without re-running detection or OCR
    calls["detect"].clear()
    calls["ocr"].clear()
    cfg["cli"]["overwrite"] = True
    mgr2 = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job2 = mgr2.create_translation_job(
            inputs=[p2],
            output_dir=str(out_dir),
            retranslate_json=True,
        )
        b_re, _ = mgr2.next_translation_batch(job2.job_id)
        assert b_re.pages[0].regions[0].text == "二页跳过"
        sub_re, _ = mgr2.submit_translation_batch(
            job2.job_id, b_re.batch_id, [{"id": 1, "translation": "重翻二页"}], return_next=True
        )
        assert sub_re.done is True
        assert calls["detect"] == []
        assert calls["ocr"] == []
        updated_json = json.loads(p2_json.read_text(encoding="utf-8"))
        key = next(iter(updated_json))
        assert updated_json[key]["regions"][0]["translation"] == "重翻二页"
    finally:
        mgr2.shutdown()


def test_mcp_service_multi_client_snippets_merges_and_skill_targets(tmp_path, monkeypatch):
    import tomllib

    svc = McpService(root_dir=str(_bootstrap.ROOT))
    try:
        url = "http://127.0.0.1:9456/mcp"
        assert json.loads(svc.get_mcp_client_config_snippet("generic", port=9456)) == {
            "mcpServers": {"manga-translator": {"url": url}}
        }
        assert json.loads(svc.get_mcp_client_config_snippet("claude_code", port=9456)) == {
            "mcpServers": {"manga-translator": {"type": "http", "url": url}}
        }
        assert json.loads(svc.get_mcp_client_config_snippet("cursor", port=9456)) == {
            "mcpServers": {"manga-translator": {"url": url}}
        }
        assert json.loads(svc.get_mcp_client_config_snippet("opencode", port=9456)) == {
            "mcp": {"manga-translator": {"type": "remote", "url": url}}
        }
        assert json.loads(svc.get_mcp_client_config_snippet("antigravity", port=9456)) == {
            "mcpServers": {"manga-translator": {"url": url}}
        }
        assert json.loads(svc.get_mcp_client_config_snippet("windsurf", port=9456)) == {
            "mcpServers": {"manga-translator": {"serverUrl": url}}
        }
        assert json.loads(svc.get_mcp_client_config_snippet("vscode", port=9456)) == {
            "servers": {"manga-translator": {"type": "http", "url": url}}
        }
        assert json.loads(svc.get_mcp_client_config_snippet("stdio_bridge", port=9456)) == {
            "mcpServers": {
                "manga-translator": {"command": "npx", "args": ["-y", "mcp-remote", url]}
            }
        }
        codex_snippet = svc.get_mcp_client_config_snippet("codex", port=9456)
        assert tomllib.loads(codex_snippet) == {
            "mcp_servers": {"manga-translator": {"url": url}}
        }

        # Copy-only clients and vscode without path must raise ValueError
        with pytest.raises(ValueError):
            svc.install_mcp_config("generic")
        with pytest.raises(ValueError):
            svc.install_mcp_config("stdio_bridge")
        with pytest.raises(ValueError):
            svc.install_mcp_config("vscode")

        # JSONC (with comments & trailing comma) merge for OpenCode
        opencode_cfg = tmp_path / "opencode.json"
        opencode_cfg.write_text(
            '{\n  // user theme\n  "theme": "dark",\n  "mcp": {\n    "existing": {"type": "local", "command": ["echo"]},\n  },\n}\n',
            encoding="utf-8",
        )
        svc.install_mcp_config("opencode", opencode_cfg, port=9456)
        oc_parsed = json.loads(opencode_cfg.read_text(encoding="utf-8"))
        assert oc_parsed["theme"] == "dark"
        assert oc_parsed["mcp"]["existing"] == {"type": "local", "command": ["echo"]}
        assert oc_parsed["mcp"]["manga-translator"] == {"type": "remote", "url": url}

        # VS Code & Windsurf & Claude Code writes
        vscode_cfg = tmp_path / ".vscode" / "mcp.json"
        svc.install_mcp_config("vscode", vscode_cfg, port=9456)
        assert json.loads(vscode_cfg.read_text(encoding="utf-8")) == {
            "servers": {"manga-translator": {"type": "http", "url": url}}
        }
        windsurf_cfg = tmp_path / "windsurf_mcp.json"
        svc.install_mcp_config("windsurf", windsurf_cfg, port=9456)
        assert json.loads(windsurf_cfg.read_text(encoding="utf-8")) == {
            "mcpServers": {"manga-translator": {"serverUrl": url}}
        }

        # Codex TOML merge: append preserving comments and existing servers
        codex_cfg = tmp_path / ".codex" / "config.toml"
        codex_cfg.parent.mkdir(parents=True, exist_ok=True)
        codex_cfg.write_text(
            '# Top comment\nmodel = "o3"\n\n# Existing server\n[mcp_servers.other]\ncommand = "npx"\n',
            encoding="utf-8",
        )
        svc.install_mcp_config("codex", codex_cfg, port=9456)
        merged_1 = codex_cfg.read_text(encoding="utf-8")
        assert "# Top comment" in merged_1
        assert "# Existing server" in merged_1
        parsed_1 = tomllib.loads(merged_1)
        assert parsed_1["model"] == "o3"
        assert parsed_1["mcp_servers"]["other"] == {"command": "npx"}
        assert parsed_1["mcp_servers"]["manga-translator"] == {"url": url}

        # Codex TOML merge: update existing quoted table preserving extra keys & comments
        codex_cfg.write_text(
            '# Header\n[mcp_servers."manga-translator"]\n# keep this note\nurl = "http://127.0.0.1:8000/mcp"\nstartup_timeout_sec = 30\n\n[mcp_servers.other]\nurl = "http://127.0.0.1:9999/mcp"\n',
            encoding="utf-8",
        )
        svc.install_mcp_config("codex", codex_cfg, port=9777)
        merged_2 = codex_cfg.read_text(encoding="utf-8")
        assert "# Header" in merged_2
        assert "# keep this note" in merged_2
        parsed_2 = tomllib.loads(merged_2)
        assert parsed_2["mcp_servers"]["manga-translator"]["url"] == "http://127.0.0.1:9777/mcp"
        assert parsed_2["mcp_servers"]["manga-translator"]["startup_timeout_sec"] == 30
        assert parsed_2["mcp_servers"]["other"]["url"] == "http://127.0.0.1:9999/mcp"

        # Skill installation default target routing with monkeypatched home
        fake_home = tmp_path / "fake_home"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        std_path = Path(svc.install_skill("agents_standard"))
        assert std_path == fake_home / ".agents" / "skills" / "manga-translate" / "SKILL.md"
        assert std_path.exists()

        codex_skill = Path(svc.install_skill("codex"))
        assert codex_skill == fake_home / ".agents" / "skills" / "manga-translate" / "SKILL.md"

        claude_skill = Path(svc.install_skill("claude_code"))
        assert claude_skill == fake_home / ".claude" / "skills" / "manga-translate" / "SKILL.md"
        assert claude_skill.exists()
    finally:
        svc.shutdown()


def test_completed_job_closes_preview_pool_and_prunes_jobs(tmp_path):
    import threading

    out_dir = tmp_path / "out"
    img_path = _make_png(tmp_path / "p1.png")
    factory, _calls = _make_stub_factory({"p1.png": [_make_block("テスト")]})
    cfg = _base_config(str(out_dir))

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job_resp = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        internal_job = mgr._jobs[job_resp.job_id]
        batch, _ = mgr.next_translation_batch(job_resp.job_id)
        sub, _ = mgr.submit_translation_batch(
            job_resp.job_id,
            batch.batch_id,
            [{"id": 1, "translation": "测试"}],
            return_next=True,
        )
        assert sub.done is True

        # Wait briefly for async finish hook to prune _jobs and reap preview worker threads.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            preview_threads = [
                t for t in threading.enumerate()
                if t.is_alive() and t.name.startswith(f"mcp-preview-{job_resp.job_id[:6]}")
            ]
            if not preview_threads and job_resp.job_id not in mgr._jobs:
                break
            time.sleep(0.02)

        assert internal_job._preview_pool is None
        assert all(p.ctx is None and p.preview_cache is None for p in internal_job.pages)
        assert job_resp.job_id not in mgr._jobs
        lingering = [
            t for t in threading.enumerate()
            if t.is_alive() and t.name.startswith(f"mcp-preview-{job_resp.job_id[:6]}")
        ]
        assert lingering == []

        # Completed job summary remains queryable via get_job_status.
        status = mgr.get_job_status(job_resp.job_id)
        assert status["status"] == "completed"
        assert status["rendered_pages"] == 1
    finally:
        mgr.shutdown()


def test_concurrent_create_translation_job_enforces_gpu_mutex(tmp_path):
    import threading

    out_dir = tmp_path / "out"
    img_path = _make_png(tmp_path / "p1.png")
    factory, _calls = _make_stub_factory({"p1.png": [_make_block("并发")]})
    cfg = _base_config(str(out_dir))

    entered_expand = threading.Event()
    release_expand = threading.Event()

    def slow_expander(sources, ex_folders, ex_files, output_base, overwrite_extract):
        entered_expand.set()
        release_expand.wait(timeout=3.0)
        return list(sources), {s: None for s in sources}, {}

    mgr = McpJobManager(
        root_dir=str(_bootstrap.ROOT),
        get_config=lambda: cfg,
        file_expander=slow_expander,
        translator_factory=factory,
    )
    try:
        results = []
        errors = []

        def _create():
            try:
                results.append(mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir)))
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_create)
        t1.start()
        assert entered_expand.wait(timeout=2.0)
        assert mgr.has_active_job() is True

        t2 = threading.Thread(target=_create)
        t2.start()
        t2.join(timeout=2.0)

        release_expand.set()
        t1.join(timeout=5.0)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
    finally:
        mgr.shutdown()


def test_retranslate_json_defaults_to_overwrite_when_not_explicitly_set(tmp_path):
    img_path = _make_png(tmp_path / "p2.png")
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    _make_png(out_dir / "p2.png")

    p2_json = Path(get_json_path(img_path, create_dir=True))
    p2_json.write_text(
        json.dumps(
            {
                os.path.abspath(img_path): {
                    "regions": [
                        {
                            "lines": [[[10, 10], [80, 10], [80, 30], [10, 30]]],
                            "texts": ["原文"],
                            "text": "原文",
                            "translation": "旧译文",
                            "prob": 0.9,
                        }
                    ],
                    "original_width": 240,
                    "original_height": 320,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    factory, calls = _make_stub_factory({"p2.png": [_make_block("原文")]})
    cfg = _base_config(str(out_dir))
    cfg["cli"]["overwrite"] = False

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        # Default overwrite=None with retranslate_json=True must NOT skip existing output image.
        job = mgr.create_translation_job(
            inputs=[img_path],
            output_dir=str(out_dir),
            retranslate_json=True,
        )
        assert job.pending_pages == 1
        assert job.skipped_pages == 0
        batch, _ = mgr.next_translation_batch(job.job_id)
        sub, _ = mgr.submit_translation_batch(
            job.job_id,
            batch.batch_id,
            [{"id": 1, "translation": "新译文"}],
            return_next=True,
        )
        assert sub.done is True
        assert calls["detect"] == []

        # Explicit overwrite=False is still respected.
        job_skip = mgr.create_translation_job(
            inputs=[img_path],
            output_dir=str(out_dir),
            retranslate_json=True,
            overwrite=False,
        )
        assert job_skip.pending_pages == 0
        assert job_skip.skipped_pages == 1
    finally:
        mgr.shutdown()


def test_finalizing_render_timeout_returns_waiting_not_done(tmp_path):
    import threading

    out_dir = tmp_path / "out"
    img_path = _make_png(tmp_path / "p1.png")
    render_entered = threading.Event()
    allow_render_finish = threading.Event()

    base_factory, _calls = _make_stub_factory({"p1.png": [_make_block("慢渲染")]})

    def slow_render_factory(params):
        translator = base_factory(params)
        orig_render = translator._run_text_rendering

        async def _slow_render(config, ctx, skip_font_scaling: bool = False, **_kwargs):
            render_entered.set()
            allow_render_finish.wait(timeout=5.0)
            return await orig_render(config, ctx, skip_font_scaling=skip_font_scaling)

        translator._run_text_rendering = _slow_render
        return translator

    cfg = _base_config(str(out_dir))
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=slow_render_factory)
    try:
        job = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        internal_job = mgr._jobs[job.job_id]
        internal_job._finalize_wait_timeout = 0.08

        batch, _ = mgr.next_translation_batch(job.job_id)
        sub, _ = mgr.submit_translation_batch(
            job.job_id,
            batch.batch_id,
            [{"id": 1, "translation": "慢渲染译文"}],
            return_next=True,
        )
        assert sub.status == "accepted"
        assert sub.done is False
        assert sub.next_batch is not None
        assert sub.next_batch.done is False
        assert sub.next_batch.waiting is True
        assert sub.next_batch.waiting_reason == "finalizing_render"

        allow_render_finish.set()
        internal_job._finalize_wait_timeout = 5.0
        final_payload, _ = mgr.next_translation_batch(job.job_id)
        assert final_payload.done is True
        assert final_payload.summary["rendered_pages"] == 1
    finally:
        allow_render_finish.set()
        mgr.shutdown()


def test_p2_inpaint_failure_during_lease_releases_ctx_and_converges_batch(tmp_path):
    out_dir = tmp_path / "out"
    img_path = _make_png(tmp_path / "p1.png")
    base_factory, _calls = _make_stub_factory({"p1.png": [_make_block("预修失败")]})

    def failing_inpaint_factory(params):
        translator = base_factory(params)

        async def _boom_inpaint(config, ctx):
            raise RuntimeError("simulated P2 inpaint OOM")

        translator._run_inpainting = _boom_inpaint
        return translator

    cfg = _base_config(str(out_dir))
    cfg["cli"]["batch_concurrent"] = True
    mgr = McpJobManager(
        root_dir=str(_bootstrap.ROOT),
        get_config=lambda: cfg,
        translator_factory=failing_inpaint_factory,
    )
    try:
        job = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        internal_job = mgr._jobs[job.job_id]
        batch, _ = mgr.next_translation_batch(job.job_id)

        # Wait for speculative P2 inpaint to fail while batch is still LEASED.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not internal_job.pages[0].failed:
            time.sleep(0.02)
        assert internal_job.pages[0].failed is True

        sub, _ = mgr.submit_translation_batch(
            job.job_id,
            batch.batch_id,
            [{"id": 1, "translation": "译文"}],
            return_next=True,
        )
        assert sub.done is True
        assert internal_job.pages[0].ctx is None
        assert internal_job._batches[batch.batch_id].status == "FAILED"
        assert internal_job.status == "failed"
    finally:
        mgr.shutdown()


def test_ipv6_endpoint_formatting_and_codex_inline_table_error(tmp_path):
    from services.mcp_service import _merge_codex_toml_config

    class _FakeConfigService:
        def get_config(self):
            return SimpleNamespace(
                mcp=SimpleNamespace(host="::1", port=8765, auto_start=False),
                app=SimpleNamespace(last_output_path=str(tmp_path)),
            )

    svc = McpService(root_dir=str(_bootstrap.ROOT), config_service=_FakeConfigService())
    try:
        assert svc.endpoint_url() == "http://[::1]:8765/mcp"
        snippet = json.loads(svc.get_mcp_client_config_snippet("generic", port=9001))
        assert snippet["mcpServers"]["manga-translator"]["url"] == "http://[::1]:9001/mcp"
    finally:
        svc.shutdown()

    inline_toml = 'mcp_servers = { other = { command = "npx" } }\n'
    with pytest.raises(ValueError, match="inline table"):
        _merge_codex_toml_config(inline_toml, "http://127.0.0.1:8765/mcp")


def test_hq_preview_error_reported_in_batch_payload(tmp_path, monkeypatch):
    """R2-1: HQ preview render failure must surface as preview_error + hints, not silent 'none'."""
    import manga_translator.mcp.job_manager as job_manager_module
    from manga_translator.mcp.batch_adapter import render_numbered_page_preview as _orig_render

    p1 = _make_png(tmp_path / "01.png")
    p2 = _make_png(tmp_path / "02.png")
    factory, _calls = _make_stub_factory(
        {
            "01.png": [_make_block("一页原文")],
            "02.png": [_make_block("二页原文")],
        }
    )

    def _maybe_fail_render(ctx, text_order, **kwargs):
        fname = os.path.basename(str(getattr(ctx, "image_name", "") or ""))
        if fname == "01.png":
            raise RuntimeError("simulated preview render crash")
        return _orig_render(ctx, text_order, **kwargs)

    monkeypatch.setattr(job_manager_module, "render_numbered_page_preview", _maybe_fail_render)

    out_dir = tmp_path / "out"
    cfg = _base_config(str(out_dir))
    cfg["cli"]["batch_size"] = 1

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1, p2], output_dir=str(out_dir))

        b1, inline1 = mgr.next_translation_batch(job.job_id)
        assert b1.pages[0].filename == "01.png"
        assert b1.pages[0].image_delivery_mode == "none"
        assert b1.pages[0].preview_error is not None
        assert "simulated preview render crash" in b1.pages[0].preview_error
        assert inline1 == []
        assert any(
            "Preview rendering failed for pages" in h and "01.png" in h for h in b1.hints
        )

        sub1, inline2 = mgr.submit_translation_batch(
            job.job_id,
            b1.batch_id,
            [{"id": 1, "translation": "第一页"}],
            return_next=True,
        )
        assert sub1.status == "accepted"
        b2 = sub1.next_batch
        assert b2 is not None and not b2.done
        assert b2.pages[0].filename == "02.png"
        assert b2.pages[0].preview_error is None
        assert b2.pages[0].image_delivery_mode == "mcp_image"
        assert len(inline2) == 1  # healthy page still delivers its preview inline
        assert all("Preview rendering failed" not in h for h in b2.hints)

        done_resp, _ = mgr.submit_translation_batch(
            job.job_id,
            b2.batch_id,
            [{"id": 1, "translation": "第二页"}],
            return_next=True,
        )
        assert done_resp.done is True
        assert done_resp.summary["rendered_pages"] == 2
        assert done_resp.summary["failed_pages"] == 0  # preview error ≠ page failure
    finally:
        mgr.shutdown()


@pytest.mark.parametrize("render_raises", [False, True])
def test_finalize_before_preview_writeback_is_ignored(tmp_path, monkeypatch, render_raises):
    """R2-3: a preview thread blocked in render must not resurrect buffers after finalize."""
    import threading

    import manga_translator.mcp.job_manager as job_manager_module
    from manga_translator.mcp.batch_adapter import render_numbered_page_preview as _orig_render

    out_dir = tmp_path / "out"
    img_path = _make_png(tmp_path / "p1.png")
    factory, _calls = _make_stub_factory({"p1.png": [_make_block("竞态防护")]})

    render_entered = threading.Event()
    allow_proceed = threading.Event()

    def _blocked_render(ctx, text_order, **kwargs):
        render_entered.set()
        allow_proceed.wait(timeout=5.0)
        if render_raises:
            raise RuntimeError("simulated preview crash")
        return _orig_render(ctx, text_order, **kwargs)

    monkeypatch.setattr(job_manager_module, "render_numbered_page_preview", _blocked_render)

    cfg = _base_config(str(out_dir))
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        internal_job = mgr._jobs[job.job_id]
        pool_prefix = f"mcp-preview-{job.job_id[:6]}"

        # Pool thread passed its final-state snapshot check and is parked inside render.
        assert render_entered.wait(timeout=10.0)

        # Finalize (via cancel) while the preview thread is still inside render.
        status = mgr.cancel_job(job.job_id)
        assert status["status"] == "cancelled"

        allow_proceed.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not any(t.is_alive() and t.name.startswith(pool_prefix) for t in threading.enumerate()):
                break
            time.sleep(0.02)

        page = internal_job.pages[0]
        assert page.preview_cache is None
        assert page.preview_error is None
        assert page.preview_ready.is_set() is True
        assert page.ctx is None  # finalize already released page buffers
    finally:
        allow_proceed.set()
        mgr.shutdown()


def test_next_translation_batch_cancel_wakeup_returns_done_without_fallback(tmp_path, monkeypatch):
    """R2-3: cancel/finalize waking preview_ready.wait() must yield done=True, no fallback render."""
    import threading

    import manga_translator.mcp.job_manager as job_manager_module
    from manga_translator.mcp.batch_adapter import render_numbered_page_preview as _orig_render

    out_dir = tmp_path / "out"
    img_path = _make_png(tmp_path / "p1.png")
    factory, _calls = _make_stub_factory({"p1.png": [_make_block("取消唤醒")]})

    render_entered = threading.Event()
    allow_proceed = threading.Event()
    render_calls = []

    def _blocked_render(ctx, text_order, **kwargs):
        render_calls.append(1)
        render_entered.set()
        allow_proceed.wait(timeout=5.0)
        return _orig_render(ctx, text_order, **kwargs)

    monkeypatch.setattr(job_manager_module, "render_numbered_page_preview", _blocked_render)

    cfg = _base_config(str(out_dir))
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))

        holder = {}

        def _lease():
            payload, inline = mgr.next_translation_batch(job.job_id)
            holder["payload"] = payload
            holder["inline"] = inline

        t = threading.Thread(target=_lease, daemon=True)
        t.start()
        assert render_entered.wait(timeout=10.0)
        time.sleep(0.1)  # let the leasing thread park in preview_ready.wait()

        status = mgr.cancel_job(job.job_id)
        assert status["status"] == "cancelled"

        t.join(timeout=5.0)
        assert not t.is_alive()
        payload = holder["payload"]
        assert payload.done is True
        assert payload.batch_id is None
        assert payload.summary["status"] == "cancelled"
        assert len(render_calls) == 1  # no sync fallback render after cancel wakeup

        allow_proceed.set()
    finally:
        allow_proceed.set()
        mgr.shutdown()


def test_next_and_submit_translation_batch_on_completed_and_all_skipped_jobs(tmp_path):
    """R3-1a & R3-1b: all-skipped and pruned completed jobs return done=True instead of KeyError."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # R3-1a: pending_pages == 0 (all pages skipped)
    skipped_img = _make_png(tmp_path / "p1.png")
    _make_png(out_dir / "p1.png")
    cfg_skip = _base_config(str(out_dir))
    cfg_skip["cli"]["overwrite"] = False
    factory_skip, _ = _make_stub_factory({"p1.png": [_make_block("跳过")]})

    mgr_skip = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg_skip, translator_factory=factory_skip)
    try:
        job_skip = mgr_skip.create_translation_job(inputs=[skipped_img], output_dir=str(out_dir))
        assert job_skip.pending_pages == 0
        assert job_skip.skipped_pages == 1

        payload, inline = mgr_skip.next_translation_batch(job_skip.job_id)
        assert payload.done is True
        assert payload.job_id == job_skip.job_id
        assert payload.progress.skipped_pages == 1
        assert payload.summary is not None
        assert payload.summary["skipped_pages"] == 1
        assert inline == []

        sub_err, sub_inline = mgr_skip.submit_translation_batch(job_skip.job_id, "batch_0001", [])
        assert sub_err.status == "error"
        assert sub_err.done is True
        assert sub_inline == []
    finally:
        mgr_skip.shutdown()

    # R3-1b: completed job pruned from _jobs into _completed_job_summaries
    done_img = _make_png(tmp_path / "p2.png")
    cfg_done = _base_config(str(out_dir))
    factory_done, _ = _make_stub_factory({"p2.png": [_make_block("完成")]})

    mgr_done = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg_done, translator_factory=factory_done)
    try:
        job_done = mgr_done.create_translation_job(inputs=[done_img], output_dir=str(out_dir))
        batch, _ = mgr_done.next_translation_batch(job_done.job_id)
        sub, _ = mgr_done.submit_translation_batch(
            job_done.job_id,
            batch.batch_id,
            [{"id": 1, "translation": "已完成译文"}],
            return_next=True,
        )
        assert sub.done is True

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and job_done.job_id in mgr_done._jobs:
            time.sleep(0.02)
        assert job_done.job_id not in mgr_done._jobs

        payload_after, inline_after = mgr_done.next_translation_batch(job_done.job_id)
        assert payload_after.done is True
        assert payload_after.summary is not None
        assert payload_after.summary["rendered_pages"] == 1
        assert inline_after == []

        sub_after, sub_after_inline = mgr_done.submit_translation_batch(
            job_done.job_id,
            batch.batch_id,
            [{"id": 1, "translation": "重试"}],
        )
        assert sub_after.status == "error"
        assert sub_after.done is True
        assert sub_after_inline == []
    finally:
        mgr_done.shutdown()


def test_segment_end_textless_page_seals_building_batch_across_skipped_gap(tmp_path):
    """R3-2: a textless page at segment end must seal the preceding building batch across skipped gaps."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    p1 = _make_png(tmp_path / "01.png")
    p2 = _make_png(tmp_path / "02.png")
    p3 = _make_png(tmp_path / "03.png")
    p4 = _make_png(tmp_path / "04.png")
    _make_png(out_dir / "03.png")  # pre-existing output -> skipped
    p3_json = Path(get_json_path(p3, create_dir=True))
    p3_json.write_text(
        json.dumps(
            {
                os.path.abspath(p3): {
                    "regions": [
                        {
                            "lines": [[[10, 10], [80, 10], [80, 30], [10, 30]]],
                            "texts": ["三页跳过"],
                            "text": "三页跳过",
                            "translation": "已有第三页译文",
                            "prob": 0.9,
                        }
                    ],
                    "original_width": 240,
                    "original_height": 320,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    factory, _ = _make_stub_factory(
        {
            "01.png": [_make_block("一页")],
            "02.png": [],
            "04.png": [_make_block("四页")],
        }
    )
    cfg = _base_config(str(out_dir))
    cfg["cli"]["overwrite"] = False
    cfg["cli"]["batch_size"] = 3

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1, p2, p3, p4], output_dir=str(out_dir))
        assert job.pending_pages == 3
        assert job.skipped_pages == 1
        internal_job = mgr._jobs[job.job_id]
        assert internal_job._segments == [[0, 1], [2]]

        b1, _ = mgr.next_translation_batch(job.job_id)
        assert [p.filename for p in b1.pages] == ["01.png"]

        sub1, _ = mgr.submit_translation_batch(
            job.job_id,
            b1.batch_id,
            [{"id": 1, "translation": "第一页"}],
            return_next=True,
        )
        b2 = sub1.next_batch
        assert b2 is not None and not b2.done
        assert [p.filename for p in b2.pages] == ["04.png"]

        done_resp, _ = mgr.submit_translation_batch(
            job.job_id,
            b2.batch_id,
            [{"id": 1, "translation": "第四页"}],
            return_next=True,
        )
        assert done_resp.done is True
        assert done_resp.summary["rendered_pages"] == 3
        assert done_resp.summary["textless_pages"] == 1
    finally:
        mgr.shutdown()


def test_retranslate_json_applies_pre_dict_and_protects_corrupt_regions_on_save(tmp_path):
    """R3-3: retranslate_json applies pre_dict, fills font_size, and blocks JSON write-back on parse failures."""
    import asyncio
    from manga_translator.mcp.batch_adapter import preprocess_page, render_and_save_page

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    img_path = _make_png(tmp_path / "p1.png")

    pre_dict_path = tmp_path / "pre_dict.txt"
    pre_dict_path.write_text("原文A 预替换A\n", encoding="utf-8")

    json_path = Path(get_json_path(img_path, create_dir=True))
    original_json_obj = {
        os.path.abspath(img_path): {
            "regions": [
                {
                    "lines": [[[10, 10], [80, 10], [80, 30], [10, 30]]],
                    "texts": ["原文A"],
                    "text": "原文A",
                    "translation": "旧译文",
                    "font_size": 0,
                    "prob": 0.9,
                },
                {
                    "lines": "invalid_not_a_list",
                    "texts": ["损坏区域"],
                    "text": "损坏区域",
                },
            ],
            "original_width": 240,
            "original_height": 320,
        }
    }
    original_json_text = json.dumps(original_json_obj, ensure_ascii=False, indent=2)
    json_path.write_text(original_json_text, encoding="utf-8")

    cfg = _base_config(str(out_dir))
    cfg["cli"]["pre_dict"] = str(pre_dict_path)
    cfg["cli"]["save_text"] = True
    engine_config, translator_params = build_mcp_engine_config(cfg, str(_bootstrap.ROOT))
    factory, _ = _make_stub_factory({"p1.png": [_make_block("原文A")]})
    translator = factory(translator_params)
    translator.save_text = True

    ctx = asyncio.run(preprocess_page(translator, img_path, engine_config, retranslate_json=True))
    assert len(ctx.text_regions) == 1
    assert ctx.text_regions[0].text == "预替换A"
    assert ctx.text_regions[0].translation == ""
    assert ctx.text_regions[0].font_size > 0
    assert ctx.load_text_parse_failures == 1

    ctx.text_regions[0].translation = "新译文"
    ctx.img_inpainted = ctx.img_rgb.copy()
    save_info = {
        "output_folder": str(out_dir),
        "format": "png",
        "overwrite": True,
        "input_folders": set(),
        "save_to_source_dir": False,
    }
    res = asyncio.run(render_and_save_page(translator, engine_config, ctx, save_info))
    assert res["success"] is True
    assert json_path.read_text(encoding="utf-8") == original_json_text


def _encode_png_b64(arr: np.ndarray) -> str:
    import base64
    import cv2

    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return base64.b64encode(buf.tobytes()).decode("ascii")


def test_retranslate_json_real_mask_refinement_and_overlays(tmp_path):
    """R4-1: retranslate_json works with real dispatch_mask_refinement (no mask & refined pending_redo) + overlays."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Subcase A: JSON without mask_raw + paint_overlay isolation across pages
    p1 = _make_png(tmp_path / "p1.png", size=(240, 320), color=(245, 245, 245))
    p2 = _make_png(tmp_path / "p2.png", size=(240, 320), color=(245, 245, 245))

    paint_bgra = np.zeros((320, 240, 4), dtype=np.uint8)
    paint_bgra[10:30, 10:30] = [0, 0, 255, 255]  # BGRA -> red (255, 0, 0) in RGB

    Path(get_json_path(p1, create_dir=True)).write_text(
        json.dumps(
            {
                os.path.abspath(p1): {
                    "regions": [
                        {
                            "lines": [[[40, 80], [140, 80], [140, 120], [40, 120]]],
                            "texts": ["原文1"],
                            "text": "原文1",
                            "translation": "旧1",
                            "prob": 0.9,
                        }
                    ],
                    "paint_overlay": _encode_png_b64(paint_bgra),
                    "original_width": 240,
                    "original_height": 320,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    Path(get_json_path(p2, create_dir=True)).write_text(
        json.dumps(
            {
                os.path.abspath(p2): {
                    "regions": [
                        {
                            "lines": [[[40, 80], [140, 80], [140, 120], [40, 120]]],
                            "texts": ["原文2"],
                            "text": "原文2",
                            "translation": "旧2",
                            "prob": 0.9,
                        }
                    ],
                    "original_width": 240,
                    "original_height": 320,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    inpaint_masks = []

    def real_mask_factory(params):
        translator = MangaTranslator(params=params)
        translator._mcp_context_enabled = True
        translator._models_loaded = True
        translator.start_model_cleanup_job = False

        async def _inpaint(config, ctx):
            fname = os.path.basename(getattr(ctx, "image_name", "") or "")
            assert ctx.mask is not None
            assert ctx.mask.ndim == 2 and ctx.mask.dtype == np.uint8
            inpaint_masks.append((fname, ctx.mask.copy()))
            return ctx.img_rgb.copy()

        async def _render(config, ctx, **_kwargs):
            return ctx.img_inpainted.copy()

        # Do NOT mock _run_mask_refinement so real dispatch_mask_refinement executes.
        translator._run_inpainting = _inpaint
        translator._run_text_rendering = _render
        return translator

    cfg = _base_config(str(out_dir))
    cfg["cli"]["batch_size"] = 2
    cfg["cli"]["save_text"] = True
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=real_mask_factory)
    try:
        job = mgr.create_translation_job(inputs=[p1, p2], output_dir=str(out_dir), retranslate_json=True)
        batch, _ = mgr.next_translation_batch(job.job_id)
        sub, _ = mgr.submit_translation_batch(
            job.job_id,
            batch.batch_id,
            [{"id": 1, "translation": "新1"}, {"id": 2, "translation": "新2"}],
            return_next=True,
        )
        assert sub.done is True
        assert sub.summary["failed_pages"] == 0
        assert len(inpaint_masks) == 2
        assert all(np.any(mask) for _fn, mask in inpaint_masks)

        out1 = np.array(Image.open(out_dir / "p1.png").convert("RGB"))
        out2 = np.array(Image.open(out_dir / "p2.png").convert("RGB"))
        inp1 = np.array(Image.open(get_inpainted_path(p1, create_dir=False)).convert("RGB"))
        assert tuple(out1[20, 20]) == (255, 0, 0)
        assert tuple(inp1[20, 20]) == (245, 245, 245)  # overlay not baked into inpainted sidecar
        assert tuple(out2[20, 20]) == (245, 245, 245)  # overlay did not leak to p2
    finally:
        mgr.shutdown()

    # Subcase B: mask_is_refined=True + pending_redo runs real dispatch_mask_refinement without crash
    inpaint_masks.clear()
    p3 = _make_png(tmp_path / "p3.png", size=(240, 320), color=(245, 245, 245))
    refined_mask = np.zeros((320, 240), dtype=np.uint8)
    refined_mask[20:44, 20:100] = 255
    refined_mask[140:164, 20:100] = 255

    Path(get_json_path(p3, create_dir=True)).write_text(
        json.dumps(
            {
                os.path.abspath(p3): {
                    "regions": [
                        {
                            "lines": [[[20, 20], [100, 20], [100, 44], [20, 44]]],
                            "texts": ["保留词"],
                            "text": "保留词",
                            "translation": "旧保留",
                            "prob": 0.9,
                        },
                        {
                            "lines": [[[20, 140], [100, 140], [100, 164], [20, 164]]],
                            "texts": ["消す言葉"],
                            "text": "消す言葉",
                            "translation": "旧过滤",
                            "prob": 0.9,
                        },
                    ],
                    "mask_raw": _encode_png_b64(refined_mask),
                    "mask_is_refined": True,
                    "original_width": 240,
                    "original_height": 320,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mgr2 = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=real_mask_factory)
    try:
        job2 = mgr2.create_translation_job(inputs=[p3], output_dir=str(out_dir), retranslate_json=True)
        b3, _ = mgr2.next_translation_batch(job2.job_id)
        sub3, _ = mgr2.submit_translation_batch(
            job2.job_id,
            b3.batch_id,
            [
                {"id": 1, "translation": "新保留"},
                {"id": 2, "translation": "消す言葉"},  # filtered out -> pending_redo
            ],
            return_next=True,
        )
        assert sub3.redo_pages == [0]
        assert sub3.done is True
        assert sub3.summary["failed_pages"] == 0
        assert len(inpaint_masks) >= 1
        final_mask = inpaint_masks[-1][1]
        assert final_mask[32, 60] > 0
        assert final_mask[152, 60] == 0
    finally:
        mgr2.shutdown()


def test_br_validation_accepts_self_closing_br_and_caps_retries(tmp_path):
    """R4-2: <br/> and <br /> pass [BR] check, retry_needed never returns empty errors, and retries cap."""
    out_dir = tmp_path / "out"
    img_path = _make_png(tmp_path / "p1.png")
    regions_map = {
        "p1.png": [
            _make_block("一行", num_lines=1, y=20),
            _make_block("二行A", num_lines=2, y=60),
            _make_block("二行B", num_lines=2, y=130),
        ]
    }
    factory, _calls = _make_stub_factory(regions_map)

    cfg = _base_config(str(out_dir))
    cfg["render"]["disable_auto_wrap"] = True
    cfg["render"]["check_br_and_retry"] = True
    cfg["translator"]["post_check_max_retry_attempts"] = 2

    # 1) <br/> / <br /> pass directly and single-region extra <br/> is stripped
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        batch, _ = mgr.next_translation_batch(job.job_id)
        sub, _ = mgr.submit_translation_batch(
            job.job_id,
            batch.batch_id,
            [
                {"id": 1, "translation": "第一<br/>句"},
                {"id": 2, "translation": "第二<br/>行"},
                {"id": 3, "translation": "第三<br />行"},
            ],
            return_next=True,
        )
        assert sub.status == "accepted"
        assert sub.done is True
        saved = json.loads(Path(get_json_path(img_path, create_dir=False)).read_text(encoding="utf-8"))
        saved_regions = next(iter(saved.values()))["regions"]
        assert saved_regions[0]["translation"] == "第一句"
    finally:
        mgr.shutdown()

    # 2) Missing [BR] retries up to max_br_retries (2), never with errors=[], then accepts on attempt 3
    mgr2 = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job2 = mgr2.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        batch2, _ = mgr2.next_translation_batch(job2.job_id)
        payload = [
            {"id": 1, "translation": "单行[BR]清理"},
            {"id": 2, "translation": "缺断句A"},
            {"id": 3, "translation": "缺断句B"},
        ]
        r1, _ = mgr2.submit_translation_batch(job2.job_id, batch2.batch_id, payload, return_next=True)
        assert r1.status == "retry_needed"
        assert len(r1.errors) > 0

        r2, _ = mgr2.submit_translation_batch(job2.job_id, batch2.batch_id, payload, return_next=True)
        assert r2.status == "retry_needed"
        assert len(r2.errors) > 0

        r3, _ = mgr2.submit_translation_batch(job2.job_id, batch2.batch_id, payload, return_next=True)
        assert r3.status == "accepted"
        assert r3.done is True
        saved2 = json.loads(Path(get_json_path(img_path, create_dir=False)).read_text(encoding="utf-8"))
        assert next(iter(saved2.values()))["regions"][0]["translation"] == "单行清理"
    finally:
        mgr2.shutdown()


def test_submit_cancel_release_preview_concurrency_guards(tmp_path, monkeypatch):
    """R4-3: release_batch blocks submitting, cancel aborts commit/glossary, preview dirs clean up, committed blocks preview."""
    import threading
    import manga_translator.mcp.job_manager as job_manager_module

    out_dir = tmp_path / "out"
    p1 = _make_png(tmp_path / "p1.png")
    prompt_file = tmp_path / "prompt.yaml"
    prompt_file.write_text(
        yaml.safe_dump({"system_prompt": "Translate", "glossary": {}}, allow_unicode=True),
        encoding="utf-8",
    )

    cfg = _base_config(str(out_dir))
    cfg["translator"]["extract_glossary"] = True
    cfg["translator"]["high_quality_prompt_path"] = str(prompt_file)
    factory, _ = _make_stub_factory({"p1.png": [_make_block("并发取消")]})

    entered_post = threading.Event()
    allow_post = threading.Event()
    orig_post = job_manager_module.apply_batch_post_processing

    async def _slow_post(*args, **kwargs):
        entered_post.set()
        allow_post.wait(timeout=5.0)
        return await orig_post(*args, **kwargs)

    monkeypatch.setattr(job_manager_module, "apply_batch_post_processing", _slow_post)

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        internal_job = mgr._jobs[job.job_id]
        batch, _ = mgr.next_translation_batch(job.job_id)

        submit_holder = {}

        def _submit():
            resp, _imgs = mgr.submit_translation_batch(
                job.job_id,
                batch.batch_id,
                [{"id": 1, "translation": "译文"}],
                new_terms=[
                    {
                        "category": "Item",
                        "original": "CancelTerm",
                        "aliases": [{"original": "CancelTerm", "translations": [{"text": "取消词"}]}],
                    }
                ],
                return_next=False,
            )
            submit_holder["resp"] = resp

        t_sub = threading.Thread(target=_submit, daemon=True)
        t_sub.start()
        assert entered_post.wait(timeout=5.0)

        rel = mgr.release_batch(job.job_id, batch.batch_id)
        assert rel["released"] is False
        assert "currently being submitted" in rel["message"]

        cancel_holder = {}
        t_cancel = threading.Thread(
            target=lambda: cancel_holder.setdefault("status", mgr.cancel_job(job.job_id)),
            daemon=True,
        )
        t_cancel.start()
        time.sleep(0.1)
        allow_post.set()

        t_sub.join(timeout=5.0)
        t_cancel.join(timeout=5.0)
        assert not t_sub.is_alive() and not t_cancel.is_alive()
        assert submit_holder["resp"].status == "error"
        assert submit_holder["resp"].done is True
        assert internal_job.pages[0].committed is False
        parsed_prompt = yaml.safe_load(prompt_file.read_text(encoding="utf-8"))
        assert parsed_prompt["glossary"] == {}
    finally:
        allow_post.set()
        monkeypatch.setattr(job_manager_module, "apply_batch_post_processing", orig_post)
        mgr.shutdown()

    # Check committed page blocks get_page_preview and file_path preview dir is cleaned up if cancelled mid-delivery
    cfg2 = _base_config(str(out_dir), image_delivery="file_path")
    orig_delivery = job_manager_module.resolve_batch_image_delivery
    delivery_written = threading.Event()
    allow_delivery_return = threading.Event()

    def _wrapped_delivery(*args, **kwargs):
        res = orig_delivery(*args, **kwargs)
        delivery_written.set()
        allow_delivery_return.wait(timeout=5.0)
        return res

    mgr2 = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg2, translator_factory=factory)
    try:
        # Cancel mid-next_translation_batch after preview file is written
        monkeypatch.setattr(job_manager_module, "resolve_batch_image_delivery", _wrapped_delivery)
        job_d = mgr2.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        preview_dir = out_dir / "manga_translator_work" / "mcp_preview" / job_d.job_id
        next_holder = {}

        def _next():
            next_holder["res"] = mgr2.next_translation_batch(job_d.job_id)

        t_next = threading.Thread(target=_next, daemon=True)
        t_next.start()
        assert delivery_written.wait(timeout=5.0)
        assert preview_dir.exists()
        mgr2.cancel_job(job_d.job_id)
        allow_delivery_return.set()
        t_next.join(timeout=5.0)

        payload_d, imgs_d = next_holder["res"]
        assert payload_d.done is True
        assert payload_d.summary["status"] == "cancelled"
        assert imgs_d == []
        assert not preview_dir.exists()
    finally:
        allow_delivery_return.set()
        mgr2.shutdown()


def test_delayed_finish_cancel_idle_dedup_and_all_skipped_ui_progress(tmp_path, monkeypatch):
    """R4-4 & R3-4: models_idle_ttl=0 disables auto-unload, positive TTL deduplicates, delayed finish won't disturb new job, 0-pending emits UI progress."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    p1 = _make_png(tmp_path / "p1.png")
    factory, _ = _make_stub_factory({"p1.png": [_make_block("测试")]})

    # 1) models_idle_ttl=0 + unload_models_after_translation=True must NOT auto-unload models
    cfg_zero = _base_config(str(out_dir), models_idle_ttl=0)
    cfg_zero["app"]["unload_models_after_translation"] = True
    mgr_zero = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg_zero, translator_factory=factory)
    unload_zero = []
    monkeypatch.setattr(mgr_zero, "unload_models", lambda: unload_zero.append(1) or {"ok": True})
    try:
        j0 = mgr_zero.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        mgr_zero.next_translation_batch(j0.job_id)
        mgr_zero.cancel_job(j0.job_id)
        time.sleep(0.15)
        assert unload_zero == []
    finally:
        mgr_zero.shutdown()

    # 2) Positive models_idle_ttl + cancel_job + delayed _handle_job_finished deduplicates unload (at most once)
    cfg = _base_config(str(out_dir), models_idle_ttl=0.05)
    cfg["app"]["unload_models_after_translation"] = True
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    unload_calls = []
    monkeypatch.setattr(mgr, "unload_models", lambda: unload_calls.append(1) or {"ok": True})
    try:
        j1 = mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        mgr.next_translation_batch(j1.job_id)
        mgr.cancel_job(j1.job_id)
        time.sleep(0.2)
        assert len(unload_calls) == 1

        # Delayed _handle_job_finished from old job while a newer job is active must not clobber or unload
        j2 = mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        old_fake_job = SimpleNamespace(
            job_id=j1.job_id,
            pages=[1],
            completed_output_sources={},
            get_status_dict=lambda: {"job_id": j1.job_id, "status": "cancelled"},
        )
        assert mgr.should_update_ui_for_job(j1.job_id) is False
        assert mgr.should_update_ui_for_job(j2.job_id) is True
        mgr._handle_job_finished(old_fake_job)
        time.sleep(0.15)
        assert mgr._active_job_id == j2.job_id
        assert len(unload_calls) == 1  # untouched while j2 is active
        mgr.cancel_job(j2.job_id)
        time.sleep(0.15)
        assert len(unload_calls) == 2
    finally:
        mgr.shutdown()

    # 3) All-skipped (pending_pages == 0) via McpService emits job_progress_changed and job_finished
    _make_png(out_dir / "p1.png")
    cfg_skip = _base_config(str(out_dir))
    cfg_skip["cli"]["overwrite"] = False
    svc = McpService(root_dir=str(_bootstrap.ROOT), config_service=SimpleNamespace(get_config=lambda: cfg_skip))
    svc.job_manager._translator_factory = factory
    progress_events = []
    finished_events = []
    svc.job_progress_changed.connect(lambda d: progress_events.append(d))
    svc.job_finished.connect(lambda d: finished_events.append(d))
    try:
        job_skip = svc.job_manager.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        assert job_skip.pending_pages == 0
        assert job_skip.skipped_pages == 1
        assert len(progress_events) == 1
        assert progress_events[0]["job_id"] == job_skip.job_id
        assert progress_events[0]["skipped_pages"] == 1
        assert len(finished_events) == 1
        assert finished_events[0]["job_id"] == job_skip.job_id
    finally:
        svc.shutdown()


def test_archive_release_fallback_render_batch_status_and_edge_fixes(tmp_path, monkeypatch):
    """R4-5, R4-6, R4-7: archived release_batch/preview fallback, render FAILED status + int page_index, output_dir & archive cleanup, async cancel."""
    import threading
    import manga_translator.mcp.job_manager as job_manager_module
    import manga_translator.utils.text_filter as text_filter_module
    import desktop_qt_ui.utils.archive_extractor as archive_extractor_module

    out_dir = tmp_path / "explicit_out"
    p1 = _make_png(tmp_path / "p1.png")
    factory, _ = _make_stub_factory({"p1.png": [_make_block("边界测试")]})

    # 1) Explicit output_dir overrides cli.save_to_source_dir=True & filter_list refresh
    filter_reloads = []
    monkeypatch.setattr(
        text_filter_module,
        "load_filter_list",
        lambda force_reload=False: filter_reloads.append(force_reload) or ([], []),
    )
    cfg = _base_config(str(out_dir))
    cfg["cli"]["save_to_source_dir"] = True
    cfg["filter_text_enabled"] = True

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        internal = mgr._jobs[job.job_id]
        assert internal.save_info["save_to_source_dir"] is False
        assert filter_reloads == [True]
        b1, _ = mgr.next_translation_batch(job.job_id)
        sub, _ = mgr.submit_translation_batch(
            job.job_id, b1.batch_id, [{"id": 1, "translation": "输出测试"}], return_next=True
        )
        assert sub.done is True
        assert (out_dir / "p1.png").exists()
        assert not (tmp_path / "manga_translator_work" / "result" / "p1.png").exists()

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and job.job_id in mgr._jobs:
            time.sleep(0.02)

        # R4-5: archived release_batch returns released=False (no KeyError), get_page_preview raises RuntimeError
        rel = mgr.release_batch(job.job_id, b1.batch_id)
        assert rel["released"] is False
        assert rel["status"] == "completed"
        with pytest.raises(RuntimeError, match="released"):
            mgr.get_page_preview(job.job_id, 0)
    finally:
        mgr.shutdown()

    # 2) Render returning success=False sets batch.status="FAILED", page.ctx=None, failed_details[].page_index as int
    async def _fail_render(*_args, **_kwargs):
        return {
            "image_name": p1,
            "output_path": None,
            "success": False,
            "skipped": False,
            "skip_reason": None,
            "error": "simulated render failure",
        }

    monkeypatch.setattr(job_manager_module, "render_and_save_page", _fail_render)
    cfg_fail = _base_config(str(out_dir))
    mgr_fail = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg_fail, translator_factory=factory)
    try:
        job_f = mgr_fail.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        internal_f = mgr_fail._jobs[job_f.job_id]
        bf, _ = mgr_fail.next_translation_batch(job_f.job_id)
        sub_f, _ = mgr_fail.submit_translation_batch(
            job_f.job_id, bf.batch_id, [{"id": 1, "translation": "失败页"}], return_next=True
        )
        assert sub_f.done is True
        assert internal_f._batches[bf.batch_id].status == "FAILED"
        assert internal_f.pages[0].ctx is None
        status_f = mgr_fail.get_job_status(job_f.job_id)
        assert isinstance(status_f["failed_details"][0]["page_index"], int)
        assert status_f["failed_details"][0]["page_index"] == 0
    finally:
        mgr_fail.shutdown()

    # 3) Archive temp cleanup when create_translation_job fails after expansion
    cleaned_archives = []
    monkeypatch.setattr(
        archive_extractor_module,
        "cleanup_archive_temp",
        lambda path: cleaned_archives.append(path),
    )

    def _bad_factory(_params):
        raise RuntimeError("translator init failed")

    mgr_arch = McpJobManager(
        root_dir=str(_bootstrap.ROOT),
        get_config=lambda: cfg_fail,
        file_expander=lambda *a: ([p1], {p1: None}, {"fake.zip": str(tmp_path / "fake_ext")}),
        translator_factory=_bad_factory,
    )
    try:
        with pytest.raises(RuntimeError, match="translator init failed"):
            mgr_arch.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        assert cleaned_archives == ["fake.zip"]
    finally:
        mgr_arch.shutdown()

    # 4) McpService.cancel_active_job_async cancels on background thread & emits job_cancel_finished
    from PyQt6.QtCore import Qt

    svc = McpService(root_dir=str(_bootstrap.ROOT), config_service=SimpleNamespace(get_config=lambda: cfg_fail))
    svc.job_manager._translator_factory = factory
    cancel_done = threading.Event()
    cancel_results = []
    svc.job_cancel_finished.connect(
        lambda ok, err: (
            cancel_results.append((ok, err, threading.current_thread().name)),
            cancel_done.set(),
        ),
        type=Qt.ConnectionType.DirectConnection,
    )
    try:
        j_async = svc.job_manager.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        assert svc.has_active_job() is True
        svc.cancel_active_job_async()
        assert cancel_done.wait(timeout=5.0)
        assert cancel_results == [(True, "", "mcp-cancel-job")]
        assert svc.has_active_job() is False
        assert svc.job_manager.get_job_status(j_async.job_id)["status"] == "cancelled"
    finally:
        svc.shutdown()


def test_recent_log_handler_filters_debug_truncates_and_tracks_sequence(tmp_path):
    import services.log_service as log_service_module

    log_file = tmp_path / "app.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)

    configure_queue_logging([fh])
    test_logger = logging.getLogger("test.log_service.recent")
    try:
        seq_start = log_service_module.get_recent_log_sequence()
        assert log_service_module.get_active_log_file_path() == os.path.normpath(
            os.path.abspath(str(log_file))
        )

        test_logger.debug("debug should only hit file")
        test_logger.info("info message 1")
        test_logger.info("A" * 2500)
        try:
            raise RuntimeError("E" * 5000)
        except RuntimeError:
            test_logger.error("boom", exc_info=True)

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and log_service_module.get_recent_log_sequence() < seq_start + 3:
            time.sleep(0.02)

        entries, latest_seq, truncated = log_service_module.get_recent_logs_since(seq_start)
        assert latest_seq == seq_start + 3
        assert truncated is False
        assert len(entries) == 3
        assert all("debug should only hit file" not in e["message"] for e in entries)
        assert entries[0]["message"] == "info message 1"
        assert entries[0]["seq"] == seq_start + 1
        assert "truncated 1500 chars" in entries[1]["message"]
        assert len(entries[1]["message"]) <= log_service_module._MAX_RECENT_LOG_MESSAGE_CHARS + 64
        assert "truncated" in entries[2]["exception"]
        assert len(entries[2]["exception"]) <= log_service_module._MAX_RECENT_LOG_EXCEPTION_CHARS + 64

        assert log_service_module.get_recent_logs_since(latest_seq) == ([], latest_seq, False)

        extra_count = log_service_module._RECENT_LOGS_MAXLEN + 10
        for idx in range(extra_count):
            test_logger.info(f"overflow line {idx}")

        deadline = time.monotonic() + 3.0
        while (
            time.monotonic() < deadline
            and log_service_module.get_recent_log_sequence() < latest_seq + extra_count
        ):
            time.sleep(0.02)

        ov_entries, ov_seq, ov_truncated = log_service_module.get_recent_logs_since(latest_seq)
        assert ov_truncated is True
        assert ov_seq == latest_seq + extra_count
        assert len(ov_entries) == log_service_module._RECENT_LOGS_MAXLEN
        assert ov_entries[-1]["message"] == f"overflow line {extra_count - 1}"
    finally:
        log_service_module.shutdown_queue_logging()

    file_contents = log_file.read_text(encoding="utf-8")
    assert "debug should only hit file" in file_contents
    assert "info message 1" in file_contents


_QT_APP_REF = None


def _get_qt_app():
    global _QT_APP_REF
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _QT_APP_REF = app
    return app


def test_mcp_page_panel_logs_visibility_gated_batched_and_no_filter(tmp_path, monkeypatch):
    from core.config_models import AppSettings
    import services.log_service as log_service_module
    import ui.main_page.pages.mcp_page as mcp_page_module

    app = _get_qt_app()

    configure_queue_logging([])
    log_svc = log_service_module.LogService(app_name="MangaTranslatorUITest")
    cfg = AppSettings()
    cfg_svc = SimpleNamespace(get_config=lambda: cfg)
    mcp_svc = McpService(root_dir=str(tmp_path), config_service=cfg_svc)

    monkeypatch.setattr(mcp_page_module, "get_config_service", lambda: cfg_svc)
    monkeypatch.setattr(mcp_page_module, "get_mcp_service", lambda: mcp_svc)
    monkeypatch.setattr(mcp_page_module, "get_log_service", lambda: log_svc)

    controller = SimpleNamespace(
        update_single_config=lambda *_a, **_k: None,
        open_output_folder=lambda: None,
    )
    panel = mcp_page_module.McpPagePanel(
        controller=controller,
        t_func=lambda key, **kwargs: key.format(**kwargs) if kwargs else key,
    )
    probe_logger = logging.getLogger("test.mcp_page.panel")

    def _wait_seq(target_seq: int) -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and log_service_module.get_recent_log_sequence() < target_seq:
            time.sleep(0.02)
        assert log_service_module.get_recent_log_sequence() >= target_seq

    try:
        assert panel.log_view.isUndoRedoEnabled() is False
        assert panel.isVisible() is False
        assert panel._log_poll_timer.isActive() is False

        seq0 = log_service_module.get_recent_log_sequence()
        probe_logger.info("hidden info log")
        _wait_seq(seq0 + 1)
        panel._sync_logs()
        assert panel.log_view.toPlainText() == ""

        panel.show()
        app.processEvents()
        assert panel._log_poll_timer.isActive() is True
        assert "hidden info log" in panel.log_view.toPlainText()

        seq1 = log_service_module.get_recent_log_sequence()
        probe_logger.debug("debug hidden from gui")
        probe_logger.info("batch line 1")
        probe_logger.info("batch line 2")
        _wait_seq(seq1 + 2)
        panel._sync_logs()
        text_after_batch = panel.log_view.toPlainText()
        assert "batch line 1" in text_after_batch
        assert "batch line 2" in text_after_batch
        assert "debug hidden from gui" not in text_after_batch

        panel._on_clear_logs_clicked()
        assert panel.log_view.toPlainText() == ""
        panel.hide()
        app.processEvents()
        assert panel._log_poll_timer.isActive() is False

        panel.show()
        app.processEvents()
        assert panel.log_view.toPlainText() == ""

        seq2 = log_service_module.get_recent_log_sequence()
        probe_logger.info("after clear info")
        _wait_seq(seq2 + 1)
        panel._sync_logs()
        text_after_clear = panel.log_view.toPlainText()
        assert "after clear info" in text_after_clear
        assert "batch line 1" not in text_after_clear
    finally:
        panel.shutdown()
        assert panel._log_poll_timer.isActive() is False
        panel.close()
        panel.deleteLater()
        app.processEvents()
        mcp_svc.shutdown()
        log_svc.shutdown()
        shutdown_queue_logging()


def test_prev_context_excludes_future_skipped_and_out_of_order_batches(tmp_path):
    """P1-2: prev_context must only include pages strictly earlier in source reading order."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    p1 = _make_png(tmp_path / "01.png")
    p2 = _make_png(tmp_path / "02.png")
    p3 = _make_png(tmp_path / "03.png")
    p4 = _make_png(tmp_path / "04.png")

    # Pre-existing outputs + JSON sidecars for p3 & p4 so they are skipped in resume_pages.
    for p_skip, trans_txt in ((p3, "第三页已有"), (p4, "第四页已有")):
        _make_png(out_dir / os.path.basename(p_skip))
        Path(get_json_path(p_skip, create_dir=True)).write_text(
            json.dumps(
                {
                    os.path.abspath(p_skip): {
                        "regions": [
                            {
                                "lines": [[[10, 10], [80, 10], [80, 30], [10, 30]]],
                                "texts": ["原文"],
                                "text": "原文",
                                "translation": trans_txt,
                                "prob": 0.9,
                            }
                        ],
                        "original_width": 240,
                        "original_height": 320,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    factory, _ = _make_stub_factory(
        {
            "01.png": [_make_block("一页原文")],
            "02.png": [_make_block("二页原文")],
        }
    )
    cfg = _base_config(str(out_dir), max_parallel_batches=2)
    cfg["cli"]["overwrite"] = False
    cfg["cli"]["batch_size"] = 1
    cfg["cli"]["context_size"] = 3

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1, p2, p3, p4], output_dir=str(out_dir))
        assert job.skipped_pages == 2
        assert job.pending_pages == 2

        b1, _ = mgr.next_translation_batch(job.job_id, worker_id="w1")
        assert b1.pages[0].filename == "01.png"
        assert b1.prev_context == []  # must NOT include future skipped p3/p4

        b2, _ = mgr.next_translation_batch(job.job_id, worker_id="w2")
        assert b2.pages[0].filename == "02.png"
        assert b2.prev_context == []

        # Out-of-order: b2 submits first, while b1 is released and re-leased -> b1 still must NOT see b2
        mgr.submit_translation_batch(
            job.job_id,
            b2.batch_id,
            [{"id": 1, "translation": "第二页译文"}],
            return_next=False,
        )
        rel = mgr.release_batch(job.job_id, b1.batch_id)
        assert rel["released"] is True

        b1_re, _ = mgr.next_translation_batch(job.job_id, worker_id="w3")
        assert b1_re.pages[0].filename == "01.png"
        assert b1_re.prev_context == []

        done_resp, _ = mgr.submit_translation_batch(
            job.job_id,
            b1_re.batch_id,
            [{"id": 1, "translation": "第一页译文"}],
            return_next=True,
        )
        assert done_resp.done is True
    finally:
        mgr.shutdown()


def test_parallel_workers_interleaved_glossary_delta(tmp_path):
    """P1-3: interleaved submits must deliver peer terms without re-echoing own terms."""
    prompt_file = tmp_path / "prompt.yaml"
    prompt_file.write_text(
        yaml.safe_dump({"system_prompt": "Translate.", "glossary": {}}, allow_unicode=True),
        encoding="utf-8",
    )

    p1 = _make_png(tmp_path / "01.png")
    p2 = _make_png(tmp_path / "02.png")
    p3 = _make_png(tmp_path / "03.png")
    factory, _ = _make_stub_factory(
        {
            "01.png": [_make_block("一")],
            "02.png": [_make_block("二")],
            "03.png": [_make_block("三")],
        }
    )

    out_dir = tmp_path / "out"
    cfg = _base_config(str(out_dir), max_parallel_batches=2)
    cfg["cli"]["batch_size"] = 1
    cfg["translator"]["extract_glossary"] = True
    cfg["translator"]["high_quality_prompt_path"] = str(prompt_file)

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1, p2, p3], output_dir=str(out_dir))
        b1, _ = mgr.next_translation_batch(job.job_id, worker_id="w1")
        b2, _ = mgr.next_translation_batch(job.job_id, worker_id="w2")
        assert b1.glossary_version == 0
        assert b2.glossary_version == 0

        # W2 submits TermB -> v=1
        sub2, _ = mgr.submit_translation_batch(
            job.job_id,
            b2.batch_id,
            [{"id": 1, "translation": "二译"}],
            new_terms=[
                {
                    "category": "Item",
                    "original": "TermB",
                    "aliases": [{"original": "TermB", "translations": [{"text": "道具B"}]}],
                }
            ],
            return_next=False,
        )
        assert sub2.merged_terms_count == 1

        # W1 submits TermA (plus an invalid category term that should be ignored) -> v=2
        sub1, _ = mgr.submit_translation_batch(
            job.job_id,
            b1.batch_id,
            [{"id": 1, "translation": "一译"}],
            new_terms=[
                {
                    "category": "Item",
                    "original": "TermA",
                    "aliases": [{"original": "TermA", "translations": [{"text": "道具A"}]}],
                },
                {
                    "category": "InvalidCat",
                    "original": "IgnoredTerm",
                    "aliases": [{"original": "IgnoredTerm", "translations": [{"text": "忽略"}]}],
                },
            ],
            return_next=True,
        )
        assert sub1.merged_terms_count == 1
        b3 = sub1.next_batch
        assert b3 is not None and not b3.done
        delta_originals = [t["original"] for t in b3.glossary_delta]
        assert delta_originals == ["TermB"]
        assert b3.glossary_version == 2

        done_resp, _ = mgr.submit_translation_batch(
            job.job_id,
            b3.batch_id,
            [{"id": 1, "translation": "三译"}],
            return_next=True,
        )
        assert done_resp.done is True
    finally:
        mgr.shutdown()


def test_ctx_preview_reader_refcount_prevents_close_race(tmp_path, monkeypatch):
    """P1-4a: concurrent cancel_job or submit_translation_batch during get_page_preview must not close ctx mid-read."""
    import threading
    import manga_translator.mcp.job_manager as job_manager_module
    from manga_translator.mcp.batch_adapter import render_cropped_region_preview as _orig_crop

    out_dir = tmp_path / "out"
    p1 = _make_png(tmp_path / "p1.png")
    factory, _ = _make_stub_factory({"p1.png": [_make_block("阅读保护")]})

    # Case 1: cancel_job while get_page_preview is blocked mid-render
    entered_crop = threading.Event()
    allow_crop = threading.Event()

    def _blocked_crop(ctx, *args, **kwargs):
        entered_crop.set()
        allow_crop.wait(timeout=5.0)
        # Verify ctx.input has not been closed while preview_readers > 0
        assert ctx.input is not None
        ctx.input.tobytes()
        return _orig_crop(ctx, *args, **kwargs)

    monkeypatch.setattr(job_manager_module, "render_cropped_region_preview", _blocked_crop)
    cfg = _base_config(str(out_dir), translation_mode="text", allow_on_demand_image=True)
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        internal_job = mgr._jobs[job.job_id]
        mgr.next_translation_batch(job.job_id)

        preview_res = {}

        def _run_preview():
            try:
                preview_res["out"] = mgr.get_page_preview(job.job_id, 0, region_ids=[1])
            except Exception as exc:
                preview_res["exc"] = exc

        t_prev = threading.Thread(target=_run_preview, daemon=True)
        t_prev.start()
        assert entered_crop.wait(timeout=5.0)

        mgr.cancel_job(job.job_id)
        # ctx must remain open while reader is still active
        assert internal_job.pages[0].ctx is not None
        assert internal_job.pages[0].preview_readers == 1

        allow_crop.set()
        t_prev.join(timeout=5.0)
        assert not t_prev.is_alive()
        assert "out" in preview_res or isinstance(preview_res.get("exc"), RuntimeError)
        if "exc" in preview_res:
            assert "already cancelled" in str(preview_res["exc"])
        assert internal_job.pages[0].preview_readers == 0
        assert internal_job.pages[0].ctx is None
    finally:
        allow_crop.set()
        monkeypatch.setattr(job_manager_module, "render_cropped_region_preview", _orig_crop)
        mgr.shutdown()

    # Case 2: submit_translation_batch waits for active preview reader before committing/rendering
    entered_crop.clear()
    allow_crop.clear()
    monkeypatch.setattr(job_manager_module, "render_cropped_region_preview", _blocked_crop)
    mgr2 = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job2 = mgr2.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        internal2 = mgr2._jobs[job2.job_id]
        b2, _ = mgr2.next_translation_batch(job2.job_id)

        t_prev2 = threading.Thread(
            target=lambda: mgr2.get_page_preview(job2.job_id, 0, region_ids=[1]),
            daemon=True,
        )
        t_prev2.start()
        assert entered_crop.wait(timeout=5.0)

        sub_holder = {}
        t_sub = threading.Thread(
            target=lambda: sub_holder.setdefault(
                "res",
                mgr2.submit_translation_batch(
                    job2.job_id,
                    b2.batch_id,
                    [{"id": 1, "translation": "提交保护"}],
                    return_next=True,
                ),
            ),
            daemon=True,
        )
        t_sub.start()
        time.sleep(0.1)
        assert internal2.pages[0].committed is False
        assert internal2.pages[0].ctx is not None

        allow_crop.set()
        t_prev2.join(timeout=5.0)
        t_sub.join(timeout=5.0)
        assert sub_holder["res"][0].done is True
        assert internal2.pages[0].ctx is None
    finally:
        allow_crop.set()
        mgr2.shutdown()


def test_slow_preview_prep_protects_lease_and_refreshes_leased_at(tmp_path, monkeypatch):
    """P1-4b: slow HQ preview preparation exceeding lease_timeout must not be stolen and refreshes leased_at."""
    import threading
    import manga_translator.mcp.job_manager as job_manager_module
    from manga_translator.mcp.batch_adapter import render_numbered_page_preview as _orig_render

    out_dir = tmp_path / "out"
    p1 = _make_png(tmp_path / "p1.png")
    factory, _ = _make_stub_factory({"p1.png": [_make_block("慢预览")]})

    entered_render = threading.Event()

    def _slow_render(ctx, text_order, **kwargs):
        entered_render.set()
        time.sleep(0.25)
        return _orig_render(ctx, text_order, **kwargs)

    monkeypatch.setattr(job_manager_module, "render_numbered_page_preview", _slow_render)
    cfg = _base_config(str(out_dir), max_parallel_batches=2, lease_timeout_seconds=0.08)

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        internal = mgr._jobs[job.job_id]
        assert entered_render.wait(timeout=5.0)

        w1_holder = {}
        t_w1 = threading.Thread(
            target=lambda: w1_holder.setdefault("res", mgr.next_translation_batch(job.job_id, worker_id="w1")),
            daemon=True,
        )
        t_w1.start()

        # Wait longer than lease_timeout_seconds while w1 is waiting on preview prep
        time.sleep(0.15)
        w2_payload, _ = mgr.next_translation_batch(job.job_id, worker_id="w2")
        assert w2_payload.waiting is True
        assert w2_payload.batch_id is None

        t_w1.join(timeout=5.0)
        w1_payload, _ = w1_holder["res"]
        assert w1_payload.waiting is False
        assert w1_payload.batch_id is not None

        batch_state = internal._batches[w1_payload.batch_id]
        assert batch_state.preparing is False
        assert batch_state.worker_id == "w1"
        # Leased timestamp was refreshed at return time so a subsequent poll does not immediately steal it
        w2_after, _ = mgr.next_translation_batch(job.job_id, worker_id="w2")
        assert w2_after.waiting is True

        sub, _ = mgr.submit_translation_batch(
            job.job_id,
            w1_payload.batch_id,
            [{"id": 1, "translation": "译文"}],
            return_next=True,
        )
        assert sub.done is True
    finally:
        mgr.shutdown()


def test_cancel_timeout_defers_cleanup_and_keeps_gpu_mutex(tmp_path):
    """P1-4c: cancel() timing out on a slow GPU thread keeps GPU mutex active until thread exits."""
    import threading

    out_dir = tmp_path / "out"
    p1 = _make_png(tmp_path / "p1.png")
    entered_detect = threading.Event()
    allow_detect = threading.Event()
    base_factory, _ = _make_stub_factory({"p1.png": [_make_block("慢检测")]})

    def _slow_detect_factory(params):
        tr = base_factory(params)
        orig_detect = tr._run_detection

        async def _slow_detect(config, ctx):
            entered_detect.set()
            allow_detect.wait(timeout=5.0)
            return await orig_detect(config, ctx)

        tr._run_detection = _slow_detect
        return tr

    cfg = _base_config(str(out_dir))
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=_slow_detect_factory)
    try:
        job = mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        internal = mgr._jobs[job.job_id]
        assert entered_detect.wait(timeout=5.0)

        cancel_status = mgr.cancel_job(job.job_id, join_timeout=0.05)
        assert cancel_status["status"] == "cancelling"
        assert mgr.has_active_job() is True
        assert internal.is_worker_alive() is True

        with pytest.raises(RuntimeError, match="already running"):
            mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        with pytest.raises(RuntimeError, match="while an MCP translation job is running"):
            mgr.unload_models()

        allow_detect.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and mgr.has_active_job():
            time.sleep(0.02)

        assert mgr.has_active_job() is False
        assert internal.is_worker_alive() is False
        assert internal.status == "cancelled"
        assert internal._finalized is True
        assert all(p.ctx is None for p in internal.pages)
        assert mgr.get_job_status(job.job_id)["status"] == "cancelled"
    finally:
        allow_detect.set()
        mgr.shutdown()


def test_verbose_batch_concurrent_isolates_debug_subfolders_and_config(tmp_path):
    """P1-6: verbose=True + batch_concurrent=True isolates thread-local image context and render config."""
    import threading
    from manga_translator.utils import Quadrilateral
    from manga_translator.utils.generic import get_image_md5

    out_dir = tmp_path / "out"
    debug_root = tmp_path / "debug_out"
    p1 = _make_png(tmp_path / "01.png", color=(240, 240, 240))
    p2 = _make_png(tmp_path / "02.png", color=(210, 210, 210))
    p3 = _make_png(tmp_path / "03.png", color=(180, 180, 180))

    blocks_by_name = {
        "01.png": [_make_block("一")],
        "02.png": [_make_block("二")],
        "03.png": [_make_block("三")],
    }
    base_factory, _ = _make_stub_factory(blocks_by_name)

    observed_subfolders = []
    observed_configs = []
    lock = threading.Lock()
    render_p1_entered = threading.Event()
    allow_render_p1 = threading.Event()

    def _verbose_factory(params):
        tr = base_factory(params)
        orig_inpaint = tr._run_inpainting
        orig_render = tr._run_text_rendering

        def _tmp_result_path(path: str) -> str:
            sf = tr._get_image_subfolder() or "shared"
            target = debug_root / sf / path
            target.parent.mkdir(parents=True, exist_ok=True)
            return str(target)

        async def _detect(config, ctx):
            fname = os.path.basename(getattr(ctx.input, "name", "") or "")
            sf = tr._get_image_subfolder()
            md5 = get_image_md5(ctx.input)
            with lock:
                observed_subfolders.append(("detect", fname, md5, sf))
            blocks = blocks_by_name.get(fname, [])
            quads = [
                Quadrilateral(np.asarray(b.lines[0], dtype=np.int32), b.text, b.prob)
                for b in blocks
            ]
            h, w = ctx.img_rgb.shape[:2]
            mask = np.zeros((h, w), dtype=np.uint8)
            for b in blocks:
                x1, y1, x2, y2 = [int(v) for v in b.xyxy]
                mask[max(0, y1):min(h, y2), max(0, x1):min(w, x2)] = 255
            return quads, mask, mask.copy()

        async def _merge(config, ctx):
            fname = os.path.basename(getattr(ctx.input, "name", "") or "")
            return [
                TextBlock(
                    lines=b.lines.copy(),
                    texts=list(b.texts),
                    prob=b.prob,
                    font_size=b.font_size,
                )
                for b in blocks_by_name.get(fname, [])
            ]

        async def _inpaint(config, ctx):
            fname = os.path.basename(getattr(ctx.input, "name", "") or "")
            sf = tr._get_image_subfolder()
            md5 = get_image_md5(ctx.input)
            with lock:
                observed_subfolders.append(("inpaint", fname, md5, sf))
            return await orig_inpaint(config, ctx)

        async def _render(config, ctx, skip_font_scaling: bool = False, **_kwargs):
            fname = os.path.basename(getattr(ctx, "image_name", "") or "")
            if fname == "01.png":
                render_p1_entered.set()
                allow_render_p1.wait(timeout=5.0)
            sf = tr._get_image_subfolder()
            md5 = get_image_md5(ctx.input)
            assert not hasattr(config, "_chinese_linebreak_debug_records")
            config._chinese_linebreak_debug_records = [{"page": fname}]
            with lock:
                observed_subfolders.append(("render", fname, md5, sf))
                observed_configs.append((fname, config))
            return await orig_render(config, ctx, skip_font_scaling=skip_font_scaling)

        tr._result_path = _tmp_result_path
        tr._run_detection = _detect
        tr._run_textline_merge = _merge
        tr._run_inpainting = _inpaint
        tr._run_text_rendering = _render
        return tr

    cfg = _base_config(str(out_dir))
    cfg["cli"]["verbose"] = True
    cfg["cli"]["batch_concurrent"] = True
    cfg["cli"]["batch_size"] = 1

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=_verbose_factory)
    try:
        job = mgr.create_translation_job(inputs=[p1, p2, p3], output_dir=str(out_dir))
        internal_job = mgr._jobs[job.job_id]
        b1, _ = mgr.next_translation_batch(job.job_id)
        sub1, _ = mgr.submit_translation_batch(
            job.job_id,
            b1.batch_id,
            [{"id": 1, "translation": "一译"}],
            return_next=True,
        )
        # RenderWorker is paused on 01.png while GpuPriorityWorker continues preprocessing/inpainting 02 & 03
        assert render_p1_entered.wait(timeout=5.0)
        b2 = sub1.next_batch
        assert b2 is not None and not b2.done
        sub2, _ = mgr.submit_translation_batch(
            job.job_id,
            b2.batch_id,
            [{"id": 1, "translation": "二译"}],
            return_next=True,
        )
        b3 = sub2.next_batch
        assert b3 is not None and not b3.done
        allow_render_p1.set()

        done_resp, _ = mgr.submit_translation_batch(
            job.job_id,
            b3.batch_id,
            [{"id": 1, "translation": "三译"}],
            return_next=True,
        )
        assert done_resp.done is True

        for stage, fname, md5, sf in observed_subfolders:
            assert md5 in sf, f"{stage} for {fname} got wrong debug subfolder {sf} (expected md5 {md5})"

        debug_dirs = [d.name for d in debug_root.iterdir() if d.is_dir()]
        assert len(debug_dirs) == 3

        config_ids = {id(cfg_obj) for _fn, cfg_obj in observed_configs}
        assert len(config_ids) == 3  # each render call got its own shallow-copied Config
        assert id(internal_job.config) not in config_ids
        assert not hasattr(internal_job.config, "_chinese_linebreak_debug_records")
    finally:
        allow_render_p1.set()
        mgr.shutdown()


def test_retranslate_json_resets_skip_font_scaling_consistently(tmp_path):
    """P2-7: retranslate_json=True must run font scaling and write skip_font_scaling=false back to JSON."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    img_path = _make_png(tmp_path / "p1.png")

    json_path = Path(get_json_path(img_path, create_dir=True))
    json_path.write_text(
        json.dumps(
            {
                os.path.abspath(img_path): {
                    "regions": [
                        {
                            "lines": [[[10, 10], [80, 10], [80, 30], [10, 30]]],
                            "texts": ["原文"],
                            "text": "原文",
                            "translation": "旧译文",
                            "prob": 0.9,
                        }
                    ],
                    "skip_font_scaling": True,
                    "original_width": 240,
                    "original_height": 320,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    factory, calls = _make_stub_factory({"p1.png": [_make_block("原文")]})
    cfg = _base_config(str(out_dir))
    cfg["cli"]["save_text"] = True
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir), retranslate_json=True)
        batch, _ = mgr.next_translation_batch(job.job_id)
        sub, _ = mgr.submit_translation_batch(
            job.job_id,
            batch.batch_id,
            [{"id": 1, "translation": "全新译文"}],
            return_next=True,
        )
        assert sub.done is True
        assert calls["render_skip_font_scaling"] == [False]
        saved = json.loads(json_path.read_text(encoding="utf-8"))
        saved_entry = next(iter(saved.values()))
        assert saved_entry["skip_font_scaling"] is False
    finally:
        mgr.shutdown()


def test_partial_page_failure_yields_completed_with_errors(tmp_path):
    """P2-8: 1 succeeded + 1 failed page must finalize as completed_with_errors."""
    out_dir = tmp_path / "out"
    p1 = _make_png(tmp_path / "01.png")
    p2 = _make_png(tmp_path / "02.png")
    base_factory, _ = _make_stub_factory(
        {
            "01.png": [_make_block("一页")],
            "02.png": [_make_block("二页")],
        }
    )

    def _fail_second_inpaint_factory(params):
        tr = base_factory(params)
        orig_inpaint = tr._run_inpainting

        async def _inpaint(config, ctx):
            fname = os.path.basename(getattr(ctx, "image_name", "") or "")
            if fname == "02.png":
                raise RuntimeError("simulated page 2 inpaint failure")
            return await orig_inpaint(config, ctx)

        tr._run_inpainting = _inpaint
        return tr

    cfg = _base_config(str(out_dir))
    cfg["cli"]["batch_size"] = 1
    mgr = McpJobManager(
        root_dir=str(_bootstrap.ROOT),
        get_config=lambda: cfg,
        translator_factory=_fail_second_inpaint_factory,
    )
    try:
        job = mgr.create_translation_job(inputs=[p1, p2], output_dir=str(out_dir))
        b1, _ = mgr.next_translation_batch(job.job_id)
        sub1, _ = mgr.submit_translation_batch(
            job.job_id, b1.batch_id, [{"id": 1, "translation": "一译"}], return_next=True
        )
        b2 = sub1.next_batch
        assert b2 is not None and not b2.done
        done_resp, _ = mgr.submit_translation_batch(
            job.job_id, b2.batch_id, [{"id": 1, "translation": "二译"}], return_next=True
        )
        assert done_resp.done is True
        assert done_resp.summary["status"] == "completed_with_errors"
        assert done_resp.summary["rendered_pages"] == 1
        assert done_resp.summary["failed_pages"] == 1
    finally:
        mgr.shutdown()


def test_cancel_during_job_creation_does_not_hit_previous_job_and_cleans_archives(tmp_path, monkeypatch):
    """P2-9: cancel_job() during _creating_job=True aborts creation without touching _latest_job_id."""
    import threading
    import desktop_qt_ui.utils.archive_extractor as archive_extractor_module

    out_dir = tmp_path / "out"
    p1 = _make_png(tmp_path / "p1.png")
    factory, _ = _make_stub_factory({"p1.png": [_make_block("初始化取消")]})
    cfg = _base_config(str(out_dir))

    # First run a completed job so _latest_job_id is populated.
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        j_old = mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        b_old, _ = mgr.next_translation_batch(j_old.job_id)
        sub_old, _ = mgr.submit_translation_batch(
            j_old.job_id, b_old.batch_id, [{"id": 1, "translation": "旧任务"}], return_next=True
        )
        assert sub_old.done is True
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and j_old.job_id in mgr._jobs:
            time.sleep(0.02)

        cleaned_archives = []
        monkeypatch.setattr(
            archive_extractor_module,
            "cleanup_archive_temp",
            lambda path: cleaned_archives.append(path),
        )

        entered_expand = threading.Event()
        release_expand = threading.Event()

        def _slow_expander(sources, ex_f, ex_files, out_b, ov):
            entered_expand.set()
            release_expand.wait(timeout=5.0)
            return list(sources), {s: None for s in sources}, {"fake_init.zip": str(tmp_path / "ext")}

        mgr._file_expander = _slow_expander

        create_holder = {}

        def _create():
            try:
                create_holder["job"] = mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
            except Exception as exc:
                create_holder["exc"] = exc

        t_create = threading.Thread(target=_create, daemon=True)
        t_create.start()
        assert entered_expand.wait(timeout=5.0)
        assert mgr.has_active_job() is True

        cancel_res = mgr.cancel_job()
        assert cancel_res["status"] == "cancelling"
        # Old job summary remains intact
        assert mgr.get_job_status(j_old.job_id)["status"] == "completed"

        release_expand.set()
        t_create.join(timeout=5.0)
        assert not t_create.is_alive()
        assert isinstance(create_holder.get("exc"), RuntimeError)
        assert "cancelled" in str(create_holder["exc"]).lower()
        assert cleaned_archives == ["fake_init.zip"]
        assert mgr.has_active_job() is False
    finally:
        mgr.shutdown()


def test_p2_inpaint_partial_failure_after_release_excludes_failed_page_on_re_lease(tmp_path):
    """P2-10: released batch skips page that failed in P2 inpaint when re-leased to another worker."""
    out_dir = tmp_path / "out"
    p1 = _make_png(tmp_path / "01.png")
    p2 = _make_png(tmp_path / "02.png")
    base_factory, _ = _make_stub_factory(
        {
            "01.png": [_make_block("一页失败")],
            "02.png": [_make_block("二页正常")],
        }
    )

    def _fail_p1_inpaint_factory(params):
        tr = base_factory(params)
        orig_inpaint = tr._run_inpainting

        async def _inpaint(config, ctx):
            fname = os.path.basename(getattr(ctx, "image_name", "") or "")
            if fname == "01.png":
                raise RuntimeError("simulated P2 inpaint failure on page 1")
            return await orig_inpaint(config, ctx)

        tr._run_inpainting = _inpaint
        return tr

    cfg = _base_config(str(out_dir))
    cfg["cli"]["batch_size"] = 2
    cfg["cli"]["batch_concurrent"] = True
    mgr = McpJobManager(
        root_dir=str(_bootstrap.ROOT),
        get_config=lambda: cfg,
        translator_factory=_fail_p1_inpaint_factory,
    )
    try:
        job = mgr.create_translation_job(inputs=[p1, p2], output_dir=str(out_dir))
        internal = mgr._jobs[job.job_id]
        b1, _ = mgr.next_translation_batch(job.job_id, worker_id="w1")
        assert len(b1.pages) == 2

        # Wait until speculative P2 inpaint fails page 0 and finishes page 1.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not (
            internal.pages[0].failed and internal.pages[1].inpaint_done and not internal.pages[0].inpaint_in_progress
        ):
            time.sleep(0.02)
        assert internal.pages[0].failed is True

        rel = mgr.release_batch(job.job_id, b1.batch_id)
        assert rel["released"] is True

        b1_re, _ = mgr.next_translation_batch(job.job_id, worker_id="w2")
        assert [pg.filename for pg in b1_re.pages] == ["02.png"]
        assert any("01.png" in h for h in b1_re.hints)
        p2_rid = b1_re.pages[0].regions[0].id

        sub, _ = mgr.submit_translation_batch(
            job.job_id,
            b1_re.batch_id,
            [{"id": p2_rid, "translation": "二页译文"}],
            return_next=True,
        )
        assert sub.done is True
        assert sub.summary["status"] == "completed_with_errors"
        assert sub.summary["rendered_pages"] == 1
        assert sub.summary["failed_pages"] == 1
    finally:
        mgr.shutdown()


def test_save_to_source_dir_rejects_archive_without_output_dir_and_cleans_temp(tmp_path):
    """P2-11: archive + save_to_source_dir=True + empty output_dir must error and remove temp extract dir."""
    import zipfile
    from desktop_qt_ui.utils.archive_extractor import get_temp_extract_dir

    inner_png = _make_png(tmp_path / "src_img.png")
    archive_path = tmp_path / "chapter.cbz"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.write(inner_png, arcname="01.png")

    cfg = _base_config("")
    cfg["app"]["last_output_path"] = ""
    cfg["cli"]["save_to_source_dir"] = True

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg)
    try:
        with pytest.raises(ValueError, match="save_to_source_dir"):
            mgr.create_translation_job(inputs=[str(archive_path)], output_dir="")
        assert not os.path.exists(get_temp_extract_dir(str(archive_path)))
    finally:
        mgr.shutdown()


def test_install_mcp_config_handles_utf8_bom_and_codex_stdio_upgrade(tmp_path):
    """P2-12: UTF-8 BOM JSON/TOML files and legacy Codex stdio [mcp_servers.manga-translator] merge cleanly."""
    import tomllib

    svc = McpService(root_dir=str(_bootstrap.ROOT))
    try:
        # 1) JSON with UTF-8 BOM
        json_cfg = tmp_path / "cursor_mcp.json"
        json_cfg.write_bytes(b"\xef\xbb\xbf" + b'{"mcpServers": {"other": {"command": "echo"}}}')
        svc.install_mcp_config("cursor", json_cfg, port=9111)
        parsed_json = json.loads(json_cfg.read_text(encoding="utf-8"))
        assert parsed_json["mcpServers"]["other"] == {"command": "echo"}
        assert parsed_json["mcpServers"]["manga-translator"] == {"url": "http://127.0.0.1:9111/mcp"}

        # 2) Codex TOML with UTF-8 BOM + legacy stdio command & multi-line args (including ] and # in strings)
        codex_cfg = tmp_path / "config.toml"
        toml_body = (
            "# Header\n"
            '[mcp_servers.manga-translator]\n'
            'command = "npx"\n'
            "args = [\n"
            '  "-y", # keep scanning\n'
            '  "mcp-remote[test]#tag",\n'
            '  "http://127.0.0.1:8000/mcp",\n'
            "]\n"
            "startup_timeout_sec = 30\n\n"
            "[mcp_servers.other]\n"
            'command = "uvx"\n'
        )
        codex_cfg.write_bytes(b"\xef\xbb\xbf" + toml_body.encode("utf-8"))
        svc.install_mcp_config("codex", codex_cfg, port=9222)

        written_bytes = codex_cfg.read_bytes()
        assert not written_bytes.startswith(b"\xef\xbb\xbf")
        written_text = written_bytes.decode("utf-8")
        assert "# Header" in written_text
        parsed_toml = tomllib.loads(written_text)
        entry = parsed_toml["mcp_servers"]["manga-translator"]
        assert entry == {"url": "http://127.0.0.1:9222/mcp", "startup_timeout_sec": 30}
        assert parsed_toml["mcp_servers"]["other"] == {"command": "uvx"}
    finally:
        svc.shutdown()


def test_secondary_ocr_api_ignored_when_hybrid_ocr_disabled(tmp_path):
    """Disabled secondary_ocr never triggers 0-API conflict."""
    img_path = _make_png(tmp_path / "p1.png")
    out_dir = tmp_path / "out"
    cfg = _base_config(str(out_dir))
    cfg["ocr"]["secondary_ocr"] = "gemini_ocr"
    cfg["ocr"]["use_hybrid_ocr"] = False

    assert find_api_backend_conflicts(cfg) == []

    factory, _calls = _make_stub_factory({"p1.png": [_make_block("一行")]})
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        ws = mgr.get_workspace_info()
        assert ws.api_conflicts == []
        job_resp = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        mgr.cancel_job(job_resp.job_id)
    finally:
        mgr.shutdown()


def test_get_job_status_reclaims_expired_lease_immediately(tmp_path, monkeypatch):
    """P3-2: get_job_status reclaims expired leases immediately without waiting for next_translation_batch."""
    import manga_translator.mcp.job_manager as job_manager_module

    now = [1000.0]
    monkeypatch.setattr(job_manager_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    img_path = _make_png(tmp_path / "p1.png")
    out_dir = tmp_path / "out"
    cfg = _base_config(str(out_dir), lease_timeout_seconds=10)
    factory, _calls = _make_stub_factory({"p1.png": [_make_block("一行")]})

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job_resp = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        batch, _ = mgr.next_translation_batch(job_resp.job_id, worker_id="w1")
        assert batch.batch_id is not None

        status_before = mgr.get_job_status(job_resp.job_id)
        assert len(status_before["leases"]) == 1
        assert status_before["queues"]["leased_batches"] == 1

        now[0] += 20.0

        status_after = mgr.get_job_status(job_resp.job_id)
        assert status_after["leases"] == []
        assert status_after["queues"]["leased_batches"] == 0
        assert status_after["queues"]["ready_batches"] >= 1
    finally:
        mgr.shutdown()


def test_mcp_page_open_output_button_prefers_last_job_output_dir(tmp_path, monkeypatch):
    """P3-3: Open Output Folder prefers latest MCP job output_dir when present, falling back to controller."""
    from PyQt6.QtCore import QUrl
    from core.config_models import AppSettings
    import services.log_service as log_service_module
    import ui.main_page.pages.mcp_page as mcp_page_module

    app = _get_qt_app()
    configure_queue_logging([])
    log_svc = log_service_module.LogService(app_name="MangaTranslatorUITest")
    cfg = AppSettings()
    cfg_svc = SimpleNamespace(get_config=lambda: cfg)
    mcp_svc = McpService(root_dir=str(tmp_path), config_service=cfg_svc)

    monkeypatch.setattr(mcp_page_module, "get_config_service", lambda: cfg_svc)
    monkeypatch.setattr(mcp_page_module, "get_mcp_service", lambda: mcp_svc)
    monkeypatch.setattr(mcp_page_module, "get_log_service", lambda: log_svc)

    opened_urls = []
    monkeypatch.setattr(
        mcp_page_module.QDesktopServices,
        "openUrl",
        lambda url: opened_urls.append(url) or True,
    )
    fallback_calls = []
    controller = SimpleNamespace(
        update_single_config=lambda *_a, **_k: None,
        open_output_folder=lambda: fallback_calls.append(True),
    )
    panel = mcp_page_module.McpPagePanel(
        controller=controller,
        t_func=lambda key, **kwargs: key.format(**kwargs) if kwargs else key,
    )
    job_out = tmp_path / "mcp_job_out"
    job_out.mkdir(parents=True, exist_ok=True)

    try:
        # 1) No job output_dir yet -> fallback to controller
        panel.open_output_button.click()
        app.processEvents()
        assert len(fallback_calls) == 1
        assert opened_urls == []

        # 2) Valid MCP job output_dir recorded -> opens QDesktopServices directly
        panel._on_job_progress_changed({"job_id": "j1", "output_dir": str(job_out), "status": "completed"})
        panel.open_output_button.click()
        app.processEvents()
        assert len(fallback_calls) == 1
        assert len(opened_urls) == 1
        assert opened_urls[0] == QUrl.fromLocalFile(os.path.realpath(str(job_out)))

        # 3) Missing directory -> fallback to controller
        job_out.rmdir()
        panel.open_output_button.click()
        app.processEvents()
        assert len(fallback_calls) == 2
        assert len(opened_urls) == 1
    finally:
        panel.shutdown()
        panel.close()
        panel.deleteLater()
        app.processEvents()
        mcp_svc.shutdown()
        log_svc.shutdown()
        shutdown_queue_logging()


def _glossary_term(category, original, translation):
    return {
        "category": category,
        "original": original,
        "aliases": [{"original": original, "translations": [{"text": translation}]}],
    }


def test_failed_post_processing_restores_regions_so_resubmit_maps_ids(tmp_path, monkeypatch):
    """A submission that fails after filtering must not shift IDs for the retry."""
    import manga_translator.translators.common as common_module

    out_dir = tmp_path / "out"
    prompt_file = tmp_path / "prompt.yaml"
    prompt_file.write_text(
        yaml.safe_dump({"system_prompt": "Translate", "glossary": {}}, allow_unicode=True),
        encoding="utf-8",
    )
    p1 = _make_png(tmp_path / "p1.png")
    factory, _ = _make_stub_factory(
        {"p1.png": [_make_block("一", y=20), _make_block("二", y=80), _make_block("三", y=140)]}
    )
    cfg = _base_config(str(out_dir))
    cfg["translator"]["extract_glossary"] = True
    cfg["translator"]["high_quality_prompt_path"] = str(prompt_file)

    def _failing_merge(*_args, **_kwargs):
        raise OSError("disk full")

    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1], output_dir=str(out_dir))
        internal_job = mgr._jobs[job.job_id]
        batch, _ = mgr.next_translation_batch(job.job_id)
        assert [r.id for r in batch.pages[0].regions] == [1, 2, 3]

        with monkeypatch.context() as patch:
            patch.setattr(common_module, "merge_glossary_to_file_with_delta", _failing_merge)
            with pytest.raises(OSError):
                # The blank id=2 is filtered before the glossary merge fails.
                mgr.submit_translation_batch(
                    job.job_id, batch.batch_id,
                    [{"id": 1, "translation": "猫"}, {"id": 2, "translation": ""}, {"id": 3, "translation": "狗"}],
                    new_terms=[_glossary_term("Item", "聖剣", "圣剑")], return_next=False,
                )

        ctx = internal_job.pages[0].ctx
        assert [r.text for r in ctx.text_regions] == ["一", "二", "三"]
        assert all(not r.translation for r in ctx.text_regions)

        retry, _ = mgr.submit_translation_batch(
            job.job_id, batch.batch_id,
            [{"id": 1, "translation": "甲"}, {"id": 2, "translation": "乙"}, {"id": 3, "translation": "丙"}],
            return_next=False,
        )
        assert retry.status == "accepted", retry.errors
    finally:
        mgr.shutdown()


def test_workspace_info_reports_disabled_context_as_zero(tmp_path):
    cfg = _base_config(str(tmp_path / "out"))
    cfg["cli"]["context_size"] = 0
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg)
    try:
        assert mgr.get_workspace_info().context_size == 0
        del cfg["cli"]["context_size"]
        assert mgr.get_workspace_info().context_size == 3
    finally:
        mgr.shutdown()
