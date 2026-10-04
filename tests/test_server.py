"""后端回归测试：配置↔结果绑定校验、版本成对落库/恢复、导出一致性。

运行：
    python -m unittest tests.test_server -v
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest

_TMP_DB = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_PATH"] = _TMP_DB

from app import server, storage  # noqa: E402


# 一份可行配置：白色背景锁定，其余角色调明度后可达 AA。
PAYLOAD = {
    "roles": [
        {"name": "background", "hex": "#ffffff", "locked": True},
        {"name": "text", "hex": "#777777", "locked": False},
        {"name": "primary", "hex": "#2563eb", "locked": False},
    ],
    "relations": [
        {"fg": "text", "bg": "background", "min_contrast": 4.5},
        {"fg": "primary", "bg": "background", "min_contrast": 4.5},
    ],
}

# 一份无解配置：两个锁定色对比不足。
INFEASIBLE_PAYLOAD = {
    "roles": [
        {"name": "a", "hex": "#777777", "locked": True},
        {"name": "b", "hex": "#7a7a7a", "locked": True},
    ],
    "relations": [{"fg": "a", "bg": "b", "min_contrast": 4.5}],
}


class ServerBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        storage.init_db()

    def setUp(self):
        self.client = server.app.test_client()

    def solve(self, payload):
        rv = self.client.post("/api/solve", json=payload)
        self.assertEqual(rv.status_code, 200, rv.data)
        data = rv.get_json()
        self.assertEqual(data["status"], "optimal", data)
        return data

    # ---------------------------------------------------------- 求解基线

    def test_solve_optimal_baseline(self):
        result = self.solve(PAYLOAD)
        self.assertIn("lightness", result)
        for rel in result["per_relation"]:
            self.assertGreaterEqual(rel["after"] + 1e-9, rel["min_contrast"])

    # ------------------------------------------------- 连续编辑后旧结果失效

    def test_version_rejects_stale_result_after_each_edit_kind(self):
        """改颜色/名称/锁定/增删角色/改关系门槛后，旧 result 必须被拒绝。"""
        result = self.solve(PAYLOAD)

        def expect_rejected(payload, tag):
            rv = self.client.post("/api/versions",
                                  json={"label": tag, "payload": payload, "result": result})
            self.assertEqual(rv.status_code, 400, f"{tag}: {rv.data}")
            self.assertIn("不一致", rv.get_json()["error"])

        # 1) 修改角色颜色
        p = copy.deepcopy(PAYLOAD)
        p["roles"][1]["hex"] = "#888888"
        expect_rejected(p, "改颜色")

        # 2) 修改角色名称（关系引用同步改名）
        p = copy.deepcopy(PAYLOAD)
        p["roles"][1]["name"] = "body"
        for rel in p["relations"]:
            if rel["fg"] == "text":
                rel["fg"] = "body"
        expect_rejected(p, "改名称")

        # 3) 切换锁定状态
        p = copy.deepcopy(PAYLOAD)
        p["roles"][0]["locked"] = False
        expect_rejected(p, "切锁定")

        # 4) 删除角色（连带关系）
        p = copy.deepcopy(PAYLOAD)
        p["roles"].pop(2)
        p["relations"] = [r for r in p["relations"] if r["fg"] != "primary"]
        expect_rejected(p, "删角色")

        # 5) 新增角色
        p = copy.deepcopy(PAYLOAD)
        p["roles"].append({"name": "muted", "hex": "#999999", "locked": False})
        expect_rejected(p, "增角色")

        # 6) 调整前景—背景关系
        p = copy.deepcopy(PAYLOAD)
        p["relations"][0]["fg"] = "primary"
        expect_rejected(p, "改关系角色")

        # 7) 调整门槛
        p = copy.deepcopy(PAYLOAD)
        p["relations"][0]["min_contrast"] = 7
        expect_rejected(p, "改门槛")

        # 8) 新增 / 删除关系
        p = copy.deepcopy(PAYLOAD)
        p["relations"].append({"fg": "primary", "bg": "text", "min_contrast": 3})
        expect_rejected(p, "增关系")
        p = copy.deepcopy(PAYLOAD)
        p["relations"].pop()
        expect_rejected(p, "删关系")

    # ------------------------------------------------- 拒绝无解/未完成结果

    def test_version_rejects_infeasible_and_missing_result(self):
        rv = self.client.post("/api/solve", json=INFEASIBLE_PAYLOAD)
        infeasible = rv.get_json()
        self.assertEqual(infeasible["status"], "infeasible")

        # 无解结果不能保存
        rv = self.client.post("/api/versions",
                              json={"label": "无解", "payload": INFEASIBLE_PAYLOAD,
                                    "result": infeasible})
        self.assertEqual(rv.status_code, 400)

        # 缺 result / result 状态不对
        rv = self.client.post("/api/versions",
                              json={"label": "无结果", "payload": PAYLOAD})
        self.assertEqual(rv.status_code, 400)
        rv = self.client.post("/api/versions",
                              json={"label": "伪造", "payload": PAYLOAD,
                                    "result": {"status": "optimal"}})
        self.assertEqual(rv.status_code, 400)

        # 无解配置即便带一份从别处偷来的最优 result 也不能存
        good = self.solve(PAYLOAD)
        rv = self.client.post("/api/versions",
                              json={"label": "偷结果", "payload": INFEASIBLE_PAYLOAD,
                                    "result": good})
        self.assertEqual(rv.status_code, 400)

    # ----------------------------------------------- 伪造/篡改字段必须拒绝

    def test_version_rejects_tampered_result_fields(self):
        result = self.solve(PAYLOAD)

        tampered = copy.deepcopy(result)
        tampered["total_delta"] = result["total_delta"] + 5
        rv = self.client.post("/api/versions",
                              json={"label": "篡改delta", "payload": PAYLOAD, "result": tampered})
        self.assertEqual(rv.status_code, 400)

        tampered = copy.deepcopy(result)
        tampered["new_hex"]["text"] = "#123456"
        rv = self.client.post("/api/versions",
                              json={"label": "篡改hex", "payload": PAYLOAD, "result": tampered})
        self.assertEqual(rv.status_code, 400)

        tampered = copy.deepcopy(result)
        first = result["ordered_names"][0]
        tampered["lightness"][first] = result["lightness"][first] + 1
        rv = self.client.post("/api/versions",
                              json={"label": "篡改明度", "payload": PAYLOAD, "result": tampered})
        self.assertEqual(rv.status_code, 400)

        tampered = copy.deepcopy(result)
        tampered["per_relation"][0]["after"] = 21.0
        rv = self.client.post("/api/versions",
                              json={"label": "篡改对比度", "payload": PAYLOAD, "result": tampered})
        self.assertEqual(rv.status_code, 400)

    # ----------------------------------------------- 正常保存：重算结果落库

    def test_version_roundtrip_uses_canonical_pair(self):
        result = self.solve(PAYLOAD)
        rv = self.client.post("/api/versions",
                              json={"label": "v1", "payload": PAYLOAD, "result": result})
        self.assertEqual(rv.status_code, 201, rv.data)
        vid = rv.get_json()["id"]

        rv = self.client.get(f"/api/versions/{vid}")
        saved = rv.get_json()
        # 落库配置被规范化（关系不含内部 index）
        self.assertEqual(saved["payload"]["roles"][0]["hex"], "#ffffff")
        self.assertNotIn("index", saved["payload"]["relations"][0])

        # SQLite 快照中的结果必须是服务端重算的权威结果，与原配置成对
        canonical = self.solve(saved["payload"])
        self.assertEqual(saved["result"]["total_delta"], canonical["total_delta"])
        self.assertEqual(saved["result"]["new_hex"], canonical["new_hex"])
        self.assertEqual(saved["result"]["lightness"], canonical["lightness"])
        self.assertEqual(
            [(r["fg"], r["bg"], round(r["after"], 9)) for r in saved["result"]["per_relation"]],
            [(r["fg"], r["bg"], round(r["after"], 9)) for r in canonical["per_relation"]],
        )

        # 即使客户端送来的是被篡改过但“看起来可行”的结果，
        # 落库的也必须是权威结果。
        tampered = copy.deepcopy(result)
        tampered["nodes"] = 999999  # 非绑定字段不影响校验
        rv = self.client.post("/api/versions",
                              json={"label": "v2", "payload": PAYLOAD, "result": tampered})
        self.assertEqual(rv.status_code, 201)
        saved2 = self.client.get(f"/api/versions/{rv.get_json()['id']}").get_json()
        self.assertEqual(saved2["result"]["nodes"], canonical["nodes"])

    # ------------------------------------------------- 导出：同一份 payload

    def test_export_rejects_stale_and_renders_canonical_css(self):
        result = self.solve(PAYLOAD)

        # 旧结果（配置已改）不能导出
        stale = copy.deepcopy(PAYLOAD)
        stale["relations"][0]["min_contrast"] = 7
        rv = self.client.post("/api/export", json={"payload": stale, "result": result})
        self.assertEqual(rv.status_code, 400)

        # 无解结果不能导出
        rv = self.client.post("/api/export",
                              json={"payload": INFEASIBLE_PAYLOAD,
                                    "result": {"status": "infeasible"}})
        self.assertEqual(rv.status_code, 400)

        # 只带 result 的旧用法：从结果重建配置，重算一致后才放行
        rv = self.client.post("/api/export", json={"result": result})
        self.assertEqual(rv.status_code, 200, rv.data)
        self.assertIn("text/css", rv.headers["Content-Type"])
        css_legacy = rv.data.decode()

        # 推荐用法：payload + result
        rv = self.client.post("/api/export", json={"payload": PAYLOAD, "result": result})
        self.assertEqual(rv.status_code, 200)
        css = rv.data.decode()
        self.assertEqual(css, css_legacy)

        # CSS 中的每个 HEX 必须来自当前配置的权威结果
        canonical = self.solve(PAYLOAD)
        for name, hexv in canonical["new_hex"].items():
            self.assertIn(hexv, css)
        for rel in canonical["per_relation"]:
            self.assertIn(f"{rel['after']:.2f}:1", css)

        # 偷来的结果 + 无解配置：拒绝
        rv = self.client.post("/api/export",
                              json={"payload": INFEASIBLE_PAYLOAD, "result": result})
        self.assertEqual(rv.status_code, 400)

    def test_snapshot_preview_css_share_one_config(self):
        """SQLite 版本快照与 CSS 导出必须绑定同一份配置的同一份结果。"""
        result = self.solve(PAYLOAD)
        rv = self.client.post("/api/versions",
                              json={"label": "pair", "payload": PAYLOAD, "result": result})
        vid = rv.get_json()["id"]
        saved = self.client.get(f"/api/versions/{vid}").get_json()

        rv = self.client.post("/api/export",
                              json={"payload": saved["payload"], "result": saved["result"]})
        self.assertEqual(rv.status_code, 200)
        css = rv.data.decode()

        # 快照结果中的全部新色值与对比度都出现在导出 CSS 中
        for name, hexv in saved["result"]["new_hex"].items():
            self.assertIn(hexv, css, f"快照角色 {name} 的颜色未出现在 CSS")
        for rel in saved["result"]["per_relation"]:
            self.assertIn(rel["fg"], css)
            self.assertIn(rel["bg"], css)
            self.assertIn(f"{rel['after']:.2f}:1", css)

        # 直接用 /api/solve 对快照配置重算，结果应完全一致
        again = self.solve(saved["payload"])
        self.assertEqual(again["new_hex"], saved["result"]["new_hex"])
        self.assertEqual(again["total_delta"], saved["result"]["total_delta"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
