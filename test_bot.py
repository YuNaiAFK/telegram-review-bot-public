import sqlite3
import unittest
from unittest.mock import Mock

from bot import ApiError, ApiNetworkError, Bot


class Flow(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.bot = Bot("test", {"owner_id": 1, "reviewer_ids": [1],
                               "common_group_ids": [], "targets": {"-2": "为什么加入？"}}, self.db)
        self.join = {"chat": {"id": -2, "title": "频道"}, "from": {"id": 3}, "user_chat_id": 4}
        self.photos = 1
        self.bot.api = Mock(side_effect=self.api)

    def api(self, method, **kwargs):
        if method == "getUserProfilePhotos":
            return {"total_count": self.photos}
        if method == "getChatMember":
            return {"status": "creator"}
        return True

    def message(self, uid, text):
        self.bot.message({"chat": {"type": "private"}, "from": {"id": uid}, "text": text})

    def test_no_photo_rejects_once(self):
        self.photos = 0
        self.bot.join(100, self.join)
        self.bot.join(100, self.join)
        self.assertEqual(self.bot.get(1)["state"], "rejected")
        self.assertEqual([c.args[0] for c in self.bot.api.call_args_list],
                         ["getUserProfilePhotos", "sendMessage", "declineChatJoinRequest"])

    def test_answer_ownership_and_approval(self):
        self.bot.join(100, self.join)
        self.message(5, "/answer 1 假回答")
        self.assertEqual(self.bot.get(1)["state"], "waiting")
        self.message(3, "/answer 1 正确回答")
        self.assertEqual(self.bot.get(1)["state"], "review")
        for uid in (5, 1):
            self.bot.callback({"id": "callback", "data": "approve:1", "from": {"id": uid}})
            self.assertEqual(self.bot.get(1)["state"], "review" if uid == 5 else "approved")

    def test_common_group_threshold(self):
        self.bot.config["min_common_groups"] = 8
        self.bot.common_counts = Mock(return_value=(7, 0, 8))
        self.bot.join(100, self.join)
        self.assertEqual(self.bot.get(1)["state"], "rejected")

    def test_unverified_group_blocks_answer_until_checked(self):
        self.bot.config["min_common_groups"] = 8
        self.bot.common_counts = Mock(return_value=(7, 1, 8))
        self.bot.join(100, self.join)
        self.message(3, "/answer 1 我的回答")
        self.assertEqual(self.bot.get(1)["state"], "waiting")
        self.bot.common_counts.return_value = (8, 0, 8)
        self.message(3, "/pending")
        self.message(3, "/answer 1 我的回答")
        self.assertEqual(self.bot.get(1)["state"], "review")

    def test_owner_account_mode_does_not_use_configured_groups(self):
        self.bot.config["min_common_groups"] = 8
        self.bot.owner_account = Mock(username="owner", **{"count.return_value": 8})
        self.bot.common_counts = Mock(side_effect=AssertionError("configured fallback"))
        self.join["from"]["username"] = "applicant"
        self.bot.join(100, self.join)
        self.bot.message({"chat": {"type": "private"}, "from": {"id": 3, "username": "applicant"},
                          "text": "/answer 1 我的回答"})
        self.bot.callback({"id": "callback", "data": "approve:1", "from": {"id": 1}})
        self.assertEqual(self.bot.get(1)["state"], "approved")
        self.assertEqual(self.bot.owner_account.count.call_count, 2)
        self.bot.owner_account.count.assert_called_with(3, "applicant")

    def test_answer_pushes_review_card_to_admin(self):
        self.bot.join(100, self.join)
        self.message(3, "/answer 1 我想加入讨论")
        cards = [c.kwargs for c in self.bot.api.call_args_list
                 if c.args[0] == "sendMessage" and c.kwargs["chat_id"] == 1]
        self.assertEqual(len(cards), 1)
        self.assertIn("回答：我想加入讨论", cards[0]["text"])
        self.assertIn("为什么加入？", cards[0]["text"])
        self.assertEqual(cards[0]["reply_markup"]["inline_keyboard"][1][0]["callback_data"],
                         "approve:1")

    def test_auto_unpin_is_opt_in_and_only_targets_channel_forwards(self):
        self.bot.identity = {"id": 8, "username": "test"}
        chat = {"id": -2, "type": "supergroup"}
        post = {"chat": chat, "message_id": 11, "is_automatic_forward": True}
        self.bot.api.side_effect = lambda method, **_: {
            "getChatMember": {"status": "creator"},
            "getChat": {"linked_chat_id": -3},
        }.get(method, True)
        self.bot.message(post)
        self.bot.api.assert_not_called()
        self.bot.message({"chat": chat, "from": {"id": 1}, "text": "/autounpin@test on"})
        self.bot.message({"chat": chat, "pinned_message": {"message_id": 12}})
        self.bot.message(post)
        self.bot.api.assert_called_with("unpinChatMessage", chat_id=-2, message_id=11)
        self.bot.message({"chat": chat, "pinned_message": post})
        self.bot.api.assert_called_with("unpinChatMessage", chat_id=-2, message_id=11)
        self.bot.message({"chat": chat, "from": {"id": 1}, "text": "/autounpin off"})
        self.bot.api.reset_mock()
        self.bot.message(post)
        self.bot.api.assert_not_called()

    def prepare_pin_lookup(self, selected):
        self.bot.identity = {"id": 8, "username": "test"}
        self.bot.owner_account = Mock(**{"old_channel_pins.return_value": selected})
        self.bot.api.side_effect = lambda method, **_: {
            "getChatMember": {"status": "creator"},
            "getChat": {"linked_chat_id": -1001234567890},
        }.get(method, True)

    def observe_forward(self, mid):
        self.bot.message({"chat": {"id": -2, "type": "supergroup"},
                          "message_id": mid, "is_automatic_forward": True,
                          "sender_chat": {"id": -1001234567890, "type": "channel"}})

    def test_unpin_preserves_untracked_and_manual_pins(self):
        m = {"chat": {"id": -2, "type": "supergroup"}, "from": {"id": 3},
             "text": "/unpin", "reply_to_message": {"message_id": 21}}
        self.bot.api.side_effect = lambda method, **_: {"status": "member"} if method == "getChatMember" else True
        self.bot.message(m)
        self.assertNotIn("unpinChatMessage", [c.args[0] for c in self.bot.api.call_args_list])
        self.prepare_pin_lookup([21])
        self.bot.api.reset_mock()
        self.bot.message(m)
        self.bot.owner_account.old_channel_pins.assert_not_called()
        self.assertNotIn("unpinChatMessage", [c.args[0] for c in self.bot.api.call_args_list])
        self.observe_forward(21)
        self.bot.message(m)
        self.bot.api.assert_called_with("unpinChatMessage", chat_id=-2, message_id=21)

    def test_deleted_account_declined_without_sending_question(self):
        self.bot.owner_account = Mock(**{"is_deleted.return_value": True})
        self.bot.join(100, self.join)
        self.bot.join(100, self.join)
        self.assertEqual(self.bot.get(1)["state"], "rejected")
        self.bot.api.assert_called_once_with("declineChatJoinRequest", chat_id=-2, user_id=3)
        self.bot.owner_account.count.assert_not_called()

    def test_telegram_refusal_to_remove_deactivated_request_is_not_recorded_as_rejected(self):
        self.bot.owner_account = Mock(**{"is_deleted.return_value": True})
        self.bot.api.side_effect = ApiError("declineChatJoinRequest: Forbidden: user is deactivated")
        with self.assertLogs(level="WARNING") as logs:
            self.bot.join(100, self.join)
        self.assertEqual(self.bot.get(1)["state"], "inactive")
        self.bot.api.assert_called_once_with("declineChatJoinRequest", chat_id=-2, user_id=3)
        self.assertIn("Telegram 不允许", logs.output[0])

    def test_deleted_after_answer_declined_on_approval(self):
        self.bot.join(100, self.join)
        self.message(3, "/answer 1 我的回答")
        self.bot.owner_account = Mock(**{"is_deleted.return_value": True})
        self.bot.callback({"id": "callback", "data": "approve:1", "from": {"id": 1}})
        self.assertEqual(self.bot.get(1)["state"], "rejected")
        self.assertNotIn("approveChatJoinRequest", [c.args[0] for c in self.bot.api.call_args_list])

    def test_unknown_account_status_does_not_trigger_deleted_rejection(self):
        self.bot.owner_account = Mock(**{"is_deleted.return_value": None})
        self.bot.join(100, self.join)
        self.assertEqual(self.bot.get(1)["state"], "waiting")
        self.assertEqual(self.bot.get(1)["notified"], 1)

    def test_registers_commands_for_correct_scopes(self):
        self.bot.api.side_effect = lambda method, **_: (
            {"type": "channel", "linked_chat_id": -3} if method == "getChat" else True)
        self.bot.register_commands()
        menus = {c.kwargs["scope"]["type"]: c.kwargs for c in self.bot.api.call_args_list
                 if c.args == ("setMyCommands",)}
        commands = lambda scope: {x["command"] for x in menus[scope]["commands"]}
        self.assertEqual(commands("all_chat_administrators"), {"help", "unpin", "unpinall", "autounpin", "unpinold"})
        self.assertEqual(commands("all_private_chats"), {"start", "pending", "answer"})
        self.assertEqual(menus["chat_administrators"]["scope"]["chat_id"], -3)
        self.assertEqual(menus["chat_administrators"]["language_code"], "zh")
        self.assertEqual(menus["chat"]["scope"]["chat_id"], 1)
        self.assertIn("reviews", commands("chat"))

    def test_owner_can_confirm_avatar_when_bot_cannot_resolve_user(self):
        self.bot.owner_account = Mock(**{"is_deleted.return_value": False,
                                         "has_visible_photo.return_value": True})
        original = self.bot.api.side_effect

        def api(method, **kwargs):
            if method == "getUserProfilePhotos":
                raise ApiError("getUserProfilePhotos: Bad Request: USER_ID_INVALID")
            return original(method, **kwargs)

        self.bot.api.side_effect = api
        self.join["from"]["username"] = "applicant"
        self.bot.join(100, self.join)
        self.bot.owner_account.has_visible_photo.assert_called_once_with(3, "applicant")
        self.assertEqual(self.bot.get(1)["notified"], 1)

    def test_owner_confirms_no_avatar_and_rejects(self):
        self.bot.owner_account = Mock(**{"is_deleted.return_value": False,
                                         "has_visible_photo.return_value": False})
        original = self.bot.api.side_effect

        def api(method, **kwargs):
            if method == "getUserProfilePhotos":
                raise ApiError("getUserProfilePhotos: Bad Request: USER_ID_INVALID")
            return original(method, **kwargs)

        self.bot.api.side_effect = api
        self.bot.join(100, self.join)
        row = self.bot.get(1)
        self.assertEqual((row["photos"], row["state"], row["notified"]), (0, "rejected", 0))
        self.bot.api.assert_called_with("declineChatJoinRequest", chat_id=-2, user_id=3)

    def test_unknown_profile_is_kept_and_pending_can_retry(self):
        original = self.bot.api.side_effect
        def unknown(method, **kwargs):
            if method == "getUserProfilePhotos":
                raise ApiError("getUserProfilePhotos: Bad Request: USER_ID_INVALID")
            return original(method, **kwargs)

        self.bot.api.side_effect = unknown
        self.bot.join(100, self.join)
        self.assertIsNone(self.bot.get(1)["photos"])
        self.assertEqual(self.bot.get(1)["state"], "waiting")
        self.assertNotIn("declineChatJoinRequest", [c.args[0] for c in self.bot.api.call_args_list])
        self.bot.api.side_effect = original
        self.message(3, "/pending")
        self.assertEqual(self.bot.get(1)["photos"], 1)
        self.assertEqual(self.bot.get(1)["notified"], 1)

    def test_group_help_refreshes_menu_and_lists_commands(self):
        self.bot.identity = {"id": 8, "username": "test"}
        self.bot.message({"chat": {"id": -2, "type": "supergroup"},
                          "from": {"id": 1}, "text": "/help@test"})
        self.assertIn("/unpinold", self.bot.api.call_args.kwargs["text"])
        self.assertTrue(any(c.kwargs.get("scope") == {"type": "chat_administrators", "chat_id": -2}
                            for c in self.bot.api.call_args_list))

    def test_unpinall_excludes_manual_pin_even_on_forwarded_channel_post(self):
        self.prepare_pin_lookup([21])
        self.observe_forward(21)
        self.observe_forward(25)
        self.bot.message({"chat": {"id": -2, "type": "supergroup"}, "from": {"id": 1},
                          "pinned_message": {"message_id": 25, "is_automatic_forward": True}})
        m = {"chat": {"id": -2, "type": "supergroup"}, "from": {"id": 1},
             "text": "/unpinall", "message_id": 40}
        self.bot.message(m)
        self.assertEqual(self.bot.owner_account.old_channel_pins.call_args.kwargs, {"limit": None})
        self.assertEqual(set(self.bot.owner_account.old_channel_pins.call_args.args[2]), {21})
        calls = self.bot.api.call_args_list
        self.assertEqual([c.kwargs["message_id"] for c in calls if c.args == ("unpinChatMessage",)], [21])
        self.assertNotIn("unpinAllChatMessages", [c.args[0] for c in calls])
        self.assertNotIn("sendMessage", [c.args[0] for c in calls])
        self.bot.api.assert_called_with("deleteMessage", chat_id=-2, message_id=40)

    def test_unpinold_uses_only_verified_pins(self):
        self.prepare_pin_lookup([21,25])
        self.observe_forward(21)
        self.observe_forward(25)
        self.bot.message({"chat": {"id": -2, "type": "supergroup"},
                          "from": {"id": 1}, "text": "/unpinold"})
        self.assertEqual(set(self.bot.owner_account.old_channel_pins.call_args.args[2]), {21,25})
        self.assertEqual(self.bot.owner_account.old_channel_pins.call_args.kwargs, {"limit": 20})
        ids = [c.kwargs["message_id"] for c in self.bot.api.call_args_list
               if c.args == ("unpinChatMessage",)]
        self.assertEqual(ids, [21,25])
        self.assertFalse(any(c.args == ("sendMessage",) for c in self.bot.api.call_args_list))

    def test_temp_chat_failure_uses_existing_private_conversation(self):
        original = self.bot.api.side_effect
        def api(method, **kwargs):
            if method == "sendMessage" and kwargs["chat_id"] == 4:
                raise ApiError("sendMessage: Bad Request: chat not found")
            return original(method, **kwargs)

        self.bot.api.side_effect = api
        self.bot.join(100, self.join)
        self.assertEqual(self.bot.get(1)["notified"], 1)
        self.assertEqual([c.kwargs["chat_id"] for c in self.bot.api.call_args_list
                          if c.args[0] == "sendMessage"], [4, 3])

    def test_private_application_issues_non_expiring_link_and_only_redeems_once(self):
        self.bot.config["admission_mode"] = "private_invite"
        self.bot.identity = {"id": 8, "username": "test"}
        invite = "https://t.me/+example-test"
        original = self.bot.api.side_effect

        def api(method, **kwargs):
            if method == "getChat":
                return {"id": -2, "type": "channel", "title": "频道"}
            if method == "getChatMember" and kwargs["user_id"] != 1:
                return {"status": "left"}
            if method == "createChatInviteLink":
                return {"invite_link": invite}
            return original(method, **kwargs)

        self.bot.api.side_effect = api
        self.message(3, "/start")
        self.bot.callback({"id": "apply", "from": {"id": 3}, "data": "apply:-2",
                           "message": {"chat": {"type": "private"}}})
        self.message(3, "我想加入讨论")
        self.assertEqual(self.bot.get(1)["state"], "review")
        self.assertTrue(any("回答：我想加入讨论" in c.kwargs.get("text", "")
                            and c.kwargs.get("chat_id") == 1
                            for c in self.bot.api.call_args_list))
        self.bot.callback({"id": "yes", "data": "approve:1", "from": {"id": 1}})
        self.assertEqual(self.bot.get(1)["state"], "invited")
        creates = [c.kwargs for c in self.bot.api.call_args_list if c.args == ("createChatInviteLink",)]
        self.assertEqual(creates, [{"chat_id": -2, "creates_join_request": True, "name": "review-1"}])
        def calls(name):
            return [c.kwargs for c in self.bot.api.call_args_list if c.args == (name,)]
        self.assertEqual(calls("approveChatJoinRequest"), [])
        wrong = {**self.join, "from": {"id": 4}, "invite_link": {"invite_link": invite}}
        self.bot.join(101, wrong)
        self.assertEqual(calls("approveChatJoinRequest"), [])
        self.assertEqual(self.bot.get(1)["state"], "invited")
        right = {**self.join, "invite_link": {"invite_link": invite}}
        self.bot.join(102, right)
        self.bot.join(103, right)
        self.assertEqual(calls("approveChatJoinRequest"), [{"chat_id": -2, "user_id": 3}])
        self.assertEqual(calls("revokeChatInviteLink"), [{"chat_id": -2, "invite_link": invite}])
        self.assertEqual(self.bot.get(1)["state"], "redeemed")
        self.assertIsNone(self.bot.get(1)["invite"])

    def test_join_without_invite_metadata_redeems_only_existing_private_approval(self):
        self.bot.config["admission_mode"] = "private_invite"
        self.bot.identity = {"id": 8, "username": "test"}
        self.bot.save("INSERT INTO requests (chat,uid,flow,state,invite) VALUES (?,?,?,?,?)",
                      -2, 3, "private", "invited", "https://t.me/+test")
        self.bot.join(101, {**self.join, "from": {"id": 4}})
        self.bot.join(102, {**self.join, "invite_link": {"invite_link": "https://t.me/+different"}})
        self.bot.join(103, self.join)
        self.bot.join(104, self.join)
        approvals = [c.kwargs for c in self.bot.api.call_args_list if c.args == ("approveChatJoinRequest",)]
        self.assertEqual(approvals, [{"chat_id": -2, "user_id": 3}])
        self.assertEqual(self.bot.get(1)["state"], "redeemed")
        self.assertNotIn("sendMessage", [c.args[0] for c in self.bot.api.call_args_list])

    def test_join_recognizes_equivalent_invite_url(self):
        self.bot.config["admission_mode"] = "private_invite"
        self.bot.save("INSERT INTO requests (chat,uid,flow,state,invite) VALUES (?,?,?,?,?)",
                      -2, 3, "private", "invited", "https://t.me/+same-link")
        self.bot.join(101, {**self.join, "invite_link": {"invite_link": "https://t.me/joinchat/same-link"}})
        self.assertEqual(self.bot.get(1)["state"], "redeemed")
        self.bot.api.assert_called_with("revokeChatInviteLink", chat_id=-2, invite_link="https://t.me/+same-link")

    def test_invite_cannot_be_reused_even_if_telegram_revocation_fails(self):
        self.bot.config["admission_mode"] = "private_invite"
        self.bot.save("INSERT INTO requests (chat,uid,flow,state,invite) VALUES (?,?,?,?,?)",
                      -2, 3, "private", "invited", "https://t.me/+test")
        original = self.bot.api.side_effect

        def api(method, **kwargs):
            if method == "revokeChatInviteLink":
                raise ApiError("network unavailable")
            return original(method, **kwargs)

        self.bot.api.side_effect = api
        j = {**self.join, "invite_link": {"invite_link": "https://t.me/+test"}}
        with self.assertLogs(level="WARNING"):
            self.bot.join(101, j)
        self.assertEqual(self.bot.get(1)["state"], "redeemed")
        self.assertIsNotNone(self.bot.get(1)["invite"])
        self.bot.join(102, j)
        self.assertEqual(sum(c.args[0] == "approveChatJoinRequest" for c in self.bot.api.call_args_list), 1)
        self.bot.api.side_effect = original
        self.bot.revoke_redeemed_invite(self.bot.get(1))
        self.assertIsNone(self.bot.get(1)["invite"])

    def test_private_application_rejects_no_photo_or_not_enough_common_groups_by_dm(self):
        self.bot.config.update(admission_mode="private_invite", min_common_groups=8)
        original = self.bot.api.side_effect
        self.bot.api.side_effect = lambda method, **kwargs: (
            {"title": "频道"} if method == "getChat" else
            {"status": "left"} if method == "getChatMember" else original(method, **kwargs))
        self.photos = 0
        self.bot.start_private_application(-2, {"id": 3})
        self.photos = 1
        self.bot.common_counts = Mock(return_value=(7, 0, 8))
        self.bot.start_private_application(-2, {"id": 4})
        self.assertEqual([self.bot.get(i)["state"] for i in (1, 2)], ["rejected", "rejected"])
        dms = {c.kwargs["chat_id"]: c.kwargs["text"] for c in self.bot.api.call_args_list
               if c.args == ("sendMessage",)}
        self.assertIn("未检测到可见头像", dms[3])
        self.assertEqual(dms[4].splitlines()[-1], "拒绝理由：共同群不足8个")
        self.assertFalse(any(c.args[0] in {"declineChatJoinRequest", "createChatInviteLink"}
                             for c in self.bot.api.call_args_list))

    def test_existing_member_is_told_without_screening_or_new_application(self):
        self.bot.config["admission_mode"] = "private_invite"
        self.bot.api.side_effect = lambda method, **_: (
            {"status": "restricted", "is_member": True} if method == "getChatMember" else True)
        self.bot.start_private_application(-2, {"id": 3})
        self.assertIsNone(self.bot.get(1))
        self.assertEqual([c.args[0] for c in self.bot.api.call_args_list], ["getChatMember", "sendMessage"])
        self.assertEqual(self.bot.api.call_args.kwargs["text"], "您已在群，不需要再次审核。")

    def test_member_joining_before_approval_does_not_get_invite(self):
        self.bot.config["admission_mode"] = "private_invite"
        rid = self.bot.save("INSERT INTO requests (chat,uid,flow,state) VALUES (?,?,?,?)",
                            -2, 3, "private", "review")
        self.bot.callback({"id": "yes", "data": f"approve:{rid}", "from": {"id": 1}})
        self.assertEqual(self.bot.get(rid)["state"], "closed")
        self.assertNotIn("createChatInviteLink", [c.args[0] for c in self.bot.api.call_args_list])

    def test_start_sends_application_button_without_chat_lookup(self):
        self.bot.config["admission_mode"] = "private_invite"
        self.bot.target_titles["-2"] = "测试群"
        self.message(3, "/start review")
        self.assertEqual([c.args[0] for c in self.bot.api.call_args_list], ["sendMessage"])
        self.assertEqual(self.bot.api.call_args.kwargs["reply_markup"]["inline_keyboard"],
                         [[{"text": "申请加入 · 测试群", "callback_data": "apply:-2"}]])

    def test_start_menu_network_failure_retries_once(self):
        self.bot.config["admission_mode"] = "private_invite"
        self.bot.api.side_effect = [ApiNetworkError("offline"), True]
        with self.assertLogs(level="WARNING"):
            self.message(3, "/start")
        self.bot.pending_apply_menus[3] = 0
        self.bot.retry_apply_menu()
        self.bot.retry_apply_menu()
        self.assertFalse(self.bot.pending_apply_menus)
        self.assertEqual(self.bot.api.call_count, 2)

    def setup_review_card(self):
        self.bot.config["admission_mode"] = "private_invite"
        self.bot.identity = {"id": 8, "username": "test"}
        self.bot.save("""INSERT INTO requests (chat,uid,dm,label,question,photos,common,answer,username,flow,state)
                         VALUES (?,?,?,?,?,?,?,?,?,'private','review')""",
                      -2, 3, 3, "频道", "问题", 1, "14 个", "原回答", "applicant")
        original = self.bot.api.side_effect

        def api(method, **kwargs):
            if method == "sendMessage" and kwargs["chat_id"] == 1:
                return {"message_id": 77}
            return original(method, **kwargs)

        self.bot.api.side_effect = api
        self.bot.review(self.bot.get(1), 1)

    def review_click(self, action):
        self.bot.callback({"id": action, "data": f"{action}:1", "from": {"id": 1},
                           "message": {"chat": {"id": 1, "type": "private"}, "message_id": 77}})

    def test_review_profile_link_and_result_only_rejection_edit_same_card(self):
        self.setup_review_card()
        buttons = self.bot.api.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual(buttons[0][0]["url"], "https://t.me/applicant")
        self.assertEqual(buttons[-1][0]["callback_data"], "reject_menu:1")
        self.bot.api.reset_mock()
        self.review_click("reject_menu")
        self.review_click("reject_silent")
        self.assertEqual(self.bot.get(1)["state"], "rejected")
        sent = [c.kwargs for c in self.bot.api.call_args_list if c.args == ("sendMessage",)]
        self.assertEqual([c["chat_id"] for c in sent], [3])
        self.assertTrue(sent[0]["text"].endswith("审核结果：未通过"))
        self.assertNotIn("拒绝理由", sent[0]["text"])
        edits = [c.kwargs for c in self.bot.api.call_args_list if c.args == ("editMessageText",)]
        self.assertEqual([c["message_id"] for c in edits], [77, 77])
        self.assertIn("仅发送了审核结果", edits[-1]["text"])
        self.assertIn("原回答", edits[-1]["text"])

    def test_rejection_reason_is_taken_only_from_reply_to_original_card(self):
        self.setup_review_card()
        self.review_click("reject_menu")
        self.review_click("reject_reason")
        self.bot.api.reset_mock()
        self.bot.message({"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": "问题未答完整",
                          "reply_to_message": {"message_id": 77, "from": {"id": 8}}})
        self.assertEqual(self.bot.get(1)["state"], "rejected")
        sent = [c.kwargs for c in self.bot.api.call_args_list if c.args == ("sendMessage",)]
        self.assertEqual([c["chat_id"] for c in sent], [3])
        self.assertIn("问题未答完整", sent[0]["text"])
        self.assertEqual(sent[0]["reply_markup"]["inline_keyboard"][0][0]["text"], "重新申请")
        edit = [c.kwargs for c in self.bot.api.call_args_list if c.args == ("editMessageText",)][0]
        self.assertEqual((edit["chat_id"], edit["message_id"]), (1, 77))
        self.assertIn("拒绝理由：问题未答完整", edit["text"])

    def test_rejected_applicant_can_reapply_even_when_button_ack_expired(self):
        self.setup_review_card()
        self.review_click("reject_menu")
        self.review_click("reject_silent")
        current = self.bot.api.side_effect
        def api(method, **kwargs):
            if method == "getChat":
                return {"title": "频道"}
            if method == "getChatMember" and kwargs["user_id"] == 3:
                return {"status": "left"}
            if method == "answerCallbackQuery":
                raise ApiError("query is too old")
            return current(method, **kwargs)

        self.bot.api.side_effect = api
        self.message(3, "/pending")
        keyboard = self.bot.api.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertTrue(keyboard[0][0]["text"].startswith("重新申请"))
        with self.assertLogs(level="WARNING"):
            self.bot.callback({"id": "expired", "data": "apply:-2", "from": {"id": 3},
                               "message": {"chat": {"id": 3, "type": "private"}}})
        self.assertEqual(self.bot.get(1)["state"], "rejected")
        self.assertEqual(self.bot.get(2)["state"], "waiting")
        self.assertEqual(self.bot.get(2)["notified"], 1)

    def test_manual_rejection(self):
        self.bot.join(100, self.join)
        self.message(3, "/answer 1 我的回答")
        self.message(1, "/reject 1 请补充你的申请动机")
        self.assertEqual(self.bot.get(1)["state"], "rejected")
        self.assertTrue(any("请补充你的申请动机" in c.kwargs.get("text", "")
                            and c.kwargs.get("chat_id") == 3 for c in self.bot.api.call_args_list))


if __name__ == "__main__":
    unittest.main()
