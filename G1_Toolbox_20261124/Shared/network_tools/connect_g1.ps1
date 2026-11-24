# G1 一键网络配置（便携版，任何 Windows 电脑可用）
# 用法:
#   .\connect_g1.ps1              设置静态 IP 并扫描机器人
#   .\connect_g1.ps1 -Restore     恢复该网卡为自动获取 IP
#   .\connect_g1.ps1 -Manual      手动选择网卡
#   .\connect_g1.ps1 -IP 192.168.123.10 -Gateway 192.168.123.1   自定义地址/网关
param(
    [switch]$Restore,
    [switch]$Manual,
    [switch]$DryRun,
    [string]$IP = "192.168.123.222",
    [string]$Gateway = "",
    [string]$RobotIP = "192.168.123.161"
)

$ErrorActionPreference = "Continue"

$VirtualPattern = 'virtual|vmware|hyper-v|vethernet|\btap\b|\btun\b|vpn|loopback|bluetooth|wsl|docker|sstap|clash|zerotier|tailscale|wireguard|pseudo|wan miniport|teredo|isatap|npcap|vbox'

function Write-Step($msg) { Write-Host "`n[>] $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[X] $msg" -ForegroundColor Red }

# ---------- 管理员检查 ----------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $DryRun) {
    Write-Err "需要管理员权限。请通过「一键连接机器人.bat」运行（会自动请求提权）。"
    Read-Host "`n按回车退出"
    exit 1
}

# ---------- 候选网卡 ----------
Write-Step "扫描有线网卡"
$all = Get-NetAdapter | Where-Object { -not $_.Virtual -and $_.InterfaceDescription -notmatch $VirtualPattern -and $_.Name -notmatch $VirtualPattern }
if (-not $all -or $all.Count -eq 0) {
    Write-Err "未找到物理网卡。"
    Read-Host "`n按回车退出"
    exit 1
}

function Get-RobotSubnetIP($idx) {
    Get-NetIPAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -like "192.168.123.*" } | Select-Object -First 1
}

$rows = @()
foreach ($a in $all) {
    $v4 = Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
    $dhcp = (Get-NetIPInterface -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).Dhcp
    $score = 0
    if ($v4 -and $v4.IPAddress -like "192.168.123.*") { $score = 100 }
    elseif ($a.Status -eq "Up" -and $v4 -and $v4.IPAddress -like "169.254.*") { $score = 60 }
    elseif ($a.Status -eq "Up" -and $a.InterfaceDescription -match "GbE|Ethernet|以太|Realtek|Intel\(R\) I2|Killer") { $score = 40 }
    elseif ($a.Status -eq "Up") { $score = 20 }
    $rows += [pscustomobject]@{
        Name = $a.Name; Desc = $a.InterfaceDescription; Status = $a.Status
        Speed = $a.LinkSpeed; IP = $(if ($v4) { $v4.IPAddress } else { "-" })
        Dhcp = $dhcp; Index = $a.ifIndex; Score = $score
    }
}

Write-Host ""
Write-Host ("{0,-12} {1,-8} {2,-12} {3,-18} {4}" -f "网卡","状态","速率","IP","DHCP")
Write-Host ("-" * 72)
foreach ($r in ($rows | Sort-Object Score -Descending)) {
    Write-Host ("{0,-12} {1,-8} {2,-12} {3,-18} {4}" -f $r.Name, $r.Status, $r.Speed, $r.IP, $r.Dhcp)
}
Write-Host ""
Write-Host "网卡详情:"
foreach ($r in ($rows | Sort-Object Score -Descending)) {
    Write-Host ("   [{0}] {1}  <-  {2}" -f $r.Index, $r.Name, $r.Desc)
}
Write-Host ""

$chosen = $null
if ($Manual) {
    $pick = Read-Host "输入要配置的网卡序号 (1-$($rows.Count)，按上表顺序)"
    $idx = [int]$pick - 1
    if ($idx -ge 0 -and $idx -lt $rows.Count) { $chosen = $rows[$idx] }
} else {
    $best = $rows | Sort-Object Score -Descending | Select-Object -First 1
    if ($best.Score -ge 20) { $chosen = $best }
    elseif ($rows.Count -eq 1) { $chosen = $rows[0] }
}

if (-not $chosen) {
    Write-Err "没有已连接网线的网卡，请先插好网线（接在机器人本体网口）。"
    Read-Host "`n按回车退出"
    exit 1
}

Write-OK "使用网卡: $($chosen.Name)  [$($chosen.Desc)]  状态=$($chosen.Status)  当前IP=$($chosen.IP)"

