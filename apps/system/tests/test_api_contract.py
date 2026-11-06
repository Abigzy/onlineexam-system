"""模块 11 · 接口测试：全量接口的准入矩阵。

## 这份用例想解决什么

前 10 个模块的 1000+ 条用例已经覆盖了大量**业务分支**，但缺一层横向的
「**逐接口的准入矩阵**」：新加一个接口、或把某个 ``permission_classes`` 改错，
只要没人专门写用例，就不会有人发现；手工枚举接口又必然漏。

所以这里从路由表反推用例：

1. 用 ``django.urls.get_resolver()`` 遍历 ``/api/v1/`` 下的**全部** URL pattern
   （含 DRF router 自动生成的 list / detail / ``@action``），得到「路径 × 方法」
   的完整操作清单——路由加了新接口，这里自动就能看到；
2. 给每个操作标注**准入级别**（公开 / 登录 / 学生 / 教师 / 管理员），
   再对匿名 / 学生 / 教师 / 管理员四种身份逐一断言；
3. 声明式清单与路由表**双向校验**（``test_contract_covers_every_route``）：
   路由里多出一个未声明的操作 → 失败；清单里留着一个已删除的操作 → 也失败。
   于是「漏测」和「测了个不存在的接口」都会立刻暴露。

## 三类必覆盖的检查

* **认证**：匿名访问受保护接口一律 401；公开白名单只有注册 / 登录 / 刷新三条，
  在 ``PUBLIC_OPERATIONS`` 里显式列出。
* **权限边界**：学生访问教师 / 管理员专属接口 → 403 或 404（**按各接口现有口径**，
  不强行统一）；管理员访问教师接口应当放行。
* **参数校验**：非法 pk、缺必填字段、``page=-1`` 等 → 400 / 404，**绝不能 500**。

## 关于「按各接口现有口径」

本项目有两套刻意的口径，用例分别照抄，不做统一：

* 跨人 / 跨身份的资源（考试、作答、成绩、学情、推荐）越权取 **404**，
  避免 403 变成「这个 id 存不存在」的探针；
* 班级类资源（id 不敏感）越权取 **403 + code 14003**，让误点别人班级的老师
  看到「权限不足」而不是「不存在」。

## 隔离

每个操作都在 ``transaction.atomic()`` 里跑完即回滚（``set_rollback(True)``）。
矩阵里既有 ``DELETE`` 也有「交卷」「发布」这类会改状态的动作，不回滚的话后一个
操作看到的就是前一个操作留下的世界——第一版矩阵正因为此把大半接口测成了 404。
"""

import json
import os
import re

from django.core.cache import cache
from django.db import transaction
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver

from apps.classroom.models import Classroom
from apps.exam.services import ExamAttemptService
from apps.exam.tests.base import PASSWORD, SUBJECT, ExamBaseTest
from apps.grading.models import GradingFeedback
from apps.grading.services import GradingService

# ==================================================================
# 准入级别
# ==================================================================
PUBLIC = "public"      # 无需登录
AUTH = "auth"          # 任意已登录用户
STUDENT = "student"    # 仅学生（教师 / 管理员也不行）
TEACHER = "teacher"    # 教师或管理员
ADMIN = "admin"        # 仅管理员

#: 放开给匿名的白名单。只放这三条——本项目的 ``DEFAULT_PERMISSION_CLASSES``
#: 是 ``IsAuthenticated``，其余接口（含 DRF 的 API 索引页）一律要登录。
#: ``/api/health/`` 不在 ``/api/v1/`` 前缀下，由 ``test_health_is_public`` 单独覆盖。
PUBLIC_OPERATIONS = {
    ("register", "POST"),
    ("login", "POST"),
    ("refresh", "POST"),
}

