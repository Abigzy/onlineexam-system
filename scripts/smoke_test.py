# -*- coding: utf-8 -*-
"""端到端冒烟测试（HTTP 真实请求，需要先跑起开发服务器）。

覆盖：
  A. 页面可达性（匿名 302 跳登录 → 会话登录 → 200）
  B. JWT 全链路：知识点 → AI 生成草稿 → 批量入库 → 条件筛选 → 统计
  C. AI 适配层：providers / test-connection / chat / usage（无 Key 时走 mock）
  D. 导入模板 / 导出（csv / xlsx）
  E. 试卷与组卷（模块三）：家底矩阵 → 试算 → 组卷 → 看/改题 → 调分 → 题序
     → 复制 → 状态流转 → 分析 → AI 说明 → 导出 → 清理
  F. 在线考试（模块四）：建考试 → 发布 → 学生开考（**校验下发不含答案**）
     → 暂存 → 心跳 → 行为上报（切屏/粘贴）→ 续考恢复 → 交卷 → 结果
     → 教师看板（概览 / 明细 / 逐题答案）→ 权限边界 → 归档
  G. 判分与成绩（模块五）：客观题自动判分 → 汇总口径（总分=客观+主观）
     → 教师成绩单 → 未出分学生不可见 → 教师复核改分留痕 → 出分
     → 学生可见 → 批改队列 / 班级成绩单 / AI 校准 / 错题本 → 页面 → 权限边界
  H. 学情分析（模块六）：可分析考试列表 → 班级总览（分布 / 参与度）→ 逐题得分率
     与区分度 → 个人掌握度雷达 → 成绩趋势（回归预测）→ GET 总览不自动调 AI
     → POST 生成诊断 → 再 POST 命中缓存 → 页面 → 参数校验 → 权限边界
  I. 个性化推荐（模块七）：每日练习清单 → GET 不调大模型 → 卡片含匹配度与理由
     → 结果落在薄弱知识点上 → 当天可复现 → 换一批不重复 → POST 让 AI 改写理由
     → 命中缓存 → 参数校验 → 页面 → 权限边界
  J. 作弊行为检测（模块八）：有行为数据的考试列表 → 班级可疑考生报告 → 埋点落到作答上
     → 逐条证据链（含埋点与聚合计数一致性核对）→ 规则目录与阈值 → 参数校验
     → 页面 → 权限边界
  K. 班级管理（模块十）：建班 → 学生分班（按学号导入）→ 成员管理 → 详情/改班
     → 按班级过滤成绩单与学情 → 学生建班被拒 → 归档
  L. 权限边界：学生访问教师资源被拒
  M. 管理端（模块十一补测）：页面可达 / 管理员权限 / 角色·学科·班级三类筛选
  N. 清理本次写入的测试数据

用法：
    终端 1： python manage.py runserver          # 默认 8000
    终端 2： python scripts/smoke_test.py
            python scripts/smoke_test.py http://127.0.0.1:8001   # 自定义端口

默认连 127.0.0.1:8000（与 run_dev.bat、README 一致）；
需要换端口时把 URL 作为第一个参数传进来。

注意：脚本会真实写入数据（1 个知识点 + 3 道题 + 1 份试卷 + 1 场考试），结尾会清理。
题库的 DELETE 是**软删除**（停用，为的是不破坏历史试卷与成绩），所以每次运行
会在库里留下 3 道「已停用」的 AI 题。试卷是硬删除（草稿可删，已发布的不可删）。
模块四那场考试因为已经有作答记录，按业务规则**不能删除、只能归档**，
所以会留下 1 场标题以「[验收]」开头的已归档考试。模块五会在这次作答上判分、
出分并留下 1 条复核记录（`GradingFeedback`），它们挂在同一份作答下，
随该场考试一并留存（用于演示「成绩单 / 错题本 / 校准统计」有真实数据可看）。
模块六会为学生 `student` 生成一份 AI 学情诊断快照（`StudentAnalysisReport`，
scope_key=all）——这也是**故意留下**的演示数据：学情页打开就能看到一份真实诊断。
它按 (student, scope) 唯一，重复跑冒烟只会覆盖、不会堆积。
模块七的「每日练习」**不建表**：推荐清单是确定性算出来的（同一学生 + 同一天 =
同一份清单），所以重复跑冒烟不会留下任何新数据。
模块八会在模块四那场考试的行为上报上**逐条落埋点**（`ExamBehaviorLog`）——
它们挂在同一份作答下随该场考试一并留存，是**故意留下**的演示数据：
作弊检测看板打开就能看到真实埋点与证据链，重复跑冒烟会在同一份作答上追加埋点。
模块十会建一个「[验收] 班级」用于验证，结尾用 DELETE **物理删除**，
不会留下残留（与考试归档不同：班级与成绩无外键关联，删除不影响历史数据）。

如需彻底清除残留，执行：

    python manage.py shell -c "from apps.questions.models import Question,KnowledgePoint;\
Question.objects.filter(source='ai', is_active=False).delete();\
KnowledgePoint.objects.filter(subject='验收').delete()"

    python manage.py shell -c "from apps.exam.models import Exam;\
Exam.objects.filter(title__startswith='[验收]').delete()"

    python manage.py shell -c "from apps.classroom.models import Classroom;\
Classroom.objects.filter(name__startswith='[验收]').delete()"
"""

import sys

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
S = requests.Session()
S.trust_env = False  # 绕过系统代理，否则本地 127.0.0.1 可能被代理拦截
S.proxies = {"http": None, "https": None}

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name} {detail}")


def csrf(session):
    return session.cookies.get("csrftoken", "")


# ==================================================================
# A. 页面可达性
# ==================================================================
print("\n=== A. 页面可达性 ===")
PAGES = ["/", "/login/", "/register/", "/question-bank/", "/question-bank/new/",
         "/question-bank/ai-generate/", "/knowledge-points/", "/api/docs/",
         "/papers/", "/papers/new/", "/proctoring/"]

for path in PAGES:
    if path == "/":
        continue
    # 关键：不跟随重定向，否则 302 会被悄悄跟到登录页变成 200
    r = S.get(BASE + path, timeout=20, allow_redirects=False)
    if path in ("/login/", "/register/", "/api/docs/"):
        check(f"匿名 GET {path}", r.status_code == 200, f"-> {r.status_code}")
    else:
        loc = r.headers.get("Location", "")
        check(f"匿名 GET {path} 跳登录", r.status_code == 302 and "/login/" in loc,
              f"-> {r.status_code} Location={loc}")

# 会话登录（页面通道）
S.get(BASE + "/login/", timeout=20)
r = S.post(BASE + "/login/",
           data={"username": "teacher", "password": "Exam@123456",
                 "csrfmiddlewaretoken": csrf(S)},
           headers={"Referer": BASE + "/login/"},
           allow_redirects=False, timeout=20)
check("页面登录（session）", r.status_code in (301, 302), f"-> {r.status_code}")

for path in ["/", "/question-bank/", "/question-bank/new/", "/question-bank/ai-generate/",
             "/knowledge-points/", "/papers/", "/papers/new/", "/profile/", "/proctoring/"]:
    r = S.get(BASE + path, timeout=20)
    check(f"登录后 GET {path}", r.status_code == 200, f"-> {r.status_code}")

# 页面 HTML 里应带上关键挂载点
r = S.get(BASE + "/question-bank/", timeout=20)
check("题库页含表格容器", "question-table" in r.text or "题库" in r.text)
r = S.get(BASE + "/question-bank/ai-generate/", timeout=20)
check("AI 出题页含生成按钮", "生成" in r.text)

# ==================================================================
# B. JWT 全链路
# ==================================================================
print("\n=== B. JWT 全链路 ===")
api = requests.Session()
api.trust_env = False
api.proxies = {"http": None, "https": None}

r = api.post(BASE + "/api/v1/auth/login/",
             json={"username": "teacher", "password": "Exam@123456"}, timeout=20)
check("API 登录", r.status_code == 200 and r.json().get("code") == 0, f"-> {r.status_code}")
token = r.json()["data"]["access"]
H = {"Authorization": f"Bearer {token}"}

# B1 知识点列表
r = api.get(BASE + "/api/v1/knowledge-points/", headers=H, timeout=20)
body = r.json()
check("知识点列表", r.status_code == 200 and body["code"] == 0,
      f"-> count={body.get('data', {}).get('count')}")

# B2 知识点树
r = api.get(BASE + "/api/v1/knowledge-points/tree/", headers=H, timeout=20)
body = r.json()
roots = body.get("data") or []
check("知识点树", r.status_code == 200 and isinstance(roots, list),
      f"-> 根节点 {len(roots)} 个，含 children={('children' in roots[0]) if roots else 'n/a'}")

# B3 新建知识点（幂等：同名已存在则复用）
kp_payload = {"name": "模块2验收知识点", "subject": "验收", "code": "VERIFY-M2"}
r = api.post(BASE + "/api/v1/knowledge-points/", headers=H, json=kp_payload, timeout=20)
kp_created = r.status_code == 201
if kp_created:
    kp_id = r.json()["data"]["id"]
else:
    rr = api.get(BASE + "/api/v1/knowledge-points/?search=模块2验收知识点", headers=H, timeout=20)
    kp_id = rr.json()["data"]["results"][0]["id"]
check("新建知识点", bool(kp_id), f"-> id={kp_id}（本次{'新建' if kp_created else '复用'}）")

