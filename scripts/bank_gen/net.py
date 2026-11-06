# -*- coding: utf-8 -*-
"""计算机网络 · 题目生成。

子网划分、广播地址、可用主机数、IP 分片、CRC 全部用标准库 `ipaddress` 或位运算算出，
答案不靠猜。
"""

import ipaddress
import random

from .common import (
    DIFF_EASY, DIFF_EXPERT, DIFF_HARD, DIFF_MEDIUM,
    Q, TYPE_JUDGE, TYPE_SINGLE,
)

SUBJECT = "计算机网络"


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


def _wrong_subnet(net, rng):
    """为子网题造 3 个似是而非的干扰项。"""
    cands = []
    for delta in (1, 2, 4, 8, 16):
        for sign in (1, -1):
            try:
                shifted = ipaddress.ip_address(int(net.network_address) + sign * net.num_addresses * delta)
                cands.append(str(shifted))
            except ValueError:
                pass
    rng.shuffle(cands)
    return cands[:3]


# ======================================================================
# 数据链路层
# ======================================================================
def gen_datalink(rng):
    kp, out = "数据链路层", []

    # CRС 校验（模 2 除法）
    for _ in range(8):
        data = rng.randint(0b1000, 0b1111)
        gen = 0b1011  # x^3 + x + 1
        work = data << 3
        for i in range(work.bit_length() - 1, 2, -1):
            if work >> i & 1:
                work ^= gen << (i - 3)
        rem = work & 0b111
        out.append(_single(
            kp, f"数据 {data:04b} 采用生成多项式 x³+x+1 做 CRC 校验，余数是（　）。",
            f"{rem:03b}", [f"{(rem + 1) & 0b111:03b}", f"{rem ^ 0b111:03b}", f"{rem:03b}"[::-1]],
            f"模 2 除法（异或、无借位）计算得到余数 {rem:03b}。", DIFF_EXPERT, rng))

    # 以太网最小帧长
    out.append(_single(kp, "以太网规定的最小帧长是（　）字节。", "64",
                       ["46", "128", "1518"], "最小 64 字节（含 14 字节首部），保证冲突检测；最大 1518 字节。",
                       DIFF_EASY, rng))
    out.append(_single(kp, "以太网 MAC 地址的长度是（　）位。", "48",
                       ["32", "64", "16"], "MAC 地址 48 位（6 字节），通常写作 12 个十六进制数。",
                       DIFF_EASY, rng))

    # 停止等待协议效率
    for _ in range(6):
        t_f, t_p = rng.choice([(1, 4), (2, 8), (1, 9), (1, 19)])
        eff = t_f / (t_f + 2 * t_p) * 100
        out.append(_single(
            kp, f"停止等待协议中，帧发送时延 {t_f}ms、单程传播时延 {t_p}ms（忽略确认帧时延），信道利用率为（　）。",
            f"{eff:.1f}%", [f"{eff * 2:.1f}%", f"{eff / 2:.1f}%", f"{100 - eff:.1f}%"],
            f"利用率 = T发 / (T发 + 2×T传) = {t_f} / ({t_f} + {2 * t_p}) ≈ {eff:.1f}%。",
            DIFF_EXPERT, rng))

    # 交换机 / 冲突域
    out.append(_single(kp, "交换机工作在 OSI 模型的（　）。", "数据链路层",
                       ["物理层", "网络层", "传输层"], "交换机按 MAC 地址转发，工作在数据链路层。",
                       DIFF_EASY, rng))
    out.append(_single(kp, "集线器（Hub）工作在 OSI 模型的（　）。", "物理层",
                       ["数据链路层", "网络层", "传输层"], "Hub 只是信号放大与广播，属物理层设备。",
                       DIFF_EASY, rng))

    out.append(_judge(kp, "交换机可以隔离冲突域，但不能隔离广播域。", True,
                      "交换机的每个端口是独立冲突域；广播域需用路由器或 VLAN 隔离。"))
    out.append(_judge(kp, "CSMA/CD 协议用于无线局域网。", False,
                      "CSMA/CD 用于有线以太网；无线局域网使用 CSMA/CA。"))
    out.append(_judge(kp, "CRC 校验可以检测出所有单比特错误。", True,
                      "CRC 的检错能力强于奇偶校验，可检出所有单比特、双比特及奇数位错误。"))
    out.append(_judge(kp, "以太网使用 CSMA/CD 协议时，发送前必须先监听信道。", True,
                      "「先听后发、边听边发、冲突停发、随机重发」是 CSMA/CD 的基本流程。"))
    return out


