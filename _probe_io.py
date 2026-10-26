# -*- coding: utf-8 -*-
"""真机 INSTALLATION 文件传输：一次性协议探测。

为什么要有这个脚本
------------------
手册（2017 v2.2）里的单表文件命令，在 31.17 固件上**未必还在** ——
`*?INSTALLATION` 只回 `NUMBER=nn`、`*?INSTALLATION CURRENT TER-CH=…`
直接 NAK（这两条是实测过的）。同步功能到底能走哪条路，只能在真机上问一遍。

于是：把这个脚本拷到仪器旁边跑一次，把生成的整个目录发回来，
就能确定三件现在拿不准的事：

  1. **单表命令还在不在**（按序号取名 / 按名字下载）
  2. **分块传输的 CRC 是什么算法**（从下载数据里的一字节反推）
  3. **数据要不要解 telnet 转义**（0xFF 有没有变成 0xFF 0xFF）

用法
----
    python _probe_io.py --ip 192.168.1.100
    python _probe_io.py --ip 192.168.1.100 --port 2222 --upload

    --upload   额外测一次「上传 + 回读 + 删除」（会在仪器上临时建一张
               ZZ-PROBE-<时间> 表，读完就删掉）。不加这个参数**绝不会写仪器**。

输出
----
    probe-<时间戳>/
        report.txt         ← 人看的总报告（先看这个）
        raw-01-*.bin       ← 每条命令收到的原始字节（含控制字符，用于复算）
        dl-*.XML           ← 真下载下来的表（如果有）

    ⚠️ 报告里有仪器上的频道表名字和表内容，转发前确认一下能不能给。
    ⚠️ 全程只读；只有加了 --upload 才会写（而且会删掉）。

只依赖标准库 + 本仓库的 promax_telnet_test / promax_fileio。
"""
import argparse
import datetime
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import promax_fileio as F                                  # noqa: E402
from promax_telnet_test import PromaxError, PromaxTelnet   # noqa: E402


def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


