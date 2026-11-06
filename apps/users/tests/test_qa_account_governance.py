"""QA 独立验收测试：账号治理（B 组）与用户管理页面（C-11 / C-12 / C-13）。

与工程师用例的分工：工程师验证「实现是否按设计工作」，本文件只盯
**PRD 的验收条目**（AC-12 ~ AC-30），并刻意换一套数据与断言写法，避免自证。

覆盖：
  * B-6 / B-7  重置密码：随机值必须过 ``AUTH_PASSWORD_VALIDATORS``、明文不进 message、
               弱密码被拒、旧密码失效新密码可用；
  * B-8       停用：Session 被踢、旧 JWT 失效、登录提示不泄露账号状态；
  * B-9       最后一个启用管理员不可停用 / 不可降级（服务层 + 接口层）；
  * B-10      注册入口无法产出 teacher；
  * C-11 ~ C-13 列表筛选 / 分页 / 建号字段级报错 / 编辑页只读与护栏。
"""

import json

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.common.constants import Role
from apps.common.exceptions import BusinessError
from apps.users.services import UserService

User = get_user_model()

FAST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
PASSWORD = "Exam@2026518"
LOGIN_URL = "/api/v1/auth/login/"
REGISTER_URL = "/api/v1/auth/register/"


def api(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


@override_settings(PASSWORD_HASHERS=FAST_HASHERS)
class GovernanceBaseTest(TestCase):
    """三类角色 + 一个可治理目标账号。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="gov_admin", email="gov_admin@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="治理管理员",
        )
        cls.teacher = User.objects.create_user(
            username="gov_teacher", email="gov_teacher@example.com", password=PASSWORD,
            role=Role.TEACHER, real_name="治理教师", subject="数据结构",
        )
        cls.student = User.objects.create_user(
            username="gov_student", email="gov_student@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="治理学生", student_no="GOV2026001",
        )

    # ---------------- 小工具 ----------------
    def api_login(self, username, password):
        return APIClient().post(
            LOGIN_URL, {"username": username, "password": password}, format="json"
        )

    def reset_password(self, target, payload=None):
        return api(self.admin).post(
            f"/api/v1/users/{target.pk}/reset-password/", payload or {}, format="json"
        )


# ======================================================================
# B-6 / B-7 重置密码
# ======================================================================
class ResetPasswordTest(GovernanceBaseTest):
    """AC-19 / AC-20 / AC-22：随机密码强度、明文不外泄、弱口令拦截。"""

    def test_generated_password_satisfies_configured_validators(self):
        """AC-19：服务端随机生成的值必须能通过当前密码策略（唯一权威口径）。"""
        response = self.reset_password(self.student)
        self.assertEqual(response.status_code, 200)
        plain = response.json()["data"]["password"]

        self.assertEqual(len(plain), 12)
        self.assertTrue(any(c.islower() for c in plain), f"缺小写字母：{plain}")
        self.assertTrue(any(c.isupper() for c in plain), f"缺大写字母：{plain}")
        self.assertTrue(any(c.isdigit() for c in plain), f"缺数字：{plain}")
        # 用 Django 的校验器再验一遍，而不是相信服务内部的判断
        try:
            validate_password(plain, user=self.student)
        except DjangoValidationError as exc:  # pragma: no cover - 失败即缺陷
            self.fail(f"生成的密码未通过 AUTH_PASSWORD_VALIDATORS：{exc.messages}")

    def test_plaintext_password_never_leaks_outside_data_field(self):
        """AC-19 / D-3：明文只能出现在 data.password 一次，message 里不能有。"""
        payload = self.reset_password(self.student).json()
        plain = payload["data"]["password"]
        self.assertEqual(payload["message"], "密码已重置")
        self.assertNotIn(plain, payload["message"])
        outside = json.dumps({k: v for k, v in payload.items() if k != "data"})
        self.assertNotIn(plain, outside)

    def test_old_password_dead_new_password_alive(self):
        """AC-22：重置后旧密码失效、新密码可登录（走真实登录接口验证）。"""
        plain = self.reset_password(self.student).json()["data"]["password"]
        self.assertEqual(self.api_login("gov_student", PASSWORD).status_code, 401)
        self.assertEqual(self.api_login("gov_student", plain).status_code, 200)

    def test_two_resets_produce_different_passwords(self):
        """生成器不能退化成常量（否则兜底密码会变成公开后门）。"""
        first = self.reset_password(self.student).json()["data"]["password"]
        second = self.reset_password(self.student).json()["data"]["password"]
        self.assertNotEqual(first, second)

    def test_weak_passwords_rejected(self):
        """AC-20：纯数字 / 与用户名相似 → 400，且旧的可用密码不被破坏。"""
        cases = {
            "123456": "纯数字 6 位",
            "135792468": "纯数字 9 位",
            "gov_student2026": "与用户名高度相似",
            "password": "常见弱口令",
            "short": "长度不足",
        }
        for weak, why in cases.items():
            with self.subTest(password=weak, reason=why):
                response = self.reset_password(self.student, {"new_password": weak})
                self.assertEqual(response.status_code, 400, f"{why} 未被拒绝")
                body = response.json()
                self.assertTrue(body["message"], "拒绝时没有给出任何提示")
                self.assertNotEqual(body["message"], "密码已重置")
        # 反复失败后，原密码必须仍然可用
        self.assertTrue(self.student.check_password(PASSWORD), "弱口令被拒后旧密码被改坏了")

    def test_strong_manual_password_accepted(self):
        response = self.reset_password(self.student, {"new_password": "Qs#2026Exam"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["password"], "Qs#2026Exam")
        self.assertEqual(self.api_login("gov_student", "Qs#2026Exam").status_code, 200)

    def test_teacher_cannot_reset_password(self):
        response = api(self.teacher).post(
            f"/api/v1/users/{self.student.pk}/reset-password/", {}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.student.check_password(PASSWORD))


# ======================================================================
# B-8 停用账号
# ======================================================================
class DeactivationTest(GovernanceBaseTest):
    """AC-25 ~ AC-28：停用即踢下线、旧 JWT 失效、登录提示区分状态。"""

    def toggle(self, target, actor=None):
        return api(actor or self.admin).post(
            f"/api/v1/users/{target.pk}/toggle-active/", {}, format="json"
        )

    def test_active_session_is_kicked_out_on_next_request(self):
        """AC-25：已登录的页面会话，下一次请求就被强制登出并说明原因。"""
        victim_client = self.client_class()
        victim_client.force_login(self.student)
        self.assertEqual(victim_client.get("/").status_code, 200)

        self.assertEqual(self.toggle(self.student).status_code, 200)

        response = victim_client.get("/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "该账号已被停用，请联系管理员")
        self.assertIn("/login/", response.redirect_chain[-1][0])

    def test_session_is_really_destroyed_not_just_redirected(self):
        """踢下线必须是真注销：再访问受保护页面应回到登录页，而不是又能点进去。"""
        victim_client = self.client_class()
        victim_client.force_login(self.student)
        self.toggle(self.student)

        for url in ("/my-exams/", "/classrooms/", "/profile/"):
            with self.subTest(url=url):
                response = victim_client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response["Location"])

    def test_old_jwt_rejected_after_deactivation(self):
        """AC-28：停用前签发的 access token 下一次调用即 401。"""
        token = self.api_login("gov_student", PASSWORD).json()["data"]["access"]
        authed = APIClient()
        authed.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(authed.get("/api/v1/auth/me/").status_code, 200)

        self.toggle(self.student)
        self.assertEqual(authed.get("/api/v1/auth/me/").status_code, 401)

    def test_page_login_reports_inactive_only_with_correct_password(self):
        """AC-26 / AC-27：正确密码 → 提示已停用；错误密码 → 仍是通用提示。"""
        self.toggle(self.student)
        client = self.client_class()

        correct = client.post("/login/", {"username": "gov_student", "password": PASSWORD})
        self.assertContains(correct, "该账号已被停用")
        self.assertNotContains(correct, "账号或密码错误")

        wrong = client.post("/login/", {"username": "gov_student", "password": "WrongPass#1"})
        self.assertContains(wrong, "账号或密码错误")
        self.assertNotContains(wrong, "已被停用")

    def test_unknown_account_keeps_generic_message(self):
        """AC-27 的边界：不存在的账号不能因为「查不到」而暴露不同文案。"""
        response = self.client_class().post(
            "/login/", {"username": "no_such_account", "password": "Whatever#2026"}
        )
        self.assertContains(response, "账号或密码错误")
        self.assertNotContains(response, "已被停用")

    def _login_body(self, username, password):
        response = self.api_login(username, password)
        self.assertEqual(response.status_code, 401, "登录失败必须是 401")
        return response.json()

    def test_api_login_does_not_leak_account_state(self):
        """AC-27 红线：**未证明密码**时，三态文案与业务码必须完全一致。

        B 方案（U-3）落地后，「停用 + 密码正确」按 AC-26 给出停用提示（40101）——
        这不构成账号枚举，因为调用方已经持有正确密码。真正要守住的是：
          * 停用账号+错密码 == 正常账号+错密码 == 账号不存在（文案与码都一致）；
          * 「停用」字样只在密码正确时出现。
        """
        self.toggle(self.student)
        inactive_correct = self._login_body("gov_student", PASSWORD)
        inactive_wrong = self._login_body("gov_student", "WrongPass#2026")
        active_wrong = self._login_body("gov_teacher", "WrongPass#2026")
        unknown = self._login_body("no_such_account", "Whatever#2026")

        # 1) 停用 + 密码正确 → 停用提示（AC-26 / PRD §4.6「与页面口径一致」）
        self.assertIn("停用", inactive_correct["message"])
        self.assertEqual(inactive_correct["code"], 40101)

        # 2) 未证明密码时：停用 / 正常 / 不存在 三者完全不可区分
        for body, why in (
            (inactive_wrong, "停用账号+错密码"),
            (active_wrong, "正常账号+错密码"),
            (unknown, "账号不存在"),
        ):
            with self.subTest(case=why):
                self.assertEqual(body["message"], "账号或密码错误，请重新输入", why)
                self.assertEqual(body["code"], 40100, f"{why} 泄露了业务码差异")
                self.assertNotIn("停用", body["message"], f"{why} 泄露了账号状态")
        self.assertEqual(
            {inactive_wrong["message"], active_wrong["message"], unknown["message"]},
            {"账号或密码错误，请重新输入"},
            "三种失败场景的提示不完全一致，存在账号枚举风险",
        )

    def test_api_request_with_inactive_session_returns_biz_code_40101(self):
        """中间件通道：带停用账号 Session 的 /api/ 请求由中间件直出 40101。

        P4 已统一文案：中间件（`apps/common/middleware.py:54`）、登录接口
        （`serializers.py:22`）、页面（`views_pages.py:177`）三处逐字相同，
        因此这里可以放心做逐字 equal，不必再「只断言语义」。
        """
        victim = self.client_class()
        victim.force_login(self.student)
        self.toggle(self.student)

        response = victim.get("/api/v1/auth/me/")
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body["code"], 40101)
        self.assertEqual(body["message"], "该账号已被停用，请联系管理员")

    def test_api_login_wording_matches_page(self):
        """接口登录失败提示与页面口径一致（A + B 已修复，装饰器已移除）。

        历史缺陷（已在 A+B 中修复）：
          1. ``serializers.py:14`` 原从 ``rest_framework_simplejwt.exceptions`` 导入
             ``AuthenticationFailed``（DRF 版的**子类**），而 SimpleJWT 5.5.1 抛的是
             **DRF 基类**实例 → ``except`` 永不命中，统一中文提示与 ``is_active``
             分支双双失效，文案落到 "找不到指定凭据对应的有效用户"；
          2. 仅修 import 仍不够：``ModelBackend.user_can_authenticate`` 把停用账号
             过滤成 ``None``，``if not self.user.is_active`` 依然不可达。

        故 B 方案在 ``validate()`` 里补了与页面登录一致的**二次判定**：
        账号存在 + 密码正确 + 已停用 → 「该账号已被停用，请联系管理员」（40101）。
        """
        self.toggle(self.student)
        body = self._login_body("gov_student", PASSWORD)
        self.assertIn("停用", body["message"])
        self.assertEqual(body["code"], 40101)

    def test_toggle_twice_restores_account(self):
        self.toggle(self.student)
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)
        self.toggle(self.student)
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)
        self.assertEqual(self.api_login("gov_student", PASSWORD).status_code, 200)

    def test_cannot_deactivate_self(self):
        """AC-23 的服务端兜底：接口必须自己拒绝，不能只靠页面把按钮置灰。"""
        response = self.toggle(self.admin)
        self.assertEqual(response.status_code, 400)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertIn("不能停用自己", response.json()["message"])


# ======================================================================
# B-9 最后一个启用管理员护栏
# ======================================================================
class LastAdminGuardTest(GovernanceBaseTest):
    """AC-18：停用与降级都要保底至少一个启用管理员。"""

    def test_service_layer_rejects_deactivating_last_admin(self):
        with self.assertRaises(BusinessError) as ctx:
            UserService.assert_admin_survives(self.admin, is_active=False)
        self.assertIn("至少保留一个启用的管理员", str(ctx.exception.detail))

    def test_service_layer_rejects_demoting_last_admin(self):
        with self.assertRaises(BusinessError):
            UserService.assert_admin_survives(self.admin, role=Role.TEACHER)

    def test_service_layer_allows_ordinary_user(self):
        # 普通教师本来就不是管理员，启停都不该被这条护栏拦住
        UserService.assert_admin_survives(self.teacher, is_active=False)
        UserService.assert_admin_survives(self.teacher, role=Role.STUDENT)

    def test_service_layer_allows_when_another_admin_survives(self):
        second = User.objects.create_user(
            username="gov_admin2", email="gov_admin2@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="第二管理员",
        )
        UserService.assert_admin_survives(second, is_active=False)
        UserService.assert_admin_survives(self.admin, role=Role.TEACHER)

    def test_api_rejects_demoting_last_admin_via_patch(self):
        """接口层：唯一管理员把自己降级 → 400 且角色不变。"""
        response = api(self.admin).patch(
            f"/api/v1/users/{self.admin.pk}/", {"role": Role.TEACHER}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("至少保留一个启用的管理员", response.json()["message"])
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, Role.ADMIN, "降级竟然生效了")
        # 仍然能进管理端（没有把自己锁在门外）
        client = self.client_class()
        client.force_login(self.admin)
        self.assertEqual(client.get("/manage/").status_code, 200)

    def test_api_allows_demotion_when_second_admin_exists(self):
        second = User.objects.create_user(
            username="gov_admin3", email="gov_admin3@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="第三管理员",
        )
        response = api(second).patch(
            f"/api/v1/users/{self.admin.pk}/", {"role": Role.TEACHER}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, Role.TEACHER)

    def test_page_edit_rejects_demoting_last_admin(self):
        client = self.client_class()
        client.force_login(self.admin)
        response = client.post(f"/manage/users/{self.admin.pk}/edit/", {
            "username": self.admin.username,
            "email": self.admin.email,
            "real_name": self.admin.real_name,
            "role": Role.TEACHER,
            "student_no": "",
            "subject": "",
            "phone": "",
            "school": "",
        })
        self.assertEqual(response.status_code, 200, "被拒绝时应停留在表单页")
        self.assertContains(response, "系统至少保留一个启用的管理员")
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, Role.ADMIN)


# ======================================================================
# B-10 注册收敛
# ======================================================================
class RegisterConvergenceTest(TestCase):
    """AC-29 / AC-30：注册只能产出学生。"""

    def test_api_rejects_teacher_and_admin_role(self):
        for role in (Role.TEACHER, Role.ADMIN):
            with self.subTest(role=role):
                response = APIClient().post(REGISTER_URL, {
                    "username": f"reg_{role}", "email": f"reg_{role}@example.com",
                    "password": "Exam@2026Reg", "password2": "Exam@2026Reg",
                    "role": role,
                }, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertFalse(User.objects.filter(username=f"reg_{role}").exists())

    def test_api_defaults_to_student(self):
        response = APIClient().post(REGISTER_URL, {
            "username": "reg_student", "email": "reg_student@example.com",
            "password": "Exam@2026Reg", "password2": "Exam@2026Reg",
            "grade_year": str(__import__("django.utils.timezone", fromlist=["now"]).now().year),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(User.objects.get(username="reg_student").role, Role.STUDENT)

    def test_register_page_has_no_teacher_option(self):
        html = self.client_class().get("/register/").content.decode("utf-8")
        self.assertNotIn('value="teacher"', html)
        self.assertNotIn('value="admin"', html)

    def test_register_page_post_with_teacher_role_creates_nothing(self):
        self.client_class().post("/register/", {
            "username": "reg_page_teacher", "email": "reg_page_teacher@example.com",
            "real_name": "越权注册", "role": Role.TEACHER,
            "password": "Exam@2026Reg", "password2": "Exam@2026Reg",
        })
        self.assertFalse(User.objects.filter(username="reg_page_teacher").exists())


# ======================================================================
# C-11 列表：筛选 / 分页 / 空结果
# ======================================================================
@override_settings(PASSWORD_HASHERS=FAST_HASHERS)
class UserListPageTest(GovernanceBaseTest):
    """AC-12 / AC-13：筛选条件组合、翻页保留条件、空结果与越界页。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        bulk = []
        for i in range(1, 26):
            bulk.append(User(
                username=f"list_s{i:02d}", email=f"list_s{i:02d}@example.com",
                role=Role.STUDENT, real_name=f"张同学{i:02d}", subject="数据结构",
                student_no=f"LIST{i:04d}",
            ))
        bulk.append(User(username="list_off", email="list_off@example.com",
                         role=Role.TEACHER, real_name="张停用教师", is_active=False))
        User.objects.bulk_create(bulk)

    def _get(self, params=""):
        client = self.client_class()
        client.force_login(self.admin)
        response = client.get(f"/manage/users/{params}")
        self.assertEqual(response.status_code, 200, params)
        return response

    def test_search_matches_name_and_is_reproducible(self):
        """AC-12：关键词命中姓名，URL 带 search= 可刷新复现。"""
        response = self._get("?search=张")
        usernames = [u.username for u in response.context["page_obj"].object_list]
        self.assertEqual(len(usernames), 10)  # 26 条命中，首页 10 条
        self.assertEqual(response.context["total_count"], 26)
        self.assertContains(response, 'value="张"')

    def test_combined_role_and_status_filter(self):
        """AC-13：角色=教师 + 状态=已停用 必须同时满足。"""
        response = self._get("?role=teacher&is_active=false")
        rows = list(response.context["page_obj"].object_list)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].username, "list_off")

        response = self._get("?role=teacher&is_active=true")
        usernames = {u.username for u in response.context["page_obj"].object_list}
        self.assertEqual(usernames, {"gov_teacher"})

    def test_subject_filter(self):
        response = self._get("?subject=数据结构")
        self.assertEqual(response.context["total_count"], 26)  # 25 学生 + 1 教师

    def test_pagination_and_filter_preservation(self):
        """翻页链接必须带着当前筛选条件（否则翻页即丢条件）。"""
        html = self._get("?role=student&page_size=10&page=2").content.decode("utf-8")
        self.assertRegex(html, r'href="\?[^"]*role=student[^"]*page=3"')
        self.assertRegex(html, r'href="\?[^"]*role=student[^"]*page=1"')

    def test_out_of_range_page_falls_back_to_last_page(self):
        response = self._get("?page=999")
        self.assertEqual(
            response.context["page_obj"].number, response.context["paginator"].num_pages
        )

    def test_empty_result_renders_empty_state(self):
        response = self._get("?search=根本不存在的用户")
        self.assertEqual(response.context["total_count"], 0)
        self.assertContains(response, "没有符合条件的用户")

    def test_invalid_page_size_ignored(self):
        response = self._get("?page_size=9999")
        self.assertEqual(response.context["page_size"], 10)

    def test_list_page_query_count_is_constant(self):
        """AC-36 的代理指标：用户列表首屏的查询条数不随数据量增长（防 N+1）。"""
        def measure() -> int:
            client = self.client_class()
            client.force_login(self.admin)
            with CaptureQueriesContext(connection) as captured:
                client.get("/manage/users/")
            return len(captured)

        baseline = measure()
        User.objects.bulk_create([
            User(username=f"perf_u{i}", email=f"perf_u{i}@example.com",
                 role=Role.STUDENT, real_name=f"性能用户{i}", subject="数据结构")
            for i in range(300)
        ])
        self.assertEqual(measure(), baseline, "列表页查询条数随数据量增长，存在 N+1")
        self.assertLess(baseline, 40, f"首屏查询过多：{baseline} 条")


