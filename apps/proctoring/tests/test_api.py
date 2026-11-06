"""作弊行为检测接口与页面测试。

覆盖：权限边界（教师专属）、返回结构、参数校验、服务与接口口径一致、页面可达性。
"""

from apps.exam.constants import BehaviorEvent
from apps.proctoring.constants import RULE_CODES
from apps.proctoring.services import ProctoringReportService

from .base import ProctoringBaseTest

EXAMS_URL = "/api/v1/proctoring/exams/"
RULES_URL = "/api/v1/proctoring/rules/"


class AnonymousAccessTest(ProctoringBaseTest):
    """未登录一律拒绝。"""

    def test_endpoints_require_authentication(self):
        for url in (EXAMS_URL, RULES_URL, f"/api/v1/proctoring/exams/{self.exam.id}/report/"):
            response = self.client.get(url)
            self.assertIn(response.status_code, (401, 403), url)


class PermissionTest(ProctoringBaseTest):
    """监考数据只有教师能看。"""

    def setUp(self):
        self.attempt = self.make_attempt_raw(self.exam)

    def test_student_is_forbidden(self):
        for url in (EXAMS_URL, RULES_URL,
                    f"/api/v1/proctoring/exams/{self.exam.id}/report/",
                    f"/api/v1/proctoring/attempts/{self.attempt.id}/evidence/"):
            response = self.get_json(url, user=self.student)
            self.assertEqual(response.status_code, 403, url)

    def test_teacher_is_allowed(self):
        for url in (EXAMS_URL, RULES_URL,
                    f"/api/v1/proctoring/exams/{self.exam.id}/report/",
                    f"/api/v1/proctoring/attempts/{self.attempt.id}/evidence/"):
            response = self.get_json(url, user=self.teacher)
            self.assertEqual(response.status_code, 200, url)

    def test_dashboard_page_student_forbidden(self):
        self.client.force_login(self.student)
        response = self.client.get("/proctoring/")
        self.assertEqual(response.status_code, 403)

    def test_dashboard_page_teacher_ok(self):
        self.client.force_login(self.teacher)
        response = self.client.get("/proctoring/")
        self.assertEqual(response.status_code, 200)

    def test_evidence_page_teacher_ok(self):
        self.client.force_login(self.teacher)
        response = self.client.get(f"/proctoring/attempts/{self.attempt.id}/evidence/")
        self.assertEqual(response.status_code, 200)


class ExamListTest(ProctoringBaseTest):
    """考试列表：只列有行为数据的考试。"""

    def test_lists_exams_with_logs(self):
        exam, attempts = self.make_exam_with_attempts(2)
        self.add_log(attempts[0], BehaviorEvent.SWITCH)
        response = self.get_json(EXAMS_URL, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]["results"]
        ids = [row["id"] for row in rows]
        self.assertIn(exam.id, ids)
        row = next(item for item in rows if item["id"] == exam.id)
        self.assertEqual(row["log_count"], 1)
        self.assertEqual(row["attempt_count"], 1)

    def test_exam_without_logs_is_hidden(self):
        exam = self.make_exam()
        response = self.get_json(EXAMS_URL, user=self.teacher)
        ids = [row["id"] for row in response.json()["data"]["results"]]
        self.assertNotIn(exam.id, ids)


