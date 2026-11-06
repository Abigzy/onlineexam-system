"""判分与成绩接口测试：权限边界 + 链路可用性。

接口测试的重点和业务测试不一样——**这里主要在问「谁能碰什么」**：

  * 学生不能判分、不能复核、不能看别人的成绩单（跨人取 404，不是 403）；
  * 学生看自己的成绩单时，成绩没公开就只能拿到「我答了什么」；
  * 教师的批改入口在没有 exam_id 这类必填参数时，要给出可读的 400 而不是 500。

跨人取 404 而不是 403 是刻意的：403 等于承认「这个资源存在，只是不给你」，
对一个编号就能穷举的系统来说，那是免费的信息泄漏。
"""

from decimal import Decimal

from apps.common.constants import AttemptStatus, QuestionType
from apps.exam.services import ExamAttemptService
from apps.grading.services import GradingService
from apps.grading.tests.base import GradingBaseTest


class GradingPermissionTest(GradingBaseTest):
    def setUp(self):
        self.attempt = self.submit_with_correct_answers()
        self.grade_url = f"/api/v1/grading/attempts/{self.attempt.id}/grade/"
        self.finalize_url = f"/api/v1/grading/attempts/{self.attempt.id}/finalize/"
        self.report_url = f"/api/v1/grading/attempts/{self.attempt.id}/report/"

    # ------------------------------------------------------------------
    # 判分
    # ------------------------------------------------------------------
    def test_teacher_can_grade(self):
        response = self.post_json(self.grade_url, {"with_ai": True}, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["attempt_id"], self.attempt.id)
        self.assertEqual(data["summary"]["score"], 23.0)
        self.assertEqual(data["status"], AttemptStatus.REVIEWING)

    def test_student_cannot_grade(self):
        response = self.post_json(self.grade_url, {}, user=self.student)
        self.assertEqual(response.status_code, 403)

    def test_anonymous_cannot_grade(self):
        response = self.client.post(self.grade_url, {}, content_type="application/json")
        self.assertIn(response.status_code, (401, 403))

    def test_grading_in_progress_attempt_returns_400(self):
        attempt = self.start(self.make_exam())
        response = self.post_json(
            f"/api/v1/grading/attempts/{attempt.id}/grade/", {}, user=self.teacher,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("交卷", response.json()["message"])

    def test_grade_unknown_attempt_returns_404(self):
        response = self.post_json("/api/v1/grading/attempts/999999/grade/", {},
                                  user=self.teacher)
        self.assertEqual(response.status_code, 404)

    # ------------------------------------------------------------------
    # 出分
    # ------------------------------------------------------------------
    def test_teacher_can_finalize(self):
        self.post_json(self.grade_url, {"with_ai": True}, user=self.teacher)
        response = self.post_json(self.finalize_url, {}, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["visible"])

    def test_student_cannot_finalize(self):
        self.assertEqual(self.post_json(self.finalize_url, {}, user=self.student).status_code, 403)


class ReviewApiTest(GradingBaseTest):
    def setUp(self):
        self.attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(self.attempt, with_ai=True)
        self.short = self.answer_of(self.attempt, QuestionType.SHORT)
        self.url = f"/api/v1/grading/answers/{self.short.id}/review/"

    def test_teacher_can_accept_ai(self):
        response = self.post_json(self.url, {"accept_ai": True}, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["answer"]["score"], float(data["feedback"]["ai_score"]))
        self.assertEqual(data["feedback"]["action"], "accept")
        # 复核记录要能让工作台算出「还剩几道没批」
        self.assertEqual(data["grading"]["subjective_reviewed"], 1)
        self.assertEqual(data["grading"]["pending"], 1)          # 还剩论述题

    def test_teacher_can_set_score_with_comment(self):
        response = self.post_json(self.url, {"score": "3", "comment": "要点不全"},
                                  user=self.teacher)
        self.assertEqual(response.status_code, 200)
        feedback = response.json()["data"]["feedback"]
        self.assertEqual(float(feedback["final_score"]), 3.0)
        self.assertEqual(feedback["action"], "adjust")
        self.assertEqual(feedback["comment"], "要点不全")

    def test_missing_score_is_rejected_with_field_error(self):
        response = self.post_json(self.url, {}, user=self.teacher)
        self.assertEqual(response.status_code, 400)
        self.assertIn("score", response.json()["data"])

    def test_score_over_full_marks_is_rejected(self):
        response = self.post_json(self.url, {"score": "99"}, user=self.teacher)
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能超过", response.json()["message"])

    def test_student_cannot_review(self):
        self.assertEqual(self.post_json(self.url, {"score": "1"}, user=self.student).status_code, 403)

    def test_review_unknown_answer_returns_404(self):
        response = self.post_json("/api/v1/grading/answers/999999/review/", {"score": "1"},
                                  user=self.teacher)
        self.assertEqual(response.status_code, 404)


class ReportApiTest(GradingBaseTest):
    def setUp(self):
        self.attempt = self.submit_with_correct_answers()
        self.url = f"/api/v1/grading/attempts/{self.attempt.id}/report/"

    def test_teacher_report_is_fully_populated(self):
        response = self.get_json(self.url, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["visible"])
        self.assertEqual(len(data["items"]), 6)

    def test_student_own_report_triggers_lazy_grading(self):
        """学生看成绩单时顺带把客观题判了——「交卷后立刻出分」就靠这一步。"""
        response = self.get_json(self.url, user=self.student)
        self.assertEqual(response.status_code, 200)

        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.objective_score, Decimal("8.00"))

    def test_student_report_is_masked_before_publish(self):
        response = self.get_json(self.url, user=self.student)
        data = response.json()["data"]
        self.assertFalse(data["visible"])                      # 混卷还有主观题没批完
        self.assertIsNone(data.get("grading"))                 # 判分进度是教师侧信息
        for item in data["items"]:
            self.assertIsNone(item["score"])
            self.assertEqual(item["reference_answer"], "")
            self.assertTrue(item["stem"])                      # 但题干和自己的作答照给
            self.assertTrue(item["selected_labels"] or item["answer_text"])

    def test_other_student_gets_404(self):
        response = self.get_json(self.url, user=self.student_b)
        self.assertEqual(response.status_code, 404)

    def test_report_after_publish_shows_score_to_owner(self):
        GradingService.grade_attempt(self.attempt, with_ai=True)
        GradingService.finalize_attempt(self.attempt)
        data = self.get_json(self.url, user=self.student).json()["data"]
        self.assertTrue(data["visible"])
        self.assertEqual(data["score"], 23.0)

    def test_finalize_without_grading_still_auto_grades_objective(self):
        """教师漏点「判分」直接出分时，至少要把客观题判掉，不能公布「全 0 分」。"""
        response = self.post_json(
            f"/api/v1/grading/attempts/{self.attempt.id}/finalize/", {}, user=self.teacher,
        )
        self.assertEqual(response.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, AttemptStatus.GRADED)
        self.assertEqual(self.attempt.objective_score, Decimal("8.00"))
        self.assertGreater(self.attempt.score, Decimal("0"))

    def test_report_unknown_attempt_returns_404(self):
        self.assertEqual(
            self.get_json("/api/v1/grading/attempts/999999/report/", user=self.teacher).status_code,
            404,
        )


