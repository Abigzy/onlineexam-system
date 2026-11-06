# -*- coding: utf-8 -*-
"""生成《AI 在线考试与学情分析系统 · 功能测试用例》Excel 工作簿。

用法（项目根目录）：
    .venv\\Scripts\\python.exe -m scripts.testcase_gen.build_xlsx

产出：docs/OnlineExam-功能测试用例.xlsx
"""

import json
import os
import re
import sys
from collections import Counter, OrderedDict, defaultdict

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.testcase_gen import (  # noqa: E402
    data_part1, data_part2, data_part3, data_part4, data_part5,
)

CASES = data_part1.CASES + data_part2.CASES + data_part3.CASES

CASE_IDS = {c[0] for c in CASES}
EXEC_PATH = os.path.join("docs", ".exec_results.json")
EXEC_RAW = []
if os.path.exists(EXEC_PATH):
    with open(EXEC_PATH, encoding="utf-8") as fh:
        EXEC_RAW = json.load(fh)
MANUAL_PATH = os.path.join("docs", ".exec_manual.json")
if os.path.exists(MANUAL_PATH):
    with open(MANUAL_PATH, encoding="utf-8") as fh:
        EXEC_RAW.extend(json.load(fh))

# 子检查点（TC-XX-01a）归并到主用例：任一失败则主用例失败
EXEC_BY_CASE = {}
for item in EXEC_RAW:
    cid = item["id"]
    base = re.sub(r"[a-z]$", "", cid)
    if cid in CASE_IDS:
        base = cid
    elif base not in CASE_IDS:
        continue
    cur = EXEC_BY_CASE.get(base)
    if cur is None:
        EXEC_BY_CASE[base] = {"result": item["result"], "notes": [item.get("actual", "")]}
    else:
        if item["result"] == "失败":
            cur["result"] = "失败"
        cur["notes"].append(item.get("actual", ""))

BUG_OF = {}
for d in data_part5.DEFECTS:
    if d[1] == "缺陷":
        BUG_OF[d[3]] = d[0]
        BUG_OF.setdefault(re.sub(r"[a-z]$", "", d[3]), d[0])


def exec_stats():
    """执行统计的唯一来源：脚本结果 + 用例判定。"""
    total = len(EXEC_RAW)
    ok = sum(1 for x in EXEC_RAW if x["result"] == "通过")
    cases = len(EXEC_BY_CASE)
    passed = sum(1 for c in CASES if decide(c)[0].startswith("通过"))
    failed = sum(1 for c in CASES if decide(c)[0] == "失败")
    pending = sum(1 for c in CASES if decide(c)[0] == "未执行")
    return {"checks": total, "checks_ok": ok, "checks_fail": total - ok,
            "cases": cases, "passed": passed, "failed": failed, "pending": pending}


def defect_stats():
    bugs = [d for d in data_part5.DEFECTS if d[1] == "缺陷"]
    closed = sum(1 for d in bugs if d[11] == "已关闭")
    return {"total": len(bugs), "closed": closed, "open": len(bugs) - closed}


def decide(case):
    """返回 (执行结果, 缺陷编号, 备注补充)。"""
    cid = case[0]
    hit = EXEC_BY_CASE.get(cid)
    if hit:
        note = "实测：" + "；".join(hit["notes"])[:160]
        if hit["result"] == "失败":
            return "失败", BUG_OF.get(cid, ""), note
        if cid in BUG_OF:
            note += f"（原缺陷 {BUG_OF[cid]} 已修复并回归通过）"
        return hit["result"], "", note
    if case[10].startswith("自动化"):
        return "通过（回归）", "", f"{data_part4.BASELINE.split('·')[0].strip()} 覆盖"
    return "未执行", "", "需人工 / 浏览器执行"


OUT_PATH = os.path.join("docs", "OnlineExam-功能测试用例.xlsx")

# ---------------------------------------------------------------- 样式常量
NAVY = "1F3864"
BLUE = "1F4E79"
LIGHT = "D9E2F3"
GREY = "F2F2F2"
WHITE = "FFFFFF"
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
FONT_NAME = "微软雅黑"

HEADERS = [
    "用例编号", "所属模块", "用例标题", "前置条件", "测试步骤", "测试数据", "预期结果",
    "优先级", "用例类型", "需求编号", "建议执行方式", "执行结果", "缺陷编号", "备注",
]
WIDTHS = [14, 22, 30, 30, 48, 38, 50, 8, 11, 14, 16, 10, 10, 26]


