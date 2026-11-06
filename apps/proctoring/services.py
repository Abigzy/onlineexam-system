"""作弊行为检测模块的服务层。

四个服务，一条线串起来：

    BehaviorLogService       把前端上报的行为事件「逐条」落成事实
            │
            ▼
    RuleEngineService        把事实翻译成「可疑信号」（阈值可配、每条都能回答「因为什么」）
            │
            ▼
    AnomalyDetectionService  规则之外再补一层无监督离群发现（孤立森林，样本不足就不判）
            │
            ▼
    ProctoringReportService  组装「可疑考生列表」与「逐条证据链」给教师看

三条纪律（与模块五、六、七一脉相承）：

1. **只读不重算。** 行为计数是模块四维护的（``ExamAttempt.switch_count`` 等），
   这里仅在日志缺失时用它兜底（取 ``max``），绝不反过来改写考试模块的计数。
2. **结论必须可解释。** 每条可疑信号都带 ``code`` / ``name`` / ``reason`` / ``evidence``，
   教师看到红色标记时一定能点开看到「因为什么」。
3. **样本不足不给结论。** 埋点太少就老实说「数据不足」，不硬凑一份异常名单。
"""

import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.exam.constants import BehaviorEvent
from apps.exam.models import Exam, ExamAttempt

from .constants import (
    ANALYZED_EVENT_TYPES,
    ANOMALY_CONTAMINATION,
    ANOMALY_FEATURES,
    ANOMALY_MIN_SAMPLES,
    ANOMALY_RANDOM_STATE,
    BURST_EVENT_COUNT,
    BURST_WINDOW_SECONDS,
    FAST_ANSWER_MIN_QUESTIONS,
    FAST_ANSWER_SECONDS_PER_QUESTION,
    FULLSCREEN_EXIT_OVER_LIMIT,
    LOG_RETENTION_DAYS,
    LONG_ABSENCE_HIGH_SECONDS,
    LONG_ABSENCE_SECONDS,
    MAD_TO_SIGMA,
    MAX_TIMELINE_EVENTS,
    PASTE_OVER_LIMIT,
    RISK_ORDER,
    ROBUST_Z_THRESHOLD,
    RULE_CATALOG,
    SWITCH_FREQUENCY_MIN_MINUTES,
    SWITCH_FREQUENCY_PER_MINUTE,
    SWITCH_LIKE_EVENTS,
    RiskLevel,
    level_for,
    rule_meta,
)
from .models import ExamBehaviorLog

logger = logging.getLogger("apps.proctoring.services")


