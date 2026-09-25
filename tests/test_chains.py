import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, iso, utcnow


class ExchangeChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db")
        self.now = utcnow()
        self.deadline = iso(self.now + timedelta(hours=20))

    def tearDown(self):
        self.tmp.cleanup()

    def donor(self, blood, hospital, expires_days=2, organ="kidney", region="East"):
        return self.svc.register_donor("coord", "coordinator", {
            "blood_type": blood, "organ": organ, "hospital": hospital, "region": region,
            "available_at": iso(self.now - timedelta(days=3)),
            "expires_at": iso(self.now + timedelta(days=expires_days)), "clinical_match": 8})

    def candidate(self, name, blood, hospital, organ="kidney", region="East", willing=True):
        return self.svc.register_candidate("coord", "coordinator", {
            "patient_name": name, "blood_type": blood, "organ": organ, "hospital": hospital,
            "region": region, "urgency": 5, "wait_days": 400, "willing": willing, "clinical_match": 9})

    def two_way(self):
        # H1 的 A 型器官给 H2 的 A 患者；H2 的 B 型器官给 H1 的 B 患者
        d1 = self.donor("A", "H1"); c1 = self.candidate("患者甲", "A", "H2")
        d2 = self.donor("B", "H2"); c2 = self.candidate("患者乙", "B", "H1")
        return [(d1["id"], c1["id"]), (d2["id"], c2["id"])]

    def create(self, groups, deadline=None):
        return self.svc.create_chain("coord1", "coordinator",
                                     {"groups": [{"donor_id": d, "candidate_id": c} for d, c in groups],
                                      "deadline": deadline or self.deadline, "note": "双院互换"})

    # ---- 建链校验 ----

    def test_create_requires_two_to_four_groups(self):
        groups = self.two_way()
        with self.assertRaises(ApiError) as ctx:
            self.create(groups[:1])
        self.assertEqual(ctx.exception.code, "invalid_groups")
        self.create(groups)

    def test_validation_per_group_checks_blood_organ_hospital_expiry(self):
        # 血型不兼容：O 器官给不了 AB? O→AB 兼容；这里用 AB→A 不兼容
        d_bad = self.donor("AB", "H3"); c_bad = self.candidate("患者丙", "A", "H4")
        d2 = self.donor("A", "H4"); c2 = self.candidate("患者丁", "A", "H3")
        with self.assertRaises(ApiError) as ctx:
            self.create([(d_bad["id"], c_bad["id"]), (d2["id"], c2["id"])])
        self.assertEqual(ctx.exception.code, "chain_validation_failed")
        self.assertIn("1", ctx.exception.errors["groups"])
        self.assertEqual(ctx.exception.errors["groups"]["1"]["code"], "blood_mismatch")
        # 校验失败不锁器官
        row = self.svc.repo.conn.execute("SELECT status FROM donors WHERE id=?", (d_bad["id"],)).fetchone()
        self.assertEqual(row["status"], "available")

    def test_same_hospital_and_unbalanced_loop_rejected(self):
        d1 = self.donor("A", "H1"); c1 = self.candidate("患者甲", "A", "H1")  # 同院
        d2 = self.donor("B", "H2"); c2 = self.candidate("患者乙", "B", "H2")
        with self.assertRaises(ApiError) as ctx:
            self.create([(d1["id"], c1["id"]), (d2["id"], c2["id"])])
        self.assertEqual(ctx.exception.errors["groups"]["1"]["code"], "cross_hospital_required")

        # 3 组全部跨院且各自匹配，但转出 {H1,H2,H3} 接收 {H2,H3,H2}：不成环
        d3 = self.donor("A", "H1"); c3 = self.candidate("患者丙", "A", "H2")
        d4 = self.donor("B", "H2"); c4 = self.candidate("患者丁", "B", "H3")
        d5 = self.donor("O", "H3"); c5 = self.candidate("患者戊", "O", "H2")
        with self.assertRaises(ApiError) as ctx:
            self.create([(d3["id"], c3["id"]), (d4["id"], c4["id"]), (d5["id"], c5["id"])])
        self.assertEqual(ctx.exception.errors["chain"][0]["code"], "hospital_loop_unbalanced")

    def test_deadline_must_precede_expiry(self):
        groups = self.two_way()
        with self.assertRaises(ApiError) as ctx:
            self.create(groups, deadline=iso(self.now + timedelta(days=5)))
        self.assertEqual(ctx.exception.code, "chain_validation_failed")
        self.assertEqual(ctx.exception.errors["groups"]["1"]["code"], "deadline_after_expiry")

    # ---- 确认成功路径 ----

    def test_full_confirmation_materializes_allocations(self):
        groups = self.two_way()
        chain = self.create(groups)
        self.assertEqual(chain["status"], "pending")
        self.assertEqual([l["status"] for l in chain["legs"]], ["pending", "pending"])
        # 建链即锁定器官，单笔分配拿不走
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("officer", "allocation_officer",
                             {"donor_id": groups[0][0], "candidate_id": self.candidate("x", "A", "H9")["id"]})
        self.assertEqual(ctx.exception.code, "donor_unavailable")

        c1 = self.svc.respond_leg(chain["id"], 1, "h2-user", "hospital", "H2", {}, approve=True)
        self.assertEqual([l["status"] for l in c1["legs"]], ["confirmed", "pending"])
        self.assertEqual(c1["status"], "pending")

        # 错医院不能确认别人的环节
        with self.assertRaises(ApiError) as ctx:
            self.svc.respond_leg(chain["id"], 2, "h2-user", "hospital", "H2", {}, approve=True)
        self.assertEqual(ctx.exception.status, 403)

        done = self.svc.respond_leg(chain["id"], 2, "h1-user", "hospital", "H1", {}, approve=True)
        self.assertEqual(done["status"], "confirmed")
        self.assertTrue(all(l["allocation_id"] for l in done["legs"]))
        # 落地的 allocation 直接是 accepted，可继续转运/交接流程
        aid = done["legs"][0]["allocation_id"]
        transit = self.svc.mark_transit(aid, "officer", "allocation_officer", {"cold_chain_temp": 3.0})
        self.assertEqual(transit["status"], "in_transit")

        audit = self.svc.chain_audit(chain["id"], "auditor", "")
        actions = [a["action"] for a in audit["audit"]]
        self.assertEqual(actions.count("chain_leg_confirmed"), 2)
        self.assertEqual(actions[0], "chain_created")
        self.assertEqual(actions[-1], "chain_confirmed")

    # ---- 拒绝 / 超时退回 ----

    def test_rejection_holds_that_leg_and_releases_others(self):
        groups = self.two_way()
        chain = self.create(groups)
        broken = self.svc.respond_leg(chain["id"], 1, "h2-user", "hospital", "H2",
                                      {"reason": "患者临时反悔"}, approve=False)
        self.assertEqual(broken["status"], "broken")
        self.assertEqual(broken["failure_reason"], "rejected: 患者临时反悔")
        statuses = {l["position"]: (l["status"], l["donor_status"]) for l in broken["legs"]}
        # 拒绝环节留存 held；未拒绝环节未过期器官恢复可选
        self.assertEqual(statuses[1], ("rejected", "held"))
        self.assertEqual(statuses[2][0], "pending")
        self.assertEqual(statuses[2][1], "available")
        # 已断链不能再确认
        with self.assertRaises(ApiError) as ctx:
            self.svc.respond_leg(chain["id"], 2, "h1-user", "hospital", "H1", {}, approve=True)
        self.assertEqual(ctx.exception.code, "chain_closed")

        # 协调员释放留存器官后，单笔分配可再次使用
        released = self.svc.release_held_donor(groups[0][0], "coord1", "coordinator",
                                               {"reason": "患者反悔，器官重新入池"})
        self.assertEqual(released["status"], "available")
        audit = self.svc.chain_audit(chain["id"], "auditor", "")
        self.assertIn("held_donor_released", [a["action"] for a in audit["audit"]])

    def test_timeout_marks_chain_broken_and_auditable(self):
        groups = self.two_way()
        chain = self.create(groups)
        # 第一组先确认；再把截止时间改到过去模拟超时
        self.svc.respond_leg(chain["id"], 1, "h2-user", "hospital", "H2", {}, approve=True)
        with self.svc.repo.tx() as conn:
            conn.execute("UPDATE exchange_chains SET deadline=? WHERE id=?",
                         (iso(self.now - timedelta(minutes=1)), chain["id"]))
        view = self.svc.get_chain(chain["id"], "coordinator", "")
        self.assertEqual(view["status"], "broken")
        self.assertTrue(view["failure_reason"].startswith("timed_out"))
        by_pos = {l["position"]: (l["status"], l["donor_status"]) for l in view["legs"]}
        self.assertEqual(by_pos[2], ("timed_out", "held"))
        self.assertEqual(by_pos[1], ("confirmed", "available"))  # 已确认环节的器官也回池
        audit = self.svc.chain_audit(chain["id"], "auditor", "")
        actions = [a["action"] for a in audit["audit"]]
        self.assertIn("chain_leg_timed_out", actions)
        self.assertIn("chain_broken", actions)

    def test_expired_organ_on_break_not_returned_to_pool(self):
        groups = self.two_way()
        chain = self.create(groups)
        # 把第二组器官改成已过期；第一组拒绝后，第二组不应回池而应 expired
        with self.svc.repo.tx() as conn:
            conn.execute("UPDATE donors SET expires_at=? WHERE id=?",
                         (iso(self.now - timedelta(minutes=1)), groups[1][0]))
        broken = self.svc.respond_leg(chain["id"], 1, "h2-user", "hospital", "H2",
                                      {"reason": "反悔"}, approve=False)
        by_pos = {l["position"]: l["donor_status"] for l in broken["legs"]}
        self.assertEqual(by_pos[1], "held")
        self.assertEqual(by_pos[2], "expired")

    def test_only_coordinator_creates_and_hospital_privacy(self):
        # viewer 不能建链
        with self.assertRaises(ApiError):
            self.svc.create_chain("v", "viewer", {"groups": [], "deadline": self.deadline})
        chain = self.create(self.two_way())
        view = self.svc.get_chain(chain["id"], "hospital", "H1")
        # H1 是第 2 组的接收医院：看不到第 1 组患者姓名
        masked = {l["position"]: l["patient_name"] for l in view["legs"]}
        self.assertEqual(masked[1], "***")
        self.assertNotEqual(masked[2], "***")


if __name__ == "__main__":
    unittest.main()
