from __future__ import annotations

import httpx


class PretixClient:
    def __init__(self, api_url: str, api_token: str, organizer: str):
        self.api_url = api_url.rstrip("/")
        self.organizer = organizer
        self.headers = {"Authorization": f"Token {api_token}"}

    @staticmethod
    def _secret_matches(order: dict, secret: str) -> bool:
        """Check `secret` against the order secret and every position secret.

        Order links carry the order secret, per-ticket links carry the secret of
        one position. Pretix treats both as credentials for the order, so we do
        too.
        """
        if not secret:
            return False
        if order.get("secret") == secret:
            return True
        return any(p.get("secret") == secret for p in order.get("positions", []))

    async def verify_order(self, event: str, code: str, secret: str) -> dict | None:
        """Verify an order code and secret against the Pretix API.

        The secret may be the order secret or the secret of one of the order's
        positions. Returns the order dict if valid, None otherwise.
        """
        url = f"{self.api_url}/organizers/{self.organizer}/events/{event}/orders/{code}/"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers)

        if resp.status_code != 200:
            return None

        order = resp.json()

        if not self._secret_matches(order, secret):
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

    async def get_admission_item_ids(self, event: str) -> set[int] | None:
        """Return the set of item IDs that grant admission to the event.

        Admission items are the primary tickets that represent an attendee;
        non-admission items (add-ons, donations, merchandise) are excluded.
        The order API only exposes an item ID per position, so we resolve
        admission status from the items endpoint.

        Returns None if the items can't be fetched, so callers can fall back to
        treating every position as an attendee rather than locking anyone out.
        """
        url = f"{self.api_url}/organizers/{self.organizer}/events/{event}/items/"
        params: dict | None = {"admission": "true"}
        ids: set[int] = set()
        try:
            async with httpx.AsyncClient() as client:
                while url:
                    resp = await client.get(url, headers=self.headers, params=params)
                    if resp.status_code != 200:
                        return None
                    data = resp.json()
                    for item in data.get("results", []):
                        # The `admission` query filter narrows the payload, but
                        # re-check here so a server that ignores it still yields
                        # only admission items.
                        if item.get("admission"):
                            ids.add(item["id"])
                    # `next` is a full URL that already carries the query string.
                    url = data.get("next")
                    params = None
        except httpx.HTTPError:
            return None
        return ids
