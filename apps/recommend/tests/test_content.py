"""内容推荐（``ContentBasedService``）的单元测试。

这里测的是「一道题凭什么排在前面」——三个可验证的结论：

1. 目标难度由整体正确率推出来，且夹在 1..4；
2. 薄弱度 = (1 - 正确率) × 证据充分度，**样本越多结论越硬**；
3. 候选池只收「薄弱知识点（含其祖先 / 后代）上的、且没做对过的」题，
   并按内容分降序排列。
"""

from django.test import TestCase

from apps.analytics.services import KnowledgeMasteryService
from apps.common.constants import QuestionType
from apps.recommend.constants import DIFFICULTY_WEIGHT, EVIDENCE_FULL_SAMPLE, KP_WEIGHT
from apps.recommend.services import CollaborativeFilteringService, ContentBasedService

from .base import RecommendBaseTest, SUBJECT


class TargetDifficultyTest(TestCase):
    """目标难度换算——纯函数，不需要数据库。"""

    def test_none_accuracy_falls_back_to_medium(self):
        self.assertEqual(ContentBasedService.target_difficulty(None), 2)

    def test_boundaries_are_clamped(self):
        self.assertEqual(ContentBasedService.target_difficulty(0.0), 1)
        self.assertEqual(ContentBasedService.target_difficulty(-0.5), 1)
        self.assertEqual(ContentBasedService.target_difficulty(1.0), 4)
        self.assertEqual(ContentBasedService.target_difficulty(1.8), 4)

    def test_middle_accuracy_maps_upward(self):
        # 正确率 20% → 1 + round(0.6) = 2；正确率 90% → 1 + round(2.7) = 4
        self.assertEqual(ContentBasedService.target_difficulty(0.2), 2)
        self.assertEqual(ContentBasedService.target_difficulty(0.9), 4)

    def test_difficulty_fit_is_one_at_target_zero_at_far_end(self):
        self.assertEqual(ContentBasedService.difficulty_fit(2, 2), 1.0)
        self.assertAlmostEqual(ContentBasedService.difficulty_fit(1, 2), 2 / 3, places=4)
        self.assertEqual(ContentBasedService.difficulty_fit(5, 2), 0.0)


