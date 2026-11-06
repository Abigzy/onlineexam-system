# -*- coding: utf-8 -*-
"""计算机组成原理 · 题目生成。

计算类题目的答案全部由 Python 算出来（补码、IEEE754、Cache 地址划分、芯片扩展），
不依赖人工录入，从根上避免"答案错"这类致命问题。
"""

import random

from .common import (
    DIFF_EASY, DIFF_EXPERT, DIFF_HARD, DIFF_MEDIUM,
    Q, TYPE_FILL, TYPE_JUDGE, TYPE_SINGLE,
)

SUBJECT = "计算机组成原理"


def _single(kp, stem, correct, distractors, analysis, diff=DIFF_MEDIUM, rng=None):
    """构造单选题：正确答案与 3 个干扰项一起打乱。"""
    opts = [correct] + list(distractors)[:3]
    rng = rng or random
    order = opts[:]
    rng.shuffle(order)
    return Q(kp=kp, qtype=TYPE_SINGLE, difficulty=diff, stem=stem,
             options=order, answer="ABCD"[order.index(correct)], analysis=analysis)


def _judge(kp, stem, is_true, analysis, diff=DIFF_EASY):
    return Q(kp=kp, qtype=TYPE_JUDGE, difficulty=diff, stem=stem,
             answer="A" if is_true else "B", analysis=analysis)


# ======================================================================
# 数据表示
# ======================================================================
def gen_data_repr(rng):
    kp, out = "数据表示", []

    # 1) 二进制 → 十六进制
    pool = rng.sample(range(16, 256), 12)
    for v in pool:
        hx = f"{v:02X}"
        wrong = [f"{v ^ 0b1000:02X}", f"{v + 16:02X}" if v + 16 < 256 else f"{v - 16:02X}",
                 f"{(v << 1) & 0xFF:02X}"]
        out.append(_single(kp, f"二进制数 {v:08b}B 的十六进制表示是（　）。",
                           hx, wrong, f"{v:08b} 每 4 位一组即 {v:04b} {v & 0x0F:04b}，合起来是 {hx}H。",
                           DIFF_EASY, rng))

    # 2) BCD 码
    for v in rng.sample(range(10, 100), 6):
        bcd = f"{v // 10:04b}{v % 10:04b}"
        out.append(_single(kp, f"十进制数 {v} 的 8421BCD 码是（　）。",
                           bcd, [f"{v:08b}", f"{v // 10:04b}{v % 10:04b}"[::-1], f"{v + 1:08b}"],
                           f"8421BCD 每位十进制数用 4 位二进制表示：{v // 10}→{v // 10:04b}，{v % 10}→{v % 10:04b}。",
                           DIFF_MEDIUM, rng))

    # 3) ASCII 码
    for ch, code in [("A", 65), ("a", 97), ("0", 48)]:
        out.append(_single(kp, f"字符 '{ch}' 的 ASCII 码值（十进制）是（　）。",
                           str(code), [str(code + 1), str(code - 1), str(code + 32)],
                           f"'{ch}' 的 ASCII 码为 {code}。", DIFF_EASY, rng))

    # 4) 无符号数表示范围
    for n in (8, 12, 16):
        out.append(_single(kp, f"{n} 位无符号二进制数能表示的最大十进制数是（　）。",
                           str(2 ** n - 1), [str(2 ** n), str(2 ** (n - 1) - 1), str(2 ** (n - 1))],
                           f"{n} 位无符号数范围 0 ~ 2^{n}-1 = {2 ** n - 1}。", DIFF_EASY, rng))

    out.append(_judge(kp, "8421BCD 码中每 4 位二进制表示一位十进制数，允许出现 1010~1111。",
                      False, "8421BCD 只使用 0000~1001 十个组合，1010~1111 是非法编码。"))
    out.append(_judge(kp, "字符 'a' 的 ASCII 码比字符 'A' 的 ASCII 码大 32。",
                      True, "大写字母与小写字母的 ASCII 码相差 32，'a'=97、'A'=65。"))
    return out


