"""QA 独立验收测试：权限红线、总览正确性与导航分流。

本文件由 QA 独立编写，**不复用**工程师用例的任何断言，只从「外部可观测行为」
出发做黑盒校验，目标是证伪而不是复述：

  * A 组权限红线：非管理员进 ``/manage/*`` 必须 302 且不泄露内容；
    学生进教师业务页必须仍是 403（不能被 mixin 改成 302）；
    学生进 ``/classrooms/`` 必须 200（不能被误伤）；
    总览接口与用户接口的角色边界。
  * C-14 / C-15：数据总览的数字与库内真实数量逐项一致；``overview()`` 的
    查询条数不随数据量增长。
  * D-16 / D-17：导航按角色分流；管理员进教师页有代管提示条，教师没有。
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Q
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ai.models import AIRequestLog
from apps.classroom.constants import ClassType
from apps.classroom.models import Classroom
from apps.common.constants import ExamStatus, QuestionType, Role
from apps.exam.models import Exam
from apps.papers.models import Paper
from apps.questions.models import Question
from apps.system.services import SystemOverviewService

User = get_user_model()

PASSWORD = "Exam@2026518"
OVERVIEW_URL = "/api/v1/system/overview/"
MANAGE_PAGES = (
    "/manage/",
    "/manage/users/",
    "/manage/users/new/",
    "/manage/org/",
    "/manage/system/",
)
TEACHER_PAGES = (
    "/question-bank/",
    "/question-bank/new/",
    "/question-bank/ai-generate/",
    "/knowledge-points/",
    "/papers/",
    "/papers/new/",
    "/exams/",
    "/grading/",
    "/analytics/",
    "/proctoring/",
)
FORBIDDEN_MARKERS = ("数据总览", "用户管理", "重置密码", "系统配置", "组织与基础数据")


def api_client(user=None):
    """构造一个独立的 API 客户端；传入 user 时直接跳过认证握手。"""
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


class RoleFixtureMixin:
    """三类角色的最小夹具（各测试类共用，避免逐条重复建号）。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="qa_admin", email="qa_admin@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="QA 管理员",
        )
        cls.teacher = User.objects.create_user(
            username="qa_teacher", email="qa_teacher@example.com", password=PASSWORD,
            role=Role.TEACHER, real_name="QA 教师", subject="数据结构",
        )
        cls.student = User.objects.create_user(
            username="qa_student", email="qa_student@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="QA 学生", student_no="QA2026001",
        )


# ======================================================================
# A. 权限红线
# ======================================================================
class ManageConsoleRedLineTest(RoleFixtureMixin, TestCase):
    """A-1 / A-2 / A-3：管理端 302、教师业务页 403、班级页对学生放行。"""

    def _login(self, user):
        client = self.client_class()
        client.force_login(user)
        return client

    def test_teacher_and_student_302_without_leaking_manage_pages(self):
        """AC-06 / AC-07：非管理员访问任意 /manage/* 都是 302 到首页，响应体为空。"""
        for user in (self.teacher, self.student):
            client = self._login(user)
            for url in MANAGE_PAGES:
                with self.subTest(role=user.role, url=url):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 302, f"{user.role} 访问 {url} 未被拦截")
                    self.assertEqual(response["Location"], "/", f"{url} 未重定向到首页")
                    body = response.content.decode("utf-8")
                    self.assertEqual(body, "", f"{url} 的 302 响应体不为空，可能泄露内容")

    def test_redirect_carries_admin_only_message(self):
        """拒绝后必须给出「该区域仅管理员可访问」的提示，而不是白页或静默跳转。"""
        client = self._login(self.student)
        response = client.get("/manage/users/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "该区域仅管理员可访问")

    def test_teacher_post_to_create_page_is_blocked(self):
        """守卫拦的是**动作**而不只是 GET：教师 POST 建号页不得建出账号。"""
        before = User.objects.count()
        client = self._login(self.teacher)
        response = client.post("/manage/users/new/", {
            "username": "qa_sneaky", "email": "sneaky@example.com",
            "real_name": "越权用户", "role": Role.STUDENT,
            "password": PASSWORD, "password2": PASSWORD,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.count(), before, "教师通过 POST 建出了账号")
        self.assertFalse(User.objects.filter(username="qa_sneaky").exists())

    def test_anonymous_manage_page_redirects_to_login(self):
        for url in MANAGE_PAGES:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response["Location"])

    def test_student_still_403_on_teacher_pages(self):
        """A-2：加 mixin 之后，学生访问教师业务页仍必须是 403，不能变 302。"""
        client = self._login(self.student)
        for url in TEACHER_PAGES:
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(
                    response.status_code, 403,
                    f"学生访问 {url} 期望 403，实际 {response.status_code}",
                )

    def test_student_can_still_open_own_classrooms(self):
        """A-3：学生必须能看「我的班级」，不能被收成仅教师可见。"""
        client = self._login(self.student)
        response = client.get("/classrooms/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "我的班级")

    def test_teacher_can_still_open_teacher_pages(self):
        """反向保护：给教师页加守卫不能把合法教师一起挡在门外。"""
        client = self._login(self.teacher)
        for url in TEACHER_PAGES:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200, url)

    def test_admin_can_open_every_teacher_page(self):
        """管理员代管：14 个教师页 + 2 个班级页都应可进入。"""
        client = self._login(self.admin)
        for url in TEACHER_PAGES + ("/classrooms/",):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200, url)


