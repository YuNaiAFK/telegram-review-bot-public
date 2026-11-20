const pinCommands = new Set(['/unpin','/unpinall','/unpinold','/autounpin']);
const age = 47 * 3600;
const canPin = m => m.status === 'creator' || (m.status === 'administrator' && m.can_pin_messages);

export async function handleGroupPins(bot, m) {
  const chat = m.chat.id, sql = bot.sql;
  const now = Math.floor(Date.now() / 1000);
  const top = m.is_automatic_forward;
  const forward = top ? m : m.pinned_message;
  const clear = async id => {
    await bot.api('unpinChatMessage', {chat_id:chat,message_id:id});
    sql.exec("UPDATE pin_provenance SET kind='cleared' WHERE chat=? AND message=?",chat,id);
  };
  if (forward?.is_automatic_forward) {
    const id = forward.message_id;
    if (!top && (!forward.sender_chat?.id || m.sender_chat?.id !== forward.sender_chat.id ||
          m.sender_chat?.type !== 'channel')) {
      sql.exec(`INSERT INTO pin_provenance VALUES (?,?,?,'manual')
        ON CONFLICT(chat,message) DO UPDATE SET kind='manual', observed=excluded.observed`,chat,id,m.date ?? now);
      return;
    }
    if (now - (m.date ?? now) >= age) return;
    sql.exec("INSERT OR IGNORE INTO pin_provenance VALUES (?,?,?,'auto')",chat,id,m.date ?? now);
    if (now - (m.date ?? now) < 60 && bot.first('SELECT 1 FROM auto_unpin_groups WHERE chat=?',chat) &&
        bot.first('SELECT kind FROM pin_provenance WHERE chat=? AND message=?',chat,id)?.kind === 'auto') {
      try { await clear(id); } catch { console.warn('Cannot clear automatic pin'); }
    }
    return;
  }
  const [entry, arg, ...extra] = m.text?.trim().split(/\s+/) ?? [];
  const [command, mention] = (entry ?? '').split('@');
  if (!pinCommands.has(command) || !m.from || m.from.is_bot || m.sender_chat) return;
  const self = await bot.api('getMe');
  if (mention && self.username.toLowerCase() !== mention.toLowerCase()) return;
  if (!canPin(await bot.member(chat,m.from.id))) return bot.send(chat,'只有有置顶权限的管理员可以使用此指令。');
  const info = await bot.api('getChat',{chat_id:chat});
  if (!info.linked_chat_id) return bot.send(chat,'请在频道的关联讨论群使用此指令。');
  if (!canPin(await bot.member(chat,self.id))) return bot.send(chat,'请给 Bot 授予本群的「置顶消息」权限。');
  async function deleteCommand() {
    try { await bot.api('deleteMessage',{chat_id:chat,message_id:m.message_id}); } catch {}
  }
  if (command === '/autounpin') {
    if (!['on','off'].includes(arg) || extra.length) return bot.send(chat,'用法：/autounpin on 或 /autounpin off。');
    if (arg === 'on') sql.exec('INSERT OR IGNORE INTO auto_unpin_groups VALUES (?)',chat);
    else sql.exec('DELETE FROM auto_unpin_groups WHERE chat=?',chat);
    await deleteCommand();
    return bot.send(chat,arg === 'on' ? '已开启自动取消频道帖置顶。' : '已关闭自动取消频道帖置顶。');
  }
  if (arg) return bot.send(chat,`用法：${command}，不需要参数。`);
  const target = command === '/unpin' ? (m.reply_to_message ?? info.pinned_message)?.message_id : null;
  const observed = new Map(sql.exec("SELECT message,observed FROM pin_provenance WHERE chat=? AND kind='auto' AND observed>=?",chat,now-age)
    .toArray().filter(row => command !== '/unpin' || row.message === target).map(r=>[r.message,r.observed]));
  if (!observed.size) return bot.send(chat,'没有可确认的自动置顶；手动和来源不明的置顶已保留。');
  let confirmed;
  try { confirmed = await bot.verifyPins(chat,info.linked_chat_id,observed,command==='/unpinall'?null:command==='/unpinold'?20:1); }
  catch { return bot.send(chat,'无法核验近期群操作记录，未取消任何置顶。'); }
  const ids=confirmed.filter(id=>observed.has(id));
  if (!ids.length) return bot.send(chat,'没有可确认的自动置顶；手动和来源不明的置顶已保留。');
  for (const id of ids) {
    try { await clear(id); }
    catch { return bot.send(chat,'清理中止，请稍后重试；没有清空全部置顶。'); }
  }
  await deleteCommand();
}
