import { spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline/promises';
import { fileURLToPath } from 'node:url';
import { readSnapshot } from './migration-snapshot.mjs';
import { allowedUpdates } from './review-bot.mjs';

import { loadDeployment } from './deployment-config.mjs';

const deployment = loadDeployment();
const project = fileURLToPath(new URL('../',import.meta.url));
const cwd = fileURLToPath(new URL('./',import.meta.url));
const base = deployment.workerUrl + '/production';
const token = process.env.TELEGRAM_BOT_TOKEN;
const rl = createInterface({input:process.stdin,output:process.stdout});
let proxy;
let stage = '预检';

function request(url, body, key) {
  const cfg = [`url = ${JSON.stringify(url)}`, 'header = "Content-Type: application/json"',
    ...(key ? [`header = "Authorization: Bearer ${key}"`] : []),
    `data = ${JSON.stringify(JSON.stringify(body))}`].join('\n');
  const result = spawnSync('curl.exe', ['--config','-','--silent','--max-time','25',
    ...(proxy ? ['--proxy',proxy,'--noproxy',''] : []), '--write-out','\n%{http_code}'], {
    input:cfg+'\n',encoding:'utf8',windowsHide:true,stdio:['pipe','pipe','ignore'],timeout:28000,
  });
  if (result.status !== 0) throw Error('网络请求失败');
  const end=result.stdout.lastIndexOf('\n');
  if (result.stdout.slice(end+1) !== '200') throw Error('接口返回 HTTP '+result.stdout.slice(end+1));
  return JSON.parse(result.stdout.slice(0,end));
}
function api(method, body={}) {
  const answer=request(`https://api.telegram.org/bot${token}/${method}`,body);
  if (!answer.ok) throw Error(`${method} 未成功`);
  return answer.result;
}
function put(name,value) {
  const result=spawnSync(process.env.ComSpec || 'cmd.exe',
    ['/d','/c','npx.cmd','--yes','--offline','wrangler','secret','put',name,'--name',deployment.workerName],{
      cwd,input:value+'\n',encoding:'utf8',windowsHide:true,stdio:['pipe','ignore','ignore'],timeout:35000,
    });
  if(result.status!==0) throw Error(`未能写入 ${name}`);
}
function checkStopped() {
  const code = `
    $ErrorActionPreference='Stop'
    $p=Join-Path (Get-Location) 'bot.lock'
    if (Test-Path -LiteralPath $p) {
      $file=[IO.File]::Open($p,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
      try { $file.Lock(0,1); $file.Unlock(0,1) } finally { $file.Dispose() }
    }
  `;
  const result=spawnSync('powershell.exe',['-NoProfile','-NonInteractive','-EncodedCommand',
    Buffer.from(code,'utf16le').toString('base64')],{
    cwd:project,windowsHide:true,stdio:'ignore',timeout:10000,
  });
  if (result.status!==0) throw Error('Windows Bot 仍在运行或数据库锁无法检查。请在控制台停止 Bot。');
}
try {
  if (!/^\d+:[A-Za-z0-9_-]+$/.test(token ?? '')) throw Error('请从 Windows 启动脚本运行，不要在命令行填写 Token');
  const config=JSON.parse((await readFile(new URL('../config.json',import.meta.url),'utf8')).replace(/^\uFEFF/,''));
  proxy=config.telegram_http_proxy || null;
  const identity=api('getMe');
  if (identity.username?.toLowerCase()!==deployment.productionBotUsername.toLowerCase()) throw Error('Token 不是正式审核 Bot，不切换。');
  const old=api('getWebhookInfo');
  if (old.url) {
    if(old.url===base+'/webhook') throw Error('正式 Bot 已指向 Cloudflare；请不要重复导入。');
    throw Error('正式 Bot 已有其他 Webhook，不覆盖。');
  }
  for (const target of Object.keys(config.targets)) {
    const permissions=api('getChatMember',{chat_id:Number(target),user_id:identity.id});
    if(permissions.status!=='administrator'||!permissions.can_invite_users) throw Error('正式 Bot 缺少目标群的邀请权限。');
  }
  checkStopped();
  if ((await rl.question('确认 Windows Bot 已停止，并切换正式 Bot 到免费 Cloudflare？输入 SWITCH：')).trim()!=='SWITCH') {
    throw Error('已取消切换。');
  }
  stage='保存正式 Worker 凭据';
  const key=randomBytes(32).toString('hex');
  const webhookKey=randomBytes(32).toString('hex');
  put('PRODUCTION_BOT_TOKEN',token);
  put('PRODUCTION_REVIEW_CONFIG',JSON.stringify({
    targets:config.targets,reviewer_ids:config.reviewer_ids,min_common_groups:config.min_common_groups,
  }));
  put('PRODUCTION_IMPORT_KEY',key);
  put('PRODUCTION_WEBHOOK_SECRET',webhookKey);
  stage='导入最终审核数据';
  checkStopped();
  const snapshot=readSnapshot();
  const imported=request(base+'/import',snapshot,key);
  for(const [name,rows] of Object.entries(snapshot.tables)) {
    if(imported[name]!==rows.length) throw Error('云端导入数量不一致');
  }
  if(!request(base+'/status',{},key).imported) throw Error('云端未确认迁移完成');
  console.log('云端数据已导入：',JSON.stringify(imported));
  stage='切换正式 Webhook';
  api('setWebhook',{url:base+'/webhook',secret_token:webhookKey,allowed_updates:allowedUpdates,max_connections:1});
  if(api('getWebhookInfo').url!==base+'/webhook') throw Error('Telegram 未确认正式 Webhook');
  console.log('正式 Bot 已切换到 Cloudflare。请私聊正式 Bot 发送 /start 验证；不要同时启动 Windows 轮询。');
} catch(error) {
  console.error(`${stage}未完成：${error.message}`);
  console.error('不要盲目重复切换；请先核对正式 Bot 的 Webhook 状态。');
  process.exitCode=1;
} finally { rl.close(); }