class QueueApiTest(GradingBaseTest):
    def setUp(self):
        self.attempt = self.submit_with_correct_answers()
        self.url = "/api/v1/grading/queue/"

    def test_queue_requires_exam_id(self):
        response = self.get_json(self.url, user=self.teacher)
        self.assertEqual(response.status_code, 400)

    def test_queue_lists_pending(self):
        response = self.get_json(self.url, data={"exam_id": self.exam.id}, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["attempt_id"], self.attempt.id)
        self.assertIn("multiple_choice_policy_display", data["exam"])

    def test_queue_only_pending_false_includes_done(self):
        GradingService.grade_attempt(self.attempt, with_ai=True)
        for question_type in (QuestionType.SHORT, QuestionType.ESSAY):
            GradingService.review_answer(self.answer_of(self.attempt, question_type),
                                         reviewer=self.teacher, score=Decimal("1"))
        only_pending = self.get_json(self.url, data={"exam_id": self.exam.id}, user=self.teacher).json()
        self.assertEqual(only_pending["data"]["count"], 0)

        all_rows = self.get_json(
            self.url, data={"exam_id": self.exam.id, "only_pending": "false"}, user=self.teacher,
        ).json()
        self.assertEqual(all_rows["data"]["count"], 1)

    def test_student_cannot_read_queue(self):
        response = self.get_json(self.url, data={"exam_id": self.exam.id}, user=self.student)
        self.assertEqual(response.status_code, 403)


class GradingExamListApiTest(GradingBaseTest):
    def test_teacher_sees_exams_with_attempts(self):
        self.submit_with_correct_answers()
        response = self.get_json("/api/v1/grading/exams/", user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], self.exam.id)
        self.assertEqual(data["results"][0]["submitted_total"], 1)

    def test_exam_without_attempts_is_excluded(self):
        self.make_exam()                                   # 没有任何作答
        response = self.get_json("/api/v1/grading/exams/", user=self.teacher)
        self.assertEqual(response.json()["data"]["count"], 0)

    def test_student_cannot_list(self):
        self.assertEqual(self.get_json("/api/v1/grading/exams/", user=self.student).status_code, 403)


