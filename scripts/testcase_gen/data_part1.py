# -*- coding: utf-8 -*-
"""功能测试用例数据（第一批）：通用规范 / 用户认证 / 权限 / AI 适配层 / 知识点。

用例字段顺序（前 8 项即「测试用例八大要素」）：
    用例编号、所属模块、用例标题、前置条件、测试步骤、测试数据、预期结果、优先级
附加：用例类型、需求编号、建议执行方式、备注
"""


def C(cid, module, title, pre, steps, data, expected, pri, ctype, fr, auto="手工", note=""):
    return [cid, module, title, pre, steps, data, expected, pri, ctype, fr, auto, note]


CASES = []

# ==================================================================
# 00 通用与接口规范（跨模块）
# ==================================================================
M = "00 通用与接口规范"
CASES += [
    C("TC-GEN-01", M, "统一响应体结构符合 {code,message,data} 约定",
      "服务已启动（runserver 8000）",
      "1) 未登录直接请求 GET /api/health/\n2) 查看响应体与 HTTP 状态码",
      "URL：http://127.0.0.1:8000/api/health/",
      "1) HTTP 200\n2) data 层无二次包裹，形如 {code:0, message:\"success\", data:{...}}；业务数据全在 data 内",
      "P0", "功能", "NFR-2", "自动化（冒烟）",
      "统一渲染器 UnifiedJSONRenderer 保证；若返回 HTML 说明 BrowsableAPIRenderer 排在前面"),

    C("TC-GEN-02", M, "未登录访问受保护接口返回 401 而非 403",
      "无 token，无 session",
      "1) 不带 Authorization 请求 GET /api/v1/questions/\n2) 观察状态码与 code",
      "无 token",
      "1) HTTP 401\n2) code=40100，message 为认证失败提示；不得返回 302 登录页（API 不跳转）",
      "P0", "权限", "NFR-4", "自动化（单测）", ""),

    C("TC-GEN-03", M, "访问不存在的资源返回 404",
      "已用 teacher 登录取得 token",
      "1) GET /api/v1/questions/999999/\n2) GET /api/v1/papers/999999/",
      "id=999999（库中不存在）",
      "1) 均返回 HTTP 404\n2) code=40400，message 说明资源不存在；不得返回 500 或空 data 的 200",
      "P0", "异常", "NFR-2", "自动化（单测）", ""),

    C("TC-GEN-04", M, "查询参数类型非法返回 400（不允许 500）",
      "student 已登录",
      "1) GET /api/v1/recommend/students/{本人id}/practice/?size=abc\n2) GET /api/v1/grading/queue/?exam_id=abc",
      "size=abc；exam_id=abc",
      "1) 均返回 HTTP 400\n2) code=40000，message/code 能指出具体字段；响应体不是 Django 500 调试页",
      "P0", "异常", "NFR-6", "自动化（单测）",
      "历史缺陷：判分模块 5 处参数曾 500，现已改 Serializer 校验"),

    C("TC-GEN-05", M, "列表分页结构与 page_size 上限",
      "题库 ≥ 30 道题",
      "1) GET /api/v1/questions/?page=1&page_size=20\n2) 再请求 page_size=200\n3) 请求 page=9999",
      "page_size=20 / 200；page=9999",
      "1) data 含 count / total_pages / page / page_size / results 六项\n2) page_size 被夹到上限 100（返回条数 ≤100）\n3) 越界页返回 404 code=40400，不抛异常",
      "P1", "功能", "NFR-9", "自动化（单测）", ""),

    C("TC-GEN-06", M, "RESTful 语义：GET 幂等、DELETE 重复调用不报 500",
      "teacher 登录；存在一个空知识点",
      "1) 连续两次 GET 同一知识点详情，比对 data\n2) DELETE 该知识点\n3) 再次 DELETE 同一 id",
      "知识点 id 由第 1 步取得",
      "1) 两次响应 data 完全一致，且库中数据无变化\n2) 第一次 200 成功\n3) 第二次 404 code=40400（不是 500）",
      "P1", "功能", "NFR-1", "手工", ""),

    C("TC-GEN-07", M, "中文、全角字符与特殊符号存取不乱码",
      "teacher 登录",
      "1) 新建单选题，题干含「《数据结构》\"栈\"与'队列' → 换行、全角空格、emoji 🙂」\n2) GET 详情比对原文\n3) 导出 CSV 用 Excel 打开",
      "题干：数据结构《栈》与\"队列\"\n第二行　含全角空格 🙂",
      "1) 创建 201\n2) 详情逐字一致，无乱码、无转义字符残留\n3) CSV 用 Excel 打开中文正常（文件头含 UTF-8 BOM）",
      "P1", "功能", "NFR-10", "自动化（单测）",
      "MySQL 5.6 + utf8；CSV 必须带 BOM"),

    C("TC-GEN-08", M, "时间字段按 Asia/Shanghai 展示，页面倒计时与接口一致",
      "student 已开考一场 60 分钟考试",
      "1) GET /api/v1/attempts/{id}/paper/，记录 deadline\n2) 与服务器 UTC 时间对比\n3) 观察答题页倒计时首屏数值",
      "服务器时间 2026-10-30 21:00（UTC 13:00）",
      "1) deadline 返回本地时间（+08:00），不是 UTC 原文\n2) 两者相差 8 小时且换算正确\n3) 页面倒计时与 deadline 差值一致（误差 ≤1s，不含 8 小时错位）",
      "P1", "功能", "FR-6-1", "自动化（单测）",
      "历史缺陷：_fmt_dt 未转本地时区，学生看到 05:42"),

    C("TC-GEN-09", M, "未登录可访问的接口白名单只有三条",
      "无 token、无 session；已导出 OpenAPI 路由清单",
      "1) 未登录请求 POST /auth/register/、POST /auth/login/、POST /auth/refresh/、GET /api/health/\n2) 未登录请求 GET /api/v1/questions/、/papers/、/exams/、/users/、/analytics/...\n3) 用 /api/schema/ 遍历全部 operation 核对公开项",
      "4 个公开入口 + 5 个受保护入口",
      "1) 四个公开入口均 200/201（health 200、refresh 需带 body）\n2) 五个受保护入口均 401 code=40100\n3) 公开操作恰好为 {register POST, login POST, refresh POST} 三项；多一条就是认证缺口",
      "P0", "安全", "NFR-4", "自动化（单测）",
      "契约测试 test_api_contract 已按此口径固化"),

    C("TC-GEN-10", M, "学生视角敏感字段横切扫描（答案类不出现 / 判分类未出分必须为空）",
      "学生已交卷且成绩**未**对本人公开（客观题被 ensure_graded 判过也不算公开）",
      "1) 以学生身份遍历全部可访问的 GET 接口（约 60 个操作）\n"
      "2) 递归扫响应体里的答案类字段：reference_answer / analysis / correct_answer\n"
      "3) 再扫判分类字段：is_correct / score / ai_score / passed 等，取到非空值即失败",
      "学生 token；黑白名单见 ANSWER_FIELD_ALLOWED / SCORE_FIELD_ALLOWED",
      "1) 答案类字段只在错题本、结果页出现（白名单接口，且各自有门禁）\n"
      "2) 其余接口一律不出现答案类字段\n"
      "3) 成绩未公开时判分类字段为 null / 空；出分后才返回真值",
      "P0", "安全", "NFR-4", "自动化（单测）",
      "BUG-05/07 之后新增的横切检查：新增接口默认受约束，放行必须到白名单写明理由"),
]

