"""Read common groups via the locally authorized Telegram owner account."""
import asyncio
import getpass
import os
import re

from bot import ROOT
from telethon import TelegramClient, errors, functions, types, utils
from urllib.parse import urlsplit


class OwnerAccount:
    def __init__(self, config):
        self.headless = os.environ.get("REVIEW_BOT_HEADLESS") == "1"
        if self.headless and not all(os.environ.get(key) for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH")):
            raise ValueError("缺少号主 API 凭据。请在控制台点「登录 / 配置凭据」。")
        api_id = os.environ.get("TELEGRAM_API_ID") or input("Telegram API ID: ").strip()
        api_hash = os.environ.get("TELEGRAM_API_HASH") or getpass.getpass("Telegram API hash: ").strip()
        if not api_id.isascii() or not api_id.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise ValueError("API ID 或 API hash 格式不正确。")
        self.owner_id = config["owner_id"]
        proxy = None
        if config.get("telegram_http_proxy"):
            url = urlsplit(config["telegram_http_proxy"])
            try:
                import python_socks.async_.asyncio
            except ImportError:
                raise ValueError("使用代理前请运行 python -m pip install -r requirements-owner.txt") from None
            proxy = ("http", url.hostname, url.port)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.client = TelegramClient(str(ROOT / "owner"), int(api_id), api_hash,
                                     receive_updates=False, flood_sleep_threshold=0,
                                     request_retries=1, connection_retries=1, proxy=proxy)

    def __enter__(self):
        try:
            self.loop.run_until_complete(self.connect())
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    async def connect(self):
        def login_required():
            raise ValueError("号主会话未登录或已过期。请在控制台点「登录 / 配置凭据」。")

        await self.client.start(
            phone=login_required if self.headless else lambda: input("号主手机号（含国家区号）: ").strip(),
            code_callback=lambda: getpass.getpass("Telegram 登录验证码: ").strip(),
            password=lambda: getpass.getpass("Telegram 两步验证密码: "),
        )
        me = await self.client.get_me()
        if not me or me.id != self.owner_id:
            raise ValueError("登录账号与 config.json 的 owner_id 不一致。")
        self.username = me.username

    def __exit__(self, *exc):
        try:
            self.loop.run_until_complete(self.disconnect())
        finally:
            self.loop.close()
            asyncio.set_event_loop(None)

    async def disconnect(self):
        await self.client.disconnect()

    async def resolve_chat(self, chat_id):
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:
            async for dialog in self.client.iter_dialogs():
                if dialog.id == chat_id:
                    return dialog.input_entity
            raise ValueError("群不在号主账号的聊天列表中") from None

    def old_channel_pins(self, chat_id, linked_id, observed, limit=20):
        channel_id, kind = utils.resolve_id(linked_id)
        if kind is not types.PeerChannel or not observed:
            return []
        source = types.PeerChannel(channel_id)

        async def collect():
            peer = await self.resolve_chat(chat_id)
            earliest = min(observed.values())
            protected = set()
            async for entry in self.client.iter_admin_log(peer, pinned=True):
                if entry.date.timestamp() < earliest:
                    break
                action = entry.original.action
                if isinstance(action, types.ChannelAdminLogEventActionUpdatePinned):
                    mid = action.message.id
                    if mid in observed:
                        protected.add(mid)
            ids = []
            async for message in self.client.iter_messages(peer, filter=types.InputMessagesFilterPinned):
                forward = message.fwd_from
                if (message.id in observed and message.id not in protected
                        and message.from_id == source and forward
                        and forward.from_id == source and forward.channel_post):
                    ids.append(message.id)
                    if limit is not None and len(ids) >= limit:
                        break
            return ids

        try:
            return self.loop.run_until_complete(asyncio.wait_for(collect(), timeout=25))
        except (errors.RPCError, OSError, ValueError, TimeoutError) as exc:
            raise RuntimeError(type(exc).__name__) from None

    def count(self, uid, username):
        try:
            return self.loop.run_until_complete(asyncio.wait_for(self._count(uid, username), timeout=20))
        except (TimeoutError, OSError, ValueError, errors.RPCError):
            return None

    async def _resolve_user(self, uid, username):
        try:
            peer = await self.client.get_input_entity(types.PeerUser(uid))
        except ValueError:
            peer = None
        if peer is None and username:
            # Resolve the current handle rather than trusting a cached username.
            resolved = await self.client(functions.contacts.ResolveUsernameRequest(username))
            if not isinstance(resolved.peer, types.PeerUser) or resolved.peer.user_id != uid:
                return None
            peer = next((user for user in resolved.users if user.id == uid), None)
            if peer is None:
                return None
        if peer is None:
            # A recent private message lets the owner account resolve users
            # who do not have a public username.
            async for dialog in self.client.iter_dialogs(limit=100):
                if dialog.is_user and dialog.id == uid:
                    peer = dialog.input_entity
                    break
        return peer

    def user_status(self, uid, username):
        async def query():
            peer = await self._resolve_user(uid, username)
            if peer is None:
                return None
            if isinstance(peer, types.User):
                return peer
            users = await self.client(functions.users.GetUsersRequest([peer]))
            return next((user for user in users if isinstance(user, types.User) and user.id == uid), None)

        try:
            return self.loop.run_until_complete(asyncio.wait_for(query(), timeout=20))
        except (TimeoutError, OSError, ValueError, errors.RPCError):
            pass
        return None

    def is_deleted(self, uid, username):
        user = self.user_status(uid, username)
        return bool(user and user.deleted)

    def has_visible_photo(self, uid, username):
        user = self.user_status(uid, username)
        if not user or user.deleted:
            return None
        photo = user.photo
        if isinstance(photo, types.UserProfilePhoto):
            return None if photo.personal else True
        if user.min:
            return None
        if photo is None or isinstance(photo, types.UserProfilePhotoEmpty):
            return False
        return None

    async def _count(self, uid, username):
        peer = await self._resolve_user(uid, username)
        if peer is None:
            return None
        groups = set()
        cursor = 0
        while True:
            page = await self.client(functions.messages.GetCommonChatsRequest(peer, cursor, 100))
            if not page.chats:
                return len(groups)
            for chat in page.chats:
                if (isinstance(chat, types.Chat) and not chat.deactivated and not chat.migrated_to
                        or isinstance(chat, types.Channel) and chat.megagroup
                        or isinstance(chat, types.ChannelForbidden) and chat.megagroup
                        or isinstance(chat, types.ChatForbidden)):
                    groups.add((type(chat) in (types.Channel, types.ChannelForbidden), chat.id))
            if isinstance(page, types.messages.Chats):
                return len(groups)
            next_cursor = min(chat.id for chat in page.chats)
            if cursor and next_cursor >= cursor:
                return None
            cursor = next_cursor