# B4 AI 生成草稿
gen_payload = {
    "subject": "数据结构",
    "knowledge_points": ["栈与队列"],
    "question_types": ["single", "multiple", "judge"],
    "difficulty": 2,
    "count": 3,
    "source_text": "栈是后进先出（LIFO）的线性表，队列是先进先出（FIFO）的线性表。",
}
r = api.post(BASE + "/api/v1/questions/ai-generate/", headers=H, json=gen_payload, timeout=90)
body = r.json()
drafts = (body.get("data") or {}).get("drafts") or []
check("AI 生成草稿", r.status_code == 200 and len(drafts) > 0,
      f"-> 草稿 {len(drafts)} 条, is_mock={(body.get('data') or {}).get('meta', {}).get('is_mock')}")

# B5 批量入库
if drafts:
    items = []
    for d in drafts:
        items.append({
            "type": d.get("type"),
            "stem": d.get("stem"),
            "difficulty": d.get("difficulty", 2),
            "score": d.get("score"),
            "options": d.get("options") or [],
            "reference_answer": d.get("reference_answer", ""),
            "analysis": d.get("analysis", ""),
            "knowledge_point_names": d.get("knowledge_point_names") or ["栈与队列"],
        })
    r = api.post(BASE + "/api/v1/questions/batch/", headers=H,
                 json={"items": items, "subject": "数据结构", "source": "ai"}, timeout=60)
    body = r.json()
    created = (body.get("data") or {}).get("created_count", 0)
    created_ids = (body.get("data") or {}).get("created_ids") or []
    check("批量入库", r.status_code == 201 and created > 0,
          f"-> 入库 {created} 道, errors={(body.get('data') or {}).get('errors')}")
else:
    created_ids = []

# B6 条件筛选
r = api.get(BASE + "/api/v1/questions/?type=single&subject=数据结构", headers=H, timeout=20)
body = r.json()
n_single = body.get("data", {}).get("count", -1)
check("筛选 type=single & subject", r.status_code == 200 and n_single >= 0, f"-> count={n_single}")

# 挑一个「有题目的知识点」和一个「空知识点」对比，避免写死 id
rr = api.get(BASE + "/api/v1/knowledge-points/?page_size=100", headers=H, timeout=20)
kps = rr.json()["data"]["results"]
with_q = next((k for k in kps if k["question_count"] > 0), None)
if with_q:
    r = api.get(BASE + f"/api/v1/questions/?knowledge_point={with_q['id']}", headers=H, timeout=20)
    cnt = r.json()["data"]["count"]
    r2 = api.get(BASE + f"/api/v1/questions/?knowledge_point={kp_id}", headers=H, timeout=20)
    check("按知识点筛选（含子知识点）", r.status_code == 200 and cnt > 0,
          f"-> 「{with_q['name']}」count={cnt}, 空知识点 count={r2.json()['data']['count']}")
else:
    check("按知识点筛选（含子知识点）", False, "-> 找不到有题目的知识点")

r = api.get(BASE + "/api/v1/questions/?difficulty=2&search=栈", headers=H, timeout=20)
check("难度 + 关键字筛选", r.status_code == 200, f"-> count={r.json()['data']['count']}")

# B7 详情（教师视角含答案）
r = api.get(BASE + "/api/v1/questions/", headers=H, timeout=20)
first = (r.json()["data"]["results"] or [None])[0]
if first:
    r = api.get(BASE + f"/api/v1/questions/{first['id']}/", headers=H, timeout=20)
    detail = r.json()["data"]
    check("详情含答案/解析字段", "reference_answer" in detail and "options" in detail,
          f"-> id={first['id']}")

# B8 统计
r = api.get(BASE + "/api/v1/questions/statistics/", headers=H, timeout=20)
body = r.json()["data"]
check("题库统计", r.status_code == 200 and "by_type" in body and "by_difficulty" in body,
      f"-> total={body.get('total')} 平均难度={body.get('average_difficulty')}")

# ==================================================================
# C. AI 适配层
# ==================================================================
print("\n=== C. AI 适配层 ===")
r = api.get(BASE + "/api/v1/ai/providers/", headers=H, timeout=20)
body = r.json()["data"]
keys = [p["key"] for p in body.get("providers", [])]
check("providers 清单", r.status_code == 200 and "deepseek" in keys,
      f"-> current={body.get('current')} active_is_mock={body.get('active_is_mock')} keys={keys}")

r = api.post(BASE + "/api/v1/ai/test-connection/", headers=H, json={}, timeout=60)
body = r.json()
check("test-connection", r.status_code == 200 and "ok" in body["data"],
      f"-> ok={body['data'].get('ok')} mock={body['data'].get('is_mock')}")

r = api.post(BASE + "/api/v1/ai/chat/", headers=H,
             json={"system": "你是助教", "prompt": "用一句话解释什么是栈"}, timeout=60)
check("AI chat 调试", r.status_code == 200 and r.json()["code"] == 0,
      f"-> is_mock={r.json()['data'].get('is_mock')}")

r = api.get(BASE + "/api/v1/ai/usage/", headers=H, timeout=20)
body = r.json()["data"]
check("AI 用量统计", r.status_code == 200 and "totals" in body,
      f"-> calls={body['totals'].get('calls')} mock={body['totals'].get('mock_calls')}")

# ==================================================================
# D. 导入模板 / 导出
# ==================================================================
print("\n=== D. 导入 / 导出 ===")
r = api.get(BASE + "/api/v1/questions/import-template/", headers=H, timeout=20)
has_bom = r.content[:3] == b"\xef\xbb\xbf"
check("下载导入模板", r.status_code == 200 and len(r.content) > 20,
      f"-> {len(r.content)} bytes, UTF-8 BOM={has_bom}")

r = api.get(BASE + "/api/v1/questions/export/?file_format=csv", headers=H, timeout=30)
check("导出 CSV", r.status_code == 200 and len(r.content) > 20,
      f"-> {len(r.content)} bytes")

r = api.get(BASE + "/api/v1/questions/export/?file_format=xlsx", headers=H, timeout=30)
check("导出 XLSX", r.status_code == 200 and len(r.content) > 100,
      f"-> {len(r.content)} bytes")

# ==================================================================
# E. 试卷与组卷（模块三）
# ==================================================================
print("\n=== E. 试卷与组卷（模块三）===")
MODULE3_TITLE = "模块3验收卷"
created_paper_id = None
cloned_paper_id = None

# E1 题库家底：各题型 × 各难度的库存矩阵
r = api.get(BASE + "/api/v1/papers/pool/", headers=H, timeout=20)
body = r.json()
pool = body.get("data") or {}
by_type = pool.get("by_type") or []
empty_cells = [
    f"{row['label']}·难度{d}"
    for row in by_type for d, n in (row.get("by_difficulty") or {}).items() if n == 0
]
check("题库家底矩阵", r.status_code == 200 and bool(by_type),
      f"-> 可用题量 {pool.get('total')}，题型 {len(by_type)} 类，"
      f"空档 {len(empty_cells)} 处")

# E2 智能组卷试算（dry_run，不落库）
compose_payload = {
    "title": MODULE3_TITLE,
    "subject": "数据结构",
    "target_difficulty": 2.0,
    "duration_minutes": 90,
    "blueprint": [
        {"type": "single", "count": 5, "score": 4},
        {"type": "judge", "count": 5, "score": 4},
        {"type": "short", "count": 2, "score": 10},
    ],
    "seed": 2026,
    "dry_run": True,
    "generate_notes": False,
}
r = api.post(BASE + "/api/v1/papers/ai-compose/", headers=H,
             json=compose_payload, timeout=90)
body = r.json()
preview = body.get("data") or {}
check("智能组卷试算（dry_run）", r.status_code == 200 and preview.get("total_score") is not None,
      f"-> 试算 {preview.get('question_count')} 题 / {preview.get('total_score')} 分 / "
      f"实际难度 {preview.get('actual_difficulty')}（目标 2.0）")

# 试算不应留下任何卷子
r_before = api.get(BASE + f"/api/v1/papers/?search={MODULE3_TITLE}", headers=H, timeout=20)
check("试算未落库", r_before.json()["data"]["count"] == 0,
      f"-> 同名卷 {r_before.json()['data']['count']} 份")

# E3 真组卷（复用同一 seed，结果应与试算一致）
compose_payload["dry_run"] = False
r = api.post(BASE + "/api/v1/papers/ai-compose/", headers=H,
             json=compose_payload, timeout=90)
body = r.json()
data = body.get("data") or {}
paper = data.get("paper") or {}
created_paper_id = paper.get("id")
report = data.get("report") or {}
check("智能组卷落库", r.status_code == 201 and bool(created_paper_id),
      f"-> #{created_paper_id} {paper.get('question_count')} 题 / {paper.get('total_score')} 分")

