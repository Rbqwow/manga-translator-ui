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
    fallback_ctx = Context(
        image_name='fallback.png',
        skipped=True,
        skip_reason='render_error',
        skip_message='font error',
        skip_display_name=None,
        text_regions=[],
        input=SimpleNamespace(size=(8, 8)),
        result=None,
    )
    contexts = [
        _context('bad.png', skipped=True, reason='retry_exhausted', message='API unavailable'),
        _context('good.png'),
        _context('existing.png', skipped=True, reason='existing_output', message='already exists'),
        fallback_ctx,
    ]
    summary = build_skip_summary(contexts)
    assert summary.count == 3
    assert [item['original_path'] for item in summary.items] == ['bad.png', 'existing.png', 'fallback.png']
    assert [item['skip_reason'] for item in summary.items] == ['retry_exhausted', 'existing_output', 'render_error']
    assert [item['skip_message'] for item in summary.items] == ['API unavailable', 'already exists', 'font error']
