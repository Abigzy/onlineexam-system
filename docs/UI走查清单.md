# UI 测试走查清单（模块 11）

> 对应模板 31 个（含 `base.html`）、页面路由 34 条（含 `POST /logout/`），「角色 × 页面」组合 44 个。
> 自动化部分：`scripts/browser_walkthrough.py`（真实 Edge 无头浏览器），本文件第 7 节是最近一次实跑结果。
> 手工部分：第 3 节的角色路径 + 第 4 节矩阵的「交互 / 空错态」列需人工勾选。

---

## 1. 判定口径

| 检查项 | 怎么算通过 | 自动化是否覆盖 |
| --- | --- | --- |
| **可达性** | HTTP 200；管理员首页等约定跳转为 302 到指定落点；越权页为 403 / 302；`<title>` 命中预期关键字 | ✅ |
| **渲染** | 关键区块（表格 / 表单 / 图表面板 / 卡片宿主）真实渲染出**可见**节点；不得停在「加载中」 | ✅ |
| **交互** | 主操作可点：新增 / 保存 / 搜索 / 重置 / 翻页 / 弹窗开关 / 交卷 | ❌ 人工（headless 不保证真实点击，见 §5.4） |
| **空态 / 错误态** | 无数据时给引导文案与下一步入口；接口失败给可读提示，不暴露 Traceback / 原始异常 | ⚠️ 部分（脚本检查指定页面的文案） |
| **权限** | 导航不出现越权入口；直接访问越权 URL 有合理提示（班级页为「权限不足」弹窗） | ✅ |
| **移动端 375px** | `documentElement.scrollWidth ≤ clientWidth`；主按钮完整在视口内；表格横向滚动而非撑破页面；弹窗不超视口 | ✅（遮挡检测受 headless 限制，见 §5.4） |
| **控制台** | 无 SEVERE。忽略：`favicon.ico` 404、第三方 CDN（jsdelivr / Edge 起始页）噪声、用例**预期内**的接口 4xx（如越权页故意触发的 403） | ✅ |

失败判定：任一项不通过即记 FAIL，必须写明「实际 / 期望」，不允许只写「有问题」。

---

## 2. 环境与数据前置

```bash
cd /d/OnlineExam
./.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8023      # 走查专用端口
```

| 项 | 值 |
| --- | --- |
| 统一密码 | `Exam@123456`（可用 `OE_PASS` 覆盖） |
| 账号 | 学生 `student`、教师 `teacher`、管理员 `admin` |
| 服务地址 | 默认 `http://127.0.0.1:8023`，可用 `OE_BASE` 覆盖 |

走查依赖的固定主键（开发库当前数据）：

| 用途 | 主键 | 说明 |
| --- | --- | --- |
| 进行中的考试 | `Exam 13` | 状态 ongoing，但时间窗（10-15 ~ 10-16）已过 → 答题页只能验错误态 |
| 已批改作答 | `Attempt 12` | 作弊证据链页所需 |
| 试卷 | `Paper 13` | 组卷编辑页所需 |
| 题目 | `Question 1` | 题目编辑页所需 |
| 班级 | `Classroom 32` / `31` | 32 是 `student` 所在班；31 是其**非**本班（越权用例） |
| 学生 | `User 3` | 学情详情页 / 用户编辑页所需 |

> 数据变了就改脚本顶部常量（`EXAM_ONGOING` / `ATTEMPT_GRADED` / `CLASSROOM_MINE` / `CLASSROOM_OTHER` 等），不要改断言逻辑。

---

## 3. 三条角色路径（可复跑的具体步骤）

### 3.1 学生（11 步）

