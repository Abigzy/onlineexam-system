# -*- coding: utf-8 -*-
"""生成《测试用例.md》——设计说明 + 条数统计 + 240 条用例详情（八大要素）。

用法：
    .venv\\Scripts\\python.exe -m scripts.testcase_gen.build_cases_md

产出：docs/测试用例.md

数据源与 Excel 完全相同（data_part1/2/3 的 CASES），所以两边条数、编号、
优先级永远一致；Excel 里多出来的「用例类型 / 需求编号 / 自动化方式 / 执行结果 / 备注」
不进本文件（按用户要求，只保留八大要素）。
"""

from __future__ import annotations

import collections
import io
import os
import re

from scripts.testcase_gen import data_part1, data_part2, data_part3, data_part4

OUT_PATH = os.path.join("docs", "测试用例.md")

CASES = data_part1.CASES + data_part2.CASES + data_part3.CASES

# 《需求分析与测试点》导图的测试点规模（导图由 build_xmind 生成，改导图时同步这两个数）
XMIND_POS = 121
XMIND_NEG = 202

# 八大要素在元组里的下标
IDX = {
    "id": 0, "module": 1, "title": 2, "pre": 3,
    "steps": 4, "data": 5, "expected": 6, "pri": 7,
}

ELEMENTS = [
    ("用例编号", "`TC-<模块>-<序号>`，模块取两位模块号；同一场景的补充用例加字母后缀（如 TC-AUTH-08B）。"
                 "编号全局唯一、不复用，删除用例后编号作废。"),
    ("所属模块", "用例归属的业务模块（15 个分组，对应需求规格的模块划分），用于按模块统计与分配执行人。"),
    ("用例标题", "一句话说清「验证什么」，采用「动作 + 对象 + 关键条件」写法，不写步骤、不写结论。"),
    ("前置条件", "执行前必须成立的环境与数据状态（登录身份、已有数据、配置开关）。"
                 "达不到前置条件时用例不可判定，需先造数。"),
    ("测试步骤", "从用户或调用方视角的可执行动作序列，用 1) 2) 3) 编号；一步只做一件事，"
                 "关键步骤标注预期观察点。"),
    ("测试数据", "步骤中用到的具体值（账号、id、参数、边界值）。禁止写「任意合法值」这类不可复现的描述。"),
    ("预期结果", "与步骤编号一一对应的可判定结论，必须能回答「通过 / 不通过」，"
                 "包含状态码、业务码、页面表现或数据变化。"),
    ("优先级", "P0 核心主流程 / P1 重要分支与异常 / P2 次要场景与体验。定义见 §1.5。"),
]

METHODS = [
    ("输入域类需求", "等价类划分 + 边界值分析",
     "分值 0~100、时长 1~600 分钟、用户名 ≥3 位、题量 1~200",
     "每个输入至少 1 个有效类 + 2 个无效类，并取 min-1/min/max/max+1"),
    ("多条件组合", "判定表 / 因果图",
     "角色 × 资源归属 × 资源状态；open_to_all × 创建者 × 学生班级",
     "列出条件桩与动作桩，逐列覆盖，避免漏掉「有权但状态不允许」的组合"),
    ("业务流程", "场景法",
     "注册→建卷→发布→开考→交卷→判分→出分→看学情",
     "基本流 1 条 + 备选流若干 + 异常流（断网、超时、AI 失败、并发）"),
    ("状态对象", "状态迁移法",
     "试卷 draft→published→archived；作答 in_progress→submitted→graded",
     "合法迁移逐条验证，非法迁移必须被拒（终态不回退）"),
    ("多参数组合", "正交实验",
     "浏览器 × 分辨率 × 角色",
     "用正交表压缩到 9 组以内，保证两两组合覆盖"),
    ("数据一致性", "交叉校验",
     "总分 = Σ 题分；已交 + 未交 = 应考；同一数字多入口",
     "同一业务数字从多个入口取值比对，不一致即为缺陷"),
    ("安全与权限", "白名单 + 反向验证",
     "学生视角字段、跨人访问、越权写操作",
     "先断言「不该有的字段不存在」，再构造绕过页面直调接口的请求"),
]


def cell(text: str) -> str:
    """表格单元格转义：竖线转义、换行转 <br>。"""
    return str(text).replace("|", r"\|").replace("\n", "<br>").strip()


