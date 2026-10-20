from PIL import Image

from ..config import Colorizer
from .common import CommonColorizer, OfflineColorizer
from .model_api_colorizer import GeminiColorizer, OpenAIColorizer


def _create_manga_colorization_v2(*args, **kwargs):
    from .manga_colorization_v2 import MangaColorizationV2
    return MangaColorizationV2(*args, **kwargs)


def __getattr__(name):
    if name == "MangaColorizationV2":
        from .manga_colorization_v2 import MangaColorizationV2
        globals()[name] = MangaColorizationV2
        return MangaColorizationV2
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


COLORIZERS = {
    Colorizer.mc2: _create_manga_colorization_v2,
    Colorizer.openai_colorizer: OpenAIColorizer,
    Colorizer.gemini_colorizer: GeminiColorizer,
}
colorizer_cache = {}
NON_CACHED_COLORIZERS = {
    Colorizer.openai_colorizer,
    Colorizer.gemini_colorizer,
}

def get_colorizer(key: Colorizer, *args, **kwargs) -> CommonColorizer:
    if key not in COLORIZERS:
        raise ValueError(f'Could not find colorizer for: "{key}". Choose from the following: %s' % ','.join(COLORIZERS))
    if key in NON_CACHED_COLORIZERS:
        return COLORIZERS[key](*args, **kwargs)
    if not colorizer_cache.get(key):
        upscaler = COLORIZERS[key]
        colorizer_cache[key] = upscaler(*args, **kwargs)
    return colorizer_cache[key]

async def prepare(key: Colorizer):
    upscaler = get_colorizer(key)
    if isinstance(upscaler, OfflineColorizer):
        await upscaler.download()

async def dispatch(key: Colorizer, device: str = 'cpu', **kwargs) -> Image.Image:
    colorizer = get_colorizer(key)
    if isinstance(colorizer, OfflineColorizer):
        await colorizer.load(device)
    return await colorizer.colorize(**kwargs)

async def unload(key: Colorizer):
    colorizer = colorizer_cache.pop(key, None)
    if isinstance(colorizer, OfflineColorizer):
        await colorizer.unload()
