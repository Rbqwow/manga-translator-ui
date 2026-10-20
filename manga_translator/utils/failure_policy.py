"""Task-wide accounting for pages skipped after request retries are exhausted."""

import os
import threading
from uuid import uuid4

from .retry import RetryExhaustedError, is_permanent_api_error


class FailureSkipLimitExceeded(RuntimeError):
    """The configured task-wide failure skip limit has been reached."""


def is_retry_exhausted(error):
    """Whether ``error`` means "requests were retried until the budget ran out".

    A retry budget cannot rescue a wrong API key, an unknown model name, or a
    disabled account, so those failures stay hard errors. The provider loops
    wrap every exhausted failure in ``RetryExhaustedError`` (and
    ``APIRotationExhaustedError`` subclasses it), which previously made
    configuration mistakes look skippable: with ``max_failure_skips=0`` a whole
    batch could report success while every page had been skipped instead.
    """
    seen = set()
    current = error
    exhausted = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, RetryExhaustedError):
            exhausted = True
        current = getattr(current, "original_error", None) or current.__cause__
    return exhausted and not is_permanent_api_error(error)


class FailureSkipPolicy:
    def __init__(self, enabled=False, max_skips=3):
        self.enabled = enabled
        self.max_skips = max(0, int(max_skips))
        self._lock = threading.Lock()
        self._items = {}
        self._unnamed_page_count = 0
        self._limit_error = None

    @property
    def skipped_items(self):
        with self._lock:
            return list(self._items.values())

    def check_limit(self):
        with self._lock:
            if self._limit_error is not None:
                raise self._limit_error

    def can_skip(self, error):
        self.check_limit()
        return self.enabled and is_retry_exhausted(error)

    def record(self, ctx, error):
        if not self.can_skip(error):
            return False
        with self._lock:
            if self._limit_error is not None:
                raise self._limit_error
            if ctx.image_name:
                path = str(ctx.image_name)
                key = ("path", os.path.normcase(os.path.abspath(path)))
            else:
                # Uploaded/in-memory images have no path. Keep a stable token
                # on both the context and its source so repeated failures still
                # count once, including after context cleanup releases input.
                source = getattr(ctx, "input", None)
                page_id = getattr(ctx, "_failure_skip_page_id", None)
                if page_id is None:
                    page_id = getattr(source, "_failure_skip_page_id", None) or uuid4().hex
                    ctx._failure_skip_page_id = page_id
                    if source is not None:
                        try:
                            source._failure_skip_page_id = page_id
                        except (AttributeError, TypeError):
                            pass
                key = ("unnamed", page_id)
                previous = self._items.get(key)
                if previous is None:
                    self._unnamed_page_count += 1
                    path = f"Unnamed image {self._unnamed_page_count}"
                else:
                    path = previous["original_path"]
            ctx.skipped = True
            ctx.skip_reason = "retry_exhausted"
            ctx.skip_message = ctx.translation_error
            ctx.skip_display_name = path
            ctx.success = False
            ctx.text_regions = []
            self._items.setdefault(key, {
                "original_path": path,
                "skip_message": ctx.skip_message,
                "skip_reason": ctx.skip_reason,
            })
            # The limit is checked *after* this page is recorded, so `max_skips`
            # counts allowed skips: max_skips=1 permits one skip and aborts on
            # it, which makes "1" effectively zero-tolerance for continuing.
            # 0 disables the limit entirely.
            if self.max_skips and len(self._items) >= self.max_skips:
                self._limit_error = FailureSkipLimitExceeded(
                    f"Failure skip limit reached ({len(self._items)}/{self.max_skips}). "
                    f"Last error: {ctx.skip_message}"
                )
                raise self._limit_error from error
        return True
