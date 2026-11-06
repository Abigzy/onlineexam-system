"""班级模块接口测试。"""

from django.contrib.auth import get_user_model

from apps.exam.tests.base import ExamBaseTest

from ..constants import CLASSROOM_PERM_DENIED
from ..models import Classroom

User = get_user_model()


class ClassroomAPITest(ExamBaseTest):
    """班级 CRUD 与成员管理的接口行为。"""

    def setUp(self):
        super().setUp()
        # ExamBaseTest 为「考试测试」建了一个班；班级模块要的是干净的班级环境，
        # 这里把它删掉，保证「空列表」「计数」这类断言不受影响。
        Classroom.objects.filter(name="考试测试班").delete()

    def _create(self, name="接口班", user=None, **extra):
        payload = {"name": name}
        payload.update(extra)
        return self.post_json("/api/v1/classrooms/", payload, user=user)

    def test_list_empty(self):
        resp = self.get_json("/api/v1/classrooms/", user=self.teacher)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["count"], 0)

    def test_create_by_teacher(self):
        resp = self._create(user=self.teacher)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["data"]
        self.assertEqual(data["name"], "接口班")
        self.assertTrue(data["is_active"])

    def test_create_forbidden_for_student(self):
        resp = self._create(user=self.student)
        self.assertEqual(resp.status_code, 403)

    def test_visible_scope(self):
        cid = self._create(user=self.teacher).json()["data"]["id"]
        # 教师可见自己建的班
        resp = self.get_json("/api/v1/classrooms/", user=self.teacher)
        self.assertEqual(resp.json()["data"]["count"], 1)
        # 学生未加入 → 看不到
        resp = self.get_json("/api/v1/classrooms/", user=self.student)
        self.assertEqual(resp.json()["data"]["count"], 0)
        # 加入后可见
        classroom = Classroom.objects.get(pk=cid)
        classroom.students.add(self.student)
        resp = self.get_json("/api/v1/classrooms/", user=self.student)
        self.assertEqual(resp.json()["data"]["count"], 1)

    def test_detail_403_when_not_visible(self):
        """班级存在但不在可见范围 → 403（不再是 404）。

        口径变更：班级 id 不是敏感资源，越权应给「权限不足」的明确提示；
        只有班级真的不存在才 404（见 test_detail_404_when_missing）。
        """
        cid = self._create(user=self.teacher).json()["data"]["id"]
        resp = self.get_json(f"/api/v1/classrooms/{cid}/", user=self.student_b)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], CLASSROOM_PERM_DENIED)

    def test_detail_404_when_missing(self):
        resp = self.get_json("/api/v1/classrooms/999999/", user=self.teacher)
        self.assertEqual(resp.status_code, 404)

    def test_update_by_manager(self):
        cid = self._create(user=self.teacher).json()["data"]["id"]
        resp = self.patch_json(
            f"/api/v1/classrooms/{cid}/", {"name": "改名班", "grade_year": "2027级"},
            user=self.teacher,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["name"], "改名班")

    def test_update_403_when_not_manager(self):
        """学生改班 → 403（班级存在，只是没权限）。"""
        cid = self._create(user=self.teacher).json()["data"]["id"]
        resp = self.patch_json(f"/api/v1/classrooms/{cid}/", {"name": "x"}, user=self.student)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], CLASSROOM_PERM_DENIED)

    def test_delete_class(self):
        cid = self._create(user=self.teacher).json()["data"]["id"]
        resp = self.delete_json(f"/api/v1/classrooms/{cid}/", user=self.teacher)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Classroom.objects.filter(pk=cid).exists())

    def test_create_subject_class_with_admin_class(self):
        admin_id = self._create(
            name="计算机 1 班", class_type="admin", user=self.teacher,
        ).json()["data"]["id"]
        resp = self._create(
            name="数据结构 1 班", class_type="subject",
            admin_class_id=admin_id, user=self.teacher,
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["data"]
        self.assertEqual(data["class_type"], "subject")
        self.assertEqual(data["admin_class"]["id"], admin_id)

    def test_admin_class_cannot_link(self):
        admin_id = self._create(
            name="计算机 1 班", class_type="admin", user=self.teacher,
        ).json()["data"]["id"]
        resp = self._create(
            name="坏班", class_type="admin", admin_class_id=admin_id, user=self.teacher,
        )
        self.assertEqual(resp.status_code, 400)

    def test_filter_by_class_type(self):
        self._create(name="计算机 1 班", class_type="admin", user=self.teacher)
        self._create(name="数据结构 1 班", class_type="subject", user=self.teacher)
        resp = self.get_json("/api/v1/classrooms/?class_type=admin", user=self.teacher)
        rows = resp.json()["data"]["results"]
        self.assertEqual([r["name"] for r in rows], ["计算机 1 班"])

    def test_add_students_by_no(self):
        cid = self._create(user=self.teacher).json()["data"]["id"]
        resp = self.post_json(
            f"/api/v1/classrooms/{cid}/students/",
            {"student_nos": [self.student.student_no, "NO-SUCH"]},
            user=self.teacher,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["added"], [self.student.student_no])
        self.assertEqual(data["missing"], ["NO-SUCH"])

    def test_members_list_and_remove(self):
        cid = self._create(user=self.teacher).json()["data"]["id"]
        self.post_json(
            f"/api/v1/classrooms/{cid}/students/",
            {"student_ids": [self.student.id, self.student_b.id]},
            user=self.teacher,
        )
        resp = self.get_json(f"/api/v1/classrooms/{cid}/students/", user=self.teacher)
        self.assertEqual(resp.json()["data"]["count"], 2)

        # DELETE 带 body：手动发（基类 delete_json 不支持 body）
        resp = self.client.delete(
            f"/api/v1/classrooms/{cid}/students/",
            {"student_ids": [self.student.id]},
            content_type="application/json",
            **self.auth(self.teacher),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["removed"], 1)

    def test_options_teacher_only(self):
        self._create(user=self.teacher)
        resp = self.get_json("/api/v1/classrooms/options/", user=self.teacher)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["count"], 1)
        resp = self.get_json("/api/v1/classrooms/options/", user=self.student)
        self.assertEqual(resp.status_code, 403)

    def test_admin_visible_all(self):
        self._create(user=self.teacher)
        resp = self.get_json("/api/v1/classrooms/", user=self.admin)
        self.assertEqual(resp.json()["data"]["count"], 1)
