"""Shared model-cache unloading for the desktop app, the CLI, and the server.

The caches below are module-level dictionaries owned by ``manga_translator``,
so both entry points must unload exactly the same set. Keeping one
implementation avoids the previous situation where the server forgot
MangaLens (which lives outside ``detector_cache``) and had its own copy of the
hook-signature probing.
"""

import gc
import inspect
import logging
import sys

logger = logging.getLogger(__name__)

# Every cache that owns inference models, in unload order.
# ``translator_cache`` is intentionally absent: it only ever holds the stateless
# `none`/`original` translators, while every model-backed translator is created
# per request, so including it counted non-models as released models.
MODEL_CACHES = (
    ('ocr', 'ocr_cache'),
    ('detection', 'detector_cache'),
    ('inpainting', 'inpainter_cache'),
    ('upscaling', 'upscaler_cache'),
    ('colorization', 'colorizer_cache'),
)

MANGALENS_MODULE = 'manga_translator.utils.mangalens_detector'


def unload_hook_args(hook) -> tuple:
    """Pass a device only to hooks that declare one.

    ``OfflineTranslator`` overrides the base ``unload()`` with ``unload(device)``
    while keeping the same behaviour, so calling every hook without arguments
    made translator unloading raise ``TypeError`` and left the largest models
    resident. The value is a compatibility placeholder, matching
    ``manga_translator/translators/__init__.py``.
    """
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        return ()
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            return ()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            return ('cuda',) if parameter.default is parameter.empty else ()
    return ()


async def unload_model(model):
    """Run a model's public unload hook, awaiting it when necessary."""
    hook = getattr(model, 'unload', None) or getattr(model, '_unload', None)
    if hook is None:
        return
    result = hook(*unload_hook_args(hook))
    if inspect.isawaitable(result):
        await result


async def cleanup_model_caches(unload_models: bool = False) -> int:
    """Unload every cached model and return how many were released.

    A model whose hook fails stays in its cache so the unload can be retried;
    removing it would drop the only owner without freeing anything.
    """
    if not unload_models:
        return 0

    cleanup_count = 0
    for module_name, cache_name in MODEL_CACHES:
        module = sys.modules.get(f'manga_translator.{module_name}')
        cache = getattr(module, cache_name, {})
        for key in list(cache):
            model = cache[key]
            try:
                await unload_model(model)
            except Exception:
                logger.warning('Failed to unload %s/%s', module_name, key, exc_info=True)
            else:
                cache.pop(key, None)
                cleanup_count += 1
            finally:
                del model

    # MangaLens is kept outside the regular detector cache, so it needs its
    # own removal from the module attribute.
    module = sys.modules.get(MANGALENS_MODULE)
    model = getattr(module, '_default_detector', None)
    if model is not None:
        try:
            model.unload_model()
        except Exception:
            logger.warning('Failed to unload MangaLens', exc_info=True)
        else:
            module._default_detector = None
            cleanup_count += 1

    gc.collect()
    return cleanup_count


def cleanup_model_caches_sync(unload_models: bool = False) -> int:
    """Blocking wrapper for callers that are not on an event loop."""
    import asyncio

    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(cleanup_model_caches(unload_models))

    # Already inside a loop: run the coroutine on a private one.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(
            asyncio.run, cleanup_model_caches(unload_models)
        ).result()
