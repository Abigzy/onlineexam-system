# -*- coding: utf-8 -*-
"""生成《需求分析与测试点》思维导图（.xmind，XMind ZEN 2020+ / XMind 8 双格式）。

用法：
    .venv\\Scripts\\python.exe -m scripts.testcase_gen.build_xmind

产出：docs/需求分析与测试点.xmind

结构约定（v2，按用户要求改版）：
- 不写方法论，只写测试点；
- 每个功能点下固定分「正向 / 反向」两支：
    正向 = 合法输入 / 正常顺序 → 应成功（20x）
    反向 = 非法输入、缺失、越权、越界、状态不允许 → 应被拒（400/401/403/404/409 或业务码）
- 叶子统一写成「条件 → 预期」，保证可直接判定，不需要再翻译成用例步骤。
"""

from __future__ import annotations

import json
import os
import zipfile
from xml.sax.saxutils import escape

OUT_PATH = os.path.join("docs", "需求分析与测试点.xmind")
SHEET_TITLE = "测试点清单（正向 / 反向）"
ROOT_TITLE = "AI 在线考试与学情分析系统\n测试点清单 · 正向（应成功）× 反向（应被拒）"


def P(*items: str) -> tuple:
    """正向分支。"""
    return (f"正向（{len(items)} 项）", list(items), {"labels": ["正向"]})


def N(*items: str) -> tuple:
    """反向分支。"""
    return (f"反向（{len(items)} 项）", list(items), {"labels": ["反向"]})


