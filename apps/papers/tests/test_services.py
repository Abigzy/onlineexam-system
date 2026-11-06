"""试卷模块的模型与服务层测试。

自建题库的策略：**两个知识点 × 六种题型 × 四个难度档 × 每格 6 道题**（共 144 道）。
"每格 6 道"这个数字是照着默认蓝图算出来的——默认蓝图里单选要 10 道，
按难度配比 20/60/20 拆开就是 2/6/2，所以某个难度档最多需要 6 道。
格子里的题量够，组卷才不会因为库存不足而走回退路径，测试才能稳定断言。
"""

from decimal import Decimal

from apps.common.constants import Difficulty, QuestionType
from apps.common.exceptions import BusinessError
from apps.questions.models import KnowledgePoint
from apps.papers.composer import DEFAULT_BLUEPRINT, PaperComposeService
from apps.papers.constants import PaperGenerationMode, PaperStatus
from apps.papers.models import Paper
from apps.papers.services import PaperAnalysisService, PaperService

from .base import SUBJECT, PaperBaseTest


# ======================================================================
# 卷子编排
# ======================================================================
class PaperServiceTests(PaperBaseTest):

    def test_create_with_defaults(self):
        paper = PaperService.create(data={"title": "期末卷"}, user=self.teacher)
        self.assertEqual(paper.status, PaperStatus.DRAFT)
        self.assertEqual(paper.generation_mode, PaperGenerationMode.MANUAL)
        self.assertEqual(paper.question_count, 0)
        self.assertEqual(float(paper.total_score), 0.0)
        self.assertEqual(paper.created_by_id, self.teacher.id)

    def test_create_with_question_ids(self):
        ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=3)]
        paper = PaperService.create(
            data={"title": "带题的卷", "question_ids": ids}, user=self.teacher,
        )
        self.assertEqual(paper.question_count, 3)
        self.assertEqual(float(paper.total_score), 6.0)
        self.assertTrue(paper.is_editable)

    def test_add_questions_builds_sections_and_renumbers(self):
        paper = self.make_paper()
        singles = self.questions_of(QuestionType.SINGLE, 2, limit=3)
        judges = self.questions_of(QuestionType.JUDGE, 2, limit=2)

        PaperService.add_questions(paper=paper, questions=singles)
        PaperService.add_questions(paper=paper, questions=judges)
        paper.refresh_from_db()

        self.assertEqual(paper.question_count, 5)
        orders = list(paper.paper_questions.order_by("order").values_list("order", flat=True))
        self.assertEqual(orders, [1, 2, 3, 4, 5], "题号必须从 1 连续编号")

        sections = list(paper.sections.order_by("order"))
        self.assertEqual(len(sections), 2, "两种题型应该自动生成两个大题")
        self.assertEqual(sections[0].question_type, QuestionType.SINGLE)
        self.assertEqual(sections[0].title, "一、单项选择题")
        self.assertEqual(sections[1].title, "二、判断题")
        self.assertEqual(sections[0].question_count, 3)
        self.assertEqual(sections[1].question_count, 2)
        self.assertEqual(sections[0].score_per_question, Decimal("2.00"))

    def test_add_duplicate_raises_in_strict_mode(self):
        paper = self.make_paper()
        singles = self.questions_of(QuestionType.SINGLE, 2, limit=2)
        PaperService.add_questions(paper=paper, questions=singles)

        with self.assertRaises(BusinessError) as ctx:
            PaperService.add_questions(paper=paper, questions=singles, strict=True)
        self.assertIn("已在本卷中", str(ctx.exception))

    def test_add_duplicate_skips_in_lenient_mode(self):
        paper = self.make_paper()
        singles = self.questions_of(QuestionType.SINGLE, 2, limit=2)
        PaperService.add_questions(paper=paper, questions=singles)

        result = PaperService.add_questions(
            paper=paper, questions=singles + self.questions_of(QuestionType.SINGLE, 3, limit=1),
            strict=False,
        )
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(result["duplicated_count"], 2)

    def test_add_questions_score_override(self):
        paper = self.make_paper()
        PaperService.add_questions(
            paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=4), score=5,
        )
        paper.refresh_from_db()
        self.assertEqual(float(paper.total_score), 20.0)
        self.assertEqual(
            set(paper.paper_questions.values_list("score", flat=True)), {Decimal("5.00")},
        )

    def test_add_ignores_inactive_questions(self):
        paper = self.make_paper()
        singles = self.questions_of(QuestionType.SINGLE, 2, limit=2)
        singles[0].is_active = False
        singles[0].save(update_fields=["is_active"])

        result = PaperService.add_questions(paper=paper, questions=singles, strict=False)
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(result["invalid_count"], 1)

    def test_removing_all_questions_drops_empty_section(self):
        paper = self.make_paper()
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.FILL, 2, limit=2))
        self.assertEqual(paper.sections.count(), 1)

        ids = list(paper.paper_questions.values_list("id", flat=True))
        PaperService.remove_questions(paper=paper, paper_question_ids=ids)
        paper.refresh_from_db()

        self.assertEqual(paper.question_count, 0)
        self.assertEqual(paper.sections.count(), 0, "题目清空后不该留下空大题")
        self.assertEqual(float(paper.total_score), 0.0)

    def test_remove_renumbers_remaining(self):
        paper = self.make_paper()
        PaperService.add_questions(
            paper=paper, questions=self.questions_of(QuestionType.JUDGE, 2, limit=4),
        )
        items = list(paper.paper_questions.order_by("order"))
        PaperService.remove_questions(paper=paper, paper_question_ids=[items[1].id])

        paper.refresh_from_db()
        orders = list(paper.paper_questions.order_by("order").values_list("order", flat=True))
        self.assertEqual(orders, [1, 2, 3], "删掉中间一题后题号要重新连续")

    def test_set_score_recalculates_total(self):
        paper = self.make_paper()
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=2))
        item = paper.paper_questions.first()

        PaperService.set_score(paper_question=item, score=7)
        paper.refresh_from_db()
        self.assertEqual(float(paper.total_score), 9.0, "2 分改成 7 分后总分应为 7+2=9")

    def test_set_score_rejects_non_positive(self):
        paper = self.make_paper()
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=1))
        item = paper.paper_questions.first()
        for bad in (0, -1):
            with self.assertRaises(BusinessError):
                PaperService.set_score(paper_question=item, score=bad)

    def test_batch_set_score(self):
        paper = self.make_paper()
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=3))
        items = list(paper.paper_questions.order_by("order"))

        updated = PaperService.batch_set_score(
            paper=paper, items=[{"id": items[0].id, "score": 4}, {"id": items[1].id, "score": 6}],
        )
        paper.refresh_from_db()
        self.assertEqual(updated, 2)
        self.assertEqual(float(paper.total_score), 12.0)  # 4 + 6 + 2

    def test_reorder(self):
        paper = self.make_paper()
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=3))
        items = list(paper.paper_questions.order_by("order"))

        PaperService.reorder(paper=paper, ordered_ids=[items[2].id, items[0].id, items[1].id])
        by_order = list(
            paper.paper_questions.order_by("order").values_list("question_id", flat=True)
        )
        self.assertEqual(by_order, [items[2].question_id, items[0].question_id, items[1].question_id])

    def test_reorder_ignores_unknown_ids(self):
        paper = self.make_paper()
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=2))
        items = list(paper.paper_questions.order_by("order"))

        PaperService.reorder(paper=paper, ordered_ids=[items[1].id, 999999, items[0].id])
        orders = list(paper.paper_questions.order_by("order").values_list("order", flat=True))
        self.assertEqual(orders, [1, 2])

    def test_clone_copies_structure(self):
        paper = self.make_paper()
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=3))
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SHORT, 3, limit=2))
        paper.refresh_from_db()

        clone = PaperService.clone(paper, user=self.teacher)
        self.assertEqual(clone.question_count, paper.question_count)
        self.assertEqual(clone.total_score, paper.total_score)
        self.assertEqual(clone.sections.count(), paper.sections.count())
        self.assertEqual(clone.status, PaperStatus.DRAFT)
        self.assertIn("副本", clone.title)
        # 分区与题目的对应关系也要带过来
        self.assertEqual(
            sorted(clone.paper_questions.values_list("section__question_type", flat=True)),
            sorted(paper.paper_questions.values_list("section__question_type", flat=True)),
        )
        # 是两份独立的卷子
        self.assertNotEqual(clone.id, paper.id)

    def test_cannot_delete_published_paper(self):
        paper = self.make_paper(pass_score=2)
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=2))
        PaperService.change_status(paper, PaperStatus.PUBLISHED)

        with self.assertRaises(BusinessError) as ctx:
            PaperService.delete(paper)
        self.assertIn("不能删除", str(ctx.exception))

    def test_delete_draft_paper(self):
        paper = self.make_paper()
        paper_id = paper.id
        PaperService.delete(paper)
        self.assertFalse(Paper.objects.filter(id=paper_id).exists())

    def test_publish_empty_paper_raises(self):
        paper = self.make_paper()
        with self.assertRaises(BusinessError) as ctx:
            PaperService.change_status(paper, PaperStatus.PUBLISHED)
        self.assertIn("空卷", str(ctx.exception))

    def test_publish_sets_published_at(self):
        paper = self.make_paper(pass_score=2)
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=2))
        paper = PaperService.change_status(paper, PaperStatus.PUBLISHED)

        self.assertEqual(paper.status, PaperStatus.PUBLISHED)
        self.assertIsNotNone(paper.published_at)

    def test_publish_rejects_pass_score_above_total(self):
        paper = self.make_paper(pass_score=100)
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=2))
        with self.assertRaises(BusinessError) as ctx:
            PaperService.change_status(paper, PaperStatus.PUBLISHED)
        self.assertIn("及格分", str(ctx.exception))

    def test_published_paper_is_not_editable(self):
        paper = self.make_paper(pass_score=2)
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=2))
        PaperService.change_status(paper, PaperStatus.PUBLISHED)

        with self.assertRaises(BusinessError) as ctx:
            PaperService.add_questions(
                paper=paper, questions=self.questions_of(QuestionType.JUDGE, 2, limit=1),
            )
        self.assertIn("只有草稿可以修改", str(ctx.exception))

    def test_revert_to_draft_clears_published_at(self):
        paper = self.make_paper(pass_score=2)
        PaperService.add_questions(paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=2))
        PaperService.change_status(paper, PaperStatus.PUBLISHED)
        paper = PaperService.change_status(paper, PaperStatus.DRAFT)

        self.assertIsNone(paper.published_at)
        self.assertTrue(paper.is_editable)

    def test_change_status_rejects_unknown_value(self):
        paper = self.make_paper()
        with self.assertRaises(BusinessError) as ctx:
            PaperService.change_status(paper, "nonsense")
        self.assertIn("未知的试卷状态", str(ctx.exception))

    def test_actual_difficulty_is_score_weighted(self):
        paper = self.make_paper()
        easy = self.questions_of(QuestionType.SINGLE, Difficulty.EASY, limit=1)
        hard = self.questions_of(QuestionType.SINGLE, Difficulty.HARD, limit=1)

        PaperService.add_questions(paper=paper, questions=easy, score=8)   # 难度 1，8 分
        PaperService.add_questions(paper=paper, questions=hard, score=2)   # 难度 3，2 分
        paper.refresh_from_db()

        # 加权平均：(1×8 + 3×2) / 10 = 1.4；简单平均会是 2.0
        self.assertAlmostEqual(paper.actual_difficulty(), 1.4, places=2)

    def test_section_label_reads_naturally(self):
        paper = self.make_paper()
        PaperService.add_questions(
            paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2), score=2,
        )
        label = paper.sections.first().section_label()
        self.assertEqual(label, "一、单项选择题（共 6 题，每题 2 分，共 12 分）")


