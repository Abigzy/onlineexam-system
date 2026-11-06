"""考试接口测试。

覆盖：
  * 角色权限边界（教师管考试、学生只能作答自己的、跨学生越权取不到数据）
  * 考试 CRUD 与状态流转
  * 学生视角**不含试卷内容**、下发**不含答案**（模块四的两条安全红线）
  * 作答链路：开考 → 暂存 → 心跳 → 行为 → 交卷 → 结果
  * 教师看板：overview / attempts / statistics / candidates
"""

from datetime import timedelta

from django.utils import timezone

from apps.common.constants import AttemptStatus, ExamStatus
from apps.exam.constants import SubmitReason
from apps.exam.models import Answer, ExamAttempt
from apps.exam.services import ExamAttemptService
from apps.papers.models import PaperQuestion
from apps.papers.services import PaperService

from .base import ExamBaseTest


class ExamApiPermissionTests(ExamBaseTest):
    """权限边界。"""

    def setUp(self):
        super().setUp()
        self.exam = self.make_exam()

    def test_student_cannot_create_exam(self):
        response = self.post_json("/api/v1/exams/", {"title": "越权", "paper_id": self.exam.paper_id},
                                 user=self.student)
        self.assertEqual(response.status_code, 403)

    def test_student_cannot_update_or_delete_exam(self):
        self.assertEqual(
            self.patch_json(f"/api/v1/exams/{self.exam.id}/", {"title": "改"},
                            user=self.student).status_code, 403)
        self.assertEqual(
            self.delete_json(f"/api/v1/exams/{self.exam.id}/", user=self.student).status_code, 403)

    def test_student_cannot_read_overview(self):
        response = self.get_json(f"/api/v1/exams/{self.exam.id}/overview/", user=self.student)
        self.assertEqual(response.status_code, 403)

    def test_teacher_cannot_start_exam(self):
        response = self.post_json(f"/api/v1/exams/{self.exam.id}/start/", {}, user=self.teacher)
        self.assertEqual(response.status_code, 403)

    def test_teacher_cannot_save_answer(self):
        attempt = self.start(self.exam)
        response = self.post_json(f"/api/v1/attempts/{attempt.id}/save/",
                                 {"paper_question_id": 1, "selected_labels": "A"},
                                 user=self.teacher)
        self.assertEqual(response.status_code, 403)

    def test_anonymous_rejected(self):
        self.assertEqual(self.client.get("/api/v1/exams/").status_code, 401)

    def test_student_cannot_read_other_students_attempt(self):
        attempt = self.start(self.exam, student=self.student_b)
        response = self.get_json(f"/api/v1/attempts/{attempt.id}/", user=self.student)
        self.assertEqual(response.status_code, 404)

    def test_student_cannot_open_other_students_paper(self):
        attempt = self.start(self.exam, student=self.student_b)
        response = self.get_json(f"/api/v1/attempts/{attempt.id}/paper/", user=self.student)
        self.assertEqual(response.status_code, 404)

    def test_teacher_can_read_any_attempt(self):
        attempt = self.start(self.exam, student=self.student)
        response = self.get_json(f"/api/v1/attempts/{attempt.id}/", user=self.teacher)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["student_id"], self.student.id)


