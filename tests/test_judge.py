"""判题沙箱测试：五态判定逐项验证。"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prepdojo import judge as judge_module  # noqa: E402
from prepdojo.judge import (  # noqa: E402
    MAX_OUTPUT_BYTES, JudgeInfrastructureError, VERDICT_AC, VERDICT_CE,
    VERDICT_MLE, VERDICT_RE, VERDICT_TLE, VERDICT_WA, _docker_wrap,
    judge_backend_status,
    judge_submission, normalize_output,
)

CASES_OK = [{"input": "3\n1 2 3\n", "output": "6\n"}]


def test_normalize_output():
    assert normalize_output("1  \n2\n\n\n") == "1\n2"
    assert normalize_output("a\r\nb\r\n") == "a\nb"


def test_ac_python():
    code = "n=int(input());print(sum(map(int,input().split())))"
    r = judge_submission(code, "python", CASES_OK)
    assert r.verdict == VERDICT_AC, r.cases


def test_ac_cpp():
    code = "#include <bits/stdc++.h>\nint main(){int n;std::cin>>n;long long s=0,x;\nfor(int i=0;i<n;++i){std::cin>>x;s+=x;}std::cout<<s<<std::endl;}"
    r = judge_submission(code, "cpp", CASES_OK)
    assert r.verdict == VERDICT_AC, (r.verdict, r.compile_error)


def test_wa_python():
    code = "n=int(input());print(999)"
    r = judge_submission(code, "python", CASES_OK)
    assert r.verdict == VERDICT_WA


def test_re_python():
    code = "raise RuntimeError('boom')"
    r = judge_submission(code, "python", CASES_OK)
    assert r.verdict == VERDICT_RE


def test_ce_cpp():
    code = "int main(){ syntax error here"
    r = judge_submission(code, "cpp", CASES_OK)
    assert r.verdict == VERDICT_CE
    assert r.compile_error


def test_tle_python():
    code = "while True: pass"
    r = judge_submission(code, "python", CASES_OK, time_limit_ms=1500)
    assert r.verdict == VERDICT_TLE


def test_ce_bad_language():
    r = judge_submission("x", "rust", CASES_OK)
    assert r.verdict == VERDICT_CE


def test_multi_case_stops_at_first_failure():
    cases = [
        {"input": "3\n1 2 3\n", "output": "6\n"},
        {"input": "4\n1 2 3 4\n", "output": "10\n"},
        {"input": "5\n1 2 3 4 5\n", "output": "15\n"},
    ]
    code = "n=int(input());print(sum(map(int,input().split())))"
    r = judge_submission(code, "python", cases)
    assert r.verdict == VERDICT_AC and len(r.cases) == 3
    bad = "n=int(input());print(sum(map(int,input().split())) if n!=4 else 0)"
    r2 = judge_submission(bad, "python", cases)
    assert r2.verdict == VERDICT_WA and len(r2.cases) == 2  # 第 2 个用例停下


def test_empty_cases_are_rejected():
    with pytest.raises(ValueError, match="用例不能为空"):
        judge_submission("print(1)", "python", [])


@pytest.mark.parametrize(("fd", "stream"), [(1, "stdout"), (2, "stderr")])
def test_output_flood_is_stopped_before_timeout(fd, stream):
    code = f"import os\nwhile True: os.write({fd}, b'x' * 65536)"
    started = time.monotonic()
    result = judge_submission(
        code, "python", [{"input": "", "output": ""}], time_limit_ms=5000,
    )
    assert result.verdict == VERDICT_RE
    assert f"{stream} 输出超过" in result.cases[0].stderr
    assert len(result.cases[0].stdout.encode()) <= MAX_OUTPUT_BYTES
    assert time.monotonic() - started < 2


def test_normal_exit_cleans_background_process_group(tmp_path):
    marker = tmp_path / "background-child-survived"
    child = (
        "import time; from pathlib import Path; time.sleep(0.4); "
        f"Path({str(marker)!r}).write_text('alive')"
    )
    code = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print('ok')\n"
    )
    result = judge_submission(code, "python", [{"input": "", "output": "ok"}])
    assert result.verdict == VERDICT_AC
    time.sleep(0.7)
    assert not marker.exists()


def test_timeout_cleans_background_process_group(tmp_path):
    marker = tmp_path / "timeout-child-survived"
    child = (
        "import time; from pathlib import Path; time.sleep(0.5); "
        f"Path({str(marker)!r}).write_text('alive')"
    )
    code = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "while True: pass\n"
    )
    result = judge_submission(
        code, "python", [{"input": "", "output": ""}], time_limit_ms=200,
    )
    assert result.verdict == VERDICT_TLE
    time.sleep(0.7)
    assert not marker.exists()


def test_memory_error_is_mle():
    result = judge_submission(
        "raise MemoryError('out of memory')", "python",
        [{"input": "", "output": ""}],
    )
    assert result.verdict == VERDICT_MLE


def test_docker_timeout_keeps_fractional_limit():
    _, command = _docker_wrap(["python3", "/tmp/main.py"], "/tmp", 1.25, 128, "image")
    timeout_pos = command.index("timeout")
    assert command[timeout_pos:timeout_pos + 4] == ["timeout", "-k", "0.2s", "1.250s"]


def test_docker_paths_under_symlinked_workdir_are_remapped(tmp_path):
    """macOS 的 /var -> /private/var 别名不能把宿主路径带进容器。"""
    real_workdir = tmp_path / "real-workdir"
    real_workdir.mkdir()
    alias_workdir = tmp_path / "workdir-alias"
    alias_workdir.symlink_to(real_workdir, target_is_directory=True)

    _, python_command = _docker_wrap(
        ["python3", str(alias_workdir / "main.py")],
        str(alias_workdir), 1.0, 128, "image")
    assert python_command[-2:] == ["python3", "/work/main.py"]
    assert f"{real_workdir.resolve()}:/work:rw" in python_command

    _, cpp_command = _docker_wrap(
        ["g++", "-o", str(alias_workdir / "main_bin"),
         str(alias_workdir / "main.cpp")],
        str(alias_workdir), 30.0, 1024, "image")
    assert cpp_command[-4:] == [
        "g++", "-o", "/work/main_bin", "/work/main.cpp"]


def test_nproc_limit_accounts_for_existing_uid_threads(monkeypatch):
    from prepdojo import _sandbox_exec

    monkeypatch.setattr(_sandbox_exec, "_current_uid_task_count", lambda: 130)
    assert _sandbox_exec._nproc_limit(64) == 194


def test_outer_budget_can_be_stricter_than_case_limit():
    started = time.monotonic()
    _, _, _, _, timed_out = judge_module._run_once(
        [sys.executable, "-c", "while True: pass"], "",
        wall_timeout_s=1.0, mem_limit_mb=512, outer_timeout_s=0.1)
    assert timed_out is True
    assert time.monotonic() - started < 0.8


def test_local_runner_does_not_use_thread_unsafe_preexec():
    command = judge_module._local_limited_command(["python3", "main.py"], 128, 2)
    assert command[0] == sys.executable
    assert command[-3:] == ["--", "python3", "main.py"]
    assert "preexec_fn" not in Path(judge_module.__file__).read_text(encoding="utf-8")


def test_unconfigured_judge_backend_is_not_ready():
    assert judge_backend_status("") == {
        "configured": False,
        "ready": False,
        "error": "未配置 judge_docker_image",
    }


def test_workdir_total_monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(judge_module, "MAX_WORK_FILE_BYTES", 100)
    monkeypatch.setattr(judge_module, "MAX_WORKDIR_BYTES", 12)
    (tmp_path / "a").write_bytes(b"a" * 8)
    (tmp_path / "b").write_bytes(b"b" * 8)
    assert "/work 总量超过" in judge_module._workdir_limit_reason(str(tmp_path))
    assert judge_module._classify(
        0, "[PrepDojo] /work 总量超过限制", False) == VERDICT_RE


def test_workdir_entry_monitor_blocks_inode_flood(tmp_path, monkeypatch):
    monkeypatch.setattr(judge_module, "MAX_WORKDIR_ENTRIES", 2)
    for index in range(3):
        (tmp_path / f"empty-{index}").touch()
    assert "/work 条目数超过" in judge_module._workdir_limit_reason(str(tmp_path))
    assert judge_module._classify(
        0, "[PrepDojo] /work 条目数超过限制", False) == VERDICT_RE


def test_docker_compile_always_uses_gpp(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, stdin_text, wall_timeout_s, mem_limit_mb, cwd=None,
                 docker_image="", outer_timeout_s=None):
        seen.append(cmd)
        output = Path(cmd[cmd.index("-o") + 1])
        output.write_bytes(b"binary")
        return "", "", 0, 1, False

    monkeypatch.setattr(judge_module, "_run_once", fake_run)
    binary, error = judge_module.compile_cpp(
        "int main(){}", tmp_path, compiler="host-only-clang++",
        docker_image="judge-image",
    )
    assert binary and not error
    assert seen[0][0] == "g++"


def _install_fake_docker(tmp_path: Path, script_body: str, monkeypatch) -> None:
    executable = tmp_path / "docker"
    executable.write_text("#!/bin/sh\n" + script_body, encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")


def test_docker_run_failure_raises_infrastructure_error(tmp_path, monkeypatch):
    _install_fake_docker(
        tmp_path,
        'if [ "$1" = run ]; then echo "docker: daemon unavailable" >&2; exit 125; fi\n'
        "exit 0\n",
        monkeypatch,
    )
    with pytest.raises(JudgeInfrastructureError, match="daemon unavailable"):
        judge_submission(
            "print(1)", "python", [{"input": "", "output": "1"}],
            docker_image="fake-image",
        )


def test_docker_oom_state_is_classified_as_mle(tmp_path, monkeypatch):
    _install_fake_docker(
        tmp_path,
        'if [ "$1" = run ]; then exit 137; fi\n'
        'if [ "$1" = inspect ]; then echo \'{"OOMKilled":true,"Error":""}\'; exit 0; fi\n'
        "exit 0\n",
        monkeypatch,
    )
    result = judge_submission(
        "print(1)", "python", [{"input": "", "output": "1"}],
        docker_image="fake-image",
    )
    assert result.verdict == VERDICT_MLE
    assert "OOMKilled" in result.cases[0].stderr


def test_user_exit_125_remains_runtime_error(tmp_path, monkeypatch):
    _install_fake_docker(
        tmp_path,
        'if [ "$1" = run ]; then exit 125; fi\n'
        'if [ "$1" = inspect ]; then echo \'{"OOMKilled":false,"Error":""}\'; exit 0; fi\n'
        "exit 0\n",
        monkeypatch,
    )
    result = judge_submission(
        "raise SystemExit(125)", "python", [{"input": "", "output": ""}],
        docker_image="fake-image",
    )
    assert result.verdict == VERDICT_RE


def test_docker_cleanup_failure_is_infrastructure_error(tmp_path, monkeypatch):
    _install_fake_docker(
        tmp_path,
        'if [ "$1" = run ]; then exit 1; fi\n'
        'if [ "$1" = inspect ]; then echo \'{"OOMKilled":false,"Error":""}\'; exit 0; fi\n'
        'if [ "$1" = rm ]; then echo "daemon cleanup failed" >&2; exit 1; fi\n'
        "exit 0\n",
        monkeypatch,
    )
    with pytest.raises(JudgeInfrastructureError, match="cleanup failed"):
        judge_submission(
            "raise RuntimeError", "python", [{"input": "", "output": ""}],
            docker_image="fake-image",
        )


def test_whole_submission_has_wall_clock_budget(monkeypatch):
    calls = 0

    def slow_success(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        return "ok\n", "", 0, 20, False

    monkeypatch.setattr(judge_module, "MAX_SUBMISSION_WALL_S", 0.01)
    monkeypatch.setattr(judge_module, "_run_once", slow_success)
    result = judge_submission(
        "print('ok')", "python",
        [{"input": "", "output": "ok"}, {"input": "", "output": "ok"}],
    )
    assert result.verdict == VERDICT_TLE and calls == 1
    assert result.cases[-1].timed_out is True
