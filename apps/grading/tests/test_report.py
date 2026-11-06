"""成绩单 / 班级成绩单 / 错题本测试。

读模型最容易出的两类问题，这个文件就是盯着它们写的：

1. **泄漏** —— 成绩还没公开，学生的成绩单里就带出了答案与逐题对错；
2. **算错** —— 排名、及格率、分数分布这些「汇总的汇总」，一个口径不一致就全错。
"""

from decimal import Decimal

from apps.common.constants import AttemptStatus, QuestionType
from apps.exam.services import ExamAttemptService
from apps.grading.services import GradingService, ScoreReportService
from apps.grading.tests.base import FILL_ANSWER, GradingBaseTest


class ReportSetupMixin:
    def setUp(self):
        self.attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(self.attempt, with_ai=True)
        self.attempt.refresh_from_db()


class AttemptReportTest(ReportSetupMixin, GradingBaseTest):
    def test_teacher_sees_everything(self):
        report = ScoreReportService.attempt_report(self.attempt, viewer=self.teacher)

        self.assertTrue(report["visible"])
        self.assertEqual(report["items"][0]["reference_answer"] != "", True)
        self.assertEqual(report["items"][0]["analysis"], "（测试解析：这里应当只对教师可见）")
        self.assertIsNotNone(report["grading"])          # 教师额外拿到判分进度
        self.assertEqual(report["student"]["id"], self.student.id)
        self.assertEqual(len(report["items"]), 6)

    def test_student_before_publish_is_masked(self):
        """未公开时：能看到自己答了什么，**看不到**对不对、更看不到答案。"""
        report = ScoreReportService.attempt_report(self.attempt, viewer=self.student)

        self.assertFalse(report["visible"])
        self.assertIsNone(report["student"])             # 学生视角不需要「学生是谁」
        self.assertIsNone(report["grading"])
        for item in report["items"]:
            self.assertIsNone(item["is_correct"])
            self.assertIsNone(item["score"])
            self.assertEqual(item["reference_answer"], "")
            self.assertEqual(item["analysis"], "")
            self.assertTrue(item["stem"])                # 题干与自己的作答仍然保留

    def test_student_after_publish_sees_scores(self):
        GradingService.finalize_attempt(self.attempt)
        report = ScoreReportService.attempt_report(self.attempt, viewer=self.student)

        self.assertTrue(report["visible"])
        self.assertEqual(report["score"], 23.0)
        self.assertEqual(report["objective_score"], 8.0)
        self.assertEqual(report["subjective_score"], 15.0)
        self.assertTrue(report["passed"])
        self.assertEqual(report["counts"]["correct"], 6)
        self.assertEqual(report["counts"]["wrong"], 0)
        self.assertEqual(report["counts"]["accuracy"], 1.0)
        for item in report["items"]:
            self.assertNotEqual(item["reference_answer"], "")

    def test_report_lists_unanswered_questions(self):
        """整卷列全：一个字没写的题也要出现在成绩单里（否则学生不知道自己漏答）。"""
        exam = self.make_objective_exam()
        attempt = self.start(exam)
        mapping = self.paper_map(attempt)
        self.save_answers(attempt, [{
            "paper_question_id": mapping[QuestionType.SINGLE]["paper_question_id"],
            "selected_labels": "A",
        }])
        ExamAttemptService.submit(attempt)
        GradingService.grade_attempt(attempt)

        report = ScoreReportService.attempt_report(attempt, viewer=self.teacher)
        self.assertEqual(len(report["items"]), 4)
        unanswered = [item for item in report["items"] if not item["is_answered"]]
        self.assertEqual(len(unanswered), 3)
        self.assertEqual(report["counts"]["unanswered"], 3)

    def test_score_ratio_and_pass_judgement(self):
        GradingService.finalize_attempt(self.attempt)
        report = ScoreReportService.attempt_report(self.attempt, viewer=self.teacher)
        self.assertEqual(report["total_score"], 23.0)
        self.assertEqual(report["pass_score"], 14.0)     # 装配里试卷及格分是 14
        self.assertEqual(report["score_ratio"], 1.0)


