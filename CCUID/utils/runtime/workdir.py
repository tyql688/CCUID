from __future__ import annotations

import shutil
import asyncio
import contextlib
from pathlib import Path


async def clear_workdir_contents(workdir: str) -> bool:
    """清空目录内容但保留目录本身：active 子进程 cwd 仍指这条 inode，rmtree 会让它变僵尸目录。"""
    p = Path(workdir)
    if not p.exists() or not p.is_dir() or p.is_symlink():
        return False

    def _wipe() -> None:
        for child in p.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    child.unlink()

    await asyncio.to_thread(_wipe)
    return True
