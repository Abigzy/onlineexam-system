"""学科老师建学科班时，自动把自己绑进授课名单。

规则口径：
  * 只在「学科班 + 创建者是教师（非管理员）」时生效；
  * 与传入的 ``teacher_ids`` **合并**，不覆盖、不重复；
  * 管理员代建 / 命令行（actor 为 None）代建不自动绑定——他们不是这门课的老师，
    授课教师仍完全由 ``teacher_ids`` 决定。
"""

from django.contrib.auth import get_user_model

from apps.exam.tests.base import PASSWORD, ExamBaseTest

from ..constants import ClassType
from ..models import Classroom
from ..services import ClassroomService

User = get_user_model()


class TeacherAutoBindTest(ExamBaseTest):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_teacher = User.objects.create_user(
            username="ab_teacher", email="ab_teacher@example.com", password=PASSWORD,
            role="teacher", real_name="另一位老师",
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _teacher_ids(classroom) -> set:
        return set(classroom.teachers.values_list("id", flat=True))

    # ------------------------------------------------------------------
    def test_teacher_creates_subject_class_binds_self(self):
        """教师建学科班、不传 teacher_ids → 授课名单只有自己。"""
        classroom = ClassroomService.create(
            self.teacher, name="数据结构 1 班", class_type=ClassType.SUBJECT,
        )
        self.assertEqual(self._teacher_ids(classroom), {self.teacher.id})

    def test_teacher_merges_other_teachers(self):
        """教师建学科班、传了别的老师 → 自己 + 对方（合并，不覆盖）。"""
        classroom = ClassroomService.create(
            self.teacher, name="数据结构 1 班", class_type=ClassType.SUBJECT,
            teacher_ids=[self.other_teacher.id],
        )
        self.assertEqual(
            self._teacher_ids(classroom),
            {self.teacher.id, self.other_teacher.id},
        )

    def test_actor_already_in_teacher_ids_not_duplicated(self):
        """传的列表已包含自己 → 不重复占两条。"""
        classroom = ClassroomService.create(
            self.teacher, name="数据结构 1 班", class_type=ClassType.SUBJECT,
            teacher_ids=[self.teacher.id],
        )
        self.assertEqual(classroom.teachers.count(), 1)
        self.assertEqual(self._teacher_ids(classroom), {self.teacher.id})

    def test_teacher_creates_admin_class_not_bound(self):
        """行政班不自动绑定：班主任本来就是行政班的制度，授课名单不掺和。"""
        classroom = ClassroomService.create(
            self.teacher, name="计算机 1 班", class_type=ClassType.ADMIN,
            head_teacher_id=self.teacher.id,
        )
        self.assertEqual(classroom.teachers.count(), 0)

    def test_admin_creates_subject_class_not_bound(self):
        """管理员代建学科班、不传 teacher_ids → 授课名单为空（管理员不是任课老师）。"""
        classroom = ClassroomService.create(
            self.admin, name="数据结构 1 班", class_type=ClassType.SUBJECT,
        )
        self.assertEqual(classroom.teachers.count(), 0)

    def test_admin_creates_subject_class_with_teacher_only_that_teacher(self):
        """管理员代建时指定老师 → 只有该老师，不含管理员自己。"""
        classroom = ClassroomService.create(
            self.admin, name="数据结构 1 班", class_type=ClassType.SUBJECT,
            teacher_ids=[self.other_teacher.id],
        )
        self.assertEqual(self._teacher_ids(classroom), {self.other_teacher.id})

    def test_api_teacher_creates_subject_class_binds_self(self):
        """走 API（POST /api/v1/classrooms/）：返回体与库里都应含创建者。"""
        resp = self.post_json(
            "/api/v1/classrooms/",
            {"name": "数据结构 1 班", "class_type": "subject", "subject": "数据结构"},
            user=self.teacher,
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["data"]
        self.assertEqual({t["id"] for t in data["teachers"]}, {self.teacher.id})

        classroom = Classroom.objects.get(pk=data["id"])
        self.assertEqual(self._teacher_ids(classroom), {self.teacher.id})
