import { DurableObject } from 'cloudflare:workers';
import { ReviewBot, telegram, matchesSecret } from './review-bot.mjs';
import { queryOwner, verifyAutoPins } from './owner-query.mjs';
import { initializeSchema, importInitialSnapshot } from './review-store.mjs';

const unavailable = () => new Response('Not found', { status: 404 });

export function routeStaging(request, env, pathname) {
  if (request.method !== 'POST') return unavailable();
  if (pathname === '/staging/webhook' && env.TEST_BOT_TOKEN && env.STAGING_REVIEW_CONFIG &&
      matchesSecret(request.headers.get('x-telegram-bot-api-secret-token'), env.STAGING_WEBHOOK_SECRET)) {
    return env.STAGING_REVIEW.getByName('test-bot').fetch(request);
  }
  if (pathname === '/staging/import-fixture' &&
      matchesSecret(request.headers.get('authorization'), env.STAGING_IMPORT_KEY ? `Bearer ${env.STAGING_IMPORT_KEY}` : '')) {
    return env.STAGING_REVIEW.getByName('import-fixture').fetch(request);
  }
  return unavailable();
}

export function routeProduction(request, env, pathname) {
  if (request.method !== 'POST' || !env.PRODUCTION_BOT_TOKEN || !env.PRODUCTION_REVIEW_CONFIG) return unavailable();
  const authorized = pathname === '/production/webhook'
    ? matchesSecret(request.headers.get('x-telegram-bot-api-secret-token'), env.PRODUCTION_WEBHOOK_SECRET)
    : ['/production/import', '/production/status'].includes(pathname) &&
      matchesSecret(request.headers.get('authorization'), env.PRODUCTION_IMPORT_KEY ? `Bearer ${env.PRODUCTION_IMPORT_KEY}` : '');
  return authorized ? env.STAGING_REVIEW.getByName('production-bot').fetch(request) : unavailable();
}

export class ReviewState extends DurableObject {
  constructor(ctx,env) {
    super(ctx,env);
    initializeSchema(ctx.storage.sql);
    ctx.storage.sql.exec('CREATE TABLE IF NOT EXISTS handled_updates (id INTEGER PRIMARY KEY)');
    this.queue = Promise.resolve();
  }
  async fetch(request) {
    const pathname = new URL(request.url).pathname;
    const production = pathname.startsWith('/production/');
    if (pathname === '/production/status') {
      return Response.json({imported: (await this.ctx.storage.get('production-imported')) === true});
    }
    if (production && pathname === '/production/webhook' && !(await this.ctx.storage.get('production-imported'))) {
      return new Response('Migration not ready', {status:503});
    }
    if (pathname === '/staging/import-fixture' || pathname === '/production/import') {
      try {
        const snapshot = await request.json();
        const counts = importInitialSnapshot(this.ctx.storage, snapshot, () => {
          if (production) this.ctx.storage.kv.put('production-imported', true);
        });
        if (production) await this.ctx.storage.setAlarm(Date.now() + 60000);
        return Response.json(counts);
      }
      catch { return new Response('Cannot import snapshot', { status: 409 }); }
    }
    let update;
    try { update = await request.json(); } catch { return new Response('Invalid update', { status: 400 }); }
    if (!Number.isSafeInteger(update.update_id)) return new Response('Invalid update', { status: 400 });
    const operation = this.queue.then(() => this.process(update, production));
    this.queue = operation.catch(() => {});
    try { await operation; return new Response('OK'); }
    catch (error) {
      console.warn('Staging update not completed', update.update_id, error?.name);
      return new Response('Try again', { status: 503 });
    }
  }
  bot(production = false) {
    const config = JSON.parse(production ? this.env.PRODUCTION_REVIEW_CONFIG : this.env.STAGING_REVIEW_CONFIG);
    if (Object.keys(config.targets ?? {}).length === 0 || !Array.isArray(config.reviewer_ids)) {
      throw new Error('Invalid staging configuration');
    }
    return new ReviewBot(this.ctx.storage.sql, telegram(production ? this.env.PRODUCTION_BOT_TOKEN : this.env.TEST_BOT_TOKEN), config, async (id, username, skipCommonGroups) => {
      try {
        const owner = JSON.parse(this.env.OWNER_PROBE_CONFIG);
        return await queryOwner({ ...owner, username, applicantId: id, skipCommonGroups });
      } catch { return null; }
    }, (...args) => verifyAutoPins(JSON.parse(this.env.OWNER_PROBE_CONFIG), ...args));
  }
  async process(update, production) {
    const { sql } = this.ctx.storage;
    if (sql.exec('SELECT id FROM handled_updates WHERE id=?',update.update_id).toArray().length) return;
    const bot = this.bot(production);
    await bot.update(update);
    sql.exec('INSERT INTO handled_updates VALUES (?)',update.update_id);
    sql.exec('DELETE FROM handled_updates WHERE id<?', update.update_id - 10000);
    const pending = sql.exec("SELECT id FROM requests WHERE state IN ('redeemed','closed') AND invite IS NOT NULL LIMIT 1").toArray().length;
    if (pending && (await this.ctx.storage.getAlarm()) === null) await this.ctx.storage.setAlarm(Date.now() + 60000);
  }
  async alarm() {
    const operation = this.queue.then(async () => {
      const r = this.ctx.storage.sql.exec("SELECT * FROM requests WHERE state IN ('redeemed','closed') AND invite IS NOT NULL LIMIT 1").toArray()[0];
      if (!r) return;
      await this.bot((await this.ctx.storage.get('production-imported')) === true).revoke(r);
      if (this.ctx.storage.sql.exec("SELECT id FROM requests WHERE state IN ('redeemed','closed') AND invite IS NOT NULL LIMIT 1").toArray().length) {
        await this.ctx.storage.setAlarm(Date.now()+60000);
      }
    });
    this.queue = operation.catch(() => {});
    await operation;
  }
}
