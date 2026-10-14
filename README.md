# 🍩 Donut SMP | Team lb Discord Bot

A Python `discord.py` bot with private tickets, support AI, giveaways, middleman requests and staff/stash applications.

## Features

- `/giveaway item:<prize> time:<30m|2h|1d>`: button to enter, **only** if the entrant already has Member role `1559829257952174199`. Re-checks role when choosing the winner.
- `/ticket`: posts a support panel; members open a private ticket through a modal. The bot mentions staff role `1559829258325594149`, responds using Cloudflare Workers AI when configured (no OpenAI key needed), accepts evidence attachments and DMs submitted evidence to the owner. Buttons request a human or close a case.
- `/middleman`: opens a panel/modal for the other trader, what you give and what you receive. A human must oversee trades.
- `/apply type:staff` and `/apply type:stash`: posts application panels with tailored questions and sends details to the owner. **Note:** Discord uses `/apply type:staff` rather than a standalone `/apply staff` command; choose the type from the menu.
- `/vouch member count`: owner-only entry of **manually verified** vouches. There is no external automatic vouch/report lookup.
- Owner receives DM action buttons: **Accept / Decline** for applications, **Punish review / Second chance / Warning** for reports. Punish review records a review decision, not a ban.
- On owner inactivity the bot sends a reminder at 30 minutes for tickets and 24 hours for staff applications; the bot **does not automatically punish accused users, grant privileges, or approve stash access** based on unverified reports/AI suggestions.
- Owner DM `!status` shows open-case count. Unrestricted AI-admin DMs are deliberately not enabled; only explicitly permission-checked commands may change server state.
- SQLite persists tickets, applications, giveaway entries and an audit trail. No verification system is implemented.

## Setup

1. Create a Discord application + bot in the [Discord Developer Portal](https://discord.com/developers/applications). Keep the token secret.
2. Invite it with `bot` and `applications.commands` scopes. Give it View Channels, Send Messages, Read Message History, Manage Channels, Embed Links, and Attach Files permissions. **Do not grant Administrator**.
3. Enable **Server Members Intent** and **Message Content Intent** in the developer portal (used for eligibility checks and AI chat). Enable Direct Messages from server members on your own account if you want owner DM reports.
4. Install Python 3.11+ and run `pip install -r requirements.txt`.
5. Copy `.env.example` to `.env` and set:
   - `DISCORD_TOKEN`: token
   - `GUILD_ID`: Discord server ID
   - `OWNER_ID`: your personal Discord user ID (required for owner DM decisions)
   - `CLOUDFLARE_ACCOUNT_ID`: your Cloudflare account ID (32-character ID)
   - `CLOUDFLARE_API_TOKEN`: secret **Workers AI** API token (create one in Cloudflare; no OpenAI key needed)
   - `CLOUDFLARE_AI_MODEL`: optional model, defaults to `@cf/meta/llama-3.2-3b-instruct`
   - `TICKET_CATEGORY_ID`: optional category ID where private channels should be created
6. Run `python bot.py` on an always-on machine/host. Discord bot actions require a running process. Slash commands will sync to the specified guild.
7. In the Discord server use `/ticket`, `/middleman`, `/apply type:staff`, `/apply type:stash`, and `/giveaway` to post panels. Panel-posting commands require **Manage Server**, as does `/giveaway`.

## Free Cloudflare AI setup

1. Sign in to [Cloudflare Workers AI](https://dash.cloudflare.com/) and open **Workers AI → Use REST API**.
2. Choose **Create a Workers AI API Token** and copy it. Copy the Cloudflare **Account ID** from the same page. This token is different from an OpenAI key; keep it secret.
3. Put `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` into the bot host's **private environment variables** (or an uncommitted `.env`). Restart the bot.
4. The default `@cf/meta/llama-3.2-3b-instruct` model is a lightweight chat model. You may need to accept Meta's model terms on first use. No OpenAI account or OpenAI API key is required.
5. Test by opening a support ticket and sending a message. If the key, model or AI service is unavailable, the bot keeps the ticket open and directs the member to human staff.

**Free limit:** Cloudflare Workers AI includes **10,000 Neurons per day per account**, resetting at **00:00 UTC**. Neurons measure input/output compute, not a guaranteed number of messages. With the default model, a typical 500-input-token / 200-output-token exchange costs approximately 8.4 Neurons, so around **1,100–1,200 short responses/day** if nothing else in the account uses the allowance. Longer responses, other Workers AI apps (including your Verify Bot), or larger models reduce that count. Once the free cap is reached, requests fail until the daily reset unless you upgrade to a paid plan. The bot uses a **12-second cooldown per ticket** to reduce spam and avoid burning through the allowance. When quota is exhausted, members can request human support.

Useful docs: [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) · [Get a Cloudflare API token](https://developers.cloudflare.com/workers-ai/get-started/rest-api/) · [Supported models](https://developers.cloudflare.com/workers-ai/models/).

Run offline unit tests with `python -m unittest discover -s tests`. The tests mock Cloudflare, so they do not consume Neurons.

## Safety and operational notes

- The bot **does not perform verification**. It only checks whether the existing Member role is present.
- Owner DMs can be disabled by account privacy settings, blocking owner notifications. Check DM settings, and test in a private server.
- AI can give assistance but may make mistakes. It cannot verify guilt, prove scam claims, check external play history, or independently read all vouch data. All final high-impact decisions remain with the owner.
- The bot does not apply bans or kicks from AI recommendations; moderation must be reviewed by a human. The Warning button posts a warning to the ticket channel.
- A production deployment should use backups, anti-spam/rate limits, transcript retention policies and a more robust job scheduler as the server scales.
- Never commit the real `.env`, Discord token or Cloudflare API token to GitHub. Use a token scoped only to Workers AI access.

This is a starter implementation. **It has not been deployed or end-to-end tested against your Discord server.**
