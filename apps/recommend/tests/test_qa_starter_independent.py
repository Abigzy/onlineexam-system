"""QA 独立验证（不依赖实现者用例）：starter 的中间态、均摊、分页与排除已做对题。

这部分场景需要精确构造「样本太少 / 已达掌握线 / 已做对题」的数据，
真实库里没有，所以在测试库里造；与 ``test_starter_practice.py`` 无重叠。

运行：
    DB_NAME=online_exam_qa2 python manage.py test \
        apps.recommend.tests.test_qa_starter_independent --noinput
"""

from collections import Counter

from apps.common.constants import QuestionType
from apps.recommend.constants import MAX_PER_KNOWLEDGE_POINT
from apps.recommend.services import RecommendationService

from .base import RecommendBaseTest, SUBJECT

TODAY = "2026-10-21"
QA_SUBJECT = "QA-操作系统"
EMPTY_SUBJECT = "QA-空学科"


class MidStateStillNormalTest(RecommendBaseTest):
    """A5：两种「有作答但不足以/不需要推荐」的中间态都不能被 starter 吞掉。"""

    # 用的是默认学科 SUBJECT，默认学科班已经装配好（见 RecommendBaseTest）

    def test_sample_too_small_stays_normal_and_insufficient(self):
        # 该学科 1 条已判分作答（< MIN_SAMPLES_FOR_WEAKNESS）→ 样本太少
        self.make_exam_with_single_question(student=self.student_b, correct=False)
        payload = RecommendationService.daily_practice(
            self.student_b, subject=SUBJECT, today=TODAY,
        )
        self.assertEqual(payload["mode"], "normal")
        self.assertFalse(payload["summary"]["starter"])
        self.assertTrue(payload["insufficient"])
        self.assertEqual(payload["items"], [])
        self.assertIn("样本太少", payload["message"])
        self.assertNotIn("入门练习", payload["message"])

    def test_all_knowledge_points_mastered_stays_normal(self):
        # 同一知识点 2 题全对 → 达到掌握线，weak_map 为空但 answered=2
        # 判分工具按题型取卷面作答，同一份卷子必须用两种题型，否则只判一道
        first = self.add_candidate(difficulty=1, qtype=QuestionType.SINGLE)
        second = self.add_candidate(difficulty=1, qtype=QuestionType.JUDGE)
        self.make_exam_with_questions(
            [first, second], student=self.student_b,
            correct_types={QuestionType.SINGLE, QuestionType.JUDGE},
        )
        payload = RecommendationService.daily_practice(
            self.student_b, subject=SUBJECT, today=TODAY,
        )
        self.assertEqual(payload["mode"], "normal")
        self.assertFalse(payload["summary"]["starter"])
        self.assertEqual(payload["summary"]["weak_point_count"], 0)
        self.assertEqual(payload["items"], [])
        self.assertIn("掌握线", payload["message"])
        self.assertNotIn("入门练习", payload["message"])


