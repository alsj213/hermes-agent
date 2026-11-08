"""card kind：结构化卡类型替代正文启发式。

内核闸门只认 ``tasks.kind``：
- implementation → QA-SPEC / contract / qa 收口 / tier 闸门；
- spec_sign（qa 签卡）、requirement（pm/chief 需求）、merge 不触发实现卡闸门。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

CONTRACT = (
    "PATH-CLAIM: src/a.py\n"
    "TEST-PLAN: quick\n"
    "CONSUMER: python -m pytest\n"
)
SIGNED = CONTRACT + "# 目标\n实现\n\n## 验收标准\nQA-SPEC: qa\n- 跑 pytest\n"
REQUIREMENT_SIGNED = "# 目标\n需求\n\n## 验收标准\nQA-SPEC: qa\n"


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


def _create(*, assignee: str, body: str = REQUIREMENT_SIGNED, kind: str | None = None):
    with kbc.connect_closing() as conn:
        return kb.create_task(
            conn, title=f"kind test {assignee}", body=body, assignee=assignee, kind=kind)


def _kind(tid: str) -> str:
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, tid).kind


class TestInfer:
    @pytest.mark.parametrize("assignee, want", [
        ("engineer", "implementation"),
        ("qa", "spec_sign"),
        ("repo", "merge"),
        ("pm", "requirement"),
        ("chief", "requirement"),
        ("ops", "requirement"),
        ("hatch", "requirement"),
    ])
    def test_role_defaults(self, assignee: str, want: str) -> None:
        assert kb._infer_task_kind(assignee) == want
        assert kb._infer_task_kind(None) == "requirement"

    def test_invalid_kind_is_rejected(self, kanban_home) -> None:
        with pytest.raises(ValueError, match="kind must be one of"):
            _create(assignee="pm", kind="garbage")

    def test_explicit_kind_wins_over_role(self, kanban_home) -> None:
        tid = _create(assignee="qa", kind="requirement")
        assert _kind(tid) == "requirement"


class TestGates:
    def test_qa_sign_card_not_tier_gated(self, kanban_home) -> None:
        # spec_sign + QA-SPEC body, no tier: qa can close.
        tid = _create(assignee="qa", body=REQUIREMENT_SIGNED)
        assert _kind(tid) == "spec_sign"
        with kbc.connect_closing() as conn:
            assert kb.complete_task(conn, tid, result="signed", acting_profile="qa") is True

    def test_pm_requirement_not_qa_role_gated(self, kanban_home) -> None:
        tid = _create(assignee="pm", body=REQUIREMENT_SIGNED)
        assert _kind(tid) == "requirement"
        with kbc.connect_closing() as conn:
            assert kb.complete_task(conn, tid, result="closed", acting_profile="pm") is True

    def test_engineer_implementation_is_qa_gated(self, kanban_home) -> None:
        tid = _create(assignee="engineer", body=SIGNED)
        assert _kind(tid) == "implementation"
        with kbc.connect_closing() as conn:
            with pytest.raises(kb.QaCompletionError):
                kb.complete_task(conn, tid, result="done", acting_profile="pm")

    def test_explicit_implementation_requires_qa_spec(self, kanban_home) -> None:
        with pytest.raises(kb.QaSpecMissingError):
            _create(assignee="pm", body="# 目标\n没有签署\n", kind="implementation")

    def test_created_event_carries_kind(self, kanban_home) -> None:
        tid = _create(assignee="pm")
        with kbc.connect_closing() as conn:
            row = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created'",
                (tid,),
            ).fetchone()
        assert json.loads(row["payload"])["kind"] == "requirement"


class TestMigration:
    def test_legacy_rows_are_backfilled_once(self, kanban_home) -> None:
        engineer = _create(assignee="engineer", body=SIGNED)
        qa = _create(assignee="qa")
        pm = _create(assignee="pm")
        with kbc.connect_closing() as conn:
            try:
                conn.execute("ALTER TABLE tasks DROP COLUMN kind")
                conn.commit()
            except sqlite3.OperationalError:
                pytest.skip("SQLite too old for DROP COLUMN")
        kb._INITIALIZED_PATHS.clear()
        kbc.init_db()
        with kbc.connect_closing() as conn:
            kinds = {
                row["id"]: row["kind"]
                for row in conn.execute(
                    "SELECT id, kind FROM tasks WHERE id IN (?, ?, ?)",
                    (engineer, qa, pm),
                )
            }
        assert kinds == {
            engineer: "implementation",
            qa: "spec_sign",
            pm: "requirement",
        }
