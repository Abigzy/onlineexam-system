"""
本地模拟回答（Mock）。

存在的意义：
  1. **没配 API Key 也能把系统跑起来** —— 课堂演示、答辩现场断网都不怕；
  2. **单元测试不依赖外网** —— 测试永远不会因为大模型抖动而失败；
  3. 给真实调用提供一个「基线对照」——例如主观题评分的字符重合度算法，
     本身就是文档里「相似度阈值」最朴素的实现。

所有 mock 输出都必须严格符合真实调用的约定结构，否则下游解析会挂。
"""

import hashlib
import re

# ------------------------------------------------------------------
# 从提示词里提取参数（mock 没有结构化入参，只能从文本里读）
# ------------------------------------------------------------------
_TYPE_KEYWORDS = [
    ("single", ("单选", "单选题")),
    ("multiple", ("多选", "多选题")),
    ("judge", ("判断", "判断题")),
    ("fill", ("填空", "填空题")),
    ("short", ("简答", "简答题")),
    ("essay", ("论述", "论述题")),
]

_TYPE_TEMPLATES = {
    "single": {
        "stem": "关于「{kp}」，下列说法正确的是（　）。",
        "options": [
            {"label": "A", "content": "{kp} 的核心特征之一，属于正确表述", "is_correct": True},
            {"label": "B", "content": "与 {kp} 的定义相矛盾的表述", "is_correct": False},
            {"label": "C", "content": "把 {kp} 与相邻概念混淆的表述", "is_correct": False},
            {"label": "D", "content": "对 {kp} 适用范围的过度推广", "is_correct": False},
        ],
        "reference_answer": "A",
    },
    "multiple": {
        "stem": "关于「{kp}」，下列哪些说法是正确的？（　）",
        "options": [
            {"label": "A", "content": "{kp} 的基本性质", "is_correct": True},
            {"label": "B", "content": "{kp} 的典型应用场景", "is_correct": True},
            {"label": "C", "content": "与 {kp} 无关的干扰项", "is_correct": False},
            {"label": "D", "content": "{kp} 的常见误解", "is_correct": False},
        ],
        "reference_answer": "AB",
    },
    "judge": {
        "stem": "「{kp}」的这一说法是正确的。（　）",
        "options": [
            {"label": "A", "content": "正确", "is_correct": True},
            {"label": "B", "content": "错误", "is_correct": False},
        ],
        "reference_answer": "A",
    },
    "fill": {
        "stem": "在 {kp} 中，实现核心功能的关键步骤是 ______。",
        "options": [],
        "reference_answer": "{kp}的关键步骤",
    },
    "short": {
        "stem": "简述「{kp}」的基本原理，并说明它的一个典型应用。",
        "options": [],
        "reference_answer": "需要答出 {kp} 的定义、基本原理，以及至少一个典型应用场景。",
    },
    "essay": {
        "stem": "结合实例论述「{kp}」在实际项目中的价值与局限。",
        "options": [],
        "reference_answer": "需包含：概念界定、价值分析、局限性讨论、实例佐证四个层次。",
    },
}

# 难度码 → 中文（推荐练习的提示词里难度用 1-4 表示）
_DIFFICULTY_LABELS = {1: "简单", 2: "中等", 3: "困难", 4: "挑战"}


def detect_types(text: str, default=("single",)) -> list:
    """从提示词里识别出要生成哪些题型。"""
    found = []
    for type_code, keywords in _TYPE_KEYWORDS:
        if any(kw in text for kw in keywords):
            found.append(type_code)
    return found or list(default)


def detect_count(text: str, default: int = 5, low: int = 1, high: int = 20) -> int:
    """从提示词里识别题量。优先匹配『N 道』『N 题』这种明确写法。"""
    match = re.search(r"(\d+)\s*(?:道|个|题)", text)
    if match:
        return max(low, min(int(match.group(1)), high))
    numbers = re.findall(r"\b(\d{1,2})\b", text)
    if numbers:
        return max(low, min(int(numbers[0]), high))
    return default


