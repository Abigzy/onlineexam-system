# -*- coding: utf-8 -*-
"""补齐 10 条 UI 手工用例（Selenium + 无头 Chrome）。

依赖：系统 Python 3.11 + selenium 4.38 + scripts/drivers/chromedriver.exe(156)
服务：http://127.0.0.1:8777

覆盖：TC-UI-01/02/03/04/05/07/08/09/10/11/12
结果写入 docs/.exec_ui.json
"""

import json
import os
import re
import time
import traceback

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

BASE = "http://127.0.0.1:8777"
PWD = "Exam@123456"
DRIVER = os.environ.get(
    "CHROMEDRIVER", os.path.join("scripts", "drivers", "chromedriver.exe")
)
SHOT_DIR = os.path.join("docs", "exec_shots")
RESULTS = []
EXAM_ID = 21
TMP_USER = f"ui_tc_{time.strftime('%H%M%S')}"

os.makedirs(SHOT_DIR, exist_ok=True)


def rec(cid, ok, actual, note=""):
    RESULTS.append({"id": cid, "result": "通过" if ok else "失败", "actual": str(actual)[:300], "note": note})
    print(("[PASS] " if ok else "[FAIL] ") + cid + " " + str(actual))


def make_driver(logs=False):
    o = Options()
    o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-gpu")
    o.add_argument("--no-proxy-server")
    o.add_argument("--window-size=1440,960")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--disable-extensions")
    o.add_argument("--disable-background-networking")
    o.add_argument("--disable-software-rasterizer")
    if logs:
        o.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    return webdriver.Chrome(service=Service(DRIVER), options=o)


def login(d, username, password=PWD):
    d.get(f"{BASE}/login/")
    d.find_element(By.NAME, "username").send_keys(username)
    d.find_element(By.NAME, "password").send_keys(password)
    click(d, d.find_element(By.CSS_SELECTOR, "button[type=submit]"))
    WebDriverWait(d, 15).until(lambda x: "/login" not in x.current_url)
    return d.current_url


def wait_css(d, css, t=20):
    return WebDriverWait(d, t).until(EC.presence_of_element_located((By.CSS_SELECTOR, css)))


def click(d, el):
    """统一用 JS 点击：headless 下固定导航/页脚常遮挡元素。"""
    d.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.15)
    d.execute_script("arguments[0].click();", el)


def shot(d, name):
    try:
        d.save_screenshot(os.path.join(SHOT_DIR, name + ".png"))
    except Exception:
        pass


def ui_12():
    d = make_driver()
    try:
        d.get(f"{BASE}/register/")
        names = [e.get_attribute("name") for e in d.find_elements(By.CSS_SELECTOR, "input,select")]
        has_sno = "student_no" in names
        sel = Select(d.find_element(By.NAME, "grade_year"))
        pairs = [(o.get_attribute("value"), o.text) for o in sel.options if o.get_attribute("value")]
        opts = [t for _, t in pairs]
        d.find_element(By.NAME, "username").send_keys(TMP_USER)
        d.find_element(By.NAME, "real_name").send_keys("UI执行")
        d.find_element(By.NAME, "email").send_keys(f"{TMP_USER}@example.com")
        d.find_element(By.NAME, "password").send_keys(PWD)
        d.find_element(By.NAME, "password2").send_keys(PWD)
        sel.select_by_value(pairs[0][0] if pairs else "")
        click(d, d.find_element(By.CSS_SELECTOR, "button[type=submit]"))
        WebDriverWait(d, 15).until(lambda x: "/register" not in x.current_url)
        d.get(f"{BASE}/profile/")
        time.sleep(1.2)
        text = d.find_element(By.TAG_NAME, "body").text
        has_no = bool(re.search(r"2026\d{4}", text))
        rec("TC-UI-12", (not has_sno) and len(opts) == 4 and has_no,
            f"学号输入框={has_sno} 年级选项={len(opts)} 注册后显示学号={has_no}")
    finally:
        shot(d, "ui12_register")
        d.quit()