class Probe(object):
    def __init__(self, ip, port, outdir, timeout=20.0):
        self.ip = ip
        self.port = port
        self.outdir = outdir
        self.timeout = timeout
        self.lines = []
        self.conn = None
        self.link = None
        self.n = 0
        self.verdict = []          # [(项目, 结论)]

    # ------------------------------------------------------------ 报告
    def say(self, msg=""):
        print(msg)
        self.lines.append(msg)

    def head(self, title):
        self.say("")
        self.say("=" * 66)
        self.say("== %s" % title)
        self.say("=" * 66)

    def verdict_add(self, item, ok, detail=""):
        self.verdict.append((item, ok, detail))

    # ------------------------------------------------------------ 连接
    def connect(self):
        self.close()
        self.conn = PromaxTelnet(self.ip, self.port, timeout=self.timeout)
        self.conn.connect()
        self.link = F.RawLink(self.conn)
        self.say("已连接 %s:%d" % (self.ip, self.port))

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None
        self.link = None

    def reconnect(self):
        """仪器只允许单会话；每条探测之间重连一次，避免被上一次的残包干扰"""
        try:
            self.close()
            self.connect()
            return True
        except Exception as e:
            self.say("  !! 重连失败：%s" % e)
            return False

    # ------------------------------------------------------------ 抓原始字节
    def raw(self, cmd, wait=2.5, title=""):
        """发一条命令，把这段时间收到的**原始字节**全抓下来并存盘"""
        if self.link is None:
            self.connect()
        self.n += 1
        tag = "%02d-%s" % (self.n, title or "cmd")
        self.say("")
        self.say("· 发送：%r" % cmd)
        try:
            self.link.drain(quiet=0.2)
            self.link.send(cmd.encode("latin-1") + bytes([13]))
        except Exception as e:
            self.say("  !! 发送失败：%s" % e)
            return b""
        buf = bytearray()
        import time
        t0 = time.time()
        while time.time() - t0 < wait:
            try:
                chunk = self.link._chunk(0.1)
            except Exception:
                break
            if chunk:
                buf.extend(chunk)
                if len(buf) > 512 * 1024:      # 别被大包拖死
                    self.say("  (收到 %d 字节，截断)" % len(buf))
                    break
        data = bytes(buf)
        path = os.path.join(self.outdir, "raw-%s.bin" % tag)
        with open(path, "wb") as f:
            f.write(data)
        self.say("  收到 %d 字节 → %s" % (len(data), os.path.basename(path)))
        self.say("  控制字节：%s" % describe_ctrl(data))
        self.say("  可见文本：%r" % printable(data)[:300])
        self.say("  十六进制（前 160 字节）：")
        for ln in hexdump(data[:160]):
            self.say("    " + ln)
        return data

    # ------------------------------------------------------------ 各探测项
    def probe_ver(self):
        self.head("0. 固件版本 *VER")
        try:
            r = self.conn.command("*VER")
            self.say("响应：%r" % (r,))
            self.verdict_add("固件版本", True, str(r).strip()[:40])
        except Exception as e:
            self.say("  !! 失败：%s" % e)
            self.verdict_add("固件版本", False, str(e))

    def probe_installation(self):
        self.head("1. 安装概况  *?INSTALLATION / *?INSTALLATION CURRENT")
        for cmd in ("*?INSTALLATION", "*?INSTALLATION CURRENT"):
            self.raw(cmd, wait=2.0, title=cmd.strip("*?").replace(" ", "_"))
            self.reconnect()

    def probe_counts(self):
        """用 PlanIO 的正式实现读一次表数（这是同步功能的入口）"""
        self.head("2. 表数（PlanIO.counts）")
        try:
            io = F.PlanIO(self.conn)
            info = io.counts()
            self.say("解析结果：%r" % (info,))
            ok = bool(info.get("TER") or info.get("SAT"))
            self.verdict_add("读表数", ok, "%r" % (info,))
            return info
        except Exception as e:
            self.say("  !! 失败：%s" % e)
            self.verdict_add("读表数", False, str(e))
            return {"TER": 0, "SAT": 0, "name": ""}

    def probe_name_by_index(self):
        self.head("3. 按序号取表名  *?INSTALLATION CURRENT TER-CH=1")
        for kind, tag in (("射频", "TER-CH"), ("卫星", "SAT-CH")):
            data = self.raw("*?INSTALLATION CURRENT %s=1" % tag, wait=2.0,
                            title="name-%s" % tag)
            has_nak = F.NAK in data
            has_line = b"*" in data
            self.verdict_add("按序号取表名（%s）" % kind,
                             has_line and not (has_nak and not has_line),
                             "NAK" if has_nak and not has_line else "有响应行")
            self.reconnect()

    def probe_download(self, kind="TER", names=None):
        self.head("4. 单表下载  *?INSTALLATION CURRENT %s-CH=<名> BLOCK=1024" % kind)
        names = names or []
        if not names:
            self.say("  （没有已知表名，跳过）")
            self.verdict_add("单表下载", False, "没有可用表名")
            return None
        name = names[0]
        self.say("用表名：%s" % name)
        dest = os.path.join(self.outdir, "dl-%s-%s.XML"
                            % (kind, safe(name)))
        try:
            io = F.PlanIO(self.conn, verbose=True)
            dest, size, crc = io.download(kind, name, dest)
            self.say("  ✔ 下载成功：%d 字节，CRC 算法 = %s" % (size, crc))
            tmp = open(dest, "rb").read()
            self.say("  文件开头：%r" % printable(tmp[:120]))
            self.say("  是不是频道表：%s"
                     % ("是" if b"CHANNEL-SET" in tmp[:400] else "**看不出**"))
            self.verdict_add("单表下载", True, "%d 字节" % size)
            self.verdict_add("CRC 算法", crc is not None, str(crc))
            self.crc_name = crc
            self.crc_known = crc is not None
            self.verdict_add("telnet 转义",
                             True,
                             "需要解转义" if getattr(io.link, "iac_mode", "") == "esc"
                             else "不需要（或未能判定）")
            return (name, dest)
        except Exception as e:
            self.say("  !! 下载失败：%s" % e)
            self.say("  （失败也要看上面的原始字节，所以这里退化为 raw 抓一次）")
            self.verdict_add("单表下载", False, str(e))
            self.reconnect()
            self.raw("*?INSTALLATION CURRENT %s-CH=%s BLOCK=1024" % (kind, name),
                     wait=3.0, title="dl-fallback")
            return None

    def probe_zip(self):
        self.head("5. 整装下载  *?INSTALLATION CURRENT ZIP BLOCK=1024")
        dest = os.path.join(self.outdir, "installation.zip")
        try:
            io = F.PlanIO(self.conn, verbose=True)
            io.download_zip(dest)
            size = os.path.getsize(dest)
            self.say("  ✔ 整装包 %d 字节 → %s" % (size, os.path.basename(dest)))
            import zipfile
            try:
                with zipfile.ZipFile(dest) as z:
                    items = [n for n in z.namelist()]
                self.say("  包内 %d 个条目：" % len(items))
                for n in items[:30]:
                    self.say("    %s" % n)
                self.verdict_add("整装下载", True, "%d 字节 / %d 条目"
                                 % (size, len(items)))
            except Exception as e:
                self.say("  !! 下回来了但不是合法 zip：%s" % e)
                self.verdict_add("整装下载", False, "不是 zip：%s" % e)
        except Exception as e:
            self.say("  !! 失败：%s" % e)
            self.verdict_add("整装下载", False, str(e))

    def probe_upload(self, kind="TER"):
        """上传一张自造的小表 → 回读比对 → 删掉。会写仪器，必须显式开启。"""
        self.head("6. 单表上传（写仪器，读完即删）  *INSTALLATION PC FILE …")
        if not self.crc_known:
            self.say("  （CRC 没反推出来，上传必失败，跳过）")
            self.verdict_add("单表上传", False, "CRC 未知，跳过")
            return
        name = "ZZ-PROBE-%s" % datetime.datetime.now().strftime("%H%M%S")
        local = os.path.join(self.outdir, "%s.XML" % name)
        with open(local, "wb") as f:
            f.write(make_probe_plan(kind, name))
        self.say("自造表：%s（%d 字节）" % (os.path.basename(local),
                                          os.path.getsize(local)))
        try:
            io = F.PlanIO(self.conn, verbose=True)
            io.crc_name = self.crc_name
            got = io.upload(local, kind=kind, name=name)
            self.say("  ✔ 上传自称成功，设备回报名字：%r" % (got,))
            back = os.path.join(self.outdir, "dl-%s.XML" % name)
            io.download(kind, name, back)
            same = open(back, "rb").read() == open(local, "rb").read()
            self.say("  回读比对：%s" % ("逐字节一致 ✔" if same else "**不一致** ✘"))
            self.verdict_add("单表上传", same,
                             "回读一致" if same else "回读不一致")
            try:
                io.remove(kind, name)
                self.say("  ✔ 已删除仪器上的 %s" % name)
            except Exception as e:
                self.say("  !! 删除失败（请手动在仪器上删掉 %s）：%s" % (name, e))
        except Exception as e:
            self.say("  !! 上传失败：%s" % e)
            self.verdict_add("单表上传", False, str(e))

    def probe_remove_stale(self):
        """顺手清掉以前探测留下的 ZZ-PROBE-* 表"""
        if not self.crc_known:
            return
        try:
            io = F.PlanIO(self.conn)
            for kind in ("TER", "SAT"):
                for n in io.plan_names(kind):
                    if n.startswith("ZZ-PROBE-"):
                        io.remove(kind, n)
                        self.say("  清掉遗留探测表 %s/%s" % (kind, n))
        except Exception:
            pass

    # ------------------------------------------------------------ 主流程
    def run(self, want_upload=False):
        self.head("PROMAX INSTALLATION 协议探测")
        self.say("时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.say("目标：%s:%d" % (self.ip, self.port))
        self.say("输出目录：%s" % self.outdir)

        self.crc_known = False
        self.crc_name = None

        try:
            self.connect()
        except Exception as e:
            self.say("!! 连不上仪器：%s" % e)
            self.say("   检查：IP 对不对、仪器网络灯、端口（默认 2222）、"
                     "有没有别的程序占着会话。")
            return

        try:
            self.probe_ver()
            self.probe_installation()
            self.reconnect()
            info = self.probe_counts()
            names = {}
            if info.get("TER") or info.get("SAT"):
                # 用 zip 里的名字兜底：万一按序号取名不通，还能拿到表名
                names = self.collect_names()
            self.reconnect()
            self.probe_name_by_index()
            self.reconnect()

            got = None
            if names.get("TER"):
                got = self.probe_download("TER", names["TER"])
            elif names.get("SAT"):
                got = self.probe_download("SAT", names["SAT"])
            # 下载成功就顺带把 CRC 反推出来了（probe_download 里记的），
            # 有了 CRC 才谈得上往仪器上传
            if not got:
                self.say("")
                self.say("（没下到表 → CRC 无从反推 → 测不了上传）")
            self.reconnect()
            self.probe_zip()
            self.reconnect()

            if want_upload:
                self.probe_upload("TER" if names.get("TER") else "SAT")
                self.reconnect()
                self.probe_remove_stale()
        except Exception:
            self.say("")
            self.say("!! 探测过程炸了，下面是完整栈：")
            self.say(traceback.format_exc())
        finally:
            self.close()
            self.report()

    def collect_names(self):
        """通过整装 ZIP 或按序号两条路拿表名（拿不到就返回空）"""
        self.head("2.5 收集表名（先试按序号，再试整装包）")
        out = {"TER": [], "SAT": []}
        try:
            io = F.PlanIO(self.conn)
            for kind in ("TER", "SAT"):
                try:
                    out[kind] = io.plan_names(kind)
                except Exception as e:
                    self.say("  按序号取名（%s）不行：%s" % (kind, e))
        except Exception as e:
            self.say("  !! %s" % e)
        if not (out["TER"] or out["SAT"]):
            try:
                self.say("  改从整装 ZIP 里读文件名…")
                z = os.path.join(self.outdir, "_names.zip")
                io = F.PlanIO(self.conn, verbose=True)
                io.download_zip(z)
                raw = F.zip_plan_names(z)
                # 🔴 设备命令里的"表名"不带 .XML（文件名去掉扩展名），
                #    zip_plan_names 给的是文件名，这里统一剥掉
                out = {k: [os.path.splitext(x)[0].strip() for x in v]
                       for k, v in raw.items()}
            except Exception as e:
                self.say("  整装包也读不到表名：%s" % e)
                out = {"TER": [], "SAT": []}
        self.say("  射频：%s" % (out.get("TER") or "（空）"))
        self.say("  卫星：%s" % (out.get("SAT") or "（空）"))
        return out

    # ------------------------------------------------------------ 收尾报告
    def report(self):
        self.head("结论汇总")
        for item, ok, detail in self.verdict:
            self.say("  [%s] %-22s %s" % ("可以" if ok else "不行", item, detail))
        self.say("")
        self.say("怎么读这份报告：")
        self.say("  · 「单表下载 = 可以」→ 同步功能走主路（快，只动一张表）")
        self.say("  · 「单表下载 = 不行」但「整装下载 = 可以」→ 自动降级到整装 ZIP")
        self.say("  · 两个都不行 → 这台固件把文件命令关掉了，只能走 USB 导入导出")
        self.say("  · 「CRC 算法」那一行决定**能不能往仪器上传**")
        path = os.path.join(self.outdir, "report.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")
        print("")
        print("报告已写入：%s" % path)
        print("把这个目录整个打包发回来就行。")


# ---------------------------------------------------------------- 工具
def safe(s):
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in s)[:40]


