# -*- coding: utf-8 -*-
"""绪山真寻 Edge 图标替换向导 — 图形界面

这是现有 PowerShell 脚本的图形前端：它不重写任何核心逻辑，而是直接调用
src/Install.ps1 与 src/Uninstall.ps1（保持命令行功能完全不变）。UI 采用
已确认的「换装工作台」方向：
深蓝预览台、纵向变体选择和单一主操作，日志按需展开。

需要管理员权限：启动时检测，未提权则通过 UAC 自我提升后重启。
打包：PyInstaller onefile，资源（start-screen.png、src/*.ps1、*.ico）随包内置。
"""
import os
import sys
import ctypes
import threading
import subprocess
import queue
import json
import time
from tkinter import messagebox

# 必须在导入 ctk 和创建示例之前调用！
# if sys.platform.startswith("win"):
#     try:
#         ctypes.windll.shcore.SetProcessDpiAwareness(1)
#     except Exception:
#         pass

import customtkinter as ctk
from PIL import Image, ImageTk
import webbrowser


# ============================================================
# 资源路径解析：开发时用项目目录，PyInstaller onefile 时用 _MEIPASS。
# 脚本依赖的相对布局（src/ 在根下、两个 .ico 在根）被原样保留。
# ============================================================
def resource_base():
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    # 开发态：本文件在 gui/ 下，项目根是其上一级
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE = resource_base()
SPLASH_PNG = os.path.join(BASE, "start-screen.png")
INSTALL_PS1 = os.path.join(BASE, "src", "Install.ps1")
UNINSTALL_PS1 = os.path.join(BASE, "src", "Uninstall.ps1")
STATUS_PS1 = os.path.join(BASE, "src", "Status.ps1")

# 两个呆毛图标变体。default 角度与原版 Edge 一致；rotated 角度更符合呆毛特征。
# GUI 让用户预览并选择，安装时把对应变体名作为 -Variant 传给 Install.ps1。
ICO_DEFAULT = os.path.join(BASE, "oyama-mahiro-ahoge.ico")
ICO_ROTATED = os.path.join(BASE, "oyama-mahiro-ahoge-rotated.ico")


# ============================================================
# UAC：检测管理员；未提权则用 ShellExecute "runas" 重新以管理员启动后退出。
# ============================================================
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_and_exit():
    """以管理员重新启动当前程序，然后退出当前非提权实例。"""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, ""
    else:
        exe, params = sys.executable, f'"{os.path.abspath(__file__)}"'
    # 透传原有参数（去掉脚本名本身）
    extra = " ".join(f'"{a}"' for a in sys.argv[1:])
    params = (params + " " + extra).strip()
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    # > 32 表示成功触发提权；否则用户在 UAC 弹窗点了"否"
    sys.exit(0 if rc > 32 else 1)


# ============================================================
# 主题色板 —— 取自动画人设图的奶油白、浅粉发色、琥珀色与制服海军蓝。
# ============================================================
HAIR_LIGHT = "#f3d7d0"
HAIR_DEEP = "#f3a8a2"
HILIGHT_YELLOW = "#f5c36d"
EYE_DEEP = "#9d5a57"
EYE_MID = "#d97f7b"
EYE_GLOW = "#c9dae9"

PINK_BG = "#fff9f5"
PINK_CARD = "#fffefc"
PINK_PRIMARY = HAIR_DEEP
PINK_PRIMARY_HOVER = "#d97f7b"
PINK_DANGER = "#f4ddd8"
PINK_DANGER_HOVER = "#edc8c0"
PINK_TEXT = EYE_DEEP
PINK_SUBTLE = "#435574"
PREVIEW_BG = "#435574"
PREVIEW_TITLE = "#fffefc"
PREVIEW_SUBTLE = "#c9dae9"
SUCCESS_GREEN = "#5e8d78"

# 自定义标题栏配色（隐藏原生白色栏，自绘主题色横条）
TITLEBAR_BG = HAIR_DEEP
TITLEBAR_FG = PINK_TEXT
CLOSE_HOVER = "#e06a66"
MIN_HOVER = PINK_PRIMARY_HOVER

WINDOW_W = 760
WINDOW_H = 620
WORKBENCH_CARD_HEIGHT = 390

