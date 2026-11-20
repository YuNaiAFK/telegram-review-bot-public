import { Api, TelegramClient } from 'teleproto';
import { StringSession } from 'teleproto/sessions';

export async function withOwner(config, operation) {
  const client = new TelegramClient(
    new StringSession(config.session),
    Number(config.apiId),
    config.apiHash,
    { connectionRetries: 1, requestRetries: 1, floodSleepThreshold: 0 },
  );
  try {
    await client.connect();
    const owner = await client.getMe();
    if (!owner || owner.bot || String(owner.id) !== String(config.ownerId)) {
      throw new Error('登录会话与配置的号主 ID 不一致');
    }
    return await operation(client);
  } finally {
    await client.disconnect();
  }
}

export async function queryOwner(config) {
  return withOwner(config, async client => {
    let peer;
    if (config.username) peer = await client.getInputEntity(config.username);
    else if (config.applicantId) {
      const dialogs = await client.getDialogs({ limit: 100 });
      peer = dialogs.find(dialog => dialog.isUser && String(dialog.id) === String(config.applicantId))?.inputEntity;
    }
    if (config.applicantId && String(peer?.userId) !== String(config.applicantId)) {
      throw new Error('未能确认申请人账号');
    }
    if (!(peer instanceof Api.InputPeerUser)) throw new Error('测试对象必须是普通用户');
    const userId = new Api.InputUser({ userId: peer.userId, accessHash: peer.accessHash });
    let avatar = null;
    if (config.applicantId) {
      const users = await client.invoke(new Api.users.GetUsers({ id: [userId] }));
      const user = users.find(item => item instanceof Api.User && String(item.id) === String(config.applicantId));
      if (user?.deleted) return { ownerVerified: true, deleted: true, avatar: null, commonGroups: null };
      if (user?.photo instanceof Api.UserProfilePhoto && !user.photo.personal) avatar = true;
      else if (user && !user.min && (!user.photo || user.photo instanceof Api.UserProfilePhotoEmpty)) avatar = false;
      if (config.skipCommonGroups) return { ownerVerified: true, avatar, commonGroups: 0 };
    }
    const groups = new Set();
    let maxId = 0n;
    for (;;) {
      const result = await client.invoke(new Api.messages.GetCommonChats({ userId, maxId, limit: 100 }));
      if (!result.chats.length) break;
      let next = null;
      for (const chat of result.chats) {
        const id = BigInt(chat.id.toString());
        if (next === null || id < next) next = id;
        if ((chat instanceof Api.Chat && !chat.deactivated && !chat.migratedTo) || chat instanceof Api.ChatForbidden) {
          groups.add(`group:${id}`);
        } else if ((chat instanceof Api.Channel || chat instanceof Api.ChannelForbidden) && chat.megagroup) {
          groups.add(`supergroup:${id}`);
        }
      }
      if (result instanceof Api.messages.Chats) break;
      if (next === null || next <= 0n || (maxId !== 0n && next >= maxId)) throw new Error('Telegram 返回了不可继续的分页');
      maxId = next;
    }
    return { ownerVerified: true, commonGroups: groups.size, avatar };
  });
}

export async function verifyAutoPins(config, chat, linked, observed, limit) {
  if (!String(linked).startsWith('-100') || !observed.size) return [];
  const source = String(linked).slice(4);
  const isSource = peer => peer instanceof Api.PeerChannel && String(peer.channelId) === source;
  return withOwner(config, async client => {
    const dialogs = await client.getDialogs({ limit: 100 });
    const peer = dialogs.find(d => String(d.id) === String(chat))?.inputEntity;
    if (!peer) throw Error('无法定位讨论群');
    const earliest = Math.min(...observed.values());
    const protectedIds = new Set();
    for await (const event of client.iterAdminLog(peer, { pinned: true })) {
      if (event.date < earliest) break;
      if (event.action instanceof Api.ChannelAdminLogEventActionUpdatePinned) protectedIds.add(event.action.message.id);
    }
    const selected = [];
    for await (const message of client.iterMessages(peer, { filter: Api.InputMessagesFilterPinned })) {
      if (!observed.has(message.id) || protectedIds.has(message.id)) continue;
      if (!isSource(message.fromId) || !isSource(message.fwdFrom?.fromId) || !message.fwdFrom?.channelPost) continue;
      selected.push(message.id);
      if (limit && selected.length >= limit) break;
    }
    return selected;
  });
}
