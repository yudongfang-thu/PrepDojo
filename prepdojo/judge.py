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
import resource
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VERDICT_AC = "AC"
VERDICT_WA = "WA"
VERDICT_TLE = "TLE"
VERDICT_MLE = "MLE"
VERDICT_RE = "RE"
VERDICT_CE = "CE"

MAX_OUTPUT_BYTES = 1 << 20  # 1MB 输出上限

# 服务器多人同时提交/编译时的全局并发闸（防把小机器打满）
_JUDGE_SEM = threading.BoundedSemaphore(int(os.environ.get("PREPDOJO_JUDGE_CONCURRENCY", "2")))


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


def _make_preexec(mem_limit_mb: int, cpu_s: int, nproc: int = 64):
    def _rl(res: int, soft: int, hard: int) -> None:
        try:
            resource.setrlimit(res, (soft, hard))
        except (ValueError, OSError):
            pass  # 平台不支持（如 macOS 的 RLIMIT_AS）则跳过

    def _limit():
        # macOS 不支持用 RLIMIT_AS 限制地址空间（设置必抛 ValueError），
        # 内存事故由 wall-clock 超时兜底；Linux 上启用完整限制。
        import sys as _sys

        if _sys.platform != "darwin":
            _rl(resource.RLIMIT_AS, mem_limit_mb << 20, mem_limit_mb << 20)
            # NPROC 在 macOS 是 per-user 全局限额，会误伤编译器等工具链，仅 Linux 启用
            _rl(resource.RLIMIT_NPROC, nproc, nproc)
        _rl(resource.RLIMIT_CPU, cpu_s, cpu_s + 1)
        _rl(resource.RLIMIT_FSIZE, MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES)
        _rl(resource.RLIMIT_CORE, 0, 0)
        os.setsid()

    return _limit


def normalize_output(s: str) -> str:
    """OJ 惯例：去每行行尾空白、去末尾空行。"""
    lines = [l.rstrip() for l in s.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _run_once(
    cmd: list[str],
    stdin_text: str,
    wall_timeout_s: float,
    mem_limit_mb: int,
    cwd: Optional[str] = None,
    docker_image: str = "",
) -> tuple[str, str, int, int, bool]:
    """返回 (stdout, stderr, returncode, wall_ms, timed_out)。

    docker_image 非空时在一次性容器内执行（服务器多用户模式）；
    为空时本地直接执行 + rlimit（单机模式）。
    """
    if docker_image:
        return _run_once_docker(cmd, stdin_text, wall_timeout_s, mem_limit_mb, cwd, docker_image)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            preexec_fn=_make_preexec(mem_limit_mb, int(wall_timeout_s) + 1),
        )
    except FileNotFoundError as e:
        return "", f"解释器/编译器不存在: {e}", 127, 0, False
    try:
        out_b, err_b = proc.communicate(input=stdin_text.encode(), timeout=wall_timeout_s)
        wall_ms = int((time.monotonic() - start) * 1000)
        out = out_b[:MAX_OUTPUT_BYTES].decode(errors="replace")
        err = err_b[:MAX_OUTPUT_BYTES].decode(errors="replace")
        return out, err, proc.returncode, wall_ms, False
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.communicate(timeout=5)
        except Exception:
            pass
        wall_ms = int((time.monotonic() - start) * 1000)
        return "", "wall-clock 超时", -9, wall_ms, True


def _docker_wrap(
    cmd: list[str], cwd: Optional[str], wall_timeout_s: float,
    mem_limit_mb: int, image: str,
) -> tuple[str, list[str]]:
    """把宿主命令包装成一次性容器命令。cwd 挂载为 /work，宿主路径重映射。"""
    cwd_str = str(Path(cwd or ".").resolve())

    def remap(a: str) -> str:
        return "/work" + a[len(cwd_str):] if a.startswith(cwd_str) else a

    inner = ["timeout", "-k", "2", f"{int(wall_timeout_s) + 1}s"] + [remap(a) for a in cmd]
    name = "prepdojo-j-" + uuid.uuid4().hex[:12]
    uid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "1000:1000"
    dcmd = [
        "docker", "run", "--rm", "-i",
        "--name", name,
        "--network", "none",            # 断网：不能探内网 / 外传数据
        "--read-only",                  # 只读 rootfs
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
        "--memory", f"{mem_limit_mb}m",
        "--memory-swap", f"{mem_limit_mb}m",
        "--cpus", "1",
        "--pids-limit", "64",
        "--user", uid,                  # 宿主 uid：/work 产物可清理，且无 root
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-v", f"{cwd_str}:/work",
        "-w", "/work",
        image, *inner,
    ]
    return name, dcmd


