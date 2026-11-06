"""每日练习成集（``RecommendationService.daily_practice``）的测试。

这是模块七的主链路，五条纪律逐条验：

1. **只读掌握度**：薄弱点直接来自 ``student_mastery()``，本模块不重算；
2. **样本不足不给推荐**：没有作答记录、或每个知识点都只做过一两次时，
   返回的是「去考试」的提示，而不是一份硬凑的清单；
3. **确定性**：同一天内反复刷新，清单与顺序都不变（所以不需要建表缓存）；
4. **同知识点限流**：一个知识点不会一次堆四五道题；
5. **降级优雅**：协同过滤算不出相似度时自动退化为纯内容推荐，并如实告知。
"""

from collections import Counter

from django.test import TestCase

from apps.common.constants import QuestionType
from apps.recommend.constants import DAILY_MAX_SIZE, MAX_PER_KNOWLEDGE_POINT
from apps.recommend.services import RecommendationService

from .base import RecommendBaseTest, SUBJECT

TODAY = "2026-09-30"


class InsufficientDataTest(RecommendBaseTest):
    """没有足够数据时，不硬推。"""

    def test_no_answers_falls_back_to_starter_practice(self):
        # 没有作答记录不再硬推「薄弱点」，而是给一组入门练习（starter）——
        # 否则新学生永远拿不到题。断言随之调整，语义由 normal 改为 starter。
        payload = RecommendationService.daily_practice(self.student_b, today=TODAY)
        self.assertFalse(payload["insufficient"])
        self.assertEqual(payload["mode"], "starter")
        self.assertTrue(payload["items"])

    def test_single_sample_knowledge_point_is_not_recommended(self):
        # 只做过 1 道某知识点的题且答错 → 归为「数据不足」，不是「薄弱」
        self.make_exam_with_single_question(student=self.student_b, correct=False)
        payload = RecommendationService.daily_practice(self.student_b, today=TODAY)
        self.assertTrue(payload["insufficient"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["summary"]["weak_point_count"], 0)
        self.assertIn("样本太少", payload["message"])

    def test_all_mastered_tells_user_no_weakness(self):
        # 六道题全答对 → 没有薄弱点，但也确实有作答记录
        self.make_weak(correct_types=set(self.questions.keys()))
        payload = RecommendationService.daily_practice(self.student, today=TODAY)
        self.assertFalse(payload["insufficient"])
        self.assertEqual(payload["items"], [])
        self.assertIn("掌握线", payload["message"])


class RecommendationSetTest(RecommendBaseTest):
    """成集逻辑。"""

    def test_items_come_from_weak_knowledge_points(self):
        self.make_weak()   # 全错 → 线性表 / 数据结构 薄弱
        payload = RecommendationService.daily_practice(self.student, today=TODAY)

        self.assertFalse(payload["insufficient"])
        self.assertTrue(payload["items"])
        weak_names = {row["name"] for row in payload["weak_points"]}
        for item in payload["items"]:
            self.assertIn(item["knowledge_point"], weak_names)
        # 所有题都挂在线性表（唯一有题的知识点）上
        self.assertEqual({item["knowledge_point"] for item in payload["items"]}, {"线性表"})

    def test_correctly_answered_questions_are_not_recommended(self):
        self.make_weak(correct_types={QuestionType.SINGLE, QuestionType.JUDGE})
        payload = RecommendationService.daily_practice(self.student, size=20, today=TODAY)
        ids = {item["question_id"] for item in payload["items"]}

        self.assertNotIn(self.questions[QuestionType.SINGLE].id, ids)
        self.assertNotIn(self.questions[QuestionType.JUDGE].id, ids)
        self.assertIn(self.questions[QuestionType.MULTIPLE].id, ids)
        # 答错的题会被标出来（便于页面上提示「这是你做错过的原题」）
        flagged = {item["question_id"] for item in payload["items"] if item["answered_before"]}
        self.assertIn(self.questions[QuestionType.MULTIPLE].id, flagged)

    def test_same_knowledge_point_is_capped(self):
        self.make_weak()
        kp2 = self.add_knowledge_point("栈与队列")
        # 注意：判分工具按**题型**取卷面数据，同一份卷子里不能只有一种题型，
        # 否则四道题会塌成一个键，只有一道题被判分。
        self.make_exam_with_questions([
            self.add_candidate(kp=kp2, difficulty=2, qtype=QuestionType.SINGLE),
            self.add_candidate(kp=kp2, difficulty=2, qtype=QuestionType.MULTIPLE),
            self.add_candidate(kp=kp2, difficulty=2, qtype=QuestionType.JUDGE),
            self.add_candidate(kp=kp2, difficulty=2, qtype=QuestionType.FILL),
        ])

        payload = RecommendationService.daily_practice(self.student, size=4, today=TODAY)
        counter = Counter(item["knowledge_point"] for item in payload["items"])
        self.assertEqual(len(counter), 2, msg="两个薄弱知识点都应出现在前排")
        for name, count in counter.items():
            self.assertLessEqual(count, MAX_PER_KNOWLEDGE_POINT,
                                 msg=f"{name} 堆了 {count} 道题")

    def test_secondary_questions_still_reachable_by_shuffle(self):
        self.make_weak()
        # 只有 6 道候选题，限流 2 → 前 2 道来自「首轮」，后 4 道来自溢出区
        payload = RecommendationService.daily_practice(self.student, size=6, today=TODAY)
        self.assertEqual(len(payload["items"]), 6)
        self.assertEqual(payload["summary"]["candidate_count"], 6)

    def test_deterministic_within_the_same_day(self):
        self.make_weak()
        first = RecommendationService.daily_practice(self.student, size=4, today=TODAY)
        second = RecommendationService.daily_practice(self.student, size=4, today=TODAY)
        self.assertEqual([item["question_id"] for item in first["items"]],
                         [item["question_id"] for item in second["items"]])
        # 顺序（含同分打散）也完全一致
        self.assertEqual([item["order"] for item in first["items"]], [1, 2, 3, 4])

    def test_offset_paging_has_no_overlap_and_sets_has_more(self):
        self.make_weak()
        first = RecommendationService.daily_practice(self.student, size=2, offset=0, today=TODAY)
        second = RecommendationService.daily_practice(self.student, size=2, offset=2, today=TODAY)
        self.assertTrue(first["has_more"])
        ids_first = {item["question_id"] for item in first["items"]}
        ids_second = {item["question_id"] for item in second["items"]}
        self.assertFalse(ids_first & ids_second, msg="「换一批」不该重复给同一道题")

    def test_has_more_false_when_pool_exhausted(self):
        self.make_weak()
        payload = RecommendationService.daily_practice(self.student, size=20, today=TODAY)
        self.assertFalse(payload["has_more"])
        self.assertEqual(payload["summary"]["returned"], payload["summary"]["candidate_count"])

    def test_offset_beyond_pool_gives_empty_but_keeps_hint(self):
        self.make_weak()
        payload = RecommendationService.daily_practice(self.student, size=2, offset=100, today=TODAY)
        self.assertEqual(payload["items"], [])
        self.assertIn("没有更多", payload["message"])

    def test_size_is_clamped(self):
        self.make_weak()
        big = RecommendationService.daily_practice(self.student, size=9999, today=TODAY)
        self.assertEqual(big["size"], DAILY_MAX_SIZE)
        small = RecommendationService.daily_practice(self.student, size=0, today=TODAY)
        self.assertEqual(small["size"], 1)


class SubjectFilterTest(RecommendBaseTest):
    """学科过滤。"""

    def test_subject_without_data_is_insufficient(self):
        self.make_weak()
        payload = RecommendationService.daily_practice(self.student, subject="语文", today=TODAY)
        self.assertTrue(payload["insufficient"])
        self.assertEqual(payload["scope_display"], "学科：语文")

    def test_subject_with_data_returns_items(self):
        self.make_weak()
        payload = RecommendationService.daily_practice(self.student, subject=SUBJECT, today=TODAY)
        self.assertFalse(payload["insufficient"])
        self.assertTrue(payload["items"])
        self.assertEqual(payload["subject"], SUBJECT)
        self.assertIn(SUBJECT, payload["subjects"])


class PayloadShapeTest(RecommendBaseTest):
    """返回结构要「前端可直接渲染」。"""

    def setUp(self):
        super().setUp()
        self.make_weak()
        self.payload = RecommendationService.daily_practice(self.student, size=3, today=TODAY)
        self.item = self.payload["items"][0]

    def test_top_level_fields(self):
        for key in ("student", "subject", "scope_key", "scope_display", "subjects", "date",
                    "size", "offset", "target_difficulty", "weak_points", "thresholds",
                    "items", "summary", "has_more", "insufficient", "message"):
            self.assertIn(key, self.payload)

    def test_item_fields(self):
        for key in ("order", "question_id", "type", "type_display", "stem", "difficulty",
                    "difficulty_display", "knowledge_point", "weak_accuracy", "weak_answered",
                    "answered_before", "match_score", "match_percent", "content_score",
                    "collaborative_score", "reason", "source", "options", "reference_answer",
                    "analysis"):
            self.assertIn(key, self.item)

    def test_match_percent_derives_from_match_score(self):
        self.assertAlmostEqual(
            self.item["match_percent"], round(self.item["match_score"] * 100, 1), places=1,
        )

    def test_choice_question_exposes_options_without_answer_marks(self):
        # 页面上要能列出选项，但选项里不能带「哪个是对的」这种字段。
        # 取一大页把候选池铺开再挑：排序键里带 question_id 的稳定哈希，
        # 仅取前 3 条时是否命中选择题取决于题目自增 id 的偏移（全量跑会变），
        # 这里不依赖那种巧合。
        payload = RecommendationService.daily_practice(self.student, size=20, today=TODAY)
        choice = next(item for item in payload["items"]
                      if item["type"] in (QuestionType.SINGLE, QuestionType.MULTIPLE,
                                          QuestionType.JUDGE))
        self.assertTrue(choice["options"])
        for option in choice["options"]:
            self.assertEqual(set(option.keys()), {"label", "content"})

    def test_local_reason_mentions_knowledge_point_and_accuracy(self):
        self.assertIn(self.item["knowledge_point"], self.item["reason"])
        self.assertIn("正确率", self.item["reason"])
        self.assertEqual(self.item["source"], "local")

    def test_weak_points_are_trimmed_for_display(self):
        for row in self.payload["weak_points"]:
            self.assertEqual(
                set(row.keys()),
                {"knowledge_point_id", "name", "full_path", "accuracy", "answered",
                 "mastery_level", "mastery_level_display"},
            )

    def test_summary_numbers(self):
        summary = self.payload["summary"]
        self.assertEqual(summary["returned"], len(self.payload["items"]))
        # weak_point_count 是总数，weak_points 列表按展示需要截断到 12 条
        self.assertGreaterEqual(summary["weak_point_count"], len(self.payload["weak_points"]))
        self.assertGreater(summary["candidate_count"], 0)
        self.assertIsNotNone(summary["avg_match"])
        self.assertGreaterEqual(summary["avg_match"], 0.0)
        self.assertLessEqual(summary["avg_match"], 1.0)


class CollaborativeIntegrationTest(RecommendBaseTest):
    """协同过滤在成集链路里的开 / 关。"""

    def test_single_student_degrades_to_content_only(self):
        self.make_weak()   # 只有甲作答 → 每题只有一名作答者 → 算不出相似度
        payload = RecommendationService.daily_practice(self.student, size=3, today=TODAY)
        self.assertFalse(payload["summary"]["cf_available"])
        self.assertIn("协同过滤", payload["message"])
        self.assertTrue(all(item["collaborative_score"] == 0.0 for item in payload["items"]))

    def test_two_students_enable_collaborative_signal(self):
        self.make_weak()
        self.judged_attempt(
            self.exam, student=self.student_b,
            correct_types={QuestionType.SINGLE, QuestionType.JUDGE},
        )
        payload = RecommendationService.daily_practice(self.student, size=6, today=TODAY)
        self.assertTrue(payload["summary"]["cf_available"])

        by_id = {item["question_id"]: item for item in payload["items"]}
        single = by_id.get(self.questions[QuestionType.SINGLE].id)
        self.assertIsNotNone(single)
        # 学生乙答对的题，与「甲做错过的题」作答分布一致 → 协同分被拉满
        self.assertGreater(single["collaborative_score"], 0.0)
        self.assertGreater(single["match_score"], single["content_score"])
        # 协同证据会写进推荐理由
        self.assertIn("同类题目", single["reason"])


class DateRotationTest(TestCase):
    """同分题目按「学生 + 日期 + 题目」打散：跨天会换，当天不变。"""

    def test_daily_key_is_stable_and_date_sensitive(self):
        from apps.recommend.services import _daily_key

        self.assertEqual(_daily_key(3, TODAY, 100), _daily_key(3, TODAY, 100))
        self.assertNotEqual(_daily_key(3, TODAY, 100), _daily_key(3, "2026-10-01", 100))
        self.assertNotEqual(_daily_key(3, TODAY, 100), _daily_key(4, TODAY, 100))