def est_lines(text, width):
    """按列宽估算换行行数（中文字符按 2 个宽度计，留 10% 余量防截断）。"""
    if not text:
        return 1
    total = 0
    for seg in str(text).split("\n"):
        units = sum(2 if ord(ch) > 127 else 1 for ch in seg)
        total += max(1, -(-units // max(4, int(width * 0.95))))
    return total


def style_title(ws, text, ncols, sub=None):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    c = ws.cell(row=1, column=1, value=text)
    c.font = Font(name=FONT_NAME, size=15, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 34
    if sub is not None:
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
        c2 = ws.cell(row=2, column=1, value=sub)
        c2.font = Font(name=FONT_NAME, size=9, color="595959")
        c2.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[2].height = 20


# ================================================================ 说明页
def build_cover(wb):
    ws = wb.active
    ws.title = "0-说明"
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDE", [18, 30, 30, 30, 22]):
        ws.column_dimensions[col].width = w

    ws.merge_cells("A1:E1")
    c = ws["A1"]
    c.value = "AI 在线考试与学情分析系统 · 功能测试用例"
    c.font = Font(name=FONT_NAME, size=20, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 52

    ws.merge_cells("A2:E2")
    c = ws["A2"]
    c.value = (f"基于 Django 4.2 + DRF 的在线考试平台 ｜ 功能测试执行依据 ｜ "
               f"版本 {data_part4.VERSION}（{data_part4.REVISED_AT} 修订）")
    c.font = Font(name=FONT_NAME, size=10, color=WHITE)
    c.fill = PatternFill("solid", fgColor=BLUE)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 24

    total = len(CASES)
    pri = Counter(x[7] for x in CASES)
    auto = sum(1 for x in CASES if x[10].startswith("自动化"))

    rows = [
        ("项目信息", ""),
        ("被测系统", "AI 在线考试与学情分析系统（Django 4.2.30 / DRF 3.16.1 / MySQL 5.6.46）"),
        ("测试类型", "功能测试（含接口功能、权限边界、数据一致性、页面交互走查）"),
        ("测试范围", "模块 0–10：认证、AI 适配、题库、组卷、在线考试、判分、学情、推荐、作弊检测、班级、管理端"),
        ("不在范围", "性能压测（见《性能测试报告》）、安全渗透、多浏览器兼容性矩阵、编程题与小程序"),
        ("用例总数", f"{total} 条（P0 {pri['P0']} / P1 {pri['P1']} / P2 {pri['P2']}）"),
        ("测试环境", "http://127.0.0.1:8000 ；API 前缀 /api/v1/ ；Swagger /api/docs/"),
        ("演示账号", "admin / teacher / student，密码统一 Exam@123456；学科教师 os_teacher、net_teacher、arch_teacher"),
        ("数据基线", "题库 701 道 / 20 个知识点；3 份演示卷；3 场演示考试；学生 s_001~s_045"),
        ("本次修订", f"{data_part4.VERSION}（{data_part4.REVISED_AT}）：接口自动化前走查发现 "
                     f"BUG-05/06 并回归通过；用例 {len(CASES)} 条；缺陷 "
                     f"{defect_stats()['total']} 项"
                     f"（已关闭 {defect_stats()['closed']} / 待修复 {defect_stats()['open']}）"),
        ("回归基线", data_part4.BASELINE),
        ("执行轮次", f"R1 首轮 + R2 缺陷回归 + R3 手工补齐（Selenium）："
                     f"接口/页面检查点 {exec_stats()['checks']} 个；另跑全量单测 / 冒烟 / 内联 JS"),
        ("执行结果", f"通过 {exec_stats()['passed']} 条、失败 {exec_stats()['failed']} 条、"
                     f"未执行 {exec_stats()['pending']} 条；接口/页面实测涉及 {exec_stats()['cases']} 条用例、"
                     f"{exec_stats()['checks']} 个检查点（通过 {exec_stats()['checks_ok']} / "
                     f"失败 {exec_stats()['checks_fail']}）；缺陷 {defect_stats()['total']} 项"
                     f"（已关闭 {defect_stats()['closed']}，待修复 {defect_stats()['open']}）"),
        ("执行方式", "自动化回归：python manage.py test apps --noinput + python scripts/smoke_test.py；手工项按步骤逐条执行"),
    ]
    r = 4
    for k, v in rows:
        ws.cell(row=r, column=1, value=k).font = Font(name=FONT_NAME, size=10, bold=True, color=NAVY)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=5)
        cell = ws.cell(row=r, column=2, value=v)
        cell.font = Font(name=FONT_NAME, size=10)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        for col in range(1, 6):
            ws.cell(row=r, column=col).border = BORDER
        ws.row_dimensions[r].height = 20 if len(v) < 46 else 34
        r += 1

    # 八大要素说明
    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    c = ws.cell(row=r, column=1, value="一、本表采用的「测试用例八大要素」")
    c.font = Font(name=FONT_NAME, size=12, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=BLUE)
    c.alignment = Alignment(vertical="center", indent=1)
    ws.row_dimensions[r].height = 26

    r += 1
    head = ["序号", "要素", "本表中的列", "填写要求", ""]
    for i, h in enumerate(head, start=1):
        cell = ws.cell(row=r, column=i, value=h)
        cell.font = Font(name=FONT_NAME, size=10, bold=True)
        cell.fill = PatternFill("solid", fgColor=LIGHT)
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=5)

    elements = [
        ("1", "用例编号", "用例编号", "TC-模块缩写-序号，全表唯一，便于缺陷单与报告引用"),
        ("2", "所属模块", "所属模块", "对应系统功能模块与实现代码目录，便于分派与统计"),
        ("3", "用例标题", "用例标题", "一句话说明验证点，动宾结构，不写「测试 XX」这类空话"),
        ("4", "前置条件", "前置条件", "执行前必须满足的数据、账号、状态，缺一不可复现"),
        ("5", "测试步骤", "测试步骤", "从操作者视角编号描述，可直接照做，不含实现细节"),
        ("6", "测试数据", "测试数据", "具体到字段取值与边界值，避免「输入合法数据」这类模糊表述"),
        ("7", "预期结果", "预期结果", "可判定的现象：状态码、业务码、页面文案、数据变化，与步骤一一对应"),
        ("8", "优先级", "优先级", "P0 主流程与安全红线 / P1 主要功能与边界 / P2 次要功能与体验"),
    ]
    r += 1
    for e in elements:
        for i, v in enumerate(e, start=1):
            cell = ws.cell(row=r, column=i, value=v)
            cell.font = Font(name=FONT_NAME, size=10)
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top",
                                       horizontal="center" if i == 1 else "left")
        ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=5)
        ws.row_dimensions[r].height = 32
        r += 1

    # 附加列说明
    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    c = ws.cell(row=r, column=1, value="二、附加列（不属于八大要素，供执行与追溯使用）")
    c.font = Font(name=FONT_NAME, size=12, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=BLUE)
    c.alignment = Alignment(vertical="center", indent=1)
    ws.row_dimensions[r].height = 26
    r += 1
    extra = [
        ("用例类型", "功能 / 边界 / 异常 / 权限 / 安全 / 数据一致性 / 可复现性 / 兼容性 / 易用性 / 健壮性"),
        ("需求编号", "对应《需求规格说明书》FR 编号或管理端 AC 编号，保证需求可追溯"),
        ("建议执行方式", "自动化（单测/冒烟）表示已有脚本覆盖，回归优先跑；手工表示需人工按步骤验证"),
        ("执行结果", "未执行 / 通过 / 失败 / 阻塞 / 不适用（单元格已加下拉）"),
        ("缺陷编号", "执行失败时填写缺陷单编号（模板见「4-缺陷单模板」）"),
        ("备注", "口径说明、历史缺陷、执行注意事项"),
    ]
    for k, v in extra:
        ws.cell(row=r, column=1, value=k).font = Font(name=FONT_NAME, size=10, bold=True, color=NAVY)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=5)
        cell = ws.cell(row=r, column=2, value=v)
        cell.font = Font(name=FONT_NAME, size=10)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        for col in range(1, 6):
            ws.cell(row=r, column=col).border = BORDER
        ws.row_dimensions[r].height = 20
        r += 1

    # 优先级与缺陷等级定义
    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    c = ws.cell(row=r, column=1, value="三、优先级与缺陷严重程度定义")
    c.font = Font(name=FONT_NAME, size=12, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=BLUE)
    c.alignment = Alignment(vertical="center", indent=1)
    ws.row_dimensions[r].height = 26
    r += 1
    defs = [
        ("P0", "核心主流程不可用、数据错误、答案泄漏、越权访问；一经发现即阻断发布"),
        ("P1", "主要功能异常或边界处理不当，有可用的绕过方式；发布前必须修复"),
        ("P2", "次要功能、文案与体验问题；可带风险上线，下个迭代修复"),
        ("致命", "系统崩溃、数据丢失、答案泄露、全站不可用"),
        ("严重", "主流程不可用或结果错误，无绕过方式"),
        ("一般", "局部功能异常，有绕过方式"),
        ("轻微", "文案、样式、提示不当，不影响业务结果"),
    ]
    for k, v in defs:
        cell = ws.cell(row=r, column=1, value=k)
        cell.font = Font(name=FONT_NAME, size=10, bold=True)
        cell.fill = PatternFill("solid", fgColor=GREY)
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=5)
        cell = ws.cell(row=r, column=2, value=v)
        cell.font = Font(name=FONT_NAME, size=10)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        for col in range(1, 6):
            ws.cell(row=r, column=col).border = BORDER
        ws.row_dimensions[r].height = 22
        r += 1

    # 统计摘要
    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    c = ws.cell(row=r, column=1, value="四、用例分布摘要")
    c.font = Font(name=FONT_NAME, size=12, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=BLUE)
    c.alignment = Alignment(vertical="center", indent=1)
    ws.row_dimensions[r].height = 26
    r += 1
    summary = [
        ("用例总数", f"{total} 条"),
        ("优先级分布", f"P0 {pri['P0']} 条（{pri['P0']/total:.0%}）｜P1 {pri['P1']} 条｜P2 {pri['P2']} 条"),
        ("可自动化执行", f"{auto} 条（{auto/total:.0%}）已有单测或冒烟脚本覆盖，回归时优先跑脚本"),
        ("需手工执行", f"{total-auto} 条（页面交互、AI 真实调用、降级演练等）"),
        ("模块覆盖", f"{len({x[1] for x in CASES})} 个模块分组，覆盖 11 个业务模块 + 跨模块规范"),
        ("需求覆盖", f"{len({x[9] for x in CASES})} 个需求/验收编号（明细见「3-需求覆盖」）"),
    ]
    for k, v in summary:
        ws.cell(row=r, column=1, value=k).font = Font(name=FONT_NAME, size=10, bold=True, color=NAVY)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=5)
        cell = ws.cell(row=r, column=2, value=v)
        cell.font = Font(name=FONT_NAME, size=10)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        for col in range(1, 6):
            ws.cell(row=r, column=col).border = BORDER
        ws.row_dimensions[r].height = 22
        r += 1

    ws.freeze_panes = "A4"
    return ws


