"""学情分析接口测试：权限边界 + 链路可用性。

和模块五的接口测试同一个重点——**这里主要在问「谁能碰什么」**：

  * 班级维度（分数分布 / 逐题区分度）是教师专属，学生一律 403；
  * 学生维度的「跨人」返回 **404 而不是 403**：403 等于承认「这个 id 存在，
    只是不给你」，对一个编号就能穷举的系统来说是免费的信息泄漏；
  * 非学生 id（教师 / 管理员的 id）来探学情，也统一按「学生不存在」处理；
  * GET 个人学情总览**不会自动调大模型**，只有 POST diagnosis 才会——这是花钱的动作。
"""

from django.contrib.auth import get_user_model

from apps.analytics.models import StudentAnalysisReport
from apps.classroom.constants import ClassType
from apps.classroom.models import Classroom
from apps.common.constants import Role
from apps.exam.models import Exam

from .base import AnalyticsBaseTest


class AnalyticsApiBase(AnalyticsBaseTest):
    def setUp(self):
        super().setUp()
        # 一场有作答（且已判分）的考试 —— 班级维度的所有接口都依赖它
        # （self.exam 是基类的只读缓存属性，直接访问即可，不要赋值）
        self.judged_attempt(self.exam, student=self.student)
        self.judged_attempt(self.exam, student=self.student_b)
        # 学情收紧后，教师只能看**自己带的班**里的学生。这里给教师建一个班并纳入
        # 两名学生，让「教师看学生学情」这个前提成立；越权用例在下面单独造班外学生。
        classroom = Classroom.objects.create(
            name="学情测试班", class_type=ClassType.ADMIN,
            created_by=self.teacher, head_teacher=self.teacher,
        )
        classroom.students.add(self.student, self.student_b)

    # 常用 URL
    @property
    def exams_url(self):
        return "/api/v1/analytics/exams/"

    @property
    def report_url(self):
        return f"/api/v1/analytics/exams/{self.exam.id}/report/"

    @property
    def questions_url(self):
        return f"/api/v1/analytics/exams/{self.exam.id}/questions/"

    def student_url(self, student, leaf):
        return f"/api/v1/analytics/students/{student.id}/{leaf}/"


# ==================================================================
# 班级维度（教师专属）
# ==================================================================
class ExamAnalyticsApiTest(AnalyticsApiBase):
    def test_exam_list_returns_options(self):
        response = self.get_json(self.exams_url, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertGreaterEqual(data["count"], 1)
        self.assertIn(self.exam.id, [row["id"] for row in data["results"]])

    def test_exam_report_shape(self):
        response = self.get_json(self.report_url, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        for key in ("exam", "participation", "distribution", "behavior",
                    "weak_knowledge_points", "questions", "ranking"):
            self.assertIn(key, data)
        # 默认不带逐人明细（成绩单已有一份，省响应体）
        self.assertNotIn("rows", data)

    def test_exam_report_include_rows(self):
        response = self.get_json(f"{self.report_url}?include_rows=true", user=self.teacher)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]["rows"]), 2)

    def test_exam_questions_endpoint(self):
        response = self.get_json(self.questions_url, user=self.teacher)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["summary"]["question_count"], 6)

    def test_unknown_exam_returns_404(self):
        response = self.get_json("/api/v1/analytics/exams/999999/report/", user=self.teacher)
        self.assertEqual(response.status_code, 404)

    # ------------------------------------------------------------------
    def test_student_cannot_access_class_dimension(self):
        for url in (self.exams_url, self.report_url, self.questions_url):
            with self.subTest(url=url):
                self.assertEqual(self.get_json(url, user=self.student).status_code, 403)

    def test_anonymous_cannot_access_class_dimension(self):
        response = self.client.get(self.report_url)
        self.assertIn(response.status_code, (401, 403))


