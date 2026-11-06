# -*- coding: utf-8 -*-
"""题库批量生成的公共层。

设计原则：
  * **计算题由程序算答案**，不靠人工或模型臆测——计算机组成原理的补码、Cache 地址划分、
    网络子网划分、操作系统的调度周转时间，这些一旦答案错，整批数据就废了；
  * 概念题用「结构化事实 + 多种问法」派生，避免机械重复同一句话；
  * 输出严格对齐 `build_template_csv()` 的 15 列表头，能直接被导入接口吃下。

不依赖 Django，可独立运行：python scripts/bank_gen/build.py
"""

import random
import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# 与 apps/questions/importers.py 的模板列严格一致
COLUMNS = [
    "知识点编号", "知识点", "学科", "题型", "难度", "分值", "题干",
    "选项A", "选项B", "选项C", "选项D", "选项E", "选项F",
    "正确答案", "解析",
]

TYPE_SINGLE = "单选题"
TYPE_JUDGE = "判断题"
TYPE_FILL = "填空题"

DIFF_EASY, DIFF_MEDIUM, DIFF_HARD, DIFF_EXPERT = "简单", "中等", "困难", "挑战"


@dataclass
class Q:
    """一道待导出题目。判断题的 options 固定为 ['正确', '错误']。"""

    kp: str
    qtype: str
    difficulty: str
    stem: str
    answer: str
    options: List[str] = field(default_factory=list)
    analysis: str = ""
    score: Optional[float] = None

    @property
    def final_score(self) -> float:
        if self.score is not None:
            return self.score
        return {TYPE_SINGLE: 2.0, TYPE_JUDGE: 1.0, TYPE_FILL: 2.0}[self.qtype]

    @property
    def final_options(self) -> List[str]:
        if self.qtype == TYPE_JUDGE:
            return ["正确", "错误"]
        return self.options


