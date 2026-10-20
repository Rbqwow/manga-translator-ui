import _bootstrap  # noqa: F401

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from manga_translator.config import CliConfig, Config
from manga_translator.manga_translator import MangaTranslator


def _textline():
    return SimpleNamespace(text='source')


def build_translator(monkeypatch, *, skip_no_text, stage, tmp_path):
    """Drive the two real textless early exits in `_translate_until_translation`.

    stage='detection' -> no textlines (``skip-no-regions``)
    stage='ocr'       -> textlines, but OCR returns nothing (``skip-no-text``)
    """
    translator = MangaTranslator(params={
        'use_gpu': False, 'filter_text_enabled': False, 'skip_no_text': skip_no_text,
    })
    translator._cleanup_gpu_memory = lambda **_kwargs: None
    translator._save_editor_base_if_needed = lambda *args: None
    calls = []

    async def detection(config, ctx):
        calls.append('detect')
        if stage == 'detection':
            return [], None, None
        return [_textline()], np.zeros((32, 32), dtype=np.uint8), np.zeros((32, 32), dtype=np.uint8)

    async def ocr(config, ctx):
        calls.append('ocr')
        return []

    async def merge(config, ctx):
        calls.append('merge')
        return [SimpleNamespace(text='source', translation='')]

    async def translate(batch, size, **kwargs):
        calls.append('translate')
        return batch

    async def mask(config, ctx):
        return np.zeros(ctx.img_rgb.shape[:2], dtype=np.uint8)

    monkeypatch.setattr(translator, '_run_detection', detection)
    monkeypatch.setattr(translator, '_run_ocr', ocr)
    monkeypatch.setattr(translator, '_run_textline_merge', merge)
    monkeypatch.setattr(translator, '_batch_translate_contexts', translate)
    monkeypatch.setattr(translator, '_run_mask_refinement', mask)
    monkeypatch.setattr(translator, '_run_inpainting', lambda *_: None)
    monkeypatch.setattr(translator, '_run_text_rendering', lambda *_: None)

    image = Image.new('RGB', (32, 32), 'white')
    image.name = str(tmp_path / 'page.png')
    translator._set_image_context(Config(), image)
    return translator, image, calls


@pytest.mark.parametrize('stage', ['detection', 'ocr'])
@pytest.mark.parametrize('skip_no_text', [False, True])
def test_textless_pages_are_reported_as_skipped_only_when_enabled(
    monkeypatch, tmp_path, stage, skip_no_text,
):
    """`cli.skip_no_text` was assigned but never read, so the switch did nothing.

    Detection must still run to learn a page is textless; the setting only
    decides how the page is counted and summarised afterwards.
    """
    translator, image, calls = build_translator(
        monkeypatch, skip_no_text=skip_no_text, stage=stage, tmp_path=tmp_path,
    )
    result = asyncio.run(translator._translate_until_translation(image, Config()))
    # Both branches still run the work that discovers the page is textless;
    # the switch only changes how the outcome is reported.
    expected = ['detect'] if stage == 'detection' else ['detect', 'ocr']
    assert calls == expected
    if skip_no_text:
        assert result.skipped is True
        assert result.skip_reason == 'no_text'
        assert result.skip_display_name == image.name
        assert result.success is True, 'a skipped textless page must not count as a failure'
        assert result.text_regions == []
    else:
        assert not result.get('skipped')
        assert result.get('skip_reason') is None
    assert result.result is not None, 'the output image is still written'
    image.close()


def test_text_pages_are_never_marked_skipped_by_the_switch(monkeypatch, tmp_path):
    translator, image, calls = build_translator(
        monkeypatch, skip_no_text=True, stage='ocr', tmp_path=tmp_path,
    )

    async def ocr_with_text(config, ctx):
        calls.append('ocr-with-text')
        return [_textline()]

    monkeypatch.setattr(translator, '_run_ocr', ocr_with_text)
    result = asyncio.run(translator._translate_until_translation(image, Config()))
    assert not result.get('skipped')
    image.close()
