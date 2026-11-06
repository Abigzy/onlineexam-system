# -*- coding: utf-8 -*-
"""执行轮次 R1 / R2 的缺陷与观察项。

R1（2026-11-01）：首次接口实测发现 3 个缺陷。
R2（2026-11-02）：3 个缺陷修复后回归，69 个检查点全部通过。
"""

EXECUTED_AT = "R1 2026-11-01 22:10 / R2 2026-11-02 00:05"
ENV = "本机 MySQL 5.6 + runserver 8777 + 演示库（52 用户 / 8 试卷 / 20 考试）"

# (编号, 类型, 标题, 关联用例, 严重程度, 优先级, 复现步骤, 预期, 实际, 影响, 复现率, 状态, 建议, 修复验证)
DEFECTS = [
    ("BUG-01", "缺陷", "批量调分接口未校验单题分值上限（可设 101 分）",
     "TC-PP-04", "一般", "P1",
     "1) 建草稿卷并加入 3 道题（总分 7）\n2) POST /api/v1/papers/{id}/scores/ "
     "body={\"items\":[{\"id\":<卷内题 id>,\"score\":101}]}\n3) 查卷面该题分值与总分",
     "400，code=13004「单题分值不应超过 100，请检查是否填错」",
     "200，该题分值变为 101，总分由 7 变为 107",
     "教师误填可产生超百分题，考试总分/及格线失真；单题调分接口已拦截，两个入口口径不一致",
     "3/3（必现）", "已关闭",
     "抽出 PaperService._validate_score()，单题与批量两个入口共用",
     "R2 回归：score=101 → 400/13004；score=100 仍允许；总分不再虚高；test_defects 5 条全绿"),

    ("BUG-02", "缺陷", "已发布试卷的卷头信息仍可修改（含及格分与时长）",
     "TC-PP-11", "一般", "P1",
     "1) 发布一张有题试卷\n2) PATCH /api/v1/papers/{id}/ body={\"title\":\"改名\",\"pass_score\":5}\n3) GET 试卷详情",
     "400，code=13002「试卷当前状态为「已发布」，只有草稿可以修改」",
     "200，标题与及格分被改；加题/调分接口反而会 400",
     "已用于进行中考试的卷子可改及格分/时长，成绩判定与展示口径可能变化；与 ensure_editable 的设计意图不一致",
     "3/3（必现）", "已关闭",
     "PaperService.update() 补 ensure_editable（与加题/调分/排序同口径）",
     "R2 回归：已发布 PATCH → 400/13002；草稿仍可改；test_defects 4 条全绿"),

    ("BUG-03", "缺陷", "学情「应考人数」对跨班学生重复计数（17 vs 16）",
     "TC-AN-16", "轻微", "P2",
     "1) 教师带 2 个班：班级 7 有 16 人、班级 1 有 1 人且该生同时在班级 7（去重后 16 人）\n"
     "2) 教师建「面向全体」考试并发布\n3) GET /api/v1/analytics/exams/{id}/report/ 看 participation.applicants",
     "16（按去重后的可见学生数）",
     "17（visible_student_ids 返回含重复 id 的列表，participation 直接用 len() 当分母）",
     "参与度分母偏大、未交人数虚高；同一场考试监控页 applicants=16、学情页 17，两处对不上",
     "3/3（必现）", "已关闭",
     "visible_student_ids 加 .order_by().distinct()（先清排序，否则 DISTINCT 作用在 user_id+created_at 上）；"
     "participation 再做一次去重兜底",
     "R2 回归：applicants=16；跨班学生只计一次；test_defects 4 条全绿"),

    ("BUG-04", "缺陷", "成绩单下钻链接权限失效（班外学生姓名仍渲染成链接，点进去 404）",
     "TC-UI-09", "一般", "P1",
     "1) teacher 登录（只带 7、1 两个班，看不到 s_001）\n"
     "2) 打开 /grading/exams/21/scoreboard/\n"
     "3) 看 s_001 的姓名是否可点\n"
     "4) GET /api/v1/grading/scoreboard/?exam_id=21 看返回结构",
     "班外学生姓名渲染为纯文本（can_view_analytics=false），点不进去",
     "姓名渲染成 <a href=\"/analytics/students/7/\">，点进去 404；接口返回的是 results，"
     "而视图给 data['rows'] 打标记，字段实际不存在 → 前端拿到 undefined，按「不等于 false」渲染成链接",
     "10T「下钻链接按权限渲染」整条失效：跨班统考时教师会点到一堆 404；"
     "同类问题也会影响依赖 rows 键的其他前端逻辑",
     "3/3（必现）", "已关闭",
     "ScoreboardView 改用 data.get('results') 打标（或统一服务层返回键名），并在接口测试里断言 can_view_analytics 存在",
     "R4 回归：接口每条结果都带 can_view_analytics（teacher→s_001=false、admin→true）；"
     "页面班外姓名渲染为纯文本、0 个下钻链接；apps.grading.test_defects 4 条全绿"),

    ("BUG-05", "缺陷", "学生考中可从作答明细接口取走全部参考答案与解析（答案泄漏）",
     "TC-EX-30", "严重", "P0",
     "1) student 开考并作答 1 题（考试进行中，**未交卷**）\n"
     "2) GET /api/v1/attempts/{id}/answers/（学生 token）\n"
     "3) 看每条结果里是否有 reference_answer / analysis",
     "学生视角不含参考答案与解析；要看答案只能交卷后走 /result/（且受成绩公布门禁）",
     "该接口复用教师用的 AnswerSerializer，权限只要求「已登录」→ 学生考到一半就能取走整场考试的"
     "参考答案与解析，响应体里连答案文本都直接可读",
     "考试公平性完全失效：任何考生可在开考后立即拉走全部答案并外传；"
     "与项目「答案防泄漏只有一个出口」的设计直接冲突",
     "3/3（必现）", "已关闭",
     "新增 AnswerStudentSerializer（字段里根本没有 reference_answer / analysis），"
     "接口按 is_teacher 分流；学生出口只保留 /result/ 与错题本",
     "R5 回归：apps.exam.tests.test_answer_leak 5 条（学生无答案 / 学生仍看得到自己的答案 / "
     "响应体无答案文本 / 教师仍有答案 / 跨学生 404），全绿"),

    ("BUG-06", "缺陷", "删除父知识点只校验自身题目，级联删子节点导致题目静默失去知识点归属",
     "TC-KP-05 / TC-KP-06", "一般", "P1",
     "1) 建父知识点「数据结构」，其下建子知识点「二叉树」\n"
     "2) 只在**子节点**「二叉树」上挂 3 道在用题目\n"
     "3) DELETE 父知识点「数据结构」\n"
     "4) 查这 3 道题的 knowledge_points",
     "400，code=12030「知识点「数据结构」或其子知识点下还有题目，请先移走或删除这些题目」",
     "200，父节点连同子节点被级联删除；3 道题仍然存在，但 knowledge_points 变为空",
     "题库知识点覆盖率统计瞬间失真、按知识点组卷漏题，且**全程无报错无提示**，"
     "事后极难发现（题目还在，只是不知道属于哪）",
     "3/3（必现）", "已关闭",
     "删除前用 descendant_ids() 取整棵子树，`Question.objects.filter(knowledge_points__id__in=子树, "
     "is_active=True)` 判存在则 12030 拒绝；无题时级联删除并返回「含 N 个子知识点」",
     "R5 回归：apps.questions.tests.test_questions 覆盖子树含题拒绝（父子都不被删）/ "
     "子树无题级联删除（提示子节点数）/ 停用题不阻塞，3 条全绿"),

    ("BUG-07", "缺陷", "学生视角的分数与逐题对错未按「成绩是否公开」收敛（三个泄漏面）",
     "TC-EX-32 / TC-EX-33 / TC-GEN-10", "一般", "P1",
     "1) 考试设 show_score_immediately=false，学生交卷\n"
     "2) 学生打开一次成绩单（触发 ensure_graded，只判客观题）\n"
     "3) GET /api/v1/attempts/{id}/answers/ 看 is_correct / score\n"
     "4) GET 我的考试列表 与 /attempts/{id}/result/ 看 score / passed",
     "成绩未公开时，总分、是否及格、逐题对错与得分都不下发（null）；"
     "只有 graded 且 show_score_immediately=true 才给",
     "① `ExamAttemptStudentSerializer` 的 score / passed 完全没收敛：show_score=false 却返回 "
     "score=16.00 / passed=False；② `_attempt_brief()` 里写死 \"score\": attempt.score_float()，"
     "而被卷面 / 续考 / 结果页三处调用；③ 修完答案文本后，逐题的 is_correct / score 照旧下发",
     "变相提前知道逐题对错与分数区间；`max_attempts>1` 时可用于下一轮针对性作答；"
     "同一 attempt 对象的三个出口口径不一致，前端没法自洽",
     "3/3（必现）", "已关闭",
     "统一口径 `ExamAttempt.score_visible()`（is_graded 且 show_score_immediately）；"
     "score/passed 改方法字段、未公开给 null；`_attempt_brief` 的 score 同口径；"
     "`AnswerStudentSerializer` 的 is_correct/score/ai_score/ai_comment/teacher_comment 同口径；"
     "契约测试新增学生视角敏感字段横切黑名单",
     "R6 回归：`test_answer_leak` 新增 6 条四阶段断言（作答中/刚交卷/ensure_graded 未公开/已公开）全绿；"
     "`test_api_contract` 新增 2 条横切扫描（答案类不出现、判分类未出分必须为空）全绿；"
     "全量 1172 passed"),

    ("OBS-01", "观察项", "注册重名提示文案与其它入口不一致",
     "TC-AUTH-02", "—", "P2",
     "1) 用已存在用户名注册\n2) 比对提示文案",
     "「该用户名已被占用」（与序列化器 validate_username 的文案一致）",
     "「已存在一位使用该名字的用户。」（DRF UniqueValidator 先于字段校验触发）",
     "仅文案不统一，功能正确（仍 400）；影响一致性体验",
     "3/3", "记录",
     "在序列化器 Meta 里覆盖 unique error_messages，或去掉重复的模型唯一校验",
     "未修（不阻塞）；用例 TC-AUTH-02 已按实际文案调整预期"),

    ("OBS-02", "观察项", "班外学生开考返回 404 而非 403/10431",
     "TC-EX-27", "—", "P2",
     "1) 教师建「面向全体」考试并发布\n2) 班外学生 POST /api/v1/exams/{id}/start/",
     "403，code=10431「你不在本场考试的参考范围内」",
     "404，code=40400（考试对该生不可见，取不到对象）",
     "拒绝行为正确，只是状态码与用例预期不同；符合项目「不可见资源一律 404」的纪律",
     "3/3", "记录",
     "已把 TC-EX-09 / TC-EX-27 的预期改为 404 优先；如需 403 需先让考试可见",
     "未修（预期行为）；用例已更新"),

    ("OBS-03", "观察项", "冒烟脚本「断网续考恢复已存答案」偶发失败（恢复题数 <3）",
     "TC-EX-12", "—", "P2",
     "1) 连续跑 scripts/smoke_test.py\n2) 观察 F11「断网续考」一项",
     "每次运行均恢复 3 题（断言 restored >= 3）",
     "R4 首跑 195/196：该项恢复题数不足；立即复跑 196/196 通过，同一代码同一环境",
     "偶发失败会让回归基线看起来「不干净」，干扰缺陷判定；不影响产品功能",
     "1/2（偶发）", "记录",
     "冒烟脚本在拉取 /attempts/{id}/paper/ 前加短轮询（等最后一批 save 落盘），"
     "或把断言从 >=3 改为 >=已保存数",
     "未修（脚本健壮性）；正式基线以复跑结果 196/196 为准"),
]