class ExamCrudApiTests(ExamBaseTest):
    """考试 CRUD 与状态。"""

    def test_teacher_creates_and_lists_exam(self):
        paper = self.make_paper()
        response = self.post_json("/api/v1/exams/", {
            "title": "期中考试", "paper_id": paper.id, "duration_minutes": 45,
            "max_switch_count": 2, "shuffle_questions": True,
        }, user=self.teacher)
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]
        self.assertEqual(data["status"], ExamStatus.DRAFT)
        self.assertEqual(data["paper"]["question_count"], 6)
        self.assertEqual(data["duration_minutes_effective"], 45)
        self.assertEqual(data["question_count"], 6)

        listing = self.get_json("/api/v1/exams/", user=self.teacher).json()["data"]
        self.assertEqual(listing["count"], 1)

    def test_create_requires_valid_paper(self):
        response = self.post_json("/api/v1/exams/", {"title": "无卷", "paper_id": 999999},
                                 user=self.teacher)
        self.assertEqual(response.status_code, 400)

    def test_create_with_participants(self):
        paper = self.make_paper()
        response = self.post_json("/api/v1/exams/", {
            "title": "指定考试", "paper_id": paper.id,
            "open_to_all": False, "participant_ids": [self.student.id],
        }, user=self.teacher)
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]
        self.assertFalse(data["open_to_all"])
        self.assertEqual(data["participant_ids"], [self.student.id])

    def test_create_rejects_non_student_participant(self):
        paper = self.make_paper()
        response = self.post_json("/api/v1/exams/", {
            "title": "错误名单", "paper_id": paper.id,
            "participant_ids": [self.teacher.id],
        }, user=self.teacher)
        self.assertEqual(response.status_code, 400)

    def test_status_flow_and_validation(self):
        exam = self.make_exam(publish=False)
        # 发布前是草稿
        response = self.post_json(f"/api/v1/exams/{exam.id}/status/", {"status": "published"},
                                 user=self.teacher)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], ExamStatus.PUBLISHED)

        # 非法流转被拒
        response = self.post_json(f"/api/v1/exams/{exam.id}/status/", {"status": "draft"},
                                 user=self.teacher)
        self.assertEqual(response.status_code, 200)  # published → draft 是允许的
        response = self.post_json(f"/api/v1/exams/{exam.id}/status/", {"status": "ongoing"},
                                 user=self.teacher)
        self.assertEqual(response.status_code, 400)

    def test_publish_rejects_empty_paper(self):
        empty = PaperService.create(data={"title": "空卷", "subject": "测试"}, user=self.teacher)
        exam = self.make_exam(publish=False, paper=empty)
        response = self.post_json(f"/api/v1/exams/{exam.id}/status/", {"status": "published"},
                                 user=self.teacher)
        self.assertEqual(response.status_code, 400)
        self.assertIn("没有题目", response.json()["message"])

    def test_delete_blocked_when_attempts_exist(self):
        exam = self.make_exam()
        self.start(exam)
        response = self.delete_json(f"/api/v1/exams/{exam.id}/", user=self.teacher)
        self.assertEqual(response.status_code, 400)

    def test_update_keeps_paper_once_started(self):
        exam = self.make_exam()
        self.start(exam)
        other = self.make_paper()
        response = self.patch_json(f"/api/v1/exams/{exam.id}/", {"paper_id": other.id},
                                  user=self.teacher)
        self.assertEqual(response.status_code, 400)

    def test_student_list_hides_draft_exams(self):
        self.make_exam(publish=False)
        listing = self.get_json("/api/v1/exams/", user=self.student).json()["data"]
        self.assertEqual(listing["count"], 0)

    def test_student_list_scoped_to_participants(self):
        self.make_exam(publish=False)
        targeted = self.make_exam(publish=False, open_to_all=False,
                                  participant_ids=[self.student_b.id])
        from apps.exam.services import ExamService

        ExamService.change_status(targeted, ExamStatus.PUBLISHED)

        mine = self.get_json("/api/v1/exams/", user=self.student).json()["data"]
        self.assertEqual(mine["count"], 0)

        theirs = self.get_json("/api/v1/exams/", user=self.student_b).json()["data"]
        self.assertEqual(theirs["count"], 1)
        self.assertEqual(theirs["results"][0]["id"], targeted.id)

    def test_student_exam_payload_has_no_paper_content(self):
        """学生还没开考时，连题型分布都不该知道。"""
        self.make_exam()
        listing = self.get_json("/api/v1/exams/", user=self.student).json()["data"]
        row = listing["results"][0]
        for forbidden in ("sections", "questions", "paper", "paper_questions", "ai_notes"):
            self.assertNotIn(forbidden, row)
        self.assertIn("can_start", row)
        self.assertIn("attempts_left", row)

    def test_student_list_shows_locked_reason(self):
        now = timezone.now()
        self.make_exam(start_time=now + timedelta(hours=3))
        listing = self.get_json("/api/v1/exams/", user=self.student).json()["data"]
        row = listing["results"][0]
        self.assertFalse(row["can_start"])
        self.assertEqual(row["locked_reason"], "考试尚未开始")

    def test_exam_filter_by_status_and_open_now(self):
        running = self.make_exam()
        draft = self.make_exam(publish=False)

        data = self.get_json("/api/v1/exams/?status=draft", user=self.teacher).json()["data"]
        self.assertEqual([row["id"] for row in data["results"]], [draft.id])

        data = self.get_json("/api/v1/exams/?is_open_now=true", user=self.teacher).json()["data"]
        self.assertEqual([row["id"] for row in data["results"]], [running.id])

    def test_candidates_endpoint(self):
        # 教师视角：按班级分组返回，且只含自己班的学生
        data = self.get_json("/api/v1/exams/candidates/", user=self.teacher).json()["data"]
        groups = data["classrooms"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "考试测试班")
        names = [s["name"] for s in groups[0]["students"]]
        self.assertIn("考生甲", names)
        self.assertIn("考生乙", names)
        self.assertNotIn("监考老师", names)

        # keyword 过滤后，只留下匹配学号的学生
        data = self.get_json("/api/v1/exams/candidates/?keyword=S2026002",
                             user=self.teacher).json()["data"]
        groups = data["classrooms"]
        self.assertEqual(len(groups), 1)
        self.assertEqual([s["name"] for s in groups[0]["students"]], ["考生乙"])

    def test_statistics_endpoint(self):
        self.make_exam()
        data = self.get_json("/api/v1/exams/statistics/", user=self.teacher).json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["attempt_total"], 0)