FONT_FAMILY = "Microsoft YaHei UI"

# ============================================================
# 在后台线程运行 PowerShell 脚本，逐行把输出推入队列；
# 只读取末尾的机器可读结果；界面不再展示技术日志或进度条。
# ============================================================
RESULT_PREFIX = "@@MAHIRO_RESULT@@"


def run_powershell(ps1_path, out_queue, extra_args=None):
    """运行一个 .ps1，把 ('line', text) / ('done', returncode) / ('error', msg) 入队。

    extra_args：可选的 (名, 值) 参数列表，如 [("Variant", "rotated")]，会拼成
    `-Variant 'rotated'` 追加到脚本调用后。值做单引号转义防注入/截断。
    """
    if not os.path.isfile(ps1_path):
        out_queue.put(("error", f"找不到脚本: {ps1_path}"))
        out_queue.put(("done", 1))
        return
    # 用 -Command 包裹：先把 PowerShell 的输出编码强制设为 UTF-8，再运行脚本。
    # Windows PowerShell 5.1 默认按 OEM 代码页（zh-CN 为 GBK）写 stdout，
    # 而我们按 UTF-8 读 → 中文乱码。这里两端都钉死 UTF-8 即可对齐。
    safe_path = ps1_path.replace("'", "''")  # 单引号转义，防路径注入/截断
    arg_str = ""
    for name, value in (extra_args or []):
        safe_val = str(value).replace("'", "''")
        arg_str += f" -{name} '{safe_val}'"
    inline = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"& '{safe_path}'{arg_str}"
    )
    cmd = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-Command", inline,
    ]
    try:
        # 让 PowerShell 以 UTF-8 输出，避免中文乱码；隐藏子进程控制台窗口。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        startupinfo = _hidden_startupinfo()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        out_queue.put(("error", f"启动 PowerShell 失败: {e}"))
        out_queue.put(("done", 1))
        return

    for line in proc.stdout:
        line = line.rstrip("\r\n")
        out_queue.put(("log", line))
        if line.startswith(RESULT_PREFIX):
            try:
                out_queue.put(("result", json.loads(line[len(RESULT_PREFIX):])))
            except json.JSONDecodeError:
                out_queue.put(("error", "脚本没有返回有效的执行结果"))
    proc.wait()
    out_queue.put(("done", proc.returncode))


def read_status(ps1_path, out_queue, request_id, retry_install=False):
    """在后台读取只读状态；失败也只影响状态提示，不阻塞安装或恢复。"""
    if not os.path.isfile(ps1_path):
        out_queue.put(("status_error", (request_id, "找不到状态脚本")))
        return
    safe_path = ps1_path.replace("'", "''")
    cmd = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"& '{safe_path}' -AsJson",
    ]
    deadline = time.monotonic() + (5.0 if retry_install else 0.0)
    while True:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                startupinfo=_hidden_startupinfo(), creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            payload = proc.stdout.strip()
            status = json.loads(payload) if proc.returncode == 0 and payload else None
        except Exception:
            status = None

        if status is not None and (
            not retry_install or _installed_status_is_healthy(status)
            or time.monotonic() >= deadline
        ):
            out_queue.put(("status", (request_id, status)))
            return
        if time.monotonic() >= deadline:
            out_queue.put(("status_error", (request_id, "暂时无法读取保护状态")))
            return
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def _installed_status_is_healthy(payload):
    guard_ok = payload.get("GuardTask", {}).get("Healthy", False)
    runtime_ok = payload.get("RuntimeTask", {}).get("Healthy", False)
    marked = payload.get("ExeMarked", 0)
    total = payload.get("ExeTotal", 0)
    return bool(
        payload.get("Ok") and payload.get("InstallStaged") and total > 0
        and marked == total and payload.get("AllProfileIconsPatched")
        and guard_ok and runtime_ok
    )


def _hidden_startupinfo():
    """创建隐藏子进程窗口的配置，供查询与实际操作共用。"""
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


