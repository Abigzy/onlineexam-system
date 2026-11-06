"""试卷导出。

两种格式，各有各的用途：
  * **Markdown** —— 方便粘进 Word / 腾讯文档再排版，也方便存档；
  * **HTML**     —— 自带 A4 打印样式，浏览器里直接 Ctrl+P 就是一份能用的卷子。

为什么不用 python-docx 生成 Word？
  题库模块的导入导出用 docx 是合理的（数据交换），但试卷要的是**打印排版**：
  分页控制、页边距、题号连续性，用 HTML + 打印 CSS 比 docx 好控，而且零依赖。
"""

from django.utils.html import escape


def _fmt(value) -> str:
    number = float(value or 0)
    return str(int(number)) if number == int(number) else f"{number:g}"


def _ordered_sections(paper) -> list:
    """按大题顺序取出 {section: [paper_question, ...]}。

    没归到任何大题的题兜底放进一个「其他」分组，宁可排得难看也别丢题。
    """
    items = sorted(paper.paper_questions.all(), key=lambda item: (item.order, item.id))
    buckets = {}
    for item in items:
        buckets.setdefault(item.section_id, []).append(item)

    groups = []
    for section in sorted(paper.sections.all(), key=lambda s: (s.order, s.id)):
        rows = buckets.pop(section.id, [])
        if rows:
            groups.append((section, rows))
    for leftover in buckets.values():
        if leftover:
            groups.append((None, leftover))
    return groups


def _ordered_options(item):
    return sorted(item.question.options.all(), key=lambda o: (o.order, o.label))


# ======================================================================
# Markdown
# ======================================================================
def build_markdown(paper, *, with_answer: bool = False, with_analysis: bool = True) -> str:
    """生成 Markdown 版卷面。"""
    lines = [f"# {paper.title}", ""]
    meta = [
        f"**学科**：{paper.subject or '—'}",
        f"**总分**：{_fmt(paper.total_score)} 分",
        f"**题量**：{paper.question_count} 题",
        f"**时长**：{paper.duration_minutes} 分钟",
    ]
    lines.append("　　".join(meta))
    if paper.description:
        lines += ["", f"> {paper.description}"]
    lines += ["", "---", ""]

    for section, rows in _ordered_sections(paper):
        if section is not None:
            lines += [f"## {section.section_label()}", ""]
            if section.description:
                lines += [section.description, ""]
        for item in rows:
            lines.append(f"**{item.order}.** {item.question.stem}　（{_fmt(item.score)} 分）")
            lines.append("")
            options = _ordered_options(item)
            for option in options:
                lines.append(f"- {option.label}. {option.content}")
            if options:
                lines.append("")

    if with_answer:
        lines += ["", "---", "", "# 参考答案", ""]
        for section, rows in _ordered_sections(paper):
            if section is not None:
                lines += [f"## {section.title}", ""]
            for item in rows:
                question = item.question
                lines.append(f"**{item.order}.** 答案：{question.reference_answer or '—'}")
                if with_analysis and question.analysis:
                    lines += ["", f"解析：{question.analysis}"]
                lines.append("")

    return "\n".join(lines).strip() + "\n"


# ======================================================================
# HTML（打印版）
# ======================================================================
_PRINT_CSS = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: "Songti SC", "SimSun", "Noto Serif CJK SC", serif;
  font-size: 11.5pt; line-height: 1.9; color: #1a1a1a; margin: 0 auto;
  max-width: 190mm; padding: 10mm 0;
}
h1 { font-size: 17pt; text-align: center; margin: 0 0 6px; letter-spacing: 2px; }
.meta { text-align: center; font-size: 10.5pt; color: #444; margin-bottom: 4px; }
.desc { font-size: 10.5pt; color: #444; margin: 10px 0 0; }
hr { border: none; border-top: 1px solid #333; margin: 14px 0; }
h2 { font-size: 13pt; margin: 22px 0 6px; }
.hint { font-size: 10.5pt; color: #555; margin: 0 0 10px; font-style: italic; }
.q { margin: 0 0 4px; }
.q .score { color: #666; font-size: 10pt; }
.opts { margin: 2px 0 14px 1.6em; }
.opts div { margin: 1px 0; }
.blank { display: inline-block; min-width: 5em; border-bottom: 1px solid #333; }
.answer { page-break-before: always; }
.answer .item { margin-bottom: 12px; }
.answer .k { font-weight: 600; }
.answer .an { white-space: pre-wrap; }
.answer .ex { color: #444; font-size: 10.5pt; margin-top: 2px; }
@media print { body { padding: 0; } }
"""


def build_html(paper, *, with_answer: bool = False, with_analysis: bool = True,
               title: str = "") -> str:
    """生成可打印的 HTML 卷面。"""
    page_title = title or paper.title
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        f"<title>{escape(page_title)}</title>",
        f"<style>{_PRINT_CSS}</style>",
        "</head><body>",
        f"<h1>{escape(paper.title)}</h1>",
    ]

    meta = "　　".join([
        f"学科：{escape(paper.subject or '—')}",
        f"总分：{_fmt(paper.total_score)} 分",
        f"题量：{paper.question_count} 题",
        f"时长：{paper.duration_minutes} 分钟",
    ])
    parts.append(f'<div class="meta">{meta}</div>')
    parts.append('<div class="meta">姓名：__________　学号：__________　班级：__________</div>')
    if paper.description:
        parts.append(f'<div class="desc">{escape(paper.description)}</div>')
    parts.append("<hr>")

    for section, rows in _ordered_sections(paper):
        if section is not None:
            parts.append(f"<h2>{escape(section.section_label())}</h2>")
            if section.description:
                parts.append(f'<p class="hint">{escape(section.description)}</p>')
        for item in rows:
            stem = escape(item.question.stem)
            parts.append(
                f'<p class="q"><strong>{item.order}.</strong> {stem}'
                f' <span class="score">（{_fmt(item.score)} 分）</span></p>'
            )
            options = _ordered_options(item)
            if options:
                parts.append('<div class="opts">')
                for option in options:
                    parts.append(f"<div>{escape(option.label)}. {escape(option.content)}</div>")
                parts.append("</div>")

    if with_answer:
        parts.append('<div class="answer"><hr><h1>参考答案</h1>')
        for section, rows in _ordered_sections(paper):
            if section is not None:
                parts.append(f"<h2>{escape(section.title)}</h2>")
            for item in rows:
                question = item.question
                parts.append('<div class="item">')
                parts.append(
                    f'<span class="k">{item.order}.</span> '
                    f'<span class="k">答案：</span>'
                    f'<span class="an">{escape(question.reference_answer or "—")}</span>'
                )
                if with_analysis and question.analysis:
                    parts.append(f'<div class="ex">解析：{escape(question.analysis)}</div>')
                parts.append("</div>")
        parts.append("</div>")

    parts += ["</body></html>"]
    return "\n".join(parts)


__all__ = ["build_html", "build_markdown"]
