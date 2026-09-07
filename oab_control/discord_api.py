"""Minimal Discord REST transport used by explicit control-plane commands."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .discord_dispatch import DiscordHttpError
from .discord_receipts import ReceiptError


class DiscordApi:
    def __init__(self, token: str, *, base_url: str = "https://discord.com/api/v10", timeout: float = 15.0):
        if not isinstance(token, str) or not token:
            raise ValueError("Discord token is required")
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def send(self, channel_id: str, content: str) -> str:
        try:
            document = self._request("POST", f"/channels/{channel_id}/messages", {"content": content})
        except HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            raise DiscordHttpError(exc.code, retry_after=retry_after) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TimeoutError("Discord response was unavailable") from exc
        message_id = document.get("id") if isinstance(document, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise TimeoutError("Discord response did not contain a message ID")
        return message_id

    def discover_thread(self, parent_channel_id: str, message_id: str) -> str:
        try:
            document = self._request("GET", f"/channels/{message_id}")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ReceiptError("unable to discover the assignment thread") from exc
        if not isinstance(document, dict) or document.get("parent_id") != parent_channel_id or document.get("id") != message_id:
            raise ReceiptError("dispatch message has no associated thread")
        return message_id

    def read_messages(self, thread_id: str) -> list[dict[str, Any]]:
        try:
            document = self._request("GET", f"/channels/{thread_id}/messages?limit=100")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ReceiptError("unable to read the assignment thread") from exc
        if not isinstance(document, list):
            raise ReceiptError("Discord thread response was invalid")
        result: list[dict[str, Any]] = []
        for item in document:
            author = item.get("author") if isinstance(item, dict) else None
            if isinstance(item, dict) and isinstance(author, dict):
                result.append({
                    "id": item.get("id"), "channel_id": item.get("channel_id"),
                    "author_id": author.get("id"), "timestamp": item.get("timestamp"),
                    "content": item.get("content"),
                })
        return result

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        document = self._request("GET", f"/channels/{channel_id}")
        if not isinstance(document, dict):
            raise ReceiptError("Discord channel response was invalid")
        return document

    def trigger_typing(self, channel_id: str) -> None:
        try:
            self._request("POST", f"/channels/{channel_id}/typing")
        except HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            raise DiscordHttpError(exc.code, retry_after=retry_after) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TimeoutError("Discord typing capability could not be checked") from exc

    def find_messages(self, channel_id: str, nonce: str) -> list[dict[str, Any]]:
        """Completely scan channel history or fail without resolving unknown."""

        before = ""
        matches: list[dict[str, Any]] = []
        for _page in range(100):
            suffix = f"&before={before}" if before else ""
            try:
                document = self._request("GET", f"/channels/{channel_id}/messages?limit=100{suffix}")
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                raise ReceiptError("unable to completely scan Discord history for reconciliation") from exc
            if not isinstance(document, list):
                raise ReceiptError("Discord reconciliation response was invalid")
            for item in document:
                author = item.get("author") if isinstance(item, dict) else None
                content = item.get("content") if isinstance(item, dict) else None
                if isinstance(author, dict) and isinstance(content, str) and f"dispatch_nonce: {nonce}" in content.splitlines():
                    matches.append({"id": item.get("id"), "author_id": author.get("id"), "thread_id": item.get("thread", {}).get("id", "") if isinstance(item.get("thread"), dict) else ""})
            if len(document) < 100:
                return matches
            before = str(document[-1].get("id", ""))
            if not before:
                raise ReceiptError("Discord reconciliation pagination was incomplete")
        raise ReceiptError("Discord reconciliation history exceeded the safe scan limit")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(
            self.base_url + path,
            data=payload,
            method=method,
            headers={"Authorization": f"Bot {self._token}", "Content-Type": "application/json", "User-Agent": "oab-control/1"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            payload = response.read().decode()
            return json.loads(payload) if payload else {}