def detect_knowledge_points(text: str, default=("综合知识点",)) -> list:
    """尽量从提示词里抠出知识点名称。

    兼容两种写法：
        【知识点列表】二叉树、图的遍历
        知识点列表：二叉树、图的遍历
    """
    patterns = [
        r"【\s*知识点列表\s*】\s*([^\n【]+)",
        r"知识点列表\s*[:：]\s*([^\n【]+)",
        r"覆盖知识点\s*[:：]\s*([^\n【]+)",
        r"考查知识点\s*[:：]\s*([^\n【]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            raw = match.group(1).strip()
            items = [item.strip(" 　·-、,，。") for item in re.split(r"[、,，;；/|]", raw)]
            items = [item for item in items if item]
            if items:
                return items[:6]
    return list(default)


def _difficulty_cycle(index: int, maximum: int = 4) -> int:
    return index % maximum + 1


# ==================================================================
# 各场景的模拟输出
# ==================================================================
def generate_questions(messages, **kwargs) -> dict:
    """模拟「AI 辅助出题」。

    输出结构与 `prompts/generate_questions.txt` 里约定的 JSON 完全一致。
    """
    prompt = _joined_text(messages)
    types = detect_types(prompt)
    count = detect_count(prompt)
    knowledge_points = detect_knowledge_points(prompt)

    questions = []
    for index in range(count):
        type_code = types[index % len(types)]
        kp = knowledge_points[index % len(knowledge_points)]
        template = _TYPE_TEMPLATES[type_code]
        options = [
            {**option, "content": option["content"].format(kp=kp)}
            for option in template["options"]
        ]
        questions.append({
            "type": type_code,
            "stem": template["stem"].format(kp=kp),
            "difficulty": _difficulty_cycle(index),
            "score": 5.0 if type_code in ("short", "essay") else 2.0,
            "options": options,
            "reference_answer": template["reference_answer"].format(kp=kp),
            "analysis": f"【本地模拟解析】本题考查「{kp}」，正确答案依据教材中该知识点的定义与适用条件。",
            "knowledge_points": [kp],
        })

    return {"questions": questions, "source": "mock", "message": "由本地模拟生成，未调用真实大模型"}


def grade_subjective(messages, **kwargs) -> dict:
    """模拟「主观题评分」。

    用「学生答案 vs 参考答案」的字符二元组重合度当相似度，
    正好对应需求文档里「0.7 以上满分、0.4~0.7 部分得分、低于 0.4 不得分」的朴素实现。

    注意：只从**最后一条消息（user）**里抽取内容——system 段落里也会出现
    「参考答案」这类词，混在一起抽会串。
    """
    prompt = _last_user_text(messages)
    reference = _extract_section(prompt, ("评分要点 / 参考答案", "参考答案", "标准答案", "参考要点"))
    answer = _extract_section(prompt, ("学生答案", "考生作答", "作答内容"))

    ratio = _similarity(answer, reference) if (answer and reference) else 0.0
    # 兜底：真实模型不会给出 0，mock 用答案长度给一个平滑值，避免全是 0
    if ratio == 0.0 and answer:
        ratio = min(0.35 + len(answer) / 800, 0.6)

    if ratio >= 0.7:
        level, reason = "excellent", "要点覆盖完整，表述准确，与参考答案高度一致。"
    elif ratio >= 0.4:
        level, reason = "partial", "答出了部分要点，但存在遗漏或表述不够准确。"
    else:
        level, reason = "poor", "与参考答案重合度较低，关键要点基本未命中。"

    return {
        "score_ratio": round(ratio, 3),
        "level": level,
        "reason": f"【本地模拟评分】{reason}（字符重合度 {ratio:.2f}）",
        "hit_points": _split_points(reference)[:2],
        "missing_points": _split_points(reference)[2:5],
        "suggestion": "建议对照参考答案补充遗漏要点，并用自己的话复述一遍。",
        "source": "mock",
    }


def analyze_weakness(messages, **kwargs) -> dict:
    """模拟「学情分析」。

    真实模型会写得更自然，但这里必须做到两件事：

    1. **结构与提示词约定完全一致**（``summary`` / ``overall_level`` / ``weak_points`` /
       ``strengths`` / ``advice``）——否则前端在没配 Key 时拿到的永远是空壳，
       页面看起来能用，其实一个字段都没验证过；
    2. **结论真的来自提示词里那张正确率表**，而不是硬编码一段话。
       这样离线演示时，学情页上的薄弱点与雷达图是同源的，不会自相矛盾。

    没有大模型可调，判定规则就退化成最朴素的一条：**正确率 < 60% 且答题数 ≥ 2**。
    与 ``apps/analytics/constants.py`` 里的 WEAKNESS_RATIO / MIN_SAMPLES_FOR_WEAKNESS 一致。
    """
    text = _last_user_text(messages)
    student = _extract_section(text, ["学生"]) or "该学生"
    table = _extract_section(text, ["知识点掌握情况"])

    rows = []
    for line in (table or "").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            continue
        name = parts[0].strip(" 　·-")
        if not name:
            continue
        answered_digits = re.sub(r"[^\d]", "", parts[1])
        accuracy_digits = re.sub(r"[^\d.]", "", parts[2])
        try:
            answered = int(answered_digits or 0)
        except ValueError:
            answered = 0
        try:
            accuracy = float(accuracy_digits or 0) / 100.0
        except ValueError:
            accuracy = 0.0
        rows.append({"name": name, "answered": answered, "accuracy": accuracy})

    weak = sorted(
        (row for row in rows if row["accuracy"] < 0.6 and row["answered"] >= 2),
        key=lambda row: row["accuracy"],
    )
    strong = [row["name"] for row in sorted(rows, key=lambda row: -row["accuracy"])
              if row["accuracy"] >= 0.7][:5]

    total_answered = sum(row["answered"] for row in rows)
    average = (
        sum(row["accuracy"] * row["answered"] for row in rows) / total_answered
        if total_answered else 0.0
    )

    if rows:
        summary = (
            f"【本地模拟】{student}累计作答 {total_answered} 道题，"
            f"整体正确率约 {round(average * 100, 1)}%；"
            + (f"其中 {len(weak)} 个知识点低于 60% 的达标线。" if weak
               else "各知识点均已达到掌握线。")
        )
    else:
        summary = f"【本地模拟】{student}暂无足够的答题数据可用于诊断。"

    if average >= 0.85:
        level = "优秀"
    elif average >= 0.70:
        level = "良好"
    elif average >= 0.60:
        level = "中等"
    else:
        level = "待提升"

    advice = []
    if weak:
        advice.append("优先巩固正确率低于 60% 的知识点，按「概念 → 例题 → 变式练习」的顺序推进。")
        advice.append("整理错题时标注失分原因（概念不清 / 审题失误 / 计算粗心），对症下药。")
    if strong:
        advice.append("保持已掌握知识点的熟练度：" + "、".join(strong) + "。")
    if not advice:
        advice.append("继续按当前节奏完成练习，并适当增加进阶题型。")

    return {
        "summary": summary,
        "overall_level": level,
        "weak_points": [
            {
                "knowledge_point": row["name"],
                "accuracy": round(row["accuracy"], 4),
                "severity": "high" if row["accuracy"] < 0.4 else "medium",
                "comment": (f"作答 {row['answered']} 题，正确率 "
                            f"{round(row['accuracy'] * 100, 1)}%，建议优先复习。"),
            }
            for row in weak[:8]
        ],
        "strengths": strong,
        "advice": advice,
        "source": "mock",
    }


def recommend_practice(messages, **kwargs) -> dict:
    """模拟「个性化推荐」。

    与 ``analyze_weakness`` 同样的要求：**结论必须来自提示词里的真实数据**，
    而不是硬编码一段话。提示词已经把两样东西喂进来了：

      * ``【薄弱知识点】`` —— 每行「完整路径 | 正确率 x%」；
      * ``【候选题目池】`` —— 每行「题目ID | 知识点 | 难度1-4 | 题干摘要」。

    于是离线模拟挑题的原则也很直白：
    **先挑命中薄弱知识点的，再挑难度贴近中等的**（与
    ``apps/recommend/services.py`` 里「略高于当前水平」的口径一致），
    并逐题给出一句与「知识点 + 正确率 + 难度」对应得上的推荐理由。

    这样没配 Key 时「每日练习」页面上看到的理由，与页面上的薄弱点、
    匹配度是**同源**的，不会自相矛盾。

    入门练习（``【推荐模式】入门练习``）是另一套口径：学生在该学科没有任何
    作答数据，理由里只说「建立掌握度起点」，绝不出现正确率 / 薄弱 / 错题。
    """
    text = _last_user_text(messages)
    student = _extract_section(text, ["学生"]) or "该学生"
    weak_text = _extract_section(text, ["薄弱知识点"])
    pool_text = _extract_section(text, ["候选题目池"])
    count_text = _extract_section(text, ["需要推荐的题目数量"])
    # 模式由提示词里的【推荐模式】小节决定：入门练习的学生没有掌握度，
    # 不能照着「薄弱知识点」的模板讲话（那会说出与事实相反的理由）。
    is_starter = "入门" in (_extract_section(text, ["推荐模式"]) or "")

    # ---- 薄弱知识点：每行「完整路径 | 正确率 42.0%」，取路径末段作为知识点名 ----
    weak = {}
    for line in (weak_text or "").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if not parts or not parts[0]:
            continue
        path = parts[0].strip(" 　·-")
        if not path or path.startswith("（"):
            continue
        name = path.split("/")[-1].strip()
        if not name:
            continue
        accuracy = None
        if len(parts) >= 2:
            digits = re.sub(r"[^\d.]", "", parts[1])
            try:
                accuracy = float(digits) / 100.0
            except ValueError:
                accuracy = None
        weak[name] = {"full_path": path, "accuracy": accuracy}

    # ---- 候选题目池 ----
    pool = []
    for line in (pool_text or "").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            continue
        id_digits = re.sub(r"[^\d]", "", parts[0])
        if not id_digits:
            continue
        difficulty_digits = re.sub(r"[^\d]", "", parts[2])
        try:
            difficulty = int(difficulty_digits or 0)
        except ValueError:
            difficulty = 0
        pool.append({
            "question_id": int(id_digits),
            "knowledge_point": parts[1],
            "difficulty": difficulty,
            "stem": parts[3] if len(parts) > 3 else "",
        })

    # ---- 要几道 ----
    try:
        count = int(re.sub(r"[^\d]", "", count_text or "") or 0)
    except ValueError:
        count = 0
    if count <= 0:
        count = min(len(pool), 6)
    count = min(count, len(pool))

    # ---- 挑题：命中薄弱知识点优先，其次难度贴近「中等」（略高于当前水平）----
    ordered = sorted(
        pool,
        key=lambda item: (
            0 if item["knowledge_point"] in weak else 1,
            abs(item["difficulty"] - 2),
            item["question_id"],
        ),
    )

    recommendations = []
    for item in ordered[:count]:
        label = _DIFFICULTY_LABELS.get(item["difficulty"], "中等")
        if is_starter:
            # 入门练习：只讲「建立基线 / 先易后难」，不提正确率、不提薄弱
            recommendations.append({
                "question_id": item["question_id"],
                "knowledge_point": item["knowledge_point"],
                "reason": (
                    f"「{item['knowledge_point']}」入门题（难度{label}），"
                    f"先练这道帮我们了解你的起点，为后续个性化推荐打底。"
                ),
            })
            continue
        info = weak.get(item["knowledge_point"]) or {}
        accuracy = info.get("accuracy")
        percent = f"{round(accuracy * 100, 1)}%" if accuracy is not None else "暂未统计"
        recommendations.append({
            "question_id": item["question_id"],
            "knowledge_point": item["knowledge_point"],
            "reason": (
                f"「{item['knowledge_point']}」正确率 {percent}，属于你的薄弱环节；"
                f"本题难度{label}，略高于当前水平，适合作为巩固练习。"
            ),
        })

    if is_starter:
        if recommendations:
            covered = list(dict.fromkeys(
                row["knowledge_point"] for row in recommendations if row["knowledge_point"]
            ))
            reason = (
                f"【本地模拟】{student}在该学科还没有作答记录，这组是入门练习："
                f"从难度适中的题开始，覆盖 {len(covered)} 个知识点，"
                f"做完之后就能按掌握度做个性化推荐了。"
            )
        else:
            reason = f"【本地模拟】{student}暂无可推荐题目（候选池为空）。"
        return {
            "reason": reason,
            "recommendations": recommendations,
            "source": "mock",
        }

    names = "、".join(list(weak)[:5]) or "暂无"
    if recommendations:
        reason = (
            f"【本地模拟】{student}的薄弱知识点主要集中在：{names}。"
            f"已从候选题目池中挑出 {len(recommendations)} 道难度贴近当前水平的题目，"
            f"优先覆盖这些知识点。"
        )
    else:
        reason = f"【本地模拟】{student}暂无可推荐题目（候选池为空）。"

    return {
        "reason": reason,
        "recommendations": recommendations,
        "source": "mock",
    }


def compose_paper(messages, **kwargs) -> str:
    """本地模拟的组卷说明。

    提示词里已经把卷面数据（概况 / 题型分布 / 难度分布 / 知识点覆盖 / 系统预警）
    都喂进来了，所以这里不需要「猜」，直接把结构化数据重新组织成四段式 Markdown 即可。
    真实模型会写得更自然，但离线演示时至少能看到一段内容与卷面一致、结构完整的说明，
    而不是掉进 generic 分支返回一段占位文本。
    """
    text = _last_user_text(messages)
    title = _extract_section(text, ["试卷标题"]) or "本试卷"
    subject = _extract_section(text, ["学科"]) or "未指定"
    overview = _extract_section(text, ["卷面概况"])
    type_summary = _extract_section(text, ["题型与分值分布"])
    difficulty_summary = _extract_section(text, ["难度分布"])
    knowledge_summary = _extract_section(text, ["知识点覆盖"])
    warning_text = _extract_section(text, ["系统检测到的问题"])

    if warning_text and "未检测到" not in warning_text:
        advice = f"组卷时系统提示了以下问题，建议发布前处理：{warning_text}"
    else:
        advice = "整卷结构与难度配比均在预期范围内，可直接用于考试；主观题建议提前明确评分要点。"

    return "\n".join([
        "> 本地模拟生成的组卷说明（未配置大模型 API Key），内容依据真实卷面数据，仅表达方式为模板生成。",
        "",
        "## 组卷思路",
        f"《{title}》为{subject}课程的组卷成果，题型与分值构成如下：{type_summary}",
        "",
        "## 内容覆盖",
        knowledge_summary or "（未标注知识点）",
        "",
        "## 难度设计",
        f"{difficulty_summary}。各档题量按目标难度插值分配，使整卷难度贴近预期值。",
        "",
        "## 使用建议",
        advice,
        "",
        f"（卷面概况：{overview}）",
    ])


def generic(messages, **kwargs) -> str:
    """兜底：返回一段说明性文本。"""
    prompt = _joined_text(messages)
    digest = hashlib.md5(prompt.encode("utf-8")).hexdigest()[:8]
    return (
        "【本地模拟回答】当前未配置大模型 API Key，本回答由本地 Mock Provider 生成，"
        "仅用于跑通流程与演示，不代表真实模型输出。\n"
        f"收到的问题摘要：{prompt[:80].strip() or '（空）'}\n"
        f"指纹：{digest}"
    )


# ==================================================================
# 内部工具
# ==================================================================
def _joined_text(messages) -> str:
    """把消息列表拼成一段纯文本，方便做关键词识别。"""
    parts = []
    for item in messages:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            parts.append(str(item.get("content", "")))
    return "\n".join(parts)


def _last_user_text(messages) -> str:
    """取最后一条 user 消息的内容（没有则退回全文）。"""
    if isinstance(messages, str):
        return messages
    for item in reversed(list(messages or [])):
        if isinstance(item, str):
            return item
        if isinstance(item, dict) and item.get("role") in (None, "user"):
            return str(item.get("content", ""))
    return _joined_text(messages)


_SECTION_PATTERN = re.compile(r"【\s*([^】\n]+?)\s*】\s*\n?(.*?)(?=\n\s*【|\Z)", re.S)
_INLINE_PATTERN = r"{label}\s*[:：]\s*(.+)"


def _parse_sections(text: str) -> dict:
    """把 ``【标题】正文`` 解析成 ``{标题: 正文}``。

    为什么不用「逐个 label 正则搜」而要一次性解析成字典：
    ``【学生答案】`` 是提示词里**最后一个**小节，后面还跟着一句指令
    （「请按 JSON 结构输出评分结果」）。逐个搜的话这句话会被当成学生答案的一部分，
    相似度凭空掉几个百分点——mock 的分数就失真了。
    先按小节切干净，再取用，边界问题一次解决。
    """
    sections = {}
    for match in _SECTION_PATTERN.finditer(text or ""):
        title = match.group(1).strip()
        body = match.group(2).strip()
        if title and title not in sections:
            sections[title] = body
    return sections


def _extract_section(text: str, labels) -> str:
    """从提示词里抽取某个小节的正文。

    兼容两种排版：
        【参考答案】\\n正文...            ← 提示词模板用的是这种
        参考答案：正文...                  ← 手写提示词时常见
    小节标题做「相等或互相包含」匹配，这样 ``参考答案`` 也能命中
    ``评分要点 / 参考答案``（真实的 JSON 示例里写的是后者）。
    """
    sections = _parse_sections(text)
    for label in labels:
        for title, body in sections.items():
            if not body:
                continue
            if title == label or label in title or title in label:
                return body
    for label in labels:
        match = re.search(_INLINE_PATTERN.format(label=re.escape(label)), text or "")
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return ""


def _split_points(text: str) -> list:
    """把参考答案按行/分号拆成要点。"""
    if not text:
        return []
    items = re.split(r"[\n;；]", text)
    return [item.strip(" 　·-、.。") for item in items if item.strip()][:8]


def _similarity(left: str, right: str) -> float:
    """字符二元组（bigram）Dice 系数，取值 0~1。"""
    if not left or not right:
        return 0.0

    def bigrams(text: str) -> set:
        normalized = re.sub(r"\s+", "", text)
        if len(normalized) < 2:
            return {normalized}
        return {normalized[i : i + 2] for i in range(len(normalized) - 1)}

    a, b = bigrams(left), bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) * 2 / (len(a) + len(b))


# 场景名 → 处理函数。新增场景时在这里登记一行即可。
SCENE_HANDLERS = {
    "generate_questions": generate_questions,
    "grade_subjective": grade_subjective,
    "analyze_weakness": analyze_weakness,
    "recommend_practice": recommend_practice,
    "compose_paper": compose_paper,
    "generic": generic,
}
