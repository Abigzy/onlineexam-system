"""AI 学情诊断测试：结构规整、缓存命中、降级兜底。

这里的重点不是「模型说了什么」（那是模型的事），而是**代码这一侧的三条纪律**：

1. **数字由代码算，话由模型说。** 诊断里的分档（优秀 / 良好 / 中等 / 待提升）
   必须由 ``_overall_level`` 决定，不能听模型自由发挥；
2. **模型输出不可信，必须逐字段容错。** 少字段、把 ``accuracy`` 写成 ``"42%"``、
   把 ``advice`` 写成字符串，都要被规整成同一套结构，下游永远拿到一样的字段；
3. **降级永不抛异常。** 模型不可用时退回本地规则诊断并标 ``degraded=True``——
   学生打开页面看到的永远是一份能看的诊断，而不是报错弹窗。
"""

from unittest.mock import patch

from apps.ai.exceptions import AIServiceError
from apps.analytics.constants import AI_REPORT_TTL_HOURS
from apps.analytics.models import StudentAnalysisReport
from apps.analytics.services import AIAnalysisService
from apps.common.constants import QuestionType
from apps.common.exceptions import BusinessError
from apps.users.models import User

from .base import AnalyticsBaseTest

PASSWORD = "Exam@123456"


class DiagnosisHelperTest(AnalyticsBaseTest):
    """纯函数层：分档、严重度、表格渲染、兜底诊断、字段容错。"""

    # ------------------------------------------------------------------
    def test_overall_level_bands(self):
        self.assertEqual(AIAnalysisService._overall_level(None), "数据不足")
        self.assertEqual(AIAnalysisService._overall_level(0.90), "优秀")
        self.assertEqual(AIAnalysisService._overall_level(0.70), "良好")
        self.assertEqual(AIAnalysisService._overall_level(0.60), "中等")
        self.assertEqual(AIAnalysisService._overall_level(0.30), "待提升")

    def test_severity_bands(self):
        self.assertEqual(AIAnalysisService._severity(0.20), "high")
        self.assertEqual(AIAnalysisService._severity(0.45), "medium")
        self.assertEqual(AIAnalysisService._severity(0.80), "low")
        # None → _to_float 归一成 0.0 → 落进最严重一档，而不是崩掉
        self.assertEqual(AIAnalysisService._severity(None), "high")
        # 字符串也能收（模型经常给 "42%" 这种）
        self.assertEqual(AIAnalysisService._severity("0.9"), "low")

    def test_accuracy_table_empty(self):
        self.assertEqual(AIAnalysisService._accuracy_table([]), "（暂无答题数据）")

    def test_accuracy_table_renders_sorted_rows(self):
        items = [
            {"full_path": "线性表", "answered": 2, "accuracy": 0.25},
            {"full_path": "数据结构", "answered": 6, "accuracy": 0.5},
        ]
        text = AIAnalysisService._accuracy_table(items)
        lines = text.splitlines()
        # 按答题数从多到少排：数据结构在前
        self.assertEqual(lines[0], "数据结构 | 6 | 50.0%")
        self.assertEqual(lines[1], "线性表 | 2 | 25.0%")

    # ------------------------------------------------------------------
    def test_fallback_diagnosis_without_data(self):
        mastery = {"summary": {"answered": 0, "accuracy": None},
                   "weak_points": [], "mastered": []}
        result = AIAnalysisService._fallback_diagnosis(mastery, "小明")

        self.assertIn("还没有可供分析", result["summary"])
        self.assertEqual(result["overall_level"], "数据不足")
        self.assertEqual(result["weak_points"], [])
        self.assertEqual(result["source"], "local")
        self.assertTrue(result["advice"])      # 空数据也要给一句建议，不能留白

    def test_fallback_diagnosis_lists_weak_points(self):
        mastery = {
            "summary": {"answered": 6, "accuracy": 0.4},
            "weak_points": [
                {"name": "线性表", "answered": 3, "accuracy": 0.33},
                {"name": "栈", "answered": 3, "accuracy": 0.5},
            ],
            "mastered": [{"name": "队列"}],
        }
        result = AIAnalysisService._fallback_diagnosis(mastery, "小红")

        self.assertEqual(result["overall_level"], "待提升")
        self.assertEqual([row["knowledge_point"] for row in result["weak_points"]],
                         ["线性表", "栈"])
        self.assertEqual(result["weak_points"][0]["severity"], "high")   # 0.33 < 0.4
        self.assertIn("队列", result["strengths"])

    # ------------------------------------------------------------------
    def test_normalize_tolerates_string_accuracy_and_string_advice(self):
        """模型把 accuracy 写成 ``"42%"``、把 advice 写成单个字符串——都要接得住。"""
        mastery = {"summary": {"answered": 6, "accuracy": 0.4},
                   "weak_points": [{"name": "线性表", "answered": 3, "accuracy": 0.33}],
                   "mastered": []}
        raw = {
            "summary": "整体偏弱",
            "overall_level": "随便写点什么",          # 非法档位 → 用代码算的兜底
            "weak_points": [{"knowledge_point": "线性表", "accuracy": "42%"}],
            "advice": "先复习线性表",                  # 字符串 → 收成单元素列表
        }
        result = AIAnalysisService._normalize_diagnosis(raw, mastery, "小刚")

        self.assertEqual(result["source"], "ai")
        self.assertEqual(result["summary"], "整体偏弱")
        self.assertEqual(result["overall_level"], "待提升")            # 兜底档位
        self.assertEqual(result["advice"], ["先复习线性表"])
        self.assertAlmostEqual(result["weak_points"][0]["accuracy"], 0.42, places=4)

    def test_normalize_accepts_plain_string_weak_point(self):
        mastery = {"summary": {"answered": 6, "accuracy": 0.4},
                   "weak_points": [], "mastered": []}
        raw = {"weak_points": ["线性表", "栈"]}
        result = AIAnalysisService._normalize_diagnosis(raw, mastery, "小刚")

        self.assertEqual([row["knowledge_point"] for row in result["weak_points"]],
                         ["线性表", "栈"])

    def test_normalize_fills_missing_fields_from_fallback(self):
        mastery = {
            "summary": {"answered": 6, "accuracy": 0.4},
            "weak_points": [{"name": "线性表", "answered": 3, "accuracy": 0.33}],
            "mastered": [{"name": "队列"}],
        }
        result = AIAnalysisService._normalize_diagnosis({}, mastery, "小刚")

        # 模型什么都没给 → 五个字段全用兜底补齐，结构仍然完整
        self.assertTrue(result["summary"])
        self.assertEqual(result["overall_level"], "待提升")
        self.assertEqual(result["weak_points"][0]["knowledge_point"], "线性表")
        self.assertEqual(result["strengths"], ["队列"])
        self.assertTrue(result["advice"])

    def test_normalize_handles_non_dict_payload(self):
        mastery = {"summary": {"answered": 6, "accuracy": 0.4},
                   "weak_points": [], "mastered": []}
        result = AIAnalysisService._normalize_diagnosis(["not", "a", "dict"],
                                                       mastery, "小刚")
        self.assertEqual(result["source"], "ai")
        self.assertTrue(result["summary"])