# ======================================================================
# 补码
# ======================================================================
def gen_twos_complement(rng):
    kp, out = "补码", []

    # 1) 十进制 → 8 位补码（十六进制）
    for v in rng.sample(range(-128, 128), 12):
        code = v & 0xFF
        wrong = [(v + 1) & 0xFF, (v - 1) & 0xFF, v & 0x7F]
        out.append(_single(
            kp, f"十进制数 {v} 的 8 位补码用十六进制表示为（　）。",
            f"{code:02X}H", [f"{w:02X}H" for w in wrong],
            f"{v} 的补码 = {v} + 256 = {code}，即 {code:02X}H。" if v < 0
            else f"正数的补码与原码相同，{v} → {code:08b}B = {code:02X}H。",
            DIFF_MEDIUM, rng))

    # 2) 补码 → 真值
    for code in rng.sample(range(1, 255), 9):
        v = code - 256 if code >= 128 else code
        out.append(_single(
            kp, f"某机器字长 8 位，补码 {code:02X}H 表示的真值是（　）。",
            str(v), [str(code), str(-code), str(code - 128 if code >= 128 else -code)],
            f"补码高位为 1 表示负数：{code:02X}H = {code:08b}B，真值 = {code} - 256 = {v}。",
            DIFF_MEDIUM, rng))

    # 3) 表示范围
    for n in (8, 16):
        lo, hi = -(2 ** (n - 1)), 2 ** (n - 1) - 1
        out.append(_single(
            kp, f"{n} 位补码能表示的整数范围是（　）。",
            f"{lo} ~ {hi}", [f"0 ~ {2 ** n - 1}", f"{lo + 1} ~ {hi}", f"-{hi} ~ {hi}"],
            f"{n} 位补码范围是 -2^{n - 1} ~ 2^{n - 1}-1，即 {lo} ~ {hi}。",
            DIFF_EASY, rng))

    # 4) 补码加减
    for _ in range(6):
        a, b = rng.randint(-40, 40), rng.randint(-40, 40)
        s = a + b
        out.append(_single(
            kp, f"用 8 位补码计算 {a} + ({b})，结果的补码是（　）。",
            f"{(s & 0xFF):02X}H",
            [f"{((a - b) & 0xFF):02X}H", f"{((s + 1) & 0xFF):02X}H", f"{((s - 1) & 0xFF):02X}H"],
            f"补码加减法统一按二进制相加：{a} + ({b}) = {s}，其 8 位补码为 {(s & 0xFF):02X}H。",
            DIFF_HARD, rng))

    out.append(_judge(kp, "补码表示中，0 只有一种编码。", True,
                      "补码的 0 唯一（00000000），这是它相对原码、反码的优势之一。"))
    out.append(_judge(kp, "已知某数的补码，求其相反数的补码可以对该补码连同符号位一起按位取反再加一。",
                      True, "补码的相反数等于该补码整体按位取反加一，符号位一并参与运算。"))
    out.append(_judge(kp, "8 位补码能表示的整数个数是 255 个。", False,
                      "8 位补码可表示 256 个整数（-128 ~ 127），因为 0 只占一个编码。"))
    return out