- [ ] 1. 打开 `/login/`，用 `student / Exam@123456` 登录 → **预期**：落到 `/`，显示「你好，李同学」+ 待考/成绩统计卡，导航只有 我的考试 / 错题本 / 我的学情 / 每日练习 / 我的班级
- [ ] 2. 点「我的考试」→ 切换 待考 / 已考 / 全部 → **预期**：卡片状态标签与按钮随之变化（待考显示「进入考试」，已考显示「查看结果」）
- [ ] 3. 进入一场考试 `/exams/13/take/` → **预期**：无开放考试时显示「无法进入考试」+ 原因 + 「返回我的考试」，**不得**是空白页或 Traceback
- [ ] 4. （有开放考试时）答题卡点题号切题 → 选选项 → 「标记」→「下一题」→ 顶部计时与「已答 x/y」同步变化
- [ ] 5. 点「交卷」→ 弹窗列出未答数量 → 「继续检查」可关闭 →「确认交卷」→ 跳转结果页
- [ ] 6. 结果页 `/exams/13/result/` → **预期**：总分 / 得分率 / 逐题对错与解析可见
- [ ] 7. 错题本 `/my-wrong-book/` → 选学科筛选 → 展开解析 → **预期**：无数据时空态文案友好
- [ ] 8. 我的学情 `/analytics/me/` → **预期**：掌握度雷达 + 薄弱知识点 + 成绩趋势渲染（图未出时不能停在「加载中」）
- [ ] 9. 每日练习 `/practice/` → 选选项 → 提交 → 看批改结论（答对/答错/自评）→「再做一次」
- [ ] 10. 我的班级 `/classrooms/` → 进本班详情看名单；再把地址改成 `/classrooms/31/`（非本班）→ **预期**：弹「权限不足」，不是空白页
- [ ] 11. 个人中心 `/profile/` → 改昵称保存 → 改密码（旧密码错误时应被拦）→ 右上角头像「退出登录」

### 3.2 教师（14 步）

- [ ] 1. 登录 `teacher` → **预期**：教师工作台，导航为 教学资源 / 组卷与考试 / 批改与分析 / 班级
- [ ] 2. 题库 `/question-bank/` → 学科/题型/难度/来源/知识点筛选 → 关键词搜索 → 翻页 → 重置
- [ ] 3. 题库「新增题目」→ 选题型切换选项区 → 填题干/答案/解析 → 保存 → 回列表可见
- [ ] 4. 题库「导入」弹窗 → 下载模板链接可点 → 选文件 → 确认导入；「导出」Excel / CSV 下载成功
- [ ] 5. AI 出题 `/question-bank/ai-generate/` → 选学科/知识点/数量 → 生成 → 采纳草稿 → 保存入库
- [ ] 6. 知识点 `/knowledge-points/` → 展开树 → 新增子节点 → 保存 → 编辑 → 停用
- [ ] 7. 试卷 `/papers/` → 筛选 → 「新建组卷」→ 勾知识点 / 改蓝图题量 → 试算 → 生成 → 导出
- [ ] 8. 考试管理 `/exams/` → 「新建考试」弹窗（选试卷/时间/参考范围）→ 保存 → 列表筛选与翻页 → 进入「监控」→ 刷新 → 可疑考生标记
- [ ] 9. 批改 `/grading/` → 选考试 → 队列点开一份 → AI 批改 → 改分 → 定稿 → 进「成绩单」
- [ ] 10. 成绩单 `/grading/exams/13/scoreboard/` → 指标卡 / 分布图 / 筛选班级
- [ ] 11. 学情 `/analytics/` → 选考试看班级掌握度 → 点学生进 `/analytics/students/3/` → 诊断
- [ ] 12. 作弊检测 `/proctoring/` → 选考试 → 可疑列表 → 进证据链 `/proctoring/attempts/12/evidence/` → 风险分/命中规则/时间线
- [ ] 13. 班级 `/classrooms/` → 「新建班级」弹窗 → 保存 → 进详情 → 添加成员（单个 / 整班导入）→ 编辑 → 删除
- [ ] 14. 个人中心 / 退出登录

### 3.3 管理员（6 步）

