# Uninstall.ps1 — 卸载：删除计划任务 → 关闭 Edge → 从 .bak 还原所有 exe → 清理 ProgramData → 刷新缓存
# 需管理员权限。

$ErrorActionPreference = 'Stop'

$base     = "$env:ProgramData\MahiroEdge"
$module   = Join-Path $base 'MahiroEdge.psm1'
$taskName = 'MahiroEdgeIconGuard'
$runtimeTaskName = 'MahiroEdgeIconRuntime'
$icoName  = 'Edge Profile.ico'   # 项目自带的原版配置文件图标（无 .bak 时兜底还原用）

function Assert-Admin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object System.Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "需要管理员权限。请右键 Uninstall.cmd 选择「以管理员身份运行」喵。"
    }
}

Write-Host "=== 绪山真寻 Edge 图标卸载 ===" -ForegroundColor Magenta
Assert-Admin

# --- 1) 停止常驻运行时任务并删除两个计划任务 ---
Write-Host "[1/4] 删除计划任务喵 ..."
# 先停掉常驻 enforcer（否则它会在我们还原后又把窗口图标改回呆毛）。
Stop-ScheduledTask  -TaskName $runtimeTaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $runtimeTaskName -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName        -Confirm:$false -ErrorAction SilentlyContinue
# enforcer 进程可能仍在跑：通过命令行特征结束它（避免误杀其它 powershell）。
try {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'IconEnforcer\.ps1' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
} catch {}

# --- 2) 关闭 Edge（解除占用以便还原 exe）---
Write-Host "[2/4] 关闭运行中的 Edge ..."
$edgeProcs = @('msedge','msedge_proxy','msedgewebview2','identity_helper','msopdfedge')
foreach ($pn in $edgeProcs) {
    Get-Process -Name $pn -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 800

# --- 3) 从备份还原 ---
Write-Host "[3/4] 恢复原版 Edge 图标喵 ..."
# 优先用常驻模块；若已被删，退回项目内模块
$srcDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path $module)) {
    $localModule = Join-Path $srcDir 'MahiroEdge.psm1'
    if (Test-Path $localModule) { $module = $localModule }
}

# 定位兜底原版图标：先看常驻目录，再看项目根（src 的上一级）。
# 当某配置文件的 Edge Profile.ico 没有 .mahiro.bak 时，用它还原，确保任何机器都能复原。
$fallbackIco = $null
foreach ($cand in @((Join-Path $base $icoName), (Join-Path (Split-Path -Parent $srcDir) $icoName))) {
    if (Test-Path -LiteralPath $cand) { $fallbackIco = $cand; break }
}

if (Test-Path $module) {
    Import-Module $module -Force
    $r = Invoke-Restore -FallbackProfileIco $fallbackIco
    Write-Host ("exe: 还原={0} 无备份={1} 失败={2} 清理孤儿备份={3}" -f $r.Restored, $r.NoBackup, $r.Failed, $r.OrphanCleaned) -ForegroundColor Cyan
    Write-Host ("配置图标: 还原={0} 兜底还原={1} 无备份={2} 失败={3}" -f `
        $r.ProfileRestored, $r.ProfileFallback, $r.ProfileNoBackup, $r.ProfileFailed) -ForegroundColor Cyan
    # 边界情形：什么都没还原（exe 没补丁、配置图标也没换）——多半本来就是原版。如实告知，不假装“已恢复”。
    if ($r.Restored -eq 0 -and $r.ProfileRestored -eq 0 -and $r.ProfileFallback -eq 0) {
        Write-Host "未发现任何呆毛补丁痕迹：Edge 图标当前已是原版，无需还原喵～" -ForegroundColor Yellow
        $script:NothingRestored = $true
    }
    if (-not $fallbackIco) {
        Write-Warning "未找到兜底原版图标 '$icoName'；没有 .bak 的配置文件图标无法还原喵。"
    }
    Clear-IconCache -RestartExplorer
} else {
    Write-Warning "找不到模块，无法自动还原喵。请手动将各 msedge.exe.mahiro.bak 改回 msedge.exe。"
}

# --- 4) 清理常驻目录 ---
Write-Host "[4/4] 清理 $base ..."
Remove-Item -LiteralPath $base -Recurse -Force -ErrorAction SilentlyContinue

if ($script:NothingRestored) {
    Write-Host "卸载完成（图标本就是原版，已确保清理干净喵）～" -ForegroundColor Green
} else {
    Write-Host "卸载完成，Edge 图标已恢复原版喵～" -ForegroundColor Green
}
