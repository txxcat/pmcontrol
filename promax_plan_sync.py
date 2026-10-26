# -*- coding: utf-8 -*-
"""频道表同步策略层：把 PlanIO 的协议细节包装成"下载/上传/列举"三个动作。

存在的理由
----------
手册（2017 v2.2）写的单表文件命令，在 31.17 固件上**未必还在**：
`*?INSTALLATION` 只回 `NUMBER=nn`、`*?INSTALLATION CURRENT TER-CH=…` 直接 NAK
（这两条是实测过的）。所以同步不能只押一条路：

    主路：单表下载/上传（快，只动一张）
    兜底：整装 ZIP（一次拿全、结构最保真；下载路径天然兼容老固件）

本模块对外只暴露"我想下这张表 / 我想上传这张表"，由它自己决定走哪条路，
并把走过哪条路记在 `self.strategy` 里（界面显示用）。

🔴 术语
    kind    = "TER"（射频）/ "SAT"（卫星）
    name    = 表名（= 文件名去掉 .XML），大小写敏感
    source  = 频道表根目录（其下有 TER/ SAT/）

用法
----
    sync = PlanSync("192.168.x.x", 2222, source="CH")
    sync.connect()
    print(sync.device_plans("TER"))
    sync.download("TER", "某张表")
    sync.upload("CH/TER/某张表.XML")
    sync.close()
"""
import os
import tempfile
import time
import zipfile

import promax_fileio as F
import promax_plan_edit as E
from promax_telnet_test import PromaxConnLost, PromaxError, PromaxTelnet

# 设备命令形态（按顺序尝试，成功一个就记住）
DOWNLOAD_FORMS = (
    "*?INSTALLATION CURRENT {tag}={name} BLOCK={block}",   # 手册原样
    "*?INSTALLATION {tag}={name} BLOCK={block}",           # 去掉 CURRENT
)


PlanError = E.PlanError


class SyncError(PlanError):
    """同步失败（用户可见的中文提示）"""


