"""R1 测试轮次发现的缺陷回归（BUG-04）。

缺陷来源：``docs/OnlineExam-功能测试用例.xlsx`` 的「8-缺陷与观察项」工作表。
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.common.constants import AttemptStatus, Role
from apps.exam.models import ExamAttempt
from apps.grading.services import GradingService

from .base import GradingBaseTest

PASSWORD = "Exam@123456"


class ScoreboardAnalyticsFlagTest(GradingBaseTest):
    """BUG-04：成绩单的 ``can_view_analytics`` 标记因键名写错而整条失效。

    回归前：视图遍历 ``data.get("rows")``，而 ``ScoreReportService.scoreboard()``
    返回的键是 ``results`` —— 取到 None，一条都不打标。前端拿到 ``undefined``，
    按「不等于 false 就当有权限」渲染成链接，班外学生照点不误、点进去 404。

    这组用例的核心不是「值对不对」，而是**字段必须存在**：
    字段缺失会让前端的权限判断静默失效，比报错更难发现。
    """

    def setUp(self):
        super().setUp()
        # 班外学生：不在教师带的班里，因此教师无权查看他的学情
        self.outsider = get_user_model().objects.create_user(
            username="bug04_outsider", email="bug04_outsider@example.com",
            password=PASSWORD, role=Role.STUDENT, real_name="班外学生",
            student_no="O2026001",
        )

    # ------------------------------------------------------------------
    def _scoreboard(self, user=None):
        return self.get_json(
            "/api/v1/grading/scoreboard/",
            data={"exam_id": self.exam.id}, user=user or self.teacher,
        )

    def _graded_attempt(self, student=None):
        """班内学生走真实判分链路；班外学生直接落一条已交卷记录。

        班外学生不能走开考接口（「面向全体」按创建者收窄后会 403），
        但成绩单的数据来源是 ``exam.attempts``，直接造记录即可复现跨班统考的场景。
        """
        if student is None:
            attempt = self.submit_with_correct_answers()
            GradingService.grade_attempt(attempt, with_ai=True)
            GradingService.finalize_attempt(attempt)
            return attempt
        now = timezone.now()
        return ExamAttempt.objects.create(
            exam=self.exam, student=student,
            started_at=now, deadline_at=now + timedelta(minutes=60),
            submitted_at=now, status=AttemptStatus.SUBMITTED,
        )

    # ------------------------------------------------------------------
    def test_flag_present_on_every_row(self):
        """核心回归：每条结果都必须带 can_view_analytics，不能是 undefined。"""
        self._graded_attempt()
        self._graded_attempt(self.outsider)

        data = self._scoreboard().json()["data"]
        self.assertTrue(data["results"], "成绩单不应为空")
        for row in data["results"]:
            self.assertIn(
                "can_view_analytics", row,
                "标记字段缺失 → 前端拿 undefined 会当成有权限，姓名被渲染成链接",
            )

    def test_outside_student_marked_false(self):
        """班外学生必须是 false：否则他的姓名会被渲染成可点链接、点进去 404。"""
        self._graded_attempt()
        self._graded_attempt(self.outsider)

        data = self._scoreboard().json()["data"]
        rows = {row["student_id"]: row for row in data["results"]}
        self.assertIn(self.outsider.id, rows, "班外学生应出现在成绩单里（跨班统考场景）")
        self.assertFalse(rows[self.outsider.id]["can_view_analytics"])
        self.assertTrue(rows[self.student.id]["can_view_analytics"], "班内学生应有权限")

    def test_admin_sees_all_true(self):
        """管理员不受学情范围限制，全部应为 true。"""
        self._graded_attempt()
        self._graded_attempt(self.outsider)

        data = self._scoreboard(user=self.admin).json()["data"]
        for row in data["results"]:
            self.assertTrue(row["can_view_analytics"], "管理员应能查看任何学生的学情")

    def test_key_name_matches_service(self):
        """护栏：视图读取的键必须真的存在于服务层返回里。

        这条看似多余，但 BUG-04 正是「服务层叫 results、视图读 rows」造成的——
        直接把两边的键名绑在一起断言，下次谁改键名都会在这里挂掉。
        """
        from apps.grading.services import ScoreReportService

        payload = ScoreReportService.scoreboard(self.exam)
        self.assertIn("results", payload, "服务层返回键已变更，视图的打标逻辑需同步")
        self.assertNotIn("rows", payload, "若服务层新增了 rows，请确认视图该读哪一个")
