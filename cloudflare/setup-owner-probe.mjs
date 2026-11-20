import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline/promises';
import { Writable } from 'node:stream';
import { fileURLToPath } from 'node:url';
import { TelegramClient } from 'teleproto';
import { StringSession } from 'teleproto/sessions';
import { loadProbeState, saveProbeState } from './probe-state.mjs';

import { loadDeployment } from './deployment-config.mjs';

const deployment = loadDeployment();
const root = fileURLToPath(new URL('.', import.meta.url));
const worker = deployment.workerName;
const verifyUrl = deployment.workerUrl + '/verify-owner';
const command = process.env.ComSpec || 'cmd.exe';

function putSecret(name, value) {
  // Only fixed names go on the command line. The secret travels via stdin.
  const allowed = new Set(['OWNER_PROBE_CONFIG', 'PROBE_ACCESS_KEY']);
  if (!allowed.has(name)) throw new Error('不允许的 Secret 名称');
  const result = spawnSync(command, ['/d', '/c', 'npx.cmd', '--yes', '--offline',
    'wrangler', 'secret', 'put', name, '--name', worker], {
    cwd: root, input: value + '\n', encoding: 'utf8',
    stdio: ['pipe', 'ignore', 'ignore'], timeout: 30000, windowsHide: true,
  });
  if (result.status !== 0) throw new Error(`未能写入 ${name}，请检查 Wrangler 授权和 Cloudflare 网络。`);
}

function verifyInCloud(key, proxyPort) {
  if (!/^[a-f0-9]{64}$/.test(key)) throw new Error('本机验证密钥无效');
  const proxy = proxyPort ? ['--proxy', `socks5h://127.0.0.1:${proxyPort}`, '--noproxy', ''] : ['--noproxy', '*'];
  const run = spawnSync('curl.exe', ['--config', '-', '--silent', '--show-error', '--max-time', '25',
    ...proxy, '--request', 'POST', '--write-out', '\n%{http_code}', verifyUrl], {
    input: `header = "Authorization: Bearer ${key}"\n`,
    encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'], timeout: 28000, windowsHide: true,
  });
  if (run.status !== 0) throw new Error(`无法连接 Cloudflare 测试接口（curl ${run.status ?? run.error?.code ?? '未知'}）。请检查本机代理。`);
  const boundary = run.stdout.lastIndexOf('\n');
  const status = Number(run.stdout.slice(boundary + 1));
  if (status !== 200) throw new Error(`测试接口返回 HTTP ${status}`);
  return JSON.parse(run.stdout.slice(0, boundary));
}

async function askApiInDialog() {
  const source = await readFile(new URL('./api-input-dialog.ps1', import.meta.url), 'utf8');
  const encoded = Buffer.from(source.replace(/^\uFEFF/, ''), 'utf16le').toString('base64');
  console.log('正在打开 Telegram API credentials 窗口。若被遮挡，请用 Alt+Tab 切换。');
  return await new Promise((resolve, reject) => {
    const child = spawn('powershell.exe', ['-NoProfile', '-STA', '-EncodedCommand', encoded], {
      stdio: ['ignore', 'pipe', 'ignore'], windowsHide: false,
    });
    let data = '';
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => { data += chunk; });
    child.on('error', () => reject(new Error('无法启动凭据输入窗口')));
    child.on('close', status => {
      if (status !== 0) {
        reject(new Error(status === 2 ? '已取消凭据输入' : '无法打开凭据输入窗口'));
        return;
      }
      try { resolve(JSON.parse(data)); }
      catch { reject(new Error('凭据窗口没有返回有效结果')); }
    });
    const cancel = () => child.kill();
    process.once('SIGINT', cancel);
    rl.once('SIGINT', cancel);
    child.once('close', () => {
      process.off('SIGINT', cancel);
      rl.off('SIGINT', cancel);
    });
  });
}

if (!process.stdin.isTTY) throw new Error('请在本机 PowerShell 窗口运行此向导。');

let hidden = false;
const output = new Writable({
  write(chunk, _encoding, done) {
    if (!hidden) process.stdout.write(chunk);
    done();
  },
});
const rl = createInterface({ input: process.stdin, output, terminal: true });
rl.on('SIGINT', () => { process.stdout.write('\n已取消。\n'); if (rl.listenerCount('SIGINT') === 1) process.exit(130); });
const ask = async (label, secret = false) => {
  if (secret) process.stdout.write(label);
  hidden = secret;
  try {
    return (await rl.question(secret ? '' : label)).trim();
  } finally {
    hidden = false;
    if (secret) process.stdout.write('\n');
  }
};

