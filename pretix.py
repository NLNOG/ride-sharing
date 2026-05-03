from __future__ import annotations

import httpx


class PretixClient:
    def __init__(self, api_url: str, api_token: str, organizer: str):
        self.api_url = api_url.rstrip("/")
        self.organizer = organizer
        self.headers = {"Authorization": f"Token {api_token}"}

    async def verify_order(self, event: str, code: str, secret: str) -> dict | None:
        """Verify an order code and secret against the Pretix API.

        Returns the order dict if valid, None otherwise.
        """
        url = f"{self.api_url}/organizers/{self.organizer}/events/{event}/orders/{code}/"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers)

        if resp.status_code != 200:
            return None

        order = resp.json()

        if order.get("secret") != secret:
            return None

        if order.get("status") in ("c", "e"):  # cancelled or expired
            return None

        return order

    async def get_event(self, event: str) -> dict | None:
        """Fetch event details (name, date_from, etc.)."""
        url = f"{self.api_url}/organizers/{self.organizer}/events/{event}/"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers)

        if resp.status_code != 200:
            return None

        return resp.json()
