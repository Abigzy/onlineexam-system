"""试卷业务服务层。

分三类职责：
  * ``PaperService``          —— 卷子的增删改编排（加题 / 调分 / 排序 / 复制 / 状态机），
                                 所有会改动冗余字段（总分、题量、分区）的操作都收在这里。
  * ``PaperAnalysisService``  —— 把卷子算成图表能直接用的数据（雷达 / 分布 / 预警）。
  * ``PaperComposeService``   —— 智能组卷算法，在 ``composer.py``。
  * ``PaperAIService``        —— 组卷说明，在 ``ai_services.py``。

为什么「总分 / 题量 / 分区」必须集中维护？
    它们都是 ``paper_question`` 的冗余派生值。如果允许视图层到处直接改，
    迟早会出现「卷面显示 100 分、实际加起来 96 分」这种对不上账的情况。
    唯一的写入口 = 唯一的真相来源。
"""

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.common.constants import Difficulty, QuestionType
from apps.common.exceptions import BusinessError
from apps.questions.models import KnowledgePoint, Question

from .constants import (
    AnalysisThreshold,
    PaperGenerationMode,
    PaperStatus,
    SECTION_HINT_BY_TYPE,
    SECTION_ORDINALS,
    SECTION_TITLE_BY_TYPE,
    SECTION_TYPE_ORDER,
)
from .models import Paper, PaperQuestion, PaperSection

logger = logging.getLogger("apps.papers")

ZERO = Decimal("0.00")


def _to_decimal(value, default=ZERO) -> Decimal:
    """把入参稳妥地转成 Decimal（浮点数直接进 Decimal 会带上一串二进制尾巴）。"""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return default


def _fmt(value) -> str:
    """12.00 → 12，12.50 → 12.5。卷面上不该出现多余的小数点。"""
    number = float(value or 0)
    return str(int(number)) if number == int(number) else f"{number:g}"


