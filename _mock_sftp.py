# -*- coding: utf-8 -*-
"""SFTP 模拟器：在本机临时目录上跑一个真的 SSH/SFTP 服务，供测试连接。

为什么需要
----------
`promax_sftp` 走的是真 SSH，没有真机就没法测；而「等真机回来再验」
等于把风险推给现场。这里用 paramiko 自己当服务端，把一棵临时目录
当成仪器的文件系统根，客户端代码一行不改就能跑通全流程。

用法
----
    root = tempfile.mkdtemp()
    os.makedirs(root + "/USR/VAR/INSTALACIONES/I-1/CH/TER")
    srv = MockSFTP(root, user="Promax", password="Monitoring2019")
    srv.start()
    ...  # srv.port 就是端口
    srv.stop()

只映射到参数给定的 root，**碰不到本机其它路径**（`_real()` 做了前缀校验）。
"""
import os
import socket
import threading
import time

import paramiko

# 复用一个固定主机密钥，省得每次生成（RSA 2048 生成要好几百毫秒）
_HOST_KEY = None


def _host_key():
    global _HOST_KEY
    if _HOST_KEY is None:
        _HOST_KEY = paramiko.RSAKey.generate(2048)
    return _HOST_KEY


class _Server(paramiko.ServerInterface):
    def __init__(self, user, password):
        self.user = user
        self.password = password
        self.authed = False

    def check_auth_password(self, username, password):
        if username == self.user and password == self.password:
            self.authed = True
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_subsystem_request(self, channel, name):
        # 不实现这条的话 sftp 子系统会被拒（默认实现依赖 transport 配置）
        return super(_Server, self).check_channel_subsystem_request(
            channel, name)


class _Handle(paramiko.SFTPHandle):
    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def chattr(self, attr):
        return paramiko.SFTP_OP_UNSUPPORTED


class _SFTP(paramiko.SFTPServerInterface):
    """把 SFTP 路径映射到本地 root 下，做前缀校验防越界"""

    def __init__(self, server, *args, **kw):
        super(_SFTP, self).__init__(server, *args, **kw)
        self.root = os.path.realpath(server.root)

    def _real(self, path):
        p = os.path.realpath(os.path.join(self.root, path.lstrip("/")))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise OSError(13, "outside root")
        return p

    def list_folder(self, path):
        p = self._real(path)
        try:
            out = []
            for name in os.listdir(p):
                a = paramiko.SFTPAttributes.from_stat(
                    os.lstat(os.path.join(p, name)))
                a.filename = name
                out.append(a)
            return out
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def open(self, path, flags, attr):
        p = self._real(path)
        try:
            binary_flag = getattr(os, "O_BINARY", 0)
            flags |= binary_flag
            mode = getattr(attr, "st_mode", None) or 0o666
            fd = os.open(p, flags, mode)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

        if (flags & os.O_WRONLY) or (flags & os.O_RDWR):
            if flags & os.O_TRUNC:
                fstr = "wb"
            elif flags & os.O_APPEND:
                fstr = "ab"
            else:
                fstr = "r+b"
        else:
            fstr = "rb"
        try:
            f = os.fdopen(fd, fstr)
        except OSError as e:
            os.close(fd)
            return paramiko.SFTPServer.convert_errno(e.errno)
        h = _Handle(flags)
        h.filename = p
        h.readfile = f
        h.writefile = f
        return h

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        try:
            os.rename(self._real(oldpath), self._real(newpath))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def canonicalize(self, path):
        return "/" + posix_norm(path)


def posix_norm(path):
    """粗归一化（模拟器够用）：去掉 . 和 ..，压缩斜杠"""
    parts = []
    for seg in path.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


class MockSFTP(object):
    """起一个后台 SFTP 服务，绑 127.0.0.1，端口自动分配"""

    def __init__(self, root, user="Promax", password="Monitoring2019"):
        self.root = os.path.abspath(root)
        self.user = user
        self.password = password
        self.sock = None
        self.port = None
        self._stop = threading.Event()
        self._thread = None
        self._transports = []

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle, args=(conn,),
                                 daemon=True)
            t.start()

    def _handle(self, conn):
        try:
            ts = paramiko.Transport(conn)
            ts.add_server_key(_host_key())
            srv = _Server(self.user, self.password)
            srv.root = self.root
            ts.set_subsystem_handler("sftp", paramiko.SFTPServer, _SFTP)
            ts.start_server(server=srv)
            self._transports.append(ts)
            while ts.is_active() and not self._stop.is_set():
                time.sleep(0.05)
            ts.close()
        except Exception:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        for ts in self._transports:
            try:
                ts.close()
            except Exception:
                pass
        self._transports = []
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def build_tree(root, plans=None, install="INSTAL-1"):
    """按真机形状铺一棵目录树：/USR/VAR/INSTALACIONES/<install>/CH/TER|SAT

    plans = {"TER": {"名称": b"<xml…>"}, "SAT": {...}}
    """
    plans = plans or {"TER": {"DEMO-A": b"<plan/>"}, "SAT": {}}
    base = os.path.join(root, "USR", "VAR", "INSTALACIONES", install, "CH")
    for kind in ("TER", "SAT"):
        d = os.path.join(base, kind)
        os.makedirs(d, exist_ok=True)
        for name, blob in plans.get(kind, {}).items():
            with open(os.path.join(d, name + ".XML"), "wb") as f:
                f.write(blob)
    return base


if __name__ == "__main__":
    import tempfile
    r = tempfile.mkdtemp(prefix="mock_sftp_")
    build_tree(r)
    s = MockSFTP(r)
    print("root:", r)
    print("port:", s.start())
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s.stop()
