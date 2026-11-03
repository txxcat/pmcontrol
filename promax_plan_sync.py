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
import socket
import tempfile
import time
import zipfile

import promax_fileio as F
import promax_plan_edit as E
import promax_sftp as SF
from promax_telnet_test import PromaxConnLost, PromaxError, PromaxTelnet

# 设备命令形态（按顺序尝试，成功一个就记住）
DOWNLOAD_FORMS = (
    "*?INSTALLATION CURRENT {tag}={name} BLOCK={block}",   # 手册原样
    "*?INSTALLATION {tag}={name} BLOCK={block}",           # 去掉 CURRENT
)

# 某个 (host, ssh端口) 上 SFTP 到底通不通 —— auto 模式只探一次，
# 免得每次同步都先等一遍 SSH 超时
_SFTP_OK = {}

TRANSPORTS = ("auto", "sftp", "telnet")


PlanError = E.PlanError


class SyncError(PlanError):
    """同步失败（用户可见的中文提示）"""


class PlanSync(object):
    """一台设备的频道表同步会话"""

    def __init__(self, host, port=2222, source="CH", log=None, progress=None,
                 verbose=False, timeout=15.0, transport="telnet",
                 sftp_port=None, sftp_user=None, sftp_password=None):
        self.host = host
        self.port = int(port)
        self.source = os.path.abspath(source)
        self.log = log or (lambda m: None)
        self.progress = progress
        self.verbose = verbose
        self.timeout = timeout
        self.transport = transport if transport in TRANSPORTS else "telnet"
        self.sftp_port = int(sftp_port or SF.SFTP_PORT)
        self.sftp_user = sftp_user or SF.SFTP_USER
        self.sftp_password = sftp_password or SF.SFTP_PASSWORD
        self.mode = None              # 实际走的路："sftp" / "telnet"
        self.sp = None                # PlanSFTP 会话
        self.conn = None
        self.io = None
        self.strategy = ""            # "sftp" / "single" / "zip" / ""
        self.crc_ready = False
        self._zip_cache = None        # (path, {kind: {name: arcname}})
        self._tag_ok = None           # 单表命令是否可用（None=未知）
        # 上次成功过的写法（"xml"/"plain" + 命令形状下标），下次直接用它，
        # 免得每张表都先被 NAK 一遍
        self._name_ok = None
        self._form_ok = None

    # ------------------------------------------------------------ 连接
    def _sftp_available(self):
        """auto 模式下先问一句：22 端口有没有人接（探一次，结果缓存）"""
        key = (self.host, self.sftp_port)
        if key in _SFTP_OK:
            return _SFTP_OK[key]
        ok = False
        try:
            s = socket.create_connection((self.host, self.sftp_port),
                                         timeout=2.0)
            try:
                s.settimeout(2.0)
                banner = s.recv(64)          # SSH 服务端会主动送 banner
                ok = banner.startswith(b"SSH-")
            finally:
                s.close()
        except OSError:
            ok = False
        _SFTP_OK[key] = ok
        return ok

    def _pick_mode(self):
        if self.transport == "sftp":
            return "sftp"
        if self.transport == "telnet":
            return "telnet"
        return "sftp" if self._sftp_available() else "telnet"

    def connect(self):
        if self.mode is not None:
            return self
        mode = self._pick_mode()
        if mode == "sftp":
            sp = SF.PlanSFTP(self.host, port=self.sftp_port,
                             user=self.sftp_user,
                             password=self.sftp_password,
                             log=self.log, timeout=self.timeout,
                             progress=self._tick)
            try:
                sp.connect()
                sp.discover()        # 目录结构不对就在这儿失败，好退回 telnet
            except SF.SftpError as e:
                sp.close()
                if self.transport == "sftp":
                    raise SyncError(str(e))
                self.log("SFTP 不可用（%s），退回 Telnet" % e)
                _SFTP_OK[(self.host, self.sftp_port)] = False
                mode = "telnet"
            else:
                self.sp = sp
                self.mode = "sftp"
                self.strategy = "sftp"
                self.crc_ready = True         # SFTP 不需要 CRC 校准
                self.log("传输方式：SFTP（%s:%d）"
                         % (self.host, self.sftp_port))
                return self
        self.mode = "telnet"
        self.conn = PromaxTelnet(self.host, self.port, timeout=self.timeout,
                                 verbose=self.verbose)
        self.conn.connect()
        self.io = F.PlanIO(self.conn, verbose=self.verbose,
                           progress=self._tick)
        self.log("已连接 %s:%d（Telnet）" % (self.host, self.port))
        return self

    def reconnect(self):
        if self.mode == "sftp":
            try:
                if self.sp is not None:
                    self.sp.close()
            except Exception:
                pass
            self.sp = None
            self.mode = None
            return self.connect()
        if self.conn is None:
            return self.connect()
        self.conn.reconnect()
        self.io = F.PlanIO(self.conn, verbose=self.verbose,
                           progress=self._tick)
        self._zip_cache = None
        return self

    def close(self):
        self._drop_zip_cache()
        if self.sp is not None:
            try:
                self.sp.close()
            except Exception:
                pass
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.sp = None
        self.conn = None
        self.io = None
        self.mode = None

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
        if self.mode == "sftp":
            return self.sp.device_info()
        return self.io.counts()

    def device_plans(self, kind):
        """设备上某频段的表名列表。

        SFTP：直接列目录，一次就准。
        Telnet：优先"按序号取名"（单表路线）；不认就退回整装 ZIP 里读文件名。
        """
        self.connect()
        if self.mode == "sftp":
            self.strategy = "sftp"
            return self.sp.device_plans(kind)
        if self._tag_ok is not False:
            try:
                names = self.io.plan_names(kind)
                if names:
                    self._tag_ok = True
                    if not self.strategy:
                        self.strategy = "single"
                    return names
                self._tag_ok = False
            except F.PromaxSilent:
                # 设备一个字节都不回：整装 ZIP 也一定同样失败，
                # 而且每试一次就要重连一次，会把单会话设备打得更懵 → 直接上抛
                raise
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
        except F.PromaxSilent:
            # 设备不应答的原话最有用（含"怎么恢复"），别包进"整装也失败了"里
            raise
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
        if self.mode == "sftp":
            self.strategy = "sftp"
            return self.sp.download(kind, name, dest, overwrite=overwrite)
        if self._tag_ok is not False:
            try:
                self._download_single(kind, name, dest)
                self._tag_ok = True
                self.strategy = "single"
                return dest
            except F.PromaxSilent:
                raise                    # 设备不应答 → 整装也一样，别白试
            except (PromaxError, F.FileIOError) as e:
                self.log("单表下载失败（%s），改用整装 ZIP" % e)
                self._tag_ok = False
        return self._download_from_zip(kind, name, dest)

    def _download_single(self, kind, name, dest):
        tag = F.TAGS[kind]
        last = None
        # 表名写法（带不带 .XML）× 命令形状，按"上次成功的"排前面。
        # 🔴 **被 NAK 不重连**：真机实测 NAK 的含义就是"这个名字/这条命令不对"
        # （带 .XML 才认），不是时序错乱。以前每次 NAK 都 reconnect，
        # 单会话设备被连打十几次 —— 现场表现是"仪器越来越不响应"。
        cands = F.name_variants(name)
        if self._name_ok:                      # "xml" / "plain"
            hit = [c for c in cands
                   if c.upper().endswith(".XML") == (self._name_ok == "xml")]
            cands = hit + [c for c in cands if c not in hit]
        forms = list(DOWNLOAD_FORMS)
        if self._form_ok is not None and self._form_ok < len(forms):
            f = forms.pop(self._form_ok)
            forms.insert(0, f)

        for cand in cands:
            for form in forms:
                cmd = form.format(tag=tag, name=cand, block=self.io.block)
                try:
                    got = self._download_by_cmd(cmd, dest)
                except PromaxConnLost:
                    raise                      # 断线得让上层挂起重连，不是换写法
                except F.PromaxSilent:
                    raise                      # 零字节 = 仪器不应答，换写法也一样
                except (PromaxError, F.FileIOError) as e:
                    last = e
                    self.log("命令不认：%s（%s）" % (cmd, e))
                    continue
                self._name_ok = "xml" if cand.upper().endswith(".XML") else "plain"
                self._form_ok = DOWNLOAD_FORMS.index(form)
                return got

        # 所有写法都没成：别写死"NAK"—— 实际原因可能是没拿到 SIZE、CRC 不符等
        raise SyncError(
            "下载「%s」失败：表名写法 × 命令形态都试过了。\n"
            "常见原因：① 表名要跟设备列出来的一字不差（大小写敏感，可能带 .XML）；"
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
        # 🔴 这里必须用 _read_size_line，**不能**用 _read_resp_line：
        # 后者要等 CR 才收工，而这台仪器的 SIZE 应答结尾是 XON、没有 CR
        # → 白等 8 秒，设备那头 5 秒等不到 ACK 就 NAK 中止，数据一个字节都收不到。
        line = io._read_size_line()
        kv = F.parse_kv(line)
        if "SIZE" not in kv:
            up = line.upper()
            if "NUMBER=" in up:
                raise F.FileIOError(
                    "这条命令被设备当成普通查询了（回了 %r）—— 写法不对" % line)
            raise F.FileIOError("没拿到 SIZE（设备回的是 %r）" % line)
        size = int(kv.get("SIZE", "0") or 0)
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
        table = idx.get(kind, {})
        # 表名带不带 .XML 都可能：整装包里的键是不带扩展名的
        arc = table.get(name) or table.get(F.plain_name(name))
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
        """上传前必须先知道 CRC 算法：随便下一张最小的表来反推

        SFTP 模式不需要 —— 那是分块协议才有的东西，直接放行。
        """
        self.connect()
        if self.mode == "sftp":
            self.crc_ready = True
            return "sftp"
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
        kind = kind or E.kind_of_path(path)
        # 🔴 **不要在这里剥掉 .XML**：设备只认带扩展名的频道表
        #    （真机实测：传不带扩展名的名字，设备会原样存成一个普通文件，
        #      表清单里根本不认它）。补扩展名的规则交给传输层统一决定。
        name = (name or os.path.basename(path)).strip()
        if self.mode == "sftp":
            self.strategy = "sftp"
            try:
                got = self.sp.upload(path, kind=kind, name=name)
            except SF.SftpError as e:
                raise SyncError("上传失败：%s" % e)
            self.log("SFTP 上传完成：%s" % name)
            return got
        if self._tag_ok is False:
            raise SyncError(
                "这台固件不接受单表文件命令（实测 31.17 对带 TER-CH= 的命令回 NAK），"
                "整装 ZIP 上传会整体替换设备安装包，暂不自动执行。\n"
                "建议改用 SFTP（设置里把传输方式改成 SFTP）。")
        self.calibrate_crc()
        # 上传前先记下设备上有什么 —— 上传后靠差集报出**设备实际存的名字**
        before = set()
        try:
            before = set(self.device_plans(kind))
        except (PromaxError, F.FileIOError):
            pass
        try:
            got = self.io.upload(path, kind=kind, name=name)
        except (PromaxError, F.FileIOError) as e:
            raise SyncError("上传失败：%s" % e)
        # 🔴 设备遇到重名**不会覆盖**，而是自动改名（真机实测：传 DEMO-X →
        #    仪器里出现的是 DEMO-X_2）。所以要回读一遍清单，把设备上的
        #    真名告诉用户，别让他以为覆盖成功了。
        try:
            after = set(self.device_plans(kind))
            added = sorted(after - before)
        except (PromaxError, F.FileIOError):
            added = []
        if added:
            real = added[0]
            if not real.upper().endswith(".XML"):
                real += ".XML"
            self.log("设备上实际存为：%s" % real)
            got = real
        self._tag_ok = True
        self.strategy = "single"
        return got

    def remove(self, kind, name):
        """删设备上的一张表（不可撤销 —— 调用方必须先确认）"""
        self.connect()
        if self.mode == "sftp":
            try:
                return self.sp.remove(kind, name)
            except SF.SftpError as e:
                raise SyncError("删除失败：%s" % e)
        if self._tag_ok is False:
            raise SyncError("这台固件不接受单表删除命令")
        try:
            return self.io.remove(kind, name)
        except (PromaxError, F.FileIOError) as e:
            raise SyncError("删除失败：%s" % e)

    # ------------------------------------------------------------ 整装
    def download_installation(self, dest_zip=None):
        """整装下载（备份/换机迁移用）。返回 zip 路径

        Telnet：走仪器的整装 ZIP 命令，拿到的是完整安装包。
        SFTP：仪器没有"打包"这回事，只能自己把 CH 目录（频道表）打下来，
              结构仍是 CH/TER|SAT，导入端不用改。
        """
        self.connect()
        dest_zip = dest_zip or os.path.join(
            self.source, "installation-%s.zip"
            % time.strftime("%Y%m%d-%H%M%S"))
        if self.mode == "sftp":
            return self._download_ch_zip(dest_zip)
        self.io.download_zip(dest_zip)
        return dest_zip

    def _download_ch_zip(self, dest_zip):
        """把仪器上的 CH 目录整个拉下来打成 zip（保持相对结构）"""
        import posixpath
        sp = self.sp
        sp.discover()
        ch_root = posixpath.join(sp.root, "CH")
        dirs = [ch_root]
        n = 0
        F.ensure_dir(dest_zip)
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
            while dirs:
                d = dirs.pop()
                for a in sp._ls(d):
                    full = posixpath.join(d, a.filename)
                    if sp._isdir(a):
                        dirs.append(full)
                        continue
                    if not a.filename.upper().endswith(E.PLAN_EXT.upper()):
                        continue          # 只收频道表，截图/日志不收
                    tmp = tempfile.mkstemp(suffix=".xml")[1]
                    try:
                        sp.sftp.get(full, tmp)
                        arc = posixpath.relpath(full, sp.root)
                        z.write(tmp, arc)
                        n += 1
                    finally:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
        self.log("SFTP 整装（仅频道表）完成：%d 个文件 → %s"
                 % (n, os.path.basename(dest_zip)))
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
