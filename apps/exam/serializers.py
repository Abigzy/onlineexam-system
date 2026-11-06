"""考试模块序列化器。

和题库 / 试卷模块一样按**视角分层**——这是本项目防信息泄漏的统一手段：

  * ``ExamListSerializer`` / ``ExamDetailSerializer`` —— 教师视角，含参考范围、防作弊策略；
  * ``ExamStudentSerializer``                        —— 学生视角，**没有任何试卷内容**，
                                                        只有「这场考试什么时候考、我考了没、剩多少时间」；
  * ``ExamAttemptSerializer``                        —— 教师视角的作答记录（含行为计数）；
  * ``ExamAttemptStudentSerializer``                 —— 学生看自己的作答概要。

学生拿到题目的唯一途径是 ``POST /exams/{id}/start/`` 与 ``GET /attempts/{id}/``，
两条都走 ``ExamAttemptService._question_payload()``，答案过滤只有那一处。
"""

from rest_framework import serializers

from apps.common.constants import ExamStatus
from apps.papers.models import Paper

from .constants import BehaviorEvent, MultipleChoicePolicy, SubmitReason
from .models import Answer, Exam, ExamAttempt


# ======================================================================
# 考试
# ======================================================================
class ExamListSerializer(serializers.ModelSerializer):
    """考试列表用（教师）。"""

    paper_id = serializers.IntegerField(read_only=True)
    paper_title = serializers.CharField(source="paper.title", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    multiple_choice_policy_display = serializers.CharField(
        source="get_multiple_choice_policy_display", read_only=True,
    )
    computed_status = serializers.SerializerMethodField()
    computed_status_display = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source="created_by.display_name", read_only=True,
                                            default="")
    duration_minutes_effective = serializers.SerializerMethodField()
    total_score = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()
    pass_score_effective = serializers.SerializerMethodField()
    applicants = serializers.SerializerMethodField()
    attempt_total = serializers.SerializerMethodField()
    in_progress_total = serializers.SerializerMethodField()
    submitted_total = serializers.SerializerMethodField()
    is_open = serializers.SerializerMethodField()

    class Meta:
        model = Exam
        fields = ["id", "title", "subject", "description", "status", "status_display",
                  "computed_status", "computed_status_display", "is_open",
                  "paper_id", "paper_title", "duration_minutes", "duration_minutes_effective",
                  "total_score", "question_count", "pass_score_effective",
                  "start_time", "end_time", "max_attempts",
                  "multiple_choice_policy", "multiple_choice_policy_display",
                  "shuffle_questions", "shuffle_options",
                  "detect_switch", "max_switch_count", "auto_submit_on_limit", "detect_paste",
                  "fullscreen_required", "allow_resume", "show_score_immediately",
                  "open_to_all", "applicants", "attempt_total", "in_progress_total",
                  "submitted_total", "created_by_name", "published_at",
                  "created_at", "updated_at"]

    def get_computed_status(self, obj) -> str:
        return obj.computed_status()

    def get_computed_status_display(self, obj) -> str:
        return dict(ExamStatus.choices).get(obj.computed_status(), "")

    def get_duration_minutes_effective(self, obj) -> int:
        return obj.effective_duration_minutes

    def get_total_score(self, obj) -> float:
        return obj.paper.total_score_float

    def get_question_count(self, obj) -> int:
        return obj.paper.question_count

    def get_pass_score_effective(self, obj) -> float:
        return float(obj.effective_pass_score or 0)

    def get_applicants(self, obj) -> int:
        # 面向全体时「应考人数」= 在册学生数（与具体考试无关，视图算一次传进来）
        if obj.open_to_all:
            return self.context.get("all_student_count") or obj.participant_count()
        return getattr(obj, "participant_total", None) or obj.participants.count()

    def get_attempt_total(self, obj) -> int:
        total = getattr(obj, "attempt_total", None)
        return total if total is not None else obj.attempt_count()

    def get_in_progress_total(self, obj) -> int:
        return getattr(obj, "in_progress_total", None) or 0

    def get_submitted_total(self, obj) -> int:
        return max(0, self.get_attempt_total(obj) - self.get_in_progress_total(obj))

    def get_is_open(self, obj) -> bool:
        return obj.is_open()