# ==================================================================
# 声明式准入清单
# ==================================================================
# key 为路由 name；默认对所有方法生效，个别方法用 ACCESS_OVERRIDES 覆写。
_DEFAULT_ACCESS = {
    # ---- 认证与用户 ----
    "register": PUBLIC,
    "login": PUBLIC,
    "refresh": PUBLIC,
    "logout": AUTH,
    "me": AUTH,
    "change-password": AUTH,
    "user-list": ADMIN,
    "user-detail": ADMIN,
    "user-students": ADMIN,
    "user-teachers": ADMIN,
    "user-reset-password": ADMIN,
    "user-toggle-active": ADMIN,
    # ---- AI 适配层 ----
    "ai-chat": TEACHER,
    "ai-logs": TEACHER,
    "ai-providers": TEACHER,
    "ai-test-connection": TEACHER,
    "ai-usage": TEACHER,
    # ---- 题库 ----
    "knowledge-point-list": TEACHER,
    "knowledge-point-detail": TEACHER,
    "knowledge-point-tree": TEACHER,
    "knowledge-point-questions": TEACHER,
    "question-list": TEACHER,
    "question-detail": TEACHER,
    "question-ai-generate": TEACHER,
    "question-batch": TEACHER,
    "question-ai-explain": TEACHER,
    "question-ai-grade": TEACHER,
    "question-import-questions": TEACHER,
    "question-import-template": TEACHER,
    "question-export": TEACHER,
    "question-statistics": TEACHER,
    "question-subjects": TEACHER,
    # ---- 试卷与组卷 ----
    "paper-list": TEACHER,
    "paper-detail": TEACHER,
    "paper-status": TEACHER,
    "paper-preview": TEACHER,
    "paper-export": TEACHER,
    "paper-clone": TEACHER,
    "paper-add-questions": TEACHER,
    "paper-remove-questions": TEACHER,
    "paper-reorder": TEACHER,
    "paper-set-scores": TEACHER,
    "paper-analysis": TEACHER,
    "paper-ai-notes": TEACHER,
    "paper-ai-compose": TEACHER,
    "paper-random-compose": TEACHER,
    "paper-pool": TEACHER,
    "paper-statistics": TEACHER,
    # ---- 在线考试 ----
    "exam-list": AUTH,          # 教师看全部、学生看「我的考试」，同一个入口
    "exam-detail": AUTH,        # 学生也能看概要（不含卷面）
    "exam-start": STUDENT,
    "exam-status": TEACHER,
    "exam-overview": TEACHER,
    "exam-attempts": TEACHER,
    "exam-statistics": TEACHER,
    "exam-candidates": TEACHER,
    "attempt-list": AUTH,
    "attempt-detail": AUTH,
    "attempt-paper": STUDENT,
    "attempt-answers": AUTH,    # GET 教师可看；POST 在视图里再卡一次「只有考生本人」
    "attempt-save": STUDENT,
    "attempt-heartbeat": STUDENT,
    "attempt-behavior": STUDENT,
    "attempt-submit": STUDENT,
    "attempt-result": AUTH,
    # ---- 判分与成绩 ----
    "grading-attempt-grade": TEACHER,
    "grading-attempt-finalize": TEACHER,
    "grading-answer-review": TEACHER,
    "grading-queue": TEACHER,
    "grading-exams": TEACHER,
    "grading-feedback-list": TEACHER,
    "grading-feedback-detail": TEACHER,
    "grading-attempt-report": AUTH,
    "grading-scoreboard": TEACHER,
    "grading-wrong-book": AUTH,
    "grading-calibration": TEACHER,
    # ---- 学情分析 ----
    "analytics-exams": TEACHER,
    "analytics-exam-report": TEACHER,
    "analytics-exam-questions": TEACHER,
    "analytics-student-mastery": AUTH,
    "analytics-student-trend": AUTH,
    "analytics-student-report": AUTH,
    "analytics-student-diagnosis": AUTH,
    # ---- 个性化推荐 ----
    "recommend-student-practice": AUTH,
    # ---- 作弊行为检测 ----
    "proctoring-exams": TEACHER,
    "proctoring-exam-report": TEACHER,
    "proctoring-attempt-evidence": TEACHER,
    "proctoring-rules": TEACHER,
    # ---- 班级 ----
    "classroom-list": AUTH,     # GET 学生可看自己所在班；POST 在视图里要求教师
    "classroom-options": TEACHER,
    "classroom-detail": AUTH,   # GET 学生可看；写操作在视图里要求可管理者
    "classroom-members": AUTH,  # GET 学生可看；增删在视图里要求教师
    # ---- 管理控制台 ----
    "system-overview": ADMIN,
    # ---- DRF router 自动生成的 API 索引页 ----
    "api-root": AUTH,
}

#: 逐个方法覆写（键为 ``"name:METHOD"``）。
ACCESS_OVERRIDES = {
    "exam-list:POST": TEACHER,
    "exam-detail:PUT": TEACHER,
    "exam-detail:PATCH": TEACHER,
    "exam-detail:DELETE": TEACHER,
    "attempt-answers:POST": STUDENT,
    "classroom-list:POST": TEACHER,
    "classroom-detail:PUT": TEACHER,
    "classroom-detail:PATCH": TEACHER,
    "classroom-detail:DELETE": TEACHER,
    "classroom-members:POST": TEACHER,
    "classroom-members:DELETE": TEACHER,
}

#: 项目里**真的写了契约**的几处越权口径：``{((name, method), actor): (status, code)}``。
#: 其余按「403 或 404 都收」处理，不做统一。
#:
#: 只列「走到了业务层」的几处（班级详情写操作 → 14003）。像 ``classroom-members``
#: 的增删，学生是在**权限类** ``IsTeacher`` 那一层就被挡下的（40300），
#: 根本没进视图，所以没有业务码可言——照实记 40300。
EXPLICIT_DENY_CODES = {
    (("classroom-detail", "DELETE"), "student"): (403, 14003),
    (("classroom-detail", "PATCH"), "student"): (403, 14003),
    (("classroom-detail", "PUT"), "student"): (403, 14003),
    (("classroom-options", "GET"), "student"): (403, 40300),
}

#: 需要 query 参数才「讲道理」的接口。不传会先撞 400/404，掩盖真正的准入结论。
QUERY_PARAMS = {
    "grading-queue": "?exam_id={exam}",
    "grading-scoreboard": "?exam_id={exam}",
    "grading-wrong-book": "?student_id={student}",
    "proctoring-exam-report": "?only_suspicious=false",
}

#: 明确记下「哪些 pattern 不进矩阵、为什么」。当前为空 = ``/api/v1/`` 全覆盖。
#: 增补时写成 ``{"<pattern>": "<原因>"}``，报告里会原样引用。
SKIPPED_PATTERNS = {}

#: 拒绝访问时可接受的状态码。403 与 404 的取舍见模块 docstring。
DENIED = (403, 404)

#: 参数校验失败时可接受的状态码（含 DRF 分页非法页抛的 404）。
BAD_REQUEST = (400, 404, 422)


