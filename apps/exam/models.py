"""在线考试模块的模型。

三张表，职责层层收窄：

    Exam          考试    —— 时间窗 + 时长 + 防作弊开关 + 参考范围（引用试卷）
    ExamAttempt   作答    —— 一次作答全部过程数据（状态机 / 计时锚点 / 行为计数）
    Answer        单题    —— 某人在某卷某题上的原始作答与判分结果

三个刻意的设计：

1. **服务端是唯一的计时权威。**
   ``deadline_at`` 在「开始考试」那一刻算好并落库，之后前端倒计时只是显示。
   每次暂存 / 交卷都用服务端当前时间校验——学生改自己电脑的时钟没有用。

2. **答案只存在 ``Answer`` 表里。**
   考试下发一律走 ``ExamAttemptService`` 且题目内容委托 ``Question.student_view()``，
   不新开第二条能把答案带出去的路径（模块二是靠序列化器分层防泄漏，这里同理）。

3. **行为计数聚合在 attempt 上，明细留到模块八。**
   模块四的用途是「切了几次屏 → 超没超阈值 → 要不要强制交卷」，聚合值就够了；
   逐条埋点（含时间戳、停留时长）是模块八 ``ExamBehaviorLog`` 的活。
   提前把明细表建出来会挖一个当下没人填的坑。
"""

import random

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.constants import AttemptStatus, ExamStatus, Role
from apps.common.models import TimeStampedModel

from .constants import (
    DEFAULT_DURATION_MINUTES,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_SWITCH_COUNT,
    DEFAULT_MULTIPLE_CHOICE_POLICY,
    SUBMIT_GRACE_SECONDS,
    MultipleChoicePolicy,
    SubmitReason,
)


class ExamQuerySet(models.QuerySet):
    """考试查询集。"""

    def active(self):
        """未归档（默认列表用）。"""
        return self.exclude(status=ExamStatus.ARCHIVED)

    def of_status(self, status: str):
        return self.filter(status=status) if status else self

    def of_subject(self, subject: str):
        return self.filter(subject=subject) if subject else self

    def visible(self):
        """对学生可见的状态：已发布或进行中（草稿 / 已结束 / 已归档都不该被看到）。"""
        return self.filter(status__in=[ExamStatus.PUBLISHED, ExamStatus.ONGOING])

    def search(self, keyword: str):
        if not keyword:
            return self
        return self.filter(
            models.Q(title__icontains=keyword)
            | models.Q(description__icontains=keyword)
            | models.Q(subject__icontains=keyword)
        )

    def for_student(self, user):
        """某个学生能参加的考试。

        两重过滤：
          * 状态可见（已发布 / 进行中）；
          * 参考范围命中——要么被显式指定为考生，要么 ``open_to_all`` 且落在
            「全体」的实际范围内（见 ``Exam.open_to_all_scope``：教师创建的
            「面向全体」只对自己可管理班级里的学生开放，管理员/无主才是全校）。
        注意：这里**不看时间窗**。时间没到就还看不到，会把「考试预告」一起挡掉，
        而预告本身是学生需要的信息；是否可开始答题由 ``Exam.is_open()`` 判定。
        """
        queryset = self.visible()
        if user is None or not getattr(user, "is_authenticated", False):
            return queryset.none()

        from django.db.models import Exists, OuterRef, Q

        from apps.classroom.models import Classroom

        # 「面向全体」对教师而言 = 该教师可管理班级里的学生；用 Exists 表达成一条子查询，
        # 这样 open_to_all 的考试也能在 queryset 层过滤，而不是逐条回退到 Python 判断。
        in_creator_scope = Exists(
            Classroom.objects.filter(students=user).filter(
                Q(created_by=OuterRef("created_by"))
                | Q(head_teacher=OuterRef("created_by"))
                | Q(teachers=OuterRef("created_by"))
            )
        )
        open_all_visible = models.Q(open_to_all=True) & (
            ~models.Q(created_by__role=Role.TEACHER) | in_creator_scope
        )
        return queryset.filter(open_all_visible | models.Q(participants=user)).distinct()

    def with_relations(self):
        """列表页统一用这个，避免 N+1。"""
        return self.select_related("paper", "created_by").prefetch_related("participants")

    def with_counts(self):
        """列表页一次把三个计数算出来，避免每行都回查一次。"""
        return self.annotate(
            participant_total=models.Count("participants", distinct=True),
            attempt_total=models.Count("attempts", distinct=True),
            in_progress_total=models.Count(
                "attempts", filter=models.Q(attempts__status=AttemptStatus.IN_PROGRESS),
                distinct=True,
            ),
        )


