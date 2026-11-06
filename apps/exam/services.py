"""在线考试服务层。

三个服务，边界刻意划得很清：

    ExamService           考试的写操作与状态机（谁能改、什么时候能开考）
    ExamAttemptService    作答全过程（开考 / 续考 / 暂存 / 心跳 / 行为 / 交卷）
    ExamAnalysisService   把作答记录算成教师看板能直接用的数据

本模块最重要的一条纪律：**计时与答案过滤都只有一处实现**。

  * 计时：``deadline_at`` 在开考那一刻算好落库，之后所有判断都读它，
    前端传上来的剩余时间只作为「客户端此刻是否卡死」的参考，绝不作为判据。
  * 答案：下发题目的唯一出口是 ``_question_payload()``，它只转出
    ``PaperQuestion.student_payload()`` 里白名单的几个字段。谁想把答案带出去，
    必须改这一处——评审时只看一个函数就够了。
"""

import logging
import random
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Avg, Max, Min, Q, Sum
from django.utils import timezone

from apps.common.constants import AttemptStatus, ExamStatus
from apps.common.exceptions import BusinessError
from apps.papers.models import PaperQuestion
from apps.questions.constants import CHOICE_TYPES

from .constants import (
    BEHAVIOR_COUNTER_FIELDS,
    DEFAULT_MULTIPLE_CHOICE_POLICY,
    SUBMIT_GRACE_SECONDS,
    BehaviorEvent,
    SubmitReason,
)
from .models import Answer, Exam, ExamAttempt

logger = logging.getLogger("apps.exam")

ZERO = Decimal("0.00")