# ==================================================================
# 01 用户与认证（模块 1）
# ==================================================================
M = "01 用户与认证（模块1）"
CASES += [
    C("TC-AUTH-01", M, "学生注册成功、学号自动派发并可立即登录",
      "用户名 stu_tc01 / 邮箱 tc01@example.com 未被占用；库中 2026 级已有学生",
      "1) POST /api/v1/auth/register/ 提交完整信息（不传 student_no）\n2) 检查返回的 student_no\n3) 用新账号 POST /api/v1/auth/login/\n4) 用派发到的学号再登录一次",
      "username=stu_tc01\nemail=tc01@example.com\npassword=Exam@123456\npassword2=Exam@123456\nreal_name=测试学生\ngrade_year=2026",
      "1) HTTP 201，code=0，message「注册成功，请使用新账号登录」\n2) data.student_no 为 8 位且以 2026 开头（如 20260047），= 4 位年级 + 4 位顺位；不含 password 字段\n3) 登录 200，返回 access + refresh，role=student\n4) 用该学号也能登录成功",
      "P0", "功能", "FR-1-1", "自动化（单测）",
      "10W 变更：注册不再接收学号，改由年级派发"),

    C("TC-AUTH-02", M, "注册用户名重复（大小写不敏感）",
      "已存在用户 student",
      "1) 用 username=STUDENT 注册\n2) 用 username=Student 再注册",
      "username=STUDENT / Student，其余字段合法",
      "1) 均返回 400\n2) 字段级错误提示「该用户名已被占用」（username 字段）",
      "P0", "异常", "FR-1-1", "自动化（单测）", ""),

    C("TC-AUTH-03", M, "注册用户名长度边界（3 位）",
      "无",
      "1) username=ab（2 位）注册\n2) username=abc（3 位）注册",
      "ab / abc，其余字段合法且不重复",
      "1) 2 位返回 400「用户名长度不能少于 3 位」\n2) 3 位通过校验（201 或后续唯一性错误，不得报长度错误）",
      "P1", "边界", "FR-1-1", "自动化（单测）", "边界值：min-1 / min"),

    C("TC-AUTH-04", M, "注册密码长度边界（6 位）",
      "无",
      "1) password=Ab@12（5 位）\n2) password=Ab@123（6 位）",
      "5 位 / 6 位，password2 一致",
      "1) 5 位返回 400「密码长度不能少于 6 位」\n2) 6 位不再报长度错误（弱口令校验另算）",
      "P1", "边界", "FR-1-1", "自动化（单测）", ""),

    C("TC-AUTH-05", M, "弱口令被密码校验器拒绝",
      "无",
      "1) password=123456 注册\n2) password=11111111 注册\n3) password=password 注册",
      "123456 / 11111111 / password，password2 与之一致",
      "1) 三次均返回 400\n2) 提示分别为「密码太常见」「密码不能全为数字」等；不得把弱口令注册成功",
      "P0", "异常", "FR-1-1", "自动化（单测）",
      "AUTH_PASSWORD_VALIDATORS：CommonPassword/NumericPassword/Similarity/MinLength"),

    C("TC-AUTH-06", M, "两次输入密码不一致",
      "无",
      "1) POST register：password=Exam@123456，password2=Exam@123457",
      "password 与 password2 不同",
      "1) 400\n2) 错误定位在 password2：「两次输入的密码不一致」",
      "P1", "异常", "FR-1-1", "自动化（单测）", ""),

    C("TC-AUTH-07", M, "邮箱必填且不可重复（大小写不敏感）",
      "teacher@example.com 已被 teacher 占用",
      "1) 不传 email 注册\n2) email=TEACHER@EXAMPLE.COM 注册",
      "email 缺失 / 大写形式的已存在邮箱",
      "1) 缺失 email 返回 400（email 为必填）\n2) 大写形式返回 400「该邮箱已被注册」",
      "P1", "异常", "FR-1-1", "自动化（单测）", ""),

    C("TC-AUTH-08", M, "入学年级校验与学号派发顺位",
      "当前年份为 2026；库中已有 2026 级与 2025 级学生",
      "1) 注册时不传 grade_year\n2) grade_year=1999\n3) grade_year=2026 连续注册两个账号\n4) grade_year=2025 注册一个账号",
      "grade_year 缺省 / 1999 / 2026×2 / 2025",
      "1) 400（年级必填）\n2) 400「请选择有效的入学年级」（仅当前年往前 3 年可选）\n3) 均 201，学号顺位递增（如 20260047 → 20260048）\n4) 2025 级学号以 2025 开头，顺位独立从 1 起（不接 2026 级的号）",
      "P0", "边界", "FR-1-1", "自动化（单测）",
      "年级取值来自 grade_year_choices()：当前年往前 3 年，共 4 项"),

    C("TC-AUTH-08B", M, "客户端伪造学号不生效",
      "无",
      "1) 注册时额外传 student_no=99999999\n2) 检查入库的学号",
      "student_no=99999999；grade_year=2026",
      "1) 仍返回 201（多余字段不报错）\n2) 入库学号是系统按年级派发的号，不是 99999999\n3) 说明 student_no 已不在注册白名单里",
      "P1", "安全", "FR-1-1", "自动化（单测）",
      "身份编号不可由客户端指定，防止冒用"),

    C("TC-AUTH-09", M, "注册接口越权指定角色被拒绝",
      "无",
      "1) 注册请求体带 role=teacher\n2) 带 role=admin",
      "role=teacher / admin",
      "1) 均 400\n2) 提示「教师账号由管理员创建，注册仅支持学生」；库中不得新增教师/管理员账号",
      "P0", "权限", "FR-1-1", "自动化（单测）",
      "角色只能由服务端决定，不接受前端传值"),

    C("TC-AUTH-10", M, "用户名登录",
      "student 账号存在且启用",
      "1) POST /api/v1/auth/login/ {username:student,password:Exam@123456}",
      "student / Exam@123456",
      "1) 200，data 含 access、refresh、user\n2) user.role=student，token 载荷含 role 与 real_name",
      "P0", "功能", "FR-1-2", "自动化（单测）", ""),

    C("TC-AUTH-11", M, "邮箱登录（大小写不敏感）",
      "student 的邮箱为 student@example.com",
      "1) 用 Student@Example.com 作为账号登录",
      "Student@Example.com / Exam@123456",
      "1) 200，返回的就是 student 本人\n2) 登录成功后 last_login_ip 有记录",
      "P0", "功能", "FR-1-2", "自动化（单测）", ""),

    C("TC-AUTH-12", M, "学号登录",
      "s_001 的学号为 20260001，密码 Exam@123456",
      "1) 用 20260001 作为账号登录\n2) 用不存在的学号 99999999 登录",
      "20260001 / 99999999",
      "1) 200，返回 s_001\n2) 401，提示与密码错误一致（不暴露学号是否存在）",
      "P1", "功能", "FR-1-2", "自动化（单测）", ""),

    C("TC-AUTH-13", M, "密码错误登录失败",
      "student 存在",
      "1) 用 student + 错误密码登录",
      "student / Wrong@123456",
      "1) 401\n2) code=40100，文案「账号或密码错误，请重新输入」\n3) 响应不含 access/refresh",
      "P0", "异常", "FR-1-2", "自动化（单测）", ""),

    C("TC-AUTH-14", M, "账号不存在与密码错误的提示完全一致（防枚举）",
      "student 存在",
      "1) 用 nosuchuser_xyz 登录\n2) 用 student + 错密码登录\n3) 比对两次响应体",
      "nosuchuser_xyz / Exam@123456",
      "1) 均 401\n2) code 均为 40100，message 逐字相同\n3) 响应耗时量级相当，不通过文案或状态码泄露账号是否存在",
      "P0", "安全", "NFR-4", "自动化（单测）", ""),

    C("TC-AUTH-15", M, "停用账号登录返回 40101 专用提示",
      "管理员已把 s_002 停用，其密码为 Exam@123456",
      "1) 用 s_002 + 正确密码登录\n2) 用 s_002 + 错误密码登录",
      "s_002 / Exam@123456；s_002 / Wrong@123",
      "1) 正确密码：401，code=40101，文案「该账号已被停用，请联系管理员」\n2) 错误密码：401，code=40100 通用文案（不得提示已停用，避免枚举）",
      "P0", "安全", "AC-26/27", "自动化（单测）",
      "停用文案在中间件、登录接口、登录页三处必须逐字一致"),

    C("TC-AUTH-16", M, "refresh 换 access 且 refresh 轮换后旧 token 失效",
      "已登录取得 access1 + refresh1",
      "1) POST /auth/refresh/ {refresh:refresh1}\n2) 用返回的新 refresh2 再换一次\n3) 再用 refresh1 换一次",
      "refresh1 / refresh2",
      "1) 200，返回新 access（及新 refresh2）\n2) refresh2 可正常换到 access\n3) refresh1 返回 401（已加入黑名单）",
      "P1", "功能", "FR-1-3", "自动化（单测）",
      "SIMPLE_JWT：ROTATE_REFRESH_TOKENS=True + BLACKLIST_AFTER_ROTATION=True"),

    C("TC-AUTH-17", M, "登出后 refresh 立即失效",
      "已登录取得 refresh",
      "1) POST /auth/logout/ {refresh}\n2) 再用同一 refresh 换 access\n3) 再次调用 logout（同一 refresh）",
      "同一 refresh",
      "1) 200「已退出登录」\n2) 401（已拉黑）\n3) 仍返回 200（token 已失效也视为登出成功，不报错）",
      "P1", "功能", "FR-1-3", "自动化（单测）", ""),

    C("TC-AUTH-18", M, "当前用户查询与资料修改（学号只读）",
      "student 已登录，学号由系统派发（如 20260047）",
      "1) GET /auth/me/\n2) PATCH /auth/me/ {real_name:测试改名, phone:13800000000}\n3) PATCH /auth/me/ {student_no:99999999}\n4) PATCH /auth/me/ {email: teacher@example.com}",
      "real_name=测试改名；phone=13800000000；student_no=99999999；email 用他人邮箱",
      "1) 200，含 id/username/role/real_name/student_no，不含 password\n2) 200，字段已更新\n3) 200 但 student_no 被忽略、保持原值（不在可写白名单；页面上以只读展示）\n4) 400「该邮箱已被其他账号使用」，原邮箱不变",
      "P1", "功能", "FR-1-4", "自动化（单测）",
      "10W 变更：学号/工号是身份标识，不允许自助修改（防冒用）"),

    C("TC-AUTH-19", M, "修改密码的三条校验与 token 失效",
      "student 已登录，原密码 Exam@123456",
      "1) 提交 old_password=Wrong@123\n2) 提交 new_password 与 old 相同\n3) 提交 confirm 不一致\n4) 正确修改为 New@123456\n5) 用旧 refresh 换 access",
      "old=Wrong@123 / new=Exam@123456 / confirm 不一致 / 正常修改",
      "1) 400，code=10005「原密码不正确」\n2) 400「新密码不能与原密码相同」\n3) 400 定位 confirm_password\n4) 200「密码修改成功，请重新登录」\n5) 401，历史 refresh 已被拉黑",
      "P0", "功能", "FR-1-4", "自动化（单测）", ""),

    C("TC-AUTH-20", M, "管理员建号的初始密码规则",
      "admin 已登录",
      "1) POST /api/v1/users/ 不带 password\n2) password=123456 建号\n3) password=Init@123456 建号",
      "username=stu_tc20 / teacher_tc20",
      "1) 业务错误 code=10006「请提供至少 6 位的初始密码」\n2) code=10011，返回密码校验器原文\n3) 201，可用新密码登录，data 不含密码明文",
      "P1", "功能", "FR-1-5", "自动化（单测）", ""),

    C("TC-AUTH-21", M, "管理员删除 / 停用账号的护栏",
      "admin 已登录，库中存在超级管理员",
      "1) DELETE /users/{自己的id}/\n2) DELETE /users/{superuser id}/\n3) POST /users/{自己的id}/toggle-active/",
      "自己的 id；superuser id",
      "1) code=10007「不能删除当前登录的账号」\n2) 403，code=10008「超级管理员账号不可删除」\n3) code=10010「不能停用自己的账号」",
      "P0", "权限", "AC-18", "自动化（单测）", "最后一名启用管理员不可停用 / 降级"),

    C("TC-AUTH-22", M, "学生访问用户管理接口被拒",
      "student 已登录",
      "1) GET /api/v1/users/\n2) POST /api/v1/users/",
      "任意请求体",
      "1) 均 403\n2) code=40300；不得返回用户列表或创建成功",
      "P0", "权限", "FR-1-5", "自动化（单测）", ""),
]

