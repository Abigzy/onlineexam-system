"""判分与成绩服务层。

四个服务，职责边界划得很清：

    ObjectiveGradingService    客观题精确匹配（单选 / 多选 / 判断 / 填空）——纯算法、可复现
    SubjectiveGradingService   AI 主观题批改（复用 apps.ai 的提示词与阈值折算）
    GradingService             编排 + 汇总 + 教师复核 + 出分（**唯一汇总写入口**）
    ScoreReportService         成绩单 / 错题本 / 班级成绩单 / AI 校准统计

三条贯穿全模块的纪律：

1. **汇总只有一个写入口。** ``GradingService.recalc()`` 是 ``objective_score`` /
   ``subjective_score`` / ``score`` 三个字段唯一的写入点，而且每次都是**从 Answer
   重新累加**（不是 ``score += x``）——这样判分天然幂等，重跑一遍不会把分数加上去
   第二次。模块三的 ``PaperService.recalc()`` 是同一个思路。

2. **客观题判分是纯函数。** 同样的答案 + 同样的策略，永远得到同样的分数，
   不依赖大模型、不依赖时间。AI 只参与主观题。

3. **大模型输出仍是不可信输入。** 主观题批改失败（超时、返回非 JSON、比例越界）
   绝不把异常抛给教师，而是**退化为「待人工判分」**：留 0 分 + 一条说明评语，
   并把尝试失败的事实记进返回结构。教师看到的永远是「能干活的工作台」，
   而不是一个报错的页面。
"""

import logging
import re
import unicodedata
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.ai.client import get_ai_client
from apps.ai.exceptions import AIServiceError
from apps.ai.schemas import parse_grading_result, score_by_threshold
from apps.common.constants import AttemptStatus, QuestionType
from apps.common.exceptions import BusinessError
from apps.exam.constants import DEFAULT_MULTIPLE_CHOICE_POLICY
from apps.exam.models import Answer, ExamAttempt
from apps.papers.models import PaperQuestion
from apps.questions.constants import CHOICE_TYPES

from .constants import (
    AI_GRADE_MAX_QUESTIONS,
    WRONG_BOOK_SUBJECTIVE_RATIO,
    GradeSource,
    MultipleChoicePolicy,
    ReviewAction,
)
from .models import GradingFeedback

logger = logging.getLogger("apps.grading")

ZERO = Decimal("0.00")

# 题型分类：客观题走精确匹配，主观题走 AI / 人工
OBJECTIVE_TYPES = tuple(QuestionType.objective_types())
SUBJECTIVE_TYPES = tuple(QuestionType.subjective_types())


