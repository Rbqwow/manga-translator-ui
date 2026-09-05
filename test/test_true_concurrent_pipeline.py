import _bootstrap  # noqa: F401

import asyncio
import os
import tempfile
import threading
import time
from typing import List

from manga_translator.utils import Context, TextBlock
from manga_translator.utils.concurrent_pipeline import ConcurrentPipeline
from manga_translator.translators.common import merge_glossary_to_file
from manga_translator.translators.prompt_loader import load_prompt_file


class _MockTranslator:
    def __init__(self):
        self.max_in_flight = 0
        self.current_in_flight = 0
        self._flight_lock = threading.Lock()
        self.completed_order = []
        self.batch_sizes_received = []
        self._resume_context_pages = []
        self._resume_context_order = {}
        self.ignore_errors = False
        self.batch_concurrent = True

    def _check_cancelled(self):
        return None

    def _append_resume_context_before(self, image_path):
        pass

    async def _report_progress(self, state):
        pass

    def _mark_context_failure(self, ctx, err, stage=""):
        ctx.translation_error = str(err)
        return ctx

    async def _batch_translate_contexts(self, batch: List[tuple], batch_size: int):
        with self._flight_lock:
            self.current_in_flight += 1
            if self.current_in_flight > self.max_in_flight:
                self.max_in_flight = self.current_in_flight
            self.batch_sizes_received.append(len(batch))

        ctx, config = batch[0]
        # 根据批次首张图片名称模拟不同延迟：img_0 所在批次较慢(0.2s)，img_2 所在批次较快(0.05s)
        delay = 0.05 if "img_2" in ctx.image_name else 0.2
        await asyncio.sleep(delay)

        with self._flight_lock:
            for ctx_item, _ in batch:
                self.completed_order.append(ctx_item.image_name)
            self.current_in_flight -= 1

        for ctx_item, _ in batch:
            for r in ctx_item.text_regions:
                r.translation = f"Translated: {r.text}"
        return batch


def test_concurrent_pipeline_batch_concurrency():
    """验证多图打包与批次并发执行深度融合：batch_size=2, 总图数4 -> 2个并发批次(每批2张)"""
    translator = _MockTranslator()
    pipeline = ConcurrentPipeline(translator, batch_size=2, concurrency=3)
    assert pipeline.batch_size == 2
    assert pipeline.concurrency == 3

    # 构造 4 个模拟图片上下文
    contexts = []
    file_names = [f"img_{i}.png" for i in range(4)]
    for name in file_names:
        ctx = Context()
        ctx.image_name = name
        tb = TextBlock([[0, 0, 10, 10]], [f"Hello from {name}"])
        ctx.text_regions = [tb]
        ctx._initial_region_ids = {id(tb)}
        pipeline.base_contexts[name] = ctx
        # 预先标记 inpaint_done，以便翻译完成后能立即推入 render_queue
        pipeline.inpaint_done[name] = True
        contexts.append(ctx)

    # 模拟检测+OCR 直接生产任务到 translation_queue
    for ctx in contexts:
        pipeline.translation_queue.put((ctx.image_name, None))
    pipeline.detection_ocr_done = True
    pipeline.total_images = len(contexts)

    # 禁用其它无关线程，仅运行翻译线程
    pipeline._detection_ocr_thread = lambda *_args: None
    pipeline._inpaint_thread = lambda: None
    pipeline._render_thread = lambda: None

    asyncio.run(pipeline._translation_async())

    # 1. 验证批次打包特性：收到的批次大小均为 2
    print(f"Batch sizes received: {translator.batch_sizes_received}")
    assert translator.batch_sizes_received == [2, 2], f"Expected batches of size [2, 2], got {translator.batch_sizes_received}"

    # 2. 验证真正的批次并发度：最大同时在途批次达到 2
    print(f"Max in-flight concurrent batches: {translator.max_in_flight}")
    assert translator.max_in_flight == 2, f"Expected 2 in-flight batches, got {translator.max_in_flight}"

    # 3. 验证非阻塞并发：较快的第 2 个批次 (img_2, img_3) 先于第 1 个批次 (img_0, img_1) 完成
    print(f"Completed order: {translator.completed_order}")
    assert translator.completed_order[:2] == ["img_2.png", "img_3.png"], "Faster batch (img_2, img_3) should finish before batch (img_0, img_1)"

    # 4. 验证 render_queue 中已包含全部 4 张图片
    render_items = []
    while not pipeline.render_queue.empty():
        render_items.append(pipeline.render_queue.get_nowait()[0].image_name)
    assert len(render_items) == 4, f"Expected 4 items in render_queue, got {len(render_items)}"
    print("Concurrent batch pipeline dispatch and packing verified successfully!")


