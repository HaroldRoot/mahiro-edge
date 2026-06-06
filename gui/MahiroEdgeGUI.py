# -*- coding: utf-8 -*-
"""绪山真寻 Edge 图标替换向导 — 图形界面

这是现有 PowerShell 脚本的图形前端：它不重写任何核心逻辑，而是直接调用
src/Install.ps1 与 src/Uninstall.ps1（保持命令行功能完全不变），并解析脚本
打印的 [N/M] 步骤标记来驱动粉色进度条。

需要管理员权限：启动时检测，未提权则通过 UAC 自我提升后重启。
打包：PyInstaller onefile，资源（start-screen.png、src/*.ps1、*.ico）随包内置。
"""
import os
import sys
import re
import ctypes
import threading
import subprocess
import queue
import tkinter.font as tkfont

# 必须在导入 ctk 和创建示例之前调用！
if sys.platform.startswith("win"):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

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
# 主题色板 —— 取自绪山真寻官方人设配色
#   头发浅粉 #ead4ce / 头发深粉 #ffaaa7 / 高光亮黄 #fbf4c8
#   眼睛深色 #975f5c / 眼睛中色 #c4776c / 眼睛浅光 #f4b386
# ============================================================
HAIR_LIGHT = "#ead4ce"
HAIR_DEEP = "#ffaaa7"
HILIGHT_YELLOW = "#fbf4c8"
EYE_DEEP = "#975f5c"
EYE_MID = "#c4776c"
EYE_GLOW = "#f4b386"

PINK_BG = HAIR_LIGHT
PINK_CARD = "#f6e0db"
PINK_PRIMARY = HAIR_DEEP
PINK_PRIMARY_HOVER = "#ff8f8b"
PINK_DANGER = "#f3c6c2"
PINK_DANGER_HOVER = "#eab3ae"
PINK_TEXT = "#8a4038"
PINK_SUBTLE = EYE_DEEP
LOG_BG = HILIGHT_YELLOW
LOG_BORDER = EYE_MID

# 自定义标题栏配色（隐藏原生白色栏，自绘主题色横条）
TITLEBAR_BG = HAIR_DEEP
TITLEBAR_FG = PINK_TEXT
CLOSE_HOVER = "#e06a66"
MIN_HOVER = PINK_PRIMARY_HOVER

FONT_FAMILY = "Microsoft YaHei UI"

# 等宽字体。按优先级探测系统已装的第一个。
# tkfont.families() 需要 Tk 根存在，故延迟到根创建后调用。
MONO_CANDIDATES = ("Cascadia Code", "JetBrains Mono", "Consolas", "Courier New")


def pick_mono_family():
    """返回系统已安装的首个等宽字体名；都没有则回退 Consolas。"""
    try:
        installed = set(tkfont.families())
    except Exception:
        return "Consolas"
    for name in MONO_CANDIDATES:
        if name in installed:
            return name
    return "Consolas"


# ============================================================
# 在后台线程运行 PowerShell 脚本，逐行把输出推入队列；
# 解析行首的 [N/M] 步骤标记估算进度。主线程轮询队列刷新 UI。
# ============================================================
STEP_RE = re.compile(r"\[(\d+)\s*/\s*(\d+)\]")


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
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
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
        out_queue.put(("line", line.rstrip("\r\n")))
    proc.wait()
    out_queue.put(("done", proc.returncode))


