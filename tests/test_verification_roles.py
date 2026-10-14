"""Offline checks for Team lb's Verified / Unverified onboarding."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

os.environ.setdefault("DISCORD_TOKEN", "ci-test-token-not-a-secret")
os.environ.setdefault("DB_PATH", ":memory:")

import bot as team


VERIFIED_ID = 1559829257952174199
GUILD_ID = 1559829257952174192


def role(identifier, name):
    return SimpleNamespace(id=identifier, name=name, managed=False)


class FakeChannel:
    def __init__(self, identifier, name, guild=None):
        self.id = identifier
        self.name = name
        self.guild = guild
        self._permissions = {}
        self.set_permissions = AsyncMock(side_effect=self._set)

    async def _set(self, target, overwrite, reason=None):
        self._permissions[target.id] = overwrite

    def overwrites_for(self, target):
        return self._permissions.get(target.id, discord.PermissionOverwrite())


def fake_guild(verify=None):
    verified = role(VERIFIED_ID, "Verified")
    unverified = role(333333333333333333, "Unverified")
    everyone = role(GUILD_ID, "@everyone")
    bot_member = role(444444444444444444, "bot")
    guild = SimpleNamespace(
        id=GUILD_ID, roles=[verified, unverified],
        default_role=everyone, me=bot_member,
        channels=[], text_channels=[], chunked=True, members=[],
        get_role=lambda identifier: verified if identifier == VERIFIED_ID else None,
        create_text_channel=AsyncMock(),
    )
    if verify:
        verify.guild = guild
        guild.channels.append(verify)
        guild.text_channels.append(verify)
    return guild, verified, unverified, everyone, bot_member


class VerificationRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_unverified_without_recreating_verified(self):
        guild, verified, unverified, *_ = fake_guild()
        guild.roles = [verified]
        guild.create_role = AsyncMock(return_value=unverified)
        made = await team.ensure_unverified_role(guild)
        self.assertEqual(made.name, "Unverified")
        guild.create_role.assert_awaited_once()
        self.assertEqual(guild.create_role.await_args.kwargs["name"], "Unverified")
        self.assertEqual(team.MEMBER_ROLE_ID, VERIFIED_ID)

    async def test_existing_unverified_is_reused(self):
        guild, _, unverified, *_ = fake_guild()
        guild.create_role = AsyncMock()
        self.assertIs(await team.ensure_unverified_role(guild), unverified)
        guild.create_role.assert_not_awaited()

    async def test_existing_verify_channel_is_unverified_only(self):
        channel = FakeChannel(222222222222222222, "verify")
        guild, verified, unverified, everyone, bot_member = fake_guild(channel)
        result = await team.ensure_verification_channel(guild, unverified)
        self.assertIs(result, channel)
        self.assertIs(channel.overwrites_for(unverified).view_channel, True)
        self.assertIs(channel.overwrites_for(verified).view_channel, False)
        self.assertIs(channel.overwrites_for(everyone).view_channel, False)
        self.assertIs(channel.overwrites_for(bot_member).view_channel, True)
        self.assertIs(channel.overwrites_for(unverified).send_messages, False)
        guild.create_text_channel.assert_not_awaited()

    async def test_channel_fails_closed_if_overwrites_cannot_be_set(self):
        channel = FakeChannel(222222222222222222, "verify")
        guild, _, unverified, *_ = fake_guild(channel)
        channel.set_permissions = AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50013, "message": "Missing Permissions"},
        ))
        self.assertIsNone(await team.ensure_verification_channel(guild, unverified))

    async def test_verify_panel_exposes_official_url_only(self):
        embed, view = team.verification_panel(GUILD_ID)
        self.assertIn("Verify", embed.title)
        self.assertEqual(len(view.children), 1)
        self.assertEqual(
            view.children[0].url,
            "https://verify.lb000.xyz/?server=1559829257952174192",
        )

    async def test_other_channels_are_hidden_from_unverified(self):
        verify = FakeChannel(222222222222222222, "verify")
        guild, _, unverified, *_ = fake_guild(verify)
        general = FakeChannel(555555555555555555, "general", guild=guild)
        guild.channels.append(general)
        with patch.object(team, "ensure_unverified_role", new=AsyncMock(return_value=unverified)), \
             patch.object(team, "ensure_verification_channel", new=AsyncMock(return_value=verify)), \
             patch.object(team, "ensure_verification_panel", new=AsyncMock()):
            await team.setup_verification_access(guild)
        self.assertIs(general.overwrites_for(unverified).view_channel, False)
        self.assertIs(general.overwrites_for(unverified).send_messages, None)

    async def test_member_join_adds_unverified_to_new_member(self):
        guild, verified, unverified, *_ = fake_guild()
        new_member = SimpleNamespace(
            id=666666666666666666, guild=guild, bot=False, roles=[],
            add_roles=AsyncMock(), remove_roles=AsyncMock(),
        )
        with patch.object(team, "ensure_unverified_role", new=AsyncMock(return_value=unverified)), \
             patch.object(team, "verified", side_effect=lambda member: verified in member.roles):
            await team.on_member_join(new_member)
        new_member.add_roles.assert_awaited_once_with(
            unverified, reason="New Team lb member must verify",
        )
        new_member.remove_roles.assert_not_awaited()

    async def test_verified_member_has_unverified_removed(self):
        guild, verified, unverified, *_ = fake_guild()
        new_member = SimpleNamespace(
            id=666666666666666666, guild=guild, bot=False,
            roles=[verified, unverified], add_roles=AsyncMock(), remove_roles=AsyncMock(),
        )
        with patch.object(team, "verified", side_effect=lambda member: verified in member.roles):
            await team.reconcile_verified_role(new_member, unverified)
        new_member.remove_roles.assert_awaited_once_with(
            unverified, reason="Member completed Discord verification",
        )
        new_member.add_roles.assert_not_awaited()

    async def test_verified_role_removal_reinstates_unverified(self):
        guild, verified, unverified, *_ = fake_guild()
        previous = SimpleNamespace(id=666666666666666666, guild=guild, bot=False, roles=[verified])
        member = SimpleNamespace(
            id=666666666666666666, guild=guild, bot=False,
            roles=[], add_roles=AsyncMock(), remove_roles=AsyncMock(),
        )
        with patch.object(team, "verified", side_effect=lambda m: verified in m.roles):
            await team.on_member_update(previous, member)
        member.add_roles.assert_awaited_once_with(
            unverified, reason="New Team lb member must verify",
        )


if __name__ == "__main__":
    unittest.main()
