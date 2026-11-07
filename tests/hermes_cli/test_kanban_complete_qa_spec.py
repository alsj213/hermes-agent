"""qa 签署行的完成闸门：正文带整行 ``QA-SPEC: qa`` 的卡，只有 qa 的 run 能收口。

闸门只有一处实现（``kanban_db._gate_qa_completion``，由 ``complete_task`` 调用），
CLI 与 agent 工具两条路都必须被同一份判定拦下：

- CLI：``hermes kanban complete`` → rc2、stderr 恰一行、零 traceback；
- 工具：``kanban_complete`` → ``{"error": ...}``，不抛 traceback。

``force=True`` 是唯一逃逸口，且必须落 ``qa_completion_override`` 审计事件。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_parser import build_parser

SIGNED_BODY = (
    "PATH-CLAIM: src/a.py\n"
    "TEST-PLAN: quick\n"
    "CONSUMER: python -m pytest\n"
    "# 目标\n\n## 验收标准\nQA-SPEC: qa\n- 跑通\n"
)
UNSIGNED_BODY = "# 目标\n\n## 验收标准\n先跑通再说\n"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # 本机 worker 环境里带着真板的 DB/TASK/RUN 指针 —— 测试必须完全隔离。
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _create(body=SIGNED_BODY, assignee="engineer"):
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title="实现卡（engineer）：X", body=body, assignee=assignee)


def _claim(tid, claimer="w:1"):
    with kbc.connect_closing() as conn:
        assert kb.claim_task(conn, tid, claimer=claimer) is not None
        return kb._current_run_id(conn, tid)


def _seed_tier_payload(tid, payload, *, profile="pm"):
    """写一条 tier 证据（独立 run）；payload 由调用方给，便于测非法形态。"""
    now = int(time.time())
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            cur = conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, ?, 'done', ?, ?, 'completed')",
                (tid, profile, now, now),
            )
            kb._append_event(conn, tid, "tier", payload, run_id=cur.lastrowid)


def _seed_tier(tid, *, tier="B", head="a" * 40, profile="pm"):
    """写一条合法 tier 证据；qa 收口前置现在要求它存在。"""
    _seed_tier_payload(
        tid, {"tier": tier, "reason": "常规改动", "head_sha": head}, profile=profile)


def _state(tid):
    """``(task 行, 事件 kind 列表)`` —— 用来断言「零变更」。"""
    with kbc.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, result, current_run_id, completed_at FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        events = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)).fetchall()]
        return dict(row), events


def _parse(argv):
    """用真 argparse（kanban_parser）解析，避免手搓 Namespace 漏字段。"""
    top = argparse.ArgumentParser(prog="hermes")
    sub = top.add_subparsers(dest="noun")
    build_parser(sub)
    return top.parse_args(["kanban", *argv])


def _last_event_payload(tid, kind):
    with kbc.connect_closing() as conn:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (tid, kind),
        ).fetchone()
    assert row is not None, f"缺少 {kind} 事件"
    return json.loads(row["payload"]) if row["payload"] else {}


# ---------------------------------------------------------------------------
# ① 有签署 + 非 qa 身份 → 拒，且状态/事件零变更
# ---------------------------------------------------------------------------

def test_engineer_identity_refused_without_state_change(kanban_home):
    tid = _create()
    before, before_events = _state(tid)
    with kbc.connect_closing() as conn:
        with pytest.raises(kb.QaCompletionError) as ei:
            kb.complete_task(conn, tid, result="done", acting_profile="engineer")
    assert "只有 qa 的 run 能收口" in str(ei.value)
    assert "engineer" in str(ei.value)
    after, after_events = _state(tid)
    assert after == before, "被拒时卡行不许有任何变更"
    assert after["status"] == "ready"
    assert after_events == before_events
    assert "completed" not in after_events


def test_pm_identity_refused(kanban_home):
    tid = _create()
    with kbc.connect_closing() as conn:
        with pytest.raises(kb.QaCompletionError):
            kb.complete_task(conn, tid, result="PASS", acting_profile="pm")


def test_active_run_profile_is_read_from_current_run(kanban_home):
    """acting_profile=None 时取 current_run → task_runs.profile；engineer 的 run 也拒。"""
    tid = _create()
    run_id = _claim(tid)
    with kbc.connect_closing() as conn:
        with pytest.raises(kb.QaCompletionError) as ei:
            kb.complete_task(conn, tid, result="done", expected_run_id=run_id)
        run_row = conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert "engineer" in str(ei.value)
    assert run_row["ended_at"] is None, "被拒时本 run 必须仍然开着"
    assert _state(tid)[0]["status"] == "running"


# ---------------------------------------------------------------------------
# ② 有签署 + qa 的 run → 放行
# ---------------------------------------------------------------------------

def test_qa_identity_completes(kanban_home):
    tid = _create()
    _seed_tier(tid)
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="PASS", acting_profile="qa") is True
    st, events = _state(tid)
    assert st["status"] == "done"
    assert "completed" in events
    assert "qa_completion_override" not in events, "qa 自己是正路，不该写 override 审计"


def test_qa_run_from_claim_completes(kanban_home):
    """真实路径：qa run 由 claim 写进 task_runs.profile，收口不需要 acting_profile。"""
    tid = _create(assignee="qa")
    _seed_tier(tid)
    run_id = _claim(tid, claimer="qa:1")
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="PASS", expected_run_id=run_id) is True
    st, events = _state(tid)
    assert st["status"] == "done" and "completed" in events


# ---------------------------------------------------------------------------
# ②b tier 前置：qa 收口前必须有合法的 pm tier 证据（E1 的根因）
# ---------------------------------------------------------------------------

def test_qa_complete_without_tier_is_refused(kanban_home):
    tid = _create()
    before, before_events = _state(tid)
    with kbc.connect_closing() as conn:
        with pytest.raises(kb.QaTierMissingError) as ei:
            kb.complete_task(conn, tid, result="PASS", acting_profile="qa")
    assert "缺 pm 的 tier 证据" in str(ei.value)
    after, after_events = _state(tid)
    assert after == before, "被拒时卡行不许有任何变更"
    assert after_events == before_events
    assert "completed" not in after_events


def test_qa_complete_with_tier_missing_reason_is_refused(kanban_home):
    tid = _create()
    _seed_tier_payload(tid, {"tier": "B", "head_sha": "a" * 40})
    with kbc.connect_closing() as conn:
        with pytest.raises(kb.QaTierMissingError) as ei:
            kb.complete_task(conn, tid, result="PASS", acting_profile="qa")
    assert "缺键 reason" in str(ei.value)
    assert _state(tid)[0]["status"] == "ready"


def test_qa_complete_with_illegal_tier_is_refused(kanban_home):
    tid = _create()
    _seed_tier_payload(tid, {"tier": "D", "reason": "x", "head_sha": "a" * 40})
    with kbc.connect_closing() as conn:
        with pytest.raises(kb.QaTierMissingError) as ei:
            kb.complete_task(conn, tid, result="PASS", acting_profile="qa")
    assert "档位非法" in str(ei.value)


def test_qa_complete_with_tier_from_non_pm_is_refused(kanban_home):
    tid = _create()
    _seed_tier(tid, profile="engineer")
    with kbc.connect_closing() as conn:
        with pytest.raises(kb.QaTierMissingError) as ei:
            kb.complete_task(conn, tid, result="PASS", acting_profile="qa")
    assert "不是 pm 的 run 写的" in str(ei.value)


def test_qa_complete_with_tier_bad_head_is_refused(kanban_home):
    tid = _create()
    _seed_tier(tid, head="not-a-sha")
    with kbc.connect_closing() as conn:
        with pytest.raises(kb.QaTierMissingError) as ei:
            kb.complete_task(conn, tid, result="PASS", acting_profile="qa")
    assert "head_sha 形态非法" in str(ei.value)


def test_e1_flow_refused_then_fixed_by_pm_tier(kanban_home):
    """E1 的原样回归：qa 先被 tier 闸门拒，pm 补 tier 后 qa 收口成功。"""
    tid = _create()
    with kbc.connect_closing() as conn:
        assert kb.claim_task(conn, tid, claimer="eng:1") is not None
        eng_run = kb._current_run_id(conn, tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "impl_done",
                             {"pr": 1, "head_sha": "a" * 40}, run_id=eng_run)
        assert kb.request_review(
            conn, tid, summary="handoff", reviewer="qa", expected_run_id=eng_run) is True
        qa_run = kb.claim_review_task(conn, tid, claimer="qa:1")
        assert qa_run is not None
        qa_run_id = kb._current_run_id(conn, tid)
        with pytest.raises(kb.QaTierMissingError):
            kb.complete_task(conn, tid, result="PASS", expected_run_id=qa_run_id)
    # pm 补 tier（独立 pm run），qa 的 run 仍开着
    _seed_tier(tid)
    with kbc.connect_closing() as conn:
        assert kb.complete_task(
            conn, tid, result="PASS", expected_run_id=qa_run_id) is True
    assert _state(tid)[0]["status"] == "done"


# ---------------------------------------------------------------------------
# ③ force=True → 放行但落 qa_completion_override 审计事件
# ---------------------------------------------------------------------------

def test_force_completes_and_records_audit_event(kanban_home):
    tid = _create()
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="operator override",
                                acting_profile="engineer", force=True) is True
    st, events = _state(tid)
    assert st["status"] == "done"
    assert "qa_completion_override" in events
    payload = _last_event_payload(tid, "qa_completion_override")
    assert payload["profile"] == "engineer"
    assert payload["run_id"] is None
    assert isinstance(payload["at"], int)


def test_force_audit_event_has_no_side_channel_switch(kanban_home):
    """契约：不加环境变量开关、不加 --allow-* —— 逃逸口只有 force。"""
    import inspect

    sig = inspect.signature(kb.complete_task)
    assert "acting_profile" in sig.parameters
    assert sig.parameters["acting_profile"].default is None
    assert "allow_qa" not in sig.parameters
    assert not any(name.startswith("allow_") for name in sig.parameters)


# ---------------------------------------------------------------------------
# ④ 无签署 → 不误伤（pm / engineer 照旧可收口）
# ---------------------------------------------------------------------------

def test_unsigned_card_completed_by_pm(kanban_home):
    tid = _create(body=UNSIGNED_BODY, assignee="pm")
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="done", acting_profile="pm") is True
    st, events = _state(tid)
    assert st["status"] == "done"
    assert "qa_completion_override" not in events


def test_unsigned_card_completed_without_any_identity(kanban_home):
    tid = _create(body=UNSIGNED_BODY, assignee="pm")
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="done") is True


# ---------------------------------------------------------------------------
# ⑤ 有签署但没有 active run（人工 CLI 收口）→ 同样拒
# ---------------------------------------------------------------------------

def test_signed_card_without_active_run_refused(kanban_home):
    tid = _create()
    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["current_run_id"] is None
        with pytest.raises(kb.QaCompletionError) as ei:
            kb.complete_task(conn, tid, result="done")
    assert "无 active run" in str(ei.value)
    assert _state(tid)[0]["status"] == "ready"


# ---------------------------------------------------------------------------
# ⑥ 提到但不构成整行 → 不触发
# ---------------------------------------------------------------------------

def test_mention_without_full_line_does_not_trigger(kanban_home):
    body = "说明：QA-SPEC: qa 是 qa 的签署行（这里不是整行）\n"
    tid = _create(body=body, assignee="pm")
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="done", acting_profile="pm") is True
    assert _state(tid)[0]["status"] == "done"


def test_trailing_space_is_not_a_signature_line(kanban_home):
    body = "## 验收标准\nQA-SPEC: qa \n- 尾空格\n"
    tid = _create(body=body, assignee="pm")
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="done", acting_profile="pm") is True


def test_exception_is_value_error(kanban_home):
    """工具层按 ValueError 走 tool_error（无 traceback），这条继承关系是契约。"""
    assert issubclass(kb.QaCompletionError, ValueError)


# ---------------------------------------------------------------------------
# 契约 F：闸门只加在「完成」这一步 —— pm 的验收 run 仍可打回与留言
# ---------------------------------------------------------------------------

def test_pm_review_run_can_request_changes_and_comment(kanban_home):
    tid = _create()
    eng_run = _claim(tid, claimer="eng:1")
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "impl_done",
                             {"pr": 1, "head_sha": "a" * 40}, run_id=eng_run)
        assert kb.request_review(conn, tid, summary="handoff", expected_run_id=eng_run) is True
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "review"
    with kbc.connect_closing() as conn:
        assert kb.claim_review_task(conn, tid, claimer="pm:1") is not None
        pm_run = kb._current_run_id(conn, tid)
        assert kb.add_comment(conn, tid, "pm", "验收中：先看 C-1") > 0
        ok, impl = kb.request_changes(
            conn, tid, reason="C-1 要改", expected_run_id=pm_run,
            cause="impl", cause_quote="C-1 要改")
    assert ok is True, impl
    st, events = _state(tid)
    assert st["status"] != "done"
    assert "changes_requested" in events


# ---------------------------------------------------------------------------
# CLI 层：rc2 + stderr 恰一行 + 零 traceback
# ---------------------------------------------------------------------------

def test_cli_complete_rc2_on_signed_card(kanban_home, capsys):
    tid = _create()
    rc = kc._cmd_complete(_parse(["complete", tid, "--result", "done"]))
    _out, err = capsys.readouterr()
    assert rc == 2
    assert "只有 qa 的 run 能收口" in err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0, f"stderr 必须恰一行：{err!r}"
    st, events = _state(tid)
    assert st["status"] == "ready"
    assert "completed" not in events


def test_cli_complete_force_rc0(kanban_home, capsys):
    tid = _create()
    rc = kc._cmd_complete(_parse(["complete", tid, "--result", "operator", "--force"]))
    _out, err = capsys.readouterr()
    assert rc == 0, err
    st, events = _state(tid)
    assert st["status"] == "done"
    assert "qa_completion_override" in events


# ---------------------------------------------------------------------------
# 工具层：{"error": ...} 含三条出路，无 traceback，卡未变更
# ---------------------------------------------------------------------------

def test_tool_complete_refused_names_three_escapes(kanban_home):
    from tools import kanban_tools as kt

    tid = _create()
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "done"}))
    assert "error" in out, out
    text = out["error"]
    assert "只有 qa 的 run 能收口" in text
    for escape in ("kanban_request_changes", "kanban_request_review", "kanban_comment"):
        assert escape in text, f"缺少出路 {escape}：{text}"
    assert "卡未变更" in text
    assert "Traceback" not in text
    st, events = _state(tid)
    assert st["status"] == "ready"
    assert "completed" not in events