class ScoreboardTest(GradingBaseTest):
    def setUp(self):
        # 甲乙两人各考一次：甲满分，乙主观题被判低分
        # 注意：不能在测试里给 ``self.exam`` 赋值——它是基类上的只读 property，
        # 这里用 ``score_exam`` 单独存，避免踩「property 不能赋值」的坑。
        self.score_exam = self.make_exam(show_score_immediately=True)
        self.attempt_a = self.submit_with_correct_answers(exam=self.score_exam,
                                                         student=self.student)
        GradingService.grade_attempt(self.attempt_a, with_ai=True)

        self.attempt_b = self.submit_with_correct_answers(exam=self.score_exam,
                                                         student=self.student_b)
        GradingService.grade_attempt(self.attempt_b, with_ai=True)
        for question_type in (QuestionType.SHORT, QuestionType.ESSAY):
            answer = self.answer_of(self.attempt_b, question_type)
            GradingService.review_answer(answer, reviewer=self.teacher, score=Decimal("0"))

        GradingService.finalize_attempt(self.attempt_a)
        GradingService.finalize_attempt(self.attempt_b)

    def test_ranking_and_summary(self):
        data = ScoreReportService.scoreboard(self.score_exam)

        self.assertEqual(data["summary"]["submitted"], 2)
        self.assertEqual(data["summary"]["graded"], 2)
        self.assertEqual(data["summary"]["pending_grading"], 0)
        self.assertEqual(data["summary"]["max_score"], 23.0)
        self.assertEqual(data["summary"]["min_score"], 8.0)
        self.assertEqual(data["summary"]["avg_score"], 15.5)
        self.assertEqual(data["summary"]["pass_count"], 1)      # 及格线 14：23 及格、8 不及格
        self.assertEqual(data["summary"]["pass_rate"], 50.0)

        rows = data["results"]
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[0]["student_id"], self.student.id)
        self.assertEqual(rows[1]["rank"], 2)
        self.assertEqual(rows[1]["student_id"], self.student_b.id)
        self.assertTrue(rows[0]["passed"])
        self.assertFalse(rows[1]["passed"])

    def test_distribution_uses_percentage_bands(self):
        data = ScoreReportService.scoreboard(self.score_exam)
        distribution = data["summary"]["distribution"]
        self.assertEqual(distribution["90-100"], 1)             # 23/23
        self.assertEqual(distribution["0-59"], 1)               # 8/23 ≈ 35%
        self.assertEqual(distribution["60-69"], 0)

    def test_scoreboard_excludes_in_progress(self):
        self.start(self.make_exam())
        data = ScoreReportService.scoreboard(self.score_exam)
        self.assertEqual(data["summary"]["submitted"], 2)

    def test_keyword_filter(self):
        data = ScoreReportService.scoreboard(self.score_exam, keyword=self.student_b.student_no)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["student_id"], self.student_b.id)

    def test_pending_grading_is_counted(self):
        exam = self.make_exam()
        self.submit_with_correct_answers(exam=exam)
        data = ScoreReportService.scoreboard(exam)
        self.assertEqual(data["summary"]["submitted"], 1)
        self.assertEqual(data["summary"]["graded"], 0)
        self.assertEqual(data["summary"]["pending_grading"], 1)
        self.assertIsNone(data["summary"]["avg_score"])         # 未判分不能拿 0 分冒充
        self.assertFalse(data["results"][0]["passed"])
        self.assertEqual(data["exam"]["id"], exam.id)
        self.assertIn("全对才得分", data["exam"]["multiple_choice_policy_display"])


class WrongBookTest(GradingBaseTest):
    """错题本：只收「已公开成绩」的作答里的错题。"""

    def _make_attempt_with_wrong(self, *, show_score_immediately, student=None):
        exam = self.make_objective_exam(show_score_immediately=show_score_immediately)
        attempt = self.start(exam, student=student or self.student)
        mapping = self.paper_map(attempt)
        self.save_answers(attempt, [
            {"paper_question_id": mapping[QuestionType.SINGLE]["paper_question_id"],
             "selected_labels": "B"},                                   # 错
            {"paper_question_id": mapping[QuestionType.MULTIPLE]["paper_question_id"],
             "selected_labels": "AB"},                                  # 对
            {"paper_question_id": mapping[QuestionType.JUDGE]["paper_question_id"],
             "selected_labels": "B"},                                   # 错
            {"paper_question_id": mapping[QuestionType.FILL]["paper_question_id"],
             "answer_text": FILL_ANSWER},                               # 对
        ])
        ExamAttemptService.submit(attempt)
        GradingService.grade_attempt(attempt)
        attempt.refresh_from_db()
        return exam, attempt

    def test_collects_only_wrong_questions(self):
        _, attempt = self._make_attempt_with_wrong(show_score_immediately=True)
        self.assertEqual(attempt.status, AttemptStatus.AUTO_GRADED)

        data = ScoreReportService.wrong_book(self.student)
        self.assertEqual(data["count"], 2)
        types = {row["question_type"] for row in data["results"]}
        self.assertEqual(types, {QuestionType.SINGLE, QuestionType.JUDGE})
        for row in data["results"]:
            self.assertTrue(row["reference_answer"])          # 错题本要给答案与解析
            self.assertTrue(row["analysis"])
            self.assertEqual(row["wrong_count"], 1)
            self.assertEqual(row["last_score"], 0.0)

    def test_dedupes_and_counts_repeats(self):
        self._make_attempt_with_wrong(show_score_immediately=True)
        self._make_attempt_with_wrong(show_score_immediately=True)      # 再考一次，还错

        data = ScoreReportService.wrong_book(self.student)
        self.assertEqual(data["count"], 2)                              # 题目去重
        self.assertTrue(all(row["wrong_count"] == 2 for row in data["results"]))

    def test_excludes_attempts_whose_score_is_not_public(self):
        self._make_attempt_with_wrong(show_score_immediately=False)
        self.assertEqual(ScoreReportService.wrong_book(self.student)["count"], 0)

    def test_excludes_unfinished_attempts(self):
        """答题中的作答还没判分，绝不能出现在错题本里。"""
        exam = self.make_objective_exam(show_score_immediately=True)
        attempt = self.start(exam)
        mapping = self.paper_map(attempt)
        self.save_answers(attempt, [{
            "paper_question_id": mapping[QuestionType.SINGLE]["paper_question_id"],
            "selected_labels": "B",
        }])
        self.assertEqual(ScoreReportService.wrong_book(self.student)["count"], 0)

    def test_only_own_wrong_questions(self):
        self._make_attempt_with_wrong(show_score_immediately=True, student=self.student_b)
        self.assertEqual(ScoreReportService.wrong_book(self.student)["count"], 0)
        self.assertEqual(ScoreReportService.wrong_book(self.student_b)["count"], 2)

    def test_subject_filter(self):
        self._make_attempt_with_wrong(show_score_immediately=True)
        self.assertEqual(ScoreReportService.wrong_book(self.student, subject="不存在的学科")["count"], 0)
        self.assertEqual(ScoreReportService.wrong_book(self.student, subject="数据结构")["count"], 2)

    def test_wrong_book_includes_knowledge_points(self):
        self._make_attempt_with_wrong(show_score_immediately=True)
        row = ScoreReportService.wrong_book(self.student)["results"][0]
        self.assertTrue(row["knowledge_points"])
        self.assertIn("full_path", row["knowledge_points"][0])