def printable(data):
    return bytes(b if 32 <= b < 127 or b in (10, 13) else 46 for b in data)


def describe_ctrl(data):
    if not data:
        return "（没收到东西）"
    hits = []
    for name, byte in (("XON", F.XON), ("XOFF", F.XOFF),
                       ("ACK", F.ACK), ("NAK", F.NAK),
                       ("IAC(0xFF)", 0xFF)):
        c = data.count(bytes([byte]))
        if c:
            hits.append("%s×%d" % (name, c))
    return "、".join(hits) or "没有控制字节"


def hexdump(data):
    out = []
    for i in range(0, len(data), 16):
        row = data[i:i + 16]
        hx = " ".join("%02X" % b for b in row)
        tx = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.append("%04X  %-47s  %s" % (i, hx, tx))
    return out


def make_probe_plan(kind, name):
    """造一张最小的、仪器应该认的频道表（只用于上传探测）"""
    if kind == "SAT":
        body = ('<CHANNEL name="Z1" downlink="3900.00" units="MHz">\n'
                '<SAT band="LOW" polarization="OFF"/>\n'
                '<SIGNAL><DVB-S2><SYMBOLRATE value="30000" units="kSymb/s"/>'
                '</DVB-S2></SIGNAL>\n</CHANNEL>')
        hdr = '<HEADER><TV system="PAL" standard="DK"/><LNB>EXTERNAL</LNB>' \
              '<LNB-LOC-OSC><BAND_C value="5150" units="MHz"/></LNB-LOC-OSC></HEADER>'
        band = "SATELLITE"
    else:
        body = ('<CHANNEL name="Z1" frequency="666.00" units="MHz">\n'
                '<SIGNAL><DVB-C><SYMBOLRATE value="6875" units="kSymb/s"/>'
                '</DVB-C></SIGNAL>\n</CHANNEL>')
        hdr = '<HEADER><TV system="PAL" standard="DK"/><LNB>EXTERNAL</LNB></HEADER>'
        band = "TERRESTRIAL"
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<CHANNEL-SET band="%s" date="%s" equipment="RANGERNeo 4" name="%s" '
           'trademark="PROMAX ELECTRONICA, S.L." version="2.0">\n'
           '%s\n<CHANNELS>\n%s\n</CHANNELS>\n</CHANNEL-SET>\n'
           % (band, "2026-1-1", name, hdr, body))
    return xml.encode("utf-8")


def main():
    ap = argparse.ArgumentParser(
        description="PROMAX INSTALLATION 文件传输协议探测（真机，只读）",
        formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--ip", required=True, help="仪器 IP")
    ap.add_argument("--port", type=int, default=2222, help="Telnet 端口，默认 2222")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="单条命令超时（秒），默认 20")
    ap.add_argument("--out", default=None, help="输出目录（默认 probe-<时间>）")
    ap.add_argument("--upload", action="store_true",
                    help="额外测上传（会在仪器上建一张 ZZ-PROBE-* 表，读完即删）")
    a = ap.parse_args()

    outdir = a.out or "probe-%s" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(outdir, exist_ok=True)
    p = Probe(a.ip, a.port, outdir, timeout=a.timeout)
    p.run(want_upload=a.upload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