# ============================================================
# 主窗口
# ============================================================
class MahiroEdgeApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")

        self.title("绪山真寻.exe")
        self.geometry("520x560")
        self.minsize(520, 560)
        self.configure(fg_color=PINK_BG)
        try:
            self.iconbitmap(ICO_DEFAULT)
        except Exception:
            pass

        # 隐藏 Windows 原生白色标题栏，下面自绘主题色横条。
        self.overrideredirect(True)
        # 无边框窗口缺少系统投影/边界。把 root 底色设成描边色，内容 inset 1px 露出细边。
        self.configure(fg_color=EYE_MID)

        self._mono_family = pick_mono_family()
        self._variant = "default"        # 当前选中的图标变体
        self._variant_cards = {}         # name -> CTkFrame（用于切换高亮边框）

        self.queue = queue.Queue()
        self.running = False
        self._build_ui()
        self.after(80, self._poll_queue)

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
        win_w, win_h = 520, 560
        x = (sw // 2) - (win_w // 2)
        y = (sh // 2) - (win_h // 2)

        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(520, 560)

        self.deiconify()
        self.lift()
        self.focus_force()

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
        # 1px inset：root 底色为描边色 EYE_MID，外层容器留 1px 露出细边，
        # 给无边框窗口一个清晰边界（替代被 overrideredirect 移除的系统边框）。
        outer = ctk.CTkFrame(self, fg_color=PINK_BG, corner_radius=0)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        # --- 自定义标题栏：主题色横条 + 标题 + 最小化/关闭按钮 ---
        self._build_titlebar(outer)

        # 内容容器（标题栏以下的一切都放这里，留出左右内边距）
        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(16, 0))

        # 顶部标题区（左对齐，告别居中）
        ctk.CTkLabel(
            body, text="Microsoft Edge 图标替换向导", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=22, weight="bold"), text_color=PINK_TEXT,
        ).pack(fill="x", pady=(4, 2))
        ctk.CTkLabel(
            body, text="若有正在运行的 Edge 浏览器和文件资源管理器窗口，请先关闭，避免丢失数据", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12), text_color=PINK_SUBTLE,
        ).pack(fill="x", pady=(0, 12))

        # --- 图标变体选择卡片 ---
        self._build_variant_cards(body)

        # --- 按钮区 ---
        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(pady=(4, 12))
        self.btn_install = ctk.CTkButton(
            btn_row, text="安装呆毛图标", width=210, height=46,
            font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight="bold"),
            fg_color=PINK_PRIMARY, hover_color=PINK_PRIMARY_HOVER,
            text_color=PINK_TEXT, corner_radius=23,
            command=self.on_install,
        )
        self.btn_install.grid(row=0, column=0, padx=8)
        self.btn_uninstall = ctk.CTkButton(
            btn_row, text="恢复原版图标", width=210, height=46,
            font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight="bold"),
            fg_color=PINK_DANGER, hover_color=PINK_DANGER_HOVER,
            text_color=PINK_TEXT, corner_radius=23,
            command=self.on_uninstall,
        )
        self.btn_uninstall.grid(row=0, column=1, padx=8)

        # --- 粉色进度条 ---
        self.progress = ctk.CTkProgressBar(
            body, width=460, height=16, corner_radius=8,
            fg_color=PINK_CARD, progress_color=PINK_PRIMARY,
            border_width=1, border_color=EYE_GLOW,
        )
        self.progress.set(0)
        self.progress.pack(pady=(6, 4))

        # --- 日志输出框（基底等宽，便于路径/日志逐字符对齐）---
        # 中文在等宽字体里会落到宋体（衬线，难看）。故做"双字体":
        # 基底用等宽，再把每行的 CJK 片段打 tag 换成微软雅黑（无衬线），中英各取所长。
        self.log = ctk.CTkTextbox(
            body, width=480, height=140, corner_radius=12,
            fg_color=LOG_BG, text_color=PINK_TEXT, border_width=1,
            border_color=LOG_BORDER, font=ctk.CTkFont(family=self._mono_family, size=12),
        )
        self.log.pack(pady=(0, 16), fill="both", expand=True)
        # CJK 片段用的无衬线字体 tag（直接配在底层 tk Textbox 上：CTkTextbox 禁止
        # 在 tag 上设 font，但底层 widget 没这限制）。
        self._cjk_font = tkfont.Font(family=FONT_FAMILY, size=12)
        try:
            self.log._textbox.tag_configure("cjk", font=self._cjk_font)
        except Exception:
            pass
        self.log.configure(state="disabled")

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
            body, text="v1.0.1", 
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
            body, text="🔗 访问 GitHub 项目仓库", 
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, underline=True), 
            text_color=EYE_DEEP, cursor="hand2"
        )
        link_lbl.pack()
        
        # 绑定左键点击事件打开浏览器
        link_lbl.bind(
            "<Button-1>", 
            lambda e: webbrowser.open("https://github.com/HaroldRoot/mahiro-edge")
        )

    # --- 图标变体选择卡片（带预览缩略图）---
    def _build_variant_cards(self, parent):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(0, 12))
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=1)

        specs = [
            ("default", ICO_DEFAULT, "原版角度", "与原版 Edge 一致"),
            ("rotated", ICO_ROTATED, "呆毛角度", "更符合呆毛特征"),
        ]
        self._variant_thumbs = []  # 持有引用防被 GC
        for col, (name, ico, label, sub) in enumerate(specs):
            card = ctk.CTkFrame(
                row, fg_color=PINK_CARD, corner_radius=14,
                border_width=2, border_color=PINK_CARD,
            )
            card.grid(row=0, column=col, padx=6, sticky="nsew")

            thumb = None
            try:
                pil = Image.open(ico)
                thumb = ctk.CTkImage(light_image=pil, size=(56, 56))
                self._variant_thumbs.append(thumb)
            except Exception:
                pass

            img_lbl = ctk.CTkLabel(card, image=thumb, text="")
            img_lbl.pack(pady=(12, 4))
            name_lbl = ctk.CTkLabel(
                card, text=label, font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
                text_color=PINK_TEXT,
            )
            name_lbl.pack()
            sub_lbl = ctk.CTkLabel(
                card, text=sub, font=ctk.CTkFont(family=FONT_FAMILY, size=11),
                text_color=PINK_SUBTLE,
            )
            sub_lbl.pack(pady=(0, 12))

            # 整张卡片（含子控件）可点选
            for w in (card, img_lbl, name_lbl, sub_lbl):
                w.configure(cursor="hand2")
                w.bind("<Button-1>", lambda e, n=name: self._select_variant(n))
            self._variant_cards[name] = card

        self._select_variant("default")  # 初始高亮

    def _select_variant(self, name):
        self._variant = name
        for n, card in self._variant_cards.items():
            card.configure(border_color=(PINK_PRIMARY if n == name else PINK_CARD))

    # --- 日志辅助 ---
    def _append_log(self, text):
        self.log.configure(state="normal")
        start_index = self.log.index("end-1c")  # 本行插入前的位置，用于定位 CJK 片段
        self.log.insert("end", text + "\n")
        self._tag_cjk_runs(start_index, text)
        self.log.see("end")
        self.log.configure(state="disabled")

    # 把刚插入这一行里的 CJK 连续片段打上 "cjk" tag（换成无衬线字体）。
    # 基底等宽字体只擅长 ASCII；中文落到等宽会变宋体衬线，故单独换雅黑。
    def _tag_cjk_runs(self, start_index, text):
        try:
            line, col = (int(x) for x in start_index.split("."))
        except Exception:
            return
        i = 0
        n = len(text)
        while i < n:
            if self._is_cjk(text[i]):
                j = i
                while j < n and self._is_cjk(text[j]):
                    j += 1
                try:
                    self.log._textbox.tag_add(
                        "cjk", f"{line}.{col + i}", f"{line}.{col + j}"
                    )
                except Exception:
                    pass
                i = j
            else:
                i += 1

    @staticmethod
    def _is_cjk(ch):
        o = ord(ch)
        # CJK 统一表意 + 扩展A + 兼容 + 中日韩标点 + 全角符号
        return (
            0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF
            or 0xF900 <= o <= 0xFAFF
        )

    def _set_busy(self, busy):
        self.running = busy
        state = "disabled" if busy else "normal"
        self.btn_install.configure(state=state)
        self.btn_uninstall.configure(state=state)

    # --- 按钮回调 ---
    def on_install(self):
        self._start(INSTALL_PS1, "安装", with_variant=True)

    def on_uninstall(self):
        self._start(UNINSTALL_PS1, "卸载", with_variant=False)

    def _start(self, ps1, verb, with_variant=False):
        if self.running:
            return
        self._verb = verb
        self._set_busy(True)

        # 平滑进度：display 缓动追 target；忙碌时 target 自己朝 ceiling 慢慢爬，
        # 即使脚本没吐新步骤也不会停滞。真实 [N/M] 只把 target 往上抬，绝不回退。
        self._prog_display = 0.0
        self._prog_target = 0.0
        self._prog_ceiling = 0.90   # 完成前的软上限，留出 10% 给收尾，避免提前填满
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self._anim_on = True
        self._animate_progress()

        # 安装时把选中的变体作为 -Variant 传给 Install.ps1；卸载无需变体。
        extra_args = [("Variant", self._variant)] if with_variant else []
        self._append_log(f"=== 开始{verb} ===")
        t = threading.Thread(
            target=run_powershell, args=(ps1, self.queue, extra_args), daemon=True
        )
        t.start()

    # --- 进度条缓动：每 ~30ms 一帧。display 指数缓动逼近 target；忙碌时 target
    #     也朝 ceiling 缓慢爬升（爬升量随接近 ceiling 而递减，越接近越慢，永不骤停）。
    def _animate_progress(self):
        if not getattr(self, "_anim_on", False):
            return
        # 忙碌中：target 朝 ceiling 缓慢爬（慢心跳，给"一直在动"的心理暗示）
        if self.running and self._prog_target < self._prog_ceiling:
            self._prog_target += (self._prog_ceiling - self._prog_target) * 0.018
        # display 缓动追 target（收尾更顺滑）
        self._prog_display += (self._prog_target - self._prog_display) * 0.20
        self.progress.set(max(0.0, min(1.0, self._prog_display)))
        # 收尾：完成且基本填满则停帧，省 CPU
        if not self.running and self._prog_display >= 0.999:
            self.progress.set(1.0)
            self._anim_on = False
            return
        self.after(30, self._animate_progress)

    # --- 轮询后台输出，刷新 UI（始终在主线程）---
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "line":
                    self._append_log(payload)
                    self._update_progress_from_line(payload)
                elif kind == "error":
                    self._append_log("⚠ " + payload)
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def _update_progress_from_line(self, line):
        m = STEP_RE.search(line)
        if not m:
            return
        cur, total = int(m.group(1)), int(m.group(2))
        if total <= 0:
            return
        # 真实步骤把 target 抬到对应比例（仍夹在 ceiling 下），且只升不降。
        frac = min(self._prog_ceiling, cur / total)
        if frac > self._prog_target:
            self._prog_target = frac

    def _finish(self, returncode):
        # 释放软上限，让缓动把进度顺滑推到 100%（动画帧里到 1.0 自动停）。
        self.running = False
        self._prog_target = 1.0
        self._prog_ceiling = 1.0
        if not getattr(self, "_anim_on", False):
            self._anim_on = True
            self._animate_progress()
        self._set_busy(False)
        if returncode == 0:
            self._append_log(f"=== 任务完成（退出码 0）===")
            self._append_log(f"=== 请稍等，文件资源管理器重启可能需要一点时间 ===")
        else:
            self._append_log(f"=== 任务失败（退出码 {returncode}）===")


def main():
    if not is_admin():
        # 未提权：触发 UAC 重新以管理员启动，然后退出当前实例。
        elevate_and_exit()
        return
    app = MahiroEdgeApp()
    app.mainloop()


if __name__ == "__main__":
    main()
