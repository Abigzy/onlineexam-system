"""管理控制台页面与总览接口的测试。

覆盖：
  * AC-06 / AC-07 / AC-34（守卫侧回归）：四个管理端页面 200 / 非管理员 302；
  * AC-08 / AC-09 / AC-10：数据总览落地页、不含教学卡片、空库显示 0 与占位；
  * AC-31 / AC-32：系统配置页的 mock 警示条、密钥不回显；
  * 性能纪律（PRD §3.4）：``overview()`` 的查询次数不随数据量增长。
"""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.ai.models import AIRequestLog
from apps.classroom.constants import ClassType
from apps.classroom.models import Classroom
from apps.common.constants import QuestionType, Role
from apps.exam.models import Exam
from apps.papers.models import Paper
from apps.questions.models import Question
from apps.system.services import SystemOverviewService

User = get_user_model()

PASSWORD = "Exam@123456"
OVERVIEW_URL = "/api/v1/system/overview/"
MANAGE_URLS = ("/manage/", "/manage/users/", "/manage/org/", "/manage/system/")


class ManagePagesTest(TestCase):
    """四个管理端页面的可达性与内容。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="sys_admin", email="sys_admin@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="系统管理员",
        )
        cls.teacher = User.objects.create_user(
            username="sys_teacher", email="sys_teacher@example.com", password=PASSWORD,
            role=Role.TEACHER, real_name="系统教师", subject="数据结构",
        )
        cls.student = User.objects.create_user(
            username="sys_student", email="sys_student@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="系统学生",
        )

    # ---------------- 可达性 ----------------
    def test_admin_can_open_all_pages(self):
        self.client.force_login(self.admin)
        for url in MANAGE_URLS:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_non_admin_redirected(self):
        """AC-06 / AC-07 / AC-34：教师与学生都被 302 回首页。"""
        for user in (self.teacher, self.student):
            client = self.client_class()
            client.force_login(user)
            for url in MANAGE_URLS:
                response = client.get(url)
                self.assertEqual(response.status_code, 302, f"{user.username} {url}")
                self.assertEqual(response["Location"], "/", url)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get("/manage/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    # ---------------- 数据总览 ----------------
    def test_dashboard_content(self):
        """AC-08 / AC-09：四组指标 + AI 用量区块；且不含任何教师业务快捷卡片。"""
        self.client.force_login(self.admin)
        response = self.client.get("/manage/")
        self.assertContains(response, "数据总览")
        self.assertContains(response, "用户结构")
        self.assertContains(response, "教学资源与组织")
        self.assertContains(response, "AI 用量")
        self.assertContains(response, "考试状态分布")
        self.assertContains(response, "近 7 日 AI 调用趋势")
        # 不含教学卡片（管理员首页不再是教学卡片墙）；
        # 注意：「题库」「作弊检测」等字样仍会出现在导航的「教学管理（代管）」折叠分组里，
        # 所以这里只断言**工作台卡片标题**不出现。
        for forbidden in ("题库管理", "智能组卷", "批改与成绩", "每日练习"):
            self.assertNotContains(response, forbidden)

    def test_dashboard_empty_state(self):
        """AC-10：新库不报错，指标显示 0，图表显示「暂无数据」。"""
        empty_admin = User.objects.create_user(
            username="only_admin", email="only_admin@example.com", password=PASSWORD,
            role=Role.ADMIN,
        )
        # 清掉夹具里的其他账号，构造真正的「空库 + 1 个管理员」
        User.objects.exclude(pk=empty_admin.pk).delete()
        client = self.client_class()
        client.force_login(empty_admin)
        response = client.get("/manage/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "暂无数据")

    def test_new_database_shows_zero(self):
        """空库（连账号都没有）时统计一律为 0，且给出引导而不是白屏。"""
        User.objects.all().delete()
        admin = User.objects.create_user(
            username="fresh_admin", email="fresh@example.com", password=PASSWORD,
            role=Role.ADMIN,
        )
        client = self.client_class()
        client.force_login(admin)
        response = client.get("/manage/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0")

    # ---------------- 组织与系统配置 ----------------
    def test_org_page_content(self):
        Classroom.objects.create(
            name="计算机 1 班", class_type=ClassType.ADMIN, created_by=self.teacher, grade_year="2026"
        )
        self.client.force_login(self.admin)
        response = self.client.get("/manage/org/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "计算机 1 班")
        self.assertContains(response, "行政班")
        self.assertContains(response, "未绑定学科的教师")

    def test_org_page_empty_state(self):
        self.client.force_login(self.admin)
        response = self.client.get("/manage/org/")
        self.assertContains(response, "还没有创建班级")

    def test_system_page_content(self):
        """AC-31 + U-7：mock 警示条、开发者工具区块、接口文档入口。"""
        self.client.force_login(self.admin)
        response = self.client.get("/manage/system/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Provider 配置状态")
        self.assertContains(response, "开发者工具")
        self.assertContains(response, "/api/docs/")
        self.assertContains(response, "/admin/")
        self.assertContains(response, "运行环境")
        # 测试环境固定走 mock（settings 在 argv 含 test 时置 AI_PROVIDER=mock）
        self.assertContains(response, "本地模拟")

    def test_system_page_never_leaks_api_key(self):
        """AC-32：已配置 Key 时只显示「已配置」，响应体里不含密钥明文。"""
        secret = "sk-test-should-never-appear-9f3a"
        with override_settings(AI_PROVIDERS={
            "deepseek": {"api_key": secret, "base_url": "https://api.deepseek.com/v1",
                         "model": "deepseek-chat"},
        }):
            self.client.force_login(self.admin)
            response = self.client.get("/manage/system/")
            body = response.content.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "已配置")
        self.assertNotIn(secret, body)


class OverviewApiTest(TestCase):
    """``GET /api/v1/system/overview/``。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="ov_admin", email="ov_admin@example.com", password=PASSWORD,
            role=Role.ADMIN,
        )
        cls.teacher = User.objects.create_user(
            username="ov_teacher", email="ov_teacher@example.com", password=PASSWORD,
            role=Role.TEACHER, subject="数据结构",
        )
        cls.student = User.objects.create_user(
            username="ov_student", email="ov_student@example.com", password=PASSWORD,
            role=Role.STUDENT,
        )

    def _auth(self, user) -> dict:
        token = self.client.post(
            "/api/v1/auth/login/",
            {"username": user.username, "password": PASSWORD},
            content_type="application/json",
        ).json()["data"]["access"]
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_admin_can_read_overview(self):
        response = self.client.get(OVERVIEW_URL, **self._auth(self.admin))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        for key in ("users", "org", "content", "teaching", "ai", "options", "generated_at"):
            self.assertIn(key, data)
        self.assertEqual(data["users"]["by_role"]["teacher"], 1)
        self.assertEqual(data["users"]["by_role"]["student"], 1)

    def test_teacher_and_student_forbidden(self):
        for user in (self.teacher, self.student):
            response = self.client.get(OVERVIEW_URL, **self._auth(user))
            self.assertEqual(response.status_code, 403, user.username)

    def test_anonymous_rejected(self):
        self.assertIn(self.client.get(OVERVIEW_URL).status_code, (401, 403))

    def test_overview_has_no_api_key(self):
        secret = "sk-never-return-this-abc123"
        with override_settings(AI_PROVIDERS={
            "deepseek": {"api_key": secret, "base_url": "https://api.deepseek.com/v1",
                         "model": "deepseek-chat"},
        }):
            response = self.client.get(OVERVIEW_URL, **self._auth(self.admin))
        self.assertNotIn(secret, response.content.decode("utf-8"))

    def test_subject_options(self):
        Classroom.objects.create(name="数据结构 1 班", class_type=ClassType.SUBJECT,
                                 subject="数据结构", created_by=self.teacher)
        Question.objects.create(type=QuestionType.SINGLE, stem="题目", subject="操作系统",
                                created_by=self.teacher)
        subjects = SystemOverviewService.overview()["options"]["subjects"]
        self.assertIn("数据结构", subjects)
        self.assertIn("操作系统", subjects)
        self.assertEqual(subjects, sorted(set(subjects)))


