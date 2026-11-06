# -*- coding: utf-8 -*-
"""操作系统 · 题目生成。

页面置换缺页次数、调度周转时间、分页地址转换全部用模拟算法算出——
FIFO/LRU 的缺页数与直觉常不一致，靠人算必错，这里直接跑算法。
"""

import random

from .common import (
    DIFF_EASY, DIFF_EXPERT, DIFF_HARD, DIFF_MEDIUM,
    Q, TYPE_JUDGE, TYPE_SINGLE,
)

SUBJECT = "操作系统"


def _single(kp, stem, correct, distractors, analysis, diff=DIFF_MEDIUM, rng=None):
    opts = [correct] + list(distractors)[:3]
    rng = rng or random
    order = opts[:]
    rng.shuffle(order)
    return Q(kp=kp, qtype=TYPE_SINGLE, difficulty=diff, stem=stem,
             options=order, answer="ABCD"[order.index(correct)], analysis=analysis)


def _judge(kp, stem, is_true, analysis, diff=DIFF_EASY):
    return Q(kp=kp, qtype=TYPE_JUDGE, difficulty=diff, stem=stem,
             answer="A" if is_true else "B", analysis=analysis)


# ---------------- 置换算法模拟 ----------------
def fifo_faults(refs, frames):
    mem, faults = [], 0
    for r in refs:
        if r in mem:
            continue
        faults += 1
        if len(mem) == frames:
            mem.pop(0)
        mem.append(r)
    return faults


def lru_faults(refs, frames):
    mem, faults = [], 0
    for r in refs:
        if r in mem:
            mem.remove(r)
            mem.append(r)      # 最近使用移到末尾
            continue
        faults += 1
        if len(mem) == frames:
            mem.pop(0)         # 淘汰最久未使用（队首）
        mem.append(r)
    return faults


def opt_faults(refs, frames):
    mem, faults = [], 0
    for i, r in enumerate(refs):
        if r in mem:
            continue
        faults += 1
        if len(mem) < frames:
            mem.append(r)
            continue
        # 找未来最远才被用到的页（或永不再用）
        far, victim = -1, mem[0]
        for page in mem:
            try:
                nxt = refs.index(page, i + 1)
            except ValueError:
                nxt = float("inf")
            if nxt > far:
                far, victim = nxt, page
        mem[mem.index(victim)] = r
    return faults


# ---------------- 调度模拟 ----------------
def sjf_times(arrival, burst):
    """非抢占 SJF：返回平均周转时间。"""
    n = len(arrival)
    done, t, total = [False] * n, 0, 0
    for _ in range(n):
        idx, best = -1, None
        for i in range(n):
            if done[i] or arrival[i] > t:
                continue
            if best is None or burst[i] < best:
                idx, best = i, burst[i]
        if idx == -1:                      # CPU 空闲，跳到下一个到达
            t = min(a for i, a in enumerate(arrival) if not done[i])
            continue
        t += burst[idx]
        done[idx] = True
        total += t - arrival[idx]
    return total / n


def fcfs_times(arrival, burst):
    order = sorted(range(len(arrival)), key=lambda i: arrival[i])
    t, total = 0, 0
    for i in order:
        t = max(t, arrival[i]) + burst[i]
        total += t - arrival[i]
    return total / len(order)


def rr_times(arrival, burst, quantum):
    """时间片轮转：返回平均周转时间。"""
    n = len(arrival)
    remain = burst[:]
    t, done, finish = 0, 0, [0] * n
    ready = []
    i = 0
    order = sorted(range(n), key=lambda k: arrival[k])
    while done < n:
        while i < n and arrival[order[i]] <= t:
            ready.append(order[i])
            i += 1
        if not ready:
            t = arrival[order[i]]
            continue
        cur = ready.pop(0)
        run = min(quantum, remain[cur])
        t += run
        remain[cur] -= run
        while i < n and arrival[order[i]] <= t:
            ready.append(order[i])
            i += 1
        if remain[cur] == 0:
            finish[cur] = t
            done += 1
        else:
            ready.append(cur)
    return sum(finish[k] - arrival[k] for k in range(n)) / n


