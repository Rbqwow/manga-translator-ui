"""Task-boundary cleanup, after all inference workers have stopped."""
import gc
import logging

from manga_translator.utils import model_cleanup
from manga_translator.utils.gpu_memory import release_gpu_memory

logger = logging.getLogger(__name__)

# Kept as a module attribute for callers that inspect the unloaded caches.
_MODEL_CACHES = model_cleanup.MODEL_CACHES

# Re-exported so existing imports keep working; the implementation lives in
# manga_translator so the server unloads exactly the same set of models.
_unload_model = model_cleanup.unload_model
_unload_hook_args = model_cleanup.unload_hook_args


async def cleanup_all_model_caches(unload_models: bool = False) -> int:
    """Keep reusable models unless unloading was requested; await their hooks."""
    return await model_cleanup.cleanup_model_caches(unload_models)


def cleanup_gpu_memory():
    return release_gpu_memory()


def cleanup_physical_memory():
    """
    释放物理内存（Windows特定）
    
    在Windows上优先调用 EmptyWorkingSet 强制释放物理内存，
    再回退到 SetProcessWorkingSetSize。
    对CPU模式特别重要。
    """
    try:
        import ctypes
        import os

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        process_handle = kernel32.OpenProcess(0x0400 | 0x0100, False, os.getpid())
        if not process_handle:
            return False

        try:
            if hasattr(psapi, "EmptyWorkingSet") and psapi.EmptyWorkingSet(process_handle):
                return True
            if kernel32.SetProcessWorkingSetSize(process_handle, -1, -1):
                return True
        finally:
            kernel32.CloseHandle(process_handle)
    except Exception:
        pass  # 非Windows系统忽略
    return False


async def full_memory_cleanup(log_callback=None, unload_models: bool = False):
    """Release models if requested, then collect cycles and unused GPU memory."""
    caches_cleared = await cleanup_all_model_caches(unload_models=unload_models)
    gc.collect()
    result = {
        'caches_cleared': caches_cleared,
        'gpu_cleared': cleanup_gpu_memory(),
        'physical_memory_released': cleanup_physical_memory(),
        'models_unloaded': unload_models,
    }

    # The caller passes this callback so the user can see what happened; it
    # used to be accepted and dropped, leaving "cleanup finished" invisible.
    if log_callback is not None:
        try:
            log_callback(
                f"Memory cleanup finished: {result['caches_cleared']} model cache(s) cleared, "
                f"GPU memory released={result['gpu_cleared']}, "
                f"working set trimmed={result['physical_memory_released']}"
            )
        except Exception:
            logger.warning('Memory cleanup log callback failed', exc_info=True)
    return result
