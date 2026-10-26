# -*- coding: utf-8 -*-
"""频道表同步（promax_plan_sync + promax_fileio）的集成测试。

全部对着 mock_promax_server.py 跑，**不碰真机**。四条路线各起一个模拟器：

  A 正常固件          → 单表路线（strategy=single），下载/上传/删除/整装
  B --io-no-tag       → 复现 31.17 对 `TER-CH=` 回 NAK：列举/下载降级到整装 ZIP，
                        上传必须**明确拒绝**（不能悄悄换整装包覆盖设备）
  C --io-esc          → 数据里 0xFF 按 telnet IAC 转义：解转义后必须逐字节一致
  D --io-crc xor      → CRC 算法换成 xor：客户端要能从下载数据反推出来

断言的共同口径是 **字节完全一致** —— 表能"下载回来看着像"没用，
仪器认的是文件内容，差一个字节都可能不认这张表。

用法：python _test_plan_sync.py
"""
import filecmp
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promax_plan_edit as E                             # noqa: E402
import promax_plan_sync as S                             # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "mock_promax_server.py")
OK = 0
BAD = 0
FAILS = []


def check(name, cond, detail=""):
    global OK, BAD
    if cond:
        OK += 1
        print("  [OK]   %s" % name)
    else:
        BAD += 1
        FAILS.append("%s   %s" % (name, detail))
        print("  [FAIL] %s   %s" % (name, detail))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def demo_dir(prefix):
    """复制 4 张 DEMO 表到临时目录（TER/ + SAT/）"""
    tmp = tempfile.mkdtemp(prefix=prefix)
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for fn in os.listdir(src):
                if fn.upper().startswith("DEMO-") and fn.upper().endswith(".XML"):
                    shutil.copy2(os.path.join(src, fn), os.path.join(dst, fn))
    return tmp


class Mock(object):
    """起一个模拟器子进程，退出时杀掉"""

    def __init__(self, plans_dir, *extra):
        self.port = free_port()
        cmd = [sys.executable, MOCK, "--port", str(self.port),
               "--plans", plans_dir, "--quiet"] + list(extra)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.STDOUT)
        self._wait()

    def _wait(self):
        t0 = time.time()
        while time.time() - t0 < 15:
            s = socket.socket()
            s.settimeout(0.3)
            try:
                s.connect(("127.0.0.1", self.port))
                s.close()
                return
            except OSError:
                s.close()
                time.sleep(0.1)
        raise RuntimeError("模拟器没起来（端口 %d）" % self.port)

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def mk_sync(port, src, verbose=False):
    return S.PlanSync("127.0.0.1", port, source=src, verbose=verbose,
                      timeout=20.0,
                      log=(lambda m: print("      · %s" % m)) if verbose else None)


