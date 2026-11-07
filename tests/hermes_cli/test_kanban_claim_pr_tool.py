"""``kanban_claim_pr`` agent 工具：worker 自救补写自己那张卡的 ``PR-CLAIM`` 行。

契约（卡 t_bf04b673 / 评论 1）：

- 只改正文的 ``PR-CLAIM: #<n>`` 一行（前插 / 同号幂等 / 异号冲突 / done 拒绝），
  ``title`` / ``status`` / ``assignee`` / ``priority`` 只读；
- ``pr`` 原样交给 ``kb.parse_pr_arg``：``0`` / ``1_0`` / ``""`` / 13 位毫秒一律报错；
- own-task only：``HERMES_KANBAN_TASK`` 指向 A 时对 B 调用必须被
  ``_enforce_worker_task_ownership`` 拒（文案含 ``worker is scoped to task``）；
- 只有 ``inserted`` 才留一行 comment，第二次调用不追加。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """临时 HERMES_HOME/HERMES_KANBAN_HOME —— 本机 worker 环境带真板指针与 delegate 围栏。"""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _dispatch(args):
    from tools import kanban_tools
    from tools.registry import registry

    return json.loads(registry.dispatch("kanban_claim_pr", args))


def _create(body="## 目标\n第一行", title="工作卡（engineer）：PR-CLAIM 工具"):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    # assignee=engineer 现在还要过「验收标准 + QA-SPEC: qa」建卡闸门（G2-b），
    # 本文件只验 claim_pr 工具的写入/冲突/取值域 —— 用 pm 造卡解耦，闸门另见
    # test_kanban_create_qa_spec.py。
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=title, body=body, assignee="pm")


def _task(task_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        return kb.get_task(conn, task_id)


def _comments(task_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        return kb.list_comments(conn, task_id)


# ---------------------------------------------------------------------------
# own-task：写入 + comment + 幂等 + 冲突
# ---------------------------------------------------------------------------

def test_own_task_prepends_claim_and_comments_once(kanban_home, monkeypatch):
    tid = _create()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    result = _dispatch({"pr": "99"})

    assert result.get("ok"), result
    assert (result["task_id"], result["outcome"], result["pr"]) == (tid, "inserted", 99)
    assert _task(tid).body.splitlines()[0] == "PR-CLAIM: #99"
    assert _task(tid).body == "PR-CLAIM: #99\n## 目标\n第一行", "正文其余字节必须逐字保留"
    comments = _comments(tid)
    assert len(comments) == 1
    assert "kanban_claim_pr" in comments[0].body


def test_second_call_is_idempotent_and_does_not_comment_again(kanban_home, monkeypatch):
    tid = _create()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    first = _dispatch({"pr": "99"})
    second = _dispatch({"pr": "99"})

    assert first["outcome"] == "inserted"
    assert second.get("ok") and second["outcome"] == "unchanged", second
    assert len(_comments(tid)) == 1, "幂等分支不许再追加 comment"
    assert _task(tid).body.count("PR-CLAIM:") == 1


def test_different_number_conflicts_and_body_untouched(kanban_home, monkeypatch):
    tid = _create(body="PR-CLAIM: #98\n正文")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    result = _dispatch({"pr": "99"})

    assert "error" in result, result
    assert "PR-CLAIM 冲突" in result["error"]
    assert _task(tid).body == "PR-CLAIM: #98\n正文"
    assert not _comments(tid)


# ---------------------------------------------------------------------------
# 取值域 / done / 跨卡 / 2048B
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["0", "1793160000000", "", "1_0", "00099", "+1", "99 "])
def test_out_of_range_pr_is_rejected(kanban_home, monkeypatch, raw):
    tid = _create(body="正文")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    result = _dispatch({"pr": raw})

    assert "error" in result, result
    assert "--pr" in result["error"], "报错必须指回 --pr 的取值域"
    assert _task(tid).body == "正文"
    assert not _comments(tid)


def test_done_card_is_rejected(kanban_home, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _create(body="正文")
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="完成", force=True) is True
    assert _task(tid).status == "done"
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    result = _dispatch({"pr": "99"})

    assert "error" in result, result
    assert "status=done" in result["error"]
    assert _task(tid).body == "正文"


def test_cross_card_is_refused(kanban_home, monkeypatch):
    mine = _create(body="我的卡")
    other = _create(body="别人的卡", title="工作卡（engineer）：别人")
    monkeypatch.setenv("HERMES_KANBAN_TASK", mine)

    result = _dispatch({"task_id": other, "pr": "99"})

    assert "error" in result, result
    assert "worker is scoped to task" in result["error"]
    assert _task(other).body == "别人的卡"
    assert _task(mine).body == "我的卡"


def test_body_overflow_is_rejected_not_truncated(kanban_home, monkeypatch):
    body = "长" * 680  # 2040B，前插 15B 后超 2048B
    tid = _create(body=body)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    result = _dispatch({"pr": "99"})

    assert "error" in result, result
    assert "2048" in result["error"]
    assert _task(tid).body == body, "溢出时正文必须原样回滚，不许截断"