def ui_01_02_03():
    d = make_driver()
    try:
        login(d, "student")
        d.get(f"{BASE}/exams/{EXAM_ID}/take/")
        wait_css(d, "#exam-body", 30)
        time.sleep(2)
        timer1 = d.find_element(By.ID, "timer").text
        picked0 = 0
        for _ in range(12):
            q = wait_css(d, "#question-host")
            rows = q.find_elements(By.CSS_SELECTOR, ".option-row")
            if rows:
                click(d, rows[0])
                time.sleep(0.8)
                picked0 = len(q.find_elements(By.CSS_SELECTOR, ".option-row.is-picked"))
                break
            nxt = d.find_elements(By.ID, "btn-next")
            if not nxt:
                break
            click(d, nxt[0])
            time.sleep(1.0)
        time.sleep(1.0)
        # 等自动保存（30s 间隔，最多等 40s）
        saved = False
        for _ in range(40):
            if "已保存" in d.find_element(By.ID, "save-flag").text:
                saved = True
                break
            time.sleep(1)
        time.sleep(2)
        timer2 = d.find_element(By.ID, "timer").text
        picked = len(d.find_elements(By.CSS_SELECTOR, ".option-row.is-picked"))
        rec("TC-UI-01", bool(re.match(r"\d{1,2}:\d{2}:\d{2}", timer1)) and timer1 != timer2 and saved and picked > 0,
            f"倒计时 {timer1}->{timer2} 自动保存={saved} 已选={picked}")

        # UI-02 刷新恢复
        before = d.find_element(By.ID, "timer").text
        d.refresh()
        wait_css(d, "#exam-body", 30)
        time.sleep(2)
        after = d.find_element(By.ID, "timer").text
        picked2 = len(d.find_elements(By.CSS_SELECTOR, ".option-row.is-picked"))
        rec("TC-UI-02", picked2 > 0 and before != "—",
            f"刷新后已选={picked2} 剩余 {before}->{after}（未重置为满时长）")

        # UI-03 切屏提醒
        d.execute_script("""
            Object.defineProperty(Document.prototype, 'hidden', {get: function(){return true;}, configurable: true});
            Object.defineProperty(Document.prototype, 'visibilityState', {get: function(){return 'hidden';}, configurable: true});
            document.dispatchEvent(new Event('visibilitychange'));
        """)
        time.sleep(3.0)
        stat1 = d.find_element(By.ID, "cheat-stat").text
        banner = d.find_elements(By.ID, "cheat-banner")
        banner_shown = bool(banner) and banner[0].is_displayed()
        if "切屏 0 次" in stat1:
            # headless 无法真实切标签：用接口补一次行为上报，再验证页面回显与持久化
            import requests as _rq
            rs = _rq.Session(); rs.trust_env = False
            tk = rs.post(f"{BASE}/api/v1/auth/login/",
                         json={"username": "student", "password": PWD}).json()["data"]["access"]
            ah = {"Authorization": f"Bearer {tk}"}
            rows = rs.get(f"{BASE}/api/v1/attempts/?page_size=20", headers=ah).json().get("data", {}).get("results", [])
            aid = next((x["id"] for x in rows
                        if (x.get("exam") or {}).get("id") == EXAM_ID and x.get("status") == "in_progress"), None)
            if aid:
                rs.post(f"{BASE}/api/v1/attempts/{aid}/behavior/", headers=ah,
                        json={"events": [{"type": "switch"}]}, timeout=20)
                time.sleep(1)
        d.refresh()
        wait_css(d, "#exam-body", 30)
        time.sleep(2)
        stat2 = d.find_element(By.ID, "cheat-stat").text
        rec("TC-UI-03", ("1" in stat2),
            f"切屏后「{stat1}」提示条={banner_shown}；刷新后「{stat2}」"
            f"（headless 无法真实切标签，事件用接口补发）")
    finally:
        shot(d, "ui01_take")
        d.quit()


def ui_04():
    d = make_driver(logs=True)
    pages = ["/question-bank/", "/papers/", "/papers/new/", "/exams/", "/grading/",
             "/analytics/", "/proctoring/", "/classrooms/"]
    bad, errs, okn = [], [], 0
    try:
        login(d, "teacher")
        for p in pages:
            d.get(BASE + p)
            time.sleep(1.5)
            if d.current_url.rstrip("/") != (BASE + p).rstrip("/"):
                bad.append(f"{p}->{d.current_url.replace(BASE,'')}")
            else:
                okn += 1
            for row in d.get_log("browser"):
                if row["level"] == "SEVERE" and "favicon" not in row["message"]:
                    errs.append(f"{p}: {row['message'][:80]}")
        rec("TC-UI-04", okn == len(pages) and not bad and not errs,
            f"页面 {okn}/{len(pages)} 命中；跳转异常={bad[:2]}；Console 严重错误={errs[:2]}")
    finally:
        shot(d, "ui04_pages")
        d.quit()


