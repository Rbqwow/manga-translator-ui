import _bootstrap  # noqa: F401

import pytest

from desktop_qt_ui.core.config_models import CliSettings
from manga_translator.config import CliConfig
from ui.main_page.dynamic_settings import _INT_INPUT_BOUNDS


@pytest.mark.parametrize('key', sorted(_INT_INPUT_BOUNDS))
def test_client_bounds_match_the_model_constraints(key):
    """P1-15: an out-of-range value was accepted, then silently reverted.

    `config_service` falls back with only a warning, and a concurrency of 0
    makes ConcurrentPipeline raise, so the widget must refuse it up front.
    """
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


_CUSTOM_VALIDATED_CLI_INT_FIELDS = {'compression_effort'}


def test_all_bounded_cli_int_fields_have_client_validators():
    for model in (CliConfig, CliSettings):
        for field, info in model.model_fields.items():
            if field in _CUSTOM_VALIDATED_CLI_INT_FIELDS or info.annotation is not int:
                continue
            has_bound = any(
                getattr(meta, attr, None) is not None
                for meta in info.metadata
                for attr in ('ge', 'le')
            )
            if has_bound:
                assert f'cli.{field}' in _INT_INPUT_BOUNDS, (
                    f'{model.__name__}.{field} has ge/le constraints but is missing from _INT_INPUT_BOUNDS'
                )
