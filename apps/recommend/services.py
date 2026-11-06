"""个性化推荐服务层。

三个服务，职责边界：

    ContentBasedService        内容推荐：按「知识点薄弱度 × 难度适配度」给题目打内容分
    CollaborativeFilteringService  协同过滤：学生×题目得分矩阵 + item-based 余弦相似度
    RecommendationService      聚合入口：每日成集、本地推荐理由、AI 改写理由（带降级）

四条贯穿全模块的纪律：

1. **只读，而且只读两样东西。** 推荐的输入只有「模块五的判分结果」与
   「模块六的知识点掌握度」。掌握度直接调 ``KnowledgeMasteryService.student_mastery()``
   拿，绝不在这里重算一遍——否则接口和学情页会给出两套薄弱点。

2. **样本不足不给推荐。** 这个门槛不是在本模块新设的：``student_mastery()`` 返回的
   ``weak_points`` 已经过滤掉了「作答数 < MIN_SAMPLES_FOR_WEAKNESS」的知识点。
   也就是说「只做过 1 题」永远不会变成一次推荐，函数不会为了凑满 6 道题去推它。

3. **协同过滤是加成，不是必需。** 冷启动（只有你一个学生、或题目刚导入）时
   item-based 相似度全是 0，推荐要能**自动退化为纯内容推荐**，而不是给出 6 道
   「匹配度 0%」的题。所以 CF 以乘数形式参与：``match = 内容分 × (1 + 0.3 × CF)``。

4. **AI 只负责措辞，不负责选题目。** 选哪几道题完全由代码决定（可复现、可解释），
   大模型只把「为什么推这道题」写得更像人话。模型挂了就退回本地规则话术，
   「每日练习」页面永远打得开。
"""

import hashlib
import logging
import math

from django.core.cache import cache
from django.db.models import Count
from django.utils import timezone

from apps.ai.client import get_ai_client
from apps.ai.exceptions import AIServiceError
from apps.analytics.constants import (
    MIN_SAMPLES_FOR_WEAKNESS,
    SUBJECTIVE_MASTERY_RATIO,
    WEAKNESS_RATIO,
)
from apps.analytics.services import KnowledgeMasteryService
from apps.common.constants import QuestionType
from apps.exam.models import Answer
from apps.questions.models import Question

from .constants import (
    AI_MAX_CANDIDATES,
    AI_REASON_TTL_HOURS,
    CANDIDATE_POOL_MAX,
    CF_BONUS,
    CF_MAX_ITEMS,
    CF_MIN_COMMON_STUDENTS,
    DAILY_DEFAULT_SIZE,
    DAILY_MAX_SIZE,
    DEFAULT_TARGET_DIFFICULTY,
    DIFFICULTY_SPAN,
    DIFFICULTY_WEIGHT,
    EVIDENCE_FULL_SAMPLE,
    KP_WEIGHT,
    MAX_PER_KNOWLEDGE_POINT,
)

logger = logging.getLogger("apps.recommend")

SUBJECTIVE_TYPES = tuple(QuestionType.subjective_types())
# 协同过滤证据达到这个程度才在推荐理由里提一句（0.5 ≈ 余弦相似度的「中等相近」）
CF_REASON_THRESHOLD = 0.5
# 只收「已判分」的作答（与学情分析同一口径）
ANALYZABLE_STATUSES = ("auto_graded", "reviewing", "graded")
# 入门练习：难度 1~2 算「易」，优先成集（不足再补 3、4）
STARTER_EASY_DIFFICULTY = 2
# 入门练习没有掌握度可比，匹配度只由「难度贴合度」加成：0.6 起步、最高 1.0
STARTER_MATCH_BASE = 0.6

# 推荐理由提示词的「模式指令」。starter 的学生在该学科**没有任何作答数据**，
# 若仍按 normal 的「薄弱知识点」口径下指令，模型只能编出「正确率暂未统计，
# 属于你的薄弱环节」这类与事实相悖的话——所以模式不同，规则必须不同。
AI_PRACTICE_MODE_LABELS = {
    "normal": "常规推荐（依据薄弱知识点）",
    "starter": "入门练习（冷启动，无历史作答）",
}
AI_PRACTICE_MODE_RULES = {
    "normal": (
        "推荐原则：\n"
        "1. 优先覆盖学生的薄弱知识点；\n"
        "2. 难度以「略高于当前水平」为宜，避免过难打击信心；\n"
        "3. 同一知识点不要连续堆叠太多题；\n"
        "4. 说明每道题为什么被推荐（一句话即可）。"
    ),
    "starter": (
        "推荐原则：\n"
        "1. 这是入门练习：学生在该学科还没有任何作答记录，本组题用于建立掌握度基线；\n"
        "2. 以难度 1~2 的题为主，让学生先建立信心；\n"
        "3. 同一知识点不要连续堆叠太多题；\n"
        "4. 禁止出现「正确率」「薄弱」「错题」这类措辞——该学科没有作答数据，这么说与事实不符；\n"
        "5. 说明每道题为什么被推荐（一句话即可）。"
    ),
}
# starter 下不喂「薄弱知识点」，改喂一句事实说明，避免模型顺手引用
AI_STARTER_WEAK_PLACEHOLDER = "（入门练习：该学科暂无作答记录，不参考历史正确率）"


