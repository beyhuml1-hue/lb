"""Read verified Discord identities directly from the Team lb verification website.

The endpoint is private and read-only. Only the Wispbyte bot modifies member
roles here; never use website records to remove Verified or other Discord roles.
"""
import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("team_lb.verify_sync")
VERIFY_URL = "https://verify.lb000.xyz/api/bot/verified"
MAX_VERIFICATIONS = 1000
SNOWFLAKE = re.compile(r"^[0-9]{17,22}$")


class VerificationSyncError(Exception):
    """Safe, user-visible connection error; never includes credentials."""


@dataclass(frozen=True)
class VerificationEvent:
    member_id: int
    verified_at: int  # Website OAuth completion time, milliseconds since epoch.


def verification_auth_header():
    """Use a private shared key for separate bots, or the same bot token if shared."""
    shared = os.getenv("VERIFY_SYNC_KEY", "").strip()
    if shared:
        if len(shared) < 32:
            raise VerificationSyncError(
                "VERIFY_SYNC_KEY must contain at least 32 characters on Wispbyte "
                "and match the Cloudflare Worker secret."
            )
        return {"Authorization": "Bearer " + shared}
    bot_token = os.getenv("DISCORD_TOKEN", "").strip()
    if not bot_token:
        raise VerificationSyncError("DISCORD_TOKEN is missing from Wispbyte.")
    # Existing websites using the same Discord bot token need no second secret.
    return {"Authorization": "Bot " + bot_token}


async def fetch_site_verifications(session, guild_id):
    """Fetch authorized website OAuth records; never follow cross-origin redirects."""
    headers = {
        **verification_auth_header(),
        "Accept": "application/json",
    }
    try:
        async with session.get(
            VERIFY_URL, headers=headers, allow_redirects=False
        ) as response:
            if response.status in (401, 403):
                raise VerificationSyncError(
                    "Website connection denied (HTTP 403). If Wispbyte and "
                    "Cloudflare use different Discord bot tokens, configure the "
                    "same private VERIFY_SYNC_KEY on both (32+ characters)."
                )
            if response.status != 200:
                raise VerificationSyncError(
                    f"Verification website returned HTTP {response.status}; "
                    "check that the new Cloudflare Worker is deployed."
                )
            data = await response.json(content_type=None)
    except VerificationSyncError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        raise VerificationSyncError(
            "Cannot contact verify.lb000.xyz securely. Check Cloudflare deployment "
            "and website availability."
        ) from None

    if not isinstance(data, dict) or data.get("source") != "verify.lb000.xyz":
        raise VerificationSyncError(
            "The website returned an unexpected sync response. "
            "Deploy the latest Cloudflare Worker."
        )
    rows = data.get("members")
    if not isinstance(rows, list) or len(rows) > MAX_VERIFICATIONS:
        raise VerificationSyncError(
            "The website returned too many or invalid verification records."
        )
    # Website also enforces the expiry. Validate again before granting roles.
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - 180 * 86400000
    requested = str(guild_id)
    results = {}
    for row in rows:
        if not isinstance(row, dict) or str(row.get("server_id")) != requested:
            continue
        user_id = str(row.get("id", ""))
        if not SNOWFLAKE.fullmatch(user_id):
            continue
        date = row.get("verified_at")
        if isinstance(date, bool) or not isinstance(date, (int, float)):
            continue
        timestamp = int(date)
        if not cutoff_ms < timestamp <= now_ms + 60000:
            continue
        member_id = int(user_id)
        results[member_id] = max(results.get(member_id, 0), timestamp)
    return [
        VerificationEvent(member_id, timestamp)
        for member_id, timestamp in sorted(results.items())
    ]
