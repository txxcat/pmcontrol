# -*- coding: utf-8 -*-
"""给"必须真连模拟器"的脚本用：确保 127.0.0.1:2222 有人在听。

已经有模拟器在跑就什么都不做（保留调用者自己指定 --lock-time 等参数的权利）；
没有就自己拉一个，脚本退出时自动收尸。

为什么要有这个：
    README「跑测试」小节第一条命令就是 `python _selftest_mock.py`，
    但没起模拟器时它直接抛裸 Traceback —— 新人容易误判成代码坏了。
    而 `_test_reconnect.py` 等一批脚本本来就会自己拉 mock，行为不一致反而更容易踩。
    （第三方在 macOS 实测反馈，2026-10-30）
"""
import atexit
import os
import socket
import subprocess
import sys
import time

HOST = "127.0.0.1"
PORT = 2222
HERE = os.path.dirname(os.path.abspath(__file__))


def _listening(host=HOST, port=PORT, timeout=0.4):
    """端口是否有人 accept。模拟器每连接起一个线程，探测连接不会占住会话。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _kill(p):
    try:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)
    except Exception:
        pass


def ensure_mock(lock_time=None, wait=15.0):
    """确保模拟器就绪。

    lock_time: 模拟器锁定所需秒数，**必须 >= 调用方的等待时长**，
               否则"该锁上的"会被误判为失败。
    返回 Popen 或 None（None = 本来就在跑，不归我们管）。
    """
    if _listening():
        return None

    script = os.path.join(HERE, "mock_promax_server.py")
    if not os.path.isfile(script):
        print("找不到 mock_promax_server.py，无法自动启动模拟器", file=sys.stderr)
        sys.exit(2)

    argv = [sys.executable, script, "--quiet"]
    if lock_time is not None:
        argv += ["--lock-time", str(lock_time)]

    print("%s:%d 没有监听，自动启动模拟器…" % (HOST, PORT), flush=True)
    p = subprocess.Popen(argv, cwd=HERE,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    deadline = time.time() + wait
    while time.time() < deadline:
        if p.poll() is not None:
            print("模拟器启动后立即退出（返回码 %s），可能是 2222 被别的程序占了"
                  % p.returncode, file=sys.stderr)
            sys.exit(2)
        if _listening():
            atexit.register(_kill, p)
            return p
        time.sleep(0.1)

    p.kill()
    print("等了 %.0f 秒模拟器仍未就绪。可手动排查：\n"
          "    python mock_promax_server.py" % wait, file=sys.stderr)
    sys.exit(2)