def _to_decimal(value, default=ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return default


def _fmt_dt(value) -> str:
    """统一按项目时区格式化成字符串。

    库里存的是 UTC（``USE_TZ=True``），直接 strftime 出来会比北京时间早 8 小时——
    学生看到「截止 05:42」而实际是 13:42，这种错必须在这里一次挡住。
    """
    if not value:
        return ""
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _answered(question_type: str, selected_labels: str, answer_text: str) -> bool:
    """这道题算不算「已作答」。

    **判据是「答案存在哪个字段」，不是「能不能机器判分」。**
    这里曾经写成错的一版：拿 ``QuestionType.objective_types()``（含填空题）去判断，
    结果填空题只填了文本、没勾选项，就被当成没作答，进度条凭空少一题。
    填空题虽然可精确判分，但答案同样存在 ``answer_text`` 里——
    只有单选 / 多选 / 判断这三种「勾选项」的题型才看 ``selected_labels``。

    另外，纯空白文本不算作答：否则学生随手敲个空格就把进度条刷满，
    真正漏答的题反而被掩盖了。
    """
    if question_type in CHOICE_TYPES:
        return bool((selected_labels or "").strip())
    return bool((answer_text or "").strip())


def _normalize_labels(raw) -> str:
    """把选项字母标准化：大写、去重、排序。

    排序是为了判分时能直接做字符串比较（"BA" 和 "AB" 是同一个答案）。
    """
    if not raw:
        return ""
    if isinstance(raw, (list, tuple, set)):
        chars = [str(item) for item in raw]
    else:
        chars = [ch for ch in str(raw)]
    cleaned = {ch.strip().upper() for ch in chars if ch and ch.strip()}
    return "".join(sorted(cleaned))


# ======================================================================
# 考试本体
# ======================================================================
class ExamService:
    """考试的写操作。"""

    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def create(*, data: dict, user=None) -> Exam:
        """新建一场考试（草稿态）。"""
        paper = data.get("paper")
        if paper is None:
            raise BusinessError("必须选择一份试卷")

        payload = dict(data)
        participant_ids = payload.pop("participant_ids", None) or []

        exam = Exam.objects.create(
            title=(payload.get("title") or "").strip() or f"{paper.title} · 考试",
            description=payload.get("description") or "",
            subject=payload.get("subject") or paper.subject or "",
            paper=paper,
            status=ExamStatus.DRAFT,
            start_time=payload.get("start_time"),
            end_time=payload.get("end_time"),
            duration_minutes=int(payload.get("duration_minutes") or 0),
            pass_score=payload.get("pass_score"),
            max_attempts=int(payload.get("max_attempts") or 1),
            multiple_choice_policy=payload.get("multiple_choice_policy")
            or DEFAULT_MULTIPLE_CHOICE_POLICY,
            shuffle_questions=bool(payload.get("shuffle_questions", False)),
            shuffle_options=bool(payload.get("shuffle_options", False)),
            detect_switch=bool(payload.get("detect_switch", True)),
            max_switch_count=int(payload.get("max_switch_count") or 5),
            auto_submit_on_limit=bool(payload.get("auto_submit_on_limit", False)),
            detect_paste=bool(payload.get("detect_paste", True)),
            fullscreen_required=bool(payload.get("fullscreen_required", False)),
            allow_resume=bool(payload.get("allow_resume", True)),
            show_score_immediately=bool(payload.get("show_score_immediately", False)),
            open_to_all=bool(payload.get("open_to_all", True)),
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

        if participant_ids:
            exam.participants.set(participant_ids)
            exam.open_to_all = False
            exam.save(update_fields=["open_to_all", "updated_at"])

        ExamService._validate_window(exam)
        logger.info("创建考试 #%s《%s》", exam.id, exam.title)
        return exam

    @staticmethod
    @transaction.atomic
    def update(exam: Exam, *, data: dict) -> Exam:
        """改考试设置。

        已经有人开始答题之后，**不再允许改试卷**——否则先考的人答的是 A 卷、
        后考的人答的是 B 卷，成绩完全不可比。其他字段仍可调整。
        """
        payload = dict(data)
        participant_ids = payload.pop("participant_ids", None)

        if "paper" in payload and payload["paper"] is not None and payload["paper"].pk != exam.paper_id:
            if exam.attempts.exists():
                raise BusinessError("已有考生开始作答，不能再更换试卷")
            exam.paper = payload["paper"]

        scalar_fields = [
            "title", "description", "subject", "start_time", "end_time",
            "duration_minutes", "pass_score", "max_attempts", "multiple_choice_policy",
            "shuffle_questions", "shuffle_options",
            "detect_switch", "max_switch_count", "auto_submit_on_limit",
            "detect_paste", "fullscreen_required",
            "allow_resume", "show_score_immediately", "open_to_all",
        ]
        for field in scalar_fields:
            if field not in payload:
                continue
            value = payload[field]
            if value is None and field not in ("start_time", "end_time", "pass_score"):
                continue
            setattr(exam, field, value)

        if exam.title is not None:
            exam.title = (exam.title or "").strip() or exam.title

        if participant_ids is not None:
            exam.participants.set(participant_ids)
            exam.open_to_all = not participant_ids
            exam.save(update_fields=["open_to_all", "updated_at"])

        exam.save()
        ExamService._validate_window(exam)
        return exam

    @staticmethod
    @transaction.atomic
    def delete(exam: Exam) -> None:
        """删考试。已经有人作答过的不能删——那是成绩凭据。"""
        if exam.attempts.exists():
            raise BusinessError("已有作答记录的考试不能删除，请改为「归档」")
        title = exam.title
        exam.delete()
        logger.info("删除考试《%s》", title)

    # ------------------------------------------------------------------
    @staticmethod
    def _validate_window(exam: Exam) -> None:
        if exam.start_time and exam.end_time and exam.end_time <= exam.start_time:
            raise BusinessError("结束时间必须晚于开始时间")
        if exam.start_time and exam.end_time:
            span = (exam.end_time - exam.start_time).total_seconds() / 60
            duration = exam.effective_duration_minutes
            if span < 1:
                raise BusinessError("考试时间窗不足 1 分钟")
            # 时间窗比考试时长还短时，学生的实际可用时间会被结束时间截断——
            # 这不是错误，但值得提醒教师，所以只放行、由前端给提示。
            if span < duration:
                logger.warning(
                    "考试 #%s 时间窗（%.0f 分钟）短于考试时长（%s 分钟），将以结束时间封顶",
                    exam.id, span, duration,
                )

    @staticmethod
    @transaction.atomic
    def change_status(exam: Exam, status: str) -> Exam:
        """状态流转。"""
        current = exam.status
        if status == current:
            return exam

        allowed = {
            ExamStatus.DRAFT: {ExamStatus.PUBLISHED, ExamStatus.ARCHIVED},
            ExamStatus.PUBLISHED: {ExamStatus.DRAFT, ExamStatus.ONGOING, ExamStatus.FINISHED,
                                   ExamStatus.ARCHIVED},
            ExamStatus.ONGOING: {ExamStatus.FINISHED, ExamStatus.ARCHIVED},
            ExamStatus.FINISHED: {ExamStatus.ARCHIVED, ExamStatus.PUBLISHED},
            ExamStatus.ARCHIVED: {ExamStatus.DRAFT},
        }
        if status not in allowed.get(current, set()):
            raise BusinessError(f"不允许从「{exam.get_status_display()}」切换到该状态")

        if status == ExamStatus.PUBLISHED:
            ExamService._validate_publishable(exam)
            if not exam.published_at:
                exam.published_at = timezone.now()

        exam.status = status
        exam.save(update_fields=["status", "published_at", "updated_at"])
        logger.info("考试 #%s 状态：%s → %s", exam.id, current, status)
        return exam

    @staticmethod
    def _validate_publishable(exam: Exam) -> None:
        """发布前的硬校验。发出去才发现问题，比不让发布难受得多。"""
        if exam.paper.question_count <= 0:
            raise BusinessError("试卷里还没有题目，不能发布考试")
        if exam.paper.status == "draft":
            raise BusinessError("引用的试卷还是草稿状态，请先在试卷模块发布它")
        if exam.max_attempts < 1:
            raise BusinessError("允许作答次数至少为 1")
        if not exam.open_to_all and not exam.participants.exists():
            raise BusinessError("已设置「指定考生」但没有选择任何学生")

    @staticmethod
    def sync_status(exam: Exam, now=None) -> Exam:
        """按时间推进状态（进行中 / 已结束）。惰性调用，不作为定时任务依赖。"""
        computed = exam.computed_status(now)
        if computed != exam.status and computed in (ExamStatus.ONGOING, ExamStatus.FINISHED):
            exam.status = computed
            exam.save(update_fields=["status", "updated_at"])
        return exam

    # ------------------------------------------------------------------
    @staticmethod
    def available_for(student):
        """学生可参加的考试（列表页用）。"""
        return Exam.objects.with_relations().for_student(student)


# ======================================================================
# 作答
# ======================================================================
class ExamAttemptService:
    """考试作答全过程。"""

    # ------------------------------------------------------------------
    # 开考
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def start(*, exam: Exam, student, ip: str = "", user_agent: str = "") -> dict:
        """开始 / 继续作答。

        返回 ``{"attempt": ..., "resumed": bool}``。**续考优先**：
        如果这个学生已经有一次进行中的作答，直接返回它而不是新建——
        这正是「断网后重新打开页面还能接着答」的实现方式。
        """
        now = timezone.now()
        ExamService.sync_status(exam, now)

        if not exam.open_to(student):
            raise BusinessError("你不在本场考试的参考范围内", code=10431, status_code=403)
        if not exam.is_open(now):
            raise BusinessError("本场考试当前不可作答（未开始、已结束或已下线）")

        # 1) 续考：有进行中的作答就直接接着答
        ongoing = exam.attempts.filter(student=student, status=AttemptStatus.IN_PROGRESS).first()
        if ongoing:
            if not exam.allow_resume:
                # 不允许续考 = 离开即作废，这里把旧的收尾再开一次新的
                ExamAttemptService.submit(ongoing, reason=SubmitReason.SYSTEM, now=now)
            elif not ongoing.is_past_grace(now):
                return {"attempt": ongoing, "resumed": True}

        # 2) 次数上限
        used = exam.attempts.filter(student=student).count()
        if used >= exam.max_attempts:
            raise BusinessError(f"本场考试最多作答 {exam.max_attempts} 次，你已用完")

        # 3) 算截止时间：开始 + 时长，再被考试结束时间封顶
        deadline = now + timezone.timedelta(minutes=exam.effective_duration_minutes)
        if exam.end_time and exam.end_time < deadline:
            deadline = exam.end_time
        if deadline <= now:
            raise BusinessError("距离考试结束已不足一秒，无法开始作答")

        attempt = ExamAttempt.objects.create(
            exam=exam,
            student=student,
            attempt_no=used + 1,
            status=AttemptStatus.IN_PROGRESS,
            started_at=now,
            deadline_at=deadline,
            total_score_snapshot=exam.total_score,
            question_count=exam.paper.question_count,
            ip_address=(ip or "")[:45],
            user_agent=(user_agent or "")[:255],
            shuffle_seed=random.randint(1, 2_000_000_000),
        )
        logger.info("考试 #%s：学生 #%s 开始第 %s 次作答", exam.id, student.pk, attempt.attempt_no)
        return {"attempt": attempt, "resumed": False}

    # ------------------------------------------------------------------
    # 下发（防答案泄漏的唯一出口）
    # ------------------------------------------------------------------
    @staticmethod
    def _question_payload(attempt: ExamAttempt, paper_question: PaperQuestion, display_order: int,
                          answer: Answer = None) -> dict:
        """构造一道题的对外数据。

        **这是答案过滤的唯一关卡。** ``student_payload()`` 已经保证不含
        ``reference_answer`` / ``analysis`` / ``is_correct``，这里再显式白名单一遍：
        即使将来有人往 ``student_payload()`` 里加了字段，也不会顺着这条路径漏出去。
        """
        raw = paper_question.student_payload()
        options = [
            {"label": item["label"], "content": item["content"]}
            for item in raw.get("options", [])
        ]
        options = attempt.shuffled_options(paper_question.id, options)

        return {
            # 只给「卷内条目 id」，不给题库题目 id —— 学生端不需要，也就无从据此去翻题库
            "paper_question_id": paper_question.id,
            "display_order": display_order,          # 本次作答里的显示题序（可能被打乱）
            "number": paper_question.order,          # 卷面原始编号
            "section_id": paper_question.section_id,
            "question_type": raw.get("type"),
            "type_display": raw.get("type_display"),
            "difficulty": raw.get("difficulty"),
            "stem": raw.get("stem"),
            "score": raw.get("score"),
            "options": options,
            "answer": answer.to_payload() if answer else {
                "paper_question_id": paper_question.id,
                "selected_labels": "",
                "answer_text": "",
                "is_flagged": False,
                "is_answered": False,
                "time_spent_seconds": 0,
                "order": paper_question.order,
            },
        }

    @staticmethod
    def build_paper_payload(attempt: ExamAttempt, now=None) -> dict:
        """组装考试下发数据（含已保存的作答，供续考恢复）。"""
        now = now or timezone.now()
        exam = attempt.exam

        questions = list(
            PaperQuestion.objects.filter(paper_id=exam.paper_id)
            .select_related("question", "section")
            .order_by("order", "id")
        )
        questions = attempt.shuffled_questions(questions)
        answer_map = {row.paper_question_id: row for row in attempt.answers.all()}

        payloads = [
            ExamAttemptService._question_payload(attempt, item, index + 1, answer_map.get(item.id))
            for index, item in enumerate(questions)
        ]

        # 按大题分组，同时保留一份扁平列表：一屏一题模式用扁平的，全卷模式用分组的
        sections = []
        grouped = {}
        for item in payloads:
            grouped.setdefault(item["section_id"], []).append(item)

        section_meta = {
            section.id: section
            for section in exam.paper.sections.all()
        }
        for section_id, rows in grouped.items():
            section = section_meta.get(section_id)
            sections.append({
                "section_id": section_id,
                "title": section.title if section else "",
                "description": section.description if section else "",
                "question_type": rows[0]["question_type"],
                "question_count": len(rows),
                "questions": rows,
            })

        return {
            "exam": ExamAttemptService._exam_brief(exam),
            "attempt": ExamAttemptService._attempt_brief(attempt, now),
            "paper": {
                "id": exam.paper_id,
                "title": exam.paper.title,
                "subject": exam.paper.subject,
                "description": exam.paper.description,
                "total_score": exam.paper.total_score_float,
                "pass_score": float(exam.effective_pass_score or 0),
                "question_count": exam.paper.question_count,
                "duration_minutes": exam.effective_duration_minutes,
            },
            "sections": sections,
            "questions": payloads,
            "remaining_seconds": attempt.remaining_seconds(now),
            "server_time": _fmt_dt(now),
            "deadline_at": _fmt_dt(attempt.deadline_at),
            "config": {
                "shuffle_questions": exam.shuffle_questions,
                "shuffle_options": exam.shuffle_options,
                "detect_switch": exam.detect_switch,
                "detect_paste": exam.detect_paste,
                "fullscreen_required": exam.fullscreen_required,
                "allow_resume": exam.allow_resume,
                "max_switch_count": exam.max_switch_count,
                "auto_submit_on_limit": exam.auto_submit_on_limit,
                "show_score_immediately": exam.show_score_immediately,
            },
            "behavior": {
                "switch_count": attempt.switch_count,
                "paste_count": attempt.paste_count,
                "fullscreen_exit_count": attempt.fullscreen_exit_count,
            },
        }

    @staticmethod
    def _exam_brief(exam: Exam) -> dict:
        return {
            "id": exam.id,
            "title": exam.title,
            "subject": exam.subject,
            "description": exam.description,
            "status": exam.status,
            "status_display": exam.get_status_display(),
            "duration_minutes": exam.effective_duration_minutes,
            "total_score": exam.paper.total_score_float,
            "pass_score": float(exam.effective_pass_score or 0),
            "question_count": exam.paper.question_count,
            "start_time": _fmt_dt(exam.start_time),
            "end_time": _fmt_dt(exam.end_time),
        }

    @staticmethod
    def _attempt_brief(attempt: ExamAttempt, now=None) -> dict:
        now = now or timezone.now()
        return {
            "id": attempt.id,
            "exam_id": attempt.exam_id,
            "attempt_no": attempt.attempt_no,
            "status": attempt.status,
            "status_display": attempt.get_status_display(),
            "started_at": _fmt_dt(attempt.started_at),
            "deadline_at": _fmt_dt(attempt.deadline_at),
            "submitted_at": _fmt_dt(attempt.submitted_at),
            "remaining_seconds": attempt.remaining_seconds(now),
            "time_spent_seconds": attempt.time_spent_seconds,
            "question_count": attempt.question_count,
            "answered_count": attempt.answered_count,
            "total_score": attempt.total_score_float(),
            # 得分与 「成绩是否对本人公开」同一把尺子（与 ExamAttemptStudentSerializer
            # 的 get_score 一致）。这里的 3 个调用方都是学生视角：卷面、续考、结果页。
            "score": attempt.score_float() if attempt.score_visible() else None,
            "is_timeout": attempt.is_timeout,
            "submit_reason": attempt.submit_reason,
            "submit_reason_display": attempt.get_submit_reason_display() if attempt.submit_reason else "",
            "progress": attempt.progress(),
            "can_resume": attempt.can_resume(now),
        }

    # ------------------------------------------------------------------
    # 续考：拉回状态
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def get_state(attempt: ExamAttempt, now=None) -> dict:
        """断网续考 / 刷新页面时拉回完整状态。

        顺带做一次超时结算：学生关掉页面拖到超时，重新打开时应该看到
        「已超时交卷」而不是还能接着答。
        """
        now = now or timezone.now()
        ExamAttemptService.resolve_expiry(attempt, now=now)
        attempt.refresh_from_db()
        if attempt.status != AttemptStatus.IN_PROGRESS:
            return {
                "attempt": ExamAttemptService._attempt_brief(attempt, now),
                "exam": ExamAttemptService._exam_brief(attempt.exam),
                "finished": True,
                "remaining_seconds": 0,
            }
        payload = ExamAttemptService.build_paper_payload(attempt, now)
        payload["finished"] = False
        return payload

    @staticmethod
    def _ensure_in_progress(attempt: ExamAttempt, now=None) -> None:
        if attempt.status != AttemptStatus.IN_PROGRESS:
            raise BusinessError("本次作答已结束，无法再修改答案")

    # ------------------------------------------------------------------
    # 暂存答案
    # ------------------------------------------------------------------
    @staticmethod
    def save_answers(attempt: ExamAttempt, items: list, *, now=None) -> dict:
        """批量暂存答案（自动保存与手动保存共用这一个入口）。

        **超时结算与保存拆成两个事务**：连宽限期都过了的时候，这里会先把作答自动
        收卷，然后才拒绝这次保存。若两者同处一个事务，拒绝时抛的异常会把收卷一起
        回滚，作答就卡在 ``in_progress`` 等心跳来兜——学生看到的却是「时间已结束」，
        状态却没落库，自相矛盾。
        """
        now = now or timezone.now()
        # resolve_expiry 内部的 submit 自带事务，在外层无事务时会独立提交
        ExamAttemptService.resolve_expiry(attempt, now=now)
        attempt.refresh_from_db()
        return ExamAttemptService._save_answers_atomic(attempt, items, now=now)

    @staticmethod
    @transaction.atomic
    def _save_answers_atomic(attempt: ExamAttempt, items: list, *, now) -> dict:
        ExamAttemptService._ensure_in_progress(attempt, now)

        # 宽限期过了就只允许交卷——防止「超时后前端继续偷偷保存」把答题时间无限延长
        if attempt.is_past_grace(now):
            raise BusinessError("作答时间已结束，请立即交卷")

        valid_ids = set(
            PaperQuestion.objects.filter(paper_id=attempt.exam.paper_id)
            .values_list("id", flat=True)
        )

        # 入口去重：同一道题在一次请求里出现多次时，**以最后一条为准**。
        # 前端「自动保存」与「手动保存」并发、弱网重试都会造出重复项；
        # 原样落库会在第二次 insert 撞 uniq_answer_attempt_question，
        # 整批 500 且一条都没存（学生以为存上了，实际全丢）。
        deduped, ignored = {}, 0
        for item in items:
            pq_id = item.get("paper_question_id")
            if pq_id not in valid_ids:
                ignored += 1
                continue
            deduped[pq_id] = item

        existing = {row.paper_question_id: row for row in attempt.answers.all()}
        pq_order = {
            row["id"]: row["order"]
            for row in PaperQuestion.objects.filter(id__in=deduped).values("id", "order")
        }
        pq_type = {
            row["id"]: row["question_type"]
            for row in PaperQuestion.objects.filter(id__in=deduped).values("id", "question_type")
        }

        saved = 0
        for pq_id, item in deduped.items():
            labels = _normalize_labels(item.get("selected_labels"))
            text = item.get("answer_text")
            text = text if isinstance(text, str) else ""

            answer = existing.get(pq_id)
            if answer is None:
                answer = Answer(attempt=attempt, paper_question_id=pq_id,
                                order=pq_order.get(pq_id, 0))
            answer.selected_labels = labels
            answer.answer_text = text
            answer.is_flagged = bool(item.get("is_flagged", answer.is_flagged))
            if item.get("time_spent_seconds") is not None:
                answer.time_spent_seconds = max(0, int(item["time_spent_seconds"]))
            answer.is_answered = _answered(pq_type.get(pq_id, ""), labels, text)
            ExamAttemptService._save_answer_row(answer, existing, pq_id)
            saved += 1

        answered_count = attempt.answers.filter(is_answered=True).count()
        attempt.answered_count = answered_count
        attempt.save(update_fields=["answered_count", "updated_at"])

        return {
            "saved": saved,
            "ignored": ignored,
            "answered_count": answered_count,
            "question_count": attempt.question_count,
            "remaining_seconds": attempt.remaining_seconds(now),
            "progress": attempt.progress(),
        }

    @staticmethod
    def _save_answer_row(answer: Answer, cache: dict, pq_id: int) -> Answer:
        """保存一条作答；并发撞唯一键时退化为更新，不让整批挂掉。

        ``Answer`` 上有唯一键 ``(attempt, paper_question)``。两个请求同时判「这道题
        还没作答」并各自 insert 时，后提交的那个会撞键——单条失败会连带整个
        ``save_answers`` 事务回滚，学生这一批答案全丢（表现是 500 且一条没存）。

        这里用嵌套 ``atomic()`` 打一个 savepoint：撞键时只回滚这一条 insert，
        再按唯一键改成 update，语义等价于「后写者胜」。入口去重能挡掉同一请求
        内的重复项，这层是给**跨请求并发**兜底的。
        """
        try:
            with transaction.atomic():
                answer.save()
        except IntegrityError:
            logger.warning(
                "作答并发撞唯一键（attempt=%s, paper_question=%s），退化为更新",
                answer.attempt_id, pq_id,
            )
            Answer.objects.filter(
                attempt_id=answer.attempt_id, paper_question_id=pq_id,
            ).update(
                selected_labels=answer.selected_labels,
                answer_text=answer.answer_text,
                is_flagged=answer.is_flagged,
                time_spent_seconds=answer.time_spent_seconds,
                is_answered=answer.is_answered,
                updated_at=timezone.now(),
            )
            answer = Answer.objects.get(
                attempt_id=answer.attempt_id, paper_question_id=pq_id,
            )
        cache[pq_id] = answer
        return answer

    @staticmethod
    def save_answer(attempt: ExamAttempt, *, paper_question_id: int, selected_labels=None,
                    answer_text: str = "", is_flagged=None, time_spent_seconds=None,
                    now=None) -> dict:
        """单题暂存（一屏一题模式下每题切走时触发）。

        **刻意不加 ``@transaction.atomic``**：``save_answers`` 内部已经把
        「超时收卷」和「保存」拆成两个事务，这里再包一层会让两者重新绑在一起——
        超时后拒绝保存抛的 400 会把自动收卷一起回滚，作答卡在 ``in_progress``，
        教师侧一直显示「进行中」、``finalize`` 被拒（BUG-03）。

        本方法自身只有一次读（回显这道题的最新态），不需要事务。
        """
        item = {"paper_question_id": paper_question_id, "answer_text": answer_text}
        if selected_labels is not None:
            item["selected_labels"] = selected_labels
        if is_flagged is not None:
            item["is_flagged"] = is_flagged
        if time_spent_seconds is not None:
            item["time_spent_seconds"] = time_spent_seconds

        result = ExamAttemptService.save_answers(attempt, [item], now=now)
        # 单题保存把范围收窄，返回这道题的最新态，前端不用再拉全量
        answer = attempt.answers.filter(paper_question_id=paper_question_id).first()
        result["answer"] = answer.to_payload() if answer else None
        return result

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def heartbeat(attempt: ExamAttempt, *, client_remaining=None, now=None) -> dict:
        """心跳上报。

        ``client_remaining`` 只是观测数据：如果客户端算出来的剩余时间和服务端
        差太多（比如学生改了系统时间、或者页面被暂停了很久），把它记下来提醒教师，
        但**绝不用它来延长或缩短考试时间**。
        """
        now = now or timezone.now()
        ExamAttemptService.resolve_expiry(attempt, now=now)
        attempt.refresh_from_db()

        server_remaining = attempt.remaining_seconds(now)
        drift = None
        if client_remaining is not None:
            try:
                drift = int(client_remaining) - server_remaining
            except (TypeError, ValueError):
                drift = None

        if attempt.status == AttemptStatus.IN_PROGRESS:
            attempt.heartbeat_count = (attempt.heartbeat_count or 0) + 1
            attempt.last_heartbeat_at = now
            attempt.save(update_fields=["heartbeat_count", "last_heartbeat_at", "updated_at"])

        return {
            "remaining_seconds": server_remaining,
            "server_time": _fmt_dt(now),
            "status": attempt.status,
            "should_submit": attempt.status != AttemptStatus.IN_PROGRESS,
            "clock_drift_seconds": drift,
            "force_submit": (
                attempt.status == AttemptStatus.IN_PROGRESS
                and attempt.exam.detect_switch
                and attempt.exam.auto_submit_on_limit
                and attempt.switch_count > attempt.exam.max_switch_count
            ),
        }

    # ------------------------------------------------------------------
    # 行为采集（防切屏 / 防粘贴）
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def record_behavior(attempt: ExamAttempt, events: list, *, now=None) -> dict:
        """上报前端行为事件。

        模块四只做聚合计数与阈值判定；明细事件（带时间戳、停留时长）是模块八的事。
        """
        now = now or timezone.now()
        attempt.refresh_from_db()
        ExamAttemptService._ensure_in_progress(attempt, now)

        exam = attempt.exam
        counters = {
            "switch_count": attempt.switch_count or 0,
            "paste_count": attempt.paste_count or 0,
            "fullscreen_exit_count": attempt.fullscreen_exit_count or 0,
        }

        # 记录下来「真正被计数」的事件，交给模块八落明细埋点。
        # 只交被计数的事件，是为了让「埋点聚合」与「ExamAttempt 聚合计数」永远对得上
        # （检测开关关掉的事件不计入计数，也就不落明细，两边口径一致）。
        counted_events = []
        for event in events:
            event_type = event.get("type")
            count = max(1, int(event.get("count") or 1))

            if event_type in BEHAVIOR_COUNTER_FIELDS:
                field = BEHAVIOR_COUNTER_FIELDS[event_type]
                # 对应类型的检测开关关了就只记账不计数，方便教师按需放宽
                if event_type == BehaviorEvent.SWITCH and not exam.detect_switch:
                    continue
                if event_type == BehaviorEvent.PASTE and not exam.detect_paste:
                    continue
                counters[field] += count
                counted_events.append(event)
            elif event_type == BehaviorEvent.BLUR:
                if exam.detect_switch:
                    counters["switch_count"] += count
                    counted_events.append(event)
            elif event_type == BehaviorEvent.HEARTBEAT:
                continue
            else:
                logger.warning("未知行为事件类型：%s", event_type)

        attempt.switch_count = counters["switch_count"]
        attempt.paste_count = counters["paste_count"]
        attempt.fullscreen_exit_count = counters["fullscreen_exit_count"]
        attempt.save(update_fields=["switch_count", "paste_count",
                                    "fullscreen_exit_count", "updated_at"])

        # 模块八：把逐条明细落成行为埋点（模块四只做聚合计数，明细是有意留给模块八的）。
        # * 惰性 import：避免 exam 与 proctoring 在模块级互相依赖；
        # * 失败绝不打断考试：埋点是「事后复盘」用的，少一条明细不该影响学生答题。
        #   record_events 自己是 @transaction.atomic（等于一个保存点），
        #   即便它出错，这里 catch 住之后外层事务仍然可用。
        if counted_events:
            try:
                from apps.proctoring.services import BehaviorLogService

                BehaviorLogService.record_events(attempt, counted_events, now=now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("行为埋点写入失败（不影响考试流程）：%s", exc)

        limit_exceeded = bool(
            exam.detect_switch and attempt.switch_count > exam.max_switch_count
        )

        auto_submitted = False
        if limit_exceeded and exam.auto_submit_on_limit:
            ExamAttemptService.submit(attempt, reason=SubmitReason.FORCED, now=now)
            auto_submitted = True

        return {
            "switch_count": attempt.switch_count,
            "paste_count": attempt.paste_count,
            "fullscreen_exit_count": attempt.fullscreen_exit_count,
            "max_switch_count": exam.max_switch_count,
            "limit_exceeded": limit_exceeded,
            "auto_submitted": auto_submitted,
            "remaining_seconds": attempt.remaining_seconds(now),
        }

    # ------------------------------------------------------------------
    # 交卷
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def submit(attempt: ExamAttempt, *, reason: str = SubmitReason.MANUAL, now=None) -> ExamAttempt:
        """交卷。

        幂等：已交过的直接返回，不覆盖第一次的交卷时间——
        否则学生刷新一次结果页，交卷时间就往后跳一次，超时判定全乱。
        """
        now = now or timezone.now()
        if attempt.status != AttemptStatus.IN_PROGRESS:
            return attempt

        timed_out = now > attempt.deadline_at
        if timed_out and reason == SubmitReason.MANUAL:
            reason = SubmitReason.TIMEOUT

        attempt.status = AttemptStatus.TIMEOUT if timed_out else AttemptStatus.SUBMITTED
        attempt.submitted_at = now
        attempt.is_timeout = timed_out
        attempt.submit_reason = reason
        attempt.time_spent_seconds = max(0, int((now - attempt.started_at).total_seconds()))
        attempt.answered_count = attempt.answers.filter(is_answered=True).count()
        attempt.save(update_fields=[
            "status", "submitted_at", "is_timeout", "submit_reason",
            "time_spent_seconds", "answered_count", "updated_at",
        ])

        logger.info(
            "考试 #%s 学生 #%s 交卷：%s 题已答 / 用时 %s 秒 / 原因 %s",
            attempt.exam_id, attempt.student_id, attempt.answered_count,
            attempt.time_spent_seconds, reason,
        )
        return attempt

    @staticmethod
    def resolve_expiry(attempt: ExamAttempt, now=None) -> bool:
        """超时结算：连宽限期都过了就自动收卷。返回是否触发了收卷。"""
        now = now or timezone.now()
        if attempt.status != AttemptStatus.IN_PROGRESS:
            return False
        if not attempt.is_past_grace(now):
            return False
        ExamAttemptService.submit(attempt, reason=SubmitReason.TIMEOUT, now=now)
        return True

    # ------------------------------------------------------------------
    @staticmethod
    def my_attempts(student):
        return ExamAttempt.objects.with_relations().filter(student=student)


# ======================================================================
# 统计分析
# ======================================================================
class ExamAnalysisService:
    """考试统计——教师看板与学生成绩页共用。"""

    @staticmethod
    def overview(exam: Exam) -> dict:
        """单场考试总览。"""
        attempts = ExamAttempt.objects.filter(exam=exam)
        total = attempts.count()
        in_progress = attempts.filter(status=AttemptStatus.IN_PROGRESS).count()
        timeout = attempts.filter(is_timeout=True).count()
        submitted = attempts.exclude(status=AttemptStatus.IN_PROGRESS).count()

        # 未判分（模块四阶段全部未判分）时这些值为 None，前端显示「—」而不是 0，
        # 免得教师把「还没判分」误读成「全班考了 0 分」
        graded = attempts.filter(status__in=[AttemptStatus.AUTO_GRADED, AttemptStatus.GRADED])
        agg = graded.aggregate(avg=Avg("score"), top=Max("score"), low=Min("score"))
        graded_count = graded.count()

        pass_count = sum(1 for row in graded if row.passed())

        applicants = exam.participant_count()
        return {
            "exam": ExamAttemptService._exam_brief(exam),
            "computed_status": exam.computed_status(),
            # 展示文案跟 `ExamListSerializer` 同源，前端不再自己维护一份中文映射。
            # 少了这个字段时，监控页徽章会 `undefined || ''` 变成空白（BUG-04）。
            "computed_status_display": dict(ExamStatus.choices).get(exam.computed_status(), ""),
            "applicants": applicants,
            "started": total,
            "in_progress": in_progress,
            "submitted": submitted,
            "timeout": timeout,
            "not_started": max(0, applicants - total),
            "graded": graded_count,
            "pending_grading": submitted - graded_count,
            "avg_score": round(float(agg["avg"]), 2) if agg["avg"] is not None else None,
            "max_score": float(agg["top"]) if agg["top"] is not None else None,
            "min_score": float(agg["low"]) if agg["low"] is not None else None,
            "pass_count": pass_count,
            "pass_rate": round(pass_count * 100.0 / graded_count, 1) if graded_count else None,
            "submit_rate": round(submitted * 100.0 / applicants, 1) if applicants else 0.0,
            "behavior": ExamAnalysisService.behavior_stats(exam),
        }

    @staticmethod
    def behavior_stats(exam: Exam) -> dict:
        """行为统计与违规嫌疑名单。"""
        attempts = ExamAttempt.objects.filter(exam=exam)
        agg = attempts.aggregate(
            switch_total=Sum("switch_count"),
            paste_total=Sum("paste_count"),
        )
        switch_total = agg["switch_total"] or 0
        paste_total = agg["paste_total"] or 0

        threshold = exam.max_switch_count
        flagged = attempts.filter(switch_count__gt=threshold) if exam.detect_switch else attempts.none()

        return {
            "switch_total": switch_total,
            "paste_total": paste_total,
            "max_switch_count": threshold,
            "flagged_count": flagged.count(),
            "flagged": [
                {
                    "attempt_id": row.id,
                    "student_id": row.student_id,
                    "student_name": getattr(row.student, "display_name", "") or str(row.student_id),
                    "switch_count": row.switch_count,
                    "paste_count": row.paste_count,
                    "status": row.status,
                    "status_display": row.get_status_display(),
                }
                for row in flagged.select_related("student").order_by("-switch_count")[:50]
            ],
        }

    @staticmethod
    def attempt_table(exam: Exam, *, status: str = "", keyword: str = "") -> list:
        """教师看板的作答明细表。"""
        queryset = ExamAttempt.objects.with_relations().filter(exam=exam)
        if status == AttemptStatus.IN_PROGRESS:
            queryset = queryset.in_progress()
        elif status:
            queryset = queryset.filter(status=status)
        if keyword:
            queryset = queryset.filter(
                Q(student__username__icontains=keyword)
                | Q(student__real_name__icontains=keyword)
                | Q(student__student_no__icontains=keyword)
            )

        now = timezone.now()
        return [
            {
                "attempt_id": row.id,
                "student_id": row.student_id,
                "student_name": getattr(row.student, "display_name", "") or "",
                "student_no": getattr(row.student, "student_no", "") or "",
                "attempt_no": row.attempt_no,
                "status": row.status,
                "status_display": row.get_status_display(),
                "is_timeout": row.is_timeout,
                "submit_reason_display": row.get_submit_reason_display() if row.submit_reason else "",
                "started_at": _fmt_dt(row.started_at),
                "submitted_at": _fmt_dt(row.submitted_at),
                "remaining_seconds": row.remaining_seconds(now),
                "time_spent_seconds": row.time_spent_seconds,
                "progress": row.progress(),
                "answered_count": row.answered_count,
                "question_count": row.question_count,
                "score": row.score_float(),
                "total_score": row.total_score_float(),
                "switch_count": row.switch_count,
                "paste_count": row.paste_count,
                "fullscreen_exit_count": row.fullscreen_exit_count,
                "heartbeat_count": row.heartbeat_count,
                "suspicious": exam.detect_switch and row.switch_count > exam.max_switch_count,
                "ip_address": row.ip_address,
            }
            for row in queryset.order_by("-started_at")[:500]
        ]

    @staticmethod
    def global_statistics(teacher=None) -> dict:
        """考试总览（管理页顶部统计卡）。"""
        queryset = Exam.objects.all()
        if teacher is not None and not getattr(teacher, "is_admin", False):
            queryset = queryset.filter(created_by=teacher)

        total = queryset.count()
        attempts = ExamAttempt.objects.filter(exam__in=queryset)

        by_status = [
            {"value": code, "label": label, "count": queryset.filter(status=code).count()}
            for code, label in ExamStatus.choices
        ]

        upcoming = queryset.filter(
            status__in=[ExamStatus.PUBLISHED, ExamStatus.ONGOING],
            start_time__gt=timezone.now(),
        ).count()
        running = sum(1 for exam in queryset.filter(
            status__in=[ExamStatus.PUBLISHED, ExamStatus.ONGOING]
        ) if exam.computed_status() == ExamStatus.ONGOING)

        return {
            "total": total,
            "by_status": by_status,
            "upcoming": upcoming,
            "running": running,
            "attempt_total": attempts.count(),
            "in_progress": attempts.in_progress().count(),
            "submitted": attempts.exclude(status=AttemptStatus.IN_PROGRESS).count(),
            "timeout": attempts.filter(is_timeout=True).count(),
            "answered_total": attempts.aggregate(total=Sum("answered_count"))["total"] or 0,
        }


__all__ = ["ExamAnalysisService", "ExamAttemptService", "ExamService"]