def top_fr(fr: str) -> str:
    m = re.match(r"(FR-\d+|NFR-\d+)", fr)
    if m:
        return m.group(1)
    if fr.startswith("AC-"):
        return "AC（验收条件）"
    if fr.startswith("10"):
        return "10x 增量项"
    return "模块9 班级"


def sort_key(name: str):
    head = name.split("-")[0]
    num = int(re.sub(r"\D", "", name) or 0)
    order = {"FR": 0, "NFR": 1, "AC（验收条件）": 2, "10x 增量项": 3, "模块9 班级": 4}
    return (order.get(head, 9), num)


def fmt_pct(a: int, b: int) -> str:
    return f"{a / b * 100:.1f}%" if b else "—"


# ---------------------------------------------------------------- 各章节
def sec_header() -> list[str]:
    return [
        "# 测试用例",
        "",
        "> **被测系统**：AI 在线考试与学情分析系统（Django 4.2 + DRF + MySQL 5.6）",
        f"> **用例集版本**：{data_part4.VERSION}（{data_part4.REVISED_AT}），**{len(CASES)} 条**；"
        "与同批交付的 Excel 版 `OnlineExam-功能测试用例.xlsx` 同源同编号",
        f"> **本文档内容**：① 测试用例设计说明　② 用例条数统计　③ {len(CASES)} 条用例详情（仅八大要素）",
        "> **生成方式**：`python -m scripts.testcase_gen.build_cases_md`"
        "（数据与 Excel 共用同一份用例源，条数与编号不会漂移）",
        "",
        "---",
        "",
    ]


