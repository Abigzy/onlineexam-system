#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""页面内联 JS 的**逻辑**校验入口。

``check_inline_js.py`` 只做 ``node --check``（语法层面），保证不了交互真的能用。
``page_js_checks/*.js`` 是把页面内联脚本真的执行一遍来验证行为：

- 从模板里抠出内联脚本（额外过滤掉 Django 模板语法与 Guido 占位符）；
- 在最小 DOM stub 下用 ``new Function`` 执行；
- 把 IIFE 末尾的入口调用换成导出纯函数，然后逐条断言。

典型用途：验证「每日练习」各题型确实渲染出了作答控件、本地批改口径正确。

用法（从项目根目录运行，需要 node 在 PATH 或设置 NODE_BIN）：
    python scripts/check_practice_js.py
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)


def node_binary() -> str:
    """优先用 NODE_BIN，其次找 PATH 里的 node。"""
    candidate = os.environ.get("NODE_BIN") or shutil.which("node")
    if not candidate:
        print("找不到 node，请设置环境变量 NODE_BIN 指向 node 可执行文件")
        sys.exit(2)
    return candidate


def main() -> None:
    node = node_binary()
    targets = sorted(
        name for name in os.listdir(os.path.join(HERE, "page_js_checks"))
        if name.endswith(".js")
    )
    if not targets:
        print("没有可执行的校验脚本")
        return

    failed = 0
    for name in targets:
        print("\n=== %s ===" % name)
        result = subprocess.run(
            [node, os.path.join(HERE, "page_js_checks", name)],
            cwd=PROJECT_ROOT,
        )
        if result.returncode != 0:
            failed += 1

    print("\n%s 个校验脚本，失败 %s 个" % (len(targets), failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
