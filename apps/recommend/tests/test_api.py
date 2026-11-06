"""推荐接口的测试。

接口只有两个动词，但权限边界要写清楚：

* ``GET`` 取推荐清单（**本地理由，绝不调大模型**）；
* ``POST`` 同一份清单，理由交给大模型改写（失败降级）。

权限与模块五、模块六同源：**跨人 404 而不是 403**（不泄露「这个 id 存不存在」），
学生只能看自己的，教师可以看任意学生的（用于布置针对性练习）。
"""

from unittest.mock import patch

from apps.recommend.services import RecommendationService
from apps.recommend.views import DailyPracticeView

from .base import RecommendBaseTest, SUBJECT

PRACTICE = "/api/v1/recommend/students/{student_id}/practice/"


class PracticeApiBase(RecommendBaseTest):
    def setUp(self):
        super().setUp()
        # 让 student 有一份真实可推荐的学情
        self.make_weak()

    def url(self, student_id):
        return PRACTICE.format(student_id=student_id)


class PracticeReadTest(PracticeApiBase):
    """GET：拿清单，且不产生任何大模型调用。"""

    def test_student_reads_own_practice(self):
        response = self.get_json(self.url(self.student.id), user=self.student)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        data = payload["data"]
        self.assertEqual(data["student"]["id"], self.student.id)
        self.assertTrue(data["items"])
        # GET 不自动调大模型——刷新页面不该变成计费器
        self.assertIsNone(data["ai"])

    def test_get_never_touches_the_model(self):
        with patch("apps.recommend.services.get_ai_client") as client_factory:
            response = self.get_json(self.url(self.student.id), user=self.student)
        self.assertEqual(response.status_code, 200)
        client_factory.assert_not_called()

    def test_teacher_reads_student_practice(self):
        response = self.get_json(self.url(self.student.id), user=self.teacher)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["items"])

    def test_size_and_offset_params_are_accepted(self):
        response = self.get_json(self.url(self.student.id), user=self.student,
                                 data={"size": 2, "offset": 0, "subject": SUBJECT})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["size"], 2)
        self.assertEqual(data["offset"], 0)
        self.assertLessEqual(len(data["items"]), 2)

    def test_student_without_data_gets_starter_not_an_error(self):
        # 新学生不再返回 insufficient 的空清单，而是 starter 入门练习；
        # 断言随之调整，接口状态码与信封结构不变。
        response = self.get_json(self.url(self.student_b.id), user=self.student_b)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["insufficient"])
        self.assertEqual(data["mode"], "starter")
        self.assertTrue(data["items"])
        self.assertTrue(data["message"])


class PracticePermissionTest(PracticeApiBase):
    """权限边界。"""

    def test_student_cannot_read_another_student(self):
        response = self.get_json(self.url(self.student_b.id), user=self.student)
        self.assertEqual(response.status_code, 404)

    def test_student_cannot_probe_a_teacher_id(self):
        response = self.get_json(self.url(self.teacher.id), user=self.student)
        self.assertEqual(response.status_code, 404)

    def test_teacher_reading_a_teacher_id_is_404(self):
        response = self.get_json(self.url(self.teacher.id), user=self.teacher)
        self.assertEqual(response.status_code, 404)

    def test_unknown_student_is_404(self):
        response = self.get_json(self.url(999999), user=self.teacher)
        self.assertEqual(response.status_code, 404)

    def test_anonymous_is_rejected(self):
        response = self.client.get(self.url(self.student.id))
        self.assertEqual(response.status_code, 401)

    def test_post_permissions_match_get(self):
        # 学生不能借 POST 看别人的推荐
        response = self.post_json(self.url(self.student_b.id), user=self.student)
        self.assertEqual(response.status_code, 404)


class PracticeValidationTest(PracticeApiBase):
    """参数非法要给 400，而不是 500。"""

    def test_non_numeric_size_is_400(self):
        response = self.get_json(self.url(self.student.id), user=self.student,
                                 data={"size": "abc"})
        self.assertEqual(response.status_code, 400)

    def test_size_above_max_is_400(self):
        response = self.get_json(self.url(self.student.id), user=self.student,
                                 data={"size": 9999})
        self.assertEqual(response.status_code, 400)

    def test_negative_offset_is_400(self):
        response = self.get_json(self.url(self.student.id), user=self.student,
                                 data={"offset": -1})
        self.assertEqual(response.status_code, 400)


class PracticeReasonTest(PracticeApiBase):
    """POST：AI 改写理由。"""

    def test_post_with_mock_provider_returns_ai_reasons(self):
        response = self.post_json(self.url(self.student.id), {"provider": "mock"},
                                  user=self.student)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        data = payload["data"]
        self.assertIsNotNone(data["ai"])
        self.assertTrue(data["ai"]["is_mock"])
        self.assertFalse(data["ai"]["degraded"])
        self.assertTrue(data["items"])
        self.assertTrue(all(item["source"] == "ai" for item in data["items"]))
        self.assertIn("模拟", payload["message"])

    def test_post_degrades_instead_of_failing(self):
        response = self.post_json(self.url(self.student.id), {"provider": "不存在的平台"},
                                  user=self.student)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["ai"]["degraded"])
        self.assertTrue(all(item["source"] == "local" for item in data["items"]))
        self.assertIn("本地推荐理由", response.json()["message"])

    def test_post_selection_matches_get(self):
        """POST 只换理由不换题——两次拿到的题目与顺序必须一致。"""
        get_data = self.get_json(self.url(self.student.id), user=self.student,
                                 data={"size": 3}).json()["data"]
        post_data = self.post_json(self.url(self.student.id),
                                   {"provider": "mock", "size": 3},
                                   user=self.student).json()["data"]
        self.assertEqual([item["question_id"] for item in get_data["items"]],
                         [item["question_id"] for item in post_data["items"]])

    def test_service_and_view_agree(self):
        """接口返回的结构与服务层一致（视图只是套了一层信封）。"""
        service_payload = RecommendationService.daily_practice(self.student, size=2)
        view_data = self.get_json(self.url(self.student.id), user=self.student,
                                  data={"size": 2}).json()["data"]
        self.assertEqual(set(service_payload["summary"]), set(view_data["summary"]))
        self.assertEqual(set(service_payload["thresholds"]), set(view_data["thresholds"]))
        self.assertEqual(set(service_payload["items"][0]), set(view_data["items"][0]))

    def test_view_class_is_wired(self):
        self.assertEqual(DailyPracticeView.permission_classes[0].__name__, "IsAuthenticated")
