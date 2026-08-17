"""Docker 判题沙箱测试（需本机 Docker 与 prepdojo-judge:latest 镜像，缺则跳过）。

镜像构建：docker build -f deploy/Dockerfile.judge -t prepdojo-judge:latest .
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prepdojo.judge import judge_submission  # noqa: E402

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
    """容器内没有宿主 data/ 目录：读配置文件必须失败（防 key 外泄）。"""
    code = (
        "from pathlib import Path\n"
        "candidates = ['data/config.yaml', '/mnt/dataY/ydf/projects/PrepDojo/data/config.yaml']\n"
        "for c in candidates:\n"
        "    print(c, Path(c).exists())\n"
        "raise SystemExit(1)\n"
    )
    res = judge_submission(code, "python", [{"input": "", "output": "never"}],
                           docker_image=IMAGE)
    # SystemExit(1) → RE；若文件真的存在打印出来也说明隔离失效，用例会因 verdict != RE 暴露
    assert res.verdict == "RE", f"沙箱隔离疑似失效: {res.cases[0].stdout}"


def test_docker_cpp_bits_header():
    code = ("#include <bits/stdc++.h>\n"
            "int main(){int a,b;std::cin>>a>>b;std::cout<<a+b<<std::endl;}\n")
    res = judge_submission(code, "cpp", [{"input": "1 2\n", "output": "3\n"}],
                           docker_image=IMAGE)
    assert res.verdict == "AC", res.compile_error or res.cases[0].stderr
