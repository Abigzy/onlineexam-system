"""学科隔离测试：教师只能看到 / 管理自己任教学科的资源。"""

from django.contrib.auth import get_user_model

from apps.common.scoping import teacher_subject, teacher_subject_scope
from apps.exam.tests.base import ExamBaseTest
from apps.questions.models import KnowledgePoint, Question

User = get_user_model()


class TeacherSubjectScopeTest(ExamBaseTest):
    """teacher_subject / teacher_subject_scope 纯函数 + 接口级验证。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.os_teacher = User.objects.create_user(
            username="os_t", email="os_t@example.com", password="Exam@123456",
            role="teacher", subject="操作系统", real_name="OS老师",
        )
        cls.net_teacher = User.objects.create_user(
            username="net_t", email="net_t@example.com", password="Exam@123456",
            role="teacher", subject="计算机网络", real_name="NET老师",
        )
        cls.kp_os = KnowledgePoint.objects.create(name="进程", subject="操作系统")
        cls.kp_net = KnowledgePoint.objects.create(name="IP协议", subject="计算机网络")
        cls.q_os = Question.objects.create(
            type="single", stem="OS 题", subject="操作系统", difficulty=2, score=2,
            reference_answer="A",
        )
        cls.q_os.knowledge_points.add(cls.kp_os)
        cls.q_net = Question.objects.create(
            type="single", stem="NET 题", subject="计算机网络", difficulty=2, score=2,
            reference_answer="A",
        )
        cls.q_net.knowledge_points.add(cls.kp_net)

    def test_teacher_subject_helper(self):
        self.assertEqual(teacher_subject(self.os_teacher), "操作系统")
        self.assertEqual(teacher_subject(self.teacher), "")  # 内置 teacher 未设学科 → 不限
        self.assertEqual(teacher_subject(self.admin), "")

    def test_scope_filters_by_subject(self):
        qs = teacher_subject_scope(self.os_teacher, Question.objects.all())
        self.assertEqual(list(qs), [self.q_os])

    def test_scope_passthrough_without_subject(self):
        # 未设学科的教师看到全部
        qs = teacher_subject_scope(self.teacher, Question.objects.all())
        self.assertIn(self.q_os, qs)
        self.assertIn(self.q_net, qs)

    def test_scope_admin_sees_all(self):
        qs = teacher_subject_scope(self.admin, Question.objects.all())
        self.assertIn(self.q_os, qs)
        self.assertIn(self.q_net, qs)

    def test_api_list_isolated(self):
        # os 教师只能看到自己学科的题
        resp = self.get_json("/api/v1/questions/", user=self.os_teacher)
        self.assertEqual(resp.status_code, 200)
        stems = [row["stem"] for row in resp.json()["data"]["results"]]
        self.assertIn("OS 题", stems)
        self.assertNotIn("NET 题", stems)

    def test_api_knowledge_point_tree_isolated(self):
        resp = self.get_json("/api/v1/knowledge-points/tree/", user=self.os_teacher)
        self.assertEqual(resp.status_code, 200)
        names = [n["name"] for n in resp.json()["data"]]
        self.assertIn("进程", names)
        self.assertNotIn("IP协议", names)

    def test_subjects_endpoint_isolated(self):
        """题库「按科目分类」接口也要遵守学科隔离。"""
        resp = self.get_json("/api/v1/questions/subjects/", user=self.os_teacher)
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]["results"]
        self.assertEqual([r["subject"] for r in rows], ["操作系统"])