# ---------- 恢复 DHCP ----------
if ($Restore) {
    Write-Step "恢复为自动获取 IP"
    if ($DryRun) { Write-Warn2 "DryRun: 不修改任何配置"; Read-Host "`n按回车退出"; exit 0 }
    Remove-NetRoute -InterfaceIndex $chosen.Index -DestinationPrefix "0.0.0.0/0" -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetIPAddress -InterfaceIndex $chosen.Index -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.PrefixOrigin -ne "WellKnown" } |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    Set-NetIPInterface -InterfaceIndex $chosen.Index -Dhcp Enabled -ErrorAction SilentlyContinue
    Set-NetIPInterface -InterfaceIndex $chosen.Index -InterfaceMetric 100 -ErrorAction SilentlyContinue
    ipconfig /renew "$($chosen.Name)" 2>$null | Out-Null
    Start-Sleep -Seconds 2
    $now = Get-NetIPAddress -InterfaceIndex $chosen.Index -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
    Write-OK "已恢复自动获取。当前 IP: $(if ($now) { $now.IPAddress } else { '（无，属正常）' })"
    Read-Host "`n按回车退出"
    exit 0
}

# ---------- 设置静态 IP ----------
Write-Step "配置静态 IP $IP/24（不设网关，机器人网段是直连的）"
if ($DryRun) {
    Write-Warn2 "DryRun: 预览模式，不修改配置"
} else {
    Get-NetIPAddress -InterfaceIndex $chosen.Index -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.PrefixOrigin -ne "WellKnown" } |
        ForEach-Object { Remove-NetIPAddress -InterfaceIndex $chosen.Index -IPAddress $_.IPAddress -Confirm:$false -ErrorAction SilentlyContinue }
    Remove-NetRoute -InterfaceIndex $chosen.Index -DestinationPrefix "0.0.0.0/0" -Confirm:$false -ErrorAction SilentlyContinue

    Set-NetIPInterface -InterfaceIndex $chosen.Index -Dhcp Disabled -ErrorAction SilentlyContinue
    if ($Gateway -ne "") {
        New-NetIPAddress -InterfaceIndex $chosen.Index -IPAddress $IP -PrefixLength 24 -DefaultGateway $Gateway -ErrorAction SilentlyContinue | Out-Null
        Write-Warn2 "已设置网关 $Gateway（G1 直连一般不需要网关；若填了不存在的网关可能导致该网段不通，可用 -Gateway '' 重跑）"
    } else {
        New-NetIPAddress -InterfaceIndex $chosen.Index -IPAddress $IP -PrefixLength 24 -ErrorAction SilentlyContinue | Out-Null
    }
    Set-NetIPInterface -InterfaceIndex $chosen.Index -InterfaceMetric 25 -ErrorAction SilentlyContinue

    Start-Sleep -Milliseconds 800
    $set = Get-NetIPAddress -InterfaceIndex $chosen.Index -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -eq $IP }
    if ($set) { Write-OK "本机已设为 $IP/24" } else { Write-Err "设置失败，请检查网卡状态"; Read-Host "`n按回车退出"; exit 1 }
}

# ---------- 扫描机器人 ----------
Write-Step "扫描 192.168.123.0/24（约 3 秒）"
$tasks = @()
foreach ($i in 1..254) {
    $p = New-Object System.Net.NetworkInformation.Ping
    $tasks += [pscustomobject]@{ IP = "192.168.123.$i"; T = $p.SendPingAsync("192.168.123.$i", 600) }
}
[void][System.Threading.Tasks.Task]::WaitAll([System.Threading.Tasks.Task[]]($tasks | ForEach-Object { $_.T }), 4000)
$alive = @()
foreach ($t in $tasks) {
    try {
        if ($t.T.IsCompleted -and $t.T.Result -and $t.T.Result.Status -eq "Success" -and $t.IP -ne $IP) { $alive += $t.IP }
    } catch { }
}

if ($alive.Count -eq 0) {
    Write-Err "网段内没有发现任何设备。"
    Write-Warn2 "检查: ①网线是否插在机器人本体网口 ②机器人是否已开机 ③网口指示灯是否亮"
} else {
    Write-OK "发现设备: $($alive -join ', ')"
    $has161 = $alive -contains $RobotIP
    $has164 = $alive -contains "192.168.123.164"
    if ($has161) { Write-OK "$RobotIP (运动控制主控) 在线 —— 可以跑 SDK 了" }
    else { Write-Warn2 "$RobotIP (运动控制主控) 不在线 —— SDK 读不到状态、也发不了指令" }
    if ($has164) { Write-OK "192.168.123.164 (开发计算单元) 在线" }
}

Write-Host ""
Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host " 下一步:" -ForegroundColor White
Write-Host "   双击 D:\DATAWORK\G1\start.bat        自检 + 控制台"
Write-Host "   双击 D:\DATAWORK\G1\pipeline_gui.bat 打开 BVH 转 G1 界面"
Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""
Read-Host "`n按回车退出"
