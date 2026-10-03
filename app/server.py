"""Flask 入口：校验、求解、配置/版本/导出接口。"""
from __future__ import annotations

import re

from flask import Flask, jsonify, request, send_from_directory

from . import color, solver, storage

app = Flask(__name__, static_folder="static")

MAX_ROLES = 14
MIN_CONTRAST_RANGE = (1.0, 21.0)


# ---------------------------------------------------------------- 校验

def _validate_payload(payload: dict) -> tuple[list[dict], list[dict]]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    raw_roles = payload.get("roles")
    raw_rels = payload.get("relations")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise ValueError("至少需要一个语义角色")
    if len(raw_roles) > MAX_ROLES:
        raise ValueError(f"角色数量不能超过 {MAX_ROLES} 个（搜索空间考虑）")
    if not isinstance(raw_rels, list):
        raise ValueError("relations 必须是数组")

    roles: list[dict] = []
    seen: set[str] = set()
    for i, r in enumerate(raw_roles):
        if not isinstance(r, dict):
            raise ValueError(f"第 {i + 1} 个角色格式有误")
        name = str(r.get("name", "")).strip()
        if not name:
            raise ValueError(f"第 {i + 1} 个角色缺少名称")
        if name in seen:
            raise ValueError(f"角色名「{name}」重复")
        seen.add(name)
        try:
            hex_color = color.normalize_hex(str(r.get("hex", "")))
        except ValueError as exc:
            raise ValueError(f"角色「{name}」：{exc}") from exc
        roles.append({"name": name, "hex": hex_color, "locked": bool(r.get("locked"))})

    relations: list[dict] = []
    for i, rel in enumerate(raw_rels):
        if not isinstance(rel, dict):
            raise ValueError(f"第 {i + 1} 条关系格式有误")
        fg = str(rel.get("fg", "")).strip()
        bg = str(rel.get("bg", "")).strip()
        if fg not in seen:
            raise ValueError(f"第 {i + 1} 条关系引用了不存在的前景角色「{fg}」")
        if bg not in seen:
            raise ValueError(f"第 {i + 1} 条关系引用了不存在的背景角色「{bg}」")
        try:
            mc = float(rel.get("min_contrast"))
        except (TypeError, ValueError):
            raise ValueError(f"第 {i + 1} 条关系的最低对比度不是数字")
        if not (MIN_CONTRAST_RANGE[0] <= mc <= MIN_CONTRAST_RANGE[1]):
            raise ValueError(
                f"第 {i + 1} 条关系的最低对比度需在 "
                f"{MIN_CONTRAST_RANGE[0]:g}~{MIN_CONTRAST_RANGE[1]:g} 之间")
        relations.append({"index": i, "fg": fg, "bg": bg, "min_contrast": mc})

    return roles, relations


def _error(message: str, status: int = 400):
    return jsonify({"error": message}), status


# ---------------------------------------------------------------- 页面

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- API

@app.get("/api/config")
def api_get_config():
    return jsonify(storage.get_config())


@app.post("/api/config")
def api_save_config():
    payload = request.get_json(force=True) or {}
    try:
        _validate_payload(payload)
    except ValueError as exc:
        return _error(str(exc))
    meta = storage.save_config(payload)
    return jsonify({"ok": True, **meta})


@app.post("/api/solve")
def api_solve():
    try:
        roles, relations = _validate_payload(request.get_json(force=True) or {})
    except ValueError as exc:
        return _error(str(exc))
    return jsonify(solver.solve(roles, relations))


@app.get("/api/versions")
def api_list_versions():
    return jsonify(storage.list_versions())


@app.post("/api/versions")
def api_create_version():
    body = request.get_json(force=True) or {}
    payload = body.get("payload")
    result = body.get("result")
    label = str(body.get("label", "")).strip() or "未命名版本"
    try:
        _validate_payload(payload or {})
    except ValueError as exc:
        return _error(str(exc))
    return jsonify(storage.create_version(label, payload, result)), 201


@app.get("/api/versions/<vid>")
def api_get_version(vid: str):
    version = storage.get_version(vid)
    if version is None:
        return _error("版本不存在", 404)
    return jsonify(version)


@app.delete("/api/versions/<vid>")
def api_delete_version(vid: str):
    if not storage.delete_version(vid):
        return _error("版本不存在", 404)
    return jsonify({"ok": True})


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _css_var_name(name: str, used: set[str], fallback_index: int) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug or slug[0].isdigit():
        slug = f"color-{slug}" if slug else f"color-{fallback_index}"
    candidate = f"--color-{slug}"
    n = 2
    base = candidate
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    used.add(candidate)
    return candidate


@app.post("/api/export")
def api_export():
    """根据求解结果导出 CSS 变量（text/css）。"""
    body = request.get_json(force=True) or {}
    result = body.get("result")
    if not isinstance(result, dict) or result.get("status") != "optimal":
        return _error("请先完成一次可行求解后再导出", 400)

    per_role = result.get("per_role", [])
    used: set[str] = set()
    var_names = {r["name"]: _css_var_name(r["name"], used, i + 1)
                 for i, r in enumerate(per_role)}

    lines = [
        "/* 多约束无障碍调色台导出 */",
        f"/* 总明度改变量 ΣΔL = {result.get('total_delta', 0)} */",
        ":root {",
    ]
    for r in per_role:
        lock = " (锁定)" if r.get("locked") else ""
        lines.append(
            f"  {var_names[r['name']]}: {r['new_hex']};  "
            f"/* {r['name']}{lock}  L {r['l_before']}→{r['l_after']} "
            f"({'+' if r['delta'] >= 0 else ''}{r['delta']}) */"
        )
    lines.append("}")

    per_rel = result.get("per_relation", [])
    if per_rel:
        lines.extend(["", "/* 对比度校验（前景 / 背景） */", ""])
        for rel in per_rel:
            lines.append(
                f"/* {rel['fg']} on {rel['bg']}: "
                f"{rel['after']:.2f}:1 (要求 ≥ {rel['min_contrast']:g}:1) */"
            )

    css = "\n".join(lines) + "\n"
    return css, 200, {"Content-Type": "text/css; charset=utf-8"}


if __name__ == "__main__":
    storage.init_db()
    app.run(host="0.0.0.0", port=8000, debug=False)
