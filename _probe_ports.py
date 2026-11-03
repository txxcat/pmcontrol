# -*- coding: utf-8 -*-
"""仪器三条远程通道的存活体检（只看，不写）。

为什么要有这个脚本
------------------
RANGER Neo 4 的手册（F9.0 / 2026-07）写明仪器对外有三条独立通道：

    2222  Telnet   远程控制命令（本程序同步功能用的就是这条）
      22  SSH      走 SFTP 传文件，手册 11.3.4 给了账号：
                   User=Promax  Password=Monitoring2019
      80  HTTP     webControl，浏览器直接开，默认密码 Password

这三条是**三个独立服务**。所以「程序里仪器不应答」不等于「仪器死了」：
2222 不回，很可能 22 和 80 还是好的 —— 那就说明是远程控制会话被占住
或者那个服务卡了，而不是整机崩了。先分清是哪一种，处置完全不同。

判据
----
  2222 正常 ：连上后每秒一个 XON(0x11) 心跳，发 `*?VER` 会回 `*VER ...`
  2222 静默 ：连得上、一个字节都不回 → 就是程序里那个「仪器没有应答」
  22   正常 ：立刻回一行 `SSH-2.0-...`
  80   正常 ：回 `HTTP/1.x 200`（或 302），页面标题一般带 webControl

用法
----
    python _probe_ports.py --ip 10.8.8.188
    python _probe_ports.py --ip 10.8.8.188 --timeout 5

只连、只读、只发一条查询命令，不改仪器任何状态。只依赖标准库。
"""
import argparse
import datetime
import os
import socket
import sys

CR = b"\r"
XON = 0x11
XOFF = 0x13
ACK = 0x06
NAK = 0x15

CTRL_NAME = {XON: "XON", XOFF: "XOFF", ACK: "ACK", NAK: "NAK"}


def ctrl_summary(buf):
    """把控制字符按出现次数统计成人话（心跳证据）"""
    counts = {}
    for b in buf:
        if b in CTRL_NAME:
            counts[CTRL_NAME[b]] = counts.get(CTRL_NAME[b], 0) + 1
    if not counts:
        return "无控制字符"
    return "、".join("%s×%d" % (k, v) for k, v in sorted(counts.items()))


def printable(buf, limit=300):
    """原始字节 → 可读（控制字符换成点，便于看命令回显）"""
    out = []
    for b in buf[:limit]:
        if b in CTRL_NAME:
            out.append("<%s>" % CTRL_NAME[b])
        elif b in (0x0D, 0x0A):
            out.append("\n")
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append("\\x%02X" % b)
    text = "".join(out)
    if len(buf) > limit:
        text += "\n…（还有 %d 字节）" % (len(buf) - limit)
    return text


def recv_some(sock, seconds):
    """在 seconds 秒内尽量多收，收满就提前返回"""
    sock.settimeout(0.4)
    buf = b""
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
    while datetime.datetime.now() < deadline:
        try:
            part = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not part:
            break
        buf += part
    return buf


def probe_telnet(ip, port, timeout):
    """2222：连上先静听，有心跳再发 *?VER"""
    r = {"port": port, "name": "Telnet 远程控制", "alive": False,
         "verdict": "", "detail": []}
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
    except OSError as e:
        r["verdict"] = "连不上（%s）" % e
        return r

    try:
        n = min(3.0, timeout)
        buf = recv_some(s, n)
        r["detail"].append("静听 %g 秒收到 %d 字节：%s"
                           % (n, len(buf), ctrl_summary(buf)))
        if not buf:
            r["verdict"] = ("连得上但零字节 —— 就是程序里那个「仪器没有应答」"
                            "（会话被占 / 该服务卡死，不是网络不通）")
            return r

        s.sendall(b"*?VER" + CR)
        resp = recv_some(s, min(6.0, timeout))
        r["detail"].append("发 `*?VER` 后收到 %d 字节" % len(resp))
        if resp:
            r["detail"].append("原始：" + printable(resp).strip())
        text = resp.decode("latin-1", "replace")
        if "*VER" in text.upper():
            r["alive"] = True
            r["verdict"] = "正常，远程控制可用"
        elif NAK in resp:
            r["alive"] = True
            r["verdict"] = "活着（回了 NAK，说明能收命令但这条被拒）"
        elif resp:
            r["alive"] = True
            r["verdict"] = "活着（有回应，但不是预期的 *VER 行）"
        else:
            r["verdict"] = "有心跳但命令无回应（服务半死，建议重启仪器）"
    finally:
        try:
            s.close()
        except OSError:
            pass
    return r


def probe_ssh(ip, port, timeout):
    """22：SSH 服务端会主动送 banner，读到 SSH- 开头就算活着"""
    r = {"port": port, "name": "SSH / SFTP 传文件", "alive": False,
         "verdict": "", "detail": []}
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
    except OSError as e:
        r["verdict"] = "连不上（%s）" % e
        return r
    try:
        buf = recv_some(s, min(4.0, timeout))
        line = buf.decode("latin-1", "replace").strip().splitlines()
        if line and line[0].startswith("SSH-"):
            r["alive"] = True
            r["detail"].append("banner：" + line[0])
            r["verdict"] = "SSH 活着，SFTP 通道可用"
        elif buf:
            r["detail"].append("原始：" + printable(buf).strip())
            r["verdict"] = "有回应但不是 SSH banner"
        else:
            r["verdict"] = "连得上但零字节（服务卡住）"
    finally:
        try:
            s.close()
        except OSError:
            pass
    return r


