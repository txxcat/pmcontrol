# -*- coding: utf-8 -*-
"""设备 ↔ 本地的频道表文件传输（INSTALLATION 命令族，分块 + 1 字节 CRC）。

手册依据（`remote-control-commands-promax-analyzers.pdf` v2.2 / 2017）
------------------------------------------------------------------
下载（设备 → PC），单张表：
    PC : *?INSTALLATION CURRENT TER-CH=<表名> BLOCK=<块字节数><CR>
    DEV: XOFF
    DEV: ACK               （文件存在；不存在则 NAK + XON，命令中止）
    DEV: *INSTALLATION SIZE=<文件字节数><CR> XON
    PC : ACK
    DEV: XOFF，然后逐块：<数据 N 字节> + <CRC 1 字节> + XON
    PC : 每块校验 CRC → ACK；错就 NAK（设备重发或中止）
上传（PC → 设备），单张表：
    PC : *INSTALLATION PC FILE TAG=TER-CH NAME=<表名> SIZE=<n> BLOCK=<块><CR>
         然后逐块下发 <数据> + <CRC>，设备每块回 ACK
删除：*INSTALLATION CURRENT REMOVE TAG=TER-CH <表名>
整装：*?INSTALLATION CURRENT ZIP BLOCK=<n>  /  *INSTALLATION PC ZIP NAME=.. SIZE=.. BLOCK=..

🔴 三个必须记住的坑
------------------
1. **这是裸字节流，不能按行读**。现有 `PromaxTelnet.command()` 靠 CR+LF 判结束、
   把 XON 当心跳 —— 用它收二进制块会当场错乱。本模块直接操作 socket，
   一律"读满 N 字节"，绝不扫描 XON。
2. **数据里可能真的出现 0x11/0x13/0xFF**。所以分块只能靠"块长度"切，
   CRC 只用来验对错。0xFF 的 telnet 转义（0xFF 0xFF）用双模式兜：
   先按原样算 CRC，全不匹配再按"解转义"重试（见 IAC_MODE）。
3. **CRC 算法手册没写**（只写 `crc (1 byte)`）。做法：从**下载**的块里反推
   —— 把 5 种常见实现都算一遍，哪个对上就是哪个，记到 `PlanIO.crc_name`。
   所以顺序必须是**先下载、后上传**；上传前若还不知道算法，拒绝上传。

用法
----
    with PromaxTelnet(ip, 2222) as c:
        io = PlanIO(c)
        print(io.list_plans("TER"))              # 设备上有哪些表
        io.download("TER", "TW-Daily", "CH/TER/TW-Daily.XML")
        io.upload("CH/TER/TW-Daily.XML")
"""
import os
import select
import time
import zipfile

from promax_telnet_test import (ACK, NAK, XON, XOFF, CR, LF,
                                PromaxConnLost, PromaxError)

CR_B, LF_B = bytes([CR]), bytes([LF])
ACK_B, NAK_B, XON_B, XOFF_B = (bytes([ACK]), bytes([NAK]),
                               bytes([XON]), bytes([XOFF]))

DEFAULT_BLOCK = 1024          # 块大小（手册示例用 1024/2048，取小一点更稳）
MAX_FILE = 8 * 1024 * 1024    # 单张表不可能这么大，超了就是协议乱了

TAGS = {"TER": "TER-CH", "SAT": "SAT-CH"}


class FileIOError(PromaxError):
    """频道表文件传输错误（用户可见）"""


# ---------------------------------------------------------------- CRC 候选
def crc8_poly07(data, init=0x00):
    """CRC-8/ATM：poly=0x07, init=0x00（嵌入式最常见的写法之一）"""
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc8_poly31(data, init=0x00):
    """CRC-8/MAXIM（Dallas）：poly=0x31 反向，reflected"""
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 0x01 else crc >> 1
    return crc


def crc_xor(data):
    v = 0
    for b in data:
        v ^= b
    return v