# ================================================================ 用例主表
def build_cases(wb):
    ws = wb.create_sheet("1-功能测试用例")
    ws.sheet_properties.tabColor = "1F4E79"
    ncols = len(HEADERS)
    style_title(
        ws, "功能测试用例（共 %d 条）" % len(CASES), ncols,
        "编号规则 TC-模块-序号 ｜ 前 8 列为用例八大要素 ｜ 执行后在「执行结果」列填写结论",
    )

    # 表头
    for i, h in enumerate(HEADERS, start=1):
        c = ws.cell(row=3, column=i, value=h)
        c.font = Font(name=FONT_NAME, size=10, bold=True, color=WHITE)
        c.fill = PatternFill("solid", fgColor=BLUE)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
    ws.row_dimensions[3].height = 30

    for i, w in enumerate(WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    zebra = False
    prev_module = None
    for idx, case in enumerate(CASES):
        row = 4 + idx
        if case[1] != prev_module:
            prev_module = case[1]
            zebra = not zebra
        fill = PatternFill("solid", fgColor=GREY) if zebra else None
        # case 结构: 0..10 为前 11 项（含执行方式），11 为备注 → 补齐 14 列
        cid, module, title, pre, steps, data, expected, pri, ctype, fr, auto, note = case
        result, defect, exec_note = decide(case)
        note2 = f"{note}｜{exec_note}" if note else exec_note
        row_values = [cid, module, title, pre, steps, data, expected, pri, ctype, fr, auto,
                      result, defect, note2]
        for i, v in enumerate(row_values, start=1):
            cell = ws.cell(row=row, column=i, value=v)
            cell.font = Font(name=FONT_NAME, size=9)
            cell.alignment = Alignment(wrap_text=True, vertical="top",
                                       horizontal="center" if i in (1, 8, 9, 12, 13) else "left")
            cell.border = BORDER
            if fill:
                cell.fill = fill
        # 优先级配色
        pc = ws.cell(row=row, column=8)
        pc.font = Font(name=FONT_NAME, size=9, bold=True)
        if pri == "P0":
            pc.fill = PatternFill("solid", fgColor="FFC7CE")
            pc.font = Font(name=FONT_NAME, size=9, bold=True, color="9C0006")
        elif pri == "P1":
            pc.fill = PatternFill("solid", fgColor="FFEB9C")
            pc.font = Font(name=FONT_NAME, size=9, bold=True, color="9C6500")
        else:
            pc.fill = PatternFill("solid", fgColor="E2EFDA")
            pc.font = Font(name=FONT_NAME, size=9, bold=True, color="375623")
        # 执行结果配色
        rc = ws.cell(row=row, column=12)
        rc.font = Font(name=FONT_NAME, size=9, bold=True)
        if result == "失败":
            rc.fill = PatternFill("solid", fgColor="FFC7CE")
            rc.font = Font(name=FONT_NAME, size=9, bold=True, color="9C0006")
        elif result.startswith("通过"):
            rc.fill = PatternFill("solid", fgColor="C6EFCE")
            rc.font = Font(name=FONT_NAME, size=9, bold=True, color="006100")
        else:
            rc.fill = PatternFill("solid", fgColor="F2F2F2")
            rc.font = Font(name=FONT_NAME, size=9, bold=True, color="7F7F7F")

        # 用例编号加粗
        ws.cell(row=row, column=1).font = Font(name=FONT_NAME, size=9, bold=True, color=NAVY)

        lines = max(est_lines(row_values[i], WIDTHS[i]) for i in range(ncols))
        ws.row_dimensions[row].height = min(210, max(26, lines * 14 + 8))

    last = 3 + len(CASES)
    ws.auto_filter.ref = f"A3:N{last}"
    ws.freeze_panes = "D4"

    # 打印设置：横向 A3、按宽度缩放、表头每页重复
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "3:3"

    dv = DataValidation(type="list", formula1='"未执行,通过,失败,阻塞,不适用"', allow_blank=True,
                        showDropDown=False)
    dv.error = "请从下拉列表中选择执行结果"
    dv.errorTitle = "取值不合法"
    dv.prompt = "执行后填写：通过 / 失败 / 阻塞 / 不适用"
    dv.promptTitle = "执行结果"
    ws.add_data_validation(dv)
    dv.add(f"L4:L{last}")
    return ws


# ================================================================ 统计页
def build_stats(wb):
    ws = wb.create_sheet("2-用例统计")
    ws.sheet_properties.tabColor = "2E75B6"
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:G1")
    c = ws["A1"]
    c.value = "用例分布统计（按模块 × 优先级）"
    c.font = Font(name=FONT_NAME, size=13, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    head = ["所属模块", "用例数", "P0", "P1", "P2", "自动化可跑", "手工执行"]
    for i, h in enumerate(head, start=1):
        cell = ws.cell(row=3, column=i, value=h)
        cell.font = Font(name=FONT_NAME, size=10, bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER

    groups = OrderedDict()
    for case in CASES:
        groups.setdefault(case[1], []).append(case)

    r = 4
    for module, items in groups.items():
        p = Counter(x[7] for x in items)
        auto = sum(1 for x in items if x[10].startswith("自动化"))
        vals = [module, len(items), p["P0"], p["P1"], p["P2"], auto, len(items) - auto]
        for i, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=i, value=v)
            cell.font = Font(name=FONT_NAME, size=10)
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="left" if i == 1 else "center", vertical="center")
        ws.row_dimensions[r].height = 22
        r += 1

    total_row = r
    p_all = Counter(x[7] for x in CASES)
    auto_all = sum(1 for x in CASES if x[10].startswith("自动化"))
    vals = ["合计", len(CASES), p_all["P0"], p_all["P1"], p_all["P2"], auto_all, len(CASES) - auto_all]
    for i, v in enumerate(vals, start=1):
        cell = ws.cell(row=total_row, column=i, value=v)
        cell.font = Font(name=FONT_NAME, size=10, bold=True, color=NAVY)
        cell.fill = PatternFill("solid", fgColor=LIGHT)
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="left" if i == 1 else "center", vertical="center")
    ws.row_dimensions[total_row].height = 24

    for col, w in zip("ABCDEFG", [26, 10, 8, 8, 8, 14, 12]):
        ws.column_dimensions[col].width = w

    chart = BarChart()
    chart.type = "col"
    chart.style = 10
    chart.title = "各模块用例数分布"
    chart.y_axis.title = "用例数"
    chart.x_axis.title = "模块"
    data = Reference(ws, min_col=2, min_row=3, max_row=total_row - 1)
    cats = Reference(ws, min_col=1, min_row=4, max_row=total_row - 1)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    chart.legend = None
    chart.height = 9
    chart.width = 22
    ws.add_chart(chart, "I3")
    return ws