def sec_design() -> list[str]:
    out = ["## 1. 测试用例设计说明", ""]

    out += [
        "### 1.1 编写说明",
        "",
        "本测试用例文档根据当前《需求分析与测试点》思维导图整理而成：先按模块把导图中的"
        "**正向测试点（应成功）**与**反向测试点（应被拒）**展开，再逐条补齐前置条件、测试步骤、"
        "测试数据与预期结果，形成可执行、可判定的用例，共 "
        f"**{len(CASES)} 条**（对应导图中 {XMIND_POS} 个正向测试点与 {XMIND_NEG} 个反向测试点）。",
        "",
        "覆盖范围与导图一一对应，包含：登录页面、注册页面（含学号派发）、个人资料、"
        "权限与越权、AI 适配层、知识点、题库管理（含导入与答案防泄漏）、试卷与组卷"
        "（含随机组卷）、在线考试（开考 / 计时 / 作答 / 交卷 / 防作弊）、判分与成绩"
        "（含成绩单与错题本）、学情分析、个性化推荐、作弊行为检测、班级管理、管理端、"
        "页面与交互，以及性能 / 安全 / 兼容性 / 可靠性等非功能项。",
        "",
        "> 导图中的每个功能点在本文档中都有对应表格行；导图偏「验证什么」，本文档补全"
        "「怎么验、用什么数据、预期是什么」。",
        "",
        "### 1.2 用例八大要素及其写法要求",
        "",
        "| 要素 | 写法要求 |",
        "| --- | --- |",
    ]
    for name, desc in ELEMENTS:
        out.append(f"| **{name}** | {desc} |")

    out += [
        "",
        "> 用例类型、需求编号、自动化方式、执行结果、缺陷编号、备注这 6 列保留在 Excel 中，"
        "用于追溯与执行登记，不重复放进本文档。",
        "",
        "### 1.3 编号规则",
        "",
        "`TC-<模块>-<序号>`，例如 `TC-AUTH-01`、`TC-EX-25`、`TC-AN-16`。同一场景下的补充用例"
        "加字母后缀（`TC-AUTH-08B` 表示「学号派发」的第二个子检查点）。编号一经分配不复用，"
        "删除的用例编号作废，避免历史测试报告指向错误的用例。",
        "",
        "### 1.4 设计方法（按需求类型选）",
        "",
        "| 需求类型 | 使用方法 | 本项目的落点举例 | 覆盖要求 |",
        "| --- | --- | --- | --- |",
    ]
    for row in METHODS:
        out.append("| " + " | ".join(cell(x) for x in row) + " |")

    out += [
        "",
        "### 1.5 优先级定义与覆盖策略",
        "",
        "| 优先级 | 定义 | 执行要求 |",
        "| --- | --- | --- |",
        "| **P0** | 核心主流程，挂了系统不可用：注册登录、开考交卷、判分出分、权限边界 | 每次回归必跑，不允许带缺陷上线 |",
        "| **P1** | 重要分支与异常：状态非法迁移、越权、边界值、AI 降级 | 每轮回归执行 |",
        "| **P2** | 次要场景与体验：文案一致性、响应式、空状态、极端边界 | 版本发布前抽检或全量 |",
        "",
        "覆盖策略：每个业务模块至少 1 条 P0；每条一级需求（FR-n）至少 1 条 P0；"
        "反向用例（异常 / 边界 / 权限 / 安全）占比不低于 30%，当前为 "
        f"**{fmt_pct(sum(1 for c in CASES if c[8] in ('异常', '边界', '权限', '安全')), len(CASES))}**。"
        "反向用例优先于正向被覆盖——线上事故多数来自没人试过的非法输入。",
        "",
        "### 1.6 测试数据与环境",
        "",
        "| 项 | 说明 |",
        "| --- | --- |",
        "| 应用 | `runserver 127.0.0.1:8777`（development 配置） |",
        "| 数据库 | MySQL 5.6.46，库 `online_exam`，字符集 utf8 |",
        "| 演示账号 | admin / teacher / 学科教师，学生 s_001~s_045；口令统一 `Exam@123456` |",
        "| 数据基线 | 题库 701 道 / 20 个知识点；3 份演示卷；3 场演示考试 |",
        "| 造数规则 | 临时数据统一加 `TC执行-` 前缀，用例执行后由脚本清理，不污染演示库 |",
        "| AI 配置 | 手工验证用真实 DeepSeek；批量回归强制 mock，保证断言稳定 |",
        "",
        "### 1.7 执行方式",
        "",
        "| 执行方式 | 覆盖用例数 | 说明 |",
        "| --- | --- | --- |",
        f"| 自动化（单测 / 冒烟 / 接口脚本） | {sum(1 for c in CASES if c[10].startswith('自动化'))} | "
        "`manage.py test apps`、`scripts/smoke_test.py`、`exec_live.py` 等 |",
        f"| 手工（浏览器 / 真实大模型） | {sum(1 for c in CASES if not c[10].startswith('自动化'))} | "
        "Selenium 无头浏览器可代跑一部分 UI 项，AI 真实调用需人工确认 |",
        "",
        "### 1.8 准出标准",
        "",
        "1. P0 用例通过率 100%，P1 通过率 ≥95%，P2 允许有记录的遗留项；",
        "2. 无致命、严重级别缺陷；一般缺陷要么修复要么有明确的风险接受记录；",
        "3. 自动化基线全绿：单测全过、冒烟 0 失败、内联 JS 0 失败、迁移无未生成变更；",
        "4. 需求覆盖矩阵无空洞（每条 FR 至少 1 条用例）。",
        "",
        "### 1.9 已知局限",
        "",
        "- 兼容性只覆盖 Chrome 桌面端（1280 / 1440），Firefox 与移动端 375px 未系统验证；",
        "- AI 输出的稳定性靠 mock + 阈值断言保证，真实模型的耗时与降级率只有抽样数据；",
        "- 并发场景只覆盖注册撞号、并发交卷等关键点，未做全链路压测（见性能测试报告）。",
        "",
        "---",
        "",
    ]
    return out