if created_paper_id:
    # 试算与落库应返回同一套字段——否则前端得分两套取数逻辑
    flat_keys = ["question_count", "total_score", "actual_difficulty",
                 "difficulty_deviation", "elapsed_ms", "seed"]
    nested_keys = ["summary", "requested", "picked", "difficulty", "knowledge",
                   "shortage", "adjusted", "feasible"]
    miss_preview = [k for k in flat_keys + nested_keys if k not in preview]
    miss_report = [k for k in flat_keys + nested_keys if k not in report]
    check("试算/落库报告字段一致",
          not miss_preview and not miss_report,
          f"-> 试算缺 {miss_preview or '无'}，落库缺 {miss_report or '无'}")

    # seed 相同 ⇒ 选题相同，这是「可复现」的关键保证
    check("同 seed 组卷可复现", report.get("question_count") == preview.get("question_count"),
          f"-> 试算 {preview.get('question_count')} 题 vs 落库 {report.get('question_count')} 题")

    check("规模偏差在容差内",
          abs(float(paper.get("actual_difficulty") or 0) - 2.0) <= 0.35,
          f"-> 实际难度 {paper.get('actual_difficulty')}（目标 2.0，容差 0.35）")

    # E4 详情（教师视角应含答案）
    r = api.get(BASE + f"/api/v1/papers/{created_paper_id}/", headers=H, timeout=20)
    detail = r.json()["data"]
    items = detail.get("paper_questions") or []
    with_answer = [q for q in items if q.get("reference_answer")]
    check("试卷详情含题目与答案", r.status_code == 200 and len(items) > 0 and len(with_answer) > 0,
          f"-> {len(items)} 题，其中 {len(with_answer)} 题带参考答案")

    # E5 大题分区（三层模型：Paper → PaperSection → PaperQuestion）
    sections = detail.get("sections") or []
    check("自动生成大题分区", len(sections) >= 2,
          f"-> {len(sections)} 个大题：" + "，".join(s.get("label", "")[:14] for s in sections))

    # E6 卷面预览（按大题分组）
    r = api.get(BASE + f"/api/v1/papers/{created_paper_id}/preview/", headers=H, timeout=20)
    preview_body = r.json()["data"]
    check("卷面预览按大题分组",
          r.status_code == 200 and len(preview_body.get("sections") or []) >= 2,
          f"-> {len(preview_body.get('sections') or [])} 组，"
          f"总分 {preview_body.get('total_score')}")

    # E7 从题库加题
    rr = api.get(BASE + "/api/v1/questions/?page_size=1", headers=H, timeout=20)
    extra = (rr.json()["data"]["results"] or [None])[0]
    if extra:
        r = api.post(BASE + f"/api/v1/papers/{created_paper_id}/questions/", headers=H,
                     json={"question_ids": [extra["id"]], "strict": False}, timeout=20)
        added = (r.json().get("data") or {}).get("added_count")
        check("往试卷加题", r.status_code == 200 and added in (0, 1),
              f"-> added={added}（若该题已在卷内则为 0，属正常）")

    # E8 批量调分
    if items:
        first_item = items[0]
        new_score = float(first_item["score"]) + 1
        r = api.post(BASE + f"/api/v1/papers/{created_paper_id}/scores/", headers=H,
                     json={"items": [{"id": first_item["id"], "score": new_score}]}, timeout=20)
        updated = (r.json().get("data") or {}).get("updated_count")
        check("批量调分", r.status_code == 200 and updated == 1,
              f"-> {first_item['score']} → {new_score} 分，总分 {(r.json().get('data') or {}).get('total_score')}")

    # E9 调整题序（倒序）
    if len(items) >= 2:
        reversed_ids = [q["id"] for q in reversed(items)]
        r = api.post(BASE + f"/api/v1/papers/{created_paper_id}/reorder/", headers=H,
                     json={"ordered_ids": reversed_ids}, timeout=20)
        check("调整题目顺序", r.status_code == 200, f"-> {r.json().get('message')}")

    # E10 组卷质量分析（工作台右栏的全部数据）
    r = api.get(BASE + f"/api/v1/papers/{created_paper_id}/analysis/", headers=H, timeout=20)
    analysis = r.json().get("data") or {}
    keys = ["overview", "type_distribution", "difficulty_distribution",
            "knowledge_coverage", "sections", "warnings"]
    missing = [k for k in keys if k not in analysis]
    check("组卷质量分析", r.status_code == 200 and not missing,
          f"-> 缺字段 {missing or '无'}，预警 {len(analysis.get('warnings') or [])} 条")
    cov = analysis.get("knowledge_coverage") or {}
    check("知识点覆盖可用（雷达图数据）",
          cov.get("distinct_count", 0) > 0 and isinstance(cov.get("indicators"), list),
          f"-> 覆盖 {cov.get('distinct_count')} 个知识点，"
          f"雷达轴 {len(cov.get('indicators') or [])} 条")

    # 带上 planned 参数才能算「覆盖率」——计划考的知识点有多少真被考到
    rr = api.get(BASE + "/api/v1/knowledge-points/?page_size=5", headers=H, timeout=20)
    planned_ids = [k["id"] for k in (rr.json()["data"]["results"] or [])]
    if planned_ids:
        qs = "&".join(f"planned={pid}" for pid in planned_ids)
        r = api.get(BASE + f"/api/v1/papers/{created_paper_id}/analysis/?{qs}",
                    headers=H, timeout=20)
        cov2 = ((r.json().get("data") or {}).get("knowledge_coverage") or {})
        check("知识点覆盖率可算",
              r.status_code == 200 and isinstance(cov2.get("coverage_ratio"), (int, float)),
              f"-> 计划 {cov2.get('planned_count')} 个，"
              f"覆盖率 {cov2.get('coverage_ratio')}，未覆盖 {len(cov2.get('missing') or [])} 个")

    # E11 AI 组卷说明
    r = api.post(BASE + f"/api/v1/papers/{created_paper_id}/ai-notes/", headers=H,
                 json={"force": True}, timeout=90)
    body = r.json()
    notes = (body.get("data") or {}).get("notes") or ""
    check("AI 组卷说明", r.status_code == 200 and len(notes) > 30,
          f"-> {len(notes)} 字, is_mock={(body.get('data') or {}).get('is_mock')}")

    # E12 导出（HTML 可打印 / Markdown）
    r = api.get(BASE + f"/api/v1/papers/{created_paper_id}/export/?file_format=html&with_answer=true",
                headers=H, timeout=30)
    check("导出 HTML 卷面", r.status_code == 200 and b"<html" in r.content.lower(),
          f"-> {len(r.content)} bytes")
    r = api.get(BASE + f"/api/v1/papers/{created_paper_id}/export/?file_format=md",
                headers=H, timeout=30)
    check("导出 Markdown 卷面", r.status_code == 200 and len(r.content) > 100,
          f"-> {len(r.content)} bytes")

    # E13 复制（做 A/B 卷）
    r = api.post(BASE + f"/api/v1/papers/{created_paper_id}/clone/", headers=H,
                 json={"title": MODULE3_TITLE + "-B 卷"}, timeout=30)
    cloned = (r.json().get("data") or {})
    cloned_paper_id = cloned.get("id")
    check("复制试卷", r.status_code == 201 and bool(cloned_paper_id),
          f"-> 新卷 #{cloned_paper_id}，题目 {cloned.get('question_count')} 道")

    # E14 状态流转：草稿 → 已发布 → 归档
    r = api.post(BASE + f"/api/v1/papers/{created_paper_id}/status/", headers=H,
                 json={"status": "published"}, timeout=20)
    published = (r.json().get("data") or {})
    check("发布试卷", r.status_code == 200 and published.get("status") == "published",
          f"-> {published.get('status_display')}，published_at={published.get('published_at')}")

    r = api.delete(BASE + f"/api/v1/papers/{created_paper_id}/", headers=H, timeout=20)
    check("已发布试卷禁止删除", r.status_code != 200,
          f"-> {r.status_code} {r.json().get('message') if r.content else ''}")

    r = api.post(BASE + f"/api/v1/papers/{created_paper_id}/status/", headers=H,
                 json={"status": "archived"}, timeout=20)
    check("归档已发布试卷", r.status_code == 200 and (r.json().get("data") or {}).get("status") == "archived",
          f"-> {(r.json().get('data') or {}).get('status_display')}")

# E15 工作台页面能渲染已有试卷（用演示卷 #1）
r = api.get(BASE + "/api/v1/papers/?page_size=1", headers=H, timeout=20)
first_paper = (r.json()["data"]["results"] or [None])[0]
if first_paper:
    r = S.get(BASE + f"/papers/{first_paper['id']}/compose/", timeout=20)
    check("组卷工作台页面渲染", r.status_code == 200 and "compose" in r.text.lower(),
          f"-> /papers/{first_paper['id']}/compose/ {r.status_code}")

r = api.get(BASE + "/api/v1/papers/statistics/", headers=H, timeout=20)
stats = r.json().get("data") or {}
check("试卷统计总览", r.status_code == 200 and "by_status" in stats,
      f"-> 共 {stats.get('total')} 份，平均 {stats.get('avg_question_count')} 题/卷")

# ==================================================================
# F. 在线考试（模块四）—— 发布 → 开考 → 暂存 → 行为 → 交卷 → 看板
# ==================================================================
print("\n=== F. 在线考试（模块四）===")

# 学生会话（页面通道）与学生会话（JWT）
S_stu = requests.Session()
S_stu.trust_env = False
S_stu.proxies = {"http": None, "https": None}
S_stu.get(BASE + "/login/", timeout=20)
S_stu.post(BASE + "/login/",
           data={"username": "student", "password": "Exam@123456",
                 "csrfmiddlewaretoken": csrf(S_stu)},
           headers={"Referer": BASE + "/login/"}, allow_redirects=False, timeout=20)

r = api.post(BASE + "/api/v1/auth/login/",
             json={"username": "student", "password": "Exam@123456"}, timeout=20)
SH = {"Authorization": f"Bearer {r.json()['data']['access']}"}

exam_id = None
attempt_id = None
questions = []

# F1 找一份已发布的试卷来开考（考试只能挂在已发布试卷上）
r = api.get(BASE + "/api/v1/papers/", headers=H,
            params={"status": "published", "page_size": 1}, timeout=20)
candidates = (r.json().get("data") or {}).get("results") or []
paper_for_exam = candidates[0] if candidates else None
check("找到可用于开考的已发布试卷", paper_for_exam is not None,
      f"-> {paper_for_exam['title'] if paper_for_exam else '无（请先跑 seed_exams）'}")