# ======================================================================
# 浮点数
# ======================================================================
def gen_float(rng):
    kp, out = "浮点数", []

    out.append(_single(kp, "IEEE 754 单精度浮点数共占（　）位。", "32",
                       ["16", "64", "80"],
                       "单精度 32 位（1 符号 + 8 阶码 + 23 尾数），双精度 64 位。", DIFF_EASY, rng))
    out.append(_single(kp, "IEEE 754 单精度浮点数的阶码采用（　）表示。", "移码",
                       ["原码", "反码", "补码"],
                       "阶码用移码（偏移量 127），便于比较大小。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "IEEE 754 单精度浮点数的阶码位数是（　）。", "8",
                       ["7", "11", "23"],
                       "单精度：符号 1 位、阶码 8 位、尾数 23 位。", DIFF_EASY, rng))
    out.append(_single(kp, "IEEE 754 双精度浮点数的尾数位数是（　）。", "52",
                       ["23", "48", "64"],
                       "双精度：1 + 11 + 52 = 64 位。", DIFF_MEDIUM, rng))

    # 规格化
    for _ in range(6):
        m = rng.choice(["0.1011", "0.1101", "0.1001", "0.1110"])
        out.append(_single(
            kp, f"尾数 {m} 已是规格化形式，其真值范围是（　）。",
            "0.5 ≤ |M| < 1", ["0.25 ≤ |M| < 0.5", "0 < |M| < 0.5", "1 ≤ |M| < 2"],
            "原码规格化要求尾数最高有效位为 1，即 0.5 ≤ |M| < 1。",
            DIFF_HARD, rng))

    # 阶码偏移
    for e in rng.sample(range(0, 255), 4):
        real = e - 127
        out.append(_single(
            kp, f"单精度浮点数阶码字段为 {e} 时，实际阶码（真值）是（　）。",
            str(real), [str(e), str(e - 128), str(e + 127)],
            f"阶码采用偏移量 127 的移码，真值 = {e} - 127 = {real}。",
            DIFF_HARD, rng))

    out.append(_judge(kp, "浮点数能表示的数值范围比定点数大，但精度是有限的。", True,
                      "浮点数用阶码扩大范围，用有限尾数位表示精度，二者不可兼得。"))
    out.append(_judge(kp, "IEEE 754 单精度浮点数的尾数采用原码表示，且隐含最高位 1。", True,
                      "规格化尾数隐含最高位 1，因此实际精度为 24 位而只存 23 位。"))
    out.append(_judge(kp, "浮点数运算时不需要对阶。", False,
                      "浮点加减必须先对阶（小阶向大阶看齐），再尾数运算、规格化、舍入。"))
    return out


