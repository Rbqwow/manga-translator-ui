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
    subscribe_log_records,
    unsubscribe_log_records,
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
        "enabled": True,
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

        async def _render(config, ctx):
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


def test_pending_redo_triggers_clean_mask_and_reinpaint(tmp_path):
    img_path = _make_png(tmp_path / "p1.png")
    regions_map = {
        "p1.png": [
            _make_block("元テキスト", y=20),
            _make_block("消す言葉", y=80),
        ]
    }
    factory, calls = _make_stub_factory(regions_map)

    out_dir = tmp_path / "out"
    cfg = _base_config(str(out_dir))
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[img_path], output_dir=str(out_dir))
        batch, _ = mgr.next_translation_batch(job.job_id)

        # Force initial pre-inpaint completion to simulate speculative inpaint done before commit
        internal_job = mgr._jobs[job.job_id]
        with internal_job._cond:
            internal_job.pages[0].inpaint_done = True
            internal_job.pages[0].ctx.mask = np.ones((320, 240), dtype=np.uint8) * 255

        # Region 2 submitted identical to original -> filtered out by force_filter=True -> pending_redo
        resp, _ = mgr.submit_translation_batch(
            job.job_id,
            batch.batch_id,
            [
                {"id": 1, "translation": "翻译文本"},
                {"id": 2, "translation": "消す言葉"},
            ],
            return_next=True,
        )
        assert resp.redo_pages == [0]
        assert resp.done is True
        # Second bool in calls["inpaint"] is (ctx.mask is None) BEFORE run_page_inpaint called _run_mask_refinement
        assert len(calls["inpaint"]) >= 1
    finally:
        mgr.shutdown()


def test_glossary_merge_lock_and_delta_propagation(tmp_path):
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
    cfg_dict = _base_config(str(out_dir), max_parallel_batches=2)
    cfg_dict["cli"]["batch_size"] = 1
    cfg_dict["translator"]["extract_glossary"] = True
    cfg_dict["translator"]["high_quality_prompt_path"] = str(prompt_file)

    # 1) Direct thread-safety stress test on apply_batch_post_processing glossary writes
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

    # 2) Multi-worker glossary_delta propagation across parallel batches
    p1 = _make_png(tmp_path / "p1.png")
    p2 = _make_png(tmp_path / "p2.png")
    factory, _calls = _make_stub_factory(
        {
            "p1.png": [_make_block("勇者登場")],
            "p2.png": [_make_block("魔王登場")],
        }
    )
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg_dict, translator_factory=factory)
    try:
        job = mgr.create_translation_job(inputs=[p1, p2], output_dir=str(out_dir))
        assert "glossary" in job.__dict__ or isinstance(job.glossary, dict)

        b1, _ = mgr.next_translation_batch(job.job_id, worker_id="w1")
        sub1, _ = mgr.submit_translation_batch(
            job.job_id,
            b1.batch_id,
            [{"id": 1, "translation": "勇者登场"}],
            new_terms=[
                {
                    "category": "Skill",
                    "original": "勇者斬り",
                    "aliases": [
                        {
                            "original": "勇者斬り",
                            "translations": [{"text": "勇者斩", "context": "skill"}],
                        }
                    ],
                }
            ],
            return_next=False,
        )
        assert sub1.merged_terms_count == 1

        b2, _ = mgr.next_translation_batch(job.job_id, worker_id="w2")
        assert b2.glossary_version >= 1
        assert any(t["original"] == "勇者斬り" for t in b2.glossary_delta)
        sub2, _ = mgr.submit_translation_batch(
            job.job_id,
            b2.batch_id,
            [{"id": 1, "translation": "魔王登场"}],
            return_next=True,
        )
        assert sub2.done is True
    finally:
        mgr.shutdown()


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