class StarterSelectionConstraintTest(RecommendBaseTest):
    """C9 / C10 / C11：选题约束要拿实际结果统计，不看注释。"""

    def setUp(self):
        super().setUp()
        # 该学科必须「已加入」，否则会被学科范围校验拦下，测不到选题约束
        self.join_subject(QA_SUBJECT, student=self.student_b)

    def _make_subject_questions(self, kp, count, difficulty=2):
        return [
            self.add_candidate(kp=kp, subject=QA_SUBJECT, difficulty=difficulty)
            for _ in range(count)
        ]

    def test_starter_excludes_questions_already_answered_correctly(self):
        # 学科 QA_SUBJECT 在掌握度口径下 answered=0（判分记录挂在 SUBJECT 的卷子上），
        # 因此仍走 starter；而这道题已经是「做对」的状态，必须被排除。
        kp1 = self.add_knowledge_point("QA-KP-1")
        kp2 = self.add_knowledge_point("QA-KP-2")
        answered = self.add_candidate(kp=kp1, subject=QA_SUBJECT, difficulty=1)
        others = self._make_subject_questions(kp1, 1) + self._make_subject_questions(kp2, 2)
        self.make_exam_with_questions(
            [answered], student=self.student_b, correct_types={answered.type},
        )

        payload = RecommendationService.daily_practice(
            self.student_b, subject=QA_SUBJECT, size=10, today=TODAY,
        )
        self.assertEqual(payload["mode"], "starter")
        got = {item["question_id"] for item in payload["items"]}
        self.assertNotIn(answered.id, got, msg="已做对的题不能出现在入门练习里")
        self.assertTrue({q.id for q in others} <= got)
        self.assertFalse(any(item["answered_before"] for item in payload["items"]))

    def test_easy_questions_dominate_even_when_hard_questions_come_first_in_db(self):
        kps = [self.add_knowledge_point("QA-D-%d" % i) for i in range(8)]
        for index, difficulty in enumerate([4, 4, 4, 4, 3, 1, 1, 2]):
            self.add_candidate(kp=kps[index], subject=QA_SUBJECT, difficulty=difficulty)

        payload = RecommendationService.daily_practice(
            self.student_b, subject=QA_SUBJECT, size=4, today=TODAY,
        )
        self.assertEqual(len(payload["items"]), 4)
        easy = [item for item in payload["items"] if item["difficulty"] <= 2]
        self.assertGreater(len(easy), len(payload["items"]) / 2)
        # 实现只承诺「难度 1~2 优先」，不承诺 1 与 2 交替出现：
        # 库里难度 1 的题只有 2 道，打散后前两道同为难度 1 是合法的。
        self.assertTrue(all(item["difficulty"] <= 2 for item in payload["items"][:2]))

    def test_one_knowledge_point_cannot_monopolize(self):
        hot = self.add_knowledge_point("QA-HOT")
        warm = self.add_knowledge_point("QA-WARM")
        cold = self.add_knowledge_point("QA-COLD")
        self._make_subject_questions(hot, 6)
        self._make_subject_questions(warm, 3)
        self._make_subject_questions(cold, 1)

        payload = RecommendationService.daily_practice(
            self.student_b, subject=QA_SUBJECT, size=20, today=TODAY,
        )
        counter = Counter(item["knowledge_point"] for item in payload["items"])
        self.assertLessEqual(max(counter.values()), MAX_PER_KNOWLEDGE_POINT, counter)
        # 六个题都挂在同一个知识点时，宁少给也不给满（不留溢出区）
        self.assertEqual(counter["QA-HOT"], MAX_PER_KNOWLEDGE_POINT)
        self.assertEqual(len(payload["items"]), 5)


class StarterPaginationTest(RecommendBaseTest):
    """B8 的构造版：10 道题、size=3 翻遍每一页。"""

    def setUp(self):
        super().setUp()
        self.join_subject(QA_SUBJECT, student=self.student_b)
        for index in range(5):
            kp = self.add_knowledge_point("QA-P-%d" % index)
            for _ in range(2):
                self.add_candidate(kp=kp, subject=QA_SUBJECT, difficulty=2)

    def test_pages_do_not_overlap_and_has_more_is_correct(self):
        seen = []
        for offset in (0, 3, 6, 9):
            payload = RecommendationService.daily_practice(
                self.student_b, subject=QA_SUBJECT, size=3, offset=offset, today=TODAY,
            )
            seen.append([item["question_id"] for item in payload["items"]])
            self.assertEqual(
                payload["has_more"], offset + 3 < 10, msg="offset=%d" % offset,
            )
        self.assertEqual([len(page) for page in seen], [3, 3, 3, 1])
        flat = [qid for page in seen for qid in page]
        self.assertEqual(len(flat), len(set(flat)), msg="翻页出现重复题")
        self.assertEqual(len(flat), 10)

    def test_same_day_is_stable_and_other_day_changes(self):
        first = RecommendationService.daily_practice(
            self.student_b, subject=QA_SUBJECT, size=6, today=TODAY,
        )
        second = RecommendationService.daily_practice(
            self.student_b, subject=QA_SUBJECT, size=6, today=TODAY,
        )
        other = RecommendationService.daily_practice(
            self.student_b, subject=QA_SUBJECT, size=6, today="2026-12-31",
        )
        self.assertEqual(
            [i["question_id"] for i in first["items"]],
            [i["question_id"] for i in second["items"]],
        )
        self.assertNotEqual(
            [i["question_id"] for i in first["items"]],
            [i["question_id"] for i in other["items"]],
        )


class SubjectWithoutActiveQuestionTest(RecommendBaseTest):
    """A4 的构造版：题目存在但全部停用，也要说「没有启用中的题目」。"""

    def test_all_questions_inactive(self):
        question = self.add_candidate(subject=EMPTY_SUBJECT, difficulty=2)
        question.is_active = False
        question.save(update_fields=["is_active"])
        # 先加入这个学科：否则会被范围校验拦下，测不到「没题」这条路
        self.join_subject(EMPTY_SUBJECT, student=self.student_b)

        payload = RecommendationService.daily_practice(
            self.student_b, subject=EMPTY_SUBJECT, today=TODAY,
        )
        self.assertTrue(payload["insufficient"])
        self.assertEqual(payload["mode"], "starter")
        self.assertEqual(payload["items"], [])
        self.assertIn("没有启用中的题目", payload["message"])
        self.assertIn(EMPTY_SUBJECT, payload["message"])
