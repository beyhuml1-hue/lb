# Donut SMP | Team lb Discord Bot

Production Python bot for Team lb: verification panels, private AI-supported tickets, giveaway management, middleman requests, applications and community vouches.

## Commands

| Command | Who can use it | What it does |
| --- | --- | --- |
| `/verify` | Everyone | Gives unverified members a private link; managers are directed to the restricted #verify channel |
| `!verify` | Server managers | Confirms that the verification panel is in the restricted #verify channel |
| `/ticket` | Server managers | Posts the private-ticket panel |
| `/middleman` | Server managers | Posts the middleman request panel |
| `/apply type:staff` or `type:stash` | Server managers | Posts the respective application panel |
| `/giveaway item time` | Server managers | Starts a giveaway requiring verified Member role `1560003144036126853` |
| `vouch @user` | Members | Gives another member **one vouch**; each giver can vouch only once every **30 minutes**, even after a restart; no self-vouching or bots |
| `!vouchcount @user number` | Owner only | Sets a user's vouch count. Raw Discord user ID works instead of a mention |
| `/vouch member count` | Owner only | Slash-command equivalent for manually correcting vouch counts |
| `!say message` | Owner or server managers | Posts the message as a purple Discord embed; disables mass-mention pings |
| `/aitest` | Owner only | Checks whether Cloudflare Workers AI is configured and performs one private live AI reply test |

Every successful `vouch @user` produces a confirmation with that person's current total. Vouch counts and the 30-minute cooldown are saved in the persistent SQLite database.

## Ticket and owner notification workflow

- Each ticket, middleman request or application creates a private channel for the member, owner and staff. Cloudflare AI answers quick questions *inside* the ticket while it is open; members can select **Request human** to stop further AI replies.
- The owner gets **no automatic DM** when a ticket opens, when evidence is posted, when a human is requested or when an old ticket reaches a time threshold. Staff assistance is handled inside the ticket, and the owner-only review decision buttons appear **inside the ticket**, not in owner DMs.
- The **Close** button concludes the case and sends the owner **one final DM**: ticket type, member, channel, closer, recorded owner decision, original request and a text-file transcript of up to the last 200 messages, including attachment links. Ticket close is one-time and the reported state is persisted.
- The owner can review the whole conversation once it is finished. No AI message automatically approves users, punishes members, executes trades or grants roles.
- To get the final DM, the owner must accept DMs from the bot. If Discord prevents DMs, the transcript remains in the ticket channel (which does not get deleted by closing).

## Deploy and start on Wispbyte

This repository uses **actual startup files**, not placeholder configuration files:

- `main.py` — Python entry point (Wispbyte `PY_FILE=main.py`)
- `bot.py` — Discord commands, vouches, ticket workflow and final report
- `cloudflare_ai.py` — reusable Cloudflare REST API client
- `requirements.txt` — required Python packages
- `Procfile` — starts the worker with `python main.py`
- `bot.sqlite3` — local persistent database created on first run; **do not delete your existing one**

Use Python **3.11 or newer**. Wispbyte's `REQUIREMENTS_FILE` is `requirements.txt`; the startup command executes `main.py`. If Wispbyte has a Git checkout, enable its `AUTO_UPDATE` or manually copy the latest `bot.py` and `cloudflare_ai.py` to the host, then restart.

Wispbyte private environment settings:

| Setting | Value / purpose |
| --- | --- |
| `DISCORD_TOKEN` | Your **new, rotated** Discord bot token; never place it in source control or chat |
| `GUILD_ID` | Team lb server ID `1559829257952174192` (default is already in code) |
| `OWNER_ID` | Owner `1500253920918900883` (default is already in code) |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID is already configured in code |
| `CLOUDFLARE_API_TOKEN` | Your private **Workers AI API token**, required for AI replies |
| `CLOUDFLARE_AI_MODEL` | Optional, defaults to `@cf/meta/llama-3.2-1b-instruct` |
| `TICKET_CATEGORY_ID` | Optional, `0` means create ticket channels at server root |
| `DB_PATH` | Optional, default `bot.sqlite3` |

