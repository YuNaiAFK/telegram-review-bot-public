import { queryOwner } from './owner-query.mjs';

const required = ['TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TELEGRAM_OWNER_SESSION', 'TELEGRAM_OWNER_ID', 'TEST_APPLICANT_USERNAME'];
const missing = required.filter(name => !process.env[name]);
if (missing.length) throw new Error(`缺少环境变量：${missing.join(', ')}`);
try {
  console.log(JSON.stringify(await queryOwner({
    apiId: process.env.TELEGRAM_API_ID,
    apiHash: process.env.TELEGRAM_API_HASH,
    session: process.env.TELEGRAM_OWNER_SESSION,
    ownerId: process.env.TELEGRAM_OWNER_ID,
    username: process.env.TEST_APPLICANT_USERNAME,
  })));
} catch (error) {
  console.error('共同群探测未完成：', error?.errorMessage ?? error?.name ?? '未知错误');
  process.exitCode = 1;
}