class ExamDetailSerializer(ExamListSerializer):
    """考试详情（教师）：多带一份试卷摘要与参考名单规模。"""

    paper = serializers.SerializerMethodField()
    participant_ids = serializers.SerializerMethodField()
    participant_names = serializers.SerializerMethodField()

    class Meta(ExamListSerializer.Meta):
        fields = ExamListSerializer.Meta.fields + [
            "paper", "participant_ids", "participant_names", "created_by_id",
        ]

    def get_paper(self, obj) -> dict:
        paper = obj.paper
        return {
            "id": paper.id,
            "title": paper.title,
            "subject": paper.subject,
            "status": paper.status,
            "total_score": paper.total_score_float,
            "pass_score": float(paper.pass_score or 0),
            "question_count": paper.question_count,
            "duration_minutes": paper.duration_minutes,
            "actual_difficulty": paper.actual_difficulty(),
            "section_count": len(paper.sections.all()),
        }

    def get_participant_ids(self, obj) -> list:
        return list(obj.participants.values_list("id", flat=True))

    def get_participant_names(self, obj) -> list:
        return [
            {"id": user.id, "name": user.display_name, "student_no": getattr(user, "student_no", "")}
            for user in obj.participants.all()[:200]
        ]


class ExamStudentSerializer(serializers.ModelSerializer):
    """学生视角的考试概要。

    **刻意不含试卷内容**：学生还没点「开始考试」时，连题型分布都不该知道。
    这里只回答三个问题——什么时候考、能不能考、我考到哪一步了。
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    computed_status = serializers.SerializerMethodField()
    computed_status_display = serializers.SerializerMethodField()
    duration_minutes_effective = serializers.SerializerMethodField()
    total_score = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()
    pass_score_effective = serializers.SerializerMethodField()
    can_start = serializers.SerializerMethodField()
    locked_reason = serializers.SerializerMethodField()
    attempts_used = serializers.SerializerMethodField()
    attempts_left = serializers.SerializerMethodField()
    my_attempt = serializers.SerializerMethodField()

    class Meta:
        model = Exam
        fields = ["id", "title", "subject", "description", "status", "status_display",
                  "computed_status", "computed_status_display",
                  "duration_minutes_effective", "total_score", "question_count",
                  "pass_score_effective", "start_time", "end_time", "max_attempts",
                  "allow_resume", "show_score_immediately",
                  "can_start", "locked_reason", "attempts_used", "attempts_left",
                  "my_attempt"]

    def _student(self):
        return self.context.get("student") or self.context.get("request").user

    def get_computed_status(self, obj) -> str:
        return obj.computed_status()

    def get_computed_status_display(self, obj) -> str:
        return dict(ExamStatus.choices).get(obj.computed_status(), "")

    def get_duration_minutes_effective(self, obj) -> int:
        return obj.effective_duration_minutes

    def get_total_score(self, obj) -> float:
        return obj.paper.total_score_float

    def get_question_count(self, obj) -> int:
        return obj.paper.question_count

    def get_pass_score_effective(self, obj) -> float:
        return float(obj.effective_pass_score or 0)

    def get_attempts_used(self, obj) -> int:
        return obj.my_attempt_count(self._student())

    def get_attempts_left(self, obj) -> int:
        return max(0, obj.max_attempts - obj.my_attempt_count(self._student()))

    def get_can_start(self, obj) -> bool:
        student = self._student()
        if not obj.open_to(student):
            return False
        if obj.my_attempt_count(student) >= obj.max_attempts:
            return False
        return obj.is_open()

    def get_locked_reason(self, obj) -> str:
        """不能开始的原因——直接告诉学生，别让他干点按钮。"""
        from django.utils import timezone

        student = self._student()
        if obj.status in (ExamStatus.DRAFT, ExamStatus.ARCHIVED):
            return "考试尚未发布"
        if not obj.open_to(student):
            return "你不在本场考试的参考范围内"
        if obj.my_attempt_count(student) >= obj.max_attempts:
            return f"作答次数已用完（{obj.max_attempts} 次）"

        now = timezone.now()
        if obj.start_time and now < obj.start_time:
            return "考试尚未开始"
        if obj.end_time and now > obj.end_time:
            return "考试已结束"
        if obj.status == ExamStatus.FINISHED:
            return "考试已结束"
        if not obj.paper.question_count:
            return "试卷还没有题目"
        return ""

    def get_my_attempt(self, obj) -> dict:
        attempt = obj.my_attempt(self._student())
        if attempt is None:
            return None
        return ExamAttemptStudentSerializer(attempt).data


class ExamWriteSerializer(serializers.Serializer):
    """新建 / 修改考试（入参）。"""

    title = serializers.CharField(required=False, allow_blank=True, max_length=200)
    description = serializers.CharField(required=False, allow_blank=True)
    subject = serializers.CharField(required=False, allow_blank=True, max_length=64)
    paper_id = serializers.IntegerField(required=False)
    start_time = serializers.DateTimeField(required=False, allow_null=True)
    end_time = serializers.DateTimeField(required=False, allow_null=True)
    duration_minutes = serializers.IntegerField(required=False, min_value=0, max_value=600)
    pass_score = serializers.DecimalField(required=False, allow_null=True,
                                          max_digits=7, decimal_places=2, min_value=0)
    max_attempts = serializers.IntegerField(required=False, min_value=1, max_value=10)
    multiple_choice_policy = serializers.ChoiceField(
        choices=MultipleChoicePolicy.choices, required=False,
        help_text="多选题给分策略，判分时生效（模块五）",
    )
    shuffle_questions = serializers.BooleanField(required=False)
    shuffle_options = serializers.BooleanField(required=False)
    detect_switch = serializers.BooleanField(required=False)
    max_switch_count = serializers.IntegerField(required=False, min_value=0, max_value=999)
    auto_submit_on_limit = serializers.BooleanField(required=False)
    detect_paste = serializers.BooleanField(required=False)
    fullscreen_required = serializers.BooleanField(required=False)
    allow_resume = serializers.BooleanField(required=False)
    show_score_immediately = serializers.BooleanField(required=False)
    open_to_all = serializers.BooleanField(required=False)
    participant_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
        help_text="指定考生 id；传了非空列表会自动把 open_to_all 置为 False",
    )

    def validate_paper_id(self, value):
        paper = Paper.objects.filter(pk=value).exclude(status="archived").first()
        if paper is None:
            raise serializers.ValidationError("试卷不存在或已归档")
        return value

    def validate_participant_ids(self, value):
        if not value:
            return value
        from django.contrib.auth import get_user_model

        from apps.common.constants import Role

        user_model = get_user_model()
        found = set(
            user_model.objects.filter(id__in=value, role=Role.STUDENT)
            .values_list("id", flat=True)
        )
        missing = [uid for uid in value if uid not in found]
        if missing:
            raise serializers.ValidationError(f"以下用户不是有效学生：{missing}")
        return value

    def validate(self, attrs):
        start_time = attrs.get("start_time", serializers.empty)
        end_time = attrs.get("end_time", serializers.empty)
        if start_time is not serializers.empty and end_time is not serializers.empty:
            if start_time and end_time and end_time <= start_time:
                raise serializers.ValidationError({"end_time": "结束时间必须晚于开始时间"})
        return attrs

    def to_service_payload(self) -> dict:
        """把校验后的数据整理成 service 层认的字典。"""
        data = dict(self.validated_data)
        paper_id = data.pop("paper_id", None)
        if paper_id is not None:
            data["paper"] = Paper.objects.get(pk=paper_id)
        return data


# ======================================================================
# 作答记录
# ======================================================================
class ExamAttemptSerializer(serializers.ModelSerializer):
    """教师视角的作答记录。"""

    student_name = serializers.CharField(source="student.display_name", read_only=True, default="")
    student_no = serializers.CharField(source="student.student_no", read_only=True, default="")
    exam_title = serializers.CharField(source="exam.title", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    submit_reason_display = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    remaining_seconds = serializers.SerializerMethodField()
    suspicious = serializers.SerializerMethodField()
    passed = serializers.SerializerMethodField()

    class Meta:
        model = ExamAttempt
        fields = ["id", "exam_id", "exam_title", "student_id", "student_name", "student_no",
                  "attempt_no", "status", "status_display", "submit_reason",
                  "submit_reason_display", "is_timeout", "passed",
                  "started_at", "deadline_at", "submitted_at", "graded_at",
                  "time_spent_seconds", "remaining_seconds",
                  "score", "objective_score", "subjective_score", "total_score_snapshot",
                  "question_count", "answered_count", "progress",
                  "switch_count", "paste_count", "fullscreen_exit_count", "heartbeat_count",
                  "last_heartbeat_at", "suspicious",
                  "ip_address", "user_agent", "created_at"]

    def get_submit_reason_display(self, obj) -> str:
        return obj.get_submit_reason_display() if obj.submit_reason else ""

    def get_progress(self, obj) -> dict:
        return obj.progress()

    def get_remaining_seconds(self, obj) -> int:
        return obj.remaining_seconds()

    def get_suspicious(self, obj) -> bool:
        return bool(obj.exam.detect_switch and obj.switch_count > obj.exam.max_switch_count)

    def get_passed(self, obj):
        return obj.passed()


class ExamAttemptStudentSerializer(serializers.ModelSerializer):
    """学生看自己的作答记录（结果页与考试列表共用）。

    不含任何行为计数的阈值判定结论——学生能看到自己切了几次屏没必要隐藏
    （前端本来就在上报），但「是否违规」的定性结论属于教师侧。
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    submit_reason_display = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    remaining_seconds = serializers.SerializerMethodField()
    passed = serializers.SerializerMethodField()
    can_resume = serializers.SerializerMethodField()
    show_score = serializers.SerializerMethodField()
    # 改成方法字段：成绩未公开时给 null（见 get_score）
    score = serializers.SerializerMethodField()

    class Meta:
        model = ExamAttempt
        fields = ["id", "exam_id", "attempt_no", "status", "status_display",
                  "submit_reason_display", "is_timeout", "passed", "show_score",
                  "started_at", "deadline_at", "submitted_at", "time_spent_seconds",
                  "remaining_seconds", "score", "total_score_snapshot",
                  "question_count", "answered_count", "progress", "can_resume",
                  "switch_count", "paste_count"]

    def get_submit_reason_display(self, obj) -> str:
        return obj.get_submit_reason_display() if obj.submit_reason else ""

    def get_progress(self, obj) -> dict:
        return obj.progress()

    def get_remaining_seconds(self, obj) -> int:
        return obj.remaining_seconds()

    def get_passed(self, obj):
        # 是否及格隐含分数区间，成绩未公开时不能给
        return obj.passed() if obj.score_visible() else None

    def get_can_resume(self, obj) -> bool:
        return obj.can_resume()

    def get_show_score(self, obj) -> bool:
        """成绩是否已经可以对本人公开。"""
        return obj.score_visible()

    def get_score(self, obj):
        """得分与 ``show_score`` 用同一把尺子。

        只给一个 ``show_score: false`` 的开关是不够的——字段照常带宽的话，
        前端一个疏忽（或直接调接口）就把分数拿走了。本项目对答案的做法是
        「字段根本不给」，分数同理。
        """
        return str(obj.score) if obj.score_visible() else None


