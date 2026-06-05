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
$taskName = 'MahiroEdgeIconGuard'

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

# --- 1) 暂存图标 + 模块 + Apply.ps1 到 ProgramData（稳定路径，供计划任务长期使用）---
Write-Host "[1/5] 暂存文件到 $base ..."
New-Item -ItemType Directory -Path $base -Force | Out-Null

$icoSrc = Join-Path $repo $icoFileName
if (-not (Test-Path $icoSrc)) { throw "找不到图标文件: $icoSrc" }
# 目标写死稳定名（不随变体变）：Apply.ps1 / Uninstall.ps1 只认这个名字。
Copy-Item -LiteralPath $icoSrc                 -Destination (Join-Path $base 'oyama-mahiro-ahoge.ico') -Force
Copy-Item -LiteralPath (Join-Path $here 'MahiroEdge.psm1') -Destination (Join-Path $base 'MahiroEdge.psm1') -Force
Copy-Item -LiteralPath (Join-Path $here 'Apply.ps1')       -Destination (Join-Path $base 'Apply.ps1')       -Force

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
Import-Module $module -Force

# --- 2) 关闭所有 Edge 进程（解除 exe 文件占用，否则无法写资源）---
Write-Host "[2/5] 关闭运行中的 Edge ..."
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
Write-Host "[3/5] 改写 Edge 图标资源 ..."
$r = Invoke-Patch -IcoPath $icoPath -Force
Write-Host ("补丁={0} 跳过={1} 失败={2} 共发现={3}" -f $r.Patched, $r.Skipped, $r.Failed, $r.Total) -ForegroundColor Cyan
if ($r.Patched -eq 0 -and $r.Failed -gt 0) {
    Write-Warning "没有任何 exe 被成功补丁。请确认 Edge 已完全关闭后重试。"
}

# --- 4) 注册自愈计划任务（登录时 + 每日 03:00），SYSTEM + 最高权限 ---
Write-Host "[4/5] 注册自愈计划任务 '$taskName' ..."
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

# --- 5) 刷新图标缓存，立即可见 ---
Write-Host "[5/5] 刷新图标缓存 ..."
Clear-IconCache -RestartExplorer

Write-Host "安装完成！桌面/任务栏的 Edge 图标现在应是粉色呆毛。" -ForegroundColor Green