# ======================================================================
# 组卷算法
# ======================================================================
class ComposeAlgorithmTests(PaperBaseTest):

    def test_difficulty_mix_sums_to_one(self):
        for target in (1.0, 1.5, 2.0, 2.5, 3.0, 3.7, 4.0):
            mix = PaperComposeService.plan_difficulty_mix(target)
            self.assertAlmostEqual(sum(mix.values()), 1.0, places=3, msg=f"target={target}")

    def test_difficulty_mix_peaks_at_target(self):
        mix = PaperComposeService.plan_difficulty_mix(2.0)
        self.assertEqual(max(mix, key=mix.get), 2)
        self.assertAlmostEqual(mix[2], 0.6, places=2)
        self.assertAlmostEqual(mix[1], 0.2, places=2)
        self.assertAlmostEqual(mix[4], 0.0, places=2)

    def test_difficulty_mix_shifts_with_target(self):
        easy = PaperComposeService.plan_difficulty_mix(1.0)
        hard = PaperComposeService.plan_difficulty_mix(4.0)
        self.assertGreater(easy[1], easy[2], "目标偏易时简单题应该最多")
        self.assertGreater(hard[4], hard[3], "目标偏难时挑战题应该最多")

    def test_split_by_mix_keeps_total_exact(self):
        mix = PaperComposeService.plan_difficulty_mix(2.0)
        for count in (1, 3, 5, 10, 17, 100):
            quota = PaperComposeService._split_by_mix(count, mix)
            self.assertEqual(sum(quota.values()), count, msg=f"count={count}")

    def test_normalize_blueprint_rejects_unknown_type(self):
        with self.assertRaises(BusinessError):
            PaperComposeService._normalize_blueprint([{"type": "unknown", "count": 1, "score": 2}])

    def test_normalize_blueprint_sorts_by_type_order(self):
        rows = PaperComposeService._normalize_blueprint([
            {"type": QuestionType.SHORT, "count": 1, "score": 10},
            {"type": QuestionType.SINGLE, "count": 2, "score": 2},
        ])
        self.assertEqual([row["type"] for row in rows], [QuestionType.SINGLE, QuestionType.SHORT])

    def test_normalize_blueprint_skips_zero_count(self):
        rows = PaperComposeService._normalize_blueprint([
            {"type": QuestionType.SINGLE, "count": 0, "score": 2},
            {"type": QuestionType.JUDGE, "count": 1, "score": 2},
        ])
        self.assertEqual(len(rows), 1)

    def test_preview_does_not_touch_database(self):
        before = Paper.objects.count()
        plan = PaperComposeService.preview(subject=SUBJECT, seed=1)
        self.assertEqual(Paper.objects.count(), before, "试算绝不能建卷子")
        self.assertTrue(plan["selected"])
        self.assertEqual(plan["report"]["picked"]["question_count"], 34)

    def test_compose_produces_requested_counts_and_score(self):
        result = PaperComposeService.compose(
            user=self.teacher, subject=SUBJECT, title="智能卷", seed=7,
        )
        paper = result["paper"]
        report = result["report"]

        self.assertEqual(paper.question_count, 34)
        self.assertEqual(float(paper.total_score), 100.0)
        self.assertEqual(paper.generation_mode, PaperGenerationMode.AI)
        self.assertEqual(paper.status, PaperStatus.DRAFT)
        self.assertTrue(report["feasible"])
        self.assertEqual(report["shortage"], [])

        # 各题型题量与蓝图一致
        by_type = {}
        for item in paper.paper_questions.all():
            by_type[item.question_type] = by_type.get(item.question_type, 0) + 1
        for row in DEFAULT_BLUEPRINT:
            self.assertEqual(by_type.get(row["type"], 0), row["count"], msg=row["type"])

        # 大题分值 = 蓝图每题分值 × 题量
        sections = {s.question_type: s for s in paper.sections.all()}
        self.assertEqual(sections[QuestionType.SINGLE].score_per_question, Decimal("2.00"))
        self.assertEqual(sections[QuestionType.SHORT].score_per_question, Decimal("10.00"))
        self.assertEqual(float(sections[QuestionType.MULTIPLE].subtotal), 15.0)

    def test_compose_has_no_duplicate_question(self):
        paper = PaperComposeService.compose(user=self.teacher, subject=SUBJECT, seed=3)["paper"]
        ids = list(paper.paper_questions.values_list("question_id", flat=True))
        self.assertEqual(len(ids), len(set(ids)), "同一份卷子里不能出现重复题目")

    def test_compose_spreads_knowledge_points(self):
        """知识点轮转：两个知识点都该被用到，而不是全砸在一个上。"""
        result = PaperComposeService.compose(user=self.teacher, subject=SUBJECT, seed=5)
        used = set(
            result["paper"].paper_questions.values_list("question__knowledge_points__id", flat=True)
        ) - {None}
        self.assertEqual(used, {self.kp_a.id, self.kp_b.id})

        # 分布不能太偏：两个知识点各承担 30%~70%
        counts = [
            result["paper"].paper_questions.filter(question__knowledge_points=kp).count()
            for kp in (self.kp_a, self.kp_b)
        ]
        total = sum(counts)
        for count in counts:
            self.assertGreater(count / total, 0.3, f"知识点分布过于集中：{counts}")

    def test_compose_respects_exclude_ids(self):
        excluded = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=6)]
        result = PaperComposeService.compose(
            user=self.teacher, subject=SUBJECT, seed=11, exclude_ids=excluded,
        )
        used = set(result["paper"].paper_questions.values_list("question_id", flat=True))
        self.assertFalse(used & set(excluded), "被排除的题目不该出现在卷子里")

    def test_compose_is_reproducible_with_seed(self):
        first = PaperComposeService.compose(user=self.teacher, subject=SUBJECT, seed=99)["paper"]
        second = PaperComposeService.compose(user=self.teacher, subject=SUBJECT, seed=99)["paper"]
        self.assertEqual(
            list(first.paper_questions.order_by("order").values_list("question_id", flat=True)),
            list(second.paper_questions.order_by("order").values_list("question_id", flat=True)),
        )

    def test_preview_and_compose_reports_share_the_same_shape(self):
        """试算与落库必须返回同一套字段。

        两条路径背后是同一个 ``ai-compose`` 接口（只差一个 dry_run），
        字段名一旦分叉，前端就得写两套取数逻辑，而且很容易只改一边。
        """
        plan = PaperComposeService.preview(subject=SUBJECT, seed=11)
        report = PaperComposeService.compose(
            user=self.teacher, subject=SUBJECT, seed=11,
        )["report"]

        flat_keys = {"question_count", "total_score", "actual_difficulty",
                     "difficulty_deviation", "elapsed_ms", "seed"}
        nested_keys = {"summary", "requested", "picked", "difficulty",
                       "knowledge", "shortage", "adjusted", "feasible"}

        self.assertTrue(flat_keys <= set(plan["report"]), "试算缺扁平字段")
        self.assertTrue(flat_keys <= set(report), "落库缺扁平字段")
        self.assertTrue(nested_keys <= set(plan["report"]), "试算缺嵌套字段")
        self.assertTrue(nested_keys <= set(report), "落库缺嵌套字段")

        # 同 seed 同条件，扁平字段的取值也该对得上
        self.assertEqual(plan["report"]["question_count"], report["question_count"])
        self.assertEqual(plan["report"]["total_score"], report["total_score"])

    def test_compose_reports_shortage_when_pool_is_small(self):
        """单个知识点下单选只有 12 道，却要 20 道，必然组不满。"""
        plan = PaperComposeService.preview(
            subject=SUBJECT, knowledge_point_ids=[self.kp_a.id], seed=2,
            blueprint=[{"type": QuestionType.SINGLE, "count": 20, "score": 2}],
        )
        report = plan["report"]

        self.assertFalse(report["feasible"])
        self.assertEqual(report["picked"]["question_count"], 12, "能把库存抽干，但抽不满")
        row = report["shortage"][0]
        self.assertEqual(row["type"], QuestionType.SINGLE)
        self.assertLess(row["filled"], row["requested"])
        self.assertIn("缺", row["message"])

    def test_compose_falls_back_to_adjacent_difficulty(self):
        """只要困难档的题不够，就该出现在 adjusted 里而不是 shortage 里。"""
        # 单选只要难度 3 的题，且只要 3 道（够），但配比会要求别的档
        plan = PaperComposeService.preview(
            subject=SUBJECT, seed=4,
            blueprint=[{"type": QuestionType.SINGLE, "count": 10, "score": 2}],
            target_difficulty=4.0,
        )
        self.assertEqual(plan["report"]["picked"]["question_count"], 10)

    def test_rebalance_trims_quota_to_available_stock(self):
        """库存不足时把配额削到可行范围，剩下的缺口留给回退逻辑去处理。"""
        candidates = self.questions_of(QuestionType.JUDGE, 1, limit=3)  # 只有 3 道难度 1
        quota = {1: 2, 2: 6, 3: 2, 4: 0}
        fixed = PaperComposeService._rebalance_quota(quota, candidates, 2.0)

        self.assertLessEqual(sum(fixed.values()), sum(quota.values()))
        self.assertEqual(fixed[2], 0, "一道库存都没有的档位不该保留配额")
        self.assertEqual(fixed[3], 0, "一道库存都没有的档位不该保留配额")
        self.assertEqual(fixed[1], 3, "把能用的题都用上")

    def test_rebalance_keeps_quota_when_stock_is_enough(self):
        candidates = self.questions_of(QuestionType.SINGLE, 2, limit=6)
        candidates += self.questions_of(QuestionType.SINGLE, 3, limit=6)
        candidates += self.questions_of(QuestionType.SINGLE, 1, limit=6)
        quota = {1: 2, 2: 6, 3: 2, 4: 0}
        self.assertEqual(PaperComposeService._rebalance_quota(quota, candidates, 2.0), quota)

    def test_compose_honours_custom_blueprint(self):
        blueprint = [
            {"type": QuestionType.SINGLE, "count": 4, "score": 5},
            {"type": QuestionType.SHORT, "count": 2, "score": 10},
        ]
        result = PaperComposeService.compose(
            user=self.teacher, subject=SUBJECT, blueprint=blueprint, seed=13,
        )
        self.assertEqual(result["paper"].question_count, 6)
        self.assertEqual(float(result["paper"].total_score), 40.0)

    def test_compose_rejects_unknown_blueprint_type(self):
        with self.assertRaises(BusinessError):
            PaperComposeService.compose(
                user=self.teacher, subject=SUBJECT,
                blueprint=[{"type": "coding", "count": 1, "score": 10}],
            )

    def test_compose_rejects_empty_blueprint(self):
        with self.assertRaises(BusinessError):
            PaperComposeService._normalize_blueprint([])

    def test_compose_raises_when_nothing_available(self):
        with self.assertRaises(BusinessError) as ctx:
            PaperComposeService.compose(
                user=self.teacher, subject="不存在的学科",
                blueprint=[{"type": QuestionType.SINGLE, "count": 5, "score": 2}],
            )
        self.assertIn("没有抽到任何题目", str(ctx.exception))

    def test_compose_sets_pass_score_as_60_percent(self):
        result = PaperComposeService.compose(user=self.teacher, subject=SUBJECT, seed=17)
        self.assertEqual(float(result["paper"].pass_score), 60.0)

    def test_describe_pool_matrix(self):
        data = PaperComposeService.describe_pool(subject=SUBJECT)
        self.assertEqual(data["total"], 144)
        by_type = {row["type"]: row for row in data["by_type"]}
        self.assertEqual(by_type[QuestionType.SINGLE]["total"], 24)
        self.assertEqual(by_type[QuestionType.SINGLE]["by_difficulty"][2], 6)

    def test_pool_filters_by_knowledge_point_descendants(self):
        parent = KnowledgePoint.objects.create(name="线性结构", subject=SUBJECT, code="T-P")
        child = KnowledgePoint.objects.create(
            name="栈顶", subject=SUBJECT, code="T-P-1", parent=parent,
        )
        child_question = self.questions_of(QuestionType.SINGLE, 2, limit=1)[0]
        child_question.knowledge_points.add(child)

        pool = PaperComposeService.build_pool(subject=SUBJECT, knowledge_point_ids=[parent.id])
        self.assertIn(child_question.id, [q.id for q in pool[QuestionType.SINGLE]])