def _walk_patterns(patterns, prefix=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _walk_patterns(pattern.url_patterns, prefix + str(pattern.pattern))
        elif isinstance(pattern, URLPattern):
            yield prefix + str(pattern.pattern), pattern


def _methods_of(pattern):
    """该 pattern 支持的方法（**不含 HEAD / OPTIONS**）。

    注意 ``ViewSetMixin.as_view()`` 会自动往 action 映射里塞一条
    ``"head": "<同名 action>"``（DRF 让 HEAD 与 GET 共用处理器）。若不过滤，
    矩阵里会凭空多出一倍 HEAD 请求，而 HEAD 的响应体是空的，断言会读到 None。
    """
    ignored = {"head", "options"}
    callback = pattern.callback
    actions = getattr(callback, "actions", None)
    if actions:
        return sorted({method.upper() for method in actions if method not in ignored})
    cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
    if cls is None:
        return []
    return [
        method.upper()
        for method in cls.http_method_names
        if method not in ignored and hasattr(cls, method)
    ]


def _strip_anchors(raw):
    """去掉 pattern 里的 ``^`` / ``$`` 锚点，只留可请求的路径。

    DRF router 注册出来的 pattern 形如 ``api/v1/exams/^(?P<pk>[^/.]+)/$``：
    ``^`` 是段首锚点、``$`` 是结尾锚点，都**不是** URL 的一部分，必须剥掉，
    否则请求会打到 ``/api/v1/^exams/`` 上、整张矩阵变成一片 404。
    而 ``[^/.]+`` 里的 ``^`` 是正则取反符，不能误伤——所以只删「紧跟 / 的 ^」。
    """
    path = raw.lstrip("^")
    path = re.sub(r"(?<=/)\^", "", path)         # 段内锚点
    return path.rstrip("$")                      # 结尾锚点


def _build_path(raw, params):
    """把路由 pattern 还原成可直接请求的 URL（路径参数换成真实实例 id）。"""
    path = _strip_anchors(raw)
    path = re.sub(r"\(\?P<(\w+)>[^)]*\)", lambda m: str(params[m.group(1)]), path)
    path = re.sub(r"<(?:\w+:)?(\w+)>", lambda m: str(params[m.group(1)]), path)
    return "/" + path


class ApiContractTest(ExamBaseTest):
    """全量接口的准入矩阵。"""

    #: 矩阵累积：``{(name, method, url): {actor: (status, code)}}``，供导出用。
    MATRIX = {}
    #: 参数校验结果：``{(name, method): (status, code)}``。
    PARAM_MATRIX = {}

    # ------------------------------------------------------------------
    # 装配：一套「每个接口都能答话」的数据
    # ------------------------------------------------------------------
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        # 下面几个 ``ExamBaseTest`` 的装配工具本来是实例方法（只读 ``self.teacher`` /
        # ``self.questions`` 这些类属性），在 ``setUpTestData`` 里显式按「未绑定函数」
        # 调用一次，把 ``cls`` 当 self 传进去——这样整套数据只建一次，12 个用例共享。
        cls.paper = ExamBaseTest.make_paper(cls)
        cls.exam = ExamBaseTest.make_exam(cls, paper=cls.paper)

        # 作答中：学生侧暂存 / 心跳 / 行为 / 卷面 / 交卷都要有一个活着的 attempt
        cls.live_attempt = ExamBaseTest.start(cls, cls.exam, student=cls.student)

        # 已判分：成绩单 / 学情 / 作弊检测 / 复核记录都要读它
        cls.judged_attempt = cls._make_judged_attempt()

        # 班级：两个学生都在里面，教师是班主任（学生可读、教师可管）
        cls.classroom = Classroom.objects.create(
            name="接口矩阵班", subject=SUBJECT, head_teacher=cls.teacher,
            class_type="subject",
        )
        cls.classroom.students.add(cls.student, cls.student_b)
        cls.classroom.teachers.add(cls.teacher)
        cls.classroom.save()

        # 复核记录：给 grading-feedback-detail 一个真实存在的主键
        answer = cls.judged_attempt.answers.first()
        cls.feedback = GradingFeedback.objects.create(
            answer=answer, attempt=cls.judged_attempt, reviewer=cls.teacher,
            original_score=answer.score or 0, final_score=answer.score or 0,
            full_score=answer.paper_question.score or 0,
            action="accept", comment="接口矩阵用",
        )

    @classmethod
    def _make_judged_attempt(cls):
        """``student_b`` 走一遍「开考 → 作答 → 交卷 → 判分」。"""
        attempt = ExamBaseTest.start(cls, cls.exam, student=cls.student_b)
        ExamAttemptService.save_answers(attempt, ExamBaseTest.answer_payload(cls, attempt))
        ExamAttemptService.submit(attempt)
        attempt.refresh_from_db()
        GradingService.grade_attempt(attempt, with_ai=False)
        return attempt.__class__.objects.get(pk=attempt.pk)

    def setUp(self):
        super().setUp()
        cache.clear()

    # ------------------------------------------------------------------
    # 路径参数：每个带参路由该用哪个实例
    # ------------------------------------------------------------------
    def _params(self):
        return {
            "user-detail": {"pk": self.student_b.id},
            "user-reset-password": {"pk": self.student_b.id},
            "user-toggle-active": {"pk": self.student_b.id},
            "knowledge-point-detail": {"pk": self.kp.id},
            "knowledge-point-questions": {"pk": self.kp.id},
            "question-detail": {"pk": self.questions["single"].id},
            "question-ai-explain": {"pk": self.questions["single"].id},
            "question-ai-grade": {"pk": self.questions["short"].id},
            "paper-detail": {"pk": self.paper.id},
            "paper-add-questions": {"pk": self.paper.id},
            "paper-ai-notes": {"pk": self.paper.id},
            "paper-analysis": {"pk": self.paper.id},
            "paper-clone": {"pk": self.paper.id},
            "paper-export": {"pk": self.paper.id},
            "paper-preview": {"pk": self.paper.id},
            "paper-remove-questions": {"pk": self.paper.id},
            "paper-reorder": {"pk": self.paper.id},
            "paper-set-scores": {"pk": self.paper.id},
            "paper-status": {"pk": self.paper.id},
            "exam-detail": {"pk": self.exam.id},
            "exam-attempts": {"pk": self.exam.id},
            "exam-overview": {"pk": self.exam.id},
            "exam-start": {"pk": self.exam.id},
            "exam-status": {"pk": self.exam.id},
            "attempt-detail": {"pk": self.live_attempt.id},
            "attempt-answers": {"pk": self.live_attempt.id},
            "attempt-behavior": {"pk": self.live_attempt.id},
            "attempt-heartbeat": {"pk": self.live_attempt.id},
            "attempt-paper": {"pk": self.live_attempt.id},
            "attempt-result": {"pk": self.judged_attempt.id},
            "attempt-save": {"pk": self.live_attempt.id},
            "attempt-submit": {"pk": self.live_attempt.id},
            "grading-feedback-detail": {"pk": self.feedback.id},
            "grading-attempt-grade": {"attempt_id": self.live_attempt.id},
            "grading-attempt-finalize": {"attempt_id": self.judged_attempt.id},
            "grading-answer-review": {"answer_id": self.judged_attempt.answers.first().id},
            "grading-attempt-report": {"attempt_id": self.judged_attempt.id},
            "analytics-exam-report": {"exam_id": self.exam.id},
            "analytics-exam-questions": {"exam_id": self.exam.id},
            "analytics-student-mastery": {"student_id": self.student_b.id},
            "analytics-student-trend": {"student_id": self.student_b.id},
            "analytics-student-report": {"student_id": self.student_b.id},
            "analytics-student-diagnosis": {"student_id": self.student_b.id},
            "recommend-student-practice": {"student_id": self.student_b.id},
            "proctoring-exam-report": {"exam_id": self.exam.id},
            "proctoring-attempt-evidence": {"attempt_id": self.judged_attempt.id},
            "classroom-detail": {"classroom_id": self.classroom.id},
            "classroom-members": {"classroom_id": self.classroom.id},
        }

    # ------------------------------------------------------------------
    # 发现：从路由表拿全量操作清单
    # ------------------------------------------------------------------
    @classmethod
    def _discover(cls):
        """返回 ``[{"name", "method", "raw"}, ...]``。

        去重按「去掉锚点后的路径 + 方法」：
          * DRF router 会给每条路由再生成 ``.json`` / ``.api`` 这类 format 变体，
            它们与主路由是同一个视图、只是内容协商方式不同，纳入矩阵只会让数字虚高；
          * router 还会在 include 前缀上挂一个 API 索引页（``api-root``），
            当它与某个资源的列表路由同址（如 ``/api/v1/users/``）时，实际命中的是
            先注册的列表路由，索引页是**不可达**的——保留它只会把 403 记到错的名字上。
        """
        seen = {}
        for raw, pattern in _walk_patterns(get_resolver().url_patterns):
            if not raw.startswith("api/v1/"):
                continue
            if "(?P<format>" in raw or "drf_format_suffix" in raw:
                continue
            for method in _methods_of(pattern):
                seen.setdefault((_strip_anchors(raw), method), {
                    "name": pattern.name, "method": method, "raw": raw,
                })
        return sorted(seen.values(), key=lambda op: (op["raw"], op["method"]))

    def _access_of(self, name, method):
        return ACCESS_OVERRIDES.get(f"{name}:{method}") or _DEFAULT_ACCESS.get(name)

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------
    def _token(self, user):
        key = f"contract-token:{user.username}"
        token = cache.get(key)
        if token:
            return token
        response = self.client.post(
            "/api/v1/auth/login/",
            {"username": user.username, "password": PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, f"{user.username} 登录失败")
        token = response.json()["data"]["access"]
        cache.set(key, token, 600)
        return token

    def _url_of(self, op, bad_params=False):
        params = dict(self._params().get(op["name"], {}))
        if bad_params:
            params = {key: "abc" for key in params}
        url = _build_path(op["raw"], params)
        if not bad_params:
            suffix = QUERY_PARAMS.get(op["name"])
            if suffix:
                url += suffix.format(exam=self.exam.id, student=self.student_b.id)
        return url

    def _probe(self, method, url, user=None, body=None):
        """发一次请求并**立刻回滚**，避免 DELETE / 交卷之类改动污染后续操作。"""
        headers = {}
        if user is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {self._token(user)}"
        with transaction.atomic():
            try:
                if method == "GET":
                    response = self.client.get(url, **headers)
                else:
                    response = self.client.generic(
                        method, url, data=json.dumps(body or {}),
                        content_type="application/json", **headers,
                    )
                status = response.status_code
                code = None
                if response.get("Content-Type", "").startswith("application/json"):
                    try:
                        code = response.json().get("code")
                    except ValueError:      # 空响应体（如 204）不算异常
                        code = None
            finally:
                transaction.set_rollback(True)
        return status, code

    def _sweep(self, actor):
        """对指定身份跑一遍全量操作，返回 ``[(op, status, code), ...]``。"""
        user = None if actor == "anonymous" else getattr(self, actor)
        from collections import Counter as _C
        
        results = []
        for op in self._discover():
            with self.subTest(name=op["name"], method=op["method"], actor=actor):
                status, code = self._probe(op["method"], self._url_of(op), user=user)
                results.append((op, status, code))
                self.MATRIX.setdefault(
                    (op["name"], op["method"], self._url_of(op)), {}
                )[actor] = (status, code)
        return results

    # ------------------------------------------------------------------
    # 断言工具
    # ------------------------------------------------------------------
    def _assert_denied(self, op, actor, status, code):
        expected = EXPLICIT_DENY_CODES.get(((op["name"], op["method"]), actor))
        if expected:
            self.assertEqual(
                (status, code), expected,
                f"{actor} 访问 {op['method']} {op['name']} 的口径应为 {expected}，"
                f"实际 {(status, code)}",
            )
            return
        self.assertIn(
            status, DENIED,
            f"{actor} 不应能访问 {op['method']} {op['name']}，"
            f"实际 {status}/{code}（应 403 或 404）",
        )

    def _assert_allowed(self, op, actor, status, code):
        self.assertNotIn(
            status, (401, 403),
            f"{actor} 访问 {op['method']} {op['name']} 被拒（{status}/{code}），"
            f"与准入清单不符",
        )
        self.assertNotEqual(status, 500, f"{actor} 访问 {op['method']} {op['name']} 触发 500")

    # ==================================================================
    # 1. 清单必须与路由表一一对应
    # ==================================================================
    def test_contract_covers_every_route(self):
        """路由里每个操作都要有准入声明；清单里也不许留已删除的接口。"""
        discovered = {(op["name"], op["method"]) for op in self._discover()}
        self.assertTrue(discovered, "没有在 /api/v1/ 下发现任何接口，路由前缀可能被改过")

        missing = sorted(
            f"{name}:{method}" for name, method in discovered
            if self._access_of(name, method) is None
        )
        self.assertEqual(
            missing, [],
            "以下接口没有在准入清单（_DEFAULT_ACCESS / ACCESS_OVERRIDES）里声明，"
            f"新增接口请补上：{missing}",
        )

        # api-root 是 router 自动生成的索引页，同一个 name 对应 6 个前缀，去重后比对
        declared = {
            (key.split(":")[0], key.split(":")[1])
            for key in ACCESS_OVERRIDES
        } | {(name, None) for name in _DEFAULT_ACCESS}
        stale = sorted({
            name for name, _ in declared if name not in {n for n, _ in discovered}
        })
        self.assertEqual(
            stale, [],
            f"清单里有路由表已经不存在的接口，请删掉：{stale}",
        )

    # ==================================================================
    # 2. 认证：匿名一律 401（白名单除外）
    # ==================================================================
    def test_anonymous_access(self):
        for op, status, code in self._sweep("anonymous"):
            with self.subTest(name=op["name"], method=op["method"]):
                if (op["name"], op["method"]) in PUBLIC_OPERATIONS:
                    self.assertNotIn(
                        status, (401, 403),
                        f"{op['method']} {op['name']} 属于公开白名单，匿名不该被拒",
                    )
                    continue
                self.assertEqual(
                    status, 401,
                    f"匿名访问 {op['method']} {op['name']} 应 401，实际 {status}/{code}",
                )
                self.assertEqual(code, 40100, f"{op['name']} 的 401 业务码应统一为 40100")

    def test_health_is_public(self):
        """``/api/health/`` 不在 /api/v1/ 前缀下，单独确认它无需登录。"""
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)

    def test_public_whitelist_is_exactly_three(self):
        """白名单必须显式且最小：多放一条就是一处认证缺口。"""
        self.assertEqual(
            PUBLIC_OPERATIONS,
            {("register", "POST"), ("login", "POST"), ("refresh", "POST")},
        )

    # ==================================================================
    # 3. 权限边界：学生
    # ==================================================================
    def test_student_access(self):
        for op, status, code in self._sweep("student"):
            with self.subTest(name=op["name"], method=op["method"]):
                level = self._access_of(op["name"], op["method"])
                if level in (PUBLIC, STUDENT, AUTH):
                    self._assert_allowed(op, "学生", status, code)
                else:
                    self._assert_denied(op, "student", status, code)

    # ==================================================================
    # 4. 权限边界：教师（管理员天然拥有教师权限）
    # ==================================================================
    def test_teacher_access(self):
        for op, status, code in self._sweep("teacher"):
            with self.subTest(name=op["name"], method=op["method"]):
                level = self._access_of(op["name"], op["method"])
                if level in (PUBLIC, AUTH, TEACHER):
                    self._assert_allowed(op, "教师", status, code)
                else:
                    self._assert_denied(op, "teacher", status, code)

    # ==================================================================
    # 5. 权限边界：管理员
    # ==================================================================
    def test_admin_access(self):
        for op, status, code in self._sweep("admin"):
            with self.subTest(name=op["name"], method=op["method"]):
                level = self._access_of(op["name"], op["method"])
                if level in (PUBLIC, AUTH, TEACHER, ADMIN):
                    self._assert_allowed(op, "管理员", status, code)
                else:
                    self._assert_denied(op, "admin", status, code)

    def test_student_can_read_own_resources(self):
        """上一条矩阵用的是**别人的** id（越权 → 404），这里补自己名下的正向证据。

        少了这一条，「所有 404 都通过」就可能是假的——万一接口对谁都 404，
        矩阵依然全绿，学生却根本看不到自己的数据。
        """
        own = self.student
        cases = [
            f"/api/v1/attempts/{self.live_attempt.id}/",
            f"/api/v1/attempts/{self.live_attempt.id}/result/",
            f"/api/v1/attempts/{self.live_attempt.id}/answers/",
            f"/api/v1/grading/attempts/{self.live_attempt.id}/report/",
            "/api/v1/grading/wrong-book/",
            f"/api/v1/analytics/students/{own.id}/mastery/",
            f"/api/v1/analytics/students/{own.id}/trend/",
            f"/api/v1/analytics/students/{own.id}/report/",
            f"/api/v1/recommend/students/{own.id}/practice/",
            f"/api/v1/classrooms/{self.classroom.id}/",
        ]
        for url in cases:
            with self.subTest(url=url):
                status, code = self._probe("GET", url, user=own)
                self.assertEqual(status, 200, f"学生读自己的 {url} 应 200，实际 {status}/{code}")
                self.assertEqual(code, 0)

    def test_student_cannot_read_others_404(self):
        """跨人读一律 404（不是 403）：不让学生拿状态码探测别人的资源是否存在。"""
        cases = [
            f"/api/v1/attempts/{self.judged_attempt.id}/",
            f"/api/v1/attempts/{self.judged_attempt.id}/paper/",
            f"/api/v1/grading/attempts/{self.judged_attempt.id}/report/",
            f"/api/v1/analytics/students/{self.student_b.id}/report/",
            f"/api/v1/recommend/students/{self.student_b.id}/practice/",
        ]
        for url in cases:
            with self.subTest(url=url):
                status, code = self._probe("GET", url, user=self.student)
                self.assertEqual(
                    status, 404,
                    f"学生读他人的 {url} 应 404，实际 {status}/{code}",
                )

    # ==================================================================
    # 6. 参数校验：全量扫一遍，任何接口都不许 500
    # ==================================================================
    def test_invalid_params_never_500(self):
        """带参路由把 id 换成 ``abc``、无参列表补非法分页与非法 id 查询，逐条确认不 500。"""
        bad_id_query = (
            "?exam_id=abc&student_id=abc&attempt_id=abc&answer_id=abc"
            "&classroom_id=abc&page=-1&page_size=abc&ordering=__nope__"
        )
        for op in self._discover():
            level = self._access_of(op["name"], op["method"])
            # 用该级别下「有权限」的身份发请求，才能真正走到参数校验那一层
            actor = {"public": None, "auth": "student", "student": "student",
                     "teacher": "teacher", "admin": "admin"}[level]
            with self.subTest(name=op["name"], method=op["method"]):
                has_param = bool(self._params().get(op["name"]))
                url = self._url_of(op, bad_params=has_param)
                if not has_param and op["method"] == "GET":
                    url += bad_id_query
                status, code = self._probe(op["method"], url,
                                           user=None if actor is None else getattr(self, actor))
                self.PARAM_MATRIX[(op["name"], op["method"])] = (status, code)
                self.assertNotEqual(
                    status, 500,
                    f"{op['method']} {op['name']} 非法参数返回 500（code={code}）",
                )
                self.assertNotEqual(code, 50000, f"{op['method']} {op['name']} 返回 50000")
                if status not in (200, 201, 204):
                    self.assertIn(
                        status, BAD_REQUEST,
                        f"{op['method']} {op['name']} 非法参数应 400/404，实际 {status}",
                    )

    def test_named_bad_params_are_400(self):
        """文档点名的三类非法参数，逐条钉死状态码。"""
        cases = [
            ("GET", f"/api/v1/questions/abc/", self.teacher, (404,)),
            ("GET", f"/api/v1/exams/abc/", self.teacher, (404,)),
            ("POST", "/api/v1/auth/login/", None, (400,)),
            ("POST", "/api/v1/auth/register/", None, (400,)),
            ("GET", "/api/v1/questions/?page=-1", self.teacher, (400,)),
            ("GET", f"/api/v1/proctoring/exams/{self.exam.id}/report/?top_n=abc",
             self.teacher, (200, 400)),
            ("GET", f"/api/v1/grading/attempts/{self.judged_attempt.id}/report/?x=1",
             self.teacher, (200,)),
        ]
        for method, url, user, allowed in cases:
            with self.subTest(method=method, url=url):
                status, _ = self._probe(method, url, user=user,
                                        body={} if method != "GET" else None)
                self.assertIn(
                    status, allowed,
                    f"{method} {url} 期望 {allowed}，实际 {status}",
                )
                self.assertNotEqual(status, 500)

    def test_query_id_params_must_not_500(self):
        """查询参数里的 id 写错（``?exam_id=abc``）必须是 400 / 404，不能 500。

        这一类曾**真实存在**：视图直接把 ``request.query_params.get("exam_id")``
        交给 ``get_object_or_404(Model, pk=...)``，Django 在 ``queryset.get()``
        阶段抛 ``ValueError: Field 'id' expected a number``，而
        ``get_object_or_404`` 只捕 ``DoesNotExist`` —— 于是 500。
        """
        cases = [
            "/api/v1/grading/queue/?exam_id=abc",
            "/api/v1/grading/scoreboard/?exam_id=abc",
            "/api/v1/grading/wrong-book/?student_id=abc",
            "/api/v1/grading/feedback/?attempt_id=abc",
            "/api/v1/grading/feedback/?answer_id=abc",
            # 传对一半也要稳住：exam_id 合法、classroom_id 非法
            f"/api/v1/grading/scoreboard/?exam_id={self.exam.id}&classroom_id=abc",
        ]
        for url in cases:
            with self.subTest(url=url):
                status, code = self._probe("GET", url, user=self.teacher)
                self.assertIn(
                    status, BAD_REQUEST,
                    f"GET {url} 非法 id 应 400/404，实际 {status}/{code}",
                )
                self.assertNotEqual(code, 50000)

    def test_missing_required_body_is_400(self):
        """缺必填字段 / 非法枚举必须是 400，不能 500、也不能假装成功。"""
        cases = [
            ("POST", "/api/v1/auth/change-password/", self.student, {}),
            ("POST", "/api/v1/auth/logout/", self.student, {}),
            ("POST", "/api/v1/questions/batch/", self.teacher, {}),
            ("POST", f"/api/v1/exams/{self.exam.id}/status/", self.teacher, {}),
            ("POST", f"/api/v1/papers/{self.paper.id}/status/", self.teacher, {}),
            # 枚举值写错：with_ai 只接受布尔
            ("POST", f"/api/v1/grading/attempts/{self.live_attempt.id}/grade/",
             self.teacher, {"with_ai": "maybe"}),
            # 状态机写错：不存在的考试状态
            ("POST", f"/api/v1/exams/{self.exam.id}/status/", self.teacher,
             {"status": "not-a-status"}),
        ]
        for method, url, user, body in cases:
            with self.subTest(method=method, url=url):
                status, code = self._probe(method, url, user=user, body=body)
                self.assertEqual(
                    status, 400,
                    f"{method} {url} 入参非法应 400，实际 {status}",
                )
                self.assertNotIn(code, (0, 50000))

    # ==================================================================
    # 7. 回归：练习接口的 POST 曾因返回结构缺键而 500
    # ==================================================================
    def test_practice_post_without_candidates_returns_200(self):
        """学生没有任何可练题目时，POST 练习必须优雅返回空清单，而不是 500。

        历史缺陷：``RecommendationService.explain()`` 在候选为空时提前返回的字典
        少了 ``model`` 键，视图直接按键读取 → ``KeyError`` → 500（三类角色全中）。
        这里用「没加入任何学科班」的学生复现空候选场景。
        """
        from apps.users.models import User

        lonely = User.objects.create_user(
            username="contract_lonely", email="lonely@example.com",
            password=PASSWORD, role="student", real_name="无班学生", student_no="S9001",
        )
        status, code = self._probe(
            "POST", f"/api/v1/recommend/students/{lonely.id}/practice/",
            user=lonely, body={},
        )
        self.assertEqual(status, 200, f"空候选场景应 200，实际 {status}/{code}")
        self.assertEqual(code, 0)

    # ==================================================================
    # 8. 矩阵导出（供 docs/接口测试报告.md 复现，不影响常规测试）
    # ==================================================================
    def test_invalid_page_returns_400_not_404(self):
        """``page`` 取值非法 → 400（参数错误），不是 404（资源不存在）。

        DRF 默认把 ``page=0`` 归成 ``InvalidPage`` → 404，在本项目里会变成 ``40400``，
        前端会误以为路径写错。这里只拦「根本不可能存在的页码」；
        ``page=999`` 这种格式对但超出范围的，仍走 404。
        """
        for bad in ("0", "-1", "abc", "1.5"):
            with self.subTest(page=bad):
                resp = self.get_json(f"/api/v1/questions/?page={bad}", user=self.teacher)
                self.assertEqual(resp.status_code, 400, f"page={bad} 应返回 400")
                self.assertEqual(resp.json()["code"], 40000)

        beyond = self.get_json("/api/v1/questions/?page=99999", user=self.teacher)
        self.assertEqual(beyond.status_code, 404, "格式正确但超出总页数 → 仍是 404")

    def test_student_gets_no_answer_fields(self):
        """答案类字段不得出现在学生可见的任何 GET 响应里。"""
        violations = []
        for op in self._student_get_operations():
            body = self._body_of(op)
            if body is None:
                continue
            found = _collect_fields(body, ANSWER_FIELDS)
            for field in found:
                if field in ANSWER_FIELD_ALLOWED.get(op["name"], set()):
                    continue
                violations.append(f"{op['name']} (`{op['method']} {self._url_of(op)}`) 下发 {field}")
        self.assertEqual(
            violations, [],
            "学生视角出现答案字段：\n  " + "\n  ".join(violations)
            + "\n\n若该接口确实需要给答案（如错题本），请到 ANSWER_FIELD_ALLOWED 里写明理由。",
        )

    def test_student_gets_no_unpublished_score(self):
        """判分类字段可以在，但成绩未公开时必须是空值。"""
        violations = []
        for op in self._student_get_operations():
            body = self._body_of(op)
            if body is None:
                continue
            found = _collect_fields(body, SCORE_FIELDS)
            allowed = SCORE_FIELD_ALLOWED.get(op["name"], set())
            for field, values in found.items():
                if field in allowed:
                    continue
                leaked = [v for v in values if not _is_blank(v)]
                if leaked:
                    violations.append(
                        f"{op['name']} (`{self._url_of(op)}`) 返回 {field}={leaked[0]!r}"
                        f"（共 {len(leaked)} 处非空）"
                    )
        self.assertEqual(
            violations, [],
            "成绩未公开却下发了分数/对错：\n  " + "\n  ".join(violations)
            + "\n\n若该接口本来就该展示成绩（如学情、错题本），"
              "请到 SCORE_FIELD_ALLOWED 里写明理由。",
        )

    # ------------------------------------------------------------------
    def _student_get_operations(self):
        """学生能访问的 GET 操作（PUBLIC / AUTH / STUDENT 三级）。"""
        for op in self._discover():
            if op["method"] != "GET":
                continue
            if self._access_of(op["name"], op["method"]) not in (PUBLIC, AUTH, STUDENT):
                continue
            yield op

    def _body_of(self, op):
        """发一次请求，返回 JSON 体；非 200 或非 JSON 返回 None。"""
        resp = self.client.get(self._url_of(op), **self.auth(self.student))
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def test_zz_dump_matrix(self):
        """``API_CONTRACT_DUMP=1`` 时把观测到的矩阵以 JSON 打到 stdout。

        用例名以 ``zz`` 开头，保证在同类的其它用例都跑完之后才执行
        （Django 的测试运行器按方法名字典序执行）。
        """
        if os.environ.get("API_CONTRACT_DUMP") != "1":
            self.skipTest("未设置 API_CONTRACT_DUMP=1，跳过矩阵导出")

        rows = []
        for (name, method, url), observed in sorted(self.MATRIX.items()):
            rows.append({
                "name": name,
                "method": method,
                "url": url,
                "access": self._access_of(name, method),
                "anonymous": observed.get("anonymous"),
                "student": observed.get("student"),
                "teacher": observed.get("teacher"),
                "admin": observed.get("admin"),
                "params": self.PARAM_MATRIX.get((name, method)),
            })
        print("\n=====API-CONTRACT-MATRIX=====")
        print(json.dumps(rows, ensure_ascii=False))
        print("=====END-MATRIX=====")


# ==================================================================
# 学生视角的敏感字段黑名单（横切检查）
# ==================================================================
#
# 背景：本项目对「答案防泄漏」的做法是**字段根本不下发**，而不是前端不渲染。
# 但这条纪律靠人工守不住——历史上有两次真实泄漏：
#   * `GET /attempts/{id}/answers/` 复用教师序列化器，学生考到一半就能拿走答案；
#   * 修完答案文本后，`is_correct` / `score` 照旧下发，交卷后能知道逐题对错。
#
# 所以这里做一层横切扫描：以学生身份把**能访问的 GET 接口**全打一遍，
# 递归遍历响应体，命中下列字段就算失败。新增接口默认受约束，
# 要放行必须到白名单里**逐接口写明理由**——不允许「整个模块豁免」。
#
# 两类判定分开，避免一刀切造成误报：
#   * 答案类：出现即违规（只能出现在错题本 / 交卷后的结果页）；
#   * 判分类：**字段可以在，表示「暂无」**，但值必须为空
#     （null / "" / []）。这样前端仍能判断「有这栏但还没出分」。

#: 学生视角完全不该出现的答案字段
ANSWER_FIELDS = ("reference_answer", "analysis", "correct_answer")
#: 判分类字段：允许出现，但未公开成绩时必须为空
SCORE_FIELDS = ("is_correct", "score", "ai_score", "ai_comment",
                "teacher_comment", "passed")

#: 放行「答案字段」的接口 → 理由
ANSWER_FIELD_ALLOWED = {
    "grading-wrong-book": "错题本：只含本人已判分的题目，给答案是产品设计",
    "attempt-result": "交卷后看结果：返回与否由 show_score_immediately 控制",
}

#: 放行「判分类字段非空」的接口 → 理由
SCORE_FIELD_ALLOWED = {
    "attempt-paper": "卷面数据：每题的 score 是**满分**（答题要显示分值），"
                     "attempt 部分的总得分已按可见性收敛",
    "attempt-save": "单题暂存回执：同样只回满分，不回得分",
    "grading-wrong-book": "错题本：展示已判分题目的对错与得分",
    "attempt-result": "结果页：成绩已公开时的正常返回",
    "analytics-me": "学情（本人）：分数与掌握度就是它的内容",
    "analytics-student-mastery": "学情（教师看学生）：同理",
    "analytics-student-trend": "学情趋势：按考试列分数",
    "analytics-student-report": "学情报告：含分数摘要",
    "recommend-practice": "练习推荐：按历史得分率挑题",
}


def _collect_fields(node, targets, out=None):
    """递归收集 JSON 里目标字段的**全部**取值。

    必须收全部而不是第一个：列表里可能有的行是 null、有的行有值，
    只取首个会漏掉后面的泄漏。
    """
    if out is None:
        out = {}
    if isinstance(node, dict):
        for key, value in node.items():
            if key in targets:
                out.setdefault(key, []).append(value)
            _collect_fields(value, targets, out)
    elif isinstance(node, list):
        for item in node:
            _collect_fields(item, targets, out)
    return out


def _is_blank(value) -> bool:
    """``None`` / 空串 / 空列表算「没有值」；``0`` 与 ``False`` 是有意义的，不算空。"""
    return value is None or value == "" or value == []
