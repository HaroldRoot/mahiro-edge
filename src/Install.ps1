# Install.ps1 — 安装：暂存文件 → 关闭 Edge → 打补丁 → 注册自愈计划任务 → 刷新图标缓存
# 需管理员权限运行（由 Install.cmd 自我提权后调用，或手动以管理员身份运行）。
#
# -Variant：选择呆毛图标变体。
#   default = oyama-mahiro-ahoge.ico        （角度与原版 Edge 一致）
#   rotated = oyama-mahiro-ahoge-rotated.ico（角度更符合呆毛特征）
# 为空时：交互式会话弹中文菜单（默认回车=default）；非交互直接回退 default。
# 关键：无论选哪个，都暂存为稳定名 oyama-mahiro-ahoge.ico —— 这样自愈任务
# Apply.ps1 与卸载 Uninstall.ps1 无需改动即可沿用所选图标。
param(
    [ValidateSet('default', 'rotated', '')]
    [string]$Variant = ''
)

$ErrorActionPreference = 'Stop'

# 脚本所在目录（源），与稳定常驻目录（目标）
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo    = Split-Path -Parent $here          # 项目根（src 的上一级）
$base    = "$env:ProgramData\MahiroEdge"
$taskName = 'MahiroEdgeIconGuard'      # 自愈：SYSTEM 改写 exe 资源（登录 + 每日）
$runtimeTaskName = 'MahiroEdgeIconRuntime'  # 常驻：交互用户向 Edge 窗口打运行时图标

function Assert-Admin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object System.Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "需要管理员权限。请右键 Install.cmd 选择「以管理员身份运行」。"
    }
}

Write-Host "=== 绪山真寻 Edge 图标安装 ===" -ForegroundColor Magenta
Assert-Admin

# --- 0) 解析图标变体 ---
# 变体 → 源文件名映射。default 是原版角度，rotated 是更符合呆毛的角度。
$variantFiles = @{
    'default' = 'oyama-mahiro-ahoge.ico'
    'rotated' = 'oyama-mahiro-ahoge-rotated.ico'
}
if (-not $Variant) {
    # 仅在交互式会话弹菜单；非交互（GUI 传参/管道）静默回退 default，绝不卡住。
    if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
        Write-Host ""
        Write-Host "请选择呆毛图标变体:" -ForegroundColor Yellow
        Write-Host "  1) 原版角度（与原版 Edge 图标一致）  [默认]"
        Write-Host "  2) 呆毛角度（角度更符合呆毛特征）"
        $choice = Read-Host "输入 1 或 2，直接回车选 1"
        $Variant = if ($choice.Trim() -eq '2') { 'rotated' } else { 'default' }
    } else {
        $Variant = 'default'
    }
}
$icoFileName = $variantFiles[$Variant]
Write-Host "已选择图标变体: $Variant ($icoFileName)" -ForegroundColor Cyan

# --- 1) 暂存图标 + 模块 + Apply.ps1 + IconEnforcer.ps1 到 ProgramData（稳定路径，供计划任务长期使用）---
Write-Host "[1/6] 暂存文件到 $base ..."
New-Item -ItemType Directory -Path $base -Force | Out-Null

$icoSrc = Join-Path $repo $icoFileName
if (-not (Test-Path $icoSrc)) { throw "找不到图标文件: $icoSrc" }
# 目标写死稳定名（不随变体变）：Apply.ps1 / Uninstall.ps1 只认这个名字。
Copy-Item -LiteralPath $icoSrc                 -Destination (Join-Path $base 'oyama-mahiro-ahoge.ico') -Force
Copy-Item -LiteralPath (Join-Path $here 'MahiroEdge.psm1')  -Destination (Join-Path $base 'MahiroEdge.psm1')   -Force
Copy-Item -LiteralPath (Join-Path $here 'Apply.ps1')        -Destination (Join-Path $base 'Apply.ps1')         -Force
Copy-Item -LiteralPath (Join-Path $here 'IconEnforcer.ps1') -Destination (Join-Path $base 'IconEnforcer.ps1')  -Force
# VBS 启动垫片：经 wscript.exe（非控制台程序）拉起 enforcer，彻底不产生控制台窗口。
# 见 run-hidden.vbs 头注释——Windows Terminal 作默认终端时，控制台会被交接到独立的
# WindowsTerminal.exe 窗口进程，enforcer 自身的 ShowWindow(SW_HIDE) 无能为力。
Copy-Item -LiteralPath (Join-Path $here 'run-hidden.vbs')   -Destination (Join-Path $base 'run-hidden.vbs')    -Force

# 暂存原版 Edge Profile.ico（供卸载时对无 .bak 的配置文件图标兜底还原）。缺失不致命。
$origIcoSrc = Join-Path $repo 'Edge Profile.ico'
if (Test-Path -LiteralPath $origIcoSrc) {
    Copy-Item -LiteralPath $origIcoSrc -Destination (Join-Path $base 'Edge Profile.ico') -Force
} else {
    Write-Warning "未找到原版 'Edge Profile.ico'；卸载时将只能依赖 .bak 还原配置文件图标。"
}