To obtain the AI token: in the [Cloudflare Dashboard](https://dash.cloudflare.com/) open **Workers AI → Use REST API → Create a Workers AI API Token**. Add it **privately** to Wispbyte. The account ID and model alone cannot authenticate requests. The token is never stored in GitHub, and **no OpenAI key is needed**. If the token is absent, the bot starts and ticket replies show a human-support fallback.

On Discord Developer Portal → Bot, enable **Server Members Intent** and **Message Content Intent**. Install the bot with both `bot` and `applications.commands` scopes, plus View/Send Messages, Read Message History, Manage Channels, Manage Roles, Embed Links and Attach Files, but **not Administrator**. If the slash-command sync reports `Missing Access`, verify that the installed bot and the `GUILD_ID` are correct.

## Cloudflare AI performance and limits

By default the bot uses the lightweight `@cf/meta/llama-3.2-1b-instruct` model and a pooled HTTP connection for quick, short answers. It allows up to 8 concurrent inference requests with a 2-second cooldown and one active request per ticket. Responses are limited to 144 generated tokens. Some predictable help questions are answered without AI usage. If capacity is limited, the bot falls back to human support.

Cloudflare's free Workers AI allocation is **10,000 Neurons per day per account**, not a guaranteed count of messages. At an illustrative 500 input + 140 output tokens, the 1B model consumes approximately **3.78 Neurons per reply**, enough for roughly **2,600 messages per day** if the account has no other AI usage. More complex messages, concurrency limits and the verification Worker can lower that substantially. The 800-message goal is an estimate and **not guaranteed**. See [Cloudflare Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/).

## Automatic Verified and Unverified onboarding

Team lb server ID: `1559829257952174192`. Your **existing Verified role** ID is `1560003144036126853`; the bot **reuses it and does not create another Verified role**.

When the Wispbyte bot starts, it automatically:

1. Finds the existing **Unverified** role by ID `1560003406687371355`; it does not create a duplicate role.
2. Finds or creates the `#verify` text channel, hides it from `@everyone` and Verified, gives **Unverified** read-only access, and posts the purple **Verify Now** Discord OAuth button once.
3. Makes all other current server channels hidden from the **Unverified** role, without changing any other role's permissions. Newly created channels are also hidden from Unverified.
4. Adds Unverified to new people who are not yet Verified, and reconciles existing server members after bot startup.
5. Removes Unverified when the existing Verified role is assigned; if Verified is removed later, it restores Unverified.

The verification website `https://verify.lb000.xyz/?server=1559829257952174192` runs in the **separate** `beyhuml1-hue/discord-bot` repository. Its `website/verification.mjs` worker now assigns **Verified** role `1560003144036126853` and then removes **Unverified** role `1560003406687371355` after successful OAuth verification. The Python bot additionally cleans up any leftover Unverified role on a Discord member update. `/verify` displays a private link to unverified members; `!verify` and manager `/verify` point to the restricted verification channel rather than publishing the panel elsewhere.

**Required permissions:** Both the Wispbyte bot and the Cloudflare verification bot need **Manage Roles** and must sit **above both Verified and Unverified** in Server Settings → Roles. The Wispbyte bot also needs **Manage Channels**, **View Channels**, **Send Messages**, **Read Message History** and **Embed Links**. Enable **Server Members Intent** and **Message Content Intent**. Without Manage Roles/Channels, automatic setup will log an error instead of silently making an unrestricted verification panel. Discord administrators can still see private channels.

**Deployment:** Pull the new `lb/bot.py` into Wispbyte and restart. Redeploy the Cloudflare `verify-portal` Worker for the updated OAuth role removal to take effect. GitHub passing checks does not automatically deploy either service. Keep your existing private bot tokens and `bot.sqlite3`; never post tokens in chat.

## Tests and security

GitHub Actions installs requirements, checks syntax and runs offline unit tests for Cloudflare AI, vouch cooldowns, vouch editing, announcement embeds and one-time closure notifications:

```bash
python -m unittest discover -s tests -v
```

Keep the existing SQLite database persistent and back it up before changing your host. Token values belong only in Wispbyte's secure startup variables or a private `.env` file, **never in GitHub**. The previously shared Discord token must remain revoked; use a newly reset one.

GitHub tests do not verify live Wispbyte or Cloudflare deployments. Run `/aitest` once your private Workers AI token is configured.
