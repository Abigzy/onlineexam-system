"""知识点掌握度测试。

重点验证三件事：
  * **向上归并** —— 子知识点的答题要汇总到祖先，否则顶层知识点永远是空的；
  * **不重复计数** —— 一道题同时挂了父与子知识点时只能算一次；
  * **「薄弱」有统计门槛** —— 只答过 1 题且答错，结论是「数据不足」而不是「薄弱」。
"""

from decimal import Decimal

from apps.analytics.constants import MasteryLevel
from apps.analytics.services import KnowledgeMasteryService, _mastery_level
from apps.common.constants import AttemptStatus, QuestionType
from apps.papers.services import PaperService

from .base import CHILD_KP_NAME, PARENT_KP_NAME, AnalyticsBaseTest


class MasteryLevelBandTest(AnalyticsBaseTest):
    """分档函数是纯函数，直接测边界。"""

    def test_bands(self):
        self.assertEqual(_mastery_level(1.0, 5), MasteryLevel.MASTERED)
        self.assertEqual(_mastery_level(0.85, 5), MasteryLevel.MASTERED)
        self.assertEqual(_mastery_level(0.84, 5), MasteryLevel.PROFICIENT)
        self.assertEqual(_mastery_level(0.70, 5), MasteryLevel.PROFICIENT)
        self.assertEqual(_mastery_level(0.60, 5), MasteryLevel.BASIC)
        self.assertEqual(_mastery_level(0.59, 5), MasteryLevel.WEAK)

    def test_insufficient_samples_is_unknown_not_weak(self):
        """样本不足时给 UNKNOWN —— 0% 正确率也不该被判薄弱。"""
        self.assertEqual(_mastery_level(0.0, 1), MasteryLevel.UNKNOWN)
        self.assertEqual(_mastery_level(None, 5), MasteryLevel.UNKNOWN)