TREE = (
    ROOT_TITLE,
    [
        # ======================================================== 模块 1
        ("模块 1 · 认证与用户", [
            ("注册", [
                P("用户名 stu01 / 邮箱 stu01@ex.com / 密码 Exam@123456 / 两次一致 → 201，可立即登录",
                  "选入学年级 2026 → 学号自动派发 202600xx（8 位 = 4 位年级 + 4 位顺位）",
                  "手填学号 20261234 且年级 2026 → 采用手填，不再派发",
                  "返回体只含 id/username/role/student_no，不含 password"),
                N("三项必填（用户名 / 密码 / 确认密码）任一为空 → 400",
                  "两次密码不一致 → 400",
                  "用户名 2 位（<3）→ 400「用户名长度不能少于 3 位」",
                  "用户名 stu01 已存在 → 400（文案「已存在该用户名」类）",
                  "邮箱格式非法（abc / a@ / @ex.com）→ 400",
                  "邮箱已存在 → 400",
                  "密码纯数字或少于 8 位 → 400",
                  "不选入学年级 → 400（10014 语义：学生必须提供 grade_year）",
                  "手填学号 20251234 + 年级 2026（前缀不符）→ 400 / 10015",
                  "请求体强行塞 student_no（普通注册）→ 被忽略，仍按年级派发",
                  "重复提交同一邮箱（并发 2 次）→ 仅 1 条成功，另 1 条 400"),
            ]),
            ("登录", [
                P("账号 teacher + 密码 Exam@123456（均正确）→ 200，返回 access + refresh",
                  "用 refresh 换新 access → 200",
                  "access 在有效期内访问受保护接口 → 200"),
                N("密码为空 → 400",
                  "用户名为空 → 400",
                  "密码错误 → 401（不提示「用户名不存在」与「密码错误」的差异）",
                  "用户名不存在 → 401，文案与密码错误一致",
                  "账号被停用 → 401 / 40101「账号已被停用」",
                  "登出后旧 refresh 再用 → 401",
                  "篡改 token 的 payload → 401",
                  "过期 access 访问 → 401，需 refresh"),
            ]),
            ("个人资料", [
                P("改 real_name / phone → 200 且立即生效",
                  "邮箱改成未被占用的地址 → 200"),
                N("PATCH 传 student_no → 不生效（字段只读，返回 200 但值不变）",
                  "邮箱改成他人已用地址 → 400",
                  "PATCH 传 role=admin → 被忽略，仍是原角色",
                  "未登录 PATCH → 401"),
            ]),
            ("权限基线", [
                P("学生访问自己的作答 / 成绩 → 200",
                  "教师访问自己班学生的学情 → 200",
                  "管理员访问任意用户列表 → 200"),
                N("匿名打开需登录页面 → 302 跳登录页",
                  "匿名调 /api/v1/ 受保护接口 → 401",
                  "学生调教师接口（建试卷）→ 403",
                  "学生查他人 student_id → 404（不是 403，避免拿状态码当 id 探针）",
                  "教师改他人创建的班级 → 403 或 404"),
            ]),
        ], {"note": "对照点：注册只看年级不该收学号；登录失败文案不能泄露「账号是否存在」。"}),

        # ======================================================== 模块 2A
        ("模块 2A · AI 适配层", [
            ("AI 出题", [
                P("合法提示词 + 有效 Key → 返回结构化题目并入库（question_count +N）",
                  "dry_run=true 试算 → 只返回预览，库中不新增题目"),
                N("无 API Key → 走 mock 降级，接口仍 200，不抛异常",
                  "AI 返回非法 JSON → 进入 errors 列表，不 500",
                  "AI 返回字段缺失 / 类型错 → 能修则修，修不了记 errors",
                  "知识点范围内无题 → 400 / 13010",
                  "提示词变量值含 { } 或 % → 不触发 format 解析错误"),
            ]),
            ("AI 批改", [
                P("答案完整 → 按阈值给分：≥0.7 满分、0.4~0.7 插值、<0.4 零分",
                  "返回同时包含 score 与评语"),
                N("空答案 → 不走模型，直接 0 分（无 token 消耗）",
                  "AI 超时 / 5xx → 降级：gradeable=false + 0 分 + 待人工，不抛异常",
                  "单题 AI 失败 → 其它题照常批改，整批不中断",
                  "AI 返回分数越界（>满分 / 负数）→ 被夹取或记 errors"),
            ]),
            ("降级与可测性", [
                P("测试环境强制 mock → 全部用例可稳定断言"),
                N("sklearn 未安装 → 作弊检测退化为 z-score，仍出结果",
                  "数据库写入失败 → 埋点/日志失败不阻断主流程"),
            ]),
        ]),

        # ======================================================== 模块 2B
        ("模块 2B · 题库与知识点", [
            ("题目 CRUD", [
                P("新建单选 / 多选 / 判断 / 填空 / 简答 → 201",
                  "编辑题干、选项、答案、解析 → 200",
                  "删除题目 → 列表不再显示（软删除 is_active=false）",
                  "按学科 / 题型 / 难度 / 知识点筛选 → 命中条数正确"),
                N("题干为空 → 400",
                  "选择题选项少于 2 个 → 400",
                  "正确答案的 label 不在选项里 → 400",
                  "分值为 0 / -1 / 101 → 400",
                  "填空题参考答案为空 → 400",
                  "学生调题目写接口 → 403",
                  "删除已被试卷引用的题 → 允许软删，但历史卷仍能读到快照"),
            ]),
            ("题库导入", [
                P("下载模板 → 按模板回填 → 导入成功并返回 created / skipped / error 计数",
                  "含重复题的文件 → 重复行进 skipped，其余正常入库"),
                N("上传 .txt / 图片 → 400（只接受 xlsx / csv）",
                  "缺表头列 → 400 并提示缺哪列",
                  "某行题型枚举非法 → 该行进 error，其它行照常入库",
                  "空文件 / 只有表头 → 400",
                  "非教师角色导入 → 403"),
            ]),
            ("知识点", [
                P("建父子知识点、改父节点、按树返回 → 200",
                  "一道题挂多个知识点 → 统计时各计一次",
                  "子树没有在用题目 → 删除父节点成功，级联删子节点并提示「含 N 个子知识点」"),
                N("把父节点设成自己的子节点（成环）→ 400",
                  "子树（含子节点）还有在用题目 → 400 / 12030，父子都不被删（不能删一半）",
                  "已停用的题目不阻塞删除 → 允许删，停用题保留",
                  "同级同名知识点 → 400"),
            ]),
            ("答案防泄漏", [
                P("学生拉题 → 只有题干 / 选项 / 分值与题型"),
                N("学生直接 GET /questions/{id}/ → 404 或 403",
                  "学生视角响应中搜 reference_answer / analysis / is_correct → 全部不存在（字段级白名单）",
                  "交卷后但未出分 → 仍拿不到答案与解析",
                  "改 URL 猜别的考试的题 → 404"),
            ]),
        ], {"note": "答案防泄漏是安全项里最容易回归的：每次改序列化器都要重跑这 4 条。"}),

        # ======================================================== 模块 3
        ("模块 3 · 试卷与组卷", [
            ("试卷结构与汇总", [
                P("加题后总分 = Σ 各题分",
                  "单题调分 → 总分同步更新",
                  "调整顺序 → 题号连续、无重复"),
                N("单题分值 101 → 400 / 13004",
                  "批量调分某题 101 → 400 / 13004（与单题同口径）",
                  "加不存在的题目 id → 400",
                  "空卷发布 → 400 或带「空卷」预警",
                  "重复加同一题 → 400 或自动合并，不出现两条同题"),
            ]),
            ("组卷（算法 + AI 说明）", [
                P("按蓝图抽题 → 题型 / 难度 / 知识点分布符合蓝图",
                  "相同 seed 两次组卷 → 抽到的题目集合与顺序完全一致",
                  "dry_run 试算 → 不落库，且报告字段与真实落库一致"),
                N("题库某题型不足 → 按库存封顶（有多少抽多少），不静默少题",
                  "换 seed → 结果不同（证明随机源由 seed 控制）",
                  "非法 seed（负数 / 超长）→ 400 或被规范化",
                  "学生调用组卷接口 → 403"),
            ]),
            ("随机组卷（FR-5-5）", [
                P("指定知识点 + 题量 → 生成 generation_mode=random 的试卷，不写 AI 说明",
                  "题量超库存 → 全部抽完并封顶，接口不报错"),
                N("知识点范围为空 → 400 / 13010",
                  "题量 0 或 201（越界）→ 400",
                  "时长 0 或 601 分钟 → 400",
                  "随机组卷请求里带 AI 参数 → 被忽略，不触发模型调用"),
            ]),
            ("状态与编辑约束", [
                P("draft → published → archived 逐级流转成功",
                  "草稿状态可改标题、及格分、时长"),
                N("已发布改标题 / 及格分 → 400 / 13002",
                  "已发布加题 / 调分 / 排序 → 400（与卷头同口径）",
                  "归档后再发布 → 400",
                  "已发布删除 → 400",
                  "删除被考试引用的试卷 → 400"),
            ]),
        ], {"note": "两个入口同一规则（单题/批量调分、卷头/加题）是本模块历史缺陷高发区。"}),

        # ======================================================== 模块 4
        ("模块 4 · 在线考试", [
            ("可见范围（FR-6-10）", [
                P("教师建「面向全体」→ 本教师可管理班级的学生列表可见",
                  "指定学生 / 指定班级 → 对应学生可见",
                  "管理员建的面向全体 → 全校学生可见"),
                N("班外学生看列表 → 不出现该考试",
                  "班外学生直接开考 → 404（考试对其不可见）",
                  "草稿 / 未到开始时间 → 学生不可见不可考",
                  "学科教师建的考试 → 非本科目班级学生不可见"),
            ]),
            ("开考", [
                P("时间窗内 + 在范围内 + 次数未用完 → 创建 attempt 并返回 deadline",
                  "重复点「开始考试」→ 复用同一 attempt，不重复计数"),
                N("次数用完 → 403 或业务码拒绝",
                  "考试已结束 → 403",
                  "已交卷后再开考（单次限制）→ 403",
                  "未登录开考 → 401"),
            ]),
            ("计时", [
                P("心跳后剩余时间正常递减",
                  "考试结束时间早于时长上限 → deadline 取较早者（封顶生效）"),
                N("篡改客户端时间 / 提前提交 deadline → 服务端判分不受影响",
                  "超时后保存答案 → 拒绝；宽限 30s 内允许",
                  "超时未交 → 自动交卷为 submitted"),
            ]),
            ("作答与保存", [
                P("选择题保存 selected_labels → is_answered=true",
                  "填空 / 主观保存 answer_text → is_answered=true",
                  "刷新页面 → 选项乱序不变（shuffle_seed 固定）、已答保留",
                  "教师拉同一份作答明细 → 仍能看到参考答案与解析（批改需要）"),
                N("交卷后再保存 → 400「本次作答已结束」",
                  "保存不属于本卷的 paper_question_id → 400",
                  "选择题传非法 label（F / 1）→ 400",
                  "答案文本超长 → 400",
                  "保存别人的 attempt → 404",
                  "学生考中拉 /attempts/{id}/answers/ → 明细里没有参考答案与解析（只能交卷后走 /result/）"),
            ]),
            ("交卷", [
                P("手动交卷 → submitted，answered_count 与提交条数一致",
                  "重复交卷 → 幂等，只有一条记录、状态不变"),
                N("未作答直接交 → 允许，未答题目按 0 分处理",
                  "交卷后看结果 → show_score=false，不显示 0 分",
                  "交别人的卷 → 404"),
            ]),
            ("防作弊", [
                P("切屏 1 次 → 计数 +1 且页面提示",
                  "达到 max_switch_count → 按配置自动交卷",
                  "粘贴行为被记录且可查证据"),
                N("考试关闭 detect_switch → 不上报切屏、计数恒为 0",
                  "埋点接口失败 → 保存与交卷不受影响",
                  "学生查他人证据 → 404"),
            ]),
        ]),

        # ======================================================== 模块 5
        ("模块 5 · 判分与成绩", [
            ("客观题判分", [
                P("单选 / 判断答对 → 满分",
                  "多选 strict 全选对 → 满分；partial 部分对 → 按比例给分",
                  "penalty 策略下错选 → 按规则扣分"),
                N("未作答 → 0 分且 is_correct=null（未判，不是答错）",
                  "多选少选 + strict → 0 分",
                  "答案 label 大小写 / 空格差异 → 归一化后判定，不误判"),
            ]),
            ("主观题批改", [
                P("有答案 → AI 给分 + 评语；教师可人工覆写分数",
                  "全部批改完成后 finalize → 状态进入 auto_graded / reviewing"),
                N("空答案 → 0 分且不调用模型",
                  "AI 失败 → gradeable=false + 0 分 + 待人工",
                  "未 finalize 直接出分 → 400",
                  "对已 graded 的作答再批改 → 拒绝"),
            ]),
            ("复核与出分", [
                P("reviewing → graded 后可公开成绩",
                  "recalc 重复调用 → 结果一致（从 Answer 重算，幂等）"),
                N("graded 回退到 reviewing → 400（终态不回退）",
                  "重复出分 → 不产生重复记录、不重复通知",
                  "对不存在的 attempt 出分 → 404"),
            ]),
            ("成绩可见性", [
                P("show_score_immediately=true → 交卷即可见自动判分结果",
                  "已 graded → 一定可见",
                  "成绩公开后：列表 / 卷面 / 结果页三处分数与是否及格一致"),
                N("开关关闭 → 学生端 show_score=false，无分数无排名",
                  "未判分 → 不显示 0 分（显示「待批改」）",
                  "未出分 → score / passed / is_correct 全为 null（不是 0 或 false）",
                  "交卷后触发 ensure_graded（只判客观题）→ 仍不能看逐题对错"),
            ]),
            ("成绩单", [
                P("教师查自己班 → 200，含排名 / 分数段 / 及格率",
                  "管理员查任意考试 → 200（全校口径）"),
                N("传别人班的 classroom_id → 404",
                  "学生访问成绩单接口 → 403",
                  "班外学生姓名 → 纯文本，无下钻链接（can_view_analytics=false）",
                  "已答 + 未答 ≠ 应考人数 → 视为数据异常（去重错误）"),
            ]),
            ("错题本", [
                P("学生查自己 → 200，只含错题",
                  "教师带 student_id 查该生 → 200"),
                N("教师不带 student_id → 400「需指定学生」",
                  "学生带他人 student_id → 404",
                  "没有错题 → 返回空列表，不报错"),
            ]),
        ], {"note": "「未作答」与「答错」必须区分：前者 is_correct=null，后者 false。统计口径全靠这个。"}),

        # ======================================================== 模块 6
        ("模块 6 · 学情分析", [
            ("掌握度", [
                P("样本 ≥2 → 返回掌握度与薄弱标记",
                  "作答挂在子知识点 → 掌握度向上归并到祖先（不重复计）"),
                N("样本 <2 → 不判薄弱（返回 null 或 unknown）",
                  "零作答学生 → 返回 null，不返回 0",
                  "分母为 0 → null（不抛除零异常）"),
            ]),
            ("区分度与趋势", [
                P("样本 ≥4 → 区分度有值（取总分前/后 27%）",
                  "≥3 个时间点 → 趋势斜率与方向正确"),
                N("样本 <4 → 区分度 null",
                  "趋势点 <3 → 不计算，返回 null",
                  "全班同分 → 区分度为 0 或 null，不出现 NaN"),
            ]),
            ("参与度", [
                P("已交 + 未交 = 应考人数",
                  "同一学生在两个班 → 只计一次（去重）"),
                N("不传 classroom_id → 按可见学生收窄，不是全校人数",
                  "传别人班 → 404",
                  "应考人数与监控页不一致 → 视为缺陷（多入口口径漂移）"),
            ]),
            ("AI 诊断", [
                P("POST .../diagnosis/ → 生成诊断文本，degraded=false",
                  "同一学生同一范围 24h 内复用缓存"),
                N("GET 报告 → 不触发模型（无额外耗时 / 无 token 消耗）",
                  "AI 失败 → 返回 fallback 文本 + degraded=true，不抛异常",
                  "无权限学生 → 404"),
            ]),
            ("权限", [
                P("教师查本班学生学情 → 200",
                  "管理员查任意学生 → 200"),
                N("教师查班外学生 → 404",
                  "学生查他人 → 404",
                  "学生访问班级维度 → 403",
                  "教师传自己能管理之外的 classroom_id → 404"),
            ]),
        ]),

        # ======================================================== 模块 7
        ("模块 7 · 个性化推荐", [
            ("每日练习", [
                P("有历史作答 → 返回 N 题且带推荐理由",
                  "同一天重复请求 → 题目集合与顺序一致（确定性打散）",
                  "跨天请求 → 集合轮换"),
                N("无历史（冷启动）→ 兜底题目非空，不返回空列表",
                  "size 传非数字（abc）→ 400",
                  "size 传 0 或超上限 → 400 或按上限截断",
                  "每知识点超过 2 题 → 被上限截断"),
            ]),
            ("权限与模型调用", [
                P("学生查自己 → 200",
                  "教师查任意学生 → 200（权限矩阵明确允许）"),
                N("学生查他人 → 404",
                  "GET 练习列表 → 不调模型（理由为静态文案）",
                  "POST 才改写理由，失败时不影响题目本身"),
            ]),
        ], {"note": "推荐对教师是「可查任意学生」，学情是「只看自己班」——口径刻意不同，用例里要标注依据。"}),

        # ======================================================== 模块 8
        ("模块 8 · 作弊行为检测", [
            ("规则命中", [
                P("切屏次数超阈值 → 命中规则并累计风险分",
                  "9 条规则（RULE_CATALOG）各有可触发数据，逐条命中",
                  "风险分达到 80 / 55 / 30 / 1 分档 → 等级正确"),
                N("正常作答无异常行为 → 风险分 0 或低，不判可疑",
                  "风险分累加 >100 → 封顶 100，不越界",
                  "考试未开启检测 → 对应规则不参与"),
            ]),
            ("算法与降级", [
                P("样本 ≥8 → 孤立森林可跑通并输出异常分",
                  "sklearn 缺失 → 退化稳健 z-score，仍给结论"),
                N("样本 <8 → 跳过异常检测，不报错",
                  "MAD=0（全同值）→ 退化为标准差，不产生 NaN",
                  "同一数据两次跑 → 结果一致（random_state 固定）"),
            ]),
            ("埋点与证据", [
                P("行为上报 → 落 ExamBehaviorLog 且聚合计数同步 +1",
                  "证据（截图 / 日志）可下载且与 attempt 对应"),
                N("埋点异常（表不存在 / 字段超长）→ 考试流程不受影响",
                  "跨人查证据 → 404",
                  "上报不存在的 attempt → 404"),
            ]),
        ]),

        # ======================================================== 模块 9
        ("模块 9 · 班级管理", [
            ("建班与唯一性", [
                P("建行政班 / 学科班 → 201",
                  "同学科 + 同年级 + 同教师 → 允许（唯一性成立）"),
                N("同学科 + 同年级 + 同教师重复建 → 400",
                  "学科班指定班主任 → 400（学科班不设班主任）",
                  "学生建班 → 403",
                  "班级名称为空 → 400"),
            ]),
            ("成员管理", [
                P("按学号导入命中的学生 → 加入班级",
                  "移除成员 → 立即从名单消失",
                  "批量添加多名学生 → 全部生效"),
                N("导入不存在的学号 → 逐条返回错误，不整批失败",
                  "重复导入同一学生 → 跳过并计数",
                  "把非学生（教师）加进班 → 400",
                  "往别人班加人 → 403 / 404"),
            ]),
            ("权限与学科隔离", [
                P("教师查看全部行政班列表 → 200",
                  "管理自己创建 / 任班主任 / 授课的班 → 200"),
                N("改别人创建的班 → 403 / 404",
                  "学科教师操作非本科目班级 → 404（teacher_subject_scope）",
                  "删除别人的班 → 404"),
            ]),
            ("删除", [
                P("确认后删除 → 记录消失，列表不再显示"),
                N("取消确认 → 班级仍在",
                  "班级有成员 / 有考试时删除 → 按规则拒绝或级联（需明确）"),
            ]),
        ]),

        # ======================================================== 模块 10
        ("模块 10 · 管理端", [
            ("建号", [
                P("建教师不填工号 → 自动派发 T+顺位",
                  "建学生带 grade_year=2026 → 学号 2026xxxx",
                  "建学生手填学号且与年级一致 → 采用手填"),
                N("建学生不传 grade_year（API）→ 400 / 10014",
                  "手填学号前缀与年级不符 → 400 / 10015",
                  "用户名 2 位 → 400",
                  "邮箱重复 → 400",
                  "非管理员建号 → 403",
                  "学生自己注册时试图指定角色 → 被强制为 student"),
            ]),
            ("停用与降级", [
                P("停用普通账号 → 立即无法登录（40101）",
                  "重新启用 → 可正常登录"),
                N("停用最后一名管理员 → 400",
                  "把最后一名管理员降级为教师 → 400",
                  "停用自己 → 400（避免自锁）",
                  "停用后旧 token 继续访问 → 被拦截（中间件回查）"),
            ]),
            ("密码重置", [
                P("重置 → 返回符合策略的新密码，可直接登录"),
                N("重置成不合规密码（纯数字）→ 400（走 validate_password）",
                  "非管理员重置他人 → 403",
                  "对其他角色重复重置 → 每次生成新的随机密码"),
            ]),
            ("系统页与诊断", [
                P("页面展示运行环境与 AI 配置状态 → 200"),
                N("AI 诊断结果不包含密钥明文",
                  "未登录访问 /manage/ → 302"),
            ]),
        ]),

        # ======================================================== 模块 11
        ("模块 11 · 页面与交互", [
            ("空态与异常态", [
                P("有数据 → 正常渲染表格 / 卡片",
                  "无数据 → 显示空状态文案，不白屏"),
                N("接口 500 → 页面给出错误提示，不白屏",
                  "加载中 → 有 loading / 骨架，不是空白",
                  "数据里出现 null → 不渲染成 NaN / undefined",
                  "控制台无 SEVERE 报错（favicon 404 除外）"),
            ]),
            ("危险操作", [
                P("删除班级 → 二次确认 → 确认后执行并跳回列表",
                  "出分 / 发布 → 确认后执行"),
                N("取消确认 → 数据不变",
                  "连点两次提交 → 只发一次请求，不产生重复记录",
                  "请求失败 → 按钮恢复可点，不是永久禁用"),
            ]),
            ("关键交互回归点", [
                P("答题页倒计时递减、30s 内显示「已保存」",
                  "刷新后已选答案与剩余时长都保留（不重置为满时长）",
                  "导入弹窗：选文件显示文件名，解析后按钮复位为「开始解析」",
                  "批改工作台滚动 → 左列保持固定（sticky）"),
                N("断网 → 提示且已答不丢",
                  "375px 宽度 → 不出现横向溢出 / 元素重叠",
                  "导入点击后不跳走页面（modal 内完成）",
                  "成绩单班外学生 → 无下钻链接（点了不会 404）"),
            ]),
        ]),

        # ======================================================== 非功能
        ("非功能测试点", [
            ("性能", [
                P("1 万条用户列表 → < 1500ms（实测 33.7ms）",
                  "登录 / 开考 / 交卷三场景并发 → 0 失败"),
                N("慢查询 / 超时 → 有日志可定位，不静默吞掉",
                  "分页参数异常（page=-1 / page_size=99999）→ 400 或截断"),
            ]),
            ("安全", [
                P("三种角色越权访问均被拒（403 / 404）",
                  "学生响应中无答案、无解析、无 is_correct",
                  "横切扫描：学生可见的所有 GET 接口都不出现答案类字段"),
                N("密钥不出现在页面源码 / 接口响应 / 日志",
                  "上传非法文件类型 → 被拒",
                  "敏感字段黑名单命中即失败：判分类字段未出分却非空也算泄漏",
                  "SQL / 模板注入字符（' OR 1=1、{{7*7}}）→ 被当普通文本"),
            ]),
            ("兼容性", [
                P("Chrome / Edge 桌面端主流程可用",
                  "1280 / 1440 宽度布局正常"),
                N("Firefox 下表格与弹窗不塌陷（待补测）",
                  "移动端 375px 若允许作答 → 需专门验证（当前未覆盖）"),
            ]),
            ("可靠性", [
                P("AI / 数据库抖动 → 降级并提示，不 500",
                  "重复提交（交卷 / 出分 / 判分）→ 幂等"),
                N("服务重启后进行中的作答 → 仍可续考（attempt 未丢）",
                  "并发交卷 2 次 → 只产生一次判分"),
            ]),
        ], {"note": "非功能项里「兼容性」目前只验了 Chrome 156 桌面端，Firefox / 移动端是已知空白，评审时要标风险。"}),
    ],
    {"note": "用法：按模块走查，正向确保主流程能走通，反向确保异常与越权都被挡住。反向项应优先于正向项被覆盖——线上事故多来自没人试过的非法输入。"},
)


