"""班级 / 考试维度分析测试。

覆盖分数分布、参与度、逐题得分率与**区分度**。区分度需要至少 4 份作答
（前 27% 与后 27% 各取一组），所以这里单独造一个 4 人班级。
"""

from django.contrib.auth import get_user_model

from apps.analytics.services import ClassAnalysisService
from apps.common.constants import QuestionType
from apps.exam.models import ExamAttempt

from .base import AnalyticsBaseTest

PASSWORD = "Exam@123456"
FULL = {QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE,
        QuestionType.FILL, QuestionType.SHORT, QuestionType.ESSAY}
MID = {QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE}


class ClassAnalysisTest(AnalyticsBaseTest):
    """造一个 4 人班级：满分 / 中等 / 低分 / 零分，让分布与区分度都有差异。"""

    def setUp(self):
        super().setUp()
        # 不直接赋值 self.exam —— 它是基类的只读缓存属性（首次访问才造卷，
        # 同一次用例内复用）。下面统一用 self.exam 即可。
        user_model = get_user_model()
        self.extra = [
            user_model.objects.create_user(
                username=f"an_extra_{index}", email=f"an_extra_{index}@example.com",
                password=PASSWORD, role="student", real_name=f"附加考生{index}",
                student_no=f"A202600{index}",
            )
            for index in range(2)
        ]
        self.students = [self.student, self.student_b, *self.extra]
        # open_to_all 收紧后，教师创建的「面向全体」考试只对本班学生开放；
        # 把动态造的附加学生也纳入基类班，否则开考会被 403 拒绝。
        self.classroom.students.add(*self.extra)
        for student, plan in zip(self.students, (FULL, MID, {QuestionType.SINGLE}, set())):
            self.judged_attempt(self.exam, student=student, correct_types=plan)

    # ------------------------------------------------------------------
    def test_distribution_buckets_and_stats(self):
        rows = ClassAnalysisService._attempt_rows(self.exam)
        data = ClassAnalysisService.distribution(rows, self.exam)

        buckets = {item["label"]: item["count"] for item in data["buckets"]}
        self.assertEqual(buckets["90-100"], 1)     # 满分那位
        self.assertEqual(buckets["0-59"], 3)
        self.assertEqual(sum(buckets.values()), 4)

        stats = data["stats"]
        self.assertEqual(stats["graded_count"], 4)
        self.assertEqual(stats["max_score"], 23.0)
        self.assertEqual(stats["min_score"], 0.0)
        self.assertEqual(stats["median_score"], 4.0)      # (6 + 2) / 2
        self.assertEqual(stats["pass_count"], 1)          # 及格分 14
        self.assertEqual(stats["pass_rate"], 25.0)
        self.assertEqual(stats["excellent_count"], 1)

    def test_normal_curve_parameters(self):
        rows = ClassAnalysisService._attempt_rows(self.exam)
        normal = ClassAnalysisService.distribution(rows, self.exam)["normal"]

        self.assertIsNotNone(normal["mu"])
        self.assertGreater(normal["sigma"], 0)
        self.assertEqual(normal["total"], 23.0)

    def test_participation(self):
        rows = ClassAnalysisService._attempt_rows(self.exam)
        part = ClassAnalysisService.participation(self.exam, rows)

        self.assertEqual(part["applicants"], 4)
        self.assertEqual(part["started"], 4)
        self.assertEqual(part["submitted"], 4)
        self.assertEqual(part["graded"], 4)
        self.assertEqual(part["pending_grading"], 0)
        self.assertEqual(part["not_started"], 0)
        self.assertEqual(part["submit_rate"], 100.0)

    def test_behavior_counters(self):
        """切屏 / 粘贴计数要**从作答行派生**出「可疑」，不是在 rows 上手工造字段。

        这里真的写库再重取，才能覆盖 ``_attempt_rows`` 里
        ``suspicious = switch_count > 0 or paste_count > 0`` 这条派生逻辑。
        """
        first, second = self.exam.attempts.order_by("id")[:2]
        ExamAttempt.objects.filter(pk=first.pk).update(switch_count=3)
        ExamAttempt.objects.filter(pk=second.pk).update(paste_count=2)

        data = ClassAnalysisService.behavior(ClassAnalysisService._attempt_rows(self.exam))

        self.assertEqual(data["switch_total"], 3)
        self.assertEqual(data["paste_total"], 2)
        self.assertEqual(data["suspicious_count"], 2)   # 两人各命中一条，第三人清白

    # ------------------------------------------------------------------
    def test_question_score_rate_and_discrimination(self):
        data = ClassAnalysisService.question_analysis(self.exam)
        items = {row["question_type"]: row for row in data["items"]}

        self.assertEqual(data["summary"]["question_count"], 6)

        # 单选：满分 / 中等 / 低分三人都答对，只有零分那位答错 → 3/4
        single = items[QuestionType.SINGLE]
        self.assertEqual(single["answered_count"], 4)
        self.assertEqual(single["correct_count"], 3)
        self.assertEqual(single["accuracy"], 0.75)
        self.assertEqual(single["score_rate"], 0.75)

        # 区分度：前 27%（满分那位）与后 27%（零分那位）的得分率差 → 1 - 0
        self.assertEqual(single["discrimination"], 1.0)
        for row in data["items"]:
            self.assertIsNotNone(row["discrimination"])

    def test_difficulty_label_from_real_score_rate(self):
        self.assertEqual(ClassAnalysisService._difficulty_label(0.9), "偏易")
        self.assertEqual(ClassAnalysisService._difficulty_label(0.7), "适中")
        self.assertEqual(ClassAnalysisService._difficulty_label(0.5), "偏难")
        self.assertEqual(ClassAnalysisService._difficulty_label(0.2), "过难")
        self.assertEqual(ClassAnalysisService._difficulty_label(None), "数据不足")

    def test_weak_questions_listed(self):
        data = ClassAnalysisService.question_analysis(self.exam)
        # 单选 0.75 不弱；其余五题 ≤ 0.5 都 < 0.6 → 进薄弱列表
        self.assertEqual(data["summary"]["weak_count"], 5)
        for row in data["weak_items"]:
            self.assertLess(row["score_rate"], 0.6)
        # 列表按得分率升序，最弱的排最前
        self.assertEqual(data["weak_items"][0]["score_rate"], 0.25)

    def test_discrimination_none_when_too_few_attempts(self):
        """少于 4 份作答时不分组，区分度给 None 而不是编一个数出来。"""
        exam = self.make_exam()
        self.judged_attempt(exam, student=self.student)
        self.judged_attempt(exam, student=self.student_b)

        data = ClassAnalysisService.question_analysis(exam)
        for row in data["items"]:
            self.assertIsNone(row["discrimination"])

    # ------------------------------------------------------------------
    def test_weak_knowledge_points_class_level(self):
        data = ClassAnalysisService.weak_knowledge_points(self.exam)

        self.assertEqual(data["summary"]["answered"], 6 * 4)
        names = [row["name"] for row in data["items"]]
        # 4 人里只有 1 人全对 → 知识点正确率 25% < 60% → 薄弱
        self.assertIn("线性表", names)
        self.assertIn("数据结构", names)

    def test_exam_report_shape(self):
        data = ClassAnalysisService.exam_report(self.exam)
        for key in ("exam", "participation", "distribution", "behavior",
                    "weak_knowledge_points", "ranking", "rows"):
            self.assertIn(key, data)
        self.assertEqual(len(data["rows"]), 4)
        self.assertEqual(data["ranking"]["top"][0]["score"], 23.0)
        self.assertEqual(data["exam"]["question_count"], 6)


__all__ = ["ClassAnalysisTest"]
