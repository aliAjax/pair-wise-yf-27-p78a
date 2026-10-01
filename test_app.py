import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 3)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)

    def _source_with_objects(self, count=2):
        source = self.store.add_source("staff", "战时流转档案", "archive", "WAR-1943-1")
        objects = []
        for i in range(count):
            obj = self.store.create_object("staff", f"M-1943-{i}", f"藏品{i}", "杂项", "库房", "同一档案来源。")
            self.store.add_event("staff", obj["id"], "transfer", "1943-05-01", "", "某地", "战时易手", source["id"], "public")
            objects.append(obj)
        return source, objects

    def test_source_update_impact_confirmation_and_apply(self):
        source, (obj1, obj2) = self._source_with_objects()
        claim_open = self.store.create_claim("claimant1", obj1["id"], "王氏家族", "返还油画")
        self.store.transition_claim("reviewer1", claim_open["id"], "under_review", "材料齐全，进入调查。")
        claim_done = self.store.create_claim("claimant1", obj2["id"], "李氏家族", "返还素描")
        self.store.transition_claim("reviewer1", claim_done["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim_done["id"], "resolved_return", "签署返还协议。")
        # 选定来源、填写原因后先列出影响范围。
        upd = self.store.create_source_update("staff", source["id"], "withdrawal", "档案被证实伪造")
        self.assertEqual(upd["status"], "pending_confirmation")
        self.assertEqual({o["id"] for o in upd["impact"]["objects"]}, {obj1["id"], obj2["id"]})
        self.assertEqual(len(upd["impact"]["events"]), 2)
        self.assertEqual({c["id"] for c in upd["impact"]["claims"]}, {claim_open["id"], claim_done["id"]})
        result = self.store.confirm_source_update("staff", upd["id"])
        self.assertEqual(result["status"], "applied")
        # 未完成主张失效，已完成返还不受影响。
        self.assertEqual(self.store.get_object("reviewer1", obj1["id"])["claims"][0]["status"], "invalidated")
        self.assertEqual(self.store.get_object("reviewer1", obj2["id"])["claims"][0]["status"], "resolved_return")
        # 失效主张可退回重算。
        self.store.transition_claim("reviewer1", claim_open["id"], "submitted", "来源撤回后重算，重新审查。")
        # 历次快照仍可查。
        self.assertGreaterEqual(len(self.store.object_history("staff", obj1["id"])), 4)
        snap = self.store.history_detail("staff", obj1["id"], 1)
        self.assertEqual(snap["snapshot"]["object"]["inventory_no"], "M-1943-0")
        with self.store.connect() as conn:
            status = conn.execute("SELECT status FROM sources WHERE id=?", (source["id"],)).fetchone()["status"]
        self.assertEqual(status, "withdrawn")

    def test_scope_change_blocks_confirm_until_refresh(self):
        source, (obj1,) = self._source_with_objects(1)
        upd = self.store.create_source_update("staff", source["id"], "withdrawal", "档案存疑撤回")
        # 确认前新增引用：请求停在待重试并带上新对象。
        obj2 = self.store.create_object("staff", "M-NEW", "新登记藏品", "杂项", "库房", "简介。")
        self.store.add_event("staff", obj2["id"], "acquisition", "1944-01-01", "", "某地", "入藏", source["id"], "internal")
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_source_update("staff", upd["id"])
        self.assertEqual(ctx.exception.code, "scope_changed")
        details = ctx.exception.details
        self.assertEqual(details["status"], "pending_retry")
        self.assertEqual([o["id"] for o in details["new_objects"]], [obj2["id"]])
        self.assertEqual(self.store.get_source_update("staff", upd["id"])["status"], "pending_retry")
        refreshed = self.store.refresh_source_update("staff", upd["id"])
        self.assertEqual(refreshed["status"], "pending_confirmation")
        self.assertEqual(len(refreshed["impact"]["objects"]), 2)
        self.assertEqual(self.store.confirm_source_update("staff", upd["id"])["status"], "applied")

    def test_concurrent_withdrawal_first_wins_second_gets_latest_impact(self):
        source, _ = self._source_with_objects(1)
        first = self.store.create_source_update("staff", source["id"], "withdrawal", "第一次撤回")
        second = self.store.create_source_update("staff", source["id"], "withdrawal", "第二次撤回")
        self.assertEqual(self.store.confirm_source_update("staff", first["id"])["status"], "applied")
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_source_update("staff", second["id"])
        self.assertEqual(ctx.exception.code, "source_update_conflict")
        self.assertEqual(ctx.exception.details["blocking_update_id"], first["id"])
        self.assertIn("objects", ctx.exception.details["impact"])

    def test_batch_failure_keeps_progress_and_retry_resumes(self):
        source, objects = self._source_with_objects(3)
        upd = self.store.create_source_update("staff", source["id"], "withdrawal", "档案撤回")
        original = ProvenanceStore._apply_item

        def flaky(self, conn, update, object_id, actor):
            if object_id == objects[1]["id"]:
                raise RuntimeError("模拟写入失败")
            return original(self, conn, update, object_id, actor)

        with mock.patch.object(ProvenanceStore, "_apply_item", flaky):
            with self.assertRaises(BusinessError) as ctx:
                self.store.confirm_source_update("staff", upd["id"])
        self.assertEqual(ctx.exception.code, "apply_failed")
        progress = ctx.exception.details["progress"]
        self.assertEqual((progress["done"], progress["pending"]), (1, 2))
        # 重试只续做未完成对象。
        result = self.store.retry_source_update("staff", upd["id"])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["progress"]["done"], 3)
        with self.store.connect() as conn:
            for obj in objects:
                count = conn.execute(
                    "SELECT COUNT(*) c FROM audit_log WHERE object_id=? AND action='source_update.apply'",
                    (obj["id"],)).fetchone()["c"]
                self.assertEqual(count, 1)
        # 重复提交不写第二遍审计。
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_source_update("staff", upd["id"])
        self.assertEqual(ctx.exception.code, "update_already_applied")
        with self.store.connect() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM audit_log WHERE action='source_update.apply'").fetchone()["c"]
        self.assertEqual(total, 3)

    def test_claimant_sees_only_own_claim_impact(self):
        with self.store.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO users(id,name,role) VALUES('claimant2','第二主张人','claimant')")
        source, (obj1, obj2) = self._source_with_objects()
        mine = self.store.create_claim("claimant1", obj1["id"], "甲家族", "返还甲藏品")
        other_claim = self.store.create_claim("claimant2", obj2["id"], "乙家族", "返还乙藏品")
        upd = self.store.create_source_update("staff", source["id"], "withdrawal", "档案撤回")
        self.store.confirm_source_update("staff", upd["id"])
        view = self.store.get_source_update("claimant1", upd["id"])
        self.assertEqual([c["id"] for c in view["impact"]["claims"]], [mine["id"]])
        self.assertEqual([o["id"] for o in view["impact"]["objects"]], [obj1["id"]])
        self.assertNotIn("progress", view)
        other = self.store.get_source_update("claimant2", upd["id"])
        self.assertEqual([c["id"] for c in other["impact"]["claims"]], [other_claim["id"]])
        self.assertEqual([o["id"] for o in other["impact"]["objects"]], [obj2["id"]])
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_source_update("public", upd["id"])
        self.assertEqual(ctx.exception.status, 403)

    def test_correction_updates_source_and_invalidates_claims(self):
        source, (obj,) = self._source_with_objects(1)
        self.store.create_claim("claimant1", obj["id"], "某家族", "返还藏品")
        upd = self.store.create_source_update("staff", source["id"], "correction", "引用编号更正", {"reference": "WAR-1943-1-REV"})
        self.assertEqual(self.store.confirm_source_update("staff", upd["id"])["status"], "applied")
        with self.store.connect() as conn:
            row = conn.execute("SELECT reference,status FROM sources WHERE id=?", (source["id"],)).fetchone()
        self.assertEqual((row["reference"], row["status"]), ("WAR-1943-1-REV", "corrected"))
        self.assertEqual(self.store.get_object("reviewer1", obj["id"])["claims"][0]["status"], "invalidated")


if __name__ == "__main__":
    unittest.main()
