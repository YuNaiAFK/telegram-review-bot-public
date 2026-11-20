import {api, db} from 'sdk';
import {and, eq, lt, gt} from 'sdk/db';
import {t} from '../schema.js';
import c from './config.js';

const send = (chat_id,text,extra) => api.sendMessage({chat_id,text,...extra});
async function notify(chat,text) {
  try { await send(chat,text); return true; } catch { return false; }
}
const member = (chat_id,user_id) => api.getChatMember({chat_id,user_id});
const photo = async user_id => (await api.getUserProfilePhotos({user_id,limit:1})).total_count > 0;
const get = id => db.select().from(t).where(eq(t.id,id)).get();
const change = (r, values) => db.update(t).set(values)
  .where(and(eq(t.id,r.id),eq(t.state,r.state))).returning();
const prompt = r => `申请 #${r.id} · ${r.title}\n${r.question}\n\n/answer ${r.id} 你的回答`;
const noPhoto = '未检测到可见头像，请设置后重新申请。';
async function auth(r,uid) {
  if (!c.reviewers.includes(uid)) return false;
  const m = await member(r.chat,uid);
  return m.status === 'creator' || (m.status === 'administrator' && m.can_invite_users);
}
function active(m) {
  return ['creator','administrator','member'].includes(m.status) || (m.status==='restricted' && m.is_member);
}
async function common(uid) {
  const groups = [...new Set(c.groups)];
  if (!groups.length) return '未配置核验群';
  const checks = await Promise.allSettled(groups.map(async chat_id => {
    if (!['group','supergroup'].includes((await api.getChat({chat_id})).type)) throw Error('不是群');
    return (await Promise.all([member(chat_id,c.owner),member(chat_id,uid)])).every(active);
  }));
  return `已确认 ${checks.filter(x=>x.value===true).length}/${groups.length} 个，${checks.filter(x=>x.status==='rejected').length} 个无法核验`;
}
async function card(r,uid,count) {
  return send(uid,`待审 #${r.id} · ${r.title}\n用户 ID：${r.uid}\n头像：已检测到\n共同群：${count}\n\n问题：${r.question}\n回答：${r.answer}\n\n拒绝：/reject ${r.id} 原因`,
    {reply_markup:{inline_keyboard:[[{text:'同意加入',callback_data:`approve:${r.id}`}]]}});
}
async function decide(r,yes,reason) {
  const [lock] = await change(r,{state:'deciding'});
  if (!lock) return '这条申请已处理或正在处理。';
  let sent = false;
  try {
    if (yes && !(await photo(r.uid))) { yes=false; reason=noPhoto; }
    if (!yes) sent=await notify(r.state==='no_photo'?r.dm:r.uid,`申请 #${r.id} · ${r.title}\n拒绝理由：${reason}`);
    const args={chat_id:r.chat,user_id:r.uid};
    if (yes) await api.approveChatJoinRequest(args);
    else await api.declineChatJoinRequest(args);
  } catch (e) {
    if (/HIDE_REQUESTER_MISSING|USER_ALREADY_PARTICIPANT/i.test(String(e.description ?? e))) {
      await change(lock,{state:'closed',answer:null});
      return 'Telegram 已无可处理的申请。';
    }
    await change(lock,{state:r.state});
    throw e;
  }
  await change(lock,{state:yes?'approved':'rejected',answer:null});
  return yes?'已同意。':sent?'已拒绝，理由已发送。':'已拒绝，理由未能送达。';
}
export async function join(j,ctx) {
  const id=ctx.update.update_id, chat=j.chat.id, uid=j.from.id, question=c.targets[chat];
  if (!question) return;
  let r=await get(id);
  const pair=and(eq(t.chat,chat),eq(t.uid,uid));
  if (!r) {
    const state=await photo(uid)?'waiting':'no_photo';
    await db.insert(t).values({id,chat,uid,dm:j.user_chat_id,title:j.chat.title,question,state})
      .onConflictDoNothing().run();
    r=await get(id);
  }
  if (await db.select().from(t).where(and(pair,gt(t.id,id))).get()) {
    if (['waiting','review','no_photo'].includes(r.state)) await change(r,{state:'superseded',answer:null});
    return;
  }
  for (const state of ['waiting','review','no_photo']) {
    await db.update(t).set({state:'superseded',answer:null})
      .where(and(pair,lt(t.id,id),eq(t.state,state))).run();
  }
  if (r.state==='no_photo') await decide(r,false,noPhoto);
  if (r.state==='waiting') await notify(r.dm,prompt(r));
}
const unavailable='申请不存在、已处理，或你没有操作权限。';
export async function message(m) {
  if (m.chat.type!=='private' || !m.from || !m.text) return;
  const uid=m.from.id;
  const [,cmd,args='']=m.text.trim().match(/^\/(\w+)(?:@\w+)?(?:\s+([\s\S]+))?$/)??[];
  if (cmd==='start' || cmd==='pending') {
    await send(uid,`你的 Telegram ID：${uid}\n/pending 查看待答申请。`);
    for (const r of await db.select().from(t).where(and(eq(t.uid,uid),eq(t.state,'waiting'))).all())
      await send(uid,prompt(r));
    return;
  }
  if (cmd==='reviews' && c.reviewers.includes(uid)) {
    let found=false;
    for (const r of await db.select().from(t).where(eq(t.state,'review')).all())
      if (await auth(r,uid)) { await card(r,uid,await common(r.uid)); found=true; }
    if (!found) await send(uid,'没有你可处理的待审申请。');
    return;
  }
  if (!['answer','reject'].includes(cmd)) return;
  const match=args.match(/^(\d{1,15})\s+([\s\S]+)$/);
  if (!match || match[2].length>1000) return send(uid,`格式：/${cmd} 申请编号 文本（最多 1000 字）`);
  const r=await get(Number(match[1]));
  if (cmd==='answer' && r?.uid===uid && r.state==='waiting') {
    const [updated]=await change(r,{state:'review',answer:match[2]});
    if (!updated) return send(uid,unavailable);
    await notify(uid,'回答已提交，请等待管理员审核。');
    const count=await common(uid);
    await Promise.allSettled([...new Set(c.reviewers)].map(async admin=>{
      if (await auth(updated,admin)) await card(updated,admin,count);
    }));
    return;
  }
  if (cmd==='reject' && r?.state==='review' && await auth(r,uid)) {
    let result;
    try { result=await decide(r,false,match[2]); }
    catch { result='拒绝未确认，请核实 Bot 权限和 Telegram 待审列表。'; }
    return send(uid,result);
  }
  return send(uid,unavailable);
}
export async function callback(q) {
  let result=unavailable;
  try {
    const match=q.data?.match(/^approve:(\d{1,15})$/);
    const r=match && await get(Number(match[1]));
    if (r?.state==='review' && await auth(r,q.from.id)) result=await decide(r,true,'');
  } catch { result='操作未确认，请核实 Bot 权限和 Telegram 待审列表。'; }
  await api.answerCallbackQuery({callback_query_id:q.id,text:result,show_alert:true});
}