- [ ] 1. 登录 `admin` → **预期**：**直接跳** `/manage/`（不落在教师/学生工作台）
- [ ] 2. 数据总览 `/manage/` → 指标卡 / 待办 / AI 用量
- [ ] 3. 用户管理 `/manage/users/` → 筛选 → 「新建用户」（随机密码生成 + 复制）→ 保存 → 编辑 → 重置密码 → 停用/启用
- [ ] 4. 组织与基础数据 `/manage/org/`、系统配置 `/manage/system/`（AI Provider 诊断按钮）
- [ ] 5. 「教学管理（代管）」→ 题库 / 考试监控 → **预期**：顶部出现黄色「以管理员身份代管教学功能」提示条，可一键返回管理控制台
- [ ] 6. 直接访问教师/学生专属页（如 `/my-exams/`）→ **预期**：403 或跳回首页，不得看到业务内容

---

## 4. 角色 × 页面 × 检查项

图例：`☐` = 待勾选；自动化覆盖项在第 7 节给出实跑结论，人工只需在「交互 / 空错态」上补勾。
所有表格列含义见 §1。

### 4.1 未登录访客

| 页面 | 路径 | 交互重点 | 可达 | 渲染 | 交互 | 空错 | 权限 | 375 | 控制台 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 落地页 | `/` | 开始使用 / 登录 按钮 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 登录页 | `/login/` | 输账号密码、记住我、错误提示 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 注册页 | `/register/` | 角色选择、必填校验、提交 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

### 4.2 学生

| 页面 | 路径 | 交互重点 | 可达 | 渲染 | 交互 | 空错 | 权限 | 375 | 控制台 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 首页（工作台） | `/` | 待考统计卡跳转 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 我的考试 | `/my-exams/` | 分页签切换、进入考试 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 答题页 | `/exams/13/take/` | 答题卡切题、选项、标记、交卷 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 考试结果 | `/exams/13/result/` | 逐题解析展开 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 错题本 | `/my-wrong-book/` | 学科筛选、展开解析 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 我的学情 | `/analytics/me/` | 雷达/趋势渲染 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 每日练习 | `/practice/` | 选项、提交、再做一次 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 我的班级 | `/classrooms/` | 卡片进详情 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 班级详情（本班） | `/classrooms/32/` | 名单查看 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 班级详情（越权） | `/classrooms/31/` | 「权限不足」弹窗 + 返回 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 个人中心 | `/profile/` | 改资料、改密码 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

### 4.3 教师

| 页面 | 路径 | 交互重点 | 可达 | 渲染 | 交互 | 空错 | 权限 | 375 | 控制台 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 首页（工作台） | `/` | 待办跳转 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 题库管理 | `/question-bank/` | 筛选/搜索/翻页/重置/导入导出 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 题目新增 | `/question-bank/new/` | 题型切换、选项增删、保存 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 题目编辑 | `/question-bank/1/edit/` | 回填、保存 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| AI 智能出题 | `/question-bank/ai-generate/` | 生成、采纳、保存 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 知识点管理 | `/knowledge-points/` | 树展开、新增/编辑/停用 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 试卷列表 | `/papers/` | 筛选、搜索、翻页 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 新建组卷 | `/papers/new/` | 知识点勾选、蓝图、试算、生成 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 编辑组卷 | `/papers/13/compose/` | 加载已有卷、保存、导出 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 考试管理 | `/exams/` | 新建考试弹窗、筛选、翻页 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 考试监控 | `/exams/13/monitor/` | 刷新、可疑筛选 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 批改工作台 | `/grading/` | 队列、AI 批改、改分、定稿 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 成绩单 | `/grading/exams/13/scoreboard/` | 筛选、指标卡、分布图 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 学情分析 | `/analytics/` | 选考试、班级掌握度 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 学生学情 | `/analytics/students/3/` | 诊断、趋势 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 作弊检测 | `/proctoring/` | 选考试、可疑列表 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 作弊证据链 | `/proctoring/attempts/12/evidence/` | 刷新、跳回看板 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 班级列表 | `/classrooms/` | 新建班级弹窗、保存 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 班级详情（他人班） | `/classrooms/31/` | 「权限不足」弹窗 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 个人中心 | `/profile/` | 改资料、改密码 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

### 4.4 管理员

