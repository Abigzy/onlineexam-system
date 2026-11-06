"""答案泄漏回归：``GET /attempts/{id}/answers/`` 不得向学生下发参考答案。

来源：接口自动化测试前的人工走查发现——该接口曾复用教师的完整序列化器，
权限又只要求登录，学生**考到一半**就能取走全部参考答案与解析，
与项目「答案防泄漏只有一个出口」的设计直接冲突。

学生看答案的正当出口是 ``GET /attempts/{id}/result/``（交卷后 + 成绩已公开）。
"""

from apps.common.constants import AttemptStatus
from apps.exam.services import ExamAttemptService
from apps.grading.services import GradingService
from apps.papers.models import PaperQuestion

from .base import ExamBaseTest


class StudentAnswerLeakTest(ExamBaseTest):
    def setUp(self):
        super().setUp()
        self.exam = self.make_exam()
        self.attempt = self.start(self.exam)
        self.target = PaperQuestion.objects.filter(paper=self.exam.paper).first()
        ExamAttemptService.save_answers(self.attempt, [
            {"paper_question_id": self.target.id, "selected_labels": "A"},
        ])

    def _answers(self, user):
        return self.get_json(f"/api/v1/attempts/{self.attempt.id}/answers/", user=user)

    # ------------------------------------------------------------------
    def test_student_gets_no_reference_answer(self):
        """核心回归：学生视角的字段里不能有参考答案与解析。"""
        row = self._answers(self.student).json()["data"]["results"][0]
        self.assertNotIn("reference_answer", row, "学生不应拿到参考答案")
        self.assertNotIn("analysis", row, "学生不应拿到解析")

    def test_student_still_sees_own_answer(self):
        """收紧权限不能把正常功能一起关掉：学生仍要能看到自己选了什么。"""
        body = self._answers(self.student).json()["data"]
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["selected_labels"], "A")
        self.assertTrue(body["results"][0]["is_answered"])
        self.assertIn("stem", body["results"][0], "题干仍应下发")

    def test_payload_string_has_no_answer_text(self):
        """整段响应体里连答案文本都不该出现（防止它藏在别的字段里）。"""
        expected = (self.target.question.correct_answer_text() or "").strip()
        # 单字符答案（如 "A"）会和 selected_labels / 各种标识符撞车，字面量断言不可靠；
        # 那种情况由 test_student_gets_no_reference_answer 的「字段不存在」断言来守。
        if len(expected) < 3:
            self.skipTest(f"答案文本「{expected}」过短，与普通标识符无法区分，跳过字面量断言")
        raw = self._answers(self.student).content.decode("utf-8")
        self.assertNotIn(expected, raw, f"响应体里出现了答案文本「{expected}」")

    def test_teacher_still_gets_answer(self):
        """教师批改必须还能看到答案——别把教师端也一起锁死。"""
        row = self._answers(self.teacher).json()["data"]["results"][0]
        self.assertIn("reference_answer", row)
        self.assertIn("analysis", row)

    def test_student_cannot_read_others_attempt(self):
        """顺带确认越权隔离没被破坏：别人看不了。"""
        response = self.get_json(
            f"/api/v1/attempts/{self.attempt.id}/answers/", user=self.student_b,
        )
        self.assertEqual(response.status_code, 404, "跨学生访问应 404（不拿 403 当探针）")


