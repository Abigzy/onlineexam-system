"""协同过滤（``CollaborativeFilteringService``）的单元测试。

这个服务里最容易出错的两件事，都在这里锁住：

1. **共同作答者太少时相似度必须是 0。** 「只有一两个人都做对了」不是规律，
   是巧合；不设门槛的话，推荐会变成「和某个人口味相同」的胡乱联想。
2. **没有弱项时要老实返回空。** 一个从没做错过题的学生没有协同证据，
   这时候应当退化为纯内容推荐，而不是硬算出一个 0.00 的「相似」。
"""

from unittest.mock import patch

from django.test import TestCase

from apps.common.constants import QuestionType
from apps.recommend.constants import CF_MIN_COMMON_STUDENTS
from apps.recommend.services import CollaborativeFilteringService

from .base import RecommendBaseTest


class CosineTest(TestCase):
    """余弦相似度——纯函数。"""

    def test_identical_vectors_are_fully_similar(self):
        vector = {1: 1.0, 2: 1.0, 3: 0.0}
        self.assertAlmostEqual(
            CollaborativeFilteringService.cosine(vector, dict(vector)), 1.0, places=9,
        )

    def test_too_few_common_students_yields_zero(self):
        # 只有一个共同作答者 → 判 0（门槛 CF_MIN_COMMON_STUDENTS = 2）
        self.assertGreaterEqual(CF_MIN_COMMON_STUDENTS, 2)
        self.assertEqual(CollaborativeFilteringService.cosine({1: 1.0}, {1: 1.0}), 0.0)

    def test_opposite_answers_are_not_similar(self):
        left = {1: 1.0, 2: 1.0}
        right = {1: 0.0, 2: 0.0}
        self.assertEqual(CollaborativeFilteringService.cosine(left, right), 0.0)

    def test_zero_norm_yields_zero(self):
        # 一道谁都没做对的题，向量范数为 0，和任何题都不相似
        self.assertEqual(CollaborativeFilteringService.cosine({1: 0.0, 2: 0.0}, {1: 1.0, 2: 1.0}), 0.0)

    def test_missing_students_count_as_zero(self):
        # 只在共同作答者上做点积，但范数按各自全部作答者算（缺的当作 0）
        left = {1: 1.0, 2: 1.0, 3: 1.0}
        right = {1: 1.0, 2: 1.0}
        expected = 2 / (3 ** 0.5 * 2 ** 0.5)
        self.assertAlmostEqual(CollaborativeFilteringService.cosine(left, right), expected, places=6)

    def test_empty_vector_yields_zero(self):
        self.assertEqual(CollaborativeFilteringService.cosine({}, {1: 1.0, 2: 1.0}), 0.0)


class WrongItemsTest(TestCase):
    """从学生行里挑出「做错的题」。"""

    def test_wrong_items_only_counts_zero_values(self):
        per_student = {7: {11: 0.0, 12: 1.0, 13: 0.0}}
        self.assertEqual(sorted(CollaborativeFilteringService.wrong_items(7, per_student)), [11, 13])

    def test_unknown_student_has_no_wrong_items(self):
        self.assertEqual(CollaborativeFilteringService.wrong_items(99, {}), [])


