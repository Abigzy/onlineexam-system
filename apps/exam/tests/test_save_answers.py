"""批量暂存的健壮性回归（BUG-02）。

来源：接口自动化测试发现——``POST /attempts/{id}/answers/`` 的 ``items`` 里
同一个 ``paper_question_id`` 出现两次时，服务端不去重直接落库，第二次 insert
撞唯一键 ``uniq_answer_attempt_question`` → **500 / 50000，且整批一条都没存**。

触发场景很真实：前端「自动保存」与「手动保存」并发、弱网重试，学生以为存上了，
实际整批丢失。

修复分两层：
  * **入口去重**——同一请求内的重复项以最后一条为准（``save_answers``）；
  * **跨请求兜底**——并发各自 insert 撞键时退化为 update（``_save_answer_row``）。
"""

from datetime import timedelta

from django.utils import timezone

from apps.exam.models import Answer
from apps.exam.services import ExamAttemptService
from apps.papers.models import PaperQuestion

from .base import ExamBaseTest


class SaveAnswersRobustnessTest(ExamBaseTest):
    def setUp(self):
        super().setUp()
        self.exam = self.make_exam()
        self.attempt = self.start(self.exam)
        self.pq = PaperQuestion.objects.filter(paper=self.exam.paper).first()

    def _post(self, items):
        return self.post_json(
            f"/api/v1/attempts/{self.attempt.id}/answers/",
            {"items": items}, user=self.student,
        )

    # ------------------------------------------------------------------
    # 1. 同一请求内重复
    # ------------------------------------------------------------------
    def test_duplicate_items_in_one_request(self):
        """一道题提交两次：正常返回，只落一条，取**最后一条**。"""
        response = self._post([
            {"paper_question_id": self.pq.id, "selected_labels": "A"},
            {"paper_question_id": self.pq.id, "selected_labels": "B"},
        ])
        self.assertEqual(response.status_code, 200, "重复项不应再触发 500")
        self.assertEqual(response.json()["code"], 0)

        rows = self.attempt.answers.filter(paper_question_id=self.pq.id)
        self.assertEqual(rows.count(), 1, "同一道题只应有一行")
        self.assertEqual(rows.first().selected_labels, "B", "后提交的应覆盖先提交的")

    def test_duplicate_items_do_not_lose_other_answers(self):
        """重复项只影响自己那道题，同批的其它题照常落库（不能整批丢）。"""
        other = PaperQuestion.objects.filter(paper=self.exam.paper).exclude(
            id=self.pq.id).first()
        response = self._post([
            {"paper_question_id": self.pq.id, "selected_labels": "A"},
            {"paper_question_id": other.id, "selected_labels": "C"},
            {"paper_question_id": self.pq.id, "selected_labels": "B"},
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.attempt.answers.count(), 2, "两道不同的题都应落库")
        self.assertEqual(
            self.attempt.answers.get(paper_question_id=self.pq.id).selected_labels, "B",
        )
        self.assertEqual(
            self.attempt.answers.get(paper_question_id=other.id).selected_labels, "C",
        )

    # ------------------------------------------------------------------
    # 2. 已存在时再提交（重复保存是常态，不该新增行）
    # ------------------------------------------------------------------
    def test_repeated_save_updates_in_place(self):
        self._post([{"paper_question_id": self.pq.id, "selected_labels": "A"}])
        self._post([{"paper_question_id": self.pq.id, "selected_labels": "D"}])

        rows = self.attempt.answers.filter(paper_question_id=self.pq.id)
        self.assertEqual(rows.count(), 1, "重复保存应更新而不是新增")
        self.assertEqual(rows.first().selected_labels, "D")

    # ------------------------------------------------------------------
    # 3. 跨请求并发兜底：撞唯一键退化为更新
    # ------------------------------------------------------------------
    def test_concurrent_insert_degrades_to_update(self):
        """两个请求同时判「这道题还没作答」并各自 insert，后者撞键时应退化为更新。

        直接构造「库里已有行、手里还拿着一个以为不存在的新对象」来模拟这个交错的
        时刻——不需要真起两个线程，撞键那条路径是一样的。
        """
        Answer.objects.create(
            attempt=self.attempt, paper_question=self.pq, order=1,
            selected_labels="A", is_answered=True,
        )
        ghost = Answer(
            attempt=self.attempt, paper_question_id=self.pq.id, order=1,
            selected_labels="B", answer_text="", is_answered=True,
        )
        # 不应抛 IntegrityError
        returned = ExamAttemptService._save_answer_row(ghost, {}, self.pq.id)

        rows = self.attempt.answers.filter(paper_question_id=self.pq.id)
        self.assertEqual(rows.count(), 1, "撞键后不能多出一行")
        self.assertEqual(rows.first().selected_labels, "B", "后写者胜")
        self.assertEqual(returned.pk, rows.first().pk, "应返回库里真实那一行")

    # ------------------------------------------------------------------
    # 4. 超宽限暂存：拒绝保存，但自动收卷必须已经落库
    # ------------------------------------------------------------------
    def test_overdue_save_submits_before_rejecting(self):
        """连宽限期都过了再暂存：返回 400，但自动收卷不能被一起回滚。

        修复前 ``resolve_expiry`` 与「拒绝保存」的回滚同处一个事务，收卷被撤销，
        作答卡在 ``in_progress`` 只能等心跳来兜——学生看到「时间已结束」，
        状态却没变，自相矛盾。
        """
        self.attempt.deadline_at = timezone.now() - timedelta(minutes=30)
        self.attempt.save(update_fields=["deadline_at"])

        response = self._post([{"paper_question_id": self.pq.id, "selected_labels": "A"}])
        self.assertEqual(response.status_code, 400, "超时后暂存应被拒绝")

        self.attempt.refresh_from_db()
        self.assertNotEqual(
            self.attempt.status, "in_progress",
            "自动收卷必须独立提交，不能被拒绝保存的异常回滚",
        )
        self.assertTrue(self.attempt.is_finished)

    # ------------------------------------------------------------------
    # 5. 单题入口 /save/ 与批量入口口径一致
    # ------------------------------------------------------------------
    def test_overdue_single_save_also_submits(self):
        """``/save/`` 也必须「先收卷再拒绝」。

        BUG-03：批量入口修好了，单题入口因为自身带 ``@transaction.atomic``，
        调用 ``save_answers`` 抛的 400 把外层事务连同收卷一起回滚了——超时后
        学生每切一题就发一次 ``/save/``，教师侧会一直显示「进行中」。
        """
        self.attempt.deadline_at = timezone.now() - timedelta(minutes=30)
        self.attempt.save(update_fields=["deadline_at"])

        response = self.post_json(
            f"/api/v1/attempts/{self.attempt.id}/save/",
            {"paper_question_id": self.pq.id, "selected_labels": "A"},
            user=self.student,
        )
        self.assertEqual(response.status_code, 400, "超时后单题暂存应被拒绝")

        self.attempt.refresh_from_db()
        self.assertNotEqual(
            self.attempt.status, "in_progress",
            "单题入口同样必须独立提交收卷，不能随拒绝一起回滚",
        )
        self.assertTrue(self.attempt.is_finished)

    def test_single_save_works_normally(self):
        """去掉事务注解不能把正常暂存也弄坏。"""
        response = self.post_json(
            f"/api/v1/attempts/{self.attempt.id}/save/",
            {"paper_question_id": self.pq.id, "selected_labels": "C"},
            user=self.student,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()["data"]
        self.assertEqual(body["saved"], 1)
        self.assertEqual(body["answer"]["selected_labels"], "C")