class AnswerSerializer(serializers.ModelSerializer):
    """作答明细（教师批改 / 学生看自己的答卷时用）。"""

    type_display = serializers.SerializerMethodField()
    stem = serializers.SerializerMethodField()
    reference_answer = serializers.SerializerMethodField()
    analysis = serializers.SerializerMethodField()
    question_type = serializers.SerializerMethodField()

    class Meta:
        model = Answer
        fields = ["id", "paper_question_id", "question_type", "type_display", "stem",
                  "selected_labels", "answer_text", "is_answered", "is_flagged",
                  "is_correct", "score", "ai_score", "ai_comment", "teacher_comment",
                  "time_spent_seconds", "order",
                  "reference_answer", "analysis"]

    def _pq(self, obj):
        return obj.paper_question

    def get_question_type(self, obj) -> str:
        return self._pq(obj).question_type

    def get_type_display(self, obj) -> str:
        return self._pq(obj).get_question_type_display()

    def get_stem(self, obj) -> str:
        return self._pq(obj).question.stem

    def get_reference_answer(self, obj) -> str:
        return self._pq(obj).question.correct_answer_text()

    def get_analysis(self, obj) -> str:
        return self._pq(obj).question.analysis


class AnswerStudentSerializer(serializers.ModelSerializer):
    """学生视角的作答明细：字段里**根本没有**参考答案与解析。

    与「答案防泄漏只有一个出口」是同一条纪律——不靠前端隐藏，而是根本不给。

    学生看答案有两个正当出口，都带门禁：
      * ``GET /attempts/{id}/result/``：交卷后，且考试配置了「立即公布成绩」；
      * 错题本 / 学情报告：只包含已判分的题目。
    考试进行中从 ``/answers/`` 直接拿答案是越权的——该接口曾经复用教师的
    完整序列化器，学生考到一半就能取走全部答案。
    """

    type_display = serializers.SerializerMethodField()
    stem = serializers.SerializerMethodField()
    question_type = serializers.SerializerMethodField()
    # 判分信息按「成绩是否已对本人公开」决定给不给（见 ExamAttempt.score_visible）
    is_correct = serializers.SerializerMethodField()
    score = serializers.SerializerMethodField()
    ai_score = serializers.SerializerMethodField()
    ai_comment = serializers.SerializerMethodField()
    teacher_comment = serializers.SerializerMethodField()

    class Meta:
        model = Answer
        # 与 AnswerSerializer 逐项一致，唯独去掉 reference_answer / analysis
        fields = ["id", "paper_question_id", "question_type", "type_display", "stem",
                  "selected_labels", "answer_text", "is_answered", "is_flagged",
                  "is_correct", "score", "ai_score", "ai_comment", "teacher_comment",
                  "time_spent_seconds", "order"]

    def _score_visible(self, obj) -> bool:
        """逐题对错与得分同受「成绩可见性」约束。

        只删答案与解析是不够的：学生交卷后打开成绩单会触发 ``ensure_graded``
        （只判客观题），随后再拉这个接口就能拿到逐题对错——不含答案文本，
        但在 ``max_attempts > 1`` 时可以用于下一轮针对性作答。
        """
        attempt = getattr(obj, "attempt", None)
        return bool(attempt is not None and attempt.exam and attempt.score_visible())

    def _decimal_str(self, value):
        return None if value is None else str(value)

    def get_is_correct(self, obj):
        return obj.is_correct if self._score_visible(obj) else None

    def get_score(self, obj):
        return self._decimal_str(obj.score) if self._score_visible(obj) else None

    def get_ai_score(self, obj):
        return self._decimal_str(obj.ai_score) if self._score_visible(obj) else None

    def get_ai_comment(self, obj) -> str:
        return (obj.ai_comment or "") if self._score_visible(obj) else ""

    def get_teacher_comment(self, obj) -> str:
        return (obj.teacher_comment or "") if self._score_visible(obj) else ""

    def _pq(self, obj):
        return obj.paper_question

    def get_question_type(self, obj) -> str:
        return self._pq(obj).question_type

    def get_type_display(self, obj) -> str:
        return self._pq(obj).get_question_type_display()

    def get_stem(self, obj) -> str:
        return self._pq(obj).question.stem