# ======================================================================
# C-12 / C-13 建号与编辑
# ======================================================================
@override_settings(PASSWORD_HASHERS=FAST_HASHERS)
class UserFormPageTest(GovernanceBaseTest):
    """AC-14 / AC-15 / AC-16 / AC-17：字段级报错、只读字段、角色护栏。"""

    def _post_create(self, **overrides):
        data = {
            "username": "form_new_teacher",
            "email": "form_new_teacher@example.com",
            "real_name": "新建教师",
            "role": Role.TEACHER,
            "student_no": "FORM0001",
            "subject": "操作系统",
            "phone": "13800000000",
            "school": "计算机学院",
            "password": "Exam@2026New",
            "password2": "Exam@2026New",
        }
        data.update(overrides)
        client = self.client_class()
        client.force_login(self.admin)
        return client.post("/manage/users/new/", data)

    def test_missing_required_fields_reported_per_field(self):
        client = self.client_class()
        client.force_login(self.admin)
        response = client.post("/manage/users/new/", {})
        self.assertEqual(response.status_code, 200)
        errors = response.context["form"].errors
        for field in ("username", "email", "password", "password2"):
            self.assertIn(field, errors, f"{field} 缺失未被报错")

    def test_duplicate_username_keeps_other_inputs(self):
        """AC-14：报错后已填内容不能丢。"""
        response = self._post_create(username=self.teacher.username)
        self.assertContains(response, "该用户名已被占用")
        self.assertContains(response, "13800000000")
        self.assertContains(response, "计算机学院")
        self.assertFalse(User.objects.filter(email="form_new_teacher@example.com").exists())

    def test_duplicate_email_rejected(self):
        response = self._post_create(email=self.teacher.email)
        self.assertContains(response, "该邮箱已被注册")
        self.assertFalse(User.objects.filter(username="form_new_teacher").exists())

    def test_duplicate_student_no_rejected(self):
        response = self._post_create(student_no=self.student.student_no)
        self.assertContains(response, "该学号 / 工号已被使用")

    def test_weak_initial_password_rejected(self):
        """AC-15：初始密码 123456 必须被拒绝，且提示能指向密码策略。"""
        response = self._post_create(password="123456", password2="123456")
        self.assertEqual(response.status_code, 200)
        password_errors = " ".join(response.context["form"].errors.get("password", []))
        self.assertIn("数字", password_errors)
        self.assertFalse(User.objects.filter(username="form_new_teacher").exists())

    def test_successful_create_allows_teacher_role(self):
        """管理端与公开注册的分界：管理员可以建教师号。"""
        response = self._post_create()
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="form_new_teacher")
        self.assertEqual(user.role, Role.TEACHER)
        self.assertEqual(user.subject, "操作系统")
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("Exam@2026New"))

    def test_edit_page_hides_username_and_status(self):
        """AC-16：用户名只读、状态字段不出现。"""
        client = self.client_class()
        client.force_login(self.admin)
        html = client.get(f"/manage/users/{self.student.pk}/edit/").content.decode("utf-8")
        self.assertNotIn('name="username"', html)
        self.assertNotIn('name="is_active"', html)
        self.assertIn(self.student.username, html)
        self.assertIn("创建后不可修改", html)

    def test_superuser_role_cannot_be_downgraded(self):
        """AC-17：超级管理员不可降级（即便还有其他管理员兜底）。"""
        second = User.objects.create_user(
            username="gov_admin4", email="gov_admin4@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="第二管理员",
        )
        boss = User.objects.create_superuser(
            username="gov_root", email="gov_root@example.com", password=PASSWORD,
        )
        client = self.client_class()
        client.force_login(second)
        response = client.post(f"/manage/users/{boss.pk}/edit/", {
            "email": boss.email, "real_name": boss.real_name or "超级管理员",
            "role": Role.TEACHER, "student_no": "", "subject": "", "phone": "", "school": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "超级管理员的角色不可降级")
        boss.refresh_from_db()
        self.assertEqual(boss.role, Role.ADMIN)

    def test_dangerous_endpoints_reject_get(self):
        """AC-37：危险动作不接受 GET，且不产生状态变更。"""
        client = APIClient()
        client.force_authenticate(user=self.admin)
        for action in ("reset-password", "toggle-active"):
            with self.subTest(action=action):
                response = client.get(f"/api/v1/users/{self.student.pk}/{action}/")
                self.assertEqual(response.status_code, 405)
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)
        self.assertTrue(self.student.check_password(PASSWORD))
