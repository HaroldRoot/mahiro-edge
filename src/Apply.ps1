# Apply.ps1 — 计划任务执行体（常驻 C:\ProgramData\MahiroEdge\）
# 幂等：发现所有 Edge exe，对未打补丁的（含 Edge 更新后新出的版本目录）重新应用呆毛图标。
# 由计划任务 MahiroEdgeIconGuard 在登录时 + 每日触发。

$ErrorActionPreference = 'Stop'

$base    = "$env:ProgramData\MahiroEdge"
$module  = Join-Path $base 'MahiroEdge.psm1'
$icoPath = Join-Path $base 'oyama-mahiro-ahoge.ico'
$logFile = Join-Path $base 'apply.log'

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    try { Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8 } catch {}
}

try {
    if (-not (Test-Path $module))  { throw "模块缺失: $module" }
    if (-not (Test-Path $icoPath)) { throw "图标缺失: $icoPath" }

    Import-Module $module -Force
    $r = Invoke-Patch -IcoPath $icoPath   # 非 Force：已打补丁的自动跳过

    Write-Log ("apply 完成: 补丁={0} 跳过={1} 失败={2} 共={3}" -f $r.Patched, $r.Skipped, $r.Failed, $r.Total)

    # 仅当本轮真正改动了 exe，才刷新图标缓存（避免每天无谓重启 explorer）
    if ($r.Patched -gt 0) {
        Clear-IconCache -RestartExplorer
        Write-Log "本轮有新补丁，已刷新图标缓存"
    }
    exit 0
}
catch {
    Write-Log ("apply 异常: " + $_.Exception.Message)
    exit 1
}
