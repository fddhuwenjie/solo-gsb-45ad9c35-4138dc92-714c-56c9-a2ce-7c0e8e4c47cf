"""多约束明度求解器。

建模：
- 每个角色的色相 H、饱和度 S 固定，只有明度 L 是变量（整数 0..100）。
- 锁定角色的 L 固定为输入值。
- 每条前景-背景关系要求 WCAG 对比度 >= min_contrast，等价于
  (L_fg, L_bg) 取值必须落在「亮前景/暗背景」或「暗前景/亮背景」两块区域内。

求解：
- 用 101 位整数位集表示每个变量的取值域（位 v = 明度 v 仍可行）。
- AC-3 做初始弧相容；DFS 选变量（MRV：域最小 → 角色名靠前），
  取值按 |L-L0| 升序，前向检查传播，并以「各变量到 L0 最近距离之和」
  为下界做分支限界。
- 目标 min Σ|L-L0|；总代价相同时取角色名顺序下明度元组字典序最小者，
  因此结果与搜索过程无关、完全确定。
- 无解时用删除法求一个极小冲突关系集（MUS）。
"""
from __future__ import annotations

from collections import deque

from . import color

FULL_DOMAIN = (1 << 101) - 1
DEFAULT_NODE_BUDGET = 500_000
MUS_TRIAL_BUDGET = 60_000
MAX_CONFLICT_GROUPS = 5


def _nearest_set_bit(mask: int, target: int) -> int | None:
    """域中距离 target 明度最近的可行值；代价相同时取较小者。"""
    if mask == 0:
        return None
    low_part = mask & ((1 << (target + 1)) - 1)      # <= target 的最高位
    high_part = mask & ~((1 << (target + 1)) - 1) & FULL_DOMAIN  # > target 的最低位
    low = low_part.bit_length() - 1 if low_part else None
    high = (high_part & -high_part).bit_length() - 1 if high_part else None
    if low is None:
        return high
    if high is None:
        return low
    return low if target - low <= high - target else high


class _Constraint:
    __slots__ = ("fg", "bg", "min_contrast", "rel_index", "fg_allowed", "bg_allowed")

    def __init__(self, fg: int, bg: int, min_contrast: float, rel_index: int):
        self.fg = fg
        self.bg = bg
        self.min_contrast = min_contrast
        self.rel_index = rel_index
        # fg_allowed[i]: 前景明度为 i 时，哪些背景明度可行（位集）
        self.fg_allowed: list[int] = [0] * 101
        self.bg_allowed: list[int] = [0] * 101


def _build_constraint(fg: int, bg: int, min_contrast: float,
                      lum: list[list[float]], rel_index: int) -> _Constraint:
    c = _Constraint(fg, bg, min_contrast, rel_index)
    lf, lb = lum[fg], lum[bg]
    for i in range(101):
        for j in range(101):
            la, lbv = lf[i], lb[j]
            lighter, darker = (la, lbv) if la >= lbv else (lbv, la)
            ratio = (lighter + 0.05) / (darker + 0.05)
            if ratio + 1e-9 >= min_contrast:
                c.fg_allowed[i] |= 1 << j
                c.bg_allowed[j] |= 1 << i
    return c


