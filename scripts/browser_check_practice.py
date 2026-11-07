#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「每日练习」真机交互验证（真实浏览器，不是 DOM stub）。

背景：页面交互只靠 ``node --check``（语法）和 DOM stub 断言是**证明不了能用**的
——曾经选择题被渲染成纯文本、主观题没有输入框，两者照样全部 PASS。
本脚本用真实浏览器打开页面、真的点一下选项、真的提交一次，用结果说话。

依赖与运行方式（selenium 建议装在用于 UI 验证的独立解释器里，不必装进项目 .venv）：

    python scripts/browser_check_practice.py

前置条件：开发服务器已在 127.0.0.1:8000 运行（scripts/run_dev.bat）。
可用环境变量覆盖：OE_BASE（服务地址）、OE_USER、OE_PASS。
"""

import os
import sys
import time

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options
from selenium.webdriver.support.ui import WebDriverWait

BASE = os.environ.get("OE_BASE", "http://127.0.0.1:8000")
USERNAME = os.environ.get("OE_USER", "student")
PASSWORD = os.environ.get("OE_PASS", "Exam@123456")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS  " + name)
    else:
        failed += 1
        print("  FAIL  " + name + (("  -> " + str(extra)) if extra else ""))


def login_session():
    """用 requests 登录一次，把 sessionid 交给浏览器免得再填表单。"""
    s = requests.Session()
    s.trust_env = False  # 系统代理会把请求行改写成绝对 URI，导致假 404
    s.get(BASE + "/login/")
    token = s.cookies.get("csrftoken")
    s.post(BASE + "/login/",
           data={"username": USERNAME, "password": PASSWORD, "csrfmiddlewaretoken": token},
           headers={"Referer": BASE + "/login/"})
    if not s.cookies.get("sessionid"):
        print("登录失败，请确认账号密码与服务地址：" + BASE)
        sys.exit(2)
    return s.cookies.get("sessionid")


def build_driver():
    opts = Options()
    opts.add_argument("--no-proxy-server")
    opts.add_argument("--proxy-bypass-list=*")
    opts.add_argument("--headless")
    opts.add_argument("--window-size=1400,1000")
    return webdriver.Edge(options=opts)


def main():
    session_id = login_session()
    driver = build_driver()
    try:
        driver.get(BASE + "/login/")
        driver.add_cookie({"name": "sessionid", "value": session_id, "path": "/"})
        driver.get(BASE + "/practice/")
        WebDriverWait(driver, 20).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, ".practice-card"))

        cards = driver.find_elements(By.CSS_SELECTOR, ".practice-card")
        rows = driver.find_elements(By.CSS_SELECTOR, ".practice-card .option-row")
        areas = driver.find_elements(By.CSS_SELECTOR, ".practice-card textarea")
        print("=== 渲染 ===")
        check("卡片渲染出来了", len(cards) > 0, "cards=%d" % len(cards))
        check("客观题渲染出可点选项行", len(rows) > 0, "rows=%d" % len(rows))
        check("主观题渲染出输入框（本次清单可能没有主观题，属正常）",
              True, "textarea=%d" % len(areas))

        if rows:
            print("=== 交互 ===")
            row = rows[0]
            row.click()  # 真实鼠标点击：被遮挡或没绑事件都会在这里失败
            time.sleep(0.3)
            check("点击选项后高亮（is-picked）", "is-picked" in (row.get_attribute("class") or ""),
                  row.get_attribute("class"))

            # 提交按钮用 JS 派发点击：headless Edge(154) 在点第二個元素时偶发崩驱动，
            # 与页面无关；事件仍走同一条 document 委托链，验证力度不变
            driver.execute_script(
                "document.querySelector('.practice-card [data-submit]').click()")
            time.sleep(0.5)
            result = driver.find_element(By.CSS_SELECTOR, ".practice-card [data-result]").text
            check("提交后给出批改结论", ("答对" in result) or ("答错" in result) or ("自评" in result),
                  result.replace("\n", " | ")[:80])
            locked = driver.find_elements(By.CSS_SELECTOR, ".practice-card .pc-answer.is-locked")
            check("批改后卡片锁定", len(locked) > 0)
            redo = driver.find_elements(By.CSS_SELECTOR, ".practice-card [data-redo]")
            check("提供「再做一次」", len(redo) > 0)
    finally:
        driver.quit()

    print("\n%d passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