# ==================================================================
# 02 权限与越权（跨模块）
# ==================================================================
M = "02 权限与越权（跨模块）"
CASES += [
    C("TC-PERM-01", M, "学生访问题库 / 知识点接口被拒",
      "student 已登录",
      "1) GET /api/v1/questions/\n2) GET /api/v1/knowledge-points/tree/\n3) GET /api/v1/questions/statistics/",
      "无特殊参数",
      "1) 均 403\n2) code=40300；题目与答案不得下发",
      "P0", "权限", "FR-4", "自动化（单测）", ""),

    C("TC-PERM-02", M, "学生创建 / 修改考试被拒",
      "student 已登录，存在已发布试卷",
      "1) POST /api/v1/exams/ {paper_id, title}\n2) PATCH /api/v1/exams/{已有考试id}/ {title}",
      "paper_id=1；title=越权测试",
      "1) 均 403\n2) 库中考试数据不变",
      "P0", "权限", "FR-6", "自动化（单测）", ""),

    C("TC-PERM-03", M, "学生访问作弊检测接口被拒",
      "student 已登录",
      "1) GET /api/v1/proctoring/exams/\n2) GET /api/v1/proctoring/exams/1/report/\n3) GET /api/v1/proctoring/attempts/1/evidence/",
      "无特殊参数",
      "1) 均 403\n2) 不返回任何考生行为数据",
      "P0", "权限", "FR-10", "自动化（单测）", "作弊检测是教师红线"),

    C("TC-PERM-04", M, "学生访问作弊检测页面返回 403 白页而非跳转",
      "student 用浏览器登录",
      "1) 地址栏访问 /proctoring/\n2) 访问 /proctoring/attempts/1/evidence/",
      "URL 直填",
      "1) 均 HTTP 403\n2) 不出现 302 跳首页（避免误以为无此功能）；页面权限由 TeacherPageMixin 兜底",
      "P0", "权限", "AC-05", "自动化（冒烟）", ""),

    C("TC-PERM-05", M, "学生查看他人成绩报告返回 404",
      "student 与 s_001 都有作答记录",
      "1) student 登录后 GET /api/v1/grading/attempts/{s_001 的 attempt id}/report/\n2) GET 自己 attempt 的同一接口",
      "他人 attempt_id / 本人 attempt_id",
      "1) 404 code=40400（不是 403）\n2) 200，只含本人数据",
      "P0", "安全", "NFR-4", "自动化（单测）",
      "跨人一律 404，避免用状态码探测 id 是否存在"),

    C("TC-PERM-06", M, "推荐接口的跨人访问规则",
      "student 登录；teacher 登录；两者 id 已知",
      "1) student GET /recommend/students/{s_001 id}/practice/\n2) student GET /recommend/students/{teacher id}/practice/\n3) teacher GET /recommend/students/{s_001 id}/practice/",
      "s_001 id；teacher id",
      "1) 404\n2) 404（目标不是学生）\n3) 200（教师可查任意学生）",
      "P1", "权限", "FR-9", "自动化（单测）",
      "§3.1 权限矩阵已明确「每日练习推荐：教师可查任意学生」，与学情「只能看本班」是刻意不同"),

    C("TC-PERM-07", M, "非管理员访问管理端页面被 302 回首页并提示",
      "teacher 用浏览器登录",
      "1) 访问 /manage/\n2) 访问 /manage/users/\n3) 观察页面提示",
      "URL 直填",
      "1) 302 跳转 /（首页）\n2) 同样 302，不出现 403 白页\n3) 首页顶部出现「该区域仅管理员可访问」提示",
      "P1", "权限", "AC-06/07", "自动化（单测）", ""),

    C("TC-PERM-08", M, "未登录访问页面跳登录页并回跳原地址",
      "清除浏览器 cookie",
      "1) 访问 /papers/\n2) 在登录页用 teacher 登录",
      "teacher / Exam@123456",
      "1) 302 到 /login/?next=/papers/\n2) 登录后回到 /papers/，不是回到首页",
      "P1", "功能", "FR-1", "手工", ""),

    C("TC-PERM-09", M, "教师访问系统总览接口被拒",
      "teacher 已登录",
      "1) GET /api/v1/system/overview/",
      "无",
      "1) 403 code=40300\n2) 不返回用户结构 / AI 用量等系统级数据",
      "P1", "权限", "AC-04", "自动化（单测）", ""),
]

