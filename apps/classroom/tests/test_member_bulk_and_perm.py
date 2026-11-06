"""班级成员：越权提示（403）与「按行政班整班导入」的行为测试。

两条需求各自的验收点：
  * 越权 —— 班级**存在**时给 403 + 业务码 ``CLASSROOM_PERM_DENIED``（前端据此弹
    「权限不足」居中弹窗），班级**不存在**仍是 404；写操作越权时不得产生任何写入。
  * 整班导入 —— 源班必须是启用的行政班，重复导入靠去重变成 ``added=0/skipped=N``，
    源班成员不因导入被改动。
"""

from django.contrib.auth import get_user_model

from apps.exam.tests.base import PASSWORD, ExamBaseTest

from ..constants import CLASSROOM_PERM_DENIED, ClassType
from ..models import Classroom

User = get_user_model()


class ClassroomMemberBulkAndPermTest(ExamBaseTest):
    """成员接口的越权口径 + 整班导入。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_user(
            username="cls_owner", email="cls_owner@example.com", password=PASSWORD,
            role="teacher", real_name="班主任甲",
        )
        cls.other = User.objects.create_user(
            username="cls_other", email="cls_other@example.com", password=PASSWORD,
            role="teacher", real_name="别的老师",
        )
        # 越权用例必须用**学科班**：行政班对全体教师可见（供学科班挂靠），
        # 拿行政班做越权样本会命中 visible_to 放行，测不到 403。
        cls.subject = Classroom.objects.create(
            name="数据结构 1 班", class_type=ClassType.SUBJECT, created_by=cls.owner,
        )
        cls.admin = Classroom.objects.create(
            name="计算机 1 班", class_type=ClassType.ADMIN, created_by=cls.owner,
        )
        cls.admin_students = [
            User.objects.create_user(
                username=f"cls_bulk_{index}", email=f"cls_bulk_{index}@example.com",
                password=PASSWORD, role="student", real_name=f"整班学生{index}",
                student_no=f"CLS2026{index:03d}",
            )
            for index in range(1, 6)
        ]
        cls.admin.students.add(*cls.admin_students)

    # ------------------------------------------------------------------
    # 1. 越权读成员列表：403 + 业务码；班级不存在仍 404
    # ------------------------------------------------------------------
    def test_members_of_others_class_returns_403_with_biz_code(self):
        resp = self.get_json(
            f"/api/v1/classrooms/{self.subject.id}/students/", user=self.other,
        )
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body["code"], CLASSROOM_PERM_DENIED)
        self.assertIn("权限", body["message"])

    def test_members_of_others_class_also_403_on_detail(self):
        """详情接口与成员接口同一口径，前端两个请求都会走弹窗分支。"""
        resp = self.get_json(f"/api/v1/classrooms/{self.subject.id}/", user=self.other)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], CLASSROOM_PERM_DENIED)

    def test_members_of_missing_class_returns_404(self):
        resp = self.get_json("/api/v1/classrooms/999999/students/", user=self.owner)
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------------
    # 2. 越权写：403 且没有任何写入
    # ------------------------------------------------------------------
    def test_add_students_without_permission_writes_nothing(self):
        before = self.subject.students.count()
        resp = self.post_json(
            f"/api/v1/classrooms/{self.subject.id}/students/",
            {"student_ids": [self.student.id, self.student_b.id]},
            user=self.other,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], CLASSROOM_PERM_DENIED)
        self.assertEqual(self.subject.students.count(), before)
        self.assertFalse(self.subject.students.filter(id=self.student.id).exists())

    def test_bulk_import_without_permission_writes_nothing(self):
        resp = self.post_json(
            f"/api/v1/classrooms/{self.subject.id}/students/",
            {"from_classroom_id": self.admin.id},
            user=self.other,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], CLASSROOM_PERM_DENIED)
        self.assertEqual(self.subject.students.count(), 0)
        self.assertEqual(self.admin.students.count(), 5)

    # ------------------------------------------------------------------
    # 3. 整班导入：首次 5 人，再导一次全部跳过
    # ------------------------------------------------------------------
    def test_bulk_import_admin_class_five_students(self):
        url = f"/api/v1/classrooms/{self.subject.id}/students/"
        resp = self.post_json(url, {"from_classroom_id": self.admin.id}, user=self.owner)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["added"], 5)
        self.assertEqual(data["skipped"], 0)
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["source"], "计算机 1 班")
        self.assertEqual(self.subject.students.count(), 5)

    def test_bulk_import_twice_adds_nothing(self):
        url = f"/api/v1/classrooms/{self.subject.id}/students/"
        self.post_json(url, {"from_classroom_id": self.admin.id}, user=self.owner)
        resp = self.post_json(url, {"from_classroom_id": self.admin.id}, user=self.owner)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["added"], 0)
        self.assertEqual(data["skipped"], 5)
        self.assertEqual(data["total"], 5)
        self.assertEqual(self.subject.students.count(), 5)
        # 导入是单向的：源班成员不受影响
        self.assertEqual(self.admin.students.count(), 5)

    # ------------------------------------------------------------------
    # 4. 源班不是行政班 → 400
    # ------------------------------------------------------------------
    def test_bulk_import_from_subject_class_returns_400(self):
        another_subject = Classroom.objects.create(
            name="线性代数 1 班", class_type=ClassType.SUBJECT, created_by=self.owner,
        )
        another_subject.students.add(self.student)
        resp = self.post_json(
            f"/api/v1/classrooms/{self.subject.id}/students/",
            {"from_classroom_id": another_subject.id},
            user=self.owner,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("只能从行政班整班导入", resp.json()["message"])
        self.assertEqual(self.subject.students.count(), 0)

    # ------------------------------------------------------------------
    # 5. 源班不存在 / 已归档 → 400
    # ------------------------------------------------------------------
    def test_bulk_import_missing_admin_class_returns_400(self):
        resp = self.post_json(
            f"/api/v1/classrooms/{self.subject.id}/students/",
            {"from_classroom_id": 999999},
            user=self.owner,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("行政班不存在或已归档", resp.json()["message"])

    def test_bulk_import_archived_admin_class_returns_400(self):
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])
        resp = self.post_json(
            f"/api/v1/classrooms/{self.subject.id}/students/",
            {"from_classroom_id": self.admin.id},
            user=self.owner,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("行政班不存在或已归档", resp.json()["message"])
        self.assertEqual(self.subject.students.count(), 0)

    # ------------------------------------------------------------------
    # 6. 页面：权限弹窗 + 行政班下拉（含人数、排除已归档）
    # ------------------------------------------------------------------
    def test_detail_page_has_perm_modal_and_admin_class_options(self):
        Classroom.objects.create(
            name="已归档行政班", class_type=ClassType.ADMIN,
            created_by=self.owner, is_active=False,
        )
        self.client.force_login(self.owner)
        resp = self.client.get(f"/classrooms/{self.subject.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        self.assertIn('id="permModal"', html)
        self.assertIn("权限不足", html)
        self.assertIn('id="from-classroom"', html)
        self.assertIn("计算机 1 班（5 人）", html)
        # 归档的行政班不进下拉
        self.assertNotIn("已归档行政班", html)
