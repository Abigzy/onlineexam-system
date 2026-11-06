"""
题目批量导入 / 导出。

为什么用 CSV 而不是直接上 Excel 解析？
  * CSV 无依赖、任何表格软件都能导出，教师最容易上手；
  * Excel（.xlsx）通过 openpyxl 同样支持，两条路径共用同一套「行 → 题目」的解析逻辑。

列头用中文，教师看得懂；解析时对题型、难度同时兼容「中文」与「英文代码」两种写法。
"""

import csv
import io
import logging
import re
import unicodedata

from apps.common.constants import Difficulty, QuestionType
from apps.common.exceptions import BusinessError

from .constants import CHOICE_TYPES, JUDGE_OPTIONS, QuestionSource

logger = logging.getLogger("apps.questions.importers")

# 模板列头（顺序即导出顺序）
COLUMNS = [
    "知识点编号", "知识点", "学科", "题型", "难度", "分值",
    "题干", "选项A", "选项B", "选项C", "选项D", "选项E", "选项F",
    "正确答案", "解析",
]

# 多值分隔符：CSV 单元格里不能直接换行，用 | 代替
MULTI_SEP = "|"

TYPE_LABEL_TO_CODE = {label: code for code, label in QuestionType.choices}
DIFFICULTY_LABEL_TO_VALUE = {label: value for value, label in Difficulty.choices}

SAMPLE_ROWS = [
    {
        "知识点编号": "CS-DS-01", "知识点": "二叉树", "学科": "数据结构", "题型": "单选题",
        "难度": "中等", "分值": "2", "题干": "二叉树的中序遍历顺序是（　）。",
        "选项A": "根 → 左 → 右", "选项B": "左 → 根 → 右",
        "选项C": "左 → 右 → 根", "选项D": "右 → 根 → 左",
        "正确答案": "B", "解析": "中序遍历遵循「左子树 → 根 → 右子树」的顺序。",
    },
    {
        "知识点编号": "CS-DS-02", "知识点": "图的遍历", "学科": "数据结构", "题型": "判断题",
        "难度": "简单", "分值": "1", "题干": "深度优先搜索（DFS）一定使用队列实现。",
        "选项A": "", "选项B": "", "选项C": "", "选项D": "", "选项E": "", "选项F": "",
        "正确答案": "B", "解析": "DFS 使用栈（或递归）实现；使用队列的是广度优先搜索（BFS）。",
    },
    {
        "知识点编号": "CS-DS-03", "知识点": "排序算法", "学科": "数据结构", "题型": "简答题",
        "难度": "困难", "分值": "5", "题干": "简述快速排序的基本思想，并说明其平均时间复杂度。",
        "选项A": "", "选项B": "", "选项C": "", "选项D": "", "选项E": "", "选项F": "",
        "正确答案": "分治思想：选定基准元素，将序列划分为小于基准和大于基准两部分，再分别递归排序。"
                    + MULTI_SEP + "平均时间复杂度为 O(n log n)。",
        "解析": "评分要点：分治思想、基准选取、递归过程、时间复杂度。",
    },
]


