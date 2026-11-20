import { timingSafeEqual } from 'node:crypto';
import { queryOwner } from './owner-query.mjs';
import { ReviewState, routeProduction, routeStaging } from './review-runtime.mjs';
export { ReviewState };
import { createConnection } from 'node:net';
import { DurableObject } from 'cloudflare:workers';
import { Api, TelegramClient } from 'teleproto';
import { StringSession } from 'teleproto/sessions';

export class OwnerProbe extends DurableObject {
  async transportCheck() {
    const saved = await this.ctx.storage.get('telegram-tcp-v1');
    if (saved) return saved;
    const status = await new Promise(resolve => {
      let socket;
      let timer;
      const done = result => {
        clearTimeout(timer);
        socket?.destroy();
        resolve(result);
      };
      try {
        socket = createConnection({ host: '91.108.56.108', port: 443 });
        timer = setTimeout(() => done('timeout'), 4500);
        socket.once('connect', () => done('connected'));
        socket.once('error', () => done('connection-error'));
      } catch {
        done('runtime-unavailable');
      }
    });
    await this.ctx.storage.put('telegram-tcp-v1', status);
    return status;
  }

  async fetch(request) {
    if (request.method === 'POST' && new URL(request.url).pathname === '/verify-owner') {
      const key = this.env.PROBE_ACCESS_KEY;
      const config = this.env.OWNER_PROBE_CONFIG;
      if (!key || !config) return new Response('Not found', { status: 404 });
      const supplied = request.headers.get('authorization') ?? '';
      const a = new TextEncoder().encode(supplied);
      const b = new TextEncoder().encode(`Bearer ${key}`);
      if (a.length !== b.length || !timingSafeEqual(a, b)) return new Response('Not found', { status: 404 });
      try {
        const settings = JSON.parse(config);
        const result = await queryOwner(settings);
        await this.ctx.storage.put('owner-query-verified',
          Number.isSafeInteger(settings.expected) && result.commonGroups === settings.expected);
        return Response.json(result, { headers: { 'Cache-Control': 'no-store' } });
      } catch {
        await this.ctx.storage.put('owner-query-verified', false);
        return Response.json({ ownerVerified: false }, { status: 502 });
      }
    }
    const persisted = this.ctx.storage.sql.exec('SELECT 1 AS ready').one().ready === 1;
    const codecRequest = new Api.messages.GetCommonChats({
      userId: new Api.InputUserSelf(), maxId: 0n, limit: 100,
    });
    this.transport ??= this.transportCheck();
    return Response.json({
      sqlite: persisted,
      mtprotoCodec: codecRequest.getBytes().length > 0,
      clientModule: Boolean(TelegramClient && new StringSession('')),
      telegramTcp: await this.transport,
      liveTelegramVerified: (await this.ctx.storage.get('owner-query-verified')) === true,
    });
  }
}

export default {
  fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname.startsWith('/production/')) return routeProduction(request, env, pathname);
    if (pathname.startsWith('/staging/')) return routeStaging(request, env, pathname);
    if ((request.method === 'GET' && pathname === '/health')
        || (request.method === 'POST' && pathname === '/verify-owner')) {
      return env.PROBE.getByName('health').fetch(request);
    }
    return new Response('Not found', { status: 404 });
  },
};
