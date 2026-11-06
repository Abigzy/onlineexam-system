"""学号 / 工号自动派发：注册不再手填，按年级 / 角色顺位生成。"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.common.constants import Role
from apps.users.constants import grade_year_choices
from apps.users.services import UserService

User = get_user_model()
PASSWORD = "Exam@123456"
REGISTER_URL = "/api/v1/auth/register/"
USERS_URL = "/api/v1/users/"
GRADE = grade_year_choices()[0][0]          # 当前年，选项里的第一个
GRADE_PREV = grade_year_choices()[1][0]     # 上一年


def _jwt(client, username, password=PASSWORD):
    response = client.post(
        "/api/v1/auth/login/", {"username": username, "password": password},
        content_type="application/json",
    )
    return response.json()["data"]["access"]


# ==================================================================
# 学生注册：按年级派发学号
# ==================================================================
class StudentNoDispatchTest(TestCase):
    def _payload(self, **overrides):
        data = {
            "username": "reg_stu",
            "email": "reg_stu@example.com",
            "password": PASSWORD,
            "password2": PASSWORD,
            "role": Role.STUDENT,
            "real_name": "注册学生",
            "grade_year": GRADE,
        }
        data.update(overrides)
        return data

    def _register(self, **overrides):
        return self.client.post(
            REGISTER_URL, self._payload(**overrides), content_type="application/json",
        )

    def test_register_dispatches_student_no(self):
        response = self._register()
        self.assertEqual(response.status_code, 201, response.content)
        user = User.objects.get(username="reg_stu")
        self.assertTrue(user.student_no.startswith(GRADE))
        self.assertEqual(len(user.student_no), 8, "学号 = 4 位年级 + 4 位顺位")
        self.assertTrue(user.student_no[4:].isdigit())

    def test_sequential_dispatch(self):
        """同年级连续注册，顺位递增。"""
        self._register(username="stu_a", email="stu_a@example.com")
        self._register(username="stu_b", email="stu_b@example.com")
        first = int(User.objects.get(username="stu_a").student_no[4:])
        second = int(User.objects.get(username="stu_b").student_no[4:])
        self.assertEqual(second, first + 1)

    def test_different_grades_sequence_independent(self):
        """不同年级的顺位各自独立，互不影响。"""
        self._register(username="g_prev", email="g_prev@example.com", grade_year=GRADE_PREV)
        self._register(username="g_cur", email="g_cur@example.com", grade_year=GRADE)
        prev_no = User.objects.get(username="g_prev").student_no
        cur_no = User.objects.get(username="g_cur").student_no
        self.assertTrue(prev_no.startswith(GRADE_PREV))
        self.assertTrue(cur_no.startswith(GRADE))
        self.assertEqual(int(prev_no[4:]), 1, "新年级的顺位应从 1 开始")
        self.assertEqual(int(cur_no[4:]), 1)

    def test_grade_required(self):
        response = self._register(grade_year="")
        self.assertEqual(response.status_code, 400)

    def test_invalid_grade_rejected(self):
        response = self._register(grade_year="1999")
        self.assertEqual(response.status_code, 400)

    def test_client_student_no_ignored(self):
        """客户端硬塞 student_no 不生效——该字段已不在注册白名单里。"""
        response = self._register(student_no="99999999")
        self.assertEqual(response.status_code, 201, response.content)
        user = User.objects.get(username="reg_stu")
        self.assertNotEqual(user.student_no, "99999999")
        self.assertTrue(user.student_no.startswith(GRADE))


# ==================================================================
# 教师工号派发 + 管理员可手填
# ==================================================================
class EmployeeNoDispatchTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="d_admin", email="d_admin@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="管理员",
        )
        # 造一条历史工号，验证「顺位在已有最大值之后」
        User.objects.create_user(
            username="t_old", email="t_old@example.com", password=PASSWORD,
            role=Role.TEACHER, student_no="T1001",
        )

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {_jwt(self.client, 'd_admin')}"}

    def _create(self, **overrides):
        data = {
            "username": "new_user", "email": "new_user@example.com",
            "real_name": "新用户", "role": Role.TEACHER, "password": PASSWORD,
        }
        data.update(overrides)
        return self.client.post(
            USERS_URL, data, content_type="application/json", **self._auth(),
        )

    def test_generate_employee_no_unit(self):
        """服务层：现有 T1001 → 派 T1002。"""
        self.assertEqual(UserService.generate_employee_no(), "T1002")

    def test_create_teacher_auto_employee_no(self):
        response = self._create()
        self.assertEqual(response.status_code, 201, response.content)
        user = User.objects.get(username="new_user")
        self.assertTrue(user.student_no.startswith("T"))
        self.assertEqual(user.student_no, "T1002")

    def test_create_student_with_grade_auto_no(self):
        response = self._create(
            username="new_s", email="new_s@example.com", role=Role.STUDENT,
            grade_year=GRADE,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(User.objects.get(username="new_s").student_no.startswith(GRADE))

    def test_admin_can_manually_set_no(self):
        """管理员不受「不能填」的限制，手填即生效。"""
        response = self._create(student_no="T9999")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(User.objects.get(username="new_user").student_no, "T9999")


# ==================================================================
# 个人资料：学号不可自助修改
# ==================================================================
class ProfileCannotChangeNoTest(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="p_stu", email="p_stu@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="原名", student_no="20260001",
        )

    def test_me_patch_cannot_change_student_no(self):
        token = _jwt(self.client, "p_stu")
        response = self.client.patch(
            "/api/v1/auth/me/",
            {"student_no": "99999999", "real_name": "改名"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.student.refresh_from_db()
        self.assertEqual(self.student.student_no, "20260001", "学号不应被自助修改")
        self.assertEqual(self.student.real_name, "改名", "其他字段仍可正常修改")

    def test_grade_year_property_parsed_from_no(self):
        self.assertEqual(self.student.grade_year, "2026")


# ==================================================================
# 管理端「页面」建号：不填学号也不能落空（R2）
# ==================================================================
class ManagePageDispatchTest(TestCase):
    """管理端页面建号走 ``AdminUserCreateForm.save()``，与 API 建号是两条路径。

    这里专门守着「表单路径也要自动派发」——曾出现 API 已派发、页面建号仍落空的情况。
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="pg_admin", email="pg_admin@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="管理员",
        )

    def _post(self, **overrides):
        data = {
            "username": "pg_user", "email": "pg_user@example.com", "real_name": "页面建号",
            "role": Role.STUDENT, "student_no": "", "grade_year": GRADE,
            "subject": "", "phone": "", "school": "",
            "password": PASSWORD, "password2": PASSWORD,
        }
        data.update(overrides)
        self.client.force_login(self.admin)
        return self.client.post("/manage/users/new/", data)

    def test_student_without_no_is_dispatched(self):
        response = self._post()
        self.assertEqual(response.status_code, 302, "建号成功应重定向")
        user = User.objects.get(username="pg_user")
        self.assertTrue(user.student_no, "学生不填学号不能落成空编号")
        self.assertEqual(len(user.student_no), 8)
        self.assertTrue(user.student_no[:4].isdigit())

    def test_student_grade_selection_takes_effect(self):
        prev = GRADE_PREV
        response = self._post(grade_year=prev)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get(username="pg_user").student_no.startswith(prev))

    def test_teacher_without_no_gets_employee_no(self):
        response = self._post(username="pg_teacher", email="pg_teacher@example.com",
                              role=Role.TEACHER)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get(username="pg_teacher").student_no.startswith("T"))

    def test_manual_no_still_wins(self):
        response = self._post(student_no="MANUAL0001")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.get(username="pg_user").student_no, "MANUAL0001")