# ==================================================================
# 读文件
# ==================================================================
def read_upload(uploaded_file) -> list:
    """把上传的文件读成「列头 → 值」的字典列表。"""
    name = (getattr(uploaded_file, "name", "") or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        return _read_xlsx(uploaded_file)
    if name.endswith(".csv") or name.endswith(".txt"):
        return _read_csv(uploaded_file)
    raise BusinessError("只支持 .xlsx 或 .csv 格式的文件", code=12020)


def _read_csv(uploaded_file) -> list:
    raw = uploaded_file.read()
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise BusinessError("CSV 文件编码无法识别，请另存为 UTF-8 编码", code=12021)

    reader = csv.DictReader(io.StringIO(text))
    rows = [{(_clean_key(k)): (v or "").strip() for k, v in row.items() if k} for row in reader]
    return [row for row in rows if any(row.values())]


def _read_xlsx(uploaded_file) -> list:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
        raise BusinessError("服务器未安装 openpyxl，无法解析 Excel，请改用 CSV", code=12022) from exc

    workbook = load_workbook(filename=io.BytesIO(uploaded_file.read()), data_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []

    header = [_clean_key(str(cell)) if cell is not None else "" for cell in rows[0]]
    result = []
    for raw_row in rows[1:]:
        row = {}
        for index, key in enumerate(header):
            if not key:
                continue
            value = raw_row[index] if index < len(raw_row) else ""
            row[key] = "" if value is None else str(value).strip()
        if any(row.values()):
            result.append(row)
    return result


def _clean_key(key: str) -> str:
    return (key or "").replace("\ufeff", "").strip()


# ==================================================================
# 归一化（判重用）
# ==================================================================
_WHITESPACE_RE = re.compile(r"[\s\u3000]+")


def normalize_stem(value) -> str:
    """题干归一化：全角转半角 + 去掉所有空白（含 \\u3000）+ 英文 lower。

    教师用中文括号「（ ）」和英文括号「( )」应当视为同一道题，
    NFKC 会把前者转成后者；中文汉字在 NFKC 下保持不变。
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE_RE.sub("", text).lower()


def normalize_subject(value) -> str:
    """学科归一化：去掉首尾空白 + 全角转半角 + lower。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return text.lower()


# ==================================================================
# 行 → 题目数据
# ==================================================================
def parse_rows(rows: list, *, default_subject: str = "") -> tuple:
    """把表格行解析成题目数据。

    返回 ``(items, errors)``：能解析的进 items，不能解析的记账到 errors 而不中断整批导入。
    """
    items, errors = [], []

    for index, row in enumerate(rows, start=2):  # 从第 2 行开始计数（第 1 行是表头）
        try:
            items.append(_parse_row(row, default_subject=default_subject, row_index=index))
        except BusinessError as exc:
            errors.append({"row": index, "error": exc.detail})
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析第 %s 行失败：%s", index, exc)
            errors.append({"row": index, "error": str(exc)})

    return items, errors


def _parse_row(row: dict, *, default_subject: str = "", row_index=None) -> dict:
    stem = (row.get("题干") or "").strip()
    if not stem:
        raise BusinessError("题干为空")

    question_type = _parse_type(row.get("题型"))
    difficulty = _parse_difficulty(row.get("难度"))
    score = _parse_score(row.get("分值"), question_type)
    subject = (row.get("学科") or default_subject or "").strip()

    reference_answer = (row.get("正确答案") or "").strip()
    if MULTI_SEP in reference_answer:
        reference_answer = "\n".join(
            part.strip() for part in reference_answer.split(MULTI_SEP) if part.strip()
        )

    options = _parse_options(row, question_type, reference_answer)

    knowledge_names = []
    single_name = (row.get("知识点") or "").strip()
    if single_name:
        for part in single_name.replace("、", ",").replace("，", ",").split(","):
            if part.strip():
                knowledge_names.append(part.strip())

    return {
        "_row": row_index,
        "type": question_type,
        "stem": stem,
        "difficulty": difficulty,
        "score": score,
        "subject": subject,
        "reference_answer": reference_answer if question_type not in CHOICE_TYPES else "",
        "analysis": (row.get("解析") or "").strip(),
        "options": options,
        "knowledge_point_names": knowledge_names,
        "source": QuestionSource.IMPORT,
    }


def _parse_type(value) -> str:
    text = (value or "").strip()
    if not text:
        raise BusinessError("题型不能为空")
    if text in dict(QuestionType.choices):
        return text
    if text in TYPE_LABEL_TO_CODE:
        return TYPE_LABEL_TO_CODE[text]
    lowered = text.lower()
    if lowered in dict(QuestionType.choices):
        return lowered
    raise BusinessError(f"无法识别的题型：{text}")


def _parse_difficulty(value):
    text = (value or "").strip()
    if not text:
        return int(Difficulty.MEDIUM)
    if text in DIFFICULTY_LABEL_TO_VALUE:
        return int(DIFFICULTY_LABEL_TO_VALUE[text])
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        return max(1, min(int(digits), 4))
    raise BusinessError(f"无法识别的难度：{text}")


def _parse_score(value, question_type: str):
    text = (value or "").strip()
    if not text:
        from .constants import DEFAULT_SCORE_BY_TYPE
        return DEFAULT_SCORE_BY_TYPE.get(question_type, 2.0)
    try:
        score = float(text)
    except ValueError:
        raise BusinessError(f"分值不是数字：{text}")
    if score <= 0:
        raise BusinessError("分值必须大于 0")
    return score


def _parse_options(row: dict, question_type: str, reference_answer: str) -> list:
    """从 选项A~选项F 列 + 正确答案 还原选项。"""
    if question_type not in CHOICE_TYPES:
        return []

    if question_type == QuestionType.JUDGE:
        letters = {ch for ch in reference_answer.upper() if ch.isalpha()}
        return [
            {"label": label, "content": text, "is_correct": label in letters}
            for label, text in JUDGE_OPTIONS
        ]

    letters = {ch for ch in reference_answer.upper() if ch.isalpha()}
    options = []
    for label in ("A", "B", "C", "D", "E", "F"):
        content = (row.get(f"选项{label}") or "").strip()
        if not content:
            continue
        options.append({"label": label, "content": content, "is_correct": label in letters})

    if len(options) < 2:
        raise BusinessError("选择题至少需要 2 个选项（请填写「选项A」「选项B」等列）")
    if not letters:
        raise BusinessError("选择题必须在「正确答案」列填写正确选项字母，如 A 或 ABD")

    return options


# ==================================================================
# 导出
# ==================================================================
def build_export_rows(questions) -> list:
    """把题目对象转成二维表（第一行是列头）。"""
    rows = [COLUMNS]
    for question in questions:
        option_map = {option.label: option.content for option in question.options.all()}
        knowledge = question.knowledge_points.all()
        rows.append([
            (knowledge[0].code if knowledge else ""),
            "、".join(kp.name for kp in knowledge),
            question.subject,
            question.get_type_display(),
            question.get_difficulty_display(),
            f"{float(question.score):g}",
            question.stem,
            option_map.get("A", ""), option_map.get("B", ""), option_map.get("C", ""),
            option_map.get("D", ""), option_map.get("E", ""), option_map.get("F", ""),
            MULTI_SEP.join(part for part in (question.correct_answer_text() or "").splitlines() if part)
            if question.is_choice or "\n" in (question.correct_answer_text() or "")
            else question.correct_answer_text(),
            question.analysis,
        ])
    return rows


def write_csv(rows: list) -> bytes:
    """导出为 UTF-8 BOM 的 CSV —— Excel 双击打开中文不乱码。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def write_xlsx(rows: list, sheet_title: str = "题库") -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as exc:
        raise BusinessError("服务器未安装 openpyxl，无法导出 Excel，请改用 CSV", code=12023) from exc

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title

    header_fill = PatternFill("solid", fgColor="4F46E5")
    header_font = Font(color="FFFFFF", bold=True)

    for row in rows:
        sheet.append(row)

    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    widths = [12, 16, 12, 10, 8, 6, 46, 20, 20, 20, 20, 20, 20, 20, 40]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    sheet.freeze_panes = "A2"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_template_csv() -> bytes:
    """下载导入模板（含 3 行示例，教师照着改即可）。"""
    rows = [COLUMNS] + [[row.get(col, "") for col in COLUMNS] for row in SAMPLE_ROWS]
    return write_csv(rows)
