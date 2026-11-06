"""管理端账号治理接口的新增测试（T02）。

覆盖验收标准：
  * AC-15 新建用户的初始密码必须满足 AUTH_PASSWORD_VALIDATORS；
  * AC-17 超级管理员角色不可降级；
  * AC-18 不允许把「最后一个启用管理员」停用或降级（U-2 口径）；
  * AC-19 重置密码默认生成 12 位强密码，明文只在响应里出现一次；
  * AC-20 手动输入的新密码必须过四项策略；
  * AC-22 重置后旧密码失效、新密码可用；
  * AC-26 / AC-27 停用账号的登录提示可区分且不泄露账号状态；
  * AC-28 停用账号的旧 JWT 下一次请求即 401；
  * AC-30 注册入口只能产出学生账号；
  * AC-37 危险动作只接受 POST，GET 返回 405；
  * U-1 / U-2 的 ``subject`` 字段与学科筛选。

新增用例全部放本文件，**不改动** ``test_auth.py`` 中的既有断言。
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.test import TestCase
from django.urls import reverse

from apps.common.constants import Role
from apps.users.constants import grade_year_choices
from apps.common.exceptions import BusinessError

from apps.users.services import UserService

User = get_user_model()

PASSWORD = "Exam@123456"
STUDENT_URL = "/api/v1/users/"


def _jwt(client, username: str, password: str = PASSWORD) -> str:
    """用账号密码换 access token（JWT 路径）。"""
    response = client.post(
        "/api/v1/auth/login/",
        {"username": username, "password": password},
        content_type="application/json",
    )
    return response.json()["data"]["access"]


class ManageApiBaseTest(TestCase):
    """公共夹具：一个管理员 + 一个教师 + 一个学生。"""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="mgr_admin", email="mgr_admin@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="管理员甲",
        )
        self.teacher = User.objects.create_user(
            username="mgr_teacher", email="mgr_teacher@example.com", password=PASSWORD,
            role=Role.TEACHER, real_name="教师甲", subject="数据结构",
        )
        self.student = User.objects.create_user(
            username="mgr_student", email="mgr_student@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="学生甲", student_no="2026001",
        )

    # ---------------- 便捷方法 ----------------
    def auth(self, user=None) -> dict:
        token = _jwt(self.client, (user or self.admin).username)
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def post_json(self, url: str, data: dict, user=None):
        return self.client.post(url, data, content_type="application/json", **self.auth(user))

    def patch_json(self, url: str, data: dict, user=None):
        return self.client.patch(url, data, content_type="application/json", **self.auth(user))


# ==================================================================
# AC-15：新建用户时的初始密码强度
# ==================================================================
class CreateUserPasswordTest(ManageApiBaseTest):
    """管理员建号的初始密码必须过 AUTH_PASSWORD_VALIDATORS。"""

    def _payload(self, **overrides) -> dict:
        data = {
            "username": "new_teacher",
            "email": "new_teacher@example.com",
            "real_name": "新教师",
            "role": Role.TEACHER,
            "subject": "操作系统",
            "password": PASSWORD,
        }
        data.update(overrides)
        return data

    def test_weak_numeric_password_rejected(self):
        """AC-15：123456 这类弱口令必须被拒，且提示落到「数字 / 常见」。"""
        response = self.post_json(STUDENT_URL, self._payload(password="123456"))
        self.assertEqual(response.status_code, 400)
        message = response.json()["message"]
        self.assertTrue("数字" in message or "常见" in message, message)
        self.assertFalse(User.objects.filter(username="new_teacher").exists())

    def test_strong_password_accepted(self):
        response = self.post_json(STUDENT_URL, self._payload())
        self.assertEqual(response.status_code, 201)
        created = User.objects.get(username="new_teacher")
        self.assertTrue(created.check_password(PASSWORD))
        self.assertEqual(created.subject, "操作系统")
        self.assertTrue(created.is_active)

    def test_create_returns_subject(self):
        """U-1：写序列化器返回 subject，便于管理端列表/表单回显。"""
        response = self.post_json(STUDENT_URL, self._payload(subject="计算机网络"))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["data"]["subject"], "计算机网络")

    def test_duplicate_username_rejected(self):
        """唯一性：用户名已被占用时给字段级错误，不落库。

        提示语由 DRF 的 UniqueValidator 给出（「已存在一位使用该名字的用户。」），
        这里只断言「错误落在 username 字段上」＋400，不锁定具体文案。
        """
        response = self.post_json(
            STUDENT_URL, self._payload(username=self.teacher.username,
                                       email="another@example.com")
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.json()["data"])
        self.assertFalse(User.objects.filter(email="another@example.com").exists())

    def test_duplicate_email_rejected(self):
        response = self.post_json(
            STUDENT_URL, self._payload(email=self.teacher.email)
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json()["data"])

    def test_duplicate_student_no_rejected(self):
        response = self.post_json(
            STUDENT_URL, self._payload(student_no=self.student.student_no)
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("student_no", response.json()["data"])

    def test_username_read_only_after_creation(self):
        """PRD §4.3：用户名创建后不可修改（update 场景忽略该字段）。"""
        response = self.patch_json(f"{STUDENT_URL}{self.teacher.pk}/", {"username": "hacked"})
        self.assertEqual(response.status_code, 200)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.username, "mgr_teacher")


# ==================================================================
# AC-17 / AC-18：角色与「最后一个启用管理员」护栏
# ==================================================================
class AdminGuardTest(ManageApiBaseTest):
    """管理员治理护栏。"""

    def test_superuser_cannot_be_downgraded(self):
        """AC-17：超级管理员角色不可降级。"""
        superuser = User.objects.create_superuser(
            username="root_admin", email="root_admin@example.com", password=PASSWORD,
        )
        response = self.patch_json(f"{STUDENT_URL}{superuser.pk}/", {"role": Role.TEACHER})
        self.assertEqual(response.status_code, 400)
        self.assertIn("超级管理员的角色不可降级", response.json()["message"])
        superuser.refresh_from_db()
        self.assertEqual(superuser.role, Role.ADMIN)

    def test_last_active_admin_cannot_be_downgraded(self):
        """AC-18：仅剩一个启用管理员时，把他降级会被拒绝。"""
        # 夹具里的 mgr_admin 是唯一的管理员，让它自己把自己降级
        response = self.patch_json(f"{STUDENT_URL}{self.admin.pk}/", {"role": Role.TEACHER})
        self.assertEqual(response.status_code, 400)
        self.assertIn("系统至少保留一个启用的管理员", response.json()["message"])
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, Role.ADMIN)

    def test_downgrade_allowed_when_another_admin_exists(self):
        """还有第二个启用管理员时，降级是允许的。"""
        second = User.objects.create_user(
            username="mgr_admin2", email="mgr_admin2@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="管理员乙",
        )
        response = self.patch_json(f"{STUDENT_URL}{second.pk}/", {"role": Role.TEACHER})
        self.assertEqual(response.status_code, 200)
        second.refresh_from_db()
        self.assertEqual(second.role, Role.TEACHER)

    def test_service_guard_rejects_last_admin_deactivation(self):
        """AC-18 的服务层口径：停用最后一个启用管理员 → BusinessError(10012)。"""
        with self.assertRaises(BusinessError) as ctx:
            UserService.assert_admin_survives(self.admin, is_active=False)
        self.assertEqual(ctx.exception.biz_code, 10012)
        self.assertIn("系统至少保留一个启用的管理员", str(ctx.exception.detail))

    def test_service_guard_passes_for_ordinary_user(self):
        """普通账号（教师/学生）的启停不受该护栏影响。"""
        UserService.assert_admin_survives(self.teacher, is_active=False)
        UserService.assert_admin_survives(self.student, role=Role.TEACHER)

    def test_service_guard_passes_when_another_admin_survives(self):
        User.objects.create_user(
            username="mgr_admin3", email="mgr_admin3@example.com", password=PASSWORD,
            role=Role.ADMIN,
        )
        UserService.assert_admin_survives(self.admin, is_active=False)  # 不抛异常即通过

    def test_cannot_deactivate_self(self):
        """不能停用自己（既有 10010 护栏保留）。"""
        response = self.post_json(f"{STUDENT_URL}{self.admin.pk}/toggle-active/", {})
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能停用自己的账号", response.json()["message"])


# ==================================================================
# AC-19 / AC-20 / AC-22：重置密码
# ==================================================================
class ResetPasswordTest(ManageApiBaseTest):
    """重置密码：随机强密码 / 策略校验 / 明文只出现一次。"""

    def test_random_password_is_strong_and_not_in_message(self):
        """AC-19：默认生成 12 位含字母与数字的强密码，且不拼进 message。"""
        response = self.post_json(f"{STUDENT_URL}{self.teacher.pk}/reset-password/", {})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        plain = body["data"]["password"]

        self.assertEqual(len(plain), 12)
        self.assertTrue(any(ch.isalpha() for ch in plain), plain)
        self.assertTrue(any(ch.isdigit() for ch in plain), plain)
        # 剔除易混淆字符
        for confusing in ("0", "O", "1", "l", "I"):
            self.assertNotIn(confusing, plain)
        # 生成物必须能通过系统自己的密码策略（D-3 的核心诉求）
        validate_password(plain, user=self.teacher)
        # 明文不得出现在 message 里（旧实现会二次泄露）
        self.assertEqual(body["message"], "密码已重置")
        self.assertNotIn(plain, body["message"])

        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.check_password(plain))

    def test_two_resets_never_share_a_password(self):
        """P3-2：兜底也必须随机——两次重置不能给出同一个口令。"""
        first = self.post_json(f"{STUDENT_URL}{self.teacher.pk}/reset-password/", {})
        second = self.post_json(f"{STUDENT_URL}{self.student.pk}/reset-password/", {})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(
            first.json()["data"]["password"], second.json()["data"]["password"]
        )

    def test_generator_gives_up_instead_of_reusing_fixed_fallback(self):
        """P3-2：随机生成连续失败时抛 BusinessError，而不是静默返回固定常量。"""
        from unittest.mock import patch

        with patch(
            "apps.users.services.UserService.generate_password", return_value="123456"
        ):
            response = self.post_json(f"{STUDENT_URL}{self.teacher.pk}/reset-password/", {})
        self.assertEqual(response.status_code, 400)
        self.assertIn("无法生成符合密码策略的随机密码", response.json()["message"])
        # 密码未被改动
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.check_password(PASSWORD))

    def test_manual_numeric_password_rejected(self):
        """AC-20：纯数字被拒，提示包含「数字」。"""
        response = self.post_json(
            f"{STUDENT_URL}{self.teacher.pk}/reset-password/", {"new_password": "88888888"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("数字", response.json()["message"])
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.check_password(PASSWORD))

    def test_manual_common_password_rejected(self):
        """AC-20：常见弱口令被拒。"""
        response = self.post_json(
            f"{STUDENT_URL}{self.teacher.pk}/reset-password/", {"new_password": "123456"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("常见", response.json()["message"])

    def test_manual_password_similar_to_username_rejected(self):
        """AC-20：与账号信息高度相似被拒。"""
        target = User.objects.create_user(
            username="zhangsan2026", email="zs@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="张三",
        )
        response = self.post_json(
            f"{STUDENT_URL}{target.pk}/reset-password/", {"new_password": "zhangsan2026"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("相似", response.json()["message"])

    def test_manual_strong_password_accepted(self):
        response = self.post_json(
            f"{STUDENT_URL}{self.teacher.pk}/reset-password/", {"new_password": "Reset@2026"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["password"], "Reset@2026")

    def test_old_password_fails_new_password_works(self):
        """AC-22：重置后用旧密码登录失败，用新密码登录成功。"""
        self.post_json(
            f"{STUDENT_URL}{self.teacher.pk}/reset-password/", {"new_password": "Reset@2026"}
        )
        old = self.client.post(
            "/api/v1/auth/login/",
            {"username": self.teacher.username, "password": PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(old.status_code, 401)

        new = self.client.post(
            "/api/v1/auth/login/",
            {"username": self.teacher.username, "password": "Reset@2026"},
            content_type="application/json",
        )
        self.assertEqual(new.status_code, 200)

    def test_teacher_cannot_reset_password(self):
        """权限边界未变：教师调重置密码接口 → 403。"""
        response = self.post_json(
            f"{STUDENT_URL}{self.student.pk}/reset-password/", {}, user=self.teacher
        )
        self.assertEqual(response.status_code, 403)


# ==================================================================
# AC-26 / AC-27 / AC-28：停用后的登录与鉴权行为
# ==================================================================
class InactiveAccountAuthTest(ManageApiBaseTest):
    """停用账号的提示可区分，但不泄露账号状态。"""

    def setUp(self):
        super().setUp()
        self.inactive = User.objects.create_user(
            username="gone_user", email="gone_user@example.com", password=PASSWORD,
            role=Role.TEACHER, real_name="已停用教师",
        )
        self.inactive.is_active = False
        self.inactive.save(update_fields=["is_active"])

    def test_page_login_with_correct_password_reports_inactive(self):
        """AC-26：账号存在且密码正确 → 明确提示已停用。"""
        response = self.client.post(
            reverse("login-page"), {"username": "gone_user", "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "该账号已被停用，请联系管理员")
        # 没有建立会话
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_page_login_with_wrong_password_keeps_generic_message(self):
        """AC-27：密码错误仍统一提示，不暴露账号是否存在。"""
        response = self.client.post(
            reverse("login-page"), {"username": "gone_user", "password": "Wrong@123456"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "账号或密码错误")
        self.assertNotContains(response, "已被停用")

    def test_page_login_unknown_account_keeps_generic_message(self):
        response = self.client.post(
            reverse("login-page"), {"username": "no_such_user", "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "账号或密码错误")
        self.assertNotContains(response, "已被停用")

    def test_api_login_inactive_with_correct_password(self):
        """接口口径（A+B 修复后）：停用 + 密码正确 → 停用提示 + 业务码 40101。

        业务码能真的透传，是因为这里抛的是项目自己的 ``BusinessError``，
        ``unified_exception_handler`` 会把 ``biz_code`` 原样放进响应体；
        用 DRF 的 ``AuthenticationFailed("...", code=40101)`` 是透不出来的
        （它的第二个位置参数是「错误标识字符串」，不是业务码）。
        """
        response = self.client.post(
            "/api/v1/auth/login/",
            {"username": "gone_user", "password": PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body["code"], 40101)
        self.assertEqual(body["message"], "该账号已被停用，请联系管理员")

    def test_api_login_wrong_password_and_unknown_account(self):
        """接口口径（A+B 修复后）：密码错与账号不存在完全不可区分（40100 + 通用文案）。"""
        wrong = self.client.post(
            "/api/v1/auth/login/",
            {"username": "gone_user", "password": "Wrong@123456"},
            content_type="application/json",
        ).json()
        unknown = self.client.post(
            "/api/v1/auth/login/",
            {"username": "no_such_account", "password": PASSWORD},
            content_type="application/json",
        ).json()
        for body in (wrong, unknown):
            self.assertEqual(body["code"], 40100)
            self.assertEqual(body["message"], "账号或密码错误，请重新输入")
        # 停用账号 + 错误密码 不得暴露停用状态
        self.assertNotIn("停用", wrong["message"])
        self.assertNotIn("停用", unknown["message"])

    def test_old_jwt_rejected_after_deactivation(self):
        """AC-28：停用后旧 access token 下一次请求即 401。"""
        token = _jwt(self.client, self.student.username)
        auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/v1/auth/me/", **auth).status_code, 200)

        self.post_json(f"{STUDENT_URL}{self.student.pk}/toggle-active/", {})
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)

        self.assertEqual(self.client.get("/api/v1/auth/me/", **auth).status_code, 401)

    def test_toggle_active_twice_returns_to_active(self):
        """停用 → 启用 是可逆操作，返回体带上最新状态。"""
        response = self.post_json(f"{STUDENT_URL}{self.student.pk}/toggle-active/", {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["is_active"])
        self.assertEqual(response.json()["message"], "账号已停用")

        again = self.post_json(f"{STUDENT_URL}{self.student.pk}/toggle-active/", {})
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["data"]["is_active"])
        self.assertEqual(again.json()["message"], "账号已启用")


# ==================================================================
# AC-30：注册入口只能产出学生
# ==================================================================
class RegisterConvergeTest(TestCase):
    """注册收敛（D-5）：接口与页面都只能创建学生账号。"""

    def _payload(self, **overrides) -> dict:
        data = {
            "username": "reg_newbie",
            "email": "reg_newbie@example.com",
            "password": PASSWORD,
            "password2": PASSWORD,
            "role": Role.STUDENT,
            "real_name": "注册同学",
            "grade_year": grade_year_choices()[0][0],
        }
        data.update(overrides)
        return data

    def test_api_register_teacher_rejected(self):
        """AC-30：接口传 role=teacher → 400，且不创建账号。"""
        response = self.client.post(
            "/api/v1/auth/register/", self._payload(role=Role.TEACHER),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("role", response.json()["data"])
        self.assertFalse(User.objects.filter(username="reg_newbie").exists())

    def test_api_register_student_ok(self):
        """回归：注册学生仍然成功。"""
        response = self.client.post(
            "/api/v1/auth/register/", self._payload(), content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        created = User.objects.get(username="reg_newbie")
        self.assertEqual(created.role, Role.STUDENT)

    def test_page_register_forces_student(self):
        """AC-29 / AC-30：页面表单强制学生，即便手工提交 teacher 也不创建账号。"""
        response = self.client.post(
            reverse("register-page"), self._payload(role=Role.TEACHER)
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="reg_newbie").exists())

    def test_register_page_has_no_teacher_option(self):
        """AC-29：注册页不再出现「教师」角色选项。"""
        response = self.client.get(reverse("register-page"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'value="teacher"')
        self.assertContains(response, "教师账号由管理员创建")


# ==================================================================
# AC-37：危险动作只接受 POST
# ==================================================================
class DangerousActionMethodTest(ManageApiBaseTest):
    """危险操作 GET → 405，且不产生任何状态变更。"""

    def test_reset_password_rejects_get(self):
        url = f"{STUDENT_URL}{self.teacher.pk}/reset-password/"
        response = self.client.get(url, **self.auth())
        self.assertEqual(response.status_code, 405)
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.check_password(PASSWORD))

    def test_toggle_active_rejects_get(self):
        url = f"{STUDENT_URL}{self.student.pk}/toggle-active/"
        response = self.client.get(url, **self.auth())
        self.assertEqual(response.status_code, 405)
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)


# ==================================================================
# U-1：subject 读字段与筛选
# ==================================================================
class UserSubjectTest(ManageApiBaseTest):
    """``subject`` 的读字段与筛选（U-1）。"""

    def test_user_list_exposes_subject(self):
        response = self.client.get(f"{STUDENT_URL}{self.teacher.pk}/", **self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["subject"], "数据结构")

    def test_filter_users_by_subject(self):
        User.objects.create_user(
            username="mgr_teacher2", email="mgr_teacher2@example.com", password=PASSWORD,
            role=Role.TEACHER, subject="计算机网络",
        )
        response = self.client.get(f"{STUDENT_URL}?subject=数据结构", **self.auth())
        self.assertEqual(response.status_code, 200)
        body = response.json()["data"]
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["username"], "mgr_teacher")

    def test_admin_can_update_subject(self):
        response = self.patch_json(f"{STUDENT_URL}{self.teacher.pk}/", {"subject": "操作系统"})
        self.assertEqual(response.status_code, 200)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.subject, "操作系统")

    def test_student_serializer_read_only_subject_untouched(self):
        """读序列化器里的 subject 是只读的：自助改资料不会顺手改学科。"""
        token = _jwt(self.client, self.teacher.username)
        self.client.patch(
            "/api/v1/auth/me/", {"subject": "试图篡改"},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.subject, "数据结构")
