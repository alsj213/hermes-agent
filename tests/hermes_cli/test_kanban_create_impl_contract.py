"""实现卡建卡契约闸门：PATH-CLAIM / TEST-PLAN / CONSUMER + 同标题去重。

闸门与 ``team_board.flow.audit_card`` 同口径，但必须在**落库前**判：
E1b 的实测是卡先落库、card-audit 再 block，pm 又建了一张替代卡。本文件钉住：

- 三条契约行必须顶格；``- PATH-CLAIM`` / ``TEST-PLAN: focused`` 直接拒；
- 同标题的 engineer 卡未结时，30 分钟内不许再建替代卡；
- 只拦 engineer；pm/qa/ops 的卡不受影响；自动收口动作卡豁免；
- decompose 的第二处 ``INSERT INTO tasks`` 也过同一道闸门（整批回滚）。
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

CONTRACT = (
    "PATH-CLAIM: src/team_board/foo.py, tests/unit/test_foo.py\n"
    "TEST-PLAN: quick\n"
    "CONSUMER: python -m pytest tests/unit/test_foo.py\n"
)
GOOD_BODY = CONTRACT + "# 目标\n实现点东西。\n\n## 验收标准\nQA-SPEC: qa\n- 跑 pytest\n"


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


def _create(title="实现：加个功能", assignee="engineer", body=GOOD_BODY):
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=title, body=body, assignee=assignee)


def _task_count():
    with kbc.connect_closing() as conn:
        return len(kb.list_tasks(conn))


def _parse(argv):
    top = argparse.ArgumentParser(prog="hermes")
    sub = top.add_subparsers(dest="noun")
    build_parser(sub)
    return top.parse_args(["kanban", *argv])


# ---------------------------------------------------------------------------
# 内核层：缺行 / 非法写法 → 拒，卡不落库
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body, needle", [
    (GOOD_BODY.replace("PATH-CLAIM: src/team_board/foo.py, tests/unit/test_foo.py\n", ""),
     "PATH-CLAIM"),
    ("- PATH-CLAIM: src/a.py\n" + GOOD_BODY.replace(
        "PATH-CLAIM: src/team_board/foo.py, tests/unit/test_foo.py\n", ""), "PATH-CLAIM"),
    (GOOD_BODY.replace("PATH-CLAIM: src/team_board/foo.py, tests/unit/test_foo.py",
                       "PATH-CLAIM: /etc/passwd"), "绝对路径"),
    (GOOD_BODY.replace("TEST-PLAN: quick", "TEST-PLAN: focused"), "TEST-PLAN"),
    (GOOD_BODY.replace("TEST-PLAN: quick\n", ""), "TEST-PLAN"),
    (GOOD_BODY.replace("CONSUMER: python -m pytest tests/unit/test_foo.py\n", ""),
     "CONSUMER"),
])
def test_engineer_contract_is_rejected_before_insert(kanban_home, body, needle):
    with pytest.raises(kb.CardContractError) as ei:
        _create(body=body)
    assert "建卡闸门拦下" in str(ei.value)
    assert needle in str(ei.value)
    assert _task_count() == 0


def test_good_engineer_card_is_accepted(kanban_home):
    assert _create()
    assert _task_count() == 1


@pytest.mark.parametrize("assignee", ["pm", "qa", "ops", "hatch", "user"])
def test_non_engineer_is_not_gated(kanban_home, assignee):
    _create(title="需求：某件事", assignee=assignee, body="# 目标\n只有正文\n")
    assert _task_count() == 1


def test_auto_action_suffix_is_exempt(kanban_home):
    # 自动收口动作卡只有 PR-CLAIM/动作说明，不是实现卡；QA-SPEC 仍由既有闸门管。
    _create(title="补证据动作卡（自动收口卡）",
            body="## 验收标准\nQA-SPEC: qa\n")
    assert _task_count() == 1


# ---------------------------------------------------------------------------
# 同标题去重：未结卡存在时不许建替代卡
# ---------------------------------------------------------------------------

def test_duplicate_engineer_title_is_rejected(kanban_home):
    first = _create()
    with pytest.raises(kb.DuplicateTaskError) as ei:
        _create()
    assert first in str(ei.value)
    assert "不要新建替代卡" in str(ei.value)
    assert _task_count() == 1


def test_duplicate_is_allowed_after_original_is_archived(kanban_home):
    first = _create()
    with kbc.connect_closing() as conn:
        assert kb.archive_task(conn, first) is True
    assert _create()
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_same_title_non_engineer_is_not_deduped(kanban_home):
    _create(title="需求：同一件事", assignee="pm", body="# 目标\n")
    _create(title="需求：同一件事", assignee="qa", body="# 目标\n")
    assert _task_count() == 2


# ---------------------------------------------------------------------------
# CLI / 工具层：错误可读、无 traceback、卡不落库
# ---------------------------------------------------------------------------

def test_cli_create_rc2_on_contract_violation(kanban_home, capsys):
    args = _parse(["create", "实现：x", "--body",
                   GOOD_BODY.replace("CONSUMER: python -m pytest tests/unit/test_foo.py\n", ""),
                   "--assignee", "engineer"])
    assert kc._cmd_create(args) == 2
    _, err = capsys.readouterr()
    assert "CONSUMER" in err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0
    assert _task_count() == 0


def test_tool_create_returns_error_on_contract_violation(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({
        "title": "实现：x",
        "body": GOOD_BODY.replace("TEST-PLAN: quick", "TEST-PLAN: focused"),
        "assignee": "engineer",
    }))
    assert "error" in out, out
    assert "TEST-PLAN" in out["error"]
    assert _task_count() == 0


# ---------------------------------------------------------------------------
# decompose 的第二处 INSERT：同一道闸门，整批回滚
# ---------------------------------------------------------------------------

def test_decompose_engineer_child_contract_is_enforced_atomically(kanban_home):
    from hermes_cli.kanban_db_graph import decompose_triage_task

    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title="需求：拆一下", body="粗需求", assignee="pm",
                              triage=True)
    children = [
        {"title": "pm 子卡", "body": "先看现状", "assignee": "pm"},
        {"title": "engineer 子卡",
         "body": "## 验收标准\nQA-SPEC: qa\n", "assignee": "engineer"},
    ]
    with kbc.connect_closing() as conn:
        with pytest.raises(kb.CardContractError):
            decompose_triage_task(conn, root, root_assignee="pm", children=children)
    assert _task_count() == 1
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, root).status == "triage"