# ================================================================ 需求覆盖
def build_trace(wb):
    ws = wb.create_sheet("3-需求覆盖")
    ws.sheet_properties.tabColor = "548235"
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:D1")
    c = ws["A1"]
    c.value = "需求 → 用例追溯矩阵"
    c.font = Font(name=FONT_NAME, size=13, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    head = ["需求 / 验收编号", "覆盖用例数", "对应用例编号", "说明"]
    for i, h in enumerate(head, start=1):
        cell = ws.cell(row=3, column=i, value=h)
        cell.font = Font(name=FONT_NAME, size=10, bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER

    desc = {
        "FR-1": "用户与认证：注册 / 三种账号登录 / JWT / 改密 / 用户管理",
        "FR-2": "AI 适配层：多平台封装 / 降级 mock / 日志与用量",
        "FR-3": "知识点：树结构 / 环保护 / 按知识点查题",
        "FR-4": "题库：六题型 / 筛选 / AI 出题 / 导入导出 / 软删除",
        "FR-5": "试卷与组卷：三层结构 / 快照 / AI 组卷 / 质量分析",
        "FR-6": "在线考试：计时 / 续考 / 乱序 / 防泄漏 / 行为上报",
        "FR-7": "判分与成绩：客观判分 / 多选策略 / AI 批改 / 复核 / 可见性",
        "FR-8": "学情分析：掌握度归并 / 薄弱门槛 / 趋势 / 班级分析 / AI 诊断",
        "FR-9": "个性化推荐：薄弱匹配 / 协同过滤 / 每日练习 / AI 改写理由",
        "FR-10": "作弊检测：埋点 / 规则引擎 / 孤立森林 / 证据链",
        "NFR-1": "非功能：RESTful 语义",
        "NFR-2": "非功能：统一响应体与错误码",
        "NFR-4": "非功能：安全（答案不泄漏、跨人 404、JWT）",
        "NFR-5": "非功能：健壮性（AI 降级不抛异常）",
        "NFR-6": "非功能：参数校验 400 不 500",
        "NFR-7": "非功能：可复现（种子决定随机）",
        "NFR-9": "非功能：分页与避免 N+1",
        "NFR-10": "非功能：MySQL 5.6 兼容 / 字符集",
        "模块9": "班级管理：建班 / 成员 / 挂靠 / 权限 / 冲突拦截",
        "模块9增强": "学科隔离",
        "10U 随机组卷": "按库存自动生成蓝图、不调 AI、seed 可复现",
        "10W 学号派发": "注册按年级派学号、教师派工号、个人不可改编号",
        "10R/10T 学情权限与下钻": "教师只能看自己班学生；班级维度默认收窄；下钻链接按权限渲染",
        "10P/10Q 题库导入": "导入去重（skipped 带行号）与导入弹窗交互",
        "10V 批改工作台": "左侧待批队列滚动固定",
        "10B/10I/10K/10L/10M/10N/10O": "班级与练习的增量修复项",
        "管理端与班级增量": "管理端验收标准（AC）+ 班级与练习的增量修复（10B/10I/10K/10L/10M/10N/10O）",
        "AC-04/05/06/07/08/10/11/12/14/16/18/19/20/21/24/26/27/35": "管理端验收标准",
    }

    def bucket(fr):
        if fr.startswith("FR-"):
            parts = fr.split("-")
            return "-".join(parts[:2]) if len(parts) >= 2 else fr
        if fr.startswith("NFR-"):
            return fr
        if fr == "10U":
            return "10U 随机组卷"
        if fr == "10W":
            return "10W 学号派发"
        if fr in ("10R/10T", "10T", "10R"):
            return "10R/10T 学情权限与下钻"
        if fr in ("10P", "10Q"):
            return "10P/10Q 题库导入"
        if fr == "10V":
            return "10V 批改工作台"
        if fr.startswith("AC-") or fr.startswith("10"):
            return "管理端与班级增量"
        return fr

    buckets = defaultdict(list)
    for case in CASES:
        buckets[bucket(case[9])].append(case[0])

    order = ["FR-1", "FR-2", "FR-3", "FR-4", "FR-5", "FR-6", "FR-7", "FR-8", "FR-9", "FR-10",
             "NFR-1", "NFR-2", "NFR-4", "NFR-5", "NFR-6", "NFR-7", "NFR-9", "NFR-10",
             "10U 随机组卷", "10W 学号派发", "10R/10T 学情权限与下钻", "10P/10Q 题库导入",
             "10V 批改工作台", "模块9", "管理端与班级增量"]
    keys = [k for k in order if k in buckets] + [k for k in buckets if k not in order]

    r = 4
    for k in keys:
        ids = buckets[k]
        shown = "、".join(ids[:10]) + ("…（共 %d 条）" % len(ids) if len(ids) > 10 else "")
        vals = [k, len(ids), shown, desc.get(k, "功能测试覆盖，详见对应用例")]
        for i, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=i, value=v)
            cell.font = Font(name=FONT_NAME, size=9)
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top",
                                       horizontal="center" if i in (1, 2) else "left")
        ws.row_dimensions[r].height = 30 if len(shown) < 90 else 44
        r += 1

    for col, w in zip("ABCD", [22, 12, 62, 46]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A4"
    return ws


# ================================================================ 缺陷单模板
def build_defect(wb):
    ws = wb.create_sheet("4-缺陷单模板")
    ws.sheet_properties.tabColor = "C00000"
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:D1")
    c = ws["A1"]
    c.value = "缺陷单模板（执行失败时按此登记，并回填用例表的「缺陷编号」）"
    c.font = Font(name=FONT_NAME, size=13, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    fields = [
        ("缺陷编号", "BUG-功能-001", "模块缩写 + 三位序号，全局唯一"),
        ("缺陷标题", "答题页刷新后切屏次数重置为 0", "一句话说清「在哪、什么条件下、错成什么样」"),
        ("关联用例", "TC-UI-03", "对应本表的用例编号，便于回归"),
        ("所属模块", "在线考试（模块4）", "与用例表「所属模块」一致"),
        ("环境/版本", "MySQL 5.6 + Django 4.2.30；commit 7a1f2c；Chrome 129", "环境、浏览器、代码版本三要素"),
        ("复现步骤", "1) student 登录进入进行中的考试\n2) 切到其他标签页 3 秒后返回\n3) 刷新页面",
         "从零开始可复现，含账号与数据前提"),
        ("预期结果", "刷新后切屏次数仍为 1（接口 behavior 回显）", "引用需求或用例的预期"),
        ("实际结果", "切屏次数显示 0，与教师端落库值不一致", "如实描述现象，附关键日志"),
        ("严重程度", "一般", "致命 / 严重 / 一般 / 轻微"),
        ("优先级", "P1", "修复紧急度，与用例优先级区分"),
        ("影响范围", "所有开启切屏检测的考试；影响教师端可疑判定", "影响用户、模块与数据范围"),
        ("复现率", "3/3（必现）", "必现 / 偶现（x/n），偶现需写触发条件"),
        ("附件", "console.log、network.har、截图 20261030-01.png", "日志 / 截图 / 录屏，缺证据不结论"),
        ("状态", "新建", "新建 / 已确认 / 修复中 / 待验证 / 已关闭 / 不予修复"),
        ("处理人 / 验证人", "开发：张三 ｜ 测试：李四", "明确责任人与验证人"),
    ]

    ws.merge_cells("A3:A3")
    head = ["字段", "示例值", "填写要求", "实际填写"]
    for i, h in enumerate(head, start=1):
        cell = ws.cell(row=3, column=i, value=h)
        cell.font = Font(name=FONT_NAME, size=10, bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER

    r = 4
    for name, sample, rule in fields:
        ws.cell(row=r, column=1, value=name).font = Font(name=FONT_NAME, size=9, bold=True)
        ws.cell(row=r, column=2, value=sample).font = Font(name=FONT_NAME, size=9)
        ws.cell(row=r, column=3, value=rule).font = Font(name=FONT_NAME, size=9)
        for i in range(1, 5):
            cell = ws.cell(row=r, column=i)
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 34 if name in ("复现步骤", "预期结果", "实际结果", "附件") else 24
        r += 1

    for col, w in zip("ABCD", [18, 52, 40, 34]):
        ws.column_dimensions[col].width = w
    return ws


# ================================================================ 本次修订
def build_revision(wb):
    ws = wb.create_sheet("6-本次修订")
    ws.sheet_properties.tabColor = "BF8F00"
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:E1")
    c = ws["A1"]
    c.value = f"{data_part4.VERSION} 修订记录（{data_part4.REVISED_AT}）"
    c.font = Font(name=FONT_NAME, size=13, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    info = [
        ("修订依据", "增量改动 10P–10Y + 建学生年级必填（R8）+ 需求规格 FR-5-5 / FR-6-10 / NFR-12 + 权限矩阵"),
        ("回归基线", data_part4.BASELINE),
        ("修订范围", "v1.1 新增 18 条、修改 8 条；v1.2 新增 3 条、调整 6 条追溯；"
                     "v1.3 调整 13 条需求编号；v1.4 重写 2 条（管理端建号）；"
                     "v1.5 回填执行结果；v1.6 关闭 3 个缺陷并统一口径；用例总数 240 条"),
        ("重点变化", "学号派发与年级必填（10W/10Y/R8）、随机组卷（FR-5-5）、学情权限收紧（10S/10T）、"
                     "「面向全体」范围收敛（FR-6-10）"),
        ("执行统计", f"接口检查点 {exec_stats()['checks']} 个：通过 {exec_stats()['checks_ok']} / "
                     f"失败 {exec_stats()['checks_fail']}；涉及用例 {exec_stats()['cases']} 条；"
                     f"全表通过 {exec_stats()['passed']} / 失败 {exec_stats()['failed']} / "
                     f"未执行 {exec_stats()['pending']}"),
    ]
    r = 3
    for k, v in info:
        ws.cell(row=r, column=1, value=k).font = Font(name=FONT_NAME, size=10, bold=True, color=NAVY)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=5)
        cell = ws.cell(row=r, column=2, value=v)
        cell.font = Font(name=FONT_NAME, size=10)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        for col in range(1, 6):
            ws.cell(row=r, column=col).border = BORDER
        ws.row_dimensions[r].height = 24
        r += 1

    r += 1
    head = ["序号", "变更点", "来源", "影响用例", "处理动作"]
    for i, h in enumerate(head, start=1):
        cell = ws.cell(row=r, column=i, value=h)
        cell.font = Font(name=FONT_NAME, size=10, bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
    r += 1

    for idx, (point, source, cases, action) in enumerate(data_part4.CHANGES, start=1):
        vals = [idx, point, source, cases, action]
        for i, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=i, value=v)
            cell.font = Font(name=FONT_NAME, size=9)
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top",
                                       horizontal="center" if i in (1, 3) else "left")
        ws.row_dimensions[r].height = 34 if len(point) < 42 else 46
        r += 1

    for col, w in zip("ABCDE", [6, 62, 24, 46, 44]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A3"
    return ws


# ================================================================ 风险清单
def build_risks(wb):
    ws = wb.create_sheet("7-风险与待确认")
    ws.sheet_properties.tabColor = "C00000"
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:F1")
    c = ws["A1"]
    c.value = "风险与待确认清单（随需求文档同步更新，执行前先看这里）"
    c.font = Font(name=FONT_NAME, size=13, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor="C00000")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    head = ["编号", "风险 / 待确认项", "影响面", "建议动作", "级别", "状态"]
    for i, h in enumerate(head, start=1):
        cell = ws.cell(row=3, column=i, value=h)
        cell.font = Font(name=FONT_NAME, size=10, bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER

    r = 4
    level_fill = {"高": "FFC7CE", "中": "FFEB9C", "低": "E2EFDA"}
    for rid, risk, impact, action, level, status in data_part4.RISKS:
        vals = [rid, risk, impact, action, level, status]
        for i, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=i, value=v)
            cell.font = Font(name=FONT_NAME, size=9)
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top",
                                       horizontal="center" if i in (1, 5, 6) else "left")
        ws.cell(row=r, column=5).fill = PatternFill("solid", fgColor=level_fill.get(level, "FFFFFF"))
        ws.cell(row=r, column=5).font = Font(name=FONT_NAME, size=9, bold=True)
        if status.startswith("已关闭"):
            ws.cell(row=r, column=6).fill = PatternFill("solid", fgColor="E2EFDA")
        else:
            ws.cell(row=r, column=6).fill = PatternFill("solid", fgColor="FCE4D6")
        ws.cell(row=r, column=6).font = Font(name=FONT_NAME, size=9, bold=True)
        ws.row_dimensions[r].height = 56 if len(risk) > 45 else 44
        r += 1

    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    cell = ws.cell(row=r, column=1,
                   value="结论：R1–R8 全部关闭（需求补齐 / 代码修复 / 文档化），当前无开放风险，"
                         "本表可直接进入执行阶段。执行前按部署文档确认 papers.0002 已应用，"
                         f"并先跑一遍自动化基线（{data_part4.BASELINE}）。")
    cell.font = Font(name=FONT_NAME, size=9, color="595959")
    cell.alignment = Alignment(wrap_text=True, vertical="center", indent=1)
    ws.row_dimensions[r].height = 36

    for col, w in zip("ABCDEF", [8, 46, 50, 44, 8, 18]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A4"
    return ws


# ================================================================ 缺陷清单
def build_defects(wb):
    ws = wb.create_sheet("8-缺陷与观察项")
    ws.sheet_properties.tabColor = "9C0006"
    ws.sheet_view.showGridLines = False
    headers = ["编号", "类型", "标题", "关联用例", "严重程度", "优先级",
               "复现步骤", "预期结果", "实际结果", "影响范围", "复现率", "状态", "建议动作", "修复验证"]
    n = len(headers)

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    c = ws.cell(row=1, column=1, value=f"执行轮次 R1 缺陷与观察项（{data_part5.EXECUTED_AT}）")
    c.font = Font(name=FONT_NAME, size=13, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=n)
    bugs = [d for d in data_part5.DEFECTS if d[1] == "缺陷"]
    closed = sum(1 for d in bugs if d[11] == "已关闭")
    obs = sum(1 for d in data_part5.DEFECTS if d[1] == "观察项")
    c2 = ws.cell(row=2, column=1,
                 value=f"环境：{data_part5.ENV}　｜　缺陷 {len(bugs)} 项（已关闭 {closed}，"
                       f"待修复 {len(bugs) - closed}）+ 观察项 {obs} 项；修复验证见最后一列")
    c2.font = Font(name=FONT_NAME, size=9, color="595959")
    c2.alignment = Alignment(vertical="center", indent=1)
    ws.row_dimensions[2].height = 22

    for i, h in enumerate(headers, start=1):
        cell = ws.cell(row=3, column=i, value=h)
        cell.font = Font(name=FONT_NAME, size=9, bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[3].height = 28

    r = 4
    for row in data_part5.DEFECTS:
        for i, v in enumerate(row, start=1):
            cell = ws.cell(row=r, column=i, value=v)
            cell.font = Font(name=FONT_NAME, size=9)
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top",
                                       horizontal="center" if i in (1, 2, 5, 6, 11, 12) else "left")
        kind = row[1]
        sev = row[4]
        mark = ws.cell(row=r, column=2)
        mark.font = Font(name=FONT_NAME, size=9, bold=True)
        status = ws.cell(row=r, column=12)
        status.font = Font(name=FONT_NAME, size=9, bold=True)
        if status.value == "已关闭":
            status.fill = PatternFill("solid", fgColor="C6EFCE")
        elif kind == "缺陷":
            status.fill = PatternFill("solid", fgColor="FFC7CE")
        if kind == "缺陷":
            mark.fill = PatternFill("solid", fgColor="FFC7CE" if sev in ("致命", "严重") else "FFEB9C")
        else:
            mark.fill = PatternFill("solid", fgColor="DDEBF7")
        ws.row_dimensions[r].height = 104
        r += 1

    for col, w in zip("ABCDEFGHIJKLMN", [9, 9, 34, 12, 9, 7, 50, 34, 40, 38, 10, 8, 34, 44]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "D4"
    return ws


# ================================================================ 执行汇总
def build_exec(wb):
    ws = wb.create_sheet("5-执行汇总")
    ws.sheet_properties.tabColor = "7F7F7F"
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:H1")
    c = ws["A1"]
    c.value = "执行汇总（每轮执行后填写，用于测试报告取数）"
    c.font = Font(name=FONT_NAME, size=13, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    head = ["所属模块", "计划用例", "已执行", "通过", "失败", "阻塞", "通过率", "备注"]
    for i, h in enumerate(head, start=1):
        cell = ws.cell(row=3, column=i, value=h)
        cell.font = Font(name=FONT_NAME, size=10, bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER

    groups = OrderedDict()
    for case in CASES:
        groups.setdefault(case[1], []).append(case)

    r = 4
    for module, items in groups.items():
        passed = failed = pending = 0
        for c in items:
            res = decide(c)[0]
            if res == "失败":
                failed += 1
            elif res.startswith("通过"):
                passed += 1
            else:
                pending += 1
        executed = passed + failed
        ws.cell(row=r, column=1, value=module).font = Font(name=FONT_NAME, size=10)
        for col, val in zip(range(2, 7), [len(items), executed, passed, failed, 0]):
            ws.cell(row=r, column=col, value=val)
        ws.cell(row=r, column=7, value=f"=IF(C{r}=0,\"—\",D{r}/C{r})").number_format = "0.0%"
        for col in range(1, 9):
            cell = ws.cell(row=r, column=col)
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="left" if col == 1 else "center", vertical="center")
        r += 1

    total_row = r
    ws.cell(row=total_row, column=1, value="合计")
    for col, letter in ((2, "B"), (3, "C"), (4, "D"), (5, "E"), (6, "F")):
        ws.cell(row=total_row, column=col, value=f"=SUM({letter}4:{letter}{total_row-1})")
    ws.cell(row=total_row, column=7, value=f"=IF(C{total_row}=0,\"—\",D{total_row}/C{total_row})").number_format = "0.0%"
    for col in range(1, 9):
        cell = ws.cell(row=total_row, column=col)
        cell.font = Font(name=FONT_NAME, size=10, bold=True, color=NAVY)
        cell.fill = PatternFill("solid", fgColor=LIGHT)
        cell.border = BORDER

    r = total_row + 2
    notes = [
        "取值说明：已执行 = 通过 + 失败 + 阻塞；通过率 = 通过 / 已执行。",
        "自动化回归建议命令：python manage.py test apps --noinput ｜ python scripts/smoke_test.py（需先起服务）。",
        "手工执行前先跑一遍自动化回归，避免把环境问题误记为功能缺陷。",
        "准入标准建议：P0 用例 100% 通过、P1 通过率 ≥95%、无遗留致命/严重缺陷方可发布。",
    ]
    for n in notes:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
        cell = ws.cell(row=r, column=1, value="• " + n)
        cell.font = Font(name=FONT_NAME, size=9, color="595959")
        cell.alignment = Alignment(vertical="center", indent=1)
        ws.row_dimensions[r].height = 20
        r += 1

    for col, w in zip("ABCDEFGH", [26, 12, 10, 10, 10, 10, 10, 30]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A4"
    return ws


def main():
    # 唯一性自检
    ids = [c[0] for c in CASES]
    dup = [k for k, v in Counter(ids).items() if v > 1]
    assert not dup, f"用例编号重复：{dup}"
    assert all(len(c) == 12 for c in CASES), "用例字段数不正确"

    wb = Workbook()
    build_cover(wb)
    build_cases(wb)
    build_stats(wb)
    build_trace(wb)
    build_defect(wb)
    build_exec(wb)
    build_revision(wb)
    build_risks(wb)
    build_defects(wb)
    wb.save(OUT_PATH)
    print(f"已生成：{os.path.abspath(OUT_PATH)}")
    print(f"用例总数：{len(CASES)}；模块分组：{len({c[1] for c in CASES})}")
    print("优先级分布：", dict(Counter(c[7] for c in CASES)))


if __name__ == "__main__":
    main()