class ReportTest(ProctoringBaseTest):
    """可疑考生报告。"""

    def setUp(self):
        self.report_exam = self.make_exam()
        self.clean = self.make_attempt_raw(self.report_exam, student=self.student)
        self.dirty = self.make_attempt_raw(self.report_exam, student=self.student_b, switch_count=9)
        self.add_log(self.dirty, BehaviorEvent.SWITCH, count=9, duration=200)

    def _report(self, **params):
        response = self.get_json(f"/api/v1/proctoring/exams/{self.report_exam.id}/report/",
                                 user=self.teacher, data=params)
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]

    def test_report_shape(self):
        data = self._report()
        for key in ("exam", "overview", "anomaly", "thresholds", "rows", "flagged_count"):
            self.assertIn(key, data)
        self.assertEqual(data["exam"]["id"], self.report_exam.id)

    def test_suspicious_row_is_top(self):
        data = self._report()
        self.assertEqual(data["rows"][0]["attempt_id"], self.dirty.id)
        self.assertTrue(data["rows"][0]["is_suspicious"])
        self.assertIn("switch_over_limit", data["rows"][0]["signal_codes"])

    def test_clean_row_is_not_suspicious(self):
        data = self._report()
        row = next(item for item in data["rows"] if item["attempt_id"] == self.clean.id)
        self.assertFalse(row["is_suspicious"])

    def test_only_suspicious_filter(self):
        data = self._report(only_suspicious=True)
        ids = [row["attempt_id"] for row in data["rows"]]
        self.assertEqual(ids, [self.dirty.id])
        self.assertGreaterEqual(data["flagged_count"], 1)

    def test_top_n(self):
        data = self._report(top_n=1)
        self.assertEqual(len(data["rows"]), 1)

    def test_invalid_top_n_returns_400(self):
        response = self.get_json(f"/api/v1/proctoring/exams/{self.report_exam.id}/report/",
                                 user=self.teacher, data={"top_n": "abc"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_exam_returns_404(self):
        response = self.get_json("/api/v1/proctoring/exams/999999/report/", user=self.teacher)
        self.assertEqual(response.status_code, 404)

    def test_view_matches_service(self):
        """接口与服务的口径必须一致（防止页面把同一指标算出两个数）。"""
        data = self._report()
        service = ProctoringReportService.exam_report(self.report_exam)
        self.assertEqual(data["overview"], service["overview"])
        self.assertEqual(data["thresholds"], service["thresholds"])
        self.assertEqual([row["attempt_id"] for row in data["rows"]],
                         [row["attempt_id"] for row in service["rows"]])
        self.assertEqual(set(data["rows"][0].keys()), set(service["rows"][0].keys()))


class EvidenceTest(ProctoringBaseTest):
    """证据链。"""

    def setUp(self):
        self.evidence_exam = self.make_exam()
        self.attempt = self.make_attempt_raw(self.evidence_exam, switch_count=6, paste_count=2)
        self.add_log(self.attempt, BehaviorEvent.SWITCH, count=4, duration=120)
        self.add_log(self.attempt, BehaviorEvent.PASTE, count=2)

    def _evidence(self):
        response = self.get_json(
            f"/api/v1/proctoring/attempts/{self.attempt.id}/evidence/", user=self.teacher)
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]

    def test_evidence_shape(self):
        data = self._evidence()
        for key in ("attempt", "risk", "metrics", "anomaly", "timeline",
                    "distribution", "counter_check", "thresholds"):
            self.assertIn(key, data)
        self.assertEqual(data["attempt"]["id"], self.attempt.id)

    def test_timeline_lists_each_log(self):
        data = self._evidence()
        self.assertEqual(len(data["timeline"]), 2)
        self.assertEqual({row["event_type"] for row in data["timeline"]},
                         {BehaviorEvent.SWITCH, BehaviorEvent.PASTE})

    def test_signals_are_explainable(self):
        data = self._evidence()
        self.assertTrue(data["risk"]["signals"])
        for signal in data["risk"]["signals"]:
            self.assertTrue(signal["reason"])
            self.assertTrue(signal["evidence"])

    def test_counter_check_reports_consistency(self):
        data = self._evidence()
        check = data["counter_check"]
        self.assertIn("consistent", check)
        self.assertEqual(check["switch_count"], 6)

    def test_unknown_attempt_returns_404(self):
        response = self.get_json("/api/v1/proctoring/attempts/999999/evidence/",
                                 user=self.teacher)
        self.assertEqual(response.status_code, 404)


class RulesCatalogTest(ProctoringBaseTest):
    """规则目录。"""

    def test_catalog_lists_all_rules(self):
        response = self.get_json(RULES_URL, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        codes = [rule["code"] for rule in data["rules"]]
        self.assertEqual(set(codes), set(RULE_CODES))
        self.assertIn("thresholds", data)
        self.assertIn("levels", data)

    def test_catalog_marks_anomaly_method(self):
        data = self.get_json(RULES_URL, user=self.teacher).json()["data"]
        self.assertIn(data["anomaly_method"], ("isolation_forest", "robust_zscore"))