# ------------------------------------------------------------------
# 小工具
# ------------------------------------------------------------------
def _safe_int(value, default=0, *, minimum=None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    return number


def _safe_float(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _round(value, digits=2):
    return round(_safe_float(value), digits)


def _parse_dt(value):
    """把前端可能传来的 ISO 字符串解析成 datetime；解析不了返回 None（用服务端时间兜底）。"""
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def _attempt_duration_seconds(attempt, now) -> int:
    """本次作答的时长（秒）：优先用模块四记好的实际用时。"""
    if attempt.time_spent_seconds:
        return int(attempt.time_spent_seconds)
    end = attempt.submitted_at or now
    if attempt.started_at:
        return max(0, int((end - attempt.started_at).total_seconds()))
    return 0


def _burst_max(logs, window_seconds: int) -> int:
    """滑动窗口内的最大事件条数（用 ``count`` 加权）。"""
    points = sorted((log.occurred_at, log.count) for log in logs if log.occurred_at)
    if not points:
        return 0
    best = running = 0
    left = 0
    for right in range(len(points)):
        running += points[right][1]
        while (points[right][0] - points[left][0]).total_seconds() > window_seconds:
            running -= points[left][1]
            left += 1
        best = max(best, running)
    return best


# ==================================================================
# 1. 埋点写入
# ==================================================================
class BehaviorLogService:
    """把前端上报的行为事件逐条落库。

    与模块四 ``ExamAttemptService.record_behavior`` 的分工：
    模块四负责「计数 + 阈值 + 强制交卷」，这里负责「落明细」。
    两个动作由模块四在一次上报里一起触发（见 ``record_behavior``）。
    """

    #: 一条埋点里 ``detail`` 字段的长度上限（MySQL 5.6 的 TextField 够用，但别塞垃圾）
    MAX_DETAIL_CHARS = 2000

    @staticmethod
    @transaction.atomic
    def record_events(attempt: ExamAttempt, events: list, *, now=None,
                      ip: str = "", user_agent: str = "") -> dict:
        """返回 ``{"created": N, "by_type": {类型: 条数}}``。

        不会抛异常打断考试链路：未知类型 / 脏字段一律跳过或兜底。
        """
        result = {"created": 0, "by_type": {}}
        if not events:
            return result

        now = now or timezone.now()
        exam = attempt.exam
        default_ip = (ip or attempt.ip_address or "")[:45]
        default_ua = (user_agent or attempt.user_agent or "")[:255]

        rows = []
        by_type = defaultdict(int)
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            # 心跳频繁且无分析价值：不落明细（模块四也只计数不展开）
            if event_type == BehaviorEvent.HEARTBEAT:
                continue
            if event_type not in ANALYZED_EVENT_TYPES:
                # 未知类型模块四已经记过 warning 了，这里静默跳过，别刷两遍日志
                continue

            count = _safe_int(event.get("count"), 1, minimum=1)
            duration = _safe_int(event.get("duration_seconds", event.get("duration")), 0, minimum=0)
            detail = event.get("detail")
            detail_text = ""
            if isinstance(detail, (dict, list)):
                try:
                    detail_text = json.dumps(detail, ensure_ascii=False)[:BehaviorLogService.MAX_DETAIL_CHARS]
                except (TypeError, ValueError):
                    detail_text = ""

            rows.append(ExamBehaviorLog(
                attempt=attempt,
                exam=exam,
                student_id=attempt.student_id,
                event_type=event_type,
                count=count,
                occurred_at=_parse_dt(event.get("occurred_at")) or now,
                duration_seconds=duration,
                question_id=event.get("question_id") or None,
                question_index=_safe_int(event.get("question_index"), 0, minimum=0),
                ip_address=(event.get("ip_address") or default_ip)[:45],
                user_agent=(event.get("user_agent") or default_ua)[:255],
                detail=detail_text,
            ))
            by_type[event_type] += count

        if rows:
            ExamBehaviorLog.objects.bulk_create(rows)

        result["created"] = len(rows)
        result["by_type"] = dict(by_type)
        return result

    @staticmethod
    def purge_old_logs(days: int = LOG_RETENTION_DAYS, *, dry_run: bool = False) -> int:
        """裁剪过期的明细埋点（写多读少的数据，需要保留策略）。返回删除条数。"""
        if days is None or days <= 0:
            return 0
        cutoff = timezone.now() - timezone.timedelta(days=days)
        queryset = ExamBehaviorLog.objects.filter(occurred_at__lt=cutoff)
        if dry_run:
            return queryset.count()
        deleted, _ = queryset.delete()
        return deleted


# ==================================================================
# 2. 规则引擎
# ==================================================================
class RuleEngineService:
    """把埋点翻译成可疑信号。

    输入是「一次作答」，输出是一份可解释的判定：
    总分、档位、以及命中的每一条规则（含一句话理由与证据数字）。
    """

    @staticmethod
    def build_metrics(attempt: ExamAttempt, logs=None) -> dict:
        """从埋点 + 聚合计数里提炼出规则要用的指标。"""
        now = timezone.now()
        if logs is None:
            logs = list(ExamBehaviorLog.objects.of_attempt(attempt).analyzed())
        else:
            logs = list(logs)

        # 逐条日志的聚合（口径与模块四的计数对齐：blur 也并进切屏类）
        switch_logged = sum(log.count for log in logs if log.event_type in SWITCH_LIKE_EVENTS)
        paste_logged = sum(log.count for log in logs if log.event_type == BehaviorEvent.PASTE)
        fullscreen_logged = sum(log.count for log in logs
                                if log.event_type == BehaviorEvent.FULLSCREEN_EXIT)

        # 日志缺失时用模块四的计数兜底（取 max，绝不反过来改考试模块的计数）
        switch_count = max(_safe_int(attempt.switch_count), switch_logged)
        paste_count = max(_safe_int(attempt.paste_count), paste_logged)
        fullscreen_exit_count = max(_safe_int(attempt.fullscreen_exit_count), fullscreen_logged)

        max_absence = max([_safe_int(log.duration_seconds) for log in logs] or [0])
        event_total = sum(log.count for log in logs)

        ips = {log.ip_address for log in logs if log.ip_address}
        if attempt.ip_address:
            ips.add(attempt.ip_address)
        uas = {log.user_agent for log in logs if log.user_agent}
        if attempt.user_agent:
            uas.add(attempt.user_agent)

        duration_seconds = _attempt_duration_seconds(attempt, now)
        duration_minutes = duration_seconds / 60.0
        answered = _safe_int(attempt.answered_count)
        seconds_per_question = (duration_seconds / answered) if answered else None

        return {
            "switch_count": switch_count,
            "switch_limit": _safe_int(attempt.exam.max_switch_count, 0),
            "switch_per_minute": _round(switch_count / duration_minutes) if duration_minutes > 0 else 0.0,
            "switch_logged": switch_logged,
            "paste_count": paste_count,
            "fullscreen_exit_count": fullscreen_exit_count,
            "max_absence_seconds": max_absence,
            "event_total": event_total,
            "event_per_minute": _round(event_total / duration_minutes) if duration_minutes > 0 else 0.0,
            "burst_max": _burst_max(logs, BURST_WINDOW_SECONDS),
            "ip_count": len(ips),
            "ip_list": sorted(ips),
            "distinct_user_agents": len(uas),
            "duration_seconds": duration_seconds,
            "duration_minutes": _round(duration_minutes),
            "answered_count": answered,
            "seconds_per_question": _round(seconds_per_question) if seconds_per_question is not None else None,
            "log_count": len(logs),
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _signal(code: str, points: int, *, reason: str, evidence: dict, level=None) -> dict:
        """组装一条可疑信号。``name`` 从规则目录取，保证与「规则说明」页一处定义。"""
        meta = rule_meta(code)
        return {
            "code": code,
            "name": meta.get("name", code),
            "level": str(level or meta.get("level") or RiskLevel.MEDIUM),
            "level_display": RiskLevel(level or meta.get("level") or RiskLevel.MEDIUM).label,
            "points": int(points),
            "reason": reason,
            "evidence": evidence,
            "advice": meta.get("advice", ""),
        }

    @staticmethod
    def _one_level_up(level: str) -> str:
        ordered = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.SEVERE]
        try:
            index = ordered.index(level)
        except ValueError:
            return level
        return ordered[min(index + 1, len(ordered) - 1)]

    @staticmethod
    def _evaluate_signals(m: dict) -> list:
        """按规则目录逐条判定。返回命中的信号列表。"""
        signals = []
        limit = m["switch_limit"]

        # R1 切屏超限
        if m["switch_count"] > limit:
            extra = m["switch_count"] - limit
            base = rule_meta("switch_over_limit").get("points", 30)
            points = base + min(base, extra * 5)
            level = RiskLevel.HIGH
            if limit > 0 and extra >= limit:
                level = RiskLevel.SEVERE
            signals.append(RuleEngineService._signal(
                "switch_over_limit", points, level=level,
                reason=f"切屏/离开页面 {m['switch_count']} 次，超过本场允许的 {limit} 次",
                evidence={"switch_count": m["switch_count"], "max_switch_count": limit,
                          "exceeded_by": extra},
            ))

        # R2 切屏过于频繁
        if (m["duration_minutes"] >= SWITCH_FREQUENCY_MIN_MINUTES
                and m["switch_per_minute"] >= SWITCH_FREQUENCY_PER_MINUTE):
            base = rule_meta("switch_frequent").get("points", 18)
            points = base + min(base, int(m["switch_per_minute"]) * 3)
            signals.append(RuleEngineService._signal(
                "switch_frequent", points,
                reason=f"作答 {m['duration_minutes']} 分钟，切屏 {m['switch_count']} 次"
                       f"（约 {m['switch_per_minute']} 次/分钟）",
                evidence={"switch_per_minute": m["switch_per_minute"],
                          "duration_minutes": m["duration_minutes"],
                          "switch_count": m["switch_count"]},
            ))

        # R3 单次离开过久
        if m["max_absence_seconds"] >= LONG_ABSENCE_SECONDS:
            base = rule_meta("long_absence").get("points", 25)
            level = RiskLevel.HIGH
            points = base
            if m["max_absence_seconds"] >= LONG_ABSENCE_HIGH_SECONDS:
                level = RiskLevel.SEVERE
                points = base + 15
            signals.append(RuleEngineService._signal(
                "long_absence", points, level=level,
                reason=f"单次离开页面最长 {m['max_absence_seconds']} 秒"
                       f"（阈值 {LONG_ABSENCE_SECONDS} 秒）",
                evidence={"max_absence_seconds": m["max_absence_seconds"],
                          "threshold_seconds": LONG_ABSENCE_SECONDS},
            ))

        # R4 粘贴外部内容
        if m["paste_count"] >= PASTE_OVER_LIMIT:
            base = rule_meta("paste_content").get("points", 20)
            points = base + min(base, (m["paste_count"] - 1) * 5)
            signals.append(RuleEngineService._signal(
                "paste_content", points,
                reason=f"粘贴 {m['paste_count']} 次（阈值 {PASTE_OVER_LIMIT} 次）",
                evidence={"paste_count": m["paste_count"], "threshold": PASTE_OVER_LIMIT},
            ))

        # R5 多次退出全屏
        if m["fullscreen_exit_count"] >= FULLSCREEN_EXIT_OVER_LIMIT:
            base = rule_meta("fullscreen_exit").get("points", 12)
            points = base + min(base, (m["fullscreen_exit_count"] - FULLSCREEN_EXIT_OVER_LIMIT) * 4)
            signals.append(RuleEngineService._signal(
                "fullscreen_exit", points,
                reason=f"退出全屏 {m['fullscreen_exit_count']} 次"
                       f"（阈值 {FULLSCREEN_EXIT_OVER_LIMIT} 次）",
                evidence={"fullscreen_exit_count": m["fullscreen_exit_count"],
                          "threshold": FULLSCREEN_EXIT_OVER_LIMIT},
            ))

        # R6 答题速度异常
        if (m["answered_count"] >= FAST_ANSWER_MIN_QUESTIONS
                and m["seconds_per_question"] is not None
                and m["seconds_per_question"] < FAST_ANSWER_SECONDS_PER_QUESTION):
            base = rule_meta("answer_too_fast").get("points", 15)
            signals.append(RuleEngineService._signal(
                "answer_too_fast", base,
                reason=f"已答 {m['answered_count']} 题，平均每题仅 "
                       f"{m['seconds_per_question']} 秒（阈值 {FAST_ANSWER_SECONDS_PER_QUESTION} 秒）",
                evidence={"answered_count": m["answered_count"],
                          "seconds_per_question": m["seconds_per_question"],
                          "threshold": FAST_ANSWER_SECONDS_PER_QUESTION},
            ))

        # R7 作答期间 IP 变更
        if m["ip_count"] > 1:
            base = rule_meta("ip_changed").get("points", 22)
            points = base + min(base, (m["ip_count"] - 2) * 6)
            signals.append(RuleEngineService._signal(
                "ip_changed", points,
                reason=f"同一次作答的来源 IP 出现 {m['ip_count']} 个：{'、'.join(m['ip_list'][:3])}",
                evidence={"ip_count": m["ip_count"], "ip_list": m["ip_list"]},
            ))

        # R8 作答期间设备变更
        if m["distinct_user_agents"] > 1:
            base = rule_meta("device_changed").get("points", 18)
            signals.append(RuleEngineService._signal(
                "device_changed", base,
                reason=f"同一次作答出现 {m['distinct_user_agents']} 种设备（User-Agent）",
                evidence={"distinct_user_agents": m["distinct_user_agents"]},
            ))

        # R9 短时密集事件
        if m["burst_max"] >= BURST_EVENT_COUNT:
            base = rule_meta("event_burst").get("points", 12)
            points = base + min(base, (m["burst_max"] - BURST_EVENT_COUNT) * 3)
            signals.append(RuleEngineService._signal(
                "event_burst", points,
                reason=f"{BURST_WINDOW_SECONDS} 秒内出现 {m['burst_max']} 条行为事件"
                       f"（阈值 {BURST_EVENT_COUNT} 条）",
                evidence={"burst_max": m["burst_max"],
                          "window_seconds": BURST_WINDOW_SECONDS,
                          "threshold": BURST_EVENT_COUNT},
            ))

        # 高分在前，便于前端把最要紧的放最上面
        signals.sort(key=lambda item: item["points"], reverse=True)
        return signals

    @staticmethod
    def evaluate(attempt: ExamAttempt, logs=None) -> dict:
        """对一次作答给出完整判定。"""
        metrics = RuleEngineService.build_metrics(attempt, logs)
        signals = RuleEngineService._evaluate_signals(metrics)

        score = min(100, sum(signal["points"] for signal in signals))
        level = level_for(score)
        insufficient = metrics["log_count"] == 0 and metrics["event_total"] == 0

        if insufficient:
            note = "本次作答没有任何行为埋点，判定依据不足（可能是正常答题或未采集到数据）"
        elif not signals:
            note = "未命中任何可疑规则"
        else:
            note = f"命中 {len(signals)} 条可疑规则"

        return {
            "attempt_id": attempt.id,
            "student_id": attempt.student_id,
            "score": score,
            "level": str(level),
            "level_display": RiskLevel(level).label,
            "risk_order": RISK_ORDER.get(level, 0),
            "is_suspicious": RISK_ORDER.get(level, 0) >= RISK_ORDER[RiskLevel.MEDIUM],
            "signals": signals,
            "signal_codes": [signal["code"] for signal in signals],
            "reasons": [signal["reason"] for signal in signals],
            "insufficient": insufficient,
            "note": note,
            "metrics": metrics,
        }


# ==================================================================
# 3. 异常检测（孤立森林 + 降级）
# ==================================================================
class AnomalyDetectionService:
    """无监督离群发现。

    规则引擎只能发现「预期的坏」（阈值写在代码里），
    孤立森林能发现「不像别人的」——比如某种谁也没预设过的异常组合。
    两者互补：规则负责可解释，模型负责兜底。

    设计纪律：
      * **样本不足不给结论**：一场考试少于 ``ANOMALY_MIN_SAMPLES`` 份作答就别训练；
      * **可降级**：未安装 scikit-learn 时退化为纯 Python 的稳健 z-score，
        功能不中断，只是方法名不同（``method`` 字段如实告知）；
      * **可复现**：随机种子固定，同样的数据每次给出同样的名单。
    """

    METHOD_FOREST = "isolation_forest"
    METHOD_ZSCORE = "robust_zscore"

    @staticmethod
    def available() -> bool:
        """孤立森林是否可用（依赖是否装好）。"""
        try:
            import sklearn.ensemble  # noqa: F401
        except Exception:  # noqa: BLE001  依赖缺失或版本不兼容都算不可用
            return False
        return True

    @staticmethod
    def feature_vector(metrics: dict) -> list:
        """按 ``ANOMALY_FEATURES`` 的顺序取特征。顺序即含义，改这里要同步改常量。"""
        return [
            _safe_float(metrics.get("switch_count")),
            _safe_float(metrics.get("paste_count")),
            _safe_float(metrics.get("fullscreen_exit_count")),
            _safe_float(metrics.get("max_absence_seconds")),
            _safe_float(metrics.get("event_per_minute")),
            _safe_float(metrics.get("seconds_per_question")),
            _safe_float(metrics.get("ip_count")),
            1.0 if _safe_int(metrics.get("distinct_user_agents")) > 1 else 0.0,
            _safe_float(metrics.get("burst_max")),
        ]

    @staticmethod
    def _normalize(raw_scores: list) -> list:
        """把任意尺度的分数线性映射到 0~1（全相同则全 0）。"""
        if not raw_scores:
            return []
        low, high = min(raw_scores), max(raw_scores)
        if high - low < 1e-12:
            return [0.0 for _ in raw_scores]
        return [round((value - low) / (high - low), 4) for value in raw_scores]

    @staticmethod
    def _robust_zscore(vectors: list) -> tuple:
        """纯 Python 降级：对每个特征算稳健 z 分（median + MAD），取最大者。

        返回 ``(最大z列表, 最可疑特征index列表)``。
        """
        if not vectors:
            return [], []
        columns = list(zip(*vectors))
        medians = [statistics.median(column) for column in columns]
        scales = []
        for column, median in zip(columns, medians):
            mad = statistics.median([abs(value - median) for value in column])
            scale = mad * MAD_TO_SIGMA
            # MAD 为 0 是常见情况（过半样本取值相同），此时退化为标准差；
            # 若标准差也为 0，说明这一维完全没有差异，无法据此判离群。
            if scale <= 1e-12:
                scale = statistics.pstdev(column)
            scales.append(scale)

        max_zs, top_indexes = [], []
        for vector in vectors:
            zs = []
            for value, median, scale in zip(vector, medians, scales):
                zs.append(abs(value - median) / scale if scale > 1e-12 else 0.0)
            top = max(range(len(zs)), key=lambda index: zs[index])
            max_zs.append(zs[top])
            top_indexes.append(top)
        return max_zs, top_indexes

    @staticmethod
    def detect(exam: Exam, attempts=None) -> dict:
        """对该场考试的全部作答做离群分析。"""
        if attempts is None:
            attempts = list(ExamAttempt.objects.filter(exam=exam).order_by("id"))
        else:
            attempts = list(attempts)

        sample_size = len(attempts)
        base = {
            "available": False,
            "method": None,
            "method_display": "",
            "sample_size": sample_size,
            "required_samples": ANOMALY_MIN_SAMPLES,
            "features": list(ANOMALY_FEATURES),
            "outlier_count": 0,
            "results": [],
            "reason": "",
        }

        if sample_size < ANOMALY_MIN_SAMPLES:
            base["reason"] = (f"样本不足（{sample_size} 份作答 < {ANOMALY_MIN_SAMPLES} 份），"
                              f"暂不做离群判定")
            return base

        # 一次性取该场考试的所有埋点，按 attempt 分组，避免逐份作答查库
        logs = ExamBehaviorLog.objects.filter(exam=exam).analyzed()
        grouped = defaultdict(list)
        for log in logs:
            grouped[log.attempt_id].append(log)

        metrics_list = [RuleEngineService.build_metrics(attempt, grouped.get(attempt.id, []))
                        for attempt in attempts]
        vectors = [AnomalyDetectionService.feature_vector(metrics) for metrics in metrics_list]

        method = AnomalyDetectionService.METHOD_FOREST
        top_indexes = [None] * sample_size
        try:
            from sklearn.ensemble import IsolationForest

            model = IsolationForest(
                n_estimators=100,
                contamination=min(max(ANOMALY_CONTAMINATION, 0.01), 0.5),
                random_state=ANOMALY_RANDOM_STATE,
            )
            model.fit(vectors)
            # score_samples 越大越「正常」，取负号让「越大越可疑」
            raw_scores = [-float(value) for value in model.score_samples(vectors)]
            flags = [bool(value == -1) for value in model.predict(vectors)]
            # 特征贡献度：孤立森林不直接给，用降级算法算出「最突出的特征」作为解释线索
            _, top_indexes = AnomalyDetectionService._robust_zscore(vectors)
        except Exception as exc:  # noqa: BLE001  依赖缺失 / 训练失败都降级，绝不抛给页面
            logger.warning("孤立森林不可用（%s），降级为稳健 z-score", exc)
            method = AnomalyDetectionService.METHOD_ZSCORE
            raw_scores, top_indexes = AnomalyDetectionService._robust_zscore(vectors)
            flags = [score >= ROBUST_Z_THRESHOLD for score in raw_scores]
            base["reason"] = "未安装 scikit-learn（或训练失败），已用稳健 z-score 近似"

        normalized = AnomalyDetectionService._normalize(raw_scores)
        results = []
        for index, attempt in enumerate(attempts):
            top_index = top_indexes[index] if top_indexes[index] is not None else None
            results.append({
                "attempt_id": attempt.id,
                "student_id": attempt.student_id,
                "anomaly_score": normalized[index],
                "is_outlier": flags[index],
                "top_feature": (ANOMALY_FEATURES[top_index]
                                if top_index is not None and top_index < len(ANOMALY_FEATURES) else ""),
            })

        base.update({
            "available": True,
            "method": method,
            "method_display": ("孤立森林（IsolationForest）" if method == AnomalyDetectionService.METHOD_FOREST
                               else "稳健 z-score（降级）"),
            "outlier_count": sum(1 for flag in flags if flag),
            "results": results,
        })
        return base


# ==================================================================
# 4. 报告聚合
# ==================================================================
class ProctoringReportService:
    """把规则判定与异常检测组装成教师能看的「名单 + 证据链」。"""

    @staticmethod
    def exam_options(user=None) -> list:
        """有行为数据的考试列表（页面选择器用）。"""
        exams = (Exam.objects.filter(behavior_logs__isnull=False)
                 .distinct().select_related("paper").order_by("-created_at"))
        rows = []
        for exam in exams:
            logs = ExamBehaviorLog.objects.filter(exam=exam)
            rows.append({
                "id": exam.id,
                "title": exam.title,
                "subject": exam.subject or "",
                "status": str(exam.status),
                "log_count": logs.count(),
                "attempt_count": logs.values("attempt_id").distinct().count(),
                "student_count": logs.values("student_id").distinct().count(),
            })
        return rows

    # ------------------------------------------------------------------
    @staticmethod
    def _level_distribution(rows: list) -> dict:
        distribution = {level: 0 for level in RiskLevel.values}
        for row in rows:
            level = row.get("risk_level") or RiskLevel.NORMAL
            distribution[level] = distribution.get(level, 0) + 1
        return distribution

    @staticmethod
    def exam_report(exam: Exam, *, only_suspicious: bool = False, top_n: int = 0) -> dict:
        """可疑考生列表 + 概览 + 异常检测。"""
        attempts = list(ExamAttempt.objects.filter(exam=exam)
                        .select_related("student").order_by("id"))
        logs = list(ExamBehaviorLog.objects.filter(exam=exam).analyzed())
        grouped = defaultdict(list)
        for log in logs:
            grouped[log.attempt_id].append(log)

        anomaly = AnomalyDetectionService.detect(exam, attempts)
        anomaly_map = {row["attempt_id"]: row for row in anomaly["results"]}

        rows = []
        event_total = 0
        type_counter = defaultdict(int)
        for log in logs:
            event_total += log.count
            type_counter[log.event_type] += log.count

        for attempt in attempts:
            attempt_logs = grouped.get(attempt.id, [])
            evaluation = RuleEngineService.evaluate(attempt, attempt_logs)
            anomaly_row = anomaly_map.get(attempt.id) or {}
            metrics = evaluation["metrics"]
            rows.append({
                "attempt_id": attempt.id,
                "student_id": attempt.student_id,
                "student_name": (getattr(attempt.student, "display_name", "")
                                 or attempt.student.username),
                "student_no": getattr(attempt.student, "student_no", "") or "",
                "status": str(attempt.status),
                "has_behavior": bool(attempt_logs),
                "log_count": len(attempt_logs),
                "switch_count": metrics["switch_count"],
                "paste_count": metrics["paste_count"],
                "fullscreen_exit_count": metrics["fullscreen_exit_count"],
                "max_absence_seconds": metrics["max_absence_seconds"],
                "answered_count": metrics["answered_count"],
                "duration_minutes": metrics["duration_minutes"],
                "risk_score": evaluation["score"],
                "risk_level": evaluation["level"],
                "risk_level_display": evaluation["level_display"],
                "risk_order": evaluation["risk_order"],
                "is_suspicious": evaluation["is_suspicious"],
                "signal_codes": evaluation["signal_codes"],
                "signal_names": [signal["name"] for signal in evaluation["signals"]],
                "reasons": evaluation["reasons"],
                "note": evaluation["note"],
                "anomaly_score": anomaly_row.get("anomaly_score"),
                "is_outlier": bool(anomaly_row.get("is_outlier")),
            })

        # 排序：先按风险分，再按异常分，最后按学号稳定排序
        rows.sort(key=lambda row: (-row["risk_score"],
                                   -(row["anomaly_score"] or 0.0),
                                   row["student_no"] or str(row["student_id"])))

        flagged = [row for row in rows if row["is_suspicious"]]
        visible = flagged if only_suspicious else rows
        if top_n and top_n > 0:
            visible = visible[:top_n]

        return {
            "exam": {
                "id": exam.id,
                "title": exam.title,
                "subject": exam.subject or "",
                "status": str(exam.status),
                "detect_switch": exam.detect_switch,
                "detect_paste": exam.detect_paste,
                "fullscreen_required": exam.fullscreen_required,
                "max_switch_count": exam.max_switch_count,
            },
            "overview": {
                "attempt_count": len(rows),
                "with_behavior_count": sum(1 for row in rows if row["has_behavior"]),
                "suspicious_count": len(flagged),
                "outlier_count": anomaly["outlier_count"],
                "event_total": event_total,
                "event_by_type": dict(type_counter),
                "level_distribution": ProctoringReportService._level_distribution(rows),
            },
            "anomaly": anomaly,
            "thresholds": ProctoringReportService.thresholds(),
            "rows": visible,
            "flagged_count": len(flagged),
        }

    # ------------------------------------------------------------------
    @staticmethod
    def attempt_evidence(attempt: ExamAttempt) -> dict:
        """一次作答的逐条证据链。"""
        logs = list(ExamBehaviorLog.objects.of_attempt(attempt)
                    .with_relations().recent_first()[:MAX_TIMELINE_EVENTS])
        evaluation = RuleEngineService.evaluate(attempt, logs)

        # 单独取这份作答在整场考试里的异常分（离群判定要有全班样本才成立）
        anomaly = AnomalyDetectionService.detect(attempt.exam)
        anomaly_row = next((row for row in anomaly["results"] if row["attempt_id"] == attempt.id),
                           None)

        # 计数一致性核对：埋点聚合 与 模块四聚合计数 对不对得上
        switch_logged = sum(log.count for log in logs if log.event_type in SWITCH_LIKE_EVENTS)
        counter_check = {
            "switch_logged": switch_logged,
            "switch_count": _safe_int(attempt.switch_count),
            "paste_logged": sum(log.count for log in logs if log.event_type == BehaviorEvent.PASTE),
            "paste_count": _safe_int(attempt.paste_count),
            "fullscreen_logged": sum(log.count for log in logs
                                     if log.event_type == BehaviorEvent.FULLSCREEN_EXIT),
            "fullscreen_exit_count": _safe_int(attempt.fullscreen_exit_count),
        }
        counter_check["consistent"] = (
            counter_check["switch_logged"] == counter_check["switch_count"]
            and counter_check["paste_logged"] == counter_check["paste_count"]
            and counter_check["fullscreen_logged"] == counter_check["fullscreen_exit_count"]
        )

        # 事件类型分布（前端画时间线柱状图用）
        type_counter = defaultdict(int)
        for log in logs:
            type_counter[log.event_type] += log.count
        distribution = [
            {"type": event_type,
             "display": BehaviorEvent(event_type).label if event_type in BehaviorEvent.values else event_type,
             "count": count}
            for event_type, count in sorted(type_counter.items(), key=lambda item: -item[1])
        ]

        return {
            "attempt": {
                "id": attempt.id,
                "exam_id": attempt.exam_id,
                "exam_title": attempt.exam.title,
                "student_id": attempt.student_id,
                "student_name": (getattr(attempt.student, "display_name", "")
                                 or attempt.student.username),
                "student_no": getattr(attempt.student, "student_no", "") or "",
                "status": str(attempt.status),
                "started_at": (timezone.localtime(attempt.started_at).strftime("%Y-%m-%d %H:%M:%S")
                               if attempt.started_at else ""),
                "submitted_at": (timezone.localtime(attempt.submitted_at).strftime("%Y-%m-%d %H:%M:%S")
                                 if attempt.submitted_at else ""),
                "duration_minutes": evaluation["metrics"]["duration_minutes"],
                "answered_count": evaluation["metrics"]["answered_count"],
                "question_count": _safe_int(attempt.question_count),
                "ip_address": attempt.ip_address,
                "user_agent": attempt.user_agent,
                "submit_reason": str(attempt.submit_reason or ""),
                "is_timeout": bool(attempt.is_timeout),
            },
            "risk": {
                "score": evaluation["score"],
                "level": evaluation["level"],
                "level_display": evaluation["level_display"],
                "is_suspicious": evaluation["is_suspicious"],
                "signals": evaluation["signals"],
                "note": evaluation["note"],
                "insufficient": evaluation["insufficient"],
            },
            "metrics": evaluation["metrics"],
            "anomaly": {
                "available": anomaly["available"],
                "method": anomaly["method"],
                "method_display": anomaly["method_display"],
                "sample_size": anomaly["sample_size"],
                "required_samples": anomaly["required_samples"],
                "reason": anomaly["reason"],
                "outlier_count": anomaly["outlier_count"],
                "attempt_score": (anomaly_row or {}).get("anomaly_score"),
                "is_outlier": bool((anomaly_row or {}).get("is_outlier")),
                "top_feature": (anomaly_row or {}).get("top_feature", ""),
            },
            "timeline": [log.to_payload() for log in logs],
            "distribution": distribution,
            "counter_check": counter_check,
            "thresholds": ProctoringReportService.thresholds(),
        }

    # ------------------------------------------------------------------
    @staticmethod
    def thresholds() -> dict:
        """当前生效的阈值（页面与接口都读这一份，避免「两处各说各话」）。"""
        return {
            "switch_frequency_per_minute": SWITCH_FREQUENCY_PER_MINUTE,
            "switch_frequency_min_minutes": SWITCH_FREQUENCY_MIN_MINUTES,
            "long_absence_seconds": LONG_ABSENCE_SECONDS,
            "long_absence_high_seconds": LONG_ABSENCE_HIGH_SECONDS,
            "paste_over_limit": PASTE_OVER_LIMIT,
            "fullscreen_exit_over_limit": FULLSCREEN_EXIT_OVER_LIMIT,
            "fast_answer_seconds_per_question": FAST_ANSWER_SECONDS_PER_QUESTION,
            "fast_answer_min_questions": FAST_ANSWER_MIN_QUESTIONS,
            "burst_window_seconds": BURST_WINDOW_SECONDS,
            "burst_event_count": BURST_EVENT_COUNT,
            "suspicious_min_level": str(RiskLevel.MEDIUM),
            "anomaly_min_samples": ANOMALY_MIN_SAMPLES,
            "anomaly_contamination": ANOMALY_CONTAMINATION,
            "log_retention_days": LOG_RETENTION_DAYS,
        }

    @staticmethod
    def rules_catalog() -> dict:
        """规则说明（让「可解释」有据可查）。"""
        return {
            "rules": [
                {
                    "code": rule["code"],
                    "name": rule["name"],
                    "metric": rule["metric"],
                    "points": rule["points"],
                    "level": str(rule["level"]),
                    "level_display": RiskLevel(rule["level"]).label,
                    "threshold_desc": rule["threshold_desc"],
                    "advice": rule["advice"],
                }
                for rule in RULE_CATALOG
            ],
            "levels": [{"value": str(level), "label": label}
                       for level, label in RiskLevel.choices],
            "thresholds": ProctoringReportService.thresholds(),
            "anomaly_available": AnomalyDetectionService.available(),
            "anomaly_method": (AnomalyDetectionService.METHOD_FOREST
                               if AnomalyDetectionService.available()
                               else AnomalyDetectionService.METHOD_ZSCORE),
        }


__all__ = [
    "AnomalyDetectionService",
    "BehaviorLogService",
    "ProctoringReportService",
    "RuleEngineService",
]
