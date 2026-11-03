# -*- coding: utf-8 -*-
"""单表下载握手 · 字节级记录器（只读，不写仪器）

背景
    probe-20261103-141007 证明这台仪器（192.168.1.100）**认**单表命令：

        -> *?INSTALLATION CURRENT TER-CH=DEMO-Plan.XML BLOCK=1024
        <- XOFF ACK *INSTALLATION SIZE=5236 XON

    但 PC 回 ACK 之后，设备一个字节都不发，30 秒后超时。
    手册 v2.2 第 7 页（CAPTURE READ 的 Protocol）写的时序是：

        RANGER -> Xoff, ACK            （文件存在）
        RANGER -> *CAPTURE SIZE=size
        RANGER -> Xon
        PC     -> ACK
        RANGER -> Xoff
        RANGER -> block1 + crc(1B) + Xon
        PC     -> ACK ...

    手册和我们的实现一致，可真机不发数据 —— 所以问题只可能出在
    「ACK 到底该什么时候发、发几次、或者这台固件压根不要 ACK 而要再发一次命令」。
    这些从文档推不出来，只能看字节。

做什么
    对同一个文件连几遍，每遍换一种「SIZE 之后做什么」：
      ack        收到 SIZE 行立刻回 ACK（我们现在这么干）
      quiet      什么都不发，纯等（看设备会不会自己吐数据）
      ack2       回 ACK，3 秒没动静再回一次
      ack512     BLOCK=512（怀疑块大小被钳）
      order      不发 ACK，改发一次同名命令（BLOCK 不变，去掉 ?）

    每遍都把收到的每一个字节连时间戳记下来，最后打印时间线。
    判定：有没有一口气收到 >200 字节的块 ⇒ 数据到底流没流。

用法
    python _probe_dl.py --ip 192.168.1.100
    python _probe_dl.py --ip 192.168.1.100 --kind SAT --idx 11
    python _probe_dl.py --ip 192.168.1.100 --only ack,order
    python _probe_dl.py --ip 192.168.1.100 --name "DEMO-Daily.XML"

    输出目录 probe-dl-<时间>/ 连 report.txt 一起发回来。

⚠️ 只读：全程不发任何写命令。上传相关的命令这里一条都没有。
"""

import argparse
import os
import socket
import sys
import time

CR = b"\r"
XON, XOFF, ACK, NAK = 0x11, 0x13, 0x06, 0x15
CTRL_NAMES = {XON: "XON", XOFF: "XOFF", ACK: "ACK", NAK: "NAK", 0x0D: "CR", 0x0A: "LF"}
TAGS = {"TER": "TER-CH", "SAT": "SAT-CH"}


# ----------------------------------------------------------------- 字节记录
class Rec:
    """把收到的字节按到达时间切片存下来"""

    def __init__(self):
        self.chunks = []                 # [(t, bytes)]
        self.t0 = time.time()

    def add(self, data):
        self.chunks.append((time.time() - self.t0, data))

    @property
    def total(self):
        return sum(len(d) for _t, d in self.chunks)

    def payload(self):
        """去掉控制字节、telnet IAC 协商后剩下的（文本 + 数据）"""
        raw = bytearray()
        for _t, d in self.chunks:
            raw.extend(d)
        out = bytearray()
        i, n = 0, len(raw)
        while i < n:
            b = raw[i]
            if b == 0xFF:                    # IAC：跳过 2 或 3 字节
                if i + 1 < n and raw[i + 1] in (0xFB, 0xFC, 0xFD, 0xFE):
                    i += 3
                elif i + 1 < n and raw[i + 1] == 0xFF:
                    out.append(0xFF)         # 转义过的 0xFF 是数据
                    i += 2
                else:
                    i += 2
                continue
            if b not in CTRL_NAMES:
                out.append(b)
            i += 1
        return bytes(out)

    def controls(self):
        cnt = {}
        for _t, d in self.chunks:
            for b in d:
                if b in CTRL_NAMES and b != 0x0D and b != 0x0A:
                    cnt[CTRL_NAMES[b]] = cnt.get(CTRL_NAMES[b], 0) + 1
        return cnt

    def biggest(self):
        return max([len(d) for _t, d in self.chunks] or [0])

    def timeline(self, width=64):
        lines = []
        for t, d in self.chunks:
            head = d[:width]
            txt = "".join(CTRL_NAMES.get(b, chr(b) if 32 <= b < 127 else ".")
                          for b in head)
            tail = " …共 %d 字节" % len(d) if len(d) > width else ""
            hexs = " ".join("%02X" % b for b in head[:20])
            lines.append("    %6.3fs  %5d B  |%s|%s"
                         % (t, len(d), txt, tail))
            lines.append("                    hex %s" % hexs)
        return "\n".join(lines)


