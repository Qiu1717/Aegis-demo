# -*- coding: utf-8 -*-
"""
apps/skills/services.py — Skill 注册中心（注册 / 启用禁用 / 删除 / 注入清单）

存储：config.user.json 的 "skills" 列表（与权限模式、思考量级同一存储层）。
    [{ "id": "explain-code", "name": "代码讲解", "description": "...",
       "instructions": "...", "type": "prompt", "enabled": false,
       "built_in": true, "created_at": "..." }]

约定：
    · id：小写字母开头，字母数字下划线连字符，≤32 字符，全局唯一；
    · 内置技能（built_in=True）不可删除、不可改名，只能启用/禁用；
    · 上限 MAX_SKILLS 条，防止配置无限膨胀；
    · 所有对外返回都不含敏感信息；指令内容只注入给本机 Agent 的 system 提示词。
"""
import re
from datetime import datetime
from typing import Any, Dict, List

from apps import logger
from apps.settings.services import read_user_config, write_user_config, USER_CFG_VERSION

#: 技能 id 规范
SKILL_ID_OK = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
#: 技能数量上限（含内置）
MAX_SKILLS = 50
#: 目前仅支持 prompt 型；后续扩展在此登记新类型（扩展点）
SKILL_TYPES = ("prompt",)

#: 内置技能（首次访问时懒注入到配置；不可删除、不可改名，可禁用）
BUILT_IN_SKILLS: List[Dict[str, Any]] = [
    {
        "id": "explain-code",
        "name": "代码讲解",
        "description": "把代码用通俗语言逐段讲解，适合学习与审查",
        "instructions": "当用户要求讲解、解释代码时：先给一句话总览，再逐块说明逻辑、"
                        "输入输出与边界情况；引用代码时带文件与行号。",
        "enabled": False,
    },
    {
        "id": "write-docs",
        "name": "文档生成",
        "description": "为代码或项目生成结构化中文文档",
        "instructions": "当用户要求写文档、README、使用说明时：输出结构化中文文档，"
                        "包含 概述 / 用法 / 参数 / 示例 / 注意事项 五节。",
        "enabled": False,
    },
]

#: 回复中标注技能使用的标记（runner 据此发 skill_used 事件）
SKILL_MARK_RE = re.compile(r"\[技能[:：]\s*([^\]】]{1,40})\]")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_skills() -> List[Dict[str, Any]]:
    """读取技能注册表；内置技能缺失时懒注入（保证升级后内置技能可用）"""
    user_cfg = read_user_config()
    skills = user_cfg.get("skills")
    if not isinstance(skills, list):
        skills = []
    changed = False
    have_ids = {str(s.get("id") or "") for s in skills if isinstance(s, dict)}
    for built in BUILT_IN_SKILLS:
        if built["id"] not in have_ids:
            entry = dict(built)
            entry["type"] = "prompt"
            entry["built_in"] = True
            entry["created_at"] = _now()
            skills.append(entry)
            changed = True
    if changed:
        user_cfg["skills"] = skills
        user_cfg["version"] = USER_CFG_VERSION
        if not write_user_config(user_cfg):
            logger.warning("内置技能懒注入写入失败，本次按内存态返回")
    return [s for s in skills if isinstance(s, dict)]


def _save_skills(skills: List[Dict[str, Any]]) -> bool:
    user_cfg = read_user_config()
    user_cfg["skills"] = skills
    user_cfg["version"] = USER_CFG_VERSION
    return write_user_config(user_cfg)


def _public(skill: Dict[str, Any]) -> Dict[str, Any]:
    """对外视图（不含内部字段）"""
    view = {
        "id": str(skill.get("id") or ""),
        "name": str(skill.get("name") or ""),
        "description": str(skill.get("description") or ""),
        "instructions": str(skill.get("instructions") or ""),
        "type": str(skill.get("type") or "prompt"),
        "enabled": bool(skill.get("enabled")),
        "built_in": bool(skill.get("built_in")),
        "created_at": str(skill.get("created_at") or ""),
    }
    if skill.get("package"):
        from .packages import package_root, RESOURCE_BOUNDARY
        view.update(package=True, source=str(skill.get("source") or ""),
                    resource_root=str(package_root(skill["id"])), resource_boundary=RESOURCE_BOUNDARY)
    return view


def list_skills() -> List[Dict[str, Any]]:
    """全部技能（含内置），保持注册顺序"""
    return [_public(s) for s in _load_skills()]


def get_enabled_skills() -> List[Dict[str, Any]]:
    """已启用技能的注入清单（name + description + instructions）"""
    enabled = []
    for skill in _load_skills():
        if not skill.get("enabled") or skill.get("type", "prompt") != "prompt":
            continue
        instructions = skill["instructions"]
        if skill.get("package"):
            from .packages import package_instructions
            instructions = package_instructions(skill)
        enabled.append({"id": skill["id"], "name": skill["name"],
                        "description": skill["description"], "instructions": instructions})
    return enabled


