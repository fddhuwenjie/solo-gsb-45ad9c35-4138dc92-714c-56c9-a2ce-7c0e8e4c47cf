"""服务端回归测试：结果与配置的绑定校验、版本快照、CSS 导出一致性。

覆盖连续编辑后旧 result 必须被拒绝、版本保存/恢复成对生效、
SQLite 快照与导出始终来自同一份 payload。

运行（仓库根目录）：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from app import color, server, solver, storage


# 一个可行、求解很快的小调色板
def base_payload() -> dict:
    return {
        "roles": [
            {"name": "bg", "hex": "#ffffff", "locked": True},
            {"name": "text", "hex": "#777777", "locked": False},
            {"name": "primary", "hex": "#2563eb", "locked": False},
        ],
        "relations": [
            {"fg": "text", "bg": "bg", "min_contrast": 4.5},
            {"fg": "primary", "bg": "bg", "min_contrast": 4.5},
        ],
    }


def infeasible_payload() -> dict:
    return {
        "roles": [
            {"name": "bg", "hex": "#ffffff", "locked": True},
            {"name": "text", "hex": "#888888", "locked": True},
        ],
        "relations": [
            {"fg": "text", "bg": "bg", "min_contrast": 4.5},
        ],
    }


class PaletteServerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        storage.DB_PATH = os.path.join(self._tmp.name, "palette.db")
        storage.init_db()
        server.app.testing = True
        self.client = server.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    # -------------------------------------------------- helpers

    def solve(self, payload: dict | None = None) -> dict:
        res = self.client.post("/api/solve", json=payload or base_payload())
        self.assertEqual(res.status_code, 200, res.data)
        data = res.get_json()
        self.assertEqual(data["status"], "optimal")
        return data

    def create_version(self, payload: dict, result: dict, label: str = "v1"):
        return self.client.post(
            "/api/versions", json={"label": label, "payload": payload, "result": result})

    def export(self, payload: dict, result: dict):
        return self.client.post("/api/export", json={"payload": payload, "result": result})

    def list_version_ids(self) -> list[str]:
        return [v["id"] for v in self.client.get("/api/versions").get_json()]


class SolveTest(PaletteServerTestBase):
    def test_solve_baseline_optimal_and_deterministic(self):
        p = base_payload()
        r1 = self.solve(p)
        r2 = self.solve(p)
        self.assertEqual(r1["total_delta"], r2["total_delta"])
        self.assertEqual(r1["new_hex"], r2["new_hex"])
        self.assertTrue(all(rel["passes"] for rel in r1["per_relation"]))

    def test_infeasible_is_reported_not_optimal(self):
        res = self.client.post("/api/solve", json=infeasible_payload())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "infeasible")


class VersionBindingTest(PaletteServerTestBase):
    # -------------------------------------------------- 正常保存/恢复

    def test_save_version_with_bound_pair_succeeds_and_persists_canonical(self):
        p = base_payload()
        result = self.solve(p)
        res = self.create_version(p, result, "good")
        self.assertEqual(res.status_code, 201, res.data)
        vid = res.get_json()["id"]

        got = self.client.get(f"/api/versions/{vid}")
        self.assertEqual(got.status_code, 200)
        version = got.get_json()
        # 配置与结果成对返回
        self.assertEqual(version["payload"]["roles"], p["roles"])
        self.assertEqual(version["result"]["status"], "optimal")

        # SQLite 中存的是服务端重算的权威结果，而非客户端原对象
        canonical = solver.solve(
            *server._validate_payload(p))
        self.assertEqual(version["result"]["new_hex"], canonical["new_hex"])
        self.assertEqual(version["result"]["total_delta"], canonical["total_delta"])
        self.assertEqual(
            [r["l_after"] for r in version["result"]["per_role"]],
            [r["l_after"] for r in canonical["per_role"]],
        )

    def test_restore_pair_reproduces_result_without_editing(self):
        """恢复版本时配置与结果必须成对：用快照 payload 重解应得到同一结果。"""
        p = base_payload()
        result = self.solve(p)
        vid = self.create_version(p, result).get_json()["id"]

        version = self.client.get(f"/api/versions/{vid}").get_json()
        resolved = self.solve(version["payload"])
        self.assertEqual(resolved["new_hex"], version["result"]["new_hex"])
        self.assertEqual(resolved["total_delta"], version["result"]["total_delta"])
        for want, got in zip(version["result"]["per_relation"], resolved["per_relation"]):
            self.assertAlmostEqual(want["after"], got["after"], places=6)

    # -------------------------------------------------- 连续编辑后旧 result 一律拒绝

    def _mutations(self):
        def change_color(p):
            p["roles"][1]["hex"] = "#123456"

        def change_name(p):
            p["roles"][1]["name"] = "body-text"
            p["relations"][0]["fg"] = "body-text"

        def lock_role(p):
            p["roles"][1]["locked"] = True

        def add_role(p):
            p["roles"].append({"name": "extra", "hex": "#abcdef", "locked": False})

        def delete_role(p):
            p["roles"].pop(1)
            p["relations"].pop(0)

        def change_fg(p):
            p["relations"][0]["fg"] = "primary"

        def change_bg(p):
            p["relations"][0]["bg"] = "primary"

        def raise_threshold(p):
            p["relations"][0]["min_contrast"] = 21

        def delete_relation(p):
            p["relations"].pop(0)

        def add_relation(p):
            p["relations"].append(
                {"fg": "text", "bg": "primary", "min_contrast": 3})

        return [
            ("改颜色", change_color),
            ("改名称", change_name),
            ("改锁定", lock_role),
            ("增角色", add_role),
            ("删角色", delete_role),
            ("改前景", change_fg),
            ("改背景", change_bg),
            ("抬门槛", raise_threshold),
            ("删关系", delete_relation),
            ("增关系", add_relation),
        ]

    def test_stale_result_rejected_after_every_kind_of_edit(self):
        original = base_payload()
        result = self.solve(original)

        for label, mutate in self._mutations():
            with self.subTest(edit=label):
                edited = base_payload()
                mutate(edited)
                # 客户端拿着旧 result 去保存当前（已编辑）配置 → 409
                res = self.create_version(edited, result)
                self.assertEqual(res.status_code, 409, f"{label}: {res.data}")
                self.assertIn("重新求解", res.get_json()["error"])
                # 拒绝时不得落库
                self.assertEqual(self.list_version_ids(), [])

                # 同样必须拒绝 CSS 导出
                exp = self.export(edited, result)
                self.assertEqual(exp.status_code, 409, f"{label}: {exp.data}")
                self.assertNotIn("--color-", exp.get_data(as_text=True))

                # 重新求解后：可行即可保存；若编辑后本身无解（如锁定一个
                # 对比度不足的角色），保存仍被拒绝，两种情况下旧结果都不能复活
                fresh_res = self.client.post("/api/solve", json=edited)
                self.assertEqual(fresh_res.status_code, 200)
                fresh = fresh_res.get_json()
                if fresh["status"] == "optimal":
                    ok = self.create_version(edited, fresh, label)
                    self.assertEqual(ok.status_code, 201, f"{label}: {ok.data}")
                else:
                    self.assertEqual(fresh["status"], "infeasible")
                    blocked = self.create_version(edited, fresh)
                    self.assertEqual(blocked.status_code, 409)
                # 清理，保证每个子用例的版本表为空
                for vid in self.list_version_ids():
                    self.client.delete(f"/api/versions/{vid}")

    # -------------------------------------------------- 无解 / 未完成 / 缺结果

    def test_infeasible_payload_rejected_even_with_optimal_looking_result(self):
        bad = infeasible_payload()
        fake = self.solve(base_payload())  # 一份别处的可行结果
        res = self.create_version(bad, fake)
        self.assertEqual(res.status_code, 409)
        self.assertIn("无可行解", res.get_json()["error"])
        self.assertEqual(self.list_version_ids(), [])

        exp = self.export(bad, fake)
        self.assertEqual(exp.status_code, 409)

    def test_non_optimal_result_rejected(self):
        p = base_payload()
        for stale in [None, {}, {"status": "infeasible"}, {"status": "exhausted"}]:
            with self.subTest(result=stale):
                res = self.create_version(p, stale)
                self.assertEqual(res.status_code, 400)
                self.assertEqual(self.list_version_ids(), [])

    def test_missing_payload_rejected(self):
        result = self.solve(base_payload())
        res = self.client.post("/api/versions", json={"result": result})
        self.assertEqual(res.status_code, 400)
        exp = self.client.post("/api/export", json={"result": result})
        self.assertEqual(exp.status_code, 400)

    def test_exhausted_result_treated_as_unfinished(self):
        """求解未完成（预算耗尽）的结果不能保存或导出。"""
        p = base_payload()
        original = server.solver.solve
        try:
            server.solver.solve = lambda roles, rels: {"status": "exhausted"}
            res = self.create_version(p, {"status": "optimal"})
            self.assertEqual(res.status_code, 409)
            self.assertIn("未完成", res.get_json()["error"])
            exp = self.export(p, {"status": "optimal"})
            self.assertEqual(exp.status_code, 409)
        finally:
            server.solver.solve = original

    # -------------------------------------------------- 篡改 result

    def test_tampered_result_fields_rejected(self):
        p = base_payload()
        result = self.solve(p)

        tamper_cases = []
        tampered = json.loads(json.dumps(result))
        tampered["total_delta"] = result["total_delta"] + 100
        tamper_cases.append(("total_delta", tampered))

        tampered = json.loads(json.dumps(result))
        tampered["per_role"][1]["new_hex"] = "#000000"
        tamper_cases.append(("new_hex", tampered))

        tampered = json.loads(json.dumps(result))
        tampered["per_relation"][0]["after"] = 99.0
        tamper_cases.append(("after", tampered))

        tampered = json.loads(json.dumps(result))
        tampered["per_role"][1]["l_after"] = 5
        tamper_cases.append(("l_after", tampered))

        for label, stale in tamper_cases:
            with self.subTest(tamper=label):
                self.assertEqual(self.create_version(p, stale).status_code, 409)
                self.assertEqual(self.export(p, stale).status_code, 409)
        self.assertEqual(self.list_version_ids(), [])

    def test_stale_result_kept_out_of_sqlite_snapshot(self):
        """直接读 SQLite：被拒结果绝不写入，成功写入的快照字段必须自洽。"""
        p = base_payload()
        good = self.solve(p)
        self.assertEqual(self.create_version(p, good).status_code, 201)

        edited = base_payload()
        edited["relations"][0]["min_contrast"] = 7
        self.assertEqual(self.create_version(edited, good).status_code, 409)

        import sqlite3
        with sqlite3.connect(storage.DB_PATH) as conn:
            rows = conn.execute("SELECT label, payload, result FROM versions").fetchall()
        self.assertEqual(len(rows), 1)
        label, payload_sql, result_sql = rows[0]
        saved_payload = json.loads(payload_sql)
        saved_result = json.loads(result_sql)
        # 快照内 result 是对快照内 payload 重算的结果
        canonical = solver.solve(*server._validate_payload(saved_payload))
        self.assertEqual(saved_result["new_hex"], canonical["new_hex"])
        self.assertEqual(saved_result["total_delta"], canonical["total_delta"])

    def test_version_normalizes_payload_so_snapshot_pair_is_canonical(self):
        """大写 HEX、名称带空白等非规范输入，入库时随权威结果一起规范化，
        使快照中的 payload 与 result 严格成对。"""
        p = base_payload()
        p["roles"][1]["hex"] = "#777777"
        p["roles"][1]["name"] = "  text  "
        # 先证明规范化不改变可行性：/api/solve 与存版本都接受同一请求
        solve_res = self.client.post("/api/solve", json=p)
        self.assertEqual(solve_res.status_code, 200, solve_res.data)
        result = solve_res.get_json()

        res = self.create_version(p, result)
        self.assertEqual(res.status_code, 201, res.data)
        version = self.client.get(
            f"/api/versions/{res.get_json()['id']}").get_json()

        saved_role = {r["name"]: r for r in version["payload"]["roles"]}["text"]
        self.assertEqual(saved_role["hex"], "#777777")          # 大写 HEX 已小写化
        self.assertEqual(saved_role["name"], "text")            # 名称空白已去除
        self.assertTrue(
            all(set(r) == {"name", "hex", "locked"} for r in version["payload"]["roles"]))
        self.assertTrue(
            all(set(r) == {"fg", "bg", "min_contrast"} for r in version["payload"]["relations"]))
        # 规范化后的 payload 仍能与快照 result 绑定
        bound = self.export(version["payload"], version["result"])
        self.assertEqual(bound.status_code, 200, bound.data)

    def test_version_delete_roundtrip(self):
        p = base_payload()
        vid = self.create_version(p, self.solve(p)).get_json()["id"]
        self.assertEqual(self.client.delete(f"/api/versions/{vid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/versions/{vid}").status_code, 404)


class ExportBindingTest(PaletteServerTestBase):
    def test_export_uses_canonical_result_not_client_bytes(self):
        """即使客户端篡改 CSS 相关字段，只要配对不通过就拒绝；
        配对通过时，CSS 中的颜色全部来自服务端重算值。"""
        p = base_payload()
        result = self.solve(p)
        res = self.export(p, result)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.headers["Content-Type"], "text/css; charset=utf-8")
        css = res.get_data(as_text=True)

        canonical = solver.solve(*server._validate_payload(p))
        for name, hexv in canonical["new_hex"].items():
            if name in {r["name"] for r in canonical["per_role"]}:
                self.assertIn(hexv, css)
        self.assertIn(f"ΣΔL = {canonical['total_delta']}", css)
        for rel in canonical["per_relation"]:
            self.assertIn(f"{rel['after']:.2f}:1", css)

    def test_export_rejects_result_from_other_payload(self):
        p1 = base_payload()
        r1 = self.solve(p1)
        p2 = base_payload()
        p2["roles"][1]["hex"] = "#222222"
        res = self.export(p2, r1)
        self.assertEqual(res.status_code, 409)

    def test_export_reflects_same_config_as_snapshot(self):
        """版本快照里的 result 与同 payload 导出的 CSS 必须来自同一配置。"""
        p = base_payload()
        result = self.solve(p)
        vid = self.create_version(p, result).get_json()["id"]
        version = self.client.get(f"/api/versions/{vid}").get_json()

        css = self.export(version["payload"], version["result"]).get_data(as_text=True)
        for role in version["result"]["per_role"]:
            self.assertIn(role["new_hex"], css)
        self.assertIn(f"ΣΔL = {version['result']['total_delta']}", css)


class ConfigEndpointTest(PaletteServerTestBase):
    def test_config_save_and_load_unaffected(self):
        p = base_payload()
        res = self.client.post("/api/config", json=p)
        self.assertEqual(res.status_code, 200)
        loaded = self.client.get("/api/config").get_json()
        self.assertEqual(loaded["roles"], p["roles"])
        self.assertEqual(loaded["relations"], p["relations"])


if __name__ == "__main__":
    unittest.main()
