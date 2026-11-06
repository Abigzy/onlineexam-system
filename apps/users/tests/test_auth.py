"""
模块一（用户与认证）的接口测试。

运行：
    python manage.py test apps.users -v 2
"""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.common.constants import Role
from apps.users.constants import grade_year_choices

User = get_user_model()

STRONG_PASSWORD = "Exam@123456"
GRADE_YEAR = grade_year_choices()[0][0]  # 当前年，注册必填


class UserModelTest(TestCase):
    """模型层：角色属性、管理器方法。"""

    def setUp(self):
        self.teacher = User.objects.create_user(
            username="t1", email="t1@example.com", password=STRONG_PASSWORD,
            role=Role.TEACHER, real_name="王老师",
        )
        self.student = User.objects.create_user(
            username="s1", email="s1@example.com", password=STRONG_PASSWORD,
            role=Role.STUDENT, student_no="2021001",
        )

    def test_role_properties(self):
        self.assertTrue(self.teacher.is_teacher)
        self.assertFalse(self.teacher.is_student)
        self.assertTrue(self.student.is_student)
        self.assertFalse(self.student.is_teacher)

    def test_manager_queryset(self):
        self.assertEqual(User.objects.teachers().count(), 1)
        self.assertEqual(User.objects.students().count(), 1)
        self.assertEqual(User.objects.search("王老师").count(), 1)
        self.assertEqual(User.objects.search("2021001").count(), 1)

    def test_display_name_fallback(self):
        u = User.objects.create_user(username="noname", email="n@example.com", password=STRONG_PASSWORD)
        self.assertEqual(u.display_name, "noname")