class MasteryAggregationTest(AnalyticsBaseTest):

    def test_child_answer_rolls_up_to_parent(self):
        """答「线性表」的题，「数据结构」也应统计到同样多。"""
        attempt = self.judged_attempt(self.exam)
        data = KnowledgeMasteryService.student_mastery(self.student)

        rows = {row["name"]: row for row in data["items"]}
        self.assertIn(CHILD_KP_NAME, rows)
        self.assertIn(PARENT_KP_NAME, rows)

        child, parent = rows[CHILD_KP_NAME], rows[PARENT_KP_NAME]
        self.assertEqual(child["answered"], 6)
        self.assertEqual(parent["answered"], 6)
        self.assertEqual(child["full_path"], f"{PARENT_KP_NAME} / {CHILD_KP_NAME}")
        self.assertEqual(parent["full_path"], PARENT_KP_NAME)
        # 归并来的只计一次，直接挂载数只属于子知识点
        self.assertEqual(child["direct_answered"], 6)
        self.assertEqual(parent["direct_answered"], 0)
        self.assertEqual(parent["accuracy"], child["accuracy"])
        self.assertEqual(attempt.status, AttemptStatus.GRADED)

    def test_same_question_on_parent_and_child_counts_once(self):
        """一道题同时挂父子知识点时，父知识点只能算一次。

        ``self.questions`` 是类级共享的（``setUpTestData`` 只跑一次），
        改完必须还原，否则会污染同类的其他用例。
        """
        original = self.questions[QuestionType.JUDGE]
        try:
            extra = self.add_question_with_knowledge_points(QuestionType.JUDGE, [])
            extra.knowledge_points.add(self.parent_kp, self.child_kp)

            self.judged_attempt(self.exam)
            data = KnowledgeMasteryService.student_mastery(self.student)
            rows = {row["name"]: row for row in data["items"]}

            # 6 道原题挂在子知识点上 → 父知识点应得到 6 条，而不是 7 条
            self.assertEqual(rows[PARENT_KP_NAME]["answered"], 6)
            self.assertEqual(rows[CHILD_KP_NAME]["answered"], 6)
            self.assertEqual(rows[PARENT_KP_NAME]["direct_answered"], 1)
        finally:
            self.questions[QuestionType.JUDGE] = original

    def test_wrong_answers_mark_weak(self):
        """只答对单选题 → 正确率 1/6，判为薄弱。"""
        self.judged_attempt(self.exam, correct_types={QuestionType.SINGLE})
        data = KnowledgeMasteryService.student_mastery(self.student)
        rows = {row["name"]: row for row in data["items"]}

        child = rows[CHILD_KP_NAME]
        self.assertEqual(child["correct"], 1)
        self.assertEqual(child["answered"], 6)
        self.assertEqual(child["mastery_level"], MasteryLevel.WEAK)
        self.assertTrue(child["is_weak"])

        weak_names = [row["name"] for row in data["weak_points"]]
        self.assertIn(CHILD_KP_NAME, weak_names)
        self.assertIn(PARENT_KP_NAME, weak_names)
        self.assertEqual(data["summary"]["weak_count"], len(weak_names))

    def test_all_correct_is_mastered(self):
        self.judged_attempt(self.exam)
        data = KnowledgeMasteryService.student_mastery(self.student)
        rows = {row["name"]: row for row in data["items"]}

        self.assertEqual(rows[CHILD_KP_NAME]["mastery_level"], MasteryLevel.MASTERED)
        self.assertEqual(rows[CHILD_KP_NAME]["accuracy"], 1.0)
        self.assertNotIn(CHILD_KP_NAME, [row["name"] for row in data["weak_points"]])

    def test_subjective_uses_score_ratio(self):
        """主观题按得分率判定达标：简答 3/5 = 60% 达标，论述 2/10 = 20% 不达标。"""
        self.judged_attempt(
            self.exam,
            correct_types={QuestionType.SINGLE, QuestionType.MULTIPLE,
                           QuestionType.JUDGE, QuestionType.FILL},
            partial={QuestionType.SHORT: "3", QuestionType.ESSAY: "2"},
        )
        records = KnowledgeMasteryService.collect_records(student=self.student)
        by_type = {row["question_type"]: row for row in records}

        self.assertTrue(by_type[QuestionType.SHORT]["passed"])
        self.assertFalse(by_type[QuestionType.ESSAY]["passed"])

    def test_unjudged_objective_excluded(self):
        """还没判分的客观题不该参与统计（is_correct 为 null）。"""
        attempt = self.judged_attempt(self.exam)
        answer = self.answer_of(attempt, QuestionType.SINGLE)
        answer.is_correct = None
        answer.score = Decimal("0")
        answer.save(update_fields=["is_correct", "score", "updated_at"])

        records = KnowledgeMasteryService.collect_records(student=self.student)
        self.assertEqual(len(records), 5)
        self.assertNotIn(QuestionType.SINGLE, {row["question_type"] for row in records})

    def test_only_analyzable_statuses_counted(self):
        """刚交卷还没判分的作答不参与学情分析。"""
        attempt = self.judged_attempt(self.exam)
        attempt.status = AttemptStatus.SUBMITTED
        attempt.save(update_fields=["status", "updated_at"])

        data = KnowledgeMasteryService.student_mastery(self.student)
        self.assertEqual(data["summary"]["answered"], 0)
        self.assertEqual(data["items"], [])

    def test_subject_filter(self):
        """按学科过滤时只能看到该学科的作答。"""
        other_subject = "操作系统"
        other_paper = self.make_paper(subject=other_subject)
        other_exam = self.make_exam(paper=other_paper)
        self.judged_attempt(other_exam, student=self.student_b)

        only_other = KnowledgeMasteryService.student_mastery(self.student_b,
                                                             subject=other_subject)
        self.assertEqual(only_other["summary"]["answered"], 6)

        none_of_this = KnowledgeMasteryService.student_mastery(self.student_b,
                                                                subject=self.child_kp.subject)
        self.assertEqual(none_of_this["summary"]["answered"], 0)

    def test_small_sample_goes_to_insufficient_not_weak(self):
        """只有 1 道题的作答：结论是「数据不足」，不进薄弱列表。"""
        paper = PaperService.create(
            data={"title": "单题卷", "subject": self.child_kp.subject,
                  "duration_minutes": 10, "pass_score": Decimal("1")},
            user=self.teacher,
        )
        PaperService.add_questions(
            paper=paper, questions=[self.questions[QuestionType.SINGLE]], strict=False,
        )
        PaperService.change_status(paper, "published")
        exam = self.make_exam(paper=paper)
        self.judged_attempt(exam, correct_types=set())

        data = KnowledgeMasteryService.student_mastery(self.student)
        rows = {row["name"]: row for row in data["items"]}
        child = rows[CHILD_KP_NAME]

        self.assertEqual(child["answered"], 1)
        self.assertEqual(child["accuracy"], 0.0)
        self.assertEqual(child["mastery_level"], MasteryLevel.UNKNOWN)
        self.assertTrue(child["is_insufficient"])
        self.assertEqual(data["weak_points"], [])
        self.assertEqual(data["summary"]["insufficient_count"], 2)
        self.assertEqual(data["summary"]["weak_count"], 0)

    def test_radar_shape(self):
        """雷达图数据必须能直接喂 ECharts：indicator 与 values 等长。"""
        self.judged_attempt(self.exam)
        radar = KnowledgeMasteryService.student_mastery(self.student)["radar"]

        self.assertTrue(radar["indicator"])
        self.assertEqual(len(radar["indicator"]), len(radar["values"]))
        for item in radar["indicator"]:
            self.assertEqual(item["max"], 100)
        for value in radar["values"]:
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 100)

    def test_summary_numbers(self):
        self.judged_attempt(self.exam, correct_types={QuestionType.SINGLE})
        summary = KnowledgeMasteryService.student_mastery(self.student)["summary"]

        self.assertEqual(summary["answered"], 6)
        self.assertEqual(summary["correct"], 1)
        self.assertAlmostEqual(summary["accuracy"], 1 / 6, places=4)
        self.assertEqual(summary["full_score"], 23.0)
        self.assertEqual(summary["score"], 2.0)


__all__ = ["MasteryAggregationTest", "MasteryLevelBandTest"]
