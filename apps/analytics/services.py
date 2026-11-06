"""学情分析服务层。

五个服务，职责边界：

    KnowledgeMasteryService   知识点掌握度矩阵 + 薄弱点识别（含子知识点向上归并）
    TrendService              成绩趋势（历次得分率序列 + 最小二乘回归预测）
    ClassAnalysisService      班级 / 考试维度：分数分布、逐题得分率与区分度、班级薄弱点
    AIAnalysisService         复用 apps.ai 的 analyze_weakness 场景，产出学情诊断（带缓存）
    AnalyticsReportService    聚合入口：一次调用给页面备齐所有图（雷达 / 趋势 / 分布 / 逐题）

三条贯穿全模块的纪律：

1. **只读判分结果，绝不重算分。** 掌握度读的是模块五已经写好的
   ``Answer.is_correct`` / ``Answer.score``。这里若自己再判一次分，
   两套口径迟早会打架（比如多选给分策略改了，学情还按老口径算）。

2. **「薄弱」是一个有统计门槛的结论。** 只做过 1 道题且答错 ≠ 薄弱，
   样本不足只能算「数据不足」。门槛由 ``MIN_SAMPLES_FOR_WEAKNESS`` 控制——
   不给门槛的话，开学第一周所有学生所有知识点都会被标红，学情页就废了。

3. **图表数据一律给「前端可直接喂 ECharts」的形状。** 雷达给
   ``{indicator, values}``、分布给 ``{buckets, stats}``、趋势给 ``{points, fit}``。
   前端只做渲染，不做聚合——聚合口径必须留在服务端，否则两个页面的同一个指标
   会算出两个数。
"""

import json
import logging
import math
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.ai.client import get_ai_client
from apps.ai.exceptions import AIServiceError
from apps.common.constants import AttemptStatus, QuestionType
from apps.common.exceptions import BusinessError
from apps.exam.models import Answer, Exam, ExamAttempt
from apps.papers.models import PaperQuestion
from apps.questions.models import KnowledgePoint

from .constants import (
    AI_MAX_KNOWLEDGE_POINTS,
    AI_REPORT_TTL_HOURS,
    ANALYZABLE_STATUSES,
    DISCRIMINATION_GROUP_RATIO,
    MASTERY_BANDS,
    MAX_WEAK_POINTS,
    MIN_SAMPLES_FOR_WEAKNESS,
    RADAR_TOP_N,
    SCORE_BUCKETS,
    SUBJECTIVE_MASTERY_RATIO,
    TREND_DEFAULT_LIMIT,
    TREND_FLAT_SLOPE,
    TREND_MAX_LIMIT,
    TREND_MIN_POINTS,
    WEAKNESS_RATIO,
    MasteryLevel,
)
from .models import StudentAnalysisReport

logger = logging.getLogger("apps.analytics")

SUBJECTIVE_TYPES = tuple(QuestionType.subjective_types())