| 页面 | 路径 | 交互重点 | 可达 | 渲染 | 交互 | 空错 | 权限 | 375 | 控制台 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 首页 → 控制台 | `/` | 302 到 `/manage/` | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 数据总览 | `/manage/` | 指标卡、待办跳转 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 用户管理 | `/manage/users/` | 筛选、翻页 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 新建用户 | `/manage/users/new/` | 生成/复制密码、保存 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 编辑用户 | `/manage/users/3/edit/` | 回填、重置密码、停用 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 组织与基础数据 | `/manage/org/` | 班级/学科分布 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 系统配置 | `/manage/system/` | AI Provider 诊断 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 代管·题库 | `/question-bank/` | 代管黄条 + 返回控制台 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 代管·考试监控 | `/exams/13/monitor/` | 同上 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 个人中心 | `/profile/` | 改资料、改密码 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

### 4.5 越权（负向用例）

| 角色 | 访问 | 期望 | 勾选 |
| --- | --- | --- | --- |
| 学生 | `/question-bank/` | 403 | ☐ |
| 学生 | `/exams/13/monitor/` | 403 | ☐ |
| 学生 | `/grading/exams/13/scoreboard/` | 403 | ☐ |
| 学生 | `/manage/users/` | 302 → `/` | ☐ |
| 教师 | `/manage/users/` | 302 → `/` | ☐ |
| 教师 | `/my-exams/` | 403 | ☐ |
| 教师 | `/my-wrong-book/` | 403 | ☐ |

### 4.6 模板覆盖对照（31/31）

| 模板 | 路由 | 覆盖方式 |
| --- | --- | --- |
| `base.html` | 全站布局 | 所有页面共用（导航 / 消息 / 页脚） |
| `index.html` | `/` | 自动（未登录落地页 + 三种登录态工作台） |
| `users/login.html` | `/login/` | 自动 |
| `users/register.html` | `/register/` | 自动 |
| `users/profile.html` | `/profile/` | 自动 |
| `users/manage_list.html` | `/manage/users/` | 自动 |
| `users/manage_form.html` | `/manage/users/new/`、`/manage/users/3/edit/` | 自动 |
| `questions/bank.html` | `/question-bank/` | 自动 + 人工交互 |
| `questions/editor.html` | `/question-bank/new/`、`/question-bank/1/edit/` | 自动 + 人工交互 |
| `questions/ai_generate.html` | `/question-bank/ai-generate/` | 自动 + 人工交互（依赖 AI Provider） |
| `questions/knowledge_points.html` | `/knowledge-points/` | 自动 + 人工交互 |
| `papers/list.html` | `/papers/` | 自动 + 人工交互 |
| `papers/compose.html` | `/papers/new/`、`/papers/13/compose/` | 自动 + 人工交互 |
| `exam/list.html` | `/exams/` | 自动 + 人工交互（新建考试弹窗） |
| `exam/monitor.html` | `/exams/13/monitor/` | 自动 |
| `exam/index.html` | `/my-exams/` | 自动 |
| `exam/take.html` | `/exams/13/take/` | 自动（仅错误态；正常作答见 TODO-2） |
| `exam/result.html` | `/exams/13/result/` | 自动 |
| `grading/workbench.html` | `/grading/` | 自动 + 人工交互（AI 批改） |
| `grading/scoreboard.html` | `/grading/exams/13/scoreboard/` | 自动 |
| `grading/wrong_book.html` | `/my-wrong-book/` | 自动 |
| `analytics/dashboard.html` | `/analytics/` | 自动 |
| `analytics/student.html` | `/analytics/me/`、`/analytics/students/3/` | 自动 |
| `recommend/practice.html` | `/practice/` | 自动（真实点击见 `browser_check_practice.py`） |
| `proctoring/dashboard.html` | `/proctoring/` | 自动 |
| `proctoring/evidence.html` | `/proctoring/attempts/12/evidence/` | 自动（含本次修复） |
| `classroom/list.html` | `/classrooms/` | 自动 + 人工交互（新建班级弹窗） |
| `classroom/detail.html` | `/classrooms/32/`、`/classrooms/31/` | 自动（含越权弹窗） |
| `system/dashboard.html` | `/manage/` | 自动 |
| `system/org.html` | `/manage/org/` | 自动 |
| `system/config.html` | `/manage/system/` | 自动 |

