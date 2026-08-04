"""
bitable.py —— 多维表格作为 hub 控制台。

每个群一行：群(ID) / 工作目录 / Skills(多选) / MCP(多选)。
- sync_group：把工作目录当前装载状态写回表（文件→表）。
- read_desired：从表读出某群期望装的 skills/mcp（表→文件，由 server 应用）。
schema 用 ensure_schema 幂等创建；多选 options 跟随 hub 清单。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import chatconfig, config, feishu, hub

ROOT = Path(__file__).resolve().parent.parent
_CFG = config.load().get("bitable", {})
APP = _CFG.get("app_token", "")
TBL = _CFG.get("table_id", "")

F_GROUP = "群"
F_WD = "工作目录"
F_SKILLS = "Skills"
F_MCP = "MCP"
F_ENGINE = "引擎"
F_MODEL = "模型"
F_MEMORY = "记忆模式"
F_REPLY = "回复策略"
F_MCP_OVERRIDE = "MCP覆盖(JSON)"


def enabled() -> bool:
    return bool(APP and TBL)


def _api(method: str, path: str = "", payload: dict | None = None) -> dict:
    url = f"{feishu.BASE}/bitable/v1/apps/{APP}/tables/{TBL}{path}"
    return feishu._request(method, url, payload, headers={"Authorization": f"Bearer {feishu._token()}"})


def list_fields() -> list[dict]:
    return (_api("GET", "/fields").get("data") or {}).get("items", [])


def list_records() -> list[dict]:
    return (_api("GET", "/records?page_size=200").get("data") or {}).get("items", [])


def _ms(names: list[str]) -> dict:
    return {"options": [{"name": n} for n in names]}


def ensure_schema() -> None:
    """幂等建表结构：主字段→群，补 工作目录/Skills/MCP，多选 options 跟随 hub。"""
    fields = list_fields()
    by_name = {f["field_name"]: f for f in fields}
    prim = next((f for f in fields if f.get("is_primary")), None)
    if prim and prim["field_name"] != F_GROUP:
        _api("PUT", f"/fields/{prim['field_id']}", {"field_name": F_GROUP, "type": 1})
    for fname in (F_WD, F_ENGINE, F_MODEL, F_MEMORY, F_REPLY, F_MCP_OVERRIDE):
        if fname not in by_name:
            _api("POST", "/fields", {"field_name": fname, "type": 1})
    for fname, opts in ((F_SKILLS, hub.list_skills()), (F_MCP, hub.list_mcp())):
        body = {"field_name": fname, "type": 4, "property": _ms(opts)}
        if fname in by_name:
            _api("PUT", f"/fields/{by_name[fname]['field_id']}", body)
        else:
            _api("POST", "/fields", body)


def _text(v) -> str:
    """多维表格文本字段读出来可能是 str 或 [{'text':..}]，统一成 str。"""
    if isinstance(v, list):
        return "".join(seg.get("text", "") if isinstance(seg, dict) else str(seg) for seg in v).strip()
    return (v or "").strip() if isinstance(v, str) else ""


def _names(v) -> list[str]:
    """多选字段读出来是 [str] 或 [{'text':..}]，统一成 [str]。"""
    out = []
    for x in v or []:
        if isinstance(x, dict):
            out.append(x.get("text") or x.get("name") or "")
        else:
            out.append(str(x))
    return [s for s in out if s]


def loaded_lists(workdir: Path) -> tuple[list[str], list[str]]:
    skills = [n for n in hub.list_skills() if hub.skill_loaded(workdir, n)]
    mcp = [n for n in hub.list_mcp() if hub.mcp_loaded(workdir, n)]
    return skills, mcp


def _find_row(chat_id: str) -> str | None:
    for r in list_records():
        if _text((r.get("fields") or {}).get(F_GROUP)) == chat_id:
            return r["record_id"]
    return None


def sync_group(chat_id: str, workdir: Path) -> None:
    """把某群当前装载状态 + 配置写回表（没有该行则新建）。"""
    skills, mcp = loaded_lists(workdir)
    cfg = chatconfig.get(workdir)
    fields = {
        F_GROUP: chat_id,
        F_WD: workdir.name,
        F_SKILLS: skills,
        F_MCP: mcp,
        F_ENGINE: "Codex",
        F_MODEL: cfg.get("codex_effective_model") or cfg.get("codex_model", chatconfig.DEFAULTS["codex_model"]),
        F_MEMORY: chatconfig.MODE_LABEL.get(cfg.get("memory_mode", "resume"), ""),
        F_REPLY: "自动（单真人全部回复，多真人仅 @ 回复）",
        F_MCP_OVERRIDE: json.dumps(cfg.get("mcp_overrides", {}), ensure_ascii=False) if cfg.get("mcp_overrides") else "",
    }
    rid = _find_row(chat_id)
    if rid:
        _api("PUT", f"/records/{rid}", {"fields": fields})
    else:
        _api("POST", "/records", {"fields": fields})


def delete_record(rid: str) -> None:
    _api("DELETE", f"/records/{rid}")


def read_all_desired() -> list[tuple[str, list[str], list[str], str]]:
    """读全表，返回 [(chat_id, 期望skills, 期望mcp, MCP覆盖JSON文本)]，跳过空群行。"""
    out = []
    for r in list_records():
        f = r.get("fields") or {}
        chat_id = _text(f.get(F_GROUP))
        if not chat_id:
            continue
        out.append((chat_id, _names(f.get(F_SKILLS)), _names(f.get(F_MCP)), _text(f.get(F_MCP_OVERRIDE))))
    return out


# ---------- 目录表（Skill库 / MCP库，从 hub 读出来填，供浏览）----------
SKILL_TBL = "Skill库"
MCP_TBL = "MCP库"


def _app_url() -> str:
    return f"{feishu.BASE}/bitable/v1/apps/{APP}"


def _tables() -> list[dict]:
    r = feishu._request("GET", f"{_app_url()}/tables?page_size=100",
                        headers={"Authorization": f"Bearer {feishu._token()}"})
    return (r.get("data") or {}).get("items", [])


def _table_id(name: str) -> str | None:
    for t in _tables():
        if t.get("name") == name:
            return t.get("table_id")
    return None


def _create_table(name: str) -> str:
    r = feishu._request("POST", f"{_app_url()}/tables", {"table": {"name": name}},
                        headers={"Authorization": f"Bearer {feishu._token()}"})
    return (r.get("data") or {}).get("table_id", "")


def _tapi(tid: str, method: str, path: str = "", payload: dict | None = None) -> dict:
    return feishu._request(method, f"{_app_url()}/tables/{tid}{path}", payload,
                           headers={"Authorization": f"Bearer {feishu._token()}"})


def _ensure_table(name: str, specs: list[tuple]) -> str:
    """确保表存在且有这些字段。specs=[(字段名, 类型), ...]，第一个作为主字段(文本)。"""
    tid = _table_id(name) or _create_table(name)
    flds = (_tapi(tid, "GET", "/fields").get("data") or {}).get("items", [])
    by = {f["field_name"]: f for f in flds}
    prim = next((f for f in flds if f.get("is_primary")), None)
    if prim and prim["field_name"] != specs[0][0]:
        _tapi(tid, "PUT", f"/fields/{prim['field_id']}", {"field_name": specs[0][0], "type": 1})
    for fname, ftype in specs[1:]:
        if fname not in by:
            _tapi(tid, "POST", "/fields", {"field_name": fname, "type": ftype})
    return tid


def _upsert(tid: str, key_field: str, key_val: str, fields: dict) -> None:
    recs = (_tapi(tid, "GET", "/records?page_size=200").get("data") or {}).get("items", [])
    rid = next((r["record_id"] for r in recs if _text((r.get("fields") or {}).get(key_field)) == key_val), None)
    if rid:
        _tapi(tid, "PUT", f"/records/{rid}", {"fields": fields})
    else:
        _tapi(tid, "POST", "/records", {"fields": fields})


def ensure_catalog() -> None:
    """从 hub 把 Skill库 / MCP库 两张目录表建好并填充（机器人写，供你浏览）。"""
    d = hub.defaults()
    stid = _ensure_table(SKILL_TBL, [("名称", 1), ("版本", 1), ("说明", 1), ("默认", 7), ("文件数", 2)])
    for n in hub.list_skills():
        m = hub.skill_meta(n)
        _upsert(stid, "名称", n, {"名称": n, "版本": m["version"], "说明": m["description"], "默认": n in d["skills"], "文件数": m["files"]})
    mtid = _ensure_table(MCP_TBL, [("名称", 1), ("Servers", 1), ("命令", 1), ("默认", 7)])
    for n in hub.list_mcp():
        m = hub.mcp_meta(n)
        _upsert(mtid, "名称", n, {"名称": n, "Servers": ", ".join(m["servers"]), "命令": m["command"], "默认": n in d["mcp"]})