class Exam(TimeStampedModel):
    """一场考试。

    「考试」与「试卷」刻意分成两张表：一份卷子可以考很多次（不同班级、不同时间、
    补考），而每次考试的时长、时间窗、防作弊策略可能都不一样。把时长写死在试卷上，
    就再也做不了一次「只考 30 分钟的随堂测」。
    """

    title = models.CharField("考试名称", max_length=200, db_index=True)
    description = models.TextField("考试说明", blank=True, default="",
                                   help_text="考前须知，会显示在学生答题页顶部")
    subject = models.CharField("学科/课程", max_length=64, blank=True, default="", db_index=True)

    paper = models.ForeignKey(
        "papers.Paper", verbose_name="试卷", on_delete=models.PROTECT,
        related_name="exams", help_text="已发布的试卷才能开考",
    )

    status = models.CharField("状态", max_length=16, choices=ExamStatus.choices,
                              default=ExamStatus.DRAFT, db_index=True)

    # ---- 时间窗 ----
    start_time = models.DateTimeField("开始时间", null=True, blank=True,
                                      help_text="留空表示发布后立即可考")
    end_time = models.DateTimeField("结束时间", null=True, blank=True,
                                    help_text="留空表示不限结束时间；到时后自动禁止进入")
    duration_minutes = models.PositiveIntegerField(
        "考试时长（分钟）", default=0, help_text="0 表示沿用试卷时长",
    )

    # ---- 计分与次数 ----
    pass_score = models.DecimalField(
        "及格分", max_digits=7, decimal_places=2, null=True, blank=True,
        help_text="留空表示沿用试卷及格分",
    )
    max_attempts = models.PositiveIntegerField("允许作答次数", default=DEFAULT_MAX_ATTEMPTS)

    # 多选题给分策略：漏选/错选怎么算分是教学策略，做成每场考试可配
    multiple_choice_policy = models.CharField(
        "多选题给分策略", max_length=16, choices=MultipleChoicePolicy.choices,
        default=DEFAULT_MULTIPLE_CHOICE_POLICY,
        help_text="严格=全对才得分；漏选部分分=错选不得分；倒扣=错选从正确项里减",
    )

    # ---- 卷面随机化 ----
    shuffle_questions = models.BooleanField(
        "题目乱序", default=False, help_text="同一场考试不同学生看到的题序不同（按作答记录固定，刷新不变）",
    )
    shuffle_options = models.BooleanField(
        "选项乱序", default=False, help_text="选择题选项顺序随机，但选项标号仍跟着内容走",
    )

    # ---- 防作弊开关（模块四只做采集与阈值判定，深度检测在模块八）----
    detect_switch = models.BooleanField("检测切屏", default=True)
    max_switch_count = models.PositiveIntegerField(
        "允许切屏次数", default=DEFAULT_MAX_SWITCH_COUNT, help_text="超过该次数记违规",
    )
    auto_submit_on_limit = models.BooleanField(
        "超限自动交卷", default=False, help_text="切屏超限时立即强制交卷；关闭则只记违规"
    )
    detect_paste = models.BooleanField("检测粘贴", default=True)
    fullscreen_required = models.BooleanField("要求全屏", default=False)

    # ---- 其他策略 ----
    allow_resume = models.BooleanField(
        "允许断网续考", default=True, help_text="关闭后中途离开页面即视为本次作答作废",
    )
    show_score_immediately = models.BooleanField(
        "交卷后立即显示得分", default=False,
        help_text="依赖模块五的判分结果；未判分时仅显示「已交卷」",
    )

    # ---- 参考范围 ----
    open_to_all = models.BooleanField("面向全体学生", default=True)
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL, verbose_name="指定考生", blank=True,
        related_name="exams_to_take", db_table="exam_participant",
        help_text="open_to_all=False 时生效",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="创建人", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="created_exams",
    )
    published_at = models.DateTimeField("发布时间", null=True, blank=True)

    objects = ExamQuerySet.as_manager()

    class Meta:
        db_table = "exam"
        verbose_name = "考试"
        verbose_name_plural = "考试"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-start_time"], name="idx_exam_status_start"),
            models.Index(fields=["subject", "status"], name="idx_exam_subject_status"),
            models.Index(fields=["paper"], name="idx_exam_paper"),
        ]

    def __str__(self):
        return self.title

    # ------------------------------------------------------------------
    # 时长与时间窗
    # ------------------------------------------------------------------
    @property
    def effective_duration_minutes(self) -> int:
        """实际考试时长：考试上没写就用试卷的。"""
        return int(self.duration_minutes or 0) or int(self.paper.duration_minutes or DEFAULT_DURATION_MINUTES)

    @property
    def effective_pass_score(self):
        """实际及格分：考试上没写就用试卷的。"""
        if self.pass_score is not None:
            return self.pass_score
        return self.paper.pass_score

    @property
    def total_score(self):
        """满分——以试卷为准（考试不该改变满分，改了就不是同一份卷子了）。"""
        return self.paper.total_score

    @property
    def question_count(self) -> int:
        return self.paper.question_count

    def is_within_window(self, now=None) -> bool:
        """当前是否处于可作答时间窗内。"""
        now = now or timezone.now()
        if self.start_time and now < self.start_time:
            return False
        if self.end_time and now > self.end_time:
            return False
        return True

    def computed_status(self, now=None) -> str:
        """按当前时间算出的「应该是」什么状态。返回值一律是普通字符串。

        已发布 + 在时间窗内 = 进行中。这个值不落库（除非显式调 ``sync_status``），
        因为它是时间的函数——时间一直在走，状态却不需要每次都写数据库。
        只有草稿 / 已结束 / 已归档是人工或终态，时间推不动它们。
        """
        now = now or timezone.now()
        if self.status in (ExamStatus.DRAFT, ExamStatus.FINISHED, ExamStatus.ARCHIVED):
            return str(self.status)
        if self.start_time and now < self.start_time:
            return str(ExamStatus.PUBLISHED)
        if self.end_time and now > self.end_time:
            return str(ExamStatus.FINISHED)
        return str(ExamStatus.ONGOING)

    def is_open(self, now=None) -> bool:
        """此刻能不能开考 / 继续答题。"""
        now = now or timezone.now()
        if self.status not in (ExamStatus.PUBLISHED, ExamStatus.ONGOING):
            return False
        if not self.is_within_window(now):
            return False
        return bool(self.paper.question_count) and self.paper.status != "archived"

    def open_to_all_scope(self):
        """「面向全体」的实际参考范围。

        管理员 / 无创建人 → 全部在册学生；
        教师 → 该教师可管理班级（自己创建 / 任班主任 / 授课）里的学生。
        学科归属已经隐含在班级关系里（教师只能建/教自己学科的班），无需再按
        ``subject`` 二次过滤——那反而会因「班级 subject 与考试 subject 写法不一致」
        把该覆盖的学生漏掉。
        """
        from django.contrib.auth import get_user_model

        from apps.classroom.models import Classroom

        creator = self.created_by
        if creator and getattr(creator, "is_teacher", False) and not getattr(creator, "is_admin", False):
            return get_user_model().objects.filter(
                classrooms__in=Classroom.objects.manageable_by(creator),
                role=Role.STUDENT, is_active=True,
            ).distinct()
        return get_user_model().objects.filter(role=Role.STUDENT, is_active=True)

    def open_to(self, user) -> bool:
        """这个学生在参考范围内吗。"""
        if self.open_to_all:
            return self.open_to_all_scope().filter(pk=getattr(user, "pk", None)).exists()
        return self.participants.filter(pk=getattr(user, "pk", None)).exists()

    # ------------------------------------------------------------------
    # 统计辅助
    # ------------------------------------------------------------------
    def attempt_count(self) -> int:
        return self.attempts.count()

    def participant_count(self) -> int:
        """应考人数：面向全体时按「全体」的实际范围算，否则按指定名单算。"""
        if self.open_to_all:
            return self.open_to_all_scope().count()
        return self.participants.count()

    def my_attempt(self, student):
        """某个学生最近一次作答（没有则 None）。"""
        if student is None or not getattr(student, "is_authenticated", False):
            return None
        return self.attempts.filter(student=student).order_by("-attempt_no", "-id").first()

    def my_attempt_count(self, student) -> int:
        if student is None or not getattr(student, "is_authenticated", False):
            return 0
        return self.attempts.filter(student=student).count()


