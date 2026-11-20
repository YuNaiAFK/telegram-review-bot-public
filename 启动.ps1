param([switch]$ReplaceToken, [switch]$ReplaceApi, [switch]$Gui, [switch]$CloudCutover)

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
$added = $false
$previousToken = $env:TELEGRAM_BOT_TOKEN
$previousApiId = $env:TELEGRAM_API_ID
$previousApiHash = $env:TELEGRAM_API_HASH
$previousHeadless = $env:REVIEW_BOT_HEADLESS

function Read-ApiValue($label, $pattern) {
    while ($true) {
        $null = Read-Host "请复制 ${label}，然后回到这里直接按 Enter（不用粘贴）"
        try {
            $value = ([string](Get-Clipboard -Raw)).Trim()
        } catch {
            Write-Warning '暂时读不到剪贴板。请重新复制再试。'
            continue
        }
        if ($value -cmatch $pattern) { return $value }
        Write-Warning "剪贴板内容不是有效的 ${label}，请只复制该值本身。"
    }
}

function Write-StartupLog([string]$message) {
    $file = Join-Path $PSScriptRoot 'startup.log'
    Add-Content -LiteralPath $file -Encoding UTF8 -Value ('{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $message)
}

$stage = '读取配置'
try {
    Write-StartupLog '启动脚本已运行'
    $config = Get-Content -Raw -Encoding UTF8 -LiteralPath config.json | ConvertFrom-Json
    if (!$config.owner_id -or !$config.reviewer_ids.Count -or !$config.targets.PSObject.Properties.Name) {
        Write-StartupLog '配置不完整，尚未启动 Python'
        Write-Warning '请先在 config.json 填写号主 ID、管理员 ID，以及至少一个群或频道的 ID 和问题。'
        return
    }
    $stage = '读取 Bot Token'
    $tokenFile = Join-Path $PSScriptRoot 'bot-token.xml'
    if ($ReplaceToken -or !$env:TELEGRAM_BOT_TOKEN) {
        if (!$ReplaceToken -and (Test-Path -LiteralPath $tokenFile)) {
            try {
                $saved = Import-Clixml -LiteralPath $tokenFile
                if ($saved -isnot [System.Security.SecureString]) { throw 'Invalid saved token' }
                $env:TELEGRAM_BOT_TOKEN = [System.Net.NetworkCredential]::new('', $saved).Password
                $saved.Dispose()
                $added = $true
            } catch {
                Write-StartupLog '无法读取已保存的 Bot Token'
                Write-Warning '无法读取已保存的 Token。请用 .\启动.ps1 -ReplaceToken 重新保存。'
                if ($Gui) { throw '无法读取 Bot Token' }
                return
            }
        } else {
            if ($Gui) {
                Write-StartupLog '缺少 Bot Token。请在图形控制台点「登录 / 配置凭据」。'
                throw '缺少 Bot Token'
            }
            $mode = Read-Host '先复制 Bot Token，然后直接按 Enter 从剪贴板读取；手动输入请键入 M'
            if ($mode -eq '') {
                $token = ([string](Get-Clipboard -Raw)).Trim()
            } elseif ($mode -eq 'M') {
                $secret = Read-Host '请输入 Bot Token（输入不会显示）' -AsSecureString
                $token = [System.Net.NetworkCredential]::new('', $secret).Password.Trim()
                $secret.Dispose()
            } else {
                Write-Warning '请直接按 Enter 或输入 M；不要把 Token 粘贴到这个提示中。'
                return
            }
            if ($token -cnotmatch '\A[0-9]+:[A-Za-z0-9_-]+\z') {
                Remove-Variable token
                Write-Warning '没有读到有效格式的 Bot Token。请只复制 Token 本身，再重新运行。'
                return
            }
            $saved = ConvertTo-SecureString -String $token -AsPlainText -Force
            try {
                Export-Clixml -InputObject $saved -LiteralPath $tokenFile
            } finally {
                $saved.Dispose()
            }
            $env:TELEGRAM_BOT_TOKEN = $token
            Remove-Variable token
            $added = $true
        }
    }
    if ($CloudCutover) {
        $stage = '切换 Cloudflare'
        node (Join-Path $PSScriptRoot 'cloudflare/cutover.mjs')
        if ($LASTEXITCODE -ne 0) { throw 'Cloudflare 切换未完成，请查看上方提示。' }
        return
    }
    $stage = '读取号主 API 凭据'
    if ($config.common_group_mode -eq 'owner_account' -and $config.min_common_groups -gt 0) {
        $apiFile = Join-Path $PSScriptRoot 'owner-api.xml'
        if ($ReplaceApi -or !$env:TELEGRAM_API_ID -or !$env:TELEGRAM_API_HASH) {
            if (!$ReplaceApi -and (Test-Path -LiteralPath $apiFile)) {
                try {
                    $api = Import-Clixml -LiteralPath $apiFile
                    if ($api -isnot [System.Management.Automation.PSCredential]) { throw 'Invalid credential' }
                    $env:TELEGRAM_API_ID = $api.UserName
                    $env:TELEGRAM_API_HASH = $api.GetNetworkCredential().Password
                    $api.Password.Dispose()
                } catch {
                    Write-StartupLog '无法读取已保存的 API 凭据'
                    Write-Warning '无法读取已保存的 API 凭据。请用 .\启动.ps1 -ReplaceApi 重新保存。'
                    if ($Gui) { throw '无法读取 API 凭据' }
                    return
                }
            } else {
                if ($Gui) {
                    Write-StartupLog '缺少号主 API 凭据。请在图形控制台点「登录 / 配置凭据」。'
                    throw '缺少号主 API 凭据'
                }
                $apiId = Read-ApiValue 'API ID' '\A[0-9]{1,10}\z'
                $apiHash = Read-ApiValue 'API hash' '\A[0-9a-fA-F]{32}\z'
                $secureHash = ConvertTo-SecureString -String $apiHash -AsPlainText -Force
                try {
                    $api = [System.Management.Automation.PSCredential]::new($apiId, $secureHash)
                    Export-Clixml -InputObject $api -LiteralPath $apiFile
                } finally {
                    $secureHash.Dispose()
                }
                $env:TELEGRAM_API_ID = $apiId
                $env:TELEGRAM_API_HASH = $apiHash
                Remove-Variable apiHash
                Write-Host 'API 凭据已保存。现在可以复制无关文字覆盖剪贴板。'
            }
        }
    }
    if ($Gui) { $env:REVIEW_BOT_HEADLESS = '1' }
    $stage = '启动 Python'
    Write-StartupLog '正在运行 python bot.py'
    python bot.py
    Write-StartupLog "Python 已退出，退出代码：$LASTEXITCODE"
    if ($LASTEXITCODE -ne 0) { throw "Bot 退出，代码：$LASTEXITCODE" }
} catch {
    Write-StartupLog ("启动失败：{0}（{1}）" -f $stage, $_.Exception.GetType().Name)
    throw
} finally {
    if ($added) { $env:TELEGRAM_BOT_TOKEN = $previousToken }
    $env:TELEGRAM_API_ID = $previousApiId
    $env:TELEGRAM_API_HASH = $previousApiHash
    $env:REVIEW_BOT_HEADLESS = $previousHeadless
    Pop-Location
}