# ======================================================================
# 网络层
# ======================================================================
def gen_network_layer(rng):
    kp, out = "网络层", []

    # IP 分片
    for _ in range(8):
        total = rng.choice([1500, 2000, 3000, 4000])
        mtu = rng.choice([1500, 1000, 800])
        if total <= mtu:
            continue
        payload = total - 20
        max_data = (mtu - 20) // 8 * 8
        frags = -(-payload // max_data)
        out.append(_single(
            kp, f"IP 数据报总长 {total} 字节（首部 20 字节），MTU 为 {mtu} 字节，需要分成（　）片。",
            f"{frags} 片", [f"{frags + 1} 片", f"{max(frags - 1, 1)} 片", f"{frags + 2} 片"],
            f"每片最多承载 ({mtu}-20)//8×8 = {max_data} 字节，{payload} 字节需 {frags} 片。",
            DIFF_EXPERT, rng))

    # 路由器 / 网关
    out.append(_single(kp, "路由器根据（　）决定分组的转发端口。", "目的 IP 地址",
                       ["源 IP 地址", "MAC 地址", "端口号"], "路由表按目的网络前缀匹配，最长前缀优先。",
                       DIFF_EASY, rng))
    out.append(_single(kp, "网络层的主要功能是（　）。", "分组转发与路由选择",
                       ["端到端的可靠传输", "比特流传输", "进程间通信"],
                       "网络层负责逻辑寻址、路由与分组转发；可靠传输由传输层（TCP）负责。", DIFF_MEDIUM, rng))

    # IP 地址分类
    for ip, cls in [("10.1.2.3", "A 类"), ("172.16.5.4", "B 类"), ("192.168.1.1", "C 类")]:
        out.append(_single(
            kp, f"IP 地址 {ip} 属于（　）。",
            cls, [c for c in ("A 类", "B 类", "C 类", "D 类") if c != cls],
            f"首字节 1~126 为 A 类，128~191 为 B 类，192~223 为 C 类；{ip} 首字节决定其类别。",
            DIFF_MEDIUM, rng))

    # TTL
    out.append(_single(kp, "IP 首部中 TTL 字段的作用是（　）。", "防止分组在网络中无限循环",
                       ["标识服务类型", "校验首部", "表示优先级"], "每经一个路由器 TTL 减 1，减到 0 则丢弃并回 ICMP 超时。",
                       DIFF_MEDIUM, rng))

    out.append(_judge(kp, "IP 协议提供的是面向连接的可靠传输服务。", False,
                      "IP 是无连接、不可靠的尽力而为服务；可靠性由 TCP 提供。"))
    out.append(_judge(kp, "同一局域网内的主机通信不需要经过路由器。", True,
                      "同网段主机通过 ARP 获取 MAC 后直接二层转发，不必经过路由器。"))
    out.append(_judge(kp, "IP 数据报分片后，各片的标识字段相同。", True,
                      "同一数据报的所有分片标识字段一致，偏移量不同，用于目的端重组。"))
    return out


# ======================================================================
# IP 协议（子网划分为主）
# ======================================================================
def gen_ip(rng):
    kp, out = "IP 协议", []

    # 子网：网络地址 / 广播地址 / 可用主机数
    for _ in range(12):
        a, b = rng.randint(1, 223), rng.randint(0, 255)
        prefix = rng.choice([24, 25, 26, 27, 28, 29])
        try:
            net = ipaddress.ip_network(f"{a}.{b}.{rng.randint(0, 255)}.{rng.randint(1, 254)}/{prefix}",
                                       strict=False)
        except ValueError:
            continue
        hosts = max(net.num_addresses - 2, 0)
        out.append(_single(
            kp, f"网络 {net.network_address}/{prefix} 的可用主机地址数是（　）。",
            str(hosts), [str(net.num_addresses), str(hosts + 2), str(hosts * 2)],
            f"/{prefix} 共 {net.num_addresses} 个地址，去掉网络地址与广播地址，可用 {hosts} 个。",
            DIFF_HARD, rng))
        out.append(_single(
            kp, f"网络 {net.network_address}/{prefix} 的广播地址是（　）。",
            str(net.broadcast_address), _wrong_subnet(net, rng),
            f"广播地址是主机位全 1：{net.broadcast_address}。", DIFF_HARD, rng))

    # 掩码与主机位
    for prefix in (25, 26, 27, 28, 30):
        mask = str(ipaddress.ip_network(f"0.0.0.0/{prefix}").netmask)
        host_bits = 32 - prefix
        out.append(_single(
            kp, f"/{prefix} 的掩码是（　）。",
            mask, [str(ipaddress.ip_network(f"0.0.0.0/{prefix + 1}").netmask),
                   str(ipaddress.ip_network(f"0.0.0.0/{prefix - 1}").netmask),
                   str(ipaddress.ip_network(f"0.0.0.0/{min(prefix + 2, 31)}").netmask)],
            f"/{prefix} 表示前 {prefix} 位为网络位，主机位 {host_bits} 位，掩码为 {mask}。",
            DIFF_MEDIUM, rng))

    # 判断两地址是否同网段
    for _ in range(6):
        prefix = rng.choice([24, 25, 26])
        base = f"192.168.{rng.randint(0, 10)}.{rng.randint(1, 200)}"
        try:
            n1 = ipaddress.ip_network(f"{base}/{prefix}", strict=False)
        except ValueError:
            continue
        other = str(ipaddress.ip_address(int(n1.network_address) + rng.choice([1, 2, 5])))
        out.append(_single(
            kp, f"地址 {base}/{prefix} 所在网络的网络地址是（　）。",
            str(n1.network_address), _wrong_subnet(n1, rng),
            f"网络地址 = IP 与掩码按位与 = {n1.network_address}/{prefix}。",
            DIFF_HARD, rng))
        break

    out.append(_single(kp, "私有地址段 192.168.0.0/16 可用的地址总数是（　）个。", "65536",
                       ["65534", "32768", "256"], "192.168.0.0/16 共 2^16 = 65536 个地址，可用 65534 个。",
                       DIFF_HARD, rng))
    out.append(_single(kp, "IPv4 地址长度为（　）位。", "32", ["64", "128", "16"],
                       "IPv4 为 32 位，IPv6 为 128 位。", DIFF_EASY, rng))
    out.append(_single(kp, "IPv6 地址长度为（　）位。", "128", ["32", "64", "256"],
                       "IPv6 用 128 位地址，写作 8 组十六进制。", DIFF_EASY, rng))
    out.append(_single(kp, "CIDR 表示法 192.168.1.0/26 中，网络前缀长度是（　）。", "26",
                       ["24", "25", "32"], "斜杠后的数字即网络前缀位数。", DIFF_EASY, rng))

    out.append(_judge(kp, "子网掩码中 1 的个数等于网络前缀长度。", True,
                      "掩码高位连续 1 的个数就是网络位数，/26 对应 255.255.255.192。"))
    out.append(_judge(kp, "192.168.1.0/24 与 192.168.1.128/25 属于同一种子网。", False,
                      "/25 把 /24 一分为二，二者是不同子网。"))
    out.append(_judge(kp, "10.0.0.0/8 属于私有地址空间，不能在公网路由。", True,
                      "RFC 1918 定义了 10/8、172.16/12、192.168/16 三段私有地址。"))
    return out


# ======================================================================
# 路由选择
# ======================================================================
def gen_routing(rng):
    kp, out = "路由选择", []

    out.append(_single(kp, "路由表中，当多条路由都匹配目的地址时，应该选择（　）。", "前缀最长的那条",
                       ["度量值最小的那条", "最先加入的那条", "前缀最短的那条"],
                       "最长前缀匹配优先，因为它给出了更精确的网络范围。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "默认路由 0.0.0.0/0 的含义是（　）。", "匹配任何目的地址的兜底路由",
                       ["只匹配 0.0.0.0", "匹配私有地址", "表示网络不可达"],
                       "0.0.0.0/0 前缀长度为 0，任何地址都匹配，作为最后兜底。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "距离向量路由协议（如 RIP）的度量通常是（　）。", "跳数",
                       ["带宽", "延迟", "丢包率"], "RIP 以跳数为度量，最大 15 跳，16 表示不可达。",
                       DIFF_EASY, rng))
    out.append(_single(kp, "OSPF 属于（　）路由协议。", "链路状态",
                       ["距离向量", "路径向量", "静态"], "OSPF 用链路状态数据库 + Dijkstra 算法计算最短路径。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "BGP 属于（　）路由协议。", "路径向量",
                       ["距离向量", "链路状态", "静态"], "BGP 用于自治系统之间，基于路径向量避免环路。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "路由表的下一跳字段为直连端口时，表示（　）。", "目的网络与本地接口直接相连",
                       ["需要经过一个路由器", "路由不可达", "使用默认网关"],
                       "直连路由无需下一跳，出接口即为目的网段所在接口。", DIFF_HARD, rng))

    # Dijkstra 跳数（简化小图）
    for _ in range(5):
        cost = rng.choice([3, 4, 5])
        out.append(_single(
            kp, f"某网络中从源到目的存在两条路径，跳数分别为 {cost} 和 {cost + 2}，若采用最短路径优先，会选择（　）。",
            f"{cost} 跳的路径", [f"{cost + 2} 跳的路径", "两条路径负载分担", "随机选择"],
            "最短路径优先（OSPF）按开销最小选路，故选跳数少的。", DIFF_HARD, rng))

    out.append(_judge(kp, "RIP 协议中，跳数为 16 表示目的网络不可达。", True,
                      "RIP 规定 16 为无穷大（不可达），因此最大有效跳数是 15。"))
    out.append(_judge(kp, "静态路由无需运行动态路由协议，配置简单但不能自动适应拓扑变化。", True,
                      "静态路由管理简单、开销小，但链路故障时需人工调整。"))
    out.append(_judge(kp, "路由器转发分组时会修改目的 IP 地址。", False,
                      "路由器只修改源/目的 MAC（逐跳重封装），目的 IP 全程不变。"))
    return out


# ======================================================================
# 传输层
# ======================================================================
def gen_transport(rng):
    kp, out = "传输层", []

    out.append(_single(kp, "传输层的作用是提供（　）。", "端到端的进程间通信",
                       ["主机到主机的通信", "网络间的路由", "比特流传输"],
                       "传输层用端口号区分进程，实现端到端（进程间）通信。", DIFF_EASY, rng))
    out.append(_single(kp, "传输层用（　）来区分同一主机上的不同应用。", "端口号",
                       ["IP 地址", "MAC 地址", "协议号"], "IP 定位主机，端口定位进程，二者合称套接字。",
                       DIFF_EASY, rng))
    out.append(_single(kp, "端口号的长度是（　）位。", "16", ["8", "32", "64"],
                       "端口号 16 位，范围 0~65535；0~1023 为熟知端口。", DIFF_EASY, rng))
    out.append(_single(kp, "熟知端口（well-known port）的范围是（　）。", "0 ~ 1023",
                       ["0 ~ 255", "1024 ~ 49151", "49152 ~ 65535"],
                       "0~1023 为熟知端口（HTTP 80、HTTPS 443、DNS 53 等）。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "HTTP 默认使用的端口号是（　）。", "80",
                       ["21", "443", "8080"], "HTTP 默认 80，HTTPS 默认 443，FTP 为 21。",
                       DIFF_EASY, rng))
    out.append(_single(kp, "TCP 与 UDP 相比，TCP 提供的是（　）。", "面向连接、可靠、字节流",
                       ["无连接、不可靠、报文", "面向连接、不可靠", "无连接、但可靠"],
                       "TCP 面向连接、可靠、字节流；UDP 无连接、不可靠、报文。", DIFF_MEDIUM, rng))

    out.append(_judge(kp, "传输层协议只在端系统（主机）中实现，路由器不处理传输层首部。", True,
                      "路由器只处理到网络层，传输层是端到端的。"))
    out.append(_judge(kp, "同一台主机上不同进程可以使用相同端口号。", False,
                      "同一协议下端口号在主机内唯一标识进程，不能与已占用端口冲突。"))
    out.append(_judge(kp, "UDP 不保证顺序交付，但保留了报文边界。", True,
                      "UDP 是面向报文的，一次发送对应一次接收，不拆分也不合并。"))
    return out


# ======================================================================
# TCP 协议
# ======================================================================
def gen_tcp(rng):
    kp, out = "TCP 协议", []

    # 序号/确认号
    for _ in range(10):
        seq = rng.randint(1000, 9000)
        length = rng.randint(100, 900)
        ack = seq + length
        out.append(_single(
            kp, f"TCP 报文段序号 seq = {seq}，携带 {length} 字节数据，接收方正确接收后应回复的确认号是（　）。",
            str(ack), [str(seq), str(ack + 1), str(seq + length - 1)],
            f"确认号 = 期望收到的下一个字节序号 = {seq} + {length} = {ack}。",
            DIFF_HARD, rng))

    # 三次握手
    out.append(_single(kp, "TCP 建立连接需要（　）次握手。", "3",
                       ["2", "4", "1"], "三次握手：SYN → SYN+ACK → ACK，确保双方收发能力都正常。",
                       DIFF_EASY, rng))
    out.append(_single(kp, "TCP 三次握手中，第二次报文段的标志位是（　）。", "SYN 和 ACK 同时置 1",
                       ["仅 SYN", "仅 ACK", "FIN"],
                       "第二次握手服务器同时确认客户端的 SYN 并发起自己的 SYN，故 SYN+ACK。",
                       DIFF_MEDIUM, rng))
    out.append(_single(kp, "TCP 四次挥手中，主动关闭方最后进入（　）状态。", "TIME_WAIT",
                       ["CLOSE_WAIT", "FIN_WAIT_1", "LAST_ACK"],
                       "主动关闭方进入 TIME_WAIT，等待 2MSL 确保最后的 ACK 能到达。", DIFF_MEDIUM, rng))

    # 拥塞控制
    out.append(_single(kp, "TCP 慢开始阶段，拥塞窗口的增长方式是（　）。", "每经过一个 RTT 翻倍（指数增长）",
                       ["每个 RTT 加一（线性增长）", "保持不变", "每收到一个 ACK 减半"],
                       "慢开始是指数增长；到达慢开始门限后转为拥塞避免（线性增长）。", DIFF_HARD, rng))
    out.append(_single(kp, "TCP 发送方检测到超时后，拥塞窗口通常（　）。", "降为 1 个 MSS 并重新慢开始",
                       ["保持不变", "减半", "翻倍"],
                       "超时视为严重拥塞：ssthresh 减半、cwnd 置 1，重新慢开始。", DIFF_HARD, rng))
    out.append(_single(kp, "TCP 收到 3 个重复 ACK 时通常执行（　）。", "快速重传并进入快速恢复",
                       ["超时重传", "关闭连接", "窗口清零"],
                       "3 个重复 ACK 说明只是个别报文丢失，快速重传避免等待超时。", DIFF_EXPERT, rng))

    # 窗口
    for _ in range(4):
        win, mss = rng.choice([(4000, 1000), (8000, 1000), (16000, 1000), (6000, 1200)])
        n = win // mss
        out.append(_single(
            kp, f"TCP 接收窗口为 {win} 字节、MSS 为 {mss} 字节，发送方一次最多可发送（　）个报文段。",
            str(n), [str(n + 1), str(n * 2), str(max(n - 1, 1))],
            f"最多段数 = 窗口 / MSS = {win} / {mss} = {n}。", DIFF_HARD, rng))

    out.append(_judge(kp, "TCP 是面向字节流的协议，不保留应用层的报文边界。", True,
                      "TCP 把数据看成连续字节流，边界需应用层自行处理。"))
    out.append(_judge(kp, "TCP 的流量控制通过滑动窗口实现，可以防止发送方淹没接收方。", True,
                      "接收方通过通告窗口限制发送速率，这是端到端的流量控制（区别于拥塞控制）。"))
    out.append(_judge(kp, "TIME_WAIT 状态持续 2MSL 是为了让网络中残留的旧报文段消失。", True,
                      "2MSL 既能保证最后 ACK 到达，也能让旧连接的重复报文过期。"))
    out.append(_judge(kp, "TCP 拥塞控制的目的是提高接收方的处理速度。", False,
                      "拥塞控制针对网络拥塞，流量控制才针对接收方处理能力。"))
    return out


# ======================================================================
# UDP 协议
# ======================================================================
def gen_udp(rng):
    kp, out = "UDP 协议", []

    out.append(_single(kp, "UDP 首部的长度是（　）字节。", "8",
                       ["12", "20", "16"], "UDP 首部固定 8 字节：源端口、目的端口、长度、校验和各 2 字节。",
                       DIFF_EASY, rng))
    out.append(_single(kp, "UDP 提供的是（　）服务。", "无连接、不可靠、面向报文",
                       ["面向连接、可靠、字节流", "无连接、可靠、字节流", "面向连接、不可靠、报文"],
                       "UDP 无连接、不保证可靠，但保留报文边界、开销小。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "下列应用最适合使用 UDP 的是（　）。", "实时视频会议",
                       ["文件传输", "电子邮件", "网页浏览"],
                       "实时音视频可容忍少量丢包但不能容忍高延迟，适合 UDP。", DIFF_MEDIUM, rng))
    out.append(_single(kp, "DNS 查询通常使用 UDP 的原因是（　）。", "查询报文短小，UDP 开销低、时延小",
                       ["UDP 保证可靠传输", "UDP 支持加密", "UDP 能自动重传"],
                       "DNS 单次查询报文小，UDP 免去建连开销；响应过大时改用 TCP。", DIFF_HARD, rng))

    # UDP 长度计算
    for _ in range(4):
        payload = rng.choice([100, 200, 512, 1000])
        total = payload + 8
        out.append(_single(
            kp, f"UDP 数据报的载荷为 {payload} 字节，则 UDP 首部中「长度」字段的值是（　）。",
            str(total), [str(payload), str(total + 8), str(payload - 8)],
            f"UDP 长度字段 = 首部 8 字节 + 数据 {payload} 字节 = {total}。", DIFF_HARD, rng))

    out.append(_judge(kp, "UDP 的校验和是可选的，全 0 表示未启用校验。", True,
                      "IPv4 下 UDP 校验和可选；IPv6 下强制使用。"))
    out.append(_judge(kp, "UDP 支持广播和多播。", True,
                      "UDP 天然适合广播/多播；TCP 是点对点连接，不支持广播。"))
    out.append(_judge(kp, "UDP 会因为网络拥塞自动降低发送速率。", False,
                      "UDP 没有拥塞控制，发多快由应用决定，这也是它低时延的原因，但也可能加剧拥塞。"))
    out.append(_judge(kp, "UDP 首部中包含序号字段用于排序。", False,
                      "UDP 首部只有源端口、目的端口、长度、校验和四个字段，没有序号。"))
    return out


def build(rng=None):
    rng = rng or random.Random(20261030)
    out = []
    for gen in (gen_datalink, gen_network_layer, gen_ip, gen_routing,
                gen_transport, gen_tcp, gen_udp):
        out.extend(gen(rng))
    return out