---

## 5. AC-35：移动端 375px 结论

### 5.1 逐页结论

**44 个「角色 × 页面」组合，44/44 无横向溢出**（`scrollWidth == clientWidth == 375`）。
逐页明细见第 7 节实跑输出（脚本会自动打印每一页的 `scrollWidth / clientWidth`）。

按角色：

| 角色 | 页面数 | 通过 | 有问题 | 结论 |
| --- | --- | --- | --- | --- |
| 未登录 | 3 | 3 | 0 | 通过 |
| 学生 | 11 | 11 | 0 | 通过 |
| 教师 | 20 | 20 | 0 | 通过 |
| 管理员 | 10 | 10 | 0 | 通过 |

### 5.2 发现问题 1 处，已修

| 页面 | 现象 | 根因 | 改动 |
| --- | --- | --- | --- |
| 作弊证据链 `/proctoring/attempts/12/evidence/` | `scrollWidth=404 > clientWidth=375`，手机端整页横向滚动 | 「命中规则」卡片里直接渲染 `JSON.stringify(signal.evidence)`（如 `{"answered_count":34,"seconds_per_question":0,...}`），是不可断行的长串；容器 287px，内容 360px，把文档撑到 404px | `templates/proctoring/evidence.html:238`：`class="sc-evidence"` → `class="sc-evidence text-break"`（Bootstrap 5 `word-break: break-word`，纯排版类，不动业务逻辑） |

修复前后（同一页面、同一断点、同一账号）：

```
修前：溢出   teacher / 作弊证据链  scrollWidth=404  clientWidth=375
修后：通过   teacher / 作弊证据链  scrollWidth=375  clientWidth=375
```

### 5.3 弹窗与按钮

| 检查 | 页面 | 结果 |
| --- | --- | --- |
| 弹窗不超视口 | 学生·越权提示 `#permModal` | 通过（`left ≥ 0`、`right ≤ clientWidth`） |
| 弹窗不超视口 | 教师·考试管理「新建考试」`#examModal` | 通过 |
| 弹窗不超视口 | 教师·班级列表「新建班级」`#classModal` | 通过 |
| 弹窗不超视口 | 教师·班级详情「权限不足」`#permModal` | 通过 |
| 主按钮完整在视口内且可点 | 题库 `#btn-reset`、班级列表 `#btn-create` | 通过（含遮挡命中检测通过） |
| 主按钮完整在视口内且可点 | 组卷 `#btnCompose` | 通过（`left ≥ 0`、`right ≤ clientWidth`、`pointer-events` 正常）；该按钮在首屏之外，遮挡检测被跳过，见 §5.4 |

### 5.4 本章结论的边界（必须知道的局限）

1. **headless 不能真实滚动**：实测 `window.scrollTo()` / `scrollIntoView()` 执行后 `scrollY` 恒为 0。因此**首屏之外**的按钮无法做「是否被遮挡」的命中检测，脚本对这些元素打印「遮挡检测跳过」，只校验「完整在视口宽度内 + `pointer-events` 非 none」。真机或 DevTools 设备模式下需人工补点一次。
2. **CDN 依赖**：Bootstrap / Bootstrap Icons / ECharts 全部走 `cdn.jsdelivr.net`，本机实测单文件 9~35s。脚本用固定 `user-data-dir` 做磁盘缓存，首屏之后才快；CDN 完全不可用时脚本会打印「Bootstrap CSS 未加载，375px 结论仅供参考」，此时不要采信移动端结论。
3. **只测了一个断点**：375px（iPhone SE/12 mini 一类）。平板 768px 与 320px 极窄屏不在本次范围。

---

## 6. 自动化脚本 `scripts/browser_walkthrough.py`

```bash
# 1) 起服务（另一个终端）
./.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8023 --noreload

# 2) 跑走查（selenium 装在系统 Python，项目 .venv 里没有）
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
    python scripts/browser_walkthrough.py

# 可选：只跑某个角色（student / teacher / admin / anonymous）
OE_ROLES=teacher ... python scripts/browser_walkthrough.py

# 可选：换服务地址 / 密码 / 导出明细
OE_BASE=http://127.0.0.1:8023 OE_PASS=Exam@123456 OE_DUMP=/tmp/wt.json ...
```

