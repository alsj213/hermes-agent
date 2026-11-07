"""``reopen``：done/blocked 卡的受审计恢复口（替代直接改 SQLite）。

E1 的恢复路径是“done 卡缺 tier → 手工改库重开 → pm 补 tier → qa 收口”。
本文件钉住替代动作的边界：只接受 done/blocked、无 active run、必须给 reason，
并写 ``reopened`` 事件（from/to/assignee/reason/actor）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_parser import build_parser


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _create(title="需求：可恢复的卡"):
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=title, body="# 目标\n做点事\n", assignee="pm")


def _done(tid):
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="done", acting_profile="pm") is True
    return tid


def _row(tid):
    with kbc.connect_closing() as conn:
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())


def _events(tid):
    with kbc.connect_closing() as conn:
        return conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
        ).fetchall()


def _parse(argv):
    top = argparse.ArgumentParser(prog="hermes")
    sub = top.add_subparsers(dest="noun")
    build_parser(sub)
    return top.parse_args(["kanban", *argv])


def test_done_card_reopens_to_review_with_audit_event(kanban_home):
    tid = _done(_create())
    with kbc.connect_closing() as conn:
        assert kb.reopen_task(
            conn, tid, to_status="review", assignee="pm",
            reason="缺 pm tier", actor="paimeng",
        ) is True
    row = _row(tid)
    assert row["status"] == "review"
    assert row["assignee"] == "pm"
    assert row["completed_at"] is None
    assert row["result"] is None
    reopened = [e for e in _events(tid) if e["kind"] == "reopened"]
    assert len(reopened) == 1
    payload = json.loads(reopened[0]["payload"])
    assert payload == {
        "from_status": "done", "to_status": "review", "assignee": "pm",
        "reason": "缺 pm tier", "actor": "paimeng",
    }


def test_blocked_card_reopens(kanban_home):
    tid = _create()
    with kbc.connect_closing() as conn:
        assert kb.block_task(conn, tid, reason="等 tier", kind="needs_input") is True
        assert kb.reopen_task(conn, tid, to_status="review", assignee="pm",
                              reason="补 tier", actor="ops") is True
    row = _row(tid)
    assert row["status"] == "review" and row["block_kind"] is None


def test_reopen_requires_reason(kanban_home):
    tid = _done(_create())
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError):
            kb.reopen_task(conn, tid, reason="   ")


def test_reopen_refuses_running_card(kanban_home):
    tid = _create()
    with kbc.connect_closing() as conn:
        assert kb.claim_task(conn, tid, claimer="pm:1") is not None
        assert kb.reopen_task(conn, tid, reason="x") is False
    assert _row(tid)["status"] == "running"


def test_reopen_refuses_archived_card(kanban_home):
    tid = _create()
    with kbc.connect_closing() as conn:
        assert kb.archive_task(conn, tid) is True
        assert kb.reopen_task(conn, tid, reason="x") is False


def test_reopened_card_can_complete_again(kanban_home):
    tid = _done(_create())
    with kbc.connect_closing() as conn:
        assert kb.reopen_task(conn, tid, to_status="review", assignee="pm",
                              reason="重做") is True
        assert kb.complete_task(conn, tid, result="done again", acting_profile="pm") is True
    assert _row(tid)["status"] == "done"


def test_cli_reopen_rc0_and_comment(kanban_home, capsys):
    tid = _done(_create())
    args = _parse(["reopen", tid, "--to", "review", "--assignee", "pm",
                   "--reason", "缺", "tier"])
    assert kc._cmd_reopen(args) == 0
    out, err = capsys.readouterr()
    assert "Reopened" in out
    assert "Traceback" not in err
    with kbc.connect_closing() as conn:
        body = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()["body"]
    assert "REOPENED to review" in body and "缺 tier" in body


def test_cli_reopen_refuses_non_done(kanban_home, capsys):
    tid = _create()
    args = _parse(["reopen", tid, "--reason", "x"])
    assert kc._cmd_reopen(args) == 1
    _, err = capsys.readouterr()
    assert "cannot reopen" in err
    assert _row(tid)["status"] != "review"
