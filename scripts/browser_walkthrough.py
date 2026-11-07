#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模块 11 · UI 测试走查（三角色 + 未登录访客 × 关键页面，真实浏览器）。

对应配套测试项目《UI 走查清单》的自动化部分：登录三种角色（外加一遍未登录访客）→
逐页打开 → 记录

    1. HTTP 状态码（会话内直接请求，绕开浏览器缓存干扰）
    2. 页面标题是否命中预期关键字
    3. 关键区块（表格 / 表单 / 图表面板）是否渲染出来（避免「加载中」卡死）
    4. 控制台 SEVERE（favicon 404、第三方 CDN、预期内接口 4xx 单列，不计入失败）
    5. 375px 视口下 ``scrollWidth > clientWidth``（横向溢出）与越界元素
    6. 375px 下主操作按钮是否完整落在视口内、可点（首屏内才做遮挡命中检测，
       因为 headless Chromium 下 ``scrollTo`` / ``scrollIntoView`` 不生效）
    7. 375px 下弹窗宽度是否超出视口（自动弹出再收起）

依赖：selenium（建议装在用于 UI 验证的独立解释器里，不必装进项目 .venv）：

    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
        python scripts/browser_walkthrough.py

    Windows 下先清掉 HTTP_PROXY / HTTPS_PROXY 等代理变量，再执行同一条命令。

前置：开发服务器已启动，默认 127.0.0.1:8023（可用 OE_BASE 覆盖）：

    ./.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8023

环境变量：
    OE_BASE   服务地址，默认 http://127.0.0.1:8023
    OE_PASS   统一密码，默认 Exam@123456
    OE_ROLES  只跑指定角色，逗号分隔（student / teacher / admin / anonymous）
    OE_DUMP   把明细 JSON 落盘到该路径

退出码：0 全通过 / 1 存在 FAIL / 2 登录失败或服务不可达。