class DiagnosisFlowTest(AnalyticsBaseTest):
    """诊断链路：生成 → 落库 → 缓存命中 → 强制刷新 → 降级。"""

    def setUp(self):
        super().setUp()
        # 造一份「做对一半」的作答，保证有知识点、也有薄弱点
        self.judged_attempt(
            self.exam, student=self.student,
            correct_types={QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE},
        )

    # ------------------------------------------------------------------
    def test_diagnose_generates_and_persists(self):
        result = AIAnalysisService.diagnose(self.student)

        # 测试环境一律走 mock provider（见 settings 里 `if "test" in sys.argv`）
        self.assertEqual(result["provider"], "mock")
        self.assertTrue(result["is_mock"])
        self.assertFalse(result["degraded"])
        self.assertFalse(result["cached"])
        self.assertEqual(result["detail"]["source"], "ai")

        # 顶层被 _flatten 展平，页面少写一层取值
        for key in ("summary", "overall_level", "weak_points", "strengths", "advice"):
            self.assertIn(key, result)

        report = StudentAnalysisReport.objects.of_student(self.student).first()
        self.assertIsNotNone(report)
        self.assertEqual(report.scope_key, "all")
        self.assertEqual(report.provider, "mock")
        self.assertTrue(report.is_mock)
        self.assertEqual(report.sample_size, 6)

    def test_second_call_hits_cache(self):
        first = AIAnalysisService.diagnose(self.student)
        second = AIAnalysisService.diagnose(self.student)

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        # 缓存命中不该再落一行
        self.assertEqual(StudentAnalysisReport.objects.of_student(self.student).count(), 1)
        self.assertEqual(first["id"], second["id"])

    def test_force_regenerates_and_updates_same_row(self):
        first = AIAnalysisService.diagnose(self.student)
        forced = AIAnalysisService.diagnose(self.student, force=True)

        self.assertFalse(forced["cached"])
        self.assertEqual(forced["id"], first["id"])       # update_or_create，不新增行
        self.assertEqual(StudentAnalysisReport.objects.of_student(self.student).count(), 1)

    def test_stale_cache_regenerates(self):
        AIAnalysisService.diagnose(self.student)
        report = StudentAnalysisReport.objects.of_student(self.student).first()
        # 把生成时间往前推，越过 TTL
        StudentAnalysisReport.objects.filter(pk=report.pk).update(
            generated_at=report.generated_at.replace(year=report.generated_at.year - 5),
        )
        again = AIAnalysisService.diagnose(self.student)
        self.assertFalse(again["cached"])

    # ------------------------------------------------------------------
    def test_subject_scope_separates_cache(self):
        AIAnalysisService.diagnose(self.student)
        AIAnalysisService.diagnose(self.student, subject="数据结构")

        scopes = set(StudentAnalysisReport.objects.of_student(self.student)
                     .values_list("scope_key", flat=True))
        self.assertEqual(scopes, {"all", "subject:数据结构"})

    # ------------------------------------------------------------------
    def test_degrade_when_ai_unavailable(self):
        """模型调用抛 AIServiceError → 退化为本地规则诊断，绝不抛给调用方。"""
        with patch("apps.analytics.services.get_ai_client",
                   side_effect=AIServiceError("模拟网络故障")):
            result = AIAnalysisService.diagnose(self.student)

        self.assertTrue(result["degraded"])
        self.assertIn("模拟网络故障", result["degraded_reason"])
        self.assertEqual(result["detail"]["source"], "local")
        self.assertTrue(result["summary"])                 # 本地诊断也有正文
        # 降级结论仍然落库，下次命中缓存就是「本地」那一份
        report = StudentAnalysisReport.objects.of_student(self.student).first()
        self.assertEqual(report.provider, "local")

    def test_degrade_on_unexpected_exception(self):
        """连「意外异常」也要接住——学生端的学情页不能因为这个 500。"""
        with patch("apps.analytics.services.get_ai_client",
                   side_effect=RuntimeError("boom")):
            result = AIAnalysisService.diagnose(self.student)

        self.assertTrue(result["degraded"])
        self.assertEqual(result["detail"]["source"], "local")

    # ------------------------------------------------------------------
    def test_no_records_raises_business_error(self):
        blank = User.objects.create_user(
            username="an_blank", email="an_blank@example.com", password=PASSWORD,
            role="student", real_name="空白考生", student_no="A2026099",
        )
        with self.assertRaises(BusinessError) as ctx:
            AIAnalysisService.diagnose(blank)
        self.assertEqual(ctx.exception.biz_code, 14001)
        self.assertIn("答题记录", str(ctx.exception.detail))

    def test_ttl_constant_positive(self):
        # 缓存有效期是个正数小时数，避免有人手滑改成 0 导致每次都打模型
        self.assertGreater(AI_REPORT_TTL_HOURS, 0)


__all__ = ["DiagnosisFlowTest", "DiagnosisHelperTest"]
