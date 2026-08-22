"""本地判题沙箱：subprocess + POSIX rlimit + wall-clock 超时。

设计说明：
- 单机模式（本地单人）："防事故"威胁模型——rlimit 防 CPU/内存/写盘事故。
- 服务器多用户模式（server-beta）：设置 judge_docker_image 后，所有编译与运行
  都在一次性 Docker 容器内执行（断网、只读 rootfs、内存/CPU/进程数限额、
  容器内无宿主 data/ 目录），防恶意代码读取配置或探测内网。
- 判定五态：AC / WA / TLE / MLE / RE（外加编译失败 CE）。
"""

from __future__ import annotations

import os
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

VERDICT_AC = "AC"
VERDICT_WA = "WA"
VERDICT_TLE = "TLE"
VERDICT_MLE = "MLE"
VERDICT_RE = "RE"
VERDICT_CE = "CE"

MAX_OUTPUT_BYTES = 1 << 20  # 1MB 输出上限
MAX_CODE_BYTES = 1 << 20
MAX_CASE_DATA_BYTES = 8 << 20
MAX_WORK_FILE_BYTES = 16 << 20  # Docker /work 单文件上限
MAX_WORKDIR_BYTES = 64 << 20  # Docker /work 总量上限
MAX_WORKDIR_ENTRIES = 4096  # 同时限制零字节文件/目录耗尽宿主 inode
MAX_SUBMISSION_WALL_S = 60.0  # 含编译与全部用例，避免一份提交长期占槽
_IO_POLL_SECONDS = 0.05
_PIPE_DRAIN_SECONDS = 2.0
_DOCKER_STARTUP_GRACE_SECONDS = 15.0

def _initial_docker_concurrency() -> int:
    try:
        return max(1, min(1024, int(os.environ.get(
            "PREPDOJO_JUDGE_CONCURRENCY", "2"))))
    except ValueError:
        return 2


# 直接调用 judge_submission 时的兜底；Web 服务启动时会用已验证配置覆盖。
_JUDGE_SEM = threading.BoundedSemaphore(_initial_docker_concurrency())


def configure_docker_concurrency(limit: int) -> None:
    """在开始接收请求前同步 Docker 运行槽与服务端全局判题配置。"""
    if type(limit) is not int or not 1 <= limit <= 1024:
        raise ValueError("Docker 判题并发必须是 1..1024 的整数")
    global _JUDGE_SEM
    _JUDGE_SEM = threading.BoundedSemaphore(limit)


class JudgeInfrastructureError(RuntimeError):
    """判题基础设施不可用，而非用户代码产生的 RE/CE。"""


