"""无构建前端的安全与交互契约（静态检查，无需浏览器/网络）。"""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "prepdojo/web/static/app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "prepdojo/web/static/index.html").read_text(encoding="utf-8")


def test_coach_request_includes_current_user_message():
    assert "coachHistory.slice(0, -1)" not in APP
    assert re.search(r"messages:\s*coachHistory\s*,", APP)


def test_untrusted_values_are_not_rendered_as_html_or_inline_javascript():
    combined = APP + INDEX
    assert ".innerHTML" not in APP
    assert "insertAdjacentHTML" not in APP
    assert not re.search(r"<[^>]+\sonclick\s*=", combined, re.IGNORECASE)
    assert not re.search(r"href\s*=\s*[\"']javascript:", combined, re.IGNORECASE)
    assert "textContent" in APP
    assert "addEventListener" in APP


def test_scores_difficulty_verdict_and_tool_trace_use_safe_helpers():
    assert "scoreText(r.score)" in APP
    assert "scoreText(safeResult.score)" in APP
    assert 'addBadge(difficultyCell, p.difficulty, "difficulty")' in APP
    assert "verdictInfo(r.verdict)" in APP
    assert "appendMessage(live, toolStartText(ev), \"tool\")" in APP
    assert 'coachRender("tool", toolStartText(ev, 40), "tool")' in APP


def test_mode_privacy_and_registration_enter_contracts():
    assert 'class="card hidden" id="card-mykey"' in INDEX
    assert 'classList.toggle("hidden", !multiuser)' in APP
    assert "保存在本服务的部署服务器" in INDEX
    assert 'if (authMode === "register") tryRegister();' in APP


def test_admin_password_ui_matches_server_minimum():
    assert "密码至少 4 位" not in APP
    assert "至少 4 位" not in APP
    assert "≥4 位" not in INDEX
    assert "密码至少 8 位" in APP
    assert "≥8 位" in INDEX


def test_hidden_case_and_malformed_llm_responses_have_fallbacks():
    assert "隐藏用例未通过（详情未公开）" in APP
    assert "const asObject" in APP and "const asArray" in APP
    assert "function parseSseEvent" in APP
    assert "if (!ev) continue;" in APP