if paper_for_exam:
    # F2 创建考试（草稿）
    r = api.post(BASE + "/api/v1/exams/", headers=H, json={
        "title": "[验收] 模块四联调考试",
        "paper_id": paper_for_exam["id"],
        "description": "冒烟测试自动创建",
        "duration_minutes": 20,
        "max_attempts": 1,
        "allow_resume": True,
        "detect_switch": True,
        "max_switch_count": 2,
        "detect_paste": True,
        "shuffle_questions": True,
    }, timeout=30)
    body = r.json()
    exam = body.get("data") or {}
    exam_id = exam.get("id")
    check("创建考试（草稿）", r.status_code == 201 and exam.get("status") == "draft",
          f"-> #{exam_id} {exam.get('status_display')} / {exam.get('question_count')} 题")

if exam_id:
    # F3 发布考试
    r = api.post(BASE + f"/api/v1/exams/{exam_id}/status/", headers=H,
                 json={"status": "published"}, timeout=20)
    data = r.json().get("data") or {}
    check("发布考试", r.status_code == 200 and data.get("status") == "published"
          and data.get("is_open") is True,
          f"-> {data.get('status_display')}，可作答={data.get('is_open')}")

    # F4 学生视角：能看到这场考试，但看不到任何试卷内容
    r = api.get(BASE + "/api/v1/exams/", headers=SH, params={"page_size": 100}, timeout=20)
    listing = r.json().get("data") or {}
    mine = [row for row in (listing.get("results") or []) if row.get("id") == exam_id]
    row = mine[0] if mine else {}
    leaked = [k for k in ("sections", "questions", "paper", "paper_questions") if k in row]
    check("学生可见这场考试", bool(mine) and row.get("can_start") is True,
          f"-> can_start={row.get('can_start')}，剩余次数={row.get('attempts_left')}")
    check("学生视角不含试卷内容", not leaked, f"-> 泄漏字段：{leaked or '无'}")

    # F5 开考（下发试卷）
    r = api.post(BASE + f"/api/v1/exams/{exam_id}/start/", headers=SH, json={}, timeout=30)
    data = r.json().get("data") or {}
    attempt_id = (data.get("attempt") or {}).get("id")
    questions = data.get("questions") or []
    blob = str(data)
    leaked_answers = [k for k in ("reference_answer", "analysis", "is_correct") if k in blob]
    check("开考并下发试卷", r.status_code == 200 and len(questions) > 0,
          f"-> attempt#{attempt_id}，{len(questions)} 题，剩余 {data.get('remaining_seconds')} 秒")
    check("下发内容不含答案", not leaked_answers, f"-> 泄漏字段：{leaked_answers or '无'}")
    check("下发含大题分区与配置", bool(data.get("sections")) and "config" in data,
          f"-> {len(data.get('sections') or [])} 个大题")

    # F6 重复开考应续考（不新建 attempt）
    r = api.post(BASE + f"/api/v1/exams/{exam_id}/start/", headers=SH, json={}, timeout=30)
    again = ((r.json().get("data") or {}).get("attempt") or {}).get("id")
    check("重复开考走续考", again == attempt_id, f"-> {again} vs {attempt_id}")

if attempt_id:
    # F7 暂存作答（自动保存走的就是这个接口）
    items = []
    for q in questions[:3]:
        if q.get("question_type") in ("single", "multiple", "judge"):
            items.append({"paper_question_id": q["paper_question_id"],
                          "selected_labels": "A", "time_spent_seconds": 8})
        else:
            items.append({"paper_question_id": q["paper_question_id"],
                          "answer_text": "冒烟测试作答内容", "time_spent_seconds": 12})
    r = api.post(BASE + f"/api/v1/attempts/{attempt_id}/answers/", headers=SH,
                 json={"items": items}, timeout=20)
    data = r.json().get("data") or {}
    check("批量暂存答案", r.status_code == 200 and data.get("saved") == len(items),
          f"-> 保存 {data.get('saved')} 题，进度 {data.get('progress', {}).get('percent')}%")

    # F8 单题暂存 + 标记待复查
    r = api.post(BASE + f"/api/v1/attempts/{attempt_id}/save/", headers=SH, json={
        "paper_question_id": questions[0]["paper_question_id"],
        "selected_labels": "a", "is_flagged": True,
    }, timeout=20)
    answer = r.json().get("data", {}).get("answer") or {}
    check("单题暂存（标签标准化 + 标记）",
          answer.get("selected_labels") == "A" and answer.get("is_flagged") is True,
          f"-> 选项={answer.get('selected_labels')}，标记={answer.get('is_flagged')}")

    # F9 心跳：服务端是权威时间
    r = api.post(BASE + f"/api/v1/attempts/{attempt_id}/heartbeat/", headers=SH,
                 json={"client_remaining": 60}, timeout=20)
    hb = r.json().get("data") or {}
    check("心跳返回服务端剩余时间", r.status_code == 200 and hb.get("remaining_seconds", 0) > 0,
          f"-> 服务端 {hb.get('remaining_seconds')} 秒，时钟偏差 {hb.get('clock_drift_seconds')} 秒")

    # F10 行为上报：切屏 / 粘贴
    r = api.post(BASE + f"/api/v1/attempts/{attempt_id}/behavior/", headers=SH,
                 json={"events": [{"type": "switch"}, {"type": "switch"}, {"type": "paste"}]},
                 timeout=20)
    bh = r.json().get("data") or {}
    check("行为上报（切屏 / 粘贴计数）",
          bh.get("switch_count") == 2 and bh.get("paste_count") == 1,
          f"-> 切屏 {bh.get('switch_count')}/{bh.get('max_switch_count')}，粘贴 {bh.get('paste_count')}")

    # F11 断网续考：重新拉回卷面与已存答案
    r = api.get(BASE + f"/api/v1/attempts/{attempt_id}/paper/", headers=SH, timeout=20)
    state = r.json().get("data") or {}
    restored = [q for q in (state.get("questions") or []) if (q.get("answer") or {}).get("is_answered")]
    check("断网续考恢复已存答案", r.status_code == 200 and len(restored) >= 3,
          f"-> 恢复 {len(restored)} 题，剩余 {state.get('remaining_seconds')} 秒")

    # F12 交卷（带上最后一批作答）
    submit_items = []
    for q in questions:
        if q.get("question_type") in ("single", "multiple", "judge"):
            submit_items.append({"paper_question_id": q["paper_question_id"], "selected_labels": "A"})
        else:
            submit_items.append({"paper_question_id": q["paper_question_id"],
                                 "answer_text": "冒烟测试作答内容"})
    r = api.post(BASE + f"/api/v1/attempts/{attempt_id}/submit/", headers=SH,
                 json={"reason": "manual", "items": submit_items}, timeout=30)
    data = r.json().get("data") or {}
    check("交卷（含最后一批作答落盘）",
          r.status_code == 200 and data.get("status") == "submitted"
          and data.get("answered_count") == len(submit_items),
          f"-> {data.get('status_display')}，已答 {data.get('answered_count')}/{data.get('question_count')}")

    # F13 交卷后不能再改答案
    r = api.post(BASE + f"/api/v1/attempts/{attempt_id}/save/", headers=SH,
                 json={"paper_question_id": questions[0]["paper_question_id"],
                       "selected_labels": "A"}, timeout=20)
    check("交卷后禁止修改答案", r.status_code == 400, f"-> {r.status_code} {r.json().get('message')}")

    # F14 结果页：未判分时不显示分数
    r = api.get(BASE + f"/api/v1/attempts/{attempt_id}/result/", headers=SH, timeout=20)
    detail = r.json().get("data") or {}
    check("结果未判分时不公开成绩",
          r.status_code == 200 and detail.get("show_score") is False,
          f"-> show_score={detail.get('show_score')}，状态={detail.get('status_display')}")

if exam_id:
    # F15 教师看板：概览
    r = api.get(BASE + f"/api/v1/exams/{exam_id}/overview/", headers=H, timeout=20)
    ov = r.json().get("data") or {}
    behavior = ov.get("behavior") or {}
    check("教师看板概览", r.status_code == 200 and ov.get("submitted") == 1,
          f"-> 应考 {ov.get('applicants')}，已开始 {ov.get('started')}，已交 {ov.get('submitted')}，"
          f"待判分 {ov.get('pending_grading')}")
    check("看板统计行为记录", behavior.get("switch_total") == 2,
          f"-> 切屏 {behavior.get('switch_total')} 次，粘贴 {behavior.get('paste_total')} 次")

    # F16 教师看板：作答明细
    r = api.get(BASE + f"/api/v1/exams/{exam_id}/attempts/", headers=H, timeout=20)
    board = r.json().get("data") or {}
    rows = board.get("results") or []
    check("教师看板作答明细", r.status_code == 200 and len(rows) == 1,
          f"-> {len(rows)} 条，嫌疑 {rows[0].get('suspicious') if rows else '—'}")

    # F17 教师：逐题明细（批改要用，含参考答案）
    if attempt_id:
        r = api.get(BASE + f"/api/v1/attempts/{attempt_id}/answers/", headers=H, timeout=20)
        answers = r.json().get("data") or {}
        first_row = (answers.get("results") or [{}])[0]
        check("教师可读逐题答案（含参考答案）",
              r.status_code == 200 and answers.get("count") > 0 and "reference_answer" in first_row,
              f"-> {answers.get('count')} 题明细")

    # F18 已有人作答的考试禁止删除
    r = api.delete(BASE + f"/api/v1/exams/{exam_id}/", headers=H, timeout=20)
    check("已作答的考试禁止删除", r.status_code == 400,
          f"-> {r.status_code} {r.json().get('message')}")

    # F19 考试页面渲染
    for path, session_obj in [
        ("/exams/", S),
        (f"/exams/{exam_id}/monitor/", S),
        ("/my-exams/", S_stu),
        (f"/exams/{exam_id}/take/", S_stu),
        (f"/exams/{exam_id}/result/", S_stu),
    ]:
        r = session_obj.get(BASE + path, timeout=20, allow_redirects=False)
        check(f"考试页面 GET {path}", r.status_code == 200, f"-> {r.status_code}")

    # F20 学生不能管考试；教师不能开考
    r = api.post(BASE + "/api/v1/exams/", headers=SH,
                 json={"title": "越权", "paper_id": paper_for_exam["id"]}, timeout=20)
    check("学生创建考试被拒", r.status_code == 403, f"-> {r.status_code}")
    r = api.get(BASE + f"/api/v1/exams/{exam_id}/overview/", headers=SH, timeout=20)
    check("学生查看考务看板被拒", r.status_code == 403, f"-> {r.status_code}")
    r = api.post(BASE + f"/api/v1/exams/{exam_id}/start/", headers=H, json={}, timeout=20)
    check("教师不能以学生身份开考", r.status_code == 403, f"-> {r.status_code}")

    # F21 考试总览
    r = api.get(BASE + "/api/v1/exams/statistics/", headers=H, timeout=20)
    stats = r.json().get("data") or {}
    check("考试总览统计", r.status_code == 200 and "by_status" in stats,
          f"-> 共 {stats.get('total')} 场，累计作答 {stats.get('attempt_total')} 次")

    # F22 清理：已作答的考试不能删，只能归档（这是有意为之的业务规则）
    r = api.post(BASE + f"/api/v1/exams/{exam_id}/status/", headers=H,
                 json={"status": "finished"}, timeout=20)
    r = api.post(BASE + f"/api/v1/exams/{exam_id}/status/", headers=H,
                 json={"status": "archived"}, timeout=20)
    check("清理：归档验收考试", r.status_code == 200
          and (r.json().get("data") or {}).get("status") == "archived",
          f"-> #exam_id={exam_id}（已作答的考试按规则只能归档）")

