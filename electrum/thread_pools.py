"""
Thread pool management for Electrum-Meowcoin.

Provides dedicated executors for wallet-heavy tasks (e.g. history rebuilds)
and for client/UI driven operations.  Separating these prevents expensive wallet
work from starving other components such as the synchronizer or verifier.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Optional


class ThreadPools:
    """Manages dedicated executors for wallet and client operations."""

    def __init__(self, *, wallet_workers: int = 20, client_workers: int = 50) -> None:
        self.wallet_executor = ThreadPoolExecutor(
            max_workers=wallet_workers, thread_name_prefix="WalletWorker"
        )
        self.client_executor = ThreadPoolExecutor(
            max_workers=client_workers, thread_name_prefix="ClientRequest"
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register executors with the asyncio loop."""
        self._loop = loop
        loop.set_default_executor(self.client_executor)

    async def run_in_wallet_thread(
        self, func: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any:
        if self._loop is None:
            raise RuntimeError("ThreadPools not initialised")
        return await self._loop.run_in_executor(
            self.wallet_executor, partial(func, *args, **kwargs)
        )

    async def run_in_client_thread(
        self, func: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any:
        if self._loop is None:
            raise RuntimeError("ThreadPools not initialised")
        return await self._loop.run_in_executor(
            self.client_executor, partial(func, *args, **kwargs)
        )

    def shutdown(self) -> None:
        self.wallet_executor.shutdown(wait=False)
        self.client_executor.shutdown(wait=False)


_shared_pools: Optional[ThreadPools] = None


def set_thread_pools(pools: ThreadPools) -> None:
    global _shared_pools
    _shared_pools = pools


def get_thread_pools(optional: bool = False) -> Optional[ThreadPools]:
    if _shared_pools is None and not optional:
        raise RuntimeError("Thread pools not initialised")
    return _shared_pools


def shutdown_thread_pools() -> None:
    global _shared_pools
    if _shared_pools is not None:
        _shared_pools.shutdown()
        _shared_pools = None


async def run_in_wallet_thread(
    func: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    pools = get_thread_pools(optional=True)
    loop = asyncio.get_running_loop()
    if pools is not None:
        return await pools.run_in_wallet_thread(func, *args, **kwargs)
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


async def run_in_client_thread(
    func: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    pools = get_thread_pools(optional=True)
    loop = asyncio.get_running_loop()
    if pools is not None:
        return await pools.run_in_client_thread(func, *args, **kwargs)
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))

