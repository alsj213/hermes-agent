"""``hermes kanban claim <卡> --pr <n>``：worker 自救，只补正文首行的 ``PR-CLAIM``。

契约（卡 t_25cdce26 / 评论 1）：

- ``--pr`` 取值域 = 1–999999999 的 ASCII 十进制（``parse_pr_arg``），非法 rc2；
- ``set_pr_claim`` 只改 ``body``：无认领行则**前插**并逐字保留原正文，同号幂等，
  异号冲突；``done`` / ``archived`` rc2；
- CLI 越界（``HERMES_KANBAN_TASK`` 指向别卡）rc1，沿用 ``_worker_run_id_for`` 文案；
- 补写成功留一行 comment：``PR-CLAIM 由 <profile> 补写（hermes kanban claim）``。
"""

from __future__ import annotations

import argparse
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
    # 本机 worker 环境带着真板的 DB/TASK 指针和 delegate 围栏 —— 测试必须完全隔离。
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _create(title="工作卡（engineer）：验证 claim", body="## 目标\nx"):
    # assignee=engineer 现在还要过「验收标准 + QA-SPEC: qa」建卡闸门（G2-b），
    # 而本文件只验 set_pr_claim / CLI claim 的正文与取值域，与本闸门无关 ——
    # 用 pm 造卡把两个闸门解耦，闸门本身另见 test_kanban_create_qa_spec.py。
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=title, body=body, assignee="pm")


def _task(task_id):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, task_id)


def _comments(task_id):
    with kbc.connect_closing() as conn:
        return kb.list_comments(conn, task_id)


def _argv(argv):
    """用真 argparse（kanban_parser）解析，避免手搓 Namespace 漏字段。"""
    top = argparse.ArgumentParser(prog="hermes")
    sub = top.add_subparsers(dest="noun")
    build_parser(sub)
    return top.parse_args(["kanban", *argv])


# ---------------------------------------------------------------------------
# 取值域：parse_pr_arg
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1", 1), ("99", 99), ("999999999", 999_999_999),
])
def test_parse_pr_arg_accepts(raw, expected):
    assert kb.parse_pr_arg(raw) == expected


@pytest.mark.parametrize("raw", [
    "0", "00", "099", "1_0", "+1", "-1", "", " ", "99 ", " 99", "abc", "1.0",
    "1793160000000", "١٢", "9" * 10, "1e3",
])
def test_parse_pr_arg_rejects(raw):
    with pytest.raises(ValueError):
        kb.parse_pr_arg(raw)


def test_parse_pr_arg_rejects_none():
    with pytest.raises(ValueError):
        kb.parse_pr_arg(None)


# ---------------------------------------------------------------------------
# set_pr_claim：只动 body
# ---------------------------------------------------------------------------

def test_insert_prepends_and_keeps_body_verbatim(kanban_home):
    tid = _create(body="## 目标\n第一行\n第二行")
    with kbc.connect_closing() as conn:
        assert kb.set_pr_claim(conn, tid, 99, by="engineer") == "inserted"
    task = _task(tid)
    assert task.body == "PR-CLAIM: #99\n## 目标\n第一行\n第二行"
    assert task.body.splitlines()[0] == "PR-CLAIM: #99"


def test_insert_does_not_touch_other_fields(kanban_home):
    tid = _create()
    before = _task(tid)
    with kbc.connect_closing() as conn:
        kb.set_pr_claim(conn, tid, 99, by="engineer")
    after = _task(tid)
    assert (after.title, after.status, after.assignee, after.priority) == (
        before.title, before.status, before.assignee, before.priority)


def test_insert_on_empty_body(kanban_home):
    tid = _create(body="")
    with kbc.connect_closing() as conn:
        assert kb.set_pr_claim(conn, tid, 7, by="engineer") == "inserted"
    assert _task(tid).body == "PR-CLAIM: #7"


def test_same_number_is_idempotent(kanban_home):
    tid = _create(body="正文")
    with kbc.connect_closing() as conn:
        assert kb.set_pr_claim(conn, tid, 99, by="engineer") == "inserted"
        assert kb.set_pr_claim(conn, tid, 99, by="engineer") == "unchanged"
    assert _task(tid).body == "PR-CLAIM: #99\n正文"


def test_different_number_conflicts(kanban_home):
    tid = _create(body="正文")
    with kbc.connect_closing() as conn:
        kb.set_pr_claim(conn, tid, 98, by="engineer")
        with pytest.raises(ValueError) as ei:
            kb.set_pr_claim(conn, tid, 99, by="engineer")
    assert "#98" in str(ei.value) and "#99" in str(ei.value)
    assert _task(tid).body == "PR-CLAIM: #98\n正文", "冲突时必须原样回滚"


@pytest.mark.parametrize("status", ["done", "archived"])
def test_terminal_status_is_rejected(kanban_home, status):
    tid = _create(body="正文")
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        with pytest.raises(ValueError) as ei:
            kb.set_pr_claim(conn, tid, 99, by="engineer")
    assert status in str(ei.value)
    assert _task(tid).body == "正文"


def test_unknown_task_is_rejected(kanban_home):
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError) as ei:
            kb.set_pr_claim(conn, "t_00000000", 99, by="engineer")
    assert "no such task" in str(ei.value)


def test_insert_over_body_limit_is_rejected_not_truncated(kanban_home):
    """前插后超 2048B → BodyTooLargeError（rc2），正文原样保留。"""
    body = "长" * 680  # 2040B
    tid = _create(body=body)
    with kbc.connect_closing() as conn:
        with pytest.raises(kb.BodyTooLargeError):
            kb.set_pr_claim(conn, tid, 99, by="engineer")
    assert _task(tid).body == body