def read_skill_resource(skill_id: str, relative_path: str) -> Dict[str, Any]:
    """读取已启用导入包的登记文本资源；返回 ok/content 或 ok=False/error_code/reason。"""
    from .packages import IMPORT_LOCK, read_resource
    if not isinstance(skill_id, str) or not SKILL_ID_OK.fullmatch(skill_id):
        return {"ok": False, "error_code": "SKILL_ID_INVALID", "reason": "技能标识不合规"}
    with IMPORT_LOCK:
        skill = next((s for s in _load_skills() if s.get("id") == skill_id), None)
        if skill is None:
            return {"ok": False, "error_code": "SKILL_NOT_FOUND", "reason": "技能不存在"}
        if not skill.get("enabled"):
            return {"ok": False, "error_code": "SKILL_NOT_ENABLED", "reason": "请先启用此技能"}
        if not skill.get("package"):
            return {"ok": False, "error_code": "SKILL_NOT_PACKAGE", "reason": "此技能不是导入的资源包"}
        return read_resource(skill, relative_path)


def register_skill(name: str, description: str, instructions: str,
                   skill_id: str = None, enabled: bool = False) -> Dict[str, Any]:
    """注册一个自定义技能（prompt 型）"""
    name = str(name or "").strip()
    description = str(description or "").strip()
    instructions = str(instructions or "").strip()
    sid = str(skill_id or "").strip().lower()

    if not name or len(name) > 40:
        return {"ok": False, "reason": "技能名称必填且不超过 40 字"}
    if len(description) > 200:
        return {"ok": False, "reason": "技能描述不超过 200 字"}
    if not instructions or len(instructions) > 4000:
        return {"ok": False, "reason": "技能指令必填且不超过 4000 字"}

    skills = _load_skills()
    if len(skills) >= MAX_SKILLS:
        return {"ok": False, "reason": "技能数量已达上限（%d 条）" % MAX_SKILLS}

    if not sid:
        # 未指定 id 时按名称生成：非 ASCII 名称转拼音不可行，退化为 skill-序号
        base = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:24] or "skill"
        sid = base
        seq = 1
        existing = {str(s.get("id") or "") for s in skills}
        while sid in existing:
            seq += 1
            sid = "%s-%d" % (base, seq)
    if not SKILL_ID_OK.match(sid):
        return {"ok": False, "reason": "技能标识需以小写字母开头，仅含小写字母/数字/下划线/连字符（≤32 位）"}
    if any(str(s.get("id") or "") == sid for s in skills):
        return {"ok": False, "reason": "技能标识已存在：%s" % sid}

    skills.append({
        "id": sid, "name": name, "description": description,
        "instructions": instructions, "type": "prompt",
        "enabled": bool(enabled), "built_in": False, "created_at": _now(),
    })
    if not _save_skills(skills):
        return {"ok": False, "reason": "写入用户配置失败，请查看日志"}
    return {"ok": True, "id": sid, "reason": ""}


def update_skill(skill_id: str, **fields) -> Dict[str, Any]:
    """更新技能：支持 enabled / name / description / instructions（内置技能限 enabled）"""
    sid = str(skill_id or "").strip().lower()
    skills = _load_skills()
    target = None
    for s in skills:
        if str(s.get("id") or "") == sid:
            target = s
            break
    if target is None:
        return {"ok": False, "reason": "技能不存在：%s" % sid}

    if target.get("package"):
        from .packages import package_instructions, PackageError
        if any(key in fields for key in ("name", "description", "instructions")):
            return {"ok": False, "reason": "技能包内容请通过明确覆盖导入更新"}
        if fields.get("enabled"):
            try:
                package_instructions(target)
            except PackageError as exc:
                return {"ok": False, "reason": str(exc)}
    if "enabled" in fields:
        target["enabled"] = bool(fields["enabled"])
    if target.get("built_in"):
        # 内置技能：只允许启用/禁用
        pass
    else:
        if "name" in fields:
            name = str(fields["name"] or "").strip()
            if not name or len(name) > 40:
                return {"ok": False, "reason": "技能名称必填且不超过 40 字"}
            target["name"] = name
        if "description" in fields:
            desc = str(fields["description"] or "").strip()
            if len(desc) > 200:
                return {"ok": False, "reason": "技能描述不超过 200 字"}
            target["description"] = desc
        if "instructions" in fields:
            ins = str(fields["instructions"] or "").strip()
            if not ins or len(ins) > 4000:
                return {"ok": False, "reason": "技能指令必填且不超过 4000 字"}
            target["instructions"] = ins

    if not _save_skills(skills):
        return {"ok": False, "reason": "写入用户配置失败，请查看日志"}
    return {"ok": True, "skill": _public(target), "reason": ""}