let client;
let stage = '读取本地号主 ID';
try {
  const local = JSON.parse((await readFile(new URL('../config.json', import.meta.url), 'utf8')).replace(/^\uFEFF/, ''));
  const ownerId = String(local.owner_id);
  if (!/^\d+$/.test(ownerId) || ownerId === '0') throw new Error('未配置号主 ID');
  stage = '读取加密配置';
  const args = new Set(process.argv.slice(2));
  let saved = args.has('--reset') ? null : loadProbeState(root);
  if (saved && saved.ownerId !== ownerId) saved = null;
  let state = saved ?? { ownerId };
  const remember = () => saveProbeState(root, state);

  console.log('此向导只配置免费的测试 Worker，不修改正式 Bot。');
  if (!saved) {
    if ((await ask('继续并将新 Telegram 会话存入测试 Worker Secret？输入 YES：')).toUpperCase() !== 'YES') {
      throw new Error('未确认');
    }
  } else {
    console.log('已读取本机加密配置，只补充未完成的步骤。');
  }
  stage = '录入 API 凭据';
  if (!state.apiId || !state.apiHash) {
    const api = process.platform === 'win32' && !args.has('--terminal-api')
      ? await askApiInDialog()
      : { apiId: await ask('Telegram API ID：'), apiHash: await ask('Telegram API hash（隐藏）：', true) };
    const apiId = Number(api.apiId);
    if (!Number.isSafeInteger(apiId) || apiId <= 0 || !/^[a-fA-F0-9]{32}$/.test(api.apiHash)) {
      throw new Error('输入格式不正确');
    }
    Object.assign(state, { apiId, apiHash: api.apiHash });
    remember();
    console.log('API 凭据已加密保存在此 Windows 账户。');
  }
  if (args.has('--edit-test')) {
    delete state.username; delete state.expected; delete state.proxyPort;
    state.uploaded = false;
    remember();
  }
  if (args.has('--relogin')) {
    delete state.session;
    state.uploaded = false;
    remember();
  }
  stage = '录入测试配置';
  if (!state.username) {
    const username = (await ask('已知共同群数量的测试账号 @用户名：')).replace(/^@/, '');
    if (!/^[A-Za-z0-9_]+$/.test(username)) throw new Error('用户名格式不正确');
    state.username = username;
    remember();
  }
  if (!Number.isSafeInteger(state.expected)) {
    const input = await ask('该测试账号已知共同群数量（包含普通群和超级群）：');
    if (!/^\d+$/.test(input) || !Number.isSafeInteger(Number(input))) throw new Error('数量格式不正确');
    state.expected = Number(input);
    remember();
  }
  if (!Number.isInteger(state.proxyPort)) {
    const port = await ask('本机 Clash SOCKS5 / 混合端口（回车使用 7897，填 0 则直连）：');
    const proxyPort = port === '' ? 7897 : Number(port);
    if (!Number.isInteger(proxyPort) || proxyPort < 0 || proxyPort > 65535) throw new Error('代理端口无效');
    state.proxyPort = proxyPort;
    remember();
  }
  const { apiId, apiHash, username, expected, proxyPort } = state;
  if (!state.session) {
    stage = 'Telegram 登录';
    client = new TelegramClient(new StringSession(''), apiId, apiHash, {
      connectionRetries: 1,
      // Teleproto counts total attempts. A second attempt is required
      // after Telegram redirects sign-in to the phone's home data center.
      requestRetries: 3,
      floodSleepThreshold: 0,
      ...(proxyPort ? { proxy: { ip: '127.0.0.1', port: proxyPort, socksType: 5 } } : {}),
    });
    let phoneForLogin;
    await client.start({
      phoneNumber: async () => {
        if (phoneForLogin) return phoneForLogin;
        for (;;) {
          const phone = (await ask('号主手机号（例如 +86 后接手机号，隐藏）：', true)).replace(/[ ()-]/g, '');
          if (/^\+[1-9]\d{6,14}$/.test(phone)) {
            phoneForLogin = phone;
            return phone;
          }
          console.log('请输入包含国家区号的完整号码，例如 +86 后接中国大陆手机号。');
        }
      },
      phoneCode: () => ask('Telegram 登录验证码（隐藏）：', true),
      password: () => ask('Telegram 两步验证密码（隐藏）：', true),
      onError: error => { throw error; },
    });
    const me = await client.getMe();
    if (!me || me.bot || String(me.id) !== ownerId) throw new Error('登录账号与号主 ID 不一致');
    const session = client.session.save();
    if (typeof session !== 'string' || !session) throw new Error('未取得有效会话');
    Object.assign(state, { session, uploaded: false });
    remember();
    await client.disconnect();
    client = null;
  }

  stage = '写入免费验证 Worker Secret';
  if (!state.key) {
    state.key = randomBytes(32).toString('hex');
    remember();
  }
  const key = state.key;
  if (!state.uploaded) {
    console.log('正在写入测试 Worker 的 Cloudflare Secrets…');
    putSecret('PROBE_ACCESS_KEY', key);
    putSecret('OWNER_PROBE_CONFIG', JSON.stringify({ apiId, apiHash, ownerId, session: state.session, username, expected }));
    state.uploaded = true;
    remember();
  }

  stage = 'Cloudflare 上验证共同群';
  console.log('正在通过当前代理访问 Cloudflare 验证接口（最多等待 25 秒）…');
  const result = verifyInCloud(key, proxyPort);
  if (!result.ownerVerified || result.commonGroups !== expected) {
    throw new Error(`对照未通过：预期 ${expected}，云端返回 ${result.commonGroups ?? '未知'}`);
  }
  console.log(`Cloudflare 已验证：与测试账号共有 ${result.commonGroups} 个群，与已知数量一致。`);
} catch (error) {
  const name = typeof error?.name === 'string' ? error.name : 'Error';
  if (stage === 'Cloudflare 上验证共同群' && /^(测试接口返回 HTTP |对照未通过：|无法连接 Cloudflare)/.test(error.message)) {
    console.error(error.message);
  } else if (stage === 'Telegram 登录') {
    console.error(name === 'PhoneNumberInvalidError'
      ? 'Telegram 不接受该手机号。重跑后会保留已填配置；请用 +国家区号 加完整手机号重试。'
      : name === 'PhoneMigrateError'
        ? 'Telegram 仍要求切换数据中心，登录未完成。已填配置已保留。'
        : `Telegram 登录未完成（${name}）。已填配置保留，可重新运行继续。`);
  } else {
    console.error(`未通过：${stage}（${name}）。`);
  }
  process.exitCode = 1;
} finally {
  rl.close();
  if (client) await client.disconnect().catch(() => {});
}