class ApiRoleBoundaryTest(RoleFixtureMixin, TestCase):
    """A-4 / A-5：接口层的角色边界（403 而不是 200 / 302）。"""

    def test_overview_api_role_matrix(self):
        """AC-08：总览接口只对管理员开放。"""
        self.assertEqual(api_client(self.admin).get(OVERVIEW_URL).status_code, 200)
        self.assertEqual(api_client(self.teacher).get(OVERVIEW_URL).status_code, 403)
        self.assertEqual(api_client(self.student).get(OVERVIEW_URL).status_code, 403)
        self.assertEqual(api_client().get(OVERVIEW_URL).status_code, 401)

    def test_user_list_forbidden_for_non_admin(self):
        for user in (self.teacher, self.student):
            with self.subTest(role=user.role):
                response = api_client(user).get("/api/v1/users/")
                self.assertEqual(response.status_code, 403)

    def test_dangerous_actions_forbidden_for_non_admin(self):
        """重置密码 / 停用两个危险动作，教师与学生都不得越权。"""
        target = self.student
        for user in (self.teacher, self.student):
            for action in ("reset-password", "toggle-active"):
                with self.subTest(role=user.role, action=action):
                    response = api_client(user).post(
                        f"/api/v1/users/{target.pk}/{action}/", {}, format="json"
                    )
                    self.assertEqual(response.status_code, 403)
        target.refresh_from_db()
        self.assertTrue(target.is_active, "越权请求竟然改动了账号状态")