def delete_skill(skill_id: str) -> Dict[str, Any]:
    """删除自定义技能（内置技能拒绝删除）"""
    sid = str(skill_id or "").strip().lower()
    skills = _load_skills()
    remaining = []
    removed = None
    for s in skills:
        if str(s.get("id") or "") == sid:
            removed = s
            continue
        remaining.append(s)
    if removed is None:
        return {"ok": False, "reason": "技能不存在：%s" % sid}
    if removed.get("built_in"):
        return {"ok": False, "reason": "内置技能不可删除，只能禁用"}
    if removed.get("package"):
        from .packages import delete_package
        saved = delete_package(removed, remaining)
    else:
        saved = _save_skills(remaining)
    if not saved:
        return {"ok": False, "reason": "写入用户配置失败，请查看日志"}
    return {"ok": True, "reason": ""}


# ── 导入 ────────────────────────────────────────────────────

def _make_skill_id(name: str, existing: set) -> str:
    """按名称生成合规且不重复的 id（与 register_skill 的规则保持一致）"""
    base = re.sub(r"[^a-z0-9_-]+", "-", str(name or "").lower()).strip("-")[:24] or "skill"
    sid = base
    seq = 1
    while sid in existing:
        seq += 1
        sid = "%s-%d" % (base, seq)
    return sid


def normalize_import(data: Any):
    """把导入载荷归一成技能列表。

    支持三种形态：
      1. 技能包：{"skills": [ {...}, {...} ]}（允许带 format / version 等附加字段）
      2. 单个技能：{"name": ..., "instructions": ...}
      3. 裸数组：[ {...}, {...} ]

    返回 (items, err)；识别失败时 items 为 None。
    """
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if isinstance(data.get("skills"), list):
            items = data["skills"]
        elif data.get("name") or data.get("instructions"):
            items = [data]
        else:
            return None, "无法识别：需要单个技能对象，或含 skills 数组的技能包"
    else:
        return None, "格式不正确：需要 JSON 对象或数组"

    if not items:
        return None, "技能包里没有任何技能"
    if len(items) > MAX_SKILLS:
        return None, "技能包含 %d 条技能，超过上限 %d 条" % (len(items), MAX_SKILLS)
    return items, ""


def import_skills(raw_data: Any, overwrite: bool = False) -> Dict[str, Any]:
    """批量导入技能：逐条校验，成功入库，失败给出原因且不中断其余条目。

    overwrite=True 时允许覆盖同 id 的自定义技能（内置技能始终拒绝覆盖）。
    """
    items, err = normalize_import(raw_data)
    if err:
        return {"ok": False, "reason": err}

    skills = _load_skills()
    existing = {str(s.get("id") or "") for s in skills}
    imported: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            skipped.append({"index": idx, "name": "第 %d 条" % idx, "reason": "不是有效的技能对象"})
            continue

        name = str(item.get("name") or "").strip()
        desc = str(item.get("description") or "").strip()
        ins = str(item.get("instructions") or "").strip()
        label = name or ("第 %d 条" % idx)

        if not name or len(name) > 40:
            skipped.append({"index": idx, "name": label, "reason": "名称必填且不超过 40 字"})
            continue
        if not ins or len(ins) > 4000:
            skipped.append({"index": idx, "name": label, "reason": "指令必填且不超过 4000 字"})
            continue
        if len(desc) > 200:
            skipped.append({"index": idx, "name": label, "reason": "描述不超过 200 字"})
            continue

        sid = str(item.get("id") or "").strip().lower()
        if sid and not SKILL_ID_OK.match(sid):
            skipped.append({"index": idx, "name": label,
                            "reason": "标识 %s 不合规（小写字母开头，仅字母/数字/下划线/连字符）" % sid})
            continue
        if not sid:
            sid = _make_skill_id(name, existing)

        if sid in existing:
            if not overwrite:
                skipped.append({"index": idx, "name": label, "reason": "技能标识已存在：%s" % sid})
                continue
            pos = None
            for i, s in enumerate(skills):
                if str(s.get("id") or "") == sid:
                    pos = i
                    break
            if pos is not None and skills[pos].get("built_in"):
                skipped.append({"index": idx, "name": label, "reason": "内置技能不可覆盖：%s" % sid})
                continue
            if pos is not None and skills[pos].get("package"):
                skipped.append({"index": idx, "name": label,
                                "reason": "已有完整技能包，请用 SKILL.md/zip/GitHub 明确覆盖导入"})
                continue
            if pos is not None:
                skills.pop(pos)

        if len(skills) >= MAX_SKILLS:
            skipped.append({"index": idx, "name": label, "reason": "技能数量已达上限（%d 条）" % MAX_SKILLS})
            continue

        entry = {
            "id": sid, "name": name, "description": desc,
            "instructions": ins, "type": "prompt",
            "enabled": bool(item.get("enabled", False)),
            "built_in": False, "created_at": _now(),
        }
        skills.append(entry)
        existing.add(sid)
        imported.append({"id": sid, "name": name})

    if not imported:
        return {"ok": False, "reason": "没有可导入的技能（详见跳过原因）", "skipped": skipped}

    if not _save_skills(skills):
        return {"ok": False, "reason": "写入用户配置失败，请查看日志"}

    return {
        "ok": True,
        "imported": imported,
        "skipped": skipped,
        "total": len(imported),
        "reason": "",
    }
