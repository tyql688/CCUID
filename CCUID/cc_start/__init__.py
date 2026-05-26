import asyncio

from gsuid_core.logger import logger
from gsuid_core.server import on_core_start, on_core_shutdown

from ..utils.session import REGISTRY
from ..utils.acp.orphans import reap_orphans


@on_core_start
async def ccuid_start() -> None:
    reaped = await asyncio.to_thread(reap_orphans)
    if reaped:
        logger.info(f"[CCUID] 启动时回收 {reaped} 个 ACP 孤儿子进程")
    await REGISTRY.start_cleanup()


@on_core_shutdown(priority=-10)
async def ccuid_shutdown() -> None:
    await REGISTRY.shutdown()
