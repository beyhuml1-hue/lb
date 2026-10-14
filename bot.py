import os
import re
import json
import time
import random
import sqlite3
import asyncio
import logging
import io
from datetime import datetime, timezone

import discord
import aiohttp
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from cloudflare_ai import generate_reply, start_ai_client, close_ai_client, ai_configured
from verify_sync import (
    VerificationSyncError, fetch_site_verifications,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN or TOKEN.startswith("replace_"):
    raise SystemExit("Set your NEW Discord bot token as DISCORD_TOKEN in private host variables. Never commit or share it.")
GUILD_ID = int(os.getenv("GUILD_ID", "1559829257952174192"))
OWNER_ID = int(os.getenv("OWNER_ID", "1500253920918900883"))
VERIFIED_ROLE_ID = 1560003144036126853
UNVERIFIED_ROLE_ID = 1560003406687371355
VERIFY_CHANNEL_NAME = "verify"
STAFF_ROLE_ID = 1559829258325594149
VOUCH_COOLDOWN_SECONDS = 30 * 60
AI_RESPONSE_COOLDOWN_SECONDS = 2.0
ai_last_response = {}
ai_busy_channels = set()
verification_role_lock = asyncio.Lock()
verification_setup_task = None
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")
COLOR = discord.Color.from_rgb(255, 185, 40)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS giveaways (message_id INTEGER PRIMARY KEY, channel_id INTEGER, item TEXT, ends INTEGER, finished INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS entries (message_id INTEGER, user_id INTEGER, PRIMARY KEY(message_id,user_id));
CREATE TABLE IF NOT EXISTS cases (channel_id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT, summary TEXT, created INTEGER, status TEXT DEFAULT 'open', owner_notice INTEGER DEFAULT 0, report_target INTEGER, evidence TEXT DEFAULT '', human_requested INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS applications (channel_id INTEGER PRIMARY KEY, answers TEXT DEFAULT '{}', recommendation TEXT DEFAULT '', decision TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS vouches (user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS vouch_cooldowns (voter_id INTEGER PRIMARY KEY, last_vouch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS verification_panels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS verification_site_sync (member_id INTEGER PRIMARY KEY, verified_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit (at INTEGER, actor INTEGER, action TEXT, detail TEXT);
""")
# Upgrade existing Wispbyte databases in place without deleting open tickets.
case_columns = {column[1] for column in db.execute("PRAGMA table_info(cases)")}
for name, declaration in {
    "closed_at": "INTEGER DEFAULT 0",
    "closed_by": "INTEGER DEFAULT 0",
    "owner_notified": "INTEGER DEFAULT 0",
}.items():
    if name not in case_columns:
        db.execute(f"ALTER TABLE cases ADD COLUMN {name} {declaration}")
db.commit()

def now(): return int(time.time())
def log_action(actor, action, detail):
    db.execute("INSERT INTO audit VALUES (?,?,?,?)", (now(), actor, action, detail[:1800]))
    db.commit()

def panel(title, description):
    return discord.Embed(title=title, description=description, color=COLOR).set_footer(text="Donut SMP | Team lb")

def is_owner(user): return user.id == OWNER_ID
def verified(member): return isinstance(member, discord.Member) and any(r.id == VERIFIED_ROLE_ID for r in member.roles)
def staff_role(guild): return guild.get_role(STAFF_ROLE_ID)
def case_for(channel_id):
    return db.execute("SELECT * FROM cases WHERE channel_id=?", (channel_id,)).fetchone()

def vouch_total(member_id):
    row = db.execute("SELECT count FROM vouches WHERE user_id=?", (member_id,)).fetchone()
    return int(row["count"]) if row else 0


def add_member_vouch(voter_id, member_id, at=None):
    """Record one vouch per voter every 30 minutes, persisted across restarts."""
    if voter_id == member_id:
        return "self", vouch_total(member_id), 0
    timestamp = now() if at is None else int(at)
    with db:
        last = db.execute(
            "SELECT last_vouch FROM vouch_cooldowns WHERE voter_id=?", (voter_id,)
        ).fetchone()
        remaining = max(0, VOUCH_COOLDOWN_SECONDS - (timestamp - last["last_vouch"])) if last else 0
        if remaining > 0:
            return "cooldown", vouch_total(member_id), remaining
        db.execute(
            "INSERT INTO vouches(user_id,count) VALUES (?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET count=count+1",
            (member_id,),
        )
        db.execute(
            "INSERT INTO vouch_cooldowns(voter_id,last_vouch) VALUES (?,?) "
            "ON CONFLICT(voter_id) DO UPDATE SET last_vouch=excluded.last_vouch",
            (voter_id, timestamp),
        )
    log_action(voter_id, "vouch_added", str(member_id))
    return "added", vouch_total(member_id), 0


def set_member_vouches(actor_id, member_id, count):
    """An owner override changes the total, without resetting voters' cooldowns."""
    if not 0 <= count <= 100000:
        raise ValueError("Vouch count must be between 0 and 100,000.")
    with db:
        db.execute(
            "INSERT INTO vouches(user_id,count) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET count=excluded.count",
            (member_id, count),
        )
    log_action(actor_id, "vouch_count_set", f"{member_id}:{count}")
    return vouch_total(member_id)


async def send_completion_report(channel, case, closed_by):
    """Send the owner exactly one DM with a finished-case summary and transcript."""
    if case["owner_notified"]:
        return
    try:
        transcript_messages = []
        async for item in channel.history(limit=200, oldest_first=False):
            transcript_messages.append(item)
        transcript_messages.reverse()
        rows = []
        for item in transcript_messages:
            timestamp = item.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            body = item.clean_content or "[no text]"
            attachments = " ".join(attachment.url for attachment in item.attachments)
            if attachments:
                body += "\nAttachments: " + attachments
            rows.append(f"[{timestamp}] {item.author} ({item.author.id}): {body[:3500]}")
        transcript = (
            f"Donut SMP | Team lb — {case['kind']} ticket\n"
            f"Channel: {channel.id}\n"
            f"Opened by: {case['user_id']}\n"
            f"Closed by: {closed_by.id}\n"
            f"Original request: {case['summary']}\n\n"
            + "\n\n".join(rows)
        )[-170000:]
        decision_row = db.execute(
            "SELECT decision FROM applications WHERE channel_id=?", (channel.id,)
        ).fetchone()
        decision = decision_row["decision"] if decision_row and decision_row["decision"] else "No owner decision recorded"
        opening = discord.utils.escape_mentions(case["summary"][:280])
        status = "Human requested" if case["human_requested"] else "Handled in ticket"
        message = (
            f"✅ **{case['kind'].title()} concluded**\n"
            f"Member: <@{case['user_id']}>\n"
            f"Channel: {channel.mention}\n"
            f"Closed by: {discord.utils.escape_markdown(str(closed_by))}\n"
            f"Outcome: {discord.utils.escape_markdown(decision)}\n"
            f"Support: {status}\n"
            f"Original request: {opening}\n\n"
            "**Full ticket transcript attached.**"
        )
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(
            content=message[:1900],
            file=discord.File(
                io.BytesIO(transcript.encode("utf-8")),
                filename=f"team-lb-{case['kind']}-{channel.id}.txt",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        db.execute(
            "UPDATE cases SET owner_notified=1 WHERE channel_id=? AND owner_notified=0",
            (channel.id,),
        )
        db.commit()
    except (discord.HTTPException, discord.Forbidden):
        logging.exception("Final ticket report could not be DMed to the owner")
    except Exception:
        logging.exception("Failed to create the final ticket report")


async def ai_reply(question, context=""):
    return await generate_reply(question, context)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
class TeamLBBot(commands.Bot):
    async def setup_hook(self):
        await super().setup_hook()
        self.verify_sync_http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12, connect=4),
            connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300),
        )
        await start_ai_client()

    async def close(self):
        try:
            await close_ai_client()
            session = getattr(self, "verify_sync_http", None)
            if session is not None and not session.closed:
                await session.close()
        finally:
            await super().close()


bot = TeamLBBot(command_prefix="!", intents=intents)

class GiveawayView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="🎉 Enter giveaway", style=discord.ButtonStyle.success, custom_id="lb:giveaway:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not verified(interaction.user):
            return await interaction.response.send_message("You need the Verified role to enter.", ephemeral=True)
        row = db.execute("SELECT * FROM giveaways WHERE message_id=?", (interaction.message.id,)).fetchone()
        if not row or row["finished"] or row["ends"] <= now():
            return await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
        cur = db.execute("INSERT OR IGNORE INTO entries VALUES (?,?)", (interaction.message.id, interaction.user.id))
        db.commit()
        await interaction.response.send_message("🎉 You're entered!" if cur.rowcount else "You're already entered.", ephemeral=True)

class OpenTicketModal(discord.ui.Modal, title="Open a support ticket"):
    subject = discord.ui.TextInput(label="What is this ticket about?", max_length=300, style=discord.TextStyle.paragraph)
    async def on_submit(self, interaction):
        await create_case(interaction, "ticket", str(self.subject))

class MiddlemanModal(discord.ui.Modal, title="Middleman request"):
    other = discord.ui.TextInput(label="Other trader's Discord user ID or @name", max_length=100)
    giving = discord.ui.TextInput(label="What are you giving?", max_length=350)
    receiving = discord.ui.TextInput(label="What are you receiving?", max_length=350)
    async def on_submit(self, interaction):
        await create_case(interaction, "middleman", f"Other trader: {self.other}\nGiving: {self.giving}\nReceiving: {self.receiving}")

class StashModal(discord.ui.Modal, title="Stash application"):
    why = discord.ui.TextInput(label="Why do you want to join?", style=discord.TextStyle.paragraph, max_length=600)
    money = discord.ui.TextInput(label="How much in-game money do you have?", max_length=100)
    async def on_submit(self, interaction):
        await create_case(interaction, "stash", f"Why: {self.why}\nMoney: {self.money}")

class StaffModal(discord.ui.Modal, title="Staff application"):
    age = discord.ui.TextInput(label="Experience moderating servers?", style=discord.TextStyle.paragraph, max_length=400)
    availability = discord.ui.TextInput(label="Timezone and weekly availability?", max_length=200)
    scenario = discord.ui.TextInput(label="How would you handle a cheating report?", style=discord.TextStyle.paragraph, max_length=600)
    motivation = discord.ui.TextInput(label="Why should we pick you?", style=discord.TextStyle.paragraph, max_length=500)
    async def on_submit(self, interaction):
        await create_case(interaction, "staff", f"Experience: {self.age}\nAvailability: {self.availability}\nScenario: {self.scenario}\nMotivation: {self.motivation}")

class PanelView(discord.ui.View):
    def __init__(self, kind):
        super().__init__(timeout=None)
        self.kind = kind
        labels = {"ticket":"🎫 Open ticket","middleman":"🤝 Request middleman","staff":"📝 Apply for staff","stash":"🏠 Apply for stash"}
        button = discord.ui.Button(label=labels[kind], style=discord.ButtonStyle.primary, custom_id=f"lb:open:{kind}")
        button.callback = self.open
        self.add_item(button)
    async def open(self, interaction):
        modal = {"ticket":OpenTicketModal,"middleman":MiddlemanModal,"staff":StaffModal,"stash":StashModal}[self.kind]()
        await interaction.response.send_modal(modal)

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="🙋 Request human", style=discord.ButtonStyle.primary, custom_id="lb:case:human")
    async def human(self, interaction, button):
        row = case_for(interaction.channel_id)
        if not row or row["status"] != "open": return await interaction.response.send_message("Ticket is closed.", ephemeral=True)
        if interaction.user.id != row["user_id"] and not is_owner(interaction.user):
            return await interaction.response.send_message("Not your ticket.", ephemeral=True)
        if row["human_requested"]:
            return await interaction.response.send_message(
                "Staff have already been requested for this ticket.", ephemeral=True
            )
        db.execute("UPDATE cases SET human_requested=1 WHERE channel_id=?", (interaction.channel_id,))
        db.commit()
        await interaction.response.send_message(
            "A human has been requested. AI replies are paused in this ticket."
        )
        role = staff_role(interaction.guild)
        if role:
            await interaction.channel.send(
                f"{role.mention} Human assistance requested.",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
    @discord.ui.button(label="🔒 Close", style=discord.ButtonStyle.danger, custom_id="lb:case:close")
    async def close(self, interaction, button):
        row = case_for(interaction.channel_id)
        if not row:
            return await interaction.response.send_message("Ticket not found.", ephemeral=True)
        if row["status"] != "open":
            return await interaction.response.send_message("Ticket already closed.", ephemeral=True)
        if (interaction.user.id != row["user_id"] and not is_owner(interaction.user)
                and not interaction.user.guild_permissions.manage_channels):
            return await interaction.response.send_message("Not allowed.", ephemeral=True)
        # Compare-and-set prevents a double click from producing two owner DMs.
        changed = db.execute(
            "UPDATE cases SET status='closed', closed_at=?, closed_by=? "
            "WHERE channel_id=? AND status='open'",
            (now(), interaction.user.id, interaction.channel_id),
        )
        db.commit()
        if not changed.rowcount:
            return await interaction.response.send_message(
                "This ticket is already closed.", ephemeral=True
            )
        log_action(interaction.user.id, "close", str(interaction.channel_id))
        await interaction.response.send_message(
            "Ticket concluded. The owner receives one final report with the transcript."
        )
        member = interaction.guild.get_member(row["user_id"])
        if member:
            overwrite = interaction.channel.overwrites_for(member)
            overwrite.send_messages = False
            try:
                await interaction.channel.set_permissions(member, overwrite=overwrite)
            except discord.Forbidden:
                logging.warning("Could not make concluded ticket read-only")
        await send_completion_report(interaction.channel, row, interaction.user)

class OwnerDecisionView(discord.ui.View):
    def __init__(self, channel_id, kind):
        super().__init__(timeout=None)
        self.channel_id, self.kind = channel_id, kind
        choices = ["Accept","Decline"] if kind in ("staff","stash") else ["Punish review","Second chance","Warning"]
        for choice in choices:
            b = discord.ui.Button(label=choice, custom_id=f"lb:decision:{kind}:{channel_id}:{choice.lower().replace(' ','_')}", style=discord.ButtonStyle.secondary)
            async def handler(interaction, decision=choice): await self.decide(interaction, decision)
            b.callback = handler
            self.add_item(b)
    async def decide(self, interaction, decision):
        if not is_owner(interaction.user): return await interaction.response.send_message("Owner only.", ephemeral=True)
        row = case_for(self.channel_id)
        if not row or row["status"] != "open":
            return await interaction.response.send_message("Ticket is already closed.", ephemeral=True)
        db.execute(
            "UPDATE applications SET decision=? WHERE channel_id=?",
            (decision, self.channel_id),
        )
        db.commit()
        log_action(interaction.user.id, "owner_decision", f"{self.channel_id}: {decision}")
        await interaction.response.send_message(f"Recorded: {decision}.")
        channel = bot.get_channel(self.channel_id)
        if channel:
            await channel.send(f"**Owner decision:** {discord.utils.escape_markdown(decision)}")
            if decision == "Warning": await channel.send("⚠️ This is an official warning. Please follow the server rules.")
            if decision == "Punish review": await channel.send("This report is approved for moderator review. Any actual ban/kick requires a separate authorized action.")

async def create_case(interaction, kind, summary):
    guild = interaction.guild
    if not guild: return await interaction.response.send_message("Use this in the server.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    existing = db.execute("SELECT channel_id FROM cases WHERE user_id=? AND kind=? AND status='open'", (interaction.user.id,kind)).fetchone()
    if existing:
        channel = guild.get_channel(existing["channel_id"])
        if channel: return await interaction.followup.send(f"You already have a {kind} case: {channel.mention}", ephemeral=True)
    role = staff_role(guild)
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                  interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                  guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)}
    if role:
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
    owner_member = guild.get_member(OWNER_ID)
    if not owner_member:
        try:
            owner_member = await guild.fetch_member(OWNER_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logging.warning("Owner not found in server; no owner-specific ticket overwrite")
    if owner_member:
        overwrites[owner_member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
    category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    try:
        channel = await guild.create_text_channel(f"{kind}-{interaction.user.name}"[:95], category=category if isinstance(category, discord.CategoryChannel) else None, overwrites=overwrites, reason="Team lb case")
    except discord.Forbidden: return await interaction.followup.send("Bot needs Manage Channels and permission overrides.", ephemeral=True)
    db.execute("INSERT INTO cases(channel_id,user_id,kind,summary,created) VALUES (?,?,?,?,?)", (channel.id, interaction.user.id,kind,summary,now()))
    db.execute("INSERT OR IGNORE INTO applications(channel_id,answers) VALUES (?,?)", (channel.id,summary))
    db.commit()
    desc = {"ticket":"Describe the issue; attach screenshots/video for reports. AI can help while staff are away.",
            "middleman":"A middleman supervises both sides of a trade. Do not hand over items until both traders confirm the terms and a human middleman is present. AI does not hold items.",
            "staff":"Your application has been recorded. AI can answer questions; owner makes final role decisions.",
            "stash":"Your application has been recorded. In-game funds/vouches aren't independently verified; owner decides access."}[kind]
    await channel.send(content=f"{interaction.user.mention} <@&{STAFF_ROLE_ID}>" if role else interaction.user.mention,
                       embed=panel(f"{kind.title()} • {interaction.user.display_name}", f"{desc}\n\n**Submitted:**\n{discord.utils.escape_mentions(summary)[:1300]}"),
                       view=TicketView(), allowed_mentions=discord.AllowedMentions(users=True, roles=bool(role)))
    # Keep owner review controls in the ticket. The owner receives no DM
    # until someone closes the ticket and its final transcript is ready.
    await channel.send(
        "🔐 **Owner review controls** (only the owner can use these buttons):",
        view=OwnerDecisionView(channel.id,kind),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await interaction.followup.send(f"Created {channel.mention}", ephemeral=True)
    log_action(interaction.user.id, "open_case", f"{kind}:{channel.id}")

@bot.tree.command(name="giveaway",description="Post a verified-member-only giveaway")
@app_commands.describe(item="Prize", time="Duration such as 30m, 2h, 1d")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway(interaction:discord.Interaction,item:str,time:str):
    match = re.fullmatch(r"(\d+)([mhd])",time.lower().strip())
    if not match: return await interaction.response.send_message("Use 30m, 2h or 1d.", ephemeral=True)
    amount, unit = int(match[1]), match[2]
    seconds = amount * {"m":60,"h":3600,"d":86400}[unit]
    if not 60 <= seconds <= 30*86400: return await interaction.response.send_message("Duration must be 1 minute to 30 days.",ephemeral=True)
    ends = now()+seconds
    await interaction.response.send_message("Giveaway created.",ephemeral=True)
    msg = await interaction.channel.send(embed=panel("🎉 Team lb Giveaway",f"**Prize:** {discord.utils.escape_mentions(item[:300])}\n**Ends:** <t:{ends}:R>\nYou must have the Verified role to enter."),view=GiveawayView())
    db.execute("INSERT INTO giveaways VALUES (?,?,?,?,0)",(msg.id,msg.channel.id,item[:300],ends))
    db.commit()

async def send_panel(interaction,kind):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("Manage Server permission required.",ephemeral=True)
    descriptions = {
        "ticket":"Open a private support ticket. Explain your problem, report misconduct with evidence, or ask for a human.",
        "middleman":"Our middleman service protects trades by recording both sides' offers. Human confirmation is required before transferring anything.",
        "staff":"Apply to help moderate Donut SMP | Team lb. Answer a short form; your application goes to the owner.",
        "stash":"Apply to join the Team lb stash/base. Explain your motivation and in-game funds. Owner reviews access."
    }
    await interaction.channel.send(embed=panel(f"🍩 {kind.title()} | Team lb",descriptions[kind]),view=PanelView(kind))
    await interaction.response.send_message("Panel posted.",ephemeral=True)

def verification_panel(guild_id):
    """Use the existing Discord OAuth verification site; never collect credentials."""
    url = f"https://verify.lb000.xyz/?server={int(guild_id)}"
    embed = discord.Embed(
        title="✅ Verify your Discord account",
        description=(
            "Welcome to **Donut SMP | Team lb**!\n\n"
            "Click **Verify Now** below to open our Discord verification website.\n"
            "1. Sign in through Discord.\n"
            "2. Review the server and permissions requested.\n"
            "3. Authorize only what you are comfortable with.\n\n"
            "Never share your Discord password or bot token."
        ),
        color=discord.Color.from_rgb(149, 119, 251),
    )
    embed.set_footer(text="Official verification • verify.lb000.xyz")
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="✅ Verify Now",
        style=discord.ButtonStyle.link,
        url=url,
    ))
    return embed, view



def find_unverified_role(guild):
    """Always use Team lb's existing Unverified role by ID, not its name."""
    role = guild.get_role(UNVERIFIED_ROLE_ID)
    return role if role is not None and not role.managed else None


def find_verification_channel(guild):
    """Reuse the existing verification text channel instead of making duplicates."""
    for name in (VERIFY_CHANNEL_NAME, "verification", "verify-here"):
        channel = discord.utils.get(guild.text_channels, name=name)
        if channel is not None:
            return channel
    return None


async def ensure_unverified_role(guild):
    """Use the existing Unverified role, never create a duplicate."""
    role = find_unverified_role(guild)
    if role is None:
        logging.error(
            "Unverified role %s is missing from Team lb; restore it or "
            "update UNVERIFIED_ROLE_ID before starting verification.",
            UNVERIFIED_ROLE_ID,
        )
    return role

async def set_verify_visibility(channel, target, allowed, read_only=False, bot_access=False):
    """Change only the relevant role overwrite, preserving unrelated permissions."""
    overwrite = channel.overwrites_for(target)
    desired = {"view_channel": allowed}
    if read_only:
        desired.update(
            send_messages=False, add_reactions=False, read_message_history=True
        )
    if bot_access:
        desired.update(
            send_messages=True, read_message_history=True, embed_links=True
        )
    if all(getattr(overwrite, key) == value for key, value in desired.items()):
        return False
    for key, value in desired.items():
        setattr(overwrite, key, value)
    try:
        await channel.set_permissions(
            target,
            overwrite=overwrite,
            reason="Keep Team lb verification visible to Unverified members only",
        )
        return True
    except discord.Forbidden:
        logging.error(
            "Cannot adjust verification visibility for #%s: grant Manage Channels",
            channel.name,
        )
    except discord.HTTPException:
        logging.exception("Unable to update permissions for channel %s", channel.id)
    return False


async def ensure_verification_channel(guild, unverified):
    verified_role = guild.get_role(VERIFIED_ROLE_ID)
    if verified_role is None:
        logging.error(
            "Team lb Verified role %s does not exist. "
            "Create/fix the original role before setting up verification.",
            VERIFIED_ROLE_ID,
        )
        return None
    channel = find_verification_channel(guild)
    if channel is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False, send_messages=False
            ),
            unverified: discord.PermissionOverwrite(
                view_channel=True, send_messages=False,
                add_reactions=False, read_message_history=True,
            ),
            verified_role: discord.PermissionOverwrite(view_channel=False),
        }
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, embed_links=True,
            )
        try:
            channel = await guild.create_text_channel(
                VERIFY_CHANNEL_NAME,
                topic="Verify with Discord to unlock Team lb. Only Unverified members can see this channel.",
                overwrites=overwrites,
                reason="Set up private Unverified-only verification channel",
            )
            logging.info("Created Unverified-only verification channel %s", channel.id)
        except discord.Forbidden:
            logging.error("Cannot create #verify: the Wispbyte bot needs Manage Channels")
            return None
        except discord.HTTPException:
            logging.exception("Discord could not create #verify")
            return None
    else:
        await set_verify_visibility(channel, guild.default_role, False)
        await set_verify_visibility(channel, unverified, True, read_only=True)
        await set_verify_visibility(channel, verified_role, False)
        if guild.me:
            await set_verify_visibility(channel, guild.me, True, bot_access=True)

    # Fail closed if the bot cannot actually restrict an existing channel.
    # Otherwise everyone might see the Verify Now button.
    required = (
        (guild.default_role, False),
        (unverified, True),
        (verified_role, False),
    )
    if any(
        channel.overwrites_for(target).view_channel is not expected
        for target, expected in required
    ):
        logging.error(
            "Verification channel #%s is not private. "
            "Grant Manage Channels and retry instead of publishing an open panel.",
            channel.name,
        )
        return None
    bot._verify_channel_id = channel.id
    return channel


async def ensure_verification_panel(channel):
    """Post the link button once, and remember it across bot restarts."""
    saved = db.execute(
        "SELECT channel_id,message_id FROM verification_panels WHERE guild_id=?",
        (channel.guild.id,),
    ).fetchone()
    if saved and saved["channel_id"] == channel.id:
        try:
            old = await channel.fetch_message(saved["message_id"])
            if old.author.id == bot.user.id:
                return old
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    try:
        async for existing in channel.history(limit=75):
            if existing.author.id == bot.user.id and any(
                embed.title == "✅ Verify your Discord account"
                for embed in existing.embeds
            ):
                with db:
                    db.execute(
                        "INSERT OR REPLACE INTO verification_panels VALUES (?,?,?)",
                        (channel.guild.id, channel.id, existing.id),
                    )
                return existing
    except (discord.Forbidden, discord.HTTPException):
        logging.warning("Cannot read #%s history to find the verification panel", channel.name)

    embed, view = verification_panel(channel.guild.id)
    try:
        posted = await channel.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        with db:
            db.execute(
                "INSERT OR REPLACE INTO verification_panels VALUES (?,?,?)",
                (channel.guild.id, channel.id, posted.id),
            )
        return posted
    except (discord.Forbidden, discord.HTTPException):
        logging.exception("Could not post the Verify Now panel in #%s", channel.name)
    return None


async def reconcile_verified_role(member, unverified=None):
    """Give Unverified to people without Verified; remove it once verified."""
    if member.bot or member.guild.id != GUILD_ID:
        return False
    unverified = unverified or find_unverified_role(member.guild)
    if not unverified:
        return False
    has_unverified = any(role.id == unverified.id for role in member.roles)
    try:
        if verified(member) and has_unverified:
            await member.remove_roles(
                unverified, reason="Member completed Discord verification"
            )
            return True
        # Do not lock the owner out for testing, but still remove Unverified
        # from the owner when the Verified role is successfully assigned.
        if member.id == OWNER_ID:
            return False
        if not verified(member) and not has_unverified:
            await member.add_roles(
                unverified, reason="New Team lb member must verify"
            )
            return True
    except discord.Forbidden:
        logging.error(
            "Cannot manage Unverified role for %s; Wispbyte bot requires "
            "Manage Roles and a higher bot role",
            member.id,
        )
    except discord.HTTPException:
        logging.exception("Could not update Unverified role for member %s", member.id)
    return False


async def setup_verification_access(guild):
    """Provision onboarding once; never delete channels or alter Verified role."""
    if guild.id != GUILD_ID:
        return
    unverified = await ensure_unverified_role(guild)
    if unverified is None:
        return
    channel = await ensure_verification_channel(guild, unverified)
    if channel is None:
        return

    # Unverified users should see #verify, not any other existing channel.
    # Set the Unverified overwrite on categories and channels, preserving all
    # permissions of other roles. New channels are handled by the create event.
    updated = 0
    for other in guild.channels:
        if other.id != channel.id:
            if await set_verify_visibility(other, unverified, False):
                updated += 1

    await ensure_verification_panel(channel)
    try:
        if not guild.chunked:
            await guild.chunk(cache=True)
    except (discord.HTTPException, asyncio.TimeoutError):
        logging.warning("Could not fetch every member; new joins still receive Unverified")

    assigned = 0
    for member in list(guild.members):
        if await reconcile_verified_role(member, unverified):
            assigned += 1
    logging.info(
        "Verification ready in #%s: protected %s channels, reconciled %s members",
        channel.name, updated, assigned,
    )


async def apply_site_verification(guild, event):
    """Apply a fresh website OAuth event once; never reinstate manually revoked roles."""
    record = db.execute(
        "SELECT verified_at FROM verification_site_sync WHERE member_id=?",
        (event.member_id,),
    ).fetchone()
    if record and record["verified_at"] >= event.verified_at:
        return "processed"

    verified_role = guild.get_role(VERIFIED_ROLE_ID)
    unverified_role = guild.get_role(UNVERIFIED_ROLE_ID)
    if verified_role is None or unverified_role is None:
        raise VerificationSyncError(
            "Team lb Verified / Unverified roles were not found by ID."
        )
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles or (
        me.top_role <= verified_role or me.top_role <= unverified_role
    ):
        raise VerificationSyncError(
            "Wispbyte bot needs Manage Roles and its role above "
            "Verified and Unverified to synchronize the website."
        )

    member = guild.get_member(event.member_id)
    if member is None:
        try:
            member = await guild.fetch_member(event.member_id)
        except discord.NotFound:
            return "not_in_server"
        except (discord.Forbidden, discord.HTTPException):
            logging.warning(
                "Cannot fetch member %s for verification website sync",
                event.member_id,
            )
            return "retry"
    if member.bot:
        return "not_member"

    try:
        changed = False
        if not verified(member):
            await member.add_roles(
                verified_role, reason="Verified with verify.lb000.xyz Discord OAuth"
            )
            changed = True
        if any(role.id == unverified_role.id for role in member.roles):
            await member.remove_roles(
                unverified_role, reason="Verified with verify.lb000.xyz Discord OAuth"
            )
            changed = True
    except discord.Forbidden:
        logging.error(
            "Discord denied verification roles for member %s. "
            "Check the Wispbyte bot's Manage Roles permission and role order.",
            member.id,
        )
        return "retry"
    except discord.HTTPException:
        logging.exception("Discord role update failed for member %s", member.id)
        return "retry"

    with db:
        db.execute(
            "INSERT INTO verification_site_sync(member_id,verified_at) VALUES (?,?) "
            "ON CONFLICT(member_id) DO UPDATE SET verified_at=excluded.verified_at",
            (member.id, event.verified_at),
        )
    if changed:
        logging.info(
            "Website verification synchronized for Discord member %s", member.id
        )
    return "roles_updated" if changed else "already_verified"


async def sync_website_verifications(guild):
    """One authenticated website sync pass, with bounded Discord API operations."""
    session = getattr(bot, "verify_sync_http", None)
    if session is None or session.closed:
        raise VerificationSyncError("Verification HTTP connection is not ready.")
    events = await fetch_site_verifications(session, guild.id)
    counts = {
        "website_records": len(events),
        "roles_updated": 0,
        "already_verified": 0,
        "processed": 0,
        "not_in_server": 0,
        "retry": 0,
    }
    attempted = 0
    for event in events:
        processed = db.execute(
            "SELECT verified_at FROM verification_site_sync WHERE member_id=?",
            (event.member_id,),
        ).fetchone()
        if processed and processed["verified_at"] >= event.verified_at:
            counts["processed"] += 1
            continue
        if attempted >= 25:
            break
        attempted += 1
        outcome = await apply_site_verification(guild, event)
        if outcome in counts:
            counts[outcome] += 1
    bot._last_verify_sync = counts
    bot._last_verify_sync_error = None
    return counts


@tasks.loop(seconds=60)
async def website_verification_loop():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    try:
        report = await sync_website_verifications(guild)
        if report["roles_updated"]:
            logging.info(
                "Website sync applied roles for %d verified Team lb members",
                report["roles_updated"],
            )
    except VerificationSyncError as error:
        bot._last_verify_sync_error = str(error)
        logging.warning("Verification website sync: %s", error)
    except Exception:
        bot._last_verify_sync_error = "Unexpected website synchronization failure"
        logging.exception("Verification website sync failed")


@website_verification_loop.before_loop
async def before_website_verification_loop():
    await bot.wait_until_ready()


@bot.tree.command(
    name="verifycheck",
    description="Owner-only diagnosis of Wispbyte verification roles and permissions",
)
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def verifycheck(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message(
            "Owner only.", ephemeral=True
        )
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message(
            "Use this in Team lb.", ephemeral=True
        )
    await interaction.response.defer(ephemeral=True, thinking=True)
    me = guild.me or guild.get_member(bot.user.id)
    verified_role = guild.get_role(VERIFIED_ROLE_ID)
    unverified_role = guild.get_role(UNVERIFIED_ROLE_ID)
    channel = find_verification_channel(guild)
    lines = [
        f"**Team lb verification check** — Guild: `{guild.id}`",
        f"Verified role: {'✅ Found' if verified_role else '❌ Missing'} (`{VERIFIED_ROLE_ID}`)",
        f"Unverified role: {'✅ Found' if unverified_role else '❌ Missing'} (`{UNVERIFIED_ROLE_ID}`)",
    ]
    if me:
        perms = me.guild_permissions
        lines.append(f"Wispbyte bot Manage Roles: {'✅' if perms.manage_roles else '❌'}")
        lines.append(f"Wispbyte bot Manage Channels: {'✅' if perms.manage_channels else '❌'}")
        for name, role in (("Verified", verified_role), ("Unverified", unverified_role)):
            if role:
                lines.append(
                    f"Wispbyte bot above {name}: "
                    f"{'✅' if me.top_role > role else '❌'}"
                )
    else:
        lines.append("❌ Cannot inspect Wispbyte bot's guild member.")
    if channel is None:
        lines.append("❌ No #verify text channel found.")
    else:
        lines.append(f"Verification channel: {channel.mention}")
        targets = (
            ("@everyone hidden", guild.default_role, False),
            ("Unverified allowed", unverified_role, True),
            ("Verified hidden", verified_role, False),
        )
        for title, role, expected in targets:
            if role:
                allowed = channel.overwrites_for(role).view_channel is expected
                lines.append(f"{title}: {'✅' if allowed else '❌'}")
    # Prove the website is reachable and actually apply any pending role changes.
    try:
        report = await sync_website_verifications(guild)
        lines.append(
            f"Website connection: ✅ Connected to verify.lb000.xyz"
            f" — {report['website_records']} site records,"
            f" {report['roles_updated']} member role updates this pass."
        )
    except VerificationSyncError as error:
        lines.append(f"Website connection: ❌ {error}")
    except Exception:
        logging.exception("Owner verification connection check failed")
        lines.append("Website connection: ❌ Check Wispbyte logs for sync failure.")
    lines.append(
        "The website uses a separate Discord application. "
        "Wispbyte can now grant roles from OAuth records even if that bot "
        "cannot manage roles, once private website sync access is configured."
    )
    await interaction.followup.send(
        "\n".join(lines)[:1950], ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="verify", description="Open the official Discord verification panel")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def verify_command(interaction: discord.Interaction):
    if not interaction.guild_id:
        return await interaction.response.send_message(
            "Use this command in your Team lb server.", ephemeral=True
        )
    if interaction.user.guild_permissions.manage_guild:
        channel = find_verification_channel(interaction.guild)
        if channel is None:
            return await interaction.response.send_message(
                "The verification channel isn't configured yet. "
                "The bot needs Manage Roles and Manage Channels; restart it.",
                ephemeral=True,
            )
        posted = await ensure_verification_panel(channel)
        await interaction.response.send_message(
            f"Verification is in {channel.mention}."
            if posted else "Couldn't post the verification panel; check bot permissions.",
            ephemeral=True,
        )
    elif verified(interaction.user):
        await interaction.response.send_message(
            "✅ You are already verified and the verification channel is hidden from you.",
            ephemeral=True,
        )
    else:
        embed, view = verification_panel(interaction.guild_id)
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True,
        )


@bot.tree.command(name="ticket",description="Create the support ticket panel")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def ticket(interaction:discord.Interaction): await send_panel(interaction,"ticket")

@bot.tree.command(name="middleman",description="Create the middleman request panel")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def middleman(interaction:discord.Interaction): await send_panel(interaction,"middleman")

@bot.tree.command(name="apply",description="Create an application panel")
@app_commands.describe(type="staff or stash")
@app_commands.choices(type=[app_commands.Choice(name="staff",value="staff"),app_commands.Choice(name="stash",value="stash")])
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def apply(interaction:discord.Interaction,type:app_commands.Choice[str]): await send_panel(interaction,type.value)

@bot.tree.command(name="aitest", description="Check your Cloudflare support AI connection")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def aitest(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    if not ai_configured():
        return await interaction.response.send_message(
            "Cloudflare AI is waiting for a private CLOUDFLARE_API_TOKEN in "
            "Wispbyte Startup variables or .env. The Account ID is configured. "
            "Restart the bot after adding your token.",
            ephemeral=True,
        )
    await interaction.response.defer(ephemeral=True, thinking=True)
    answer = await generate_reply(
        "Say hello and confirm in one sentence that Discord ticket AI is available."
    )
    await interaction.followup.send(
        "Cloudflare AI replied:\n" + discord.utils.escape_mentions(answer)[:1700],
        ephemeral=True,
    )


@bot.tree.command(name="vouch",description="Set an applicant's manually reviewed vouch count")
@app_commands.describe(member="Member to update",count="Reviewed vouches")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def vouch(interaction:discord.Interaction,member:discord.Member,count:app_commands.Range[int,0,100000]):
    if not is_owner(interaction.user): return await interaction.response.send_message("Owner only.",ephemeral=True)
    total = set_member_vouches(interaction.user.id, member.id, count)
    await interaction.response.send_message(
        f"Set {member.mention}'s vouches to **{total}**.", ephemeral=True
    )

@bot.event
async def on_message(message):
    if message.author.bot: return
    if isinstance(message.channel,discord.DMChannel):
        if message.author.id != OWNER_ID: return
        if message.content.lower().startswith("!status"):
            active = db.execute("SELECT COUNT(*) n FROM cases WHERE status='open'").fetchone()["n"]
            return await message.channel.send(f"Team lb online. {active} open cases. Use server slash commands to post panels.")
        return await message.channel.send("Owner DM received. For safety I don't execute unrestricted natural-language commands. Use Discord moderation tools and the owner decision buttons.")
    if message.guild and message.content.strip().lower() == "!verify":
        if message.author.guild_permissions.manage_guild:
            channel = find_verification_channel(message.guild)
            if channel is None:
                await message.reply(
                    "The verification channel isn't ready. Grant Manage Roles "
                    "and Manage Channels to the bot, then restart.",
                    mention_author=False,
                )
            else:
                posted = await ensure_verification_panel(channel)
                await message.reply(
                    f"Verification panel is in {channel.mention}."
                    if posted else "Could not post the verification panel.",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        else:
            await message.reply(
                "Use **/verify** in the verification channel.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return
    if not message.guild:
        return
    say = re.fullmatch(r"!say(?:\s+([\s\S]*))?", message.content, flags=re.IGNORECASE)
    if say:
        if not (is_owner(message.author) or message.author.guild_permissions.manage_guild):
            return await message.reply(
                "Only the owner or server managers can use !say.", mention_author=False
            )
        content = (say.group(1) or "").strip()
        if not content:
            return await message.reply(
                "Write a message after !say.", mention_author=False
            )
        if len(content) > 3800:
            return await message.reply(
                "Keep embed messages under 3,800 characters.", mention_author=False
            )
        embed = discord.Embed(
            description=content,
            color=discord.Color.from_rgb(149, 119, 251),
        )
        embed.set_footer(text="Donut SMP | Team lb")
        await message.channel.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )
        return
    edit_count = re.fullmatch(
        r"!vouchcount\s+(?:<@!?([0-9]{17,22})>|([0-9]{17,22}))\s+(\d{1,6})\s*",
        message.content, flags=re.IGNORECASE
    )
    if message.content.strip().lower().startswith("!vouchcount"):
        if not is_owner(message.author):
            return await message.reply(
                "Only the owner can edit vouch counts.", mention_author=False
            )
        if not edit_count:
            return await message.reply(
                "Usage: !vouchcount @user 5 (you can also use a Discord user ID).",
                mention_author=False,
            )
        member_id = int(edit_count.group(1) or edit_count.group(2))
        count = int(edit_count.group(3))
        if count > 100000:
            return await message.reply("Maximum vouch count is 100,000.", mention_author=False)
        total = set_member_vouches(message.author.id, member_id, count)
        return await message.channel.send(
            f"✅ Set <@{member_id}>'s vouches to **{total}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    vouch = re.fullmatch(
        r"vouch\s+<@!?([0-9]{17,22})>\s*", message.content,
        flags=re.IGNORECASE,
    )
    if vouch:
        recipient_id = int(vouch.group(1))
        if recipient_id == message.author.id:
            return await message.reply(
                "You can't vouch for yourself.", mention_author=False
            )
        recipient = message.guild.get_member(recipient_id)
        if recipient is None:
            try:
                recipient = await message.guild.fetch_member(recipient_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return await message.reply(
                    "That person isn't a member of this server.", mention_author=False
                )
        if recipient.bot:
            return await message.reply(
                "You can't vouch for a bot.", mention_author=False
            )
        outcome, count, remaining = add_member_vouch(
            message.author.id, recipient_id
        )
        if outcome == "cooldown":
            minutes = (remaining + 59) // 60
            return await message.reply(
                f"You can give another vouch in **{minutes} minute(s)**.",
                mention_author=False,
            )
        return await message.channel.send(
            f"✅ <@{recipient_id}> received a vouch! They now have "
            f"**{count} vouch{'es' if count != 1 else ''}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    row = case_for(message.channel.id)
    if not row or row["status"] != "open": return
    if message.author.id == OWNER_ID or (isinstance(message.author,discord.Member) and any(r.id == STAFF_ROLE_ID for r in message.author.roles)): return
    if message.author.id != row["user_id"]: return
    evidence = " ".join(a.url for a in message.attachments)
    if evidence:
        db.execute("UPDATE cases SET evidence=substr(evidence || ?,1,8000) WHERE channel_id=?", ("\n"+evidence,message.channel.id))
        db.commit()
        # Evidence is included in the final transcript; do not DM the owner yet.
    if message.content:
        # Store every applicant answer even if the AI reply is rate-limited.
        if row["kind"] in ("staff", "stash"):
            db.execute("UPDATE applications SET answers=substr(answers || ?,1,12000) WHERE channel_id=?",
                       ("\n" + message.content[:1000], message.channel.id))
            db.commit()
        # Pause AI if the member has requested a human, and avoid overlap in a ticket.
        if row["human_requested"]:
            return
        channel_id = message.channel.id
        time_now = time.monotonic()
        if (channel_id in ai_busy_channels or
                time_now - ai_last_response.get(channel_id, 0) < AI_RESPONSE_COOLDOWN_SECONDS):
            return
        ai_busy_channels.add(channel_id)
        ai_last_response[channel_id] = time_now
        try:
            async with message.channel.typing():
                answer = await ai_reply(
                    message.content,
                    f"Kind: {row['kind']}. Initial submission: {row['summary']}."
                )
            await message.channel.send(answer, allowed_mentions=discord.AllowedMentions.none())
        finally:
            ai_busy_channels.discard(channel_id)

@tasks.loop(seconds=30)
async def maintenance():
    await bot.wait_until_ready()
    for row in db.execute("SELECT * FROM giveaways WHERE finished=0 AND ends<=?",(now(),)).fetchall():
        entries = db.execute("SELECT user_id FROM entries WHERE message_id=?",(row["message_id"],)).fetchall()
        channel=bot.get_channel(row["channel_id"])
        valid=[]
        if channel and channel.guild:
            for entry in entries:
                member=channel.guild.get_member(entry["user_id"])
                if member and verified(member): valid.append(member)
        winner = random.SystemRandom().choice(valid) if valid else None
        db.execute("UPDATE giveaways SET finished=1 WHERE message_id=?",(row["message_id"],));db.commit()
        if channel:
            try:
                await channel.send(f"🎉 **Giveaway ended: {discord.utils.escape_mentions(row['item'])}**\nWinner: {winner.mention if winner else 'No eligible entries'}")
                msg=await channel.fetch_message(row["message_id"])
                await msg.edit(view=None)
            except discord.HTTPException: logging.exception("Could not finish giveaway UI")

@maintenance.before_loop
async def before_maintenance(): await bot.wait_until_ready()

@bot.event
async def on_ready():
    if not getattr(bot,"_initialized",False):
        bot.add_view(GiveawayView())
        bot.add_view(TicketView())
        for kind in ("ticket","middleman","staff","stash"): bot.add_view(PanelView(kind))
        # Owner decision buttons need registration per case after every restart.
        for row in db.execute("SELECT channel_id,kind FROM cases WHERE status='open'").fetchall():
            bot.add_view(OwnerDecisionView(row["channel_id"],row["kind"]))
        if bot.get_guild(GUILD_ID) is None:
            logging.error(
                "The bot is not in guild %s. Invite the same application token "
                "with bot and applications.commands scopes.",
                GUILD_ID,
            )
        else:
            try:
                synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
                logging.info("Synced %d slash commands in Team lb", len(synced))
            except discord.Forbidden:
                logging.error("Cannot register slash commands in guild %s: Missing Access", GUILD_ID)
            except discord.HTTPException:
                logging.exception("Team lb slash command sync failed")
        if not maintenance.is_running():
            maintenance.start()
        if not website_verification_loop.is_running():
            website_verification_loop.start()
        guild = bot.get_guild(GUILD_ID)
        if guild is not None:
            bot._verification_setup_task = asyncio.create_task(
                setup_verification_access(guild),
                name="team-lb-verification-setup",
            )
        bot._initialized=True
    logging.info("Connected as %s",bot.user)
    logging.info(
        "Cloudflare ticket AI: %s",
        "configured" if ai_configured() else "token missing; ticket fallback only",
    )


@bot.event
async def on_guild_join(guild):
    if guild.id == GUILD_ID:
        try:
            synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
            logging.info("Synced %d commands after joining Team lb", len(synced))
        except discord.HTTPException:
            logging.exception("Could not sync slash commands after joining Team lb")
        bot._verification_setup_task = asyncio.create_task(
            setup_verification_access(guild),
            name="team-lb-verification-join",
        )


@bot.event
async def on_member_join(member):
    if member.guild.id == GUILD_ID and not member.bot:
        unverified = await ensure_unverified_role(member.guild)
        if unverified is not None:
            await reconcile_verified_role(member, unverified)


@bot.event
async def on_member_update(before, after):
    # The verification Worker assigns the existing Verified role, then removes
    # Unverified. Also repair roles if a moderator changes them manually.
    if after.guild.id == GUILD_ID and not after.bot:
        await reconcile_verified_role(after)


@bot.event
async def on_guild_channel_create(channel):
    if channel.guild.id != GUILD_ID:
        return
    # The setup code creates #verify with Unverified access preconfigured.
    active = getattr(bot, "_verify_channel_id", None)
    if channel.id == active or (active is None and channel.name == VERIFY_CHANNEL_NAME):
        return
    unverified = find_unverified_role(channel.guild)
    if unverified:
        await set_verify_visibility(channel, unverified, False)

def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