def _solve_core(roles_sorted: list[dict], relations: list[dict],
                lum: list[list[float]], node_budget: int) -> dict:
    """核心搜索。roles_sorted 已按名称排序；relations 的 index 为原始序号。

    返回 status: optimal / infeasible / exhausted。
    """
    n = len(roles_sorted)
    l0 = [color.hex_to_hsl(r["hex"])[2] for r in roles_sorted]

    name_to_pos = {r["name"]: k for k, r in enumerate(roles_sorted)}
    constraints: list[_Constraint] = []
    for rel in relations:
        fg = name_to_pos[rel["fg"]]
        bg = name_to_pos[rel["bg"]]
        constraints.append(
            _build_constraint(fg, bg, rel["min_contrast"], lum, rel.get("index", 0)))

    domains = [FULL_DOMAIN] * n
    for k, r in enumerate(roles_sorted):
        if r["locked"]:
            domains[k] = 1 << l0[k]

    # 邻居表：(邻居位置, 本端 0=fg/1=bg, 约束)
    neighbors: list[list[tuple[int, int, _Constraint]]] = [[] for _ in range(n)]
    for c in constraints:
        neighbors[c.fg].append((c.bg, 0, c))
        neighbors[c.bg].append((c.fg, 1, c))

    # 自引用关系：同一角色既当前景又当背景时对比度恒为 1:1，
    # 不能作为二元 CSP 处理（会被当成两个独立变量），需单独判定。
    reasons: dict[int, str] = {}
    self_conflict = False
    for c in constraints:
        if c.fg == c.bg and c.min_contrast > 1.0 + 1e-9:
            self_conflict = True
            reasons[c.rel_index] = (
                f"「{roles_sorted[c.fg]['name']}」同时作为前景与背景，"
                f"自身对比度恒为 1.00:1，无法达到 ≥ {c.min_contrast:g}:1"
            )
    if self_conflict:
        return {"status": "infeasible", "reasons": reasons}

    # 锁定-锁定关系一开始就不满足 → 记录明确原因
    for c in constraints:
        if domains[c.fg].bit_count() == 1 and domains[c.bg].bit_count() == 1:
            lf = domains[c.fg].bit_length() - 1
            lb = domains[c.bg].bit_length() - 1
            la, lbv = lum[c.fg][lf], lum[c.bg][lb]
            lighter, darker = (la, lbv) if la >= lbv else (lbv, la)
            ratio = (lighter + 0.05) / (darker + 0.05)
            if ratio + 1e-9 < c.min_contrast:
                reasons[c.rel_index] = (
                    f"「{roles_sorted[c.fg]['name']}」与「{roles_sorted[c.bg]['name']}」"
                    f"均已锁定，对比度仅 {ratio:.2f}:1，而要求 ≥ {c.min_contrast:g}:1"
                )

    # AC-3 初始弧相容
    queue: deque[tuple[int, int, _Constraint, int]] = deque()
    for c in constraints:
        queue.append((c.fg, c.bg, c, 0))
        queue.append((c.bg, c.fg, c, 1))
    while queue:
        x, y, c, side = queue.popleft()
        allowed = c.fg_allowed if side == 0 else c.bg_allowed
        supported = 0
        bits = domains[x]
        while bits:
            bit = bits & -bits
            v = bit.bit_length() - 1
            if allowed[v] & domains[y]:
                supported |= bit
            bits -= bit
        if supported != domains[x]:
            if supported == 0:
                return {"status": "infeasible", "reasons": reasons}
            domains[x] = supported
            for z, z_side, zc in neighbors[x]:
                if not (z == y and zc is c):
                    queue.append((z, x, zc, z_side))

    # 每个变量按距 L0 的代价排序的静态取值顺序（代价相同取较小明度）
    value_order = [sorted(range(101), key=lambda v: (abs(v - l0[k]), v)) for k in range(n)]

    best_cost: int | None = None
    best_tuple: tuple[int, ...] | None = None
    nodes = 0
    budget_hit = False

    def min_cost(mask: int, target: int) -> int:
        b = _nearest_set_bit(mask, target)
        return abs(b - target) if b is not None else 10**9

    def dfs(doms: list[int], assign: list[int | None], paid: int) -> None:
        nonlocal best_cost, best_tuple, nodes, budget_hit
        nodes += 1
        if nodes > node_budget:
            budget_hit = True
            return

        unassigned = [k for k in range(n) if assign[k] is None]
        if not unassigned:
            chosen = tuple(assign)  # type: ignore[arg-type]
            if best_cost is None or (paid, chosen) < (best_cost, best_tuple):
                best_cost, best_tuple = paid, chosen
            return

        # 可采纳下界：已付代价 + 每个未分配变量取其当前域内最近明度
        lb = paid + sum(min_cost(doms[k], l0[k]) for k in unassigned)
        if best_cost is not None and lb > best_cost:
            return

        # MRV：域最小，平局角色名靠前（k 即名称排序后的位置）
        x = min(unassigned, key=lambda k: (doms[k].bit_count(), k))
        for v in value_order[x]:
            if not ((doms[x] >> v) & 1):
                continue
            if nodes > node_budget:
                budget_hit = True
                return

            new_doms = doms.copy()
            new_assign = assign.copy()
            new_assign[x] = v
            new_doms[x] = 1 << v

            ok = True
            for y, side, c in neighbors[x]:
                if new_assign[y] is not None:
                    continue
                allowed = c.fg_allowed[v] if side == 0 else c.bg_allowed[v]
                restricted = new_doms[y] & allowed
                if restricted == 0:
                    ok = False
                    break
                new_doms[y] = restricted
            if ok:
                dfs(new_doms, new_assign, paid + abs(v - l0[x]))

    dfs(domains, [None] * n, 0)

    if best_tuple is None:
        return {"status": "exhausted" if budget_hit else "infeasible", "reasons": reasons}

    return {
        "status": "optimal",
        "nodes": nodes,
        "proven_optimal": not budget_hit,
        "lightness_tuple": best_tuple,
        "total_delta": best_cost,
        "l0": l0,
    }


