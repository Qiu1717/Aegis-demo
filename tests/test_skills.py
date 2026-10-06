# -*- coding: utf-8 -*-
"""
tests/test_skills.py — 验证 apps.skills 注册中心 + AgentRunner 技能标注去重

覆盖（任务要求）：
  · list_skills 空配置返回两个内置技能（explain-code / write-docs，built_in=True）
  · register_skill 正常注册 -> {"ok":True,"id":...}，且再次 list_skills 包含它
  · 无 instructions 注册被拒（ok=False）
  · 重复 id 注册被拒（ok=False）
  · update_skill(enabled=True) 持久化，get_enabled_skills 随后包含它
  · delete_skill 删除内置技能被拒（ok=False）
  · SKILL_MARK_RE 从 "[技能:代码讲解]" 提取名称；AgentRunner._emit_skill_marks 去重
    （同一文本调用两次，第二次为空）

隔离策略：每个用例使用独立的临时数据目录（monkeypatch AEGIS_DATA_DIR），
既不污染真实 %APPDATA% 配置，也不与 conftest 的 .tmpdata 互相串扰，
保证用例间顺序无关、可重复运行。
"""
import os
import sys
import threading

import pytest

# ── 保证项目根在 sys.path（与 conftest 一致；单独运行本文件也成立）──
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from apps.skills import services as S
from apps.skills.services import SKILL_MARK_RE
from apps.agent.runner import AgentRunner


BUILT_IN_IDS = {"explain-code", "write-docs"}


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """每个用例独立的用户数据目录，确保配置读写互不污染。"""
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AEGIS_DATA_DIR", str(d))
    yield


def test_list_skills_empty_config_returns_builtins():
    """空配置首次读取应懒注入两个内置技能，且 built_in=True。"""
    skills = S.list_skills()
    ids = {s["id"] for s in skills}
    assert BUILT_IN_IDS <= ids, "内置技能未注入：%s" % ids
    for s in skills:
        if s["id"] in BUILT_IN_IDS:
            assert s["built_in"] is True
            assert s["enabled"] is False


def test_register_skill_returns_ok_and_listed():
    """正常注册返回 ok=True 且带 id；再次 list_skills 应包含它。"""
    res = S.register_skill("My Skill", "desc", "instructions")
    assert res.get("ok") is True, res
    assert res.get("id")
    ids = {s["id"] for s in S.list_skills()}
    assert res["id"] in ids


def test_register_skill_without_instructions_rejected():
    """指令为空应被校验拒绝（ok=False）。"""
    res = S.register_skill("No Instr", "desc", "")
    assert res.get("ok") is False
    assert "reason" in res


def test_register_duplicate_id_rejected():
    """重复 id 注册应被拒（ok=False）。"""
    r1 = S.register_skill("Dup Skill", "desc", "instructions")
    assert r1.get("ok") is True
    dup_id = r1["id"]
    r2 = S.register_skill("Another", "desc", "instructions", skill_id=dup_id)
    assert r2.get("ok") is False, r2


def test_update_skill_enable_persists_and_enabled_list_includes():
    """update_skill(enabled=True) 持久化后，get_enabled_skills 应包含该技能。"""
    r = S.register_skill("En Skill", "desc", "instructions")
    assert r.get("ok") is True
    sid = r["id"]
    up = S.update_skill(sid, enabled=True)
    assert up.get("ok") is True
    enabled_ids = {s["id"] for s in S.get_enabled_skills()}
    assert sid in enabled_ids


def test_delete_builtin_rejected():
    """内置技能不可删除（ok=False）。"""
    res = S.delete_skill("explain-code")
    assert res.get("ok") is False
    assert "reason" in res


def test_skill_mark_re_extracts_name():
    """SKILL_MARK_RE 能从 "[技能:代码讲解]" 提取出名称。"""
    names = SKILL_MARK_RE.findall("[技能:代码讲解]")
    assert names == ["代码讲解"]


def test_runner_emit_skill_marks_dedups():
    """AgentRunner._emit_skill_marks 对同一文本重复调用应去重（第二次为空）。"""
    runner = AgentRunner(
        workspace=None,
        user_task="x",
        session_id=None,
        cancel_event=threading.Event(),
        thinking_level="balanced",
        skills=[],
    )
    text = "我使用了 [技能:代码讲解] 来完成讲解。"
    first = list(runner._emit_skill_marks(text))
    assert len(first) == 1
    assert first[0]["type"] == "skill_used"
    assert first[0]["skill"] == "代码讲解"
    # 第二次扫描同一文本：已广播过的技能去重，应为空
    second = list(runner._emit_skill_marks(text))
    assert second == []
