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


# ------------------------------------------------- 结果与配置的绑定校验

def _canonical_solve(roles: list[dict], relations: list[dict]) -> dict:
    """对配置做一次权威求解（搜索预算与线上一致）。"""
    return solver.solve(roles, relations)


def _reconcile_result(payload: dict, supplied: object) -> dict:
    """校验客户端带来的 result 与 payload 绑定且为已完成的可行解。

    服务端永远不信任客户端 result：对 payload 重新求解，只在 supplied 与
    权威结果的关键绑定字段（状态、每角色明度/新 HEX、每关系对比度）一致时
    接受，并返回服务端重算的权威结果用于落库与导出；不一致、无解或未完成
    （infeasible / exhausted / 预算耗尽未证明最优）一律拒绝。
    """
    if not isinstance(supplied, dict) or supplied.get("status") != "optimal":
        raise ValueError("请先完成一次可行求解后再操作（结果缺失、无解或未完成）")

    roles, relations = _validate_payload(payload)
    canonical = _canonical_solve(roles, relations)
    if canonical.get("status") != "optimal":
        raise ValueError("当前配置不存在满足全部约束的可行解，请重新求解")

    def mismatch(detail: str):
        raise ValueError(f"结果与当前配置不一致（{detail}），请重新求解后再操作")

    if supplied.get("total_delta") != canonical["total_delta"]:
        mismatch("总明度改变量")

    light = supplied.get("lightness")
    if not isinstance(light, dict) or set(light) != set(canonical["lightness"]):
        mismatch("角色集合")
    for name, value in canonical["lightness"].items():
        if light.get(name) != value:
            mismatch(f"角色「{name}」的明度")

    new_hex = supplied.get("new_hex")
    if not isinstance(new_hex, dict) or set(new_hex) != set(canonical["new_hex"]):
        mismatch("角色集合")
    for name, value in canonical["new_hex"].items():
        if str(new_hex.get(name, "")).lower() != value:
            mismatch(f"角色「{name}」的颜色")

    # 锁定状态属于配置的一部分：result 里每个角色的 locked 也必须与 payload 一致
    supplied_per_role = supplied.get("per_role")
    if not isinstance(supplied_per_role, list) \
            or len(supplied_per_role) != len(canonical["per_role"]):
        mismatch("角色数量")
    canonical_role = {r["name"]: r for r in canonical["per_role"]}
    for r in supplied_per_role:
        if not isinstance(r, dict) or r.get("name") not in canonical_role:
            mismatch("角色结果")
        if bool(r.get("locked")) != bool(canonical_role[r["name"]]["locked"]):
            mismatch(f"角色「{r['name']}」的锁定状态")

    supplied_rels = supplied.get("per_relation")
    if not isinstance(supplied_rels, list) or len(supplied_rels) != len(relations):
        mismatch("关系数量")
    canonical_by_index = {r["index"]: r for r in canonical["per_relation"]}
    for pos, rel in enumerate(supplied_rels):
        if not isinstance(rel, dict):
            mismatch("关系结果格式")
        can = canonical_by_index.get(pos)
        if can is None:
            mismatch("关系索引")
        if str(rel.get("fg", "")).strip() != can["fg"] or \
                str(rel.get("bg", "")).strip() != can["bg"]:
            mismatch(f"第 {pos + 1} 条关系的角色引用")
        try:
            mc = float(rel.get("min_contrast"))
        except (TypeError, ValueError):
            mismatch(f"第 {pos + 1} 条关系的门槛")
        if abs(mc - can["min_contrast"]) > 1e-9:
            mismatch(f"第 {pos + 1} 条关系的对比度门槛")
        try:
            after = float(rel.get("after"))
        except (TypeError, ValueError):
            mismatch(f"第 {pos + 1} 条关系的对比度")
        if abs(after - can["after"]) > 1e-6 or after + 1e-9 < can["min_contrast"]:
            mismatch(f"第 {pos + 1} 条关系不满足对比度门槛")

    # 落库 / 导出永远使用服务端重算的权威结果
    return canonical


def _payload_from_result(result: dict) -> dict:
    """旧接口（只带 result）的兜底路径：从最优结果重建配置再绑定校验。

    重建出的配置重算后必须仍能得到同一结果，因此伪造或过期的 result
    依旧无法通过校验。
    """
    per_role = result.get("per_role")
    per_rel = result.get("per_relation")
    if not isinstance(per_role, list) or not isinstance(per_rel, list):
        raise ValueError("结果结构不完整，请重新求解")
    roles: list[dict] = []
    for r in per_role:
        if not isinstance(r, dict) or not isinstance(r.get("name"), str) \
                or not isinstance(r.get("old_hex"), str):
            raise ValueError("结果结构不完整，请重新求解")
        roles.append({
            "name": r["name"],
            "hex": r["old_hex"],
            "locked": bool(r.get("locked")),
        })
    relations = []
    for rel in per_rel:
        if not isinstance(rel, dict):
            raise ValueError("结果结构不完整，请重新求解")
        relations.append({
            "fg": rel.get("fg"),
            "bg": rel.get("bg"),
            "min_contrast": rel.get("min_contrast"),
        })
    return {"roles": roles, "relations": relations}


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
        if not isinstance(payload, dict):
            raise ValueError("缺少与结果绑定的配置 payload")
        # 不接受客户端旧 result：重算绑定校验，落库的 payload/result 来自同一份配置
        canonical = _reconcile_result(payload, result)
        roles, relations = _validate_payload(payload)
        clean_payload = {"roles": roles, "relations": [
            {"fg": r["fg"], "bg": r["bg"], "min_contrast": r["min_contrast"]}
            for r in relations
        ]}
    except ValueError as exc:
        return _error(str(exc))
    return jsonify(storage.create_version(label, clean_payload, canonical)), 201


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


def _render_css(result: dict) -> str:
    """由权威最优结果渲染 CSS 变量文本。"""
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

    return "\n".join(lines) + "\n"


@app.post("/api/export")
def api_export():
    """根据与配置绑定的求解结果导出 CSS 变量（text/css）。

    请求体可同时携带 {payload, result}（推荐，与存为版本同一份 payload）；
    为兼容旧用法只带 result 时，从结果重建配置。两条路径都要先重算并做
    绑定校验，保证导出的 CSS 与配置一致、可行且已完成求解。
    """
    body = request.get_json(force=True) or {}
    result = body.get("result")
    payload = body.get("payload")
    try:
        if not isinstance(result, dict) or result.get("status") != "optimal":
            raise ValueError("请先完成一次可行求解后再导出")
        if not isinstance(payload, dict):
            payload = _payload_from_result(result)
        canonical = _reconcile_result(payload, result)
    except ValueError as exc:
        return _error(str(exc), 400)

    css = _render_css(canonical)
    return css, 200, {"Content-Type": "text/css; charset=utf-8"}


if __name__ == "__main__":
    storage.init_db()
    app.run(host="0.0.0.0", port=8000, debug=False)
