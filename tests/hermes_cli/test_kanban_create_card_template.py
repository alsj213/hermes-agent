"""建卡模板闸门：正文出现模板固定段落标题 → 建卡即拒（CLI rc2 / 工具 tool_error）。

闸门只有一处实现（``kanban_db._validate_card_template``），常量
``FIXED_HEADINGS`` / ``FIXED_HEADING_RE`` 镜像本仓 ``src/team_board/cardtemplate.py``
的同名常量（逐字比对在 team-board 的 ``tests/unit/test_cardtemplate_kernel.py``）。

本文件的判据（卡 t_280eb625 的差异要求 4）：

- 红例：正文含 ``## <固定标题>``（顶格整行）→ :class:`kb.CardTemplateError`，卡不落库；
- 干净 body：落库成功；
- CLI 层：``hermes kanban create`` → rc2，stderr 恰一行、零 traceback。

口径（与卡边界例外一致）：

- 只判「写进正文的固定段落标题」，**不**校验五段是否齐全 —— 缺段不拦，
  否则会误杀 chief 需求卡与 ops 巡检卡；
- 与 assignee 无关：所有卡都拦（不像 ``_validate_qa_spec`` 只管 engineer）；
- 近似写法不算：``### 完成契约`` / 行内 ``前文 ## 完成契约`` / 标题带后缀。
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


REF_LINE = "TEMPLATE: card-template.md#v3"
SIGN = "QA-SPEC: qa"
CLEAN_BODY = REF_LINE + "\n# 目标\n实现点东西。\n"
CLEAN_ENGINEER_BODY = (
    REF_LINE + "\nPATH-CLAIM: src/a.py\nTEST-PLAN: quick\n"
    "CONSUMER: python -m pytest\n# 目标\n实现点东西。\n\n## 验收标准\n" + SIGN
    + "\n- 跑 pytest\n")
RED_ONE_SECTION_BODY = REF_LINE + "\n# 目标\n\n## 完成契约\n- 只做这些\n"
RED_TWO_SECTIONS_BODY = (
    REF_LINE + "\n# 目标\n\n## 完成契约\n- 只做这些\n\n## 长内容去哪\n- 卡评论\n")


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
# 常量：镜像口径与构造方式
# ---------------------------------------------------------------------------

def test_fixed_headings_tuple_order_is_contract():
    """条目与顺序都算契约：team-board 侧逐字比对，换序也会红。"""
    assert kb.FIXED_HEADINGS == (
        "完成契约", "验收三段式", "验收三段", "边界默认值",
        "R6 三条", "交付方式", "长内容去哪",
    )


def test_regex_is_derived_from_headings_not_a_literal():
    """正则必须由元组推出（不许另写字面量），且带 MULTILINE。"""
    import re

    derived = r"^##\s*(?:" + "|".join(map(re.escape, kb.FIXED_HEADINGS)) + r")\s*$"
    assert kb.FIXED_HEADING_RE.pattern == derived
    assert kb.FIXED_HEADING_RE.flags & re.MULTILINE


# ---------------------------------------------------------------------------
# 内核层：红例被拒且不落库
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heading", kb.FIXED_HEADINGS)
def test_each_fixed_heading_is_rejected(kanban_home, heading):
    body = REF_LINE + "\n# 目标\n\n## " + heading + "\n- x\n"
    with pytest.raises(kb.CardTemplateError) as ei:
        _create("需求：某件事", "pm", body)
    msg = str(ei.value)
    assert "建卡闸门拦下" in msg, msg
    assert heading in msg, msg
    assert REF_LINE in msg, msg
    assert _task_count() == 0, "被拒的卡不许落库"


def test_heading_without_space_is_rejected(kanban_home):
    """``##完成契约`` 也算命中（``\\s*`` 允许零空格）。"""
    with pytest.raises(kb.CardTemplateError) as ei:
        _create("需求：某件事", "pm", REF_LINE + "\n##完成契约\n- x\n")
    assert "完成契约" in str(ei.value)


def test_all_hits_are_reported_in_first_seen_order_without_duplicates(kanban_home):
    with pytest.raises(kb.CardTemplateError) as ei:
        _create("需求：某件事", "pm", RED_TWO_SECTIONS_BODY + "\n## 完成契约\n- 再来一次\n")
    msg = str(ei.value)
    assert "「完成契约」「长内容去哪」" in msg, msg


