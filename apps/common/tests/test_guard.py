"""页面守卫与停用踢下线的测试。

覆盖：
  * AC-06 / AC-07：``/manage/*`` 对非管理员是「302 首页 + 提示」，不是 403 白页；
  * AC-25：账号被停用后，已登录的页面会话下一次请求被强制登出并提示；
  * AC-34（守卫侧）：学生访问管理端被拒；
  * 回归护栏：``RoleRequiredMixin`` 的默认拒绝形态仍是 403（教师业务页不受本次改动影响）；
  * JWT 请求不受中间件影响。
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.common.constants import Role

User = get_user_model()

PASSWORD = "Exam@123456"
MANAGE_DENIED_MESSAGE = "该区域仅管理员可访问"
# 与登录接口 / 页面同文案（P4 统一，见 apps/common/middleware.py 顶部说明）
INACTIVE_MESSAGE = "该账号已被停用，请联系管理员"


class GuardBaseTest(TestCase):
    """公共夹具：管理员 / 教师 / 学生三种账号。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="guard_admin", email="guard_admin@example.com",
            password=PASSWORD, role=Role.ADMIN, real_name="管理老师",
        )
        cls.teacher = User.objects.create_user(
            username="guard_teacher", email="guard_teacher@example.com",
            password=PASSWORD, role=Role.TEACHER, real_name="守门教师",
        )
        cls.student = User.objects.create_user(
            username="guard_student", email="guard_student@example.com",
            password=PASSWORD, role=Role.STUDENT, real_name="守门学生",
        )


class AdminConsoleGuardTest(GuardBaseTest):
    """``/manage/*`` 的角色守卫。"""

    def test_anonymous_redirected_to_login(self):
        """未登录 → /login/?next=（沿用 AccessMixin 默认行为）。"""
        response = self.client.get("/manage/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
        self.assertIn("next=", response["Location"])

    def test_admin_can_open_dashboard(self):
        self.client.force_login(self.admin)
        response = self.client.get("/manage/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "数据总览")

    def test_teacher_redirected_with_message(self):
        """AC-06：教师直达 /manage/users/ → 302 首页 + 提示。"""
        self.client.force_login(self.teacher)
        response = self.client.get("/manage/users/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[0][0], "/")
        self.assertEqual(response.redirect_chain[0][1], 302)
        self.assertContains(response, MANAGE_DENIED_MESSAGE)

    def test_student_redirected_without_content_leak(self):
        """AC-07 / AC-34：学生访问 /manage/ 被重定向，且不泄露页面内容。"""
        self.client.force_login(self.student)
        response = self.client.get("/manage/", follow=True)
        self.assertEqual(response.redirect_chain[0][0], "/")
        self.assertContains(response, MANAGE_DENIED_MESSAGE)
        # 403 白页不应出现；管理端页面正文也不应出现在响应里
        self.assertNotContains(response, "仅管理员可见")

    def test_student_denied_on_every_manage_page(self):
        """四条管理端页面路由对学生的拒绝形态一致。"""
        self.client.force_login(self.student)
        for url in ("/manage/", "/manage/users/", "/manage/org/", "/manage/system/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertEqual(response["Location"], "/", url)

    def test_admin_can_open_all_manage_pages(self):
        self.client.force_login(self.admin)
        for url in ("/manage/", "/manage/users/", "/manage/org/", "/manage/system/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_role_mixin_default_still_403(self):
        """回归护栏：``RoleRequiredMixin`` 默认仍是 403，教师业务页行为不变。"""
        self.client.force_login(self.student)
        for url in ("/question-bank/", "/proctoring/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_teacher_page_ok_for_teacher_and_admin(self):
        for user in (self.teacher, self.admin):
            client = self.client_class()
            client.force_login(user)
            self.assertEqual(client.get("/question-bank/").status_code, 200)


class InactiveSessionGuardTest(GuardBaseTest):
    """停用账号即时踢下线（AC-25）。"""

    def setUp(self):
        self.inactive = User.objects.create_user(
            username="inactive_user", email="inactive@example.com",
            password=PASSWORD, role=Role.STUDENT, real_name="离职学生",
        )
        self.inactive.is_active = False
        self.inactive.save(update_fields=["is_active"])

    def test_inactive_session_kicked_out(self):
        """已登录的页面会话下一次请求 → 登出 + 302 /login/ + 提示。"""
        self.client.force_login(self.inactive)
        response = self.client.get("/profile/", follow=True)
        self.assertEqual(response.redirect_chain[0][0], "/login/")
        self.assertEqual(response.redirect_chain[0][1], 302)
        self.assertContains(response, INACTIVE_MESSAGE)

    def test_inactive_session_is_really_logged_out(self):
        """踢下线之后会话已失效：再访问管理端会回到「未登录」流程。"""
        self.client.force_login(self.inactive)
        self.client.get("/profile/")
        response = self.client.get("/manage/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_inactive_session_api_request_gets_401(self):
        """``/api/`` 的会话请求返回统一响应体的 401，而不是重定向。"""
        self.client.force_login(self.inactive)
        response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body["code"], 40101)
        self.assertEqual(body["message"], INACTIVE_MESSAGE)
        self.assertIsNone(body["data"])

    def test_active_session_unaffected(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get("/profile/").status_code, 200)

    def test_health_endpoint_skipped(self):
        """探活 / 文档等前缀不做复查，避免运维与文档被会话状态牵连。"""
        self.client.force_login(self.inactive)
        self.assertEqual(self.client.get("/api/health/").status_code, 200)

    def test_jwt_request_not_intercepted(self):
        """JWT 请求在中间件阶段 request.user 是 Anonymous，天然不受影响。

        用启用账号取一个 token，再用它请求接口：应正常 200（说明中间件没有误伤 JWT 路径）。
        """
        token = self.client.post(
            "/api/v1/auth/login/",
            {"username": self.student.username, "password": PASSWORD},
            content_type="application/json",
        ).json()["data"]["access"]
        response = self.client.get(
            "/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["username"], self.student.username)