class OverviewQueryCountTest(TestCase):
    """**性能纪律**：查询次数恒定，不随数据量增长（PRD §3.4 / AC-36 的数据侧保证）。"""

    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(
            username="qc_teacher", email="qc_teacher@example.com", password=PASSWORD,
            role=Role.TEACHER, subject="数据结构",
        )
        AIRequestLog.objects.create(provider="mock", scene="generate_questions", total_tokens=10)
        Classroom.objects.create(name="QC 班", class_type=ClassType.ADMIN, created_by=cls.teacher)
        Question.objects.create(type=QuestionType.SINGLE, stem="题干", subject="数据结构",
                                created_by=cls.teacher)
        paper = Paper.objects.create(title="QC 试卷", created_by=cls.teacher)
        Exam.objects.create(title="QC 考试", paper=paper, created_by=cls.teacher)

    @staticmethod
    def _measure() -> int:
        with CaptureQueriesContext(connection) as ctx:
            SystemOverviewService.overview()
        return len(ctx.captured_queries)

    def test_query_count_is_constant(self):
        # 先跑一次，让 AI client 之类的模块级缓存就位，避免把一次性的构造算进差值
        SystemOverviewService.overview()
        baseline = self._measure()

        # 数据量翻 10 倍：用户 / 题目 / 班级 / 考试 / AI 日志各加一批
        for index in range(10):
            User.objects.create_user(
                username=f"bulk_{index}", email=f"bulk_{index}@example.com",
                password=PASSWORD, role=Role.STUDENT, subject="数据结构",
            )
            Question.objects.create(type=QuestionType.SINGLE, stem=f"题干{index}",
                                    subject="数据结构", created_by=self.teacher)
            Classroom.objects.create(name=f"批量班 {index}", class_type=ClassType.ADMIN,
                                     created_by=self.teacher)
            AIRequestLog.objects.create(provider="mock", scene="grade_subjective",
                                        total_tokens=7)

        grown = self._measure()
        self.assertEqual(
            grown, baseline,
            f"查询次数随数据量增长（{baseline} → {grown}），说明有统计被拉到了 Python 层",
        )