def crc_sum(data):
    return sum(data) & 0xFF


def crc_sum_neg(data):
    return (-sum(data)) & 0xFF


def crc_sum_neg_crc8(data):
    """有些固件用 `0x100 - crc8` 的形式"""
    return (-crc8_poly07(data)) & 0xFF


# 名字 → 函数。顺序即优先级（命中第一个就定案）
CRC_CANDIDATES = [
    ("crc8-0x07", crc8_poly07),
    ("crc8-0x31", crc8_poly31),
    ("xor", crc_xor),
    ("sum", crc_sum),
    ("sum-neg", crc_sum_neg),
    ("crc8-0x07-neg", crc_sum_neg_crc8),
]


def guess_crc(data, want):
    """返回第一个算出 want 的算法名，都不匹配返回 None"""
    for name, fn in CRC_CANDIDATES:
        if fn(data) == want:
            return name
    return None


def crc_of(name, data):
    for n, fn in CRC_CANDIDATES:
        if n == name:
            return fn(data)
    raise FileIOError("未知的 CRC 算法：%s" % name)


# ---------------------------------------------------------------- 裸字节读写
class RawLink(object):
    """在 PromaxTelnet 的 socket 上做"读满 N 字节"的裸读写。

    🔴 三条硬要求（都踩过）：
    1. 必须有**自己的待处理缓冲** `_pend`：`recv()` 一次可能多收（读到下一块的
       开头），若直接把整块当"本块"用，数据就错位了。
    2. **等到控制字节不能顺手扔数据**。发送方常把 XOFF 紧贴数据发过来，
       旧写法"收集到别的字节就丢弃"会把数据首字节吃掉。
    3. IAC（0xFF）转义要不要解，事先不知道 → 两种模式都支持，
       由 CRC 结果决定（见 PlanIO._check_or_learn_crc）。
    """

    def __init__(self, conn, verbose=False):
        self.conn = conn
        self.verbose = verbose
        self.iac_mode = "raw"        # raw = 不解 0xFF 转义；esc = 按 telnet 解转义
        self._pend = bytearray()     # 已收未用字节（原始）
        self._ff = False             # esc 模式下等待 0xFF 的配对字节

    # -- 日志
    def _log(self, msg):
        if self.verbose:
            print("    · [io] %s" % msg)

    # -- 底层
    def _chunk(self, wait=0.05):
        b = self.conn._read_chunk(wait)
        if b is None:
            raise PromaxConnLost("设备关闭了连接", kind="closed",
                                 host=self.conn.host, port=self.conn.port)
        return b

    def drain(self, quiet=0.15):
        """丢掉残留字节（上个命令的行尾、心跳 XON 等）"""
        try:
            self.conn._buf = bytearray()
            self.conn._lines = []
        except AttributeError:
            pass
        t0 = time.time()
        got = len(self._pend)
        self._pend = bytearray()
        self._ff = False
        while time.time() - t0 < quiet:
            b = self._chunk(0.02)
            got += len(b)
            if b:
                t0 = time.time()
        self._log("drain 丢弃 %d 字节" % got)

    def send(self, data):
        self._log("TX %d 字节 %r" % (len(data), data[:40]))
        self.conn._send_raw(data)

    def _ingest(self, raw):
        """把收到的原始字节并入 _pend（esc 模式下解 IAC 转义）"""
        if self.iac_mode != "esc":
            self._pend.extend(raw)
            return
        for byte in raw:
            if self._ff:
                self._ff = False
                if byte == 0xFF:
                    self._pend.append(0xFF)
                # 否则是 IAC 序列，整段丢掉
                continue
            if byte == 0xFF:
                self._ff = True
                continue
            self._pend.append(byte)

    def _ensure(self, n, timeout=20.0):
        """保证 _pend 里至少有 n 字节"""
        t0 = time.time()
        while len(self._pend) < n:
            if time.time() - t0 > timeout:
                return False
            b = self._chunk(0.05)
            if b:
                self._ingest(b)
        return True

    def take(self, n, timeout=20.0):
        """取走恰好 n 字节（不够就等，超时抛错）"""
        if not self._ensure(n, timeout):
            raise FileIOError("接收超时：要 %d 字节，只收到 %d 字节"
                              % (n, len(self._pend)))
        out = bytes(self._pend[:n])
        del self._pend[:n]
        return out

    def read_exact(self, n, timeout=20.0):
        return self.take(n, timeout)

    def skip_ctrl(self, timeout=10.0):
        """丢掉开头的流控/换行字节，停在第一个数据字节前（**不消费数据**）"""
        t0 = time.time()
        while True:
            while self._pend and self._pend[0] in (XON, XOFF, CR, LF, ACK, NAK):
                del self._pend[:1]
            if self._pend:
                return True
            if time.time() - t0 > timeout:
                return False
            b = self._chunk(0.05)
            if b:
                self._ingest(b)

    def wait_ctrl(self, wanted=(ACK, NAK), timeout=8.0):
        """等一个控制字节（wanted 之内）；夹在中间的流控/换行直接跳过。

        🔴 遇到**数据字节**时抛错而不是丢弃 —— 那意味着协议时序和预期不同，
        丢掉就再也对不齐了（排查时这条错误信息比静默乱码有用得多）。
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._pend:
                b = self._pend[0]
                if b in wanted:
                    del self._pend[:1]
                    self._log("收到控制字节 0x%02X" % b)
                    return b
                if b in (XON, XOFF, CR, LF):
                    del self._pend[:1]
                    continue
                raise FileIOError("等控制字节时收到数据字节 0x%02X（期望 %s）"
                                  % (b, [hex(x) for x in wanted]))
            chunk = self._chunk(0.05)
            if chunk:
                self._ingest(chunk)
        raise FileIOError("等控制字节超时（想要 %s）"
                          % ([hex(x) for x in wanted],))

    def read_line(self, timeout=8.0):
        """读一行（到 CR/LF 为止），返回 str

        ⚠️ 响应前面常紧贴着 XOFF/ACK（仪器发完控制字节立刻跟正文），
        所以正文里要**剔掉控制字节**再返回，否则调用方拿到的行不以 `*` 开头。
        """
        deny = (XON, XOFF, ACK, NAK)

        def clean(raw):
            txt = bytes(b for b in raw if b not in deny)
            return txt.decode("latin-1").strip(" \t\x00\r\n")

        t0 = time.time()
        while time.time() - t0 < timeout:
            i_cr = self._pend.find(CR_B)
            i_lf = self._pend.find(LF_B)
            idx = [x for x in (i_cr, i_lf) if x >= 0]
            if idx:
                n = min(idx)
                line = clean(self._pend[:n])
                del self._pend[:n + 1]
                while self._pend and self._pend[0] in (CR, LF):
                    del self._pend[:1]
                if line:
                    return line
                continue
            chunk = self._chunk(0.05)
            if chunk:
                self._ingest(chunk)
        if self._pend:
            line = clean(self._pend)
            self._pend = bytearray()
            return line
        raise FileIOError("等响应行超时")


# ---------------------------------------------------------------- 主类
class PlanIO(object):
    """频道表文件传输。所有方法都是**同步阻塞**的，调用方放后台线程。"""

    def __init__(self, conn, block=DEFAULT_BLOCK, verbose=False, progress=None):
        self.conn = conn
        self.link = RawLink(conn, verbose=verbose)
        self.block = max(128, int(block))
        self.verbose = verbose
        self.progress = progress      # callback(done_bytes, total_bytes, text)
        self.crc_name = None          # 下载时反推出来的算法名
        self.last_crc_tries = []      # 最近一块的 CRC 尝试记录（诊断用）

    # -- 日志
    def _log(self, msg):
        if self.verbose:
            print("    · [planio] %s" % msg)

    def _tick(self, done, total, text=""):
        if self.progress:
            try:
                self.progress(done, total, text)
            except Exception:
                pass

    # ------------------------------------------------------------ 列举
    def counts(self):
        """设备上有多少张表：返回 {"TER": n, "SAT": n, "name": 当前安装名}"""
        self.link.drain()
        self.link.send(b"*?INSTALLATION CURRENT" + CR_B)
        line = self._read_resp_line(tries=4, timeout=5.0)
        kv = parse_kv(line)
        out = {"TER": 0, "SAT": 0, "name": kv.get("NAME", "")}
        for key, kind in (("TER-CH", "TER"), ("SAT-CH", "SAT")):
            if key in kv:
                try:
                    out[kind] = int(kv[key])
                except ValueError:
                    pass
        # 真机这条命令会回**多行**（NAME/TER-CH/... 换行继续），
        # 剩下的续行留着会污染下一条命令的解析 → 直接清掉
        self.link.drain(quiet=0.08)
        return out

    def plan_names(self, kind):
        """列出某频段所有表名。

        手册：`*?INSTALLATION CURRENT TER-CH=<序号>` 取第 n 张的名字
        （PDF 里这张表排版乱了，响应形状靠实测；这里做宽松解析）

        🔴 被拒时设备只回一个 NAK、**不发响应行**（31.17 实测如此）。
           所以发完命令先看控制字节：拿到 NAK 立刻抛错，
           不然要白等好几轮行超时（每轮 8 秒 × 6 轮）。
        """
        tag = TAGS[kind]
        n = self.counts()[kind]
        names = []
        for i in range(1, n + 1):
            self.link.drain(quiet=0.05)
            self.link.send(("*?INSTALLATION CURRENT %s=%d" % (tag, i)).encode("ascii") + CR_B)
            c = None
            try:
                c = self.link.wait_ctrl((ACK, NAK), timeout=2.0)
            except FileIOError as e:
                # 有的固件不发控制字节、直接给响应行 → 交给下面读行
                self._log("取表名前没等到控制字节（%s），改为直接读行" % e)
            if c == NAK:
                raise FileIOError("设备拒绝了「按序号取表名」（NAK）：%s=%d"
                                  % (tag, i))
            line = self._read_resp_line(tries=3, timeout=4.0)
            kv = parse_kv(line)
            name = kv.get(tag) or kv.get("NAME") or ""
            name = name.strip()
            if name:
                names.append(name)
        return names

    # ------------------------------------------------------------ 下载
    def download(self, kind, name, dest, block=None):
        """把设备上的一张表存到 dest。返回 (dest, 字节数, crc 算法名)

        若发现数据被 telnet 转义过（0xFF → 0xFF 0xFF），会**重建会话**后用
        解转义模式再下一遍 —— 一份文件里不可能混着两种编码，所以只可能整体重来。
        """
        try:
            return self._download_once(kind, name, dest, block)
        except _NeedEscMode:
            self._log("首次下载判定为 IAC 转义，重连后改用解转义模式重试")
            self.conn.reconnect()
            self.link = RawLink(self.conn, verbose=self.verbose)
            self.link.iac_mode = "esc"
            self.crc_name = None
            return self._download_once(kind, name, dest, block)

    def _download_once(self, kind, name, dest, block=None):
        block = block or self.block
        tag = TAGS[kind]
        self.link.drain()
        cmd = "*?INSTALLATION CURRENT %s=%s BLOCK=%d" % (tag, name, block)
        self._log("下载 %s" % cmd)
        self.link.send(cmd.encode("latin-1") + CR_B)

        c = self.link.wait_ctrl((ACK, NAK), timeout=10.0)
        if c == NAK:
            # 分不清"没这张表"和"不认这条命令"（31.17 对带 TER-CH= 的一律 NAK），
            # 两种可能都写出来，让上层/用户自己判断
            raise FileIOError(
                "设备 NAK：%s-CH=%s 下载被拒（可能没这张表，"
                "也可能这台固件不认单表文件命令）" % (kind, name))

        line = self._read_resp_line()
        kv = parse_kv(line)
        try:
            size = int(kv.get("SIZE", "0"))
        except ValueError:
            size = 0
        if size <= 0 or size > MAX_FILE:
            raise FileIOError("设备返回的文件大小不合理：%r（原始行 %r）"
                              % (kv.get("SIZE"), line))
        self._log("文件 %s 共 %d 字节，块 %d" % (name, size, block))

        self.link.send(ACK_B)
        data = self._recv_blocks(size, block, tag="下载 " + name)
        ensure_dir(dest)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return dest, len(data), self.crc_name

    def _recv_blocks(self, size, block, tag=""):
        """按块收满 size 字节；每块校验 CRC 并回 ACK"""
        out = bytearray()
        while len(out) < size:
            want = min(block, size - len(out))
            # 设备常在块前发 XOFF、块后发 XON：跳掉控制字节再读数据
            self.link.skip_ctrl(timeout=15.0)
            chunk = self.link.take(want, timeout=30.0)
            crc = self.link.take(1, timeout=10.0)[0]
            self._check_or_learn_crc(chunk, crc)
            out.extend(chunk)
            self.link.send(ACK_B)
            self._tick(len(out), size, tag)
        return bytes(out)

    def _check_or_learn_crc(self, chunk, want):
        """校验一块；第一块顺便把 CRC 算法定下来"""
        if self.crc_name:
            got = crc_of(self.crc_name, chunk)
            if got != want:
                raise FileIOError("CRC 校验失败（%s）：算得 0x%02X，设备给 0x%02X"
                                  % (self.crc_name, got, want))
            return
        tries = [(n, fn(chunk)) for n, fn in CRC_CANDIDATES]
        self.last_crc_tries = tries
        hit = [n for n, v in tries if v == want]
        if hit:
            self.crc_name = hit[0]
            self._log("CRC 算法判定为 %s（设备 0x%02X）" % (self.crc_name, want))
            return
        # 都对不上：若数据里有 0xFF，八成是 telnet 转义 —— 换个模式整份重来
        if self.link.iac_mode == "raw" and 0xFF in chunk:
            raise _NeedEscMode()
        raise FileIOError("CRC 全不匹配（设备 0x%02X，候选 %s）"
                          % (want, ["%s=0x%02X" % t for t in tries]))

    # ------------------------------------------------------------ 上传
    def upload(self, path, kind=None, name=None, block=None):
        """把本地表文件推到设备。返回设备上的表名"""
        if not os.path.isfile(path):
            raise FileIOError("文件不存在：%s" % path)
        kind = kind or ("SAT" if os.path.basename(
            os.path.dirname(os.path.abspath(path))).upper().startswith("SAT")
            else "TER")
        name = (name or os.path.splitext(os.path.basename(path))[0]).strip()
        if not name:
            raise FileIOError("上传的表名不能为空")
        if not self.crc_name:
            # 🔴 不知道 CRC 算法就别上传（乱发会让设备写进坏文件）
            raise FileIOError("还不知道 CRC 算法：请先下载一张表做校准")
        data = open(path, "rb").read()
        if not data:
            raise FileIOError("文件是空的：%s" % path)
        block = block or self.block
        tag = TAGS[kind]

        self.link.drain()
        cmd = "*INSTALLATION PC FILE TAG=%s NAME=%s SIZE=%d BLOCK=%d" % (
            tag, name, len(data), block)
        self._log("上传 %s（%d 字节）" % (cmd, len(data)))
        self.link.send(cmd.encode("latin-1") + CR_B)
        c = self.link.wait_ctrl((ACK, NAK), timeout=10.0)
        if c == NAK:
            raise FileIOError("设备拒绝接收（NAK）：%s" % name)

        sent = 0
        while sent < len(data):
            chunk = data[sent:sent + block]
            self.link.send(chunk + bytes([crc_of(self.crc_name, chunk)]))
            self.link.wait_ctrl((ACK, NAK), timeout=20.0)
            sent += len(chunk)
            self._tick(sent, len(data), "上传 " + name)
        self._log("上传完成：%s（%d 字节）" % (name, len(data)))
        return name

    # ------------------------------------------------------------ 删除
    def remove(self, kind, name):
        """删掉设备上的一张表"""
        tag = TAGS[kind]
        self.link.drain()
        cmd = "*INSTALLATION CURRENT REMOVE %s=%s" % (tag, name)
        self._log("删除 %s" % cmd)
        self.link.send(cmd.encode("latin-1") + CR_B)
        c = self.link.wait_ctrl((ACK, NAK), timeout=10.0)
        if c == NAK:
            raise FileIOError("设备拒绝删除（NAK）：%s" % name)
        return True

    # ------------------------------------------------------------ 整装
    def download_zip(self, dest):
        """整装下载：`*?INSTALLATION CURRENT ZIP BLOCK=` → 存成 zip"""
        self.link.drain()
        self.link.send(("*?INSTALLATION CURRENT ZIP BLOCK=%d"
                        % self.block).encode("ascii") + CR_B)
        c = self.link.wait_ctrl((ACK, NAK), timeout=10.0)
        if c == NAK:
            raise FileIOError("设备拒绝整装下载（NAK）")
        line = self._read_resp_line()
        size = int(parse_kv(line).get("SIZE", "0") or 0)
        if size <= 0 or size > MAX_FILE:
            raise FileIOError("整装包大小不合理：%r" % line)
        self.link.send(ACK_B)
        data = self._recv_blocks(size, self.block, tag="整装下载")
        ensure_dir(dest)
        with open(dest + ".tmp", "wb") as f:
            f.write(data)
        os.replace(dest + ".tmp", dest)
        return dest, len(data)

    # ------------------------------------------------------------ 杂项
    def _read_resp_line(self, tries=6, timeout=8.0):
        """读一条响应行；收到的若不是 `*XXX` 开头就继续读（跳过回显/心跳）

        `tries`/`timeout` 可调：命令被拒时设备根本不回行，
        默认 6 轮 × 8 秒会白等 48 秒。连着两次空就认命，早点报错。
        """
        empty = 0
        for _ in range(tries):
            line = self.link.read_line(timeout=timeout)
            k = line.find("*")
            if k == 0:
                return line
            if k > 0:                       # 前面粘了别的东西，从 * 处截断
                return line[k:]
            self._log("跳过非响应行: %r" % line)
            if not line:
                empty += 1
                if empty >= 2:
                    break
        raise FileIOError("连续收到的都不是响应行（设备没应答，或这条命令不被支持）")


class _NeedEscMode(Exception):
    """内部信号：这份文件要用「解 telnet 转义」模式重收（需重建会话）"""


# ---------------------------------------------------------------- 工具
def parse_kv(line):
    """把 `*INSTALLATION NAME=x TER-CH=2` 拆成 dict（键大写，值保持原样）"""
    out = {}
    if not line:
        return out
    body = line[1:] if line.startswith("*") else line
    for item in body.split():
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.upper()] = v
    return out


def ensure_dir(path):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def download_with_retry(io, kind, name, dest, attempts=None):
    """兼容旧调用的薄包装（重试逻辑已并入 PlanIO.download）"""
    return io.download(kind, name, dest)


def zip_plan_names(zip_path):
    """看一眼整装 zip 里有哪些表（诊断用）"""
    out = {"TER": [], "SAT": []}
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            u = n.replace("\\", "/").upper()
            if u.endswith(".XML"):
                base = n.rsplit("/", 1)[-1]
                if "/SAT/" in u:
                    out["SAT"].append(base)
                elif "/TER/" in u:
                    out["TER"].append(base)
    return out