# ======================================================================
# 进程管理
# ======================================================================
def gen_process_mgmt(rng):
    kp, out = "进程管理", []

    out.append(_single(kp, "进程的三态模型中，不包括下列哪个状态（　）。", "就绪",
                       ["运行", "阻塞", "创建"], "三态是就绪、运行、阻塞（等待）；创建/终止属于五态模型。",
                       DIFF_EASY, rng))
    out.append(_single(kp, "进程从运行态转为阻塞态通常是因为（　）。", "等待 I/O 或某个事件",
                       ["时间片用完", "被更高优先级抢占", "进程执行完毕"],
                       "主动等待资源/事件而让出 CPU；时间片用完是转就绪。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "时间片用完导致的状态转换是（　）。", "运行 → 就绪",
                       ["运行 → 阻塞", "就绪 → 运行", "阻塞 → 运行"],
                       "时间片耗尽仍可运行，只是让出 CPU，回到就绪队列。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "进程控制块 PCB 中不包含（　）。", "程序代码",
                       ["进程状态", "程序计数器", "资源清单"],
                       "PCB 保存管理与切换所需信息（状态、PC、寄存器、资源），代码段不在其中。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "进程与程序的根本区别是（　）。", "进程是动态的，程序是静态的",
                       ["进程占用内存，程序不占用", "进程有名字，程序没有", "进程可以并发，程序不能"],
                       "程序是静态指令集合，进程是程序的一次执行（动态、有生命周期）。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "创建进程时，操作系统首先要做的是（　）。", "分配 PCB 并初始化",
                       ["分配内存", "加载程序", "调度运行"],
                       "PCB 是进程存在的唯一标志，创建即建立 PCB。", DIFF_MEDIUM, rng))

    # 并发执行时间
    for _ in range(8):
        io_t, cpu_t = rng.choice([(30, 20), (40, 30), (20, 10), (50, 40)])
        seq = cpu_t + io_t + cpu_t
        out.append(_single(
            kp, f"某进程先计算 {cpu_t}ms、再 I/O {io_t}ms、再计算 {cpu_t}ms，顺序执行共需（　）ms。",
            str(seq), [str(cpu_t + io_t), str(seq + io_t), str(2 * cpu_t)],
            f"顺序执行时间 = {cpu_t} + {io_t} + {cpu_t} = {seq}ms。", DIFF_EASY, rng))

    out.append(_judge(kp, "进程是资源分配的基本单位，线程是 CPU 调度的基本单位。", True,
                      "这是进程与线程最常见的考点，二者在不同层次承担基本单位角色。"))
    out.append(_judge(kp, "一个进程被阻塞后，操作系统会立即调度其他进程运行。", True,
                      "阻塞使 CPU 空闲，调度器会从就绪队列选择新进程（若无就绪进程则空闲）。"))
    out.append(_judge(kp, "进程的 PCB 在进程运行结束后可以立即释放。", True,
                      "进程终止后其 PCB 仍需保留到父进程回收（wait），否则产生僵尸进程。"))
    out.append(_judge(kp, "同一进程内的多个线程共享进程的地址空间。", True,
                      "线程共享代码段、数据段、打开的文件，但各自有独立的栈和寄存器。"))
    out.append(_judge(kp, "进程切换不需要保存和恢复上下文。", False,
                      "切换必须保存当前进程的 PC、寄存器等现场，再恢复下一个进程的现场。"))
    return out