def test_concurrent_pipeline_single_image_mode():
    """验证当 batch_size=1 时，自动退化为纯单图并发模式"""
    translator = _MockTranslator()
    pipeline = ConcurrentPipeline(translator, batch_size=1, concurrency=3)
    assert pipeline.batch_size == 1
    assert pipeline.concurrency == 3

    contexts = []
    file_names = [f"img_{i}.png" for i in range(3)]
    for name in file_names:
        ctx = Context()
        ctx.image_name = name
        tb = TextBlock([[0, 0, 10, 10]], [f"Hello from {name}"])
        ctx.text_regions = [tb]
        ctx._initial_region_ids = {id(tb)}
        pipeline.base_contexts[name] = ctx
        pipeline.inpaint_done[name] = True
        contexts.append(ctx)

    for ctx in contexts:
        pipeline.translation_queue.put((ctx.image_name, None))
    pipeline.detection_ocr_done = True
    pipeline.total_images = len(contexts)

    pipeline._detection_ocr_thread = lambda *_args: None
    pipeline._inpaint_thread = lambda: None
    pipeline._render_thread = lambda: None

    asyncio.run(pipeline._translation_async())

    assert translator.batch_sizes_received == [1, 1, 1]
    assert translator.max_in_flight == 3
    print("Single image concurrency mode verified successfully!")


def test_multithreaded_glossary_merging():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = os.path.join(tmpdir, "prompt.yaml")
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write('system_prompt: ""\nglossary:\n  Person: []\n  Location: []\n  Org: []\n  Item: []\n  Skill: []\n  Creature: []\n')

        # 10 个线程并发向同一个 YAML 文件合并不同分类的术语
        terms_per_thread = 5
        thread_count = 10
        errors = []

        def worker(tid):
            try:
                terms = [
                    {
                        "category": "Person" if i % 2 == 0 else "Location",
                        "original": f"Term_T{tid}_I{i}",
                        "aliases": [
                            {"original": f"Term_T{tid}_I{i}", "translations": [{"text": f"Trans_T{tid}_I{i}"}]}
                        ],
                    }
                    for i in range(terms_per_thread)
                ]
                ok = merge_glossary_to_file(yaml_path, terms)
                if not ok:
                    errors.append(f"Worker {tid} merge returned False")
            except Exception as e:
                errors.append(f"Worker {tid} exception: {e}")
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Glossary merge errors: {errors}"

        # 验证读取合并后的 YAML 文件无损坏且所有术语完整保留
        data = load_prompt_file(yaml_path)
        assert data is not None, "Failed to parse merged YAML file"
        persons = data["glossary"]["Person"]
        locations = data["glossary"]["Location"]
        total_merged = len(persons) + len(locations)
        expected_count = thread_count * terms_per_thread
        print(f"Merged terms count: {total_merged} / expected {expected_count}")
        assert total_merged == expected_count, f"Expected {expected_count} terms, got {total_merged}"
        print("Multithreaded glossary merging thread safety verified successfully!")


def test_prompt_respects_user_config_in_concurrent_mode():
    """验证并发模式下尊重用户配置，不再强制开启 extract_glossary 与覆盖提示词路径"""
    from manga_translator.manga_translator import MangaTranslator
    from manga_translator.config import Config

    translator = MangaTranslator({"batch_concurrent": True})
    assert translator._is_effective_batch_concurrent() is True

    config = Config()
    config.translator.extract_glossary = False
    config.translator.high_quality_prompt_path = None

    ctx = Context()
    asyncio.run(translator._load_and_prepare_prompts(config, ctx))

    assert config.translator.extract_glossary is False, "extract_glossary should NOT be auto-enabled in concurrent mode"
    assert config.translator.high_quality_prompt_path is None, "high_quality_prompt_path should NOT default to dict/prompt_example.yaml"
    assert ctx.custom_prompt_json is None, "custom_prompt_json should remain None when no prompt path specified"
    print("Prompt respects user config in concurrent mode verified successfully!")