# ======================================================================
# 卷子编排
# ======================================================================
class PaperService:
    """试卷的写操作。视图层只负责取参数和权限，业务规则全在这里。"""

    # ------------------------------------------------------------------
    # 创建 / 更新 / 删除
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def create(*, data: dict, user=None, mode: str = PaperGenerationMode.MANUAL) -> Paper:
        """新建一份空卷。

        ``data`` 里允许带 ``question_ids``：如果有，建完卷子顺手把题塞进去，
        这样「新建卷子并加题」对外仍然只是一个动作。
        """
        payload = dict(data)
        question_ids = payload.pop("question_ids", None) or []

        paper = Paper.objects.create(
            title=payload.get("title") or "未命名试卷",
            subject=payload.get("subject") or "",
            description=payload.get("description") or "",
            duration_minutes=int(payload.get("duration_minutes") or 90),
            pass_score=_to_decimal(payload.get("pass_score"), Decimal("60.00")),
            target_difficulty=float(payload.get("target_difficulty") or 2.0),
            generation_mode=mode,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

        if question_ids:
            questions = list(Question.objects.filter(id__in=question_ids, is_active=True))
            PaperService.add_questions(paper=paper, questions=questions, strict=False)

        PaperService.recalc(paper)
        logger.info("创建试卷 #%s《%s》", paper.id, paper.title)
        return paper

    @staticmethod
    @transaction.atomic
    def update(paper: Paper, *, data: dict) -> Paper:
        """改卷头信息。

        题量、总分是派生值，不接受外部传入——传了也会被 ``recalc`` 覆盖掉。

        与加题 / 调分 / 排序同口径：**只有草稿能改**。已发布的卷子可能正被进行中的
        考试引用，随手改及格分或时长会让成绩判定与展示口径悄悄变化。
        """
        PaperService.ensure_editable(paper)

        fields = ["title", "subject", "description", "duration_minutes",
                  "pass_score", "target_difficulty"]
        for field in fields:
            if field not in data:
                continue
            value = data[field]
            if field == "pass_score":
                value = _to_decimal(value, paper.pass_score)
            elif field == "duration_minutes":
                value = int(value or paper.duration_minutes)
            elif field == "target_difficulty":
                value = float(value or paper.target_difficulty)
            setattr(paper, field, value if value is not None else "")
        paper.save()
        return paper

    @staticmethod
    @transaction.atomic
    def delete(paper: Paper) -> None:
        """删卷子。两道关卡：

        1. 发布过的卷子不给删——它可能已经被考试引用（模块四）；
        2. **被考试引用的不给删**——``Exam.paper`` 是 ``on_delete=PROTECT``，
           直接 ``paper.delete()`` 会抛 ``ProtectedError``，被统一异常处理器兜成
           500。而「退回草稿」能绕过第 1 关的 status 判断，所以引用检查必须显式做，
           不能指望发布状态挡住。
        """
        if paper.is_published:
            raise BusinessError(
                "已发布的试卷不能删除，请先归档或退回草稿；"
                "若已被考试引用，还需先处理引用它的考试",
                code=13001,
            )
        # 引用检查：漏了这一步就是 500（BUG-01）
        referencing = paper.exams.count()
        if referencing:
            raise BusinessError(
                f"试卷《{paper.title}》已被 {referencing} 场考试引用，无法删除；"
                f"如确需下线请改为「归档」，以保留考试与成绩的可追溯性",
                code=13008,
            )
        title = paper.title
        paper.delete()
        logger.info("删除试卷《%s》", title)

    # ------------------------------------------------------------------
    # 题目增删改
    # ------------------------------------------------------------------
    @staticmethod
    def ensure_editable(paper: Paper) -> None:
        """只有草稿能改。已发布的卷子要改，先退回草稿。"""
        if not paper.is_editable:
            raise BusinessError(
                f"试卷当前状态为「{paper.get_status_display()}」，只有草稿可以修改",
                code=13002,
            )

    @staticmethod
    @transaction.atomic
    def add_questions(*, paper: Paper, questions: list, score=None, strict: bool = False) -> dict:
        """往卷子里加题。

        ``strict=True``（教师手动加题）：遇到已在卷内的题直接报错，避免教师困惑
        「为什么我加了 10 道只进去 8 道」。
        ``strict=False``（AI 组卷 / 批量导入）：重复的静默跳过，尽量多成事。

        分值优先级的确定顺序：显式传入 > 分区统一分值 > 题目自身默认分值。
        """
        PaperService.ensure_editable(paper)

        existing_ids = set(paper.paper_questions.values_list("question_id", flat=True))
        added, duplicated, invalid = [], [], []

        for question in questions:
            if question is None or not getattr(question, "is_active", True):
                invalid.append(question)
                continue
            if question.id in existing_ids:
                if strict:
                    raise BusinessError(f"题目「{question.stem[:20]}…」已在本卷中", code=13003)
                duplicated.append(question)
                continue

            section = PaperService._section_for(paper, question.type)
            unit_score = score
            if unit_score is None and section is not None and section.score_per_question:
                unit_score = section.score_per_question
            if unit_score is None:
                unit_score = question.score or Decimal("2.00")

            item = PaperQuestion(
                paper=paper, section=section, question=question,
                order=0, score=_to_decimal(unit_score),
            )
            item.sync_from_question()
            item.save()

            existing_ids.add(question.id)
            added.append(item)

        # 新增后可能有新题型出现 → 补分区；同时统一全卷顺序和汇总
        PaperService.sync_sections(paper)
        PaperService.renumber(paper)
        PaperService.recalc(paper)

        return {
            "added": added,
            "duplicated": duplicated,
            "invalid": invalid,
            "added_count": len(added),
            "duplicated_count": len(duplicated),
            "invalid_count": len(invalid),
        }

    @staticmethod
    @transaction.atomic
    def remove_questions(*, paper: Paper, paper_question_ids: list) -> int:
        """移出若干题（按 ``paper_question.id``）。返回真正删掉的数量。"""
        PaperService.ensure_editable(paper)
        queryset = paper.paper_questions.filter(id__in=paper_question_ids)
        removed = queryset.count()
        queryset.delete()

        PaperService.sync_sections(paper)
        PaperService.renumber(paper)
        PaperService.recalc(paper)
        return removed

    @staticmethod
    def _validate_score(value) -> Decimal:
        """单题分值的统一校验——单题调分与批量调分必须同一口径。

        刻意放在服务层而不是只靠序列化器：批量保存时序列化器只做类型约束，
        上限必须由业务层把关，否则两个入口的规则会漂移（曾出现「单题调分拦了 101、
        批量调分却放行」的不一致）。越界抛 13004，超限提示与单题入口逐字一致。
        """
        if value is None or value <= 0:
            raise BusinessError("分值必须大于 0", code=13004)
        if value > 100:
            raise BusinessError("单题分值不应超过 100，请检查是否填错", code=13004)
        return value

    @staticmethod
    @transaction.atomic
    def set_score(*, paper_question: PaperQuestion, score) -> PaperQuestion:
        """给卷内某道题单独调分（只影响本卷，不动题库里的默认分值）。"""
        PaperService.ensure_editable(paper_question.paper)
        value = PaperService._validate_score(_to_decimal(score))

        paper_question.score = value
        paper_question.save(update_fields=["score", "updated_at"])

        PaperService.sync_sections(paper_question.paper)
        PaperService.recalc(paper_question.paper)
        return paper_question

    @staticmethod
    @transaction.atomic
    def batch_set_score(*, paper: Paper, items: list) -> int:
        """批量调分。``items`` 形如 ``[{"id": 1, "score": 3}, ...]``。

        每一行的分值都走 ``_validate_score``：非法即整批拒绝，而不是静默跳过——
        「填了 101 却提示保存成功、实际没生效」比直接报错更难排查。
        """
        PaperService.ensure_editable(paper)
        mapping = {}
        for row in items:
            if row.get("id") is None:
                continue
            mapping[int(row["id"])] = PaperService._validate_score(
                _to_decimal(row.get("score"))
            )
        if not mapping:
            return 0

        updated = 0
        for item in paper.paper_questions.filter(id__in=list(mapping)):
            value = mapping[item.id]
            if item.score != value:
                item.score = value
                item.save(update_fields=["score", "updated_at"])
                updated += 1

        PaperService.sync_sections(paper)
        PaperService.recalc(paper)
        return updated

    @staticmethod
    @transaction.atomic
    def reorder(*, paper: Paper, ordered_ids: list) -> Paper:
        """按传入的 id 顺序重排全卷题目。未出现在列表里的题排到后面（保持原有相对顺序）。"""
        PaperService.ensure_editable(paper)
        wanted = [int(i) for i in ordered_ids]
        current = list(paper.paper_questions.values_list("id", flat=True))

        head = [i for i in wanted if i in current]
        tail = [i for i in current if i not in set(head)]
        final = head + tail

        for index, item_id in enumerate(final, 1):
            PaperQuestion.objects.filter(pk=item_id).update(order=index)
        return paper

    # ------------------------------------------------------------------
    # 分区（大题）
    # ------------------------------------------------------------------
    @staticmethod
    def _section_for(paper: Paper, question_type: str):
        """找该题型对应的分区（没有就返回 None，稍后由 sync_sections 统一补建）。"""
        for section in paper.sections.all():
            if section.question_type == question_type:
                return section
        return None

    @staticmethod
    @transaction.atomic
    def sync_sections(paper: Paper) -> dict:
        """把分区校正成与卷内题目一致的状态。

        干四件事：
          1. 给出现的题型补建分区（按固定题型顺序编号，卷面顺序才稳定）；
          2. 把题目归到对应题型的分区下；
          3. 分区内分值统一时回填 ``score_per_question``，卷面才能印「每题 2 分」；
          4. 删掉已经没有题目的空分区。

        之所以是「同步」而不是「只增不减」：教师把某题型的题全删了，
        卷面上不该还留着一个「五、论述题（共 0 题）」的空壳。
        """
        used_types = set(paper.paper_questions.values_list("question_type", flat=True))
        ordered_codes = [code for code in SECTION_TYPE_ORDER if code in used_types]

        existing = {section.question_type: section for section in paper.sections.all()}
        alive, created, removed = [], [], []

        for index, code in enumerate(ordered_codes, 1):
            title = f"{SECTION_ORDINALS[index - 1]}、{SECTION_TITLE_BY_TYPE.get(code, code)}"
            section = existing.get(code)
            if section is None:
                section = PaperSection.objects.create(
                    paper=paper, question_type=code, title=title, order=index,
                    description=SECTION_HINT_BY_TYPE.get(code, ""),
                )
                created.append(section)
            else:
                touched = []
                if section.title != title:
                    section.title = title
                    touched.append("title")
                if section.order != index:
                    section.order = index
                    touched.append("order")
                if not section.description:
                    section.description = SECTION_HINT_BY_TYPE.get(code, "")
                    touched.append("description")
                if touched:
                    section.save(update_fields=touched)
            alive.append(section)

        for code, section in existing.items():
            if code not in ordered_codes:
                removed.append(section)
        if removed:
            PaperSection.objects.filter(id__in=[s.id for s in removed]).delete()

        # 题目归位 + 分区分值回填
        for section in alive:
            items = paper.paper_questions.filter(question_type=section.question_type)
            items.exclude(section=section).update(section=section, updated_at=timezone.now())

            scores = set(items.values_list("score", flat=True))
            per = scores.pop() if len(scores) == 1 else None
            if section.score_per_question != per:
                section.score_per_question = per
                section.save(update_fields=["score_per_question", "updated_at"])

        return {"sections": alive, "created": created, "removed": removed}

    @staticmethod
    @transaction.atomic
    def renumber(paper: Paper) -> None:
        """重排全卷连续题号（从 1 开始，跨大题连续）。

        卷面题号必须连续：删掉一题后如果留着空号，打印出来会很难看，
        答题卡对不上号更是事故。
        """
        items = list(
            paper.paper_questions.select_related("section").order_by(
                "section__order", "order", "id"
            )
        )
        for index, item in enumerate(items, 1):
            if item.order != index:
                PaperQuestion.objects.filter(pk=item.pk).update(order=index)

    # ------------------------------------------------------------------
    # 汇总重算
    # ------------------------------------------------------------------
    @staticmethod
    def recalc(paper: Paper, *, save: bool = True) -> Paper:
        """重算冗余汇总字段。任何改动题目的操作收尾都必须调它。"""
        agg = paper.paper_questions.aggregate(total=Sum("score"), count=Count("id"))
        paper.total_score = agg["total"] or ZERO
        paper.question_count = agg["count"] or 0
        if save:
            paper.save(update_fields=["total_score", "question_count", "updated_at"])
        return paper

    # ------------------------------------------------------------------
    # 复制 / 状态
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def clone(paper: Paper, *, user=None, title: str = None) -> Paper:
        """复制一份卷子（题型分区与分值原样带过来）。

        典型用途：一份卷子考完想换个知识点重考，或者做 A/B 卷。
        复制出来的**必然是草稿**——复制一份已发布的卷子不该直接又发布一次。
        """
        new_paper = Paper.objects.create(
            title=title or f"{paper.title}（副本）",
            subject=paper.subject,
            description=paper.description,
            duration_minutes=paper.duration_minutes,
            pass_score=paper.pass_score,
            target_difficulty=paper.target_difficulty,
            status=PaperStatus.DRAFT,
            generation_mode=paper.generation_mode,
            created_by=user if getattr(user, "is_authenticated", False) else paper.created_by,
        )

        section_map = {}
        for section in paper.sections.all():
            section_map[section.id] = PaperSection.objects.create(
                paper=new_paper, title=section.title, question_type=section.question_type,
                description=section.description, order=section.order,
                score_per_question=section.score_per_question,
            )

        PaperQuestion.objects.bulk_create([
            PaperQuestion(
                paper=new_paper, section=section_map.get(item.section_id),
                question_id=item.question_id, order=item.order, score=item.score,
                question_type=item.question_type, difficulty=item.difficulty,
            )
            for item in paper.paper_questions.all()
        ])

        PaperService.recalc(new_paper)
        logger.info("复制试卷《%s》→《%s》", paper.title, new_paper.title)
        return new_paper

    @staticmethod
    @transaction.atomic
    def change_status(paper: Paper, status: str) -> Paper:
        """状态机：草稿 ⇄ 已发布 → 已归档。

        发布是有前置条件的：空卷不能发布，及格分不能超过总分。
        这类校验放这里而不是模型层，是为了能给出人能看懂的中文原因。
        """
        if status not in dict(PaperStatus.choices):
            raise BusinessError(f"未知的试卷状态：{status}", code=13005)
        if status == paper.status:
            return paper

        if status == PaperStatus.PUBLISHED:
            PaperService.recalc(paper)
            if paper.question_count == 0:
                raise BusinessError("空卷不能发布，请先添加题目", code=13006)
            if paper.pass_score > paper.total_score:
                raise BusinessError(
                    f"及格分（{_fmt(paper.pass_score)}）不能高于总分（{_fmt(paper.total_score)}）",
                    code=13007,
                )
            paper.published_at = paper.published_at or timezone.now()
        elif status == PaperStatus.DRAFT and paper.is_published:
            # 已发布 → 草稿：撤回，保留发布时间的历史记录无意义，清掉
            paper.published_at = None

        paper.status = status
        paper.save(update_fields=["status", "published_at", "updated_at"])
        logger.info("试卷《%s》状态 → %s", paper.title, paper.get_status_display())
        return paper


# ======================================================================
# 组卷质量分析
# ======================================================================
class PaperAnalysisService:
    """把一份卷子算成图表数据与预警。

    纯读操作，不改库——所以这里所有方法都不加事务。
    ECharts 需要的形状在这里统一成型，前端不做二次加工。
    """

    # ------------------------------------------------------------------
    @staticmethod
    def overview(paper: Paper) -> dict:
        """卷面概览（信息条）。"""
        return {
            "id": paper.id,
            "title": paper.title,
            "subject": paper.subject,
            "status": paper.status,
            "status_display": paper.get_status_display(),
            "generation_mode": paper.generation_mode,
            "generation_mode_display": paper.get_generation_mode_display(),
            "question_count": paper.question_count,
            "total_score": paper.total_score_float,
            "pass_score": float(paper.pass_score or 0),
            "duration_minutes": paper.duration_minutes,
            "target_difficulty": float(paper.target_difficulty or 0),
            "actual_difficulty": paper.actual_difficulty(),
            "difficulty_deviation": paper.difficulty_deviation(),
            "section_count": paper.sections.count(),
            "knowledge_point_count": len(paper.knowledge_point_ids()),
        }

    # ------------------------------------------------------------------
    @staticmethod
    def type_distribution(paper: Paper) -> list:
        """题型分布：[{type, label, count, score, ratio}]，按固定题型顺序。"""
        rows = (
            paper.paper_questions.values("question_type")
            .annotate(count=Count("id"), score=Sum("score"))
        )
        bucket = {row["question_type"]: row for row in rows}
        total_count = sum(row["count"] for row in bucket.values()) or 1

        result = []
        for code in SECTION_TYPE_ORDER:
            row = bucket.get(code)
            if not row:
                continue
            result.append({
                "type": code,
                "label": SECTION_TITLE_BY_TYPE.get(code, code),
                "count": row["count"],
                "score": float(row["score"] or 0),
                "ratio": round(row["count"] / total_count, 4),
            })
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def difficulty_distribution(paper: Paper) -> list:
        """难度分布：[{value, label, count, score, ratio}]，四档全列（含 0）。"""
        rows = (
            paper.paper_questions.values("difficulty")
            .annotate(count=Count("id"), score=Sum("score"))
        )
        bucket = {int(row["difficulty"]): row for row in rows}
        total_count = sum(row["count"] for row in bucket.values()) or 1

        result = []
        for value, label in Difficulty.choices:
            row = bucket.get(int(value))
            result.append({
                "value": int(value),
                "label": label,
                "count": row["count"] if row else 0,
                "score": float(row["score"] or 0) if row else 0.0,
                "ratio": round((row["count"] / total_count) if row else 0.0, 4),
            })
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def knowledge_coverage(paper: Paper, *, planned_ids=None) -> dict:
        """知识点覆盖情况，直接喂 ECharts 雷达图。

        雷达的「满分值」取本卷单个知识点的最高题量——不同卷子题量差很多，
        固定成 10 会让题量少的卷子缩成一个点，没法看。
        """
        rows = (
            paper.paper_questions
            .values("question__knowledge_points__id", "question__knowledge_points__name")
            .annotate(count=Count("id"), score=Sum("score"))
            .order_by("-count")
        )

        items, seen = [], set()
        for row in rows:
            point_id = row["question__knowledge_points__id"]
            if point_id is None or point_id in seen:
                continue
            seen.add(point_id)
            items.append({
                "id": point_id,
                "name": row["question__knowledge_points__name"],
                "question_count": row["count"],
                "score": float(row["score"] or 0),
            })

        planned = [int(i) for i in (planned_ids or [])]
        covered_planned = [i for i in planned if i in seen]
        coverage_ratio = round(len(covered_planned) / len(planned), 4) if planned else None

        max_count = max((item["question_count"] for item in items), default=1)

        missing = []
        if planned:
            names = dict(
                KnowledgePoint.objects.filter(id__in=planned).values_list("id", "name")
            )
            missing = [
                {"id": pid, "name": names.get(pid, str(pid))}
                for pid in planned if pid not in seen
            ]

        return {
            "indicators": [
                {"name": item["name"], "max": max(max_count, 1)} for item in items
            ],
            "values": [item["question_count"] for item in items],
            "items": items,
            "distinct_count": len(items),
            "planned_count": len(planned),
            "coverage_ratio": coverage_ratio,
            "missing": missing,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def section_stats(paper: Paper) -> list:
        """按大题统计（卷面结构 + 各大题得分率的分母）。"""
        result = []
        for section in paper.sections.all():
            result.append({
                "id": section.id,
                "title": section.title,
                "label": section.section_label(),
                "question_type": section.question_type,
                "order": section.order,
                "question_count": section.question_count,
                "score_per_question": (
                    float(section.score_per_question)
                    if section.score_per_question else None
                ),
                "subtotal": float(section.subtotal),
            })
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def warnings(paper: Paper, *, planned_ids=None) -> list:
        """组卷质量预警。每条都是「能看懂 + 能动手改」的一句话。"""
        result = []
        overview = PaperAnalysisService.overview(paper)

        if overview["question_count"] == 0:
            result.append({"level": "error", "code": "empty",
                           "message": "试卷还没有题目，无法发布。"})
            return result

        # 1) 难度偏差
        deviation = overview["difficulty_deviation"]
        if abs(deviation) > AnalysisThreshold.DIFFICULTY_TOLERANCE:
            direction = "偏难" if deviation > 0 else "偏易"
            result.append({
                "level": "warning", "code": "difficulty_deviation",
                "message": (
                    f"整卷实际难度系数 {overview['actual_difficulty']}，"
                    f"比目标 {overview['target_difficulty']} {direction} "
                    f"（偏差 {deviation:+}），建议调整难易题配比。"
                ),
            })

        # 2) 知识点覆盖
        coverage = PaperAnalysisService.knowledge_coverage(paper, planned_ids=planned_ids)
        ratio = coverage["coverage_ratio"]
        if ratio is not None and ratio < AnalysisThreshold.MIN_KNOWLEDGE_COVERAGE:
            missing_names = "、".join(item["name"] for item in coverage["missing"][:5])
            covered_count = coverage["planned_count"] - len(coverage["missing"])
            result.append({
                "level": "warning", "code": "knowledge_coverage",
                "message": (
                    f"计划覆盖 {coverage['planned_count']} 个知识点，"
                    f"实际只覆盖 {covered_count} 个（{ratio:.0%}）。"
                    f"未覆盖：{missing_names}。"
                ),
            })

        # 3) 及格分与总分
        if overview["pass_score"] > overview["total_score"]:
            result.append({
                "level": "error", "code": "pass_score",
                "message": (
                    f"及格分 {_fmt(overview['pass_score'])} 高于总分 "
                    f"{_fmt(overview['total_score'])}，无法发布。"
                ),
            })

        # 4) 题型过于单一
        distribution = PaperAnalysisService.type_distribution(paper)
        if len(distribution) == 1:
            result.append({
                "level": "info", "code": "single_type",
                "message": f"全卷只有「{distribution[0]['label']}」一种题型，建议增加题型以提升区分度。",
            })

        # 5) 客观题比例（自动判分覆盖率）
        objective = sum(
            row["count"] for row in distribution
            if row["type"] in QuestionType.objective_types()
        )
        subjective = overview["question_count"] - objective
        if objective == 0:
            result.append({
                "level": "warning", "code": "all_subjective",
                "message": "全卷均为主观题，将全部依赖 AI 辅助批改，建议加入客观题。",
            })
        elif subjective > 0:
            result.append({
                "level": "info", "code": "subjective_present",
                "message": f"含 {subjective} 道主观题需人工或 AI 批改。",
            })

        # 6) 考试时长与题量的匹配度（经验值：客观题约 1.5 分钟/题，主观题约 8 分钟/题）
        expected = objective * 1.5 + subjective * 8
        duration = overview["duration_minutes"]
        if duration and expected > duration * 1.15:
            result.append({
                "level": "warning", "code": "time_tight",
                "message": (
                    f"按常规作答速度约需 {expected:.0f} 分钟，"
                    f"而考试时长只有 {duration} 分钟，学生可能做不完。"
                ),
            })
        elif duration and expected < duration * 0.5:
            result.append({
                "level": "info", "code": "time_loose",
                "message": f"题量偏少（预计约 {expected:.0f} 分钟可完成），时长 {duration} 分钟略显宽松。",
            })

        return result

    # ------------------------------------------------------------------
    @staticmethod
    def full_report(paper: Paper, *, planned_ids=None) -> dict:
        """一次性给出工作台需要的全部数据（少发几个请求，页面少闪几次）。"""
        coverage = PaperAnalysisService.knowledge_coverage(paper, planned_ids=planned_ids)
        return {
            "overview": PaperAnalysisService.overview(paper),
            "type_distribution": PaperAnalysisService.type_distribution(paper),
            "difficulty_distribution": PaperAnalysisService.difficulty_distribution(paper),
            "knowledge_coverage": coverage,
            "sections": PaperAnalysisService.section_stats(paper),
            "warnings": PaperAnalysisService.warnings(paper, planned_ids=planned_ids),
        }


__all__ = ["PaperAnalysisService", "PaperService"]
