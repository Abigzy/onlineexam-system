"""首页工作台（教师端 / 学生端二次设计）测试。

设计要点：**导航 = 功能地图，首页 = 数据工作台**。
首页不再罗列功能入口卡片，只给「关键数字 + 待办 + 最近动态」。
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.constants import AttemptStatus, Role
from apps.exam.tests.base import SUBJECT, ExamBaseTest

from ..services import HomeDashboardService

User = get_user_model()


class HomeDashboardBaseTest(ExamBaseTest):
    def _submit_attempt(self, exam, student=None, *, status=AttemptStatus.SUBMITTED,
                        score=Decimal("0")):
        attempt = self.start(exam, student=student)
        attempt.status = status
        attempt.score = score
        attempt.submitted_at = timezone.now()
        attempt.save(update_fields=["status", "score", "submitted_at"])
        return attempt

    @staticmethod
    def _stat_map(payload):
        return {item["label"]: item["value"] for item in payload["stats"]}


class TeacherDashboardTest(HomeDashboardBaseTest):
    def test_stats_and_sections_render(self):
        exam = self.make_exam(publish=True)
        self._submit_attempt(exam)

        data = HomeDashboardService.build(self.teacher)
        stats = self._stat_map(data)
        self.assertEqual(stats["待批改"], 1)
        self.assertGreaterEqual(stats["进行中考试"], 1)
        self.assertIn("我的班级", stats)
        self.assertTrue(data["todos"])
        self.assertTrue(data["recent_exams"])

        self.client.force_login(self.teacher)
        home = self.client.get(reverse("home"))
        self.assertContains(home, "待办事项")
        self.assertContains(home, "最近考试")
        self.assertContains(home, "待批改")
        # 首页不再是入口卡片墙
        self.assertNotContains(home, "quick-link")

    def test_no_pending_shows_empty_state(self):
        self.make_exam(publish=True)   # 有考试但没人交卷
        data = HomeDashboardService.build(self.teacher)
        self.assertEqual(self._stat_map(data)["待批改"], 0)

    def test_subject_teacher_only_counts_own_subject(self):
        """有任教学科的教师，工作台只统计自己学科的考试。"""
        self.make_exam(publish=True)   # subject = SUBJECT（数据结构）
        other = User.objects.create_user(
            username="home_os_t", email="home_os_t@example.com",
            password="Exam@123456", role=Role.TEACHER, subject="操作系统",
        )
        data = HomeDashboardService.build(other)
        stats = self._stat_map(data)
        self.assertEqual(stats["进行中考试"], 0)
        self.assertEqual(data["recent_exams"], [])


class StudentDashboardTest(HomeDashboardBaseTest):
    def test_latest_graded_attempt_shows(self):
        exam = self.make_exam(publish=True)
        self._submit_attempt(
            exam, status=AttemptStatus.AUTO_GRADED, score=Decimal("12"),
        )

        data = HomeDashboardService.build(self.student)
        self.assertIsNotNone(data["latest"])
        self.assertEqual(data["latest"]["score"], 12.0)
        self.assertFalse(data["latest"]["graded"])
        self.assertTrue(data["todos"])
        self.assertEqual(data["stats"][0]["label"], "待考")

        self.client.force_login(self.student)
        home = self.client.get(reverse("home"))
        self.assertContains(home, "待办事项")
        self.assertContains(home, "最近一次成绩")
        self.assertContains(home, "快捷动作")
        self.assertNotContains(home, "quick-link")

    def test_in_progress_attempt_is_top_todo(self):
        exam = self.make_exam(publish=True)
        self.start(exam)   # 不交卷 → 进行中

        data = HomeDashboardService.build(self.student)
        self.assertEqual(data["stats"][1]["label"], "进行中")
        self.assertEqual(data["stats"][1]["value"], 1)
        self.assertEqual(data["todos"][0]["badge"], "继续答题")

    def test_no_data_student_gets_empty_state(self):
        data = HomeDashboardService.build(self.student_b)
        self.assertIsNone(data["latest"])
        self.assertEqual(data["todos"], [])
        self.client.force_login(self.student_b)
        home = self.client.get(reverse("home"))
        self.assertContains(home, "暂无待办")


class HomeDashboardScopeTest(HomeDashboardBaseTest):
    def test_admin_has_no_dashboard(self):
        """管理员默认被重定向到 /manage/，工作台不为其构建。"""
        self.assertEqual(HomeDashboardService.build(self.admin), {})
        client = Client()
        client.force_login(self.admin)
        resp = client.get(reverse("home"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("manage-dashboard"))

    def test_teacher_nav_is_grouped(self):
        """教师导航按领域分组（功能地图），不再 9 项平铺。"""
        self.client.force_login(self.teacher)
        home = self.client.get(reverse("home"))
        for group in ("教学资源", "组卷与考试", "批改与分析"):
            self.assertContains(home, group)
        # 分组下的具体入口在折叠菜单里仍然可达
        self.assertContains(home, "/question-bank/")
        self.assertContains(home, "/proctoring/")