class ExamAttemptQuerySet(models.QuerySet):
    """作答记录查询集。"""

    def in_progress(self):
        return self.filter(status=AttemptStatus.IN_PROGRESS)

    def submitted(self):
        return self.filter(status__in=[
            AttemptStatus.SUBMITTED, AttemptStatus.AUTO_GRADED,
            AttemptStatus.REVIEWING, AttemptStatus.GRADED, AttemptStatus.TIMEOUT,
        ])

    def of_exam(self, exam):
        return self.filter(exam=exam)

    def of_student(self, student):
        return self.filter(student=student)

    def with_relations(self):
        return self.select_related("exam", "exam__paper", "student")

    def flagged(self):
        """违规嫌疑：切屏或粘贴次数超阈值（阈值各处不同，这里只给可排序的原始计数）。"""
        return self.filter(models.Q(switch_count__gt=0) | models.Q(paste_count__gt=0))


class ExamAttempt(TimeStampedModel):
    """一次作答记录——考试模块的核心状态机。

    状态流转：
        答题中 ──交卷──► 已交卷 ──(模块五判分)──► 自动判分完成 ──► 待人工复核 ──► 已出分
           │
           └──超时──► 超时交卷（同样进入判分链路）

    模块四只负责把记录推到「已交卷 / 超时交卷」；判分与出分是模块五的事。
    """

    exam = models.ForeignKey(Exam, verbose_name="考试", on_delete=models.CASCADE,
                             related_name="attempts")
    student = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="考生",
                                on_delete=models.CASCADE, related_name="exam_attempts")
    attempt_no = models.PositiveIntegerField("第几次作答", default=1)

    status = models.CharField("状态", max_length=16, choices=AttemptStatus.choices,
                              default=AttemptStatus.IN_PROGRESS, db_index=True)

    # ---- 计时锚点（服务端权威）----
    started_at = models.DateTimeField("开始时间", db_index=True)
    deadline_at = models.DateTimeField("截止时间", db_index=True,
                                       help_text="开始时间 + 时长，并被考试结束时间封顶")
    submitted_at = models.DateTimeField("交卷时间", null=True, blank=True)
    graded_at = models.DateTimeField("判分时间", null=True, blank=True)
    time_spent_seconds = models.PositiveIntegerField("实际用时（秒）", default=0)

    # ---- 得分（模块五填充；模块四只留快照与占位）----
    total_score_snapshot = models.DecimalField("满分快照", max_digits=7, decimal_places=2,
                                               default=0)
    score = models.DecimalField("得分", max_digits=7, decimal_places=2, default=0)
    objective_score = models.DecimalField("客观题得分", max_digits=7, decimal_places=2, default=0)
    subjective_score = models.DecimalField("主观题得分", max_digits=7, decimal_places=2, default=0)

    question_count = models.PositiveIntegerField("题目数量", default=0)
    answered_count = models.PositiveIntegerField("已作答数", default=0)

    # ---- 行为计数（防切屏 / 防粘贴）----
    switch_count = models.PositiveIntegerField("切屏次数", default=0)
    paste_count = models.PositiveIntegerField("粘贴次数", default=0)
    fullscreen_exit_count = models.PositiveIntegerField("退出全屏次数", default=0)
    heartbeat_count = models.PositiveIntegerField("心跳次数", default=0)
    last_heartbeat_at = models.DateTimeField("最后心跳", null=True, blank=True)

    # ---- 环境信息 ----
    ip_address = models.CharField("IP", max_length=45, blank=True, default="")
    user_agent = models.CharField("User-Agent", max_length=255, blank=True, default="")

    # ---- 结束信息 ----
    is_timeout = models.BooleanField("是否超时", default=False)
    submit_reason = models.CharField("交卷原因", max_length=16, choices=SubmitReason.choices,
                                     blank=True, default="")

    # 乱序种子：同一份作答每次刷新都要还原成同一顺序，所以种子必须固定下来
    shuffle_seed = models.IntegerField("乱序种子", default=0)

    objects = ExamAttemptQuerySet.as_manager()

    class Meta:
        db_table = "exam_attempt"
        verbose_name = "作答记录"
        verbose_name_plural = "作答记录"
        ordering = ["-started_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["exam", "student", "attempt_no"],
                                    name="uniq_attempt_exam_student_no"),
        ]
        indexes = [
            models.Index(fields=["exam", "status"], name="idx_attempt_exam_status"),
            models.Index(fields=["student", "-started_at"], name="idx_attempt_student_started"),
        ]

    def __str__(self):
        return f"{self.exam_id}-{self.student_id}#{self.attempt_no}"

    # ------------------------------------------------------------------
    # 计时
    # ------------------------------------------------------------------
    def remaining_seconds(self, now=None) -> int:
        """剩余秒数。已交卷返回 0。"""
        if self.status != AttemptStatus.IN_PROGRESS:
            return 0
        now = now or timezone.now()
        return max(0, int((self.deadline_at - now).total_seconds()))

    def is_expired(self, now=None) -> bool:
        """是否已超时（含宽限期）。"""
        now = now or timezone.now()
        return now > self.deadline_at

    def is_past_grace(self, now=None) -> bool:
        """是否连宽限期都过了——过了就只受理交卷，不再受理暂存。"""
        now = now or timezone.now()
        return now > self.deadline_at + timezone.timedelta(seconds=SUBMIT_GRACE_SECONDS)

    # ------------------------------------------------------------------
    # 状态便捷判断
    # ------------------------------------------------------------------
    @property
    def is_in_progress(self) -> bool:
        return self.status == AttemptStatus.IN_PROGRESS

    @property
    def is_finished(self) -> bool:
        return self.status in (
            AttemptStatus.SUBMITTED, AttemptStatus.AUTO_GRADED,
            AttemptStatus.REVIEWING, AttemptStatus.GRADED, AttemptStatus.TIMEOUT,
        )

    @property
    def is_graded(self) -> bool:
        return self.status in (AttemptStatus.AUTO_GRADED, AttemptStatus.GRADED)

    def score_visible(self) -> bool:
        """成绩是否已可对**本人**公开 —— 全项目唯一口径。

        规则（模块五定的，比模块四多一层）：

            ``GRADED``      —— 教师已出分 → **一定可见**。出分这个动作本身就是
                               「发布成绩」；否则教师复核完学生还是看不到分，人工
                               复核那条链路就白做了；
            ``AUTO_GRADED`` —— 自动判完 → 仅当考试配置了「交卷后立即显示得分」；
            其余            —— 不可见。

        注解：这里**曾经**写成 ``is_graded and show_score_immediately``，
        而 ``grading`` 侧另写了一份同样的判断（GRADED 恒 True）。两份在 GRADED 上
        结论相反，导致同一份作答在 ``/grading/attempts/{id}/report/`` 显示
        ``visible=true``、在 ``/attempts/{id}/answers/`` 却是 ``score=null``。
        现在规则只住在这里，评分侧那份改为委托（``ScoreReportService.score_visible``）。
        """
        if self.status == AttemptStatus.GRADED:
            return True
        if self.status == AttemptStatus.AUTO_GRADED:
            return bool(self.exam.show_score_immediately)
        return False

    def can_resume(self, now=None) -> bool:
        """能不能接着答（断网续考判定）。"""
        return self.is_in_progress and self.exam.allow_resume and not self.is_past_grace(now)

    def progress(self) -> dict:
        """作答进度，给进度条用。"""
        total = self.question_count or 0
        answered = self.answered_count or 0
        return {
            "answered": answered,
            "total": total,
            "unanswered": max(0, total - answered),
            "percent": round(answered * 100.0 / total, 1) if total else 0.0,
        }

    def score_float(self) -> float:
        return float(self.score or 0)

    def total_score_float(self) -> float:
        return float(self.total_score_snapshot or 0)

    def passed(self) -> bool:
        """是否及格。未判分时返回 None（不能拿 0 分假装没及格）。"""
        if not self.is_graded:
            return None
        return float(self.score or 0) >= float(self.exam.effective_pass_score or 0)

    # ------------------------------------------------------------------
    # 随机化（确定性乱序）
    # ------------------------------------------------------------------
    def shuffled_questions(self, items: list) -> list:
        """按作答记录固定的种子打乱题序。

        为什么按 **attempt** 而不是按请求随机：学生刷新页面、断网重连、
        换设备续考，看到的题序必须一致，否则「第 5 题」到底指哪道题就说不清了。
        """
        if not self.exam.shuffle_questions:
            return items
        result = list(items)
        random.Random(self.shuffle_seed or 1).shuffle(result)
        return result

    def shuffled_options(self, paper_question_id: int, options: list) -> list:
        """选项乱序。种子掺入题号，保证每题顺序不同但每次都一样。"""
        if not self.exam.shuffle_options:
            return options
        result = list(options)
        random.Random((self.shuffle_seed or 1) * 1000003 + int(paper_question_id)).shuffle(result)
        return result