# ==================================================================
# G. 判分与成绩（模块五）
# ==================================================================
print("\n=== G. 判分与成绩（模块五）===")

if attempt_id:
    # G1 教师判分：客观题自动（不调大模型，快且免费）
    r = api.post(BASE + f"/api/v1/grading/attempts/{attempt_id}/grade/",
                 headers=H, json={}, timeout=60)
    gd = r.json().get("data") or {}
    objective = gd.get("objective") or {}
    check("教师判分（客观题自动）",
          r.status_code == 200 and objective.get("graded", -1) >= 0,
          f"-> 判了 {objective.get('graded')} 道客观题得 {objective.get('score')} 分，"
          f"状态 {gd.get('status_display')}")

    # G2 汇总口径：总分 = 客观 + 主观（模块五的唯一写入口 recalc 保证）
    summary = gd.get("summary") or {}
    check("汇总口径：总分 = 客观 + 主观",
          abs(float(summary.get("score", 0))
              - float(summary.get("objective_score", 0))
              - float(summary.get("subjective_score", 0))) < 0.01,
          f"-> 客观 {summary.get('objective_score')} + 主观 {summary.get('subjective_score')}"
          f" = {summary.get('score')}")

    # G3 教师成绩单：逐题明细（answer_id 是批改复核单题的钥匙）
    r = api.get(BASE + f"/api/v1/grading/attempts/{attempt_id}/report/", headers=H, timeout=30)
    report = r.json().get("data") or {}
    items = report.get("items") or []
    check("教师成绩单（逐题得分）",
          r.status_code == 200 and report.get("visible") is True and len(items) > 0
          and items[0].get("answer_id") is not None,
          f"-> {len(items)} 题，总分 {report.get('score')}/{report.get('total_score')}")

    # G4 未出分：学生拿不到成绩（连逐题对错都不给）
    r = api.get(BASE + f"/api/v1/grading/attempts/{attempt_id}/report/", headers=SH, timeout=30)
    stu = r.json().get("data") or {}
    check("未出分时学生看不到成绩", r.status_code == 200 and stu.get("visible") is False,
          f"-> visible={stu.get('visible')}，published={stu.get('published')}")

    # G5 教师复核一道题：改分 + 评语，并留下审计记录
    target = items[0] if items else None
    if target and target.get("answer_id"):
        cur = float(target.get("score") or 0)
        full = float(target.get("full_score") or 0)
        new_score = 0.0 if cur > 0 else full
        r = api.post(BASE + f"/api/v1/grading/answers/{target['answer_id']}/review/",
                     headers=H, json={"score": new_score, "comment": "冒烟复核：人工改分"},
                     timeout=30)
        R = r.json().get("data") or {}
        fb = R.get("feedback") or {}
        check("教师复核改分并留痕",
              r.status_code == 200 and fb.get("final_score") is not None,
              f"-> {fb.get('original_score')} → {fb.get('final_score')}（动作 {fb.get('action')}）")

        r = api.get(BASE + "/api/v1/grading/feedback/", headers=H,
                    params={"attempt_id": attempt_id}, timeout=20)
        fbs = (r.json().get("data") or {}).get("results") or []
        check("复核记录可回查", r.status_code == 200 and len(fbs) >= 1,
              f"-> {len(fbs)} 条复核记录")

    # G6 出分：状态推进到「已出分」，学生从此可见
    r = api.post(BASE + f"/api/v1/grading/attempts/{attempt_id}/finalize/",
                 headers=H, json={}, timeout=30)
    fd = r.json().get("data") or {}
    check("教师出分（状态推进到已出分）",
          r.status_code == 200 and (fd.get("attempt") or {}).get("status") == "graded",
          f"-> {r.json().get('message')}")

    # G7 出分后学生可见（可见性规则升级：GRADED 一定可见）
    r = api.get(BASE + f"/api/v1/grading/attempts/{attempt_id}/report/", headers=SH, timeout=30)
    stu = r.json().get("data") or {}
    check("出分后学生可见成绩",
          r.status_code == 200 and stu.get("visible") is True
          and len(stu.get("items") or []) > 0,
          f"-> 得分 {stu.get('score')}/{stu.get('total_score')}，及格={stu.get('passed')}")

    # G8 批改队列（工作台左侧）
    r = api.get(BASE + "/api/v1/grading/queue/", headers=H,
                params={"exam_id": exam_id}, timeout=30)
    q = r.json().get("data") or {}
    check("批改队列", r.status_code == 200 and q.get("count") is not None,
          f"-> {q.get('count')} 条待批改")

    # G9 班级成绩单（排名 / 分数段分布）
    r = api.get(BASE + "/api/v1/grading/scoreboard/", headers=H,
                params={"exam_id": exam_id}, timeout=30)
    sb = r.json().get("data") or {}
    sb_sum = sb.get("summary") or {}
    check("班级成绩单（排名 / 分数段）",
          r.status_code == 200 and "results" in sb and "distribution" in sb_sum,
          f"-> {sb.get('count')} 人，平均 {sb_sum.get('avg_score')}，"
          f"及格率 {sb_sum.get('pass_rate')}%")

    # G10 AI 批改校准统计
    r = api.get(BASE + "/api/v1/grading/calibration/", headers=H, timeout=20)
    cal = r.json().get("data") or {}
    check("AI 批改校准统计", r.status_code == 200 and "total_reviews" in cal,
          f"-> 复核 {cal.get('total_reviews')} 条，AI 样本 {cal.get('sample')}，"
          f"采纳率 {cal.get('accept_rate')}")

    # G11 学生错题本
    r = api.get(BASE + "/api/v1/grading/wrong-book/", headers=SH, timeout=30)
    wb = r.json().get("data") or {}
    check("学生错题本", r.status_code == 200 and "results" in wb,
          f"-> {wb.get('count')} 道错题")

    # G12 判分与成绩页面渲染
    for path, sess in [("/grading/", S),
                       (f"/grading/exams/{exam_id}/scoreboard/", S),
                       ("/my-wrong-book/", S_stu)]:
        rr = sess.get(BASE + path, timeout=20, allow_redirects=False)
        check(f"判分页面 GET {path}", rr.status_code == 200, f"-> {rr.status_code}")

    # G13 权限边界：判分 / 出分 / 队列 / 成绩单都是教师专属
    r = api.post(BASE + f"/api/v1/grading/attempts/{attempt_id}/grade/",
                 headers=SH, json={}, timeout=20)
    check("学生判分被拒", r.status_code == 403, f"-> {r.status_code}")
    r = api.get(BASE + "/api/v1/grading/queue/", headers=SH,
                params={"exam_id": exam_id}, timeout=20)
    check("学生看批改队列被拒", r.status_code == 403, f"-> {r.status_code}")
    r = api.get(BASE + "/api/v1/grading/scoreboard/", headers=SH,
                params={"exam_id": exam_id}, timeout=20)
    check("学生看班级成绩单被拒", r.status_code == 403, f"-> {r.status_code}")

# ==================================================================
# H. 学情分析（模块六）
# ==================================================================
print("\n=== H. 学情分析（模块六）===")

# 学生 / 教师各自的用户 id（学生维度接口按 id 访问；跨人 / 非学生 id 要能挡住）
student_id = (api.get(BASE + "/api/v1/auth/me/", headers=SH, timeout=20)
              .json().get("data") or {}).get("id")
teacher_id = (api.get(BASE + "/api/v1/auth/me/", headers=H, timeout=20)
              .json().get("data") or {}).get("id")

# H1 可分析的考试列表（教师，页面选择器）
r = api.get(BASE + "/api/v1/analytics/exams/", headers=H, timeout=30)
opts = r.json().get("data") or {}
check("学情：可分析考试列表", r.status_code == 200 and "results" in opts,
      f"-> {opts.get('count')} 场有作答的考试")

