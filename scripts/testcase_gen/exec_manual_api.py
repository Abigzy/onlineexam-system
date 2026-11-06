# -*- coding: utf-8 -*-
"""补齐 21 条手工用例里的「接口 / 页面级」部分（10 条）+ 若干辅助验证。

覆盖：TC-GEN-06、TC-PERM-08、TC-AI-02、TC-QB-08、TC-QB-15、TC-QB-17、
      TC-PP-13、TC-PP-21、TC-MG-12
结果写入 docs/.exec_manual_api.json（后续与 .exec_results.json 合并）。
"""

import io
import json
import os
import re
import time
from datetime import datetime, timedelta

import requests

BASE = "http://127.0.0.1:8777"
PWD = "Exam@123456"
TS = time.strftime("%H%M%S")
S = requests.Session()
S.trust_env = False

RESULTS = []
PAPERS = []


def rec(cid, ok, actual, note=""):
    RESULTS.append({"id": cid, "result": "通过" if ok else "失败", "actual": str(actual)[:300], "note": note})
    print(("[PASS] " if ok else "[FAIL] ") + cid + " " + str(actual))


def body(r):
    try:
        return r.json()
    except Exception:
        return {}


def code(r):
    return body(r).get("code")


def login(name):
    r = S.post(f"{BASE}/api/v1/auth/login/", json={"username": name, "password": PWD}, timeout=20)
    return body(r).get("data", {}).get("access")


def H(t):
    return {"Authorization": f"Bearer {t}"}


def session_login(username, password=PWD):
    """表单登录（页面用 session），带 CSRF。"""
    s = requests.Session()
    s.trust_env = False
    r = s.get(f"{BASE}/login/", timeout=20)
    token = s.cookies.get("csrftoken") or re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text).group(1)
    r = s.post(f"{BASE}/login/", data={"username": username, "password": password,
                                       "csrfmiddlewaretoken": token},
               headers={"Referer": f"{BASE}/login/"}, allow_redirects=False, timeout=20)
    return s, r


