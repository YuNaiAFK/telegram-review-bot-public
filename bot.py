import json
import logging
import os
import sqlite3
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
MEMBERS = {"creator", "administrator", "member"}


class StopRequested(Exception):
    pass


def check_stop():
    if (ROOT / "stop.request").exists():
        raise StopRequested


def wait_or_stop(seconds):
    until = time.monotonic() + seconds
    while True:
        check_stop()
        remaining = until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


class ApiError(Exception):
    pass


class ApiNetworkError(ApiError):
    pass


class ApiRateLimit(ApiError):
    pass


class Bot:
    def __init__(self, token, config, db, owner_account=None):
        self.token = token
        self.identity = None
        self.next_link_cleanup = 0
        self.target_titles = {}
        self.pending_apply_menus = {}
        self.config = config
        proxy = config.get("telegram_http_proxy")
        self.opener = build_opener(ProxyHandler({"https": proxy, "http": proxy})) if proxy else build_opener()
        self.db = db
        self.owner_account = owner_account
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE IF NOT EXISTS auto_unpin_groups (chat INTEGER PRIMARY KEY)")
        db.execute("""CREATE TABLE IF NOT EXISTS pin_provenance (
            chat INTEGER, message INTEGER, observed INTEGER, kind TEXT, PRIMARY KEY(chat,message))""")
        db.execute("DELETE FROM pin_provenance WHERE observed < ?", (int(time.time()) - 47 * 3600,))
        db.execute("""CREATE TABLE IF NOT EXISTS review_cards (
            admin INTEGER NOT NULL, message INTEGER NOT NULL, request INTEGER NOT NULL,
            stage TEXT NOT NULL DEFAULT 'review', PRIMARY KEY (admin, message))""")
        db.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY, event INTEGER UNIQUE, chat INTEGER,
            uid INTEGER, dm INTEGER, label TEXT, question TEXT,
            photos INTEGER, common TEXT, answer TEXT,
            state TEXT DEFAULT 'waiting', notified INTEGER DEFAULT 0)""")
        if not any(col["name"] == "username" for col in db.execute("PRAGMA table_info(requests)")):
            db.execute("ALTER TABLE requests ADD COLUMN username TEXT")

        columns = {col["name"] for col in db.execute("PRAGMA table_info(requests)")}
        for column, definition in (
            ("flow", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("invite", "TEXT"),
            ("redeem_event", "INTEGER"),
        ):
            if column not in columns:
                db.execute(f"ALTER TABLE requests ADD COLUMN {column} {definition}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS request_invite ON requests(invite) WHERE invite IS NOT NULL")


    @property
    def private_invites(self):
        return self.config.get("admission_mode") == "private_invite"

    def active_flow(self, row):
        return bool(row) and (row["flow"] == "private") == self.private_invites

    def api(self, method, **data):
        check_stop()
        req = Request(f"https://api.telegram.org/bot{self.token}/{method}",
                      json.dumps(data).encode(), {"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=5 if method == "answerCallbackQuery" else 20) as response:
                body = json.load(response)
        except HTTPError as exc:
            body = json.loads(exc.read())
            if exc.code == 429:
                raise ApiRateLimit(f"{method}: Telegram 限流，请等待 {body.get('parameters', {}).get('retry_after', '?')} 秒") from None
            raise ApiError(f"{method}: {body.get('description', exc.code)}") from None
        except (URLError, OSError, HTTPException):
            raise ApiNetworkError(f"{method}: 网络连接失败") from None
        if not body["ok"]:
            raise ApiError(f"{method}: {body['description']}")
        return body["result"]

    def send(self, chat, text, **kwargs):
        return self.api("sendMessage", chat_id=chat, text=text, **kwargs)

    def tell(self, chat, text):
        try:
            self.send(chat, text)
            return True
        except ApiRateLimit:
            logging.warning("私聊通知被 Telegram 限流")
            return False
        except ApiError:
            logging.warning("私聊通知未送达")
            return False

    def applicant_message(self, row, destination, text, **kwargs):
        # New join updates carry a temporary private chat ID; an existing
        # conversation can still be reachable by user ID after it expires.
        try:
            self.send(destination, text, **kwargs)
            return True
        except ApiRateLimit as exc:
            logging.warning("申请 #%s 私聊限流：%s", row["id"], exc)
            return False
        except ApiError as exc:
            logging.warning("申请 #%s 私聊失败：%s", row["id"], exc)
            detail = str(exc).lower()
            if destination != row["uid"] and (
                "chat not found" in detail or "can't initiate conversation" in detail
            ):
                try:
                    self.send(row["uid"], text, **kwargs)
                    return True
                except ApiRateLimit as retry:
                    logging.warning("申请 #%s 私聊限流：%s", row["id"], retry)
                except ApiError as retry:
                    logging.warning("申请 #%s 私聊会话不可达：%s", row["id"], retry)
            return False

    def deliver_question(self, row, destination):
        rid = row["id"]
        instruction = ("请直接回复这条消息（最多 1000 字）。"
                       if row["flow"] == "private" else f"请发送 /answer {rid} 你的回答（最多 1000 字）。")
        prompt = f"申请 #{rid} · {row['label']}\n{row['question']}\n\n{instruction}"
        if self.applicant_message(row, destination, prompt):
            self.save("UPDATE requests SET notified=1 WHERE id=?", rid)
            return True
        return False

    def get(self, rid):
        return self.db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()

    def save(self, sql, *args):
        with self.db:
            return self.db.execute(sql, args).lastrowid

    def active_member(self, chat, uid):
        m = self.api("getChatMember", chat_id=chat, user_id=uid)
        return m["status"] in MEMBERS or (
            m["status"] == "restricted" and m["is_member"])

    def common_counts(self, uid):
        owner = self.config["owner_id"]
        count = unknown = 0
        groups = set(self.config["common_group_ids"])
        for group in groups:
            try:
                if self.api("getChat", chat_id=group)["type"] not in {"group", "supergroup"}:
                    unknown += 1
                    continue
                if self.active_member(group, owner) and self.active_member(group, uid):
                    count += 1
            except ApiError:
                unknown += 1
        return count, unknown, len(groups)

    def common(self, uid):
        count, unknown, total = self.common_counts(uid)
        if not total:
            return "未配置核验群，无法统计"
        return f"配置范围内已确认 {count} 个（共 {total} 个，{unknown} 个无法核验）"

    def profile_count(self, uid, username=None):
        try:
            return self.api("getUserProfilePhotos", user_id=uid, limit=1)["total_count"]
        except ApiError as exc:
            message = str(exc).upper()
            if "USER_ID_INVALID" in message or "USER NOT FOUND" in message:
                if self.owner_account is None:
                    return None
                visible = self.owner_account.has_visible_photo(uid, username)
                if visible is True:
                    logging.info("Bot API 无法查询该头像，已通过号主账号确认有头像")
                    return 1
                if visible is False:
                    logging.info("Bot API 无法查询该头像，已通过号主账号确认没有可见头像")
                    return 0
                return None
            raise

    def is_deleted(self, uid, username):
        return self.owner_account is not None and self.owner_account.is_deleted(uid, username) is True

    @staticmethod
    def is_deactivated_error(exc):
        message = str(exc).upper()
        return "USER IS DEACTIVATED" in message or "INPUT_USER_DEACTIVATED" in message

    def decline(self, row, *, deleted=False):
        try:
            self.api("declineChatJoinRequest", chat_id=row["chat"], user_id=row["uid"])
        except ApiError as exc:
            if self.is_deactivated_error(exc):
                self.save("UPDATE requests SET state='inactive', answer=NULL WHERE id=?", row["id"])
                logging.warning("申请 #%s：账号已停用，Telegram 不允许 Bot 移除该待审记录", row["id"])
                return "账号已停用，但 Telegram 未允许 Bot 移除该待审记录。请管理员在群里手动处理。"
            if deleted and "HIDE_REQUESTER_MISSING" in str(exc):
                self.save("UPDATE requests SET state='closed', answer=NULL WHERE id=?", row["id"])
                logging.info("申请 #%s：账号已注销，Telegram 已无待审记录", row["id"])
                return "账号已注销，Telegram 已无待审记录。"
            raise
        self.save("UPDATE requests SET state='rejected', answer=NULL WHERE id=?", row["id"])
        if deleted:
            logging.info("申请 #%s：账号已注销，已自动拒绝", row["id"])
            return "账号已注销，已自动拒绝。"
        return None

    def reject_deleted(self, row):
        if row["flow"] == "private":
            self.save("UPDATE requests SET state='rejected', answer=NULL WHERE id=?", row["id"])
            return "账号已注销，已自动拒绝。"
        return self.decline(row, deleted=True)

    def show_apply_menu(self, uid, *, retry=True):
        buttons = []
        targets = self.config["targets"]
        rejected = {str(row["chat"]) for row in self.db.execute(
            "SELECT chat FROM requests WHERE state='rejected' AND id IN "
            "(SELECT MAX(id) FROM requests WHERE uid=? AND flow='private' GROUP BY chat)", (uid,))}
        for index, chat in enumerate(targets, 1):
            title = self.target_titles.get(chat)
            label = f"申请加入 · {title}" if title else (
                "申请加入" if len(targets) == 1 else f"申请加入 · 群或频道 {index}（{chat}）")
            if chat in rejected:
                label = "重新申请" + label.removeprefix("申请加入")
            buttons.append([{"text": label, "callback_data": f"apply:{chat}"}])
        text = "点下方按钮开始入群审核。Bot 会先检查头像和共同群，再发审核题。"
        if rejected.intersection(targets):
            text = "上次申请未通过，可以点「重新申请」再试。\n" + text
        if uid in self.config["reviewer_ids"]:
            text += "\n管理员可发送 /reviews 查看待审申请，并在卡片上操作。"
        try:
            self.send(uid, text, reply_markup={"inline_keyboard": buttons})
            self.pending_apply_menus.pop(uid, None)
        except ApiNetworkError:
            if retry:
                self.pending_apply_menus[uid] = time.monotonic() + 3
                logging.warning("申请入口发送遇到网络错误，将自动重试一次")
                return
            logging.warning("申请入口重发失败：Bot API 网络连接不可用")
        finally:
            if not retry:
                self.pending_apply_menus.pop(uid, None)

    def retry_apply_menu(self):
        now = time.monotonic()
        uid = next((uid for uid, due in self.pending_apply_menus.items() if now >= due), None)
        if uid is not None:
            try:
                self.show_apply_menu(uid, retry=False)
            except ApiError as exc:
                logging.warning("申请入口重发失败：%s", exc)

    def skip_existing_member(self, chat, uid):
        if not self.active_member(chat, uid):
            return False
        self.save("UPDATE requests SET state='closed', answer=NULL WHERE chat=? AND uid=? "
                  "AND flow='private' AND state IN ('waiting','review','invited','redeeming')",
                  chat, uid)
        self.tell(uid, "您已在群，不需要再次审核。")
        return True

    def start_private_application(self, chat, user):
        uid = user["id"]
        question = self.config["targets"].get(str(chat))
        if not question or self.skip_existing_member(chat, uid):
            return
        row = self.db.execute(
            "SELECT * FROM requests WHERE chat=? AND uid=? AND flow='private' "
            "AND state IN ('waiting','review','invited','redeeming') ORDER BY id DESC LIMIT 1",
            (chat, uid)).fetchone()
        username = user.get("username")
        if row and row["state"] in {"review", "invited", "redeeming"}:
            if row["state"] == "invited":
                self.deliver_invite(row)
            else:
                self.send(uid, "你的申请正在审核或入群处理中。")
            return
        if not row:
            target = self.api("getChat", chat_id=chat)
            rid = self.save("""INSERT INTO requests
                (chat,uid,dm,label,question,username,flow) VALUES (?,?,?,?,?,?,'private')""",
                chat, uid, uid, target.get("title", str(chat)), question, username)
            row = self.get(rid)
        else:
            self.save("UPDATE requests SET username=? WHERE id=?", username, row["id"])
            row = self.get(row["id"])
        self.prompt_private_application(row)

    def prompt_private_application(self, row):
        uid = row["uid"]
        if self.is_deleted(uid, row["username"]):
            self.reject_deleted(row)
            return
        photos = self.profile_count(uid, row["username"])
        self.save("UPDATE requests SET photos=? WHERE id=?", photos, row["id"])
        current = self.get(row["id"])
        if photos == 0:
            self.decide(current, False, "未检测到可见头像，请设置头像后重新申请。", uid)
        elif photos is None:
            self.send(uid, "暂时无法核验头像，申请已保留。请稍后发送 /pending 重试。")
        else:
            self.send_question(current, uid)

    def deliver_invite(self, row):
        return self.applicant_message(
            row, row["uid"], f"你的申请已通过：{row['label']}。\n"
            "链接没有时间限制，仅限本账号使用一次。请勿转发。",
            reply_markup={"inline_keyboard": [[{"text": "加入", "url": row["invite"]}]]},
        )

    def issue_invite(self, row):
        # Users who applied through the previous flow must be able to submit a
        # new Telegram join request using their assigned link.
        old = self.db.execute(
            "SELECT id FROM requests WHERE uid=? AND chat=? AND flow='legacy' "
            "AND state IN ('waiting','review')", (row["uid"], row["chat"])).fetchall()
        if old:
            try:
                self.api("declineChatJoinRequest", chat_id=row["chat"], user_id=row["uid"])
            except ApiError as exc:
                if "HIDE_REQUESTER_MISSING" not in str(exc):
                    raise
            self.save("UPDATE requests SET state='superseded', answer=NULL WHERE uid=? AND chat=? "
                      "AND flow='legacy' AND state IN ('waiting','review')", row["uid"], row["chat"])
        invite = self.api("createChatInviteLink", chat_id=row["chat"], creates_join_request=True,
                          name=f"review-{row['id']}")["invite_link"]
        self.save("UPDATE requests SET state='invited', invite=?, answer=NULL WHERE id=?", invite, row["id"])
        if self.deliver_invite(self.get(row["id"])):
            return "已通过，已私聊发送专属邀请链接。"
        return "已通过，链接已保存；私聊未送达，申请人可发 /pending 取回。"

    def revoke_redeemed_invite(self, row):
        try:
            self.api("revokeChatInviteLink", chat_id=row["chat"], invite_link=row["invite"])
        except ApiError as exc:
            if not any(code in str(exc).upper() for code in ("INVITE_HASH_EXPIRED", "INVITE_HASH_INVALID")):
                self.next_link_cleanup = time.monotonic() + 60
                # Do not log the secret URL or API error text for a link operation.
                logging.warning("申请 #%s：一次性链接已锁定，Telegram 撤销未确认，稍后重试", row["id"])
                return
        self.save("UPDATE requests SET invite=NULL WHERE id=?", row["id"])

    def redeem_private_invite(self, event, j):
        chat, uid = j["chat"]["id"], j["from"]["id"]
        link = (j.get("invite_link") or {}).get("invite_link")
        if link:
            candidates = [link]
            # Telegram clients can express the same invite as /+HASH or /joinchat/HASH.
            url = urlsplit(link)
            if url.scheme == "https" and url.netloc == "t.me":
                token = (url.path[2:] if url.path.startswith("/+") else
                         url.path.removeprefix("/joinchat/") if url.path.startswith("/joinchat/") else "")
                if token and token.isascii() and token.replace("-", "").replace("_", "").isalnum():
                    candidates += [f"https://t.me/+{token}", f"https://t.me/joinchat/{token}"]
            slots = ",".join("?" for _ in candidates)
            row = self.db.execute(f"SELECT * FROM requests WHERE chat=? AND invite IN ({slots}) "
                                  "AND flow='private'", (chat, *candidates)).fetchone()
        else:
            # Some join updates omit the invite. Only a still-unused approval
            # for this exact Telegram account and target can be redeemed.
            row = self.db.execute(
                "SELECT * FROM requests WHERE chat=? AND uid=? AND flow='private' AND invite IS NOT NULL "
                "AND (state='invited' OR (state='redeeming' AND redeem_event=?)) "
                "ORDER BY id DESC LIMIT 1", (chat, uid, event)).fetchone()
            if row:
                logging.info("申请 #%s：加入更新不含链接，使用该账号的有效审批记录", row["id"])
        if row and row["uid"] == uid and (
            row["state"] == "invited" or
            (row["state"] == "redeeming" and row["redeem_event"] == event)
        ):
            self.save("UPDATE requests SET state='redeeming', redeem_event=? WHERE id=?", event, row["id"])
            try:
                self.api("approveChatJoinRequest", chat_id=chat, user_id=uid)
            except ApiError as exc:
                if not any(code in str(exc) for code in ("HIDE_REQUESTER_MISSING", "USER_ALREADY_PARTICIPANT")):
                    raise
                if not self.active_member(chat, uid):
                    # A consumed or unavailable request must not turn this
                    # same link back into a reusable invitation.
                    self.save("UPDATE requests SET state='closed' WHERE id=?", row["id"])
                    self.revoke_redeemed_invite(row)
                    self.tell(uid, "专属链接的入群申请已失效，请发送 /apply 重新申请。")
                    return
            self.save("UPDATE requests SET state='redeemed' WHERE id=?", row["id"])
            self.revoke_redeemed_invite(row)
            return
        try:
            self.api("declineChatJoinRequest", chat_id=chat, user_id=uid)
        except ApiError as exc:
            if "HIDE_REQUESTER_MISSING" not in str(exc):
                raise

    def join(self, event, j):
        chat, uid = j["chat"]["id"], j["from"]["id"]
        question = self.config["targets"].get(str(chat))
        if not question:
            return
        if self.private_invites:
            return self.redeem_private_invite(event, j)
        row = self.db.execute("SELECT * FROM requests WHERE event=?", (event,)).fetchone()
        if row and row["state"] != "waiting":
            return
        deleted = self.is_deleted(uid, j["from"].get("username"))
        if not row:
            photos = 0 if deleted else self.profile_count(uid, j["from"].get("username"))
            self.save("UPDATE requests SET state='superseded' WHERE chat=? AND uid=? "
                      "AND state IN ('waiting','review')", chat, uid)
            rid = self.save("""INSERT INTO requests
                (event,chat,uid,dm,label,question,photos,username) VALUES (?,?,?,?,?,?,?,?)""",
                event, chat, uid, j["user_chat_id"],
                j["chat"]["title"], question, photos, j["from"].get("username"))
            row = self.get(rid)
        if deleted:
            self.reject_deleted(row)
        elif row["photos"] == 0:
            self.decide(row, False, "未检测到机器人可见的头像。请设置可见头像后重新申请。",
                        row["dm"])
        elif row["photos"] is None:
            logging.info("申请 #%s：Bot 暂不能核验头像，保留待审，可用 /pending 重试", row["id"])
            self.applicant_message(row, row["dm"], "已收到你的入群申请。暂时无法核验头像，请在此发送 /pending 重试。")
        elif not row["notified"]:
            self.send_question(row, row["dm"])

    def common_gate(self, row, minimum):
        if self.owner_account is not None:
            count = self.owner_account.count(row["uid"], row["username"])
            if count is None:
                return None, None
            return count >= minimum, f"号主账号已确认 {count} 个共同群"
        count, unknown, total = self.common_counts(row["uid"])
        detail = f"配置范围内已确认 {count} 个（共 {total} 个，{unknown} 个无法核验）"
        return (True if count >= minimum else None if count + unknown >= minimum else False), detail

    def send_question(self, row, destination):
        rid = row["id"]
        minimum = self.config.get("min_common_groups", 0)
        if minimum:
            passed, detail = self.common_gate(row, minimum)
            if passed is None:
                tip = "暂时无法核实共同群数量，尚未通过初筛。请稍后发送 /pending 重试。"
                if self.owner_account and self.owner_account.username:
                    tip += f" 若你没有公开用户名，可先联系号主 @{self.owner_account.username}，再重试。"
                self.applicant_message(row, destination, tip)
                return
            if not passed:
                self.decide(row, False, f"共同群不足{minimum}个", destination)
                return
            self.save("UPDATE requests SET common=? WHERE id=?", detail, rid)
        self.deliver_question(row, destination)

    def review_card(self, row, stage="review", result=None, hint=None):
        rid = row["id"]
        username = row["username"]
        public = bool(username and username.isascii() and username.replace("_", "").isalnum())
        name = f"@{username}" if public else "未设置公开用户名"
        url = f"https://t.me/{username}" if public else f"tg://user?id={row['uid']}"
        title = "审核结果" if result else "待审"
        text = (f"{title} #{rid} · {row['label']}\n用户名：{name}\n用户 ID：{row['uid']}\n"
                f"头像：已检测到\n共同群：{row['common'] or '未记录'}\n\n"
                f"问题：{row['question']}\n回答：{row['answer'] or '—'}")
        buttons = [[{"text": f"查看 {name}" if public else "查看用户资料", "url": url}]]

        def button(label, action):
            return {"text": label, "callback_data": f"{action}:{rid}"}

        if result:
            text += f"\n\n处理结果：{result}"
        elif stage == "choose":
            text += "\n\n请选择只发送审核结果，或附上拒绝理由。"
            buttons += [[button("回复理由", "reject_reason"), button("仅发送结果", "reject_silent")],
                        [button("返回审核", "review_back")]]
        elif stage == "reason":
            text += "\n\n请使用 Telegram 的「回复」功能回复这条审核消息，填写拒绝理由（最多 1000 字）。"
            buttons += [[button("仅发送结果", "reject_silent"), button("返回审核", "review_back")]]
        else:
            label = "通过并发送专属链接" if row["flow"] == "private" else "同意加入"
            buttons += [[button(label, "approve")], [button("拒绝", "reject_menu")]]
        if hint:
            text += f"\n{hint}"
        return {"text": text, "reply_markup": {"inline_keyboard": buttons}}

    def edit_review_card(self, row, admin, message, stage="review", result=None, hint=None):
        try:
            self.api("editMessageText", chat_id=admin, message_id=message,
                     **self.review_card(row, stage, result, hint))
        except ApiError as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        self.save("""INSERT INTO review_cards (admin,message,request,stage) VALUES (?,?,?,?)
                     ON CONFLICT(admin,message) DO UPDATE SET request=excluded.request,stage=excluded.stage""",
                  admin, message, row["id"], "finished" if result else stage)

    def finish_review_cards(self, row, result):
        updated = set()
        for card in list(self.db.execute("SELECT admin,message FROM review_cards WHERE request=?",
                                         (row["id"],))):
            try:
                self.edit_review_card(row, card["admin"], card["message"], result=result)
                updated.add((card["admin"], card["message"]))
            except ApiError as exc:
                logging.warning("申请 #%s 已处理，但无法更新一张审核卡：%s", row["id"], exc)
        return updated

    def review(self, row, destination):
        if not row["common"]:
            self.save("UPDATE requests SET common=? WHERE id=?", self.common(row["uid"]), row["id"])
            row = self.get(row["id"])
        card = self.db.execute("SELECT message, stage FROM review_cards WHERE request=? AND admin=? "
                               "ORDER BY message DESC LIMIT 1", (row["id"], destination)).fetchone()
        if card:
            try:
                self.edit_review_card(row, destination, card["message"], card["stage"])
                return
            except ApiError as exc:
                if "message to edit not found" not in str(exc).lower():
                    raise
                self.save("DELETE FROM review_cards WHERE admin=? AND message=?", destination, card["message"])
        payload = self.review_card(row)
        sent = self.send(destination, **payload)
        if isinstance(sent, dict) and sent.get("message_id"):
            self.save("INSERT OR IGNORE INTO review_cards (admin,message,request) VALUES (?,?,?)",
                      destination, sent["message_id"], row["id"])

    def accept_rejection_reply(self, m):
        admin = m["from"]["id"]
        reply = m.get("reply_to_message") or {}
        message_id = reply.get("message_id")
        if not message_id:
            return False
        card = self.db.execute("SELECT request FROM review_cards WHERE admin=? AND message=? AND stage='reason'",
                               (admin, message_id)).fetchone()
        if not card:
            return False
        row = self.get(card["request"])
        if not self.active_flow(row) or row["state"] != "review":
            return True
        if not self.authorized(row, admin):
            return True
        reason = m["text"].strip()
        if len(reason) > 1000:
            self.edit_review_card(row, admin, message_id, "reason", hint="理由超过 1000 字，请缩短后重新回复。")
            return True
        result = self.decide(row, False, reason, row["uid"])
        self.finish_review_cards(row, f"{result}\n拒绝理由：{reason}")
        return True

    def decide(self, row, approve, reason, destination):
        rid = row["id"]
        if approve and row["flow"] == "private" and self.skip_existing_member(row["chat"], row["uid"]):
            return "申请人已在群内，无需审核，未发送邀请链接。"
        if approve and self.is_deleted(row["uid"], row["username"]):
            return self.reject_deleted(row)
        if approve:
            photos = self.profile_count(row["uid"], row["username"])
            if photos is None:
                return "Bot 暂时无法核验头像，未放行。请申请人先私聊 Bot，再重试。"
            if photos == 0:
                return self.decide(row, False, "复核时未检测到可见头像，请设置后重新申请。", destination)
        minimum = self.config.get("min_common_groups", 0)
        if approve and minimum:
            passed, detail = self.common_gate(row, minimum)
            if passed is None:
                return "共同群数量暂无法确认，未放行。请稍后重试。"
            if not passed:
                return self.decide(row, False, f"共同群不足{minimum}个", destination)
        detail = f"拒绝理由：{reason}" if reason else "审核结果：未通过"
        decline_text = f"申请 #{rid} · {row['label']}\n{detail}"
        delivered = "已拒绝，理由已发送。" if reason else "已拒绝，仅发送了审核结果。"
        if row["flow"] == "private":
            if approve:
                return self.issue_invite(row)
            self.save("UPDATE requests SET state='rejected', answer=NULL WHERE id=?", rid)
            sent = self.applicant_message(row, destination, decline_text, reply_markup={
                "inline_keyboard": [[{"text": "重新申请", "callback_data": f"apply:{row['chat']}"}]],
            })
            return delivered if sent else "已拒绝，但私聊通知未送达。"
        # Telegram 的临时私聊权限在请求处理后可能立即失效。
        sent = self.applicant_message(row, destination, decline_text) if not approve else True
        if not approve:
            result = self.decline(row)
            return result or (delivered if sent else "已拒绝，但 Telegram 未能送达通知。")
        self.api("approveChatJoinRequest", chat_id=row["chat"], user_id=row["uid"])
        self.save("UPDATE requests SET state='approved', answer=NULL WHERE id=?", rid)
        return "已同意。"

    def authorized(self, row, uid):
        if uid not in self.config["reviewer_ids"]:
            return False
        member = self.api("getChatMember", chat_id=row["chat"], user_id=uid)
        return member["status"] == "creator" or (
            member["status"] == "administrator" and member.get("can_invite_users", False))

    def answer_callback(self, callback_id, **options):
        try:
            self.api("answerCallbackQuery", callback_query_id=callback_id, **options)
        except ApiError as exc:
            logging.warning("按钮反馈未送达：%s", exc)

    def callback(self, q):
        result = "申请已处理、已失效或你没有审批权限。"
        data = q.get("data", "")
        if self.private_invites and data.startswith("apply:"):
            target = data[6:]
            if (q.get("message", {}).get("chat", {}).get("type") == "private"
                    and target in self.config["targets"]):
                self.answer_callback(q["id"])
                try:
                    self.start_private_application(int(target), q["from"])
                except ApiError as exc:
                    logging.warning("无法开始私聊审核：%s", exc)
                    self.tell(q["from"]["id"], "暂时无法开始审核，请稍后发送 /apply 重试。")
                return
        action, _, rid = data.partition(":")
        if (action in {"approve", "reject_menu", "reject_reason", "reject_silent", "review_back"}
                and rid.isascii() and rid.isdigit() and len(rid) <= 18):
            row = self.get(int(rid))
            admin = q["from"]["id"]
            msg = q.get("message") or {}
            chat = msg.get("chat", {})
            message_id = msg.get("message_id") if chat.get("type") == "private" and chat.get("id") == admin else None
            if self.active_flow(row) and row["state"] == "review":
                try:
                    if self.authorized(row, admin):
                        if message_id:
                            self.save("INSERT OR IGNORE INTO review_cards (admin,message,request) VALUES (?,?,?)",
                                      admin, message_id, row["id"])
                        stages = {"reject_menu": "choose", "reject_reason": "reason", "review_back": "review"}
                        if action in stages and message_id:
                            self.edit_review_card(row, admin, message_id, stages[action])
                            result = ""
                        elif action == "approve" or (action == "reject_silent" and message_id):
                            result = self.decide(row, action == "approve", None, row["uid"])
                            if self.get(row["id"])["state"] != "review":
                                updated = self.finish_review_cards(row, result)
                                if (admin, message_id) in updated:
                                    result = ""
                except ApiError as exc:
                    logging.warning("申请 #%s 审核操作失败：%s", row["id"], exc)
                    result = "操作未确认，请检查 Bot 权限后重试。"
        self.answer_callback(q["id"], **({"text": result, "show_alert": True} if result else {}))

    private_commands = (
        ("start", "开始使用"),
        ("pending", "查看我的待答申请"),
        ("answer", "提交回答：编号 回答"),
    )
    reviewer_commands = (
        ("reviews", "查看待审申请"),
    )
    group_admin_commands = (
        ("help", "显示本群管理指令"),
        ("unpin", "取消一条可确认的自动置顶，保留手动置顶"),
        ("unpinall", "清理可确认的自动置顶，保留手动置顶"),
        ("autounpin", "频道帖自动撤销置顶：on 开启，off 关闭"),
        ("unpinold", "清理自动置顶，每次最多 20 条"),
    )

    def set_commands(self, scope, entries, **options):
        try:
            self.api("setMyCommands", scope=scope, **options, commands=[
                {"command": command, "description": description}
                for command, description in entries
            ])
            return True
        except ApiError as exc:
            logging.warning("指令列表同步失败（%s）：%s", scope["type"], exc)
            return False

    def applicant_commands(self):
        if self.private_invites:
            return self.private_commands + (("apply", "选择要申请的群或频道"),)
        return self.private_commands

    def set_reviewer_commands(self, uid):
        return self.set_commands({"type": "chat", "chat_id": uid},
                                 self.applicant_commands() + self.reviewer_commands)

    def register_commands(self):
        private_ok = self.set_commands({"type": "all_private_chats"}, self.applicant_commands())
        group_ok = self.set_commands({"type": "all_chat_administrators"}, self.group_admin_commands)
        if private_ok and group_ok:
            logging.info("已同步私聊和群管理员指令列表")
        for uid in set(self.config["reviewer_ids"]):
            self.set_reviewer_commands(uid)

        groups = set()
        for chat_id in self.config["targets"]:
            try:
                info = self.api("getChat", chat_id=int(chat_id))
            except ApiError as exc:
                logging.warning("无法查询指令菜单的目标聊天：%s", exc)
                continue
            if info.get("title"):
                self.target_titles[chat_id] = info["title"]
            if info["type"] in {"group", "supergroup"}:
                groups.add(info["id"])
            elif info["type"] == "channel" and info.get("linked_chat_id"):
                groups.add(info["linked_chat_id"])
        for chat_id in groups:
            self.set_group_commands(chat_id)

    def set_group_commands(self, chat_id):
        scope = {"type": "chat_administrators", "chat_id": chat_id}
        synced = self.set_commands(scope, self.group_admin_commands)
        chinese_synced = self.set_commands(scope, self.group_admin_commands, language_code="zh")
        if synced and chinese_synced:
            logging.info("群 %s 管理员菜单已同步", chat_id)
        return synced

    @staticmethod
    def can_unpin(member):
        return member["status"] == "creator" or (
            member["status"] == "administrator" and member.get("can_pin_messages", False))

    def delete_group_command(self, m):
        message_id = m.get("message_id")
        if not message_id:
            return
        chat = m["chat"]["id"]
        try:
            self.api("deleteMessage", chat_id=chat, message_id=message_id)
        except ApiError as exc:
            logging.warning("群 %s 指令已执行，但无法删除指令消息：%s", chat, exc)

    def record_group_pin(self, m):
        chat = m["chat"]["id"]
        forwarded = m if m.get("is_automatic_forward") else m.get("pinned_message")
        if not forwarded or not forwarded.get("is_automatic_forward"):
            return False
        mid = forwarded["message_id"]
        timestamp = m.get("date", int(time.time()))
        if forwarded is not m:
            sender = m.get("sender_chat") or {}
            source = forwarded.get("sender_chat") or {}
            if (not source.get("id") or sender.get("id") != source["id"]
                    or sender.get("type") != "channel"):
                self.save("""INSERT INTO pin_provenance VALUES (?,?,?,'manual')
                             ON CONFLICT(chat,message) DO UPDATE SET kind='manual',observed=excluded.observed""",
                          chat, mid, timestamp)
                return True
        self.save("INSERT OR IGNORE INTO pin_provenance VALUES (?,?,?,'auto')", chat, mid, timestamp)
        pin = self.db.execute("SELECT kind FROM pin_provenance WHERE chat=? AND message=?",
                              (chat, mid)).fetchone()
        # Stale updates can arrive after an administrator has pinned the post.
        if (pin["kind"] == "auto" and time.time() - timestamp < 60
                and self.db.execute("SELECT 1 FROM auto_unpin_groups WHERE chat=?", (chat,)).fetchone()):
            try:
                self.remove_auto_pin(chat, mid)
            except ApiError as exc:
                logging.warning("群 %s 自动取消置顶失败：%s", chat, exc)
        return True

    def remove_auto_pin(self, chat, mid):
        self.api("unpinChatMessage", chat_id=chat, message_id=mid)
        self.save("UPDATE pin_provenance SET kind='cleared' WHERE chat=? AND message=?", chat, mid)

    def group_message(self, m):
        chat = m["chat"]["id"]
        if self.record_group_pin(m):
            return

        parts = m.get("text", "").strip().split()
        if not parts:
            return
        command, _, mention = parts[0].lower().partition("@")
        if command not in {"/help", "/unpin", "/unpinall", "/autounpin", "/unpinold"}:
            return
        if mention and (not self.identity or mention != self.identity["username"].lower()):
            return
        sender = m.get("from")
        if m.get("sender_chat") or not sender or sender.get("is_bot"):
            self.send(chat, "请用个人身份发送管理指令，不能用匿名管理员或频道身份。")
            return
        member = self.api("getChatMember", chat_id=chat, user_id=sender["id"])
        if command == "/help":
            if member["status"] in {"creator", "administrator"}:
                self.set_group_commands(chat)
                self.delete_group_command(m)
                self.send(chat, "本群管理指令\n" + "\n".join(
                    f"/{name} — {description}" for name, description in self.group_admin_commands))
            elif mention:
                self.send(chat, "Telegram 未将你的账号识别为本群管理员。请切换到有权限的个人账号。")
            return
        if not self.can_unpin(member):
            self.send(chat, "只有有置顶权限的管理员可以使用此指令。")
            return

        if command in {"/unpinall", "/unpinold"}:
            if len(parts) != 1:
                self.send(chat, f"用法：{command} 只清理可核验的自动置顶。")
                return
            if self.unpin_old(chat, limit=None if command == "/unpinall" else 20):
                self.delete_group_command(m)
            return

        if command == "/autounpin":
            if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
                self.send(chat, "用法：/autounpin on 开启，/autounpin off 关闭。")
                return
            if parts[1].lower() == "off":
                self.save("DELETE FROM auto_unpin_groups WHERE chat=?", chat)
                self.delete_group_command(m)
                self.send(chat, "已关闭本群的自动取消置顶。")
                return
            info = self.api("getChat", chat_id=chat)
            if not info.get("linked_chat_id"):
                self.send(chat, "本群未关联频道。请在频道的关联讨论群使用此指令。")
                return
            if not self.identity or not self.can_unpin(self.api(
                    "getChatMember", chat_id=chat, user_id=self.identity["id"])):
                self.send(chat, "请先给 Bot 授予本群的「置顶消息」权限。")
                return
            self.save("INSERT OR IGNORE INTO auto_unpin_groups (chat) VALUES (?)", chat)
            self.delete_group_command(m)
            self.send(chat, "已开启：新频道帖转入本群后，Bot 会自动取消这些帖子的置顶。")
            return

        if len(parts) != 1:
            self.send(chat, "请回复要取消置顶的消息并发送 /unpin；直接发送则取消最近一条置顶。")
            return
        selected = m.get("reply_to_message") or self.api("getChat", chat_id=chat).get("pinned_message")
        if not selected:
            self.send(chat, "本群没有可取消的置顶消息。")
            return
        if self.unpin_old(chat, selected=selected["message_id"], limit=1):
            self.delete_group_command(m)

    def unpin_old(self, chat, *, selected=None, limit=20):
        observed = {
            item["message"]: item["observed"] for item in self.db.execute(
                "SELECT message,observed FROM pin_provenance WHERE chat=? AND kind='auto' AND observed>=?",
                (chat, int(time.time()) - 47 * 3600))
            if selected is None or item["message"] == selected
        }
        if not observed:
            self.send(chat, "没有找到可确认的自动置顶。手动置顶和来源不明的置顶已保留。")
            return
        if self.owner_account is None:
            self.send(chat, "查看历史置顶需要先登录号主账号。")
            return
        linked = self.api("getChat", chat_id=chat).get("linked_chat_id")
        if not linked:
            self.send(chat, "请在频道的关联讨论群使用 /unpinold。")
            return
        if not self.identity or not self.can_unpin(self.api(
                "getChatMember", chat_id=chat, user_id=self.identity["id"])):
            self.send(chat, "请先给 Bot 授予本群的「置顶消息」权限。")
            return
        try:
            ids = self.owner_account.old_channel_pins(chat, linked, observed, limit=limit)
        except RuntimeError as exc:
            logging.warning("群 %s 无法读取历史置顶：%s", chat, exc)
            self.send(chat, "未能读取历史置顶，未作修改。请确认号主账号是本群管理员并能读取近期操作记录。")
            return
        if not ids:
            self.send(chat, "没有找到可确认的自动置顶。手动置顶和来源不明的置顶已保留。")
            return
        removed = 0
        for mid in ids:
            try:
                self.remove_auto_pin(chat, mid)
            except ApiError as exc:
                logging.warning("群 %s 清理历史置顶中止：%s", chat, exc)
                self.send(chat, f"已取消 {removed} 条置顶，后续操作中止。请稍后再试。")
                return
            removed += 1
        logging.info("群 %s 已取消 %s 条关联频道帖的历史置顶", chat, removed)
        return True

    def message(self, m):
        if m["chat"]["type"] in {"group", "supergroup"}:
            self.group_message(m)
            return
        if m["chat"]["type"] != "private" or not m.get("text", "").strip():
            return
        uid = m["from"]["id"]
        if uid in self.config["reviewer_ids"] and self.accept_rejection_reply(m):
            return
        parts = m["text"].strip().split(maxsplit=2)
        cmd = parts[0].split("@")[0]
        if self.private_invites:
            if cmd in {"/start", "/apply"}:
                self.show_apply_menu(uid)
                if cmd == "/start" and uid in self.config["reviewer_ids"]:
                    self.set_reviewer_commands(uid)
                return
            if cmd == "/pending":
                requests = list(self.db.execute(
                    "SELECT * FROM requests WHERE uid=? AND flow='private' "
                    "AND state IN ('waiting','review','invited','redeeming')", (uid,)))
                if not requests:
                    self.show_apply_menu(uid)
                for row in requests:
                    if self.skip_existing_member(row["chat"], uid):
                        continue
                    if row["state"] == "invited":
                        self.deliver_invite(row)
                    elif row["state"] == "waiting":
                        self.save("UPDATE requests SET username=? WHERE id=?",
                                  m["from"].get("username"), row["id"])
                        self.prompt_private_application(self.get(row["id"]))
                    else:
                        self.send(uid, f"申请 #{row['id']} · {row['label']} 正在审核或入群处理中。")
                return
            if not cmd.startswith("/"):
                ready = list(self.db.execute(
                    "SELECT id, label FROM requests WHERE uid=? AND flow='private' AND state='waiting' "
                    "AND notified=1 AND photos>0 ORDER BY id", (uid,)))
                reply = m.get("reply_to_message") or {}
                reply_text = reply.get("text", "")
                for item in ready:
                    if (reply.get("from", {}).get("id") == (self.identity or {}).get("id")
                            and reply_text.startswith(f"申请 #{item['id']} · ")):
                        ready = [item]
                        break
                if len(ready) == 1:
                    cmd = "/answer"
                    parts = [cmd, str(ready[0]["id"]), m["text"].strip()]
                elif ready:
                    self.send(uid, "你有多条待答申请。请回复对应题目，或发送 /answer 编号 回答。")
                    return
                else:
                    self.send(uid, "请先发送 /apply 选择要申请的群或频道。")
                    return
        if cmd in {"/start", "/pending"}:
            tip = f"你的 Telegram ID：{uid}\n用 /pending 查看未回答的申请。"
            if cmd == "/start" and uid in self.config["reviewer_ids"]:
                self.set_reviewer_commands(uid)
                tip += ("\n管理员：/reviews 查看待审，并在审核卡片上操作。"
                        "\n取消置顶请到关联讨论群发送 /unpin、/unpinall、/unpinold 或 /autounpin on。")
            self.send(uid, tip)
            for row in self.db.execute("SELECT * FROM requests WHERE uid=? AND state='waiting'", (uid,)):
                username = m["from"].get("username")
                self.save("UPDATE requests SET username=? WHERE id=?", username, row["id"])
                if self.is_deleted(uid, username):
                    self.reject_deleted(row)
                    continue
                photos = self.profile_count(uid, username)
                self.save("UPDATE requests SET photos=? WHERE id=?", photos, row["id"])
                current = self.get(row["id"])
                if photos is None:
                    self.send(uid, f"申请 #{row['id']}：暂时无法核验头像，请稍后发 /pending 重试。")
                elif photos == 0:
                    self.decide(current, False, "未检测到机器人可见的头像。请设置后重新申请。", uid)
                else:
                    self.send_question(current, uid)
            return
        if cmd == "/reviews" and uid in self.config["reviewer_ids"]:
            found = False
            for row in list(self.db.execute("SELECT * FROM requests WHERE state='review'")):
                if self.active_flow(row) and self.authorized(row, uid):
                    self.review(row, uid)
                    found = True
            if not found:
                self.send(uid, "没有你可处理的待审申请。")
            return
        if cmd not in {"/answer", "/reject"}:
            return
        if (len(parts) != 3 or not parts[1].isascii() or not parts[1].isdigit()
                or len(parts[1]) > 18 or len(parts[2]) > 1000):
            self.send(uid, f"格式：{cmd} 申请编号 文本（最多 1000 字）")
            return
        row = self.get(int(parts[1]))
        if cmd == "/answer":
            if (not self.active_flow(row) or row["uid"] != uid or row["state"] != "waiting" or not row["photos"]
                    or (self.config.get("min_common_groups", 0) and not row["common"])):
                self.send(uid, "没有找到你可以回答的申请。")
                return
            if row["flow"] == "private" and self.skip_existing_member(row["chat"], uid):
                return
            self.save("UPDATE requests SET state='review', answer=?, username=? WHERE id=?",
                      parts[2], m["from"].get("username"), row["id"])
            self.tell(uid, "回答已提交，请等待管理员审核。")
            for admin in set(self.config["reviewer_ids"]):
                try:
                    if self.authorized(row, admin):
                        self.review(self.get(row["id"]), admin)
                except ApiError:
                    logging.warning("申请 #%s 未能推送给一位管理员，可用 /reviews 找回", row["id"])
        elif self.active_flow(row) and row["state"] == "review" and self.authorized(row, uid):
            result = self.decide(row, False, parts[2], row["uid"])
            self.finish_review_cards(row, f"{result}\n拒绝理由：{parts[2]}")
            self.send(uid, result)
        else:
            self.send(uid, "申请已处理、已失效或你没有审批权限。")

    def run(self):
        offset = None
        while True:
            try:
                updates = self.api("getUpdates", offset=offset, timeout=8,
                                   allowed_updates=["chat_join_request", "message", "callback_query"])
                for update in updates:
                    event = update["update_id"]
                    try:
                        if "chat_join_request" in update:
                            self.join(event, update["chat_join_request"])
                        elif "message" in update:
                            self.message(update["message"])
                        elif "callback_query" in update:
                            self.callback(update["callback_query"])
                    except ApiError as err:
                        logging.error("更新 %s 未完成：%s", event, err)
                        if "chat_join_request" in update and not any(
                            code in str(err) for code in ("HIDE_REQUESTER_MISSING", "USER_ALREADY_PARTICIPANT")
                        ):
                            raise
                    offset = event + 1
                if self.pending_apply_menus:
                    self.retry_apply_menu()
                if self.private_invites and time.monotonic() >= self.next_link_cleanup:
                    outstanding = self.db.execute(
                        "SELECT * FROM requests WHERE flow='private' AND state IN ('redeemed','closed') "
                        "AND invite IS NOT NULL ORDER BY id LIMIT 1").fetchone()
                    if outstanding:
                        self.revoke_redeemed_invite(outstanding)
            except ApiError as err:
                logging.warning("%s", err)
                wait_or_stop(5)


def main():
    from logging.handlers import RotatingFileHandler

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), RotatingFileHandler(
            ROOT / "bot.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")],
    )
    logging.info("启动 Windows 审查 Bot")
    try:
        if os.name == "nt":
            import msvcrt

            with (ROOT / "bot.lock").open("a+b") as lock:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise SystemExit("已有审查 Bot 实例在运行。请先停止旧实例。") from None
                try:
                    check_stop()
                    start()
                finally:
                    (ROOT / "stop.request").unlink(missing_ok=True)
        else:
            start()
    except (KeyboardInterrupt, StopRequested):
        logging.info("已停止 Bot")
    except SystemExit as exc:
        if isinstance(exc.code, str):
            logging.error("启动中止：%s", exc.code)
        raise
    except Exception as exc:
        logging.error("启动或运行失败：%s", type(exc).__name__)
        raise SystemExit("请查看 bot.log 最后几行定位失败阶段。") from None


def start():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("请设置 TELEGRAM_BOT_TOKEN。")
    config_path = ROOT / "config.json"
    if not config_path.is_file():
        raise SystemExit("请复制 config.example.json 为 config.json 并填写配置。")
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if (not config["targets"] or not config["reviewer_ids"]
            or any(not q.strip() or len(q) > 1000 for q in config["targets"].values())):
        raise SystemExit("请配置目标、管理员及不超过 1000 字的入群问题。")
    proxy = config.get("telegram_http_proxy")
    if proxy:
        try:
            parsed = urlsplit(proxy)
            if (parsed.scheme != "http" or not parsed.hostname or not parsed.port
                    or parsed.username or parsed.password or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment):
                raise ValueError
        except ValueError:
            raise SystemExit("telegram_http_proxy 格式应为 http://主机:端口，且不含账号密码。") from None
        logging.info("Telegram 连接已启用 HTTP 代理：%s:%s", parsed.hostname, parsed.port)
    minimum = config.get("min_common_groups", 0)
    mode = config.get("common_group_mode", "configured")
    if mode not in {"configured", "owner_account"}:
        raise SystemExit("common_group_mode 必须是 configured 或 owner_account。")
    groups = config["common_group_ids"]
    if type(minimum) is not int or minimum < 0:
        raise SystemExit("min_common_groups 必须是非负整数。")
    if mode == "configured" and (any(type(group) is not int or group >= 0 for group in groups)
                                 or len(set(groups)) < minimum):
        raise SystemExit(f"共同群门槛为 {minimum}：请在 common_group_ids 填写足够的不同群数字 ID。")
    from contextlib import nullcontext
    session = nullcontext()
    if mode == "owner_account" and minimum:
        try:
            from owner_account import OwnerAccount
        except ModuleNotFoundError as exc:
            if exc.name != "telethon":
                raise
            raise SystemExit("请先安装依赖：python -m pip install -r requirements-owner.txt") from None
        try:
            session = OwnerAccount(config)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    if mode == "owner_account" and minimum:
        logging.info("正在连接 Telegram 号主账号（MTProto）")
    try:
        with session as account, sqlite3.connect(ROOT / "reviews.sqlite3") as db:
            if account is not None:
                logging.info("Telegram 号主账号已连通并完成身份校验")
            logging.info("正在连接 Telegram Bot API（getMe）")
            bot = Bot(token, config, db, account)
            try:
                identity = bot.api("getMe")
            except ApiError as exc:
                logging.error("Telegram Bot API 验证失败：%s", exc)
                raise SystemExit(1) from None
            bot.identity = identity
            bot.register_commands()
            logging.info("机器人 @%s 已启动", identity["username"])
            bot.run()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()