class PlanSync(object):
    """一台设备的频道表同步会话"""

    def __init__(self, host, port=2222, source="CH", log=None, progress=None,
                 verbose=False, timeout=15.0):
        self.host = host
        self.port = int(port)
        self.source = os.path.abspath(source)
        self.log = log or (lambda m: None)
        self.progress = progress
        self.verbose = verbose
        self.timeout = timeout
        self.conn = None
        self.io = None
        self.strategy = ""            # "single" / "zip" / ""
        self.crc_ready = False
        self._zip_cache = None        # (path, {kind: {name: arcname}})
        self._tag_ok = None           # 单表命令是否可用（None=未知）

    # ------------------------------------------------------------ 连接
    def connect(self):
        if self.conn is not None:
            return self
        self.conn = PromaxTelnet(self.host, self.port, timeout=self.timeout,
                                 verbose=self.verbose)
        self.conn.connect()
        self.io = F.PlanIO(self.conn, verbose=self.verbose,
                           progress=self._tick)
        self.log("已连接 %s:%d" % (self.host, self.port))
        return self

    def reconnect(self):
        if self.conn is None:
            return self.connect()
        self.conn.reconnect()
        self.io = F.PlanIO(self.conn, verbose=self.verbose,
                           progress=self._tick)
        self._zip_cache = None
        return self

    def close(self):
        self._drop_zip_cache()
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None
        self.io = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def _tick(self, done, total, text=""):
        if self.progress:
            try:
                self.progress(done, total, text)
            except Exception:
                pass

    # ------------------------------------------------------------ 列举
    def device_info(self):
        """设备概况：{'TER': n, 'SAT': n, 'name': 安装名}"""
        self.connect()
        return self.io.counts()

    def device_plans(self, kind):
        """设备上某频段的表名列表。

        优先"按序号取名"（单表路线）；不认就退回整装 ZIP 里读文件名。
        """
        self.connect()
        if self._tag_ok is not False:
            try:
                names = self.io.plan_names(kind)
                if names:
                    self._tag_ok = True
                    if not self.strategy:
                        self.strategy = "single"
                    return names
                self._tag_ok = False
            except (PromaxError, F.FileIOError) as e:
                self.log("按序号取名失败（%s），改用整装 ZIP 列举" % e)
                self._tag_ok = False
        idx = self._ensure_zip()
        return sorted(idx.get(kind, {}))

    def _ensure_zip(self):
        """下整装包并建索引；失败抛 SyncError"""
        self.connect()
        if self._zip_cache and os.path.isfile(self._zip_cache[0]):
            return self._zip_cache[1]
        tmpdir = tempfile.mkdtemp(prefix="pm_sync_")
        zp = os.path.join(tmpdir, "installation.zip")
        self.log("改用整装 ZIP 路线：正在下载当前安装包…")
        try:
            self.io.download_zip(zp)
        except (PromaxError, F.FileIOError) as e:
            raise SyncError("整装下载也失败了：%s\n（这台固件可能两种文件命令都改过，"
                            "请把 _probe_io.py 的输出发我）" % e)
        idx = {"TER": {}, "SAT": {}}
        with zipfile.ZipFile(zp) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                arc = info.filename.replace("\\", "/")
                up = arc.upper()
                if not up.endswith(E.PLAN_EXT.upper()):
                    continue
                if "/SAT/" in up or up.startswith("SAT/"):
                    kind = "SAT"
                elif "/TER/" in up or up.startswith("TER/"):
                    kind = "TER"
                else:
                    continue
                name = os.path.splitext(arc.rsplit("/", 1)[-1])[0].strip()
                idx[kind][name] = arc
        self._zip_cache = (zp, idx)
        self.strategy = self.strategy or "zip"
        self.log("整装包里有：射频 %d 张 / 卫星 %d 张"
                 % (len(idx["TER"]), len(idx["SAT"])))
        return idx

    def _drop_zip_cache(self):
        if self._zip_cache:
            d = os.path.dirname(self._zip_cache[0])
            try:
                import shutil
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        self._zip_cache = None

    # ------------------------------------------------------------ 下载
    def download(self, kind, name, dest=None, overwrite=True):
        """把设备上的一张表存到本地。返回本地路径。

        dest 默认 = source/TER|SAT/<name>.XML；目标已存在且 overwrite=False 时抛错。
        """
        self.connect()
        dest = dest or E.plan_path(self.source, kind, name)
        if os.path.exists(dest) and not overwrite:
            raise SyncError("本地已有同名表：%s" % os.path.basename(dest))
        if self._tag_ok is not False:
            try:
                self._download_single(kind, name, dest)
                self._tag_ok = True
                self.strategy = "single"
                return dest
            except (PromaxError, F.FileIOError) as e:
                self.log("单表下载失败（%s），改用整装 ZIP" % e)
                self._tag_ok = False
        return self._download_from_zip(kind, name, dest)

    def _download_single(self, kind, name, dest):
        tag = F.TAGS[kind]
        last = None
        for form in DOWNLOAD_FORMS:
            try:
                cmd = form.format(tag=tag, name=name, block=self.io.block)
                return self._download_by_cmd(cmd, dest)
            except (PromaxError, F.FileIOError) as e:
                last = e
                self.log("命令不认：%s（%s）" % (cmd, e))
                try:
                    self.reconnect()          # 命令被拒后重新对齐时序
                except PromaxError:
                    raise
        # 两种命令形状都被 NAK：分不清是"没这张表"还是"不认单表命令"
        raise SyncError(
            "设备拒绝了「%s」的下载请求（NAK）。\n"
            "可能原因：① 表名不对（设备上的名字大小写敏感）；"
            "② 这台固件不认单表文件命令。\n"
            "最后一条命令：%s\n设备原话：%s"
            % (name, DOWNLOAD_FORMS[-1].format(tag=tag, name=name,
                                               block=self.io.block), last))

    def _download_by_cmd(self, cmd, dest):
        """用给定命令走一遍分块下载（PlanIO 只认它自己的命令形状，这里绕开它）"""
        io = self.io
        io.link.drain()
        self.log("下载 %s" % cmd)
        io.link.send(cmd.encode("latin-1") + bytes([13]))
        c = io.link.wait_ctrl((F.ACK, F.NAK), timeout=10.0)
        if c == F.NAK:
            raise F.FileIOError("设备 NAK")
        line = io._read_resp_line()
        size = int(F.parse_kv(line).get("SIZE", "0") or 0)
        if size <= 0 or size > F.MAX_FILE:
            raise F.FileIOError("SIZE 不合理：%r" % line)
        io.link.send(F.ACK_B)
        data = io._recv_blocks(size, io.block, tag="下载")
        F.ensure_dir(dest)
        with open(dest + ".tmp", "wb") as f:
            f.write(data)
        os.replace(dest + ".tmp", dest)
        self.crc_ready = True
        return dest

    def _download_from_zip(self, kind, name, dest):
        idx = self._ensure_zip()
        arc = idx.get(kind, {}).get(name)
        if not arc:
            have = "、".join(sorted(idx.get(kind, {}))) or "（空）"
            raise SyncError("设备上没有这张表：%s\n设备现有：%s" % (name, have))
        zp = self._zip_cache[0]
        F.ensure_dir(dest)
        with zipfile.ZipFile(zp) as z, z.open(arc) as src, \
                open(dest + ".tmp", "wb") as out:
            out.write(src.read())
        os.replace(dest + ".tmp", dest)
        self.log("从整装包取出 %s → %s" % (arc, os.path.basename(dest)))
        return dest

    # ------------------------------------------------------------ 上传
    def calibrate_crc(self):
        """上传前必须先知道 CRC 算法：随便下一张最小的表来反推"""
        self.connect()
        if self.io.crc_name:
            self.crc_ready = True
            return self.io.crc_name
        info = self.io.counts()
        for kind in ("TER", "SAT"):
            if info.get(kind):
                names = self.device_plans(kind)
                if not names:
                    continue
                tmp = os.path.join(tempfile.mkdtemp(prefix="pm_cal_"), "cal.XML")
                try:
                    self.download(kind, names[0], tmp, overwrite=True)
                except SyncError as e:
                    self.log("校准下载失败：%s" % e)
                    continue
                if self.io.crc_name:
                    self.crc_ready = True
                    self.log("CRC 算法校准完成：%s" % self.io.crc_name)
                    return self.io.crc_name
        raise SyncError("无法校准 CRC：设备上没有可下载的表，或下载命令都不通")

    def upload(self, path, kind=None, name=None):
        """上传一张本地表到设备。返回设备上的表名。

        🔴 只走单表上传。整装 ZIP 上传会**整体替换**设备上的安装包，
           风险太大，不自动降级 —— 需要时由用户显式选择（后续版本）。
        """
        self.connect()
        if self._tag_ok is False:
            raise SyncError(
                "这台固件不接受单表文件命令（实测 31.17 对带 TER-CH= 的命令回 NAK），"
                "整装 ZIP 上传会整体替换设备安装包，暂不自动执行。\n"
                "请先跑 _probe_io.py 把结果发我，再决定用哪条路。")
        kind = kind or E.kind_of_path(path)
        name = (name or os.path.splitext(os.path.basename(path))[0]).strip()
        self.calibrate_crc()
        try:
            got = self.io.upload(path, kind=kind, name=name)
            self._tag_ok = True
            self.strategy = "single"
            return got
        except (PromaxError, F.FileIOError) as e:
            raise SyncError("上传失败：%s" % e)

    def remove(self, kind, name):
        """删设备上的一张表（不可撤销 —— 调用方必须先确认）"""
        self.connect()
        if self._tag_ok is False:
            raise SyncError("这台固件不接受单表删除命令")
        try:
            return self.io.remove(kind, name)
        except (PromaxError, F.FileIOError) as e:
            raise SyncError("删除失败：%s" % e)

    # ------------------------------------------------------------ 整装
    def download_installation(self, dest_zip=None):
        """整装下载（备份/换机迁移用）。返回 zip 路径"""
        self.connect()
        dest_zip = dest_zip or os.path.join(
            self.source, "installation-%s.zip"
            % time.strftime("%Y%m%d-%H%M%S"))
        self.io.download_zip(dest_zip)
        return dest_zip

    def import_installation(self, zip_path, overwrite=False):
        """把整装包里的表导入本地 source（按 TER/SAT 分开落盘）"""
        res = {"written": [], "skipped": []}
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                arc = info.filename.replace("\\", "/")
                up = arc.upper()
                if not up.endswith(E.PLAN_EXT.upper()):
                    continue
                if "/SAT/" in up:
                    kind = "SAT"
                elif "/TER/" in up:
                    kind = "TER"
                else:
                    continue
                name = os.path.splitext(arc.rsplit("/", 1)[-1])[0].strip()
                dest = E.plan_path(self.source, kind, name)
                if os.path.exists(dest) and not overwrite:
                    res["skipped"].append(name)
                    continue
                F.ensure_dir(dest)
                with z.open(info) as src, open(dest + ".tmp", "wb") as out:
                    out.write(src.read())
                os.replace(dest + ".tmp", dest)
                res["written"].append(name)
        return res
