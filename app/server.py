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


# ---------------------------------------------------------------- 结果绑定校验

class ResultStaleError(Exception):
    """客户端带来的 result 与其 payload 不配对（被篡改、无解或未完成）。

    与 ValueError（入参格式错误，400）区分：这类请求的配置本身合法，
    只是结果已过期，返回 409 要求前端重新求解。
    """

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _resolve_bound_result(payload, result):
    """校验 result 与 payload 配对，返回 (规范化 payload, 权威 result)。

    版本保存与 CSS 导出都必须经过这里：服务端从不信任客户端带来的
    result，而是按 payload 重新求解，再核对客户端结果与权威结果一致。
    任何不一致（编辑后未重解）、无解或未完成（预算耗尽）的结果都被拒绝。
    返回的 payload 经过规范化（HEX 小写化、名称去空白、bool 锁定等），
    保证 SQLite 快照、预览与 CSS 始终来自同一份配置。
    """
    try:
        roles, relations = _validate_payload(payload or {})
    except ValueError:
        raise ResultStaleError("配置已变化，请重新求解后再保存或导出", 400)

    canonical = solver.solve(roles, relations)
    if canonical["status"] != "optimal":
        if canonical["status"] == "exhausted":
            raise ResultStaleError("求解尚未完成（搜索预算耗尽），请调整后重新求解", 409)
        raise ResultStaleError("当前配置无可行解，请调整后重新求解", 409)

    if not isinstance(result, dict) or result.get("status") != "optimal":
        raise ResultStaleError("请先完成一次可行求解后再保存或导出", 400)

    mismatch = _result_mismatch(result, canonical)
    if mismatch:
        raise ResultStaleError(
            f"求解结果与当前配置不一致（{mismatch}），请重新求解后再保存或导出", 409)

    canonical_payload = {
        "roles": roles,
        "relations": [
            {"fg": rel["fg"], "bg": rel["bg"], "min_contrast": rel["min_contrast"]}
            for rel in relations
        ],
    }
    return canonical_payload, canonical


def _result_mismatch(result: dict, canonical: dict) -> str | None:
    """结构比对客户端 result 与服务端重算结果；不一致时返回原因。"""
    if result.get("total_delta") != canonical["total_delta"]:
        return "总明度改变量不同"

    cli_roles = result.get("per_role")
    if not isinstance(cli_roles, list) or len(cli_roles) != len(canonical["per_role"]):
        return "角色集合不同"
    for got, want in zip(cli_roles, canonical["per_role"]):
        if not isinstance(got, dict) or got.get("name") != want["name"]:
            return "角色集合不同"
        for key in ("l_after", "delta", "new_hex", "old_hex", "locked"):
            if got.get(key) != want[key]:
                return f"角色「{want['name']}」的结果已过期"

    cli_rels = result.get("per_relation")
    if not isinstance(cli_rels, list) or len(cli_rels) != len(canonical["per_relation"]):
        return "关系集合不同"
    for got, want in zip(cli_rels, canonical["per_relation"]):
        if not isinstance(got, dict):
            return "关系集合不同"
        if (got.get("fg") != want["fg"] or got.get("bg") != want["bg"]
                or got.get("min_contrast") != want["min_contrast"]):
            return "前景—背景关系已变化"
        try:
            if abs(float(got.get("after")) - float(want["after"])) > 1e-6:
                return f"关系「{want['fg']} on {want['bg']}」的对比度已过期"
        except (TypeError, ValueError):
            return f"关系「{want['fg']} on {want['bg']}」的对比度格式有误"
    return None


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
        # 重算并绑定校验：快照里的 result 必须由同一份 payload 推出
        canonical_payload, canonical = _resolve_bound_result(payload, result)
    except ResultStaleError as exc:
        return _error(str(exc), exc.status)
    return jsonify(storage.create_version(label, canonical_payload, canonical)), 201


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
    """由服务端重算确认过的求解结果生成 CSS 变量文本。"""
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
    """根据与 payload 绑定校验的求解结果导出 CSS 变量（text/css）。"""
    body = request.get_json(force=True) or {}
    try:
        _canonical_payload, canonical = _resolve_bound_result(body.get("payload"), body.get("result"))
    except ResultStaleError as exc:
        return _error(str(exc), exc.status)

    css = _render_css(canonical)
    return css, 200, {"Content-Type": "text/css; charset=utf-8"}


if __name__ == "__main__":
    storage.init_db()
    app.run(host="0.0.0.0", port=8000, debug=False)