# ================================================================ A 正常固件
def case_a():
    print("\n== A 正常固件：单表路线 ==")
    dev = demo_dir("pm_sync_dev_")
    src = demo_dir("pm_sync_local_")
    mock = Mock(dev)
    sync = mk_sync(mock.port, src, verbose=True)
    try:
        sync.connect()
        info = sync.device_info()
        check("设备概况读到表数", info["TER"] == 2 and info["SAT"] == 2, info)

        names = sync.device_plans("TER")
        check("单表命令列出射频表",
              sorted(names) == ["DEMO-Analog", "DEMO-Digital"], names)
        check("走的是单表路线", sync.strategy == "single", sync.strategy)

        # ---- 下载：必须逐字节一致 ----
        out = os.path.join(src, "TER", "DL-Digital.XML")
        got = sync.download("TER", "DEMO-Digital", out, overwrite=True)
        check("下载落到指定路径", got == out and os.path.isfile(out), got)
        check("下载内容与设备上逐字节一致",
              filecmp.cmp(out, os.path.join(dev, "TER", "DEMO-Digital.XML"),
                          shallow=False),
              "%d vs %d 字节" % (os.path.getsize(out),
                                 os.path.getsize(
                                     os.path.join(dev, "TER",
                                                  "DEMO-Digital.XML"))))
        check("CRC 算法自动识别出来", sync.io.crc_name is not None, "没识别出")
        check("识别结果 = 服务端默认 crc8-0x07",
              sync.io.crc_name == "crc8-0x07", sync.io.crc_name)
        check("下载的表能被编辑器解析", len(E.load_plan(out)) > 0)

        # ---- 卫星表（downlink 语义）也能下 ----
        outs = os.path.join(src, "SAT", "DL-C.XML")
        sync.download("SAT", "DEMO-C-Band", outs, overwrite=True)
        check("卫星表下载一致",
              filecmp.cmp(outs, os.path.join(dev, "SAT", "DEMO-C-Band.XML"),
                          shallow=False))
        check("卫星表仍是 downlink 语义",
              any("downlink" in el.attrib
                  for el in E.load_plan(outs).root.iter("CHANNEL")))

        # ---- 上传：改一张表传上去，再下回来比对 ----
        up = os.path.join(src, "TER", "UP-TEST.XML")
        p = E.load_plan(os.path.join(dev, "TER", "DEMO-Digital.XML"))
        p.add_channel("UP-ADDED", 666.0, "DVB-C", symbol_rate=6875)
        p.save(up)
        uploaded = sync.upload(up, kind="TER", name="UP-TEST")
        check("上传返回设备上的表名", uploaded == "UP-TEST", uploaded)
        names = sync.device_plans("TER")
        check("设备表清单里出现了新表", "UP-TEST" in names, names)
        back = os.path.join(src, "TER", "UP-BACK.XML")
        sync.download("TER", "UP-TEST", back, overwrite=True)
        check("上传后下回来逐字节一致",
              filecmp.cmp(up, back, shallow=False),
              "%d vs %d" % (os.path.getsize(up), os.path.getsize(back)))
        check("回读的表里有新增频点",
              "UP-ADDED" in open(back, "rb").read().decode("utf-8"))

        # ---- 删除 ----
        sync.remove("TER", "UP-TEST")
        names = sync.device_plans("TER")
        check("删除后表清单里没有了", "UP-TEST" not in names, names)

        # ---- 整装 ZIP ----
        zp = os.path.join(src, "inst.zip")
        sync.download_installation(zp)
        check("整装 zip 下载成功", os.path.isfile(zp), zp)
        with zipfile.ZipFile(zp) as z:
            arc = [n for n in z.namelist() if n.upper().endswith(".XML")]
        check("整装 zip 里 4 张表",
              len(arc) == 4, arc)
        check("整装 zip 里 TER/SAT 分开",
              any("/TER/" in a.upper() or a.upper().startswith("TER/")
                  for a in arc) and
              any("/SAT/" in a.upper() or a.upper().startswith("SAT/")
                  for a in arc), arc)

        # ---- 整装导入本地 ----
        imp = tempfile.mkdtemp(prefix="pm_sync_imp_")
        sync.source = imp                       # import_installation 写到 self.source
        res = sync.import_installation(zp, overwrite=True)
        check("整装导入写好 4 张", len(res["written"]) == 4, res)
        got = sorted(os.path.splitext(os.path.basename(it["path"]))[0]
                     for it in E.list_plans(imp))
        check("导入后本地 4 张表能列出来",
              got == ["DEMO-Analog", "DEMO-C-Band", "DEMO-Digital",
                      "DEMO-Ku-Band"], got)
        check("导入的表按 TER/SAT 分开落盘",
              os.path.isdir(os.path.join(imp, "TER")) and
              os.path.isdir(os.path.join(imp, "SAT")) and
              len(os.listdir(os.path.join(imp, "TER"))) == 2 and
              len(os.listdir(os.path.join(imp, "SAT"))) == 2,
              os.listdir(imp))
        check("导入的射频表仍能解析",
              len(E.load_plan(os.path.join(imp, "TER", "DEMO-Digital.XML"))) > 0)
        shutil.rmtree(imp, ignore_errors=True)
        sync.source = src

        # ---- 下载不存在的表 ----
        try:
            sync.download("TER", "NO-SUCH-TABLE",
                          os.path.join(src, "TER", "x.XML"), overwrite=True)
            check("下载不存在的表要报错", False, "没报错")
        except S.SyncError as e:
            msg = str(e)
            check("错误里点名是哪张表", "NO-SUCH-TABLE" in msg, msg)
            check("错误里给出两种可能原因",
                  "大小写" in msg and "不认单表" in msg, msg)
    finally:
        sync.close()
        mock.stop()
        shutil.rmtree(dev, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)


