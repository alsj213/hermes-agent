"""建卡 PR-CLAIM 闸门：合并类卡正文首行缺 ``PR-CLAIM: #<n>`` → 建卡即拒。

闸门只有一处实现（``kanban_db._validate_pr_claim``，由 ``create_task`` 调用），
CLI 与 agent 工具两条路都必须在这里被同一份校验拦下：

- CLI：``hermes kanban create`` → rc2、stderr 恰一行、零 traceback；
- 工具：``kanban_create`` → ``{"error": ...}``，不抛 traceback。
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


def _create(title, body=None, assignee="repo"):
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
# 触发谓词：标题前缀命中合并/收口语义（正文关键词不算）
# ---------------------------------------------------------------------------

MERGE_TITLES = [
    "合并卡（pm）：PR #99 合入 main",
    "收口卡：PR #47",
    "合并 PR #45（merge_guard）",
    "合并PR#45 无空格",
    "收口：PR #12",
    "收口(PR #12)",
]


@pytest.mark.parametrize("title", MERGE_TITLES)
def test_merge_title_without_claim_is_rejected(kanban_home, title):
    with pytest.raises(kb.PrClaimMissingError) as ei:
        _create(title, "正文里没有认领行")
    assert "PR-CLAIM 闸门拦下建卡" in str(ei.value)
    assert _task_count() == 0, "被拒的卡不许落库"


@pytest.mark.parametrize("title", MERGE_TITLES)
def test_merge_title_with_claim_first_line_is_accepted(kanban_home, title):
    tid = _create(title, "PR-CLAIM: #99\n正文第二行")
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert task.body.startswith("PR-CLAIM: #99")


def test_claim_after_leading_blank_lines_is_accepted(kanban_home):
    """首行取「第一个非空行」（照规格实现），前面空行不影响。"""
    tid = _create("合并卡（pm）：PR #99", "\n\nPR-CLAIM: #99\n正文")
    assert tid


def test_neutral_title_is_not_gated(kanban_home):
    """非合并类卡照旧自由建：标题不含合并/收口前缀。"""
    tid = _create("工作卡（engineer）：X", "随便写点背景")
    assert tid
    tid2 = _create("实现（engineer）：合并类卡建卡闸门", "标题里有「合并」但不是前缀 → 不拦")
    assert tid2
    assert _task_count() == 2


def test_title_keyword_inside_not_prefix_is_not_gated(kanban_home):
    """判据是标题**前缀**：正文/标题中段出现「合并」不该逼人写认领行。"""
    tid = _create("文档（pm）：补充合并流程说明", "无认领行")
    assert tid


# ---------------------------------------------------------------------------
# 边界：认领行形态
# ---------------------------------------------------------------------------

BAD_CLAIMS = [
    ("PR-CLAIM:#99", "少空格"),
    ("PR-CLAIM: #0", "PR 号 0 非法"),
    ("PR-CLAIM: #099", "前导 0 非法"),
    ("PR-CLAIM: #99 尾随字符", "行尾有尾随字符"),
    ("PR-CLAIM: #-1", "负数"),
    ("PR-CLAIM: #", "空号"),
    ("pr-claim: #99", "小写不算"),
]


@pytest.mark.parametrize("first_line,why", BAD_CLAIMS)
def test_bad_claim_line_is_rejected(kanban_home, first_line, why):
    with pytest.raises(kb.PrClaimMissingError):
        _create("合并卡（pm）：PR #99", first_line + "\n正文")
    assert _task_count() == 0, why


def test_claim_on_second_line_is_rejected(kanban_home):
    """认领行必须在**首行**；第二行不算（否则等于没有机器闸门）。"""
    with pytest.raises(kb.PrClaimMissingError):
        _create("合并卡（pm）：PR #99", "说明文字\nPR-CLAIM: #99")
    assert _task_count() == 0


def test_empty_body_is_rejected(kanban_home):
    with pytest.raises(kb.PrClaimMissingError):
        _create("合并卡（pm）：PR #99", "")
    with pytest.raises(kb.PrClaimMissingError):
        _create("合并卡（pm）：PR #99", None)


def test_error_message_carries_trigger_and_first_line(kanban_home):
    with pytest.raises(kb.PrClaimMissingError) as ei:
        _create("合并卡（pm）：PR #99", "先说结论：今天不合并")
    msg = str(ei.value)
    assert "合并卡" in msg
    assert "先说结论：今天不合并" in msg
    assert "PR-CLAIM: #<PR号>" in msg


def test_exception_is_value_error(kanban_home):
    """工具层按 ValueError 走 tool_error（无 traceback），这条继承关系是契约。"""
    assert issubclass(kb.PrClaimMissingError, ValueError)


def test_body_limit_checked_before_pr_claim(kanban_home):
    """顺序契约：2048B 上限先判（R16 先例在前），超限报 BodyTooLargeError 而不是认领缺失。"""
    with pytest.raises(kb.BodyTooLargeError):
        _create("合并卡（pm）：PR #99", "无认领行" * 400)


# ---------------------------------------------------------------------------
# CLI 层：rc2 + stderr 恰一行 + 零 traceback
# ---------------------------------------------------------------------------

def test_cli_create_rc2_on_merge_card_without_claim(kanban_home, capsys):
    args = _parse_create_argv(["create", "合并卡（pm）：PR #99 合入 main",
                               "--body", "x", "--assignee", "pm"])
    rc = kc._cmd_create(args)
    _out, err = capsys.readouterr()
    assert rc == 2
    assert "PR-CLAIM 闸门拦下建卡" in err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0, f"stderr 必须恰一行：{err!r}"
    assert _task_count() == 0


def test_cli_create_rc0_with_claim(kanban_home, capsys):
    args = _parse_create_argv(["create", "合并卡（pm）：PR #99 合入 main",
                               "--body", "PR-CLAIM: #99\n正文", "--assignee", "repo"])
    assert kc._cmd_create(args) == 0
    assert _task_count() == 1


def test_cli_create_rc0_for_neutral_card(kanban_home, capsys):
    # 正文带 qa 签署行：assignee=engineer 现在还要过验收标准闸门（G2-b），
    # 本条只验「中性标题不被 PR-CLAIM 拦」。
    args = _parse_create_argv(["create", "工作卡（engineer）：X",
                               "--body", "PATH-CLAIM: src/a.py\nTEST-PLAN: quick\n"
                               "CONSUMER: python -m pytest\n\n"
                               "## 验收标准\nQA-SPEC: qa\n",
                               "--assignee", "engineer"])
    assert kc._cmd_create(args) == 0
    assert _task_count() == 1


def test_cli_create_rc2_on_oversized_body(kanban_home, capsys):
    """except 元组两支都走 rc2：2048B 上限这条路不许因为本次改动退化。"""
    args = _parse_create_argv(["create", "工作卡（engineer）：X",
                               "--body", "长" * 800, "--assignee", "engineer"])
    assert kc._cmd_create(args) == 2
    _out, err = capsys.readouterr()
    assert "body 超过硬上限" in err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0
    assert _task_count() == 0


# ---------------------------------------------------------------------------
# 工具层：kanban_create 返回 {"error": ...}，不抛
# ---------------------------------------------------------------------------

def test_tool_create_rejected_with_tool_error(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "合并卡（pm）：PR #99",
                                        "body": "没有认领行", "assignee": "pm"}))
    assert "error" in out
    assert "PR-CLAIM 闸门拦下建卡" in out["error"]
    assert _task_count() == 0


def test_tool_create_accepts_claim(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "合并卡（pm）：PR #99",
                                        "body": "PR-CLAIM: #99\n正文", "assignee": "repo"}))
    assert "error" not in out, out
    assert _task_count() == 1
