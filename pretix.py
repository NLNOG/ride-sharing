from __future__ import annotations

import hmac
import re

import httpx

# Order codes, secrets and position numbers as they appear in Pretix's own URLs.
# We build a ticket-shop URL from these, so anything outside the character class
# is refused rather than pasted into a path.
_CODE_RE = re.compile(r"^[A-Za-z0-9]+$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9]+$")
_POSITION_RE = re.compile(r"^[0-9]+$")


class PretixClient:
    def __init__(self, api_url: str, api_token: str, organizer: str, presale_url: str = ""):
        self.api_url = api_url.rstrip("/")
        self.organizer = organizer
        self.headers = {"Authorization": f"Token {api_token}"}
        # The ticket shop lives at the API host minus the API path, unless it is
        # configured explicitly (self-hosted installs can split the two).
        self.presale_url = (presale_url or re.sub(r"/api(/v\d+)?/?$", "", self.api_url)).rstrip("/")

    async def get_order(self, event: str, code: str) -> dict | None:
        """Fetch an order by code. Performs no secret check of its own."""
        url = f"{self.api_url}/organizers/{self.organizer}/events/{event}/orders/{code}/"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers)

        if resp.status_code != 200:
            return None

        return resp.json()

    async def verify_order(self, event: str, code: str, secret: str) -> dict | None:
        """Verify an order code and its order secret against the Pretix API.

        Returns the order dict if valid, None otherwise.
        """
        order = await self.get_order(event, code)
        if not order:
            return None

        if not secret or not hmac.compare_digest(order.get("secret") or "", secret):
            return None

        if order.get("status") in ("c", "e"):  # cancelled or expired
            return None

        return order

    async def verify_ticket(
        self, event: str, code: str, position: str, secret: str,
    ) -> dict | None:
        """Verify a per-ticket link and return the order it belongs to.

        A ticket link (`/ticket/<code>/<position>/<secret>/`) carries a
        position's `web_secret`, which the REST API never exposes — the position
        `secret` it does expose is the barcode scanned at the door, a different
        value that must not be accepted as a login credential. So the ticket shop
        validates the link for us: it renders the page only for the right
        secret and 404s otherwise. Order details still come from the API.

        Returns the order dict if the link is valid, None otherwise.
        """
        if not (
            _CODE_RE.match(code or "")
            and _POSITION_RE.match(position or "")
            and _SECRET_RE.match(secret or "")
        ):
            return None

        path = f"/{self.organizer}/{event}/ticket/{code}/{position}/{secret}/"
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(f"{self.presale_url}{path}")
        except httpx.HTTPError:
            return None

        # A shop that bounces us elsewhere (event password gate, sign-in page)
        # can answer 200 for any secret, so only a response still sitting on the
        # ticket page counts as proof.
        if resp.status_code != 200:
            return None
        if f"/ticket/{code}/{position}/" not in resp.url.path:
            return None

        order = await self.get_order(event, code)
        if not order or order.get("status") in ("c", "e"):
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
