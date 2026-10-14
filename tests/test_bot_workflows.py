"""Test live bot command routing and database-backed ticket/vouch behavior.

All Discord calls are mocked; no token, gateway or AI inference is used.
"""
import importlib
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "ci-only-token")
os.environ["DB_PATH"] = ":memory:"
bot_module = importlib.import_module("bot")


def make_message(text, sender=111111111111111111, guild_id=1559829257952174192, manager=False):
    author = SimpleNamespace(
        id=sender, bot=False, guild_permissions=SimpleNamespace(manage_guild=manager)
    )
    channel = SimpleNamespace(
        id=222222222222222222,
        send=AsyncMock(),
        reply=AsyncMock(),
    )
    recipient = SimpleNamespace(id=333333333333333333, bot=False)
    guild = SimpleNamespace(
        id=guild_id,
        get_member=lambda member_id: recipient if member_id == recipient.id else None,
    )
    return SimpleNamespace(
        author=author,
        content=text,
        channel=channel,
        guild=guild,
        attachments=[],
        reply=AsyncMock(),
    )


class VouchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ("vouches", "vouch_cooldowns", "audit"):
            bot_module.db.execute("DELETE FROM " + name)
        bot_module.db.commit()

    def test_vouch_cooldown_is_global_and_lasts_30_minutes(self):
        voter, target1, target2 = 111111111111111111, 333333333333333333, 444444444444444444
        self.assertEqual(
            bot_module.add_member_vouch(voter, target1, at=10000),
            ("added", 1, 0),
        )
        self.assertEqual(
            bot_module.add_member_vouch(voter, target2, at=10001),
            ("cooldown", 0, 1799),
        )
        self.assertEqual(
            bot_module.add_member_vouch(voter, target2, at=11800),
            ("added", 1, 0),
        )
        self.assertEqual(bot_module.vouch_total(target1), 1)
        self.assertEqual(bot_module.vouch_total(target2), 1)

    def test_self_vouch_is_rejected(self):
        me = 111111111111111111
        self.assertEqual(bot_module.add_member_vouch(me, me, at=10000), ("self", 0, 0))
        self.assertEqual(bot_module.vouch_total(me), 0)

    def test_owner_override_does_not_reset_cooldown(self):
        voter, member = 111111111111111111, 333333333333333333
        self.assertEqual(bot_module.add_member_vouch(voter, member, at=10000)[0], "added")
        bot_module.set_member_vouches(bot_module.OWNER_ID, member, 28)
        self.assertEqual(bot_module.vouch_total(member), 28)
        self.assertEqual(
            bot_module.add_member_vouch(voter, member, at=10500),
            ("cooldown", 28, 1300),
        )

    async def test_vouch_mention_adds_to_existing_total(self):
        msg = make_message("vouch <@333333333333333333>")
        await bot_module.on_message(msg)
        msg.channel.send.assert_awaited_once()
        self.assertIn("**1 vouch**", msg.channel.send.await_args.args[0])
        self.assertEqual(bot_module.vouch_total(333333333333333333), 1)

    async def test_self_vouch_command_replies_with_error(self):
        user_id = 111111111111111111
        msg = make_message(f"vouch <@{user_id}>", sender=user_id)
        await bot_module.on_message(msg)
        msg.reply.assert_awaited_once()
        self.assertEqual(bot_module.vouch_total(user_id), 0)

    async def test_vouch_cooldown_works_in_discord_command(self):
        first = make_message("vouch <@333333333333333333>")
        second = make_message("vouch <@333333333333333333>")
        await bot_module.on_message(first)
        await bot_module.on_message(second)
        second.reply.assert_awaited_once()
        self.assertIn("minute", second.reply.await_args.args[0])
        self.assertEqual(bot_module.vouch_total(333333333333333333), 1)

    async def test_owner_can_edit_vouches_by_user_id(self):
        msg = make_message("!vouchcount 333333333333333333 18", sender=bot_module.OWNER_ID)
        await bot_module.on_message(msg)
        self.assertEqual(bot_module.vouch_total(333333333333333333), 18)
        self.assertIn("**18**", msg.channel.send.await_args.args[0])

    async def test_non_owner_cannot_edit_vouches(self):
        msg = make_message("!vouchcount 333333333333333333 18")
        await bot_module.on_message(msg)
        msg.reply.assert_awaited_once()
        self.assertEqual(bot_module.vouch_total(333333333333333333), 0)

    async def test_say_requires_manager_permission(self):
        msg = make_message("!say Important announcement")
        await bot_module.on_message(msg)
        msg.reply.assert_awaited_once()
        msg.channel.send.assert_not_awaited()

    async def test_say_creates_embed_without_mentions(self):
        msg = make_message("!say Hello @everyone", manager=True)
        await bot_module.on_message(msg)
        msg.channel.send.assert_awaited_once()
        kwargs = msg.channel.send.await_args.kwargs
        self.assertEqual(kwargs["embed"].description, "Hello @everyone")
        self.assertFalse(kwargs["allowed_mentions"].everyone)

    def test_database_cooldown_survives_connection_restart(self):
        with tempfile.TemporaryDirectory() as root:
            original = bot_module.db
            path = os.path.join(root, "cooldowns.db")
            try:
                bot_module.db = sqlite3.connect(path)
                bot_module.db.row_factory = sqlite3.Row
                bot_module.db.executescript(
                    "CREATE TABLE vouches(user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0);"
                    "CREATE TABLE vouch_cooldowns(voter_id INTEGER PRIMARY KEY,last_vouch INTEGER NOT NULL);"
                    "CREATE TABLE audit(at INTEGER,actor INTEGER,action TEXT,detail TEXT);"
                )
                voter, target = 111111111111111111, 333333333333333333
                self.assertEqual(bot_module.add_member_vouch(voter, target, at=10000)[0], "added")
                bot_module.db.close()
                bot_module.db = sqlite3.connect(path)
                bot_module.db.row_factory = sqlite3.Row
                self.assertEqual(
                    bot_module.add_member_vouch(voter, target, at=10100),
                    ("cooldown", 1, 1700),
                )
                bot_module.db.close()
            finally:
                bot_module.db = original


class CompletionReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot_module.db.execute("DELETE FROM cases")
        bot_module.db.execute("DELETE FROM applications")
        bot_module.db.commit()

    async def test_owner_gets_one_conclusion_dm_with_transcript(self):
        channel_id = 222222222222222222
        owner = SimpleNamespace(send=AsyncMock())
        msg = SimpleNamespace(
            created_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
            clean_content="The trade is resolved.",
            author=SimpleNamespace(id=111111111111111111),
            attachments=[],
        )

        async def history(**kwargs):
            yield msg

        channel = SimpleNamespace(
            id=channel_id, mention=f"<#{channel_id}>", history=history
        )
        bot_module.db.execute(
            "INSERT INTO cases(channel_id,user_id,kind,summary,created,status) "
            "VALUES (?,?,?,?,?,'closed')",
            (channel_id, 111111111111111111, "ticket", "Dispute", 10000),
        )
        bot_module.db.execute(
            "INSERT INTO applications(channel_id,answers,decision) VALUES (?,?,?)",
            (channel_id, "Dispute", "Second chance"),
        )
        bot_module.db.commit()
        actor = SimpleNamespace(id=bot_module.OWNER_ID)
        with patch.object(bot_module.bot, "fetch_user", new=AsyncMock(return_value=owner)):
            await bot_module.send_completion_report(channel, bot_module.case_for(channel_id), actor)
            await bot_module.send_completion_report(channel, bot_module.case_for(channel_id), actor)
        owner.send.assert_awaited_once()
        self.assertIn("Second chance", owner.send.await_args.kwargs["content"])
        self.assertIn("Full ticket transcript", owner.send.await_args.kwargs["content"])
        self.assertEqual(bot_module.case_for(channel_id)["owner_notified"], 1)


if __name__ == "__main__":
    unittest.main()