# ==================================================================
# 学生建号「入学年级」必填（页面 + API 两条路径）
# ==================================================================
class GradeRequiredTest(TestCase):
    """学生必须有年级——否则学号无从派发，手填学号也无从校验。"""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="gr_admin", email="gr_admin@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="管理员",
        )

    # ---- 页面建号 ----
    def _post_page(self, **overrides):
        data = {
            "username": "gr_user", "email": "gr_user@example.com", "real_name": "建号",
            "role": Role.STUDENT, "student_no": "", "grade_year": GRADE,
            "subject": "", "phone": "", "school": "",
            "password": PASSWORD, "password2": PASSWORD,
        }
        data.update(overrides)
        self.client.force_login(self.admin)
        return self.client.post("/manage/users/new/", data)

    def test_page_student_without_grade_rejected(self):
        response = self._post_page(grade_year="")
        self.assertEqual(response.status_code, 200, "应留在表单页并报错")
        self.assertIn("grade_year", response.context["form"].errors)
        self.assertFalse(User.objects.filter(username="gr_user").exists())

    def test_page_student_no_must_match_grade(self):
        prev = GRADE_PREV
        response = self._post_page(student_no=f"{prev}9999", grade_year=GRADE)
        self.assertEqual(response.status_code, 200)
        self.assertIn("student_no", response.context["form"].errors)
        self.assertFalse(User.objects.filter(username="gr_user").exists())

    def test_page_student_no_matching_grade_ok(self):
        response = self._post_page(student_no=f"{GRADE}9999")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.get(username="gr_user").student_no, f"{GRADE}9999")

    def test_page_teacher_needs_no_grade(self):
        response = self._post_page(username="gr_t", email="gr_t@example.com",
                                   role=Role.TEACHER, grade_year="")
        self.assertEqual(response.status_code, 302, "教师不需要年级")
        self.assertTrue(User.objects.get(username="gr_t").student_no.startswith("T"))

    # ---- API 建号 ----
    def _post_api(self, **overrides):
        data = {
            "username": "gr_api", "email": "gr_api@example.com", "real_name": "API 建号",
            "role": Role.STUDENT, "password": PASSWORD, "grade_year": GRADE,
        }
        data.update(overrides)
        token = _jwt(self.client, "gr_admin")
        return self.client.post(
            USERS_URL, data, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def test_api_student_without_grade_rejected(self):
        response = self._post_api(grade_year="")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(User.objects.filter(username="gr_api").exists())

    def test_api_student_no_mismatch_grade_rejected(self):
        prev = GRADE_PREV
        response = self._post_api(student_no=f"{prev}9999", grade_year=GRADE)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="gr_api").exists())
