# -*- coding: utf-8 -*-
"""频道表文件传输：SFTP 路线（走仪器自带的 SSH 服务，端口 22）。

为什么加这条路
--------------
Telnet（2222）那条路传文件要靠「分块 + 1 字节 CRC + 逐块 ACK」，
而 CRC 算法手册根本没写，只能从真机数据反推 —— 反推不出来就**不能上传**。
RANGER Neo 4 手册（F9.0）第 11.3.4 节写明了仪器自带 SFTP 服务：

    User: Promax    Password: Monitoring2019    Port: 22

而频道表就是仪器上的普通文件，手册 10.10.2 第 7 步写得很直白：
「进到安装目录的 CH 文件夹，按射频/卫星选 TER 或 SAT，把表放进去」。
也就是说 —— **传一张表不需要懂分块协议，只需要能读写一个 XML 文件**。

⚠️ 前提
    SFTP 是较新固件才有的（手册 "NEW" 栏里明确标为新增特性）。
    老固件可能没开 22 端口。所以调用方要能接受「SFTP 不可用」并退回 telnet。
    另外账号密码是**手册里的出厂默认值**，现场可能改过 —— 允许外部覆盖。

🔴 paramiko 必须在**模块顶层**导入
    PyInstaller 只做静态分析，写在函数里 `import paramiko` 它看不见，
    打包出来的 exe 就会在 connect() 时 ImportError。这里顶层 try/except，
    既能被分析到，又保证没装 paramiko 时程序其余部分照常跑。

🔴 路径不猜
    仪器上的安装目录名随固件/语言版本变（INSTALACIONES / INSTALL / DATA…），
    写死必然错。这里用 `discover()` 在文件系统里找「名为 CH、下面有 TER/SAT
    子目录」的目录，找到哪个用哪个，并把候选都记下来（日志里能看到）。

用法
----
    s = PlanSFTP("192.168.x.x")
    s.connect()
    print(s.device_plans("TER"))
    s.download("TER", "某张表", "CH/TER/某张表.XML")
    s.upload("CH/TER/某张表.XML")
    s.close()

    # 只做体检（不传文件）：
    python -c "import promax_sftp,sys; promax_sftp.probe(sys.argv[1])" 192.168.31.183

只依赖 paramiko（+ 本仓库的 promax_plan_edit 取路径约定）。
paramiko 缺失时不会在 import 阶段炸 —— 报错推迟到 connect()，
这样打包漏了依赖也只是「SFTP 不可用」，不影响程序其余部分。
"""
import os
import posixpath
import stat

import promax_plan_edit as E

# 顶层导入：PyInstaller 静态分析只认模块级 import（写进函数里会被漏掉，
# 打包后表现为 connect() 时 ImportError）。没装也不影响程序其余部分。
try:
    import paramiko
except Exception:                                    # pragma: no cover
    paramiko = None

PlanError = E.PlanError

# 手册 11.3.4 给的出厂默认值（现场可能改过，所以允许覆盖）
SFTP_USER = "Promax"
SFTP_PASSWORD = "Monitoring2019"
SFTP_PORT = 22

# 找安装目录时跳过的目录：WWW 里有几万个前端文件，进去就是几分钟
SKIP_NAMES = {
    "proc", "sys", "dev", "run", "tmp", "tmpfs", "www", "assets",
    "node_modules", "lost+found", "lib", "lib64", "bin", "sbin",
    "include", "share", "fonts", "icons", "images", "sounds",
}

CH_DIR = "CH"
BAND_DIRS = {"TER", "SAT", "AUX"}


def ensure_dir(path):
    """确保文件所在目录存在（本地路径）"""
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    return path


def _have_paramiko():
    return paramiko is not None


class SftpError(PlanError):
    """SFTP 传输失败（用户可见的中文提示）"""