# ============================================================
# 主窗口
# ============================================================
class MahiroEdgeApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")

        self.title("绪山真寻.exe")
        self.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.minsize(WINDOW_W, WINDOW_H)
        self.configure(fg_color=PINK_BG)
        try:
            self.iconbitmap(ICO_DEFAULT)
        except Exception:
            pass

        # 隐藏 Windows 原生白色标题栏，下面自绘主题色横条。
        self.overrideredirect(True)
        # 无边框窗口缺少系统投影/边界。把 root 底色设成描边色，内容 inset 1px 露出细边。
        self.configure(fg_color=EYE_MID)

        self._variant = "default"        # 当前选中的图标变体
        self._variant_cards = {}         # name -> CTkFrame（用于切换高亮边框）

        self.queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.running = False
        self._last_result = None
        self._operation_error = None
        self._operation_log = []
        self._status_request_id = 0
        self._verb = None
        self._build_ui()
        self.after(80, self._poll_queue)
        self.after(120, self._poll_status)
        self.after(0, self._refresh_status)

        self.withdraw()
        self._splash = None
        self.after(0, self._show_splash)

    # --- 启动画面 ---
    def _show_splash(self):
        if not os.path.isfile(SPLASH_PNG):
            self._end_splash()
            return
        try:
            pil = Image.open(SPLASH_PNG)
        except Exception:
            self._end_splash()
            return

        # 创建 Toplevel 窗口
        sp = ctk.CTkToplevel(self)
        sp.overrideredirect(True)
        sp.attributes("-topmost", True)
        sp.configure(fg_color=PINK_BG)

        # 此时可以用 sp 来获取当前屏幕的准确分辨率（对多显示器更友好）
        sp.update_idletasks() 
        sw = sp.winfo_screenwidth()
        sh = sp.winfo_screenheight()

        # 等比缩放
        w = max(320, min(460, int(min(sw, sh) * 0.42)))
        h = int(pil.height * (w / pil.width))
        self._splash_img = ctk.CTkImage(light_image=pil, size=(w, h))
        
        pad = 18
        ctk.CTkLabel(sp, image=self._splash_img, text="", fg_color=PINK_BG).pack(padx=pad, pady=pad)
        
        # 精确计算加上 Padding 后的窗口大小
        ww, hh = w + pad * 2, h + pad * 2
        
        # 居中算法（数学逻辑没问题，但通过增加限制防止计算出负数）
        x = max(0, (sw - ww) // 2)
        y = max(0, (sh - hh) // 2)
        
        # 设置几何属性
        sp.geometry(f"{ww}x{hh}+{x}+{y}")
        
        # 强制刷新一次，防止闪烁或错位
        sp.update_idletasks()
        
        self._splash = sp
        self.after(2500, self._end_splash)

    def _end_splash(self):
        if self._splash is not None:
            try:
                self._splash.destroy()
            except Exception:
                pass
            self._splash = None
        
        # --- 让主窗口也居中显示，防止在屏幕边缘随机弹出来 ---
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_w, win_h = WINDOW_W, WINDOW_H
        x = (sw // 2) - (win_w // 2)
        y = (sh // 2) - (win_h // 2)

        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(WINDOW_W, WINDOW_H)

        self.deiconify()
        self.lift()
        self.focus_force()
        # CTk 在高 DPI 下会让两列的请求高度略有差异；显示后按右侧实际高度校准左侧。
        self.after(80, self._align_workbench_cards)

        # overrideredirect 会让窗口从任务栏消失：找回任务栏按钮，再叠加系统级模糊。
        self._enable_taskbar()
        self._apply_window_effect()

        # 启动画面结束后，在后台静默预加载信息弹窗，并立刻将其隐藏
        self.after(500, self._preload_info)
    
    # 配合增加一个小方法
    def _preload_info(self):
        self._show_info_dialog()
        if hasattr(self, "_info_win") and self._info_win is not None:
            self._info_win.withdraw() # 画完立刻藏起来

    def _align_workbench_cards(self):
        """让效果预览和选择卡片的可见底边严格对齐。"""
        preview = getattr(self, "_preview_card", None)
        choice = getattr(self, "_choice_card", None)
        if preview is None or choice is None:
            return
        current, desired = preview.winfo_height(), choice.winfo_height()
        if current > 1 and desired > 1 and current != desired:
            requested = int(preview.cget("height") * desired / current)
            preview.configure(height=requested)

    # ============================================================
    # 自定义标题栏配套：任务栏按钮、系统级模糊、最小化、窗口拖动
    # ============================================================
    def _hwnd(self):
        """取本窗口的 Win32 HWND。"""
        return ctypes.windll.user32.GetParent(self.winfo_id())

    def _enable_taskbar(self):
        """overrideredirect 窗口默认无任务栏按钮。改 GWL_EXSTYLE：
        清 WS_EX_TOOLWINDOW、加 WS_EX_APPWINDOW，再 hide→show 让样式生效。"""
        try:
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            user32 = ctypes.windll.user32
            hwnd = self._hwnd()
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            # 重新映射窗口让任务栏按钮出现（SW_HIDE=0, SW_SHOW=5）
            user32.ShowWindow(hwnd, 0)
            user32.ShowWindow(hwnd, 5)
        except Exception:
            pass

    def _apply_window_effect(self):
        return

    def _minimize(self):
        """最小化。overrideredirect 与 iconify 有已知冲突，改用 Win32 ShowWindow。"""
        try:
            ctypes.windll.user32.ShowWindow(self._hwnd(), 6)  # SW_MINIMIZE
        except Exception:
            try:
                self.iconify()
            except Exception:
                pass

    # --- 自定义标题栏拖动：记录按下时的鼠标-窗口偏移，移动时贴着走 ---
    def _start_move(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_move(self, event):
        x = self.winfo_x() + (event.x - getattr(self, "_drag_x", 0))
        y = self.winfo_y() + (event.y - getattr(self, "_drag_y", 0))
        self.geometry(f"+{x}+{y}")

    def _build_ui(self):
        # 1px inset：root 底色为描边色 EYE_MID，外层容器留 1px 露出细边。
        outer = ctk.CTkFrame(self, fg_color=PINK_BG, corner_radius=0)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        self._build_titlebar(outer)

        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=30, pady=(20, 20))

        ctk.CTkLabel(
            body, text="MAHIRO EDGE · 图标守护", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"), text_color=PINK_PRIMARY,
        ).pack(fill="x")
        ctk.CTkLabel(
            body, text="给 Edge 换上粉色呆毛", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=28, weight="bold"), text_color=PREVIEW_BG,
        ).pack(fill="x", pady=(3, 3))
        ctk.CTkLabel(
            body, text="先挑喜欢的角度，再一键启用长期保护。", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13), text_color=PINK_SUBTLE,
        ).pack(fill="x", pady=(0, 14))

        self.status_label = ctk.CTkLabel(
            body, text="正在检查保护状态…", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=SUCCESS_GREEN, fg_color="#eaf4ee", corner_radius=12,
        )
        self.status_label.pack(fill="x", pady=(0, 18), ipady=7)

        workbench = ctk.CTkFrame(body, fg_color="transparent")
        # 两张卡片按各自内容收束；不要让较短的选择区被预览区拉成空白大卡片。
        workbench.pack(fill="x")
        workbench.grid_columnconfigure(0, weight=5)
        workbench.grid_columnconfigure(1, weight=6)

        preview = ctk.CTkFrame(
            workbench, height=WORKBENCH_CARD_HEIGHT, fg_color=PREVIEW_BG, corner_radius=28
        )
        self._preview_card = preview
        preview.grid(row=0, column=0, sticky="new", padx=(0, 16))
        preview.grid_propagate(False)
        ctk.CTkLabel(
            preview, text="效果预览", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"), text_color=PREVIEW_SUBTLE,
        ).pack(fill="x", padx=24, pady=(24, 0))
        self._preview_images = {}
        for name, path in (("default", ICO_DEFAULT), ("rotated", ICO_ROTATED)):
            try:
                self._preview_images[name] = ctk.CTkImage(
                    light_image=Image.open(path), size=(132, 132)
                )
            except Exception:
                self._preview_images[name] = None
        self.preview_icon = ctk.CTkLabel(preview, text="", image=None)
        self.preview_icon.pack(expand=True, pady=(12, 4))
        ctk.CTkLabel(
            preview, text="Microsoft Edge",
            font=ctk.CTkFont(family=FONT_FAMILY, size=19, weight="bold"), text_color=PREVIEW_TITLE,
        ).pack(pady=(0, 4))
        ctk.CTkLabel(
            preview, text="桌面、任务栏与新窗口都会同步",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12), text_color=PREVIEW_SUBTLE,
        ).pack(pady=(0, 24))

        choice = ctk.CTkFrame(
            workbench, height=WORKBENCH_CARD_HEIGHT, fg_color=PINK_CARD, corner_radius=28
        )
        self._choice_card = choice
        choice.grid(row=0, column=1, sticky="new")
        choice.grid_propagate(False)
        ctk.CTkLabel(
            choice, text="选择呆毛角度", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=19, weight="bold"), text_color=PREVIEW_BG,
        ).pack(fill="x", padx=24, pady=(24, 2))
        ctk.CTkLabel(
            choice, text="之后随时可以回来换一种。", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12), text_color=PINK_SUBTLE,
        ).pack(fill="x", padx=24)
        self._build_variant_cards(choice)

        self.btn_install = ctk.CTkButton(
            choice, text="启用呆毛保护", height=46,
            font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight="bold"),
            fg_color=PINK_PRIMARY, hover_color=PINK_PRIMARY_HOVER,
            text_color=PINK_TEXT, corner_radius=23, command=self.on_install,
        )
        self.btn_install.pack(fill="x", padx=24, pady=(12, 7))
        self.btn_uninstall = ctk.CTkButton(
            choice, text="恢复原版图标", height=34,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            fg_color="transparent", hover_color=HAIR_LIGHT,
            text_color=PINK_SUBTLE, corner_radius=17, border_width=0,
            command=self.on_uninstall,
        )
        self.btn_uninstall.pack(fill="x", padx=24, pady=(0, 2))

    # --- 自定义标题栏 ---
    def _build_titlebar(self, parent):
        bar = ctk.CTkFrame(parent, height=38, corner_radius=0, fg_color=TITLEBAR_BG)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        title = ctk.CTkLabel(
            bar, text="  绪山真寻.exe", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            text_color=TITLEBAR_FG,
        )
        title.pack(side="left", padx=(8, 0))

        # 关闭按钮（最右）
        ctk.CTkButton(
            bar, text="✕", width=44, height=38, corner_radius=0,
            font=ctk.CTkFont(size=15), fg_color=TITLEBAR_BG,
            hover_color=CLOSE_HOVER, text_color=TITLEBAR_FG,
            command=self._on_close,
        ).pack(side="right")
        # 最小化按钮
        ctk.CTkButton(
            bar, text="—", width=44, height=38, corner_radius=0,
            font=ctk.CTkFont(size=15), fg_color=TITLEBAR_BG,
            hover_color=MIN_HOVER, text_color=TITLEBAR_FG,
            command=self._minimize,
        ).pack(side="right")
        # 信息按钮
        ctk.CTkButton(
            bar, text="ⓘ", width=44, height=38, corner_radius=0,
            font=ctk.CTkFont(size=16), fg_color=TITLEBAR_BG,
            hover_color=MIN_HOVER, text_color=TITLEBAR_FG,
            command=self._show_info_dialog,
        ).pack(side="right")

        # 拖动：标题栏底条与标题文字都可拖
        for w in (bar, title):
            w.bind("<Button-1>", self._start_move)
            w.bind("<B1-Motion>", self._on_move)

    def _on_close(self):
        try:
            self.destroy()
        except Exception:
            os._exit(0)
    
    # --- 信息弹窗 ---
    def _show_info_dialog(self):
        # 1. 如果窗口已经存在，直接解除隐藏并聚焦
        if hasattr(self, "_info_win") and self._info_win is not None and self._info_win.winfo_exists():
            self._info_win.deiconify() # 解除隐藏
            self._info_win.lift()      # 提升到顶层
            self._info_win.focus()     # 获取焦点
            
            # 重新计算一下居中（防止主窗口被拖动过）
            w, h = 300, 180
            x = self.winfo_x() + (self.winfo_width() - w) // 2
            y = self.winfo_y() + (self.winfo_height() - h) // 2
            self._info_win.geometry(f"+{x}+{y}")
            return

        # 2. 如果是首次调用，则创建窗口（后续代码几乎不变）
        info_win = ctk.CTkToplevel(self)
        self._info_win = info_win
        
        # 隐藏系统原生标题栏，复用主窗口的无边框和描边方案
        info_win.overrideredirect(True)
        info_win.configure(fg_color=EYE_MID) # 底色作为边框色
        info_win.attributes("-topmost", True) # 确保在最上层

        # 居中计算：悬浮在主窗口正中央
        info_win.update_idletasks()
        w, h = 300, 180
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        info_win.geometry(f"{w}x{h}+{x}+{y}")
        info_win.transient(self)
        
        # 外层容器：留出 1px 细边
        outer = ctk.CTkFrame(info_win, fg_color=PINK_BG, corner_radius=0)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        # 迷你的自定义标题栏
        bar = ctk.CTkFrame(outer, height=32, corner_radius=0, fg_color=TITLEBAR_BG)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        
        ctk.CTkLabel(
            bar, text="  关于", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=TITLEBAR_FG,
        ).pack(side="left")
        
        # 弹窗关闭按钮
        ctk.CTkButton(
            bar, text="✕", width=38, height=32, corner_radius=0,
            font=ctk.CTkFont(size=14), fg_color=TITLEBAR_BG,
            hover_color=CLOSE_HOVER, text_color=TITLEBAR_FG,
            command=info_win.withdraw, 
        ).pack(side="right")

        # 内容容器（使用粉色卡片底色）
        body = ctk.CTkFrame(outer, fg_color=PINK_CARD, corner_radius=12)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 16))

        # 版本号
        ctk.CTkLabel(
            body, text="v1.0.3",
            font=ctk.CTkFont(family=FONT_FAMILY, size=18, weight="bold"), 
            text_color=PINK_TEXT
        ).pack(pady=(12, 2))
        
        # 作者信息
        ctk.CTkLabel(
            body, text="作者：HaroldRoot", 
            font=ctk.CTkFont(family=FONT_FAMILY, size=12), 
            text_color=PINK_SUBTLE
        ).pack(pady=(0, 10))

        # 项目仓库链接（加下划线，模拟超链接效果）
        link_lbl = ctk.CTkLabel(
            body, text="🔗 访问 GitHub 项目仓库喵", 
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, underline=True), 
            text_color=EYE_DEEP, cursor="hand2"
        )
        link_lbl.pack()
        
        # 绑定左键点击事件打开浏览器
        link_lbl.bind(
            "<Button-1>", 
            lambda e: webbrowser.open("https://github.com/HaroldRoot/mahiro-edge")
        )

    # --- 图标变体选择：纵向单选项，和左侧大预览同步。---
    def _build_variant_cards(self, parent):
        specs = [
            ("default", ICO_DEFAULT, "原版角度", "与原版 Edge 一致喵"),
            ("rotated", ICO_ROTATED, "呆毛角度", "更符合呆毛特征喵"),
        ]
        self._variant_cards = {}
        for name, _ico, label, sub in specs:
            card = ctk.CTkFrame(
                parent, fg_color=PINK_BG, corner_radius=15,
                border_width=2, border_color=HAIR_LIGHT,
            )
            card.pack(fill="x", padx=24, pady=(12 if name == "default" else 5, 0))
            name_lbl = ctk.CTkLabel(
                card, text=label, font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
                text_color=PREVIEW_BG, anchor="w",
            )
            name_lbl.grid(row=0, column=0, padx=14, pady=(10, 0), sticky="w")
            sub_lbl = ctk.CTkLabel(
                card, text=sub, font=ctk.CTkFont(family=FONT_FAMILY, size=11),
                text_color=PINK_SUBTLE, anchor="w",
            )
            sub_lbl.grid(row=1, column=0, padx=14, pady=(0, 10), sticky="w")
            indicator = ctk.CTkLabel(
                card, text="○", font=ctk.CTkFont(size=17, weight="bold"), text_color=PINK_PRIMARY,
            )
            indicator.grid(row=0, column=1, rowspan=2, padx=14)
            card.grid_columnconfigure(0, weight=1)

            # 整张卡片（含子控件）可点选
            for w in (card, name_lbl, sub_lbl, indicator):
                w.configure(cursor="hand2")
                w.bind("<Button-1>", lambda e, n=name: self._select_variant(n))
            self._variant_cards[name] = (card, indicator)

        self._select_variant("default")  # 初始高亮

    def _select_variant(self, name):
        self._variant = name
        for n, (card, indicator) in self._variant_cards.items():
            selected = n == name
            card.configure(
                fg_color=(HAIR_LIGHT if selected else PINK_BG),
                border_color=(PINK_PRIMARY if selected else HAIR_LIGHT),
            )
            indicator.configure(text=("●" if selected else "○"))
        if hasattr(self, "preview_icon"):
            self.preview_icon.configure(image=self._preview_images.get(name))

    def _set_busy(self, busy):
        self.running = busy
        state = "disabled" if busy else "normal"
        self.btn_install.configure(state=state)
        self.btn_uninstall.configure(state=state)

    def _refresh_status(self):
        if not self.running:
            self._status_request_id += 1
            request_id = self._status_request_id
            threading.Thread(
                target=read_status, args=(STATUS_PS1, self.status_queue, request_id), daemon=True
            ).start()

    def _poll_status(self):
        try:
            while True:
                kind, payload = self.status_queue.get_nowait()
                if kind == "status":
                    request_id, payload = payload
                    if self.running or request_id != self._status_request_id:
                        continue
                    if self._verb == "安装" and self._last_result and self._last_result.get("Success"):
                        guard_ok = payload.get("GuardTask", {}).get("Healthy", False)
                        runtime_ok = payload.get("RuntimeTask", {}).get("Healthy", False)
                        marked = payload.get("ExeMarked", 0)
                        total = payload.get("ExeTotal", 0)
                        profiles_ok = payload.get("AllProfileIconsPatched", False)
                        if payload.get("Ok") and payload.get("InstallStaged") and total > 0 and marked == total and profiles_ok and guard_ok and runtime_ok:
                            self.status_label.configure(
                                text=f"安装完成 · EXE {marked}/{total} · 两项任务正常", text_color=SUCCESS_GREEN
                            )
                        else:
                            self.status_label.configure(
                                text=f"安装未完全完成 · EXE {marked}/{total} · 请查看详细日志", text_color=PINK_TEXT
                            )
                            guard = payload.get("GuardTask", {})
                            runtime = payload.get("RuntimeTask", {})
                            diagnostics = [
                                "安装后状态核对：",
                                f"状态查询正常: {payload.get('Ok', False)}；暂存图标: {payload.get('InstallStaged', False)}",
                                f"可补丁 EXE 已标记: {marked}/{total}；无图标资源跳过: {payload.get('ExeNoIcon', 0)}；配置图标已替换: {payload.get('ProfileIconPatched', 0)}/{payload.get('ProfileIconTotal', 0)}",
                                f"自愈任务: Healthy={guard.get('Healthy', False)}, State={guard.get('State', 'unknown')}, LastTaskResult={guard.get('LastTaskResult', 'unknown')}",
                                f"运行时任务: Healthy={runtime.get('Healthy', False)}, State={runtime.get('State', 'unknown')}, ProcessAlive={runtime.get('ProcessAlive', False)}",
                            ]
                            if payload.get("Error"):
                                diagnostics.append(f"状态错误: {payload['Error']}")
                            self._show_operation_details("\n".join(diagnostics))
                        continue
                    guard = payload.get("GuardTask", {}).get("Exists", False)
                    runtime = payload.get("RuntimeTask", {}).get("Exists", False)
                    marked = payload.get("ExeMarked", 0)
                    total = payload.get("ExeTotal", 0)
                    guard_healthy = payload.get("GuardTask", {}).get("Healthy", False)
                    runtime_healthy = payload.get("RuntimeTask", {}).get("Healthy", False)
                    profiles_healthy = payload.get("AllProfileIconsPatched", False)
                    if payload.get("Ok") and payload.get("InstallStaged") and guard and runtime and guard_healthy and runtime_healthy and profiles_healthy and total > 0 and marked == total:
                        text, color = f"已启用保护 · EXE {marked}/{total} · 两项任务正常", "#5D7A52"
                    elif payload.get("Ok") and not total:
                        text, color = "未检测到可处理的 Edge 安装", PINK_TEXT
                    else:
                        text, color = "未完全启用 · 可重新安装或查看日志", PINK_TEXT
                    self.status_label.configure(text=text, text_color=color)
                elif kind == "status_error":
                    request_id, message = payload
                    if not self.running and request_id == self._status_request_id:
                        self.status_label.configure(text=message, text_color=PINK_TEXT)
                        if self._verb == "安装" and self._last_result and self._last_result.get("Success"):
                            self._show_operation_details(f"安装后状态核对失败：{message}")
        except queue.Empty:
            pass
        self.after(120, self._poll_status)

    # --- 按钮回调 ---
    def on_install(self):
        if not messagebox.askokcancel(
            "确认安装",
            "即将关闭所有 Edge 进程、备份原始图标资源、创建两个计划任务，"
            "并重启 Windows 资源管理器。\n\n已保存的网页内容请先手动保存。",
            icon="warning", parent=self,
        ):
            return
        self._start(INSTALL_PS1, "安装", with_variant=True)

    def on_uninstall(self):
        if not messagebox.askokcancel(
            "确认恢复",
            "即将关闭所有 Edge 进程、停止图标常驻任务，并从备份恢复原始图标。\n\n确定继续吗？",
            icon="warning", parent=self,
        ):
            return
        self._start(UNINSTALL_PS1, "卸载", with_variant=False)

    def _start(self, ps1, verb, with_variant=False):
        if self.running:
            return
        self._verb = verb
        self._last_result = None
        self._operation_error = None
        self._operation_log = []
        self._status_request_id += 1
        self._set_busy(True)
        self.status_label.configure(text=f"正在{verb}，请稍候…", text_color=PINK_SUBTLE)

        # 安装时把选中的变体作为 -Variant 传给 Install.ps1；卸载无需变体。
        extra_args = [("Variant", self._variant)] if with_variant else []
        t = threading.Thread(
            target=run_powershell, args=(ps1, self.queue, extra_args), daemon=True
        )
        t.start()

    # --- 轮询后台输出，刷新 UI（始终在主线程）---
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "result":
                    self._last_result = payload
                elif kind == "log":
                    self._operation_log.append(payload)
                elif kind == "error":
                    self._operation_error = payload
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def _finish(self, returncode):
        self.running = False
        self._set_busy(False)
        operation_succeeded = returncode == 0 and self._last_result and self._last_result.get("Success")
        if operation_succeeded:
            if self._verb == "安装":
                # The script result alone does not include the discovered EXE denominator.
                # Verify the complete installed state before presenting success.
                self._status_request_id += 1
                request_id = self._status_request_id
                threading.Thread(
                    target=read_status,
                    args=(STATUS_PS1, self.status_queue, request_id, True),
                    daemon=True,
                ).start()
                self.status_label.configure(text="安装完成，正在核对保护状态…", text_color=PINK_SUBTLE)
            else:
                self.status_label.configure(
                    text=self._last_result.get("Message", "操作完成。"), text_color=SUCCESS_GREEN
                )
        elif self._last_result and self._last_result.get("Partial"):
            self.status_label.configure(
                text=self._last_result.get("Message", "操作只部分完成。"), text_color=PINK_TEXT
            )
        else:
            message = self._operation_error or f"操作失败（退出码 {returncode}）。"
            self.status_label.configure(text=message, text_color=PINK_TEXT)
        if not operation_succeeded:
            self._show_operation_details()

    def _show_operation_details(self, diagnostic=None):
        """Expose full PowerShell output whenever an operation fails or is partial."""
        details = "\n".join(self._operation_log).strip()
        if self._operation_error:
            details = (details + "\n\n" + self._operation_error).strip()
        if diagnostic:
            details = (details + "\n\n" + diagnostic).strip()
        if not details:
            details = self.status_label.cget("text")
        win = ctk.CTkToplevel(self)
        win.title(f"{self._verb}详细日志")
        win.geometry("680x420")
        win.transient(self)
        box = ctk.CTkTextbox(win, wrap="word", font=ctk.CTkFont(family=FONT_FAMILY, size=12))
        box.pack(fill="both", expand=True, padx=12, pady=12)
        box.insert("1.0", details)
        box.configure(state="disabled")


def main():
    if not is_admin():
        # 未提权：触发 UAC 重新以管理员启动，然后退出当前实例。
        elevate_and_exit()
        return
    app = MahiroEdgeApp()
    app.mainloop()


if __name__ == "__main__":
    main()