# ==================================================================
# 03 AI 适配层（模块 2A）
# ==================================================================
M = "03 AI 适配层（模块2A）"
CASES += [
    C("TC-AI-01", M, "AI 平台清单返回配置状态且不泄露 Key",
      "teacher 已登录",
      "1) GET /api/v1/ai/providers/\n2) 检查响应体是否含密钥字符串",
      "无",
      "1) 200，返回 current、allow_mock、active_is_mock、providers 列表（每家含是否已配 Key）与 prompts 清单\n2) 不含任何 api_key 明文；只暴露「已配置/未配置」状态",
      "P1", "功能", "FR-2-1", "自动化（单测）", ""),

    C("TC-AI-02", M, "AI 连通性自检",
      "teacher 已登录；.env 已配 DEEPSEEK_API_KEY",
      "1) POST /api/v1/ai/test-connection/ {provider:deepseek}\n2) 断网或填错 Key 后再测",
      "provider=deepseek；错误 Key=sk-invalid",
      "1) 成功返回 ok=true、耗时与模型名\n2) 失败返回可读错误（含平台返回摘要），不抛 500；耗时字段存在",
      "P1", "功能", "FR-2-4", "手工", "真实调用会产生费用，回归时用 provider=mock"),

    C("TC-AI-03", M, "未配置 Key 时自动降级 mock，功能不中断",
      "清空 DEEPSEEK_API_KEY（或 .env 置空后重启）",
      "1) 调任一 AI 场景（如 /ai/chat/ 或 AI 出题）\n2) 查看返回",
      "prompt=测试降级",
      "1) 请求成功（200），不报错\n2) 返回 is_mock=true / provider=mock，业务字段结构完整",
      "P0", "异常", "FR-2-2", "自动化（单测）",
      "build_provider() 负责降级；测试环境强制 mock"),

    C("TC-AI-04", M, "AI 出题返回草稿且不落库",
      "teacher 登录；选择一个知识点",
      "1) 记录题库总数\n2) POST /api/v1/questions/ai-generate/ {knowledge_point_id, count:5, provider:mock}\n3) 再查题库总数",
      "knowledge_point_id=1，count=5，provider=mock，type=single",
      "1) 返回 5 条草稿（题干/选项/答案/解析俱全）\n2) 草稿不含数据库 id（未入库）\n3) 题库总数不变",
      "P0", "功能", "FR-4-4", "自动化（单测）", ""),

    C("TC-AI-05", M, "大模型返回脏数据时容错不中断整批",
      "使用 mock 的脏数据场景（缺字段 / 非法题型 / 非 JSON）",
      "1) 触发一次 AI 出题或 AI 批改\n2) 观察返回的 items 与 errors",
      "构造 3 条数据：1 条合法、1 条缺 stem、1 条 type=code",
      "1) 合法条目被保留\n2) 非法条目进入 errors 并说明原因，不让整个请求失败\n3) HTTP 仍为 200",
      "P0", "异常", "FR-2-2", "自动化（单测）",
      "能修就修、修不了丢 errors"),

    C("TC-AI-06", M, "AI 调用日志与用量统计可对账",
      "已发生 N 次 AI 调用（含 mock 与失败）",
      "1) GET /api/v1/ai/logs/?page_size=50\n2) GET /api/v1/ai/usage/\n3) 比对次数",
      "近 7 天",
      "1) 每条日志含 provider/model/scene/tokens/latency_ms/success/is_mock/created_at\n2) usage 的次数与 logs 条数一致\n3) 失败调用 success=false 且计入失败率",
      "P1", "数据一致性", "FR-2-3", "自动化（单测）", ""),

    C("TC-AI-07", M, "AI 服务异常时的降级与错误码",
      "teacher 登录；provider 指向不可达地址或超时",
      "1) 触发需要 AI 的业务动作（如生成组卷说明）\n2) 观察响应",
      "provider=openai（Key 无效）",
      "1) 返回 502，code=13020（或对应业务码），message 为可读中文\n2) 不出现 500 堆栈；页面有降级提示而非崩溃",
      "P1", "异常", "FR-2-2", "自动化（单测）", ""),

    C("TC-AI-08", M, "学生访问 AI 管理接口被拒",
      "student 已登录",
      "1) GET /api/v1/ai/logs/\n2) GET /api/v1/ai/usage/",
      "无",
      "1) 均 403\n2) 不泄露 provider / token 用量信息",
      "P1", "权限", "FR-2", "自动化（单测）", ""),
]

