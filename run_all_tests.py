# -*- coding: utf-8 -*-
"""一条命令跑完全部测试。

为什么要有这个：测试脚本有 12 个、全平铺在仓库根目录，以前跑全量得照着
README 逐行贴 13 条命令，还容易漏。（第三方在 macOS 实测反馈 2026-10-30 的 P3-1）

用法：
    python run_all_tests.py                 # 全跑
    python run_all_tests.py platform layout # 只跑名字含这些词的
    python run_all_tests.py --list          # 看清单不跑
    python run_all_tests.py --fail-fast     # 第一个失败就停

退出码：0 全绿；1 有失败；2 环境问题（脚本缺失 / 超时）。
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
TIMEOUT = 900           # 单个套件上限；plan_edit / selftest 是耗时大户

# (脚本, 说明, 是否计数式)
# 计数式 = 脚本自己打印「通过 N   失败 M」；非计数式只看退出码
SUITES = [
    ("_selftest_mock.py",  "协议级自检（对着模拟器）", True),
    ("_test_promax.py",    "Promax 封装 / 结果文件读写", True),
    ("_test_timer.py",     "顶栏计时器", True),
    ("_test_retest.py",    "失败重测", True),
    ("_test_progress.py",  "进度反馈 / 进度条", True),
    ("_test_highlight.py", "右侧列表高亮", True),
    ("_test_layout.py",    "右列布局自适应", True),
    ("_test_platform.py",  "跨平台打开文件 / 文件名归属", True),
    ("_test_plan_edit.py", "频道表编辑 / zip 备份", True),
    ("_test_plan_sync.py", "与设备同步（模拟器四条路线）", True),
    ("_test_reconnect.py", "断线重连", True),
    ("_e2e_mock.py",       "端到端跑表（无断言）", False),
]

# 各脚本的汇总行有两种写法：`通过 23   失败 0` 与 `通过 126 / 失败 0`
COUNT_RE = re.compile(r"通过\s+(\d+)\s*/?\s*失败\s+(\d+)")
BADLINE_RE = re.compile(r"\[FAIL\]|\[!! \]|Traceback|Error")


def run_one(script):
    """跑一个套件，返回 (ok, passed, failed, secs, tail_lines, bad_lines)。"""
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        return False, 0, 0, 0.0, ["脚本不存在：%s" % script], []

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"        # 子进程统一 utf-8，避免 Windows 下 GBK 乱码

    t0 = time.time()
    try:
        p = subprocess.run([PY, "-u", path], cwd=HERE, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=TIMEOUT)
        out = p.stdout.decode("utf-8", "replace")
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.output or b"").decode("utf-8", "replace") if e.output else ""
        return (False, 0, 0, time.time() - t0,
                ["超时（>%ds）" % TIMEOUT], ["超时"])
    secs = time.time() - t0

    lines = out.splitlines()
    tail = [l for l in lines if l.strip()][-6:]
    bad = [l for l in lines if BADLINE_RE.search(l)]

    m = None
    for l in reversed(lines):                # 取最后一次出现的汇总行
        m = COUNT_RE.search(l)
        if m:
            break
    if m:
        passed, failed = int(m.group(1)), int(m.group(2))
    else:
        passed, failed = 0, 0

    ok = (code == 0)
    return ok, passed, failed, secs, tail, bad


def main():
    args = [a for a in sys.argv[1:]]
    if "--list" in args:
        for s, desc, counted in SUITES:
            print("  %-22s %s%s" % (s, desc, "" if counted else "（非计数）"))
        return 0

    fail_fast = "--fail-fast" in args
    picks = [a for a in args if not a.startswith("-")]
    suites = [x for x in SUITES
              if not picks or any(p.lower() in x[0].lower() for p in picks)]
    if not suites:
        print("没有匹配的套件。用 --list 看清单。")
        return 2

    print("=" * 62)
    print("pmcontrol 测试总览  |  python %s" % sys.version.split()[0])
    print("=" * 62)

    tot_pass = tot_fail = 0
    bad_suites = []
    t_start = time.time()

    for i, (script, desc, counted) in enumerate(suites, 1):
        print("\n[%d/%d] %-22s %s" % (i, len(suites), script, desc))
        ok, passed, failed, secs, tail, bad = run_one(script)

        if counted and (passed or failed):
            tot_pass += passed
            tot_fail += failed
            mark = "OK  " if failed == 0 else "FAIL"
            print("        [%s] 通过 %d   失败 %d   (%.1fs)"
                  % (mark, passed, failed, secs))
        else:
            mark = "OK  " if ok else "FAIL"
            print("        [%s] 退出码 %s   (%.1fs)"
                  % (mark, "0" if ok else "非 0", secs))

        if not ok or (counted and failed):
            bad_suites.append(script)
            for l in bad[:12]:
                print("        | %s" % l.strip())
            if not bad:
                for l in tail:
                    print("        | %s" % l.strip())
            if fail_fast:
                print("\n--fail-fast：停在 %s" % script)
                break

    total = time.time() - t_start
    print("\n" + "=" * 62)
    if tot_pass or tot_fail:
        print("合计：通过 %d   失败 %d" % (tot_pass, tot_fail))
    print("套件：%d 个，%d 个有问题%s"
          % (len(suites), len(bad_suites),
             "：" + "、".join(bad_suites) if bad_suites else ""))
    print("用时：%d 分 %d 秒" % (total // 60, total % 60))
    print("=" * 62)
    return 1 if bad_suites else 0


if __name__ == "__main__":
    sys.exit(main())