class ScoreboardApiTest(GradingBaseTest):
    def test_teacher_can_read_scoreboard(self):
        attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(attempt, with_ai=True)
        GradingService.finalize_attempt(attempt)

        response = self.get_json("/api/v1/grading/scoreboard/",
                                 data={"exam_id": self.exam.id}, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["summary"]["graded"], 1)
        self.assertEqual(data["results"][0]["rank"], 1)

    def test_missing_exam_id_returns_400(self):
        self.assertEqual(self.get_json("/api/v1/grading/scoreboard/", user=self.teacher).status_code,
                         400)

    def test_student_cannot_read_scoreboard(self):
        response = self.get_json("/api/v1/grading/scoreboard/",
                                 data={"exam_id": self.exam.id}, user=self.student)
        self.assertEqual(response.status_code, 403)


class WrongBookApiTest(GradingBaseTest):
    def setUp(self):
        exam = self.make_objective_exam(show_score_immediately=True)
        attempt = self.start(exam)
        mapping = self.paper_map(attempt)
        self.save_answers(attempt, [{
            "paper_question_id": mapping[QuestionType.SINGLE]["paper_question_id"],
            "selected_labels": "B",
        }])
        ExamAttemptService.submit(attempt)
        GradingService.grade_attempt(attempt)

    def test_student_reads_own_wrong_book(self):
        response = self.get_json("/api/v1/grading/wrong-book/", user=self.student)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["student"]["id"], self.student.id)

    def test_student_cannot_peek_at_others(self):
        """传别人的 student_id 会被无视，返回的仍是自己的数据。"""
        response = self.get_json("/api/v1/grading/wrong-book/",
                                 data={"student_id": self.student_b.id}, user=self.student)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["student"]["id"], self.student.id)

    def test_teacher_must_specify_student(self):
        response = self.get_json("/api/v1/grading/wrong-book/", user=self.teacher)
        self.assertEqual(response.status_code, 400)

    def test_teacher_can_read_specified_student(self):
        response = self.get_json("/api/v1/grading/wrong-book/",
                                 data={"student_id": self.student.id}, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["count"], 1)


class CalibrationApiTest(GradingBaseTest):
    def test_teacher_can_read_calibration(self):
        attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(attempt, with_ai=True)
        GradingService.review_answer(self.answer_of(attempt, QuestionType.SHORT),
                                     reviewer=self.teacher, accept_ai=True)

        response = self.get_json("/api/v1/grading/calibration/", user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["sample"], 1)
        self.assertEqual(data["accept_rate"], 100.0)

    def test_student_cannot_read_calibration(self):
        self.assertEqual(self.get_json("/api/v1/grading/calibration/", user=self.student).status_code,
                         403)


class FeedbackApiTest(GradingBaseTest):
    def setUp(self):
        self.attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(self.attempt, with_ai=True)
        GradingService.review_answer(self.answer_of(self.attempt, QuestionType.SHORT),
                                     reviewer=self.teacher, score=Decimal("3"))

    def test_teacher_can_list_feedback(self):
        response = self.get_json("/api/v1/grading/feedback/", user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["action"], "adjust")

    def test_filter_by_attempt(self):
        other = self.submit_with_correct_answers(exam=self.make_exam(), student=self.student_b)
        GradingService.grade_attempt(other, with_ai=True)
        GradingService.review_answer(self.answer_of(other, QuestionType.SHORT),
                                     reviewer=self.teacher, accept_ai=True)

        response = self.get_json("/api/v1/grading/feedback/",
                                 data={"attempt_id": self.attempt.id}, user=self.teacher)
        self.assertEqual(response.json()["data"]["count"], 1)

    def test_student_cannot_list_feedback(self):
        self.assertEqual(self.get_json("/api/v1/grading/feedback/", user=self.student).status_code,
                         403)


class ExamPolicyApiTest(GradingBaseTest):
    """多选策略是考试级配置，必须能通过考试接口读写。"""

    def test_exam_exposes_and_accepts_policy(self):
        response = self.patch_json(f"/api/v1/exams/{self.exam.id}/",
                                   {"multiple_choice_policy": "partial"}, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["multiple_choice_policy"], "partial")
        self.assertIn("漏选", data["multiple_choice_policy_display"])

        self.exam.refresh_from_db()
        self.assertEqual(self.exam.multiple_choice_policy, "partial")

    def test_invalid_policy_is_rejected(self):
        response = self.patch_json(f"/api/v1/exams/{self.exam.id}/",
                                   {"multiple_choice_policy": "whatever"}, user=self.teacher)
        self.assertEqual(response.status_code, 400)
