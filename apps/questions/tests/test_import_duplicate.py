"""题库导入判重测试。

规则：按「学科 + 题型 + 归一化题干」判重，重复行跳过（不报错、不中断整批），
并且必须在返回结果里如实列出跳过项，让教师知道第几行被跳过。
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.questions.importers import build_template_csv, normalize_stem, write_csv
from apps.questions.models import Question

User = get_user_model()
PASSWORD = "Exam@123456"
URL = "/api/v1/questions/import/"


def _header():
    return build_template_csv().decode("utf-8-sig").splitlines()[0].split(",")


class ImportDuplicateTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(
            username="dup_teacher", email="dup_teacher@example.com",
            password=PASSWORD, role="teacher", real_name="判重老师",
        )

    def setUp(self):
        cache.clear()

    def auth(self):
        token = self.client.post(
            "/api/v1/auth/login/",
            {"username": self.teacher.username, "password": PASSWORD},
            content_type="application/json",
        ).json()["data"]["access"]
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    # --------------------------------------------------------------
    @staticmethod
    def row(stem, *, subject="数据结构", qtype="单选题", answer="B",
            knowledge="二叉树", option_a="甲", option_b="乙"):
        return [knowledge, "二叉树", subject, qtype, "中等", "2", stem,
                option_a, option_b, "", "", "", "", answer, ""]

    def post_csv(self, rows, **extra):
        """用真实模板表头拼 CSV 并上传。"""
        content = write_csv([_header()] + rows)
        upload = SimpleUploadedFile("questions.csv", content, content_type="text/csv")
        payload = {"file": upload}
        payload.update(extra)
        return self.client.post(URL, payload, **self.auth())


# ==================================================================
# 归一化函数
# ==================================================================
class NormalizeTest(ImportDuplicateTestBase):
    def test_normalize_stem_ignores_whitespace_and_fullwidth(self):
        self.assertEqual(normalize_stem("二叉树（中序） 遍历"), normalize_stem("二叉树(中序)遍历"))
        self.assertEqual(normalize_stem("  A  B\u3000C "), "abc")

    def test_normalize_keeps_chinese_unchanged(self):
        self.assertEqual(normalize_stem("冒泡排序"), "冒泡排序")


# ==================================================================
# 文件内 / 库内判重
# ==================================================================
class ImportDedupeApiTest(ImportDuplicateTestBase):
    def test_same_stem_twice_in_one_file_keeps_first(self):
        resp = self.post_csv([
            self.row("二叉树的先序遍历顺序是什么？"),
            self.row("二叉树的先序遍历顺序是什么？"),
        ])
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["created_count"], 1)
        self.assertEqual(Question.objects.count(), 1)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(data["skipped"][0]["reason"], "batch")
        self.assertEqual(data["skipped"][0]["row"], 3)  # 表头第 1 行，重复的是第 3 行

    def test_stem_already_in_db_is_skipped(self):
        Question.objects.create(
            type="single", stem="堆排序的平均时间复杂度是多少？",
            subject="数据结构", reference_answer="A",
        )
        resp = self.post_csv([self.row("堆排序的平均时间复杂度是多少？")])
        data = resp.json()["data"]
        self.assertEqual(data["created_count"], 0)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(data["skipped"][0]["reason"], "db")
        self.assertEqual(Question.objects.count(), 1)

    def test_whitespace_and_fullwidth_parens_treated_as_duplicate(self):
        Question.objects.create(
            type="single", stem="下列（  ）属于线性结构。",
            subject="数据结构", reference_answer="A",
        )
        resp = self.post_csv([self.row("  下列( )属于线性结构。  ")])
        data = resp.json()["data"]
        self.assertEqual(data["created_count"], 0)
        self.assertEqual(data["skipped_count"], 1)

    def test_different_type_same_stem_both_created(self):
        Question.objects.create(
            type="single", stem="栈的特点是后进先出。",
            subject="数据结构", reference_answer="A",
        )
        resp = self.post_csv([
            self.row("栈的特点是后进先出。", qtype="判断题", answer="A"),
        ])
        data = resp.json()["data"]
        self.assertEqual(data["created_count"], 1)
        self.assertEqual(data["skipped_count"], 0)

    def test_different_subject_same_stem_both_created(self):
        Question.objects.create(
            type="single", stem="请说明该概念的适用场景。",
            subject="数据结构", reference_answer="A",
        )
        resp = self.post_csv([self.row("请说明该概念的适用场景。", subject="操作系统")])
        data = resp.json()["data"]
        self.assertEqual(data["created_count"], 1)
        self.assertEqual(data["skipped_count"], 0)

    def test_inactive_question_still_counts_as_duplicate(self):
        Question.objects.create(
            type="single", stem="已停用的旧题仍然算重复。",
            subject="数据结构", reference_answer="A", is_active=False,
        )
        resp = self.post_csv([self.row("已停用的旧题仍然算重复。")])
        data = resp.json()["data"]
        self.assertEqual(data["created_count"], 0)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(data["skipped"][0]["reason"], "db")

    def test_dry_run_reports_skipped_without_writing(self):
        Question.objects.create(
            type="single", stem="试跑模式也要给出跳过明细。",
            subject="数据结构", reference_answer="A",
        )
        resp = self.post_csv([
            self.row("试跑模式也要给出跳过明细。"),
            self.row("这条是新题，试跑不会入库。"),
        ], dry_run="true")
        data = resp.json()["data"]
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["created_count"], 0)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(data["skipped"][0]["row"], 2)
        self.assertEqual(Question.objects.count(), 1)  # 只有 setUp 里那一条

    def test_response_fields_stay_backward_compatible(self):
        resp = self.post_csv([self.row("字段兼容性检查用的题干。")])
        data = resp.json()["data"]
        for field in ("total_rows", "parsed", "created_count", "created_ids",
                      "dry_run", "errors", "error_count"):
            self.assertIn(field, data)
        self.assertIn("skipped_count", data)
        self.assertIn("skipped", data)
        self.assertEqual(data["total_rows"], 1)
        self.assertEqual(data["parsed"], 1)
        self.assertEqual(len(data["created_ids"]), 1)
        self.assertEqual(data["error_count"], 0)

    def test_batch_duplicate_reported_before_db_duplicate(self):
        """同一批里出现第 2 次时连库都没进，reason 必须是 batch 而不是 db。"""
        resp = self.post_csv([
            self.row("先出现的那条会被保留。"),
            self.row("先出现的那条会被保留。"),
            self.row("先出现的那条会被保留。"),
        ])
        data = resp.json()["data"]
        self.assertEqual(data["created_count"], 1)
        self.assertEqual([s["reason"] for s in data["skipped"]], ["batch", "batch"])
        self.assertEqual([s["row"] for s in data["skipped"]], [3, 4])

    def test_import_twice_only_first_batch_lands(self):
        """真实回归：同一份含 2 条相同题干的文件导入两次，第二次全部跳过。"""
        rows = [
            self.row("重复导入的第一条题干。"),
            self.row("重复导入的第一条题干。"),
        ]
        first = self.post_csv(rows).json()["data"]
        self.assertEqual(first["created_count"], 1)
        self.assertEqual(first["skipped_count"], 1)

        second = self.post_csv(rows).json()
        self.assertEqual(second["data"]["created_count"], 0)
        self.assertEqual(second["data"]["skipped_count"], 2)
        self.assertEqual(Question.objects.count(), 1)
        self.assertIn("跳过 2 道重复", second["message"])
        self.assertIn("已导入 0 道题", second["message"])
