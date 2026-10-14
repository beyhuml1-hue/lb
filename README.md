# 🍩 Donut SMP | Team lb Discord Bot

A Python `discord.py` bot with private tickets, support AI, giveaways, middleman requests and staff/stash applications.

## Features

- `/giveaway item:<prize> time:<30m|2h|1d>`: button to enter, **only** if the entrant already has Member role `1559829257952174199`. Re-checks role when choosing the winner.
- `/verify`: opens the official Discord OAuth verification panel with a purple **Verify Now** link button. Server managers post a public panel; other members receive a private button. Managers can also post the public embed using `!verify`.
- `/ticket`: posts a support panel; members open private tickets through a modal. The bot mentions staff role `1559829258325594149`, gives quick Cloudflare AI answers when configured (no OpenAI key needed), accepts evidence attachments and DMs them to the owner. Members can request a human (which pauses AI) or close a case.
- `/middleman`: opens a panel/modal for the other trader, what you give and what you receive. A human must oversee trades.
- `/apply type:staff` and `/apply type:stash`: posts application panels with tailored questions and sends details to the owner. **Note:** Discord uses `/apply type:staff` rather than a standalone `/apply staff` command; choose the type from the menu.
- `/vouch member count`: owner-only entry of **manually verified** vouches. There is no external automatic vouch/report lookup.
- Owner receives DM action buttons: **Accept / Decline** for applications, **Punish review / Second chance / Warning** for reports. Punish review records a review decision, not a ban.
- On owner inactivity the bot sends a reminder at 30 minutes for tickets and 24 hours for staff applications; the bot **does not automatically punish accused users, grant privileges, or approve stash access** based on unverified reports/AI suggestions.
- Owner DM `!status` shows open-case count. Unrestricted AI-admin DMs are deliberately not enabled; only explicitly permission-checked commands may change server state.
- SQLite persists tickets, applications, giveaway entries and an audit trail. No verification system is implemented.

## Start the bot (Python 3.11+)

**Required startup file: `main.py`** (and `requirements.txt` for packages). `bot.py` also works if your host explicitly starts it. The `Procfile` contains `worker: python main.py`.