if exam_id:
    # H2 班级学情总览（分数分布 / 参与度 / 薄弱知识点）
    r = api.get(BASE + f"/api/v1/analytics/exams/{exam_id}/report/", headers=H, timeout=30)
    rep = r.json().get("data") or {}
    dist = (rep.get("distribution") or {}).get("stats") or {}
    check("学情：班级学情总览（分布 + 参与度）",
          r.status_code == 200 and "participation" in rep
          and dist.get("graded_count") is not None,
          f"-> 已判 {dist.get('graded_count')} 份，平均 {dist.get('avg_score')}，"
          f"最高 {dist.get('max_score')}")

    # H3 逐题得分率与区分度（前端「各题得分率」图）
    r = api.get(BASE + f"/api/v1/analytics/exams/{exam_id}/questions/", headers=H, timeout=30)
    qa = r.json().get("data") or {}
    q_items = qa.get("items") or []
    check("学情：逐题得分率与区分度",
          r.status_code == 200 and len(q_items) > 0
          and "discrimination" in q_items[0] and "score_rate" in q_items[0],
          f"-> {len(q_items)} 题，薄弱 {qa.get('summary', {}).get('weak_count')} 道")

if student_id:
    # H4 个人掌握度矩阵（雷达 indicator / values 必须等长）
    r = api.get(BASE + f"/api/v1/analytics/students/{student_id}/mastery/",
                headers=H, timeout=30)
    m = r.json().get("data") or {}
    radar = m.get("radar") or {}
    check("学情：个人掌握度矩阵（雷达）",
          r.status_code == 200 and "radar" in m
          and len(radar.get("indicator") or []) == len(radar.get("values") or []),
          f"-> {m.get('summary', {}).get('knowledge_point_count')} 个知识点，"
          f"薄弱 {len(m.get('weak_points') or [])} 个")

    # H5 成绩趋势（含最小二乘回归预测）
    r = api.get(BASE + f"/api/v1/analytics/students/{student_id}/trend/",
                headers=H, timeout=30)
    tr = r.json().get("data") or {}
    check("学情：成绩趋势（回归预测）",
          r.status_code == 200 and "points" in tr and "fit" in tr,
          f"-> {len(tr.get('points') or [])} 个数据点，"
          f"方向 {(tr.get('fit') or {}).get('direction')}")

    # H6 GET 总览不自动调大模型：连调两次，诊断快照必须一模一样
    # （GET 只会回传已有缓存，绝不会顺手生成——否则刷新页面就变成计费器）
    report_url = BASE + f"/api/v1/analytics/students/{student_id}/report/"
    first_get = api.get(report_url, headers=SH, timeout=30)
    second_get = api.get(report_url, headers=SH, timeout=30)
    diag_before = (first_get.json().get("data") or {}).get("diagnosis")
    diag_after = (second_get.json().get("data") or {}).get("diagnosis")
    check("学情：GET 总览不自动调大模型",
          first_get.status_code == 200 and diag_before == diag_after,
          f"-> 两次 GET 诊断一致（{'有缓存' if diag_before else '暂无缓存'}）")

    # H7 POST 生成 AI 诊断：显式指定 mock provider（不联网、不花钱、结果可复现）
    r = api.post(BASE + f"/api/v1/analytics/students/{student_id}/diagnosis/",
                 headers=SH, json={"provider": "mock", "force": True}, timeout=60)
    dg = r.json().get("data") or {}
    check("学情：生成 AI 诊断",
          r.status_code == 200 and bool(dg.get("summary")) and dg.get("cached") is False,
          f"-> {r.json().get('message')}；档次 {dg.get('overall_level')}，"
          f"薄弱 {dg.get('weak_point_count')} 个，降级={dg.get('degraded')}")

    # H8 再 POST 命中缓存（不重复计费）
    r = api.post(BASE + f"/api/v1/analytics/students/{student_id}/diagnosis/",
                 headers=SH, json={"provider": "mock"}, timeout=30)
    dg2 = r.json().get("data") or {}
    check("学情：诊断命中缓存", r.status_code == 200 and dg2.get("cached") is True,
          f"-> {r.json().get('message')}")

    # H9 总览此时带回已有诊断（顶层已展平 weak_points / advice）
    r = api.get(BASE + f"/api/v1/analytics/students/{student_id}/report/",
                headers=SH, timeout=30)
    rep3 = r.json().get("data") or {}
    check("学情：总览带回已有诊断",
          r.status_code == 200 and (rep3.get("diagnosis") or {}).get("advice") is not None,
          f"-> 诊断档次 {(rep3.get('diagnosis') or {}).get('overall_level')}")

    # H10 学情页面渲染（教师仪表盘 / 学生我的学情 / 教师看单生）
    for path, sess in [("/analytics/", S),
                       ("/analytics/me/", S_stu),
                       (f"/analytics/students/{student_id}/", S)]:
        rr = sess.get(BASE + path, timeout=20, allow_redirects=False)
        check(f"学情页面 GET {path}", rr.status_code == 200, f"-> {rr.status_code}")

    # H11 参数校验：limit 传非数字要 400，不能 500
    r = api.get(BASE + f"/api/v1/analytics/students/{student_id}/trend/", headers=SH,
                params={"limit": "abc"}, timeout=20)
    check("学情：limit 非法返回 400", r.status_code == 400, f"-> {r.status_code}")

# H12 权限边界：班级维度是教师专属；学生跨人 / 拿非学生 id 探学情一律 404
r = api.get(BASE + "/api/v1/analytics/exams/", headers=SH, timeout=20)
check("学生看学情班级维度被拒", r.status_code == 403, f"-> {r.status_code}")
if exam_id:
    r = api.get(BASE + f"/api/v1/analytics/exams/{exam_id}/report/", headers=SH, timeout=20)
    check("学生看班级学情被拒", r.status_code == 403, f"-> {r.status_code}")
if teacher_id:
    r = api.get(BASE + f"/api/v1/analytics/students/{teacher_id}/report/",
                headers=SH, timeout=20)
    check("拿教师 id 探学情返回 404", r.status_code == 404, f"-> {r.status_code}")

# ==================================================================
# I. 个性化推荐（模块七）
# ==================================================================
print("\n=== I. 个性化推荐（模块七）===")

practice_url = BASE + "/api/v1/recommend/students/{student_id}/practice/"

# 另一位学生（用于验证「跨人 404」）——教师身份查学生列表
other_student_id = None
r = api.get(BASE + "/api/v1/users/", headers=H,
            params={"role": "student", "page_size": 10}, timeout=20)
if r.status_code == 200:
    for row in ((r.json().get("data") or {}).get("results") or []):
        if row.get("id") != student_id:
            other_student_id = row["id"]
            break

if student_id:
    # 「每日练习只能选已加入的学科」上线后，学生的可选学科 = 所在学科班的学科。
    # 冒烟造的作答都在「数据结构」，所以先把该学生放进一个数据结构学科班，
    # 否则推荐接口会（正确地）拒绝出题，I4/I5 那批断言就失去意义。
    rec_classroom_id = None
    r = api.post(BASE + "/api/v1/classrooms/", headers=H,
                 json={"name": "[验收] 推荐用学科班", "class_type": "subject",
                       "subject": "数据结构", "grade_year": "2026级"}, timeout=20)
    if r.status_code == 201:
        rec_classroom_id = r.json()["data"]["id"]
        api.post(BASE + f"/api/v1/classrooms/{rec_classroom_id}/students/", headers=H,
                 json={"student_ids": [student_id]}, timeout=20)
    check("推荐：学生已加入对应学科班", bool(rec_classroom_id), f"-> {r.status_code}")

    # I1 每日练习清单（本地推荐理由）
    r = api.get(practice_url.format(student_id=student_id), headers=SH, timeout=60)
    pr = r.json().get("data") or {}
    items = pr.get("items") or []
    psummary = pr.get("summary") or {}
    check("推荐：每日练习清单",
          r.status_code == 200 and "items" in pr and "weak_points" in pr,
          f"-> 候选 {psummary.get('candidate_count')} 题，推荐 {len(items)} 题，"
          f"薄弱知识点 {psummary.get('weak_point_count')} 个")

    # I2 GET 绝不自动调大模型（返回里的 ai 必须是 None）
    check("推荐：GET 不自动调大模型", pr.get("ai") is None, f"-> ai={pr.get('ai')}")

    # I3 卡片结构完整：题目 + 匹配度 + 推荐理由
    if items:
        first = items[0]
        check("推荐：卡片含匹配度与推荐理由",
              all(key in first for key in ("question_id", "stem", "type_display",
                                           "difficulty_display", "knowledge_point",
                                           "match_percent", "reason", "reference_answer",
                                           "options", "analysis")),
              f"-> 匹配度 {first.get('match_percent')}%，"
              f"理由：{str(first.get('reason'))[:32]}…")

    # I4 推荐只落在薄弱知识点上
    weak_names = {row.get("name") for row in (pr.get("weak_points") or [])}
    check("推荐：题目都落在薄弱知识点上",
          bool(items) and all(item.get("knowledge_point") in weak_names for item in items),
          f"-> 依据 {len(weak_names)} 个薄弱知识点")

    # I5 同一天内两次请求结果一致（确定性 → 不需要建表缓存）
    r2 = api.get(practice_url.format(student_id=student_id), headers=SH, timeout=60)
    ids1 = [item["question_id"] for item in items]
    ids2 = [item["question_id"]
            for item in ((r2.json().get("data") or {}).get("items") or [])]
    check("推荐：当天结果稳定可复现", r2.status_code == 200 and ids1 == ids2,
          f"-> 两次都是 {len(ids1)} 题且顺序一致")

    # I6 「换一批」翻页不与第一批重复
    r3 = api.get(practice_url.format(student_id=student_id), headers=SH,
                 params={"size": 2, "offset": 2}, timeout=60)
    ids3 = [item["question_id"]
            for item in ((r3.json().get("data") or {}).get("items") or [])]
    check("推荐：换一批不重复给同一道题",
          r3.status_code == 200 and not (set(ids1[:2]) & set(ids3)),
          f"-> 第二批 {len(ids3)} 题，与第一批无交集")

    # I7 同知识点限流：一次推荐里同一知识点最多 2 道
    limit = (pr.get("thresholds") or {}).get("max_per_knowledge_point") or 2
    per_kp = {}
    for item in items:
        per_kp[item["knowledge_point"]] = per_kp.get(item["knowledge_point"], 0) + 1
    check("推荐：同一知识点不堆叠",
          bool(items) and all(count <= limit for count in per_kp.values()),
          f"-> 每个知识点最多 {limit} 道，实际 {sorted(per_kp.values(), reverse=True)}")

    # I8 POST 让大模型改写推荐理由（显式走 mock：不联网、不花钱、可复现）
    r = api.post(practice_url.format(student_id=student_id), headers=SH,
                 json={"provider": "mock", "size": 3}, timeout=60)
    body = r.json()
    ai = (body.get("data") or {}).get("ai") or {}
    ai_items = (body.get("data") or {}).get("items") or []
    check("推荐：AI 改写推荐理由",
          r.status_code == 200 and ai.get("source") == "ai" and not ai.get("degraded")
          and all(item.get("source") == "ai" for item in ai_items),
          f"-> {body.get('message')}；provider={ai.get('provider')}")

    # I9 POST 只换理由不换题（选题由代码决定，不受模型影响）
    check("推荐：AI 只改写理由，不改选题",
          [item["question_id"] for item in ai_items] == ids1[:3],
          f"-> 题目与顺序与 GET 一致（{len(ai_items)} 题）")

    # I10 再 POST 命中缓存（不重复计费）
    r = api.post(practice_url.format(student_id=student_id), headers=SH,
                 json={"provider": "mock", "size": 3}, timeout=30)
    ai2 = ((r.json().get("data") or {}).get("ai") or {})
    check("推荐：推荐理由命中缓存",
          r.status_code == 200 and ai2.get("cached") is True,
          f"-> {r.json().get('message')}")

    # I11 参数校验：size 传非数字要 400，不能 500
    r = api.get(practice_url.format(student_id=student_id), headers=SH,
                params={"size": "abc"}, timeout=20)
    check("推荐：size 非法返回 400", r.status_code == 400, f"-> {r.status_code}")

    # I12 「每日练习」页面（学生）
    rr = S_stu.get(BASE + "/practice/", timeout=20, allow_redirects=False)
    check("推荐页面 GET /practice/", rr.status_code == 200, f"-> {rr.status_code}")

    # I13 权限边界：跨人 / 拿非学生 id 一律 404
    if other_student_id:
        r = api.get(practice_url.format(student_id=other_student_id), headers=SH, timeout=20)
        check("推荐：学生看别人的推荐返回 404", r.status_code == 404, f"-> {r.status_code}")
    if teacher_id:
        r = api.get(practice_url.format(student_id=teacher_id), headers=SH, timeout=20)
        check("推荐：拿教师 id 探推荐返回 404", r.status_code == 404, f"-> {r.status_code}")
    r = api.get(practice_url.format(student_id=999999), headers=SH, timeout=20)
    check("推荐：不存在的学生返回 404", r.status_code == 404, f"-> {r.status_code}")