def _run_once_docker(
    cmd: list[str], stdin_text: str, wall_timeout_s: float,
    mem_limit_mb: int, cwd: Optional[str], image: str,
) -> tuple[str, str, int, int, bool]:
    start = time.monotonic()
    name, dcmd = _docker_wrap(cmd, cwd, wall_timeout_s, mem_limit_mb, image)
    try:
        with _JUDGE_SEM:
            try:
                proc = subprocess.Popen(
                    dcmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, cwd=cwd,
                )
            except FileNotFoundError:
                return "", "docker 不存在：请安装 Docker 或清空 judge_docker_image 配置", 127, 0, False
            # 内层 timeout 兜底 + 外层宽限（容器启动开销）；挂死时强杀容器
            try:
                out_b, err_b = proc.communicate(
                    input=stdin_text.encode(), timeout=wall_timeout_s + 20)
                wall_ms = int((time.monotonic() - start) * 1000)
                out = out_b[:MAX_OUTPUT_BYTES].decode(errors="replace")
                err = err_b[:MAX_OUTPUT_BYTES].decode(errors="replace")
                return out, err, proc.returncode, wall_ms, False
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
                try:
                    proc.kill()
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                wall_ms = int((time.monotonic() - start) * 1000)
                return "", "wall-clock 超时", -9, wall_ms, True
    except Exception as e:  # docker daemon 异常等
        return "", f"沙箱执行失败: {e}", 127, int((time.monotonic() - start) * 1000), False


def _classify(returncode: int, stderr: str, timed_out: bool) -> Optional[str]:
    if timed_out:
        return VERDICT_TLE
    if returncode == 0:
        return None
    low = stderr.lower()
    if "memoryerror" in low or "out of memory" in low or "bad_alloc" in low:
        return VERDICT_MLE
    if returncode in (124, -9, 137):  # 124=容器内 timeout；-9/137=SIGKILL（CPU rlimit 兑现）
        return VERDICT_TLE
    return VERDICT_RE


CPP_EXTRA_INCLUDE = str(Path(__file__).resolve().parent / "cpp_include")


def compile_cpp(code: str, workdir: Path, compiler: str = "clang++",
                docker_image: str = "") -> tuple[Optional[str], str]:
    src = workdir / "main.cpp"
    src.write_text(code, encoding="utf-8")
    binary = workdir / "main_bin"
    out, err, rc, _, _ = _run_once(
        [compiler, "-std=c++17", "-O2", "-I", CPP_EXTRA_INCLUDE,
         "-o", str(binary), str(src)],
        stdin_text="", wall_timeout_s=30, mem_limit_mb=1024, cwd=str(workdir),
        docker_image=docker_image,
    )
    if rc != 0 or not binary.exists():
        return None, (err or out)[-4000:]
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
    language = language.lower()
    with tempfile.TemporaryDirectory(prefix="prepdojo-judge-") as td:
        tdp = Path(td)
        if language in {"python", "python3", "py"}:
            entry = tdp / "main.py"
            entry.write_text(code, encoding="utf-8")
            cmd = ["python3", str(entry)]
        elif language in {"cpp", "c++", "cxx"}:
            binary, ce = compile_cpp(code, tdp, cpp_compiler, docker_image)
            if binary is None:
                return JudgeResult(verdict=VERDICT_CE, compile_error=ce)
            cmd = [binary]
        else:
            return JudgeResult(verdict=VERDICT_CE, compile_error=f"不支持的语言: {language}")

        results: list[CaseResult] = []
        for i, case in enumerate(cases):
            out, err, rc, wall_ms, timed_out = _run_once(
                cmd, case["input"], time_limit_ms / 1000, mem_limit_mb, cwd=str(tdp),
                docker_image=docker_image,
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