class PlanSFTP(object):
    """一台设备的频道表 SFTP 会话

    参数
    ----
    host      仪器 IP
    port      SSH 端口，默认 22
    user      默认取手册的 Promax
    password  默认取手册的 Monitoring2019
    root      已知的安装目录（给了就不做 discover，快很多）
    """

    def __init__(self, host, port=SFTP_PORT, user=SFTP_USER,
                 password=SFTP_PASSWORD, root=None, log=None,
                 timeout=15.0, progress=None):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.root = root
        self.log = log or (lambda m: None)
        self.timeout = timeout
        self.progress = progress
        self.client = None
        self.sftp = None
        self.installs = []            # [(名称, CH 的绝对路径)]
        self._ch = {}                 # {"TER": "/…/CH/TER", …}

    # ------------------------------------------------------------ 连接
    def _banner(self):
        """先裸连读一行 SSH banner —— 连不上时这是最有用的信息

        仪器用的是嵌入式 SSH（dropbear / 老 openssh 都可能），算法套件
        往往很旧。真出问题时，`SSH-2.0-xxx` 这行能直接说出对方是什么实现，
        比"认证失败"四个字值钱得多。
        """
        import socket
        s = None
        try:
            s = socket.create_connection((self.host, self.port),
                                         timeout=min(4.0, self.timeout))
            s.settimeout(min(4.0, self.timeout))
            buf = s.recv(128)
            line = buf.split(b"\n")[0].strip().decode("latin-1", "replace")
            return line if line.startswith("SSH-") else ""
        except OSError:
            return ""
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    def connect(self):
        if self.sftp is not None:
            return self
        if not _have_paramiko():
            raise SftpError(
                "缺少 paramiko，SFTP 路线不可用。\n"
                "装一下就好：pip install paramiko")
        c = paramiko.SSHClient()
        # 仪器是局域网设备、密钥会随固件升级变 —— 记不记都无意义，
        # 但必须放行，否则第一次连接直接 UnknownHostKey
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(self.host, port=self.port, username=self.user,
                      password=self.password,
                      timeout=self.timeout, banner_timeout=self.timeout,
                      auth_timeout=self.timeout,
                      look_for_keys=False, allow_agent=False)
        except Exception as e:
            c.close()
            banner = self._banner()
            raise SftpError(
                "SFTP 连不上 %s:%d（%s）。\n"
                "① 这台固件可能没有 SFTP（较新固件才有）\n"
                "② 账号密码可能被改过（手册默认 %s / %s）\n"
                "③ 先跑 _probe_ports.py 看 22 端口开没开\n"
                "对方 SSH：%s"
                % (self.host, self.port, e, SFTP_USER, SFTP_PASSWORD,
                   banner or "（没读到 banner，22 端口可能根本没开）"))
        self.client = c
        self.sftp = c.open_sftp()
        self.sftp.get_channel().settimeout(max(10.0, self.timeout))
        try:
            ver = c.get_transport().remote_version
        except Exception:
            ver = ""
        self.log("SFTP 已连接 %s:%d（%s，%s）"
                 % (self.host, self.port, self.user, ver or "SSH"))
        return self

    def close(self):
        for obj in (self.sftp, self.client):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self.sftp = None
        self.client = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 路径发现
    def _ls(self, path):
        try:
            return self.sftp.listdir_attr(path)
        except Exception:
            return []

    def _isdir(self, attr):
        return stat.S_ISDIR(attr.st_mode or 0)

    def discover(self):
        """找「名为 CH、下面有 TER/SAT/AUX 子目录」的目录

        仪器上的安装目录名随固件/语言变，写死必错 → 广度优先找。
        WWW 之类的噪音目录直接跳过，否则要爬几万个文件。
        """
        if self._ch:
            return self._ch
        self.connect()
        if self.root:
            self._adopt(self.root)
            if self._ch:
                return self._ch

        # 先试几个最像的根，命中就不爬全盘
        quick = ["/USR/VAR/INSTALACIONES", "/USR/VAR/INSTALLATIONS",
                 "/USR/VAR/INSTALL", "/USR/DATA/INSTALACIONES",
                 "/USR/DATA/INSTALLATIONS", "/USR/DATA", "/USR/VAR",
                 "/VAR/INSTALACIONES", "/DATA", "/USR"]
        for q in quick:
            if self._adopt(q):
                if self.log:
                    self.log("安装目录：%s" % q)
                return self._ch

        found = self._bfs(["/"])
        if not found:
            raise SftpError(
                "在仪器上没找到频道表目录（CH/TER 或 CH/SAT）。\n"
                "可能是这台固件的目录结构不同 —— 请把下面这段发我：\n%s"
                % self._tree_hint())
        return self._ch

    def _adopt(self, path):
        """把一个候选目录当成安装目录看：<path>/CH/TER|SAT 存在就采用"""
        ch = posixpath.join(path, CH_DIR)
        subs = {a.filename.upper(): a.filename for a in self._ls(ch)
                if self._isdir(a)}
        hit = {k: posixpath.join(ch, subs[k]) for k in BAND_DIRS
               if k in subs}
        if not hit:
            return False
        self._ch = hit
        self.root = path
        self.installs.append((posixpath.basename(path), ch))
        self.log("找到频道表目录：%s" % ch)
        return True

    def _bfs(self, roots):
        """广度优先找 CH 目录；深度和访问数都设上限，别把仪器爬穿"""
        seen = set()
        queue = [(r, 0) for r in roots]
        visited = 0
        while queue and visited < 3000:
            path, depth = queue.pop(0)
            if path in seen or depth > 6:
                continue
            seen.add(path)
            visited += 1
            base = posixpath.basename(path).upper()
            if base == CH_DIR:
                parent = posixpath.dirname(path)
                if self._adopt(parent):
                    return True
                continue
            for a in self._ls(path):
                if not self._isdir(a):
                    continue
                nm = a.filename
                if nm in (".", "..") or nm.lower() in SKIP_NAMES:
                    continue
                queue.append((posixpath.join(path, nm), depth + 1))
        return False

    def _tree_hint(self):
        """找不着时给一段"我看过哪儿"的摘要，方便人工判断"""
        lines = []
        for p in ("/", "/USR", "/USR/VAR", "/USR/DATA", "/DATA"):
            names = [a.filename for a in self._ls(p) if self._isdir(a)]
            if names:
                lines.append("  %-12s → %s" % (p, ", ".join(sorted(names)[:12])))
        return "\n".join(lines) or "  （连根目录都列不出来）"

    def band_dir(self, kind):
        """某频段在仪器上的目录"""
        self.discover()
        d = self._ch.get(kind.upper())
        if not d:
            raise SftpError("仪器上没有 %s 目录（现有：%s）"
                            % (kind.upper(), "、".join(sorted(self._ch))))
        return d

    # ------------------------------------------------------------ 列举
    def device_info(self):
        """{'TER': n, 'SAT': n, 'AUX': n, 'name': 安装目录名}"""
        self.discover()
        out = {"name": posixpath.basename(self.root or "")}
        for kind in ("TER", "SAT"):
            out[kind] = len(self.device_plans(kind))
        return out

    def device_plans(self, kind):
        """仪器上某频段的表名列表（不带 .XML）"""
        d = self.band_dir(kind)
        names = []
        for a in self._ls(d):
            if self._isdir(a):
                continue
            fn = a.filename
            if not fn.upper().endswith(E.PLAN_EXT.upper()):
                continue
            names.append(os.path.splitext(fn)[0])
        return sorted(names)

    def _remote_path(self, kind, name):
        """表名 → 仪器上的绝对路径（表名带不带 .XML 都接受）"""
        d = self.band_dir(kind)
        fn = name if name.upper().endswith(E.PLAN_EXT.upper()) \
            else name + E.PLAN_EXT
        return posixpath.join(d, fn)

    # ------------------------------------------------------------ 下载
    def download(self, kind, name, dest=None, overwrite=True):
        """把仪器上的一张表存到本地。返回本地路径"""
        self.connect()
        dest = dest or E.plan_path(os.getcwd(), kind, name)
        if os.path.exists(dest) and not overwrite:
            raise SftpError("本地已有同名表：%s" % os.path.basename(dest))
        remote = self._remote_path(kind, name)
        ensure_dir(dest)
        tmp = dest + ".part"
        try:
            self.sftp.get(remote, tmp)
        except FileNotFoundError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            have = "、".join(self.device_plans(kind)) or "（空）"
            raise SftpError("仪器上没有这张表：%s\n仪器现有：%s" % (name, have))
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise SftpError("下载失败（%s）：%s" % (name, e))
        os.replace(tmp, dest)
        self.log("已下载 %s → %s" % (remote, os.path.basename(dest)))
        return dest

    # ------------------------------------------------------------ 上传
    def upload(self, path, kind=None, name=None, backup=True):
        """把本地表推到仪器。返回仪器上的表名

        ⚠️ 会覆盖同名表。backup=True 时先把仪器上那份读回内存，
           返回值的 `.backup` 属性里带着（调用方要留就自己落盘）。
        """
        self.connect()
        kind = kind or E.kind_of_path(path)
        name = (name or os.path.splitext(os.path.basename(path))[0]).strip()
        if not name:
            raise SftpError("上传的表名不能为空")
        if not os.path.isfile(path):
            raise SftpError("文件不存在：%s" % path)
        remote = self._remote_path(kind, name)

        old = None
        if backup:
            try:
                with self.sftp.open(remote, "rb") as f:
                    old = f.read()
            except IOError:
                old = None                    # 本来就没有同名表，正常

        # 先传成 .part 再改名：中途断线也不会留下半张表顶掉原表
        tmp = remote + ".part"
        try:
            self.sftp.put(path, tmp, confirm=True)
            self._replace(tmp, remote)
        except Exception as e:
            try:
                self.sftp.remove(tmp)
            except Exception:
                pass
            raise SftpError("上传失败（%s）：%s" % (name, e))
        self.log("已上传 %s → %s" % (os.path.basename(path), remote))
        return _Uploaded(name, old)

    def _replace(self, src, dst):
        """原子替换：优先 posix_rename，不行就删了再改名"""
        try:
            self.sftp.posix_rename(src, dst)
            return
        except (IOError, OSError, AttributeError):
            pass
        try:
            self.sftp.remove(dst)
        except IOError:
            pass
        self.sftp.rename(src, dst)

    # ------------------------------------------------------------ 删除
    def remove(self, kind, name):
        """删仪器上的一张表（不可撤销）"""
        self.connect()
        remote = self._remote_path(kind, name)
        try:
            self.sftp.remove(remote)
        except FileNotFoundError:
            raise SftpError("仪器上没有这张表：%s" % name)
        except Exception as e:
            raise SftpError("删除失败（%s）：%s" % (name, e))
        self.log("已删除 %s" % remote)
        return True


