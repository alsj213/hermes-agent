"""worker_context 注入：按卡的 ``TEMPLATE`` 引用贴固定段落（fail-soft）。

判据（卡 t_c0f2c33e 的差异要求 1–3）：

- 解析顺序 ``$HERMES_CARD_TEMPLATE`` → ``<kanban_home>/templates/card-template.md``；
- 版本号取自卡正文引用行（``kb.TEMPLATE_REF_RE``，MULTILINE），固定段落取文件里
  ``<!-- FIXED:<ver>:BEGIN -->`` 与 ``<!-- FIXED:<ver>:END -->`` 之间的全文，
  注入段标题 ``## Template (card-template.md#<ver>)``，超 4096 字节按字节截断标
  ``… [truncated]``；
- fail-soft：无 TEMPLATE 行 / 文件缺失 / 锚点缺 / 版本不符 → 不抛异常、不动既有段，
  只多一行 ``_template: <原因>；按 docs/card-template.md 写正文``。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

REF = "TEMPLATE: card-template.md#v3"
FIXED_BLOCK = "## 完成契约\n- 只做卡上写的，别扩范围。\n"
TEMPLATE_FILE = (
    "# card-template.md（人读说明，不进上下文）\n\n"
    "<!-- FIXED:v3:BEGIN -->\n" + FIXED_BLOCK + "<!-- FIXED:v3:END -->\n"
)
HINT_SUFFIX = "；按 docs/card-template.md 写正文"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # worker 里带着真板的 DB/TASK 指针与模板覆盖 —— 测试必须完全隔离。
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_CARD_TEMPLATE"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _create(body: str, title: str = "模板注入用例") -> str:
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=title, body=body, assignee="pm")


def _ctx(task_id: str) -> str:
    with kbc.connect_closing() as conn:
        return kb.build_worker_context(conn, task_id)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 正常路径：注入段
# ---------------------------------------------------------------------------

def test_ctx_injects_template_section_from_file(kanban_home, tmp_path, monkeypatch):
    """有文件 → 注入段标题 + 固定段落原文（含首词「完成契约」）。"""
    tpl = _write(tmp_path / "tpl" / "card-template.md", TEMPLATE_FILE)
    monkeypatch.setenv("HERMES_CARD_TEMPLATE", str(tpl))

    ctx = _ctx(_create(REF + "\n# 目标\n实现点东西。\n"))

    assert "## Template (card-template.md#v3)" in ctx, ctx
    assert "完成契约" in ctx, ctx
    assert FIXED_BLOCK.strip() in ctx, ctx
    # 人读说明在锚点之外，不许被带进上下文。
    assert "人读说明，不进上下文" not in ctx, ctx
    # 既有段照旧。
    assert "## Body" in ctx and "实现点东西。" in ctx, ctx


def test_ctx_template_default_path_under_kanban_home(kanban_home):
    """无环境变量 → 回落到 ``<kanban_home>/templates/card-template.md``。"""
    home = kb.kanban_home()
    tpl = _write(home / "templates" / "card-template.md", TEMPLATE_FILE)
    assert tpl.is_file()

    ctx = _ctx(_create(REF + "\n# 目标\nx\n"))

    assert "## Template (card-template.md#v3)" in ctx, ctx
    assert "完成契约" in ctx, ctx


def test_ctx_truncates_block_over_4096_bytes(kanban_home, tmp_path, monkeypatch):
    """超 4096 字节按字节截断并标 ``… [truncated]``。"""
    big = "A" * 5000
    tpl = _write(
        tmp_path / "tpl.md",
        "<!-- FIXED:v3:BEGIN -->\n" + big + "\n<!-- FIXED:v3:END -->\n",
    )
    monkeypatch.setenv("HERMES_CARD_TEMPLATE", str(tpl))

    ctx = _ctx(_create(REF + "\n# 目标\nx\n"))

    assert "## Template (card-template.md#v3)" in ctx, ctx
    assert "… [truncated]" in ctx, ctx
    assert big not in ctx, "超长固定段落不许整段进上下文"
    injected = ctx.split("## Template (card-template.md#v3)\n", 1)[1].split("\n", 1)[0]
    cap = kb._CTX_MAX_TEMPLATE_BYTES + len("… [truncated]".encode())
    assert len(injected.encode("utf-8")) <= cap


# ---------------------------------------------------------------------------
# fail-soft：不抛异常、不注入段、只多一行 _template 提示
# ---------------------------------------------------------------------------

def test_ctx_missing_template_file_fails_soft(kanban_home, tmp_path, monkeypatch):
    """文件缺失 → 不注入段，加一行 ``_template: …缺失…``。"""
    monkeypatch.setenv("HERMES_CARD_TEMPLATE", str(tmp_path / "nope" / "card-template.md"))

    ctx = _ctx(_create(REF + "\n# 目标\nx\n"))  # 不许抛

    assert "## Template (" not in ctx, ctx
    hints = [ln for ln in ctx.splitlines() if ln.startswith("_template: ")]
    assert len(hints) == 1, ctx
    assert "缺失" in hints[0] and hints[0].endswith(HINT_SUFFIX), hints[0]
    # 既有段一字不动：头两行仍是 header。
    assert ctx.splitlines()[0].startswith("# Kanban task "), ctx
    assert "## Body" in ctx, ctx


def test_ctx_without_template_line_does_not_inject(kanban_home, tmp_path, monkeypatch):
    """卡正文没有 TEMPLATE 行 → 不注入（哪怕模板文件就在）。"""
    monkeypatch.setenv("HERMES_CARD_TEMPLATE", str(_write(tmp_path / "tpl.md", TEMPLATE_FILE)))

    ctx = _ctx(_create("# 目标\n没有引用行的老卡。\n"))

    assert "## Template (" not in ctx, ctx
    assert "完成契约" not in ctx, ctx
    hints = [ln for ln in ctx.splitlines() if ln.startswith("_template: ")]
    assert len(hints) == 1 and "没有" in hints[0], ctx


def test_ctx_version_mismatch_fails_soft(kanban_home, tmp_path, monkeypatch):
    """卡要 v3、文件只有 v2 锚点 → fail-soft 且原因写明版本不符。"""
    tpl = _write(
        tmp_path / "tpl.md",
        "<!-- FIXED:v2:BEGIN -->\n老段落\n<!-- FIXED:v2:END -->\n",
    )
    monkeypatch.setenv("HERMES_CARD_TEMPLATE", str(tpl))

    ctx = _ctx(_create(REF + "\n# 目标\nx\n"))

    assert "## Template (" not in ctx, ctx
    hints = [ln for ln in ctx.splitlines() if ln.startswith("_template: ")]
    assert len(hints) == 1, ctx
    assert "版本不符" in hints[0] and "v3" in hints[0] and "v2" in hints[0], hints[0]


# ---------------------------------------------------------------------------
# 契约常量（镜像 cardtemplate.py 的写法，别单方面改）
# ---------------------------------------------------------------------------

def test_template_ref_re_is_multiline_and_captures_version():
    assert kb.TEMPLATE_REF_RE.flags & re.MULTILINE
    m = kb.TEMPLATE_REF_RE.search("前文\n" + REF + "\n后文")
    assert m is not None and m.group(1) == "v3"
    # 无版本号 / 近似写法不算引用行。
    assert kb.TEMPLATE_REF_RE.search("TEMPLATE: card-template.md") is None
    assert kb.TEMPLATE_REF_RE.search("见 TEMPLATE: card-template.md#v3") is None