def normalize(text: str) -> str:
    """题干归一化，与导入判重口径一致：全角转半角 + 去空白 + 英文小写。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = re.sub(r"[\s\u3000]+", "", s)
    return s.lower()


class Bank:
    """一个学科的题池：负责去重、分布校验、导出。"""

    def __init__(self, subject: str):
        self.subject = subject
        self.items: List[Q] = []
        self._seen: set = set()

    # ---------------- 收录 ----------------
    def add(self, q: Q, *, allow_dup: bool = False) -> bool:
        key = (q.kp, q.qtype, normalize(q.stem))
        if not allow_dup and key in self._seen:
            return False
        self._seen.add(key)
        self.items.append(q)
        return True

    def extend(self, questions: Sequence[Q]) -> int:
        n = 0
        for q in questions:
            n += 1 if self.add(q) else 0
        return n

    # ---------------- 校验 ----------------
    def validate(self) -> List[str]:
        problems = []
        for i, q in enumerate(self.items, start=1):
            if q.qtype == TYPE_SINGLE:
                if len(q.options) != 4:
                    problems.append(f"#{i} 单选题选项数={len(q.options)}：{q.stem[:30]}")
                if q.answer not in ("A", "B", "C", "D"):
                    problems.append(f"#{i} 单选题答案非法={q.answer}：{q.stem[:30]}")
            elif q.qtype == TYPE_JUDGE:
                if q.answer not in ("A", "B"):
                    problems.append(f"#{i} 判断题答案非法={q.answer}")
            elif q.qtype == TYPE_FILL:
                if not q.answer.strip():
                    problems.append(f"#{i} 填空题答案为空：{q.stem[:30]}")
            if q.qtype in (TYPE_SINGLE, TYPE_JUDGE) and not q.analysis.strip():
                problems.append(f"#{i} 缺解析：{q.stem[:30]}")
        return problems

    def distribution(self) -> dict:
        by_type, by_diff, by_kp = {}, {}, {}
        for q in self.items:
            by_type[q.qtype] = by_type.get(q.qtype, 0) + 1
            by_diff[q.difficulty] = by_diff.get(q.difficulty, 0) + 1
            by_kp[q.kp] = by_kp.get(q.kp, 0) + 1
        return {"总数": len(self.items), "题型": by_type, "难度": by_diff, "知识点": by_kp}

    # ---------------- 导出 ----------------
    def write_xlsx(self, path: str) -> str:
        wb = Workbook()
        ws = wb.active
        ws.title = "题目"

        header_fill = PatternFill("solid", fgColor="1F4E79")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        for col, name in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=1, column=col, value=name)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for row, q in enumerate(self.items, start=2):
            opts = q.final_options
            values = [
                "",                      # 知识点编号：留空，导入时按名称关联已有知识点
                q.kp,
                self.subject,
                q.qtype,
                q.difficulty,
                q.final_score,
                q.stem,
                *(opts + [""] * (6 - len(opts))),
                q.answer,
                q.analysis,
            ]
            for col, value in enumerate(values, start=1):
                ws.cell(row=row, column=col, value=value)

        widths = [12, 14, 16, 10, 8, 6, 60, 22, 22, 22, 22, 10, 10, 10, 50]
        for col, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.freeze_panes = "A2"

        wb.save(path)
        return path


# ======================================================================
# 概念题派生器：一条事实 → 判断 / 单选 / 填空
# ======================================================================

@dataclass
class Fact:
    """一条可考的知识事实。

    statement 是一个**正确**的陈述；wrong 是同一问题上常见的错误说法（用于判断题的假命题）。
    question 是单选/填空用的问句；options 为 4 个候选（第一个必须是正确答案）。
    """

    kp: str
    statement: str
    question: str
    options: List[str]
    wrong: str = ""
    analysis: str = ""
    difficulty: str = DIFF_MEDIUM


def facts_to_single(facts: Sequence[Fact], rng: random.Random) -> List[Q]:
    """每条事实派一道单选：打乱 4 个选项，答案跟着走。"""
    out = []
    for f in facts:
        opts = list(f.options[:4])
        if len(opts) < 4:
            continue
        correct = opts[0]
        order = opts[:]
        rng.shuffle(order)
        answer = "ABCD"[order.index(correct)]
        out.append(Q(
            kp=f.kp, qtype=TYPE_SINGLE, difficulty=f.difficulty,
            stem=f.question, options=order, answer=answer,
            analysis=f.analysis or f.statement,
        ))
    return out


def facts_to_judge(facts: Sequence[Fact], rng: random.Random, *, ratio: float = 0.5) -> List[Q]:
    """每条事实派一道判断；按 ratio 的比例改写成假命题（答案 B）。"""
    out = []
    for f in facts:
        make_wrong = bool(f.wrong) and rng.random() < ratio
        if make_wrong:
            out.append(Q(
                kp=f.kp, qtype=TYPE_JUDGE, difficulty=DIFF_EASY,
                stem=f.wrong, answer="B",
                analysis=f.analysis or f.statement,
            ))
        else:
            out.append(Q(
                kp=f.kp, qtype=TYPE_JUDGE, difficulty=DIFF_EASY,
                stem=f.statement, answer="A",
                analysis=f.analysis or f.statement,
            ))
    return out


def facts_to_fill(facts: Sequence[Fact], rng: random.Random, *, count: int = 20) -> List[Q]:
    """从事实里挑若干条做填空：把答案词挖空。

    只处理 options[0] 能在 statement 中原样出现的，挖空后可读。
    """
    out = []
    pool = [f for f in facts if f.options and f.options[0] and f.options[0] in f.statement]
    rng.shuffle(pool)
    for f in pool[:count]:
        blanked = f.statement.replace(f.options[0], "（　）", 1)
        out.append(Q(
            kp=f.kp, qtype=TYPE_FILL, difficulty=DIFF_MEDIUM,
            stem=blanked, answer=f.options[0],
            analysis=f.analysis or f.statement,
        ))
    return out