def solve(roles: list[dict], relations: list[dict],
          node_budget: int = DEFAULT_NODE_BUDGET) -> dict:
    """求解入口（roles 已通过 API 层校验）。

    roles: [{"name", "hex", "locked"}]
    relations: [{"fg", "bg", "min_contrast", "index"}]
    """
    order = sorted(range(len(roles)), key=lambda i: roles[i]["name"])
    roles_sorted = [roles[i] for i in order]
    names = [r["name"] for r in roles_sorted]

    hsl0 = [color.hex_to_hsl(r["hex"]) for r in roles_sorted]
    h0 = [t[0] for t in hsl0]
    s0 = [t[1] for t in hsl0]
    lum = [[color.relative_luminance(color.hex_to_rgb(
        color.hsl_to_hex(h0[k], s0[k], v))) for v in range(101)] for k in range(len(roles))]

    result = _solve_core(roles_sorted, relations, lum, node_budget)

    if result["status"] == "infeasible":
        return _diagnose(roles_sorted, relations, lum, result.get("reasons", {}))
    if result["status"] == "exhausted":
        return {"status": "exhausted"}

    best_tuple = result["lightness_tuple"]
    lightness = {names[k]: best_tuple[k] for k in range(len(names))}
    new_hex, old_hex = {}, {}
    for k, r in enumerate(roles_sorted):
        old_hex[r["name"]] = r["hex"]
        # 明度未移动时保留原 HEX，避免 H/S 整数化重建产生的微小漂移
        new_hex[r["name"]] = (
            r["hex"] if (r["locked"] or best_tuple[k] == result["l0"][k])
            else color.hsl_to_hex(h0[k], s0[k], best_tuple[k])
        )

    per_role = []
    for k, r in enumerate(roles_sorted):
        per_role.append({
            "name": r["name"],
            "locked": bool(r["locked"]),
            "old_hex": r["hex"],
            "new_hex": new_hex[r["name"]],
            "h": h0[k],
            "s": s0[k],
            "l_before": result["l0"][k],
            "l_after": best_tuple[k],
            "delta": best_tuple[k] - result["l0"][k],
        })

    contrast_before, contrast_after, per_rel = {}, {}, []
    for rel in relations:
        before = color.contrast_of_hex(old_hex[rel["fg"]], old_hex[rel["bg"]])
        after = color.contrast_of_hex(new_hex[rel["fg"]], new_hex[rel["bg"]])
        contrast_before[rel["index"]] = before
        contrast_after[rel["index"]] = after
        per_rel.append({
            "index": rel["index"],
            "fg": rel["fg"],
            "bg": rel["bg"],
            "min_contrast": rel["min_contrast"],
            "before": before,
            "after": after,
            "passes": after + 1e-9 >= rel["min_contrast"],
        })

    return {
        "status": "optimal",
        "nodes": result["nodes"],
        "proven_optimal": result["proven_optimal"],
        "total_delta": result["total_delta"],
        "ordered_names": names,
        "lightness": lightness,
        "per_role": per_role,
        "per_relation": per_rel,
        "new_hex": new_hex,
        "old_hex": old_hex,
        "contrast_after": contrast_after,
        "contrast_before": contrast_before,
    }


def _diagnose(roles_sorted, relations, lum, reasons) -> dict:
    """求极小冲突关系集（MUS），并尽量列出相互独立的冲突簇。

    - 默认贪心删除得到一个极小不可满足子集；
    - 对每个预先识别出的「锁定-锁定」死结（reasons），若未被已有冲突组
      覆盖，则强制保留该关系再做一次贪心删除，得到包含它的冲突簇。
    """
    by_index = {rel["index"]: rel for rel in relations}

    def feasible(subset: list[dict]) -> bool:
        res = _solve_core(roles_sorted, subset, lum, MUS_TRIAL_BUDGET)
        # 只有确认无解才算「删后仍无解」；预算耗尽时保守视为可行（保留该约束），
        # 这样报告的冲突集可能是极小集的超集，但必定真实不可满足。
        return res["status"] != "infeasible"

    def greedy_reduce(forced: int | None) -> tuple[int, ...]:
        """贪心删除；forced 指定的关系永不尝试删除。"""
        kept = sorted(relations, key=lambda rel: rel["index"] == forced)
        i = 0
        while i < len(kept):
            rel = kept[i]
            if rel["index"] == forced:
                i += 1
                continue
            trial = kept[:i] + kept[i + 1:]
            if feasible(trial):
                i += 1  # 删了它就有解 → 冲突必要成员，保留
            else:
                kept.pop(i)  # 删了仍无解 → 与冲突无关，移除
        return tuple(sorted(rel["index"] for rel in kept))

    groups: list[tuple[int, ...]] = []

    def add_group(indices: tuple[int, ...]) -> None:
        if indices and indices not in groups:
            groups.append(indices)

    add_group(greedy_reduce(None))
    covered = set(groups[0])
    for seed in sorted(reasons):
        if len(groups) >= MAX_CONFLICT_GROUPS or seed in covered or seed not in by_index:
            continue
        group = greedy_reduce(seed)
        if seed in group:
            add_group(group)
            covered.update(group)

    result_groups = []
    all_reasons: dict[str, str] = {}
    for indices in groups:
        group_reasons = {str(k): v for k, v in reasons.items() if k in indices}
        all_reasons.update(group_reasons)
        result_groups.append({
            "indices": list(indices),
            "relations": [by_index[i] for i in indices],
            "reasons": group_reasons,
        })

    first = groups[0]
    return {
        "status": "infeasible",
        "conflict_indices": list(first),
        "conflicts": [by_index[i] for i in first],
        "groups": result_groups,
        "reasons": all_reasons,
    }
