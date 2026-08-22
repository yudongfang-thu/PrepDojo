"""本地判题子进程限额启动器。

此文件由 ``judge.py`` 以全新的 Python 进程执行；先设置 POSIX rlimit，再
``exec`` 用户程序。这样无需在多线程 Web 进程的 fork/exec 间隙运行
``preexec_fn``，避免其已知死锁风险。
"""

from __future__ import annotations

import os
import resource
import sys


def _set_limit(kind: int, soft: int, hard: int) -> None:
    try:
        _, current_hard = resource.getrlimit(kind)
        if current_hard != resource.RLIM_INFINITY:
            hard = min(hard, current_hard)
        soft = min(soft, hard)
        resource.setrlimit(kind, (soft, hard))
    except (ValueError, OSError):
        # macOS 不支持 RLIMIT_AS 等组合；其余可用限额仍继续设置。
        pass


def main(argv: list[str]) -> int:
    if len(argv) < 6 or argv[4] != "--":
        print("PrepDojo sandbox helper 参数错误", file=sys.stderr)
        return 126
    try:
        mem_mb, cpu_s, nproc, file_bytes = map(int, argv[:4])
    except ValueError:
        print("PrepDojo sandbox helper 限额参数错误", file=sys.stderr)
        return 126
    command = argv[5:]
    if not command:
        print("PrepDojo sandbox helper 缺少命令", file=sys.stderr)
        return 126

    if sys.platform != "darwin":
        _set_limit(resource.RLIMIT_AS, mem_mb << 20, mem_mb << 20)
        _set_limit(resource.RLIMIT_NPROC, nproc, nproc)
    _set_limit(resource.RLIMIT_CPU, cpu_s, cpu_s + 1)
    _set_limit(resource.RLIMIT_FSIZE, file_bytes, file_bytes)
    _set_limit(resource.RLIMIT_CORE, 0, 0)
    try:
        os.execvp(command[0], command)
    except FileNotFoundError:
        print(f"解释器/编译器不存在: {command[0]}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"无法执行程序: {exc}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