class Answer(TimeStampedModel):
    """一道题的作答。

    存放约定（与题库模块的答案存放约定一一对应，判分时才不会错位）：
      * 单选 / 多选 / 判断 —— ``selected_labels`` 存选项字母（如 "ABD"，已排序去重）；
      * 填空 —— ``answer_text`` 每个空一行，与 ``Question.reference_answer`` 的行序对应；
      * 简答 / 论述 —— ``answer_text`` 是整段作答文本。

    ``is_correct`` 用 **null 表示未判分**。这一点很关键：拿 False 当默认值的话，
    「还没判分」和「判错了」在数据库里长得一样，模块五的批改工作台就分不清该拉哪些。
    """

    attempt = models.ForeignKey(ExamAttempt, verbose_name="作答记录", on_delete=models.CASCADE,
                                related_name="answers")
    paper_question = models.ForeignKey(
        "papers.PaperQuestion", verbose_name="卷内题目", on_delete=models.PROTECT,
        related_name="answers",
    )

    # ---- 原始作答 ----
    selected_labels = models.CharField("所选选项", max_length=32, blank=True, default="",
                                       help_text="选择题 / 判断题的字母，已排序去重")
    answer_text = models.TextField("作答内容", blank=True, default="",
                                   help_text="填空按行分空，简答论述为整段文本")
    is_flagged = models.BooleanField("标记待复查", default=False)
    is_answered = models.BooleanField("已作答", default=False, db_index=True)

    # ---- 判分结果（模块五填充）----
    is_correct = models.BooleanField("是否正确", null=True, blank=True, default=None,
                                     db_index=True)
    score = models.DecimalField("得分", max_digits=6, decimal_places=2, default=0)
    ai_score = models.DecimalField("AI 建议分", max_digits=6, decimal_places=2, null=True, blank=True)
    ai_comment = models.TextField("AI 评语", blank=True, default="")
    teacher_comment = models.TextField("教师评语", blank=True, default="")

    time_spent_seconds = models.PositiveIntegerField("本题用时（秒）", default=0)
    order = models.PositiveIntegerField("题序快照", default=0)

    class Meta:
        db_table = "exam_answer"
        verbose_name = "作答明细"
        verbose_name_plural = "作答明细"
        ordering = ["attempt", "order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["attempt", "paper_question"],
                                    name="uniq_answer_attempt_question"),
        ]
        indexes = [
            models.Index(fields=["attempt", "is_answered"], name="idx_answer_attempt_answered"),
            models.Index(fields=["paper_question"], name="idx_answer_paper_question"),
        ]

    def __str__(self):
        return f"{self.attempt_id}-{self.paper_question_id}"

    # ------------------------------------------------------------------
    @property
    def question(self):
        """对应的题库题目（判分要读参考答案与选项）。"""
        return self.paper_question.question

    def answer_text_display(self) -> str:
        return (self.answer_text or "").strip()

    def to_payload(self) -> dict:
        """回给前端的作答数据（续考恢复用）。"""
        return {
            "paper_question_id": self.paper_question_id,
            "selected_labels": self.selected_labels or "",
            "answer_text": self.answer_text or "",
            "is_flagged": self.is_flagged,
            "is_answered": self.is_answered,
            "time_spent_seconds": self.time_spent_seconds,
            "order": self.order,
        }


__all__ = ["Answer", "Exam", "ExamAttempt", "ExamAttemptQuerySet", "ExamQuerySet"]