def judge_backend_status(docker_image: str) -> dict:
    """只读探测 Docker CLI、daemon 与判题镜像，供健康检查使用。"""
    import shutil

    if not docker_image:
        return {
            "configured": False,
            "ready": False,
            "error": "未配置 judge_docker_image",
        }
    status = {"configured": True, "ready": False, "image": docker_image}
    if not shutil.which("docker"):
        status["error"] = "docker 客户端不存在"
        return status
    try:
        daemon = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        status["error"] = f"Docker daemon 探测失败: {e}"
        return status
    if daemon.returncode != 0:
        status["error"] = (
            daemon.stderr.strip()[:500] or "Docker daemon 不可用"
        )
        return status
    try:
        image = subprocess.run(
            ["docker", "image", "inspect", docker_image],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        status["error"] = f"判题镜像探测失败: {e}"
        return status
    if image.returncode != 0:
        status["error"] = image.stderr.strip()[:500] or "判题镜像不存在"
        return status
    status["ready"] = True
    status["daemon_version"] = daemon.stdout.strip()
    return status


@dataclass
class CaseResult:
    idx: int
    verdict: str
    time_ms: int
    stdout: str = ""
    stderr: str = ""
    expected: str = ""
    timed_out: bool = False


@dataclass
class JudgeResult:
    verdict: str
    cases: list[CaseResult] = field(default_factory=list)
    compile_error: str = ""
    max_time_ms: int = 0


_SANDBOX_EXEC = str(Path(__file__).with_name("_sandbox_exec.py"))


def _local_limited_command(
    command: list[str], mem_limit_mb: int, cpu_s: int, nproc: int = 64,
) -> list[str]:
    return [
        sys.executable, _SANDBOX_EXEC, str(mem_limit_mb), str(max(1, cpu_s)),
        str(nproc), str(MAX_OUTPUT_BYTES), "--", *command,
    ]


def normalize_output(s: str) -> str:
    """OJ 惯例：去每行行尾空白、去末尾空行。"""
    lines = [l.rstrip() for l in s.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """强制清理 proc 创建的整个会话；父进程已退出时仍会清理其后台子进程。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _close_pipe(pipe) -> None:
    if pipe is None:
        return
    try:
        pipe.close()
    except OSError:
        pass


def _bounded_process_io(
    proc: subprocess.Popen,
    stdin_bytes: bytes,
    timeout_s: float,
    terminate: Callable[[], None],
    monitor: Optional[Callable[[], Optional[str]]] = None,
    cleanup_on_parent_exit: Optional[Callable[[], None]] = None,
) -> tuple[bytes, bytes, int, bool, str]:
    """非阻塞地泵入 stdin，并对 stdout/stderr 分别做硬上限。

    返回 stdout、stderr、returncode、是否超时、主动终止原因。任何输出一旦
    越过上限都会立刻调用 terminate，不会先把无限输出缓存到宿主内存。
    """
    if timeout_s <= 0:
        raise ValueError("wall timeout 必须大于 0")

    sel = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    stdin_pos = 0
    stopped_reason = ""
    timed_out = False
    stop_sent = False
    parent_exit_cleaned = False
    drain_deadline: Optional[float] = None
    deadline = time.monotonic() + timeout_s

    def unregister_and_close(pipe) -> None:
        try:
            sel.unregister(pipe)
        except (KeyError, ValueError):
            pass
        _close_pipe(pipe)

    def request_stop(reason: str, is_timeout: bool = False) -> None:
        nonlocal stopped_reason, timed_out, stop_sent, drain_deadline
        if stop_sent:
            return
        stopped_reason = reason
        timed_out = is_timeout
        stop_sent = True
        terminate()
        drain_deadline = time.monotonic() + _PIPE_DRAIN_SECONDS

    for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        if pipe is None:
            continue
        os.set_blocking(pipe.fileno(), False)
        sel.register(pipe, selectors.EVENT_READ, name)
    if proc.stdin is not None:
        if stdin_bytes:
            os.set_blocking(proc.stdin.fileno(), False)
            sel.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            _close_pipe(proc.stdin)

    try:
        while True:
            now = time.monotonic()
            parent_done = proc.poll() is not None
            if parent_done and cleanup_on_parent_exit and not parent_exit_cleaned:
                cleanup_on_parent_exit()
                parent_exit_cleaned = True
                drain_deadline = now + _PIPE_DRAIN_SECONDS

            if not stop_sent:
                if not parent_done and now >= deadline:
                    request_stop("wall-clock 超时", is_timeout=True)
                elif monitor:
                    reason = monitor()
                    if reason:
                        request_stop(reason)

            registered = list(sel.get_map().values())
            readable_left = any(k.data in {"stdout", "stderr"} for k in registered)
            if parent_done and not readable_left:
                break
            if drain_deadline is not None and now >= drain_deadline:
                break

            wait_s = _IO_POLL_SECONDS
            if not stop_sent and not parent_done:
                wait_s = min(wait_s, max(0.0, deadline - now))
            events = sel.select(wait_s)
            for key, _ in events:
                pipe = key.fileobj
                if key.data == "stdin":
                    try:
                        written = os.write(pipe.fileno(), stdin_bytes[stdin_pos:stdin_pos + 65536])
                        stdin_pos += written
                        if stdin_pos >= len(stdin_bytes):
                            unregister_and_close(pipe)
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, OSError):
                        unregister_and_close(pipe)
                    continue

                try:
                    chunk = os.read(pipe.fileno(), 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    unregister_and_close(pipe)
                    continue
                buf = buffers[key.data]
                room = MAX_OUTPUT_BYTES - len(buf)
                if room > 0:
                    buf.extend(chunk[:room])
                if len(chunk) > room:
                    request_stop(f"{key.data} 输出超过 {MAX_OUTPUT_BYTES} 字节限制")

            if proc.poll() is not None and proc.stdin is not None:
                try:
                    sel.get_key(proc.stdin)
                except (KeyError, ValueError):
                    pass
                else:
                    unregister_and_close(proc.stdin)
    finally:
        for key in list(sel.get_map().values()):
            unregister_and_close(key.fileobj)
        sel.close()

    if proc.poll() is None:
        terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    return (
        bytes(buffers["stdout"]),
        bytes(buffers["stderr"]),
        proc.returncode if proc.returncode is not None else -9,
        timed_out,
        stopped_reason,
    )


def _decode_output(data: bytes) -> str:
    return data.decode(errors="replace")


def _append_diagnostic(stderr: str, diagnostic: str) -> str:
    if not diagnostic:
        return stderr
    marker = f"\n[PrepDojo] {diagnostic}"
    encoded = marker.encode()
    if len(encoded) >= MAX_OUTPUT_BYTES:
        return marker[-MAX_OUTPUT_BYTES:]
    keep = MAX_OUTPUT_BYTES - len(encoded)
    return stderr.encode(errors="replace")[:keep].decode(errors="replace") + marker


def _run_once(
    cmd: list[str],
    stdin_text: str,
    wall_timeout_s: float,
    mem_limit_mb: int,
    cwd: Optional[str] = None,
    docker_image: str = "",
    outer_timeout_s: Optional[float] = None,
) -> tuple[str, str, int, int, bool]:
    """返回 (stdout, stderr, returncode, wall_ms, timed_out)。

    docker_image 非空时在一次性容器内执行（服务器多用户模式）；
    为空时本地直接执行 + rlimit（单机模式）。
    """
    if docker_image:
        return _run_once_docker(
            cmd, stdin_text, wall_timeout_s, mem_limit_mb, cwd, docker_image,
            outer_timeout_s=outer_timeout_s)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            _local_limited_command(
                cmd, mem_limit_mb, int(wall_timeout_s) + 1),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return "", f"解释器/编译器不存在: {e}", 127, 0, False
    try:
        out_b, err_b, rc, timed_out, stopped_reason = _bounded_process_io(
            proc,
            stdin_text.encode(),
            min(wall_timeout_s, outer_timeout_s)
            if outer_timeout_s is not None else wall_timeout_s,
            terminate=lambda: _kill_process_group(proc),
            cleanup_on_parent_exit=lambda: _kill_process_group(proc),
        )
        wall_ms = int((time.monotonic() - start) * 1000)
        err = _append_diagnostic(_decode_output(err_b), stopped_reason)
        return _decode_output(out_b), err, rc, wall_ms, timed_out
    finally:
        # 即使主进程正常退出，也清掉同一进程组内仍存活的后台子进程。
        _kill_process_group(proc)


def _docker_wrap(
    cmd: list[str], cwd: Optional[str], wall_timeout_s: float,
    mem_limit_mb: int, image: str,
) -> tuple[str, list[str]]:
    """把宿主命令包装成一次性容器命令。cwd 挂载为 /work，宿主路径重映射。"""
    cwd_path = Path(cwd or ".").resolve()
    cwd_str = str(cwd_path)

    def remap(a: str) -> str:
        path = Path(a)
        if not path.is_absolute():
            return a
        try:
            relative = path.resolve(strict=False).relative_to(cwd_path)
        except (OSError, RuntimeError, ValueError):
            return a
        return "/work" if relative == Path(".") else f"/work/{relative.as_posix()}"

    precise_timeout = f"{max(wall_timeout_s, 0.001):.3f}s"
    inner = ["timeout", "-k", "0.2s", precise_timeout] + [remap(a) for a in cmd]
    name = "prepdojo-j-" + uuid.uuid4().hex[:12]
    uid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "1000:1000"
    dcmd = [
        "docker", "run", "--pull", "never", "-i",
        "--name", name,
        "--init",
        "--network", "none",            # 断网：不能探内网 / 外传数据
        "--read-only",                  # 只读 rootfs
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
        "--memory", f"{mem_limit_mb}m",
        "--memory-swap", f"{mem_limit_mb}m",
        "--cpus", "1",
        "--pids-limit", "64",
        "--ulimit", f"fsize={MAX_WORK_FILE_BYTES}:{MAX_WORK_FILE_BYTES}",
        "--ulimit", "core=0:0",
        "--user", uid,                  # 宿主 uid：/work 产物可清理，且无 root
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-v", f"{cwd_str}:/work:rw",
        "-w", "/work",
        image, *inner,
    ]
    return name, dcmd


def _workdir_limit_reason(cwd: Optional[str]) -> Optional[str]:
    """监控 bind mount 的真实占用与条目数；不跟随软链接。"""
    root = Path(cwd or ".")
    total = 0
    entries = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            entries += len(dirnames) + len(filenames)
            if entries > MAX_WORKDIR_ENTRIES:
                return f"/work 条目数超过 {MAX_WORKDIR_ENTRIES} 个限制"
            dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
            for filename in filenames:
                path = Path(dirpath) / filename
                try:
                    file_stat = path.stat(follow_symlinks=False)
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                if file_stat.st_size > MAX_WORK_FILE_BYTES:
                    return f"/work 单文件超过 {MAX_WORK_FILE_BYTES} 字节限制"
                total += file_stat.st_size
                if total > MAX_WORKDIR_BYTES:
                    return f"/work 总量超过 {MAX_WORKDIR_BYTES} 字节限制"
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return None


def _docker_stop(name: str, proc: subprocess.Popen) -> None:
    try:
        subprocess.run(
            ["docker", "kill", name], capture_output=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    _kill_process_group(proc)


def _docker_state(name: str) -> dict:
    import json

    try:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", name],
            capture_output=True, timeout=5, check=False,
        )
    except FileNotFoundError as e:
        raise JudgeInfrastructureError("docker 客户端不存在，请安装 Docker") from e
    except (subprocess.SubprocessError, OSError) as e:
        raise JudgeInfrastructureError(f"无法读取 Docker 容器状态: {e}") from e
    if inspected.returncode != 0:
        detail = inspected.stderr.decode(errors="replace").strip()[:1000]
        raise JudgeInfrastructureError(f"无法读取 Docker 容器状态: {detail or 'docker inspect 失败'}")
    try:
        state = json.loads(inspected.stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise JudgeInfrastructureError("Docker 返回了无法解析的容器状态") from e
    if not isinstance(state, dict):
        raise JudgeInfrastructureError("Docker 返回了无效的容器状态")
    return state


def _docker_remove(name: str) -> Optional[str]:
    try:
        removed = subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as e:
        return str(e)
    if removed.returncode == 0:
        return None
    detail = removed.stderr.decode(errors="replace").strip()
    if "no such container" in detail.lower():
        return None
    return detail[:1000] or "docker rm 失败"


def _run_once_docker(
    cmd: list[str], stdin_text: str, wall_timeout_s: float,
    mem_limit_mb: int, cwd: Optional[str], image: str,
    outer_timeout_s: Optional[float] = None,
) -> tuple[str, str, int, int, bool]:
    start = time.monotonic()
    name, dcmd = _docker_wrap(cmd, cwd, wall_timeout_s, mem_limit_mb, image)
    try:
        with _JUDGE_SEM:
            try:
                proc = subprocess.Popen(
                    dcmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, cwd=cwd,
                    start_new_session=True,
                )
            except FileNotFoundError as e:
                raise JudgeInfrastructureError("docker 客户端不存在，请安装 Docker") from e
            try:
                out_b, err_b, rc, timed_out, stopped_reason = _bounded_process_io(
                    proc,
                    stdin_text.encode(),
                    min(wall_timeout_s + _DOCKER_STARTUP_GRACE_SECONDS,
                        outer_timeout_s)
                    if outer_timeout_s is not None
                    else wall_timeout_s + _DOCKER_STARTUP_GRACE_SECONDS,
                    terminate=lambda: _docker_stop(name, proc),
                    monitor=lambda: _workdir_limit_reason(cwd),
                )
                wall_ms = int((time.monotonic() - start) * 1000)
                out = _decode_output(out_b)
                err = _decode_output(err_b)
                try:
                    state = _docker_state(name)
                except JudgeInfrastructureError as e:
                    if rc == 125:
                        detail = (err or out).strip()[:1500]
                        raise JudgeInfrastructureError(
                            f"Docker 无法启动判题容器: {detail or e}"
                        ) from e
                    raise
                state_error = str(state.get("Error") or "").strip()
                if state_error:
                    raise JudgeInfrastructureError(f"Docker 容器启动失败: {state_error[:2000]}")
                if state.get("OOMKilled"):
                    stopped_reason = "容器达到内存上限（OOMKilled）"
                    err = _append_diagnostic(err, stopped_reason)
                elif rc == 137 and not timed_out and not stopped_reason:
                    # cgroup 有时只杀死负载子进程，State.OOMKilled 不一定置位；
                    # 内层 timeout 的真正超时固定返回 124，因此 137 优先按 MLE。
                    err = _append_diagnostic(err, "容器进程被 SIGKILL，可能达到内存上限")
                else:
                    err = _append_diagnostic(err, stopped_reason)

                low = err.lower()
                if rc in (126, 127) and (
                    "error response from daemon" in low
                    or "failed to create task" in low
                    or "failed to run command" in low
                    or "executable file not found" in low
                ):
                    raise JudgeInfrastructureError(f"Docker 判题镜像不完整: {err.strip()[:2000]}")
                return out, err, rc, wall_ms, timed_out
            finally:
                import sys

                active_error = sys.exc_info()[1]
                cleanup_error = _docker_remove(name)
                _kill_process_group(proc)
                if cleanup_error:
                    detail = f"无法清理 Docker 判题容器 {name}: {cleanup_error}"
                    if isinstance(active_error, JudgeInfrastructureError):
                        raise JudgeInfrastructureError(f"{active_error}；{detail}") from active_error
                    if active_error is None:
                        raise JudgeInfrastructureError(detail)
    except JudgeInfrastructureError:
        raise
    except (subprocess.SubprocessError, OSError) as e:
        raise JudgeInfrastructureError(f"Docker 判题执行失败: {e}") from e


def _classify(returncode: int, stderr: str, timed_out: bool) -> Optional[str]:
    if timed_out:
        return VERDICT_TLE
    low = stderr.lower()
    if (
        "memoryerror" in low
        or "out of memory" in low
        or "bad_alloc" in low
        or "oomkilled" in low
        or "达到内存上限" in stderr
    ):
        return VERDICT_MLE
    if "输出超过" in stderr or "/work " in stderr:
        return VERDICT_RE
    if returncode == 0:
        return None
    if returncode in (124, -9, -signal.SIGXCPU):
        return VERDICT_TLE
    return VERDICT_RE


CPP_EXTRA_INCLUDE = str(Path(__file__).resolve().parent / "cpp_include")


def resolve_compiler(preferred: str) -> Optional[str]:
    """按 配置 → g++ → clang++ → c++ 顺序取第一个宿主可用的编译器。

    判题环境差异大（macOS 只有 clang++，Docker 镜像只有 g++），
    不应因单个编译器名缺失而全部 CE。Docker 模式下镜像保证 g++。
    """
    import shutil as _sh

    for cand in (preferred, "g++", "clang++", "c++"):
        if cand and _sh.which(cand):
            return cand
    return None


def compile_cpp(code: str, workdir: Path, compiler: str = "clang++",
                docker_image: str = "",
                outer_timeout_s: Optional[float] = None) -> tuple[Optional[str], str]:
    # Docker 镜像契约固定为 g++；不能用宿主 PATH 决定容器内编译器。
    resolved = "g++" if docker_image else (resolve_compiler(compiler) or "g++")
    src = workdir / "main.cpp"
    src.write_text(code, encoding="utf-8")
    binary = workdir / "main_bin"
    include_args = [] if docker_image else ["-I", CPP_EXTRA_INCLUDE]
    out, err, rc, _, _ = _run_once(
        [resolved, "-std=c++17", "-O2", *include_args,
         "-o", str(binary), str(src)],
        stdin_text="", wall_timeout_s=30, mem_limit_mb=1024, cwd=str(workdir),
        docker_image=docker_image, outer_timeout_s=outer_timeout_s,
    )
    if rc != 0:
        return None, (err or out)[-4000:]
    if not binary.exists():
        if docker_image:
            raise JudgeInfrastructureError("Docker 判题镜像中的 g++ 未生成编译产物")
        return None, "编译器未生成可执行文件"
    return str(binary), ""


def judge_submission(
    code: str,
    language: str,
    cases: list[dict],
    time_limit_ms: int = 5000,
    mem_limit_mb: int = 512,
    cpp_compiler: str = "clang++",
    docker_image: str = "",
) -> JudgeResult:
    """cases: [{"input": "...", "output": "..."}]，逐用例运行。

    docker_image 非空时编译与运行均在容器沙箱内（服务器多用户模式）。
    """
    if not cases:
        raise ValueError("判题用例不能为空")
    if not isinstance(code, str) or len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError("代码必须是 UTF-8 字符串且不能超过 1MB")
    if not isinstance(language, str):
        raise ValueError("language 必须是字符串")
    if type(time_limit_ms) is not int or not 100 <= time_limit_ms <= 60_000:
        raise ValueError("time_limit_ms 必须是 100-60000 的整数")
    if type(mem_limit_mb) is not int or not 16 <= mem_limit_mb <= 4096:
        raise ValueError("mem_limit_mb 必须是 16-4096 的整数")
    if len(cases) > 200:
        raise ValueError("判题用例不能超过 200 个")
    case_bytes = 0
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or not isinstance(case.get("input"), str) \
                or not isinstance(case.get("output"), str):
            raise ValueError(f"用例 #{index} 的 input/output 必须是字符串")
        case_bytes += len(case["input"].encode("utf-8"))
        case_bytes += len(case["output"].encode("utf-8"))
    if case_bytes > MAX_CASE_DATA_BYTES:
        raise ValueError("全部判题用例不能超过 8MB")
    started = time.monotonic()
    deadline = started + MAX_SUBMISSION_WALL_S
    language = language.lower()
    with tempfile.TemporaryDirectory(prefix="prepdojo-judge-") as td:
        tdp = Path(td)
        if language in {"python", "python3", "py"}:
            entry = tdp / "main.py"
            entry.write_text(code, encoding="utf-8")
            cmd = ["python3", str(entry)]
        elif language in {"cpp", "c++", "cxx"}:
            binary, ce = compile_cpp(
                code, tdp, cpp_compiler, docker_image,
                outer_timeout_s=max(0.001, deadline - time.monotonic()))
            if binary is None:
                return JudgeResult(verdict=VERDICT_CE, compile_error=ce)
            cmd = [binary]
        else:
            return JudgeResult(verdict=VERDICT_CE, compile_error=f"不支持的语言: {language}")

        results: list[CaseResult] = []
        for i, case in enumerate(cases):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(CaseResult(
                    idx=i, verdict=VERDICT_TLE,
                    time_ms=int((time.monotonic() - started) * 1000), timed_out=True))
                return JudgeResult(
                    verdict=VERDICT_TLE, cases=results,
                    max_time_ms=max(r.time_ms for r in results))
            out, err, rc, wall_ms, timed_out = _run_once(
                cmd, case["input"], min(time_limit_ms / 1000, remaining),
                mem_limit_mb, cwd=str(tdp),
                docker_image=docker_image,
                outer_timeout_s=remaining,
            )
            verdict = _classify(rc, err, timed_out)
            if verdict is None:
                verdict = (
                    VERDICT_AC
                    if normalize_output(out) == normalize_output(case["output"])
                    else VERDICT_WA
                )
            results.append(
                CaseResult(idx=i, verdict=verdict, time_ms=wall_ms,
                           stdout=out[-2000:], stderr=err[-1000:],
                           expected=case["output"][-2000:], timed_out=timed_out)
            )
            if verdict != VERDICT_AC:
                overall = VERDICT_TLE if verdict == VERDICT_TLE else verdict
                return JudgeResult(
                    verdict=overall,
                    cases=results,
                    max_time_ms=max(r.time_ms for r in results),
                )

        return JudgeResult(
            verdict=VERDICT_AC,
            cases=results,
            max_time_ms=max((r.time_ms for r in results), default=0),
        )
