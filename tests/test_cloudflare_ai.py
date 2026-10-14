"""Offline tests for Team lb's Cloudflare AI adapter (no API calls)."""
import os
import unittest
from unittest.mock import patch

import cloudflare_ai as ai


class FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.data


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class CloudflareAITests(unittest.IsolatedAsyncioTestCase):
    def configured(self, fake_response):
        env = {
            "CLOUDFLARE_ACCOUNT_ID": "a" * 32,
            "CLOUDFLARE_API_TOKEN": "test-only-token",
            "CLOUDFLARE_AI_MODEL": ai.DEFAULT_MODEL,
        }
        session = FakeSession(fake_response)
        return env, session

    async def test_missing_credentials_falls_back_without_call(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(await ai.generate_reply("help"), ai.NOT_CONFIGURED)

    async def test_response_calls_expected_endpoint_and_model(self):
        env, session = self.configured(FakeResponse(200, {
            "success": True, "result": {"response": "Try restarting Minecraft."}
        }))
        with patch.dict(os.environ, env, clear=True), patch.object(
            ai.aiohttp, "ClientSession", return_value=session
        ):
            reply = await ai.generate_reply("My game crashed", "support ticket")
        self.assertEqual(reply, "Try restarting Minecraft.")
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/ai/run/" + ai.DEFAULT_MODEL)
        self.assertEqual(kwargs["json"]["max_tokens"], 220)
        self.assertEqual(kwargs["json"]["messages"][0]["role"], "system")
        self.assertIn("My game crashed", kwargs["json"]["messages"][1]["content"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-only-token")
        self.assertFalse(kwargs["allow_redirects"])

    async def test_long_inputs_and_outputs_are_capped(self):
        env, session = self.configured(FakeResponse(200, {
            "success": True, "result": {"response": "A" * 2000}
        }))
        with patch.dict(os.environ, env, clear=True), patch.object(
            ai.aiohttp, "ClientSession", return_value=session
        ):
            reply = await ai.generate_reply("Q" * 5000, "C" * 5000)
        self.assertEqual(len(reply), ai.MAX_REPLY_CHARS)
        user_prompt = session.calls[0][1]["json"]["messages"][1]["content"]
        self.assertEqual(user_prompt.count("Q"), 1800)
        self.assertEqual(user_prompt.count("C"), 1200)

    async def test_free_daily_quota_has_clear_message(self):
        env, session = self.configured(FakeResponse(429, {
            "success": False, "errors": [{"code": 3036, "message": "quota"}]
        }))
        with patch.dict(os.environ, env, clear=True), patch.object(
            ai.aiohttp, "ClientSession", return_value=session
        ):
            self.assertEqual(await ai.generate_reply("help"), ai.DAILY_LIMIT)

    async def test_capacity_limit_falls_back_to_staff(self):
        env, session = self.configured(FakeResponse(429, {
            "success": False, "errors": [{"code": 3040, "message": "capacity"}]
        }))
        with patch.dict(os.environ, env, clear=True), patch.object(
            ai.aiohttp, "ClientSession", return_value=session
        ):
            self.assertEqual(await ai.generate_reply("help"), ai.TEMPORARY_FAILURE)

    async def test_auth_failure_does_not_expose_token(self):
        env, session = self.configured(FakeResponse(403, {
            "success": False, "errors": [{"code": 5016, "message": "license"}]
        }))
        with patch.dict(os.environ, env, clear=True), patch.object(
            ai.aiohttp, "ClientSession", return_value=session
        ):
            self.assertEqual(await ai.generate_reply("help"), ai.TEMPORARY_FAILURE)

    async def test_empty_model_reply_falls_back(self):
        env, session = self.configured(FakeResponse(200, {
            "success": True, "result": {"response": ""}
        }))
        with patch.dict(os.environ, env, clear=True), patch.object(
            ai.aiohttp, "ClientSession", return_value=session
        ):
            self.assertEqual(await ai.generate_reply("help"), ai.TEMPORARY_FAILURE)

    async def test_invalid_account_prevents_request(self):
        with patch.dict(os.environ, {
            "CLOUDFLARE_ACCOUNT_ID": "wrong",
            "CLOUDFLARE_API_TOKEN": "test-only",
        }, clear=True):
            self.assertEqual(await ai.generate_reply("help"), ai.NOT_CONFIGURED)


if __name__ == "__main__":
    unittest.main()
