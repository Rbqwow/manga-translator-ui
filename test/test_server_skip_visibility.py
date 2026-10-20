import _bootstrap  # noqa: F401

from types import SimpleNamespace

from manga_translator.server.to_json import build_skip_summary, to_translation
from manga_translator.utils import Context


def _context(name, *, skipped=False, reason=None, message=None):
    return Context(
        image_name=name,
        skipped=skipped,
        skip_reason=reason,
        skip_message=message,
        skip_display_name=name,
        text_regions=[],
        input=SimpleNamespace(size=(8, 8)),
        result=None,
    )


def test_skipped_page_is_distinguishable_from_a_textless_page():
    """Both used to serialize as zero regions, so the client could not tell
    a skipped page from "no text here" and history silently dropped it."""
    skipped = to_translation(_context('a.png', skipped=True, reason='retry_exhausted', message='timeout'))
    textless = to_translation(_context('b.png'))

    assert skipped.skipped is True
    assert skipped.skip_reason == 'retry_exhausted'
    assert skipped.skip_message == 'timeout'
    assert textless.skipped is False
    assert textless.skip_reason is None


def test_skip_summary_matches_the_desktop_summary_shape():
    contexts = [
        _context('bad.png', skipped=True, reason='retry_exhausted', message='API unavailable'),
        _context('good.png'),
        _context('existing.png', skipped=True, reason='existing_output', message='already exists'),
    ]
    summary = build_skip_summary(contexts)
    assert summary.count == 2
    assert [item['original_path'] for item in summary.items] == ['bad.png', 'existing.png']
    assert summary.items[0]['skip_reason'] == 'retry_exhausted'


def test_no_skips_produces_an_empty_summary():
    assert build_skip_summary([_context('a.png')]).count == 0
    assert build_skip_summary([]).count == 0