# ==================================================================
# J. 作弊行为检测（模块八）
# ==================================================================
print("\n=== J. 作弊行为检测（模块八）===")

proctoring_exams_url = BASE + "/api/v1/proctoring/exams/"
proctoring_rules_url = BASE + "/api/v1/proctoring/rules/"

# J1 有行为数据的考试列表（教师）
r = api.get(proctoring_exams_url, headers=H, timeout=20)
pexam_body = r.json().get("data") or {}
pexam_rows = pexam_body.get("results") or []
check("作弊检测：有行为数据的考试列表",
      r.status_code == 200 and any(row["id"] == exam_id for row in pexam_rows),
      f"-> {len(pexam_rows)} 场考试带埋点")

# J2 班级作弊检测报告（可疑考生列表）
prep_row = None
if exam_id:
    r = api.get(BASE + f"/api/v1/proctoring/exams/{exam_id}/report/", headers=H, timeout=60)
    prep = r.json().get("data") or {}
    pov = prep.get("overview") or {}
    check("作弊检测：班级报告返回可疑名单",
          r.status_code == 200 and "rows" in prep and "overview" in prep,
          f"-> 作答 {pov.get('attempt_count')} 份，行为事件 {pov.get('event_total')} 条，"
          f"可疑 {pov.get('suspicious_count')} 人")
    prep_row = next((item for item in (prep.get("rows") or [])
                     if item["attempt_id"] == attempt_id), None)

# J3 埋点确实落到了这次作答上（模块四上报的行为 → 模块八明细）
check("作弊检测：埋点落到作答上",
      prep_row is not None and prep_row.get("has_behavior") and prep_row.get("switch_count") == 2,
      f"-> 切屏 {prep_row.get('switch_count')}，粘贴 {prep_row.get('paste_count')}"
      if prep_row else "-> 未在报告里找到该作答")

# J4 逐条证据链（含「埋点聚合」与「ExamAttempt 聚合计数」一致性核对）
if attempt_id:
    r = api.get(BASE + f"/api/v1/proctoring/attempts/{attempt_id}/evidence/",
                headers=H, timeout=60)
    ev = r.json().get("data") or {}
    timeline = ev.get("timeline") or []
    counter = ev.get("counter_check") or {}
    risk = ev.get("risk") or {}
    check("作弊检测：证据链逐条列出埋点",
          r.status_code == 200 and len(timeline) >= 3,
          f"-> {len(timeline)} 条埋点，风险档 {risk.get('level_display')}")
    check("作弊检测：埋点与聚合计数一致",
          counter.get("consistent") is True,
          f"-> 切屏 {counter.get('switch_logged')}/{counter.get('switch_count')}，"
          f"粘贴 {counter.get('paste_logged')}/{counter.get('paste_count')}")

# J5 规则目录与阈值（可解释的依据 + 异常检测方法）
r = api.get(proctoring_rules_url, headers=H, timeout=20)
rules_body = r.json().get("data") or {}
check("作弊检测：规则目录与阈值说明",
      r.status_code == 200 and len(rules_body.get("rules") or []) >= 5
      and "thresholds" in rules_body,
      f"-> {len(rules_body.get('rules') or [])} 条规则，异常检测 "
      f"{rules_body.get('anomaly_method')}")

# J6 页面（教师会话）
rr = S.get(BASE + "/proctoring/", timeout=20, allow_redirects=False)
check("作弊检测看板 GET /proctoring/", rr.status_code == 200, f"-> {rr.status_code}")

# J7 参数校验：top_n 传非数字要 400，不能 500
if exam_id:
    r = api.get(BASE + f"/api/v1/proctoring/exams/{exam_id}/report/", headers=H,
                params={"top_n": "abc"}, timeout=20)
    check("作弊检测：参数非法返回 400", r.status_code == 400, f"-> {r.status_code}")

# J8 权限边界：监考数据学生不能看
r = api.get(proctoring_exams_url, headers=SH, timeout=20)
check("作弊检测：学生访问被拒", r.status_code == 403, f"-> {r.status_code}")
if attempt_id:
    r = api.get(BASE + f"/api/v1/proctoring/attempts/{attempt_id}/evidence/",
                headers=SH, timeout=20)
    check("作弊检测：学生看证据链被拒", r.status_code == 403, f"-> {r.status_code}")

# ==================================================================
# K. 班级管理（模块十）
# ==================================================================
print("\n=== K. 班级管理（模块十）===")
r = api.get(BASE + "/api/v1/auth/me/", headers=SH, timeout=20)
stu_id = r.json()["data"]["id"]
r = api.post(BASE + "/api/v1/classrooms/", headers=H,
             json={"name": "[验收] 测试班级", "grade_year": "2026级", "subject": "数据结构"},
             timeout=20)
check("班级：教师建班", r.status_code == 201, f"-> {r.status_code}")
classroom_id = r.json()["data"]["id"]

# 题库按科目分类（题库页「科目」下拉的数据源）
r = api.get(BASE + "/api/v1/questions/subjects/", headers=H, timeout=20)
check("题库：按科目分类", r.status_code == 200 and len(r.json()["data"]["results"]) > 0,
      f"-> {len(r.json()['data']['results'])} 个科目")

# 班级类型：行政班 / 学科班，学科班可挂靠行政班
r = api.get(BASE + "/api/v1/classrooms/", headers=H,
            params={"class_type": "admin"}, timeout=20)
admins = r.json()["data"]["results"] if r.status_code == 200 else []
check("班级：按类型筛选行政班", r.status_code == 200
      and all(c["class_type"] == "admin" for c in admins), f"-> {len(admins)} 个")
sub_class_id = None
if admins:
    r = api.post(BASE + "/api/v1/classrooms/", headers=H,
                 json={"name": "[验收] 学科班", "class_type": "subject",
                       "admin_class_id": admins[0]["id"]}, timeout=20)
    ok = r.status_code == 201 and r.json()["data"]["admin_class"]["id"] == admins[0]["id"]
    check("班级：学科班挂靠行政班", ok, f"-> {r.status_code}")
    if ok:
        sub_class_id = r.json()["data"]["id"]

r = api.post(BASE + f"/api/v1/classrooms/{classroom_id}/students/", headers=H,
             json={"student_ids": [stu_id]}, timeout=20)
check("班级：按 id 添加学生", r.status_code == 200 and stu_id in r.json()["data"]["added"],
      f"-> added={r.json()['data']['added']}")

r = api.get(BASE + "/api/v1/classrooms/", headers=H, timeout=20)
check("班级：教师列表可见", r.status_code == 200 and r.json()["data"]["count"] >= 1,
      f"-> count={r.json()['data']['count']}")