class RegisterApiTest(TestCase):
    """POST /api/v1/auth/register/"""

    url = "/api/v1/auth/register/"

    def _payload(self, **overrides):
        data = {
            "username": "newbie",
            "email": "newbie@example.com",
            "password": STRONG_PASSWORD,
            "password2": STRONG_PASSWORD,
            "role": Role.STUDENT,
            "real_name": "新同学",
            "grade_year": GRADE_YEAR,
        }
        data.update(overrides)
        return data

    def test_register_success(self):
        resp = self.client.post(self.url, self._payload(), content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["username"], "newbie")
        self.assertNotIn("password", body["data"])
        self.assertTrue(User.objects.filter(username="newbie").exists())

    def test_password_mismatch(self):
        resp = self.client.post(
            self.url, self._payload(password2="Different@123"), content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("password2", resp.json()["data"])

    def test_duplicate_username(self):
        User.objects.create_user(username="newbie", email="x@example.com", password=STRONG_PASSWORD)
        resp = self.client.post(self.url, self._payload(), content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("username", resp.json()["data"])

    def test_weak_password_rejected(self):
        resp = self.client.post(
            self.url, self._payload(password="123", password2="123"), content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_cannot_register_as_admin(self):
        resp = self.client.post(self.url, self._payload(role=Role.ADMIN), content_type="application/json")
        self.assertEqual(resp.status_code, 400)


class LoginApiTest(TestCase):
    """POST /api/v1/auth/login/ —— 支持用户名 / 邮箱 / 学号三种登录方式。"""

    url = "/api/v1/auth/login/"

    def setUp(self):
        self.user = User.objects.create_user(
            username="login_user", email="login@example.com",
            password=STRONG_PASSWORD, role=Role.STUDENT, student_no="2021002",
        )

    def _login(self, account):
        return self.client.post(
            self.url, {"username": account, "password": STRONG_PASSWORD}, content_type="application/json"
        )

    def test_login_with_username(self):
        resp = self._login("login_user")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertIn("access", body)
        self.assertIn("refresh", body)
        self.assertEqual(body["user"]["role"], Role.STUDENT)

    def test_login_with_email(self):
        self.assertEqual(self._login("login@example.com").status_code, 200)

    def test_login_with_student_no(self):
        self.assertEqual(self._login("2021002").status_code, 200)

    def test_login_wrong_password(self):
        resp = self.client.post(
            self.url, {"username": "login_user", "password": "Wrong@123456"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 401)

    def test_token_payload_contains_role(self):
        import jwt

        body = self._login("login_user").json()["data"]
        payload = jwt.decode(body["access"], options={"verify_signature": False})
        self.assertEqual(payload["role"], Role.STUDENT)
        self.assertEqual(payload["username"], "login_user")


class MeApiTest(TestCase):
    """GET / PATCH /api/v1/auth/me/ 和改密接口。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username="me_user", email="me@example.com", password=STRONG_PASSWORD,
            role=Role.TEACHER, real_name="原名字",
        )
        token = self.client.post(
            "/api/v1/auth/login/",
            {"username": "me_user", "password": STRONG_PASSWORD},
            content_type="application/json",
        ).json()["data"]["access"]
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_get_me_requires_auth(self):
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 401)

    def test_get_me(self):
        resp = self.client.get("/api/v1/auth/me/", **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["username"], "me_user")

    def test_patch_me(self):
        resp = self.client.patch(
            "/api/v1/auth/me/", {"real_name": "新名字", "school": "计算机学院"},
            content_type="application/json", **self.auth,
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.real_name, "新名字")

    def test_change_password(self):
        resp = self.client.post(
            "/api/v1/auth/change-password/",
            {"old_password": STRONG_PASSWORD, "new_password": "NewPass@2026", "confirm_password": "NewPass@2026"},
            content_type="application/json", **self.auth,
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewPass@2026"))

    def test_change_password_wrong_old(self):
        resp = self.client.post(
            "/api/v1/auth/change-password/",
            {"old_password": "Wrong@123456", "new_password": "NewPass@2026", "confirm_password": "NewPass@2026"},
            content_type="application/json", **self.auth,
        )
        self.assertEqual(resp.status_code, 400)


class LogoutApiTest(TestCase):
    def test_logout_blacklists_refresh(self):
        user = User.objects.create_user(username="lo", email="lo@example.com", password=STRONG_PASSWORD)
        tokens = self.client.post(
            "/api/v1/auth/login/", {"username": user.username, "password": STRONG_PASSWORD},
            content_type="application/json",
        ).json()["data"]
        auth = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access']}"}

        resp = self.client.post(
            "/api/v1/auth/logout/", {"refresh": tokens["refresh"]},
            content_type="application/json", **auth,
        )
        self.assertEqual(resp.status_code, 200)

        # 拉黑之后再用同一个 refresh 换 access，应当失败
        again = self.client.post(
            "/api/v1/auth/refresh/", {"refresh": tokens["refresh"]}, content_type="application/json"
        )
        self.assertEqual(again.status_code, 401)


class UserResourcePermissionTest(TestCase):
    """用户资源仅管理员可访问。"""

    def setUp(self):
        self.student = User.objects.create_user(
            username="s_perm", email="s_perm@example.com", password=STRONG_PASSWORD, role=Role.STUDENT
        )
        self.admin = User.objects.create_superuser(
            username="a_perm", email="a_perm@example.com", password=STRONG_PASSWORD
        )

    def _auth(self, user):
        token = self.client.post(
            "/api/v1/auth/login/", {"username": user.username, "password": STRONG_PASSWORD},
            content_type="application/json",
        ).json()["data"]["access"]
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_student_forbidden(self):
        self.assertEqual(self.client.get("/api/v1/users/", **self._auth(self.student)).status_code, 403)

    def test_admin_can_list(self):
        resp = self.client.get("/api/v1/users/", **self._auth(self.admin))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["count"], 2)

    def test_admin_can_filter_by_role(self):
        resp = self.client.get("/api/v1/users/students/", **self._auth(self.admin))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["count"], 1)

    def test_admin_can_reset_password(self):
        resp = self.client.post(
            f"/api/v1/users/{self.student.pk}/reset-password/", {"new_password": "Reset@2026"},
            content_type="application/json", **self._auth(self.admin),
        )
        self.assertEqual(resp.status_code, 200)
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password("Reset@2026"))


class PageViewTest(TestCase):
    """页面渲染是否正常。"""

    def test_home_page_anonymous(self):
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "AI 在线考试")

    def test_login_page(self):
        self.assertEqual(self.client.get(reverse("login-page")).status_code, 200)

    def test_register_page(self):
        self.assertEqual(self.client.get(reverse("register-page")).status_code, 200)

    def test_profile_requires_login(self):
        resp = self.client.get(reverse("profile-page"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_page_login_flow(self):
        User.objects.create_user(
            username="pageuser", email="page@example.com",
            password=STRONG_PASSWORD, role=Role.STUDENT, real_name="页面用户",
        )
        resp = self.client.post(
            reverse("login-page"), {"username": "pageuser", "password": STRONG_PASSWORD}
        )
        self.assertEqual(resp.status_code, 302)
        home = self.client.get(reverse("home"))
        self.assertContains(home, "页面用户")

    def test_register_page_auto_login_flow(self):
        """注册成功后必须真的建立会话（回归：多认证后端下 login 缺 backend 会抛 ValueError）。"""
        resp = self.client.post(
            reverse("register-page"),
            {
                "username": "newbie",
                "password": STRONG_PASSWORD,
                "password2": STRONG_PASSWORD,
                "email": "newbie@example.com",
                "role": Role.STUDENT,
                "real_name": "新同学",
                "grade_year": GRADE_YEAR,
            },
        )
        self.assertEqual(resp.status_code, 302)
        # 会话已建立 → 能以登录态打开首页
        home = self.client.get(reverse("home"))
        self.assertContains(home, "新同学")
        # 二次设计：首页是「工作台」（待办 + 快捷动作），不再是与导航重复的入口卡片墙
        self.assertContains(home, "待办事项")
        self.assertContains(home, "快捷动作")
        self.assertNotContains(home, "quick-link")

    def test_home_page_dashboard_by_role(self):
        """已登录首页按角色渲染工作台；功能入口只留在导航里（不再重复罗列）。"""
        User.objects.create_user(
            username="t1", email="t1@example.com",
            password=STRONG_PASSWORD, role=Role.TEACHER, real_name="首页教师",
        )
        client = Client()
        client.force_login(User.objects.get(username="t1"))
        home = client.get(reverse("home"))
        # 教师工作台：待办 + 最近考试
        self.assertContains(home, "待办事项")
        self.assertContains(home, "最近考试")
        # 导航仍是功能地图（分组入口），但首页不再复制这些卡片
        self.assertContains(home, "批改与分析")
        self.assertNotContains(home, "quick-link")

    def test_health_endpoint(self):
        resp = self.client.get(reverse("health"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["status"], "ok")