# ================================================================ B 31.17
def case_b():
    print("\n== B 模拟 31.17：单表命令回 NAK → 整装 ZIP 兜底 ==")
    dev = demo_dir("pm_sync_dev_")
    src = demo_dir("pm_sync_local_")
    mock = Mock(dev, "--io-no-tag")
    sync = mk_sync(mock.port, src, verbose=True)
    try:
        sync.connect()
        names = sync.device_plans("TER")
        check("列举降级后仍拿到表名",
              sorted(names) == ["DEMO-Analog", "DEMO-Digital"], names)
        check("记录下走的是整装路线", sync.strategy == "zip", sync.strategy)

        out = os.path.join(src, "TER", "DL-Zip.XML")
        sync.download("TER", "DEMO-Analog", out, overwrite=True)
        check("从整装包里取出的表逐字节一致",
              filecmp.cmp(out, os.path.join(dev, "TER", "DEMO-Analog.XML"),
                          shallow=False))
        check("下载后 strategy 仍是 zip", sync.strategy == "zip", sync.strategy)

        # 上传必须拒绝，不能悄悄整装覆盖
        up = os.path.join(src, "TER", "UP-X.XML")
        shutil.copy2(os.path.join(dev, "TER", "DEMO-Digital.XML"), up)
        try:
            sync.upload(up, kind="TER", name="UP-X")
            check("31.17 上上传应被拒绝", False, "居然传上去了")
        except S.SyncError as e:
            msg = str(e)
            check("上传被明确拒绝", True)
            check("拒绝理由说清了风险",
                  "整装" in msg and ("替换" in msg or "NAK" in msg), msg)
            check("提示指向诊断脚本", "_probe_io" in msg, msg)
    finally:
        sync.close()
        mock.stop()
        shutil.rmtree(dev, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)


# ================================================================ C IAC 转义
def case_c():
    print("\n== C 数据里 0xFF 按 telnet 转义（--io-esc）==")
    dev = demo_dir("pm_sync_dev_")
    src = demo_dir("pm_sync_local_")
    mock = Mock(dev, "--io-esc")
    sync = mk_sync(mock.port, src, verbose=True)
    try:
        sync.connect()
        out = os.path.join(src, "TER", "DL-Esc.XML")
        sync.download("TER", "DEMO-Digital", out, overwrite=True)
        check("解转义后逐字节一致",
              filecmp.cmp(out, os.path.join(dev, "TER", "DEMO-Digital.XML"),
                          shallow=False),
              "%d vs %d" % (os.path.getsize(out),
                                os.path.getsize(
                                    os.path.join(dev, "TER",
                                                 "DEMO-Digital.XML"))))
        check("转义模式下 CRC 也能对上", sync.io.crc_name is not None)
    finally:
        sync.close()
        mock.stop()
        shutil.rmtree(dev, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)


# ================================================================ D 换 CRC
def case_d():
    print("\n== D 换 CRC 算法（--io-crc xor）==")
    dev = demo_dir("pm_sync_dev_")
    src = demo_dir("pm_sync_local_")
    mock = Mock(dev, "--io-crc", "xor")
    sync = mk_sync(mock.port, src, verbose=True)
    try:
        sync.connect()
        out = os.path.join(src, "TER", "DL-Xor.XML")
        sync.download("TER", "DEMO-Digital", out, overwrite=True)
        check("xor 下内容仍然一致",
              filecmp.cmp(out, os.path.join(dev, "TER", "DEMO-Digital.XML"),
                          shallow=False))
        check("反推出的是 xor（不是默认的 crc8-0x07）",
              sync.io.crc_name == "xor", sync.io.crc_name)

        # 换了算法还能不能上传（CRC 必须用反推出的那套算）
        up = os.path.join(src, "TER", "UP-XOR.XML")
        shutil.copy2(os.path.join(dev, "TER", "DEMO-Digital.XML"), up)
        sync.upload(up, kind="TER", name="UP-XOR")
        check("xor 下上传成功", "UP-XOR" in sync.device_plans("TER"))
    finally:
        sync.close()
        mock.stop()
        shutil.rmtree(dev, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)


def main():
    case_a()
    case_b()
    case_c()
    case_d()
    print("\n%s\n通过 %d / 失败 %d" % ("=" * 46, OK, BAD))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