# ---------------------------------------------------------------- 生成逻辑
_seq = {"n": 0}


def _tid() -> str:
    _seq["n"] += 1
    return f"t{_seq['n']:04d}"


def _build(node) -> dict:
    # 叶子允许直接写字符串（正向/反向的条目都是这种）
    if isinstance(node, str):
        return {"id": _tid(), "class": "topic", "title": node}

    title = node[0]
    children = node[1] if len(node) > 1 else []
    meta = node[2] if len(node) > 2 else {}

    topic = {"id": _tid(), "class": "topic", "title": title}

    if children:
        topic["children"] = {"attached": [_build(c) for c in children]}

    if meta.get("labels"):
        topic["labels"] = meta["labels"]
    if meta.get("markers"):
        topic["markers"] = [{"markerId": m} for m in meta["markers"]]
    if meta.get("note"):
        topic["notes"] = {"plain": {"content": meta["note"]}}
    return topic


def build() -> dict:
    _seq["n"] = 0
    root = _build(TREE)
    root["structureClass"] = "org.xmind.ui.map.unbalanced"
    return {
        "id": "sheet-requirement-analysis",
        "class": "sheet",
        "title": SHEET_TITLE,
        "rootTopic": root,
        "topicPositioning": "fixed",
    }


def _legacy_topic(t: dict) -> str:
    """把节点转成 XMind 8（content.xml）格式。"""
    out = [f'<topic id="{t["id"]}">', f"<title>{escape(t['title'])}</title>"]

    labels = t.get("labels")
    if labels:
        out.append("<labels>" + "".join(f"<label>{escape(x)}</label>" for x in labels) + "</labels>")

    markers = t.get("markers")
    if markers:
        out.append(
            "<marker-refs>"
            + "".join(f'<marker-ref marker-id="{m["markerId"]}"/>' for m in markers)
            + "</marker-refs>"
        )

    note = ((t.get("notes") or {}).get("plain") or {}).get("content")
    if note:
        out.append(f"<notes><plain>{escape(note)}</plain></notes>")

    kids = (t.get("children") or {}).get("attached")
    if kids:
        inner = "".join(_legacy_topic(k) for k in kids)
        out.append(f'<children><topics type="attached">{inner}</topics></children>')

    out.append("</topic>")
    return "".join(out)