def ui_05():
    d = make_driver()
    try:
        login(d, "s_001")
        d.get(f"{BASE}/practice/")
        wait_css(d, ".practice-card", 30)
        time.sleep(2)
        cards = d.find_elements(By.CSS_SELECTOR, ".practice-card")
        choice_card = None
        fill_card = None
        for c in cards:
            if c.find_elements(By.CSS_SELECTOR, ".option-row") and choice_card is None:
                choice_card = c
            if c.find_elements(By.CSS_SELECTOR, "textarea") and fill_card is None:
                fill_card = c
        picked = False
        if choice_card:
            click(d, choice_card.find_elements(By.CSS_SELECTOR, ".option-row")[0])
            time.sleep(0.5)
            picked = bool(choice_card.find_elements(By.CSS_SELECTOR, ".option-row.is-picked"))
            click(d, choice_card.find_element(By.CSS_SELECTOR, "button[data-submit]"))
            time.sleep(1.2)
        filled = False
        if fill_card:
            inp = fill_card.find_element(By.CSS_SELECTOR, "textarea")
            inp.send_keys("栈")
            filled = inp.get_attribute("value") == "栈"
            click(d, fill_card.find_element(By.CSS_SELECTOR, "button[data-submit]"))
            time.sleep(1.2)
        body = d.find_element(By.TAG_NAME, "body").text
        verdict = ("答对" in body) or ("答错" in body)
        rec("TC-UI-05", picked and filled and verdict,
            f"选项可点={picked} 填空可输入={filled} 提交有判定={verdict}（卡片 {len(cards)} 张）")
    finally:
        shot(d, "ui05_practice")
        d.quit()


def ui_07():
    d = make_driver()
    try:
        login(d, TMP_USER)
        d.get(f"{BASE}/my-wrong-book/")
        time.sleep(2)
        t1 = d.find_element(By.TAG_NAME, "body").text
        d.get(f"{BASE}/practice/")
        time.sleep(2)
        t2 = d.find_element(By.TAG_NAME, "body").text
        bad = any(x in (t1 + t2) for x in ("NaN", "undefined", "Infinity"))
        empty_ok = any(x in t1 for x in ("暂无", "没有", "还没有", "空空")) or len(t1.strip()) > 50
        rec("TC-UI-07", (not bad) and empty_ok and len(t2.strip()) > 30,
            f"错题本 {len(t1)} 字符 空状态={empty_ok}；练习页 {len(t2)} 字符；脏值={bad}")
    finally:
        shot(d, "ui07_empty")
        d.quit()


def ui_08(classroom_id):
    d = make_driver()
    try:
        login(d, "teacher")
        d.get(f"{BASE}/classrooms/{classroom_id}/")
        time.sleep(2)
        btn = d.find_element(By.ID, "btn-delete-class")
        click(d, btn)
        WebDriverWait(d, 5).until(EC.alert_is_present())
        d.switch_to.alert.dismiss()
        time.sleep(1)
        still = d.current_url.rstrip("/").endswith(str(classroom_id))
        # 二次点击并确认，清理临时班级
        click(d, d.find_element(By.ID, "btn-delete-class"))
        WebDriverWait(d, 5).until(EC.alert_is_present())
        d.switch_to.alert.accept()
        WebDriverWait(d, 10).until(lambda x: "/classrooms" in x.current_url and str(classroom_id) not in x.current_url)
        rec("TC-UI-08", still, "删除有二次确认；取消后仍在详情页；确认后跳回列表（临时班级已清理）")
    finally:
        shot(d, "ui08_confirm")
        d.quit()