class ScoreTest(TestCase):
    """候选题目打分：与「做错过的题」越像分越高，且归一化到 0~1。"""

    def setUp(self):
        # 三位学生的作答（1 = 达标，0 = 未达标）
        #   qW 是学生 1 做错的题
        self.per_question = {
            100: {1: 0.0, 2: 1.0, 3: 1.0},   # 与学生 1 做错的 qW 完全一致的作答分布
            101: {1: 1.0, 2: 1.0, 3: 0.0},   # 一半相似
            102: {1: 1.0, 2: 0.0, 3: 0.0},   # 范式不同
        }
        self.per_student = {
            1: {100: 0.0, 101: 1.0, 102: 1.0},
            2: {100: 1.0, 101: 1.0, 102: 0.0},
            3: {100: 1.0, 101: 0.0, 102: 0.0},
        }

    def test_no_wrong_items_means_no_collaborative_signal(self):
        per_student = {5: {100: 1.0, 101: 1.0}}
        self.assertEqual(
            CollaborativeFilteringService.score(5, [100, 101], per_student, self.per_question), {},
        )

    def test_no_candidates_means_no_signal(self):
        self.assertEqual(
            CollaborativeFilteringService.score(1, [], self.per_student, self.per_question), {},
        )

    def test_scores_are_normalized_and_rank_similar_items_higher(self):
        scores = CollaborativeFilteringService.score(
            1, [100, 101, 102], self.per_student, self.per_question,
        )
        self.assertIn(100, scores)
        # 满分归一化：与学生 1 做错过的 qW 分布一致的 q100 排第一
        self.assertAlmostEqual(scores[100], 1.0, places=4)
        self.assertGreater(scores[100], scores[101])
        self.assertGreaterEqual(scores[101], 0.0)
        for value in scores.values():
            self.assertLessEqual(value, 1.0)
            self.assertGreaterEqual(value, 0.0)

    def test_zero_similarity_everywhere_returns_empty(self):
        # 候选表里没有任何与学生 1 的错题共享两个作答者的题 → 空（无证据）
        per_question = {200: {8: 1.0, 9: 1.0}}
        scores = CollaborativeFilteringService.score(
            1, [200], self.per_student, per_question,
        )
        self.assertEqual(scores, {})


class MatrixTest(RecommendBaseTest):
    """从真实判分结果建矩阵。"""

    def test_records_use_graded_answers_only(self):
        self.make_weak()
        records = CollaborativeFilteringService.records()
        self.assertEqual(len(records), 6)          # 六道题各一条
        self.assertTrue(all(question_id for _, question_id, _ in records))

    def test_unsubmitted_attempt_does_not_enter_matrix(self):
        from apps.exam.services import ExamAttemptService

        attempt = self.start(self.exam, student=self.student)
        ExamAttemptService.save_answers(attempt, self.answer_payload(attempt))
        # 只保存不交卷 → 状态仍是 in_progress，不该进矩阵
        self.assertEqual(CollaborativeFilteringService.records(), [])

    def test_build_matrix_shapes(self):
        self.make_weak()
        per_student, per_question = CollaborativeFilteringService.build_matrix()
        self.assertIn(self.student.id, per_student)
        self.assertEqual(len(per_student[self.student.id]), 6)
        self.assertEqual(len(per_question), 6)
        # 全错 → 每道题的「做错」标记都是 0.0
        self.assertTrue(all(value == 0.0 for value in per_student[self.student.id].values()))

    def test_subjective_questions_use_score_ratio(self):
        self.make_weak(correct_types={QuestionType.SHORT})
        per_student, _ = CollaborativeFilteringService.build_matrix()
        row = per_student[self.student.id]
        short_id = self.questions[QuestionType.SHORT].id
        essay_id = self.questions[QuestionType.ESSAY].id
        self.assertEqual(row[short_id], 1.0)     # 简答拿满分 → 达标
        self.assertEqual(row[essay_id], 0.0)     # 论述 0 分 → 未达标

    def test_item_cap_keeps_most_answered_questions(self):
        self.make_weak()
        with patch("apps.recommend.services.CF_MAX_ITEMS", 2):
            _, per_question = CollaborativeFilteringService.build_matrix()
        self.assertEqual(len(per_question), 2)

    def test_second_student_makes_similarity_computable(self):
        # 学生甲全错、学生乙答对单选与判断 → 每道题都有两名作答者
        self.make_weak()
        self.judged_attempt(
            self.exam, student=self.student_b,
            correct_types={QuestionType.SINGLE, QuestionType.JUDGE},
        )
        per_student, per_question = CollaborativeFilteringService.build_matrix()
        self.assertEqual(len(per_student), 2)
        self.assertTrue(all(len(vector) >= 2 for vector in per_question.values()))

        scores = CollaborativeFilteringService.score(
            self.student.id, list(per_question), per_student, per_question,
        )
        # 学生甲全错；乙答对的那两道题的作答分布与「甲做错过的题」更接近
        single_id = self.questions[QuestionType.SINGLE].id
        self.assertIn(single_id, scores)
        self.assertAlmostEqual(scores[single_id], 1.0, places=4)