# ==================================================================
# 通用工具
# ==================================================================
def _to_float(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round(value, ndigits: int = 4):
    if value is None:
        return None
    return round(float(value), ndigits)


def _safe_div(numerator, denominator):
    """安全除法：分母为 0 时返回 ``None``（不是 0——0 会被误读成「全军覆没」）。"""
    denominator = _to_float(denominator)
    if denominator == 0:
        return None
    return _to_float(numerator) / denominator


def _mean(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def _stdev(values):
    """总体标准差（教学统计里把全班当总体，不按样本自由度校正）。"""
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return 0.0
    mu = sum(values) / len(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / len(values))


def _fmt_dt(value) -> str:
    """统一用本地时区格式化——库里存 UTC，直接给前端会差 8 小时。"""
    if not value:
        return ""
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")


def _mastery_level(ratio, samples: int) -> str:
    """正确率 + 样本量 → 掌握度档位。"""
    if ratio is None or samples < MIN_SAMPLES_FOR_WEAKNESS:
        return MasteryLevel.UNKNOWN
    for floor, level in MASTERY_BANDS:
        if ratio >= floor:
            return level
    return MasteryLevel.WEAK


def _scope_key(subject: str = "") -> str:
    return f"subject:{subject}" if subject else "all"


def _scope_display(subject: str = "") -> str:
    return f"学科：{subject}" if subject else "全部学科"


def _bucket_of(ratio) -> str:
    """得分率 → 分数段标签。"""
    percent = _to_float(ratio) * 100
    for label, floor in SCORE_BUCKETS:
        if percent >= floor:
            return label
    return SCORE_BUCKETS[-1][0]


# ==================================================================
# 一、知识点掌握度
# ==================================================================
class KnowledgeMasteryService:
    """知识点掌握度矩阵与薄弱点识别。

    归并口径（重要）：一道题挂的知识点可能有多层，比如「数据结构 / 树 / 二叉树」。
    学生在这道题上的表现，**既属于「二叉树」，也属于「树」和「数据结构」**——
    只算叶子节点的话，顶层知识点永远是空的，雷达图和班级薄弱点就没法用了。
    所以这里把每条作答向上播报到所有祖先节点，并对同一道题去重，
    避免「一道题同时挂了父与子知识点」时被算两次。
    """

    # ------------------------------------------------------------------
    @staticmethod
    def _answer_queryset(student=None, subject: str = "", exam=None, student_ids=None):
        queryset = Answer.objects.filter(
            attempt__status__in=ANALYZABLE_STATUSES,
        ).select_related("paper_question__question").prefetch_related(
            "paper_question__question__knowledge_points",
        )
        if student is not None:
            queryset = queryset.filter(attempt__student=student)
        if exam is not None:
            queryset = queryset.filter(attempt__exam=exam)
        if student_ids is not None:
            queryset = queryset.filter(attempt__student_id__in=student_ids)
        if subject:
            queryset = queryset.filter(attempt__exam__subject=subject)
        return queryset

    # ------------------------------------------------------------------
    @staticmethod
    def collect_records(student=None, subject: str = "", exam=None, student_ids=None) -> list:
        """把作答明细转成「可聚合的答题记录」。

        只收**已经判过分**的题：
          * 客观题 —— ``is_correct`` 不为 null（null 表示模块五还没判）；
          * 主观题 —— 用得分率衡量，满分必须大于 0 才有意义。
        """
        records = []
        for answer in KnowledgeMasteryService._answer_queryset(
            student, subject, exam, student_ids,
        ):
            paper_question = answer.paper_question
            if paper_question is None:
                continue
            full = _to_float(paper_question.score)
            score = _to_float(answer.score)
            question_type = paper_question.question_type
            is_subjective = question_type in SUBJECTIVE_TYPES

            if is_subjective:
                if full <= 0:
                    continue
                ratio = score / full
                passed = ratio >= SUBJECTIVE_MASTERY_RATIO
            else:
                if answer.is_correct is None:
                    continue
                passed = bool(answer.is_correct)
                ratio = _safe_div(score, full)
                if ratio is None:
                    ratio = 1.0 if passed else 0.0

            question = paper_question.question
            records.append({
                "answer_id": answer.id,
                "attempt_id": answer.attempt_id,
                "question_type": question_type,
                "is_subjective": is_subjective,
                "passed": passed,
                "score": score,
                "full_score": full,
                "ratio": ratio,
                "kp_ids": [kp.id for kp in question.knowledge_points.all()] if question else [],
                "kp_names": [kp.name for kp in question.knowledge_points.all()] if question else [],
            })
        return records

    # ------------------------------------------------------------------
    @staticmethod
    def _kp_map() -> dict:
        """一次性把知识点读进内存。知识点是「几百个」量级，全量加载比递归查询快得多。"""
        return {kp.id: kp for kp in KnowledgePoint.objects.all()}

    @staticmethod
    def _ancestor_ids(kp_id: int, kp_map: dict) -> list:
        """自身 + 所有祖先的 id（带环保护）。"""
        ids, node, guard = [], kp_map.get(kp_id), 0
        while node is not None and guard < 10:
            ids.append(node.id)
            node = kp_map.get(node.parent_id)
            guard += 1
        return ids

    # ------------------------------------------------------------------
    @staticmethod
    def aggregate(records: list, kp_map: dict = None) -> list:
        """答题记录 → 每个知识点的掌握度条目。"""
        kp_map = kp_map if kp_map is not None else KnowledgeMasteryService._kp_map()
        if not kp_map:
            return []

        stats: dict = {}
        for record in records:
            if not record["kp_ids"]:
                continue
            # 同一道题向上播报时对「祖先 ∩ 自身」去重，避免父/子知识点同挂时重复计数
            targets = set()
            for kp_id in record["kp_ids"]:
                targets.update(KnowledgeMasteryService._ancestor_ids(kp_id, kp_map))

            for kp_id in targets:
                bucket = stats.setdefault(kp_id, {
                    "answered": 0, "correct": 0, "score": 0.0, "full_score": 0.0,
                    "direct": 0,
                })
                bucket["answered"] += 1
                bucket["score"] += record["score"]
                bucket["full_score"] += record["full_score"]
                if record["passed"]:
                    bucket["correct"] += 1

            for kp_id in record["kp_ids"]:
                if kp_id in stats:
                    stats[kp_id]["direct"] += 1

        items = []
        for kp_id, bucket in stats.items():
            kp = kp_map.get(kp_id)
            if kp is None or bucket["answered"] <= 0:
                continue
            accuracy = bucket["correct"] / bucket["answered"]
            score_ratio = _safe_div(bucket["score"], bucket["full_score"])
            level = _mastery_level(accuracy, bucket["answered"])
            items.append({
                "knowledge_point_id": kp.id,
                "name": kp.name,
                "full_path": kp.full_path,
                "subject": kp.subject or "",
                "parent_id": kp.parent_id,
                "answered": bucket["answered"],
                "direct_answered": bucket["direct"],
                "correct": bucket["correct"],
                "score": _round(bucket["score"], 2),
                "full_score": _round(bucket["full_score"], 2),
                "accuracy": _round(accuracy),
                "score_ratio": _round(score_ratio),
                "mastery_level": level,
                "mastery_level_display": MasteryLevel(level).label,
                "is_weak": level == MasteryLevel.WEAK,
                "is_insufficient": bucket["answered"] < MIN_SAMPLES_FOR_WEAKNESS,
            })

        items.sort(key=lambda row: (-row["answered"], -row["accuracy"], row["name"]))
        return items

    # ------------------------------------------------------------------
    @staticmethod
    def student_mastery(student, subject: str = "") -> dict:
        """一个学生的知识点掌握度（含雷达图数据与薄弱点）。"""
        records = KnowledgeMasteryService.collect_records(student=student, subject=subject)
        items = KnowledgeMasteryService.aggregate(records)

        weak_points = [
            row for row in items
            if row["is_weak"] and not row["is_insufficient"]
        ]
        weak_points.sort(key=lambda row: (row["accuracy"], -row["answered"]))
        weak_points = weak_points[:MAX_WEAK_POINTS]

        insufficient = [row for row in items if row["is_insufficient"]]
        mastered = [row for row in items if row["mastery_level"] == MasteryLevel.MASTERED]

        total_answered = len(records)
        total_correct = sum(1 for row in records if row["passed"])

        level_counts = {level: 0 for level in MasteryLevel.values}
        for row in items:
            level_counts[row["mastery_level"]] += 1

        radar_items = sorted(items, key=lambda row: -row["answered"])[:RADAR_TOP_N]
        radar_items.sort(key=lambda row: row["full_path"])

        return {
            "student_id": getattr(student, "id", None),
            "scope_key": _scope_key(subject),
            "scope_display": _scope_display(subject),
            "subject": subject or "",
            "items": items,
            "weak_points": weak_points,
            "insufficient": insufficient,
            "mastered": mastered[:MAX_WEAK_POINTS],
            "radar": {
                "indicator": [
                    {"name": row["name"], "max": 100, "full_path": row["full_path"]}
                    for row in radar_items
                ],
                "values": [round((row["accuracy"] or 0) * 100, 1) for row in radar_items],
                "items": [
                    {"name": row["name"], "accuracy": row["accuracy"],
                     "answered": row["answered"], "level": row["mastery_level"]}
                    for row in radar_items
                ],
            },
            "summary": {
                "answered": total_answered,
                "correct": total_correct,
                "accuracy": _round(_safe_div(total_correct, total_answered)),
                "score": _round(sum(r["score"] for r in records), 2),
                "full_score": _round(sum(r["full_score"] for r in records), 2),
                "score_ratio": _round(_safe_div(
                    sum(r["score"] for r in records),
                    sum(r["full_score"] for r in records),
                )),
                "knowledge_point_count": len(items),
                "weak_count": len(weak_points),
                "insufficient_count": len(insufficient),
                "mastered_count": len(mastered),
                "level_counts": level_counts,
            },
        }


# ==================================================================
# 二、成绩趋势
# ==================================================================
class TrendService:
    """成绩趋势与线性回归预测。

    用**最小二乘线性回归**而不是「拿最近两次相减」：两次作答的波动可能来自
    题目难度、身体状况、甚至当天有没有睡好，单次差值没有统计意义。
    点数不足 ``TREND_MIN_POINTS`` 时不做预测（宁可不给结论，也不给错误结论）。
    """

    # ------------------------------------------------------------------
    @staticmethod
    def linear_fit(values: list) -> dict:
        """对一列有序数值做最小二乘拟合。

        返回斜率、截距、决定系数 R²、趋势方向与「下一个点」的预测值。
        """
        series = [float(v) for v in values if v is not None]
        n = len(series)
        if n < 2:
            return {
                "n": n, "slope": None, "intercept": None, "r2": None,
                "direction": "unknown", "predict_next": None, "enough_points": False,
            }

        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(series) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, series))

        if sxx == 0:
            slope, intercept, r2 = 0.0, mean_y, None
        else:
            slope = sxy / sxx
            intercept = mean_y - slope * mean_x
            syy = sum((y - mean_y) ** 2 for y in series)
            r2 = _round((sxy ** 2) / (sxx * syy), 4) if syy else None

        if abs(slope) < TREND_FLAT_SLOPE:
            direction = "flat"
        elif slope > 0:
            direction = "up"
        else:
            direction = "down"

        predict_next = None
        if n >= TREND_MIN_POINTS:
            predict_next = _round(min(1.0, max(0.0, intercept + slope * n)))

        return {
            "n": n,
            "slope": _round(slope),
            "intercept": _round(intercept),
            "r2": r2,
            "direction": direction,
            "predict_next": predict_next,
            "enough_points": n >= TREND_MIN_POINTS,
            "fitted": [_round(intercept + slope * x) for x in xs],
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _class_avg_map(exam_ids: list) -> dict:
        """每场考试的班级平均得分率（一次查询批量算完，避免每次作答查一次库）。"""
        if not exam_ids:
            return {}
        rows = ExamAttempt.objects.filter(
            exam_id__in=exam_ids, status__in=ANALYZABLE_STATUSES,
        ).values_list("exam_id", "score", "total_score_snapshot")

        grouped: dict = {}
        for exam_id, score, total in rows:
            total = _to_float(total)
            if total <= 0:
                continue
            grouped.setdefault(exam_id, []).append(_to_float(score) / total)
        return {exam_id: _mean(values) for exam_id, values in grouped.items()}

    # ------------------------------------------------------------------
    @staticmethod
    def student_trend(student, subject: str = "", limit: int = TREND_DEFAULT_LIMIT) -> dict:
        """一个学生的历次成绩趋势。"""
        limit = max(1, min(int(limit or TREND_DEFAULT_LIMIT), TREND_MAX_LIMIT))

        queryset = (
            ExamAttempt.objects.filter(student=student, status__in=ANALYZABLE_STATUSES)
            .select_related("exam")
            .order_by("submitted_at", "id")
        )
        if subject:
            queryset = queryset.filter(exam__subject=subject)

        attempts = list(queryset)
        if len(attempts) > limit:
            attempts = attempts[-limit:]

        class_avg = TrendService._class_avg_map(list({a.exam_id for a in attempts}))

        points = []
        for index, attempt in enumerate(attempts, start=1):
            total = _to_float(attempt.total_score_snapshot)
            if total <= 0:
                continue
            ratio = _to_float(attempt.score) / total
            average = class_avg.get(attempt.exam_id)
            points.append({
                "index": index,
                "attempt_id": attempt.id,
                "exam_id": attempt.exam_id,
                "exam_title": getattr(attempt.exam, "title", "") or "",
                "subject": getattr(attempt.exam, "subject", "") or "",
                "submitted_at": _fmt_dt(attempt.submitted_at or attempt.started_at),
                "score": _round(attempt.score, 2),
                "total_score": _round(total, 2),
                "ratio": _round(ratio),
                "percent": round(ratio * 100, 1),
                "class_avg_ratio": _round(average),
                "class_avg_percent": round(average * 100, 1) if average is not None else None,
                "delta_vs_class": _round(ratio - average) if average is not None else None,
                "passed": attempt.passed(),
                "is_timeout": attempt.is_timeout,
                "has_subjective": attempt.subjective_score is not None
                                 and _to_float(attempt.subjective_score) > 0,
            })

        fit = TrendService.linear_fit([row["ratio"] for row in points])
        ratios = [row["ratio"] for row in points]
        best = max(points, key=lambda row: row["ratio"], default=None)
        worst = min(points, key=lambda row: row["ratio"], default=None)

        return {
            "student_id": getattr(student, "id", None),
            "subject": subject or "",
            "points": points,
            "fit": fit,
            "summary": {
                "count": len(points),
                "avg_ratio": _round(_mean(ratios)),
                "avg_percent": round((_mean(ratios) or 0) * 100, 1) if ratios else None,
                "first_ratio": ratios[0] if ratios else None,
                "last_ratio": ratios[-1] if ratios else None,
                "change": _round(ratios[-1] - ratios[0]) if len(ratios) > 1 else None,
                "best": {"exam_title": best["exam_title"], "percent": best["percent"]}
                        if best else None,
                "worst": {"exam_title": worst["exam_title"], "percent": worst["percent"]}
                         if worst else None,
                "direction": fit["direction"],
                "predict_percent": (round(fit["predict_next"] * 100, 1)
                                    if fit["predict_next"] is not None else None),
            },
        }


# ==================================================================
# 三、班级 / 考试维度
# ==================================================================
class ClassAnalysisService:
    """一场考试的班级学情。"""

    # ------------------------------------------------------------------
    @staticmethod
    def _attempt_rows(exam, student_ids=None) -> list:
        attempts = (
            exam.attempts.exclude(status=AttemptStatus.IN_PROGRESS)
            .select_related("student").order_by("-score", "id")
        )
        if student_ids is not None:
            attempts = attempts.filter(student_id__in=student_ids)
        rows = []
        for attempt in attempts:
            total = _to_float(attempt.total_score_snapshot)
            ratio = (_to_float(attempt.score) / total) if total > 0 else None
            rows.append({
                "attempt_id": attempt.id,
                "student_id": attempt.student_id,
                "student_name": getattr(attempt.student, "display_name", "") or "",
                "student_no": getattr(attempt.student, "student_no", "") or "",
                "status": attempt.status,
                "status_display": attempt.get_status_display(),
                "graded": attempt.is_graded,
                "score": _round(attempt.score, 2),
                "total_score": _round(total, 2),
                "ratio": _round(ratio),
                "percent": round(ratio * 100, 1) if ratio is not None else None,
                "passed": attempt.passed(),
                "is_timeout": attempt.is_timeout,
                "switch_count": attempt.switch_count,
                "paste_count": attempt.paste_count,
                "suspicious": attempt.switch_count > 0 or attempt.paste_count > 0,
                "time_spent_seconds": attempt.time_spent_seconds,
                "submitted_at": _fmt_dt(attempt.submitted_at),
            })
        return rows

    # ------------------------------------------------------------------
    @staticmethod
    def distribution(rows: list, exam) -> dict:
        """分数段分布 + 集中趋势 + 正态曲线参数。"""
        graded = [row for row in rows if row["graded"] and row["ratio"] is not None]
        ratios = [row["ratio"] for row in graded]
        scores = [row["score"] for row in graded]

        buckets = {label: 0 for label, _ in SCORE_BUCKETS}
        for ratio in ratios:
            buckets[_bucket_of(ratio)] += 1

        pass_count = sum(1 for row in graded if row["passed"])
        mean_ratio = _mean(ratios)
        std_ratio = _stdev(ratios)

        return {
            "buckets": [{"label": label, "count": buckets[label]} for label, _ in SCORE_BUCKETS],
            "stats": {
                "graded_count": len(graded),
                "avg_score": _round(_mean(scores), 2),
                "max_score": _round(max(scores), 2) if scores else None,
                "min_score": _round(min(scores), 2) if scores else None,
                "median_score": _round(_median(scores), 2),
                "avg_ratio": _round(mean_ratio),
                "avg_percent": round(mean_ratio * 100, 1) if mean_ratio is not None else None,
                "std_ratio": _round(std_ratio),
                "pass_count": pass_count,
                "pass_rate": round(pass_count * 100.0 / len(graded), 1) if graded else None,
                "excellent_count": sum(1 for row in graded
                                       if (row["percent"] or 0) >= 90),
            },
            # 前端按正态曲线画时直接取这两个参数，不用自己算标准差
            "normal": {
                "mu": _round(mean_ratio),
                "sigma": _round(std_ratio),
                "total": float(exam.total_score or 0),
            },
        }

    # ------------------------------------------------------------------
    @staticmethod
    def participation(exam, rows: list, student_ids=None) -> dict:
        graded_count = sum(1 for row in rows if row["graded"])
        times = [row["time_spent_seconds"] for row in rows if row["time_spent_seconds"]]
        if student_ids is not None:
            # 指定班级时，「应考」= 班级成员中在参考范围内的学生数
            # 去重兜底：上游 visible_student_ids 已改为 distinct，但入参也可能来自
            # 多班合并等拼装逻辑，直接 len() 会把跨班学生算两遍（应考人数虚高）
            scoped_ids = set(student_ids)
            if exam.open_to_all:
                applicants = len(scoped_ids)
            else:
                applicants = exam.participants.filter(id__in=scoped_ids).count()
            started = exam.attempts.filter(student_id__in=scoped_ids).count()
        else:
            applicants = exam.participant_count()
            started = exam.attempts.count()
        return {
            "applicants": applicants,
            "started": started,
            "submitted": len(rows),
            "graded": graded_count,
            "pending_grading": len(rows) - graded_count,
            "not_started": max(0, applicants - started),
            "timeout_count": sum(1 for row in rows if row["is_timeout"]),
            "avg_time_spent_seconds": int(_mean(times) or 0),
            "submit_rate": round(len(rows) * 100.0 / applicants, 1)
                           if applicants else None,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def behavior(rows: list) -> dict:
        return {
            "switch_total": sum(row["switch_count"] for row in rows),
            "paste_total": sum(row["paste_count"] for row in rows),
            "suspicious_count": sum(1 for row in rows if row["suspicious"]),
        }

    # ------------------------------------------------------------------
    @staticmethod
    def question_analysis(exam, student_ids=None) -> dict:
        """逐题得分率与区分度。

        **区分度**用「总分前 27% 与后 27% 两组在该题上的得分率之差」：
        一道好题应该能区分出高低分学生（差值大），差题（比如答案有歧义、
        或者简单到人人都会）差值接近 0。这是教育测量学里最常用的简易口径。
        """
        paper_questions = list(
            PaperQuestion.objects.filter(paper_id=exam.paper_id)
            .select_related("question").order_by("order", "id")
        )
        answers = (
            Answer.objects.filter(attempt__exam=exam)
            .exclude(attempt__status=AttemptStatus.IN_PROGRESS)
            .values("paper_question_id", "score", "is_correct", "is_answered",
                    "selected_labels", "attempt_id")
        )
        if student_ids is not None:
            answers = answers.filter(attempt__student_id__in=student_ids)
        answers = list(answers)
        rows = ClassAnalysisService._attempt_rows(exam, student_ids)
        ratio_by_attempt = {row["attempt_id"]: row["ratio"] for row in rows
                            if row["ratio"] is not None}

        grouped: dict = {}
        for item in answers:
            grouped.setdefault(item["paper_question_id"], []).append(item)

        # 总分高低分组（各取 27%）
        ordered = sorted(
            (row for row in rows if row["ratio"] is not None),
            key=lambda row: -row["ratio"],
        )
        group_size = max(1, int(round(len(ordered) * DISCRIMINATION_GROUP_RATIO)))
        if len(ordered) < 4:
            top_ids, bottom_ids = set(), set()
        else:
            top_ids = {row["attempt_id"] for row in ordered[:group_size]}
            bottom_ids = {row["attempt_id"] for row in ordered[-group_size:]}

        items = []
        for index, paper_question in enumerate(paper_questions, start=1):
            full = _to_float(paper_question.score)
            records = grouped.get(paper_question.id, [])
            answered = [r for r in records if r["is_answered"]]
            scores = [_to_float(r["score"]) for r in answered]
            score_rate = _safe_div(sum(scores), full * len(answered)) if answered else None

            if paper_question.question_type in SUBJECTIVE_TYPES:
                correct = None
                accuracy = None
            else:
                judged = [r for r in answered if r["is_correct"] is not None]
                correct = sum(1 for r in judged if r["is_correct"])
                accuracy = _safe_div(correct, len(judged))

            def _group_rate(target_ids):
                subset = [r for r in answered if r["attempt_id"] in target_ids]
                if not subset or full <= 0:
                    return None
                return sum(_to_float(r["score"]) for r in subset) / (full * len(subset))

            top_rate = _group_rate(top_ids)
            bottom_rate = _group_rate(bottom_ids)
            discrimination = (_round(top_rate - bottom_rate)
                              if top_rate is not None and bottom_rate is not None else None)

            items.append({
                "order": index,
                "number": paper_question.order,
                "paper_question_id": paper_question.id,
                "question_type": paper_question.question_type,
                "type_display": paper_question.get_question_type_display(),
                "difficulty": paper_question.difficulty,
                "stem": (paper_question.question.stem or "")[:80]
                        if paper_question.question else "",
                "full_score": _round(full, 2),
                "answered_count": len(answered),
                "correct_count": correct,
                "accuracy": _round(accuracy),
                "avg_score": _round(_mean(scores), 2),
                "score_rate": _round(score_rate),
                "discrimination": discrimination,
                "difficulty_label": ClassAnalysisService._difficulty_label(score_rate),
                "is_weak": score_rate is not None and score_rate < WEAKNESS_RATIO,
            })

        rates = [row["score_rate"] for row in items if row["score_rate"] is not None]
        weak_items = sorted(
            [row for row in items if row["is_weak"]],
            key=lambda row: row["score_rate"],
        )
        return {
            "exam": {"id": exam.id, "title": exam.title, "subject": exam.subject or ""},
            "items": items,
            "weak_items": weak_items[:MAX_WEAK_POINTS],
            "summary": {
                "question_count": len(items),
                "avg_score_rate": _round(_mean(rates)),
                "weak_count": len(weak_items),
                "no_discrimination_count": sum(
                    1 for row in items
                    if row["discrimination"] is not None and abs(row["discrimination"]) < 0.1
                ),
            },
        }

    @staticmethod
    def _difficulty_label(score_rate):
        """用**实测得分率**给题目难度定性——比教师预设的难度更接近真实。"""
        if score_rate is None:
            return "数据不足"
        if score_rate >= 0.85:
            return "偏易"
        if score_rate >= 0.60:
            return "适中"
        if score_rate >= 0.40:
            return "偏难"
        return "过难"

    # ------------------------------------------------------------------
    @staticmethod
    def weak_knowledge_points(exam, student_ids=None) -> dict:
        """班级层面的薄弱知识点排行（把全班作答当成一个「大样本」来看）。"""
        records = KnowledgeMasteryService.collect_records(exam=exam, student_ids=student_ids)
        items = KnowledgeMasteryService.aggregate(records)
        weak = [row for row in items if row["is_weak"] and not row["is_insufficient"]]
        weak.sort(key=lambda row: (row["accuracy"], -row["answered"]))
        return {
            "items": weak[:MAX_WEAK_POINTS],
            "summary": {
                "knowledge_point_count": len(items),
                "weak_count": len(weak),
                "answered": len(records),
            },
        }

    # ------------------------------------------------------------------
    @staticmethod
    def exam_report(exam, student_ids=None) -> dict:
        """一场考试的完整班级学情（页面一次拿全）。"""
        rows = ClassAnalysisService._attempt_rows(exam, student_ids)
        return {
            "exam": {
                "id": exam.id,
                "title": exam.title,
                "subject": exam.subject or "",
                "status": exam.status,
                "computed_status": exam.computed_status(),
                "total_score": float(exam.total_score or 0),
                "pass_score": float(exam.effective_pass_score or 0),
                "question_count": exam.question_count,
                "show_score_immediately": exam.show_score_immediately,
            },
            "participation": ClassAnalysisService.participation(exam, rows, student_ids),
            "distribution": ClassAnalysisService.distribution(rows, exam),
            "behavior": ClassAnalysisService.behavior(rows),
            "weak_knowledge_points": ClassAnalysisService.weak_knowledge_points(exam, student_ids),
            "ranking": {
                "top": rows[:5],
                "bottom": rows[-5:][::-1] if len(rows) > 5 else [],
            },
            "rows": rows,
        }


# ==================================================================
# 四、AI 学情诊断
# ==================================================================
class AIAnalysisService:
    """AI 学情诊断（复用 ``apps.ai`` 的 analyze_weakness 场景）。

    分工与模块三的组卷说明一致：**数字由代码算，话由模型说**。
    掌握度矩阵是确定的（可复核、可复现），大模型只负责把它翻译成学生看得懂的
    诊断与建议。所以提示词里喂的是**已经算好的正确率表**，不让模型自己算数。

    降级纪律（与模块五同源）：模型不可用、返回非 JSON、字段缺失，
    一律退化为**本地规则诊断**并标 ``degraded=True``——
    学生打开学情页看到的永远是「一份能看的诊断」，而不是一个报错弹窗。
    """

    # ------------------------------------------------------------------
    @staticmethod
    def _accuracy_table(items: list, limit: int = AI_MAX_KNOWLEDGE_POINTS) -> str:
        """渲染提示词里的「知识点 | 答题数 | 正确率」表。"""
        if not items:
            return "（暂无答题数据）"
        rows = sorted(items, key=lambda row: -row["answered"])[:limit]
        return "\n".join(
            f"{row['full_path']} | {row['answered']} | {round((row['accuracy'] or 0) * 100, 1)}%"
            for row in rows
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _overall_level(ratio) -> str:
        if ratio is None:
            return "数据不足"
        if ratio >= 0.85:
            return "优秀"
        if ratio >= 0.70:
            return "良好"
        if ratio >= 0.60:
            return "中等"
        return "待提升"

    @staticmethod
    def _severity(accuracy) -> str:
        accuracy = _to_float(accuracy)
        if accuracy < 0.40:
            return "high"
        if accuracy < 0.52:
            return "medium"
        return "low"

    # ------------------------------------------------------------------
    @staticmethod
    def _fallback_diagnosis(mastery: dict, student_name: str) -> dict:
        """本地规则诊断——大模型不可用时的兜底。

        结论完全来自真实数据，只是表达方式朴素。宁可朴素也不要空白：
        学生打开页面看到「暂无诊断」会以为功能坏了。
        """
        summary_data = mastery["summary"]
        weak_points = mastery["weak_points"]
        mastered = mastery["mastered"]
        accuracy = summary_data.get("accuracy")

        if not summary_data["answered"]:
            headline = f"{student_name}还没有可供分析的答题记录。"
        elif weak_points:
            weakest = "、".join(row["name"] for row in weak_points[:3])
            headline = (
                f"{student_name}累计作答 {summary_data['answered']} 道题，"
                f"整体正确率 {round((accuracy or 0) * 100, 1)}%，"
                f"其中 {len(weak_points)} 个知识点未达标，最薄弱的是：{weakest}。"
            )
        else:
            headline = (
                f"{student_name}累计作答 {summary_data['answered']} 道题，"
                f"整体正确率 {round((accuracy or 0) * 100, 1)}%，各知识点均已达掌握线。"
            )

        detail_weak = [
            {
                "knowledge_point": row["name"],
                "accuracy": round(row["accuracy"] or 0, 4),
                "severity": AIAnalysisService._severity(row["accuracy"]),
                "comment": (
                    f"作答 {row['answered']} 题，正确率 {round((row['accuracy'] or 0) * 100, 1)}%，"
                    f"低于 {int(WEAKNESS_RATIO * 100)}% 的达标线，建议优先复习。"
                ),
            }
            for row in weak_points[:8]
        ]

        advice = []
        if detail_weak:
            advice.append("先把薄弱知识点按「概念 → 例题 → 变式练习」的顺序过一遍，不要直接刷难题。")
            advice.append("对错题做归因记录：是概念不清、审题失误还是计算粗心，三类问题的解法完全不同。")
        if mastered:
            advice.append(
                "保持已掌握知识点的熟练度，可用少量综合题维持手感："
                + "、".join(row["name"] for row in mastered[:5]) + "。"
            )
        if not advice:
            advice.append("继续按当前节奏完成练习，并适当增加进阶题型以提升区分度。")

        return {
            "summary": headline,
            "overall_level": AIAnalysisService._overall_level(accuracy),
            "weak_points": detail_weak,
            "strengths": [row["name"] for row in mastered[:5]],
            "advice": advice,
            "source": "local",
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_diagnosis(data: dict, mastery: dict, student_name: str) -> dict:
        """把大模型的输出规整成稳定结构。

        模型可能只给一半字段、把 ``advice`` 写成字符串、把 ``accuracy`` 写成 ``"42%"``。
        这里逐字段容错，缺的用本地诊断补齐——**下游永远拿到同一套字段**。
        """
        fallback = AIAnalysisService._fallback_diagnosis(mastery, student_name)
        if not isinstance(data, dict):
            data = {}

        def _as_list(value):
            if isinstance(value, list):
                return [item for item in value if item not in (None, "")]
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            return []

        summary = str(data.get("summary") or "").strip() or fallback["summary"]
        overall = str(data.get("overall_level") or "").strip()
        if overall not in ("优秀", "良好", "中等", "待提升"):
            overall = fallback["overall_level"]

        weak_points = []
        for item in _as_list(data.get("weak_points")):
            if isinstance(item, str):
                weak_points.append({
                    "knowledge_point": item, "accuracy": None,
                    "severity": "medium", "comment": "",
                })
                continue
            if not isinstance(item, dict):
                continue
            accuracy = item.get("accuracy")
            if isinstance(accuracy, str):
                accuracy = accuracy.strip().rstrip("%")
                try:
                    accuracy = float(accuracy) / (100 if "%" in str(item.get("accuracy")) else 1)
                except (TypeError, ValueError):
                    accuracy = None
            elif accuracy is not None:
                try:
                    accuracy = float(accuracy)
                except (TypeError, ValueError):
                    accuracy = None
            severity = str(item.get("severity") or "").strip().lower()
            if severity not in ("high", "medium", "low"):
                severity = AIAnalysisService._severity(accuracy)
            weak_points.append({
                "knowledge_point": str(item.get("knowledge_point")
                                       or item.get("name") or "").strip(),
                "accuracy": accuracy,
                "severity": severity,
                "comment": str(item.get("comment") or "").strip(),
            })
        weak_points = [row for row in weak_points if row["knowledge_point"]]
        if not weak_points:
            weak_points = fallback["weak_points"]

        advice = [str(item).strip() for item in _as_list(data.get("advice")) if str(item).strip()]
        if not advice:
            advice = fallback["advice"]

        strengths = [str(item).strip() for item in _as_list(data.get("strengths"))
                     if str(item).strip()] or fallback["strengths"]

        return {
            "summary": summary,
            "overall_level": overall,
            "weak_points": weak_points,
            "strengths": strengths,
            "advice": advice,
            "source": "ai",
        }

    # ------------------------------------------------------------------
    @staticmethod
    def diagnose(student, subject: str = "", *, force: bool = False, user=None,
                 provider: str = None) -> dict:
        """生成（或命中缓存返回）一份学情诊断。

        ``force=False`` 且缓存未过期时直接返回库里的那份——学生在学情页反复刷新
        不该被反复计费，而且模型不是确定性函数，每次结论都不一样反而让人困惑。
        """
        scope_key = _scope_key(subject)
        student_name = (getattr(student, "display_name", "")
                        or getattr(student, "username", "") or "该学生")

        mastery = KnowledgeMasteryService.student_mastery(student, subject)
        if not mastery["items"] and not mastery["summary"]["answered"]:
            raise BusinessError("该学生还没有已判分的答题记录，暂时无法生成学情诊断", code=14001)

        cached = StudentAnalysisReport.objects.of_student(student).for_scope(scope_key).first()
        if cached and not force and not cached.is_stale(AI_REPORT_TTL_HOURS):
            payload = cached.to_payload()
            payload.update(AIAnalysisService._flatten(payload))
            payload["cached"] = True
            payload["degraded"] = cached.provider == "local"
            payload["degraded_reason"] = "" if cached.provider != "local" else "当前诊断由本地规则生成"
            return payload

        variables = {
            "student_name": student_name,
            "scope": mastery["scope_display"],
            "accuracy_table": AIAnalysisService._accuracy_table(mastery["items"]),
        }

        degraded_reason = ""
        provider_name, model_name, is_mock = "local", "", False
        try:
            client = get_ai_client(provider)
            result = client.run_scene("analyze_weakness", user=user, **variables)
            parsed = result.json(strict=False)
            if not parsed:
                raise AIServiceError("模型未返回可解析的 JSON")
            diagnosis = AIAnalysisService._normalize_diagnosis(parsed, mastery, student_name)
            provider_name = client.name
            model_name = client.provider.model
            is_mock = bool(client.is_mock)
        except AIServiceError as exc:
            degraded_reason = f"AI 调用失败：{exc}"
            logger.warning("学情诊断降级（student=%s）：%s", getattr(student, "id", None), exc)
            diagnosis = AIAnalysisService._fallback_diagnosis(mastery, student_name)
        except Exception as exc:  # noqa: BLE001 —— 任何意外都不该让学生看不到学情页
            degraded_reason = f"AI 调用异常：{exc}"
            logger.exception("学情诊断异常降级（student=%s）", getattr(student, "id", None))
            diagnosis = AIAnalysisService._fallback_diagnosis(mastery, student_name)

        overall_level = str(diagnosis.get("overall_level") or "")
        payload_text = json.dumps(diagnosis, ensure_ascii=False)
        now = timezone.now()

        with transaction.atomic():
            report, _ = StudentAnalysisReport.objects.update_or_create(
                student=student,
                scope_key=scope_key,
                defaults={
                    "scope_display": mastery["scope_display"],
                    "subject": subject or "",
                    "summary": str(diagnosis.get("summary") or ""),
                    "overall_level": overall_level,
                    "weak_point_count": len(diagnosis.get("weak_points") or []),
                    "knowledge_point_count": mastery["summary"]["knowledge_point_count"],
                    "sample_size": mastery["summary"]["answered"],
                    "payload": payload_text,
                    "provider": provider_name,
                    "model": model_name,
                    "is_mock": is_mock,
                    "generated_at": now,
                },
            )

        payload = report.to_payload()
        payload.update(AIAnalysisService._flatten(payload))
        payload["cached"] = False
        payload["degraded"] = bool(degraded_reason)
        payload["degraded_reason"] = degraded_reason
        logger.info(
            "生成学情诊断：学生 #%s（%s），知识点 %s 个，薄弱 %s 个，降级=%s",
            getattr(student, "id", None), scope_key,
            mastery["summary"]["knowledge_point_count"],
            payload["weak_point_count"], bool(degraded_reason),
        )
        return payload

    # ------------------------------------------------------------------
    @staticmethod
    def _flatten(payload: dict) -> dict:
        """把 detail 里的三个列表提到顶层，页面少写一层取值。"""
        detail = payload.get("detail") or {}
        return {
            "weak_points": detail.get("weak_points") or [],
            "strengths": detail.get("strengths") or [],
            "advice": detail.get("advice") or [],
            "overall_level": payload.get("overall_level") or detail.get("overall_level") or "",
        }


# ==================================================================
# 五、聚合入口
# ==================================================================
class AnalyticsReportService:
    """页面/接口的聚合入口——一次调用备齐所有图的数据。"""

    # ------------------------------------------------------------------
    @staticmethod
    def student_subjects(student) -> list:
        """这个学生做过题的学科列表（页面筛选下拉）。"""
        rows = (
            ExamAttempt.objects.filter(student=student, status__in=ANALYZABLE_STATUSES)
            .exclude(exam__subject="")
            .values_list("exam__subject", flat=True)
        )
        return sorted({subject for subject in rows if subject})

    # ------------------------------------------------------------------
    @staticmethod
    def student_report(student, subject: str = "", *,
                       trend_limit: int = TREND_DEFAULT_LIMIT) -> dict:
        """学生个人学情总览（雷达 + 趋势 + 薄弱点 + 已有诊断）。

        **这里不会自动调大模型**：GET 请求顺手调一次 AI，学生会把页面刷新成
        计费器。AI 诊断由页面上的按钮触发 ``POST .../diagnosis/``。
        """
        mastery = KnowledgeMasteryService.student_mastery(student, subject)
        trend = TrendService.student_trend(student, subject, trend_limit)

        cached = (StudentAnalysisReport.objects.of_student(student)
                  .for_scope(_scope_key(subject)).first())
        diagnosis = None
        if cached:
            diagnosis = cached.to_payload()
            diagnosis.update(AIAnalysisService._flatten(diagnosis))
            diagnosis["stale"] = cached.is_stale(AI_REPORT_TTL_HOURS)

        return {
            "student": {
                "id": getattr(student, "id", None),
                "name": getattr(student, "display_name", "") or "",
                "student_no": getattr(student, "student_no", "") or "",
                "username": getattr(student, "username", "") or "",
            },
            "scope_key": _scope_key(subject),
            "scope_display": _scope_display(subject),
            "subject": subject or "",
            "subjects": AnalyticsReportService.student_subjects(student),
            "mastery": mastery,
            "trend": trend,
            "diagnosis": diagnosis,
            "thresholds": {
                "weakness_ratio": WEAKNESS_RATIO,
                "min_samples": MIN_SAMPLES_FOR_WEAKNESS,
                "subjective_mastery_ratio": SUBJECTIVE_MASTERY_RATIO,
                "trend_min_points": TREND_MIN_POINTS,
            },
        }

    # ------------------------------------------------------------------
    @staticmethod
    def class_report(exam, *, include_rows: bool = False, student_ids=None) -> dict:
        """班级学情总览（分布 + 逐题 + 薄弱点 + 参与度）。"""
        report = ClassAnalysisService.exam_report(exam, student_ids)
        report["questions"] = ClassAnalysisService.question_analysis(exam, student_ids)
        if not include_rows:
            report.pop("rows", None)
        return report

    # ------------------------------------------------------------------
    @staticmethod
    def exam_options(user=None) -> list:
        """教师可分析的考试列表（有作答记录的）。"""
        queryset = Exam.objects.with_relations().filter(attempts__isnull=False).distinct()
        if user is not None and not getattr(user, "is_admin", False):
            queryset = queryset.filter(created_by=user)

        rows = []
        for exam in queryset.order_by("-created_at")[:200]:
            submitted = exam.attempts.exclude(status=AttemptStatus.IN_PROGRESS).count()
            rows.append({
                "id": exam.id,
                "title": exam.title,
                "subject": exam.subject or "",
                "status": exam.status,
                "computed_status": exam.computed_status(),
                "total_score": float(exam.total_score or 0),
                "question_count": exam.question_count,
                "submitted_total": submitted,
                "graded_total": exam.attempts.filter(
                    status__in=(AttemptStatus.AUTO_GRADED, AttemptStatus.GRADED)).count(),
            })
        return rows


__all__ = [
    "AIAnalysisService",
    "AnalyticsReportService",
    "ClassAnalysisService",
    "KnowledgeMasteryService",
    "TrendService",
]
