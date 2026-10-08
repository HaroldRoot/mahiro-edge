# Status.ps1 — 只读诊断：供 GUI 和命令行显示 MahiroEdge 的实际保护状态。
# 不需要管理员权限；-AsJson 时 stdout 只输出一行 JSON，方便调用方稳定解析。

param([switch]$AsJson)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$base = "$env:ProgramData\MahiroEdge"
$module = Join-Path $here 'MahiroEdge.psm1'
if (-not (Test-Path -LiteralPath $module)) {
    $module = Join-Path $base 'MahiroEdge.psm1'
}

function Get-TaskHealth {
    param([string]$Name)
    try {
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction SilentlyContinue
        $infoReadable = $null -ne $info
        $actionText = (@($task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ')
        $enabled = [bool]$task.Settings.Enabled
        $correctAction = if ($Name -eq 'MahiroEdgeIconGuard') {
            $actionText -match 'Apply\.ps1'
        } else {
            $actionText -match 'run-hidden\.vbs'
        }
        $processAlive = $false
        if ($Name -eq 'MahiroEdgeIconRuntime') {
            try {
                $sessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
                $processAlive = [bool](Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction Stop |
                    Where-Object { $_.SessionId -eq $sessionId -and $_.CommandLine -and $_.CommandLine -match 'IconEnforcer\.ps1' } | Select-Object -First 1)
            } catch { $processAlive = $false }
        }
        $notYetRunResult = 0x41303 # Task Scheduler: task has not yet run.
        $sentinelTime = $infoReadable -and ($null -eq $info.LastRunTime -or
            $info.LastRunTime -eq [datetime]::MinValue -or $info.LastRunTime.Year -le 1999)
        $neverRun = $infoReadable -and ($info.LastTaskResult -eq $notYetRunResult -or
            ($sentinelTime -and ($null -eq $info.LastTaskResult -or $info.LastTaskResult -eq 0)))
        $lastResultOk = $infoReadable -and ($neverRun -or $info.LastTaskResult -eq 0)
        $healthy = $enabled -and $correctAction
        if ($Name -eq 'MahiroEdgeIconGuard') {
            $healthy = $healthy -and $lastResultOk -and ([string]$task.State -in @('Ready', 'Running'))
        } else {
            $healthy = $healthy -and $processAlive
        }
        return [ordered]@{
            Exists = $true
            State = [string]$task.State
            Enabled = $enabled
            CorrectAction = [bool]$correctAction
            ProcessAlive = $processAlive
            Healthy = [bool]$healthy
            LastRunTime = if ($info) { $info.LastRunTime } else { $null }
            LastTaskResult = if ($info) { $info.LastTaskResult } else { $null }
        }
    } catch {
        return [ordered]@{ Exists = $false; State = 'Missing'; Enabled = $false; CorrectAction = $false; ProcessAlive = $false; Healthy = $false; LastRunTime = $null; LastTaskResult = $null }
    }
}

$result = [ordered]@{
    Ok = $true
    InstallStaged = (Test-Path -LiteralPath (Join-Path $base 'oyama-mahiro-ahoge.ico'))
    ExeTotal = 0
    ExeMarked = 0
    ExeDiscovered = 0
    ExeNoIcon = 0
    AllExesPatched = $false
    ProfileIconTotal = 0
    ProfileIconPatched = 0
    AllProfileIconsPatched = $false
    GuardTask = Get-TaskHealth 'MahiroEdgeIconGuard'
    RuntimeTask = Get-TaskHealth 'MahiroEdgeIconRuntime'
    Error = $null
}

try {
    if (-not (Test-Path -LiteralPath $module)) { throw "找不到 MahiroEdge 模块: $module" }
    Import-Module $module -Force
    $exes = @(Find-EdgeExecutables)
    $patchableExes = @($exes | Where-Object { Test-HasIconResources -ExePath $_ })
    $result.ExeDiscovered = $exes.Count
    $result.ExeNoIcon = $exes.Count - $patchableExes.Count
    $result.ExeTotal = $patchableExes.Count
    $result.ExeMarked = @($patchableExes | Where-Object { Test-IsPatched -ExePath $_ }).Count
    $result.AllExesPatched = ($result.ExeTotal -gt 0 -and $result.ExeMarked -eq $result.ExeTotal)
    # 非公开 helper；在同一模块会话中可读取，用于只读状态展示。
    $profileIcons = @(Find-EdgeProfileIcons)
    $result.ProfileIconTotal = $profileIcons.Count
    $icoPath = Join-Path $base 'oyama-mahiro-ahoge.ico'
    if (Test-Path -LiteralPath $icoPath) {
        $icoBytes = [System.IO.File]::ReadAllBytes($icoPath)
        $result.ProfileIconPatched = @($profileIcons | Where-Object { Test-ProfileIconApplied -IcoTarget $_ -IcoBytes $icoBytes }).Count
        $result.AllProfileIconsPatched = ($result.ProfileIconPatched -eq $result.ProfileIconTotal)
    }
} catch {
    $result.Ok = $false
    $result.Error = $_.Exception.Message
}

if ($AsJson) {
    $result | ConvertTo-Json -Compress -Depth 4
    exit 0
}

Write-Host "MahiroEdge 状态"
Write-Host ("可补丁 EXE 资源标记: {0}/{1}（无图标资源跳过 {2}，共发现 {3}）" -f `
    $result.ExeMarked, $result.ExeTotal, $result.ExeNoIcon, $result.ExeDiscovered)
Write-Host ("配置图标: {0}" -f $result.ProfileIconTotal)
Write-Host ("自愈任务: {0}" -f $result.GuardTask.State)
Write-Host ("运行时任务: {0}" -f $result.RuntimeTask.State)
if (-not $result.Ok) { Write-Warning $result.Error }