# ======================================================================
# C-14 / C-15：数据总览的数字与性能
# ======================================================================
class DashboardNumberAccuracyTest(RoleFixtureMixin, TestCase):
    """C-14：页面上的每个数字都必须等于库内真实数量（用 ORM 独立复算）。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher_no_subject = User.objects.create_user(
            username="qa_teacher2", email="qa_teacher2@example.com", password=PASSWORD,
            role=Role.TEACHER, real_name="QA 无学科教师",
        )
        cls.inactive_student = User.objects.create_user(
            username="qa_student_off", email="qa_student_off@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="QA 停用学生", is_active=False,
        )

        cls.admin_class = Classroom.objects.create(
            name="计科 2101", class_type=ClassType.ADMIN, head_teacher=cls.teacher,
            subject="数据结构",
        )
        cls.archived_class = Classroom.objects.create(
            name="软工 2002", class_type=ClassType.ADMIN, is_active=False,
        )
        Classroom.objects.create(
            name="数据结构学科班", class_type=ClassType.SUBJECT, admin_class=cls.admin_class,
        )

        Question.objects.create(type=QuestionType.SINGLE, stem="QA 单选题", subject="数据结构")
        Question.objects.create(type=QuestionType.SINGLE, stem="QA 单选题 2", subject="数据结构")
        Question.objects.create(
            type=QuestionType.SHORT, stem="QA 简答题", subject="操作系统",
            is_active=False,
        )

        cls.paper = Paper.objects.create(title="QA 期中卷", subject="数据结构")
        Paper.objects.create(title="QA 已发布卷", subject="数据结构",
                             status="published")

        cls.exam = Exam.objects.create(title="QA 期末考试", paper=cls.paper,
                                       status=ExamStatus.PUBLISHED)
        Exam.objects.create(title="QA 草稿考试", paper=cls.paper, status=ExamStatus.DRAFT)

        AIRequestLog.objects.create(provider="mock", scene="qa", total_tokens=120)
        AIRequestLog.objects.create(provider="mock", scene="qa", success=False,
                                    total_tokens=30)

    def test_dashboard_numbers_match_database(self):
        client = self.client_class()
        client.force_login(self.admin)
        response = client.get("/manage/")
        self.assertEqual(response.status_code, 200)
        overview = response.context["overview"]

        # --- 用户结构 ---
        self.assertEqual(overview["users"]["total"], User.objects.count())
        self.assertEqual(
            overview["users"]["by_role"]["admin"],
            User.objects.filter(Q(role=Role.ADMIN) | Q(is_superuser=True)).count(),
        )
        self.assertEqual(
            overview["users"]["by_role"]["teacher"],
            User.objects.filter(role=Role.TEACHER).count(),
        )
        self.assertEqual(
            overview["users"]["by_role"]["student"],
            User.objects.filter(role=Role.STUDENT).count(),
        )
        self.assertEqual(
            overview["users"]["inactive"], User.objects.filter(is_active=False).count()
        )
        self.assertEqual(
            overview["users"]["teachers_without_subject"],
            User.objects.filter(role=Role.TEACHER, subject="").count(),
        )

        # --- 组织 ---
        self.assertEqual(overview["org"]["classrooms"], Classroom.objects.count())
        self.assertEqual(
            overview["org"]["admin_classes"],
            Classroom.objects.filter(class_type=ClassType.ADMIN).count(),
        )
        self.assertEqual(
            overview["org"]["subject_classes"],
            Classroom.objects.filter(class_type=ClassType.SUBJECT).count(),
        )
        self.assertEqual(
            overview["org"]["archived_classrooms"],
            Classroom.objects.filter(is_active=False).count(),
        )

        # --- 教学资源 ---
        self.assertEqual(overview["content"]["questions"], Question.objects.count())
        self.assertEqual(
            overview["content"]["questions_active"],
            Question.objects.filter(is_active=True).count(),
        )
        self.assertEqual(overview["content"]["papers"], Paper.objects.count())
        self.assertEqual(
            overview["content"]["papers_published"],
            Paper.objects.filter(status="published").count(),
        )
        self.assertEqual(
            sum(row["total"] for row in overview["content"]["questions_by_type"]),
            Question.objects.count(),
        )
        self.assertEqual(
            len(overview["content"]["questions_by_type"]), len(QuestionType.values)
        )

        # --- 考试 ---
        self.assertEqual(overview["teaching"]["exams"], Exam.objects.count())
        self.assertEqual(
            sum(row["total"] for row in overview["teaching"]["exams_by_status"]),
            Exam.objects.count(),
        )
        self.assertEqual(
            len(overview["teaching"]["exams_by_status"]), len(ExamStatus.values)
        )

        # --- AI 用量 ---
        since = timezone.now() - timedelta(days=overview["recent_days"])
        self.assertEqual(
            overview["ai"]["calls_recent"],
            AIRequestLog.objects.filter(created_at__gte=since).count(),
        )
        self.assertEqual(overview["ai"]["failed_recent"], 1)
        self.assertEqual(overview["ai"]["tokens_recent"], 150)
        self.assertEqual(len(overview["ai"]["trend"]), overview["recent_days"])

    def test_overview_api_and_page_share_same_numbers(self):
        """AC-36 的反面：页面与接口不能各数一套。"""
        client = self.client_class()
        client.force_login(self.admin)
        page_overview = client.get("/manage/").context["overview"]
        api_overview = api_client(self.admin).get(OVERVIEW_URL).json()["data"]
        for key in ("users", "org", "content", "teaching"):
            self.assertEqual(page_overview[key], api_overview[key], f"{key} 两处口径不一致")


class OverviewPerformanceTest(RoleFixtureMixin, TestCase):
    """C-15：``overview()`` 的查询数必须与数据量无关（防 N+1）。"""

    @staticmethod
    def _measure() -> int:
        with CaptureQueriesContext(connection) as captured:
            SystemOverviewService.overview()
        return len(captured)

    def test_query_count_does_not_grow_with_data(self):
        baseline = self._measure()

        # 灌入 40 个用户 / 30 道题 / 20 个班级 / 10 条 AI 日志
        User.objects.bulk_create([
            User(username=f"qa_bulk_{i}", email=f"qa_bulk_{i}@example.com",
                 role=Role.STUDENT, subject="数据结构")
            for i in range(40)
        ])
        Question.objects.bulk_create([
            Question(type=QuestionType.SINGLE, stem=f"QA 批量题 {i}", subject="数据结构")
            for i in range(30)
        ])
        Classroom.objects.bulk_create([
            Classroom(name=f"QA 批量班 {i}", class_type=ClassType.ADMIN) for i in range(20)
        ])
        AIRequestLog.objects.bulk_create([
            AIRequestLog(provider="mock", scene="qa", total_tokens=1) for _ in range(10)
        ])

        self.assertEqual(
            self._measure(), baseline,
            "overview() 的查询条数随数据量增长，存在 N+1",
        )


# ======================================================================
# D-16 / D-17：导航分流与代管提示
# ======================================================================
class NavigationSplitTest(RoleFixtureMixin, TestCase):
    """D-16：三种角色的导航内容互不串味。"""

    def _html(self, user, url):
        client = self.client_class()
        client.force_login(user)
        response = client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.content.decode("utf-8")

    def test_admin_nav_has_four_manage_entries_and_delegation_group(self):
        html = self._html(self.admin, "/manage/")
        for label in ("数据总览", "用户管理", "组织与基础数据", "系统配置"):
            self.assertIn(label, html)
        self.assertIn("教学管理（代管）", html)
        # 教师业务入口只能出现在折叠组里，不能作为一级 nav-link 出现
        self.assertNotIn('class="nav-link" href="/question-bank/"', html)

    def test_teacher_nav_has_no_manage_entry(self):
        html = self._html(self.teacher, "/question-bank/")
        self.assertNotIn("/manage/", html)
        for label in ("数据总览", "用户管理", "系统配置", "组织与基础数据"):
            self.assertNotIn(label, html)
        for label in ("题库", "AI 出题", "知识点", "试卷", "考试", "批改", "学情",
                      "作弊检测", "班级"):
            self.assertIn(label, html)

    def test_student_nav_only_student_entries(self):
        html = self._html(self.student, "/my-exams/")
        self.assertNotIn("/manage/", html)
        self.assertNotIn("/question-bank/", html)
        self.assertNotIn("/proctoring/", html)
        for label in ("我的考试", "错题本", "我的学情", "每日练习", "我的班级"):
            self.assertIn(label, html)

    def test_admin_home_lands_on_manage_console(self):
        client = self.client_class()
        client.force_login(self.admin)
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/")

    def test_teacher_and_student_home_unaffected(self):
        # 二次设计后首页是工作台：教师/学生看到的是「待办事项」，不再是功能入口卡片
        for user, marker in ((self.teacher, "待办事项"), (self.student, "待办事项")):
            with self.subTest(role=user.role):
                client = self.client_class()
                client.force_login(user)
                response = client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, marker)


class DelegationBannerTest(RoleFixtureMixin, TestCase):
    """D-17：代管提示条只属于管理员。"""

    DELEGATION_TEXT = "你正在以管理员身份代管教学功能"

    def test_admin_sees_banner_on_teacher_page(self):
        client = self.client_class()
        client.force_login(self.admin)
        for url in ("/question-bank/", "/papers/", "/classrooms/"):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, self.DELEGATION_TEXT)

    def test_teacher_never_sees_banner(self):
        client = self.client_class()
        client.force_login(self.teacher)
        for url in ("/question-bank/", "/papers/", "/classrooms/"):
            with self.subTest(url=url):
                self.assertNotContains(client.get(url), self.DELEGATION_TEXT)

    def test_student_never_sees_banner(self):
        client = self.client_class()
        client.force_login(self.student)
        self.assertNotContains(client.get("/classrooms/"), self.DELEGATION_TEXT)