1. **Reset the Discord bot token** in the Discord Developer Portal if it was ever posted or shared. Treat the old token as compromised. Never upload the replacement to GitHub or send it in a chat.
2. Install dependencies: `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to a private `.env` file **or** add the same values in your host's private environment variables. The example already contains the server ID, owner ID and public Cloudflare Account ID. Paste your **new Discord token** as `DISCORD_TOKEN` privately.
4. In [Cloudflare Workers AI → Use REST API](https://dash.cloudflare.com/), create a **Workers AI API Token** and set `CLOUDFLARE_API_TOKEN` privately. Cloudflare suggests using its Workers AI API Token template. Do not substitute your Discord token or an OpenAI key.
5. Start with `python main.py`. The bot syncs its slash commands to your configured guild; open a support ticket to check AI responses.

For **Wispbyte**: choose Python 3.11+ and set `PY_FILE=main.py`, `REQUIREMENTS_FILE=requirements.txt`, then restart. For a host supporting **Procfile**, it already starts `python main.py`. The Python bot must run on an always-on host; the Cloudflare verification website is a *separate deployment* and does not launch this Python process.

### Required private variables

| Name | Meaning |
| --- | --- |
| `DISCORD_TOKEN` | **Newly reset secret** Discord bot token; required to start |
| `GUILD_ID` | Team lb server ID (already set in the template; code also has a default) |
| `OWNER_ID` | Server owner's Discord ID (already set in the template; code also has a default) |
| `CLOUDFLARE_ACCOUNT_ID` | Your public Cloudflare account identifier, already set in the example |
| `CLOUDFLARE_API_TOKEN` | Secret Cloudflare Workers AI token; required for live AI replies |
| `CLOUDFLARE_AI_MODEL` | Optional: defaults to `@cf/meta/llama-3.2-1b-instruct` |
| `TICKET_CATEGORY_ID` | Optional Discord ticket category ID; use `0` to create in server root |
| `DB_PATH` | Optional persistent SQLite path; default `bot.sqlite3` |

**Discord bot permissions:** Invite with `bot` and `applications.commands`; grant View Channels, Send Messages, Read Message History, Manage Channels, Embed Links and Attach Files, but not Administrator. Enable **Server Members Intent** and **Message Content Intent** in Discord Developer Portal → Bot. Keep the Member role (`1559829257952174199`) below the bot's role if your other Verify Bot assigns it.

## Fast AI responses and free message capacity

- Uses Cloudflare Workers AI with the **Llama 3.2 1B** lightweight model by default for quick responses. Set `CLOUDFLARE_AI_MODEL=@cf/meta/llama-3.2-3b-instruct` to trade some volume for better-quality AI answers. You may need to accept Meta's model terms on first use.
- Up to **8 simultaneous AI requests**, using a pooled HTTPS connection instead of establishing a connection for every message. Replies are capped at **144 generated tokens** and ticket inputs are trimmed to reduce response time and compute cost. Requests fall back to staff if Cloudflare cannot respond within about **15 seconds**.
- **2-second per-ticket cooldown** plus in-flight deduplication prevents an accidental burst in the same ticket from consuming the allowance. Other tickets can receive replies concurrently. A few public FAQs (such as how to verify) are answered instantly **without any AI request**.
- Cloudflare's **free allocation is 10,000 Neurons per account per day**, resetting at 00:00 UTC. This is **not** 10,000 messages. With the 1B model, an illustrative response with 500 input tokens and 140 output tokens consumes about **3.78 Neurons**, or approximately **2,600 AI replies/day** before any other Workers AI usage. It is an estimate **not a guarantee** of 800 or 2,600 replies: length, model choice, availability and other activity on your Cloudflare account (including the Verify Bot) affect the remaining allowance. The free quota is enforced by Cloudflare; staff fallback works once exhausted.
- Text-generation request rate limit is normally **300 requests/minute**, separate from your daily Neuron quota. A 2-second *per-ticket* cooldown does not limit different tickets to one at a time.
- View the **actual Neuron usage** in the Cloudflare Workers AI dashboard. Paid Cloudflare plans may charge for use beyond the free allocation; do not enable paid usage if you require zero cost.

Docs: [Pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) · [Rate limits](https://developers.cloudflare.com/workers-ai/platform/limits/) · [Create API token](https://developers.cloudflare.com/workers-ai/get-started/rest-api/).

## The verification command

Use `/verify` in the Team lb server to show a purple verification embed with the **Verify Now** button linking to `https://verify.lb000.xyz/?server=<your guild ID>`. Members see it privately; people with **Manage Server** permission can publish it to the channel. A manager can also use `!verify` to post the same panel publicly.

Verification is handled by your existing Cloudflare Verify Portal, **not by this Python bot**, and it is separate from the Cloudflare AI token. The bot does not ask members to share a Discord password. The external website controls which role is assigned; make sure it assigns the Member role `1559829257952174199` if you want successful verification to unlock Team lb giveaways.

## Verification and remaining features

`/giveaway` checks the existing Member role from your separate Verify Bot; this Python repository does **not** run the verification website. `/ticket`, `/middleman`, `/apply type:staff`, `/apply type:stash`, and `/giveaway` publish panels (Manage Server required for panel-posting). The bot does not automatically approve applications, ban members or determine guilt from AI responses. The human owner makes final decisions.

Run syntax and offline AI tests with `python -m compileall -q main.py bot.py cloudflare_ai.py tests` and `python -m unittest discover -s tests -v`; tests use mocked Cloudflare responses and require no paid inference.

## Safety and operational notes

- The bot **does not perform verification**. It only checks whether the existing Member role is present.
- Owner DMs can be disabled by account privacy settings, blocking owner notifications. Check DM settings, and test in a private server.
- AI can give assistance but may make mistakes. It cannot verify guilt, prove scam claims, check external play history, or independently read all vouch data. All final high-impact decisions remain with the owner.
- The bot does not apply bans or kicks from AI recommendations; moderation must be reviewed by a human. The Warning button posts a warning to the ticket channel.
- A production deployment should use backups, anti-spam/rate limits, transcript retention policies and a more robust job scheduler as the server scales.
- Never commit the real `.env`, Discord token or Cloudflare API token to GitHub. The private `.env` and `.env.*` files are gitignored except the sanitized `.env.example` template.

This is a starter implementation. **It has not been deployed or end-to-end tested against your Discord server.**
