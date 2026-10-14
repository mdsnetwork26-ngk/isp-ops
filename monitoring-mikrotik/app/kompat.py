"""Penyesuaian agar aplikasi tetap jalan di Python 3.8.

`asyncio.to_thread` baru ada di Python 3.9. Server ISP banyak yang masih
memakai Ubuntu 20.04 (Python 3.8), jadi kita pakai padanannya:
`loop.run_in_executor(None, ...)` - sama-sama memindahkan kerja blocking ke
thread lain agar event loop tidak terhenti.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

PUNYA_TO_THREAD = sys.version_info >= (3, 9)


async def to_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Padanan asyncio.to_thread untuk Python 3.8 ke atas."""
    if PUNYA_TO_THREAD:
        return await asyncio.to_thread(func, *args, **kwargs)  # novm  # type: ignore[attr-defined]
    loop = asyncio.get_event_loop()
    if kwargs:
        from functools import partial

        func = partial(func, **kwargs)  # type: ignore[assignment]
    return await loop.run_in_executor(None, func, *args)
