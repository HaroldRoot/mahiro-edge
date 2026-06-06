# -*- coding: utf-8 -*-
"""打包 GUI 为单文件 exe（PyInstaller onefile）。

用法：.venv\\Scripts\\python.exe build.py

产物：dist/绪山真寻.exe —— 双击即用（自带 UAC 提权 manifest）。
所有资源（start-screen.png、两个 .ico、src/*.ps1）随包内置，解包到运行时
临时目录后，路径布局与项目根一致，因此 Install.ps1 的 $repo / src 解析照常工作。

环境要求：用官方 python.org 的 CPython 建 .venv，
不要用 Anaconda——conda 把 Tcl/libffi 等原生依赖散放在 venv 外的非标准位置，
PyInstaller 追踪不到，会反复出现 DLL load failed / Tcl 版本冲突。官方 CPython
自洽，无需手工补 DLL。
"""
import os
import sys
import shutil
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
SEP = ";"  # Windows 上 --add-data 的 源;目标 分隔符
APP_NAME = "绪山真寻"


def data(src_rel, dest_rel):
    """构造一个 --add-data 参数；dest_rel 为包内目标目录（'.' 表示根）。"""
    return f"{os.path.join(ROOT, src_rel)}{SEP}{dest_rel}"


def find_upx():
    """探测 UPX（可执行文件压缩器）。装了就用，能再砍 30%~50% 体积；没有则跳过。

    优先 PATH，其次项目根下的 upx/ 目录。返回 UPX 所在目录或 None。"""
    exe = shutil.which("upx")
    if exe:
        return os.path.dirname(exe)
    local = os.path.join(ROOT, "upx")
    if os.path.isdir(local) and any(
        n.lower() == "upx.exe" for n in os.listdir(local)
    ):
        return local
    return None


def main():
    entry = os.path.join(ROOT, "gui", "MahiroEdgeGUI.py")
    icon = os.path.join(ROOT, "oyama-mahiro-ahoge.ico")

    adds = [
        data("start-screen.png", "."),
        data("oyama-mahiro-ahoge.ico", "."),
        data("oyama-mahiro-ahoge-rotated.ico", "."),
        data("Edge Profile.ico", "."),
        data(os.path.join("src", "Install.ps1"), "src"),
        data(os.path.join("src", "Uninstall.ps1"), "src"),
        data(os.path.join("src", "Apply.ps1"), "src"),
        data(os.path.join("src", "IconEnforcer.ps1"), "src"),
        data(os.path.join("src", "run-hidden.vbs"), "src"),
        data(os.path.join("src", "MahiroEdge.psm1"), "src"),
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--windowed",            # 无控制台窗口（GUI）
        "--uac-admin",           # manifest 请求管理员（双击即提权）
        "--name", APP_NAME,
        "--icon", icon,
        "--optimize", "2",       # 等价 python -OO：剥离断言与 docstring
        # 只收 customtkinter 的数据资源（主题 JSON / 字体），不拉二进制，体积更小
        "--collect-data", "customtkinter",
    ]

    # 排除用不到的大块/无关模块，进一步瘦身（极简 venv 下多数本就不存在，留作保险）。
    excludes = [
        "numpy", "scipy", "pandas", "matplotlib",      # 科学栈
        "PySide6", "PyQt5", "PyQt6", "wx",             # 其它 GUI 框架
        "pytest", "test", "unittest", "tkinter.test",  # 测试
        "pydoc", "doctest", "lib2to3", "pip", "setuptools",  # 开发期工具
        "sqlite3", "email", "html", "http", "xmlrpc",  # GUI 用不到的标准库
        "PIL.ImageQt",                                  # 拉 Qt 的 Pillow 桥接
    ]
    for m in excludes:
        cmd += ["--exclude-module", m]

    # 有 UPX 就启用（对 onefile 的内嵌 DLL/pyd 逐个压缩）；否则跳过。
    upx_dir = find_upx()
    if upx_dir:
        cmd += ["--upx-dir", upx_dir]
        # tcl/tk 与 vcruntime 用 UPX 压会偶发损坏或被杀软误报，排除掉更稳。
        for n in ("vcruntime140.dll", "tcl86t.dll", "tk86t.dll",
                  "python3.dll", "_tkinter.pyd"):
            cmd += ["--upx-exclude", n]
        print(f"已启用 UPX 压缩: {upx_dir}")
    else:
        print("[INFO] 未发现 UPX，跳过可执行体压缩。"
              "装一个 upx 放进 PATH 或 ./upx 可再砍约 30% 体积。")

    for a in adds:
        cmd += ["--add-data", a]
    cmd.append(entry)

    print("运行 PyInstaller:\n  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc == 0:
        out = os.path.join(ROOT, "dist", APP_NAME + ".exe")
        size_mb = os.path.getsize(out) / (1024 * 1024) if os.path.isfile(out) else 0
        print(f"\n[OK] 打包完成: {out}  ({size_mb:.1f} MB)")
    else:
        print(f"\n[FAIL] 打包失败，退出码 {rc}")
    sys.exit(rc)


if __name__ == "__main__":
    main()