class EvidenceTest(TestCase):
    """证据充分度与薄弱度。"""

    def test_evidence_grows_with_samples_and_saturates(self):
        self.assertEqual(ContentBasedService.evidence(0), 0.0)
        self.assertEqual(ContentBasedService.evidence(EVIDENCE_FULL_SAMPLE // 2), 0.5)
        self.assertEqual(ContentBasedService.evidence(EVIDENCE_FULL_SAMPLE), 1.0)
        self.assertEqual(ContentBasedService.evidence(EVIDENCE_FULL_SAMPLE * 3), 1.0)

    def test_kp_weakness_discounts_thin_evidence(self):
        thin = {"accuracy": 0.0, "answered": EVIDENCE_FULL_SAMPLE // 2}
        solid = {"accuracy": 0.0, "answered": EVIDENCE_FULL_SAMPLE}
        self.assertLess(ContentBasedService.kp_weakness(thin),
                        ContentBasedService.kp_weakness(solid))
        self.assertEqual(ContentBasedService.kp_weakness(solid), 1.0)

    def test_kp_weakness_zero_when_fully_mastered(self):
        row = {"accuracy": 1.0, "answered": EVIDENCE_FULL_SAMPLE}
        self.assertEqual(ContentBasedService.kp_weakness(row), 0.0)


class CandidatePoolTest(RecommendBaseTest):
    """候选池的召回范围与排序。"""

    def test_descendants_include_self_and_children(self):
        kp_map = KnowledgeMasteryService._kp_map()
        ids = ContentBasedService.descendants_of_many([self.parent_kp.id], kp_map)
        self.assertIn(self.parent_kp.id, ids)
        self.assertIn(self.child_kp.id, ids)

    def test_weak_map_comes_from_mastery_not_recomputed(self):
        self.make_weak()   # 全错 → 线性表与数据结构都薄弱
        mastery = KnowledgeMasteryService.student_mastery(self.student)
        weak_map = ContentBasedService.weak_map(mastery)
        # weak_points 已经过滤掉样本不足的，这里不该出现 unknown 档
        for row in weak_map.values():
            self.assertNotEqual(row["mastery_level"], "unknown")
        self.assertIn(self.child_kp.id, weak_map)

    def test_candidates_exclude_correctly_answered_questions(self):
        # 单选 / 判断答对，其余答错 → 做对的两道题不该再被推荐
        self.make_weak(correct_types={QuestionType.SINGLE, QuestionType.JUDGE})
        mastery = KnowledgeMasteryService.student_mastery(self.student)
        weak_map = ContentBasedService.weak_map(mastery)
        kp_map = KnowledgeMasteryService._kp_map()

        # 「已做对」的判定与成集时一致：取自协同过滤那张学生×题目表
        per_student, _ = CollaborativeFilteringService.build_matrix()
        correct_ids = {qid for qid, value in (per_student.get(self.student.id) or {}).items()
                       if value == 1.0}
        self.assertIn(self.questions[QuestionType.SINGLE].id, correct_ids)

        candidates = ContentBasedService.score_candidates(
            SUBJECT, weak_map, kp_map, exclude_ids=correct_ids,
            accuracy=mastery["summary"]["accuracy"],
        )
        ids = {row["question_id"] for row in candidates}
        self.assertNotIn(self.questions[QuestionType.SINGLE].id, ids)
        self.assertNotIn(self.questions[QuestionType.JUDGE].id, ids)
        # 答错的那几道仍在池子里（重练也是练习的一种）
        self.assertIn(self.questions[QuestionType.MULTIPLE].id, ids)

    def test_candidates_ignore_questions_outside_weak_knowledge_points(self):
        self.make_weak()
        other_kp = self.add_knowledge_point("计算机网络")
        other_question = self.add_candidate(kp=other_kp, difficulty=2)

        mastery = KnowledgeMasteryService.student_mastery(self.student)
        candidates = ContentBasedService.score_candidates(
            SUBJECT, ContentBasedService.weak_map(mastery),
            KnowledgeMasteryService._kp_map(),
            accuracy=mastery["summary"]["accuracy"],
        )
        self.assertNotIn(other_question.id, {row["question_id"] for row in candidates})

    def test_candidates_rank_difficulty_close_to_target_higher(self):
        self.make_weak()

        easy = self.add_candidate(difficulty=1)
        medium = self.add_candidate(difficulty=2)
        expert = self.add_candidate(difficulty=4)

        mastery = KnowledgeMasteryService.student_mastery(self.student)
        # 正确率 0 → 目标难度 1；难度 1 应当比难度 4 排得更前
        candidates = ContentBasedService.score_candidates(
            SUBJECT, ContentBasedService.weak_map(mastery),
            KnowledgeMasteryService._kp_map(), accuracy=0.0,
        )
        score = {row["question_id"]: row["content_score"] for row in candidates}

        self.assertGreater(score[easy.id], score[expert.id])
        self.assertGreater(score[easy.id], score[medium.id])
        # 内容分 = 薄弱度 × 0.65 + 难度适配度 × 0.35，封顶 1.0
        for row in candidates:
            self.assertLessEqual(row["content_score"], 1.0)
            self.assertGreaterEqual(row["content_score"], 0.0)

        # 薄弱度与难度适配度确实是按公式合成的
        top = candidates[0]
        expected = round(
            KP_WEIGHT * ContentBasedService.kp_weakness(
                {"accuracy": top["weak_accuracy"], "answered": top["weak_answered"]},
            ) + DIFFICULTY_WEIGHT * ContentBasedService.difficulty_fit(
                top["difficulty"], 1,
            ), 4,
        )
        self.assertAlmostEqual(top["content_score"], expected, places=4)

    def test_candidates_sorted_by_content_score_desc(self):
        self.make_weak()
        for difficulty in (1, 2, 3, 4):
            self.add_candidate(difficulty=difficulty)

        mastery = KnowledgeMasteryService.student_mastery(self.student)
        candidates = ContentBasedService.score_candidates(
            SUBJECT, ContentBasedService.weak_map(mastery),
            KnowledgeMasteryService._kp_map(), accuracy=mastery["summary"]["accuracy"],
        )
        scores = [row["content_score"] for row in candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_empty_when_no_weak_points(self):
        # 没有任何作答记录 → 没有薄弱点 → 候选池为空
        self.assertEqual(ContentBasedService.score_candidates(
            SUBJECT, {}, KnowledgeMasteryService._kp_map(),
        ), [])

    def test_match_question_reports_weakest_knowledge_point(self):
        self.make_weak()
        question = self.add_candidate(difficulty=2)
        mastery = KnowledgeMasteryService.student_mastery(self.student)
        matched = ContentBasedService.match_question(
            question, ContentBasedService.weak_map(mastery),
            KnowledgeMasteryService._kp_map(),
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched["name"], self.child_kp.name)
