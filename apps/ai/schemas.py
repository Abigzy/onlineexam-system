"""
结构化输出解析与校验。

大模型的输出永远是「不可信输入」：字段可能缺失、类型可能不对、难度可能是 7、
单选题可能勾了三个正确选项。**入库前必须过一遍这里**，否则脏数据会污染题库。

设计原则：
  * 能修复的就修复（补默认值、夹取范围、推导答案）；
  * 修不了的就丢弃该条目并记录原因，而不是让整个请求失败；
  * 结构性错误（比如压根没有 questions 字段）才抛异常。
"""

from dataclasses import dataclass, field

from apps.common.constants import Difficulty, QuestionType
from apps.ai.exceptions import AIResponseFormatError

CHOICE_TYPES = {QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE}
VALID_TYPES = {value for value, _ in QuestionType.choices}
TYPE_ALIASES = {
    "single_choice": QuestionType.SINGLE,
    "单选题": QuestionType.SINGLE,
    "multi_choice": QuestionType.MULTIPLE,
    "multiple_choice": QuestionType.MULTIPLE,
    "多选题": QuestionType.MULTIPLE,
    "true_false": QuestionType.JUDGE,
    "判断题": QuestionType.JUDGE,
    "填空": QuestionType.FILL,
    "填空题": QuestionType.FILL,
    "简答": QuestionType.SHORT,
    "简答题": QuestionType.SHORT,
    "论述": QuestionType.ESSAY,
    "论述题": QuestionType.ESSAY,
}

DIFFICULTY_ALIASES = {
    "easy": Difficulty.EASY, "简单": Difficulty.EASY, "容易": Difficulty.EASY,
    "medium": Difficulty.MEDIUM, "中等": Difficulty.MEDIUM, "一般": Difficulty.MEDIUM,
    "hard": Difficulty.HARD, "困难": Difficulty.HARD, "较难": Difficulty.HARD,
    "expert": Difficulty.EXPERT, "挑战": Difficulty.EXPERT, "很难": Difficulty.EXPERT,
}


# ==================================================================
# 出题
# ==================================================================
@dataclass
class GeneratedOption:
    label: str
    content: str
    is_correct: bool = False


@dataclass
class GeneratedQuestion:
    type: str
    stem: str
    difficulty: int = Difficulty.MEDIUM
    score: float = 2.0
    options: list = field(default_factory=list)
    reference_answer: str = ""
    analysis: str = ""
    knowledge_points: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def is_choice(self) -> bool:
        return self.type in CHOICE_TYPES

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "stem": self.stem,
            "difficulty": int(self.difficulty),
            "score": float(self.score),
            "options": [
                {"label": o.label, "content": o.content, "is_correct": o.is_correct}
                for o in self.options
            ],
            "reference_answer": self.reference_answer,
            "analysis": self.analysis,
            "knowledge_points": list(self.knowledge_points),
            "warnings": list(self.warnings),
        }


def parse_generated_questions(payload: dict, *, max_items: int = 50) -> list:
    """把模型返回的 JSON 解析成 GeneratedQuestion 列表。"""
    if not isinstance(payload, dict):
        raise AIResponseFormatError("模型返回的顶层结构不是对象", raw=str(payload)[:500])

    raw_items = payload.get("questions")
    if raw_items is None and isinstance(payload.get("items"), list):
        raw_items = payload["items"]
    if not isinstance(raw_items, list) or not raw_items:
        raise AIResponseFormatError("模型返回结果中缺少 questions 数组", raw=str(payload)[:500])

    questions = []
    for index, item in enumerate(raw_items[:max_items]):
        if not isinstance(item, dict):
            continue
        parsed = _parse_one_question(item, index)
        if parsed is not None:
            questions.append(parsed)

    if not questions:
        raise AIResponseFormatError("模型返回的题目全部无法解析", raw=str(payload)[:500])
    return questions


