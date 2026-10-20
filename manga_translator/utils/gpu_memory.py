"""Release unused accelerator allocations at batch and model boundaries."""
import logging

logger = logging.getLogger(__name__)


def release_gpu_memory(device=None, *, aggressive=True):
    import torch

    released = False
    device = str(device) if device is not None else None
    try:
        if (device is None or device.startswith('cuda')) and torch.cuda.is_available():
            torch.cuda.synchronize()
            if aggressive:
                torch.cuda.empty_cache()
                if hasattr(torch.cuda, 'ipc_collect'):
                    torch.cuda.ipc_collect()
            released = True
        # Per-image cleanup need not stall every other pipeline stage.
        if aggressive and (device is None or device.startswith('mps')) and torch.backends.mps.is_available():
            torch.mps.synchronize()
            torch.mps.empty_cache()
            released = True
    except Exception:
        logger.debug('Accelerator memory cleanup failed', exc_info=True)
    return released
