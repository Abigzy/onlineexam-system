"""学科班去重：同一学生不能在「同一授课教师 + 同一学科 + 同一年级」的多个学科班里。

规则口径（与产品确认过）：
  * 「老师」= ``Classroom.teachers``（授课教师），不是 ``head_teacher``；
  * 只有**学科班**互相约束，行政班不受限、也不参与判定；
  * 判定不了就不拦——目标班的 ``teachers`` / ``subject`` / ``grade_year`` 任一为空
    时直接放行（宁可放过，不可误伤）；
  * 冲突学生**跳过**（不打断整批），并逐个说明「卡在哪个班」，同时保持
    ``added + skipped == total`` 这条老断言恒成立。
"""

from django.contrib.auth import get_user_model

from apps.exam.tests.base import PASSWORD, ExamBaseTest

from ..constants import ClassType
from ..models import Classroom
from ..services import ClassroomService, _normalize_grade, _normalize_subject

User = get_user_model()

SUBJECT = "操作系统"
GRADE = "2026级"


class SubjectClassFixture(ExamBaseTest):
    """公共 fixture：两个老师 + 6 个学生 + 基准班「操作系统 1 班」（含 students[0]）。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.t1 = User.objects.create_user(
            username="cf_t1", email="cf_t1@example.com", password=PASSWORD,
            role="teacher", real_name="王老师",
        )
        cls.t2 = User.objects.create_user(
            username="cf_t2", email="cf_t2@example.com", password=PASSWORD,
            role="teacher", real_name="李老师",
        )
        cls.students = [
            User.objects.create_user(
                username=f"cf_s{index}", email=f"cf_s{index}@example.com",
                password=PASSWORD, role="student", real_name=f"冲突学生{index}",
                student_no=f"CF2026{index:03d}",
            )
            for index in range(1, 7)
        ]

    def setUp(self):
        super().setUp()
        # 基准班：王老师 / 操作系统 / 2026级
        self.class_a = self.make_subject_class(
            name="操作系统 1 班", teachers=[self.t1],
        )
        self.class_a.students.add(self.students[0])

    # ------------------------------------------------------------------
    # 造班工具
    # ------------------------------------------------------------------
    @staticmethod
    def make_subject_class(*, name, teachers, subject=SUBJECT, grade_year=GRADE,
                           created_by=None):
        classroom = Classroom.objects.create(
            name=name, class_type=ClassType.SUBJECT, subject=subject,
            grade_year=grade_year, created_by=created_by or (teachers or [None])[0],
        )
        if teachers:
            classroom.teachers.set(teachers)
        return classroom


class SubjectClassConflictTest(SubjectClassFixture):
    """冲突检测：三个加人入口 + 页面提示。"""

    # ------------------------------------------------------------------
    # 1. 同老师 + 同学科 + 同年级 → 跳过
    # ------------------------------------------------------------------
    def test_same_teacher_subject_grade_is_skipped(self):
        class_b = self.make_subject_class(name="操作系统 2 班", teachers=[self.t1])
        result = ClassroomService.add_students(
            class_b, [self.students[0].id, self.students[1].id],
        )
        self.assertEqual(result["added"], [self.students[1].id])
        self.assertEqual(result["missing"], [])
        # 冲突学生不能进 missing（missing 的语义是「查无此人」）
        self.assertNotIn(self.students[0].id, result["missing"])
        self.assertEqual(len(result["conflicts"]), 1)
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["student_id"], self.students[0].id)
        self.assertEqual(conflict["student_no"], "CF2026001")
        self.assertEqual(conflict["student_name"], "冲突学生1")
        self.assertEqual(conflict["classroom_id"], self.class_a.id)
        self.assertEqual(conflict["classroom_name"], "操作系统 1 班")
        self.assertEqual(
            set(class_b.students.values_list("id", flat=True)), {self.students[1].id},
        )

    # ------------------------------------------------------------------
    # 2-4. 三个维度任一不同 → 允许加入
    # ------------------------------------------------------------------
    def test_different_teacher_is_allowed(self):
        class_b = self.make_subject_class(name="操作系统 2 班", teachers=[self.t2])
        result = ClassroomService.add_students(class_b, [self.students[0].id])
        self.assertEqual(result["added"], [self.students[0].id])
        self.assertEqual(result["conflicts"], [])
        self.assertTrue(class_b.students.filter(id=self.students[0].id).exists())

    def test_different_subject_is_allowed(self):
        class_b = self.make_subject_class(
            name="数据结构 1 班", teachers=[self.t1], subject="数据结构",
        )
        result = ClassroomService.add_students(class_b, [self.students[0].id])
        self.assertEqual(result["added"], [self.students[0].id])
        self.assertEqual(result["conflicts"], [])

    def test_different_grade_is_allowed(self):
        class_b = self.make_subject_class(
            name="操作系统 1 班（2025级）", teachers=[self.t1], grade_year="2025级",
        )
        result = ClassroomService.add_students(class_b, [self.students[0].id])
        self.assertEqual(result["added"], [self.students[0].id])
        self.assertEqual(result["conflicts"], [])

    # ------------------------------------------------------------------
    # 5. 行政班不受限（既不当「冲突源」，也不做「冲突目标」）
    # ------------------------------------------------------------------
    def test_admin_classes_are_not_limited(self):
        admin_a = Classroom.objects.create(
            name="计算机 1 班", class_type=ClassType.ADMIN,
            subject=SUBJECT, grade_year=GRADE, created_by=self.t1,
        )
        admin_a.teachers.set([self.t1])
        admin_b = Classroom.objects.create(
            name="计算机 2 班", class_type=ClassType.ADMIN,
            subject=SUBJECT, grade_year=GRADE, created_by=self.t1,
        )
        admin_b.teachers.set([self.t1])
        admin_a.students.add(self.students[0])

        # 目标班是行政班：即便老师/学科/年级都一样也不校验
        result = ClassroomService.add_students(
            admin_b, [self.students[0].id, self.students[1].id],
        )
        self.assertEqual(
            result["added"], [self.students[0].id, self.students[1].id],
        )
        self.assertEqual(result["conflicts"], [])

        # 学生已经在行政班里，不影响他进学科班
        class_b = self.make_subject_class(name="操作系统 2 班", teachers=[self.t1])
        result = ClassroomService.add_students(class_b, [self.students[1].id])
        self.assertEqual(result["added"], [self.students[1].id])
        self.assertEqual(result["conflicts"], [])

    # ------------------------------------------------------------------
    # 6. 按学号路径同样生效
    # ------------------------------------------------------------------
    def test_add_students_by_no_also_skips(self):
        class_b = self.make_subject_class(name="操作系统 2 班", teachers=[self.t1])
        result = ClassroomService.add_students_by_no(
            class_b, ["CF2026001", "CF2026002", "CF9999999"],
        )
        self.assertEqual(result["added"], ["CF2026002"])
        self.assertEqual(result["missing"], ["CF9999999"])
        self.assertEqual(len(result["conflicts"]), 1)
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["student_no"], "CF2026001")
        self.assertEqual(conflict["classroom_name"], "操作系统 1 班")

    # ------------------------------------------------------------------
    # 7. 整班导入：5 人里 2 人已在同类班
    # ------------------------------------------------------------------
    def test_bulk_import_skips_conflicts(self):
        admin = Classroom.objects.create(
            name="计算机 1 班", class_type=ClassType.ADMIN, created_by=self.t1,
        )
        # students[0] 已在「操作系统 1 班」（同为王老师 / 操作系统 / 2026级）
        admin.students.add(*self.students[:5])
        class_b = self.make_subject_class(name="操作系统 2 班", teachers=[self.t1])
        # 再造第二个冲突班，验证「命中多个只取 id 最小的那个」
        class_c = self.make_subject_class(name="操作系统 3 班", teachers=[self.t1])
        class_c.students.add(self.students[1])

        result = ClassroomService.add_students_from_classroom(class_b, admin)
        self.assertEqual(result["added"], 3)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["source"], "计算机 1 班")
        self.assertEqual(result["added"] + result["skipped"], result["total"])
        self.assertEqual(len(result["conflicts"]), 2)
        by_id = {item["student_id"]: item for item in result["conflicts"]}
        self.assertEqual(by_id[self.students[0].id]["classroom_name"], "操作系统 1 班")
        self.assertEqual(
            by_id[self.students[1].id]["classroom_id"], class_c.id,
        )
        self.assertEqual(class_b.students.count(), 3)

    # ------------------------------------------------------------------
    # 8. 判定不了就不拦：teachers / subject / grade_year 为空
    # ------------------------------------------------------------------
    def test_no_check_when_target_class_cannot_be_identified(self):
        # ① 没有授课教师
        no_teacher = self.make_subject_class(name="无老师学科班", teachers=[])
        result = ClassroomService.add_students(no_teacher, [self.students[0].id])
        self.assertEqual(result["added"], [self.students[0].id])
        self.assertEqual(result["conflicts"], [])

        # ② 学科为空
        no_subject = self.make_subject_class(
            name="无学科学科班", teachers=[self.t1], subject="",
        )
        result = ClassroomService.add_students(no_subject, [self.students[0].id])
        self.assertEqual(result["added"], [self.students[0].id])
        self.assertEqual(result["conflicts"], [])

        # ③ 年级为空
        no_grade = self.make_subject_class(
            name="无年级学科班", teachers=[self.t1], grade_year="",
        )
        result = ClassroomService.add_students(no_grade, [self.students[0].id])
        self.assertEqual(result["added"], [self.students[0].id])
        self.assertEqual(result["conflicts"], [])

    # ------------------------------------------------------------------
    # 9. 返回值向后兼容：老字段一个不少
    # ------------------------------------------------------------------
    def test_payload_is_backward_compatible(self):
        class_b = self.make_subject_class(name="操作系统 2 班", teachers=[self.t1])
        added = ClassroomService.add_students(class_b, [self.students[0].id])
        self.assertEqual({"added", "missing", "conflicts"}, set(added))

        by_no = ClassroomService.add_students_by_no(class_b, ["CF2026002"])
        self.assertEqual({"added", "missing", "conflicts"}, set(by_no))

        admin = Classroom.objects.create(
            name="计算机 3 班", class_type=ClassType.ADMIN, created_by=self.t1,
        )
        admin.students.add(*self.students[2:5])
        bulk = ClassroomService.add_students_from_classroom(class_b, admin)
        self.assertEqual(
            {"added", "skipped", "source", "total", "conflicts"}, set(bulk),
        )
        self.assertEqual(bulk["added"] + bulk["skipped"], bulk["total"])

    # ------------------------------------------------------------------
    # 10. 页面：冲突弹窗容器
    # ------------------------------------------------------------------
    def test_detail_page_has_conflict_modal(self):
        self.client.force_login(self.t1)
        resp = self.client.get(f"/classrooms/{self.class_a.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn('id="conflictModal"', html)
        self.assertIn('id="conflict-list"', html)
        self.assertIn("部分学生未加入", html)
        self.assertIn("modal-dialog-centered", html)

    # ------------------------------------------------------------------
    # 接口：conflicts 透传到前端（学号导入路径）
    # ------------------------------------------------------------------
    def test_api_returns_conflicts(self):
        class_b = self.make_subject_class(name="操作系统 2 班", teachers=[self.t1])
        resp = self.post_json(
            f"/api/v1/classrooms/{class_b.id}/students/",
            {"student_nos": ["CF2026001", "CF2026002"]},
            user=self.t1,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["added"], ["CF2026002"])
        self.assertEqual(len(data["conflicts"]), 1)
        self.assertEqual(data["conflicts"][0]["classroom_name"], "操作系统 1 班")

    def test_api_bulk_import_all_skipped_reports_zero_added(self):
        admin = Classroom.objects.create(
            name="计算机 4 班", class_type=ClassType.ADMIN, created_by=self.t1,
        )
        admin.students.add(self.students[0])
        class_b = self.make_subject_class(name="操作系统 2 班", teachers=[self.t1])
        resp = self.post_json(
            f"/api/v1/classrooms/{class_b.id}/students/",
            {"from_classroom_id": admin.id},
            user=self.t1,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["added"], 0)
        self.assertEqual(data["skipped"], 1)
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["conflicts"]), 1)


class SubjectConflictNormalizationTest(SubjectClassFixture):
    """年级 / 学科写法不一致时不能漏拦（真实库里 ``'2026级'`` 与 ``'2026'`` 混写）。

    背景：学生 20260001 已在「操作系统 1 班」（``grade_year='2026级'``）里，
    仍能加入「操作系统 2 班」（``grade_year='2026'``），因为旧实现用 SQL 精确相等比较。
    """

    # ------------------------------------------------------------------
    # 1. '2026级' vs '2026' —— 归一化后同年级 → 拦
    # ------------------------------------------------------------------
    def test_grade_with_and_without_suffix_conflicts(self):
        class_b = self.make_subject_class(
            name="操作系统 2 班", teachers=[self.t1], grade_year="2026",
        )
        result = ClassroomService.add_students(
            class_b, [self.students[0].id, self.students[1].id],
        )
        self.assertEqual(result["added"], [self.students[1].id])
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["student_id"], self.students[0].id)
        self.assertEqual(result["conflicts"][0]["classroom_id"], self.class_a.id)
        self.assertEqual(result["conflicts"][0]["classroom_name"], "操作系统 1 班")
        self.assertFalse(class_b.students.filter(id=self.students[0].id).exists())

    # ------------------------------------------------------------------
    # 2. '2026' vs '2027级' —— 真不同年级 → 放行
    # ------------------------------------------------------------------
    def test_different_grade_year_still_allowed(self):
        class_b = self.make_subject_class(
            name="操作系统 2 班", teachers=[self.t1], grade_year="2027级",
        )
        result = ClassroomService.add_students(class_b, [self.students[0].id])
        self.assertEqual(result["added"], [self.students[0].id])
        self.assertEqual(result["conflicts"], [])
        self.assertTrue(class_b.students.filter(id=self.students[0].id).exists())

    # ------------------------------------------------------------------
    # 3. '2026届' vs '2026级' —— 同一年份、后缀不同 → 拦
    # ------------------------------------------------------------------
    def test_grade_suffix_jie_vs_ji_conflicts(self):
        class_b = self.make_subject_class(
            name="操作系统 2 班", teachers=[self.t1], grade_year="2026届",
        )
        result = ClassroomService.add_students(class_b, [self.students[0].id])
        self.assertEqual(result["added"], [])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["classroom_name"], "操作系统 1 班")

    # ------------------------------------------------------------------
    # 4. 学科带空格：'操作系统 ' vs '操作系统' → 拦（双向）
    # ------------------------------------------------------------------
    def test_subject_with_trailing_space_conflicts(self):
        # ① 目标班学科带尾随空格，库里是干净的
        class_b = self.make_subject_class(
            name="操作系统 2 班", teachers=[self.t1], subject="操作系统 ",
        )
        result = ClassroomService.add_students(class_b, [self.students[0].id])
        self.assertEqual(result["added"], [])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["classroom_id"], self.class_a.id)

        # ② 库里带空格，目标班干净——比对是对称的
        class_c = self.make_subject_class(
            name="操作系统 3 班", teachers=[self.t1], subject="操作系统\u3000",
        )
        class_c.students.add(self.students[1])
        class_d = self.make_subject_class(name="操作系统 4 班", teachers=[self.t1])
        result = ClassroomService.add_students(class_d, [self.students[1].id])
        self.assertEqual(result["added"], [])
        self.assertEqual(result["conflicts"][0]["classroom_id"], class_c.id)

    # ------------------------------------------------------------------
    # 5. 归一化后仍是不同学科 → 放行
    # ------------------------------------------------------------------
    def test_different_subject_after_normalization_allowed(self):
        class_b = self.make_subject_class(
            name="计算机网络 1 班", teachers=[self.t1], subject="计算机网络",
        )
        result = ClassroomService.add_students(class_b, [self.students[0].id])
        self.assertEqual(result["added"], [self.students[0].id])
        self.assertEqual(result["conflicts"], [])

    # ------------------------------------------------------------------
    # 6. 归一化函数本身（边界：空、非数字年级、全角空格）
    # ------------------------------------------------------------------
    def test_normalizer_rules(self):
        self.assertEqual(_normalize_grade("2026级"), "2026")
        self.assertEqual(_normalize_grade("2026"), "2026")
        self.assertEqual(_normalize_grade("2026届"), "2026")
        self.assertEqual(_normalize_grade(" 2026 级 "), "2026")
        self.assertEqual(_normalize_grade(""), "")
        self.assertEqual(_normalize_grade(None), "")
        # 取不到年份就不硬猜，只做 strip + lower
        self.assertEqual(_normalize_grade(" 大二 "), "大二")
        self.assertEqual(_normalize_grade("N/A"), "n/a")
        self.assertNotEqual(_normalize_grade("2026级"), _normalize_grade("2027级"))

        self.assertEqual(_normalize_subject("操作系统"), "操作系统")
        self.assertEqual(_normalize_subject("操作系统 "), "操作系统")
        self.assertEqual(_normalize_subject(" 操作系统"), "操作系统")
        self.assertEqual(_normalize_subject("操作 系统"), "操作系统")
        self.assertEqual(_normalize_subject("操作系统\u3000"), "操作系统")
        self.assertEqual(_normalize_subject("OS"), "os")
        self.assertEqual(_normalize_subject(""), "")
        self.assertEqual(_normalize_subject(None), "")