# ======================================================================
# 动作入参
# ======================================================================
class ExamStatusChangeSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ExamStatus.choices)


class StartExamSerializer(serializers.Serializer):
    """开考。目前没有必填参数，留出结构以便将来加「确认考前须知」等开关。"""

    confirm = serializers.BooleanField(required=False, default=True)


class AnswerItemSerializer(serializers.Serializer):
    """一道题的作答内容。"""

    paper_question_id = serializers.IntegerField()
    selected_labels = serializers.CharField(required=False, allow_blank=True, max_length=32)
    answer_text = serializers.CharField(required=False, allow_blank=True)
    is_flagged = serializers.BooleanField(required=False)
    time_spent_seconds = serializers.IntegerField(required=False, min_value=0, max_value=86400)


class SaveAnswerSerializer(AnswerItemSerializer):
    """单题暂存（和批量共用一个结构，少定义一份契约）。"""


class SaveAnswersSerializer(serializers.Serializer):
    """批量暂存（自动保存 / 交卷前落盘）。"""

    items = AnswerItemSerializer(many=True, allow_empty=False, max_length=500)


class HeartbeatSerializer(serializers.Serializer):
    client_remaining = serializers.IntegerField(required=False, min_value=0, max_value=86400 * 2)


class BehaviorEventSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=BehaviorEvent.choices)
    count = serializers.IntegerField(required=False, min_value=1, max_value=100, default=1)
    detail = serializers.CharField(required=False, allow_blank=True, max_length=200)


class BehaviorReportSerializer(serializers.Serializer):
    events = BehaviorEventSerializer(many=True, allow_empty=False, max_length=200)


class SubmitSerializer(serializers.Serializer):
    """交卷。

    ``reason`` 只作为「学生自述」记录下来，最终以服务端判定的超时与否为准。
    ``items`` 让前端在交卷的同一次请求里带上最后一批作答——「点了交卷但最后一题
    没存上」是很常见的丢分事故，多带一个字段就能彻底避免。
    """

    reason = serializers.ChoiceField(choices=SubmitReason.choices,
                                     required=False, default=SubmitReason.MANUAL)
    items = AnswerItemSerializer(many=True, required=False, allow_empty=True, max_length=500)


__all__ = [
    "AnswerItemSerializer",
    "AnswerSerializer",
    "BehaviorReportSerializer",
    "ExamAttemptSerializer",
    "ExamAttemptStudentSerializer",
    "ExamDetailSerializer",
    "ExamListSerializer",
    "ExamStatusChangeSerializer",
    "ExamStudentSerializer",
    "ExamWriteSerializer",
    "HeartbeatSerializer",
    "SaveAnswerSerializer",
    "SaveAnswersSerializer",
    "StartExamSerializer",
    "SubmitSerializer",
]