# ======================================================================
# 通用小工具
# ======================================================================
def _to_decimal(value, default=ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return default


def _round2(value) -> Decimal:
    """统一保留两位小数（分数一律两位，避免 2.6666667 这种值流到前端）。"""
    return _to_decimal(value).quantize(Decimal("0.01"))


def _fmt_num(value) -> str:
    """给提示语用：12.00 → 12，12.50 → 12.5。"""
    number = float(_to_decimal(value))
    return str(int(number)) if number == int(number) else f"{number:g}"


def _fmt_dt(value) -> str:
    if not value:
        return ""
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def normalize_labels(raw) -> str:
    """选项字母标准化：大写、去重、排序。

    与模块四 ``apps.exam.services._normalize_labels()`` 的口径完全一致——
    这里重新实现一份而不是去 import 私有函数：判分正确性不能依赖另一个模块的
    私有实现，两处都保留自己的测试更稳。
    """
    if not raw:
        return ""
    if isinstance(raw, (list, tuple, set)):
        chars = [str(item) for item in raw]
    else:
        chars = list(str(raw))
    # 只留字母数字：学生端偶发的 "A,B" / "A B" 这类写法不该被当成多选了一个标点
    cleaned = {ch.strip().upper() for ch in chars if ch and ch.strip() and ch.strip().isalnum()}
    return "".join(sorted(cleaned))


_PUNCTUATION = "。.，,；;、:：'\"“”‘’()（）[]【】{}<>《》！!？?~～"


def normalize_text(value) -> str:
    """填空 / 文本归一化。

    做四件事，每一件都对应一类真实丢分事故：
      1. **全角转半角**（``NFKC``）——学生用中文输入法敲的 ``１２３``／``（`` 和半角
         的 ``123``／``(`` 是同一个答案，不该判错；
      2. **去掉所有空白**——「栈 和 队列」与「栈和队列」应当等价；
      3. **去掉首尾标点**——学生习惯在句末加句号；
      4. **英文小写**——``Stack`` 和 ``stack`` 不该有差别。
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", "", text)
    return text.strip(_PUNCTUATION).lower()


def _split_lines(text) -> str:
    """按行拆分，并丢掉**末尾**的空行。

    只丢末尾：中间的空行是有意义的（表示「这个空没填」），
    丢了会让后面的空全部错位，那是比漏判更糟的事故。
    """
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _paper_has_subjective(attempt: ExamAttempt) -> bool:
    """这份卷子里有没有主观题——决定判分后是「自动完成」还是「等人工复核」。"""
    return attempt.exam.paper.paper_questions.filter(
        question_type__in=SUBJECTIVE_TYPES
    ).exists()


def _unjudgeable(reason: str, full) -> dict:
    """题目本身没法自动判分（如选择题没配正确答案）。

    返回 ``judgeable=False`` 而不是 0 分：0 分意味着「学生答错了」，
    而「题目缺答案」是教师侧的数据问题，两者绝不能混为一谈。
    """
    return {
        "judgeable": False,
        "is_correct": None,
        "score": ZERO,
        "full_score": _to_decimal(full),
        "reason": reason,
    }


# ======================================================================
# 客观题判分
# ======================================================================
class ObjectiveGradingService:
    """客观题精确匹配判分。

    四种题型三种算法：
      * 单选 / 判断 —— 选项字母集合相等即满分；
      * 多选       —— 按考试配置的策略给分（严格 / 漏选部分分 / 倒扣）；
      * 填空       —— 按行对齐逐空比对，允许多个可接受答案（``|`` 分隔），按空计分。
    """

    # ------------------------------------------------------------------
    @staticmethod
    def grade(answer: Answer, *, policy: str = DEFAULT_MULTIPLE_CHOICE_POLICY) -> dict:
        """判一道客观题。**纯函数**——不写库，返回值里带 ``is_correct`` 与 ``score``。"""
        paper_question = answer.paper_question
        question = paper_question.question
        question_type = paper_question.question_type
        full = _to_decimal(paper_question.score)

        if question_type == QuestionType.FILL:
            return ObjectiveGradingService._grade_fill(answer, question, full)

        correct = set(question.correct_labels())
        chosen = set(normalize_labels(answer.selected_labels))

        if question_type == QuestionType.MULTIPLE:
            return ObjectiveGradingService._grade_multiple(chosen, correct, full, policy)
        return ObjectiveGradingService._grade_single(chosen, correct, full)

    # ------------------------------------------------------------------
    @staticmethod
    def _grade_single(chosen: set, correct: set, full) -> dict:
        if not correct:
            return _unjudgeable("题目未配置正确答案，需人工处理", full)
        if not chosen:
            return {"judgeable": True, "is_correct": False, "score": ZERO,
                    "full_score": _to_decimal(full), "reason": "未作答"}
        if chosen == correct:
            return {"judgeable": True, "is_correct": True, "score": _round2(full),
                    "full_score": _to_decimal(full), "reason": "作答正确"}
        return {"judgeable": True, "is_correct": False, "score": ZERO,
                "full_score": _to_decimal(full), "reason": "作答错误"}

    # ------------------------------------------------------------------
    @staticmethod
    def _grade_multiple(chosen: set, correct: set, full, policy: str) -> dict:
        """多选题给分。

        三种情形的名字先说清楚，后面代码才好读：
          * **全对**：选了且只选了所有正确项；
          * **漏选**：选的都是对的，但少选了几个（``hit > 0`` 且 ``wrong == 0``）；
          * **错选**：选了至少一个错误项（``wrong`` 非空）。
        """
        full = _to_decimal(full)
        if not correct:
            return _unjudgeable("题目未配置正确答案，需人工处理", full)
        if not chosen:
            return {"judgeable": True, "is_correct": False, "score": ZERO,
                    "full_score": full, "reason": "未作答"}

        hit = chosen & correct
        wrong = chosen - correct

        if hit == correct and not wrong:
            return {"judgeable": True, "is_correct": True, "score": _round2(full),
                    "full_score": full, "reason": "全部选对"}

        if policy == MultipleChoicePolicy.PARTIAL:
            if wrong:
                return {"judgeable": True, "is_correct": False, "score": ZERO, "full_score": full,
                        "reason": "存在错选，本策略下不得分"}
            score = full * len(hit) / len(correct)
            return {"judgeable": True, "is_correct": False, "score": _round2(score),
                    "full_score": full,
                    "reason": f"漏选，按比例得分（{len(hit)}/{len(correct)}）"}

        if policy == MultipleChoicePolicy.PENALTY:
            # 错选倒扣：正确项数减去错误项数，最低 0 分（不倒欠）
            net = len(hit) - len(wrong)
            score = max(ZERO, full * Decimal(net) / Decimal(len(correct)))
            reason = "错选倒扣后为 0 分" if net <= 0 else f"错选倒扣，净得分 {net}/{len(correct)}"
            return {"judgeable": True, "is_correct": False, "score": _round2(score),
                    "full_score": full, "reason": reason}

        # STRICT（默认）：漏选与错选都不得分
        reason = "存在错选，不得分" if wrong else "漏选，本策略下不得分"
        return {"judgeable": True, "is_correct": False, "score": ZERO,
                "full_score": full, "reason": reason}

    # ------------------------------------------------------------------
    @staticmethod
    def _grade_fill(answer: Answer, question, full) -> dict:
        """填空题按空给分。

        ``Question.reference_answer`` 的约定是「一行一个空」（模块二定下的），
        这里严格跟着这个约定走：第 N 行参考答案 ↔ 学生作答的第 N 行。
        一行里用 ``|`` 分隔的多个写法视为等价（如 ``栈|栈式结构``）。
        """
        full = _to_decimal(full)
        reference_lines = _split_lines(question.reference_answer)
        if not reference_lines:
            return _unjudgeable("填空题未提供参考答案，需人工处理", full)

        student_lines = _split_lines(answer.answer_text)
        blanks = len(reference_lines)
        per_blank = full / Decimal(blanks)

        hit, detail = 0, []
        for index, reference in enumerate(reference_lines):
            alternatives = {normalize_text(part) for part in reference.split("|")}
            alternatives.discard("")
            got = normalize_text(student_lines[index]) if index < len(student_lines) else ""
            matched = bool(got) and got in alternatives
            if matched:
                hit += 1
            detail.append({
                "blank": index + 1,
                "got": student_lines[index].strip() if index < len(student_lines) else "",
                "matched": matched,
            })

        score = _round2(per_blank * hit)
        return {
            "judgeable": True,
            "is_correct": hit == blanks,
            "score": score,
            "full_score": full,
            "reason": f"答对 {hit}/{blanks} 个空",
            "blanks": detail,
        }

    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def grade_attempt(answers, *, policy: str = DEFAULT_MULTIPLE_CHOICE_POLICY,
                      persist: bool = True) -> dict:
        """批量判一份作答里的全部客观题。"""
        graded, skipped, correct, unanswered = 0, 0, 0, 0
        total = ZERO
        reasons = []

        for answer in answers:
            if answer.paper_question.question_type in SUBJECTIVE_TYPES:
                continue
            result = ObjectiveGradingService.grade(answer, policy=policy)
            if not result["judgeable"]:
                skipped += 1
                reasons.append({
                    "answer_id": answer.id,
                    "paper_question_id": answer.paper_question_id,
                    "reason": result["reason"],
                })
                continue

            if persist:
                answer.is_correct = result["is_correct"]
                answer.score = result["score"]
                answer.save(update_fields=["is_correct", "score", "updated_at"])

            graded += 1
            total += result["score"]
            if result["is_correct"]:
                correct += 1
            if not answer.is_answered:
                unanswered += 1

        return {
            "graded": graded,
            "skipped": skipped,
            "correct": correct,
            "unanswered": unanswered,
            "score": float(_round2(total)),
            "policy": policy,
            "warnings": reasons,
        }


# ======================================================================
# 主观题 AI 批改
# ======================================================================
class SubjectiveGradingService:
    """主观题（简答 / 论述）AI 批改。

    复用模块二就备好的两样东西：``prompts/grade_subjective.txt`` 提示词与
    ``schemas.score_by_threshold()`` 阈值折算（0.7 满分 / 0.4~0.7 部分分 / <0.4 不得分）。
    本模块不新写任何提示词——批改口径只有一处，改也只需要改那一处。
    """

    # ------------------------------------------------------------------
    @staticmethod
    def grade(answer: Answer, *, user=None, provider: str = None) -> dict:
        """批改一道主观题（不写库）。"""
        paper_question = answer.paper_question
        question = paper_question.question
        full = _to_decimal(paper_question.score)
        student_answer = (answer.answer_text or "").strip()

        # 空答案不走大模型：白花一次调用，结论还只能是 0 分
        if not student_answer:
            return {
                "gradeable": True,
                "score": ZERO,
                "ratio": 0.0,
                "level": "poor",
                "comment": "未作答，不得分。",
                "hit_points": [],
                "missing_points": [],
                "suggestion": "",
                "is_mock": False,
                "full_score": full,
                "error": "",
            }

        client = get_ai_client(provider)
        try:
            result = client.run_scene(
                "grade_subjective",
                user=user,
                stem=question.stem,
                full_score=float(full),
                reference_answer=question.reference_answer or "（未提供参考答案）",
                student_answer=student_answer,
            )
        except AIServiceError as exc:
            return SubjectiveGradingService._degrade(full, f"AI 调用失败：{exc}")

        try:
            grading = parse_grading_result(result.json(strict=True))
        except (AIServiceError, Exception) as exc:  # noqa: BLE001 —— 任何解析异常都不该炸页面
            logger.warning("主观题评分结果解析失败（answer=%s）：%s", answer.id, exc)
            return SubjectiveGradingService._degrade(full, f"AI 返回内容无法解析：{exc}")

        score = _round2(score_by_threshold(grading.score_ratio, full_score=float(full)))
        return {
            "gradeable": True,
            "score": score,
            "ratio": grading.score_ratio,
            "level": grading.level,
            "comment": SubjectiveGradingService._compose_comment(grading),
            "hit_points": grading.hit_points,
            "missing_points": grading.missing_points,
            "suggestion": grading.suggestion,
            "is_mock": client.is_mock,
            "full_score": full,
            "error": "",
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _degrade(full, reason: str) -> dict:
        """AI 不可用时的降级结果：**给 0 分但明确标注「待人工判分」**。

        不抛异常是刻意的：教师点「批量 AI 批改」，不该因为一次网络抖动就整批失败，
        更不该让前面已经批好的部分白干。
        """
        return {
            "gradeable": False,
            "score": ZERO,
            "ratio": None,
            "level": "",
            "comment": f"【待人工判分】{reason}",
            "hit_points": [],
            "missing_points": [],
            "suggestion": "",
            "is_mock": False,
            "full_score": _to_decimal(full),
            "error": reason,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _compose_comment(grading) -> str:
        """把结构化评分结果拼成教师一眼能读完的评语。"""
        parts = []
        if grading.reason:
            parts.append(grading.reason)
        if grading.hit_points:
            parts.append("命中要点：" + "；".join(grading.hit_points))
        if grading.missing_points:
            parts.append("遗漏要点：" + "；".join(grading.missing_points))
        if grading.suggestion:
            parts.append("改进建议：" + grading.suggestion)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def grade_attempt(answers, *, user=None, provider: str = None,
                      max_questions: int = AI_GRADE_MAX_QUESTIONS,
                      only_missing: bool = True, persist: bool = True) -> dict:
        """批量批改一份作答里的主观题。

        ``only_missing=True``（默认）只批改**还没有 AI 建议分**的题——
        教师手滑点两次按钮，不该把大模型账单也翻一倍。
        """
        targets = [
            answer for answer in answers
            if answer.paper_question.question_type in SUBJECTIVE_TYPES
            and (not only_missing or answer.ai_score is None)
        ]
        overflow = max(0, len(targets) - max_questions)
        targets = targets[:max_questions]

        graded, failed, is_mock = 0, 0, False
        ratio, total = [], ZERO
        details = []

        for answer in targets:
            result = SubjectiveGradingService.grade(answer, user=user, provider=provider)
            is_mock = is_mock or result["is_mock"]
            if result["gradeable"]:
                graded += 1
            else:
                failed += 1
            if result["ratio"] is not None:
                ratio.append(result["ratio"])
            total += result["score"]

            if persist:
                answer.ai_score = result["score"]
                answer.ai_comment = result["comment"]
                # AI 只给「建议分」。若教师还没复核过（score 仍为 0），把建议分先当默认分，
                # 让成绩单在没有人工干预时也不至于全 0；教师复核会覆盖它并留痕。
                if not answer.feedbacks.exists():
                    answer.score = result["score"]
                answer.save(update_fields=["ai_score", "ai_comment", "score", "updated_at"])

            details.append({
                "answer_id": answer.id,
                "paper_question_id": answer.paper_question_id,
                "score": float(result["score"]),
                "ratio": result["ratio"],
                "gradeable": result["gradeable"],
                "error": result["error"],
            })

        return {
            "graded": graded,
            "failed": failed,
            "overflow": overflow,
            "is_mock": is_mock,
            "score": float(_round2(total)),
            "avg_ratio": round(sum(ratio) / len(ratio), 4) if ratio else None,
            "details": details,
        }


# ======================================================================
# 判分编排 + 复核 + 出分
# ======================================================================
class GradingService:
    """判分主流程。"""

    # ------------------------------------------------------------------
    # 汇总（唯一写入口）
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def recalc(attempt: ExamAttempt) -> dict:
        """从 ``Answer`` 重新累加出三个汇总分。

        **这是 ``objective_score`` / ``subjective_score`` / ``score`` 唯一的写入口。**
        每次都重新求和而不是增量累加，所以判分、复核、批量重判怎么重复跑都不会把分数
        越加越多——幂等性靠「重算」而不是靠「别重复调」来保证。
        """
        objective, subjective = ZERO, ZERO
        for row in attempt.answers.select_related("paper_question").all():
            score = _to_decimal(row.score)
            if row.paper_question.question_type in SUBJECTIVE_TYPES:
                subjective += score
            else:
                objective += score

        attempt.objective_score = _round2(objective)
        attempt.subjective_score = _round2(subjective)
        attempt.score = _round2(objective + subjective)
        attempt.save(update_fields=["objective_score", "subjective_score", "score", "updated_at"])
        return {
            "objective_score": float(attempt.objective_score),
            "subjective_score": float(attempt.subjective_score),
            "score": float(attempt.score),
            "total_score": attempt.total_score_float(),
        }

    # ------------------------------------------------------------------
    # 状态推进
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def sync_status(attempt: ExamAttempt) -> str:
        """按判分进展推进作答状态。

        规则（**已出分 ``GRADED`` 是终态，绝不回退**）：

            有主观题且还没人工复核完 → 待人工复核 ``REVIEWING``
            没有主观题（全客观题）    → 自动判分完成 ``AUTO_GRADED``
        """
        if attempt.status == AttemptStatus.GRADED:
            return attempt.status
        if attempt.status not in (AttemptStatus.SUBMITTED, AttemptStatus.TIMEOUT,
                                  AttemptStatus.AUTO_GRADED, AttemptStatus.REVIEWING):
            return attempt.status

        target = (AttemptStatus.REVIEWING if _paper_has_subjective(attempt)
                  else AttemptStatus.AUTO_GRADED)

        fields = []
        if attempt.status != target:
            attempt.status = target
            fields.append("status")
        if not attempt.graded_at:
            attempt.graded_at = timezone.now()
            fields.append("graded_at")
        if fields:
            attempt.save(update_fields=fields + ["updated_at"])
        return attempt.status

    # ------------------------------------------------------------------
    # 判分主流程
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def grade_attempt(attempt: ExamAttempt, *, user=None, with_ai: bool = False,
                      policy: str = None, provider: str = None,
                      force: bool = False) -> dict:
        """判一份作答。

        ``with_ai=False``（默认）只判客观题——快、免费、可重复；
        主观题要花钱调大模型，必须由教师显式点「AI 批改」触发。
        """
        if attempt.status == AttemptStatus.IN_PROGRESS:
            raise BusinessError("本次作答还在进行中，交卷后才能判分")
        if attempt.status == AttemptStatus.GRADED and not force:
            raise BusinessError("本次作答已出分。如需重新判分，请显式指定 force")

        policy = policy or attempt.exam.multiple_choice_policy or DEFAULT_MULTIPLE_CHOICE_POLICY

        answers = list(
            attempt.answers.select_related("paper_question__question").all()
        )
        objective = ObjectiveGradingService.grade_attempt(answers, policy=policy)
        subjective = None
        if with_ai:
            subjective = SubjectiveGradingService.grade_attempt(
                answers, user=user, provider=provider,
            )

        summary = GradingService.recalc(attempt)
        status = GradingService.sync_status(attempt)

        logger.info(
            "判分完成：作答 #%s 客观 %.2f / 主观 %.2f / 合计 %.2f（策略 %s，AI %s）",
            attempt.id, summary["objective_score"], summary["subjective_score"],
            summary["score"], policy, bool(with_ai),
        )
        return {
            "attempt_id": attempt.id,
            "status": status,
            "status_display": attempt.get_status_display(),
            "policy": policy,
            "objective": objective,
            "subjective": subjective,
            "summary": summary,
            "graded_at": _fmt_dt(attempt.graded_at),
        }

    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def ensure_graded(attempt: ExamAttempt, *, user=None) -> bool:
        """惰性判分：已交卷但客观题还没判过的，补一次。返回是否真的判了。

        刻意**只判客观题**：主观题要调大模型，绝不能在一次 GET 里顺手触发
        （费钱、慢，而且不是每个 GET 都来自「想看成绩」的意图）。
        """
        if attempt.status in (AttemptStatus.AUTO_GRADED, AttemptStatus.REVIEWING,
                              AttemptStatus.GRADED):
            return False
        if attempt.status not in (AttemptStatus.SUBMITTED, AttemptStatus.TIMEOUT):
            return False
        GradingService.grade_attempt(attempt, user=user, with_ai=False)
        return True

    # ------------------------------------------------------------------
    # 教师复核
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def review_answer(answer: Answer, *, reviewer, score=None, comment: str = "",
                      accept_ai: bool = False, action: str = None) -> GradingFeedback:
        """教师复核一道题：改分 + 评语，并留下一条 ``GradingFeedback``。

        两种用法：
          * ``accept_ai=True`` —— 一键采纳 AI 建议分（``score`` 会被忽略）；
          * 显式给 ``score``   —— 教师自己定分，此时按「原来有没有 AI 建议」自动判定
            是「调整」还是「自评」，用于校准统计。

        分数会被夹在 ``[0, 本题满分]`` 之间——超过满分的分数一旦写进库，
        成绩单、及格率、班级排名会一起错，而且很难查出来。
        """
        attempt = answer.attempt
        if attempt.status == AttemptStatus.IN_PROGRESS:
            raise BusinessError("本次作答还在进行中，不能批改")

        paper_question = answer.paper_question
        question_type = paper_question.question_type
        full = _to_decimal(paper_question.score)
        ai_score = answer.ai_score
        original = _to_decimal(answer.score)

        if accept_ai:
            if ai_score is None:
                raise BusinessError("这道题还没有 AI 建议分，无法采纳")
            final = _to_decimal(ai_score)
            action = ReviewAction.ACCEPT
        else:
            if score is None or score == "":
                raise BusinessError("请填写复核分数，或使用「采纳 AI 建议」")
            final = _to_decimal(score)
            if action is None:
                action = (ReviewAction.ADJUST if ai_score is not None
                          else ReviewAction.OVERRIDE)

        if final < 0:
            raise BusinessError("分数不能为负数")
        if final > full:
            raise BusinessError(f"分数不能超过本题满分 {_fmt_num(full)} 分")

        answer.score = final
        if question_type in SUBJECTIVE_TYPES:
            # 主观题没有「对 / 错」的二元结论，用分数表达即可，这里保持 null
            answer.is_correct = None
        else:
            answer.is_correct = bool(full > 0 and final >= full)
        if comment:
            answer.teacher_comment = comment
        answer.save(update_fields=["score", "is_correct", "teacher_comment", "updated_at"])

        if ai_score is not None:
            source = GradeSource.AI
        elif question_type in SUBJECTIVE_TYPES:
            source = GradeSource.NONE
        else:
            source = GradeSource.AUTO

        ai_ratio = None
        if ai_score is not None and full > 0:
            ai_ratio = round(float(ai_score) / float(full), 4)

        feedback = GradingFeedback.objects.create(
            answer=answer,
            attempt=attempt,
            reviewer=reviewer if getattr(reviewer, "is_authenticated", False) else None,
            question_type=question_type,
            source=source,
            action=action,
            ai_score=ai_score,
            ai_ratio=ai_ratio,
            original_score=original,
            final_score=final,
            delta=_round2(final - original),
            full_score=full,
            comment=comment or "",
        )

        GradingService.recalc(attempt)
        logger.info(
            "复核：作答 #%s 题 #%s → %.2f 分（原 %.2f，%s）",
            attempt.id, answer.paper_question_id, final, original, action,
        )
        return feedback

    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def finalize_attempt(attempt: ExamAttempt, *, grader=None) -> ExamAttempt:
        """出分：把状态推到 ``GRADED``，学生从此可以看到成绩。

        出分前做两件保护性的事：

        1. **先把客观题判了**（``ensure_graded``，幂等且不调大模型）——
           否则教师漏点一次「判分」就直接出分，会把「全班 0 分」公布出去；
        2. **重算一次汇总** —— 防止「最后一道题改了分但没人重算」。
        """
        if attempt.status == AttemptStatus.IN_PROGRESS:
            raise BusinessError("本次作答还在进行中，无法出分")

        GradingService.ensure_graded(attempt)
        attempt.refresh_from_db()
        GradingService.recalc(attempt)
        attempt.status = AttemptStatus.GRADED
        attempt.graded_at = timezone.now()
        attempt.save(update_fields=["status", "graded_at", "updated_at"])
        logger.info("出分：作答 #%s → %.2f 分", attempt.id, attempt.score)
        return attempt

    # ------------------------------------------------------------------
    # 进度查询
    # ------------------------------------------------------------------
    @staticmethod
    def pending_answers(attempt: ExamAttempt) -> list:
        """还需要教师处理的题目。

        口径：
          * 主观题 —— 只要还没有复核记录就算待处理（AI 给过分也得教师点头）；
          * 客观题 —— 只有 ``is_correct is None``（没判过）的才算；
            已经判过的（对或错）不需要人工介入。
        """
        reviewed = set(
            GradingFeedback.objects.filter(attempt=attempt).values_list("answer_id", flat=True)
        )
        pending = []
        for row in attempt.answers.select_related("paper_question").all():
            if row.paper_question.question_type in SUBJECTIVE_TYPES:
                if row.id not in reviewed:
                    pending.append(row)
            elif row.is_correct is None:
                pending.append(row)
        return pending

    # ------------------------------------------------------------------
    @staticmethod
    def grading_status(attempt: ExamAttempt) -> dict:
        """一份作答的判分进展（工作台顶部的进度条就用它）。"""
        rows = list(attempt.answers.select_related("paper_question").all())
        objective_rows = [r for r in rows if r.paper_question.question_type not in SUBJECTIVE_TYPES]
        subjective_rows = [r for r in rows if r.paper_question.question_type in SUBJECTIVE_TYPES]

        graded_objective = [r for r in objective_rows if r.is_correct is not None]
        reviewed_ids = set(
            GradingFeedback.objects.filter(attempt=attempt).values_list("answer_id", flat=True)
        )
        reviewed_subjective = [r for r in subjective_rows if r.id in reviewed_ids]
        ai_suggested = [r for r in subjective_rows if r.ai_score is not None]

        pending = GradingService.pending_answers(attempt)
        return {
            "attempt_id": attempt.id,
            "status": attempt.status,
            "status_display": attempt.get_status_display(),
            "graded": attempt.is_graded,
            "has_subjective": bool(
                attempt.exam.paper.paper_questions.filter(
                    question_type__in=SUBJECTIVE_TYPES).exists()
            ),
            "objective_total": len(objective_rows),
            "objective_graded": len(graded_objective),
            "subjective_total": len(subjective_rows),
            "subjective_reviewed": len(reviewed_subjective),
            "ai_suggested": len(ai_suggested),
            "pending": len(pending),
            "pending_answer_ids": [r.id for r in pending],
            "complete": not pending,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def grading_queue(exam, *, only_pending: bool = True) -> list:
        """一场考试的批改队列（教师工作台左侧列表）。"""
        attempts = (
            exam.attempts.exclude(status=AttemptStatus.IN_PROGRESS)
            .select_related("student").order_by("submitted_at", "id")
        )
        rows = []
        for attempt in attempts:
            status = GradingService.grading_status(attempt)
            if only_pending and status["complete"]:
                continue
            rows.append({
                "attempt_id": attempt.id,
                "student_id": attempt.student_id,
                "student_name": getattr(attempt.student, "display_name", "") or "",
                "student_no": getattr(attempt.student, "student_no", "") or "",
                "attempt_no": attempt.attempt_no,
                "status": attempt.status,
                "status_display": attempt.get_status_display(),
                "is_timeout": attempt.is_timeout,
                "submitted_at": _fmt_dt(attempt.submitted_at),
                "score": attempt.score_float(),
                "total_score": attempt.total_score_float(),
                **{key: status[key] for key in (
                    "objective_total", "objective_graded",
                    "subjective_total", "subjective_reviewed",
                    "pending", "graded", "complete",
                )},
            })
        return rows


# ======================================================================
# 成绩单 / 错题本 / 班级成绩单 / AI 校准
# ======================================================================
class ScoreReportService:
    """成绩相关的读模型。"""

    # ------------------------------------------------------------------
    # 可见性（学生能不能看自己的分）
    # ------------------------------------------------------------------
    @staticmethod
    def score_visible(attempt: ExamAttempt) -> bool:
        """学生能否看到这场作答的分数 —— **委托给** ``ExamAttempt.score_visible()``。

        规则只允许住在一处（``apps/exam/models.py``）：这里是第二个调用口，
        不要再写一遍判断。曾经两侧各写一份，GRADED 上结论相反，导致同一份作答
        在 ``/grading/attempts/{id}/report/`` 与 ``/attempts/{id}/answers/``
        给出互相矛盾的答案。
        """
        return attempt.score_visible()

    # ------------------------------------------------------------------
    # 成绩单
    # ------------------------------------------------------------------
    @staticmethod
    def attempt_report(attempt: ExamAttempt, *, viewer=None, include_answers: bool = True) -> dict:
        """一份作答的完整成绩单。

        ``viewer`` 是教师时无条件给全量数据；是学生时按 ``score_visible()`` 决定
        要不要把「答案与得分」一起给出去。**未公开时连逐题对错都不给**——
        只给「我答了什么」，不给「我答得对不对」。
        """
        is_teacher = bool(getattr(viewer, "is_teacher", False))
        visible = is_teacher or ScoreReportService.score_visible(attempt)
        exam = attempt.exam

        paper_questions = list(
            PaperQuestion.objects.filter(paper_id=exam.paper_id)
            .select_related("question", "section").order_by("order", "id")
        )
        answer_map = {row.paper_question_id: row
                      for row in attempt.answers.select_related("paper_question__question").all()}

        items, correct_count, wrong_count, unanswered = [], 0, 0, 0
        for index, paper_question in enumerate(paper_questions, start=1):
            answer = answer_map.get(paper_question.id)
            question = paper_question.question
            is_subjective = paper_question.question_type in SUBJECTIVE_TYPES
            full_score = float(paper_question.score or 0)

            item = {
                "order": index,
                "number": paper_question.order,
                "paper_question_id": paper_question.id,
                # answer_id 是批改工作台复核单题的钥匙（POST /grading/answers/{id}/review/）
                "answer_id": answer.id if answer else None,
                "question_type": paper_question.question_type,
                "type_display": paper_question.get_question_type_display(),
                "difficulty": paper_question.difficulty,
                "stem": question.stem,
                "full_score": full_score,
                "selected_labels": answer.selected_labels if answer else "",
                "answer_text": answer.answer_text if answer else "",
                "is_answered": bool(answer and answer.is_answered),
                "is_flagged": bool(answer and answer.is_flagged),
                "score": None,
                "is_correct": None,
                "ai_score": None,
                "ai_comment": "",
                "teacher_comment": "",
                "reference_answer": "",
                "analysis": "",
                "reviewed": False,
            }

            if not item["is_answered"]:
                unanswered += 1

            if visible and answer is not None:
                item["score"] = float(answer.score or 0)
                item["ai_score"] = float(answer.ai_score) if answer.ai_score is not None else None
                item["ai_comment"] = answer.ai_comment or ""
                item["teacher_comment"] = answer.teacher_comment or ""
                item["reviewed"] = answer.feedbacks.exists()
                if is_subjective:
                    item["score_ratio"] = (round((answer.score or 0) / paper_question.score, 4)
                                           if paper_question.score else None)
                else:
                    item["is_correct"] = answer.is_correct

            if visible or is_teacher:
                item["reference_answer"] = question.correct_answer_text()
                item["analysis"] = question.analysis

            if is_subjective:
                # 主观题没有「对错」，用「得分率是否达标」当作掌握与否的判断
                if visible and answer is not None and paper_question.score:
                    ratio = float(answer.score or 0) / float(paper_question.score)
                    if ratio >= WRONG_BOOK_SUBJECTIVE_RATIO:
                        correct_count += 1
                    else:
                        wrong_count += 1
            else:
                if answer is not None and answer.is_correct is True:
                    correct_count += 1
                elif answer is not None and answer.is_correct is False:
                    wrong_count += 1

            items.append(item)

        total_score = attempt.total_score_float()
        pass_score = float(exam.effective_pass_score or 0)
        return {
            "attempt": {
                "id": attempt.id,
                "exam_id": exam.id,
                "exam_title": exam.title,
                "subject": exam.subject,
                "attempt_no": attempt.attempt_no,
                "status": attempt.status,
                "status_display": attempt.get_status_display(),
                "is_timeout": attempt.is_timeout,
                "started_at": _fmt_dt(attempt.started_at),
                "submitted_at": _fmt_dt(attempt.submitted_at),
                "graded_at": _fmt_dt(attempt.graded_at),
                "time_spent_seconds": attempt.time_spent_seconds,
            },
            "student": {
                "id": attempt.student_id,
                "name": getattr(attempt.student, "display_name", "") or "",
                "student_no": getattr(attempt.student, "student_no", "") or "",
            } if is_teacher else None,
            "visible": visible,
            "published": ScoreReportService.score_visible(attempt),
            # 得分类字段与 visible 同一把尺子。只把 items 里的答案藏起来是不够的：
            # 顶层 score / passed / counts.correct 同样能反推出「我考得怎么样」。
            # 未公开时只保留满分与及格线（学生本来就知道卷面分值）。
            "score": float(attempt.score or 0) if visible else None,
            "total_score": total_score,
            "objective_score": float(attempt.objective_score or 0) if visible else None,
            "subjective_score": float(attempt.subjective_score or 0) if visible else None,
            "pass_score": pass_score,
            "passed": attempt.passed() if visible else None,
            "score_ratio": (round(float(attempt.score or 0) / total_score, 4)
                            if (visible and total_score) else None),
            "counts": {
                "question_count": len(items),
                "answered": sum(1 for it in items if it["is_answered"]),
                "unanswered": unanswered,
                "correct": correct_count if visible else None,
                "wrong": wrong_count if visible else None,
                "accuracy": (round(correct_count / (correct_count + wrong_count), 4)
                             if (visible and (correct_count + wrong_count)) else None),
            },
            "grading": GradingService.grading_status(attempt) if is_teacher else None,
            "items": items if include_answers else [],
        }

    # ------------------------------------------------------------------
    # 班级成绩单
    # ------------------------------------------------------------------
    @staticmethod
    def scoreboard(exam, *, keyword: str = "", student_ids=None) -> dict:
        """一场考试的成绩单（含排名与分数分布）。``student_ids`` 传了则只统计这些学生。"""
        queryset = (
            exam.attempts.exclude(status=AttemptStatus.IN_PROGRESS)
            .select_related("student")
        )
        if student_ids is not None:
            queryset = queryset.filter(student_id__in=student_ids)
        if keyword:
            queryset = queryset.filter(
                Q(student__username__icontains=keyword)
                | Q(student__real_name__icontains=keyword)
                | Q(student__student_no__icontains=keyword)
            )

        rows = sorted(
            queryset, key=lambda row: (-float(row.score or 0), row.id)
        )

        total_score = float(exam.total_score or 0)
        # 分数分档按「得分率」，否则不同满分的考试没法放到同一张分布图里比
        buckets = [("90-100", 90), ("80-89", 80), ("70-79", 70), ("60-69", 60), ("0-59", 0)]
        distribution = {label: 0 for label, _ in buckets}
        graded_rows = [row for row in rows if row.is_graded]
        for row in graded_rows:
            ratio = (float(row.score or 0) / total_score * 100) if total_score else 0
            for label, floor in buckets:
                if ratio >= floor:
                    distribution[label] += 1
                    break

        graded_count = len(graded_rows)
        pass_count = sum(1 for row in graded_rows if row.passed())
        scores = [float(row.score or 0) for row in graded_rows]

        return {
            "exam": {
                "id": exam.id,
                "title": exam.title,
                "subject": exam.subject,
                "status": exam.status,
                "computed_status": exam.computed_status(),
                "total_score": total_score,
                "pass_score": float(exam.effective_pass_score or 0),
                "question_count": exam.question_count,
                "multiple_choice_policy": exam.multiple_choice_policy,
                "multiple_choice_policy_display": exam.get_multiple_choice_policy_display(),
                "show_score_immediately": exam.show_score_immediately,
            },
            "summary": {
                "applicants": exam.participant_count(),
                "started": exam.attempts.count(),
                "submitted": len(rows),
                "graded": graded_count,
                "pending_grading": len(rows) - graded_count,
                "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
                "max_score": max(scores) if scores else None,
                "min_score": min(scores) if scores else None,
                "pass_count": pass_count,
                "pass_rate": round(pass_count * 100.0 / graded_count, 1) if graded_count else None,
                "distribution": distribution,
            },
            "results": [
                {
                    "rank": index + 1,
                    "attempt_id": row.id,
                    "student_id": row.student_id,
                    "student_name": getattr(row.student, "display_name", "") or "",
                    "student_no": getattr(row.student, "student_no", "") or "",
                    "attempt_no": row.attempt_no,
                    "status": row.status,
                    "status_display": row.get_status_display(),
                    "is_timeout": row.is_timeout,
                    "submitted_at": _fmt_dt(row.submitted_at),
                    "score": float(row.score or 0),
                    "objective_score": float(row.objective_score or 0),
                    "subjective_score": float(row.subjective_score or 0),
                    "total_score": row.total_score_float(),
                    "passed": row.passed(),
                    "switch_count": row.switch_count,
                    "paste_count": row.paste_count,
                }
                for index, row in enumerate(rows)
            ],
            "count": len(rows),
        }

    # ------------------------------------------------------------------
    # 错题本
    # ------------------------------------------------------------------
    @staticmethod
    def wrong_book(student, *, subject: str = "", limit: int = 200) -> dict:
        """学生错题本。

        只收「已经能看到分数」的作答里的错题——成绩还没公布就先给学生看答案，
        等于把这份卷子提前泄了（同一份卷子可能还有别的班在考）。
        """
        visible_filter = (
            Q(attempt__status=AttemptStatus.GRADED)
            | Q(attempt__status=AttemptStatus.AUTO_GRADED,
                attempt__exam__show_score_immediately=True)
        )
        queryset = (
            Answer.objects.filter(attempt__student=student)
            .filter(visible_filter)
            .select_related("paper_question__question", "attempt__exam")
            .order_by("-attempt__submitted_at", "order")
        )
        if subject:
            queryset = queryset.filter(attempt__exam__subject=subject)

        # 按题目去重：同一道题错三次只算一条「错题」，但把次数与最近一次记下来
        grouped = {}
        for answer in queryset[: limit * 5]:
            paper_question = answer.paper_question
            question = paper_question.question
            full_score = float(paper_question.score or 0)
            score = float(answer.score or 0)

            if paper_question.question_type in SUBJECTIVE_TYPES:
                if not full_score:
                    continue
                is_wrong = score / full_score < WRONG_BOOK_SUBJECTIVE_RATIO
            else:
                is_wrong = answer.is_correct is False
            if not is_wrong:
                continue

            key = paper_question.question_id
            entry = grouped.get(key)
            if entry is None:
                grouped[key] = {
                    "question_id": question.id,
                    "question_type": paper_question.question_type,
                    "type_display": paper_question.get_question_type_display(),
                    "difficulty": paper_question.difficulty,
                    "stem": question.stem,
                    "options": [
                        {"label": option.label, "content": option.content}
                        for option in sorted(question.options.all(), key=lambda o: (o.order, o.label))
                    ],
                    "reference_answer": question.correct_answer_text(),
                    "analysis": question.analysis,
                    "knowledge_points": [
                        {"id": kp.id, "name": kp.name, "full_path": kp.full_path}
                        for kp in question.knowledge_points.all()
                    ],
                    "wrong_count": 1,
                    "last_wrong_at": _fmt_dt(answer.attempt.submitted_at),
                    "last_answer": answer.selected_labels or answer.answer_text or "",
                    "last_score": score,
                    "full_score": full_score,
                    "exam_title": answer.attempt.exam.title,
                    "exam_id": answer.attempt.exam_id,
                    "attempt_id": answer.attempt_id,
                }
            else:
                entry["wrong_count"] += 1

        rows = list(grouped.values())[:limit]
        rows.sort(key=lambda item: (-item["wrong_count"], -item["difficulty"]))
        return {
            "count": len(rows),
            "results": rows,
            "subjects": list(
                Answer.objects.filter(attempt__student=student)
                .filter(visible_filter)
                .exclude(attempt__exam__subject="")
                .values_list("attempt__exam__subject", flat=True)
                .distinct().order_by("attempt__exam__subject")
            ) if not subject else [],
        }

    # ------------------------------------------------------------------
    # AI 校准统计
    # ------------------------------------------------------------------
    @staticmethod
    def calibration(teacher=None) -> dict:
        """AI 批改与教师最终判分的一致性统计。

        这是「AI 辅助批改靠不靠谱」唯一有说服力的度量方式：拿教师最终分当基准，
        看 AI 建议分偏了多少、有多大比例被原样采纳。**只统计有 AI 建议分的记录**，
        否则教师自己改的题会把指标稀释掉。
        """
        queryset = GradingFeedback.objects.with_relations()
        if teacher is not None and not getattr(teacher, "is_admin", False):
            queryset = queryset.filter(reviewer=teacher)

        total = queryset.count()
        with_ai = list(queryset.with_ai_suggestion())
        accepted = queryset.accepted().count()

        deltas, abs_deltas, band_hits = [], [], 0
        by_type = {}

        for feedback in with_ai:
            ai_score = float(feedback.ai_score or 0)
            final_score = float(feedback.final_score or 0)
            full = float(feedback.full_score or 0)
            delta = final_score - ai_score
            deltas.append(delta)
            abs_deltas.append(abs(delta))
            if ScoreReportService._same_band(ai_score, final_score, full):
                band_hits += 1

            bucket = by_type.setdefault(
                feedback.question_type or "unknown",
                {"count": 0, "abs_delta_sum": 0.0, "exact": 0},
            )
            bucket["count"] += 1
            bucket["abs_delta_sum"] += abs(delta)
            if abs(delta) < 0.01:
                bucket["exact"] += 1

        sample = len(with_ai)
        return {
            "sample": sample,
            "total_reviews": total,
            "accepted": accepted,
            "adjusted": queryset.filter(action=ReviewAction.ADJUST).count(),
            "overridden": queryset.filter(action=ReviewAction.OVERRIDE).count(),
            "accept_rate": round(accepted * 100.0 / total, 1) if total else None,
            "mean_abs_delta": round(sum(abs_deltas) / sample, 2) if sample else None,
            "mean_signed_delta": round(sum(deltas) / sample, 2) if sample else None,
            "band_match_rate": round(band_hits * 100.0 / sample, 1) if sample else None,
            "by_question_type": [
                {
                    "question_type": key,
                    "count": value["count"],
                    "mean_abs_delta": round(value["abs_delta_sum"] / value["count"], 2),
                    "exact_match_rate": round(value["exact"] * 100.0 / value["count"], 1),
                }
                for key, value in sorted(by_type.items())
            ],
            "note": "以「教师复核后的最终分」为基准衡量 AI 建议分；样本仅含 AI 给过建议分的题目。",
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _same_band(ai_score: float, final_score: float, full: float) -> bool:
        """AI 与教师是否落在同一个得分档（满分 / 部分 / 零分）。"""
        if full <= 0:
            return abs(ai_score - final_score) < 0.01
        ai_band = ScoreReportService._band(ai_score / full)
        final_band = ScoreReportService._band(final_score / full)
        return ai_band == final_band

    @staticmethod
    def _band(ratio: float) -> str:
        if ratio >= 0.7:
            return "full"
        if ratio >= 0.4:
            return "partial"
        return "none"


__all__ = [
    "GradingService",
    "ObjectiveGradingService",
    "ScoreReportService",
    "SubjectiveGradingService",
    "normalize_labels",
    "normalize_text",
]