r = api.get(BASE + "/api/v1/classrooms/", headers=SH, timeout=20)
check("班级：学生看到自己所在班", r.status_code == 200
      and any(c["id"] == classroom_id for c in r.json()["data"]["results"]), "")

r = api.get(BASE + f"/api/v1/classrooms/{classroom_id}/", headers=H, timeout=20)
check("班级：详情", r.status_code == 200
      and r.json()["data"]["name"] == "[验收] 测试班级", f"-> {r.status_code}")

r = api.patch(BASE + f"/api/v1/classrooms/{classroom_id}/", headers=H,
              json={"grade_year": "2027级"}, timeout=20)
check("班级：局部更新", r.status_code == 200
      and r.json()["data"]["grade_year"] == "2027级", "")

r = api.get(BASE + f"/api/v1/classrooms/{classroom_id}/students/", headers=H, timeout=20)
check("班级：成员列表", r.status_code == 200 and r.json()["data"]["count"] >= 1,
      f"-> count={r.json()['data']['count']}")

# 按班级过滤成绩单与学情（复用模块四那场考试的 exam_id）
r = api.get(BASE + "/api/v1/grading/scoreboard/", headers=H,
            params={"exam_id": exam_id, "classroom_id": classroom_id}, timeout=20)
check("班级：成绩单按班过滤", r.status_code == 200, f"-> {r.status_code}")
r = api.get(BASE + f"/api/v1/analytics/exams/{exam_id}/report/", headers=H,
            params={"classroom_id": classroom_id}, timeout=20)
check("班级：学情按班过滤", r.status_code == 200, f"-> {r.status_code}")

r = api.post(BASE + "/api/v1/classrooms/", headers=SH,
             json={"name": "学生偷建"}, timeout=20)
check("班级：学生建班被拒", r.status_code == 403, f"-> {r.status_code}")

r = api.delete(BASE + f"/api/v1/classrooms/{classroom_id}/", headers=H, timeout=20)
check("班级：删除（物理删除）", r.status_code == 200, f"-> {r.status_code}")
r = api.get(BASE + f"/api/v1/classrooms/{classroom_id}/", headers=H, timeout=20)
check("班级：删除后不可见", r.status_code == 404, f"-> {r.status_code}")
if sub_class_id:
    r = api.delete(BASE + f"/api/v1/classrooms/{sub_class_id}/", headers=H, timeout=20)
    check("班级：删除学科班", r.status_code == 200, f"-> {r.status_code}")

# ==================================================================
# L. 权限边界：学生访问题库应 403
# ==================================================================
print("\n=== L. 权限边界 ===")
r = api.post(BASE + "/api/v1/auth/login/",
             json={"username": "student", "password": "Exam@123456"}, timeout=20)
sh = {"Authorization": f"Bearer {r.json()['data']['access']}"}
r = api.get(BASE + "/api/v1/questions/", headers=sh, timeout=20)
check("学生访问题库被拒", r.status_code == 403, f"-> {r.status_code}")
r = api.get(BASE + "/api/v1/knowledge-points/", headers=sh, timeout=20)
check("学生访问知识点被拒", r.status_code == 403, f"-> {r.status_code}")
r = api.get(BASE + "/api/v1/papers/", headers=sh, timeout=20)
check("学生访问试卷被拒", r.status_code == 403, f"-> {r.status_code}")
r = api.post(BASE + "/api/v1/papers/ai-compose/", headers=sh,
             json={"dry_run": True}, timeout=20)
check("学生调用智能组卷被拒", r.status_code == 403, f"-> {r.status_code}")
r = api.get(BASE + "/question-bank/", headers=sh, timeout=20, allow_redirects=False)
check("学生页面被拒/跳转", r.status_code in (302, 403), f"-> {r.status_code}")

# 页面通道也要验一次（页面走 session，不是 JWT）
S2 = requests.Session()
S2.trust_env = False
S2.proxies = {"http": None, "https": None}
S2.get(BASE + "/login/", timeout=20)
S2.post(BASE + "/login/",
        data={"username": "student", "password": "Exam@123456",
              "csrfmiddlewaretoken": csrf(S2)},
        headers={"Referer": BASE + "/login/"}, allow_redirects=False, timeout=20)
r = S2.get(BASE + "/question-bank/", timeout=20, allow_redirects=False)
check("学生会话访问题库页 403", r.status_code == 403, f"-> {r.status_code}")
r = S2.get(BASE + "/papers/", timeout=20, allow_redirects=False)
check("学生会话访问试卷页 403", r.status_code == 403, f"-> {r.status_code}")

# ==================================================================
# M. 管理端（模块十一补测）：页面可达 / 权限 / 三类筛选
# ==================================================================
print("\n=== M. 管理端 ===")
# 管理员页面会话（S 是教师会话，管理端要单独登一个，避免把教师身份带过去）
SA = requests.Session()
SA.trust_env = False
SA.proxies = {"http": None, "https": None}
SA.get(BASE + "/login/", timeout=20)
r = SA.post(BASE + "/login/",
            data={"username": "admin", "password": "Exam@123456",
                  "csrfmiddlewaretoken": csrf(SA)},
            headers={"Referer": BASE + "/login/"}, allow_redirects=False, timeout=20)
check("管理端：管理员页面登录", r.status_code in (301, 302), f"-> {r.status_code}")

for path in ["/manage/", "/manage/users/", "/manage/org/", "/manage/system/"]:
    r = SA.get(BASE + path, timeout=20, allow_redirects=False)
    check(f"管理端 GET {path}", r.status_code == 200, f"-> {r.status_code}")

# 教师不是管理员，不得进管理端（管理员控制台只对 admin 开放）
r = S.get(BASE + "/manage/users/", timeout=20, allow_redirects=False)
check("管理端：教师访问被拒", r.status_code in (302, 403), f"-> {r.status_code}")

# 筛选：角色 / 学科 / 行政班（三个下拉都在页面上）
r = SA.get(BASE + "/manage/users/?role=student&page_size=5", timeout=20)
check("管理端：按角色筛选", r.status_code == 200 and 'name="role"' in r.text,
      f"-> {r.status_code}")
check("管理端：学科下拉存在", 'name="subject"' in r.text)
check("管理端：班级下拉存在", 'name="classroom"' in r.text)

r = SA.get(BASE + "/manage/users/?subject=数据结构&page_size=5", timeout=20)
check("管理端：按学科筛选", r.status_code == 200, f"-> {r.status_code}")

# 取一个行政班 id 验证班级筛选（教师 token 可读班级列表）
r = api.get(BASE + "/api/v1/classrooms/", headers=H,
            params={"class_type": "admin"}, timeout=20)
admin_classes = r.json()["data"]["results"] if r.status_code == 200 else []
if admin_classes:
    cid = admin_classes[0]["id"]
    r = SA.get(BASE + f"/manage/users/?classroom={cid}&page_size=5", timeout=20)
    check("管理端：按行政班筛选", r.status_code == 200, f"-> {r.status_code}")
else:
    check("管理端：按行政班筛选", True, "跳过（库里没有行政班）")
# 管理员账号不得被筛选条件排除自身以外的越权操作影响：越权由 API 权限层管，页面只读
r = SA.get(BASE + "/manage/users/?classroom=abc", timeout=20)
check("管理端：非法班级参数不报 500", r.status_code == 200, f"-> {r.status_code}")

# ==================================================================
# N. 清理本次写入的测试数据（保持演示库干净）
# ==================================================================
print("\n=== N. 清理 ===")
removed = 0
for qid in created_ids:
    rr = api.delete(BASE + f"/api/v1/questions/{qid}/", headers=H, timeout=20)
    if rr.status_code == 200:
        removed += 1
check("清理本次 AI 入库的题目（软删除）", removed == len(created_ids),
      f"-> 已停用 {removed}/{len(created_ids)} 道")

if kp_created:
    # 知识点是 subject='验收'，教师 token 会被 teacher_subject_scope 过滤成 404
    # （教师只看得到自己任教学科的知识点）。清理属于运维动作，用管理员身份。
    rr = api.post(BASE + "/api/v1/auth/login/",
                  json={"username": "admin", "password": "Exam@123456"}, timeout=20)
    AH = {"Authorization": f"Bearer {rr.json()['data']['access']}"}
    rr = api.delete(BASE + f"/api/v1/knowledge-points/{kp_id}/", headers=AH, timeout=20)
    check("清理本次新建的知识点（管理员身份）", rr.status_code == 200, f"-> {rr.status_code}")
else:
    print("[SKIP] 知识点为已存在，保留")

# 推荐用学科班：本轮为了让学生「有可练学科」临时建的，删掉避免堆积
if globals().get("rec_classroom_id"):
    rr = api.delete(BASE + f"/api/v1/classrooms/{rec_classroom_id}/", headers=H, timeout=20)
    check("清理推荐用学科班", rr.status_code == 200, f"-> {rr.status_code}")

# 试卷：先退回草稿再删（已发布的卷子按业务规则不允许直接删）
for pid in (created_paper_id, cloned_paper_id):
    if not pid:
        continue
    api.post(BASE + f"/api/v1/papers/{pid}/status/", headers=H,
             json={"status": "draft"}, timeout=20)
    rr = api.delete(BASE + f"/api/v1/papers/{pid}/", headers=H, timeout=20)
    check("清理本次创建的试卷", rr.status_code == 200, f"-> #{pid} {rr.status_code}")

if not created_paper_id:
    print("[SKIP] 本次未创建试卷，无需清理")

# ==================================================================
print(f"\n{'=' * 50}")
print(f"结果：{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("端到端冒烟测试全部通过 🎉")
