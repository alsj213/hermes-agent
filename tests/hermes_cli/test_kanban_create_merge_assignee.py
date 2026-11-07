"""建卡 assignee 闸门：合并类卡的 assignee 必须是 ``repo`` → 否则建卡即拒。

闸门只有一处实现（``kanban_db._validate_merge_assignee``，由 ``create_task`` 调用），
CLI 与 agent 工具两条路都必须在这里被同一份校验拦下：

- CLI：``hermes kanban create`` → rc2、stderr 恰一行、零 traceback；
- 工具：``kanban_create`` → ``{"error": ...}``，不抛 traceback。

谓词复用 ``_PR_CLAIM_TRIGGER_RE``（唯一正则），所以触发标题集合与 PR-CLAIM 闸门一致。
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


# 合并类卡必须同时满足 PR-CLAIM 闸门（正文首行），所以正文统一带认领行。
CLAIM_BODY = "PR-CLAIM: #99\n正文"


def _create(title, assignee, body=CLAIM_BODY):
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


MERGE_TITLES = [
    "合并卡（pm）：PR #99 合入 main",
    "收口卡：PR #47",
    "合并 PR #45（merge_guard）",
    "合并PR#45 无空格",
    "收口：PR #12",
    "收口(PR #12)",
]


# ---------------------------------------------------------------------------
# 内核层：合并类卡 + 非 repo assignee → MergeAssigneeError，卡不落库
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", MERGE_TITLES)
@pytest.mark.parametrize("assignee", ["pm", "engineer", "qa", "hatch", "ops", "repo2", "REPO2",
                                      "repos", "repoo"])
def test_merge_title_with_non_repo_assignee_is_rejected(kanban_home, title, assignee):
    """取值域边界：repo 的近邻串（repo2 / repos / repoo）、大小写变体、其它 profile 全拒。"""
    with pytest.raises(kb.MergeAssigneeError) as ei:
        _create(title, assignee)
    msg = str(ei.value)
    assert "合并类卡建卡闸门拦下" in msg
    assert "合并权归 repo bot" in msg
    assert _task_count() == 0, "被拒的卡不许落库"


@pytest.mark.parametrize("title", MERGE_TITLES)
def test_merge_title_with_repo_assignee_is_accepted(kanban_home, title):
    tid = _create(title, "repo")
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert task.assignee == "repo"
    assert _task_count() == 1


def test_empty_assignee_is_rejected_with_kong_wording(kanban_home):
    """assignee 为空也要拦（None → 「空」），并且不是 AttributeError。"""
    with pytest.raises(kb.MergeAssigneeError) as ei:
        _create("合并卡（pm）：PR #99", None)
    assert "收到「空」" in str(ei.value)
    assert _task_count() == 0


def test_assignee_case_and_whitespace_are_canonicalized(kanban_home):
    """判据用已规范化的 assignee（create_task 里 `_canonical_assignee` 之后）：
    ``REPO`` / `` repo `` 规范化成 ``repo`` → 放行，不另写一套大小写/空白规则。"""
    for raw in ("REPO", " repo", "repo ", " Repo "):
        tid = _create("合并卡（pm）：PR #99", raw)
        assert tid, raw
    assert _task_count() == 4


@pytest.mark.parametrize("title", [
    "返工 PR #51：docs 口径",
    "文档（pm）：补充合并流程说明",
    "实现（engineer）：合并类卡建卡闸门",
    "工作卡（engineer）：X",
])
def test_non_merge_titles_are_not_gated(kanban_home, title):
    """不误伤：标题不含合并/收口**前缀** → assignee 自由。"""
    tid = _create(title, "pm", body="无认领行也不拦")
    assert tid
    assert _task_count() == 1


def test_exception_is_value_error(kanban_home):
    """工具层按 ValueError 走 tool_error（无 traceback），这条继承关系是契约。"""
    assert issubclass(kb.MergeAssigneeError, ValueError)


def test_pr_claim_gate_still_runs_first(kanban_home):
    """顺序契约：PR-CLAIM 闸门在前（合同要求 _validate_pr_claim 之后调用）。"""
    with pytest.raises(kb.PrClaimMissingError):
        _create("合并卡（pm）：PR #99", "pm", body="没有认领行")
    assert _task_count() == 0


def test_body_limit_still_runs_before(kanban_home):
    """R16 上限仍是第一道：超限报 BodyTooLargeError，不是 MergeAssigneeError。"""
    with pytest.raises(kb.BodyTooLargeError):
        _create("合并卡（pm）：PR #99", "pm", body="长" * 800)
    assert _task_count() == 0


def test_repo_profile_registry_is_not_consulted(kanban_home, monkeypatch):
    """纯字符串判据：不读 profile 注册表、不校验 repo 是否存在。

    把 ``hermes_cli.profiles`` 里的查找入口全换成会抛 AssertionError 的桩；
    若闸门偷偷查注册表，这里会炸。
    """
    import hermes_cli.profiles as prof

    names = [n for n in ("get_profile", "load_profiles", "profile_exists", "list_profiles")
             if hasattr(prof, n)]
    assert names, "profiles 模块的查找入口改了名，本条需同步"
    for name in names:
        def _boom(*a, __n=name, **kw):
            raise AssertionError(f"闸门不许读 profile 注册表（调用了 {__n}）")
        monkeypatch.setattr(prof, name, _boom)
    assert isinstance(kb._MERGE_CARD_ASSIGNEE, str) and kb._MERGE_CARD_ASSIGNEE == "repo"
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="合并卡（pm）：PR #99", body=CLAIM_BODY,
                             assignee="repo")
    assert tid


# ---------------------------------------------------------------------------
# CLI 层：rc2 + stderr 恰一行 + 零 traceback
# ---------------------------------------------------------------------------

def test_cli_create_rc2_on_merge_card_with_pm(kanban_home, capsys):
    args = _parse_create_argv(["create", "合并卡：PR #9（t_1234abcd）",
                               "--body", CLAIM_BODY, "--assignee", "pm"])
    rc = kc._cmd_create(args)
    _out, err = capsys.readouterr()
    assert rc == 2
    assert "合并权归 repo bot" in err
    assert err.startswith("kanban: "), err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0, f"stderr 必须恰一行：{err!r}"
    assert _task_count() == 0


def test_cli_create_rc0_with_repo(kanban_home, capsys):
    args = _parse_create_argv(["create", "合并卡：PR #9（t_1234abcd）",
                               "--body", CLAIM_BODY, "--assignee", "repo"])
    assert kc._cmd_create(args) == 0
    _out, err = capsys.readouterr()
    assert "Traceback" not in err
    assert _task_count() == 1


def test_cli_create_rc0_for_rework_title(kanban_home, capsys):
    """不误伤真板上的实际标题形态：返工 PR #51 …"""
    args = _parse_create_argv(["create", "返工 PR #51：docs 口径",
                               "--body", "正文", "--assignee", "pm"])
    assert kc._cmd_create(args) == 0
    assert _task_count() == 1


