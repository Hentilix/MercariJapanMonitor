# -*- coding: utf-8 -*-
<#
  Phase 5.6 — 安装/卸载 Windows 计划任务 "MarketplaceMonitor"。

  用法（在项目根目录或任意位置均可）：
    powershell -ExecutionPolicy Bypass -File scripts\manage_task.ps1              # 注册任务
    powershell -ExecutionPolicy Bypass -File scripts\manage_task.ps1 -Uninstall   # 删除任务

  任务配置：
    TaskName : MarketplaceMonitor
    Program  : <项目目录>\.venv\Scripts\python.exe
    Arguments: main.py
    Start in : <项目目录>
    Trigger  : 当前用户登录时（后台运行，不弹出窗口，不自动打开浏览器）
    其他     : 意外退出后 1 分钟后重启，最多重试 3 次；运行时限 365 天

  说明：
    - 登录任务读不到仅存在于当前 PowerShell 会话里的环境变量，SMTP_PASSWORD /
      DEEPSEEK_API_KEY_FOR_MJM 等必须以「用户级」环境变量方式设置（setx 或系统属性）。
    - 本脚本绝不写入任何密钥，也不从任何文件读取密钥。
    - 运行日志输出到 <项目目录>\data\logs\monitor.log。
#>
param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "MarketplaceMonitor"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除计划任务：$TaskName"
    exit 0
}

if (-not (Test-Path $PythonExe)) {
    throw "未找到虚拟环境 Python：$PythonExe（请先 python -m venv .venv 并安装 requirements.txt）"
}

foreach ($Name in @("SMTP_PASSWORD", "DEEPSEEK_API_KEY_FOR_MJM")) {
    if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($Name, "User"))) {
        Write-Warning "用户级环境变量 $Name 未设置——任务运行时对应功能将不可用（登录任务读不到会话级变量）。可用：setx $Name <值>"
    }
}

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument '"main.py"' -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Host "已注册计划任务：$TaskName"
Write-Host ("  Program  : {0}" -f $PythonExe)
Write-Host '  Arguments: main.py'
Write-Host ("  Start in : {0}" -f $ProjectDir)
Write-Host ("  Trigger  : 用户登录时（{0}）" -f $env:USERNAME)
Write-Host ("  日志文件 : {0}\data\logs\monitor.log" -f $ProjectDir)
Write-Host "  自动开浏览器: 否（show=False，后台常驻）"