注意（历史坑）：headless Edge 连续两次真实鼠标点击会崩 msedgedriver，
本脚本不点元素，只量布局与读控制台，故不受影响；需要点击的交互验证见
``scripts/browser_check_practice.py``。
"""

import json
import os
import sys
import tempfile
import time

import requests
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options
from selenium.webdriver.support.ui import WebDriverWait

BASE = os.environ.get("OE_BASE", "http://127.0.0.1:8023").rstrip("/")
PASSWORD = os.environ.get("OE_PASS", "Exam@123456")
DESKTOP = (1400, 1200)
MOBILE = (375, 812)

# 走查用固定主键（开发库当前数据，前置条件见配套测试项目《UI 走查清单》）
EXAM_ONGOING = 13          # 进行中的考试
ATTEMPT_GRADED = 12        # 一条已批改的作答（作弊证据页用）
PAPER_ID = 13              # 有题目的试卷
QUESTION_ID = 1            # 任一题目（编辑页用）
CLASSROOM_MINE = 32        # 学生本人所在班级
CLASSROOM_OTHER = 31       # 学生不在的班级（越权提示用例）
STUDENT_ID = 3             # 学情分析详情页的学生

RESULTS = []
MOBILE_ROWS = []
ASSETS = {"bootstrap": False}


# ==================================================================
# 断言与输出
# ==================================================================
def check(tag, cond, actual="", expected=""):
    RESULTS.append((tag, bool(cond), actual, expected))
    print(("  PASS  " if cond else "  FAIL  ") + tag)
    if not cond:
        print("        实际: %r" % (actual,))
        print("        期望: %r" % (expected,))
    return bool(cond)


def info(msg):
    print("        " + msg)


# ==================================================================
# 会话与浏览器
# ==================================================================
def login(username):
    """用 requests 登录，返回 (session, sessionid)；浏览器只需要 sessionid。"""
    s = requests.Session()
    s.trust_env = False  # 系统代理会把请求行改写成绝对 URI，导致假 404
    s.get(BASE + "/login/", timeout=20)
    token = s.cookies.get("csrftoken")
    resp = s.post(BASE + "/login/", timeout=20, allow_redirects=False,
                  data={"username": username, "password": PASSWORD,
                        "csrfmiddlewaretoken": token},
                  headers={"Referer": BASE + "/login/"})
    sid = s.cookies.get("sessionid")
    if not sid or resp.status_code != 302:
        print("登录失败: %s（HTTP %s）BASE=%s" % (username, resp.status_code, BASE))
        sys.exit(2)
    return s, sid


PROFILE_DIR = os.path.join(tempfile.gettempdir(), "oe_walkthrough_profile")


def driver_for(session_id=None):
    opts = Options()
    opts.add_argument("--no-proxy-server")
    opts.add_argument("--proxy-bypass-list=*")
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=%d,%d" % DESKTOP)
    opts.add_argument("--disable-gpu")
    # 固定用户目录：Bootstrap / ECharts 走 jsdelivr CDN，首次拉取可能几十秒，
    # 之后靠磁盘缓存秒开，否则每页都要等 CDN（实测 30s+/次）。
    opts.add_argument("--user-data-dir=" + PROFILE_DIR)
    opts.add_argument("--disk-cache-size=104857600")
    opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    drv = webdriver.Edge(options=opts)
    drv.set_page_load_timeout(180)
    safe_get(drv, BASE + "/login/")
    if session_id:
        drv.add_cookie({"name": "sessionid", "value": session_id, "path": "/"})
    return drv


def safe_get(drv, url):
    """导航；第三方 CDN 慢导致 page load 超时不算页面故障，继续用已渲染的 DOM 检查。"""
    try:
        drv.get(url)
        return True
    except WebDriverException as exc:
        if "timeout" in str(exc).lower():
            info("（资源加载超时，多为第三方 CDN 慢；继续检查已渲染内容）")
            return False
        raise


def bootstrap_ready(drv):
    """Bootstrap CSS 是否真的加载了——没加载时 375px 结论没有意义。"""
    try:
        return bool(drv.execute_script(
            "return Array.prototype.some.call("
            "document.querySelectorAll('link[rel=stylesheet]'),"
            "function(l){return l.href.indexOf('bootstrap')>=0 && !!l.sheet;});"))
    except WebDriverException:
        return False


def wait_ready(drv, selector, timeout=20):
    """等关键区块出现（元素存在且可见）。selector 为 None 时只等 DOM 就绪。"""
    if selector:
        try:
            WebDriverWait(drv, timeout).until(
                lambda d: any(e.is_displayed() for e in d.find_elements(By.CSS_SELECTOR, selector)))
            return True
        except Exception:
            return False
    WebDriverWait(drv, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete")
    return True


def goto(drv, path, selector, timeout=20):
    safe_get(drv, BASE + path)
    ok = wait_ready(drv, selector, timeout)
    time.sleep(0.6)  # 图表 / 动画收尾
    return ok


def drain_logs(drv):
    """清空控制台缓冲，避免上一页（甚至浏览器启动页）的日志串到当前页。"""
    try:
        drv.get_log("browser")
    except WebDriverException:
        pass


def console_errors(drv, ignore_http=()):
    """返回 (本项目的 SEVERE, 可忽略的噪声)。

    ignore_http 里的状态码视为「预期内的接口 4xx」——如越权页故意触发 403
    以便前端弹「权限不足」，浏览器仍会记一条 Failed to load resource。
    """
    logs = drv.get_log("browser")
    ours, noise = [], []
    for log in logs:
        if log.get("level") != "SEVERE":
            continue
        msg = log.get("message", "")
        if "favicon.ico" in msg:
            noise.append("[favicon] " + msg[:120])
        elif any("status of %d" % code in msg for code in ignore_http):
            noise.append("[预期内 4xx] " + msg[:120])
        elif "cdn.jsdelivr.net" in msg or "ntp.msn.cn" in msg or "assets.msn.cn" in msg \
                or "ERR_NAME_NOT_RESOLVED" in msg or "ERR_INTERNET_DISCONNECTED" in msg \
                or "ERR_CONNECTION" in msg:
            noise.append("[第三方资源] " + msg[:120])
        else:
            ours.append(msg[:300])
    return ours, noise


MOBILE_JS = """
const cw = document.documentElement.clientWidth;
const sw = document.documentElement.scrollWidth;
const offenders = [];
document.querySelectorAll('body *').forEach(function (el) {
    const st = getComputedStyle(el);
    if (st.position === 'fixed' || st.display === 'none' || st.visibility === 'hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    if (st.overflowX === 'auto' || st.overflowX === 'scroll') return;  // 表格横滑是刻意设计
    // 两种越界：盒子自己伸出视口；或盒子撑住了但内部内容（长串文本 / 宽子元素）溢出
    const outside = (r.right > cw + 1) || (r.left < -1);
    const inner = (el.clientWidth > 0) && (el.scrollWidth > el.clientWidth + 1);
    if (!outside && !inner) return;
    if (outside) {
        const p = el.parentElement;
        if (p) {
            const pr = p.getBoundingClientRect();
            if ((pr.right > cw + 1) || (pr.left < -1)) return;  // 让最外层背锅，只报根因
        }
    } else if (el.parentElement) {
        const pe = el.parentElement;
        if (pe.clientWidth > 0 && pe.scrollWidth > pe.clientWidth + 1) return;  // 只报最内层
    }
    offenders.push({
        tag: el.tagName.toLowerCase(),
        id: el.id || '',
        cls: (el.getAttribute('class') || '').slice(0, 60),
        left: Math.round(r.left), right: Math.round(r.right), w: Math.round(r.width),
        scrollW: el.scrollWidth, clientW: el.clientWidth,
        text: (el.textContent || '').trim().slice(0, 40)
    });
});
return {cw: cw, sw: sw, iw: window.innerWidth, offenders: offenders.slice(0, 5)};
"""

BUTTON_JS = """
const el = document.querySelector(arguments[0]);
if (!el) return {found: false};
const r = el.getBoundingClientRect();
const cw = document.documentElement.clientWidth;
const cs = getComputedStyle(el);
const inFold = r.top >= 0 && r.bottom <= window.innerHeight;
let hit = null;
if (inFold) {
    const h = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    hit = h ? (h === el || el.contains(h) || h.contains(el)) : false;
}
return {
    found: true, left: Math.round(r.left), right: Math.round(r.right),
    visible: r.width > 0 && r.height > 0,
    pointerEvents: cs.pointerEvents,
    inside: r.left >= -1 && r.right <= cw + 1,
    inFold: inFold,
    hit: hit
};
"""

MODAL_JS = """
const el = document.querySelector(arguments[0]);
if (!el || typeof bootstrap === 'undefined') return {found: !!el, opened: false};
bootstrap.Modal.getOrCreateInstance(el).show();
return {found: true, opened: true};
"""

MODAL_RECT_JS = """
const el = document.querySelector(arguments[0] + ' .modal-dialog');
if (!el) return null;
const r = el.getBoundingClientRect();
return {left: Math.round(r.left), right: Math.round(r.right),
        top: Math.round(r.top), cw: document.documentElement.clientWidth};
"""


def mobile_probe(drv, path, btn=None, modal=None, label=""):
    """切到 375px 量布局，返回 (溢出信息, 详情行)；完成后切回桌面尺寸。

    Chromium 系浏览器有最小窗口宽度（约 500px），``set_window_size(375, ...)``
    在 headless 下拿不到真正的 375 视口，必须用 CDP 的设备度量覆盖模拟手机。
    """
    drv.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",
                        {"width": MOBILE[0], "height": MOBILE[1],
                         "deviceScaleFactor": 2, "mobile": True})
    time.sleep(0.5)
    data = drv.execute_script(MOBILE_JS)
    overflow = data["sw"] > data["cw"] + 1
    row = {
        "page": label, "path": path,
        "client": data["cw"], "scroll": data["sw"],
        "overflow": overflow,
        "offenders": data["offenders"],
        "button": None, "modal": None,
    }
    if btn:
        try:
            b = drv.execute_script(BUTTON_JS, btn)
        except WebDriverException as exc:
            b = {"found": False, "error": str(exc)[:80]}
        row["button"] = b
    if modal:
        try:
            res = drv.execute_script(MODAL_JS, modal)
            if res.get("opened"):
                time.sleep(0.5)
                rect = drv.execute_script(MODAL_RECT_JS, modal)
                row["modal"] = rect
                drv.execute_script(
                    "bootstrap.Modal.getOrCreateInstance(arguments[0]).hide();"
                    "document.querySelectorAll('.modal-backdrop').forEach(function(b){b.remove();});"
                    "document.body.classList.remove('modal-open');", modal)
                time.sleep(0.2)
            else:
                row["modal"] = {"skipped": "Bootstrap JS 不可用或弹窗不存在"}
        except WebDriverException as exc:
            row["modal"] = {"skipped": str(exc)[:80]}
    drv.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
    time.sleep(0.3)
    MOBILE_ROWS.append(row)
    return overflow, row


# ==================================================================
# 用例表
# ==================================================================
def case(role, name, path, title, ready=None, btn=None, modal=None,
         expect=(200,), allow_redirect=None, modal_text=None,
         expect_text=None, ignore_http=()):
    """一条走查用例。

    expect_text: 错误态/空态要求页面出现的可读文案（不是原始异常）。
    ignore_http: 预期内的接口 4xx（浏览器会记一条「Failed to load resource」，
                 属业务预期而非前端故障，单列不算 SEVERE 失败）。
    """
    return dict(role=role, name=name, path=path, title=title, ready=ready,
                btn=btn, modal=modal, expect=expect, expect_text=expect_text,
                allow_redirect=allow_redirect, modal_text=modal_text,
                ignore_http=tuple(ignore_http))


STUDENT_CASES = [
    case("student", "首页", "/", "首页", ".stat-card"),
    case("student", "我的考试", "/my-exams/", "我的考试", "#exam-cards"),
    case("student", "考试结果", "/exams/%d/result/" % EXAM_ONGOING, "考试结果", "#result-host"),
    case("student", "错题本", "/my-wrong-book/", "我的错题本", "#wrong-list"),
    case("student", "我的学情", "/analytics/me/", "学情", "#student-meta"),
    case("student", "每日练习", "/practice/", "每日练习", "#practice-host"),
    case("student", "我的班级", "/classrooms/", "班级", "#class-list"),
    case("student", "班级详情（本班）", "/classrooms/%d/" % CLASSROOM_MINE, "班级详情", "#c-info"),
    case("student", "班级详情（越权）", "/classrooms/%d/" % CLASSROOM_OTHER, "班级详情",
         "#c-info", modal="#permModal", modal_text="权限不足", ignore_http=(403,)),
    case("student", "个人中心", "/profile/", "个人中心", "form"),
    # 答题页：库里没有「此刻可作答」的考试（唯一 ongoing 的 13 号已过时间窗，
# start 接口返回 400），所以自动化只验**错误态**——页面要把「不可作答」讲清楚并给回头路。
    # 正常作答路径（选项 / 交卷 / 计时）需有开放中的考试才能复跑，见清单 TODO。
    case("student", "答题页（不可作答）", "/exams/%d/take/" % EXAM_ONGOING, "答题",
         "#loading-box .bi-slash-circle", expect_text="无法进入考试",
         ignore_http=(400,)),
]

TEACHER_CASES = [
    case("teacher", "首页", "/", "首页", ".stat-card"),
    case("teacher", "题库管理", "/question-bank/", "题库管理", "#question-rows", btn="#btn-reset"),
    case("teacher", "题目新增", "/question-bank/new/", "新建", "#btn-save"),
    case("teacher", "题目编辑", "/question-bank/%d/edit/" % QUESTION_ID, "编辑", "#btn-save"),
    case("teacher", "AI 智能出题", "/question-bank/ai-generate/", "AI 智能出题", "#btn-generate"),
    case("teacher", "知识点管理", "/knowledge-points/", "知识点管理", "#kp-code"),
    case("teacher", "试卷列表", "/papers/", "试卷与组卷", "#listSummary"),
    case("teacher", "新建组卷", "/papers/new/", "组卷", "#blueprintRows", btn="#btnCompose"),
    case("teacher", "编辑组卷", "/papers/%d/compose/" % PAPER_ID, "组卷", "#blueprintRows"),
    case("teacher", "考试管理", "/exams/", "考试管理", "#exam-body", modal="#examModal"),
    case("teacher", "考试监控", "/exams/%d/monitor/" % EXAM_ONGOING, "考试监控", "#attempt-body"),
    case("teacher", "批改工作台", "/grading/", "批改工作台", "#queue-list"),
    case("teacher", "成绩单", "/grading/exams/%d/scoreboard/" % EXAM_ONGOING, "成绩单", "#i-pass"),
    case("teacher", "学情分析", "/analytics/", "学情分析", "#dashboard-host"),
    case("teacher", "学生学情", "/analytics/students/%d/" % STUDENT_ID, "学情", "#student-meta"),
    case("teacher", "作弊检测", "/proctoring/", "作弊行为检测", "#kpi-row"),
    case("teacher", "作弊证据链", "/proctoring/attempts/%d/evidence/" % ATTEMPT_GRADED,
         "作答证据链", "#risk-score"),
    case("teacher", "班级列表", "/classrooms/", "班级", "#class-list", btn="#btn-create",
         modal="#classModal"),
    # teacher 名下没有班级，31 号班的授课教师是 net_teacher → 越权，应弹「权限不足」
    case("teacher", "班级详情（他人班级）", "/classrooms/%d/" % CLASSROOM_OTHER, "班级详情",
         "#c-info", modal="#permModal", modal_text="权限不足", ignore_http=(403,)),
    case("teacher", "个人中心", "/profile/", "个人中心", "form"),
]

ADMIN_CASES = [
    # 管理员访问首页应 302 到管理控制台（AC-02 / P0-3）
    case("admin", "首页→管理控制台", "/", "数据总览", ".page-head",
         expect=(302,), allow_redirect="/manage/"),
    case("admin", "数据总览", "/manage/", "数据总览", ".page-head"),
    case("admin", "用户管理", "/manage/users/", "用户管理", "#filter-form"),
    case("admin", "新建用户", "/manage/users/new/", "新建用户", "#user-form"),
    case("admin", "编辑用户", "/manage/users/%d/edit/" % STUDENT_ID, "编辑用户", "#user-form"),
    case("admin", "组织与基础数据", "/manage/org/", "组织与基础数据", ".page-head"),
    case("admin", "系统配置", "/manage/system/", "系统配置", ".page-head"),
    case("admin", "代管·题库", "/question-bank/", "题库管理", "#question-rows"),
    case("admin", "代管·考试监控", "/exams/%d/monitor/" % EXAM_ONGOING, "考试监控", "#attempt-body"),
    case("admin", "个人中心", "/profile/", "个人中心", "form"),
]

# 未登录访客（登录前的落地页与入口页，模板 index/login/register 的未登录分支）
ANON_CASES = [
    case("anonymous", "落地页（未登录）", "/", "首页", ".hero-cta"),
    case("anonymous", "登录页", "/login/", "登录", "form"),
    case("anonymous", "注册页", "/register/", "注册", "form"),
]

# 越权 / 重定向用例：(role, 页面, 路径, 期望状态, 期望跳转前缀)
NEGATIVE = {
    "student": [
        ("越权·题库（学生）", "/question-bank/", 403, None),
        ("越权·考试监控（学生）", "/exams/%d/monitor/" % EXAM_ONGOING, 403, None),
        ("越权·成绩单（学生）", "/grading/exams/%d/scoreboard/" % EXAM_ONGOING, 403, None),
        ("越权·用户管理（学生）", "/manage/users/", 302, "/"),
    ],
    "teacher": [
        ("越权·用户管理（教师）", "/manage/users/", 302, "/"),
        ("越权·我的考试（教师）", "/my-exams/", 403, None),
        ("越权·学生错题本（教师）", "/my-wrong-book/", 403, None),
    ],
    "admin": [],
}

NAV_EXPECT = {
    # 角色: (必须出现的导航词, 必须不出现的导航词)
    "student": (["我的考试", "错题本", "每日练习"], ["题库", "用户管理", "数据总览", "考试管理"]),
    "teacher": (["教学资源", "组卷与考试", "班级"], ["用户管理", "数据总览"]),
    "admin": (["数据总览", "用户管理", "系统配置"], []),
}


# ==================================================================
# 走查主流程
# ==================================================================
def run_nav(role, drv):
    drain_logs(drv)
    goto(drv, "/", ".navbar")
    nav = drv.find_element(By.CSS_SELECTOR, ".navbar")
    text = nav.text
    must, must_not = NAV_EXPECT[role]
    check("%s / 导航含角色入口 %s" % (role, must),
          all(w in text for w in must), [w for w in must if w not in text], "全部出现")
    check("%s / 导航不含越权入口 %s" % (role, must_not or "（无）"),
          all(w not in text for w in must_not), [w for w in must_not if w in text], "全部不出现")
    if role == "admin":
        info("管理员首页已重定向到 %s" % drv.current_url)


def run_case(sess, drv, c):
    tag = "%s / %s" % (c["role"], c["name"])
    print("\n--- %s (%s) ---" % (c["name"], c["path"]))

    # 1) 状态码：跟随重定向看落点
    try:
        http = sess.get(BASE + c["path"], timeout=40, allow_redirects=False)
    except requests.RequestException as exc:
        check(tag + " 状态码", False, type(exc).__name__, c["expect"])
        return

    if c["allow_redirect"]:
        check(tag + " 状态码（允许 %s 重定向）" % c["allow_redirect"],
              http.status_code in c["expect"]
              and c["allow_redirect"] in (http.headers.get("Location") or ""),
              "%s → %s" % (http.status_code, http.headers.get("Location")), c["allow_redirect"])
    else:
        check(tag + " 状态码", http.status_code in c["expect"],
              http.status_code, "in %s" % (c["expect"],))

    # 2) 渲染与关键区块
    drain_logs(drv)
    try:
        ready_ok = goto(drv, c["path"], c["ready"])
    except WebDriverException as exc:
        check(tag + " 页面可打开", False, str(exc)[:120], "正常加载")
        return

    if c["ready"]:
        check(tag + " 关键区块 %s" % c["ready"], ready_ok, "未出现",
              "渲染出可见节点")
        if not ready_ok:
            body = drv.find_element(By.TAG_NAME, "body").text
            if "加载中" in body:
                info("⚠ 页面仍停在「加载中」：可能是接口未返回，见控制台 SEVERE 明细")

    check(tag + " 标题含「%s」" % c["title"], c["title"] in drv.title, drv.title,
          "含 %s" % c["title"])

    # 3) 控制台
    severe, noise = console_errors(drv, c["ignore_http"])
    check(tag + " 控制台无 SEVERE", not severe, severe, "无")
    for n in noise[:2]:
        info("忽略噪声: " + n)

    # 4) 空态 / 错误态：只对指定页面检查可读文案
    body_text = drv.find_element(By.TAG_NAME, "body").text
    if c["expect_text"]:
        check(tag + " 空/错误态文案含「%s」" % c["expect_text"],
              c["expect_text"] in body_text, body_text[:160], "含可读提示")
        check(tag + " 错误态不暴露原始异常",
              "Traceback" not in body_text and "Exception" not in body_text,
              body_text[:160], "无可读异常")
    if c["modal_text"]:
        # 越权提示：等弹窗出现并核对文案
        try:
            WebDriverWait(drv, 10).until(
                lambda d: d.find_element(By.CSS_SELECTOR, c["modal"]).is_displayed())
            shown = True
        except Exception:
            shown = False
        check(tag + " 越权提示弹窗 %s" % c["modal"], shown, "未弹出", "弹出")
        if shown:
            check(tag + " 弹窗文案含「%s」" % c["modal_text"],
                  c["modal_text"] in body_text, body_text[:120], "含")

    # 5) 移动端 375px
    overflow, row = mobile_probe(drv, c["path"], btn=c["btn"], modal=c["modal"],
                                label="%s / %s" % (c["role"], c["name"]))
    check(tag + " 375px 无横向溢出", not overflow,
          "scrollWidth=%s > clientWidth=%s" % (row["scroll"], row["client"]), "相等")
    if overflow:
        for o in row["offenders"]:
            info("越界元素: <%s id=%r class=%r> left=%s right=%s w=%s"
                 % (o["tag"], o["id"], o["cls"], o["left"], o["right"], o["w"]))

    b = row["button"]
    if b is not None:
        if not b.get("found"):
            check(tag + " 375px 主按钮 %s 存在" % c["btn"], False, b, "存在")
        else:
            check(tag + " 375px 主按钮 %s 完整在视口内" % c["btn"], b.get("inside"),
                  b, "left>=0 且 right<=clientWidth")
            check(tag + " 375px 主按钮 %s 可点（pointer-events 非 none）" % c["btn"],
                  b.get("visible") and b.get("pointerEvents") != "none", b, "可见且可点击")
            if b.get("hit") is None:
                info("（按钮在首屏之外，headless 下无法真实滚动，遮挡检测跳过）")
            else:
                check(tag + " 375px 主按钮 %s 未被遮挡" % c["btn"], b.get("hit"),
                      b, "可命中自身")

    m = row["modal"]
    if isinstance(m, dict) and "right" in m:
        check(tag + " 375px 弹窗不超视口", m["left"] >= -1 and m["right"] <= m["cw"] + 1,
              m, "left>=0 且 right<=clientWidth")
    elif isinstance(m, dict) and m.get("skipped"):
        info("弹窗未测: " + str(m["skipped"]))


def run_negative(sess, role):
    for name, path, want, location in NEGATIVE[role]:
        resp = sess.get(BASE + path, timeout=30, allow_redirects=False)
        if location:
            check("%s / %s 跳转 %s" % (role, name, location),
                  resp.status_code == want
                  and (resp.headers.get("Location") or "").startswith(location),
                  "%s → %s" % (resp.status_code, resp.headers.get("Location")),
                  "%s → %s" % (want, location))
        else:
            check("%s / %s = %s" % (role, name, want), resp.status_code == want,
                  resp.status_code, want)
            if resp.status_code == 403:
                body = resp.text.strip()
                # Django DEBUG 下的默认 403 页只有一行文本，记录实际表现供报告引用
                info("403 响应体长度 %d，首 60 字: %r" % (len(body), body[:60]))


def main():
    print("走查目标: %s" % BASE)
    health = requests.get(BASE + "/api/health/", timeout=10)
    print("健康检查: HTTP %s\n" % health.status_code)

    only = {r.strip() for r in os.environ.get("OE_ROLES", "").split(",") if r.strip()}
    groups = [
        ("student", "student", STUDENT_CASES, True),
        ("teacher", "teacher", TEACHER_CASES, True),
        ("admin", "admin", ADMIN_CASES, True),
        (None, "anonymous", ANON_CASES, False),
    ]
    for account, role, cases, do_nav in groups:
        if only and role not in only:
            continue
        print("\n" + "=" * 62)
        print("角色: %s（账号 %s）" % (role, account or "未登录"))
        print("=" * 62)
        if account:
            sess, sid = login(account)
        else:
            sess, sid = requests.Session(), None  # 未登录：新会话无 cookie
            sess.trust_env = False
        drv = driver_for(sid)
        try:
            ASSETS["bootstrap"] = ASSETS["bootstrap"] or bootstrap_ready(drv)
            if do_nav:
                run_nav(role, drv)
            for c in cases:
                run_case(sess, drv, c)
            if do_nav:
                run_negative(sess, role)
        finally:
            drv.quit()
        time.sleep(0.5)  # 给 msedgedriver 收尾，避免下一个会话偶发启动失败

    failed = [r for r in RESULTS if not r[1]]
    print("\n===== 汇总: %d checks, %d FAIL =====" % (len(RESULTS), len(failed)))
    for tag, _, actual, expected in failed:
        print("FAIL %s | 实际: %r | 期望: %r" % (tag, actual, expected))

    print("\n===== 375px 逐页结论 =====")
    overflowed = [r for r in MOBILE_ROWS if r["overflow"]]
    for row in MOBILE_ROWS:
        flag = "溢出" if row["overflow"] else "通过"
        print("%-4s %-26s scrollWidth=%-4s clientWidth=%s"
              % (flag, row["page"], row["scroll"], row["client"]))
    print("\n溢出页面 %d / %d" % (len(overflowed), len(MOBILE_ROWS)))
    if not ASSETS["bootstrap"]:
        print("⚠ Bootstrap CSS 未加载（jsdelivr CDN 不可用），上表 375px 结论仅供参考")

    if os.environ.get("OE_DUMP"):
        with open(os.environ["OE_DUMP"], "w", encoding="utf-8") as fh:
            json.dump({"results": RESULTS, "mobile": MOBILE_ROWS},
                      fh, ensure_ascii=False, indent=2)
        print("明细已写入 %s" % os.environ["OE_DUMP"])

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
