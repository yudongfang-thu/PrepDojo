"""Docker 判题沙箱测试（需本机 Docker 与 prepdojo-judge:latest 镜像，缺则跳过）。

镜像构建：docker build -f deploy/Dockerfile.judge -t prepdojo-judge:latest .
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prepdojo.judge import JudgeInfrastructureError, judge_submission  # noqa: E402

IMAGE = "prepdojo-judge:latest"


def _image_ready() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", IMAGE],
                       capture_output=True, timeout=15)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _image_ready(),
                                reason="docker 或 prepdojo-judge:latest 镜像不可用")


def test_docker_python_ac():
    res = judge_submission("n=int(input());print(n*2)", "python",
                           [{"input": "21\n", "output": "42\n"}],
                           docker_image=IMAGE)
    assert res.verdict == "AC", res.cases[0].stderr


def test_docker_tle():
    res = judge_submission("import time\ntime.sleep(60)", "python",
                           [{"input": "", "output": "x"}],
                           time_limit_ms=1500, docker_image=IMAGE)
    assert res.verdict == "TLE"


def test_docker_cannot_read_host_secrets():
    """存在于宿主、但不在 /work 的文件，在容器内必须确实不可见。"""
    repo = Path(__file__).resolve().parent.parent
    candidates = [str(repo / "AGENTS.md"), str(repo / "data" / "config.yaml")]
    assert Path(candidates[0]).exists()  # 确保测试不是拿不存在的文件自证安全
    code = (
        "from pathlib import Path\n"
        f"candidates = {candidates!r}\n"
        "found = [c for c in candidates if Path(c).exists()]\n"
        "if found:\n"
        "    print('HOST_SECRET_VISIBLE', found)\n"
        "    raise SystemExit(42)\n"
        "print('isolated')\n"
    )
    res = judge_submission(code, "python", [{"input": "", "output": "isolated"}],
                           docker_image=IMAGE)
    assert res.verdict == "AC", f"沙箱隔离失效: {res.cases[0].stdout}"


def test_docker_cpp_bits_header():
    code = ("#include <bits/stdc++.h>\n"
            "int main(){int a,b;std::cin>>a>>b;std::cout<<a+b<<std::endl;}\n")
    res = judge_submission(code, "cpp", [{"input": "1 2\n", "output": "3\n"}],
                           docker_image=IMAGE)
    assert res.verdict == "AC", res.compile_error or res.cases[0].stderr


def test_docker_missing_image_is_infrastructure_error():
    with pytest.raises(JudgeInfrastructureError, match="Docker 无法启动"):
        judge_submission(
            "print(1)", "python", [{"input": "", "output": "1"}],
            docker_image="prepdojo-image-that-must-not-exist:invalid",
        )


def test_docker_output_flood_is_bounded():
    res = judge_submission(
        "import os\nwhile True: os.write(1, b'x' * 65536)",
        "python", [{"input": "", "output": ""}],
        time_limit_ms=5000, docker_image=IMAGE,
    )
    assert res.verdict == "RE"
    assert "输出超过" in res.cases[0].stderr


def test_docker_oom_is_mle():
    code = "chunks=[]\nwhile True: chunks.append(bytearray(8 * 1024 * 1024))"
    res = judge_submission(
        code, "python", [{"input": "", "output": ""}],
        time_limit_ms=5000, mem_limit_mb=64, docker_image=IMAGE,
    )
    assert res.verdict == "MLE", res.cases[0].stderr


def test_docker_workdir_total_is_bounded():
    code = (
        "chunk = b'x' * (8 * 1024 * 1024)\n"
        "for i in range(10):\n"
        "    with open(f'f{i}', 'wb') as f:\n"
        "        f.write(chunk)\n"
        "print('finished')\n"
    )
    res = judge_submission(
        code, "python", [{"input": "", "output": "finished"}],
        time_limit_ms=5000, mem_limit_mb=128, docker_image=IMAGE,
    )
    assert res.verdict == "RE"
    assert "/work 总量超过" in res.cases[0].stderr