def _parse_one_question(item: dict, index: int) -> GeneratedQuestion:
    warnings = []

    stem = str(item.get("stem") or item.get("question") or item.get("title") or "").strip()
    if not stem:
        return None  # 没题干的直接丢弃

    qtype = _normalize_type(item.get("type"), stem, index)
    difficulty = _normalize_difficulty(item.get("difficulty"))
    score = _normalize_score(item.get("score"), qtype)

    raw_options = item.get("options") or item.get("choices") or []
    options = _normalize_options(raw_options, qtype, warnings)

    reference_answer = str(
        item.get("reference_answer") or item.get("answer") or item.get("correct_answer") or ""
    ).strip()

    if qtype in CHOICE_TYPES:
        correct = "".join(o.label for o in options if o.is_correct)
        if correct:
            reference_answer = correct
        elif reference_answer:
            # 模型没标 is_correct，但给了答案字母 → 回填
            letters = {ch for ch in reference_answer.upper() if ch.isalpha()}
            for option in options:
                if option.label.upper() in letters:
                    option.is_correct = True
            warnings.append("模型未标记正确选项，已按参考答案回填")
        else:
            warnings.append("选择题没有可用答案，需人工补充")

    knowledge_points = _normalize_string_list(
        item.get("knowledge_points") or item.get("knowledge_points_names") or item.get("knowledge")
    )

    return GeneratedQuestion(
        type=qtype,
        stem=stem,
        difficulty=difficulty,
        score=score,
        options=options,
        reference_answer=reference_answer,
        analysis=str(item.get("analysis") or item.get("explanation") or "").strip(),
        knowledge_points=knowledge_points,
        warnings=warnings,
    )


def _normalize_type(value, stem: str, index: int) -> str:
    raw = str(value or "").strip().lower()
    if raw in VALID_TYPES:
        return raw
    if raw in TYPE_ALIASES:
        return TYPE_ALIASES[raw]
    # 模型没给类型：用「有没有 options」猜一个，选择题默认单选
    return QuestionType.SHORT if not stem else QuestionType.SINGLE


def _normalize_difficulty(value) -> int:
    if value is None:
        return int(Difficulty.MEDIUM)
    if isinstance(value, str):
        key = value.strip().lower()
        if key in DIFFICULTY_ALIASES:
            return int(DIFFICULTY_ALIASES[key])
        stripped = key.replace("★", "").replace("级", "").strip()
        value = int(stripped) if stripped.isdigit() else int(Difficulty.MEDIUM)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(Difficulty.MEDIUM)
    return max(int(Difficulty.EASY), min(number, int(Difficulty.EXPERT)))


def _normalize_score(value, qtype: str) -> float:
    default = 5.0 if qtype in QuestionType.subjective_types() else 2.0
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if score <= 0 or score > 100:
        return default
    return round(score, 2)


def _normalize_options(raw_options, qtype: str, warnings: list) -> list:
    if qtype not in CHOICE_TYPES:
        return []
    if isinstance(raw_options, dict):
        raw_options = [{"label": k, "content": v} for k, v in raw_options.items()]
    if not isinstance(raw_options, list):
        warnings.append("选项格式无法识别")
        return []

    options, seen = [], set()
    for idx, raw in enumerate(raw_options):
        if isinstance(raw, str):
            raw = {"content": raw}
        if not isinstance(raw, dict):
            continue
        content = str(raw.get("content") or raw.get("text") or raw.get("value") or "").strip()
        if not content:
            continue
        label = str(raw.get("label") or raw.get("key") or "").strip().upper()
        if not label or len(label) > 4:
            label = chr(ord("A") + idx)
        if label in seen:
            label = chr(ord("A") + idx)
        seen.add(label)
        is_correct = bool(
            raw.get("is_correct") or raw.get("correct") or raw.get("isCorrect")
            or str(raw.get("is_correct", "")).strip().lower() in ("true", "1", "yes", "是")
        )
        options.append(GeneratedOption(label=label, content=content, is_correct=is_correct))

    options.sort(key=lambda o: o.label)
    correct_count = sum(1 for o in options if o.is_correct)

    if qtype in (QuestionType.SINGLE, QuestionType.JUDGE) and correct_count > 1:
        keep = next(o for o in options if o.is_correct)
        for option in options:
            option.is_correct = option is keep
        warnings.append("单选题出现多个正确选项，已只保留第一个")

    if qtype == QuestionType.MULTIPLE and correct_count == 0:
        warnings.append("多选题没有标记正确选项，需人工确认")

    if len(options) < 2:
        warnings.append("选项少于 2 个，需人工补充")

    return options


