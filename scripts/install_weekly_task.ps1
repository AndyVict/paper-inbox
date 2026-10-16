param(
    [ValidateSet('Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday')]
    [string]$Day = 'Monday',
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')]
    [string]$At = '10:00',
    [ValidatePattern('^(sync|abstracts|translation|jcr|delivery)(,(sync|abstracts|translation|jcr|delivery))*$')]
    [string]$Stages = 'sync,abstracts,delivery',
    [ValidateSet('en','zh')]
    [string]$Language = 'en',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{2,160}$')]
    [string]$Snapshot = 'local-jcr',
    [switch]$Fetch,
    [switch]$EnablePush,
    [switch]$Register,
    [switch]$Disabled
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entryPath = Join-Path $projectRoot 'scripts\run_weekly.py'
$taskName = 'PaperInbox-Weekly'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'missing_project_python' }
$stageNames = $Stages.Split(',')
if (@($stageNames | Select-Object -Unique).Count -ne $stageNames.Count) { throw 'duplicate_weekly_stages' }
if ($EnablePush -and $stageNames -notcontains 'delivery') { throw 'push_requires_delivery_stage' }
if ($Register -and (Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue)) {
    throw 'weekly_task_already_exists_not_overwritten'
}
if ($EnablePush) {
    & $pythonPath -X utf8 $entryPath --check-push-ready
    if ($LASTEXITCODE -ne 0) { throw 'wxpusher_receipt_not_confirmed' }
}
$taskArgs = '-X utf8 "' + $entryPath + '" --stages "' + $Stages + '" --language ' + $Language + ' --snapshot "' + $Snapshot + '"'
if ($Fetch -or $EnablePush) { $taskArgs += ' --fetch' }
if ($EnablePush) { $taskArgs += ' --send' }
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $taskArgs -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$settings.Enabled = -not $Disabled
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$definition = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Paper Inbox literature alerts; local time; WxPusher App; receipt required for push.'
$directory = Join-Path $projectRoot 'data\schedule'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$xmlPath = Join-Path $directory 'paper-inbox-weekly.xml'
$xml = (Export-ScheduledTask -InputObject $definition) -replace 'encoding="UTF-16"', 'encoding="UTF-8"'
[System.IO.File]::WriteAllText($xmlPath, $xml, (New-Object System.Text.UTF8Encoding($false)))
if ($Register) {
    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -InputObject $definition | Out-Null
    $actualTask = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
    if ($actualTask.Settings.Enabled -ne (-not $Disabled)) { throw 'weekly_task_enabled_state_mismatch' }
    Write-Output "Registered: $taskName; $Day $At; Push=$EnablePush"
} else {
    Write-Output "Exported plan only; local time: $Day $At; Push=$EnablePush; not registered."
}