def to_legacy_xml(sheet: dict) -> str:
    """同时写一份 content.xml，兼容 XMind 8（2020+ 读 content.json，互不干扰）。"""
    root = sheet["rootTopic"]
    root_copy = {k: v for k, v in root.items() if k != "structureClass"}
    root_xml = _legacy_topic(root_copy).replace(
        f'<topic id="{root["id"]}">',
        f'<topic id="{root["id"]}" structure-class="{root.get("structureClass", "")}">',
        1,
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
        '<xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0" version="2.0">'
        f'<sheet id="{sheet["id"]}">{root_xml}'
        f"<title>{escape(sheet['title'])}</title>"
        "</sheet></xmap-content>"
    )


def main() -> None:
    content = [build()]
    sheet = content[0]

    metadata = {
        "creator": {"name": "XMind", "version": "12.0.2"},
        "activeSheetId": "sheet-requirement-analysis",
        "dataStructureVersion": "2",
        "layoutEngineVersion": "3",
    }
    manifest = {
        "file-entries": {
            "content.json": {},
            "metadata.json": {},
            "content.xml": {},
            "meta.xml": {},
            "styles.xml": {},
            "META-INF/manifest.xml": {},
        }
    }
    meta_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
        '<meta xmlns="urn:xmind:xmap:xmlns:meta:2.0" version="2.0"/>'
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
        '<xmap-styles xmlns="urn:xmind:xmap:xmlns:style:2.0" version="2.0"/>'
    )
    legacy_manifest = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
        '<manifest xmlns="urn:xmind:xmap:xmlns:manifest:1.0">'
        '<file-entry full-path="content.xml" media-type="text/xml"/>'
        '<file-entry full-path="styles.xml" media-type="text/xml"/>'
        '<file-entry full-path="meta.xml" media-type="text/xml"/>'
        "</manifest>"
    )

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with zipfile.ZipFile(OUT_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("content.xml", to_legacy_xml(sheet))
        zf.writestr("meta.xml", meta_xml)
        zf.writestr("styles.xml", styles_xml)
        zf.writestr("META-INF/manifest.xml", legacy_manifest)

    def count(t: dict) -> int:
        return 1 + sum(count(c) for c in (t.get("children") or {}).get("attached", []))

    total = count(sheet["rootTopic"])
    depth = 0
    pos = neg = 0

    def walk(t: dict, d: int) -> None:
        nonlocal depth, pos, neg
        depth = max(depth, d)
        if (t.get("labels") or []) == ["正向"]:
            pos += len((t.get("children") or {}).get("attached", []))
        if (t.get("labels") or []) == ["反向"]:
            neg += len((t.get("children") or {}).get("attached", []))
        for c in (t.get("children") or {}).get("attached", []):
            walk(c, d + 1)

    walk(sheet["rootTopic"], 1)
    print(f"已生成：{os.path.abspath(OUT_PATH)}")
    print(f"主题总数：{total}；最大层级：{depth}；正向测试点：{pos}；反向测试点：{neg}")
    print(f"文件大小：{os.path.getsize(OUT_PATH) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
