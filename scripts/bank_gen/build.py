# -*- coding: utf-8 -*-
"""题库批量生成入口。

用法：
    python scripts/bank_gen/build.py                 # 三科全生成
    python scripts/bank_gen/build.py --subject coa   # 只生成计算机组成原理
    python scripts/bank_gen/build.py --outdir out    # 指定输出目录

输出三个 .xlsx，列结构与 `manage.py` 的导入模板完全一致，可直接在
「题库 → 导入」里上传（建议先用 dry_run 预览）。
"""

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bank_gen.common import Bank, facts_to_fill, facts_to_judge, facts_to_single  # noqa: E402


def build_one(code: str, seed: int) -> Bank:
    """按学科代码组装题池。"""
    rng = random.Random(seed)

    if code == "coa":
        from bank_gen import coa, coa_facts

        bank = Bank(coa.SUBJECT)
        bank.extend(coa.build(rng))
        bank.extend(facts_to_single(coa_facts.FACTS, rng))
        bank.extend(facts_to_judge(coa_facts.FACTS, rng))
        bank.extend(facts_to_fill(coa_facts.FACTS, rng, count=18))
        return bank

    if code == "net":
        from bank_gen import net, net_facts

        bank = Bank(net.SUBJECT)
        bank.extend(net.build(rng))
        bank.extend(facts_to_single(net_facts.FACTS, rng))
        bank.extend(facts_to_judge(net_facts.FACTS, rng))
        bank.extend(facts_to_fill(net_facts.FACTS, rng, count=18))
        return bank

    if code == "os":
        from bank_gen import os_fund, os_facts

        bank = Bank(os_fund.SUBJECT)
        bank.extend(os_fund.build(rng))
        bank.extend(facts_to_single(os_facts.FACTS, rng))
        bank.extend(facts_to_judge(os_facts.FACTS, rng))
        bank.extend(facts_to_fill(os_facts.FACTS, rng, count=18))
        return bank

    raise SystemExit(f"未知学科代码：{code}")


NAMES = {"coa": "计算机组成原理", "net": "计算机网络", "os": "操作系统"}


def main():
    parser = argparse.ArgumentParser(description="批量生成题库导入文件")
    parser.add_argument("--subject", default="all", choices=["all", "coa", "net", "os"])
    parser.add_argument("--outdir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "题库导入"))
    parser.add_argument("--seed", type=int, default=20261030)
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    codes = ["coa", "net", "os"] if args.subject == "all" else [args.subject]

    total = 0
    for code in codes:
        bank = build_one(code, args.seed)
        problems = bank.validate()
        dist = bank.distribution()
        path = os.path.join(args.outdir, f"题库导入_{NAMES[code]}.xlsx")
        bank.write_xlsx(path)

        print(f"\n【{NAMES[code]}】{dist['总数']} 题 → {path}")
        print("  题型:", "、".join(f"{k} {v}" for k, v in dist["题型"].items()))
        print("  难度:", "、".join(f"{k} {v}" for k, v in dist["难度"].items()))
        print("  知识点:", "、".join(f"{k} {v}" for k, v in dist["知识点"].items()))
        if problems:
            print(f"  ⚠️ 校验发现 {len(problems)} 处问题：")
            for p in problems[:10]:
                print("    -", p)
        else:
            print("  ✅ 校验通过（选项数 / 答案合法性 / 解析完整性）")
        total += dist["总数"]

    print(f"\n合计 {total} 题，输出目录：{args.outdir}")


if __name__ == "__main__":
    main()
