# 注册 Windows 任务计划：每个交易日 15:30 自动执行复盘
# 用法：右键「使用 PowerShell 运行」，或在 PowerShell 中执行 .\注册定时任务.ps1
# 卸载：.\注册定时任务.ps1 -Remove

param([switch]$Remove)

$ErrorActionPreference = "Stop"
$TaskName = "钱多多智能复盘_每日复盘"
$OldTaskNames = @("A股智能复盘系统_每日复盘")   # 历史任务名，注册时一并清理
$Root = $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    foreach ($old in $OldTaskNames) {
        Unregister-ScheduledTask -TaskName $old -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Host "已删除定时任务：$TaskName" -ForegroundColor Yellow
    exit 0
}

# 清理改名前注册的旧任务，避免重复触发
foreach ($old in $OldTaskNames) {
    if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $old -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "已清理旧任务：$old" -ForegroundColor Yellow
    }
}

# 优先使用项目虚拟环境的 Python
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$Script = Join-Path $Root "run_review.py"

Write-Host "Python  : $Python"
Write-Host "脚本    : $Script"
Write-Host "执行时间: 每天 15:30（脚本内部会自动判断是否交易日，非交易日直接跳过）"

$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Daily -At 15:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "每交易日收盘后自动采集行情与新闻并生成复盘报告" -Force | Out-Null

Write-Host "`n定时任务注册成功：$TaskName" -ForegroundColor Green
Write-Host "可在「任务计划程序」中查看。立即测试运行：" -ForegroundColor Green
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"