class _Uploaded(str):
    """上传结果 = 表名（还是字符串，老代码照样能用），额外挂 .backup 原文"""

    def __new__(cls, name, backup):
        obj = str.__new__(cls, name)
        obj.backup = backup
        return obj


# ---------------------------------------------------------------- 体检
def probe(host, port=SFTP_PORT, user=SFTP_USER, password=SFTP_PASSWORD):
    """命令行体检：连上、找目录、列表、读一个文件，全程只读"""
    s = PlanSFTP(host, port=port, user=user, password=password,
                 log=lambda m: print("  " + m))
    print("=" * 60)
    print("SFTP 体检  %s:%d" % (host, port))
    print("=" * 60)
    try:
        s.connect()
        print("  SSH banner / 认证：OK")
        s.discover()
        print("  安装目录：%s" % (s.root,))
        for kind in ("TER", "SAT"):
            try:
                names = s.device_plans(kind)
            except SftpError as e:
                print("  %s：%s" % (kind, e))
                continue
            print("  %s 共 %d 张：%s" % (kind, len(names),
                                       "、".join(names[:8]) +
                                       ("…" if len(names) > 8 else "")))
        # 真读一个文件，确认不只是能列表
        for kind in ("TER", "SAT"):
            names = s.device_plans(kind)
            if not names:
                continue
            remote = s._remote_path(kind, names[0])
            with s.sftp.open(remote, "rb") as f:
                head = f.read(120)
            print("  试读 %s：%d 字节开头 %r"
                  % (remote, len(head), head[:60]))
            break
    except SftpError as e:
        print("  ✘ %s" % e)
        return 1
    finally:
        s.close()
    print("\n全部只读。SFTP 路线可用。")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法：python promax_sftp.py <仪器IP> [端口]")
        sys.exit(2)
    sys.exit(probe(sys.argv[1],
                   int(sys.argv[2]) if len(sys.argv) > 2 else SFTP_PORT))
