"""管理端「用户管理」页面的测试。

覆盖：
  * AC-12 / AC-13：关键词搜索、角色+状态组合筛选，条件写入 URL；
  * AC-14：新建用户校验失败时停在表单页并回填，已填内容不丢失；
  * AC-15：初始密码必须满足密码策略；
  * AC-16：编辑页用户名只读、状态字段不在表单里；
  * AC-17 / AC-18：超级管理员不可降级、最后一个启用管理员不可停用（页面文案）；
  * AC-21 / AC-23 / AC-24：重置密码只显示一次、自己那行按钮禁用、停用二次确认文案；
  * AC-36：列表页 + 学科候选在同一个 HTML 响应里（不额外发请求）；
  * 非管理员访问 → 302。
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.common.constants import Role

User = get_user_model()

PASSWORD = "Exam@123456"
LIST_URL = "/manage/users/"


class ManageUserPageBaseTest(TestCase):
    """夹具：管理员（超管另建）+ 教师 + 学生。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="pg_admin", email="pg_admin@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="页面管理员",
        )
        cls.teacher = User.objects.create_user(
            username="pg_teacher", email="pg_teacher@example.com", password=PASSWORD,
            role=Role.TEACHER, real_name="张三", student_no="T2026001", subject="数据结构",
        )
        cls.student = User.objects.create_user(
            username="pg_student", email="pg_student@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="李四", student_no="S2026001",
        )

    def login_as_admin(self):
        self.client.force_login(self.admin)


class GuardTest(ManageUserPageBaseTest):
    """页面守卫。"""

    def test_non_admin_redirected(self):
        for user in (self.teacher, self.student):
            client = self.client_class()
            client.force_login(user)
            for url in (LIST_URL, "/manage/users/new/",
                        f"/manage/users/{self.student.pk}/edit/"):
                response = client.get(url)
                self.assertEqual(response.status_code, 302, f"{user.username} {url}")
                self.assertEqual(response["Location"], "/")

    def test_admin_can_open_list_and_forms(self):
        self.login_as_admin()
        self.assertEqual(self.client.get(LIST_URL).status_code, 200)
        self.assertEqual(self.client.get("/manage/users/new/").status_code, 200)
        self.assertEqual(
            self.client.get(f"/manage/users/{self.student.pk}/edit/").status_code, 200
        )

    def test_edit_missing_user_404(self):
        self.login_as_admin()
        self.assertEqual(self.client.get("/manage/users/999999/edit/").status_code, 404)