# ==================================================================
# 学生维度
# ==================================================================
class StudentAnalyticsApiTest(AnalyticsApiBase):
    def test_teacher_can_view_student_in_own_class(self):
        """教师能看自己班的学生（本类的 setUp 已把 student / student_b 纳入该教师的班）。"""
        response = self.get_json(self.student_url(self.student, "mastery"), user=self.teacher)
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.json()["data"])

    def test_teacher_cannot_view_student_outside_own_class(self):
        """收紧后：不在自己班的学生，各接口一律 404（不拿 403 当 id 探针）。"""
        outsider = get_user_model().objects.create_user(
            username="outsider_stu", password="Exam@123456", role=Role.STUDENT,
        )
        for leaf in ("mastery", "trend", "report"):
            with self.subTest(leaf=leaf):
                response = self.get_json(self.student_url(outsider, leaf), user=self.teacher)
                self.assertEqual(response.status_code, 404)

    def test_teacher_page_denied_for_student_outside_own_class(self):
        """页面与接口同口径，否则会出现「页面能打开、数据全是 404」的割裂体验。"""
        outsider = get_user_model().objects.create_user(
            username="outsider_stu2", password="Exam@123456", role=Role.STUDENT,
        )
        self.client.force_login(self.teacher)
        response = self.client.get(f"/analytics/students/{outsider.id}/")
        self.assertEqual(response.status_code, 404)

    def test_teacher_sees_student_of_class_he_only_teaches(self):
        """授课教师（不在班主任位）同样能看该班学生。"""
        other_teacher = get_user_model().objects.create_user(
            username="lecturer", password="Exam@123456", role=Role.TEACHER,
        )
        classroom = Classroom.objects.create(
            name="授课班", class_type=ClassType.SUBJECT, subject="数据结构",
            created_by=self.teacher,
        )
        classroom.teachers.add(other_teacher)
        classroom.students.add(self.student)
        response = self.get_json(self.student_url(self.student, "mastery"), user=other_teacher)
        self.assertEqual(response.status_code, 200)

    def test_admin_can_view_any_student(self):
        admin = get_user_model().objects.create_user(
            username="scope_admin", password="Exam@123456", role=Role.ADMIN,
        )
        response = self.get_json(self.student_url(self.student, "mastery"), user=admin)
        self.assertEqual(response.status_code, 200)

    # ------------------------------------------------------------------
    # 班级维度：只能统计自己班的学生
    # ------------------------------------------------------------------
    def test_class_report_scoped_to_own_students(self):
        """open_to 收紧后，班外学生进不了考试，班级统计自然不含他们。"""
        outsider = get_user_model().objects.create_user(
            username="outsider_cls", password="Exam@123456", role=Role.STUDENT,
        )
        self.assertFalse(self.exam.open_to(outsider))
        # 「面向全体」不再是全校：应考人数应少于在册学生总数
        total = get_user_model().objects.filter(role=Role.STUDENT, is_active=True).count()
        self.assertLess(self.exam.participant_count(), total)

    def test_class_report_rejects_foreign_classroom(self):
        """classroom_id 必须是自己的班，否则 404（不拿 403 探测别人的班）。"""
        others = Classroom.objects.create(name="别人的班", class_type=ClassType.ADMIN)
        response = self.get_json(f"{self.report_url}?classroom_id={others.id}", user=self.teacher)
        self.assertEqual(response.status_code, 404)

    def test_attempts_mark_unviewable_student(self):
        """作答列表带上「能否查看学情」标记；班外学生进不了考试，不会出现在列表里。"""
        outsider = get_user_model().objects.create_user(
            username="outsider_mark", password="Exam@123456", role=Role.STUDENT,
        )
        data = self.get_json(f"/api/v1/exams/{self.exam.id}/attempts/", user=self.teacher).json()["data"]
        rows = {row["student_id"]: row for row in data["results"]}
        self.assertIn(self.student.id, rows)
        self.assertTrue(rows[self.student.id]["can_view_analytics"])
        self.assertNotIn(outsider.id, rows)

    def test_student_can_view_self(self):
        response = self.get_json(self.student_url(self.student, "report"), user=self.student)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["student"]["id"], self.student.id)
        self.assertIn("mastery", data)
        self.assertIn("trend", data)
        self.assertIn("thresholds", data)

    def test_student_cannot_view_other_student(self):
        """跨人取 404 而不是 403 —— 不给出「这个 id 存在」的信号。"""
        url = self.student_url(self.student_b, "mastery")
        response = self.get_json(url, user=self.student)
        self.assertEqual(response.status_code, 404)

    def test_non_student_target_is_404(self):
        """拿教师 id 来探学情，一律当「学生不存在」。"""
        url = self.student_url(self.teacher, "report")
        self.assertEqual(self.get_json(url, user=self.teacher).status_code, 404)

    # ------------------------------------------------------------------
    def test_report_does_not_auto_call_ai(self):
        """GET 总览不生成诊断，只回传已有缓存（此时还没有）——避免刷新变计费器。"""
        response = self.get_json(self.student_url(self.student, "report"), user=self.student)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"]["diagnosis"])
        self.assertEqual(StudentAnalysisReport.objects.of_student(self.student).count(), 0)

    def test_diagnosis_post_then_cached(self):
        url = self.student_url(self.student, "diagnosis")

        first = self.post_json(url, {}, user=self.student)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["data"]["cached"])
        self.assertIn("已生成", first.json()["message"])
        self.assertEqual(StudentAnalysisReport.objects.of_student(self.student).count(), 1)

        second = self.post_json(url, {}, user=self.student)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["data"]["cached"])
        self.assertIn("缓存", second.json()["message"])

    def test_diagnosis_persists_into_report(self):
        self.post_json(self.student_url(self.student, "diagnosis"), {}, user=self.student)
        report = self.get_json(self.student_url(self.student, "report"), user=self.student)
        diagnosis = report.json()["data"]["diagnosis"]
        self.assertIsNotNone(diagnosis)
        self.assertIn("summary", diagnosis)       # _flatten 提到顶层
        self.assertIn("advice", diagnosis)

    def test_diagnosis_cross_student_404(self):
        url = self.student_url(self.student_b, "diagnosis")
        self.assertEqual(self.post_json(url, {}, user=self.student).status_code, 404)

    # ------------------------------------------------------------------
    def test_invalid_limit_returns_400(self):
        response = self.get_json(
            f"{self.student_url(self.student, 'trend')}?limit=abc", user=self.student,
        )
        self.assertEqual(response.status_code, 400)

    def test_limit_above_max_returns_400(self):
        response = self.get_json(
            f"{self.student_url(self.student, 'trend')}?limit=999", user=self.student,
        )
        self.assertEqual(response.status_code, 400)

    def test_trend_endpoint_shape(self):
        response = self.get_json(self.student_url(self.student, "trend"), user=self.student)
        self.assertEqual(response.status_code, 200)
        self.assertIn("points", response.json()["data"])


# ==================================================================
# 边界：班级维度里没有作答的考试
# ==================================================================
class EmptyExamAnalyticsTest(AnalyticsBaseTest):
    def test_exam_without_attempts_not_listed(self):
        # 一场没有任何作答的考试不该出现在「可分析的考试」下拉里
        exam = Exam.objects.create(
            title="空考试", paper_id=self.make_paper().id, created_by=self.teacher,
            duration_minutes=30,
        )
        response = self.get_json("/api/v1/analytics/exams/", user=self.teacher)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(exam.id, [row["id"] for row in response.json()["data"]["results"]])


__all__ = [
    "AnalyticsApiBase",
    "EmptyExamAnalyticsTest",
    "ExamAnalyticsApiTest",
    "StudentAnalyticsApiTest",
]
