import _bootstrap  # noqa: F401

import pytest
from PyQt6.QtWidgets import QApplication

from desktop_qt_ui.core.config_models import CliSettings
from manga_translator.config import CliConfig
from ui.main_page.dynamic_settings import _INT_INPUT_BOUNDS


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('key', sorted(_INT_INPUT_BOUNDS))
def test_client_bounds_match_the_model_constraints(app, key):
    """P1-15: an out-of-range value was accepted, then silently reverted.

    `config_service` falls back with only a warning, and a concurrency of 0
    makes ConcurrentPipeline raise, so the widget must refuse it up front.
    """
    from PyQt6.QtGui import QIntValidator

    field = key.rsplit('.', 1)[-1]
    low, high = _INT_INPUT_BOUNDS[key]
    assert low <= high

    for model in (CliConfig, CliSettings):
        info = model.model_fields[field]
        for meta in info.metadata:
            limit = getattr(meta, 'ge', None)
            if limit is not None:
                assert low >= limit, f'{key}: client minimum {low} is below the model minimum {limit}'
            limit = getattr(meta, 'le', None)
            if limit is not None:
                assert high <= limit, f'{key}: client maximum {high} exceeds the model maximum {limit}'

    validator = QIntValidator(low, high)
    assert validator.bottom() == low and validator.top() == high


def test_translation_concurrency_rejects_zero_and_overflow(app):
    low, high = _INT_INPUT_BOUNDS['cli.translation_concurrency']
    assert (low, high) == (1, 32)
    assert CliConfig.model_fields['translation_concurrency'].metadata
    # The pipeline raises for this value, so it must never reach it.
    from manga_translator.utils.concurrent_pipeline import ConcurrentPipeline

    with pytest.raises(ValueError):
        ConcurrentPipeline(object(), batch_size=1, max_workers=0)