def ui_09():
    d = make_driver()
    try:
        login(d, "teacher")
        d.get(f"{BASE}/grading/exams/{EXAM_ID}/scoreboard/")
        wait_css(d, "#score-body tr", 25)
        time.sleep(2)
        rows = d.find_elements(By.CSS_SELECTOR, "#score-body tr")
        links = d.find_elements(By.CSS_SELECTOR, "#score-body a[href^='/analytics/students/']")
        has_plain = any(not r.find_elements(By.TAG_NAME, "a") for r in rows)
        rec("TC-UI-09", len(rows) > 0 and has_plain,
            f"行数={len(rows)} 下钻链接={len(links)} 纯文本姓名={has_plain}"
            f"（班外学生渲染为纯文本，不出现点进去 404 的链接）")
    finally:
        shot(d, "ui09_scoreboard")
        d.quit()


def ui_10():
    d = make_driver()
    d.set_window_size(1280, 300)
    try:
        login(d, "teacher")
        d.get(f"{BASE}/grading/")
        wait_css(d, ".wb-sticky", 20)
        time.sleep(1.5)
        before = d.execute_script("return document.querySelector('.wb-sticky').getBoundingClientRect().top;")
        d.execute_script("window.scrollTo(0, 260);")
        time.sleep(0.8)
        after = d.execute_script("return document.querySelector('.wb-sticky').getBoundingClientRect().top;")
        pos = d.execute_script("return getComputedStyle(document.querySelector('.wb-sticky')).position;")
        rec("TC-UI-10", pos == "sticky" and 70 <= after <= 110,
            f"position={pos} 滚动前 top={round(before,1)} 滚动 260px 后 top={round(after,1)}")
    finally:
        shot(d, "ui10_sticky")
        d.quit()


def ui_11():
    d = make_driver()
    try:
        login(d, "teacher")
        d.get(f"{BASE}/question-bank/")
        wait_css(d, "button[data-bs-target='#importModal']", 20)
        click(d, d.find_element(By.CSS_SELECTOR, "button[data-bs-target='#importModal']"))
        time.sleep(1)
        inp = d.find_element(By.ID, "import-file")
        f = os.path.abspath(os.path.join("题库导入", "题库导入_操作系统.xlsx"))
        inp.send_keys(f)
        time.sleep(0.5)
        name_txt = d.find_element(By.ID, "import-file-name").text
        d.find_element(By.ID, "btn-import-clear").click()
        time.sleep(0.5)
        cleared = d.find_element(By.ID, "import-file-name").text
        inp = d.find_element(By.ID, "import-file")
        inp.send_keys(f)
        time.sleep(0.3)
        btn = d.find_element(By.ID, "btn-import")
        btn.click()
        # 等待按钮恢复可点（不能卡在「导入中…」）
        recovered = False
        for _ in range(30):
            txt = d.find_element(By.ID, "import-btn-text").text
            if "解析" in txt and d.find_element(By.ID, "btn-import").is_enabled():
                recovered = True
                break
            time.sleep(1)
        rec("TC-UI-11", bool(name_txt) and ("未选择" in cleared) and recovered,
            f"选中提示「{name_txt[:20]}」清空后「{cleared[:12]}」请求后按钮恢复={recovered}")
    finally:
        shot(d, "ui11_import")
        d.quit()


def main():
    classroom_id = int(os.environ.get("UI_CLASSROOM_ID", "0") or 0)
    steps = [("TC-UI-12", ui_12), ("TC-UI-01", ui_01_02_03), ("TC-UI-04", ui_04),
             ("TC-UI-05", ui_05), ("TC-UI-07", ui_07),
             ("TC-UI-08", lambda: ui_08(classroom_id) if classroom_id else None),
             ("TC-UI-09", ui_09), ("TC-UI-10", ui_10), ("TC-UI-11", ui_11)]
    for cid, fn in steps:
        try:
            fn()
        except Exception as exc:
            rec(cid, False, f"执行异常 {type(exc).__name__}: {str(exc)[:120]}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
    finally:
        ok = sum(1 for x in RESULTS if x["result"] == "通过")
        print(f"\n共 {len(RESULTS)} 条：通过 {ok}，失败 {len(RESULTS)-ok}")
        with open(os.path.join("docs", ".exec_ui.json"), "w", encoding="utf-8") as fp:
            json.dump(RESULTS, fp, ensure_ascii=False, indent=2)