# ==================================================================
# 04 知识点（模块 2B）
# ==================================================================
M = "04 知识点（模块2B）"
CASES += [
    C("TC-KP-01", M, "知识点建成三级树并按树形返回",
      "teacher 登录；学科=数据结构",
      "1) 建「数据结构」\n2) 建子节点「树」parent=数据结构\n3) 建子节点「二叉树」parent=树\n4) GET /api/v1/knowledge-points/tree/",
      "name=数据结构 / 树 / 二叉树；subject=数据结构",
      "1) 三次创建均 201\n2) tree 返回嵌套结构：数据结构 → 树 → 二叉树\n3) full_path 为「数据结构 / 树 / 二叉树」，level 分别为 1/2/3",
      "P0", "功能", "FR-3-1", "自动化（单测）", ""),

    C("TC-KP-02", M, "同级同名知识点被拒绝",
      "已存在「数据结构」下的「树」",
      "1) 再次在「数据结构」下建同名「树」\n2) 在「数据结构」根级建「树」",
      "name=树；parent 分别为 数据结构 / 空",
      "1) 400，提示「同级下已存在同名知识点」\n2) 第 2 次可创建成功（不同级允许同名）",
      "P1", "异常", "FR-3-1", "自动化（单测）", ""),

    C("TC-KP-03", M, "知识点上级不能指向自己",
      "存在知识点「树」",
      "1) PATCH 该知识点 {parent: 自身 id}",
      "parent=<树自身 id>",
      "1) 400\n2) 提示「上级知识点不能是自己」；库中父子关系不变",
      "P0", "异常", "FR-3-1", "自动化（单测）", ""),

    C("TC-KP-04", M, "知识点上级不能指向自己的子孙（成环保护）",
      "存在 数据结构 → 树 → 二叉树",
      "1) PATCH「数据结构」{parent: 二叉树 id}\n2) GET tree 复查",
      "parent=<二叉树 id>",
      "1) 400，提示「上级知识点不能是自己的子节点（会形成环）」\n2) 树结构未被破坏，无循环引用",
      "P0", "异常", "FR-3-1", "自动化（单测）",
      "descendant_ids() 带环保护"),

    C("TC-KP-05", M, "子树没有在用题目时允许级联删除父知识点",
      "「操作系统」下建 2 个子知识点，整棵子树都没有题目",
      "1) DELETE /api/v1/knowledge-points/{操作系统 id}/\n2) GET 知识点列表核对",
      "父知识点 id（含 2 个子节点）",
      "1) 200，message「知识点「操作系统」已删除（含 2 个子知识点）」\n"
      "2) 父节点与 2 个子节点都不再存在",
      "P1", "功能", "FR-3-3", "自动化（单测）",
      "口径变更：原「有子节点一律拒绝」改为「整棵子树无在用题目则允许级联，并说明删掉几个子节点」"),

    C("TC-KP-06", M, "子树里还有在用题目时拒绝删除父知识点",
      "父节点自身没有题，但子节点「二叉树」挂着 3 道在用题目",
      "1) DELETE 父知识点\n2) 查父、子节点是否都还在\n3) 把 3 道题移走后重试",
      "父 / 子知识点 id",
      "1) 400，code=12030，message 含「子知识点」与「还有题目」\n"
      "2) 父子**都还在**（拒绝时不能删掉一半）\n"
      "3) 移走题目后删除成功，并级联删掉子节点",
      "P0", "异常", "FR-3-2", "自动化（单测）",
      "BUG-06：检查范围必须是整棵子树；只看自身的 questions 会静默删掉子节点上的题目归属"),

    C("TC-KP-07", M, "按父知识点查题包含子孙知识点的题",
      "「数据结构」有 1 道题，「树」有 1 道，「二叉树」有 1 道",
      "1) GET /api/v1/knowledge-points/{数据结构 id}/questions/\n2) GET /api/v1/questions/?knowledge_point={树 id}",
      "父 / 子知识点 id",
      "1) 返回 3 道题（含全部子孙）\n2) 返回「树」与「二叉树」下的 2 道题，不含父节点自身的题",
      "P0", "功能", "FR-3-2", "自动化（单测）", ""),

    C("TC-KP-08", M, "学生访问知识点接口被拒",
      "student 已登录",
      "1) GET /api/v1/knowledge-points/\n2) GET /api/v1/knowledge-points/tree/",
      "无",
      "1) 均 403\n2) 不下发知识点结构",
      "P1", "权限", "FR-3", "自动化（单测）", ""),

    C("TC-KP-09", M, "已停用的题目不阻塞知识点删除",
      "知识点下挂 1 道 is_active=false 的题，没有在用题目",
      "1) DELETE 该知识点\n2) 查库核对知识点与停用题目",
      "知识点 id；停用题目 id",
      "1) 200，删除成功\n2) 知识点已删；停用题目仍在（软删除的题不参与归属校验）",
      "P2", "边界", "FR-3-2", "自动化（单测）",
      "只统计 is_active=True 的题目，避免历史停用题把知识点「锁死」导致无法整理"),
]
