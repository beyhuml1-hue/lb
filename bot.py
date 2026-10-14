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
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from cloudflare_ai import generate_reply, start_ai_client, close_ai_client

load_dotenv()
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN or TOKEN.startswith("replace_"):
    raise SystemExit("Set your NEW Discord bot token as DISCORD_TOKEN in private host variables. Never commit or share it.")
GUILD_ID = int(os.getenv("GUILD_ID", "1559829257952174192"))
OWNER_ID = int(os.getenv("OWNER_ID", "1500253920918900883"))
MEMBER_ROLE_ID = 1559829257952174199
STAFF_ROLE_ID = 1559829258325594149
AI_RESPONSE_COOLDOWN_SECONDS = 2.0
ai_last_response = {}
ai_busy_channels = set()
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
def verified(member): return isinstance(member, discord.Member) and any(r.id == MEMBER_ROLE_ID for r in member.roles)
def staff_role(guild): return guild.get_role(STAFF_ROLE_ID)
def case_for(channel_id):
    return db.execute("SELECT * FROM cases WHERE channel_id=?", (channel_id,)).fetchone()

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
        await start_ai_client()

    async def close(self):
        try:
            await close_ai_client()
        finally:
            await super().close()


bot = TeamLBBot(command_prefix="!", intents=intents)

class GiveawayView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="🎉 Enter giveaway", style=discord.ButtonStyle.success, custom_id="lb:giveaway:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not verified(interaction.user):
            return await interaction.response.send_message("You need the existing verified Member role to enter.", ephemeral=True)
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
        if not row or (interaction.user.id != row["user_id"] and not is_owner(interaction.user) and not interaction.user.guild_permissions.manage_channels):
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
        if not row: return await interaction.response.send_message("Case not found.", ephemeral=True)
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
    if role: overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
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
    msg = await interaction.channel.send(embed=panel("🎉 Team lb Giveaway",f"**Prize:** {discord.utils.escape_mentions(item[:300])}\n**Ends:** <t:{ends}:R>\nYou must have the existing Member role to enter."),view=GiveawayView())
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


@bot.tree.command(name="verify", description="Open the official Discord verification panel")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def verify_command(interaction: discord.Interaction):
    if not interaction.guild_id:
        return await interaction.response.send_message(
            "Use this command in your Team lb server.", ephemeral=True
        )
    embed, view = verification_panel(interaction.guild_id)
    # Managers post a panel for the whole channel. Everyone else gets a private
    # verification button, keeping public channels clear of repeated panels.
    if interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Verification panel posted.", ephemeral=True)
        await interaction.channel.send(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


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

@bot.tree.command(name="vouch",description="Set an applicant's manually reviewed vouch count")
@app_commands.describe(member="Member to update",count="Reviewed vouches")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def vouch(interaction:discord.Interaction,member:discord.Member,count:app_commands.Range[int,0,100000]):
    if not is_owner(interaction.user): return await interaction.response.send_message("Owner only.",ephemeral=True)
    db.execute("INSERT OR REPLACE INTO vouches VALUES (?,?)",(member.id,count)); db.commit()
    log_action(interaction.user.id,"vouch",f"{member.id}:{count}")
    await interaction.response.send_message(f"Stored {count} reviewed vouches for {member.mention}.",ephemeral=True)

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
            embed, view = verification_panel(message.guild.id)
            await message.channel.send(embed=embed, view=view)
        else:
            await message.reply(
                "Use **/verify** to get your personal verification link.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return
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
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        maintenance.start()
        bot._initialized=True
    logging.info("Connected as %s",bot.user)

def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