def test_cli_pr_claim_rc2_unchanged(kanban_home, capsys):
    """PR-CLAIM 两条路不许因本次改动退化。"""
    args = _parse_create_argv(["create", "合并卡（pm）：PR #99 合入 main",
                               "--body", "x", "--assignee", "repo"])
    assert kc._cmd_create(args) == 2
    _out, err = capsys.readouterr()
    assert "PR-CLAIM 闸门拦下建卡" in err
    assert err.strip().count("\n") == 0
    assert _task_count() == 0


# ---------------------------------------------------------------------------
# 工具层：kanban_create 返回 {"error": ...}，不抛
# ---------------------------------------------------------------------------

def test_tool_create_rejected_with_tool_error(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "合并卡：PR #9（t_1234abcd）",
                                        "body": CLAIM_BODY, "assignee": "pm"}))
    assert "error" in out, out
    assert "合并权归 repo bot" in out["error"]
    assert _task_count() == 0


def test_tool_create_accepts_repo(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "合并卡：PR #9（t_1234abcd）",
                                        "body": CLAIM_BODY, "assignee": "repo"}))
    assert "error" not in out, out
    assert _task_count() == 1


def test_tool_create_neutral_card_unaffected(kanban_home):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "返工 PR #51：docs 口径",
                                        "body": "正文", "assignee": "pm"}))
    assert "error" not in out, out
    assert _task_count() == 1
