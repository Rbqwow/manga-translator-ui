"""Run image encoding without blocking the server or abandoning its resources."""

import asyncio


async def run_image_encoding(func, *args, **kwargs):
    """Finish native encoding before the caller closes images or temporary files.

    Cancelling an asyncio task cannot stop an encoder's thread. Shield the worker
    and drain it, including on repeated cancellation, before propagating cancel.
    The event loop remains available while the worker finishes.
    """
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # A failed encoder must not replace cancellation or leave an unobserved
        # task exception after the caller starts cleaning up its resources.
        if not worker.cancelled():
            worker.exception()
        raise