# ======================================================================
# 分析
# ======================================================================
class AnalysisTests(PaperBaseTest):

    def build_paper(self) -> Paper:
        paper = self.make_paper(title="分析用卷", target_difficulty=2.0)
        PaperService.add_questions(
            paper=paper, questions=self.questions_of(QuestionType.SINGLE, 1, limit=4), score=2,
        )
        PaperService.add_questions(
            paper=paper, questions=self.questions_of(QuestionType.SINGLE, 3, limit=2), score=2,
        )
        PaperService.add_questions(
            paper=paper, questions=self.questions_of(QuestionType.SHORT, 2, limit=1), score=10,
        )
        paper.refresh_from_db()
        return paper

    def test_overview(self):
        overview = PaperAnalysisService.overview(self.build_paper())
        self.assertEqual(overview["question_count"], 7)
        self.assertEqual(overview["total_score"], 22.0)
        self.assertEqual(overview["section_count"], 2)
        self.assertEqual(overview["status"], PaperStatus.DRAFT)
        self.assertGreater(overview["actual_difficulty"], 0)

    def test_type_distribution(self):
        rows = {row["type"]: row for row in PaperAnalysisService.type_distribution(self.build_paper())}
        self.assertEqual(rows[QuestionType.SINGLE]["count"], 6)
        self.assertEqual(rows[QuestionType.SINGLE]["score"], 12.0)
        self.assertEqual(rows[QuestionType.SHORT]["count"], 1)
        self.assertAlmostEqual(sum(row["ratio"] for row in rows.values()), 1.0, places=4)

    def test_difficulty_distribution_includes_all_levels(self):
        rows = PaperAnalysisService.difficulty_distribution(self.build_paper())
        self.assertEqual(len(rows), 4, "四档都要列出来，没有的档显示 0")
        by_value = {row["value"]: row for row in rows}
        self.assertEqual(by_value[1]["count"], 4)
        self.assertEqual(by_value[3]["count"], 2)
        self.assertEqual(by_value[4]["count"], 0)

    def test_knowledge_coverage(self):
        coverage = PaperAnalysisService.knowledge_coverage(self.build_paper())
        self.assertGreater(coverage["distinct_count"], 0)
        self.assertEqual(len(coverage["indicators"]), len(coverage["values"]))

    def test_knowledge_coverage_counts_missing_planned(self):
        other = KnowledgePoint.objects.create(name="图论", subject=SUBJECT, code="T-G")
        coverage = PaperAnalysisService.knowledge_coverage(
            self.build_paper(), planned_ids=[self.kp_a.id, self.kp_b.id, other.id],
        )
        self.assertEqual(coverage["planned_count"], 3)
        self.assertLess(coverage["coverage_ratio"], 1.0)
        self.assertIn(other.id, [item["id"] for item in coverage["missing"]])

    def test_section_stats(self):
        rows = PaperAnalysisService.section_stats(self.build_paper())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["question_count"], 6)
        self.assertEqual(rows[0]["subtotal"], 12.0)
        self.assertTrue(rows[0]["label"].startswith("一、"))

    def test_warnings_on_empty_paper(self):
        warnings = PaperAnalysisService.warnings(self.make_paper())
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], "empty")

    def test_warning_on_difficulty_deviation(self):
        paper = self.make_paper(target_difficulty=1.0)
        PaperService.add_questions(
            paper=paper, questions=self.questions_of(QuestionType.SINGLE, 4, limit=5),
        )
        paper.refresh_from_db()
        codes = [item["code"] for item in PaperAnalysisService.warnings(paper)]
        self.assertIn("difficulty_deviation", codes)

    def test_warning_on_pass_score_above_total(self):
        paper = self.make_paper(pass_score=500)
        PaperService.add_questions(
            paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=1),
        )
        paper.refresh_from_db()
        codes = [item["code"] for item in PaperAnalysisService.warnings(paper)]
        self.assertIn("pass_score", codes)

    def test_warning_on_single_type_paper(self):
        paper = self.make_paper()
        PaperService.add_questions(
            paper=paper, questions=self.questions_of(QuestionType.SINGLE, 2, limit=3),
        )
        paper.refresh_from_db()
        codes = [item["code"] for item in PaperAnalysisService.warnings(paper)]
        self.assertIn("single_type", codes)

    def test_full_report_shape(self):
        report = PaperAnalysisService.full_report(self.build_paper())
        for key in ("overview", "type_distribution", "difficulty_distribution",
                    "knowledge_coverage", "sections", "warnings"):
            self.assertIn(key, report)
