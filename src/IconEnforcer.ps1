# IconEnforcer.ps1 — 常驻运行时图标强制器（在交互用户会话中运行）
#
# 为什么需要它：现代 Chromium Edge 的“窗口图标”是在运行时由其自带的品牌资源
# （.pak）动态设置的，与 PE 文件里被我们改写的 RT_GROUP_ICON 无关。因此：
#   - 任务栏「从不合并/显示标签」时，Windows 实时读窗口图标 → 仍是原版蓝绿色；
#   - `flutter run` 等以 app 模式拉起的 Edge 窗口同理。
# 改写 exe 资源（Install/Apply 做的事）只修好“静态/快捷方式”图标，碰不到运行时窗口图标。
#
# 本脚本做的事：把粉色呆毛 .ico 载入为 HICON，常驻轮询所有 Edge 顶层窗口，
# 对图标还不是我们的窗口发 WM_SETICON。窗口图标是 per-window 运行时状态、
# 新窗口会重置，故必须常驻（由计划任务 MahiroEdgeIconRuntime 在登录时拉起）。
#
# 以“当前交互用户”身份运行（不是 SYSTEM）：session 0 的服务无法向用户桌面窗口
# 发消息；两端同为中完整性级别，UIPI 不拦截，无需提权。
param(
    [string]$IcoPath = "$env:ProgramData\MahiroEdge\oyama-mahiro-ahoge.ico",
    [int]$IntervalMs = 1500
)

$ErrorActionPreference = 'Stop'

# 立刻隐藏本进程的控制台窗口。powershell.exe 是控制台子系统程序，Windows 会在
# PowerShell 能处理 -WindowStyle Hidden 之前就先分配一个控制台窗口；而本脚本是
# 无限循环常驻，那个空窗口便会一直挂在桌面/任务栏上。故从进程内部自行隐藏，
# 不依赖启动方式（计划任务交互式拉起时 -WindowStyle Hidden 并不可靠）。
try {
    Add-Type -Name Win -Namespace ConHide -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern System.IntPtr GetConsoleWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
'@
    $h = [ConHide.Win]::GetConsoleWindow()
    if ($h -ne [IntPtr]::Zero) { [void][ConHide.Win]::ShowWindow($h, 0) }  # SW_HIDE = 0
} catch {}

# 单实例：命名互斥体。已有实例在跑就安静退出（计划任务可能重复触发）。
$mutexName = 'Global\MahiroEdgeIconRuntime'
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) { return }

if (-not (Test-Path -LiteralPath $IcoPath)) {
    throw "图标文件不存在: $IcoPath"
}

# ============================================================
# 内嵌 C#：载入 .ico 为 HICON + 枚举 Edge 顶层窗口并打图标。
# 用 SendMessageTimeout(SMTO_ABORTIFHUNG) 防个别无响应窗口卡死轮询。
# 通过外部传入的目标 PID 集合判定“是不是 Edge 窗口”，避免逐窗口查进程名（慢）。
# ============================================================
$cs = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class EdgeIcon {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern IntPtr LoadImage(IntPtr hinst, string name, uint type, int cx, int cy, uint fuLoad);
    [DllImport("user32.dll")]
    public static extern bool DestroyIcon(IntPtr h);

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr l);
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")]
    public static extern int GetWindowTextLength(IntPtr h);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")]
    public static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, IntPtr w, IntPtr l, uint flags, uint timeout, out IntPtr res);

    const uint IMAGE_ICON = 1, LR_LOADFROMFILE = 0x10, LR_DEFAULTCOLOR = 0;
    const uint WM_SETICON = 0x80, WM_GETICON = 0x7F;
    const uint SMTO_ABORTIFHUNG = 0x2;
    static readonly IntPtr ICON_SMALL = (IntPtr)0, ICON_BIG = (IntPtr)1;

    public static IntPtr LoadIco(string path, int sz) {
        return LoadImage(IntPtr.Zero, path, IMAGE_ICON, sz, sz, LR_LOADFROMFILE | LR_DEFAULTCOLOR);
    }

    // 对一批目标 PID 拥有的可见、带标题的顶层窗口打图标。返回本轮新打的窗口数。
    public static int Enforce(int[] pids, IntPtr hBig, IntPtr hSmall) {
        var set = new HashSet<uint>();
        foreach (int p in pids) set.Add((uint)p);
        int stamped = 0;
        EnumWindowsProc cb = (h, l) => {
            if (!IsWindowVisible(h)) return true;
            if (GetWindowTextLength(h) == 0) return true;
            uint pid; GetWindowThreadProcessId(h, out pid);
            if (!set.Contains(pid)) return true;
            IntPtr cur;
            SendMessageTimeout(h, WM_GETICON, ICON_BIG, IntPtr.Zero, SMTO_ABORTIFHUNG, 200, out cur);
            if (cur == hBig) return true;   // 已是我们的图标，跳过
            IntPtr r;
            SendMessageTimeout(h, WM_SETICON, ICON_BIG,   hBig,   SMTO_ABORTIFHUNG, 200, out r);
            SendMessageTimeout(h, WM_SETICON, ICON_SMALL, hSmall, SMTO_ABORTIFHUNG, 200, out r);
            stamped++;
            return true;
        };
        EnumWindows(cb, IntPtr.Zero);
        return stamped;
    }
}
'@
Add-Type -TypeDefinition $cs -Language CSharp

# 载入大小两种尺寸的 HICON（进程存活期间常驻，不销毁）。
$hBig   = [EdgeIcon]::LoadIco($IcoPath, 32)
$hSmall = [EdgeIcon]::LoadIco($IcoPath, 16)
if ($hBig -eq [IntPtr]::Zero -or $hSmall -eq [IntPtr]::Zero) {
    throw "载入图标失败: $IcoPath"
}

# Edge 窗口所属的进程名（浏览器 + app 代理 + PWA 启动器）。
$edgeProcNames = @('msedge', 'msedge_proxy', 'msedge_pwa_launcher')

# 常驻轮询：每 $IntervalMs 取一次目标 PID 集合，交给 C# 批量打图标。
try {
    while ($true) {
        $pids = @()
        foreach ($n in $edgeProcNames) {
            $pids += (Get-Process -Name $n -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
        }
        if ($pids.Count -gt 0) {
            [void][EdgeIcon]::Enforce([int[]]$pids, $hBig, $hSmall)
        }
        Start-Sleep -Milliseconds $IntervalMs
    }
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
