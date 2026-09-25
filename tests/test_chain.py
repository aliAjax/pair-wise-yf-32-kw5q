import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from app import ApiError, OrganAllocationService, iso, utcnow


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, organ="kidney", blood="O", hospital="H1", expires_days=2):
        return self.svc.register_donor("coord", "coordinator", {"blood_type": blood, "organ": organ, "hospital": hospital, "region": "East",
                                                                "available_at": iso(self.now - timedelta(days=1)),
                                                                "expires_at": iso(self.now + timedelta(days=expires_days))})

    def candidate(self, name, organ="kidney", blood="A", hospital="H2"):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": name, "blood_type": blood, "organ": organ, "hospital": hospital,
                                                                    "region": "East", "urgency": 4, "wait_days": 100, "willing": True})

    def pairs(self, spec):
        return [(self.donor(blood=b, hospital=dh), self.candidate(f"患者{i}", blood=cb, hospital=ch))
                for i, (b, dh, cb, ch) in enumerate(spec, 1)]

    def create(self, pairs, deadline=None):
        body = {"legs": [{"donor_id": d["id"], "candidate_id": c["id"]} for d, c in pairs],
                "confirm_deadline": iso(deadline or self.now + timedelta(hours=6))}
        return self.svc.chains.create_chain("coord", "coordinator", body)

    def test_chain_created_and_fully_confirmed(self):
        chain = self.create(self.pairs([("O", "H1", "A", "H2"), ("O", "H2", "B", "H1")]))
        self.assertEqual(chain["status"], "pending")
        self.assertEqual([leg["seq"] for leg in chain["legs"]], [1, 2])
        self.assertTrue(chain["confirm_deadline"])
        donors = {d["id"]: d for d in self.svc.state("coordinator", "")["donors"]}
        for leg in chain["legs"]:
            self.assertEqual(donors[leg["donor_id"]]["status"], "chain_pending")
            self.assertEqual(leg["status"], "pending")
        leg1, leg2 = chain["legs"]
        view = self.svc.chains.confirm_leg(chain["id"], leg1["seq"], "hosp-h2", "hospital", "H2")
        self.assertEqual(view["legs"][0]["status"], "confirmed")
        self.assertEqual(view["status"], "pending")
        view = self.svc.chains.confirm_leg(chain["id"], leg2["seq"], "hosp-h1", "hospital", "H1")
        self.assertEqual(view["status"], "confirmed")
        donors = {d["id"]: d for d in self.svc.state("coordinator", "")["donors"]}
        for leg in chain["legs"]:
            self.assertEqual(donors[leg["donor_id"]]["status"], "allocated")
        actions = [e["action"] for e in self.svc.chains.chain_audit(chain["id"], "auditor")]
        self.assertEqual(actions, ["chain_created", "leg_confirmed", "leg_confirmed", "chain_confirmed"])

    def test_validation_is_all_or_nothing(self):
        bad = self.pairs([("AB", "H1", "O", "H2"), ("O", "H2", "B", "H1")])  # 第 1 组血型不兼容
        with self.assertRaises(ApiError) as ctx:
            self.create(bad)
        self.assertEqual(ctx.exception.code, "chain_mismatch")
        self.assertEqual(ctx.exception.details["issues"][0]["leg"], 1)
        donors = {d["id"]: d for d in self.svc.state("coordinator", "")["donors"]}
        for d, _ in bad:  # 校验不过时不锁任何器官
            self.assertEqual(donors[d["id"]]["status"], "available")
        with self.assertRaises(ApiError) as ctx:  # 少于 2 组
            self.create(self.pairs([("O", "H1", "A", "H2")]))
        self.assertEqual(ctx.exception.code, "chain_mismatch")
        five = [("O", "H1", "A", "H2"), ("O", "H2", "B", "H1"), ("O", "H1", "AB", "H2"), ("O", "H2", "A", "H1"), ("O", "H1", "B", "H2")]
        with self.assertRaises(ApiError) as ctx:  # 多于 4 组
            self.create(self.pairs(five))
        self.assertEqual(ctx.exception.code, "chain_mismatch")
        with self.assertRaises(ApiError) as ctx:  # 截止时间晚于器官有效期
            self.create(self.pairs([("O", "H1", "A", "H2"), ("O", "H2", "B", "H1")]), deadline=self.now + timedelta(days=7))
        self.assertEqual(ctx.exception.code, "chain_mismatch")

    def test_reject_keeps_leg_and_releases_others(self):
        pairs = self.pairs([("O", "H1", "A", "H2"), ("O", "H2", "B", "H3"), ("O", "H3", "AB", "H1")])
        chain = self.create(pairs)
        self.svc.chains.confirm_leg(chain["id"], 1, "hosp-h2", "hospital", "H2")
        view = self.svc.chains.reject_leg(chain["id"], 2, "hosp-h3", "hospital", "H3", {"reason": "患者反悔"})
        self.assertEqual(view["status"], "broken")
        statuses = {leg["seq"]: leg["status"] for leg in view["legs"]}
        self.assertEqual(statuses, {1: "released", 2: "rejected", 3: "released"})
        donors = {d["id"]: d for d in self.svc.state("coordinator", "")["donors"]}
        self.assertEqual(donors[pairs[0][0]["id"]]["status"], "available")   # 未过期器官恢复可选
        self.assertEqual(donors[pairs[1][0]["id"]]["status"], "chain_pending")  # 被拒绝环节留下
        self.assertEqual(donors[pairs[2][0]["id"]]["status"], "available")
        audit = self.svc.chains.chain_audit(chain["id"], "auditor")
        actions = [e["action"] for e in audit]
        self.assertEqual(actions, ["chain_created", "leg_confirmed", "leg_rejected", "organ_returned", "organ_returned", "chain_broken"])
        rejected = next(e for e in audit if e["action"] == "leg_rejected")
        self.assertIn("患者反悔", rejected["detail_json"])
        # 恢复可选的器官可以重新进入普通分配
        candidate = self.candidate("新患者", blood="A", hospital="H2")
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": pairs[0][0]["id"], "candidate_id": candidate["id"]})
        self.assertEqual(allocation["status"], "proposed")

    def test_timeout_keeps_unconfirmed_legs(self):
        pairs = self.pairs([("O", "H1", "A", "H2"), ("O", "H2", "B", "H1")])
        chain = self.create(pairs, deadline=self.now + timedelta(hours=1))
        self.svc.chains.confirm_leg(chain["id"], 1, "hosp-h2", "hospital", "H2")
        original = app.utcnow
        app.utcnow = lambda: self.now + timedelta(hours=2)  # 越过约定确认时间
        try:
            view = self.svc.chains.get_chain(chain["id"], "coordinator", "")
        finally:
            app.utcnow = original
        self.assertEqual(view["status"], "timed_out")
        statuses = {leg["seq"]: leg["status"] for leg in view["legs"]}
        self.assertEqual(statuses, {1: "released", 2: "timed_out"})
        donors = {d["id"]: d for d in self.svc.state("coordinator", "")["donors"]}
        self.assertEqual(donors[pairs[0][0]["id"]]["status"], "available")
        self.assertEqual(donors[pairs[1][0]["id"]]["status"], "chain_pending")
        actions = [e["action"] for e in self.svc.chains.chain_audit(chain["id"], "auditor")]
        self.assertEqual(actions, ["chain_created", "leg_confirmed", "organ_returned", "leg_timed_out", "chain_timed_out"])
        with self.assertRaises(ApiError) as ctx:  # 超时后不能再确认
            self.svc.chains.confirm_leg(chain["id"], 2, "hosp-h1", "hospital", "H1")
        self.assertEqual(ctx.exception.code, "chain_closed")

    def test_permissions_privacy_and_double_booking(self):
        pairs = self.pairs([("O", "H1", "A", "H2"), ("O", "H2", "B", "H1")])
        with self.assertRaises(ApiError) as ctx:  # 非协调员不能建链
            self.svc.chains.create_chain("allocator", "allocation_officer",
                                         {"legs": [{"donor_id": pairs[0][0]["id"], "candidate_id": pairs[0][1]["id"]},
                                                   {"donor_id": pairs[1][0]["id"], "candidate_id": pairs[1][1]["id"]}],
                                          "confirm_deadline": iso(self.now + timedelta(hours=6))})
        self.assertEqual(ctx.exception.status, 403)
        chain = self.create(pairs)
        with self.assertRaises(ApiError) as ctx:  # 无关医院不能查看
            self.svc.chains.get_chain(chain["id"], "hospital", "H9")
        self.assertEqual(ctx.exception.status, 403)
        masked = self.svc.chains.get_chain(chain["id"], "hospital", "H2")
        by_seq = {leg["seq"]: leg for leg in masked["legs"]}
        self.assertNotEqual(by_seq[1]["patient_name"], "***")
        self.assertEqual(by_seq[2]["patient_name"], "***")
        with self.assertRaises(ApiError) as ctx:  # 错误医院不能确认
            self.svc.chains.confirm_leg(chain["id"], 1, "hosp-h1", "hospital", "H1")
        self.assertEqual(ctx.exception.status, 403)
        extra = self.pairs([("O", "H3", "B", "H1")])
        with self.assertRaises(ApiError) as ctx:  # 患者已在其他待确认链中
            self.create([(extra[0][0], pairs[1][1]), (extra[0][0], extra[0][1])])
        self.assertEqual(ctx.exception.code, "chain_mismatch")


if __name__ == "__main__": unittest.main()