def test_insert_records_event(kanban_home):
    tid = _create(body="正文")
    with kbc.connect_closing() as conn:
        kb.set_pr_claim(conn, tid, 99, by="engineer")
        events = kb.list_events(conn, tid)
    edited = [e for e in events if e.kind == "edited"]
    assert edited, "写入必须留事件"
    assert ("pr_claim" in (edited[-1].payload or "")) or ("99" in str(edited[-1].payload))


# ---------------------------------------------------------------------------
# CLI 层：rc / 文案 / comment
# ---------------------------------------------------------------------------

def test_cli_claim_pr_rc0_inserts(kanban_home, capsys):
    tid = _create()
    assert kc._cmd_claim(_argv(["claim", tid, "--pr", "99"])) == 0
    out, err = capsys.readouterr()
    assert f"已为 {tid} 写入 PR-CLAIM: #99" in out
    assert err == ""
    assert _task(tid).body.splitlines()[0] == "PR-CLAIM: #99"


def test_cli_claim_pr_leaves_comment(kanban_home, capsys):
    tid = _create()
    kc._cmd_claim(_argv(["claim", tid, "--pr", "99"]))
    bodies = [c.body for c in _comments(tid)]
    assert any("PR-CLAIM 由 " in b and "补写（hermes kanban claim）" in b for b in bodies)


def test_cli_claim_pr_twice_is_idempotent(kanban_home, capsys):
    tid = _create()
    kc._cmd_claim(_argv(["claim", tid, "--pr", "99"]))
    capsys.readouterr()
    assert kc._cmd_claim(_argv(["claim", tid, "--pr", "99"])) == 0
    out, _ = capsys.readouterr()
    assert "PR-CLAIM 已是 #99（未改动）" in out
    assert _task(tid).body.count("PR-CLAIM:") == 1
    assert len(_comments(tid)) == 1, "幂等分支不许重复留 comment"


def test_cli_claim_pr_conflict_rc2(kanban_home, capsys):
    tid = _create()
    kc._cmd_claim(_argv(["claim", tid, "--pr", "98"]))
    capsys.readouterr()
    assert kc._cmd_claim(_argv(["claim", tid, "--pr", "99"])) == 2
    _, err = capsys.readouterr()
    assert err.startswith("kanban: ")
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0, f"stderr 必须恰一行：{err!r}"
    assert _task(tid).body.splitlines()[0] == "PR-CLAIM: #98"


@pytest.mark.parametrize("bad", ["0", "1793160000000", "", "1_0", "+1"])
def test_cli_claim_pr_bad_value_rc2(kanban_home, capsys, bad):
    tid = _create()
    assert kc._cmd_claim(_argv(["claim", tid, "--pr", bad])) == 2
    _, err = capsys.readouterr()
    assert err.startswith("kanban: ")
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0, f"stderr 必须恰一行：{err!r}"
    assert _task(tid).body == "## 目标\nx", "非法 --pr 不许落库"


def test_cli_claim_pr_done_card_rc2(kanban_home, capsys):
    tid = _create()
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    assert kc._cmd_claim(_argv(["claim", tid, "--pr", "99"])) == 2
    _, err = capsys.readouterr()
    assert "status=done" in err
    assert "PR-CLAIM" not in (_task(tid).body or "")


def test_cli_claim_pr_scoped_to_own_card_rc0(kanban_home, capsys, monkeypatch):
    """worker 改自己那张：HERMES_KANBAN_TASK == 目标 → rc0。"""
    tid = _create()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    assert kc._cmd_claim(_argv(["claim", tid, "--pr", "99"])) == 0
    assert _task(tid).body.splitlines()[0] == "PR-CLAIM: #99"


def test_cli_claim_pr_other_card_rc1(kanban_home, capsys, monkeypatch):
    """worker 改别卡：rc1，沿用 _worker_run_id_for 的既有文案，不落库。"""
    tid = _create()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_c6ab9672")
    assert kc._cmd_claim(_argv(["claim", tid, "--pr", "99"])) == 1
    _, err = capsys.readouterr()
    assert "worker is scoped to task t_c6ab9672" in err
    assert f"refusing to mutate {tid}" in err
    assert _task(tid).body == "## 目标\nx"


def test_cli_claim_pr_scope_checked_before_pr_value(kanban_home, capsys, monkeypatch):
    """顺序契约（评论 1 第 3 条「先调 _worker_run_id_for」）：越界优先于 --pr 校验。"""
    tid = _create()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_c6ab9672")
    assert kc._cmd_claim(_argv(["claim", tid, "--pr", "0"])) == 1
    _, err = capsys.readouterr()
    assert "worker is scoped to task" in err


# ---------------------------------------------------------------------------
# 解析层：不回归
# ---------------------------------------------------------------------------

def test_parser_claim_without_pr_keeps_ttl():
    args = _argv(["claim", "t_12345678"])
    assert args.pr is None
    assert args.ttl == kb.DEFAULT_CLAIM_TTL_SECONDS


def test_parser_claim_with_pr():
    args = _argv(["claim", "t_12345678", "--pr", "99"])
    assert args.pr == "99", "--pr 必须按字符串接收（否则 +1/1_0 走 argparse SystemExit）"


def test_cmd_claim_without_pr_still_claims(kanban_home, capsys):
    """老形态 `claim <id>` 不许改语义：没有 --pr 时仍走原子认领。"""
    tid = _create()
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    rc = kc._cmd_claim(_argv(["claim", tid]))
    out, _ = capsys.readouterr()
    assert rc == 0
    assert out.startswith(f"Claimed {tid}")
    assert "PR-CLAIM" not in (_task(tid).body or "")