# ======================================================================
# 存储器
# ======================================================================
def gen_memory(rng):
    kp, out = "存储器", []

    # 1) 地址线/数据线 → 容量
    for _ in range(8):
        a, d = rng.choice([(10, 8), (12, 8), (10, 16), (13, 8), (11, 4), (14, 16)])
        bits = (2 ** a) * d
        kb = bits // 8 // 1024
        out.append(_single(
            kp, f"某存储器有 {a} 根地址线、{d} 根数据线，其容量是（　）。",
            f"{kb}KB", [f"{kb * 2}KB", f"{kb // 2}KB", f"{kb * 8}KB"],
            f"存储单元数 = 2^{a} = {2 ** a}，每单元 {d} 位，总容量 = {2 ** a} × {d} bit = {kb}KB。",
            DIFF_HARD, rng))

    # 2) 芯片扩展
    for _ in range(7):
        chip, total = rng.choice([("1K×4", "4K×8"), ("2K×8", "8K×8"), ("16K×1", "64K×8"),
                                  ("1K×8", "8K×8")])
        c_bits = eval(chip.replace("K", "*1024").replace("×", "*"))
        t_bits = eval(total.replace("K", "*1024").replace("×", "*"))
        n = t_bits // c_bits
        out.append(_single(
            kp, f"用 {chip} 位的存储芯片构成 {total} 位的存储器，需要（　）片。",
            str(n), [str(n * 2), str(n // 2) if n > 1 else "2", str(n + 1)],
            f"所需片数 = ({total}) / ({chip}) = {n} 片。", DIFF_HARD, rng))

    # 3) 存储周期与带宽
    for _ in range(4):
        t = rng.choice([50, 100, 200])
        w = rng.choice([8, 16, 32])
        bw = w // 8 * (1 / (t * 1e-9)) / 1e6
        out.append(_single(
            kp, f"存储器存取周期 {t}ns、数据线 {w} 位，其带宽约为（　）。",
            f"{bw:.0f}MB/s", [f"{bw * 2:.0f}MB/s", f"{bw / 2:.0f}MB/s", f"{bw / 4:.0f}MB/s"],
            f"带宽 = 字节数 / 周期 = {w // 8}B / {t}ns ≈ {bw:.0f}MB/s。",
            DIFF_EXPERT, rng))

    out.append(_judge(kp, "SRAM 需要周期性地刷新才能保持数据。", False,
                      "SRAM 靠双稳态触发器保存数据，不需刷新；需要刷新的是 DRAM。"))
    out.append(_judge(kp, "DRAM 的集成度高于 SRAM，但速度较慢、需要刷新。", True,
                      "DRAM 单元简单（一个晶体管 + 一个电容），集成度高、成本低，但需刷新且速度慢于 SRAM。"))
    out.append(_judge(kp, "存储器的存取周期通常大于存取时间。", True,
                      "存取周期包含存取时间以及恢复时间，因此更长。"))
    return out


# ======================================================================
# Cache
# ======================================================================
def gen_cache(rng):
    kp, out = "Cache", []

    # 1) 直接映射地址划分
    for _ in range(8):
        cache_kb, block_b = rng.choice([(4, 16), (8, 32), (16, 64), (2, 8)])
        lines = cache_kb * 1024 // block_b
        idx = lines.bit_length() - 1
        off = block_b.bit_length() - 1
        out.append(_single(
            kp, f"主存地址 20 位，Cache 容量 {cache_kb}KB、块大小 {block_b}B，采用直接映射，块内地址占（　）位。",
            str(off), [str(idx), str(off + 1), str(20 - idx - off)],
            f"块内地址位数 = log2({block_b}) = {off} 位（Cache 行数 {lines}，行号 {idx} 位，标记 {20 - idx - off} 位）。",
            DIFF_HARD, rng))

    # 2) 命中率 / 平均访问时间
    for _ in range(7):
        h = rng.choice([0.90, 0.95, 0.92, 0.98])
        tc, tm = rng.choice([(10, 100), (5, 50), (20, 200)])
        t = h * tc + (1 - h) * tm
        out.append(_single(
            kp, f"Cache 命中率 {h * 100:.0f}%、命中时访问时间 {tc}ns、缺失时 {tm}ns，平均访问时间是（　）。",
            f"{t:.1f}ns", [f"{t + 10:.1f}ns", f"{t - 5:.1f}ns", f"{tm:.0f}ns"],
            f"平均访问时间 = {h} × {tc} + {1 - h:.2f} × {tm} = {t:.1f}ns。",
            DIFF_HARD, rng))

    # 3) 映射方式特点
    out.append(_single(kp, "直接映射方式的主要缺点是（　）。", "冲突率高、灵活性差",
                       ["硬件实现复杂", "需要按内容查找", "不能使用 SRAM"],
                       "直接映射一个主存块只能放到唯一 Cache 行，容易冲突。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "全相联映射方式的特点是（　）。", "主存块可放入任意 Cache 行",
                       ["主存块只能放固定行", "只能按组存放", "不需要标记位"],
                       "全相联映射允许任意放置，冲突少但比较器多、成本高。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "Cache 替换算法中，LRU 的含义是（　）。", "最近最少使用",
                       ["先进先出", "随机替换", "最不经常使用"],
                       "LRU = Least Recently Used，淘汰最久未被访问的行。", DIFF_EASY, rng))

    # 4) 写策略
    out.append(_single(kp, "写直达（Write Through）的特点是（　）。", "每次写同时更新 Cache 和主存",
                       ["只写 Cache，替换时才写主存", "只写主存，不写 Cache", "写操作直接绕过 Cache"],
                       "写直达保证一致性但写操作开销大；写回（Write Back）只在替换时写回主存。",
                       DIFF_MEDIUM, rng))

    out.append(_judge(kp, "Cache 的容量越大，命中率一定越高，且访问速度不变。", False,
                      "容量增大会提高命中率，但也会增加命中判断与译码延迟，访问速度可能变慢。"))
    out.append(_judge(kp, "Cache 与主存之间的地址映射由硬件自动完成，对程序员透明。", True,
                      "地址映射、替换、一致性维护都由硬件完成，程序无需干预。"))
    out.append(_judge(kp, "增加 Cache 块大小一定能提高命中率。", False,
                      "块过大会减少行数、加剧冲突，命中率可能反而下降（空间局部性与冲突的权衡）。"))
    return out


# ======================================================================
# 指令系统
# ======================================================================
def gen_isa(rng):
    kp, out = "指令系统", []

    # 1) 操作码位数 → 指令条数
    for n in (4, 5, 6, 8):
        out.append(_single(
            kp, f"指令操作码占 {n} 位时，最多可表示（　）条不同的指令。",
            str(2 ** n), [str(n * 2), str(2 ** n - 1), str(2 ** (n - 1))],
            f"{n} 位操作码可组合出 2^{n} = {2 ** n} 种。", DIFF_EASY, rng))

    # 2) 指令字长与地址字段
    for _ in range(6):
        word, op, addr = rng.choice([(16, 4, 6), (32, 8, 12), (16, 6, 5)])
        n = (word - op) // addr
        out.append(_single(
            kp, f"指令字长 {word} 位、操作码 {op} 位、每个地址码 {addr} 位，该指令最多可有（　）个地址。",
            str(n), [str(n + 1), str(n - 1), str(word // addr)],
            f"地址位数 = ({word} - {op}) / {addr} = {n}。", DIFF_HARD, rng))

    # 3) 寻址方式
    for _ in range(6):
        base, off = rng.randint(1000, 4000), rng.randint(4, 64)
        out.append(_single(
            kp, f"变址寻址中，变址寄存器内容为 {base}，形式地址为 {off}，则有效地址是（　）。",
            str(base + off), [str(base), str(off), str(base - off)],
            f"变址寻址 EA = (变址寄存器) + 形式地址 = {base} + {off} = {base + off}。",
            DIFF_HARD, rng))

    out.append(_single(kp, "立即寻址方式中，操作数存放在（　）。", "指令中",
                       ["内存单元", "寄存器", "堆栈"],
                       "立即寻址的操作数直接写在指令里，取指即取数，速度最快。", DIFF_EASY, rng))
    out.append(_single(kp, "间接寻址比直接寻址多访问（　）次内存。", "1",
                       ["0", "2", "3"],
                       "间接寻址需要先取地址再按地址取数，比直接寻址多一次访存。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "RISC 指令系统的典型特征是（　）。", "指令数量少、定长、寻址方式简单",
                       ["指令数量多、变长、寻址方式丰富", "必须使用微程序控制", "不支持流水线"],
                       "RISC 精简指令集，指令少而规整，便于流水线实现。", DIFF_MEDIUM, rng))

    out.append(_judge(kp, "指令的操作码越长，可表示的指令种类越多，可用于地址码的位数就越少。", True,
                      "指令字长固定时，操作码与地址码此消彼长。"))
    out.append(_judge(kp, "CISC 的指令通常采用定长格式。", False,
                      "CISC 指令变长；RISC 才强调定长指令格式。"))
    out.append(_judge(kp, "寄存器寻址的操作数在寄存器中，不需要访问内存。", True,
                      "寄存器寻址直接取寄存器内容，访存次数为 0。"))
    return out


# ======================================================================
# CPU
# ======================================================================
def gen_cpu(rng):
    kp, out = "CPU", []

    # 1) 主频 + CPI → MIPS
    for _ in range(8):
        f = rng.choice([1, 2, 3, 4])
        cpi = rng.choice([2, 4, 5, 8])
        mips = f * 1000 / cpi
        out.append(_single(
            kp, f"某 CPU 主频 {f}GHz、平均 CPI = {cpi}，其运算速度约为（　）MIPS。",
            f"{mips:.0f}", [f"{mips * 2:.0f}", f"{mips / 2:.0f}", f"{f * 1000:.0f}"],
            f"MIPS = 主频 / CPI = {f}×10^3 / {cpi} ≈ {mips:.0f}。", DIFF_HARD, rng))

    # 2) 流水线加速比
    for _ in range(6):
        k = rng.choice([4, 5, 6])
        n = rng.choice([100, 200, 500])
        ideal = n * k / (k + n - 1)
        out.append(_single(
            kp, f"{k} 段流水线执行 {n} 条指令（各段等长、无冲突），理想加速比约为（　）。",
            f"{ideal:.1f}", [f"{k}", f"{k / 2:.1f}", f"{ideal / 2:.1f}"],
            f"加速比 = n×k / (k + n - 1) = {n}×{k} / {k + n - 1} ≈ {ideal:.1f}。",
            DIFF_EXPERT, rng))

    # 3) 执行时间
    for _ in range(6):
        n, cpi, f = rng.choice([(10 ** 6, 2, 1), (5 * 10 ** 6, 4, 2), (10 ** 6, 5, 3)])
        ms = n * cpi / (f * 1e9) * 1000
        out.append(_single(
            kp, f"执行 {n} 条指令、平均 CPI = {cpi}、主频 {f}GHz，所需时间约为（　）ms。",
            f"{ms:.2f}", [f"{ms * 2:.2f}", f"{ms / 2:.2f}", f"{ms * 10:.2f}"],
            f"T = 指令数 × CPI / 主频 = {n} × {cpi} / {f}×10^9 = {ms:.2f}ms。",
            DIFF_EXPERT, rng))

    out.append(_single(kp, "CPU 中负责指令译码的部件是（　）。", "控制器",
                       ["运算器", "寄存器组", "Cache"],
                       "控制器完成取指、译码、发控制信号；运算器负责算术逻辑运算。", DIFF_EASY, rng))
    out.append(_single(kp, "程序计数器 PC 存放的是（　）。", "下一条要执行指令的地址",
                       ["当前指令", "运算结果", "指令操作码"],
                       "PC 始终指向下一条待取指令的地址，取指后自动增量。", DIFF_EASY, rng))
    out.append(_single(kp, "流水线中「数据相关」指的是（　）。", "后一条指令需要前一条指令尚未产生的结果",
                       ["多条指令争用同一功能部件", "指令地址计算冲突", "控制信号时序错位"],
                       "数据相关（RAW）需通过转发或停顿解决；结构相关指争用部件。", DIFF_MEDIUM, rng))

    out.append(_judge(kp, "提高主频一定能同比例提升程序执行速度。", False,
                      "主频提升常伴随 CPI 或访存瓶颈变化（存储墙），实际加速往往低于主频提升比例。"))
    out.append(_judge(kp, "流水线技术可以提高指令的吞吐率，但不缩短单条指令的完成时间。", True,
                      "流水线提升吞吐率；单条指令的延迟反而可能因流水寄存器而略微增加。"))
    out.append(_judge(kp, "指令周期一定等于一个时钟周期。", False,
                      "指令周期通常包含多个机器周期，每个机器周期又含多个时钟周期。"))
    return out


def build(rng=None):
    """生成计算机组成原理的全部题目。"""
    rng = rng or random.Random(20261030)
    out = []
    for gen in (gen_data_repr, gen_twos_complement, gen_float, gen_memory,
                gen_cache, gen_isa, gen_cpu):
        out.extend(gen(rng))
    return out
