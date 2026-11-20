import { readFileSync } from 'node:fs';

export function loadDeployment() {
  let config;
  try {
    config = JSON.parse(readFileSync(new URL('./deployment.local.json', import.meta.url), 'utf8').replace(/^\uFEFF/, ''));
  } catch {
    throw new Error('请复制 deployment.example.json 为 deployment.local.json 并填写本机部署配置。');
  }
  const { workerName, workerUrl, testBotUsername, productionBotUsername } = config;
  const username = value => typeof value === 'string' && /^[a-zA-Z0-9_]+bot$/i.test(value);
  let url;
  try { url = new URL(workerUrl); } catch { throw new Error('workerUrl 必须是 Cloudflare Worker 的 HTTPS 地址。'); }
  if (typeof workerName !== 'string' || !/^[a-z0-9-]{1,63}$/.test(workerName)
      || url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/'
      || !username(testBotUsername) || !username(productionBotUsername)
      || testBotUsername.toLowerCase() === productionBotUsername.toLowerCase()) {
    throw new Error('部署配置不正确；请检查 Worker 地址以及两个不同的 Bot 用户名。');
  }
  return { workerName, workerUrl: url.origin, testBotUsername, productionBotUsername };
}
