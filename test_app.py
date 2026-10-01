import base64
import tempfile
import unittest
from pathlib import Path

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


class RetractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _cited_source(self, ref="REF-1"):
        source = self.store.add_source("staff", "购藏档案", "archive", ref)
        obj = self.store.create_object("staff", f"M-{ref}", "青铜器", "礼器", "市博物馆", "入藏来源待核。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        return source, obj, event, claim

    def test_retraction_impact_and_confirm_flow(self):
        source, obj, event, claim = self._cited_source()
        retraction = self.store.create_retraction("staff", source["id"], "来源档案存疑，启动撤回。")
        self.assertEqual(retraction["status"], "pending")
        self.assertEqual(retraction["impact"]["object_ids"], [obj["id"]])
        self.assertEqual(retraction["impact"]["event_ids"], [event["id"]])
        self.assertEqual(retraction["impact"]["claim_ids"], [claim["id"]])
        confirmed = self.store.confirm_retraction("staff", retraction["retraction_id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["processed"], 1)
        # 来源已撤回，事件仍保留引用。
        row = self.store.connect().execute("SELECT status FROM sources WHERE id=?", (source["id"],)).fetchone()
        self.assertEqual(row["status"], "retracted")
        # 重复确认幂等：不重复写审计。
        again = self.store.confirm_retraction("staff", retraction["retraction_id"])
        self.assertTrue(again["already_confirmed"])
        self.assertEqual(again["processed"], 0)
        audits = self.store.connect().execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE object_id=? AND action='retraction.confirm'", (obj["id"],)
        ).fetchone()["c"]
        self.assertEqual(audits, 1)

    def test_new_citation_before_confirm_goes_pending_retry(self):
        source, obj1, _, _ = self._cited_source("REF-2")
        retraction = self.store.create_retraction("staff", source["id"], "撤回原因。")
        # 确认前新增一件引用该来源的藏品。
        obj2 = self.store.create_object("staff", "M-REF-2B", "青铜器", "礼器", "市博物馆", "新发现引用。")
        self.store.add_event("staff", obj2["id"], "acquisition", "2001-03-01", "", "本市", "同批入藏", source["id"], "public")
        first = self.store.confirm_retraction("staff", retraction["retraction_id"])
        self.assertEqual(first["status"], "pending_retry")
        self.assertEqual(first["new_object_ids"], [obj2["id"]])
        self.assertEqual(self.store.get_retraction("staff", retraction["retraction_id"])["status"], "pending_retry")
        # 重试确认：新对象被纳入并完成。
        second = self.store.confirm_retraction("staff", retraction["retraction_id"])
        self.assertEqual(second["status"], "confirmed")
        self.assertEqual(second["processed"], 2)
        items = self.store.connect().execute(
            "SELECT object_id,status FROM retraction_items WHERE retraction_id=? ORDER BY object_id",
            (retraction["retraction_id"],),
        ).fetchall()
        self.assertEqual([(i["object_id"], i["status"]) for i in items],
                         [(obj1["id"], "done"), (obj2["id"], "done")])

    def test_concurrent_retraction_first_wins_latest_impact(self):
        source, _, _, _ = self._cited_source("REF-3")
        first = self.store.create_retraction("staff", source["id"], "先到者发起撤回。")
        # 另一人同时提交同一来源的撤回：不新建，拿到已有最新影响范围。
        second = self.store.create_retraction("staff", source["id"], "后到者也发起撤回。")
        self.assertFalse(second["created"])
        self.assertEqual(second["retraction_id"], first["retraction_id"])
        self.assertEqual(second["impact"]["object_ids"], first["impact"]["object_ids"])
        # 先到者确认后来源撤回，后到者再确认拿到最新影响范围。
        self.store.confirm_retraction("staff", first["retraction_id"])
        again = self.store.confirm_retraction("staff", second["retraction_id"])
        self.assertEqual(again["status"], "confirmed")
        self.assertTrue(again["already_confirmed"])

    def test_batch_failure_preserves_progress_and_retries(self):
        source = self.store.add_source("staff", "批量档案", "archive", "REF-4")
        objects = []
        for i in range(3):
            obj = self.store.create_object("staff", f"M-REF-4-{i}", "青铜器", "礼器", "市博物馆", "批量引用。")
            self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "购入", source["id"], "public")
            objects.append(obj)
        retraction = self.store.create_retraction("staff", source["id"], "批量撤回。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_retraction("staff", retraction["retraction_id"], fail_after=2)
        self.assertEqual(ctx.exception.code, "batch_failed")
        # 进度保留：2 件完成、1 件待续。
        items = self.store.connect().execute(
            "SELECT object_id,status FROM retraction_items WHERE retraction_id=? ORDER BY object_id",
            (retraction["retraction_id"],),
        ).fetchall()
        self.assertEqual([i["status"] for i in items], ["done", "done", "pending"])
        # 重试只续做未完成对象，不重复写审计。
        retried = self.store.confirm_retraction("staff", retraction["retraction_id"])
        self.assertEqual(retried["status"], "confirmed")
        self.assertEqual(retried["processed"], 1)
        for obj in objects:
            count = self.store.connect().execute(
                "SELECT COUNT(*) AS c FROM audit_log WHERE object_id=? AND action='retraction.confirm'",
                (obj["id"],),
            ).fetchone()["c"]
            self.assertEqual(count, 1)

    def test_source_update_invalidates_unfinished_claims(self):
        source, obj, _, unfinished = self._cited_source("REF-5")
        done = self.store.create_claim("claimant1", obj["id"], "李氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", done["id"], "under_review", "材料齐全进入调查。")
        self.store.transition_claim("reviewer1", done["id"], "resolved_return", "签署返还协议完成。")
        result = self.store.update_source("staff", source["id"], {"reference": "REF-5-REVISED"})
        self.assertEqual([c["claim_id"] for c in result["invalidated_claims"]], [unfinished["id"]])
        view = self.store.get_object("claimant1", obj["id"])
        statuses = {c["id"]: (c["status"], c["impact_status"]) for c in view["claims"]}
        self.assertEqual(statuses[unfinished["id"]], ("submitted", "invalid"))
        self.assertEqual(statuses[done["id"]], ("resolved_return", "normal"))
        # 历史快照仍可查。
        history = self.store.object_history("reviewer1", obj["id"])
        self.assertGreaterEqual(len(history), 2)

    def test_claimant_sees_only_own_impact(self):
        source = self.store.add_source("staff", "多方档案", "archive", "REF-6")
        obj = self.store.create_object("staff", "M-REF-6", "青铜器", "礼器", "市博物馆", "多方主张。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "购入", source["id"], "public")
        claim1 = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        claim2 = self.store.create_claim("claimant2", obj["id"], "张氏家族", "返还藏品")
        retraction = self.store.create_retraction("staff", source["id"], "撤回影响多方。")
        view1 = self.store.get_retraction("claimant1", retraction["retraction_id"])
        view2 = self.store.get_retraction("claimant2", retraction["retraction_id"])
        self.assertEqual(view1["impact"]["claim_ids"], [claim1["id"]])
        self.assertEqual(view2["impact"]["claim_ids"], [claim2["id"]])
        self.assertEqual(view1["impact"]["object_ids"], [obj["id"]])
        self.assertEqual(view2["impact"]["object_ids"], [obj["id"]])
        self.assertEqual(view1["impact"]["event_ids"], [event["id"]])
        # 主张人列表也只含与自己有关的撤回。
        self.assertEqual([r["id"] for r in self.store.list_retractions("claimant1")], [retraction["retraction_id"]])
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_retraction("public", retraction["retraction_id"])
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
