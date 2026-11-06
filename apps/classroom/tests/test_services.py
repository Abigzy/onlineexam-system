"""班级模块服务层测试。"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.common.exceptions import BusinessError
from apps.exam.tests.base import PASSWORD

from ..constants import ClassType
from ..models import Classroom
from ..services import ClassroomService

User = get_user_model()


class ClassroomServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(
            username="c_teacher", email="c_teacher@example.com", password=PASSWORD,
            role="teacher", real_name="班主任",
        )
        cls.teacher_b = User.objects.create_user(
            username="c_teacher_b", email="c_teacher_b@example.com", password=PASSWORD,
            role="teacher", real_name="授课老师",
        )
        cls.student = User.objects.create_user(
            username="c_student", email="c_student@example.com", password=PASSWORD,
            role="student", real_name="学生甲", student_no="C2026001",
        )
        cls.student_b = User.objects.create_user(
            username="c_student_b", email="c_student_b@example.com", password=PASSWORD,
            role="student", real_name="学生乙", student_no="C2026002",
        )

    def test_create_basic(self):
        classroom = ClassroomService.create(
            self.teacher, name="计算机 2401 班", grade_year="2026级",
            subject="数据结构", head_teacher_id=self.teacher.id,
            teacher_ids=[self.teacher_b.id],
        )
        self.assertEqual(classroom.name, "计算机 2401 班")
        self.assertEqual(classroom.head_teacher_id, self.teacher.id)
        self.assertEqual(list(classroom.teachers.all()), [self.teacher_b])
        self.assertEqual(classroom.created_by_id, self.teacher.id)

    def test_create_rejects_non_teacher_head(self):
        with self.assertRaises(BusinessError):
            ClassroomService.create(
                self.teacher, name="坏班", head_teacher_id=self.student.id,
            )

    def test_create_rejects_bad_teacher_in_list(self):
        with self.assertRaises(BusinessError):
            ClassroomService.create(
                self.teacher, name="坏班", teacher_ids=[self.student.id],
            )

    def test_add_students_only_student_role(self):
        classroom = ClassroomService.create(self.teacher, name="班")
        result = ClassroomService.add_students(
            classroom, [self.student.id, self.student_b.id, self.teacher.id],
        )
        # 教师 id 不是学生角色，被记为 missing
        self.assertEqual(result["added"], sorted([self.student.id, self.student_b.id]))
        self.assertEqual(result["missing"], [self.teacher.id])
        self.assertEqual(set(classroom.students.values_list("id", flat=True)),
                         {self.student.id, self.student_b.id})

    def test_add_students_by_no(self):
        classroom = ClassroomService.create(self.teacher, name="班")
        result = ClassroomService.add_students_by_no(
            classroom, ["C2026001", "C2026002", "C9999999"],
        )
        self.assertEqual(result["added"], ["C2026001", "C2026002"])
        self.assertEqual(result["missing"], ["C9999999"])

    def test_remove_students(self):
        classroom = ClassroomService.create(self.teacher, name="班")
        ClassroomService.add_students(classroom, [self.student.id, self.student_b.id])
        removed = ClassroomService.remove_students(classroom, [self.student.id])
        self.assertEqual(removed, 1)
        self.assertEqual(list(classroom.students.values_list("id", flat=True)),
                         [self.student_b.id])

    def test_archive_soft_delete(self):
        classroom = ClassroomService.create(self.teacher, name="班")
        ClassroomService.archive(classroom)
        classroom.refresh_from_db()
        self.assertFalse(classroom.is_active)
        self.assertTrue(Classroom.objects.filter(pk=classroom.pk).exists())

    # ------------------------------------------------------------------
    # 班级类型 / 行政班挂靠 / 删除
    # ------------------------------------------------------------------
    def test_create_subject_class_links_admin_class(self):
        admin_class = ClassroomService.create(
            self.teacher, name="计算机 1 班", class_type=ClassType.ADMIN,
        )
        subject_class = ClassroomService.create(
            self.teacher, name="数据结构 1 班", class_type=ClassType.SUBJECT,
            admin_class_id=admin_class.id,
        )
        self.assertEqual(subject_class.class_type, ClassType.SUBJECT)
        self.assertEqual(subject_class.admin_class_id, admin_class.id)

    def test_admin_class_cannot_link_admin_class(self):
        admin_class = ClassroomService.create(
            self.teacher, name="计算机 1 班", class_type=ClassType.ADMIN,
        )
        with self.assertRaises(BusinessError):
            ClassroomService.create(
                self.teacher, name="坏班", class_type=ClassType.ADMIN,
                admin_class_id=admin_class.id,
            )

    def test_subject_class_rejects_non_admin_target(self):
        other = ClassroomService.create(
            self.teacher, name="另一个学科班", class_type=ClassType.SUBJECT,
        )
        with self.assertRaises(BusinessError):
            ClassroomService.create(
                self.teacher, name="坏班 2", class_type=ClassType.SUBJECT,
                admin_class_id=other.id,
            )

    def test_update_switch_to_admin_clears_admin_class(self):
        admin_class = ClassroomService.create(
            self.teacher, name="计算机 1 班", class_type=ClassType.ADMIN,
        )
        subject_class = ClassroomService.create(
            self.teacher, name="数据结构 1 班", class_type=ClassType.SUBJECT,
            admin_class_id=admin_class.id,
        )
        ClassroomService.update(subject_class, class_type=ClassType.ADMIN)
        subject_class.refresh_from_db()
        self.assertIsNone(subject_class.admin_class_id)

    def test_delete_physical(self):
        classroom = ClassroomService.create(self.teacher, name="班")
        pk = classroom.pk
        ClassroomService.delete(classroom)
        self.assertFalse(Classroom.objects.filter(pk=pk).exists())

    # ------------------------------------------------------------------
    # 学科班不设班主任 / 授课教师可管理
    # ------------------------------------------------------------------
    def test_subject_class_rejects_head_teacher(self):
        with self.assertRaises(BusinessError):
            ClassroomService.create(
                self.teacher, name="数据结构 1 班", class_type=ClassType.SUBJECT,
                head_teacher_id=self.teacher.id,
            )

    def test_switch_to_subject_clears_head_teacher(self):
        classroom = ClassroomService.create(
            self.teacher, name="某行政班", class_type=ClassType.ADMIN,
            head_teacher_id=self.teacher.id,
        )
        ClassroomService.update(classroom, class_type=ClassType.SUBJECT)
        classroom.refresh_from_db()
        self.assertIsNone(classroom.head_teacher_id)

    def test_teacher_in_teacher_list_can_manage(self):
        # teacher_b 建班，只把 teacher 放进授课名单
        classroom = ClassroomService.create(
            self.teacher_b, name="学科班", class_type=ClassType.SUBJECT,
            teacher_ids=[self.teacher.id],
        )
        # 授课教师（不是创建人、不是班主任）也能管理自己教的班
        self.assertTrue(ClassroomService.can_manage(self.teacher, classroom))
        self.assertIn(classroom, Classroom.objects.manageable_by(self.teacher))
        # 学生不能管理
        self.assertFalse(ClassroomService.can_manage(self.student, classroom))

    def test_can_manage(self):
        owned = ClassroomService.create(self.teacher, name="我建的")
        headed = ClassroomService.create(
            self.teacher_b, name="他建的", head_teacher_id=self.teacher.id,
        )
        others = ClassroomService.create(self.teacher_b, name="别人的")
        self.assertTrue(ClassroomService.can_manage(self.teacher, owned))
        self.assertTrue(ClassroomService.can_manage(self.teacher, headed))
        self.assertFalse(ClassroomService.can_manage(self.teacher, others))
        self.assertFalse(ClassroomService.can_manage(self.student, owned))


class ClassroomQuerySetTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(
            username="q_teacher", email="q_teacher@example.com", password=PASSWORD,
            role="teacher", real_name="老师",
        )
        cls.teacher_b = User.objects.create_user(
            username="q_teacher_b", email="q_teacher_b@example.com", password=PASSWORD,
            role="teacher", real_name="另一位老师",
        )
        cls.student = User.objects.create_user(
            username="q_student", email="q_student@example.com", password=PASSWORD,
            role="student", real_name="学生", student_no="Q2026001",
        )
        cls.other = User.objects.create_user(
            username="q_other", email="q_other@example.com", password=PASSWORD,
            role="student", real_name="路人", student_no="Q2026002",
        )
        cls.c1 = Classroom.objects.create(
            name="一班", created_by=cls.teacher, head_teacher=cls.teacher,
        )
        cls.c1.students.add(cls.student)
        # 二班由另一位老师创建，当前老师只是授课（teachers），不算「可管理」
        cls.c2 = Classroom.objects.create(name="二班", created_by=cls.teacher_b)
        cls.c2.teachers.add(cls.teacher)

    def test_visible_to_student(self):
        self.assertEqual(
            list(Classroom.objects.visible_to(self.student).order_by("id")), [self.c1],
        )

    def test_visible_to_teacher(self):
        self.assertEqual(
            set(Classroom.objects.visible_to(self.teacher)),
            {self.c1, self.c2},
        )

    def test_manageable_by_teacher(self):
        # c1 自己创建兼班主任；c2 自己只是授课教师 —— 新规则：授课也算「自己教的班」，可管理
        self.assertEqual(
            set(Classroom.objects.manageable_by(self.teacher)),
            {self.c1, self.c2},
        )
        # 完全无关的教师不可管理
        self.assertEqual(
            list(Classroom.objects.manageable_by(self.teacher_b).order_by("id")),
            [self.c2],
        )

    def test_search(self):
        self.assertEqual(list(Classroom.objects.search("一班")), [self.c1])