def sec_stats() -> list[str]:
    total = len(CASES)
    nfr_n = len({m.group(1) for c in CASES
                 if (m := re.match(r"(NFR-\d+)", c[9]))})
    pri = collections.Counter(c[7] for c in CASES)
    ctype = collections.Counter(c[8] for c in CASES)
    auto = sum(1 for c in CASES if c[10].startswith("自动化"))

    out = ["## 2. 测试用例条数统计", "", "### 2.1 总体", ""]
    out += [
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 用例总数 | **{total}** |",
        f"| 覆盖业务模块 | {len(dict.fromkeys(c[1] for c in CASES))} 个 |",
        f"| P0 / P1 / P2 | {pri['P0']} / {pri['P1']} / {pri['P2']} |",
        f"| 自动化 / 手工 | {auto} / {total - auto}（自动化占 {fmt_pct(auto, total)}） |",
        f"| 反向类用例（异常+边界+权限+安全） | "
        f"{sum(v for k, v in ctype.items() if k in ('异常', '边界', '权限', '安全'))}"
        f"（{fmt_pct(sum(v for k, v in ctype.items() if k in ('异常', '边界', '权限', '安全')), total)}） |",
        f"| 覆盖的一级需求 | FR-1~FR-10 全覆盖，NFR {nfr_n} 项 |",
        "",
        "### 2.2 按模块统计",
        "",
        "| 序号 | 所属模块 | 用例数 | P0 | P1 | P2 | 自动化 | 手工 | 占比 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    groups = collections.OrderedDict()
    for c in CASES:
        groups.setdefault(c[1], []).append(c)

    for i, (mod, items) in enumerate(groups.items(), start=1):
        p = collections.Counter(x[7] for x in items)
        a = sum(1 for x in items if x[10].startswith("自动化"))
        out.append(
            f"| {i} | {cell(mod)} | {len(items)} | {p['P0']} | {p['P1']} | {p['P2']} | "
            f"{a} | {len(items) - a} | {fmt_pct(len(items), total)} |"
        )
    out.append(f"| — | **合计** | **{total}** | **{pri['P0']}** | **{pri['P1']}** | "
               f"**{pri['P2']}** | **{auto}** | **{total - auto}** | **100%** |")

    out += [
        "",
        "### 2.3 按用例类型统计",
        "",
        "| 用例类型 | 条数 | 占比 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    type_desc = {
        "功能": "正常路径与业务规则",
        "异常": "非法输入、错误操作、失败分支",
        "权限": "角色越权、跨用户访问",
        "边界": "上下限与临界值",
        "数据一致性": "多入口同源数据比对",
        "安全": "答案防泄漏、越权、注入类",
        "可复现性": "随机种子与顺序稳定性",
        "易用性": "二次确认、防重复提交",
        "兼容性": "浏览器与分辨率",
        "健壮性": "降级与容错",
    }
    for k, v in ctype.most_common():
        out.append(f"| {k} | {v} | {fmt_pct(v, total)} | {type_desc.get(k, '')} |")
    out.append(f"| **合计** | **{total}** | **100%** | — |")

    out += [
        "",
        "### 2.4 按需求覆盖统计",
        "",
        "| 需求编号 | 用例数 | 其中 P0 | 覆盖判断 |",
        "| --- | --- | --- | --- |",
    ]
    byfr = collections.defaultdict(lambda: [0, 0])
    for c in CASES:
        k = top_fr(c[9])
        byfr[k][0] += 1
        if c[7] == "P0":
            byfr[k][1] += 1
    for k in sorted(byfr, key=sort_key):
        n, p0 = byfr[k]
        verdict = "P0 覆盖" if p0 else "仅 P1/P2 覆盖"
        out.append(f"| {k} | {n} | {p0} | {verdict} |")

    out += [
        "",
        "> FR-1 ~ FR-10 每条都有 P0 用例；NFR-9（可维护性）与 NFR-10 各 1 条、为 P1/P2，"
        "属非功能项，不设 P0。",
        "",
        "---",
        "",
    ]
    return out


def sec_details() -> list[str]:
    out = [
        f"## 3. 用例详情（{len(CASES)} 条）",
        "",
        "按模块分组，每条只保留八大要素。表格内的 `<br>` 表示原用例的分行。",
        "",
    ]
    groups = collections.OrderedDict()
    for c in CASES:
        groups.setdefault(c[1], []).append(c)

    for idx, (mod, items) in enumerate(groups.items(), start=1):
        out.append(f"### 3.{idx} {mod}（{len(items)} 条）")
        out.append("")
        out.append("| 用例编号 | 所属模块 | 用例标题 | 前置条件 | 测试步骤 | 测试数据 | 预期结果 | 优先级 |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for c in items:
            row = [
                f"`{c[IDX['id']]}`",
                cell(c[IDX["module"]]),
                cell(c[IDX["title"]]),
                cell(c[IDX["pre"]]),
                cell(c[IDX["steps"]]),
                cell(c[IDX["data"]]),
                cell(c[IDX["expected"]]),
                c[IDX["pri"]],
            ]
            out.append("| " + " | ".join(row) + " |")
        out.append("")
    return out


def main() -> None:
    lines = []
    lines += sec_header()
    lines += sec_design()
    lines += sec_stats()
    lines += sec_details()
    content = "\n".join(lines).rstrip() + "\n"

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with io.open(OUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)

    pri = collections.Counter(c[7] for c in CASES)
    print(f"已生成：{os.path.abspath(OUT_PATH)}")
    print(f"用例 {len(CASES)} 条（P0 {pri['P0']} / P1 {pri['P1']} / P2 {pri['P2']}）；"
          f"行数 {len(lines)}；大小 {os.path.getsize(OUT_PATH) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
