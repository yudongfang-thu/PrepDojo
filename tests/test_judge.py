"""判题沙箱测试：五态判定逐项验证。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prepdojo.judge import (  # noqa: E402
    VERDICT_AC, VERDICT_CE, VERDICT_RE, VERDICT_TLE, VERDICT_WA,
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
