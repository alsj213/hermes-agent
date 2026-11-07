"""建卡验收标准闸门：``assignee=engineer`` 的实现卡必须有「## 验收标准」段 + qa 签署行。

闸门只有一处实现（``kanban_db._validate_qa_spec``），但有两处调用点 —— 也就是全仓
两处 ``INSERT INTO tasks``：主路径 ``create_task``（CLI 与 agent 工具都走它）和
``kanban_db_graph._insert_decomposed_child``（decompose 子卡，同记 ``created`` 事件）。
两条路都必须在这里被同一份校验拦下：

- CLI：``hermes kanban create`` → rc2、stderr 恰一行、零 traceback；
- 工具：``kanban_create`` → ``{"error": ...}``，不抛 traceback。

必填范围唯一定义处是 ``_QA_SPEC_REQUIRED_ASSIGNEES``（只有 engineer）——
其余 assignee（pm / ops / qa / hatch …）不受本闸门约束，老板手写的需求卡不许被拦。
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
    # 本机 worker 环境里带着真板的 DB/TASK 指针 —— 测试必须完全隔离，
    # 一律清掉，只走 HERMES_HOME 下的临时板。
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


SIGN = "QA-SPEC: qa"
CONTRACT = "PATH-CLAIM: src/a.py\nTEST-PLAN: quick\nCONSUMER: python -m pytest\n"
GOOD_BODY = CONTRACT + "# 目标\n实现点东西。\n\n## 验收标准\n" + SIGN + "\n- 跑 pytest\n"
NO_SECTION_BODY = "# 目标\n实现点东西。\n\n## 边界\n别扩面。\n"
SECTION_NO_SIGN_BODY = "# 目标\n实现点东西。\n\n## 验收标准\n- 跑 pytest\n"
SIGN_TRAILING_SPACE_BODY = "# 目标\n\n## 验收标准\n" + SIGN + " \n- 跑 pytest\n"
HEADING_WITH_SUFFIX_BODY = "# 目标\n\n## 验收标准（qa）\n" + SIGN + "\n- 跑 pytest\n"
BLANK_THEN_SIGN_BODY = CONTRACT + "# 目标\n\n## 验收标准\n\n   \n" + SIGN + "\n- 跑 pytest\n"
SIGN_LAST_BODY = CONTRACT + "# 目标\n\n## 验收标准\n" + SIGN


def _create(title, assignee, body):
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=title, body=body, assignee=assignee)


def _task_count():
    with kbc.connect_closing() as conn:
        return len(kb.list_tasks(conn))


def _parse_create_argv(argv):
    """用真 argparse（kanban_parser）解析，避免手搓 Namespace 漏字段。"""
    top = argparse.ArgumentParser(prog="hermes")
    sub = top.add_subparsers(dest="noun")
    build_parser(sub)
    return top.parse_args(["kanban", *argv])


# ---------------------------------------------------------------------------
# 内核层：拒绝（缺段 / 缺签署 / 近似写法），卡不落库
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body, reason", [
    (NO_SECTION_BODY, "缺该段"),
    (SECTION_NO_SIGN_BODY, "第一行必须是「QA-SPEC: qa」"),
    (SIGN_TRAILING_SPACE_BODY, "第一行必须是「QA-SPEC: qa」"),
    (HEADING_WITH_SUFFIX_BODY, "缺该段"),
    ("# 目标\n\n## 验收标准\n", "第一行必须是「QA-SPEC: qa」"),
    ("", "缺该段"),
])
def test_engineer_card_without_signed_spec_is_rejected(kanban_home, body, reason):
    with pytest.raises(kb.QaSpecMissingError) as ei:
        _create("实现：加个功能", "engineer", body)
    msg = str(ei.value)
    assert "建卡闸门拦下" in msg, msg
    assert reason in msg, msg
    assert _task_count() == 0, "被拒的卡不许落库"


def test_engineer_card_with_signed_spec_is_accepted(kanban_home):
    tid = _create("实现：加个功能", "engineer", GOOD_BODY)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert task.assignee == "engineer"
    assert _task_count() == 1


@pytest.mark.parametrize("body", [BLANK_THEN_SIGN_BODY, SIGN_LAST_BODY])
def test_signed_spec_after_blank_lines_or_at_eof_is_accepted(kanban_home, body):
    """段内先空行再签署 / 签署行是正文最后一行 —— 都算合格。"""
    assert _create("实现：加个功能", "engineer", body)
    assert _task_count() == 1


@pytest.mark.parametrize("assignee", ["pm", "ops", "qa", "hatch", "user"])
def test_other_assignees_are_not_gated(kanban_home, assignee):
    """不误伤：老板手写的需求卡、运帷卡一律照建。"""
    assert _create("需求：某件事", assignee, NO_SECTION_BODY)
    assert _task_count() == 1


def test_scope_tuple_is_the_single_source(kanban_home):
    """必填范围唯一定义处：扩面只改这个元组。"""
    assert kb._QA_SPEC_REQUIRED_ASSIGNEES == ("engineer",)
    assert kb._QA_SPEC_HEADING == "## 验收标准"


def test_assignee_case_and_whitespace_are_canonicalized(kanban_home):
    """判据用已规范化的 assignee（create_task 里 `_canonical_assignee` 之后）。"""
    with pytest.raises(kb.QaSpecMissingError):
        _create("实现：加个功能", " Engineer ", NO_SECTION_BODY)
    assert _create("实现：加个功能", " Engineer ", GOOD_BODY)
    assert _task_count() == 1


def test_exception_is_value_error(kanban_home):
    """工具层按 ValueError 走 tool_error（无 traceback），这条继承关系是契约。"""
    assert issubclass(kb.QaSpecMissingError, ValueError)


def test_body_limit_still_runs_before(kanban_home):
    """R16 上限仍是第一道：超限报 BodyTooLargeError，不是 QaSpecMissingError。"""
    with pytest.raises(kb.BodyTooLargeError):
        _create("实现：加个功能", "engineer", "长" * 800)
    assert _task_count() == 0


def test_merge_assignee_gate_still_runs_first(kanban_home):
    """顺序契约：_validate_qa_spec 在 _validate_merge_assignee 之后调用。"""
    with pytest.raises(kb.MergeAssigneeError):
        _create("合并卡（pm）：PR #99", "engineer", "PR-CLAIM: #99\n正文\n")
    assert _task_count() == 0


def test_edit_task_is_not_gated(kanban_home):
    """只拦建卡：存量不追溯，edit_task 不动。"""
    tid = _create("实现：加个功能", "engineer", GOOD_BODY)
    with kbc.connect_closing() as conn:
        kb.edit_task(conn, tid, body=NO_SECTION_BODY)
        task = kb.get_task(conn, tid)
    assert task.body == NO_SECTION_BODY


# ---------------------------------------------------------------------------
# CLI 层：rc2 + stderr 恰一行 + 零 traceback
# ---------------------------------------------------------------------------

def test_cli_create_rc2_when_section_missing(kanban_home, capsys):
    args = _parse_create_argv(["create", "实现：x", "--body", NO_SECTION_BODY,
                               "--assignee", "engineer"])
    assert kc._cmd_create(args) == 2
    _out, err = capsys.readouterr()
    assert err.startswith("kanban: "), err
    assert "缺该段" in err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0, f"stderr 必须恰一行：{err!r}"
    assert _task_count() == 0


def test_cli_create_rc2_when_sign_missing(kanban_home, capsys):
    args = _parse_create_argv(["create", "实现：x", "--body", SECTION_NO_SIGN_BODY,
                               "--assignee", "engineer"])
    assert kc._cmd_create(args) == 2
    _out, err = capsys.readouterr()
    assert "QA-SPEC: qa" in err
    assert err.strip().count("\n") == 0
    assert _task_count() == 0


def test_cli_create_rc0_with_signed_spec(kanban_home, capsys):
    args = _parse_create_argv(["create", "实现：x", "--body", GOOD_BODY,
                               "--assignee", "engineer"])
    assert kc._cmd_create(args) == 0
    _out, err = capsys.readouterr()
    assert "Traceback" not in err
    assert _task_count() == 1


@pytest.mark.parametrize("assignee", ["pm", "ops"])
def test_cli_create_rc0_for_other_assignees(kanban_home, capsys, assignee):
    """不误伤：同 body（缺段）+ 非 engineer → rc0。"""
    args = _parse_create_argv(["create", "需求：x", "--body", NO_SECTION_BODY,
                               "--assignee", assignee])
    assert kc._cmd_create(args) == 0
    _out, err = capsys.readouterr()
    assert "Traceback" not in err
    assert _task_count() == 1


# ---------------------------------------------------------------------------
# 工具层：kanban_create 返回 {"error": ...}，不抛
# ---------------------------------------------------------------------------

def test_tool_create_rejected_with_tool_error(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "实现：x", "body": NO_SECTION_BODY,
                                        "assignee": "engineer"}))
    assert "error" in out, out
    assert "缺该段" in out["error"]
    assert _task_count() == 0


def test_tool_create_accepts_signed_spec(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "实现：x", "body": GOOD_BODY,
                                        "assignee": "engineer"}))
    assert "error" not in out, out
    assert _task_count() == 1


def test_tool_create_neutral_card_unaffected(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "需求：x", "body": NO_SECTION_BODY,
                                        "assignee": "pm"}))
    assert "error" not in out, out
    assert _task_count() == 1


# ---------------------------------------------------------------------------
# 第二处 ``INSERT INTO tasks``：decompose 子卡（同属「建卡」）也必须过同一道闸门
#
# 判据不是「谁写的代码」，而是「这张卡是新造的」。``_insert_decomposed_child``
# 写库时同样记一条 ``created`` 事件（payload 带 ``from_decompose_of``），所以
# engineer 卡不能从 decompose 这条后门落库。抛点在 ``decompose_triage_task`` 的
# write_txn 内 → 整批 fan-out 回滚（root 不翻状态、无子卡、无事件）。
# ---------------------------------------------------------------------------

def _triage_root(assignee="pm", title="需求：拆一下"):
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=title, body="粗需求，等拆。",
                              assignee=assignee, triage=True)


def _decompose(root_id, children, root_assignee="pm"):
    from hermes_cli.kanban_db_graph import decompose_triage_task

    with kbc.connect_closing() as conn:
        return decompose_triage_task(
            conn, root_id, root_assignee=root_assignee, children=children)


def _events(kind):
    with kbc.connect_closing() as conn:
        return conn.execute(
            "SELECT task_id FROM task_events WHERE kind = ?", (kind,)).fetchall()


def _decomposed_created_events():
    with kbc.connect_closing() as conn:
        return conn.execute(
            "SELECT task_id FROM task_events WHERE kind = 'created' "
            "AND json_extract(payload, '$.from_decompose_of') IS NOT NULL").fetchall()


def test_decompose_engineer_child_without_spec_is_rejected_atomically(kanban_home):
    root = _triage_root()
    children = [
        {"title": "pm 子卡", "body": NO_SECTION_BODY, "assignee": "pm"},
        {"title": "engineer 子卡", "body": "y", "assignee": "engineer"},
    ]
    with pytest.raises(kb.QaSpecMissingError) as ei:
        _decompose(root, children)
    assert "缺该段" in str(ei.value), ei.value
    assert _task_count() == 1, "整批 fan-out 回滚：只留 root，不能有半个图"
    with kbc.connect_closing() as conn:
        root_task = kb.get_task(conn, root)
        assert root_task is not None and root_task.status == "triage"
    assert _events("decomposed") == []
    assert _decomposed_created_events() == []


def test_decompose_engineer_child_without_sign_is_rejected(kanban_home):
    root = _triage_root()
    with pytest.raises(kb.QaSpecMissingError) as ei:
        _decompose(root, [{"title": "engineer 子卡", "body": SECTION_NO_SIGN_BODY,
                           "assignee": "engineer"}])
    assert "QA-SPEC: qa" in str(ei.value), ei.value
    assert _task_count() == 1


def test_decompose_engineer_child_with_signed_spec_is_accepted(kanban_home):
    root = _triage_root()
    ids = _decompose(root, [
        {"title": "pm 子卡", "body": NO_SECTION_BODY, "assignee": "pm"},
        {"title": "engineer 子卡", "body": GOOD_BODY, "assignee": "engineer"},
    ])
    assert ids and len(ids) == 2
    assert _task_count() == 3
    with kbc.connect_closing() as conn:
        root_task = kb.get_task(conn, root)
        eng_task = kb.get_task(conn, ids[1])
        assert root_task is not None and root_task.status == "todo"
        assert eng_task is not None and eng_task.assignee == "engineer"
    assert len(_events("decomposed")) == 1


def test_decompose_other_assignees_are_not_gated(kanban_home):
    """不误伤：pm / qa / ops 子卡缺段照拆。"""
    root = _triage_root()
    ids = _decompose(root, [
        {"title": "pm 子卡", "body": NO_SECTION_BODY, "assignee": "pm"},
        {"title": "qa 子卡", "body": NO_SECTION_BODY, "assignee": "qa"},
        {"title": "ops 子卡", "body": None, "assignee": "ops"},
    ])
    assert ids and len(ids) == 3
    assert _task_count() == 4


def test_decompose_child_assignee_is_canonicalized(kanban_home):
    root = _triage_root()
    with pytest.raises(kb.QaSpecMissingError):
        _decompose(root, [{"title": "engineer 子卡", "body": NO_SECTION_BODY,
                           "assignee": " Engineer "}])
    assert _task_count() == 1


def test_apply_fanout_reports_rejection_without_partial_writes(kanban_home):
    """decompose 业务层把闸门拒绝转成 ok=False（不抛、不留半个图）。"""
    from hermes_cli import kanban_decompose as decomp

    root = _triage_root()
    routing = decomp._Routing(orchestrator="pm", default_assignee="pm",
                              auto_promote=True, roster=[],
                              valid_names={"pm", "engineer"})
    parsed = {"fanout": True, "tasks": [
        {"title": "pm 子卡", "body": NO_SECTION_BODY, "assignee": "pm"},
        {"title": "engineer 子卡", "body": "y", "assignee": "engineer"},
    ]}
    out = decomp._apply_fanout(root, parsed, routing, "pm")
    assert out.ok is False, out
    assert "建卡闸门拦下" in out.reason, out.reason
    assert out.child_ids is None or out.child_ids == []
    assert _task_count() == 1