做什么：

1. 每个角色用 `requests` 登录取 sessionid（`trust_env=False`，避开系统代理造成的假 404），再把 cookie 注入浏览器；
2. 逐页打开，记录 HTTP 状态、`<title>`、关键区块是否可见、控制台 SEVERE；
3. 用 CDP `Emulation.setDeviceMetricsOverride` 把视口切成 375×812（Chromium 最小窗口约 500px，`set_window_size` 拿不到真 375），量 `scrollWidth / clientWidth` 并定位越界元素；
4. 对配置了主按钮 / 弹窗的页面，额外做按钮在视口内、弹窗不超视口检查；
5. 输出 PASS/FAIL 明细 + 汇总 + 375px 逐页表；有 FAIL 时退出码 1。

不做什么：不点按钮、不发业务请求、不建数据。真实点击交互请用 `scripts/browser_check_practice.py`（它负责「真的点一下」）。

---

## 7. 本次执行记录

### 7.1 走查结果

```
走查目标: http://127.0.0.1:8023
健康检查: HTTP 200

角色: student（账号 student）    → 66 checks
角色: teacher（账号 teacher）    → 118 checks
角色: admin（账号 admin）        → 52 checks
角色: anonymous（账号 未登录）    → 15 checks

===== 汇总: 251 checks, 0 FAIL =====
===== 375px 逐页结论 =====
通过   student / 首页               scrollWidth=375  clientWidth=375
通过   student / 我的考试             scrollWidth=375  clientWidth=375
通过   student / 考试结果             scrollWidth=375  clientWidth=375
通过   student / 错题本             scrollWidth=375  clientWidth=375
通过   student / 我的学情             scrollWidth=375  clientWidth=375
通过   student / 每日练习             scrollWidth=375  clientWidth=375
通过   student / 我的班级             scrollWidth=375  clientWidth=375
通过   student / 班级详情（本班）         scrollWidth=375  clientWidth=375
通过   student / 班级详情（越权）         scrollWidth=375  clientWidth=375
通过   student / 个人中心             scrollWidth=375  clientWidth=375
通过   student / 答题页（不可作答）        scrollWidth=375  clientWidth=375
通过   teacher / 首页               scrollWidth=375  clientWidth=375
通过   teacher / 题库管理             scrollWidth=375  clientWidth=375
通过   teacher / 题目新增             scrollWidth=375  clientWidth=375
通过   teacher / 题目编辑             scrollWidth=375  clientWidth=375
通过   teacher / AI 智能出题          scrollWidth=375  clientWidth=375
通过   teacher / 知识点管理            scrollWidth=375  clientWidth=375
通过   teacher / 试卷列表             scrollWidth=375  clientWidth=375
通过   teacher / 新建组卷             scrollWidth=375  clientWidth=375
通过   teacher / 编辑组卷             scrollWidth=375  clientWidth=375
通过   teacher / 考试管理             scrollWidth=375  clientWidth=375
通过   teacher / 考试监控             scrollWidth=375  clientWidth=375
通过   teacher / 批改工作台            scrollWidth=375  clientWidth=375
通过   teacher / 成绩单              scrollWidth=375  clientWidth=375
通过   teacher / 学情分析             scrollWidth=375  clientWidth=375
通过   teacher / 学生学情             scrollWidth=375  clientWidth=375
通过   teacher / 作弊检测             scrollWidth=375  clientWidth=375
通过   teacher / 作弊证据链            scrollWidth=375  clientWidth=375
通过   teacher / 班级列表             scrollWidth=375  clientWidth=375
通过   teacher / 班级详情（他人班级）       scrollWidth=375  clientWidth=375
通过   teacher / 个人中心             scrollWidth=375  clientWidth=375
通过   admin / 首页→管理控制台           scrollWidth=375  clientWidth=375
通过   admin / 数据总览               scrollWidth=375  clientWidth=375
通过   admin / 用户管理               scrollWidth=375  clientWidth=375
通过   admin / 新建用户               scrollWidth=375  clientWidth=375
通过   admin / 编辑用户               scrollWidth=375  clientWidth=375
通过   admin / 组织与基础数据            scrollWidth=375  clientWidth=375
通过   admin / 系统配置               scrollWidth=375  clientWidth=375
通过   admin / 代管·题库              scrollWidth=375  clientWidth=375
通过   admin / 代管·考试监控            scrollWidth=375  clientWidth=375
通过   admin / 个人中心               scrollWidth=375  clientWidth=375
通过   anonymous / 落地页（未登录）       scrollWidth=375  clientWidth=375
通过   anonymous / 登录页            scrollWidth=375  clientWidth=375
通过   anonymous / 注册页            scrollWidth=375  clientWidth=375

溢出页面 0 / 44
```