class ExamAttemptApiTests(ExamBaseTest):
    """作答链路。"""

    def setUp(self):
        super().setUp()
        self.exam = self.make_exam()
        self.url = f"/api/v1/exams/{self.exam.id}/start/"

    def start_via_api(self, user=None):
        response = self.post_json(self.url, {}, user=user or self.student)
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()["data"]

    # ------------------------------------------------------------------
    def test_start_returns_paper_without_answers(self):
        data = self.start_via_api()
        self.assertFalse(data["finished"])
        self.assertEqual(len(data["questions"]), 6)
        self.assertGreater(data["remaining_seconds"], 0)
        self.assertIn("config", data)

        blob = str(data)
        self.assertNotIn("reference_answer", blob)
        self.assertNotIn("analysis", blob)
        self.assertNotIn("is_correct", blob)

    def test_start_is_idempotent_for_same_student(self):
        first = self.start_via_api()
        second = self.start_via_api()
        self.assertEqual(first["attempt"]["id"], second["attempt"]["id"])
        self.assertEqual(ExamAttempt.objects.filter(exam=self.exam).count(), 1)

    def test_start_blocked_for_unpublished_exam(self):
        """草稿考试对学生来说「不存在」——查不到比看得见但点不动更合理。"""
        exam = self.make_exam(publish=False)
        response = self.post_json(f"/api/v1/exams/{exam.id}/start/", {}, user=self.student)
        self.assertEqual(response.status_code, 404)

    def test_start_blocked_before_window_opens(self):
        exam = self.make_exam(start_time=timezone.now() + timedelta(hours=3))
        response = self.post_json(f"/api/v1/exams/{exam.id}/start/", {}, user=self.student)
        self.assertEqual(response.status_code, 400)

    def test_save_answers_bulk(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        first = data["questions"][0]

        response = self.post_json(f"/api/v1/attempts/{attempt_id}/answers/", {
            "items": [{"paper_question_id": first["paper_question_id"],
                       "selected_labels": "A", "time_spent_seconds": 9}],
        }, user=self.student)
        self.assertEqual(response.status_code, 200)
        body = response.json()["data"]
        self.assertEqual(body["saved"], 1)
        self.assertEqual(body["answered_count"], 1)

    def test_save_single_answer(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        first = data["questions"][0]
        response = self.post_json(f"/api/v1/attempts/{attempt_id}/save/", {
            "paper_question_id": first["paper_question_id"],
            "selected_labels": "a", "is_flagged": True,
        }, user=self.student)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["answer"]["selected_labels"], "A")
        self.assertTrue(response.json()["data"]["answer"]["is_flagged"])

    def test_save_rejects_foreign_question(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        other_paper = self.make_paper()
        from apps.papers.models import PaperQuestion

        foreign = PaperQuestion.objects.filter(paper=other_paper).first()
        response = self.post_json(f"/api/v1/attempts/{attempt_id}/answers/", {
            "items": [{"paper_question_id": foreign.id, "selected_labels": "A"}],
        }, user=self.student)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["saved"], 0)

    def test_paper_endpoint_restores_answers(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        first = data["questions"][0]
        self.post_json(f"/api/v1/attempts/{attempt_id}/save/", {
            "paper_question_id": first["paper_question_id"], "selected_labels": "A",
        }, user=self.student)

        restored = self.get_json(f"/api/v1/attempts/{attempt_id}/paper/", user=self.student)
        self.assertEqual(restored.status_code, 200)
        payload = restored.json()["data"]
        saved = [q for q in payload["questions"] if q["answer"]["is_answered"]]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["answer"]["selected_labels"], "A")

    def test_heartbeat_endpoint(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        response = self.post_json(f"/api/v1/attempts/{attempt_id}/heartbeat/",
                                 {"client_remaining": 100}, user=self.student)
        self.assertEqual(response.status_code, 200)
        body = response.json()["data"]
        self.assertGreater(body["remaining_seconds"], 0)
        self.assertFalse(body["should_submit"])

    def test_behavior_endpoint_and_limit(self):
        exam = self.make_exam(max_switch_count=1)
        data = self.post_json(f"/api/v1/exams/{exam.id}/start/", {},
                             user=self.student).json()["data"]
        attempt_id = data["attempt"]["id"]

        response = self.post_json(f"/api/v1/attempts/{attempt_id}/behavior/",
                                 {"events": [{"type": "switch"}, {"type": "switch"}]},
                                 user=self.student)
        self.assertEqual(response.status_code, 200)
        body = response.json()["data"]
        self.assertEqual(body["switch_count"], 2)
        self.assertTrue(body["limit_exceeded"])
        self.assertFalse(body["auto_submitted"])

    def test_behavior_auto_submit_closes_attempt(self):
        exam = self.make_exam(max_switch_count=1, auto_submit_on_limit=True)
        data = self.post_json(f"/api/v1/exams/{exam.id}/start/", {},
                             user=self.student).json()["data"]
        attempt_id = data["attempt"]["id"]

        response = self.post_json(f"/api/v1/attempts/{attempt_id}/behavior/",
                                 {"events": [{"type": "switch"}, {"type": "switch"}]},
                                 user=self.student)
        body = response.json()["data"]
        self.assertTrue(body["auto_submitted"])
        attempt = ExamAttempt.objects.get(pk=attempt_id)
        self.assertEqual(attempt.status, AttemptStatus.SUBMITTED)
        self.assertEqual(attempt.submit_reason, SubmitReason.FORCED)

    def test_behavior_rejects_unknown_event_type(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        response = self.post_json(f"/api/v1/attempts/{attempt_id}/behavior/",
                                 {"events": [{"type": "teleport"}]}, user=self.student)
        self.assertEqual(response.status_code, 400)

    def test_submit_with_items_flushes_last_batch(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        items = []
        for question in data["questions"]:
            if question["question_type"] in ("single", "multiple", "judge"):
                items.append({"paper_question_id": question["paper_question_id"],
                              "selected_labels": "A"})
            else:
                items.append({"paper_question_id": question["paper_question_id"],
                              "answer_text": "作答内容"})

        response = self.post_json(f"/api/v1/attempts/{attempt_id}/submit/",
                                 {"reason": "manual", "items": items}, user=self.student)
        self.assertEqual(response.status_code, 200)
        body = response.json()["data"]
        self.assertEqual(body["status"], AttemptStatus.SUBMITTED)
        self.assertEqual(body["answered_count"], 6)
        # 交卷时带上的最后一批作答必须真的落库
        self.assertEqual(Answer.objects.filter(attempt_id=attempt_id).count(), 6)

    def test_submit_is_idempotent_over_http(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        first = self.post_json(f"/api/v1/attempts/{attempt_id}/submit/", {},
                              user=self.student).json()["data"]
        second = self.post_json(f"/api/v1/attempts/{attempt_id}/submit/", {},
                               user=self.student).json()["data"]
        self.assertEqual(first["submitted_at"], second["submitted_at"])

    def test_save_after_submit_rejected(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        self.post_json(f"/api/v1/attempts/{attempt_id}/submit/", {}, user=self.student)
        response = self.post_json(f"/api/v1/attempts/{attempt_id}/save/", {
            "paper_question_id": data["questions"][0]["paper_question_id"],
            "selected_labels": "A",
        }, user=self.student)
        self.assertEqual(response.status_code, 400)

    def test_paper_endpoint_reports_finished_after_submit(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        self.post_json(f"/api/v1/attempts/{attempt_id}/submit/", {}, user=self.student)
        response = self.get_json(f"/api/v1/attempts/{attempt_id}/paper/", user=self.student)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["finished"])

    def test_result_hides_score_until_graded(self):
        data = self.start_via_api()
        attempt_id = data["attempt"]["id"]
        self.post_json(f"/api/v1/attempts/{attempt_id}/submit/", {}, user=self.student)
        response = self.get_json(f"/api/v1/attempts/{attempt_id}/result/", user=self.student)
        self.assertEqual(response.status_code, 200)
        detail = response.json()["data"]
        self.assertFalse(detail["show_score"])
        self.assertNotIn("answers", detail)

    def test_my_attempt_list_only_own(self):
        mine = self.start(self.exam, student=self.student)
        self.start(self.exam, student=self.student_b)

        listing = self.get_json("/api/v1/attempts/", user=self.student).json()["data"]
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["results"][0]["id"], mine.id)
        # 学生视角不应出现 student_id 之外的身份字段（越权面越小越好）
        self.assertNotIn("ip_address", listing["results"][0])
        self.assertNotIn("user_agent", listing["results"][0])

        all_rows = self.get_json("/api/v1/attempts/", user=self.teacher).json()["data"]
        self.assertEqual(all_rows["count"], 2)

    def test_answers_endpoint_get_for_teacher(self):
        attempt = self.start(self.exam)
        target = PaperQuestion.objects.filter(paper=self.exam.paper).first()
        ExamAttemptService.save_answers(attempt, [
            {"paper_question_id": target.id, "selected_labels": "A"},
        ])
        response = self.get_json(f"/api/v1/attempts/{attempt.id}/answers/", user=self.teacher)
        self.assertEqual(response.status_code, 200)
        body = response.json()["data"]
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["selected_labels"], "A")
        # 教师批改要看参考答案
        self.assertIn("reference_answer", body["results"][0])


class ExamTeacherBoardApiTests(ExamBaseTest):
    """教师看板。"""

    def setUp(self):
        super().setUp()
        self.exam = self.make_exam()

    def test_overview_and_attempts(self):
        attempt = self.start(self.exam)

        ExamAttemptService.submit(attempt)
        ExamAttemptService.start(exam=self.exam, student=self.student_b)

        overview = self.get_json(f"/api/v1/exams/{self.exam.id}/overview/",
                                user=self.teacher).json()["data"]
        self.assertEqual(overview["started"], 2)
        self.assertEqual(overview["submitted"], 1)
        self.assertEqual(overview["in_progress"], 1)
        self.assertIsNone(overview["avg_score"])

        board = self.get_json(f"/api/v1/exams/{self.exam.id}/attempts/",
                             user=self.teacher).json()["data"]
        self.assertEqual(board["count"], 2)
        self.assertEqual(board["overview"]["started"], 2)
        self.assertTrue(any(row["status"] == AttemptStatus.IN_PROGRESS for row in board["results"]))

    def test_overview_includes_status_display(self):
        """overview 必须带 `computed_status_display`。

        BUG-04：监控页徽章读的就是这个字段，后端漏返回时前端 `undefined || ''`
        把徽章清成空白。接口测试此前只断言了数字字段，所以没发现。
        """
        board = self.get_json(f"/api/v1/exams/{self.exam.id}/attempts/",
                              user=self.teacher).json()["data"]
        overview = board["overview"]
        self.assertIn("computed_status", overview)
        self.assertIn(
            "computed_status_display", overview,
            "overview 缺少 computed_status_display，监控页状态徽章会是空白",
        )
        self.assertTrue(
            overview["computed_status_display"],
            "展示文案不能为空串",
        )

    def test_attempts_keyword_filter(self):
        self.start(self.exam, student=self.student)
        self.start(self.exam, student=self.student_b)
        board = self.get_json(f"/api/v1/exams/{self.exam.id}/attempts/?keyword=考生乙",
                             user=self.teacher).json()["data"]
        self.assertEqual(board["count"], 1)
        self.assertEqual(board["results"][0]["student_name"], "考生乙")

    def test_attempts_flags_suspicious_student(self):
        attempt = self.start(self.exam, student=self.student)

        ExamAttemptService.record_behavior(attempt, [{"type": "switch", "count": 5}])

        overview = self.get_json(f"/api/v1/exams/{self.exam.id}/overview/",
                                user=self.teacher).json()["data"]
        self.assertEqual(overview["behavior"]["flagged_count"], 1)
        self.assertEqual(overview["behavior"]["flagged"][0]["switch_count"], 5)

        board = self.get_json(f"/api/v1/exams/{self.exam.id}/attempts/",
                             user=self.teacher).json()["data"]
        self.assertTrue(board["results"][0]["suspicious"])

    def test_attempt_filter_suspicious_only(self):
        clean = self.start(self.exam, student=self.student)
        dirty = self.start(self.exam, student=self.student_b)

        ExamAttemptService.record_behavior(dirty, [{"type": "switch", "count": 9}])

        data = self.get_json("/api/v1/attempts/?suspicious=true",
                             user=self.teacher).json()["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], dirty.id)

    def test_attempt_filter_only_in_progress(self):
        attempt = self.start(self.exam)

        ExamAttemptService.submit(attempt)
        ExamAttemptService.start(exam=self.exam, student=self.student_b)

        data = self.get_json("/api/v1/attempts/?only_in_progress=true",
                             user=self.teacher).json()["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["student_id"], self.student_b.id)