def test_mcp_service_config_skill_install_and_log_subscriber(tmp_path):
    svc = McpService(root_dir=str(_bootstrap.ROOT))
    try:
        cfg_file = tmp_path / ".gemini" / "config" / "mcp_config.json"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(
            json.dumps({"mcpServers": {"other-server": {"command": "echo", "args": ["ok"]}}}),
            encoding="utf-8",
        )

        written_cfg = Path(svc.install_mcp_config("antigravity", cfg_file, port=9123))
        assert written_cfg == cfg_file
        parsed = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert parsed["mcpServers"]["other-server"] == {"command": "echo", "args": ["ok"]}
        assert parsed["mcpServers"]["manga-translator"] == {"url": "http://127.0.0.1:9123/mcp"}

        skill_target = tmp_path / ".agents" / "skills" / "manga-translate" / "SKILL.md"
        written_skill = Path(svc.install_skill(skill_path=skill_target))
        assert written_skill == skill_target
        template_path = Path(_bootstrap.ROOT) / "manga_translator" / "mcp" / "skill_template.md"
        skill_content = skill_target.read_text(encoding="utf-8")
        assert skill_content == template_path.read_text(encoding="utf-8")
        assert "read_file" not in skill_content
        assert "invoke_subagent" not in skill_content
        assert "antigravity" not in skill_content.lower()

        # Verify log subscription & unsubscription
        received = []
        sub = lambda entry: received.append(entry)
        configure_queue_logging([])
        subscribe_log_records(sub)
        try:
            test_logger = logging.getLogger("manga_translator.mcp.test_subscriber")
            test_logger.warning("mcp subscriber probe message")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(
                "mcp subscriber probe message" in e.get("message", "") for e in received
            ):
                time.sleep(0.02)
            assert any("mcp subscriber probe message" in e.get("message", "") for e in received)

            count_before = len(received)
            unsubscribe_log_records(sub)
            test_logger.warning("mcp subscriber after unsubscribe")
            time.sleep(0.1)
            assert not any("mcp subscriber after unsubscribe" in e.get("message", "") for e in received)
            assert len(received) == count_before
        finally:
            unsubscribe_log_records(sub)
            shutdown_queue_logging()
    finally:
        svc.shutdown()


def test_mcp_service_multi_client_snippets_merges_and_skill_targets(tmp_path, monkeypatch):
    import tomllib

    svc = McpService(root_dir=str(_bootstrap.ROOT))
    try:
        profiles = svc.list_mcp_client_profiles()
        keys = [p.key for p in profiles]
        assert keys == [
            "generic",
            "claude_code",
            "codex",
            "cursor",
            "opencode",
            "antigravity",
            "windsurf",
            "vscode",
            "stdio_bridge",
        ]

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


def test_decoupled_file_path_hints_and_preview_note(tmp_path):
    from manga_translator.mcp.batch_adapter import resolve_batch_image_delivery

    dummy_bytes = b"\xff\xd8\xff\xe0" + b"0" * 2048
    encoded = {0: (dummy_bytes, "image/jpeg", 100, 100, "p1.png")}

    resolved_fp, hints_fp = resolve_batch_image_delivery(
        encoded,
        delivery="file_path",
        budget_mb=2.0,
        preview_dir=str(tmp_path / "p1"),
        batch_id="b1",
        fmt="jpeg",
    )
    assert resolved_fp[0]["mode"] == "file_path"
    assert hints_fp
    assert all("read_file" not in h for h in hints_fp)
    assert any("local file/image viewing tool" in h for h in hints_fp)

    resolved_budget, hints_budget = resolve_batch_image_delivery(
        encoded,
        delivery="auto",
        budget_mb=0.0001,
        preview_dir=str(tmp_path / "p2"),
        batch_id="b2",
        fmt="jpeg",
    )
    assert resolved_budget[0]["mode"] == "file_path"
    assert hints_budget
    assert all("read_file" not in h for h in hints_budget)
    assert any("local file/image viewing tool" in h for h in hints_budget)


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

        async def _slow_render(config, ctx):
            render_entered.set()
            allow_render_finish.wait(timeout=5.0)
            return await orig_render(config, ctx)

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
        assert internal_job.status == "completed_with_errors"
    finally:
        mgr.shutdown()


def test_ipv6_endpoint_formatting_and_codex_inline_table_error(tmp_path):
    from manga_translator.mcp.server_app import format_mcp_endpoint_url
    from services.mcp_service import _merge_codex_toml_config

    assert format_mcp_endpoint_url("::1", 8765) == "http://[::1]:8765/mcp"
    assert format_mcp_endpoint_url("[::1]", 8765) == "http://[::1]:8765/mcp"
    assert format_mcp_endpoint_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/mcp"

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