def connect(ip, port, timeout, log):
    s = socket.socket()
    s.settimeout(timeout)
    s.connect((ip, port))
    s.setblocking(False)
    log("已连接 %s:%d" % (ip, port))
    return s


def close(s):
    """SO_LINGER(1,0) 强制 RST —— 仪器只允许一个会话，留半死连接会让后面全部无响应"""
    try:
        import struct
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except Exception:
        pass
    try:
        s.close()
    except Exception:
        pass


def pump(s, rec, seconds, stops=(), label=""):
    """收 seconds 秒，全部记进 rec

    stops 命中（按已收正文做子串匹配）就提前收工。
    🔴 必须支持 stop：真机的 `*INSTALLATION SIZE=5236` **不带 CR**，
       结尾只有 XON。只认 CR 的话这里会白等满 10 秒，期间设备在等我们 ACK，
       两边互等到超时 —— 探针就会报出「没拿到 SIZE 行」这种假结论。
    """
    end = time.time() + seconds
    while time.time() < end:
        try:
            d = s.recv(65536)
        except BlockingIOError:
            time.sleep(0.02)
            continue
        except OSError as e:
            log_line(label, "连接出错：%s" % e)
            break
        if not d:
            time.sleep(0.02)
            continue
        rec.add(d)
        if stops:
            txt = rec.payload()
            if any(p in txt for p in stops):
                break
    return bool(rec.chunks)


def log_line(label, msg):
    print(("[%s] " % label if label else "") + msg)


def ask_name(ip, port, timeout, kind, idx, log):
    """用序号取真实文件名（这条在真机上验过，0 基）"""
    s = connect(ip, port, timeout, log)
    try:
        rec = Rec()
        s.sendall(("*?INSTALLATION CURRENT %s=%d" % (TAGS[kind], idx)).encode() + CR)
        pump(s, rec, 4.0, stops=(b"\r",))
        txt = rec.payload().decode("latin-1", "replace")
        for tok in txt.split():
            if tok.startswith("NAME="):
                return tok[5:].strip()
        return ""
    finally:
        close(s)


# ----------------------------------------------------------------- 各种尝试
def trial(ip, port, timeout, name, kind, block, mode, log, wait_data=18.0):
    """跑一种尝试，返回 (rec, 说明)"""
    tag = TAGS[kind]
    cmd = "*?INSTALLATION CURRENT %s=%s BLOCK=%d" % (tag, name, block)
    s = connect(ip, port, timeout, log)
    rec = Rec()
    try:
        log("  发送：%s" % cmd)
        s.sendall(cmd.encode("latin-1") + CR)

        # 先等 SIZE 行（最多 10 秒）；零字节就重连再试一次 ——
        # 仪器只许一个会话，上一轮残留连接会让新连接"连得上、收不到"
        pump(s, rec, 10.0, stops=(b"\r", b"SIZE="), label=mode)
        if rec.total == 0:
            log("  一个字节都没收到 → 关连接重试一次（多半是残留会话占着）")
            close(s)
            s = connect(ip, port, timeout, log)
            rec = Rec()
            s.sendall(cmd.encode("latin-1") + CR)
            pump(s, rec, 10.0, stops=(b"\r", b"SIZE="), label=mode)

        txt = rec.payload().decode("latin-1", "replace").strip()
        log("  SIZE 行：%r（%d 字节）" % (txt, rec.total))
        if "SIZE=" not in txt:
            return rec, "没拿到 SIZE 行，这条命令本身就不认"

        if mode == "quiet":
            log("  不发 ACK，纯等 %.0f 秒" % wait_data)
            pump(s, rec, wait_data, label=mode)

        elif mode == "order":
            time.sleep(0.2)
            cmd2 = cmd.replace("*?", "*", 1)      # 去掉 ?，当订单发
            log("  改发同名命令（不带 ?，当订单用）：%s" % cmd2)
            s.sendall(cmd2.encode("latin-1") + CR)
            pump(s, rec, wait_data, label=mode)

        elif mode == "ack2":
            s.sendall(bytes([ACK]))
            log("  已回 ACK，等 3 秒")
            pump(s, rec, 3.0, label=mode)
            if rec.biggest() < 200:
                s.sendall(bytes([ACK]))
                log("  3 秒没数据，再回一次 ACK，再等 %.0f 秒" % wait_data)
                pump(s, rec, wait_data, label=mode)
            else:
                pump(s, rec, wait_data - 3.0, label=mode)

        else:                                    # ack（现状）
            s.sendall(bytes([ACK]))
            log("  已回 ACK，等 %.0f 秒" % wait_data)
            pump(s, rec, wait_data, label=mode)

        # 判定
        if rec.biggest() > 200:
            return rec, "数据流起来了（最大一片 %d 字节）" % rec.biggest()
        return rec, "SIZE 之后设备仍无数据（最大一片 %d 字节）" % rec.biggest()
    finally:
        close(s)


