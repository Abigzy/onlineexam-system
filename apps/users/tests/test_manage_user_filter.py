"""管理端「用户管理」列表：学生学科列（3a）与班级筛选（3b）。

覆盖：
  * 学生学科来自其所在的**学科班**（不是 ``User.subject``），多学科用「、」连接；
  * 没加入任何学科班的学生显示「未加入学科班」；教师仍显示自己的任教学科；
  * 班级筛选对**行政班与学科班**都生效（只带出该班学生，教师 / 管理员 /
    别的班的学生不被带入），可与 role 组合；非法 ``classroom=abc`` 只忽略该条件，不报 500；
  * 学科筛选同时覆盖两条路径：教师的 ``User.subject`` 与学生的学科班，两者可与
    ``classroom`` 组合（交集而不是「学科条件先把学生清空」）；
  * 下拉按「行政班 / 学科班」两个 optgroup 分组，某一类为空时不渲染空分组；
  * 翻页链接保留 ``classroom``；
  * 查询数不随数据量增长（新增计算不引入 N+1）。
"""

import re

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.classroom.constants import ClassType
from apps.classroom.models import Classroom
from apps.common.constants import Role

User = get_user_model()

FAST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
PASSWORD = "Exam@123456"
LIST_URL = "/manage/users/"


@override_settings(PASSWORD_HASHERS=FAST_HASHERS)
class ManageUserFilterBaseTest(TestCase):
    """夹具：管理员 + 教师 + 三个学生；两个行政班、两个学科班。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="mf_admin", email="mf_admin@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="筛选管理员",
        )
        cls.teacher = User.objects.create_user(
            username="mf_teacher", email="mf_teacher@example.com", password=PASSWORD,
            role=Role.TEACHER, real_name="赵老师", subject="数据结构",
        )
        cls.teacher_bare = User.objects.create_user(
            username="mf_teacher_bare", email="mf_teacher_bare@example.com",
            password=PASSWORD, role=Role.TEACHER, real_name="钱老师",
        )
        cls.student_a = User.objects.create_user(
            username="mf_student_a", email="mf_student_a@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="学生甲", student_no="MF2026001",
        )
        cls.student_b = User.objects.create_user(
            username="mf_student_b", email="mf_student_b@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="学生乙", student_no="MF2026002",
        )
        cls.student_free = User.objects.create_user(
            username="mf_student_free", email="mf_student_free@example.com",
            password=PASSWORD, role=Role.STUDENT, real_name="学生丙", student_no="MF2026003",
        )

        # 行政班：class_a 有学生甲 + 学生乙，class_b 只有学生乙所在的另一个班
        cls.class_a = Classroom.objects.create(
            name="计科 1 班", grade_year="2026", class_type=ClassType.ADMIN,
        )
        cls.class_b = Classroom.objects.create(
            name="计科 2 班", grade_year="2026", class_type=ClassType.ADMIN,
        )
        cls.class_a.students.add(cls.student_a, cls.student_b)
        cls.class_b.students.add(cls.student_free)

        # 学科班：学生甲的「数据结构」+「操作系统」，学生乙只有「数据结构」
        cls.subject_ds = Classroom.objects.create(
            name="数据结构 1 班", subject="数据结构", class_type=ClassType.SUBJECT,
            admin_class=cls.class_a,
        )
        cls.subject_os = Classroom.objects.create(
            name="操作系统 1 班", subject="操作系统", class_type=ClassType.SUBJECT,
            admin_class=cls.class_a,
        )
        cls.subject_ds_copy = Classroom.objects.create(
            name="数据结构 2 班", subject="数据结构", class_type=ClassType.SUBJECT,
            admin_class=cls.class_a,
        )
        cls.subject_ds.students.add(cls.student_a)
        cls.subject_os.students.add(cls.student_a)
        cls.subject_ds_copy.students.add(cls.student_a, cls.student_b)

    def setUp(self):
        self.client.force_login(self.admin)

    def _row_html(self, username) -> str:
        """只取某个用户的整行 HTML，避免断言串到别的行 / 筛选下拉里。"""
        response = self.client.get(LIST_URL, {"search": username})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        for chunk in body.split("<tr"):
            if f">{username}<" in chunk:
                return chunk
        self.fail(f"列表里找不到用户 {username} 的行")

    def _cell(self, username, index) -> str:
        """取该行的第 index 个 <td>（从 0 起）。"""
        row = self._row_html(username)
        return row.split("<td")[index + 1].split("</td>")[0]


# ----------------------------------------------------------------------
# 3a：学生学科列
# ----------------------------------------------------------------------
class StudentSubjectColumnTest(ManageUserFilterBaseTest):
    """学生的「学科」列显示其所在学科班的学科。"""

    def test_student_subject_from_subject_classroom(self):
        """学生甲的「数据结构」出现在列表页（学生本身 User.subject 是空的）。"""
        self.assertFalse(self.student_a.subject)
        response = self.client.get(LIST_URL, {"search": "mf_student_a"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "数据结构")
        # 视图已经把学科挂到当前页的对象上，模板不需要再查库
        row = response.context["page_obj"].object_list[0]
        self.assertEqual(sorted(row.subject_list), ["操作系统", "数据结构"])

    def test_student_multiple_subjects_joined_by_separator(self):
        """学生甲在三个学科班（两个同叫「数据结构」）→ 去重后「数据结构、操作系统」。"""
        expected = "、".join(sorted(["数据结构", "操作系统"]))
        cell = self._cell("mf_student_a", 4)
        visible = re.search(r'title="[^"]*">([^<]*)</span>', cell).group(1)
        self.assertEqual(visible, expected)          # 用「、」连接，且已排序
        self.assertEqual(visible.count("数据结构"), 1)  # 同名学科去重

    def test_student_without_subject_classroom(self):
        """学生丙只在行政班，没有学科班 → 「未加入学科班」。"""
        cell = self._cell("mf_student_free", 4)
        self.assertIn("未加入学科班", cell)
        self.assertNotIn("数据结构", cell)

    def test_teacher_column_unchanged(self):
        """教师：有学科显示学科，没绑定显示「未绑定」；管理员显示 —。"""
        self.assertIn("数据结构", self._cell("mf_teacher", 4))
        self.assertIn("未绑定", self._cell("mf_teacher_bare", 4))
        admin_cell = self._cell("mf_admin", 4)
        self.assertIn("—", admin_cell)
        self.assertNotIn("未加入学科班", admin_cell)

    def test_subject_column_does_not_break_layout(self):
        """多学科时列宽受限（text-truncate + title 显示完整值）。"""
        cell = self._cell("mf_student_a", 4)
        self.assertIn("text-truncate", cell)
        self.assertIn('title="操作系统、数据结构"', cell)


# ----------------------------------------------------------------------
# 3b：班级筛选（行政班 + 学科班）
# ----------------------------------------------------------------------
class ClassroomFilterTest(ManageUserFilterBaseTest):
    """按班级过滤，行政班与学科班同一口径。"""

    def test_filter_by_admin_classroom(self):
        """只返回该班学生；不在班里的教师 / 管理员不被带入。"""
        response = self.client.get(LIST_URL, {"classroom": str(self.class_a.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mf_student_a")
        self.assertContains(response, "mf_student_b")
        self.assertNotContains(response, "mf_student_free")
        self.assertNotContains(response, "mf_teacher")
        self.assertNotContains(response, "mf_admin")
        # M2M join 会重复行 → 必须 distinct，甲乙各出现一次
        self.assertEqual(response.context["total_count"], 2)

    def test_filter_by_subject_classroom(self):
        """学科班 id 也能筛：只带出「数据结构 1 班」的学生甲，乙（在 2 班）不被带入。

        旧口径是「传学科班 id → 0 条」（过滤器里写死 ``class_type=ADMIN``），
        本次放开为「班级 id 唯一即可」，故断言反转。
        """
        response = self.client.get(LIST_URL, {"classroom": str(self.subject_ds.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mf_student_a")
        self.assertNotContains(response, "mf_student_b")
        self.assertNotContains(response, "mf_student_free")
        self.assertNotContains(response, "mf_teacher")
        self.assertEqual(response.context["total_count"], 1)

    def test_filter_by_subject_classroom_combined_with_role(self):
        """学科班 + role=student 组合生效：班里塞个老师也筛不出来。"""
        self.subject_ds.students.add(self.teacher)

        only_class = self.client.get(LIST_URL, {"classroom": str(self.subject_ds.pk)})
        self.assertContains(only_class, "mf_student_a")
        self.assertContains(only_class, "mf_teacher")
        self.assertEqual(only_class.context["total_count"], 2)

        combined = self.client.get(
            LIST_URL, {"classroom": str(self.subject_ds.pk), "role": Role.STUDENT}
        )
        self.assertEqual(combined.status_code, 200)
        self.assertContains(combined, "mf_student_a")
        self.assertNotContains(combined, "mf_teacher")
        self.assertEqual(combined.context["total_count"], 1)

    def test_filter_by_admin_classroom_combined_with_role(self):
        """行政班 + role=student 组合生效（班级里若有非学生成员会被挡掉）。"""
        # 把管理员塞进行政班，验证 role 条件仍然起作用
        self.class_b.students.add(self.admin)

        only_class = self.client.get(LIST_URL, {"classroom": str(self.class_b.pk)})
        self.assertContains(only_class, "mf_student_free")
        self.assertContains(only_class, "mf_admin")

        combined = self.client.get(
            LIST_URL, {"classroom": str(self.class_b.pk), "role": Role.STUDENT}
        )
        self.assertEqual(combined.status_code, 200)
        self.assertContains(combined, "mf_student_free")
        self.assertNotContains(combined, "mf_admin")
        self.assertEqual(combined.context["total_count"], 1)

    def test_filter_by_admin_classroom_combined_with_search(self):
        """行政班 + 关键词搜索组合生效（空结果走既有空态）。"""
        hit = self.client.get(LIST_URL, {"classroom": str(self.class_a.pk), "search": "学生甲"})
        self.assertContains(hit, "mf_student_a")
        self.assertNotContains(hit, "mf_student_b")

        miss = self.client.get(LIST_URL, {"classroom": str(self.class_b.pk), "search": "学生甲"})
        self.assertContains(miss, "没有符合条件的用户")

    def test_invalid_classroom_is_ignored(self):
        """classroom=abc 不报 500：200 且该条件不生效。"""
        response = self.client.get(LIST_URL, {"classroom": "abc"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], User.objects.count())

    def test_classroom_preserved_in_pagination_links(self):
        """翻页链接保留 classroom 参数。"""
        for index in range(6):
            Classroom.objects.create(
                name=f"批量班{index}", class_type=ClassType.ADMIN,
            )
        for index in range(15):
            student = User.objects.create_user(
                username=f"mf_page_{index}", email=f"mf_page_{index}@example.com",
                password=PASSWORD, role=Role.STUDENT,
            )
            self.class_a.students.add(student)

        response = self.client.get(
            LIST_URL, {"classroom": str(self.class_a.pk), "page_size": "10"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("classroom=", response.context["querystring"])
        body = response.content.decode("utf-8")
        self.assertIn(f"?classroom={self.class_a.pk}&amp;page_size=10&amp;page=2", body)

    def test_classroom_options_grouped_by_type(self):
        """下拉按「行政班」「学科班」两个 optgroup 分组，选项带人数，行政班整体排在学科班之前。"""
        response = self.client.get(LIST_URL, {"classroom": str(self.class_a.pk)})
        options = response.context["classroom_options"]
        admin_names = [i["name"] for i in options if i["class_type"] == ClassType.ADMIN]
        subject_names = [i["name"] for i in options if i["class_type"] == ClassType.SUBJECT]
        # 同一类内部按 name 排序：两条行政班只差数字，任何排序规则下顺序都确定
        self.assertEqual(admin_names, ["计科 1 班", "计科 2 班"])
        # 中文名的库内排序依数据库 collation 而异，这里只验集合（顺序由 order_by 保证）
        self.assertEqual(
            sorted(subject_names), sorted(["数据结构 1 班", "数据结构 2 班", "操作系统 1 班"])
        )
        # 排序口径：行政班整体在学科班之前
        self.assertTrue(options[0]["class_type"] == ClassType.ADMIN)
        self.assertTrue(options[-1]["class_type"] == ClassType.SUBJECT)

        admin_options = [i for i in options if i["name"] == "计科 1 班"]
        self.assertEqual(admin_options[0]["student_count"], 2)
        self.assertEqual(
            [i for i in options if i["name"] == "计科 2 班"][0]["student_count"], 1
        )

        body = response.content.decode("utf-8")
        self.assertIn('<optgroup label="行政班">', body)
        self.assertIn('<optgroup label="学科班">', body)
        self.assertIn("计科 1 班（2 人）", body)
        self.assertIn("计科 2 班（1 人）", body)
        self.assertIn(f'value="{self.class_a.pk}"', body)

    def test_classroom_options_show_subject_for_subject_classroom(self):
        """学科班选项带学科名（`名称 · 学科（N 人）`）；选中的那个 option 是 selected。"""
        response = self.client.get(LIST_URL, {"classroom": str(self.subject_ds.pk)})
        body = response.content.decode("utf-8")
        self.assertIn("数据结构 1 班 · 数据结构（1 人）", body)
        self.assertIn("操作系统 1 班 · 操作系统（1 人）", body)
        # 行政班不拼学科
        self.assertIn("计科 1 班（2 人）", body)
        self.assertNotIn("计科 1 班 · ", body)

        target = f'value="{self.subject_ds.pk}"'
        start = body.index(target)
        option_html = body[start:body.index("</option>", start)]
        self.assertIn("selected", option_html)

    def test_subject_classroom_option_omits_empty_subject(self):
        """学科班 subject 为空时，文案里不出现孤立的「· 」。"""
        self.subject_ds.subject = ""
        self.subject_ds.save(update_fields=["subject"])
        body = self.client.get(LIST_URL).content.decode("utf-8")
        self.assertIn("数据结构 1 班（1 人）", body)
        self.assertNotIn("数据结构 1 班 · （", body)

    def test_empty_subject_group_not_rendered(self):
        """所有学科班归档后，页面不渲染空的「学科班」optgroup，但行政班分组仍在。"""
        Classroom.objects.filter(class_type=ClassType.SUBJECT).update(is_active=False)

        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn('<optgroup label="行政班">', body)
        self.assertNotIn('<optgroup label="学科班">', body)
        self.assertFalse(response.context["has_subject_classrooms"])
        self.assertTrue(response.context["has_admin_classrooms"])

    def test_archived_classroom_not_in_options(self):
        """归档（is_active=False）的班级（两类都一样）不出现在下拉里。"""
        self.class_b.is_active = False
        self.class_b.save(update_fields=["is_active"])
        self.subject_os.is_active = False
        self.subject_os.save(update_fields=["is_active"])
        response = self.client.get(LIST_URL)
        options = response.context["classroom_options"]
        names = [item["name"] for item in options]
        self.assertNotIn("计科 2 班", names)
        self.assertNotIn("操作系统 1 班", names)
        self.assertIn("计科 1 班", names)


# ----------------------------------------------------------------------
# 3c：学科筛选（教师看任教学科，学生看所在学科班的学科）
# ----------------------------------------------------------------------
class SubjectFilterTest(ManageUserFilterBaseTest):
    """``?subject=`` 必须同时覆盖教师与**学生**两条路径。

    学生的 ``User.subject`` 恒为空，真实学科来自 ``classrooms`` 里学科班的
    ``Classroom.subject``。旧实现只 match ``User.subject``，导致：
      * 选学科筛不出任何学生；
      * 学科 + 班级组合时学科条件先清空学生，交集恒为 0。
    """

    def test_filter_subject_matches_student_in_subject_classroom(self):
        """学生甲在「操作系统 1 班」→ 按学科「操作系统」能筛到。"""
        self.assertFalse(self.student_a.subject)
        response = self.client.get(LIST_URL, {"subject": "操作系统"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mf_student_a")
        self.assertEqual(response.context["total_count"], 1)
        # 学生乙只在「数据结构」学科班，不该被「操作系统」带出
        self.assertNotContains(response, "mf_student_b")

    def test_filter_subject_excludes_student_only_in_admin_classroom(self):
        """学生丙只在行政班（没有任何学科班）→ 按任意学科都筛不到。"""
        response = self.client.get(LIST_URL, {"subject": "操作系统"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "mf_student_free")
        # 反证：不加学科条件时他是在列表里的
        self.assertContains(self.client.get(LIST_URL), "mf_student_free")

    def test_filter_subject_still_matches_teacher(self):
        """教师路径不被破坏：``User.subject`` 直接命中。"""
        response = self.client.get(LIST_URL, {"subject": "数据结构"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mf_teacher")
        # 未填任教学科的教师不会因为「空 == 空」被误带出
        self.assertNotContains(response, "mf_teacher_bare")

    def test_filter_subject_combined_with_subject_classroom(self):
        """学科 + 该学科班组合：仍能命中（用户报的第二个症状，锁死）。"""
        response = self.client.get(
            LIST_URL, {"subject": "操作系统", "classroom": str(self.subject_os.pk)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mf_student_a")
        self.assertEqual(response.context["total_count"], 1)

    def test_filter_subject_combined_with_unrelated_classroom_is_empty(self):
        """学科「操作系统」+ 另一个班（计科 2 班，只有学生丙）→ 交集为 0，而非 500。"""
        response = self.client.get(
            LIST_URL, {"subject": "操作系统", "classroom": str(self.class_b.pk)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 0)
        self.assertContains(response, "没有符合条件的用户")
        self.assertNotContains(response, "mf_student_a")

    def test_filter_subject_ignores_surrounding_spaces(self):
        """``subject= 操作系统 `` 与 ``subject=操作系统`` 等价（入库前 strip）。"""
        bare = self.client.get(LIST_URL, {"subject": "操作系统"})
        padded = self.client.get(LIST_URL, {"subject": " 操作系统 "})
        self.assertEqual(padded.status_code, 200)
        self.assertEqual(bare.context["total_count"], padded.context["total_count"])
        self.assertEqual(padded.context["total_count"], 1)
        self.assertContains(padded, "mf_student_a")


# ----------------------------------------------------------------------
# 查询数回归（防 N+1）
# ----------------------------------------------------------------------
class ManageUserListQueryCountTest(ManageUserFilterBaseTest):
    """每页 20 条时，学科 / 行政班人数计算不得引入 N+1。"""

    def _measure(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(LIST_URL, {"page_size": "20"})
        self.assertEqual(response.status_code, 200)
        # 当前页确实有学生，确保「学科聚合」这条查询每次都会被执行（两次测量口径一致）
        self.assertTrue(
            any(user.role == Role.STUDENT for user in response.context["page_obj"].object_list)
        )
        return len(captured)

    def test_query_count_is_constant(self):
        baseline = self._measure()

        extra_class = Classroom.objects.create(
            name="压测行政班", class_type=ClassType.ADMIN,
        )
        extra_subject = Classroom.objects.create(
            name="压测学科班", subject="计算机网络", class_type=ClassType.SUBJECT,
        )
        students = User.objects.bulk_create([
            User(username=f"mf_perf_{index}", email=f"mf_perf_{index}@example.com",
                 role=Role.STUDENT, real_name=f"压测学生{index}", subject="")
            for index in range(100)
        ])
        # MySQL 上 bulk_create 不回填主键，重新查一次再建 M2M 关系
        perf_users = list(User.objects.filter(username__startswith="mf_perf_"))
        self.assertEqual(len(perf_users), 100)
        extra_class.students.add(*perf_users)
        extra_subject.students.add(*perf_users)

        self.assertEqual(self._measure(), baseline, "列表页查询条数随数据量增长，存在 N+1")
        self.assertLess(baseline, 40, f"首屏查询过多：{baseline} 条")
