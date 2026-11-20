import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const filename = 'owner-probe.dpapi';
const program = (save, file) => `
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
$path = Join-Path (Get-Location) '${file}'
$scope = [Security.Cryptography.DataProtectionScope]::CurrentUser
${save ? `
$bytes = [Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())
$encrypted = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, $scope)
$tmp = $path + '.' + [Guid]::NewGuid().ToString('N')
try {
  [IO.File]::WriteAllBytes($tmp, $encrypted)
  if ([IO.File]::Exists($path)) { [IO.File]::Replace($tmp, $path, ($tmp + ".bak")); [IO.File]::Delete($tmp + ".bak") }
  else { [IO.File]::Move($tmp, $path) }
} finally { if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) } }
` : `
$bytes = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($path), $null, $scope)
[Console]::Write([Convert]::ToBase64String($bytes))
`}
`;

function exchange(root, save, input, file) {
  if (![filename, 'staging-bot.dpapi'].includes(file)) throw new Error('无效的凭据存储名称');
  if (process.platform !== 'win32') throw new Error('此向导的凭据存储仅支持 Windows');
  const encoded = Buffer.from(program(save, file), 'utf16le').toString('base64');
  const result = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], {
    cwd: root, input, encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'], timeout: 10000, windowsHide: true,
  });
  if (result.status !== 0) throw new Error('无法访问当前 Windows 用户的加密配置');
  return result.stdout.trim();
}

export function loadProbeState(root, file = filename) {
  if (!existsSync(join(root, file))) return null;
  return JSON.parse(Buffer.from(exchange(root, false, '', file), 'base64').toString('utf8'));
}

export function saveProbeState(root, state, file = filename) {
  exchange(root, true, Buffer.from(JSON.stringify(state), 'utf8').toString('base64'), file);
}
