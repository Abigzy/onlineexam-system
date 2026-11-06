"""每日练习的**学科范围**收口：只能练「已加入的学科」。

口径：「已加入的学科」= 学生所在**学科班**（``Classroom.class_type == SUBJECT``）
的 ``subject``。题库里有启用题、或学生曾经作答过，都不足以让一个学科可选。

本文件逐条验契约：

1. 加入一个学科班 → 下拉只有它；
2. 加入两个 → 按**该学生的作答量**降序（熟悉的在前）；
3. 一个学科班都没加入 → ``insufficient``，说清「联系任课教师」；
4. 深链指向没加入的学科 → 不返回任何题目；
5. 已加入但 0 作答 → 仍走 starter（冷启动不能被误伤）；
6. ``subject=""``（全部学科）→ starter / normal 的候选也只能落在已加入的学科内。
"""

from apps.recommend.services import RecommendationService

from .base import RecommendBaseTest, SUBJECT

TODAY = "2026-09-30"
JOINED = "操作系统"
NOT_JOINED = "计算机网络"


class SubjectScopeTest(RecommendBaseTest):
    """可选范围 = 已加入的学科班。"""

    # 范围是本类的被测对象，所以不用默认学科班，每个用例显式加入
    default_subject_classroom = False

    def seed(self, subject, count=3, difficulty=2):
        for index in range(count):
            kp = self.add_knowledge_point(f"{subject}-SCOPE-KP-{index}")
            self.add_candidate(kp=kp, subject=subject, difficulty=difficulty)

    # ------------------------------------------------------------------
    def test_joined_subject_is_the_only_option(self):
        self.seed(JOINED)
        self.seed(NOT_JOINED)          # 有题但没加入
        self.join_subject(JOINED, student=self.student_b)

        self.assertEqual(RecommendationService.selectable_subjects(self.student_b), [JOINED])

    def test_options_ordered_by_answered_count(self):
        """两个学科都加入时，作答量多的排在前面。"""
        self.seed(SUBJECT)
        self.seed(JOINED)
        self.make_weak()               # 在 SUBJECT 上留下 6 条已判分作答
        self.join_subject(SUBJECT, student=self.student)
        self.join_subject(JOINED, student=self.student)

        self.assertEqual(
            RecommendationService.selectable_subjects(self.student), [SUBJECT, JOINED],
        )

    # ------------------------------------------------------------------
    def test_no_subject_classroom_returns_insufficient(self):
        self.seed(SUBJECT)
        payload = RecommendationService.daily_practice(self.student_b, today=TODAY)

        self.assertTrue(payload["insufficient"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["selectable_subjects"], [])
        # 这不是 starter（不是「没作答」也不是「没题」），mode 保持普通值
        self.assertEqual(payload["mode"], "normal")
        self.assertEqual(payload["summary"]["mode"], "normal")
        self.assertIn("还没有加入任何学科班", payload["message"])

    def test_deep_link_to_unjoined_subject_is_blocked(self):
        self.seed(NOT_JOINED)
        self.join_subject(JOINED, student=self.student_b)   # 加入的是另一个学科

        payload = RecommendationService.daily_practice(
            self.student_b, subject=NOT_JOINED, today=TODAY,
        )
        self.assertTrue(payload["insufficient"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["mode"], "normal")
        self.assertIn("不在你加入的学科范围内", payload["message"])
        self.assertIn(NOT_JOINED, payload["message"])

    def test_joined_subject_without_history_still_gets_starter(self):
        """冷启动不能误伤：加入了、但该学科 0 作答 → 仍走 starter 出题。"""
        self.seed(JOINED)
        self.join_subject(JOINED, student=self.student_b)

        payload = RecommendationService.daily_practice(
            self.student_b, subject=JOINED, today=TODAY,
        )
        self.assertEqual(payload["mode"], "starter")
        self.assertFalse(payload["insufficient"])
        self.assertTrue(payload["items"])
        self.assertTrue(all(item["subject"] == JOINED for item in payload["items"]))

    def test_all_subjects_starter_stays_inside_joined_scope(self):
        """``subject=""`` 时 starter 也不能从没加入的学科里抽题。"""
        self.seed(JOINED)
        self.seed(NOT_JOINED)
        self.join_subject(JOINED, student=self.student_b)

        payload = RecommendationService.daily_practice(self.student_b, today=TODAY)
        self.assertEqual(payload["mode"], "starter")
        self.assertTrue(payload["items"])
        self.assertEqual(
            {item["subject"] for item in payload["items"]}, {JOINED},
            msg="全部学科的入门练习只能来自已加入的学科",
        )


class SubjectScopePageTest(RecommendBaseTest):
    """页面：下拉只列已加入的学科；没有学科班时给引导空态。"""

    default_subject_classroom = False

    def test_deep_link_out_of_scope_falls_back_in_page(self):
        """越界学科的链接：下拉里没有它，选中值回落到「全部学科」。"""
        self.add_candidate(subject=NOT_JOINED, difficulty=2)
        self.join_subject(JOINED, student=self.student)

        self.client.force_login(self.student)
        response = self.client.get("/practice/", {"subject": NOT_JOINED})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        self.assertNotIn('value="%s"' % NOT_JOINED, html)
        self.assertNotIn('value="%s" selected' % NOT_JOINED, html)
        # 首屏脚本仍会带上 URL 的学科（用于接口校验并展示越界提示），
        # 但下拉里不提供这个选项
        self.assertIn("const INITIAL_SUBJECT = '%s';" % NOT_JOINED, html)