def main():
    ap = argparse.ArgumentParser(description="单表下载握手字节级记录（只读）")
    ap.add_argument("--ip", default="192.168.1.100")
    ap.add_argument("--port", type=int, default=2222)
    ap.add_argument("--kind", default="TER", choices=["TER", "SAT"])
    ap.add_argument("--idx", type=int, default=0, help="用第几张表（0 基）")
    ap.add_argument("--name", default="", help="直接指定文件名（含 .XML），跳过序号查询")
    ap.add_argument("--block", type=int, default=1024)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--only", default="", help="只跑哪几种，逗号分隔：ack,quiet,ack2,ack512,order")
    ap.add_argument("--wait", type=float, default=18.0, help="每种尝试等多久")
    ap.add_argument("--out", default="", help="输出目录（默认 probe-dl-<时间>）")
    a = ap.parse_args()

    out = a.out or ("probe-dl-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)

    lines = []

    def log(msg):
        print(msg)
        lines.append(msg)

    log("=" * 66)
    log("== PROMAX 单表下载握手 · 字节级记录")
    log("=" * 66)
    log("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("目标：%s:%d" % (a.ip, a.port))
    log("输出目录：%s" % out)

    # 1. 先确认表名（能查到就说明序号路线是通的）
    name = a.name.strip()
    if not name:
        log("")
        log("-- 0. 取第 %d 张 %s 表的真实文件名" % (a.idx, a.kind))
        try:
            name = ask_name(a.ip, a.port, a.timeout, a.kind, a.idx, log)
        except Exception as e:
            log("  取名字失败：%s" % e)
            name = ""
        log("  ⇒ %r" % name if name else "  ⇒ 没取到名字")
    if not name:
        log("")
        log("没拿到表名就没法继续。先跑 _probe_ports.py 确认 2222 还活着，"
            "或用 --name 手动指定（别忘带 .XML）。")
        write(out, lines)
        return 2

    # 2. 逐种尝试
    only = [x.strip() for x in a.only.split(",") if x.strip()]
    plan = [("ack", a.block), ("quiet", a.block), ("ack2", a.block),
            ("ack512", 512), ("order", a.block)]
    if only:
        plan = [p for p in plan if p[0] in only]

    results = []
    for mode, block in plan:
        log("")
        log("-- %s（BLOCK=%d）" % (mode, block))
        try:
            rec, verdict = trial(a.ip, a.port, a.timeout, name, a.kind,
                                 block, mode, log, wait_data=a.wait)
        except Exception as e:
            log("  !! 整轮失败：%s" % e)
            results.append((mode, block, "%s（异常 %s）" % ("没跑起来", e)))
            continue
        fn = os.path.join(out, "%s-%d.bin" % (mode, block))
        with open(fn, "wb") as f:
            for _t, d in rec.chunks:
                f.write(d)
        log("  控制字节：%s；共收 %d 字节 → %s"
            % (rec.controls(), rec.total, os.path.basename(fn)))
        if rec.chunks:
            log(rec.timeline())
        log("  ⇒ %s" % verdict)
        results.append((mode, block, verdict))

    # 3. 结论
    log("")
    log("=" * 66)
    log("== 结论")
    log("=" * 66)
    log("表名：%r" % name)
    for mode, block, verdict in results:
        log("  %-8s BLOCK=%-5d %s" % (mode, block, verdict))
    good = [r for r in results if "数据流起来了" in r[2]]
    log("")
    if good:
        log("⇒ 有可用的握手法：%s。把 report 发回来，我按它改 promax_fileio。" % good[0][0])
    else:
        log("⇒ 这几种握手法都拿不到数据。这台固件很可能只认整装 ZIP 或文件系统路线"
            "（SFTP / webControl），telnet 单表下载先别惦记了。")
        log("   下一步优先跑：python promax_sftp.py %s" % a.ip)
    log("")
    log("把这整个目录（report.txt + *.bin）发回来。")

    write(out, lines)
    return 0


def write(out, lines):
    p = os.path.join(out, "report.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n报告已写入：%s" % p)


if __name__ == "__main__":
    sys.exit(main())