$module  = Join-Path $base 'MahiroEdge.psm1'
$icoPath = Join-Path $base 'oyama-mahiro-ahoge.ico'
$applyPs = Join-Path $base 'Apply.ps1'
$enforcerVbs = Join-Path $base 'run-hidden.vbs'
Import-Module $module -Force

# --- 2) 关闭所有 Edge 进程（解除 exe 文件占用，否则无法写资源）---
Write-Host "[2/6] 关闭运行中的 Edge ..."
$edgeProcs = @('msedge','msedge_proxy','msedgewebview2','identity_helper','msopdfedge')
foreach ($pn in $edgeProcs) {
    Get-Process -Name $pn -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_.CloseMainWindow() | Out-Null } catch {}
    }
}
Start-Sleep -Seconds 1
foreach ($pn in $edgeProcs) {
    Get-Process -Name $pn -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 800

# --- 3) 打补丁 ---
Write-Host "[3/6] 改写 Edge 图标资源 ..."
# 边界情形预判：补丁前先看是否所有 exe 早已是呆毛（重复安装）。用 -Force 仍会重写，
# 但据此给用户一句明确的“本来就是呆毛”提示，而不是默默重跑。
$preExes = Find-EdgeExecutables
$preAll  = ($preExes.Count -gt 0) -and -not ($preExes | Where-Object { -not (Test-IsPatched -ExePath $_) })
$r = Invoke-Patch -IcoPath $icoPath -Force
Write-Host ("补丁={0} 跳过={1} 失败={2} 共发现={3}" -f $r.Patched, $r.Skipped, $r.Failed, $r.Total) -ForegroundColor Cyan
if ($preAll) {
    Write-Host "（检测到 Edge 图标此前已是呆毛，本次为重新确保应用——这是正常的幂等行为。）" -ForegroundColor Yellow
}
if ($r.Patched -eq 0 -and $r.Failed -gt 0) {
    Write-Warning "没有任何 exe 被成功补丁。请确认 Edge 已完全关闭后重试。"
}

# --- 4) 注册自愈计划任务（登录时 + 每日 03:00），SYSTEM + 最高权限 ---
Write-Host "[4/6] 注册自愈计划任务 '$taskName' ..."
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
           -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$applyPs`""
$trigLogon = New-ScheduledTaskTrigger -AtLogOn
$trigDaily = New-ScheduledTaskTrigger -Daily -At 3am
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger @($trigLogon, $trigDaily) -Principal $principal -Settings $settings `
    -Description '保持 Microsoft Edge 图标为绪山真寻粉色呆毛（Edge 更新后自动重新应用）' | Out-Null

# --- 5) 注册运行时图标常驻任务（登录时拉起，常驻轮询），当前交互用户身份 ---
# 为什么是“当前用户”而非 SYSTEM：session 0 的 SYSTEM 进程无法向用户桌面窗口发
# WM_SETICON。本任务在用户登录会话内常驻，给每个 Edge 窗口实时打上呆毛运行时图标，
# 修好「任务栏展开标签」「flutter run 起的 app 窗口」等 exe 资源补丁碰不到的场景。
Write-Host "[5/6] 注册运行时图标任务 '$runtimeTaskName' 并启动 ..."
try {
    $curUser = "$env:USERDOMAIN\$env:USERNAME"
    # 经 wscript.exe 拉起 VBS 垫片，再由垫片隐藏式启动 enforcer。wscript 非控制台程序，
    # 不产生控制台、也不触发 Windows Terminal 交接，故无空窗口。
    $rtAction = New-ScheduledTaskAction -Execute 'wscript.exe' `
                -Argument "`"$enforcerVbs`""
    $rtTrigger = New-ScheduledTaskTrigger -AtLogOn -User $curUser
    # 交互令牌运行（能摸到用户窗口）；不要最高权限（避免 UIPI 完整性级别错配反而发不进）。
    $rtPrincipal = New-ScheduledTaskPrincipal -UserId $curUser -LogonType Interactive -RunLevel Limited
    # 常驻进程：取消执行时限，关掉“空闲即停/电池即停”，允许与已有实例并存（互斥体兜底单实例）。
    $rtSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                  -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Unregister-ScheduledTask -TaskName $runtimeTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $runtimeTaskName -Action $rtAction `
        -Trigger $rtTrigger -Principal $rtPrincipal -Settings $rtSettings `
        -Description '常驻：把运行中的 Edge 窗口图标实时替换为绪山真寻粉色呆毛' | Out-Null
    Start-ScheduledTask -TaskName $runtimeTaskName -ErrorAction SilentlyContinue  # 本次会话立即生效，无需重新登录
} catch {
    Write-Warning "运行时图标任务注册失败（不影响静态图标）：$($_.Exception.Message)"
}

# --- 6) 刷新图标缓存，立即可见 ---
Write-Host "[6/6] 刷新图标缓存 ..."
Clear-IconCache -RestartExplorer

Write-Host "安装完成！桌面/任务栏的 Edge 图标现在应是粉色呆毛。" -ForegroundColor Green
