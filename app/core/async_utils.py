"""Run bounded synchronous work without releasing its resources on cancellation."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from functools import partial
from weakref import ReferenceType, WeakKeyDictionary, ref

_gates: WeakKeyDictionary[asyncio.AbstractEventLoop, ReferenceType[asyncio.Semaphore]] = (
    WeakKeyDictionary()
)


async def blocking_call[T, **P](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Keep worker ownership until its real thread exits, including repeated cancel.

    Preserve serial media computation per event loop while letting I/O proceed.
    Callers must supply bounded operations. Cancelling an asyncio task cannot
    terminate a running OS thread, so its gate remains held until real exit.
    """
    loop = asyncio.get_running_loop()
    gate_ref = _gates.get(loop)
    gate = gate_ref() if gate_ref is not None else None
    if gate is None:
        gate = asyncio.Semaphore(1)
        # Weak values avoid retaining a closed loop through its bound semaphore.
        # Every queued/running call holds its own strong reference until exit.
        _gates[loop] = ref(gate)
    async with gate:
        context = contextvars.copy_context()
        future = loop.run_in_executor(None, context.run, partial(function, *args, **kwargs))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not future.cancelled():
                future.exception()  # Observe late worker errors; preserve caller cancellation.
            raise