def probe_http(ip, port, timeout):
    """80：发一个最小 GET，看有没有 HTTP 状态行"""
    r = {"port": port, "name": "HTTP webControl", "alive": False,
         "verdict": "", "detail": []}
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
    except OSError as e:
        r["verdict"] = "连不上（%s）" % e
        return r
    try:
        req = ("GET / HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n"
               % ip).encode("latin-1")
        s.sendall(req)
        buf = b""
        s.settimeout(0.5)
        deadline = datetime.datetime.now() + datetime.timedelta(
            seconds=min(6.0, timeout))
        while datetime.datetime.now() < deadline and len(buf) < 65536:
            try:
                part = s.recv(4096)
            except socket.timeout:
                break
            except OSError:
                break
            if not part:
                break
            buf += part
            if b"\r\n\r\n" in buf:
                break
        head = buf.split(b"\r\n\r\n")[0].decode("latin-1", "replace")
        status = head.splitlines()[0] if head else ""
        if status.upper().startswith("HTTP/"):
            r["alive"] = True
            r["detail"].append(status)
            r["verdict"] = "webControl 活着，可用浏览器访问"
            for ln in head.splitlines()[1:]:
                if ln.lower().startswith(("server:", "location:")):
                    r["detail"].append(ln.strip())
        elif buf:
            r["detail"].append("原始：" + printable(buf).strip())
            r["verdict"] = "有回应但不是 HTTP"
        else:
            r["verdict"] = "连得上但零字节（服务卡住）"
    finally:
        try:
            s.close()
        except OSError:
            pass
    return r


def advice(results):
    """按存活组合给处置建议"""
    by = {r["port"]: r for r in results}
    tel = by.get(2222, {}).get("alive")
    ssh = by.get(22, {}).get("alive")
    web = by.get(80, {}).get("alive")
    out = []
    if tel:
        out.append("• 2222 正常 → 直接跑同步/测试即可，不用管下面这些。")
        return out
    if web:
        out.append("• 80 回 HTTP → 用浏览器开 http://<仪器IP>/ ，密码默认 Password；"
                   "进 Settings 里有 “Reboot Equipment”，不用拔电源就能重启仪器。")
        out.append("• 顺便看一眼右上角有没有 webControl 已连接图标 —— "
                   "有就说明有别的会话占着（另一台电脑 / 没关的浏览器标签）。")
    if ssh:
        out.append("• 22 回 SSH → 传频道表其实有正规通道："
                   "SFTP Promax / Monitoring2019，端口 22，"
                   "直接读写安装目录里的 CH/TER、CH/SAT。"
                   "这条不依赖 2222，也不涉及分块 + CRC。")
    if not any((tel, ssh, web)):
        out.append("• 三条全不通 → 检查 IP / 网线 / 是否接的是 IPCTRL 口；"
                   "仍不通就断电重启仪器。")
    elif not tel:
        out.append("• 2222 不响应但别的服务活着 → 是远程控制服务被占或卡死。"
                   "先重启仪器（webControl 的 Reboot 或断电），再跑 _probe_io.py。")
    return out


def main():
    ap = argparse.ArgumentParser(description="仪器三条远程通道存活体检（只读）")
    ap.add_argument("--ip", required=True, help="仪器 IP，例如 10.8.8.188")
    ap.add_argument("--timeout", type=float, default=3.0, help="单步超时秒数")
    ap.add_argument("--save", action="store_true", help="把报告写成文件")
    args = ap.parse_args()

    print("=" * 62)
    print("仪器远程通道体检   %s   %s"
          % (args.ip, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("=" * 62)

    results = [
        probe_telnet(args.ip, 2222, args.timeout),
        probe_ssh(args.ip, 22, args.timeout),
        probe_http(args.ip, 80, args.timeout),
    ]

    lines = []
    for r in results:
        flag = "✔ 活" if r["alive"] else "✘ 不通"
        head = "%-5s %-16s %s" % (r["port"], r["name"], flag)
        print("\n" + head)
        print("      " + r["verdict"])
        lines.append(head + "  " + r["verdict"])
        for d in r["detail"]:
            for i, ln in enumerate(d.splitlines()):
                prefix = "      · " if i == 0 else "        "
                print(prefix + ln)
                lines.append(prefix + ln)

    print("\n" + "-" * 62)
    print("怎么办")
    print("-" * 62)
    tips = advice(results)
    for t in tips:
        print(t)
        lines.append(t)

    if args.save:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "probe-ports-%s" % stamp)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "report.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("仪器 IP：%s\n" % args.ip)
            f.write("\n".join(lines))
        print("\n报告已保存：%s" % p)

    return 0


if __name__ == "__main__":
    sys.exit(main())