# ======================================================================
# 进程与线程
# ======================================================================
def gen_thread(rng):
    kp, out = "进程与线程", []

    out.append(_single(kp, "引入线程的主要目的是（　）。", "减少进程切换开销、提高并发度",
                       ["增加内存容量", "简化编程", "提高磁盘速度"],
                       "线程共享进程资源，切换只需保存少量寄存器，开销远小于进程切换。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "同一进程内的线程不共享（　）。", "栈和寄存器",
                       ["代码段", "全局变量", "打开的文件"], "线程私有栈与寄存器，其余资源（代码、数据、文件）共享。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "用户级线程的最大缺点是（　）。", "一个线程阻塞会导致整个进程阻塞",
                       ["切换开销大", "无法并发", "不能用多核"],
                       "内核看不到用户级线程，任一线程发起阻塞系统调用，整个进程都被阻塞。",
                       DIFF_HARD, rng))

    for _ in range(8):
        n = rng.choice([4, 8, 16])
        ms = rng.choice([2, 5, 10])
        total = n * ms
        out.append(_single(
            kp, f"{n} 个线程并行执行，每个需要 {ms}ms CPU 时间，单核下总 CPU 时间为（　）ms。",
            str(total), [str(ms), str(total // 2), str(total * 2)],
            f"单核串行执行，总时间 = {n} × {ms} = {total}ms（不计切换开销）。", DIFF_HARD, rng))

    out.append(_judge(kp, "线程切换的开销通常小于进程切换。", True,
                      "线程切换不涉及地址空间切换，开销更小。"))
    out.append(_judge(kp, "多线程程序在多核 CPU 上一定能获得加速。", False,
                      "受 Amdahl 定律与锁竞争限制，串行部分与同步开销会抵消并行收益。"))
    out.append(_judge(kp, "线程有自己的程序计数器。", True,
                      "每个线程独立执行流，必须有独立的 PC 与寄存器组。"))
    out.append(_judge(kp, "进程间通信不需要操作系统介入。", False,
                      "管道、消息队列、共享内存、信号量等都需要内核提供机制与同步。"))
    out.append(_judge(kp, "协程（coroutine）属于用户态调度，切换开销比线程更小。", True,
                      "协程在用户态保存现场，不陷入内核，切换代价极小。"))
    return out


# ======================================================================
# 处理机调度
# ======================================================================
def gen_scheduling(rng):
    kp, out = "处理机调度", []

    for _ in range(8):
        n = rng.choice([3, 4])
        arrival = [0] * n
        burst = [rng.randint(2, 9) for _ in range(n)]
        f = fcfs_times(arrival, burst)
        s = sjf_times(arrival, burst)
        better = "SJF" if s < f else "FCFS"
        out.append(_single(
            kp, f"三个作业同时到达，服务时间分别为 {'、'.join(map(str, burst))}，"
                f"SJF 与 FCFS 哪个平均周转时间更短（　）。",
            better,
            [x for x in ("SJF", "FCFS", "两者相同", "无法确定") if x != better],
            f"FCFS 平均周转 {f:.2f}，SJF 平均周转 {s:.2f}；短作业优先可减少平均等待。",
            DIFF_HARD, rng))

    for _ in range(8):
        n = 3
        burst = [rng.randint(3, 10) for _ in range(n)]
        arrival = [0] * n
        avg = sjf_times(arrival, burst)
        out.append(_single(
            kp, f"三个同时到达的作业服务时间为 {'、'.join(map(str, burst))}，SJF 的平均周转时间是（　）。",
            f"{avg:.2f}", [f"{avg + 1:.2f}", f"{avg - 1:.2f}", f"{sum(burst) / n:.2f}"],
            f"按 {sorted(burst)} 顺序执行，完成时刻累计求平均得 {avg:.2f}。",
            DIFF_EXPERT, rng))

    for _ in range(6):
        burst = [rng.randint(4, 9) for _ in range(3)]
        q = rng.choice([2, 3])
        avg = rr_times([0, 0, 0], burst, q)
        out.append(_single(
            kp, f"三个作业同时到达、服务时间 {'、'.join(map(str, burst))}，"
                f"时间片 {q} 的轮转调度平均周转时间是（　）。",
            f"{avg:.2f}", [f"{avg + 2:.2f}", f"{avg - 2:.2f}", f"{sum(burst) / 3:.2f}"],
            f"按时间片 {q} 依次轮转，模拟得到平均周转 {avg:.2f}。",
            DIFF_EXPERT, rng))

    out.append(_single(kp, "下列调度算法中，可能导致饥饿的是（　）。", "优先级调度（低优先级长期得不到 CPU）",
                       ["先来先服务", "时间片轮转", "短作业优先"], "优先级调度与 SJF 都可能让某些进程长期等待。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "时间片轮转算法中，时间片过大会导致（　）。", "退化为先来先服务",
                       ["切换开销剧增", "响应时间变短", "无法抢占"], "时间片足够大时每个进程一次跑完，等价 FCFS。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "实时调度算法最关心的是（　）。", "满足任务的截止时间",
                       ["平均周转时间最短", "吞吐量最大", "公平性"],
                       "硬实时系统必须保证截止期，平均性能是次要目标。", DIFF_MEDIUM, rng))

    out.append(_judge(kp, "SJF 算法能保证平均等待时间最短。", True,
                      "在非抢占、作业同时到达的前提下，SJF 的平均等待时间最优。"))
    out.append(_judge(kp, "时间片轮转调度的平均周转时间总是优于 FCFS。", False,
                      "RR 改善响应时间，但频繁切换会增大周转时间，不一定优于 FCFS。"))
    out.append(_judge(kp, "多级反馈队列调度兼顾了响应时间与吞吐量。", True,
                      "短作业在高优先级队列快速完成，长作业逐级下降，兼顾两者。"))
    return out


# ======================================================================
# 内存管理
# ======================================================================
def gen_memory(rng):
    kp, out = "内存管理", []

    # 分页地址转换
    for _ in range(11):
        page_size = rng.choice([1024, 2048, 4096])
        addr = rng.randint(1, 60000)
        pno, off = addr // page_size, addr % page_size
        out.append(_single(
            kp, f"页面大小 {page_size}B，逻辑地址 {addr} 的页号与页内偏移是（　）。",
            f"页号 {pno}，偏移 {off}",
            [f"页号 {pno + 1}，偏移 {off}", f"页号 {off}，偏移 {pno}", f"页号 {pno}，偏移 {off + 1}"],
            f"页号 = {addr} // {page_size} = {pno}，偏移 = {addr} % {page_size} = {off}。",
            DIFF_HARD, rng))

    # 页表大小
    for _ in range(6):
        bits, entry = rng.choice([(20, 4), (16, 2), (24, 4)])
        pages = 2 ** bits
        kb = pages * entry // 1024
        out.append(_single(
            kp, f"逻辑地址空间 {bits} 位、页表项 {entry}B，页表最大占用（　）KB。",
            f"{kb}", [f"{kb * 2}", f"{kb // 2}", f"{kb * 4}"],
            f"页数 2^{bits} = {pages}，页表大小 = {pages} × {entry}B = {kb}KB。",
            DIFF_EXPERT, rng))

    # 碎片
    out.append(_single(kp, "固定分区分配的主要缺点是（　）。", "存在内部碎片",
                       ["存在外部碎片", "分配开销大", "无法多道运行"],
                       "分区固定、作业小于分区时，分区内的剩余空间被浪费（内部碎片）。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "动态分区分配的主要缺点是（　）。", "产生外部碎片",
                       ["产生内部碎片", "需要页表", "不能换出"], "反复分配释放后留下许多不连续小空闲区（外部碎片）。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "分页存储管理中，页表的作用是（　）。", "记录页号到物理块号的映射",
                       ["记录进程状态", "记录空闲分区", "记录中断向量"],
                       "页表把逻辑页号映射到内存物理块号，实现离散分配。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "分段与分页的本质区别是（　）。", "分段按逻辑意义划分，分页按固定大小划分",
                       ["分段更快", "分页有外部碎片", "分段不需要地址转换"],
                       "段是逻辑单位（长度可变），页是物理单位（固定大小）。", DIFF_HARD, rng))

    out.append(_judge(kp, "分页存储管理不会产生外部碎片，但可能有内部碎片。", True,
                      "页内最后一页通常填不满，产生内部碎片；页之间无外部碎片。"))
    out.append(_judge(kp, "页表一定存放在内存中，且每次访存都需要访问页表。", False,
                      "页表在内存，但 TLB 可缓存页表项，命中时无需访问页表。"))
    out.append(_judge(kp, "引入快表（TLB）可以显著减少地址转换的访存次数。", True,
                      "TLB 命中可省去一次访存，有效访问时间接近直接访存。"))
    return out


# ======================================================================
# 虚拟内存
# ======================================================================
def gen_vm(rng):
    kp, out = "虚拟内存", []

    for _ in range(12):
        tlb_hit = rng.choice([0.8, 0.9, 0.95])
        tlb_t, mem_t = 10, 100
        eat = tlb_hit * (tlb_t + mem_t) + (1 - tlb_hit) * (tlb_t + 2 * mem_t)
        out.append(_single(
            kp, f"TLB 命中率 {tlb_hit * 100:.0f}%、TLB 访问 10ns、内存访问 100ns，有效访问时间约为（　）ns。",
            f"{eat:.0f}", [f"{eat + 10:.0f}", f"{eat - 10:.0f}", "100"],
            f"EAT = {tlb_hit}×(10+100) + {1 - tlb_hit:.2f}×(10+200) ≈ {eat:.0f}ns。",
            DIFF_EXPERT, rng))

    for _ in range(6):
        p = rng.choice([0.001, 0.002, 0.01])
        page_fault_cost = rng.choice([5000, 8000, 10000])
        eat = (1 - p) * 100 + p * page_fault_cost
        out.append(_single(
            kp, f"缺页率 {p * 100:.1f}%、正常访存 100ns、缺页处理 {page_fault_cost}ns，"
                f"有效访问时间约为（　）ns。",
            f"{eat:.0f}", [f"{eat * 2:.0f}", f"{eat + 100:.0f}", "100"],
            f"EAT = (1-{p})×100 + {p}×{page_fault_cost} ≈ {eat:.0f}ns。",
            DIFF_EXPERT, rng))

    out.append(_single(kp, "虚拟内存的理论基础是（　）。", "程序的局部性原理",
                       ["内存足够大", "地址空间连续", "进程数量有限"],
                       "时间与空间局部性使程序在一段时间内只访问少量页面，故可只装入部分页面。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "虚拟内存的最大容量通常由（　）决定。", "地址位数（逻辑地址空间）与物理内存之和无关",
                       ["物理内存大小", "磁盘容量", "Cache 容量"],
                       "虚拟地址空间由地址位数决定；物理内存与磁盘只影响实际可容纳的进程数。",
                       DIFF_HARD, rng))
    out.append(_single(kp, "工作集（working set）模型的目的是（　）。", "防止系统抖动（thrashing）",
                       ["提高磁盘利用率", "加快地址转换", "减少页表大小"],
                       "工作集是近期访问的页面集合，保证其驻留内存可避免频繁缺页抖动。",
                       DIFF_HARD, rng))
    out.append(_single(kp, "抖动（thrashing）产生的根本原因是（　）。", "分配给进程的物理页框过少",
                       ["CPU 太快", "磁盘太慢", "页表太大"], "页框不足导致刚换出的页立刻又被访问，CPU 大部分时间在处理缺页。",
                       DIFF_MEDIUM, rng))

    out.append(_judge(kp, "虚拟内存可以让程序使用的地址空间超过物理内存容量。", True,
                      "通过换入换出，逻辑地址空间可大于物理内存。"))
    out.append(_judge(kp, "缺页中断属于一种内部中断（异常）。", True,
                      "缺页是执行指令过程中产生的异常（fault），处理后可重新执行该指令。"))
    out.append(_judge(kp, "增加物理内存一定能消除抖动。", False,
                      "若进程数过多导致每进程页框仍不足，增加内存只能推迟抖动发生。"))
    return out


# ======================================================================
# 页面置换
# ======================================================================
def gen_page_replace(rng):
    kp, out = "页面置换", []

    for _ in range(14):
        frames = rng.choice([3, 4])
        refs = [rng.randint(1, 5) for _ in range(rng.randint(8, 11))]
        ff, lf, of = fifo_faults(refs, frames), lru_faults(refs, frames), opt_faults(refs, frames)
        algo = rng.choice([("FIFO", ff), ("LRU", lf)])
        out.append(_single(
            kp, f"页框 {frames} 个，访问串 {' '.join(map(str, refs))}，采用 {algo[0]} 算法的缺页次数是（　）。",
            str(algo[1]), [str(algo[1] + 1), str(max(algo[1] - 1, 1)), str(algo[1] + 2)],
            f"逐步模拟 {algo[0]} 得到缺页 {algo[1]} 次（同一访问串下 OPT {of} 次、"
            f"{'LRU' if algo[0] == 'FIFO' else 'FIFO'} {algo[1] if algo[0] == 'LRU' else ff} 次）。",
            DIFF_EXPERT, rng))

    out.append(_single(kp, "下列页面置换算法中，实际不可实现但可作为评价基准的是（　）。", "最佳置换（OPT）",
                       ["FIFO", "LRU", "Clock"], "OPT 需要预知未来访问序列，只能作为理论下界。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "FIFO 置换算法可能出现（　）。", "Belady 异常（页框增多缺页反而增加）",
                       ["抖动", "饥饿", "死锁"], "FIFO 不满足栈式算法性质，增加页框可能增加缺页。",
                       DIFF_HARD, rng))
    out.append(_single(kp, "LRU 算法淘汰的是（　）。", "最长时间未被访问的页面",
                       ["最先调入的页面", "访问次数最少的页面", "随机选择的页面"],
                       "LRU 依据最近使用时间，近似 OPT 且可实现。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "Clock（时钟）算法是对（　）算法的近似实现。", "LRU",
                       ["OPT", "FIFO", "LFU"], "Clock 用访问位模拟 LRU，开销比严格 LRU 低得多。",
                       DIFF_HARD, rng))

    out.append(_judge(kp, "增加页框数一定能减少缺页次数。", False,
                      "FIFO 存在 Belady 异常，页框增加缺页反而可能增加；LRU/OPT 不会。"))
    out.append(_judge(kp, "LRU 算法不会出现 Belady 异常。", True,
                      "LRU 属栈式算法，页框增加时缺页数单调不增。"))
    out.append(_judge(kp, "局部置换是指只在当前进程的页面中选择淘汰对象。", True,
                      "局部置换缩小影响范围；全局置换可从所有进程的页中挑选。"))
    return out


# ======================================================================
# 死锁
# ======================================================================
def gen_deadlock(rng):
    kp, out = "死锁", []

    for _ in range(10):
        procs = rng.choice([3, 4])
        need = rng.randint(1, 2)
        total = procs * need            # 每进程最多 need 个，总数 procs*need 必不死锁？
        safe = total - procs + 1        # 经典结论：资源数 ≥ n(m-1)+1 必不死锁
        out.append(_single(
            kp, f"{procs} 个进程各最多需要 {need} 个同类资源，至少需要（　）个资源才能保证不死锁。",
            str(safe),
            [str(safe + 1), str(max(safe - 1, 1)), str(procs * need)],
            f"最坏情况每个进程都持有 {need - 1} 个：{procs}×{need - 1} = {procs * (need - 1)}，"
            f"再加 1 个即可打破僵局，故需 {safe} 个。",
            DIFF_EXPERT, rng))

    out.append(_single(kp, "死锁的四个必要条件不包括（　）。", "资源可共享",
                       ["互斥", "请求与保持", "循环等待"], "四个条件是互斥、请求与保持、不可剥夺、循环等待。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "破坏「请求与保持」条件的常用方法是（　）。", "一次性申请全部资源",
                       ["资源按序分配", "允许抢占", "增大资源数量"],
                       "一次性分配（静态分配）使进程不会持有一部分再申请另一部分。", DIFF_HARD, rng))
    out.append(_single(kp, "破坏「循环等待」条件的常用方法是（　）。", "资源有序分配",
                       ["一次性申请", "允许抢占", "取消互斥"],
                       "给资源编号，进程只能按编号递增顺序申请，从而不可能形成环。", DIFF_HARD, rng))
    out.append(_single(kp, "银行家算法属于（　）策略。", "死锁避免",
                       ["死锁预防", "死锁检测", "死锁恢复"],
                       "银行家算法在分配前判断系统是否仍处于安全状态，属避免策略。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "系统处于不安全状态意味着（　）。", "可能发生死锁",
                       ["一定发生死锁", "一定不会死锁", "与死锁无关"],
                       "不安全状态只是存在死锁的可能，不必然发生。", DIFF_HARD, rng))
    out.append(_single(kp, "死锁检测的常用工具是（　）。", "资源分配图（含环检测）",
                       ["页表", "TLB", "信号量"],
                       "资源分配图出现环（且每类资源单实例）即判定死锁。", DIFF_MEDIUM, rng))

    out.append(_judge(kp, "死锁与饥饿是同一个概念。", False,
                      "死锁是互相等待永不推进；饥饿是长期得不到资源但系统仍在推进。"))
    out.append(_judge(kp, "预防死锁会降低资源利用率。", True,
                      "破坏必要条件往往牺牲并发性（如一次性分配、按序申请）。"))
    out.append(_judge(kp, "银行家算法需要预先知道每个进程的最大资源需求。", True,
                      "这是它的前提假设，实际系统中很难准确获得。"))
    out.append(_judge(kp, "只要资源分配图中存在环，系统就一定死锁。", False,
                      "每类资源多实例时，有环不一定死锁，需进一步化简判断。"))
    return out


def build(rng=None):
    rng = rng or random.Random(20261030)
    out = []
    for gen in (gen_process_mgmt, gen_thread, gen_scheduling, gen_memory,
                gen_vm, gen_page_replace, gen_deadlock):
        out.extend(gen(rng))
    return out
