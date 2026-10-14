"""Offline integration tests for Wispbyte <-> verify.lb000.xyz role sync."""
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ["DB_PATH"] = ":memory:"

import bot
import verify_sync


SITE_GUILD = bot.GUILD_ID
SITE_MEMBER = 222222222222222222


class HttpReply:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self.payload


class HttpClient:
    closed = False

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.reply


class WebsiteFetchTests(unittest.IsolatedAsyncioTestCase):
    def sample(self):
        timestamp = int(time.time() * 1000)
        return {
            "source": "verify.lb000.xyz", "synced_at": timestamp,
            "members": [
                {"id": str(SITE_MEMBER), "server_id": str(SITE_GUILD), "verified_at": timestamp},
                {"id": "333333333333333333", "server_id": "999999999999999999", "verified_at": timestamp},
            ],
        }

    async def test_fetch_filters_other_servers_and_keeps_website_timestamp(self):
        data = self.sample()
        client = HttpClient(HttpReply(payload=data))
        with patch.dict(os.environ, {"VERIFY_SYNC_KEY": "a" * 40}, clear=True):
            events = await verify_sync.fetch_site_verifications(client, SITE_GUILD)
        self.assertEqual(events, [verify_sync.VerificationEvent(
            SITE_MEMBER, data["members"][0]["verified_at"]
        )])
        self.assertEqual(client.calls[0][0], "https://verify.lb000.xyz/api/bot/verified")
        self.assertEqual(client.calls[0][1]["headers"]["Authorization"], "Bearer " + "a" * 40)
        self.assertFalse(client.calls[0][1]["allow_redirects"])

    async def test_can_use_same_bot_token_without_shared_secret(self):
        client = HttpClient(HttpReply(payload=self.sample()))
        with patch.dict(os.environ, {"DISCORD_TOKEN": "local-private-token"}, clear=True):
            await verify_sync.fetch_site_verifications(client, SITE_GUILD)
        self.assertEqual(
            client.calls[0][1]["headers"]["Authorization"], "Bot local-private-token"
        )

    async def test_unauthorized_sync_gives_actionable_message_not_secrets(self):
        client = HttpClient(HttpReply(status=403))
        with patch.dict(os.environ, {"VERIFY_SYNC_KEY": "m" * 48}, clear=True):
            with self.assertRaises(verify_sync.VerificationSyncError) as ctx:
                await verify_sync.fetch_site_verifications(client, SITE_GUILD)
        self.assertIn("VERIFY_SYNC_KEY", str(ctx.exception))
        self.assertNotIn("m" * 48, str(ctx.exception))

    async def test_rejects_invalid_source_and_malformed_response(self):
        client = HttpClient(HttpReply(payload={"members": self.sample()["members"]}))
        with patch.dict(os.environ, {"VERIFY_SYNC_KEY": "b" * 40}, clear=True):
            with self.assertRaises(verify_sync.VerificationSyncError):
                await verify_sync.fetch_site_verifications(client, SITE_GUILD)

    async def test_expired_and_future_events_never_grant_roles(self):
        data = self.sample()
        data["members"].extend([
            {"id": "444444444444444444", "server_id": str(SITE_GUILD), "verified_at": 1},
            {"id": "555555555555555555", "server_id": str(SITE_GUILD), "verified_at": int(time.time() * 1000) + 200000},
        ])
        client = HttpClient(HttpReply(payload=data))
        with patch.dict(os.environ, {"VERIFY_SYNC_KEY": "c" * 40}, clear=True):
            events = await verify_sync.fetch_site_verifications(client, SITE_GUILD)
        self.assertEqual(len(events), 1)

    def test_short_secret_cannot_weaken_auth(self):
        with patch.dict(os.environ, {"VERIFY_SYNC_KEY": "short", "DISCORD_TOKEN": "dummy"}, clear=True):
            with self.assertRaises(verify_sync.VerificationSyncError):
                verify_sync.verification_auth_header()


class HigherBotRole:
    def __le__(self, other):
        return False


class RoleSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.db.execute("DELETE FROM verification_site_sync")
        bot.db.commit()

        self.verified_role = SimpleNamespace(id=bot.VERIFIED_ROLE_ID)
        self.unverified_role = SimpleNamespace(id=bot.UNVERIFIED_ROLE_ID)
        self.member = SimpleNamespace(
            id=SITE_MEMBER, bot=False, roles=[self.unverified_role],
        )

        async def add_role(role, **kwargs):
            self.member.roles.append(role)

        async def remove_role(role, **kwargs):
            self.member.roles = [r for r in self.member.roles if r.id != role.id]

        self.member.add_roles = AsyncMock(side_effect=add_role)
        self.member.remove_roles = AsyncMock(side_effect=remove_role)
        self.guild = SimpleNamespace(
            id=SITE_GUILD,
            me=SimpleNamespace(
                top_role=HigherBotRole(),
                guild_permissions=SimpleNamespace(manage_roles=True),
            ),
            get_role=lambda identifier: (
                self.verified_role if identifier == self.verified_role.id
                else self.unverified_role if identifier == self.unverified_role.id
                else None
            ),
            get_member=lambda identifier: self.member if identifier == SITE_MEMBER else None,
        )
        self.event = verify_sync.VerificationEvent(SITE_MEMBER, int(time.time() * 1000))

    async def test_grant_verified_remove_unverified_and_record_event(self):
        with patch.object(bot, "verified", side_effect=lambda m: self.verified_role in m.roles):
            result = await bot.apply_site_verification(self.guild, self.event)
        self.assertEqual(result, "roles_updated")
        self.member.add_roles.assert_awaited_once()
        self.member.remove_roles.assert_awaited_once()
        self.assertEqual([r.id for r in self.member.roles], [bot.VERIFIED_ROLE_ID])
        row = bot.db.execute(
            "SELECT verified_at FROM verification_site_sync WHERE member_id=?",
            (SITE_MEMBER,),
        ).fetchone()
        self.assertEqual(row["verified_at"], self.event.verified_at)

    async def test_dont_override_manual_role_removal_after_processing(self):
        with patch.object(bot, "verified", side_effect=lambda m: self.verified_role in m.roles):
            self.assertEqual(
                await bot.apply_site_verification(self.guild, self.event),
                "roles_updated",
            )
            self.member.roles = []
            self.assertEqual(
                await bot.apply_site_verification(self.guild, self.event),
                "processed",
            )
            self.member.add_roles.assert_awaited_once()

    async def test_fresh_website_verification_can_regrant_after_revocation(self):
        with patch.object(bot, "verified", side_effect=lambda m: self.verified_role in m.roles):
            await bot.apply_site_verification(self.guild, self.event)
            self.member.roles = [self.unverified_role]
            newer = verify_sync.VerificationEvent(SITE_MEMBER, self.event.verified_at + 1000)
            self.assertEqual(
                await bot.apply_site_verification(self.guild, newer),
                "roles_updated",
            )
            self.assertEqual(self.member.add_roles.await_count, 2)

    async def test_sync_fails_closed_without_role_permission(self):
        self.guild.me.guild_permissions.manage_roles = False
        with self.assertRaises(verify_sync.VerificationSyncError):
            await bot.apply_site_verification(self.guild, self.event)
        self.member.add_roles.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