@pytest.mark.parametrize("body", [
    REF_LINE + "\n# 目标\n实现点东西。\n",
    "# 目标\n\n## 验收标准\n" + SIGN + "\n- 跑 pytest\n",  # 验收标准不在固定段落表内
    "# 目标\n\n### 完成契约\n- 三级标题不算\n",
    "# 目标\n- 前文 ## 完成契约 行内出现不算\n",
    "# 目标\n\n## 完成契约（v3）\n- 标题带后缀不算\n",
    "# 目标\n\n## 完成契约清单\n- 后缀文字不算\n",
    "",
])
def test_clean_bodies_are_accepted(kanban_home, body):
    assert _create("需求：某件事", "pm", body)
    assert _task_count() == 1


def test_none_body_is_accepted(kanban_home):
    """``body=None`` 不判红（没有正文 = 没抄模板）。"""
    assert _create("需求：某件事", "pm", None)
    assert _task_count() == 1


def test_does_not_require_all_five_sections(kanban_home):
    """缺段不拦：五段是否齐全不归本闸门（边界例外，防误杀 chief / ops 卡）。"""
    assert _create("需求：某件事", "pm", "# 目标\n只写差异部分。\n")
    assert _create("实现：x", "engineer", CLEAN_ENGINEER_BODY)
    assert _task_count() == 2


@pytest.mark.parametrize("assignee", ["pm", "ops", "qa", "hatch", "engineer", "user"])
def test_gate_applies_to_every_assignee(kanban_home, assignee):
    """与 assignee 无关：所有卡都拦（qa 签署行也救不回固定段落）。"""
    body = (RED_ONE_SECTION_BODY if assignee != "engineer"
            else "PATH-CLAIM: src/a.py\nTEST-PLAN: quick\nCONSUMER: python -m pytest\n"
            + RED_ONE_SECTION_BODY + "\n## 验收标准\n" + SIGN + "\n")
    with pytest.raises(kb.CardTemplateError):
        _create("需求：某件事", assignee, body)
    assert _task_count() == 0


def test_engineer_card_with_clean_body_still_needs_qa_spec(kanban_home):
    """两道闸门互相独立：本闸门放行了，qa 签署闸门照拦。"""
    with pytest.raises(kb.QaSpecMissingError):
        _create("实现：x", "engineer", CLEAN_BODY)
    assert _task_count() == 0


def test_qa_spec_gate_runs_first(kanban_home):
    """插入点是 ``_validate_qa_spec`` 之后 —— engineer 卡两道都缺时报签署闸门。"""
    with pytest.raises(kb.QaSpecMissingError):
        _create("实现：x", "engineer", RED_ONE_SECTION_BODY)


def test_body_limit_gate_runs_first(kanban_home):
    """R16 2048B 上限仍是第一道：超限 + 固定段落 → BodyTooLargeError。"""
    with pytest.raises(kb.BodyTooLargeError):
        _create("需求：某件事", "pm", RED_ONE_SECTION_BODY + ("填充" * 700))


def test_exception_is_value_error(kanban_home):
    """工具层按 ValueError 走 tool_error（无 traceback），这条继承关系是契约。"""
    assert issubclass(kb.CardTemplateError, ValueError)


# ---------------------------------------------------------------------------
# CLI 层：rc2 + stderr 恰一行
# ---------------------------------------------------------------------------

def test_cli_create_rc2_on_fixed_heading(kanban_home, capsys):
    args = _parse_create_argv(["create", "需求：x", "--body", RED_ONE_SECTION_BODY,
                               "--assignee", "pm"])
    assert kc._cmd_create(args) == 2
    _out, err = capsys.readouterr()
    assert "建卡闸门拦下" in err
    assert "完成契约" in err
    assert REF_LINE in err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0, f"stderr 必须恰一行：{err!r}"
    assert _task_count() == 0


def test_cli_create_rc0_on_clean_body(kanban_home, capsys):
    args = _parse_create_argv(["create", "需求：x", "--body", CLEAN_BODY,
                               "--assignee", "pm"])
    assert kc._cmd_create(args) == 0
    _out, err = capsys.readouterr()
    assert "Traceback" not in err
    assert _task_count() == 1


# ---------------------------------------------------------------------------
# 工具层：kanban_create 返回 {"error": ...}，不抛
# ---------------------------------------------------------------------------

def test_tool_create_rejected_with_tool_error(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "需求：x", "body": RED_ONE_SECTION_BODY,
                                        "assignee": "pm"}))
    assert "error" in out, out
    assert "建卡闸门拦下" in out["error"]
    assert _task_count() == 0


def test_tool_create_accepts_clean_body(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "需求：x", "body": CLEAN_BODY,
                                        "assignee": "pm"}))
    assert "error" not in out, out
    assert _task_count() == 1
