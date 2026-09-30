from __future__ import annotations

import shutil
import asyncio
import contextlib
from pathlib import Path


def _wipe_contents(p: Path) -> bool:
    if not p.is_dir() or p.is_symlink():
        return False
    for child in p.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                child.unlink()
    return True


async def clear_workdir_contents(workdir: str) -> bool:
    """清空目录内容但保留目录本身：active 子进程 cwd 仍指这条 inode，rmtree 会让它变僵尸目录。"""
    return await asyncio.to_thread(_wipe_contents, Path(workdir))
