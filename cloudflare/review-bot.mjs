import { timingSafeEqual } from 'node:crypto';

import { handleGroupPins } from './review-pins.mjs';

export const allowedUpdates = ['message', 'callback_query', 'chat_join_request'];
export const activeMember = member => ['creator','administrator','member'].includes(member.status)
  || (member.status === 'restricted' && member.is_member);

export function matchesSecret(value, secret) {
  if (typeof value !== 'string' || !secret) return false;
  const a = new TextEncoder().encode(value), b = new TextEncoder().encode(secret);
  return a.length === b.length && timingSafeEqual(a, b);
}

export class TelegramError extends Error {}

export function telegram(token, transport = fetch) {
  return async (method, payload = {}) => {
    let reply;
    try {
      const response = await transport(`https://api.telegram.org/bot${token}/${method}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload), signal: AbortSignal.timeout(12000),
      });
      reply = await response.json();
    } catch {
      throw new TelegramError(`${method}: 网络请求失败`);
    }
    if (!reply.ok) throw new TelegramError(`${method}: ${reply.description ?? 'Telegram 请求失败'}`);
    return reply.result;
  };
}

export class ReviewBot {
  constructor(sql, api, config, commonGroups, verifyPins) {
    this.sql = sql; this.api = api; this.config = config; this.commonGroups = commonGroups; this.verifyPins = verifyPins;
  }
  first(query, ...params) { return this.sql.exec(query, ...params).toArray()[0]; }
  send(chat_id, text, keyboard) {
    return this.api('sendMessage', { chat_id, text, ...(keyboard ? { reply_markup: { inline_keyboard: keyboard } } : {}) });
  }
  async trySend(...args) { try { await this.send(...args); return true; } catch { return false; } }
  member(chat_id, user_id) { return this.api('getChatMember', { chat_id, user_id }); }
  request(id) { return this.first('SELECT * FROM requests WHERE id=?', id); }
  async authorized(r, uid) {
    if (!this.config.reviewer_ids.includes(uid)) return false;
    const m = await this.member(r.chat, uid);
    return m.status === 'creator' || (m.status === 'administrator' && m.can_invite_users);
  }
  async menu(uid) {
    const buttons = [];
    for (const id of Object.keys(this.config.targets)) {
      const target = await this.api('getChat', { chat_id: Number(id) });
      buttons.push([{ text: `申请加入 · ${target.title}`, callback_data: `apply:${id}` }]);
    }
    await this.send(uid, '请选择要加入的群或频道。', buttons);
  }
  async screen(r) {
    if (activeMember(await this.member(r.chat, r.uid))) return { stop: '您已在群，不需要再次审核。', state: 'closed' };
    let photos;
    try { photos = (await this.api('getUserProfilePhotos', { user_id: r.uid, limit: 1 })).total_count; }
    catch (error) {
      if (!/USER_ID_INVALID|user not found/i.test(error.message)) {
        return { stop: '暂时无法核验头像，请发送 /pending 重试。' };
      }
    }
    const minimum = this.config.min_common_groups ?? 8;
    const profile = await this.commonGroups(r.uid, r.username, minimum === 0 || photos === 0);
    if (profile?.deleted) return { stop: '账号已注销。', state: 'rejected' };
    photos ??= profile?.avatar === true ? 1 : profile?.avatar === false ? 0 : null;
    if (photos === 0) return { stop: '未检测到可见头像，请设置头像后重新申请。', state: 'rejected' };
    if (photos === null) return { stop: '暂时无法核验头像，请发送 /pending 重试。' };
    const count = minimum ? profile?.commonGroups : 0;
    if (!Number.isSafeInteger(count)) return { stop: '暂时无法核验共同群，请发送 /pending 重试。' };
    if (count < minimum) return { stop: `共同群不足${minimum}个`, state: 'rejected' };
    return { photos, common: `号主账号已确认 ${count} 个共同群` };
  }
  async reject(r, reason) {
    this.sql.exec("UPDATE requests SET state='rejected', answer=NULL WHERE id=?", r.id);
    const sent = await this.trySend(r.uid, `申请 #${r.id} · ${r.label}\n${reason ? '拒绝理由：'+reason : '审核结果：未通过'}`,
      [[{ text: '重新申请', callback_data: `apply:${r.chat}` }]]);
    return sent ? '已拒绝，审核结果已发送。' : '已拒绝，私聊未送达。';
  }
  invite(r) {
    return this.trySend(r.uid, `审核已通过：${r.label}。链接无时间限制，仅限本账户使用一次。`,
      [[{ text: '加入', url: r.invite }]]);
  }
  async apply(chat, user) {
    const question = this.config.targets[chat];
    if (!question) return;
    if (activeMember(await this.member(chat, user.id))) {
      await this.send(user.id, '您已在群，不需要再次审核。');
      return;
    }
    let r = this.first(`SELECT * FROM requests WHERE chat=? AND uid=? AND flow='private'
      AND state IN ('waiting','review','invited','redeeming') ORDER BY id DESC LIMIT 1`, chat, user.id);
    if (r?.state === 'invited') return this.invite(r);
    if (r && r.state !== 'waiting') return this.send(user.id, '申请正在审核或入群处理中。');
    if (!r) {
      const target = await this.api('getChat', { chat_id: chat });
      r = this.first(`INSERT INTO requests (chat,uid,dm,label,question,flow,username)
        VALUES (?,?,?,?,?,'private',?) RETURNING *`, chat, user.id, user.id, target.title, question, user.username ?? null);
    } else {
      r = this.first('UPDATE requests SET username=? WHERE id=? RETURNING *', user.username ?? null, r.id);
    }
    const check = await this.screen(r);
    if (check.state === 'rejected') return this.reject(r, check.stop);
    if (check.stop) return this.send(r.uid, check.stop);
    this.sql.exec('UPDATE requests SET photos=?,common=? WHERE id=?', check.photos, check.common, r.id);
    await this.send(r.uid, `申请 #${r.id} · ${r.label}\n${r.question}\n\n请直接回复这条消息（最多 1000 字）。`);
    this.sql.exec('UPDATE requests SET notified=1 WHERE id=?', r.id);
  }
  card(r, stage='review', result) {
    const username = /^[A-Za-z0-9_]+$/.test(r.username ?? '') ? r.username : '';
    const kb = [[{ text: username ? `查看 @${username}` : '查看用户资料',
      url: username ? `https://t.me/${username}` : `tg://user?id=${r.uid}` }]];
    const button = (text, action) => ({ text, callback_data: `${action}:${r.id}` });
    if (!result) {
      if (stage === 'choose') kb.push([button('回复理由','reject_reason'),button('仅发送结果','reject_silent')], [button('返回审核','review_back')]);
      else if (stage === 'reason') kb.push([button('仅发送结果','reject_silent'),button('返回审核','review_back')]);
      else kb.push([button('通过并发送专属链接','approve')], [button('拒绝','reject_menu')]);
    }
    const suffix = result ? `\n\n处理结果：${result}` : stage === 'reason' ? '\n\n请回复本条消息，填写拒绝理由。' : '';
    return { text: `申请 #${r.id} · ${r.label}\n用户 ID：${r.uid}\n共同群：${r.common}\n\n问题：${r.question}\n回答：${r.answer}${suffix}`,
      reply_markup: { inline_keyboard: kb } };
  }
  async editCard(r, admin, message, stage, result) {
    try {
      await this.api('editMessageText', { chat_id: admin, message_id: message, ...this.card(r, stage, result) });
    } catch (error) {
      if (!String(error.message).toLowerCase().includes('message is not modified')) throw error;
    }
    this.sql.exec(`INSERT INTO review_cards (admin,message,request,stage) VALUES (?,?,?,?)
      ON CONFLICT(admin,message) DO UPDATE SET stage=excluded.stage`, admin,message,r.id,result ? 'finished' : stage);
  }
  async push(r, uid) {
    const old = this.first('SELECT message,stage FROM review_cards WHERE admin=? AND request=? ORDER BY message DESC LIMIT 1', uid,r.id);
    if (old) return this.editCard(r,uid,old.message,old.stage);
    const result = await this.api('sendMessage', { chat_id: uid, ...this.card(r) });
    this.sql.exec('INSERT OR IGNORE INTO review_cards (admin,message,request) VALUES (?,?,?)',uid,result.message_id,r.id);
  }
  async finish(r, result) {
    for (const card of this.sql.exec('SELECT * FROM review_cards WHERE request=?',r.id).toArray()) {
      try { await this.editCard(r,card.admin,card.message,'review',result); } catch { /* /reviews still reflects authoritative state */ }
    }
  }
  async approve(r) {
    const check = await this.screen(r);
    if (check.state === 'rejected') return this.reject(r,check.stop);
    if (check.state === 'closed') {
      this.sql.exec("UPDATE requests SET state='closed',answer=NULL WHERE id=?",r.id);
      await this.trySend(r.uid,check.stop);
    }
    if (check.stop) return check.stop;
    const created = await this.api('createChatInviteLink', { chat_id:r.chat, name:`review-${r.id}`, creates_join_request:true });
    this.sql.exec("UPDATE requests SET state='invited',invite=?,answer=NULL WHERE id=?",created.invite_link,r.id);
    const sent = await this.invite(this.request(r.id));
    return sent ? '已通过，专属链接已发送。' : '已通过，可用 /pending 取回链接。';
  }
  async callback(q) {
    const [action,arg] = (q.data ?? '').split(':');
    const uid=q.from.id, chat=q.message?.chat, message=q.message?.message_id;
    const ack = async text => { try { await this.api('answerCallbackQuery', { callback_query_id:q.id, ...(text ? {text,show_alert:true} : {}) }); } catch {} };
    if (chat?.type !== 'private' || chat.id !== uid || !message) return ack();
    if (action === 'apply' && Object.hasOwn(this.config.targets,arg)) { await ack(); return this.apply(Number(arg),q.from); }
    if (!['approve','reject_menu','reject_reason','reject_silent','review_back'].includes(action)) return ack();
    const r=this.request(Number(arg));
    if (r?.flow !== 'private' || r.state !== 'review' || !await this.authorized(r,uid)) return ack('申请已处理或你没有权限。');
    const stages = { reject_menu:'choose', reject_reason:'reason', review_back:'review' };
    if (action in stages) { await this.editCard(r,uid,message,stages[action]); return ack(); }
    this.sql.exec('INSERT OR IGNORE INTO review_cards (admin,message,request) VALUES (?,?,?)',uid,message,r.id);
    const result = action === 'approve' ? await this.approve(r) : await this.reject(r,null);
    if (this.request(r.id).state !== 'review') { await this.finish(r,result); return ack(); }
    await ack(result);
  }
  async message(m) {
    if (['group','supergroup'].includes(m.chat.type)) return handleGroupPins(this, m);
    if (m.chat.type !== 'private' || !m.from || !m.text?.trim()) return;
    const uid=m.from.id,text=m.text.trim(),cmd=text.split(/\s+/,1)[0].split('@')[0];
    if (['/start','/apply'].includes(cmd)) return this.menu(uid);
    if (cmd === '/pending') {
      const rows=this.sql.exec("SELECT * FROM requests WHERE uid=? AND flow='private' AND state IN ('waiting','review','invited','redeeming')",uid).toArray();
      if (!rows.length) return this.menu(uid);
      for (const r of rows) {
        if (r.state === 'invited') await this.invite(r);
        else if (r.state === 'waiting') await this.apply(r.chat,m.from);
        else await this.send(uid,`申请 #${r.id} 正在审核或入群处理中。`);
      }
      return;
    }
    if (cmd === '/reviews' && this.config.reviewer_ids.includes(uid)) {
      for (const r of this.sql.exec("SELECT * FROM requests WHERE state='review' AND flow='private'").toArray()) {
        if (await this.authorized(r,uid)) await this.push(r,uid);
      }
      return;
    }
    if (text.length > 1000) return this.send(uid,'请将回复控制在 1000 字以内。');
    if (m.reply_to_message && this.config.reviewer_ids.includes(uid)) {
      const card=this.first("SELECT request FROM review_cards WHERE admin=? AND message=? AND stage='reason'",uid,m.reply_to_message.message_id);
      const r=card && this.request(card.request);
      if (r?.state === 'review' && await this.authorized(r,uid)) return this.finish(r,await this.reject(r,text));
    }
    const pending=this.sql.exec("SELECT * FROM requests WHERE uid=? AND flow='private' AND state='waiting' AND notified=1 AND photos>0",uid).toArray();
    const replyText=m.reply_to_message?.text ?? '';
    const matched=pending.find(item=>replyText.startsWith(`申请 #${item.id} · `));
    const r=matched ?? (pending.length === 1 && !m.reply_to_message ? pending[0] : null);
    if (!r) return this.send(uid,'请回复对应的审核题，或发 /pending 查看申请。');
    if (activeMember(await this.member(r.chat,uid))) {
      this.sql.exec("UPDATE requests SET state='closed' WHERE id=?",r.id);
      return this.send(uid,'您已在群，不需要再次审核。');
    }
    const saved=this.first("UPDATE requests SET state='review',answer=?,username=? WHERE id=? RETURNING *",text,m.from.username ?? null,r.id);
    await this.trySend(uid,'回答已提交，请等待管理员审核。');
    for (const admin of this.config.reviewer_ids) {
      try { if (await this.authorized(saved,admin)) await this.push(saved,admin); } catch { /* /reviews can recover the card */ }
    }
  }
  async join(j,event) {
    const chat=j.chat.id,uid=j.from.id;
    if (!Object.hasOwn(this.config.targets,String(chat))) return;
    const offered=j.invite_link?.invite_link;
    const r=offered ? this.first("SELECT * FROM requests WHERE chat=? AND invite=? AND flow='private'",chat,offered)
      : this.first("SELECT * FROM requests WHERE chat=? AND uid=? AND flow='private' AND invite IS NOT NULL AND state IN ('invited','redeeming') ORDER BY id DESC LIMIT 1",chat,uid);
    if (r?.uid !== uid || !(r.state === 'invited' || (r.state === 'redeeming' && r.redeem_event === event))) {
      try { await this.api('declineChatJoinRequest',{chat_id:chat,user_id:uid}); }
      catch (error) { if (!String(error.message).includes('HIDE_REQUESTER_MISSING')) throw error; }
      return;
    }
    this.sql.exec("UPDATE requests SET state='redeeming',redeem_event=? WHERE id=?",event,r.id);
    try { await this.api('approveChatJoinRequest',{chat_id:chat,user_id:uid}); }
    catch (error) {
      if (!/HIDE_REQUESTER_MISSING|USER_ALREADY_PARTICIPANT/.test(error.message)) throw error;
      if (!activeMember(await this.member(chat,uid))) {
        this.sql.exec("UPDATE requests SET state='closed' WHERE id=?",r.id);
        return this.revoke(r);
      }
    }
    this.sql.exec("UPDATE requests SET state='redeemed' WHERE id=?",r.id);
    await this.revoke(r);
  }
  async revoke(r) {
    try { await this.api('revokeChatInviteLink',{chat_id:r.chat,invite_link:r.invite}); }
    catch (error) {
      if (!/INVITE_HASH_INVALID|INVITE_HASH_EXPIRED/.test(error.message)) return false;
    }
    this.sql.exec('UPDATE requests SET invite=NULL WHERE id=?',r.id);
    return true;
  }
  async update(update) {
    if (update.callback_query) await this.callback(update.callback_query);
    else if (update.chat_join_request) await this.join(update.chat_join_request,update.update_id);
    else if (update.message) await this.message(update.message);
  }
}
