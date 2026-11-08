"""原子写与文件锁：history.json / settings.json 的写盘基础设施。

为什么需要这一层：

* **固定临时文件名会互相截断**。原来两个 store 都写 `history.json.tmp`：两个
  进程（或同一进程里两个实例）同时保存时，A 刚写完还没 replace，B 就把同名
  临时文件截断重写，A 的 replace 可能搬走 B 的半截内容——用户看到的是
  「历史记录偶尔少几条」，而且事后完全无法复现。临时文件名必须每次唯一。
* **没有 fsync 的 replace 挡不住掉电**。`os.replace` 只保证「目录项替换」是
  原子的，文件内容可能还留在系统缓存里。断电后最坏的情况是「文件名在、内容是
  空的」，比根本没有历史更糟——下一次运行会把它当成空基线，静默丢掉全部记录。
  所以写完先 `flush()` + `os.fsync()`，再 replace。
* **读-合并-写是 TOCTOU**。settings.json 是按单键保存的（切主题只写 theme），
  `save()` 必须先把磁盘上的现状读出来合并。两个线程同时这样做就会互相覆盖：
  后写的那个用自己读到的旧快照覆盖掉先写那个的新键。整段必须串行化。

锁的粒度是「一次完整读改写」，不是「进程生命周期」：GUI 里读盘在后台线程、
保存偏好在主线程，两件事都是几十毫秒的短操作，串行化对用户完全无感，却能
根治「设置自己变回去」「历史少一条」这类偶发问题。

设计上刻意只用标准库、不用 msvcrt：锁文件用 `O_CREAT | O_EXCL` 原子创建，
Windows 与 POSIX 语义一致，出错时的降级路径也简单（抢不到锁就退化成无锁写，
宁可冒一次覆盖风险，也不能让用户点「刷新」之后卡死）。
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------

LOCK_SUFFIX = ".lock"
DEFAULT_LOCK_TIMEOUT = 5.0          # 抢锁最多等多久（秒），超时降级为无锁写
LOCK_RETRY_SECONDS = 0.02           # 两次重试之间的间隔
LOCK_STALE_SECONDS = 30.0           # 超过这个年龄的锁文件视为上一个进程崩溃留下的

# 读文本时该捕的异常。UnicodeDecodeError 不是 OSError 的子类，早先只写
# `except OSError` 的地方碰到非 UTF-8 的 json 会把异常一路冒到界面层。
TEXT_READ_ERRORS: tuple[type[BaseException], ...] = (
    OSError, UnicodeDecodeError, ValueError,
)


def temp_path_for(path: Path) -> Path:
    """同目录下的唯一临时文件名。

    必须留在同一个目录里：`os.replace` 只有跨同一文件系统才是原子的，
    `%TEMP%` 与数据目录可能不在同一个盘上。
    """
    return path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp"


def write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    """把 text 原子地写成 path：唯一临时名 → fsync → replace。

    任何一步失败都会清掉临时文件再抛：宁可这次保存失败（界面会提示），
    也不能在数据目录里留一堆半截的 .tmp 文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path_for(path)
    try:
        with open(tmp, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def read_text(path: Path, encoding: str = "utf-8") -> str | None:
    """读文本；读不到（不存在 / 无权限 / 不是 UTF-8）一律返回 None。

    只用于「读坏了就退回默认值」的场景；需要区分「文件不存在」与「文件损坏」
    的地方（history.load）自己 try，别用这个。
    """
    try:
        return path.read_text(encoding=encoding)
    except TEXT_READ_ERRORS:
        return None


# ---------------------------------------------------------------------------
# 文件锁
# ---------------------------------------------------------------------------


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + LOCK_SUFFIX)


def _is_stale(lock_path: Path) -> bool:
    """锁文件是不是上一个进程崩溃后留下的。

    只按年龄判断，不看 pid：倒不是因为 pid 难取，而是 pid 会被系统复用，
    拿它判断「持有者还活着没有」反而更容易误判成活锁。
    """
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return True                     # 文件刚被别的进程删掉，当它已经过期
    return age > LOCK_STALE_SECONDS


def _discard(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


def _acquire(lock_path: Path, timeout: float) -> int | None:
    """抢锁，成功返回文件描述符，失败返回 None。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _is_stale(lock_path):
                # 上一个进程写完没来得及删（崩了 / 被杀了），直接接管
                _discard(lock_path)
                continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(LOCK_RETRY_SECONDS)
        except OSError:
            # 目录不可写、锁文件被杀毒软件占住等等：直接降级成无锁写
            return None


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT,
              enabled: bool = True) -> Iterator[bool]:
    """对 path 加一把跨进程的写锁，yield 是否真的拿到了锁。

    拿不到锁**不抛异常**：调用方照常写，只是失去串行化保护。锁是用来防偶发
    覆盖的，不是用来在保存失败时给用户看报错的——保存本身能不能成，由随后的
    写盘结果决定。
    """
    if not enabled:
        yield False
        return
    lock_path = _lock_path(path)
    handle = _acquire(lock_path, timeout)
    acquired = handle is not None
    try:
        yield acquired
    finally:
        if acquired:
            try:
                os.close(handle)        # type: ignore[arg-type]
            except OSError:
                pass
            _discard(lock_path)


__all__ = [
    "LOCK_SUFFIX", "DEFAULT_LOCK_TIMEOUT", "LOCK_RETRY_SECONDS", "LOCK_STALE_SECONDS",
    "TEXT_READ_ERRORS", "temp_path_for", "write_text", "read_text", "file_lock",
]