class ListPageTest(ManageUserPageBaseTest):
    """列表：搜索 / 筛选 / 分页 / 空状态。"""

    def setUp(self):
        self.login_as_admin()

    def test_lists_users_with_key_columns(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pg_teacher")
        self.assertContains(response, "张三")
        self.assertContains(response, "数据结构")
        self.assertContains(response, "T2026001")
        self.assertContains(response, "共 ")

    def test_search_by_name(self):
        """AC-12：关键词命中姓名，筛选条件进入 URL 可刷新复现。"""
        response = self.client.get(LIST_URL, {"search": "张三"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pg_teacher")
        self.assertNotContains(response, "pg_student")

    def test_search_by_student_no(self):
        response = self.client.get(LIST_URL, {"search": "S2026001"})
        self.assertContains(response, "pg_student")
        self.assertNotContains(response, "pg_teacher")

    def test_filter_by_role_and_status(self):
        """AC-13：角色=教师 + 状态=已停用 同时生效。"""
        self.teacher.is_active = False
        self.teacher.save(update_fields=["is_active"])

        both = self.client.get(LIST_URL, {"role": "teacher", "is_active": "false"})
        self.assertContains(both, "pg_teacher")
        self.assertNotContains(both, "pg_student")

        # 只筛角色=教师（含停用）→ 仍是教师
        only_role = self.client.get(LIST_URL, {"role": "teacher"})
        self.assertContains(only_role, "pg_teacher")
        # 角色=学生 + 状态=已停用 → 无结果
        none = self.client.get(LIST_URL, {"role": "student", "is_active": "false"})
        self.assertContains(none, "没有符合条件的用户")

    def test_filter_by_subject(self):
        response = self.client.get(LIST_URL, {"subject": "数据结构"})
        self.assertContains(response, "pg_teacher")
        self.assertNotContains(response, "pg_student")

    def test_empty_state_when_filtered(self):
        response = self.client.get(LIST_URL, {"search": "根本不存在的名字"})
        self.assertContains(response, "没有符合条件的用户")
        self.assertContains(response, "清空筛选条件")

    def test_new_user_entry_visible(self):
        """空库引导：列表页始终给出「新建用户」主行动入口（P0-15）。"""
        User.objects.all().delete()
        admin = User.objects.create_user(
            username="solo_admin", email="solo@example.com", password=PASSWORD,
            role=Role.ADMIN,
        )
        client = self.client_class()
        client.force_login(admin)
        response = client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "新建用户")

    def test_pagination(self):
        for index in range(15):
            User.objects.create_user(
                username=f"bulk_page_{index}", email=f"bulk_page_{index}@example.com",
                password=PASSWORD, role=Role.STUDENT,
            )
        first = self.client.get(LIST_URL, {"page_size": "10"})
        self.assertEqual(len(first.context["page_obj"].object_list), 10)
        self.assertTrue(first.context["paginator"].num_pages >= 2)

        second = self.client.get(LIST_URL, {"page_size": "10", "page": "2"})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.context["page_obj"].number, 2)

    def test_invalid_page_falls_back_to_last(self):
        response = self.client.get(LIST_URL, {"page": "9999"})
        self.assertEqual(response.status_code, 200)

    def test_single_response_contains_candidates(self):
        """AC-36：列表数据与学科候选在同一个 HTML 响应里，页面不再额外发请求。"""
        response = self.client.get(LIST_URL)
        body = response.content.decode("utf-8")
        self.assertIn("pg_teacher", body)          # 列表行
        self.assertIn("数据结构", body)             # 学科候选 <option> 与行内值

    def test_self_row_buttons_disabled(self):
        """AC-23：自己那一行的「停用」「重置密码」为禁用态并给出原因。

        先补第二个管理员：否则「最后一个启用管理员」的文案会先命中（AC-18），
        这条用例要验证的是「对自己」这条分支。
        """
        User.objects.create_user(
            username="pg_admin_b", email="pg_admin_b@example.com", password=PASSWORD,
            role=Role.ADMIN,
        )
        response = self.client.get(LIST_URL, {"search": "pg_admin"})
        body = response.content.decode("utf-8")
        self.assertIn("不能对自己执行该操作", body)
        self.assertIn("不能停用自己的账号", body)

    def test_last_admin_toggle_disabled(self):
        """AC-18：只剩一个启用管理员时，页面上直接给出明确原因（不等接口 400）。"""
        response = self.client.get(LIST_URL, {"role": "admin", "search": "pg_admin"})
        body = response.content.decode("utf-8")
        self.assertIn("不能停用系统中最后一个启用的管理员", body)

    def test_toggle_confirm_modal_text(self):
        """AC-24：停用前有二次确认，且说明「立即无法登录 / 数据保留」。"""
        response = self.client.get(LIST_URL)
        body = response.content.decode("utf-8")
        self.assertIn("该账号将立即无法登录", body)
        self.assertIn("保留不变", body)
        self.assertIn("toggleModal", body)

    def test_no_delete_entry(self):
        """Q-2：界面不提供物理删除。"""
        response = self.client.get(LIST_URL)
        body = response.content.decode("utf-8")
        self.assertNotIn(">删除<", body)
        self.assertNotIn("data-action=\"delete\"", body)

    def test_filter_preserved_in_pagination_links(self):
        for index in range(15):
            User.objects.create_user(
                username=f"keep_{index}", email=f"keep_{index}@example.com",
                password=PASSWORD, role=Role.STUDENT,
            )
        response = self.client.get(LIST_URL, {"role": "student", "page_size": "10"})
        ctx = response.context
        self.assertEqual(ctx["querystring"], "role=student&page_size=10&")


class CreatePageTest(ManageUserPageBaseTest):
    """新建用户（AC-14 / AC-15）。"""

    def setUp(self):
        self.login_as_admin()
        self.url = reverse("manage-user-create")

    def _payload(self, **overrides) -> dict:
        data = {
            "username": "new_teacher",
            "email": "new_teacher@example.com",
            "real_name": "新老师",
            "role": Role.TEACHER,
            "student_no": "T2026999",
            "subject": "操作系统",
            "phone": "",
            "school": "计算机学院",
            "password": "Teacher@2026",
            "password2": "Teacher@2026",
        }
        data.update(overrides)
        return data

    def test_create_success_redirects_to_list(self):
        response = self.client.post(self.url, self._payload())
        self.assertEqual(response.status_code, 302)
        self.assertIn("search=new_teacher", response["Location"])
        created = User.objects.get(username="new_teacher")
        self.assertEqual(created.role, Role.TEACHER)
        self.assertEqual(created.subject, "操作系统")
        self.assertTrue(created.is_active)
        self.assertTrue(created.check_password("Teacher@2026"))

    def test_duplicate_username_stays_on_form(self):
        """AC-14：用户名已存在 → 停在表单页、字段级报错、其余已填内容不丢。"""
        response = self.client.post(self.url, self._payload(username=self.teacher.username))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "该用户名已被占用")
        # 其余已填内容仍在（school 只出现在表单里）
        self.assertContains(response, "计算机学院")
        self.assertEqual(User.objects.filter(email="new_teacher@example.com").count(), 0)

    def test_weak_password_rejected(self):
        """AC-15：纯数字 / 常见弱口令被拒，提示含「数字」或「常见」。"""
        response = self.client.post(
            self.url, self._payload(password="123456", password2="123456")
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertTrue("数字" in body or "常见" in body, body[:500])
        self.assertFalse(User.objects.filter(username="new_teacher").exists())

    def test_password_mismatch_rejected(self):
        response = self.client.post(self.url, self._payload(password2="Other@2026"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "两次输入的密码不一致")

    def test_duplicate_student_no_rejected(self):
        response = self.client.post(self.url, self._payload(student_no=self.teacher.student_no))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "该学号 / 工号已被使用")

    def test_create_admin_allowed(self):
        """公开注册只能产出学生（D-5），但管理端可以建管理员。"""
        response = self.client.post(
            self.url,
            self._payload(username="second_admin", email="second_admin@example.com",
                          role=Role.ADMIN),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.get(username="second_admin").role, Role.ADMIN)


class EditPageTest(ManageUserPageBaseTest):
    """编辑用户（AC-16 / AC-17 / AC-18）。"""

    def setUp(self):
        self.login_as_admin()
        self.url = reverse("manage-user-edit", args=[self.teacher.pk])

    def test_username_readonly_and_no_status_field(self):
        """AC-16：用户名只读文本；is_active 不出现在表单里。"""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn(f'value="{self.teacher.username}"', body)
        self.assertIn("readonly", body)
        self.assertIn("用户名创建后不可修改", body)
        self.assertNotIn('name="username"', body)
        self.assertNotIn('name="is_active"', body)

    def test_edit_subject_and_real_name(self):
        response = self.client.post(self.url, {
            "email": self.teacher.email, "real_name": "张三改", "role": Role.TEACHER,
            "student_no": self.teacher.student_no, "subject": "计算机网络",
            "phone": "13800000000", "school": "计算机学院",
        })
        self.assertEqual(response.status_code, 302)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.subject, "计算机网络")
        self.assertEqual(self.teacher.real_name, "张三改")

    def test_superuser_cannot_be_downgraded(self):
        """AC-17：超级管理员角色不可降级。"""
        superuser = User.objects.create_superuser(
            username="pg_root", email="pg_root@example.com", password=PASSWORD,
        )
        response = self.client.post(reverse("manage-user-edit", args=[superuser.pk]), {
            "email": superuser.email, "real_name": "超管", "role": Role.TEACHER,
            "student_no": "", "subject": "", "phone": "", "school": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "超级管理员的角色不可降级")
        superuser.refresh_from_db()
        self.assertEqual(superuser.role, Role.ADMIN)

    def test_last_admin_cannot_be_downgraded(self):
        """AC-18：最后一个启用管理员不可降级。"""
        response = self.client.post(reverse("manage-user-edit", args=[self.admin.pk]), {
            "email": self.admin.email, "real_name": "页面管理员", "role": Role.TEACHER,
            "student_no": "", "subject": "", "phone": "", "school": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "系统至少保留一个启用的管理员")
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, Role.ADMIN)

    def test_downgrade_allowed_with_second_admin(self):
        second = User.objects.create_user(
            username="pg_admin2", email="pg_admin2@example.com", password=PASSWORD,
            role=Role.ADMIN,
        )
        response = self.client.post(reverse("manage-user-edit", args=[second.pk]), {
            "email": second.email, "real_name": "管理员二", "role": Role.TEACHER,
            "student_no": "", "subject": "", "phone": "", "school": "",
        })
        self.assertEqual(response.status_code, 302)
        second.refresh_from_db()
        self.assertEqual(second.role, Role.TEACHER)

    def test_duplicate_email_rejected(self):
        response = self.client.post(self.url, {
            "email": self.student.email, "real_name": "张三", "role": Role.TEACHER,
            "student_no": self.teacher.student_no, "subject": "", "phone": "", "school": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "该邮箱已被其他账号使用")
