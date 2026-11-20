import { spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline/promises';
import { fileURLToPath } from 'node:url';
import { allowedUpdates } from './review-bot.mjs';
import { loadProbeState, saveProbeState } from './probe-state.mjs';

import { loadDeployment } from './deployment-config.mjs';

const deployment = loadDeployment();
const cwd = fileURLToPath(new URL('.', import.meta.url));
const expectedBot = deployment.testBotUsername.toLowerCase();
const worker = deployment.workerName;
const webhook = deployment.workerUrl + '/staging/webhook';
const rl = createInterface({ input: process.stdin, output: process.stdout });

function tokenDialog() {
  const script = `
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $ErrorActionPreference = 'Stop'
    $form = New-Object Windows.Forms.Form
    $form.Text = 'Telegram test bot token'
    $form.StartPosition = 'CenterScreen'
    $form.ClientSize = New-Object Drawing.Size(470, 136)
    $form.TopMost = $true
    $label = New-Object Windows.Forms.Label
    $label.Text = 'Paste the BotFather token for the test bot.'
    $label.AutoSize = $true; $label.SetBounds(16, 15, 430, 24)
    $box = New-Object Windows.Forms.TextBox
    $box.SetBounds(16, 45, 438, 25); $box.UseSystemPasswordChar = $true
    $ok = New-Object Windows.Forms.Button
    $ok.Text = 'Continue'; $ok.SetBounds(278, 84, 84, 28); $ok.DialogResult = 'OK'
    $cancel = New-Object Windows.Forms.Button
    $cancel.Text = 'Cancel'; $cancel.SetBounds(370, 84, 84, 28); $cancel.DialogResult = 'Cancel'
    $form.Controls.AddRange(@($label, $box, $ok, $cancel))
    $form.AcceptButton = $ok; $form.CancelButton = $cancel
    $form.Add_Shown({ $form.Activate(); [void]$box.Focus() })
    try {
      if ($form.ShowDialog() -ne 'OK') { exit 2 }
      [Console]::Write($box.Text.Trim())
    } finally { $form.Dispose() }
  `;
  const result = spawnSync('powershell.exe', ['-NoProfile','-STA','-EncodedCommand',
    Buffer.from(script,'utf16le').toString('base64')], {
    windowsHide:false, encoding:'utf8', stdio:['ignore','pipe','ignore'],
  });
  const token = result.stdout?.trim() ?? '';
  if (result.status !== 0 || !/^\d+:[A-Za-z0-9_-]+$/.test(token)) throw Error('未输入有效的测试 Bot Token。');
  return token;
}

function telegram(token, method, body = {}) {
  const cfg = 'url = ' + JSON.stringify(`https://api.telegram.org/bot${token}/${method}`)
    + '\nheader = "Content-Type: application/json"\ndata = ' + JSON.stringify(JSON.stringify(body)) + '\n';
  const proxy = proxyPort ? ['--proxy',`socks5h://127.0.0.1:${proxyPort}`,'--noproxy',''] : ['--noproxy','*'];
  const response = spawnSync('curl.exe',['--silent','--show-error','--max-time','20','--config','-', ...proxy],{
    input:cfg, encoding:'utf8', windowsHide:true, stdio:['pipe','pipe','ignore'],timeout:23000,
  });
  if (response.status !== 0) throw Error(`${method}：无法连接 Telegram。`);
  const parsed = JSON.parse(response.stdout);
  if (!parsed.ok) throw Error(`${method}：${parsed.description ?? 'Telegram 拒绝了请求'}`);
  return parsed.result;
}

function put(name,value) {
  const proc = spawnSync(process.env.ComSpec || 'cmd.exe',
    ['/d','/c','npx.cmd','--yes','--offline','wrangler','secret','put',name,'--name',worker],{
      cwd, input:value+'\n', encoding:'utf8', windowsHide:true,
      stdio:['pipe','ignore','ignore'],timeout:35000,
    });
  if (proc.status !== 0) throw Error(`未能写入测试 Worker 的 ${name}，请检查 Wrangler 登录状态。`);
}

let proxyPort;
try {
  console.log(`只绑定 @${deployment.testBotUsername}，不修改正式 Bot。`);
  const groupInput = (await rl.question('测试群数字 ID，或私有超级群的消息链接：')).trim();
  const link = groupInput.match(/^https:\/\/t\.me\/c\/(\d+)\/(?:\d+\/)?\d+(?:\?.*)?$/);
  const chatId = link ? `-100${link[1]}` : groupInput;
  const group = Number(chatId);
  if (!/^-\d+$/.test(chatId) || !Number.isSafeInteger(group)) throw Error('请填写有效的负数群 ID 或 t.me/c/ 消息链接。');
  const portInput = (await rl.question('Clash 混合端口（默认 7897，填 0 直连）：')).trim();
  proxyPort = portInput === '' ? 7897 : Number(portInput);
  if (!Number.isInteger(proxyPort) || proxyPort < 0 || proxyPort > 65535) throw Error('代理端口无效。');
  const cache = 'staging-bot.dpapi';
  const saved = process.argv.includes('--replace-token') ? null : loadProbeState(cwd, cache);
  const reused = saved?.username === expectedBot && /^\d+:[A-Za-z0-9_-]+$/.test(saved.token ?? '');
  console.log(reused ? '已读取本机加密保存的测试 Bot Token。' : '请在弹窗粘贴测试 Bot Token。');
  const token = reused ? saved.token : tokenDialog();
  let me;
  try { me = telegram(token,'getMe'); }
  catch (error) {
    if (reused && /unauthorized|not found/i.test(error.message)) {
      throw Error('已保存的测试 Bot Token 无效，请加 --replace-token 重新录入。');
    }
    throw error;
  }
  if (me.username?.toLowerCase() !== expectedBot) throw Error('Token 不属于指定的测试 Bot，已停止，未修改 Webhook。');
  if (!reused) {
    saveProbeState(cwd, { username: expectedBot, token }, cache);
    console.log('测试 Bot Token 已加密保存，下次无需输入。');
  }
  const local = JSON.parse((await readFile(new URL('../config.json',import.meta.url),'utf8')).replace(/^\uFEFF/,''));
  if (Object.keys(local.targets).includes(String(group))) throw Error('这是正式审核目标。请换一个独立测试群。');
  const room = telegram(token,'getChat',{chat_id:group});
  if (!['group','supergroup'].includes(room.type)) throw Error('此向导只绑定测试群。');
  let rights;
  try { rights = telegram(token,'getChatMember',{chat_id:group,user_id:me.id}); }
  catch (error) {
    if (/member not found/i.test(error.message)) throw Error('测试群能读取，但群成员中没有指定的测试 Bot。请先加 Bot 并设为管理员。');
    throw error;
  }
  if (rights.status !== 'administrator' || !rights.can_invite_users) throw Error('请给测试 Bot 授予邀请用户权限。');
  let reviewers = 0;
  for (const id of local.reviewer_ids) {
    try {
      const admin = telegram(token,'getChatMember',{chat_id:group,user_id:id});
      if (admin.status === 'creator' || (admin.status === 'administrator' && admin.can_invite_users)) reviewers++;
    } catch (error) {
      if (!/member not found|user not found|USER_ID_INVALID/i.test(error.message)) throw error;
    }
  }
  if (!reviewers) throw Error(`测试群里没有符合要求的审核管理员。请将已登记账号（${local.reviewer_ids.join('、')}）加入测试群，并设为有「邀请用户」权限的管理员。`);
  const current = telegram(token,'getWebhookInfo');
  if (current.url && current.url !== webhook) throw Error('测试 Bot 已有别的 Webhook，未覆盖。');
  if ((await rl.question('已核对测试 Bot 和群。要绑定测试 Webhook 吗？输入 YES：')).trim().toUpperCase() !== 'YES') {
    throw Error('已取消，没有修改 Webhook。');
  }
  const key = randomBytes(32).toString('hex');
  console.log('正在写入测试 Worker 的 Secret…');
  put('TEST_BOT_TOKEN',token);
  put('STAGING_REVIEW_CONFIG', JSON.stringify({
    reviewer_ids:local.reviewer_ids, min_common_groups:local.min_common_groups ?? 8,
    targets:{[group]:'这是测试申请。请简短说明你想加入测试群的原因。'},
  }));
  put('STAGING_WEBHOOK_SECRET',key);
  telegram(token,'setWebhook',{url:webhook,secret_token:key,allowed_updates:allowedUpdates,max_connections:1});
  if (telegram(token,'getWebhookInfo').url !== webhook) throw Error('Telegram 未确认测试 Webhook。');
  console.log('测试 Bot 已绑定。让审核管理员先私聊测试 Bot 点 Start，再用申请账号测试。');
} catch (error) {
  console.error(error.message);
  process.exitCode=1;
} finally { rl.close(); }
