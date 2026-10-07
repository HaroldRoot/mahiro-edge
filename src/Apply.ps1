# Apply.ps1 — 计划任务执行体（常驻 C:\ProgramData\MahiroEdge\）
# 幂等：发现所有 Edge exe，对未打补丁的（含 Edge 更新后新出的版本目录）重新应用呆毛图标。
# 由计划任务 MahiroEdgeIconGuard 在登录时 + 每日触发。
#
# 本计划任务以 SYSTEM 运行，不操作任何用户的图标缓存或 Explorer。
# 缓存刷新由交互式安装/卸载会话处理；登录时若仍在开机启动窗口内也会跳过，
# 避免重启 Explorer 丢掉尚未执行的 HKLM\...\Run 启动项。

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

    # EXE 或每配置文件图标有变动时才刷新缓存，避免配置图标更新后仍显示旧任务栏图标。
    # Clear-IconCache 会跳过 SYSTEM/session 0；交互会话还会遵守开机启动窗口并
    # 仅处理当前 session 的 Explorer。按运行上下文记录实际跳过原因或刷新结果。
    if (($r.Patched + $r.ProfilePatched) -gt 0) {
        $sessionId = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
        $isSystemContext = ([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq 'S-1-5-18') -or ($sessionId -eq 0)
        $pidBefore = @(Get-Process -Name explorer -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $sessionId } | Select-Object -ExpandProperty Id)
        Clear-IconCache -RestartExplorer
        $pidAfter = @(Get-Process -Name explorer -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $sessionId } | Select-Object -ExpandProperty Id)
        if ($isSystemContext) {
            Write-Log "本轮有新补丁；SYSTEM/session 0 未清理用户缓存或重启 explorer，需在交互式安装/会话中刷新"
        } elseif (($pidBefore -join ',') -ne ($pidAfter -join ',')) {
            Write-Log "本轮有新补丁，已刷新图标缓存并重启 explorer"
        } elseif ((Get-SystemUptimeMinutes) -lt 10) {
            Write-Log ("本轮有新补丁（开机 {0:N1} 分钟）；处于启动窗口内，缓存刷新延后至交互式会话" -f (Get-SystemUptimeMinutes))
        } elseif ($pidBefore.Count -eq 0) {
            Write-Log "本轮有新补丁；当前 session 没有运行中的 explorer，已清理缓存"
        } else {
            Write-Log "本轮有新补丁；缓存清理已执行，但当前 session 的 explorer 重启未确认"
        }
    }
    if (($r.Failed + $r.ProfileFailed) -gt 0) {
        Write-Log "apply 部分失败"
        exit 2
    }
    exit 0
}
catch {
    Write-Log ("apply 异常: " + $_.Exception.Message)
    exit 1
}
