"""R1 测试轮次发现的缺陷回归（BUG-03）。

缺陷来源：``docs/OnlineExam-功能测试用例.xlsx`` 的「8-缺陷与观察项」工作表。
"""

from apps.analytics.scoping import visible_student_ids
from apps.analytics.services import ClassAnalysisService
from apps.classroom.constants import ClassType
from apps.classroom.models import Classroom

from .base import AnalyticsBaseTest


class CrossClassroomApplicantCountTest(AnalyticsBaseTest):
    """BUG-03：跨班学生在「应考人数」里被重复计数（17 vs 16）。

    回归前：``visible_student_ids`` 按 ``classrooms__students`` 展开却没 distinct，
    同一个学生挂在两个班里就会产生两条记录；``participation`` 又直接 ``len()``
    当分母，于是应考人数虚高，监控页与学情页对不上。
    """

    def _put_student_in_second_classroom(self):
        """再造一个同属该教师的班，把同一名学生加进去。"""
        return Classroom.objects.create(
            name="重叠班", class_type=ClassType.ADMIN,
            created_by=self.teacher, head_teacher=self.teacher,
        )

    def test_visible_ids_deduplicated(self):
        other = self._put_student_in_second_classroom()
        other.students.add(self.student)

        ids = list(visible_student_ids(self.teacher))
        self.assertEqual(len(ids), len(set(ids)), "跨班学生不应在可见 id 里出现两次")
        self.assertIn(self.student.id, ids)

    def test_same_student_in_two_classrooms_counts_once(self):
        """直接对比修复前后：可见人数应等于去重后的人数。"""
        other = self._put_student_in_second_classroom()
        other.students.add(self.student)

        ids = list(visible_student_ids(self.teacher))
        distinct = set(ids)
        report = ClassAnalysisService.participation(self.exam, [], student_ids=ids)
        self.assertEqual(report["applicants"], len(distinct))

    def test_participation_dedups_duplicate_input(self):
        """兜底：即使入参自带重复 id，也不该把人数算多。"""
        ids = [self.student.id, self.student.id, self.student_b.id]
        report = ClassAnalysisService.participation(self.exam, [], student_ids=ids)
        self.assertEqual(report["applicants"], 2)

    def test_admin_scope_unaffected(self):
        """管理员本来就是全校学生、无重复，别被去重逻辑改坏。"""
        ids = list(visible_student_ids(self.admin))
        self.assertEqual(len(ids), len(set(ids)))