# ==================================================================
# 通用工具
# ==================================================================
def _to_float(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round(value, ndigits: int = 4):
    if value is None:
        return None
    return round(float(value), ndigits)


def _daily_key(student_id, date_str: str, question_id) -> str:
    """当天内稳定的「伪随机」排序键。

    匹配度完全相同时（比如一批同知识点同难度的题）按这个键排，
    同一天刷新页面顺序不变；换一天顺序会变，学生不会每天看到一模一样的清单。
    用哈希而不是 ``random``，是为了让结果**可复现**——同样的输入永远同样的输出。
    """
    seed = f"{student_id}|{date_str}|{question_id}"
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


# ==================================================================
# 一、内容推荐
# ==================================================================
class ContentBasedService:
    """基于薄弱知识点的内容推荐。

    一道题值不值得推，看两件事：

    * **弱不弱** —— 它考的知识点正确率有多低（``1 - 正确率``）；
    * **合不合身** —— 难度是不是「略高于当前水平」。
      提示词里明确写着「避免过难打击信心」，所以难度**不是越高越好**，
      而是越接近目标难度越好。
    """

    # ------------------------------------------------------------------
    @staticmethod
    def target_difficulty(accuracy) -> int:
        """整体正确率 → 目标难度（1..4）。

        无作答数据时退回中等，不猜。
        """
        if accuracy is None:
            return int(DEFAULT_TARGET_DIFFICULTY)
        raw = 1 + round(_to_float(accuracy) * 3)
        return int(min(4, max(1, raw)))

    @staticmethod
    def difficulty_fit(difficulty, target) -> float:
        """难度适配度：完全命中目标难度为 1，差满三档为 0。"""
        gap = abs(int(difficulty or 0) - int(target))
        return max(0.0, 1.0 - gap / DIFFICULTY_SPAN)

    @staticmethod
    def evidence(answered) -> float:
        """证据充分度：作答量达到 ``EVIDENCE_FULL_SAMPLE`` 记为 1，不足则按比例折算。"""
        return min(1.0, _to_float(answered) / EVIDENCE_FULL_SAMPLE)

    @staticmethod
    def kp_weakness(weak_row: dict) -> float:
        """知识点薄弱度 = (1 - 正确率) × 证据充分度。"""
        weakness = 1.0 - _to_float(weak_row["accuracy"])
        return weakness * ContentBasedService.evidence(weak_row["answered"])

    # ------------------------------------------------------------------
    @staticmethod
    def weak_map(mastery: dict) -> dict:
        """``{知识点 id: 薄弱条目}``。

        注意 ``mastery["weak_points"]`` 已经把「作答数不足」的知识点排除在外了，
        所以这里不需要再判一次样本量。
        """
        return {row["knowledge_point_id"]: row for row in mastery.get("weak_points") or []}

    @staticmethod
    def descendants_of_many(kp_ids, kp_map: dict) -> set:
        """一批知识点各自的「自身 + 全部后代」的并集。

        为什么要往**下**展开：学情分析把作答记录向上归并到祖先，所以
        「父知识点薄弱」意味着「挂在它任意子知识点上的题也属于这个弱项」。
        把弱项的后代全部捞出来，才能一次查询把候选题目池缩到正确的范围。
        """
        children = {}
        for kp in kp_map.values():
            children.setdefault(kp.parent_id, []).append(kp.id)

        collected = set()
        frontier = [kp_id for kp_id in kp_ids if kp_id in kp_map]
        guard = 0
        while frontier and guard < 10:
            collected.update(frontier)
            nxt = []
            for kp_id in frontier:
                nxt.extend(children.get(kp_id, []))
            frontier = [kp_id for kp_id in nxt if kp_id not in collected]
            guard += 1
        return collected

    # ------------------------------------------------------------------
    @staticmethod
    def match_question(question, weak_map: dict, kp_map: dict):
        """这道题命中哪个薄弱知识点（命中多个时取最薄弱的那个）。"""
        best = None
        for kp in question.knowledge_points.all():
            for kp_id in KnowledgeMasteryService._ancestor_ids(kp.id, kp_map):
                row = weak_map.get(kp_id)
                if row is None:
                    continue
                if best is None or _to_float(row["accuracy"]) < _to_float(best["accuracy"]):
                    best = row
        return best

    # ------------------------------------------------------------------
    @staticmethod
    def score_candidates(subject: str, weak_map: dict, kp_map: dict, *,
                         exclude_ids=None, accuracy=None, subjects=None) -> list:
        """薄弱知识点覆盖的题目 → 带内容分的候选列表（按内容分降序）。

        ``subjects`` 是「学生已加入的学科」白名单，只在 ``subject`` 为空
        （全部学科）时生效：全科推荐同样不能越过学生加入的学科范围。
        """
        if not weak_map or not kp_map:
            return []

        target_ids = ContentBasedService.descendants_of_many(weak_map.keys(), kp_map)
        if not target_ids:
            return []

        queryset = (
            Question.objects.active().with_relations()
            .filter(knowledge_points__id__in=target_ids)
            .distinct()
        )
        if subject:
            queryset = queryset.filter(subject=subject)
        elif subjects:
            queryset = queryset.filter(subject__in=list(subjects))
        if exclude_ids:
            queryset = queryset.exclude(id__in=exclude_ids)

        target = ContentBasedService.target_difficulty(accuracy)
        rows = []
        for question in queryset:
            weak = ContentBasedService.match_question(question, weak_map, kp_map)
            if weak is None:
                continue
            weakness = ContentBasedService.kp_weakness(weak)
            fit = ContentBasedService.difficulty_fit(question.difficulty, target)
            rows.append({
                "question": question,
                "question_id": question.id,
                "question_type": question.type,
                "type_display": question.get_type_display(),
                "stem": question.stem,
                "difficulty": question.difficulty,
                "difficulty_display": question.get_difficulty_display(),
                "subject": question.subject or "",
                "knowledge_point": weak["name"],
                "knowledge_point_id": weak["knowledge_point_id"],
                "weak_accuracy": _round(weak["accuracy"]),
                "weak_answered": weak["answered"],
                "difficulty_fit": _round(fit),
                "content_score": _round(KP_WEIGHT * weakness + DIFFICULTY_WEIGHT * fit),
            })

        rows.sort(key=lambda row: (-row["content_score"], row["question_id"]))
        return rows[:CANDIDATE_POOL_MAX]


# ==================================================================
# 二、协同过滤
# ==================================================================
class CollaborativeFilteringService:
    """item-based 协同过滤。

    「和你做错过的题考点相近的题，你也容易错」——这就是全部思想。

    做法：

    1. 建 ``学生 × 题目 → 是否达标`` 的 0/1 矩阵（取模块五的判分结果）；
    2. 把每道题的「作答者向量」拿出来，两两算余弦相似度
       （``CF_MIN_COMMON_STUDENTS`` 个共同作答者之下判为 0——
       只有一两个共同点的「相似」是巧合，不是规律）；
    3. 对某个学生，把候选题目与「他做错过的每一道题」的相似度求平均，
       就是他在这道题上的协同得分。

    **冷启动是常态，不是异常**：一个新学生、一次没考过的知识点、
    或者题库刚导入时，相似度全为 0。这时返回空字典，
    由上层退化为纯内容推荐（见 ``RecommendationService.daily_practice``）。
    """

    # ------------------------------------------------------------------
    @staticmethod
    def _passed_of(question_type, is_correct, score, full_score):
        """这道题算不算「达标」。口径与学情分析完全一致。"""
        if question_type in SUBJECTIVE_TYPES:
            full = _to_float(full_score)
            if full <= 0:
                return None
            return _to_float(score) / full >= SUBJECTIVE_MASTERY_RATIO
        if is_correct is None:
            return None
        return bool(is_correct)

    # ------------------------------------------------------------------
    @staticmethod
    def records() -> list:
        """``[(student_id, question_id, 1.0/0.0), ...]`` —— 已判分的作答。"""
        rows = (
            Answer.objects
            .filter(
                attempt__status__in=ANALYZABLE_STATUSES,
                paper_question__question__isnull=False,
            )
            .values_list(
                "attempt__student_id",
                "paper_question__question_id",
                "paper_question__question_type",
                "is_correct",
                "score",
                "paper_question__score",
            )
        )
        records = []
        for student_id, question_id, qtype, is_correct, score, full in rows:
            passed = CollaborativeFilteringService._passed_of(qtype, is_correct, score, full)
            if passed is None:
                continue
            records.append((student_id, question_id, 1.0 if passed else 0.0))
        return records

    @staticmethod
    def build_matrix(records=None):
        """返回 ``(per_student, per_question)`` 两张倒排表。

        * ``per_student[student_id] = {question_id: 1.0/0.0}``
        * ``per_question[question_id] = {student_id: 1.0/0.0}``

        题目数量按作答量截断到 ``CF_MAX_ITEMS``，避免题库上千道时
        相似度计算变成 O(n²) 的灾难。
        """
        records = CollaborativeFilteringService.records() if records is None else records
        per_student, per_question = {}, {}
        for student_id, question_id, value in records:
            per_student.setdefault(student_id, {})[question_id] = value
            per_question.setdefault(question_id, {})[student_id] = value

        if len(per_question) > CF_MAX_ITEMS:
            kept = sorted(per_question, key=lambda qid: -len(per_question[qid]))[:CF_MAX_ITEMS]
            per_question = {qid: per_question[qid] for qid in kept}
        return per_student, per_question

    # ------------------------------------------------------------------
    @staticmethod
    def cosine(left: dict, right: dict) -> float:
        """两个「作答者向量」的余弦相似度；共同作答者太少则判 0。"""
        if not left or not right:
            return 0.0
        common = left.keys() & right.keys()
        if len(common) < CF_MIN_COMMON_STUDENTS:
            return 0.0
        dot = sum(left[key] * right[key] for key in common)
        norm_left = math.sqrt(sum(value * value for value in left.values()))
        norm_right = math.sqrt(sum(value * value for value in right.values()))
        if norm_left == 0 or norm_right == 0:
            return 0.0
        return dot / (norm_left * norm_right)

    @staticmethod
    def wrong_items(student_id, per_student) -> list:
        """这个学生做错的题目 id（按题去重）。"""
        row = per_student.get(student_id) or {}
        return [qid for qid, value in row.items() if value == 0.0]

    # ------------------------------------------------------------------
    @staticmethod
    def score(student_id, candidate_ids, per_student, per_question) -> dict:
        """候选题目 → 协同得分（已归一化到 0~1）。

        没有任何弱项（没做错过题）或算不出相似度时返回空字典，
        表示「本次没有协同证据可用」。
        """
        wrong = CollaborativeFilteringService.wrong_items(student_id, per_student)
        if not wrong:
            return {}

        candidates = [qid for qid in candidate_ids if qid in per_question]
        if not candidates:
            return {}

        raw = {}
        for qid in candidates:
            vector = per_question.get(qid) or {}
            total = 0.0
            for wrong_id in wrong:
                total += CollaborativeFilteringService.cosine(
                    vector, per_question.get(wrong_id) or {},
                )
            raw[qid] = total / len(wrong)

        ceiling = max(raw.values()) if raw else 0.0
        if ceiling <= 0:
            return {}
        return {qid: value / ceiling for qid, value in raw.items()}


# ==================================================================
# 三、聚合入口
# ==================================================================
class RecommendationService:
    """每日练习的成集与推荐理由。"""

    # ------------------------------------------------------------------
    @staticmethod
    def _reason(question_row: dict, answered_before: bool) -> str:
        """本地规则推荐理由——**一句话**说清「为什么推这道题」。

        这是降级路径的默认话术，也是 AI 不可用时的兜底：
        宁可朴素，也不能让卡片上的「推荐理由」是空白。
        """
        parts = [
            f"「{question_row['knowledge_point']}」正确率 "
            f"{round(_to_float(question_row['weak_accuracy']) * 100, 1)}%"
            f"（{question_row['weak_answered']} 题作答）",
            f"本题难度{question_row['difficulty_display']}，贴合你当前水平",
        ]
        if _to_float(question_row.get("collaborative_score")) >= CF_REASON_THRESHOLD:
            parts.append("同类题目你失分较多")
        if answered_before:
            parts.append("这是你做错过的原题")
        return "，".join(parts) + "，建议现在练一遍。"

    # ------------------------------------------------------------------
    @staticmethod
    def _item_payload(row: dict) -> dict:
        """候选行 → 页面/接口直接渲染的题目卡片。"""
        question = row["question"]
        return {
            "question_id": row["question_id"],
            "type": row["question_type"],
            "type_display": row["type_display"],
            "stem": row["stem"],
            "difficulty": row["difficulty"],
            "difficulty_display": row["difficulty_display"],
            "subject": row["subject"],
            "knowledge_point": row["knowledge_point"],
            "weak_accuracy": row["weak_accuracy"],
            "weak_answered": row["weak_answered"],
            "answered_before": row["answered_before"],
            "match_score": row["match_score"],
            "match_percent": row["match_percent"],
            "content_score": row["content_score"],
            "collaborative_score": row["collaborative_score"],
            "reason": row["reason"],
            "source": row["source"],
            # 练习模式要能看到答案与解析——不然「练习」无从谈起
            "options": question.student_view()["options"],
            "reference_answer": question.correct_answer_text(),
            "analysis": question.analysis or "",
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _starter_rows(student_id, subject: str, *, correct_ids, answered_ids,
                      date_str: str, allowed_subjects=None) -> list:
        """入门练习（starter）的候选池：易题优先 + 知识点均摊 + 当天稳定打散。

        冷启动时没有任何掌握度可用，能依据的只有「题目本身」：先给难度 1~2 的题
        建立信心，再按知识点摊开，避免一组练习全压在同一个知识点上。
        排序键里没有随机数——同一天刷新永远是同一份清单。

        ``allowed_subjects`` 是「学生已加入的学科」白名单。``subject`` 为空
        （全部学科）时必须靠它收口：否则入门练习会从学生没加入的学科里抽题。
        """
        queryset = Question.objects.active().with_relations()
        if subject:
            queryset = queryset.filter(subject=subject)
        elif allowed_subjects:
            queryset = queryset.filter(subject__in=list(allowed_subjects))
        if correct_ids:
            queryset = queryset.exclude(id__in=correct_ids)

        target = int(DEFAULT_TARGET_DIFFICULTY)
        rows = []
        for question in queryset:
            kp = next(iter(question.knowledge_points.all()), None)
            fit = ContentBasedService.difficulty_fit(question.difficulty, target)
            match_score = _round(STARTER_MATCH_BASE + (1 - STARTER_MATCH_BASE) * fit)
            rows.append({
                "question": question,
                "question_id": question.id,
                "question_type": question.type,
                "type_display": question.get_type_display(),
                "stem": question.stem,
                "difficulty": question.difficulty,
                "difficulty_display": question.get_difficulty_display(),
                "subject": question.subject or "",
                "knowledge_point": kp.name if kp else "",
                "knowledge_point_id": kp.id if kp else None,
                # 入门练习没有「薄弱度」可讲，留空而不是编一个 0%
                "weak_accuracy": None,
                "weak_answered": 0,
                "answered_before": question.id in answered_ids,
                "match_score": match_score,
                "match_percent": round(match_score * 100, 1),
                "content_score": match_score,
                "collaborative_score": 0.0,
                "reason": "该学科入门题，帮你建立掌握度基线。",
                "source": "starter",
            })

        # 难度梯度：1~2 同一档优先；不够再补 3、4（3 在 4 之前）。
        # 同档内用「学生 + 日期 + 题目」的哈希打散：跨天换题、当天不换。
        rows.sort(key=lambda row: (
            0 if row["difficulty"] <= STARTER_EASY_DIFFICULTY else row["difficulty"] - 1,
            _daily_key(student_id, date_str, row["question_id"]),
        ))

        # 知识点均摊：每个知识点最多 MAX_PER_KNOWLEDGE_POINT 道。
        # 与正常模式不同，这里**不留溢出区**——入门练习宁可少给几道，
        # 也不要让一个知识点把所有名额占满。
        picked, used = [], {}
        for row in rows:
            kp_id = row["knowledge_point_id"]
            if kp_id is not None:
                if used.get(kp_id, 0) >= MAX_PER_KNOWLEDGE_POINT:
                    continue
                used[kp_id] = used.get(kp_id, 0) + 1
            picked.append(row)
        return picked

    # ------------------------------------------------------------------
    @staticmethod
    def _starter_practice(student, subject: str, payload: dict, *, date_str: str,
                          size: int, offset: int, allowed_subjects=None) -> dict:
        """冷启动兜底：该学科还没有作答样本时，先给一组入门练习。

        「没作答」不等于「没得练」：新学生、或老学生切到没做过的学科，
        照样应该拿到一组能建立掌握度基线的题；只有**题库里确实没题**时，
        才回落到 insufficient——并且要和「还没作答」区分开，说清是没题。

        ``allowed_subjects`` 透传给 ``_starter_rows``：``subject`` 为空时，
        入门练习也只能从学生已加入的学科里抽题。
        """
        student_id = getattr(student, "id", None)
        records = CollaborativeFilteringService.records()
        per_student, _ = CollaborativeFilteringService.build_matrix(records)
        student_row = per_student.get(student_id) or {}
        # 已经做对的题不再推荐——判对口径复用 CF 矩阵，不另写一套
        correct_ids = {qid for qid, value in student_row.items() if value == 1.0}

        payload["mode"] = "starter"
        payload["summary"]["mode"] = "starter"
        payload["summary"]["starter"] = True

        label = subject or "全部学科"
        rows = RecommendationService._starter_rows(
            student_id, subject,
            correct_ids=correct_ids, answered_ids=set(student_row), date_str=date_str,
            allowed_subjects=allowed_subjects,
        )
        if not rows:
            payload["insufficient"] = True
            payload["message"] = (
                f"「{label}」暂时没有启用中的题目，请换一个学科或联系教师补充题库。"
            )
            return payload

        page = rows[offset: offset + size]
        items = []
        for index, row in enumerate(page, start=1):
            item = RecommendationService._item_payload(row)
            item["order"] = index
            items.append(item)

        matched = [row["match_score"] for row in page]
        # 覆盖的知识点数：页面 KPI 直接显示它，避免前端再从 items 猜一遍
        covered = {row["knowledge_point_id"] for row in page
                   if row["knowledge_point_id"] is not None}
        payload["items"] = items
        payload["has_more"] = offset + size < len(rows)
        payload["summary"].update({
            "candidate_count": len(rows),
            "returned": len(items),
            "avg_match": _round(sum(matched) / len(matched)) if matched else None,
            "cf_available": False,
            "starter_knowledge_points": len(covered),
        })
        if not items:
            payload["message"] = "没有更多可推荐的题目了，换一批或换个学科试试。"
        else:
            payload["message"] = (
                f"你在「{label}」还没有答题记录，"
                f"先来一组入门练习（覆盖 {len(covered)} 个知识点）"
            )
        return payload

    # ------------------------------------------------------------------
    @staticmethod
    def daily_practice(student, subject: str = "", *, size: int = DAILY_DEFAULT_SIZE,
                       offset: int = 0, today=None) -> dict:
        """生成「每日练习」推荐清单。

        整个流程是**确定性**的：同样的学生、同样的数据、同一天，
        永远得到同一份清单（顺序也一致）。所以不需要建表缓存，
        刷新页面不会换题，学生可以安心一道一道做完。

        ``size`` 传 ``None`` 用默认条数；传 0 或负数会被夹到 1（而不是悄悄换成默认值——
        「夹紧」比「改语义」更容易预测）。
        """
        if size is None:
            size = DAILY_DEFAULT_SIZE
        else:
            try:
                size = int(size)
            except (TypeError, ValueError):
                size = DAILY_DEFAULT_SIZE
        size = max(1, min(size, DAILY_MAX_SIZE))
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0

        current_date = today or timezone.localdate()
        date_str = current_date.isoformat() if hasattr(current_date, "isoformat") else str(current_date)

        # 可选范围先算一次：payload 要用，后面的范围校验与候选收口也要用
        allowed = RecommendationService.selectable_subjects(student)
        subject = (subject or "").strip()

        mastery = KnowledgeMasteryService.student_mastery(student, subject)
        summary = mastery["summary"]
        accuracy = summary["accuracy"]
        target_difficulty = ContentBasedService.target_difficulty(accuracy)

        payload = {
            "student": {
                "id": getattr(student, "id", None),
                "name": getattr(student, "display_name", "") or getattr(student, "username", "") or "",
                "student_no": getattr(student, "student_no", "") or "",
                "username": getattr(student, "username", "") or "",
            },
            "subject": subject or "",
            "scope_key": mastery["scope_key"],
            "scope_display": mastery["scope_display"],
            "subjects": RecommendationService.subjects(student),
            # 下拉真正该列的范围：只含学生**已加入**的学科；subjects 语义保持不变
            "selectable_subjects": allowed,
            "date": date_str,
            "size": size,
            "offset": offset,
            "target_difficulty": target_difficulty,
            # "normal" = 按薄弱点成集；"starter" = 冷启动的入门练习
            "mode": "normal",
            "weak_points": [
                {
                    "knowledge_point_id": row["knowledge_point_id"],
                    "name": row["name"],
                    "full_path": row["full_path"],
                    "accuracy": row["accuracy"],
                    "answered": row["answered"],
                    "mastery_level": row["mastery_level"],
                    "mastery_level_display": row["mastery_level_display"],
                }
                for row in (mastery["weak_points"] or [])[:12]
            ],
            "thresholds": {
                "weakness_ratio": WEAKNESS_RATIO,
                "min_samples": MIN_SAMPLES_FOR_WEAKNESS,
                "cf_min_common_students": CF_MIN_COMMON_STUDENTS,
                "max_per_knowledge_point": MAX_PER_KNOWLEDGE_POINT,
            },
            "items": [],
            "summary": {
                "candidate_count": 0,
                "returned": 0,
                "weak_point_count": len(mastery["weak_points"] or []),
                "avg_match": None,
                "cf_available": False,
                "target_difficulty": target_difficulty,
                "answered_count": summary["answered"],
                "mode": "normal",
                "starter": False,
                # 入门练习覆盖的知识点数；normal 恒为 0，字段常驻以便前端稳定取值
                "starter_knowledge_points": 0,
            },
            "has_more": False,
            "insufficient": False,
            "message": "",
        }

        # ---- 范围门槛：没加入学科班，就没有可练的学科 ----
        if not allowed:
            payload["insufficient"] = True
            payload["message"] = "你还没有加入任何学科班，请联系任课教师安排后再来练习。"
            return payload

        # ---- 深链越界：URL 上带了没加入的学科，不返回任何题目 ----
        if subject and subject not in allowed:
            payload["insufficient"] = True
            payload["message"] = (
                f"「{subject}」不在你加入的学科范围内，请从已加入的学科中选择。"
            )
            return payload

        # ---- 前置门槛：没有薄弱点就没有推荐 ----
        weak_map = ContentBasedService.weak_map(mastery)
        if not weak_map:
            # 冷启动兜底：该学科一条已判分作答都没有，但题库里有启用题，
            # 就先给一组入门练习——否则新学生 / 新学科永远拿不到题。
            if summary["answered"] == 0:
                return RecommendationService._starter_practice(
                    student, subject, payload,
                    date_str=date_str, size=size, offset=offset,
                    allowed_subjects=allowed,
                )
            if summary["insufficient_count"] == summary["knowledge_point_count"]:
                payload["insufficient"] = True
                payload["message"] = (
                    f"每个知识点的作答都不到 "
                    f"{payload['thresholds']['min_samples']} 题，样本太少，"
                    f"先完整做一套试卷再来找薄弱点更准。"
                )
            else:
                payload["message"] = "各知识点都已达到掌握线，暂时没有需要补强的弱项。"
            return payload

        # ---- 建 CF 矩阵（一次读库，全流程共用）----
        records = CollaborativeFilteringService.records()
        per_student, per_question = CollaborativeFilteringService.build_matrix(records)
        student_row = per_student.get(getattr(student, "id", None)) or {}
        # 已经做对的题不再推荐——那是「已掌握」，不是「要练」
        correct_ids = {qid for qid, value in student_row.items() if value == 1.0}

        # ---- 内容推荐 ----
        kp_map = KnowledgeMasteryService._kp_map()
        candidates = ContentBasedService.score_candidates(
            subject, weak_map, kp_map, exclude_ids=correct_ids, accuracy=accuracy,
            # 全科推荐同样不能越过「已加入的学科」——薄弱知识点是按全部作答算的
            subjects=allowed,
        )
        if not candidates:
            payload["message"] = (
                "薄弱知识点上暂时没有可推荐的新题（题都已练过，或该知识点下没有启用中的题目）。"
            )
            return payload

        # ---- 协同过滤加分 ----
        candidate_ids = [row["question_id"] for row in candidates]
        cf_scores = CollaborativeFilteringService.score(
            getattr(student, "id", None), candidate_ids, per_student, per_question,
        )

        for row in candidates:
            cf_score = cf_scores.get(row["question_id"], 0.0)
            row["collaborative_score"] = _round(cf_score)
            row["match_score"] = _round(min(1.0, row["content_score"] * (1 + CF_BONUS * cf_score)))
            row["match_percent"] = round(row["match_score"] * 100, 1)
            row["answered_before"] = row["question_id"] in student_row
            row["reason"] = RecommendationService._reason(row, row["answered_before"])
            row["source"] = "local"

        # ---- 排序与分页 ----
        student_id = getattr(student, "id", None)
        candidates.sort(key=lambda row: (
            -row["match_score"],
            _daily_key(student_id, date_str, row["question_id"]),
        ))
        # 同知识点限流：先按「每个知识点最多 MAX_PER_KNOWLEDGE_POINT 道」成集，
        # 名额用满的题排到后面（「换一批」仍能取到，只是不占前排）。
        primary, overflow, used = [], [], {}
        for row in candidates:
            kp_id = row["knowledge_point_id"]
            if used.get(kp_id, 0) < MAX_PER_KNOWLEDGE_POINT:
                used[kp_id] = used.get(kp_id, 0) + 1
                primary.append(row)
            else:
                overflow.append(row)
        sequence = primary + overflow

        total = len(sequence)
        page = sequence[offset: offset + size]

        items = []
        for index, row in enumerate(page, start=1):
            item = RecommendationService._item_payload(row)
            item["order"] = index
            items.append(item)

        matched = [row["match_score"] for row in page]
        payload["items"] = items
        payload["has_more"] = offset + size < total
        payload["summary"].update({
            "candidate_count": total,
            "returned": len(items),
            "avg_match": _round(sum(matched) / len(matched)) if matched else None,
            "cf_available": bool(cf_scores),
        })
        if not items:
            payload["message"] = "没有更多可推荐的题目了，换一批或换个学科试试。"
        elif not cf_scores:
            payload["message"] = (
                "当前样本还不足以做协同过滤，本次推荐只依据你的知识点掌握度。"
            )
        return payload

    # ------------------------------------------------------------------
    @staticmethod
    def explain(student, items, subject: str = "", *, provider: str = None,
                user=None, mode: str = "normal") -> dict:
        """让大模型改写推荐理由（复用 ``recommend_practice`` 场景）。

        **选题是代码干的，模型只负责措辞。** 提示词里喂的是已经排好序的候选题目池
        与已经算好的薄弱知识点，不让模型自己挑题、也不让它自己算正确率。

        ``mode`` 决定「按什么口径措辞」：``normal`` 讲薄弱知识点，
        ``starter`` 讲建立掌握度基线——冷启动的学生没有数据，不能说他有薄弱点。

        降级纪律与模块六同源：模型不可用 / 返回非 JSON / 字段缺失，
        一律保留本地理由并标 ``degraded=True``——页面照常可用，只是话朴素一点。
        """
        mode = "starter" if mode == "starter" else "normal"
        items = items or []
        if not items:
            # 返回结构必须与下面的完整分支**同构**：调用方（DailyPracticeView）按固定
            # 字段读取 ``explained["model"]``，少一个键就是一次 500。
            return {"reason": "", "items": [], "source": "local", "degraded": False,
                    "degraded_reason": "", "cached": False, "provider": "",
                    "model": "", "is_mock": False}

        student_id = getattr(student, "id", None)
        signature = hashlib.md5(
            ",".join(str(item.get("question_id")) for item in items).encode("utf-8")
        ).hexdigest()
        # 缓存键带上 mode：同一批题在两种口径下的措辞不同，不能互相命中
        cache_key = f"recommend:ai_reason:{student_id}:{mode}:{subject or 'all'}:{signature}"
        cached = cache.get(cache_key)
        if cached:
            return {**cached, "cached": True}

        student_name = (getattr(student, "display_name", "")
                        or getattr(student, "username", "") or "该学生")
        if mode == "starter":
            # 冷启动不喂「薄弱知识点」——喂了模型就会照着说
            weak_text = AI_STARTER_WEAK_PLACEHOLDER
        else:
            mastery = KnowledgeMasteryService.student_mastery(student, subject)
            weak_text = "\n".join(
                f"{row['full_path']} | 正确率 {round(_to_float(row['accuracy']) * 100, 1)}%"
                for row in (mastery["weak_points"] or [])[:10]
            ) or "（暂无薄弱知识点）"

        pool = items[:AI_MAX_CANDIDATES]
        candidate_pool = "\n".join(
            f"{item['question_id']} | {item['knowledge_point']} | "
            f"{item['difficulty']} | {str(item['stem'])[:40]}"
            for item in pool
        )

        degraded_reason = ""
        provider_name, model_name, is_mock = "local", "", False
        overall_reason = ""
        ai_items = [dict(item) for item in items]
        try:
            client = get_ai_client(provider)
            result = client.run_scene(
                "recommend_practice",
                user=user,
                mode=mode,
                mode_label=AI_PRACTICE_MODE_LABELS[mode],
                mode_rules=AI_PRACTICE_MODE_RULES[mode],
                student_name=student_name,
                weak_points=weak_text,
                count=len(pool),
                candidate_pool=candidate_pool,
            )
            parsed = result.json(strict=False)
            if not parsed:
                raise AIServiceError("模型未返回可解析的 JSON")
            overall_reason = str(parsed.get("reason") or "").strip()
            ai_map = {}
            for entry in parsed.get("recommendations") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    qid = int(entry.get("question_id"))
                except (TypeError, ValueError):
                    continue
                ai_map[qid] = str(entry.get("reason") or "").strip()
            for item in ai_items:
                ai_reason = ai_map.get(item.get("question_id"))
                if ai_reason:
                    item["reason"] = ai_reason
                    item["source"] = "ai"
            provider_name = client.name
            model_name = client.provider.model
            is_mock = bool(client.is_mock)
        except AIServiceError as exc:
            degraded_reason = f"AI 调用失败：{exc}"
            logger.warning("推荐理由降级（student=%s）：%s", student_id, exc)
        except Exception as exc:  # noqa: BLE001 —— 任何意外都不该让「每日练习」打不开
            degraded_reason = f"AI 调用异常：{exc}"
            logger.exception("推荐理由异常降级（student=%s）", student_id)

        payload = {
            "reason": overall_reason or (
                "这是入门练习：先做几道难度适中的题，帮我们建立你的掌握度基线。"
                if mode == "starter"
                else "根据你的薄弱知识点与历史错题，为你挑选了下面这些练习。"
            ),
            "items": ai_items,
            "cached": False,
            "source": "local" if degraded_reason else "ai",
            "degraded": bool(degraded_reason),
            "degraded_reason": degraded_reason,
            "provider": provider_name,
            "model": model_name,
            "is_mock": is_mock,
        }
        if not degraded_reason:
            cache.set(cache_key, payload, AI_REASON_TTL_HOURS * 3600)
        return payload

    # ------------------------------------------------------------------
    @staticmethod
    def selectable_subjects(student) -> list:
        """页面学科下拉的可选范围：只包含学生**已加入**的学科（学科班的学科）。

        口径变更：早先还并入「题库里有启用题的学科」（让冷启动学生能练没做过的学科），
        现在产品要求收窄——没加入的学科不允许选择。

        「已加入」= 学生所在**学科班**（``Classroom.class_type == SUBJECT``）的
        ``subject``。学生的 ``User.subject`` 是空的，不能拿它当依据。

        排序：先按该学生**在该学科的作答量**降序（熟悉的在前），
        无作答的再按名称。去重、``strip()``、去掉空字符串。
        """
        # 惰性导入：班级模块不应成为推荐模块的导入期依赖（与 apps.analytics 的处理一致）
        from apps.classroom.constants import ClassType

        joined_rows = (
            student.classrooms
            .filter(class_type=ClassType.SUBJECT, is_active=True)
            .exclude(subject="")
            .values_list("subject", flat=True)
            .distinct()
        )
        answered = {}
        for subject in joined_rows:
            subject = (subject or "").strip()
            if subject and subject not in answered:
                answered[subject] = 0
        if not answered:
            return []

        answered_rows = (
            Answer.objects
            .filter(
                attempt__student=student,
                attempt__status__in=ANALYZABLE_STATUSES,
                paper_question__question__isnull=False,
                attempt__exam__subject__in=list(answered),
            )
            .values("attempt__exam__subject")
            .annotate(answered=Count("id"))
        )
        for row in answered_rows:
            subject = (row["attempt__exam__subject"] or "").strip()
            if subject in answered:
                answered[subject] = row["answered"]

        # 作答量降序；作答量相同时按名称，保证结果稳定可复现
        return [subject for subject, _ in sorted(answered.items(), key=lambda kv: (-kv[1], kv[0]))]

    # ------------------------------------------------------------------
    @staticmethod
    def subjects(student) -> list:
        """这个学生做过题的学科列表（页面筛选下拉）。"""
        from apps.exam.models import ExamAttempt

        rows = (
            ExamAttempt.objects.filter(student=student, status__in=ANALYZABLE_STATUSES)
            .exclude(exam__subject="")
            .values_list("exam__subject", flat=True)
        )
        return sorted({subject for subject in rows if subject})


__all__ = [
    "CollaborativeFilteringService",
    "ContentBasedService",
    "RecommendationService",
]