def main():
    admin, teacher = login("admin"), login("teacher")

    # ---------------- TC-GEN-06 GET 幂等 / DELETE 重复 ----------------
    r = S.post(f"{BASE}/api/v1/knowledge-points/", headers=H(teacher),
               json={"name": f"TC执行-KP-{TS}", "subject": "数据结构"}, timeout=20)
    kp = body(r).get("data") or {}
    kid = kp.get("id")
    a = S.get(f"{BASE}/api/v1/knowledge-points/{kid}/", headers=H(teacher), timeout=20).text
    time.sleep(0.3)
    b = S.get(f"{BASE}/api/v1/knowledge-points/{kid}/", headers=H(teacher), timeout=20).text
    d1 = S.delete(f"{BASE}/api/v1/knowledge-points/{kid}/", headers=H(teacher), timeout=20)
    d2 = S.delete(f"{BASE}/api/v1/knowledge-points/{kid}/", headers=H(teacher), timeout=20)
    rec("TC-GEN-06", a == b and d1.status_code in (200, 204) and d2.status_code == 404,
        f"两次 GET 一致={a == b} 首次 DELETE={d1.status_code} 再次={d2.status_code}")

    # ---------------- TC-PERM-08 未登录跳转 + 回跳 ----------------
    s = requests.Session(); s.trust_env = False
    r1 = s.get(f"{BASE}/papers/", allow_redirects=False, timeout=20)
    loc = r1.headers.get("Location", "")
    page = s.get(f"{BASE}/login/?next=/papers/", timeout=20)
    tok = s.cookies.get("csrftoken") or re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text).group(1)
    r2 = s.post(f"{BASE}/login/", data={"username": "teacher", "password": PWD,
                                        "csrfmiddlewaretoken": tok, "next": "/papers/"},
                headers={"Referer": f"{BASE}/login/?next=/papers/"},
                allow_redirects=False, timeout=20)
    rec("TC-PERM-08", r1.status_code == 302 and "/login/" in loc and "next=/papers/" in loc
        and r2.status_code == 302 and r2.headers.get("Location", "").endswith("/papers/"),
        f"未登录 {r1.status_code} -> {loc}；登录后 {r2.status_code} -> {r2.headers.get('Location')}")

    # ---------------- TC-AI-02 连通性自检（真实 provider） ----------------
    t0 = time.time()
    r = S.post(f"{BASE}/api/v1/ai/test-connection/", headers=H(teacher),
               json={"provider": "deepseek"}, timeout=90)
    d = body(r).get("data") or {}
    rec("TC-AI-02", r.status_code == 200 and d.get("ok") is True,
        f"HTTP {r.status_code} ok={d.get('ok')} provider={d.get('provider')} "
        f"latency={d.get('latency_ms')}ms is_mock={d.get('is_mock')} 总耗时={int((time.time()-t0)*1000)}ms",
        "调用真实 DeepSeek（极短提示）")

    # ---------------- TC-QB-08 关键词搜索 ----------------
    r1 = S.get(f"{BASE}/api/v1/questions/", headers=H(teacher), params={"search": "二叉树", "page_size": 5}, timeout=30)
    c1 = (body(r1).get("data") or {}).get("count")
    r2 = S.get(f"{BASE}/api/v1/questions/", headers=H(teacher), params={"search": "zzz不存在xyz", "page_size": 5}, timeout=30)
    c2 = (body(r2).get("data") or {}).get("count")
    rec("TC-QB-08", r1.status_code == 200 and c1 > 0 and c2 == 0, f"命中 {c1} 条；无关词 {c2} 条")

    # ---------------- TC-QB-15 模板下载 + 回填导入 ----------------
    r = S.get(f"{BASE}/api/v1/questions/import-template/", headers=H(teacher), timeout=30)
    raw = r.content
    is_csv = raw[:3] == b"\xef\xbb\xbf" or raw[:1] == b"\xef" or b"," in raw[:200]
    lines = [x for x in raw.decode("utf-8-sig").splitlines() if x.strip()]
    header = lines[0].split(",") if lines else []
    # 模板自带 3 行示例：直接整份回填导入，验证「下载即可用」
    r2 = S.post(f"{BASE}/api/v1/questions/import/", headers=H(teacher),
                files={"file": ("question_import_template.csv", raw, "text/csv")},
                data={"subject": "数据结构"}, timeout=60)
    d2 = body(r2).get("data") or {}
    created = d2.get("created_count")
    rec("TC-QB-15", r.status_code == 200 and is_csv and len(header) >= 10
        and r2.status_code in (200, 201) and created >= 1,
        f"模板 {r.status_code} csv={is_csv} 列头 {len(header)} 个；回填导入 created={created}，"
        f"跳过重复={d2.get('skipped_count')}，错误={d2.get('error_count')}")
    for qid in d2.get("created_ids") or []:
        S.delete(f"{BASE}/api/v1/questions/{qid}/", headers=H(teacher), timeout=20)

    # ---------------- TC-QB-17 format 参数不生效 ----------------
    r = S.get(f"{BASE}/api/v1/questions/export/", headers=H(teacher),
              params={"format": "csv"}, timeout=30)
    ctype = r.headers.get("Content-Type", "")
    rec("TC-QB-17", not ctype.startswith("text/csv"),
        f"HTTP {r.status_code} Content-Type={ctype[:40]}（未按 csv 返回）")

    # ---------------- 造一张临时卷用于 PP-13 / PP-21 ----------------
    r = S.post(f"{BASE}/api/v1/papers/", headers=H(teacher),
               json={"title": f"TC执行-归档卷-{TS}", "subject": "数据结构", "pass_score": 5}, timeout=30)
    pid = (body(r).get("data") or {}).get("id")
    if pid:
        PAPERS.append(pid)
        S.post(f"{BASE}/api/v1/papers/{pid}/questions/", headers=H(teacher),
               json={"question_ids": [776, 775]}, timeout=30)
        S.post(f"{BASE}/api/v1/papers/{pid}/status/", headers=H(teacher),
               json={"status": "published"}, timeout=30)

    # TC-PP-13 归档
    r = S.post(f"{BASE}/api/v1/papers/{pid}/status/", headers=H(teacher), json={"status": "archived"}, timeout=30)
    r2 = S.get(f"{BASE}/api/v1/papers/{pid}/", headers=H(teacher), timeout=30)
    rec("TC-PP-13", r.status_code == 200 and (body(r2).get("data") or {}).get("status") == "archived",
        f"归档 HTTP {r.status_code} 状态={(body(r2).get('data') or {}).get('status')}")

    # TC-PP-21 导出 html / markdown
    r = S.post(f"{BASE}/api/v1/papers/{pid}/status/", headers=H(teacher), json={"status": "published"}, timeout=30)
    rh = S.get(f"{BASE}/api/v1/papers/{pid}/export/", headers=H(teacher), params={"file_format": "html"}, timeout=30)
    rm = S.get(f"{BASE}/api/v1/papers/{pid}/export/", headers=H(teacher), params={"file_format": "markdown"}, timeout=30)
    ok = (rh.status_code == 200 and b"<html" in rh.content[:200].lower()
          and rm.status_code == 200 and len(rm.content) > 50)
    rec("TC-PP-21", ok, f"HTML {rh.status_code} {len(rh.content)}B；Markdown {rm.status_code} {len(rm.content)}B")

    # ---------------- TC-MG-12 管理端组织页 / 系统页 ----------------
    s_admin, lr = session_login("admin")
    p1 = s_admin.get(f"{BASE}/manage/org/", timeout=30)
    p2 = s_admin.get(f"{BASE}/manage/system/", timeout=30)
    rec("TC-MG-12", p1.status_code == 200 and p2.status_code == 200
        and "班级" in p1.text and ("AI" in p2.text or "运行环境" in p2.text),
        f"org={p1.status_code}({len(p1.text)}B) system={p2.status_code}({len(p2.text)}B)")

    # ---------------- 清理临时试卷 ----------------
    for x in PAPERS:
        S.post(f"{BASE}/api/v1/papers/{x}/status/", headers=H(admin), json={"status": "draft"}, timeout=20)
        S.delete(f"{BASE}/api/v1/papers/{x}/", headers=H(admin), timeout=20)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        ok = sum(1 for x in RESULTS if x["result"] == "通过")
        print(f"\n共 {len(RESULTS)} 条：通过 {ok}，失败 {len(RESULTS)-ok}")
        with open(os.path.join("docs", ".exec_manual_api.json"), "w", encoding="utf-8") as f:
            json.dump(RESULTS, f, ensure_ascii=False, indent=2)
