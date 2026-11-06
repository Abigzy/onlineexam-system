"""入门练习（starter）冷启动兜底。

背景：推荐原先只有「薄弱知识点 → 内容分 + 协同过滤」一条路，
新学生、或老学生切到没做过的学科时永远拿不到题——题库里刚补满的学科
一道都练不到。starter 就是在「没有掌握度可用」时给一组可练的题。

本文件逐条验契约：

1. 冷启动能出题，且同一天内可复现（刷新不换题）；
2. 有作答的学生切到陌生学科同样走 starter，回到熟悉学科仍是 normal；
3. 学科没题时如实说明「没有启用中的题目」，与「还没作答」区分开；
4. 入门题以难度 1~2 为主，且同一知识点不超过 ``MAX_PER_KNOWLEDGE_POINT`` 道。
"""

import re
from collections import Counter
from decimal import Decimal

from django.conf import settings

from apps.common.constants import QuestionType
from apps.recommend.constants import MAX_PER_KNOWLEDGE_POINT
from apps.recommend.services import RecommendationService

from .base import RecommendBaseTest, SUBJECT

TODAY = "2026-09-30"
# 学生没做过的学科：只用来验证「有题可练」，与默认试卷的数据结构无关
OTHER_SUBJECT = "操作系统"


class StarterPracticeTest(RecommendBaseTest):
    """冷启动 / 陌生学科的入门练习。"""

    def setUp(self):
        super().setUp()
        # 默认学科班的装配见 RecommendBaseTest（两位学生都已在「数据结构」班）；
        # 这里再补一个陌生学科，验证「已加入但没做过」的冷启动
        for student in (self.student, self.student_b):
            self.join_subject(OTHER_SUBJECT, student=student)

    def seed(self, difficulties, subject=OTHER_SUBJECT):
        """在指定学科造题，每题挂一个独立知识点。

        独立知识点是为了避开「每知识点最多 2 道」的均摊：同一知识点堆 8 道题，
        取 4 道时会被截到 2 道，就测不出难度梯度了。
        """
        types = [QuestionType.SINGLE, QuestionType.MULTIPLE,
                 QuestionType.JUDGE, QuestionType.FILL]
        for index, difficulty in enumerate(difficulties):
            kp = self.add_knowledge_point(f"{subject}-KP-{index}")
            self.add_candidate(kp=kp, subject=subject, difficulty=difficulty,
                               qtype=types[index % len(types)])

    # ------------------------------------------------------------------
    def test_new_student_gets_starter_items(self):
        """0 条作答的新学生，在「有题」的学科里能拿到入门练习。"""
        self.seed([1, 2, 1, 2])
        payload = RecommendationService.daily_practice(
            self.student_b, subject=OTHER_SUBJECT, today=TODAY,
        )

        self.assertEqual(payload["mode"], "starter")
        self.assertFalse(payload["insufficient"])
        self.assertTrue(payload["items"])
        self.assertTrue(payload["summary"]["starter"])
        self.assertEqual(payload["summary"]["mode"], "starter")
        # 文案要说清「没作答」而不是「没题」
        self.assertIn(OTHER_SUBJECT, payload["message"])
        self.assertIn("入门练习", payload["message"])
        # 每道题仍要能被页面直接渲染
        for item in payload["items"]:
            self.assertTrue(item["reason"])
            self.assertIsNotNone(item["match_score"])
            self.assertIsNotNone(item["match_percent"])
            self.assertGreaterEqual(item["order"], 1)

    def test_same_day_is_deterministic(self):
        """同一天两次调用，题目与顺序完全一致（刷新不换题）。"""
        self.seed([1, 2, 1, 2])
        first = RecommendationService.daily_practice(
            self.student_b, subject=OTHER_SUBJECT, size=4, today=TODAY,
        )
        second = RecommendationService.daily_practice(
            self.student_b, subject=OTHER_SUBJECT, size=4, today=TODAY,
        )
        self.assertEqual(len(first["items"]), 4)
        self.assertEqual([item["question_id"] for item in first["items"]],
                         [item["question_id"] for item in second["items"]])
        self.assertEqual([item["order"] for item in first["items"]], [1, 2, 3, 4])

    def test_student_with_history_in_another_subject_gets_starter(self):
        """老学生切到没做过的学科：有作答记录，但该学科没有样本 → starter。"""
        self.make_weak()   # 在默认学科（数据结构）上留下作答
        self.seed([1, 2])

        payload = RecommendationService.daily_practice(
            self.student, subject=OTHER_SUBJECT, today=TODAY,
        )
        self.assertEqual(payload["mode"], "starter")
        self.assertTrue(payload["items"])
        self.assertEqual(payload["subject"], OTHER_SUBJECT)

    def test_known_subject_still_uses_normal_mode(self):
        """回归：有薄弱点的熟悉学科，仍走内容分 + 协同过滤的 normal 路径。"""
        self.make_weak()
        payload = RecommendationService.daily_practice(
            self.student, subject=SUBJECT, today=TODAY,
        )
        self.assertEqual(payload["mode"], "normal")
        self.assertFalse(payload["summary"]["starter"])
        self.assertTrue(payload["items"])
        self.assertTrue(all(item["source"] == "local" for item in payload["items"]))

    def test_subject_without_questions_says_so(self):
        """学科确实没题：保持 insufficient，并说清是「没题」不是「没作答」。"""
        # 先加入语文的学科班——否则会被范围校验拦下，测不到「没题」这条路
        self.join_subject("语文", student=self.student_b)
        payload = RecommendationService.daily_practice(
            self.student_b, subject="语文", today=TODAY,
        )
        self.assertTrue(payload["insufficient"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["mode"], "starter")
        self.assertIn("没有启用中的题目", payload["message"])
        self.assertIn("语文", payload["message"])

    def test_starter_prefers_easy_questions(self):
        """难度梯度：先给 1~2，取 4 道时易题占比必须过半。"""
        self.seed([3, 4, 3, 4, 1, 1, 2, 2])
        payload = RecommendationService.daily_practice(
            self.student_b, subject=OTHER_SUBJECT, size=4, today=TODAY,
        )

        self.assertEqual(len(payload["items"]), 4)
        easy = [item for item in payload["items"] if item["difficulty"] <= 2]
        self.assertGreater(len(easy), len(payload["items"]) / 2,
                           msg="入门练习应以难度 1~2 为主")

    def test_starter_caps_questions_per_knowledge_point(self):
        """知识点均摊：同一个知识点最多 2 道，宁可少给也不垄断名额。"""
        for _ in range(6):
            self.add_candidate(subject=OTHER_SUBJECT, difficulty=2)

        payload = RecommendationService.daily_practice(
            self.student_b, subject=OTHER_SUBJECT, size=6, today=TODAY,
        )
        counter = Counter(item["knowledge_point"] for item in payload["items"])
        self.assertEqual(len(payload["items"]), MAX_PER_KNOWLEDGE_POINT)
        for name, count in counter.items():
            self.assertLessEqual(count, MAX_PER_KNOWLEDGE_POINT,
                                 msg=f"{name} 堆了 {count} 道入门题")

    # ------------------------------------------------------------------
    # 措辞：starter 页面上不能出现「薄弱」——学生在该学科根本没有数据
    # ------------------------------------------------------------------
    def test_starter_ai_reasons_avoid_weak_wording(self):
        """starter 的 AI 改写理由（mock）不得出现「薄弱 / 正确率」。"""
        self.seed([1, 2, 1, 2])
        payload = RecommendationService.daily_practice(
            self.student_b, subject=OTHER_SUBJECT, today=TODAY,
        )
        result = RecommendationService.explain(
            self.student_b, payload["items"], subject=OTHER_SUBJECT,
            provider="mock", user=self.student_b, mode="starter",
        )

        self.assertFalse(result["degraded"])
        self.assertEqual(result["source"], "ai")
        self.assertIn("入门练习", result["reason"])
        self.assertNotIn("薄弱", result["reason"])
        for item in result["items"]:
            self.assertNotIn("薄弱", item["reason"], msg=f"题目 {item['question_id']} 理由含「薄弱」")
            self.assertNotIn("正确率", item["reason"])

    def test_starter_practice_view_uses_starter_wording(self):
        """视图层把 mode 透传给 explain——POST 改写后整份 payload 不含「薄弱」。"""
        from apps.recommend.views import _practice

        self.seed([1, 2, 1, 2])
        payload = _practice(
            self.student_b, {"subject": OTHER_SUBJECT, "size": 4},
            provider="mock", user=self.student_b, with_ai=True,
        )
        self.assertEqual(payload["mode"], "starter")
        self.assertNotIn("薄弱", payload["ai"]["reason"])
        for item in payload["items"]:
            self.assertNotIn("薄弱", item["reason"])

    def test_normal_ai_reasons_still_reference_weak_points(self):
        """回归：normal 的措辞保持原样（仍讲薄弱环节），不能被 starter 的改动带偏。"""
        self.make_weak()
        payload = RecommendationService.daily_practice(
            self.student, subject=SUBJECT, size=3, today=TODAY,
        )
        result = RecommendationService.explain(
            self.student, payload["items"], subject=SUBJECT,
            provider="mock", user=self.student, mode="normal",
        )
        self.assertTrue(all("薄弱环节" in item["reason"] for item in result["items"]))
        self.assertIn("薄弱知识点", result["reason"])

    def test_starter_summary_reports_covered_knowledge_points(self):
        """summary 提供覆盖知识点数，供页面 KPI 直接使用。"""
        self.seed([1, 2, 1, 2])
        payload = RecommendationService.daily_practice(
            self.student_b, subject=OTHER_SUBJECT, today=TODAY,
        )
        expected = {item["knowledge_point"] for item in payload["items"]}
        self.assertEqual(payload["summary"]["starter_knowledge_points"], len(expected))

    def test_starter_kpi_copy_is_starter_specific(self):
        """页面 KPI 在 starter 下给出「入门练习 · 覆盖 N 个知识点」，而不是薄弱点。"""
        template = (settings.BASE_DIR / "templates/recommend/practice.html").read_text(
            encoding="utf-8",
        )
        self.assertIn("kpi('入门练习'", template)
        self.assertIn("'覆盖 ' + num(starterKnowledgeCount(data)) + ' 个知识点'", template)

    def test_starter_footer_drops_weakness_formula(self):
        """starter 页脚改讲入门口径、不含「薄弱」；normal 页脚公式一字不改（对照）。

        真正的行为验证在 ``scripts/page_js_checks/practice.js``（执行模板内联 JS），
        这里守住模板里的两条文案，避免被后续改动悄悄换回薄弱度公式。
        """
        template = (settings.BASE_DIR / "templates/recommend/practice.html").read_text(
            encoding="utf-8",
        )
        matched = re.search(r"function footerHtml\(data\) \{(.*?)\n    \}", template, re.S)
        self.assertIsNotNone(matched, msg="模板里找不到 footerHtml")
        body = matched.group(1)

        # starter 分支：讲「难度梯度 + 知识点覆盖」，不提历史正确率
        self.assertIn("data.mode === 'starter'", body)
        starter_hint = re.search(r"'匹配度 = 难度贴合度（([^']*)）'", body)
        self.assertIsNotNone(starter_hint, msg="starter 页脚文案缺失")
        self.assertIn("不参考历史正确率", starter_hint.group(0))
        self.assertNotIn("薄弱", starter_hint.group(0))

        # normal 分支：原公式保持原样
        self.assertIn("知识点薄弱度 × 证据充分度", body)


class SelectableSubjectsTest(RecommendBaseTest):
    """页面下拉的可选范围：**只含学生已加入的学科**（学科班的学科）。

    口径变更：早先还并入「题库里有启用题的学科」，让冷启动学生能练没做过的学科；
    现在产品要求收窄——没加入的学科不允许选择，「有题」不再是入选理由。

    本类不启用默认学科班（``default_subject_classroom = False``），
    每个用例自己决定加入哪些学科，才能把「加入了 / 没加入」对照清楚。
    """

    default_subject_classroom = False

    def test_question_only_subjects_are_not_selectable(self):
        """题库里有题但没加入学科班 → 不出现在下拉里；加入后才出现。"""
        for name in ("计算机组成原理", OTHER_SUBJECT, "计算机网络"):
            self.add_candidate(subject=name, difficulty=2)

        before = RecommendationService.selectable_subjects(self.student_b)
        for name in ("计算机组成原理", OTHER_SUBJECT, "计算机网络"):
            self.assertNotIn(name, before, msg=f"{name} 没加入学科班，不该可选")

        self.join_subject(OTHER_SUBJECT, student=self.student_b)
        after = RecommendationService.selectable_subjects(self.student_b)
        self.assertEqual(after, [OTHER_SUBJECT])

    def test_payload_keeps_subjects_and_adds_selectable(self):
        """``subjects`` 语义不变（只含做过的），新字段才是下拉的数据源。"""
        self.add_candidate(subject=OTHER_SUBJECT, difficulty=2)
        self.join_subject(OTHER_SUBJECT, student=self.student_b)
        payload = RecommendationService.daily_practice(self.student_b, today=TODAY)

        self.assertEqual(payload["subjects"], [])
        self.assertEqual(payload["selectable_subjects"], [OTHER_SUBJECT])

    def test_answered_subjects_come_first_and_are_deduped(self):
        self.make_weak()                      # SUBJECT 上有 6 条已判分作答
        self.add_candidate(subject=SUBJECT, difficulty=2)   # 同一学科也有题
        self.add_candidate(subject=OTHER_SUBJECT, difficulty=2)
        for subject in (SUBJECT, OTHER_SUBJECT):
            self.join_subject(subject, student=self.student)

        subjects = RecommendationService.selectable_subjects(self.student)
        self.assertEqual(subjects.count(SUBJECT), 1, msg="同一学科不能重复出现")
        self.assertEqual(subjects[0], SUBJECT, msg="有作答的学科应排在最前")
        self.assertIn(OTHER_SUBJECT, subjects)
        self.assertNotIn("", subjects)
        self.assertLess(subjects.index(SUBJECT), subjects.index(OTHER_SUBJECT))

    def test_more_answered_questions_rank_higher(self):
        """两个学科都做过时，按作答量降序——不是按字母序。"""
        from apps.papers.services import PaperService

        self.make_weak()                      # SUBJECT：6 条
        question = self.add_candidate(subject=OTHER_SUBJECT, difficulty=2)
        paper = PaperService.create(
            data={"title": "跨学科卷", "subject": OTHER_SUBJECT,
                  "duration_minutes": 30, "pass_score": Decimal("1")},
            user=self.teacher,
        )
        PaperService.add_questions(paper=paper, questions=[question], strict=False)
        PaperService.change_status(paper, "published")
        exam = self.make_exam(paper=paper, subject=OTHER_SUBJECT)
        self.judged_attempt(exam, student=self.student, correct_types={question.type})
        for subject in (SUBJECT, OTHER_SUBJECT):
            self.join_subject(subject, student=self.student)

        subjects = RecommendationService.selectable_subjects(self.student)
        self.assertLess(subjects.index(SUBJECT), subjects.index(OTHER_SUBJECT),
                        msg="作答 6 题的学科应排在作答 1 题的学科前面")

    def test_answered_subject_outside_joined_scope_is_dropped(self):
        """在新口径下 ``selectable_subjects`` 不再必然包含 ``subjects`` 里的值。

        学生曾在某学科作答，但后来退出该学科班 → 下拉里没有它（不再可选）。
        """
        self.make_weak()                       # SUBJECT 上有作答
        self.join_subject(OTHER_SUBJECT, student=self.student)

        payload = RecommendationService.daily_practice(self.student, today=TODAY)
        self.assertIn(SUBJECT, payload["subjects"], msg="subjects 语义不变：作答过就有")
        self.assertNotIn(SUBJECT, payload["selectable_subjects"],
                         msg="已退出该学科班，下拉里不该再有它")


class StarterPageTest(RecommendBaseTest):
    """页面入口：下拉只能列出学生**已加入**的学科。"""

    default_subject_classroom = False

    def test_page_select_lists_only_joined_subjects(self):
        for name in ("计算机组成原理", OTHER_SUBJECT, "计算机网络"):
            self.add_candidate(subject=name, difficulty=2)
        # 只加入「操作系统」，另外两个学科「有题但没加入」
        self.join_subject(OTHER_SUBJECT, student=self.student)

        self.client.force_login(self.student)
        response = self.client.get("/practice/", {"subject": OTHER_SUBJECT})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        self.assertIn('value="%s"' % OTHER_SUBJECT, html, msg="下拉缺少已加入的学科")
        for name in ("计算机组成原理", "计算机网络"):
            self.assertNotIn('value="%s"' % name, html, msg=f"下拉不该出现未加入的学科 {name}")
        # URL 上的学科要预选中，且被写进首屏脚本
        self.assertIn('value="%s" selected' % OTHER_SUBJECT, html)
        self.assertIn("const INITIAL_SUBJECT = '%s';" % OTHER_SUBJECT, html)

    def test_page_shows_guide_when_no_subject_classroom(self):
        """一个学科班都没加入 → 页面给出引导空态，并禁用会报错的按钮。"""
        self.add_candidate(subject=OTHER_SUBJECT, difficulty=2)

        self.client.force_login(self.student)
        response = self.client.get("/practice/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        self.assertIn('data-scope="none"', html)
        self.assertNotIn('value="%s"' % OTHER_SUBJECT, html)
        # 引导空态文案由接口 message 给出（模板里同源的一份在 JS 空态渲染里）
        self.assertIn("你还没有加入任何学科班", self._guide_message())
        # 两个按钮禁用，避免点了直接撞范围拦截
        self.assertRegex(html, r'id="btn-shuffle"\s+disabled')
        self.assertRegex(html, r'id="btn-ai-reason"\s+disabled')

    def _guide_message(self):
        """从服务端拿一次「没有学科班」的 message——保证文案与接口同源。"""
        payload = RecommendationService.daily_practice(self.student, today=TODAY)
        self.assertTrue(payload["insufficient"])
        return payload["message"]

    def test_starter_banner_has_no_weakness_wording(self):
        template = (settings.BASE_DIR / "templates/recommend/practice.html").read_text(
            encoding="utf-8",
        )
        matched = re.search(r"function starterBanner\(\) \{(.*?)\n    \}", template, re.S)
        self.assertIsNotNone(matched, msg="模板里找不到 starterBanner")
        body = matched.group(1)
        self.assertIn("按你的掌握度做个性化推荐", body)
        self.assertNotIn("薄弱", body)
