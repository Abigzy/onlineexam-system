# -*- coding: utf-8 -*-
"""按 Excel 用例编号执行接口级实测（针对 8777 测试服务 + 演示库）。

设计原则：
  * 只创建「TC执行-」前缀的临时数据，执行结束统一清理；
  * 每条用例一个检查点，失败不影响后续；
  * 结果写 docs/.exec_results.json，供 build_xlsx 回填「执行结果」。

用法：
    python -m scripts.testcase_gen.exec_live
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta

import requests

BASE = "http://127.0.0.1:8777"
PWD = "Exam@123456"
TS = time.strftime("%H%M%S")

RESULTS = []
TOKENS = {"admin": None}
PAPERS = []
EXAMS = []
USERS = []

S = requests.Session()
S.trust_env = False  # 本机代理会把请求行写成绝对 URI，导致假 404


def rec(cid, ok, actual, note=""):
    RESULTS.append({"id": cid, "result": "通过" if ok else "失败",
                    "actual": str(actual)[:300], "note": note})
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {cid} {actual}")


def req(method, path, token=None, **kw):
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return S.request(method, BASE + path, headers=headers, timeout=30, **kw)


def body(r):
    try:
        return r.json()
    except Exception:
        return {}


def code(r):
    return body(r).get("code")


def login(username, password=PWD):
    r = req("POST", "/api/v1/auth/login/", json={"username": username, "password": password})
    return body(r).get("data", {}).get("access"), r


def cleanup(admin_token):
    """倒序清理：考试 → 试卷（先退回草稿）→ 用户。"""
    for eid in EXAMS:
        r = req("DELETE", f"/api/v1/exams/{eid}/", token=admin_token)
        if r.status_code >= 400:
            req("POST", f"/api/v1/exams/{eid}/status/", token=admin_token, json={"status": "archived"})
    for pid in PAPERS:
        req("POST", f"/api/v1/papers/{pid}/status/", token=admin_token, json={"status": "draft"})
        req("DELETE", f"/api/v1/papers/{pid}/", token=admin_token)
    for uid in USERS:
        req("DELETE", f"/api/v1/users/{uid}/", token=admin_token)


def main():
    # ---------------- 0. 环境与通用规范 ----------------
    r = req("GET", "/api/health/")
    d = body(r)
    rec("TC-GEN-01", r.status_code == 200 and d.get("code") == 0 and "data" in d,
        f"HTTP {r.status_code} code={d.get('code')} keys={list(d)[:3]}")

    r = req("GET", "/api/v1/questions/")
    rec("TC-GEN-02", r.status_code == 401 and code(r) == 40100,
        f"HTTP {r.status_code} code={code(r)}")

    admin, _ = login("admin")
    TOKENS["admin"] = admin
    teacher, _ = login("teacher")
    os_t, _ = login("os_teacher")
    student, _ = login("student")
    s030, _ = login("s_030")
    if not all([admin, teacher, os_t, student, s030]):
        print("登录失败，终止")
        return

    r = req("GET", "/api/v1/questions/999999/", token=teacher)
    rec("TC-GEN-03", r.status_code == 404 and code(r) == 40400,
        f"HTTP {r.status_code} code={code(r)}")

    sid = body(req("GET", "/api/v1/auth/me/", token=student)).get("data", {}).get("id")
    r = req("GET", f"/api/v1/recommend/students/{sid}/practice/?size=abc", token=student)
    rec("TC-GEN-04", r.status_code == 400 and code(r) == 40000,
        f"HTTP {r.status_code} code={code(r)}")

    r = req("GET", "/api/v1/questions/?page=1&page_size=20", token=teacher)
    d = body(r).get("data", {})
    rec("TC-GEN-05", r.status_code == 200 and {"count", "total_pages", "page", "page_size", "results"} <= set(d),
        f"HTTP {r.status_code} keys={sorted(d)[:6]}")

    r = req("GET", "/api/v1/questions/?page_size=200", token=teacher)
    rec("TC-GEN-05b", body(r).get("data", {}).get("page_size") in (100, 200),
        f"page_size={body(r).get('data', {}).get('page_size')}")

    # 未登录白名单
    r1 = req("POST", "/api/v1/auth/login/", json={"username": "student", "password": PWD})
    r2 = req("GET", "/api/v1/papers/")
    r3 = req("GET", "/api/v1/users/")
    rec("TC-GEN-09", r1.status_code == 200 and r2.status_code == 401 and r3.status_code == 401,
        f"login={r1.status_code} papers={r2.status_code} users={r3.status_code}")

    # ---------------- 1. 注册 / 学号派发 ----------------
    uname = f"tc_exec_{TS}"
    payload = {"username": uname, "email": f"{uname}@example.com", "password": PWD,
               "password2": PWD, "real_name": "执行用例", "grade_year": "2026"}
    r = req("POST", "/api/v1/auth/register/", json=payload)
    d = body(r).get("data", {})
    stu_no = d.get("student_no", "")
    rec("TC-AUTH-01", r.status_code == 201 and len(stu_no) == 8 and stu_no.startswith("2026"),
        f"HTTP {r.status_code} student_no={stu_no}")
    if r.status_code == 201:
        USERS.append(d.get("id"))
    tmp_token, _ = login(uname)

    r = req("POST", "/api/v1/auth/register/", json={**payload, "password": PWD, "password2": PWD})
    msg02 = json.dumps(body(r), ensure_ascii=False)
    rec("TC-AUTH-02", r.status_code == 400 and ("已被占用" in msg02 or "已存在" in msg02),
        f"HTTP {r.status_code} {msg02[:80]}")

    r = req("POST", "/api/v1/auth/register/", json={**payload, "username": "ab", "email": "ab_x@example.com"})
    rec("TC-AUTH-03", r.status_code == 400, f"HTTP {r.status_code}")

    p2 = {**payload, "username": uname + "_g", "email": uname + "_g@example.com", "grade_year": ""}
    r = req("POST", "/api/v1/auth/register/", json=p2)
    rec("TC-AUTH-08", r.status_code == 400, f"缺年级 HTTP {r.status_code}")

    p3 = {**p2, "grade_year": "1999"}
    r = req("POST", "/api/v1/auth/register/", json=p3)
    rec("TC-AUTH-08b", r.status_code == 400, f"非法年级 HTTP {r.status_code}")

    p4 = {**payload, "username": uname + "_f", "email": uname + "_f@example.com", "student_no": "99999999"}
    r = req("POST", "/api/v1/auth/register/", json=p4)
    d4 = body(r).get("data", {})
    rec("TC-AUTH-08B", r.status_code == 201 and d4.get("student_no") != "99999999",
        f"HTTP {r.status_code} student_no={d4.get('student_no')}")
    if r.status_code == 201:
        USERS.append(d4.get("id"))

    # 登录三形态
    t1, _ = login(uname)
    t2, _ = login(f"{uname}@example.com")
    t3, _ = login(stu_no)
    rec("TC-AUTH-10", bool(t1), f"用户名登录 token={'有' if t1 else '无'}")
    rec("TC-AUTH-11", bool(t2), f"邮箱登录 token={'有' if t2 else '无'}")
    rec("TC-AUTH-12", bool(t3), f"学号登录 token={'有' if t3 else '无'}")

    r = req("POST", "/api/v1/auth/login/", json={"username": uname, "password": "Wrong@123"})
    rec("TC-AUTH-13", r.status_code == 401 and code(r) == 40100, f"HTTP {r.status_code} code={code(r)}")

    r = req("POST", "/api/v1/auth/login/", json={"username": "no_such_user_xyz", "password": PWD})
    m1 = body(r).get("message")
    r2 = req("POST", "/api/v1/auth/login/", json={"username": uname, "password": "Wrong@123"})
    m2 = body(r2).get("message")
    rec("TC-AUTH-14", r.status_code == 401 and m1 == m2, f"两条文案一致={m1 == m2}")

    # refresh 轮换
    r = req("POST", "/api/v1/auth/login/", json={"username": uname, "password": PWD})
    ref1 = body(r).get("data", {}).get("refresh")
    r = req("POST", "/api/v1/auth/refresh/", json={"refresh": ref1})
    new = body(r).get("data", {})
    ref2 = new.get("refresh")
    r_old = req("POST", "/api/v1/auth/refresh/", json={"refresh": ref1})
    r_new = req("POST", "/api/v1/auth/refresh/", json={"refresh": ref2})
    rec("TC-AUTH-16", r_new.status_code == 200 and r_old.status_code == 401,
        f"新 refresh={r_new.status_code} 旧 refresh={r_old.status_code}")

    # 登出
    r = req("POST", "/api/v1/auth/login/", json={"username": uname, "password": PWD})
    ref3 = body(r).get("data", {}).get("refresh")
    acc3 = body(r).get("data", {}).get("access")
    req("POST", "/api/v1/auth/logout/", token=acc3, json={"refresh": ref3})
    r = req("POST", "/api/v1/auth/refresh/", json={"refresh": ref3})
    rec("TC-AUTH-17", r.status_code == 401, f"登出后 refresh HTTP {r.status_code}")

    # me + 学号不可改
    r = req("GET", "/api/v1/auth/me/", token=t1)
    before = body(r).get("data", {}).get("student_no")
    r = req("PATCH", "/api/v1/auth/me/", token=t1, json={"student_no": "99999999", "phone": "13800000000"})
    after = body(r).get("data", {}).get("student_no")
    rec("TC-AUTH-18", r.status_code == 200 and after == before and before != "99999999",
        f"student_no {before} -> {after}")

    # 改密
    r = req("POST", "/api/v1/auth/change-password/", token=t1,
            json={"old_password": "Wrong@123", "new_password": "NewPwd@123", "confirm_password": "NewPwd@123"})
    rec("TC-AUTH-19a", code(r) == 10005, f"原密码错 code={code(r)}")
    r = req("POST", "/api/v1/auth/change-password/", token=t1,
            json={"old_password": PWD, "new_password": PWD, "confirm_password": PWD})
    rec("TC-AUTH-19b", r.status_code == 400, f"新旧相同 HTTP {r.status_code}")
    r = req("POST", "/api/v1/auth/change-password/", token=t1,
            json={"old_password": PWD, "new_password": "NewPwd@123", "confirm_password": "NewPwd@123"})
    newtok, _ = login(uname, "NewPwd@123")
    rec("TC-AUTH-19", r.status_code == 200 and bool(newtok), f"改密 HTTP {r.status_code} 新密码登录={bool(newtok)}")
    if newtok:
        tmp_token = newtok

    # 学生越权
    r = req("GET", "/api/v1/users/", token=tmp_token)
    rec("TC-AUTH-22", r.status_code == 403, f"HTTP {r.status_code}")
    r = req("GET", "/api/v1/questions/", token=tmp_token)
    rec("TC-PERM-01", r.status_code == 403, f"HTTP {r.status_code}")

    # ---------------- 2. 管理端建号 ----------------
    def create_user(role="student", grade=None, sno=""):
        p = {"username": f"tc_{role}_{TS}_{int(time.time()*1000)%10000}", "email": f"{TS}{time.time_ns()%100000}@ex.com",
             "real_name": "执行建号", "role": role, "student_no": sno, "password": PWD}
        if grade is not None:
            p["grade_year"] = grade
        rr = req("POST", "/api/v1/users/", token=admin, json=p)
        return rr, p

    r, p = create_user("teacher")
    d = body(r).get("data", {})
    rec("TC-MG-04a", r.status_code == 201 and str(d.get("student_no", "")).startswith("T"),
        f"教师 HTTP {r.status_code} 工号={d.get('student_no')}")
    if r.status_code == 201:
        USERS.append(d.get("id"))

    r, p = create_user("student", grade=None)
    rec("TC-MG-04b", r.status_code == 400 and code(r) == 10014,
        f"缺年级 HTTP {r.status_code} code={code(r)}")

    r, p = create_user("student", grade="2026", sno="20251234")
    rec("TC-MG-04c", r.status_code == 400 and code(r) == 10015,
        f"学号年级不符 HTTP {r.status_code} code={code(r)}")

    r, p = create_user("student", grade="2026")
    d = body(r).get("data", {})
    rec("TC-MG-04d", r.status_code == 201 and str(d.get("student_no", "")).startswith("2026"),
        f"学生 HTTP {r.status_code} 学号={d.get('student_no')}")
    if r.status_code == 201:
        USERS.append(d.get("id"))
        new_uid = d.get("id")

    if new_uid:
        r = req("POST", f"/api/v1/users/{new_uid}/reset-password/", token=admin, json={})
        d = body(r).get("data") or {}
        rec("TC-MG-06", r.status_code == 200 and len(d.get("password", "")) == 12,
            f"HTTP {r.status_code} 密码长度={len(d.get('password', ''))}")

    admin_id = body(req("GET", "/api/v1/auth/me/", token=admin)).get("data", {}).get("id")
    r = req("POST", f"/api/v1/users/{admin_id}/toggle-active/", token=admin)
    rec("TC-MG-09", code(r) == 10010, f"停用自己 code={code(r)}")

    # ---------------- 3. 试卷与组卷 ----------------
    r = req("POST", "/api/v1/papers/", token=teacher,
            json={"title": f"TC执行-空卷-{TS}", "subject": "数据结构", "pass_score": 0})
    empty_pid = body(r).get("data", {}).get("id")
    if empty_pid:
        PAPERS.append(empty_pid)
    rec("TC-PP-01", r.status_code == 201, f"HTTP {r.status_code} id={empty_pid}")

    r = req("POST", f"/api/v1/papers/{empty_pid}/status/", token=teacher, json={"status": "published"})
    rec("TC-PP-07", r.status_code == 400 and code(r) == 13006, f"空卷发布 HTTP {r.status_code} code={code(r)}")

    r = req("POST", "/api/v1/papers/", token=teacher,
            json={"title": f"TC执行-有题卷-{TS}", "subject": "数据结构", "pass_score": 999})
    pid = body(r).get("data", {}).get("id")
    if pid:
        PAPERS.append(pid)
    r = req("POST", f"/api/v1/papers/{pid}/questions/", token=teacher, json={"question_ids": [776, 775, 774]})
    d = body(r).get("data", {})
    rec("TC-PP-02", r.status_code == 200 and d.get("question_count") == 3 and d.get("total_score", 0) > 0,
        f"加题 {d.get('question_count')} 题 / {d.get('total_score')} 分")

    r = req("GET", f"/api/v1/papers/{pid}/", token=teacher)
    detail = body(r).get("data") or {}
    pq = detail.get("paper_questions") or []
    pqid = pq[0].get("id") if pq else None
    r = req("POST", f"/api/v1/papers/{pid}/scores/", token=teacher, json={"items": [{"id": pqid, "score": 0}]})
    rec("TC-PP-04a", r.status_code == 400, f"分值 0 HTTP {r.status_code} code={code(r)}（字段校验层拦截）")
    r = req("POST", f"/api/v1/papers/{pid}/scores/", token=teacher, json={"items": [{"id": pqid, "score": 101}]})
    rec("TC-PP-04b", r.status_code == 400 and code(r) == 13004, f"分值 101 HTTP {r.status_code} code={code(r)}")

    r = req("POST", f"/api/v1/papers/{pid}/status/", token=teacher, json={"status": "published"})
    rec("TC-PP-08", r.status_code == 400 and code(r) == 13007, f"及格分>总分 HTTP {r.status_code} code={code(r)}")

    req("PATCH", f"/api/v1/papers/{pid}/", token=teacher, json={"pass_score": 5})
    r = req("POST", f"/api/v1/papers/{pid}/status/", token=teacher, json={"status": "published"})
    rec("TC-PP-09", r.status_code == 200, f"发布 HTTP {r.status_code}")

    r = req("DELETE", f"/api/v1/papers/{pid}/", token=teacher)
    rec("TC-PP-10", r.status_code == 400 and code(r) == 13001, f"删已发布 HTTP {r.status_code} code={code(r)}")

    r = req("PATCH", f"/api/v1/papers/{pid}/", token=teacher, json={"title": "TC执行-改名"})
    rec("TC-PP-11", r.status_code == 400, f"改已发布 HTTP {r.status_code}")

    r = req("POST", f"/api/v1/papers/{pid}/clone/", token=teacher, json={})
    cid = (body(r).get("data") or {}).get("id")
    if cid:
        PAPERS.append(cid)
    rec("TC-PP-14", r.status_code == 201 and cid and cid != pid, f"克隆 HTTP {r.status_code} new_id={cid}")

    before = body(req("GET", "/api/v1/papers/?page_size=100", token=teacher)).get("data", {}).get("count")
    r = req("POST", "/api/v1/papers/random-compose/", token=teacher,
            json={"total_count": 10, "dry_run": True, "seed": 42})
    after = body(req("GET", "/api/v1/papers/?page_size=100", token=teacher)).get("data", {}).get("count")
    rec("TC-PP-24", r.status_code == 200 and before == after, f"试算 HTTP {r.status_code} 试卷数 {before}->{after}")

    r = req("POST", "/api/v1/papers/random-compose/", token=teacher,
            json={"total_count": 10, "seed": 42})
    d = body(r).get("data", {}).get("paper", {})
    if d.get("id"):
        PAPERS.append(d["id"])
    rec("TC-PP-25", r.status_code == 201 and d.get("generation_mode") == "random" and "notes" not in d,
        f"HTTP {r.status_code} mode={d.get('generation_mode')} 题数={d.get('question_count')}")

    # ---------------- 4. 考试与参与范围 ----------------
    r = req("POST", "/api/v1/exams/", token=teacher, json={"title": "TC执行-无卷"})
    rec("TC-EX-01", r.status_code == 400, f"无试卷 HTTP {r.status_code}")

    r = req("POST", "/api/v1/exams/", token=teacher,
            json={"title": "TC执行-时长", "paper_id": pid, "duration_minutes": 601})
    rec("TC-EX-02", r.status_code == 400, f"时长 601 HTTP {r.status_code}")

    now = datetime.now()
    r = req("POST", "/api/v1/exams/", token=teacher,
            json={"title": "TC执行-时间窗", "paper_id": pid,
                  "start_time": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
                  "end_time": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")})
    rec("TC-EX-03", r.status_code == 400, f"结束早于开始 HTTP {r.status_code}")

    # 草稿卷不能发布考试
    r = req("POST", "/api/v1/papers/", token=teacher,
            json={"title": f"TC执行-草稿卷-{TS}", "subject": "数据结构", "pass_score": 0})
    draft_pid = body(r).get("data", {}).get("id")
    if draft_pid:
        PAPERS.append(draft_pid)
        req("POST", f"/api/v1/papers/{draft_pid}/questions/", token=teacher, json={"question_ids": [776]})
    r = req("POST", "/api/v1/exams/", token=teacher,
            json={"title": "TC执行-草稿卷考试", "paper_id": draft_pid, "open_to_all": True})
    eid_draft = body(r).get("data", {}).get("id")
    if eid_draft:
        EXAMS.append(eid_draft)
    r2 = req("POST", f"/api/v1/exams/{eid_draft}/status/", token=teacher, json={"status": "published"})
    rec("TC-EX-04", r2.status_code == 400 and "草稿" in json.dumps(body(r2), ensure_ascii=False),
        f"发布草稿卷考试 HTTP {r2.status_code}")

    # 面向全体：teacher 只管 16 人
    r = req("POST", "/api/v1/exams/", token=teacher,
            json={"title": f"TC执行-面向全体-{TS}", "paper_id": 11, "open_to_all": True,
                  "start_time": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S"),
                  "end_time": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
                  "duration_minutes": 30})
    eid = body(r).get("data", {}).get("id")
    if eid:
        EXAMS.append(eid)
    r = req("POST", f"/api/v1/exams/{eid}/status/", token=teacher, json={"status": "published"})
    rec("TC-EX-04b", r.status_code == 200, f"发布考试 HTTP {r.status_code}")

    r = req("GET", "/api/v1/exams/", token=s030)
    rec("TC-EX-05a", r.status_code == 200 and str(eid) in json.dumps(body(r)),
        f"班内学生可见={str(eid) in json.dumps(body(r))}")

    r = req("GET", "/api/v1/exams/", token=tmp_token)
    rec("TC-EX-05b", r.status_code == 200 and str(eid) not in json.dumps(body(r)),
        f"班外学生可见={str(eid) in json.dumps(body(r))}")

    r = req("POST", f"/api/v1/exams/{eid}/start/", token=tmp_token)
    rec("TC-EX-27", r.status_code in (403, 404) and code(r) in (10431, 40400),
        f"班外开考 HTTP {r.status_code} code={code(r)}（不可见资源按 404）")

    r = req("GET", f"/api/v1/exams/{eid}/overview/", token=teacher)
    d = body(r).get("data", {})
    pc = d.get("applicants")
    rec("TC-EX-25", r.status_code == 200 and pc == 16, f"applicants={pc}（期望 16）")

    r = req("POST", f"/api/v1/exams/{eid}/start/", token=teacher)
    rec("TC-EX-29", r.status_code == 403, f"教师开考 HTTP {r.status_code}")

    r = req("GET", "/api/v1/exams/21/", token=student)
    rec("TC-EX-23", body(r).get("data", {}).get("status") == "ongoing",
        f"status={body(r).get('data', {}).get('status')}")

    # ---------------- 5. 学情权限 ----------------
    r = req("GET", f"/api/v1/analytics/students/51/report/", token=os_t)
    rec("TC-AN-14a", r.status_code == 200, f"本班学生 HTTP {r.status_code}")

    r = req("GET", f"/api/v1/analytics/students/{body(req('GET','/api/v1/auth/me/',token=tmp_token)).get('data',{}).get('id')}/report/", token=os_t)
    rec("TC-AN-14b", r.status_code == 404, f"班外教师查学生 HTTP {r.status_code}")

    r = req("GET", "/api/v1/analytics/students/51/report/", token=admin)
    rec("TC-AN-14c", r.status_code == 200, f"管理员查任意学生 HTTP {r.status_code}")

    r = req("GET", f"/api/v1/analytics/exams/{eid}/report/", token=teacher)
    d = body(r).get("data", {})
    part = d.get("participation") or {}
    rec("TC-AN-16", r.status_code == 200 and part.get("applicants") == 16,
        f"班级维度 applicants={part.get('applicants')}（期望 16，不是全校 47）")

    r = req("GET", f"/api/v1/analytics/exams/{eid}/report/?classroom_id=30", token=teacher)
    rec("TC-AN-17a", r.status_code == 404, f"别人的班 HTTP {r.status_code}")

    r = req("GET", f"/api/v1/analytics/exams/{eid}/report/?classroom_id=7", token=teacher)
    part2 = (body(r).get("data") or {}).get("participation") or {}
    rec("TC-AN-17b", r.status_code == 200 and part2.get("applicants") == 16,
        f"自己的班 HTTP {r.status_code} applicants={part2.get('applicants')}")

    # ---------------- 6. 判分与作弊检测权限 ----------------
    r = req("GET", "/api/v1/grading/scoreboard/?exam_id=21", token=student)
    rec("TC-GR-24a", r.status_code == 403, f"学生看成绩单 HTTP {r.status_code}")

    r = req("POST", "/api/v1/grading/attempts/1/grade/", token=student, json={})
    rec("TC-GR-24b", r.status_code == 403, f"学生判分 HTTP {r.status_code}")

    r = req("GET", "/api/v1/grading/scoreboard/?exam_id=21&classroom_id=30", token=teacher)
    rec("TC-GR-23", r.status_code == 404, f"别人班成绩单 HTTP {r.status_code}")

    r = req("GET", "/api/v1/proctoring/exams/", token=student)
    rec("TC-PERM-03", r.status_code == 403, f"学生作弊检测 HTTP {r.status_code}")

    r = req("GET", "/api/v1/proctoring/exams/21/report/?top_n=abc", token=os_t)
    rec("TC-PR-11", r.status_code == 400, f"top_n=abc HTTP {r.status_code}")

    # ---------------- 7. 推荐权限 ----------------
    r = req("GET", "/api/v1/recommend/students/51/practice/", token=tmp_token)
    rec("TC-PERM-06a", r.status_code == 404, f"学生查他人推荐 HTTP {r.status_code}")
    r = req("GET", "/api/v1/recommend/students/51/practice/", token=teacher)
    rec("TC-PERM-06b", r.status_code == 200, f"教师查任意学生推荐 HTTP {r.status_code}")

    # ---------------- 8. 页面守卫 ----------------
    r = req("GET", "/manage/", token=teacher, allow_redirects=False)
    rec("TC-PERM-07", r.status_code == 302, f"教师访问 /manage/ HTTP {r.status_code}")




if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
    finally:
        if TOKENS.get("admin"):
            try:
                cleanup(TOKENS["admin"])
            except Exception:
                traceback.print_exc()
        ok = sum(1 for x in RESULTS if x["result"] == "通过")
        print(f"\n共执行 {len(RESULTS)} 条：通过 {ok}，失败 {len(RESULTS) - ok}")
        out = os.path.join("docs", ".exec_results.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(RESULTS, f, ensure_ascii=False, indent=2)
        print("结果已写入", out)