def test_concurrent_pipeline_stop_and_cancellation():
    class _HangingTranslator(_MockTranslator):
        async def _batch_translate_contexts(self, batch: List[tuple], batch_size: int):
            await asyncio.sleep(10.0)
            return batch

    translator = _HangingTranslator()
    pipeline = ConcurrentPipeline(translator, batch_size=3)

    contexts = []
    for i in range(3):
        ctx = Context()
        ctx.image_name = f"hang_{i}.png"
        tb = TextBlock([[0, 0, 10, 10]], ["Hang"])
        ctx.text_regions = [tb]
        ctx._initial_region_ids = {id(tb)}
        pipeline.base_contexts[ctx.image_name] = ctx
        pipeline.translation_queue.put((ctx.image_name, None))
        contexts.append(ctx)
    pipeline.total_images = 3

    async def run_and_stop():
        async def stop_after_delay():
            await asyncio.sleep(0.15)
            pipeline.stop()

        await asyncio.gather(
            pipeline._translation_async(),
            stop_after_delay(),
        )

    t0 = time.time()
    asyncio.run(run_and_stop())
    duration = time.time() - t0

    assert pipeline.translation_thread_done is True
    assert duration < 2.0, f"Shutdown took {duration}s, expected < 2.0s"
    print(f"Pipeline stop and cancellation cleanly finished in {duration:.3f}s!")


def test_glossary_context_propagation_across_tasks():
    with tempfile.TemporaryDirectory() as tmpdir:
        prompt_yaml = os.path.join(tmpdir, "prompt.yaml")
        with open(prompt_yaml, "w", encoding="utf-8") as f:
            f.write('system_prompt: ""\nglossary:\n  Person: []\n  Location: []\n  Org: []\n  Item: []\n  Skill: []\n  Creature: []\n')

        from manga_translator.manga_translator import MangaTranslator
        from manga_translator.config import Config
        from manga_translator.translators.common import CommonTranslator

        translator_mgr = MangaTranslator({"batch_concurrent": True})

        # 1. 模拟 Task 1：并发提取出了新术语并写入提示词文件
        new_terms = [
            {
                "category": "Person",
                "original": "ル菲",
                "aliases": [{"original": "ル菲", "translations": [{"text": "路飞"}]}],
            }
        ]
        merge_glossary_to_file(prompt_yaml, new_terms)

        # 2. 模拟 Task 2：用户配置了自定义提示词文件
        config2 = Config()
        config2.translator.high_quality_prompt_path = prompt_yaml
        config2.translator.extract_glossary = True
        ctx2 = Context()
        asyncio.run(translator_mgr._load_and_prepare_prompts(config2, ctx2))

        # 3. 验证 Task 2 的 ctx.custom_prompt_json 已经加载了 Task 1 提取的术语
        assert ctx2.custom_prompt_json is not None
        person_terms = ctx2.custom_prompt_json["glossary"]["Person"]
        assert len(person_terms) == 1
        assert person_terms[0]["original"] == "ル菲"

        class _ConcreteTranslator(CommonTranslator):
            async def _translate(self, from_lang, to_lang, queries, ctx=None):
                return queries

        common = _ConcreteTranslator()
        sys_prompt = common._build_system_prompt(
            "JPN",
            "CHS",
            custom_prompt_json=ctx2.custom_prompt_json,
            extract_glossary=True
        )
        assert "ル菲" in sys_prompt
        assert "路飞" in sys_prompt
        print("Glossary context propagation across concurrent tasks verified successfully!")


if __name__ == "__main__":
    test_concurrent_pipeline_batch_concurrency()
    test_concurrent_pipeline_single_image_mode()
    test_multithreaded_glossary_merging()
    test_prompt_respects_user_config_in_concurrent_mode()
    test_concurrent_pipeline_stop_and_cancellation()
    test_glossary_context_propagation_across_tasks()
    print("ALL CONCURRENT TRANSLATION TESTS PASSED!")