耗时约 2 分 15 秒（首次冷启动受 CDN 影响会慢一些）。

### 7.2 配套检查

```
$ ./.venv/Scripts/python.exe manage.py check
System check identified no issues (0 silenced).

$ NODE_BIN=... ./.venv/Scripts/python.exe scripts/check_inline_js.py
检查了 26 个脚本块，失败 0 个
```

---

## 8. 发现与处置

| # | 发现 | 严重度 | 处置 |
| --- | --- | --- | --- |
| 1 | 作弊证据链页 375px 横向溢出（证据 JSON 长串撑破布局） | 中（AC-35 卡点） | **已修**：`templates/proctoring/evidence.html:238` 加 `text-break`，复测 404 → 375 |
| 2 | 越权访问返回 Django 默认 403 页（纯文本「403 Forbidden」，无导航、无返回入口、无站点样式） | 中 | **只记录不修**：需要新增 `templates/403.html` 并配置 handler403，属新增模板 + 视图配置，超出本次「仅响应式类」授权范围。见 TODO-1 |
| 3 | 答题页正常作答路径未实测（库中唯一 ongoing 的 13 号考试时间窗已过，`/api/v1/exams/13/start/` 返回 400「本场考试当前不可作答」） | 中 | 自动化改为验**错误态**（页面给出「无法进入考试」+ 返回入口，无 Traceback，已通过）；正常路径列入 TODO-2 |
| 4 | 3 个模板的 `<table>` 未包 `.table-responsive`（`classroom/detail.html`、`papers/compose.html`、`proctoring/evidence.html` 的第二张表），表体由 JS 注入到 `.panel`（`overflow:hidden`） | 低 | 实测 375px 均未溢出（列少、内容可折行），**保持现状**；后续若加列需补 `table-responsive` |
| 5 | 全站 `favicon.ico` 404 | 极低 | 忽略（脚本已单列，不计 FAIL） |
| 6 | 首页等页面首次加载依赖 jsdelivr CDN（单文件 9~35s） | 低 | 记录：离线/内网环境需本地化静态资源，否则样式与图表不生效 |

---

## 9. 未覆盖 / TODO

- **TODO-1（中）**：新增 `templates/403.html`（沿用 `base.html`，给「权限不足 + 返回上一页/首页」），让越权页不再是裸 403 文本。涉及新增模板 + `handler403` 配置，需单独排期。
- **TODO-2（中）**：准备一场「此刻可作答」的考试（`status=ongoing` 且时间窗覆盖当前），补跑答题页正常路径：答题卡切题 → 选项 → 标记 → 自动保存 → 交卷弹窗 → 结果页。脚本已留好位置，改 `EXAM_ONGOING` 即可复用。
- **TODO-3（低）**：交互项人工补勾。自动化不点按钮，第 4 节矩阵的「交互」列需在真机/浏览器手工过一次（尤其：AI 出题生成、批改组卷试算、整班导入这类有异步耗时的操作）。
- **TODO-4（低）**：真机 375px 复核首屏外按钮遮挡（headless 无法滚动，见 §5.4）。
- **TODO-5（低）**：补 768px（平板）与 320px（极窄）两个断点，目前只有 375px。