def _normalize_string_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("、", ",").replace("，", ",").split(",")]
        return [p for p in parts if p][:10]
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("name") or item.get("title") or ""
            text = str(item).strip()
            if text:
                result.append(text)
        return result[:10]
    return []


# ==================================================================
# 主观题评分
# ==================================================================
@dataclass
class GradingResult:
    score_ratio: float
    level: str = "partial"
    reason: str = ""
    hit_points: list = field(default_factory=list)
    missing_points: list = field(default_factory=list)
    suggestion: str = ""
    warnings: list = field(default_factory=list)

    @property
    def score_percent(self) -> int:
        return round(self.score_ratio * 100)

    def to_dict(self) -> dict:
        return {
            "score_ratio": self.score_ratio,
            "score_percent": self.score_percent,
            "level": self.level,
            "reason": self.reason,
            "hit_points": self.hit_points,
            "missing_points": self.missing_points,
            "suggestion": self.suggestion,
            "warnings": self.warnings,
        }


LEVELS = ("excellent", "good", "partial", "poor")


def parse_grading_result(payload: dict) -> GradingResult:
    """解析主观题评分结果。

    兼容模型可能返回的多种写法：``score_ratio`` / ``ratio`` / ``score``（0~100）。
    """
    if not isinstance(payload, dict):
        raise AIResponseFormatError("评分结果不是对象", raw=str(payload)[:500])

    warnings = []
    raw = payload.get("score_ratio", payload.get("ratio"))
    if raw is None:
        score = payload.get("score")
        if score is None:
            raise AIResponseFormatError("评分结果缺少 score_ratio 字段", raw=str(payload)[:500])
        try:
            raw = float(score) / 100.0
            warnings.append("模型返回 0~100 的分值，已换算为比例")
        except (TypeError, ValueError):
            raise AIResponseFormatError("评分结果中的 score 不是数字", raw=str(payload)[:500])

    try:
        ratio = float(raw)
    except (TypeError, ValueError):
        raise AIResponseFormatError("score_ratio 不是数字", raw=str(payload)[:500])

    if ratio > 1.0:
        if 10.0 <= ratio <= 100.0:
            # 看起来模型把「比例」当成了百分数（如 75 表示 75%）
            ratio = ratio / 100.0
            warnings.append("score_ratio 像是百分数，已换算为比例")
        else:
            # 3.5、-0.2 这类明显越界的值，一律按满分/零分处理，避免悄悄给出离谱分数
            ratio = 1.0
            warnings.append("score_ratio 超出 0~1 范围，已按满分处理")
    ratio = max(0.0, min(ratio, 1.0))

    level = str(payload.get("level") or "").strip().lower()
    if level not in LEVELS:
        level = _level_from_ratio(ratio)

    return GradingResult(
        score_ratio=round(ratio, 4),
        level=level,
        reason=str(payload.get("reason") or payload.get("comment") or "").strip(),
        hit_points=_normalize_string_list(payload.get("hit_points")),
        missing_points=_normalize_string_list(payload.get("missing_points")),
        suggestion=str(payload.get("suggestion") or payload.get("advice") or "").strip(),
        warnings=warnings,
    )


def _level_from_ratio(ratio: float) -> str:
    if ratio >= 0.85:
        return "excellent"
    if ratio >= 0.6:
        return "good"
    if ratio >= 0.3:
        return "partial"
    return "poor"


def score_by_threshold(ratio: float, *, full_score: float,
                       full_threshold: float = 0.7, partial_threshold: float = 0.4) -> float:
    """按需求文档的阈值策略把「得分比例」折算成实际分数。

    文档原文：相似度 0.7 以上满分，0.4~0.7 部分得分，低于 0.4 不得分。
    这里在「部分得分」区间内做线性插值，避免 0.69 和 0.41 拿到同一分。
    """
    full_score = float(full_score)
    if ratio >= full_threshold:
        return round(full_score, 2)
    if ratio >= partial_threshold:
        span = max(full_threshold - partial_threshold, 1e-6)
        return round(full_score * (ratio - partial_threshold) / span, 2)
    return 0.0