class AnswerScoreVisibilityTest(ExamBaseTest):
    """D-03R：光删答案与解析不够，逐题对错与得分也要按「成绩可见性」收敛。

    泄漏路径（修复前）：学生交卷 → 打开成绩单触发 ``ensure_graded``（只判客观题）
    → 再拉 ``/answers/`` 就能知道逐题对错与得分。不含答案文本，所以不算致命；
    但 ``max_attempts > 1`` 时可用于下一轮针对性作答。
    """

    def setUp(self):
        super().setUp()
        self.exam = self.make_exam()
        self.attempt = self.start(self.exam)
        self.target = PaperQuestion.objects.filter(paper=self.exam.paper).first()
        ExamAttemptService.save_answers(self.attempt, [
            {"paper_question_id": self.target.id, "selected_labels": "A"},
        ])

    def _row(self):
        body = self.get_json(
            f"/api/v1/attempts/{self.attempt.id}/answers/", user=self.student,
        ).json()["data"]
        return body["results"][0]

    def _force_graded(self):
        """把作答推到「已出分」终态（真实链路要教师复核，这里直给状态）。"""
        ExamAttemptService.submit(self.attempt)
        GradingService.ensure_graded(self.attempt)
        self.attempt.refresh_from_db()
        self.attempt.status = AttemptStatus.GRADED
        self.attempt.save(update_fields=["status"])

    # ------------------------------------------------------------------
    def test_in_progress_hides_correctness(self):
        """答题中：还没判分，字段应为 null。"""
        row = self._row()
        self.assertIsNone(row["is_correct"])
        self.assertIsNone(row["score"])

    def test_submitted_not_graded_hides_correctness(self):
        """刚交卷、还没判分。"""
        ExamAttemptService.submit(self.attempt)
        row = self._row()
        self.assertIsNone(row["is_correct"])
        self.assertIsNone(row["score"])

    def test_lazy_graded_without_publish_hides_correctness(self):
        """**核心场景**：客观题已被 ensure_graded 判过，但成绩没对本人公开。"""
        self.exam.show_score_immediately = False
        self.exam.save(update_fields=["show_score_immediately"])
        ExamAttemptService.submit(self.attempt)
        GradingService.ensure_graded(self.attempt)
        self.attempt.refresh_from_db()
        self.assertTrue(
            self.attempt.is_graded or self.attempt.status == AttemptStatus.REVIEWING,
            "前提：客观题应已被判（否则这条用例测不到泄漏）",
        )
        row = self._row()
        self.assertIsNone(row["is_correct"], "未公开成绩时不能给出逐题对错")
        self.assertIsNone(row["score"])

    def test_graded_counts_as_published(self):
        """教师出分本身就是「发布成绩」—— 即使 show_score_immediately 为 False。

        这是模块五定的口径（人工复核完还看不到分，那条链路就白做了）。
        与 `/grading/attempts/{id}/report/` 必须给出**同一个**答案。
        """
        self.exam.show_score_immediately = False
        self.exam.save(update_fields=["show_score_immediately"])
        self._force_graded()
        row = self._row()
        self.assertIsNotNone(row["is_correct"], "GRADED 应视为已发布")
        self.assertIsNotNone(row["score"])

    def test_auto_graded_without_switch_hides_correctness(self):
        """自动判完但考试没开「立即显示得分」→ 仍然不给。"""
        self.exam.show_score_immediately = False
        self.exam.save(update_fields=["show_score_immediately"])
        ExamAttemptService.submit(self.attempt)
        GradingService.ensure_graded(self.attempt)
        self.attempt.refresh_from_db()
        self.attempt.status = AttemptStatus.AUTO_GRADED
        self.attempt.save(update_fields=["status"])

        row = self._row()
        self.assertIsNone(row["is_correct"])
        self.assertIsNone(row["score"])

    def test_auto_graded_with_switch_shows_correctness(self):
        """自动判完 + 开了「立即显示得分」→ 给。"""
        self.exam.show_score_immediately = True
        self.exam.save(update_fields=["show_score_immediately"])
        ExamAttemptService.submit(self.attempt)
        GradingService.ensure_graded(self.attempt)
        self.attempt.refresh_from_db()
        self.attempt.status = AttemptStatus.AUTO_GRADED
        self.attempt.save(update_fields=["status"])

        row = self._row()
        self.assertIsNotNone(row["is_correct"])
        self.assertIsNotNone(row["score"])

    def test_graded_and_published_shows_correctness(self):
        """成绩公开后要正常给出——别把正常功能一起藏了。"""
        self.exam.show_score_immediately = True
        self.exam.save(update_fields=["show_score_immediately"])
        self._force_graded()
        row = self._row()
        self.assertIsNotNone(row["is_correct"])
        self.assertIsNotNone(row["score"])

    def _report(self):
        """学生看自己的成绩单（/grading/attempts/{id}/report/）。"""
        return self.get_json(
            f"/api/v1/grading/attempts/{self.attempt.id}/report/", user=self.student,
        ).json()["data"]

    def test_report_and_answers_agree_when_graded(self):
        """BUG-01 回归：同一份作答，两个接口必须给同一个可见性结论。

        修复前：``/grading/attempts/{id}/report/`` 的 ``ScoreReportService.score_visible()``
        认「GRADED 恒可见」，``/attempts/{id}/answers/`` 认 ``is_graded and show_score``，
        同一时刻一个说可见、一个把字段藏成 null。
        """
        self.exam.show_score_immediately = False
        self.exam.save(update_fields=["show_score_immediately"])
        self._force_graded()

        report = self._report()
        row = self._row()
        self.assertTrue(report["visible"], "GRADED 应视为已发布")
        self.assertIsNotNone(report["score"])
        self.assertIsNotNone(row["score"], "answers 不能与 report 相反")
        self.assertIsNotNone(row["is_correct"])
        # 两端对「是否公开」的回答必须一致
        self.assertEqual(report["visible"], row["score"] is not None)

    def test_report_hides_top_level_when_not_published(self):
        """未公开时，report 的顶层得分与对错计数也要藏——光藏 items 不够。"""
        self.exam.show_score_immediately = False
        self.exam.save(update_fields=["show_score_immediately"])
        ExamAttemptService.submit(self.attempt)
        GradingService.ensure_graded(self.attempt)
        self.attempt.refresh_from_db()
        self.attempt.status = AttemptStatus.AUTO_GRADED
        self.attempt.save(update_fields=["status"])

        report = self._report()
        self.assertFalse(report["visible"])
        self.assertIsNone(report["score"], "顶层 score 也得分敛")
        self.assertIsNone(report["passed"])
        self.assertIsNone(report["counts"]["correct"])
        self.assertIsNone(report["counts"]["wrong"])
        # 与 answers 端一致
        self.assertIsNone(self._row()["score"])

    def test_own_answer_always_visible(self):
        """自己的作答内容任何时候都能看，不受成绩可见性影响。"""
        row = self._row()
        self.assertEqual(row["selected_labels"], "A")
        self.assertTrue(row["is_answered"])
        self.assertIn("stem", row)
