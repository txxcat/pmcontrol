# -*- coding: utf-8 -*-
"""自绘小控件（ttk 没有、又不想引入第三方主题时用）。

目前只有 `ToggleSwitch`（滑动开关）：设置窗里原来那一排 yes/no 下拉框，
改成"点一下滑过去"的开关 —— 布尔量用下拉框选，既多一次点击，
也容易误让人以为有第三种状态。

用法：
    var = tk.BooleanVar(value=True)
    ToggleSwitch(row, variable=var).pack(side="left")
    # 取值永远是 bool：var.get() / var.set(False)，没有 "yes"/"no" 字符串

实现要点：
  · 轨道用**一条圆头粗线**画（比手工拼两个半圆 + 矩形干净），滑块是圆
  · 点击只改 var，画面靠 var 的 trace 回调更新 —— 所以代码里 var.set()
    也会带动开关滑动，不会出现"值变了、样子没变"
  · Canvas 不支持透明，底色从 ttk 主题里问（见 promax_theme.canvas_bg）
"""
import tkinter as tk

import promax_theme as theme


def _mix(c1, c2, t):
    """两色线性混合：t=0 → c1，t=1 → c2。"""
    t = max(0.0, min(1.0, float(t)))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(a, b))


class ToggleSwitch(tk.Canvas):
    """滑动开关，绑 `tk.BooleanVar`。

    参数：
      variable  —— BooleanVar（可省，省了就用内部的一个）
      command   —— 切换后回调，收到新的 bool
      width/height/pad —— 一般不传，默认 46×24、滑块离边 3px
    """

    WIDTH, HEIGHT, PAD = 46, 24, 3
    FRAME_MS = 14          # 一帧 14ms ≈ 70fps，肉眼是"滑过去"而不是"跳过去"

    def __init__(self, master, variable=None, command=None, width=None,
                 height=None, pad=None, bg=None, **kw):
        # ⚠️ BooleanVar 的第一个位置参数是 master，不是初始值 ——
        #    写成 BooleanVar(False) 会报 'bool' object has no attribute '_root'
        self._var = (variable if variable is not None
                     else tk.BooleanVar(master=master, value=False))
        self._sw = int(width or self.WIDTH)
        self._sh = int(height or self.HEIGHT)
        self._pad = float(self.PAD if pad is None else pad)
        self._command = command
        self._anim = None
        self._destroyed = False

        kw.setdefault("highlightthickness", 0)     # 深色模式下白框很扎眼
        kw.setdefault("borderwidth", 0)
        kw.setdefault("takefocus", 1)              # 键盘也能操作（见下方 <space>）
        # 背景色必须和四周一致，否则是一个看得见的方块
        super().__init__(master, width=self._sw, height=self._sh,
                         bg=bg or theme.canvas_bg(master), **kw)

        self._pos = 1.0 if bool(self._var.get()) else 0.0
        self._trace = self._var.trace_add("write", lambda *_: self._sync())
        self.bind("<Button-1>", self._on_click)
        self.bind("<space>", self._on_click)
        self.bind("<Return>", self._on_click)
        self.bind("<Destroy>", self._on_destroy)
        self._redraw()

    # ---------------- 对外 ----------------
    def get(self):
        return bool(self._var.get())

    def set(self, value, animate=True):
        self._var.set(bool(value))
        if not animate:
            self._kill_anim()
            self._pos = 1.0 if bool(self._var.get()) else 0.0
            self._redraw()

    def variable(self):
        return self._var

    # ---------------- 交互 ----------------
    def _on_click(self, _e=None):
        self._var.set(not bool(self._var.get()))     # 画面由 trace 驱动
        if self._command:
            try:
                self._command(bool(self._var.get()))
            except Exception:
                pass
        return "break"

    def _on_destroy(self, _e=None):
        self._destroyed = True
        self._kill_anim()
        try:
            self._var.trace_remove("write", self._trace)
        except Exception:
            pass

    # ---------------- 动画 ----------------
    def _kill_anim(self):
        if self._anim is not None:
            try:
                self.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None

    def _sync(self):
        target = 1.0 if bool(self._var.get()) else 0.0
        if abs(target - self._pos) < 0.01:
            self._pos = target
            self._redraw()
        else:
            self._animate(target)

    def _animate(self, target):
        self._kill_anim()
        step = 0.2 if target > self._pos else -0.2

        def tick():
            self._anim = None
            if self._destroyed:
                return
            nxt = self._pos + step
            if (step > 0 and nxt >= target) or (step < 0 and nxt <= target):
                self._pos = target
            else:
                self._pos = nxt
            self._redraw()
            if self._pos != target and not self._destroyed:
                self._anim = self.after(self.FRAME_MS, tick)

        tick()          # 先走一帧：点击立刻有反馈，不等下一个事件循环

    # ---------------- 绘制 ----------------
    def _redraw(self):
        if self._destroyed:
            return
        try:
            self.delete("all")
            p = theme.palette()
            w, h = self._sw, self._sh
            r = h / 2.0
            track = _mix(p["switch_off"], p["switch_on"], self._pos)
            # 胶囊轨道：圆头粗线，两端各留 r，正好填满 w×h
            self.create_line(r, r, w - r, r, width=h, capstyle="round",
                             fill=track)
            d = h - 2 * self._pad
            x0 = self._pad + (w - 2 * self._pad - d) * self._pos
            cy = h / 2.0
            self.create_oval(x0, cy - d / 2.0, x0 + d, cy + d / 2.0,
                             fill=p["switch_knob"], outline=p["switch_edge"],
                             width=1)
        except tk.TclError:
            self._destroyed = True
