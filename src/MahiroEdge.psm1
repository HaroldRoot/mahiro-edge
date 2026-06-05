# MahiroEdge.psm1 — 核心模块：发现 Edge、解析 .ico、改写 PE 图标资源、还原、刷新缓存
# 所有公开函数：Find-EdgeExecutables / Invoke-Patch / Invoke-Restore / Clear-IconCache / Test-IsPatched

$ErrorActionPreference = 'Stop'

# ============================================================
# 内嵌 C#：Win32 资源 API（枚举 + 写入 RT_ICON / RT_GROUP_ICON）
# ============================================================
$cs = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class ResApi {
    [DllImport("kernel32", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern IntPtr LoadLibraryEx(string f, IntPtr h, uint flags);
    [DllImport("kernel32", SetLastError=true)]
    public static extern bool FreeLibrary(IntPtr h);

    public delegate bool EnumResNameProc(IntPtr h, IntPtr type, IntPtr name, IntPtr l);
    public delegate bool EnumResLangProc(IntPtr h, IntPtr type, IntPtr name, ushort lang, IntPtr l);

    [DllImport("kernel32", SetLastError=true)]
    public static extern bool EnumResourceNames(IntPtr h, IntPtr type, EnumResNameProc cb, IntPtr l);
    [DllImport("kernel32", SetLastError=true)]
    public static extern bool EnumResourceLanguages(IntPtr h, IntPtr type, IntPtr name, EnumResLangProc cb, IntPtr l);

    [DllImport("kernel32", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern IntPtr BeginUpdateResource(string fileName, bool deleteExisting);
    [DllImport("kernel32", SetLastError=true)]
    public static extern bool UpdateResource(IntPtr h, IntPtr type, IntPtr name, ushort lang, byte[] data, uint cb);
    [DllImport("kernel32", SetLastError=true)]
    public static extern bool EndUpdateResource(IntPtr h, bool discard);

    public const uint LOAD_LIBRARY_AS_DATAFILE = 0x2;
    public static readonly IntPtr RT_ICON = (IntPtr)3;
    public static readonly IntPtr RT_GROUP_ICON = (IntPtr)14;
    public static readonly IntPtr RT_RCDATA = (IntPtr)10;

    [DllImport("kernel32", SetLastError=true)]
    public static extern IntPtr FindResource(IntPtr h, IntPtr name, IntPtr type);

    // 补丁标记：存为 RT_RCDATA 下的整数 ID（避免字符串名的编码陷阱）
    public static readonly IntPtr MARKER_ID = (IntPtr)0xE100;

    // 检测补丁标记资源是否存在
    public static bool HasMarker(string path) {
        IntPtr h = LoadLibraryEx(path, IntPtr.Zero, LOAD_LIBRARY_AS_DATAFILE);
        if (h == IntPtr.Zero) return false;
        try {
            IntPtr r = FindResource(h, MARKER_ID, RT_RCDATA);
            return r != IntPtr.Zero;
        } finally { FreeLibrary(h); }
    }

    // 一个图标组的描述：资源名（数字或字符串）+ 该组拥有的语言 ID 列表
    public class GroupInfo {
        public bool IsIntId;
        public ushort IntId;       // 当 IsIntId 为 true
        public string StrId;       // 当 IsIntId 为 false
        public List<ushort> Langs = new List<ushort>();
    }

    // 枚举 EXE 内所有 RT_GROUP_ICON 组及其语言
    public static List<GroupInfo> EnumGroups(string path) {
        var result = new List<GroupInfo>();
        IntPtr h = LoadLibraryEx(path, IntPtr.Zero, LOAD_LIBRARY_AS_DATAFILE);
        if (h == IntPtr.Zero) throw new Exception("LoadLibraryEx failed: " + Marshal.GetLastWin32Error());
        try {
            EnumResNameProc nameCb = (hh, t, n, l) => {
                var gi = new GroupInfo();
                ulong v = (ulong)n.ToInt64();
                if (v < 0x10000) { gi.IsIntId = true; gi.IntId = (ushort)v; }
                else { gi.IsIntId = false; gi.StrId = Marshal.PtrToStringUni(n); }
                EnumResLangProc langCb = (h2, t2, n2, lang, l2) => { gi.Langs.Add(lang); return true; };
                EnumResourceLanguages(hh, RT_GROUP_ICON, n, langCb, IntPtr.Zero);
                result.Add(gi);
                return true;
            };
            EnumResourceNames(h, RT_GROUP_ICON, nameCb, IntPtr.Zero);
        } finally { FreeLibrary(h); }
        return result;
    }
}
'@
Add-Type -TypeDefinition $cs -Language CSharp

# 共享 RT_ICON 资源 ID 起始值（远离 Edge 既有 ID，避免冲突）
$script:IconIdBase = 0xE000

# ============================================================
# 解析 .ico 文件：返回每帧的原始位图字节 + 目录元数据
# .ico 结构：ICONDIR(6) + N x ICONDIRENTRY(16)；每个 entry 末尾 4 字节是
# 该帧位图在文件中的偏移量，前面是 width/height/colors/planes/bitcount/bytesize。
# ============================================================
function Get-IconImagesFromIco {
    param([Parameter(Mandatory)][string]$IcoPath)

    $bytes = [System.IO.File]::ReadAllBytes($IcoPath)
    if ($bytes.Length -lt 6) { throw "ico 文件过小: $IcoPath" }
    $reserved = [BitConverter]::ToUInt16($bytes, 0)
    $type     = [BitConverter]::ToUInt16($bytes, 2)
    $count    = [BitConverter]::ToUInt16($bytes, 4)
    if ($reserved -ne 0 -or $type -ne 1 -or $count -lt 1) {
        throw "不是有效的 .ico 文件: $IcoPath (reserved=$reserved type=$type count=$count)"
    }

    $images = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt $count; $i++) {
        $off = 6 + ($i * 16)
        $width    = $bytes[$off]
        $height   = $bytes[$off + 1]
        $colors   = $bytes[$off + 2]
        # $off+3 reserved
        $planes   = [BitConverter]::ToUInt16($bytes, $off + 4)
        $bitcount = [BitConverter]::ToUInt16($bytes, $off + 6)
        $byteSize = [BitConverter]::ToUInt32($bytes, $off + 8)
        $dataOff  = [BitConverter]::ToUInt32($bytes, $off + 12)

        $img = New-Object byte[] $byteSize
        [Array]::Copy($bytes, [int]$dataOff, $img, 0, [int]$byteSize)

        [void]$images.Add([pscustomobject]@{
            Width = $width; Height = $height; Colors = $colors
            Planes = $planes; BitCount = $bitcount; ByteSize = $byteSize
            Data = $img
        })
    }
    return $images
}

# ============================================================
# 构建 RT_GROUP_ICON 资源体（GRPICONDIR）。
# 结构：6 字节头 + N x 14 字节 GRPICONDIRENTRY。
# 与文件 .ico 的 16 字节 entry 不同：末尾是 2 字节的 RT_ICON 资源 ID（非文件偏移）。
# $images 为 Get-IconImagesFromIco 结果；$ids 为对应分配的 RT_ICON 资源 ID 列表。
# ============================================================
function New-GroupIconBytes {
    param([Parameter(Mandatory)]$Images, [Parameter(Mandatory)][int[]]$Ids)

    $ms = New-Object System.IO.MemoryStream
    $bw = New-Object System.IO.BinaryWriter $ms
    $bw.Write([uint16]0)             # reserved
    $bw.Write([uint16]1)             # type = icon
    $bw.Write([uint16]$Images.Count) # count

    for ($i = 0; $i -lt $Images.Count; $i++) {
        $im = $Images[$i]
        $bw.Write([byte]$im.Width)
        $bw.Write([byte]$im.Height)
        $bw.Write([byte]$im.Colors)
        $bw.Write([byte]0)                  # reserved
        $bw.Write([uint16]$im.Planes)
        $bw.Write([uint16]$im.BitCount)
        $bw.Write([uint32]$im.ByteSize)
        $bw.Write([uint16]$Ids[$i])         # RT_ICON 资源 ID（2 字节，关键差异）
    }
    $bw.Flush()
    $out = $ms.ToArray()
    $bw.Dispose(); $ms.Dispose()
    return ,$out
}

# ============================================================
# 发现所有 Edge 的 msedge.exe / msedge_proxy.exe（多策略，去重）
# 策略：注册表 App Paths / StartMenuInternet + 常见安装根（含 per-user）
# 对每个安装根，既补丁顶层 exe，也补丁每个版本号子目录里的 exe。
# ============================================================
function Find-EdgeExecutables {
    $targetNames = @('msedge.exe', 'msedge_proxy.exe')  # 范围：浏览器 + app 模式，不含 WebView2
    $roots = New-Object System.Collections.Generic.HashSet[string]

    # --- 1) 注册表线索 ---
    $regCandidates = @()
    try {
        $p = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe' -ErrorAction SilentlyContinue).'(default)'
        if ($p) { $regCandidates += $p }
    } catch {}
    try {
        $c = (Get-ItemProperty 'HKLM:\SOFTWARE\Clients\StartMenuInternet\Microsoft Edge\shell\open\command' -ErrorAction SilentlyContinue).'(default)'
        if ($c) { $regCandidates += ($c -replace '"','').Trim() }
    } catch {}
    foreach ($rc in $regCandidates) {
        try {
            $dir = Split-Path -Parent $rc
            if ($dir -and (Test-Path $dir)) { [void]$roots.Add($dir) }
        } catch {}
    }

    # --- 2) 常见固定安装根 ---
    $fixed = @(
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application",
        "$env:ProgramFiles\Microsoft\Edge\Application",
        "$env:LocalAppData\Microsoft\Edge\Application"
    )
    foreach ($f in $fixed) { if ($f -and (Test-Path $f)) { [void]$roots.Add($f) } }

    # --- 3) 在每个根下收集 exe：顶层 + 版本号子目录 ---
    $found = New-Object System.Collections.Generic.HashSet[string]
    foreach ($root in $roots) {
        foreach ($name in $targetNames) {
            $top = Join-Path $root $name
            if (Test-Path $top) { [void]$found.Add((Resolve-Path $top).Path) }
        }
        try {
            $verDirs = Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue |
                       Where-Object { $_.Name -match '^\d+(\.\d+)+$' }
            foreach ($vd in $verDirs) {
                foreach ($name in $targetNames) {
                    $vp = Join-Path $vd.FullName $name
                    if (Test-Path $vp) { [void]$found.Add((Resolve-Path $vp).Path) }
                }
            }
        } catch {}
    }
    return @($found)
}

# ============================================================
# 给单个 exe 打补丁：写入 .ico 各帧为 RT_ICON，重写所有 RT_GROUP_ICON 指向它们，
# 并写入标记资源。写前自动备份为 <exe>.mahiro.bak（已存在则保留原备份）。
# ============================================================
function Set-ExeIcon {
    param(
        [Parameter(Mandatory)][string]$ExePath,
        [Parameter(Mandatory)]$Images
    )

    # 枚举现有图标组（含语言）——必须在 BeginUpdateResource 之前完成（独占句柄）
    # 某些 Edge exe（如 msedge_proxy.exe）本身不含图标资源，无需也无法补丁 → 返回特殊标记
    $groups = [ResApi]::EnumGroups($ExePath)
    if (-not $groups -or $groups.Count -eq 0) {
        return 'NoIcon'
    }

    # 备份（仅首次；保护原始未补丁文件）。仅在确有图标可改时才备份。
    $bak = "$ExePath.mahiro.bak"
    if (-not (Test-Path $bak)) {
        Copy-Item -LiteralPath $ExePath -Destination $bak -Force
    }

    # 为每帧分配共享 RT_ICON 资源 ID
    $ids = @()
    for ($i = 0; $i -lt $Images.Count; $i++) { $ids += ($script:IconIdBase + $i) }

    $langDefault = 1033  # en-US 兜底
    $h = [ResApi]::BeginUpdateResource($ExePath, $false)
    if ($h -eq [IntPtr]::Zero) {
        throw "BeginUpdateResource 失败: $ExePath (err=$([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
    }
    try {
        # 1) 写入所有 RT_ICON 帧（用默认语言）
        for ($i = 0; $i -lt $Images.Count; $i++) {
            $ok = [ResApi]::UpdateResource($h, [ResApi]::RT_ICON, [IntPtr]$ids[$i], [uint16]$langDefault,
                                           $Images[$i].Data, [uint32]$Images[$i].Data.Length)
            if (-not $ok) { throw "写 RT_ICON #$($ids[$i]) 失败" }
        }

        # 2) 构建 GRPICONDIR 并重写每个现有组（保留其原名与语言）
        $grpBytes = New-GroupIconBytes -Images $Images -Ids $ids
        foreach ($g in $groups) {
            $langs = if ($g.Langs.Count -gt 0) { $g.Langs } else { @($langDefault) }
            foreach ($lang in $langs) {
                if ($g.IsIntId) {
                    $namePtr = [IntPtr]$g.IntId
                    $ok = [ResApi]::UpdateResource($h, [ResApi]::RT_GROUP_ICON, $namePtr, [uint16]$lang,
                                                   $grpBytes, [uint32]$grpBytes.Length)
                } else {
                    # 字符串名：用 Marshal 分配 Unicode 指针
                    $strPtr = [System.Runtime.InteropServices.Marshal]::StringToHGlobalUni($g.StrId)
                    try {
                        $ok = [ResApi]::UpdateResource($h, [ResApi]::RT_GROUP_ICON, $strPtr, [uint16]$lang,
                                                       $grpBytes, [uint32]$grpBytes.Length)
                    } finally {
                        [System.Runtime.InteropServices.Marshal]::FreeHGlobal($strPtr)
                    }
                }
                if (-not $ok) { throw "重写 RT_GROUP_ICON 失败 (lang=$lang)" }
            }
        }

        # 3) 写入补丁标记（整数资源 ID，避免字符串名编码问题）
        $marker = [System.Text.Encoding]::ASCII.GetBytes("MahiroEdge")
        $ok = [ResApi]::UpdateResource($h, [ResApi]::RT_RCDATA, [ResApi]::MARKER_ID, [uint16]$langDefault, $marker, [uint32]$marker.Length)
        if (-not $ok) { throw "写补丁标记失败" }

        if (-not [ResApi]::EndUpdateResource($h, $false)) {
            throw "EndUpdateResource 提交失败: $ExePath"
        }
        $h = [IntPtr]::Zero
    }
    catch {
        if ($h -ne [IntPtr]::Zero) { [void][ResApi]::EndUpdateResource($h, $true) }  # discard
        throw
    }
}

function Test-IsPatched {
    param([Parameter(Mandatory)][string]$ExePath)
    try { return [ResApi]::HasMarker($ExePath) } catch { return $false }
}

# ============================================================
# 发现所有用户的 Edge 每配置文件图标（Edge Profile.ico）。
# Edge 把固定到任务栏 / 配置文件快捷方式的 IconLocation 显式指向这个 .ico；
# 一旦快捷方式有显式图标，Windows 就用它而忽略 exe 内嵌图标——这正是
# "桌面/exe 变了但任务栏没变" 的根因。必须连这些 .ico 一起换掉。
# 以 SYSTEM 运行时可访问 C:\Users\* 下所有用户，实现全机覆盖。
# 路径：C:\Users\<user>\AppData\Local\Microsoft\Edge\User Data\<profile>\Edge Profile.ico
# ============================================================
function Find-EdgeProfileIcons {
    $icons = New-Object System.Collections.Generic.HashSet[string]
    $usersRoot = Join-Path $env:SystemDrive '\Users'
    $userDirs = Get-ChildItem -Path $usersRoot -Directory -ErrorAction SilentlyContinue
    foreach ($u in $userDirs) {
        $udata = Join-Path $u.FullName 'AppData\Local\Microsoft\Edge\User Data'
        if (-not (Test-Path $udata)) { continue }
        try {
            Get-ChildItem -Path $udata -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $p = Join-Path $_.FullName 'Edge Profile.ico'
                if (Test-Path $p) { [void]$icons.Add($p) }
            }
        } catch {}
    }
    return @($icons)
}

# 用 ahoge 字节覆盖单个配置文件 .ico（首次先备份为 <ico>.mahiro.bak）。
function Set-ProfileIcon {
    param(
        [Parameter(Mandatory)][string]$IcoTarget,
        [Parameter(Mandatory)][byte[]]$IcoBytes
    )
    $bak = "$IcoTarget.mahiro.bak"
    $item = Get-Item -LiteralPath $IcoTarget -Force
    if ($item.IsReadOnly) { $item.IsReadOnly = $false }
    if (-not (Test-Path $bak)) {
        Copy-Item -LiteralPath $IcoTarget -Destination $bak -Force
    }
    [System.IO.File]::WriteAllBytes($IcoTarget, $IcoBytes)
    # 设为只读：阻止 Edge 启动 / 更新时悄悄重新生成原版图标（自愈任务为兜底）。
    (Get-Item -LiteralPath $IcoTarget -Force).IsReadOnly = $true
}

# 字节比较：当前 .ico 是否已是 ahoge（幂等判断，避免无谓重写 + explorer 重启）。
function Test-ProfileIconApplied {
    param([Parameter(Mandatory)][string]$IcoTarget, [Parameter(Mandatory)][byte[]]$IcoBytes)
    try {
        $cur = [System.IO.File]::ReadAllBytes($IcoTarget)
        if ($cur.Length -ne $IcoBytes.Length) { return $false }
        for ($i = 0; $i -lt $cur.Length; $i++) { if ($cur[$i] -ne $IcoBytes[$i]) { return $false } }
        return $true
    } catch { return $false }
}

# ============================================================
# 批量补丁：发现所有 exe，逐个应用（单个失败不影响其它），返回统计。
# $Force=$false 时跳过已打补丁的 exe（幂等，供计划任务高频调用）。
# ============================================================
function Invoke-Patch {
    param(
        [Parameter(Mandatory)][string]$IcoPath,
        [switch]$Force
    )
    if (-not (Test-Path $IcoPath)) { throw "图标文件不存在: $IcoPath" }
    $images = Get-IconImagesFromIco -IcoPath $IcoPath
    $icoBytes = [System.IO.File]::ReadAllBytes($IcoPath)   # 原始 .ico 字节，用于覆盖每配置文件图标

    $exes = Find-EdgeExecutables
    $patched = 0; $skipped = 0; $failed = 0; $noIcon = 0
    foreach ($exe in $exes) {
        try {
            if (-not $Force -and (Test-IsPatched -ExePath $exe)) {
                Write-Host "[跳过] 已是呆毛: $exe"
                $skipped++; continue
            }
            $res = Set-ExeIcon -ExePath $exe -Images $images
            if ($res -eq 'NoIcon') {
                Write-Host "[无图标] 本身不含图标资源，跳过: $exe"
                $noIcon++
            } else {
                Write-Host "[成功] $exe"
                $patched++
            }
        } catch {
            Write-Warning "[失败] $exe : $($_.Exception.Message)"
            $failed++
        }
    }

    # --- 同步覆盖每配置文件 Edge Profile.ico（修复任务栏快捷方式显式图标）---
    $profIcons = Find-EdgeProfileIcons
    $profPatched = 0; $profSkipped = 0; $profFailed = 0
    foreach ($pi in $profIcons) {
        try {
            if (-not $Force -and (Test-ProfileIconApplied -IcoTarget $pi -IcoBytes $icoBytes)) {
                $profSkipped++; continue
            }
            Set-ProfileIcon -IcoTarget $pi -IcoBytes $icoBytes
            Write-Host "[配置图标] $pi"
            $profPatched++
        } catch {
            Write-Warning "[配置图标失败] $pi : $($_.Exception.Message)"
            $profFailed++
        }
    }

    return [pscustomobject]@{
        Total = $exes.Count; Patched = $patched; Skipped = $skipped; NoIcon = $noIcon; Failed = $failed; Exes = $exes
        ProfileIcons = $profIcons.Count; ProfilePatched = $profPatched; ProfileSkipped = $profSkipped; ProfileFailed = $profFailed
    }
}

# ============================================================
# 还原：对每个发现的 exe，若存在 .mahiro.bak 则从备份恢复并删除备份。
# $FallbackProfileIco：项目自带的原版 Edge Profile.ico。当某个配置文件图标
# 没有 .mahiro.bak（用户当初无备份 / 备份丢失）时，用它兜底还原，避免无法复原。
# 注意：exe 没有兜底——原版 exe 因含数字签名无法随包分发，只能依赖 .bak。
# ============================================================
function Invoke-Restore {
    param([string]$FallbackProfileIco)

    $fallbackBytes = $null
    if ($FallbackProfileIco -and (Test-Path $FallbackProfileIco)) {
        try { $fallbackBytes = [System.IO.File]::ReadAllBytes($FallbackProfileIco) } catch {}
    }

    $exes = Find-EdgeExecutables
    # 也扫描备份文件本身（防 exe 已被删但备份残留）
    $restored = 0; $missing = 0; $failed = 0
    foreach ($exe in $exes) {
        $bak = "$exe.mahiro.bak"
        try {
            if (Test-Path $bak) {
                Copy-Item -LiteralPath $bak -Destination $exe -Force
                Remove-Item -LiteralPath $bak -Force
                Write-Host "[还原] $exe"
                $restored++
            } else {
                $missing++
            }
        } catch {
            Write-Warning "[还原失败] $exe : $($_.Exception.Message)"
            $failed++
        }
    }

    # --- 还原每配置文件 Edge Profile.ico ---
    # 优先 .mahiro.bak；无备份时退回项目自带原版图标（$fallbackBytes）兜底。
    $profRestored = 0; $profMissing = 0; $profFailed = 0; $profFallback = 0
    foreach ($pi in (Find-EdgeProfileIcons)) {
        $bak = "$pi.mahiro.bak"
        try {
            $cur = Get-Item -LiteralPath $pi -Force -ErrorAction SilentlyContinue
            if ($cur -and $cur.IsReadOnly) { $cur.IsReadOnly = $false }
            if (Test-Path $bak) {
                Copy-Item -LiteralPath $bak -Destination $pi -Force
                Remove-Item -LiteralPath $bak -Force
                Write-Host "[配置图标还原] $pi"
                $profRestored++
            } elseif ($fallbackBytes) {
                [System.IO.File]::WriteAllBytes($pi, $fallbackBytes)
                Write-Host "[配置图标兜底还原] $pi"
                $profFallback++
            } else {
                $profMissing++
            }
        } catch {
            Write-Warning "[配置图标还原失败] $pi : $($_.Exception.Message)"
            $profFailed++
        }
    }

    return [pscustomobject]@{
        Restored = $restored; NoBackup = $missing; Failed = $failed
        ProfileRestored = $profRestored; ProfileFallback = $profFallback
        ProfileNoBackup = $profMissing; ProfileFailed = $profFailed
    }
}

# ============================================================
# 清理 Windows 图标缓存并重启 explorer，使新图标立即可见。
# ============================================================
function Clear-IconCache {
    param([switch]$RestartExplorer)
    try { Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue } catch {}
    Start-Sleep -Milliseconds 400
    $local = $env:LOCALAPPDATA
    Remove-Item -Path "$local\IconCache.db" -Force -ErrorAction SilentlyContinue
    Remove-Item -Path "$local\Microsoft\Windows\Explorer\iconcache_*.db" -Force -ErrorAction SilentlyContinue
    Remove-Item -Path "$local\Microsoft\Windows\Explorer\thumbcache_*.db" -Force -ErrorAction SilentlyContinue
    if ($RestartExplorer) {
        if (-not (Get-Process -Name explorer -ErrorAction SilentlyContinue)) {
            Start-Process explorer
        }
    }
}

Export-ModuleMember -Function Find-EdgeExecutables, Get-IconImagesFromIco, Set-ExeIcon, `
    Invoke-Patch, Invoke-Restore, Clear-IconCache, Test-IsPatched, `
    Find-EdgeProfileIcons, Set-ProfileIcon, Test-ProfileIconApplied
