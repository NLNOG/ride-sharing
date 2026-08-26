"""Tests for the NLNOG Ride Share app."""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from httpx import ASGITransport, AsyncClient, Cookies
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cleanup import cleanup_expired_events
from models import Attendee, Base, Ride, RideClaim, RideOffer, RideRequest

# Use a test database
os.environ["DATABASE_URL"] = "sqlite:///./test_rideshare.db"

passed = 0
failed = 0


def ok(name: str):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name: str, detail: str = ""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

def test_models():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    a1 = Attendee(pretix_event="ev", pretix_order_code="A", pretix_position_id=1, name="Driver", email="d@x.com")
    a2 = Attendee(pretix_event="ev", pretix_order_code="B", pretix_position_id=1, name="Pass1", email="p1@x.com")
    a3 = Attendee(pretix_event="ev", pretix_order_code="C", pretix_position_id=1, name="Pass2", email="p2@x.com")
    db.add_all([a1, a2, a3])
    db.commit()

    ride = Ride(
        event="ev", driver_id=a1.id, departure_location="AMS",
        departure_time=datetime(2026, 9, 15, 8, 0), seats=3,
    )
    db.add(ride)
    db.commit()

    # Direction defaults
    assert ride.direction == "round_trip"
    ok("ride direction defaults to round_trip")

    # Contact presence
    assert a1.has_contact is False
    a1.phone = "+31600000000"
    assert a1.has_contact is True
    a1.phone = ""
    a1.notes = "find me on IRC"
    assert a1.has_contact is True
    a1.notes = ""
    ok("attendee has_contact reflects phone/notes")

    # No claims
    assert ride.seats_available == 3
    ok("seats_available with no claims")

    # Approved + pending
    c1 = RideClaim(ride_id=ride.id, passenger_id=a2.id, status="approved")
    c2 = RideClaim(ride_id=ride.id, passenger_id=a3.id, status="pending")
    db.add_all([c1, c2])
    db.commit()
    db.refresh(ride)

    assert ride.seats_available == 2
    ok("seats_available counts only approved")
    assert len(ride.approved_claims) == 1
    ok("approved_claims filter")
    assert len(ride.pending_claims) == 1
    ok("pending_claims filter")

    # Reject pending
    c2.status = "rejected"
    db.commit()
    db.refresh(ride)
    assert ride.seats_available == 2
    assert len(ride.pending_claims) == 0
    ok("rejected claims don't count")

    # Ride offers
    rr = RideRequest(event="ev", requester_id=a2.id, location="Rotterdam")
    db.add(rr)
    db.commit()
    assert rr.direction == "to_event"
    ok("request direction defaults to to_event")
    offer = RideOffer(ride_id=ride.id, request_id=rr.id, status="pending")
    db.add(offer)
    db.commit()
    db.refresh(rr)
    assert len(rr.offers) == 1
    ok("ride offers relationship")

    # Cascade delete
    db.delete(ride)
    db.commit()
    assert db.query(RideClaim).count() == 0
    assert db.query(RideOffer).count() == 0
    ok("cascade delete cleans up claims and offers")

    db.close()


def test_directions():
    from main import directions_compatible

    assert directions_compatible("round_trip", "to_event") is True
    assert directions_compatible("round_trip", "from_event") is True
    assert directions_compatible("to_event", "to_event") is True
    assert directions_compatible("from_event", "from_event") is True
    assert directions_compatible("to_event", "from_event") is False
    assert directions_compatible("from_event", "to_event") is False
    ok("directions_compatible matches one-way rides correctly")


# ---------------------------------------------------------------------------
# Retention cleanup tests
# ---------------------------------------------------------------------------

def _seed_event(db, event: str):
    """Create a full set of data (attendees, ride, request, claim, offer) for an event."""
    driver = Attendee(pretix_event=event, pretix_order_code="D", pretix_position_id=1, name="Driver")
    passenger = Attendee(pretix_event=event, pretix_order_code="P", pretix_position_id=1, name="Passenger")
    db.add_all([driver, passenger])
    db.commit()

    ride = Ride(event=event, driver_id=driver.id, departure_location="AMS",
                departure_time=datetime(2026, 9, 15, 8, 0), seats=3)
    rr = RideRequest(event=event, requester_id=passenger.id, location="Rotterdam")
    db.add_all([ride, rr])
    db.commit()

    db.add(RideClaim(ride_id=ride.id, passenger_id=passenger.id, status="approved"))
    db.add(RideOffer(ride_id=ride.id, request_id=rr.id, status="pending"))
    db.commit()


async def test_cleanup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    _seed_event(db, "past_ev")
    _seed_event(db, "future_ev")

    now = datetime.now(timezone.utc)
    # past_ev ended well beyond the 4-day window; future_ev hasn't ended yet.
    event_dates = {
        "past_ev": {"date_to": (now - timedelta(days=10)).isoformat()},
        "future_ev": {"date_to": (now + timedelta(days=10)).isoformat()},
    }

    fake_pretix = type("P", (), {})()
    fake_pretix.get_event = AsyncMock(side_effect=lambda slug: event_dates.get(slug))

    purged = await cleanup_expired_events(db, fake_pretix)

    assert purged == ["past_ev"]
    ok("only the expired event is purged")

    # All past_ev data is gone...
    assert db.query(Attendee).filter_by(pretix_event="past_ev").count() == 0
    assert db.query(Ride).filter_by(event="past_ev").count() == 0
    assert db.query(RideRequest).filter_by(event="past_ev").count() == 0
    ok("expired event attendees, rides, requests deleted")

    # ...including the claims and offers that cascade from its rides/requests.
    assert db.query(RideClaim).count() == 1  # only future_ev's claim remains
    assert db.query(RideOffer).count() == 1  # only future_ev's offer remains
    ok("expired event claims and offers cascade-deleted")

    # future_ev is untouched.
    assert db.query(Attendee).filter_by(pretix_event="future_ev").count() == 2
    assert db.query(Ride).filter_by(event="future_ev").count() == 1
    assert db.query(RideRequest).filter_by(event="future_ev").count() == 1
    ok("not-yet-expired event is preserved")

    # An event Pretix can't resolve is left alone, never deleted.
    _seed_event(db, "unknown_ev")
    event_dates.clear()
    purged = await cleanup_expired_events(db, fake_pretix)
    assert purged == []
    assert db.query(Attendee).filter_by(pretix_event="unknown_ev").count() == 2
    ok("events with no Pretix data are never purged")

    db.close()


# ---------------------------------------------------------------------------
# Helper: create a client per user to avoid cookie conflicts
# ---------------------------------------------------------------------------

async def make_user_client(transport, pretix_mod, email_mod, event_mock, order: dict) -> AsyncClient:
    """Authenticate and return a client with the session cookie set."""
    client = AsyncClient(transport=transport, base_url="http://test")
    with patch.object(pretix_mod, "verify_order", new_callable=AsyncMock, return_value=order), \
         patch.object(pretix_mod, "get_event", new_callable=AsyncMock, return_value=event_mock), \
         patch.object(pretix_mod, "get_admission_item_ids", new_callable=AsyncMock, return_value=None), \
         patch.object(email_mod, "send"):
        r = await client.get(
            f"/auth/ev/{order['positions'][0]['attendee_name']}/{order['secret']}",
            follow_redirects=False,
        )
        assert r.status_code == 303
    return client


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

async def test_app():
    # Clean DB
    if os.path.exists("test_rideshare.db"):
        os.remove("test_rideshare.db")

    from main import app, pretix, email

    event_mock = {"date_from": "2026-09-15T09:00:00+02:00"}
    alice_order = {
        "secret": "alicesecret", "status": "p",
        "positions": [{"id": 1, "attendee_name": "Alice Driver", "attendee_email": "alice@test.com"}],
    }
    bob_order = {
        "secret": "bobsecret", "status": "p",
        "positions": [{"id": 2, "attendee_name": "Bob Rider", "attendee_email": "bob@test.com"}],
    }

    transport = ASGITransport(app=app)

    with patch.object(email, "send"):
        # Create per-user clients
        alice = await make_user_client(transport, pretix, email, event_mock, alice_order)
        bob = await make_user_client(transport, pretix, email, event_mock, bob_order)

        # -- Index page --
        anon = AsyncClient(transport=transport, base_url="http://test")
        r = await anon.get("/")
        assert r.status_code == 200 and "NLNOG" in r.text
        ok("index page")
        await anon.aclose()

        # -- Dashboard --
        r = await alice.get("/dashboard")
        assert r.status_code == 200 and "Alice Driver" in r.text
        ok("alice dashboard")

        # -- Offering without contact info is blocked --
        r = await alice.post("/rides/offer", data={
            "departure_location": "Amsterdam",
            "departure_time": "2026-09-15T08:00",
            "return_time": "2026-09-15T17:00",
            "seats": "3",
            "notes": "test ride",
        }, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/rides/offer"
        r = await alice.get("/dashboard")
        assert "Amsterdam" not in r.text
        ok("offer without contact info is blocked")

        # -- Alice offers a ride (with contact) --
        r = await alice.post("/rides/offer", data={
            "departure_location": "Amsterdam",
            "departure_time": "2026-09-15T08:00",
            "return_time": "2026-09-15T17:00",
            "direction": "round_trip",
            "seats": "3",
            "notes": "test ride",
            "contact_phone": "+31600000001",
        }, follow_redirects=False)
        assert r.status_code == 303
        ok("alice offers ride")

        r = await alice.get("/dashboard")
        assert "Amsterdam" in r.text and "(yours)" in r.text
        ok("dashboard shows alice ride")

        # -- Ride detail for alice (driver) --
        r = await alice.get("/rides/1")
        assert r.status_code == 200 and "Edit ride" in r.text
        ok("alice ride detail as driver")

        # -- Claiming without contact info is blocked --
        r = await bob.post("/rides/1/claim", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/profile"
        r = await bob.get("/rides/1")
        assert "pending driver approval" not in r.text
        ok("claim without contact info is blocked")

        # -- Profile save with no contact info is rejected --
        r = await bob.post("/profile", data={"phone": "", "notes": ""}, follow_redirects=False)
        assert r.status_code == 303
        r = await bob.get("/profile")
        assert "reach you" in r.text
        ok("empty profile save is rejected")

        # -- Bob sets his contact info --
        r = await bob.post("/profile", data={"phone": "+31600000002", "notes": ""}, follow_redirects=False)
        assert r.status_code == 303
        ok("bob sets contact info")

        # -- Bob requests a seat (pending) --
        r = await bob.post("/rides/1/claim", follow_redirects=False)
        assert r.status_code == 303
        ok("bob claims seat")

        r = await bob.get("/rides/1")
        assert "pending driver approval" in r.text
        assert "Cancel request" in r.text
        ok("bob sees pending status")

        # -- Alice sees pending request --
        r = await alice.get("/rides/1")
        assert "Pending requests" in r.text
        assert "Bob Rider" in r.text
        assert "Approve" in r.text
        ok("alice sees pending request")

        # -- Alice dashboard shows pending count --
        r = await alice.get("/dashboard")
        assert "pending seat request" in r.text
        ok("dashboard shows pending count")

        # -- Alice approves bob (extract claim ID from page) --
        r = await alice.get("/rides/1")
        claim_id = re.search(r"/rides/1/claims/(\d+)/approve", r.text).group(1)
        r = await alice.post(f"/rides/1/claims/{claim_id}/approve", follow_redirects=False)
        assert r.status_code == 303
        ok("alice approves bob")

        r = await bob.get("/rides/1")
        assert "pending driver approval" not in r.text
        assert "Confirmed passengers" in r.text
        ok("bob sees approved status")

        # -- Bob's dashboard shows 'joined' --
        r = await bob.get("/dashboard")
        assert "joined" in r.text
        ok("bob dashboard shows joined badge")

        # -- Bob unclaims --
        r = await bob.post("/rides/1/unclaim", follow_redirects=False)
        assert r.status_code == 303
        ok("bob unclaims")

        r = await bob.get("/rides/1")
        assert "Request a seat" in r.text
        ok("bob can request again after unclaim")

        # -- Reject flow: bob requests again, alice rejects --
        await bob.post("/rides/1/claim", follow_redirects=False)
        r = await alice.get("/rides/1")
        assert "Pending requests" in r.text
        ok("pending shows after re-claim")

        # Extract the new claim ID from the page
        claim_id = re.search(r"/rides/1/claims/(\d+)/reject", r.text).group(1)
        r = await alice.post(f"/rides/1/claims/{claim_id}/reject", follow_redirects=False)
        assert r.status_code == 303
        ok("alice rejects bob")

        r = await bob.get("/rides/1")
        assert "not approved" in r.text
        ok("bob sees rejection")

        r = await bob.get("/dashboard")
        assert "rejected" in r.text
        ok("bob dashboard shows rejected badge")

        # -- Ride request + offer flow --
        r = await bob.post("/requests/new", data={
            "location": "Rotterdam",
            "departure_time": "2026-09-15T07:00",
            "direction": "to_event",
            "notes": "need a lift",
            "contact_phone": "+31600000002",
        }, follow_redirects=False)
        assert r.status_code == 303
        ok("bob posts ride request")

        # Alice offers ride to bob's request
        r = await alice.post("/requests/1/offer", follow_redirects=False)
        assert r.status_code == 303
        ok("alice offers to bob request")

        r = await alice.get("/dashboard")
        assert "Offered" in r.text
        ok("alice dashboard shows offered status")

        # Bob sees offer
        r = await bob.get("/requests/1")
        assert r.status_code == 200
        assert "Alice Driver" in r.text
        assert "Accept" in r.text
        ok("bob sees offer on request detail")

        # Bob accepts offer (extract offer ID)
        offer_id = re.search(r"/requests/1/offers/(\d+)/approve", r.text).group(1)
        r = await bob.post(f"/requests/1/offers/{offer_id}/approve", follow_redirects=False)
        assert r.status_code == 303
        assert "/rides/1" in r.headers["location"]
        ok("bob accepts offer")

        # Bob is now an approved passenger
        r = await bob.get("/rides/1")
        assert "Confirmed passengers" in r.text
        assert "Bob Rider" in r.text
        ok("bob is confirmed after accepting offer")

        # -- Profile update --
        r = await alice.post("/profile", data={
            "phone": "+31612345678",
            "notes": "IRC: alice",
        }, follow_redirects=False)
        assert r.status_code == 303
        ok("profile update")

        r = await alice.get("/profile")
        assert "+31612345678" in r.text
        ok("profile shows updated phone")

        # -- Edit ride --
        r = await alice.post("/rides/1/edit", data={
            "departure_location": "Amsterdam Centraal",
            "departure_time": "2026-09-15T08:30",
            "return_time": "2026-09-15T17:30",
            "direction": "round_trip",
            "seats": "4",
            "notes": "updated",
            "contact_phone": "+31612345678",
        }, follow_redirects=False)
        assert r.status_code == 303
        ok("edit ride")

        r = await alice.get("/rides/1")
        assert "Amsterdam Centraal" in r.text
        ok("ride shows updated details")

        # -- Can't reduce seats below approved --
        r = await alice.post("/rides/1/edit", data={
            "departure_location": "Amsterdam Centraal",
            "departure_time": "2026-09-15T08:30",
            "seats": "0",
            "notes": "",
        }, follow_redirects=False)
        assert r.status_code == 303  # redirects back to edit with error
        ok("can't reduce seats below approved count")

        # -- Delete ride --
        r = await alice.post("/rides/1/delete", follow_redirects=False)
        assert r.status_code == 303
        ok("delete ride")

        r = await alice.get("/dashboard")
        assert "Amsterdam" not in r.text
        ok("ride removed from dashboard")

        # -- One-way rides and direction compatibility --
        # Alice offers a one-way ride leaving the event
        r = await alice.post("/rides/offer", data={
            "departure_location": "Utrecht",
            "departure_time": "2026-09-15T18:00",
            "return_time": "2026-09-15T20:00",
            "direction": "from_event",
            "seats": "2",
            "contact_phone": "+31612345678",
        }, follow_redirects=False)
        assert r.status_code == 303
        ok("alice offers one-way ride")

        r = await alice.get("/dashboard")
        ride_id = re.search(r"/rides/(\d+)", r.text).group(1)
        r = await alice.get(f"/rides/{ride_id}")
        assert "From the event only" in r.text
        assert "Return time" not in r.text
        ok("one-way ride hides return time")

        # Bob posts a request to the event (incompatible with a from-event ride)
        r = await bob.post("/requests/new", data={
            "location": "Den Haag",
            "direction": "to_event",
            "contact_phone": "+31600000002",
        }, follow_redirects=False)
        assert r.status_code == 303
        r = await bob.get("/dashboard")
        req_id = re.search(r"/requests/(\d+)", r.text).group(1)
        ok("bob posts a to-event request")

        # Alice's from-event ride can't offer to a to-event request
        r = await alice.get("/dashboard")
        assert "Offer ride" not in r.text
        ok("incompatible request hides offer button")

        r = await alice.post(f"/requests/{req_id}/offer", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard"
        r = await bob.get(f"/requests/{req_id}")
        assert "No ride offers yet" in r.text
        ok("incompatible direction offer is blocked")

        # -- Logout --
        r = await alice.get("/logout", follow_redirects=False)
        assert r.status_code == 303
        ok("logout")

        r = await alice.get("/dashboard", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        ok("logged out user redirected")

        await alice.aclose()
        await bob.aclose()

    # Clean up
    if os.path.exists("test_rideshare.db"):
        os.remove("test_rideshare.db")


# ---------------------------------------------------------------------------
# Non-admission product tests
# ---------------------------------------------------------------------------

async def test_non_admission_positions():
    """Non-admission products (add-ons, donations) must not become attendees."""
    from main import app, email, engine, pretix

    # test_app() unlinks the shared DB file at its end, leaving the engine with
    # stale pooled connections; reset to a fresh schema before exercising auth.
    engine.dispose()
    if os.path.exists("test_rideshare.db"):
        os.remove("test_rideshare.db")
    Base.metadata.create_all(engine)

    event_mock = {"date_from": "2026-09-15T09:00:00+02:00"}
    transport = ASGITransport(app=app)

    # Item 10 grants admission; item 11 (donation/add-on) does not.
    admission_ids = {10}

    # One admission ticket + one donation on the same order.
    single = {
        "secret": "sec1", "status": "p",
        "positions": [
            {"id": 1, "item": 10, "attendee_name": "Real Attendee", "attendee_email": "real@test.com"},
            {"id": 2, "item": 11, "attendee_name": None, "attendee_email": None},
        ],
    }

    with patch.object(pretix, "verify_order", new_callable=AsyncMock, return_value=single), \
         patch.object(pretix, "get_event", new_callable=AsyncMock, return_value=event_mock), \
         patch.object(pretix, "get_admission_item_ids", new_callable=AsyncMock, return_value=admission_ids), \
         patch.object(email, "send"):
        client = AsyncClient(transport=transport, base_url="http://test")
        # Only one admission position remains, so we log straight in instead of
        # showing the attendee picker (which would mean the donation counted).
        r = await client.get("/auth/ev/CODE/sec1", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard", r.headers.get("location")
        ok("donation position is not treated as an extra attendee")
        await client.aclose()

    # Two admission tickets + one donation -> picker shows exactly two people.
    multi = {
        "secret": "sec2", "status": "p",
        "positions": [
            {"id": 1, "item": 10, "attendee_name": "Alice", "attendee_email": "alice@test.com"},
            {"id": 2, "item": 10, "attendee_name": "Bob", "attendee_email": "bob@test.com"},
            {"id": 3, "item": 11, "attendee_name": None, "attendee_email": None},
        ],
    }

    with patch.object(pretix, "verify_order", new_callable=AsyncMock, return_value=multi), \
         patch.object(pretix, "get_event", new_callable=AsyncMock, return_value=event_mock), \
         patch.object(pretix, "get_admission_item_ids", new_callable=AsyncMock, return_value=admission_ids), \
         patch.object(email, "send"):
        client = AsyncClient(transport=transport, base_url="http://test")
        r = await client.get("/auth/ev/CODE/sec2", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/auth/pick", r.headers.get("location")
        r = await client.get("/auth/pick")
        assert r.text.count('name="position_id"') == 2, r.text.count('name="position_id"')
        assert "Alice" in r.text and "Bob" in r.text
        ok("attendee picker excludes the donation position")
        await client.aclose()

    engine.dispose()
    if os.path.exists("test_rideshare.db"):
        os.remove("test_rideshare.db")


# ---------------------------------------------------------------------------
# Auth link tests
# ---------------------------------------------------------------------------

def test_auth_link_parsing():
    """Both Pretix link formats resolve to a structured auth route."""
    from main import _parse_pretix_link, _position_by_number
    from pretix import PretixClient

    assert _parse_pretix_link(
        "https://pretix.eu/nlnog/nlnogday2026/order/AMBV9/3rpcz29wtm69eblc/"
    ) == ("nlnogday2026", "AMBV9", "", "3rpcz29wtm69eblc")
    assert _parse_pretix_link(
        "https://pretix.eu/nlnog/nlnogday2026/order/AMBV9/3rpcz29wtm69eblc/open/abc123/"
    ) == ("nlnogday2026", "AMBV9", "", "3rpcz29wtm69eblc")
    ok("order links parse")

    # Ticket link: the position number sits between the code and the secret.
    assert _parse_pretix_link(
        "https://pretix.eu/nlnog/nlnogday2026/ticket/AMBV9/1/3rpcz29wtm69eblc/"
    ) == ("nlnogday2026", "AMBV9", "1", "3rpcz29wtm69eblc")
    ok("ticket links parse")

    assert _parse_pretix_link("https://pretix.eu/nlnog/nlnogday2026/order/AMBV9/") is None
    assert _parse_pretix_link("https://pretix.eu/nlnog/nlnogday2026/ticket/AMBV9/1/") is None
    assert _parse_pretix_link("https://example.com/hello/") is None
    assert _parse_pretix_link("") is None
    ok("non-order links are rejected")

    # Ticket links number positions with Pretix's per-order positionid, which is
    # not the global id we store on an attendee.
    positions = [{"id": 41, "positionid": 1}, {"id": 42, "positionid": 2}]
    assert _position_by_number(positions, "2")["id"] == 42
    assert _position_by_number(positions, "42") is None
    assert _position_by_number(positions, "") is None
    ok("ticket position number resolves via positionid")

    # The ticket shop base is derived from the API base unless configured.
    assert PretixClient("https://pretix.eu/api/v1", "t", "nlnog").presale_url == "https://pretix.eu"
    assert PretixClient(
        "https://tickets.example.com/api/v1", "t", "nlnog",
        presale_url="https://shop.example.com/",
    ).presale_url == "https://shop.example.com"
    ok("ticket shop base URL is derived from the API base")


async def test_verify_ticket():
    """A ticket link is validated by the ticket shop, then read from the API."""
    from pretix import PretixClient

    client = PretixClient("https://pretix.eu/api/v1", "token", "nlnog")
    order = {"code": "AMBV9", "secret": "ordersecret", "status": "p", "positions": []}
    requested: list[str] = []

    class FakeResponse:
        def __init__(self, status_code: int, url: str):
            self.status_code = status_code
            self.url = httpx.URL(url)

    def fake_shop(status_code: int, final_url: str | None = None):
        """Patch the shop request; `final_url` fakes a redirect somewhere else."""
        async def _get(self, url, **kwargs):
            requested.append(str(url))
            return FakeResponse(status_code, final_url or str(url))
        return patch.object(httpx.AsyncClient, "get", _get)

    with fake_shop(200), \
         patch.object(PretixClient, "get_order", new_callable=AsyncMock, return_value=order):
        assert await client.verify_ticket("nlnogday2026", "AMBV9", "1", "3rpcz29wtm69eblc") == order
        assert requested == [
            "https://pretix.eu/nlnog/nlnogday2026/ticket/AMBV9/1/3rpcz29wtm69eblc/"
        ], requested
        ok("valid ticket link verifies against the ticket shop")

    # A wrong secret 404s on the shop, and must never reach the order at all.
    get_order = AsyncMock(return_value=order)
    with fake_shop(404), patch.object(PretixClient, "get_order", get_order):
        assert await client.verify_ticket("nlnogday2026", "AMBV9", "1", "wrongsecret") is None
        assert get_order.await_count == 0
        ok("ticket link with a bad secret is rejected")

    # A shop that redirects away (password gate) can answer 200 for any secret.
    with fake_shop(200, "https://pretix.eu/nlnog/nlnogday2026/unlock/"), \
         patch.object(PretixClient, "get_order", new_callable=AsyncMock, return_value=order):
        assert await client.verify_ticket("nlnogday2026", "AMBV9", "1", "anything") is None
        ok("a 200 from somewhere other than the ticket page is not proof")

    # The secret and position land in a URL path, so both are charset-checked.
    get_order = AsyncMock(return_value=order)
    with fake_shop(200), patch.object(PretixClient, "get_order", get_order):
        assert await client.verify_ticket("nlnogday2026", "AMBV9", "1", "../../evil") is None
        assert await client.verify_ticket("nlnogday2026", "AMBV9", "..", "secret") is None
        # Starlette decodes %2F in a path segment, so the code is checked too.
        assert await client.verify_ticket("nlnogday2026", "../../x", "1", "secret") is None
        assert await client.verify_ticket("nlnogday2026", "AMBV9", "", "") is None
        assert get_order.await_count == 0
        ok("malformed position and secret never reach the shop")

    # The position secret the API does expose is the door barcode, not a
    # credential: an order link carrying it must not log anyone in.
    barcode = {
        "code": "AMBV9", "secret": "ordersecret", "status": "p",
        "positions": [{"id": 1, "positionid": 1, "secret": "barcodesecret"}],
    }
    with patch.object(PretixClient, "get_order", new_callable=AsyncMock, return_value=barcode):
        assert await client.verify_order("nlnogday2026", "AMBV9", "ordersecret") == barcode
        assert await client.verify_order("nlnogday2026", "AMBV9", "barcodesecret") is None
        assert await client.verify_order("nlnogday2026", "AMBV9", "") is None
        ok("only the order secret authenticates an order link")


async def test_auth_link_login():
    """A ticket link logs its own attendee in without showing the picker."""
    from main import SessionLocal, app, email, engine, pretix

    engine.dispose()
    if os.path.exists("test_rideshare.db"):
        os.remove("test_rideshare.db")
    Base.metadata.create_all(engine)

    event_mock = {"date_from": "2026-09-15T09:00:00+02:00"}
    transport = ASGITransport(app=app)

    # Group order: two attendees, so an order link would show the picker.
    group = {
        "secret": "ordersecret", "status": "p",
        "positions": [
            {"id": 41, "positionid": 1, "attendee_name": "Alice", "attendee_email": "alice@test.com"},
            {"id": 42, "positionid": 2, "attendee_name": "Bob", "attendee_email": "bob@test.com"},
        ],
    }

    with patch.object(pretix, "verify_ticket", new_callable=AsyncMock, return_value=group), \
         patch.object(pretix, "verify_order", new_callable=AsyncMock, return_value=group), \
         patch.object(pretix, "get_event", new_callable=AsyncMock, return_value=event_mock), \
         patch.object(pretix, "get_admission_item_ids", new_callable=AsyncMock, return_value=None), \
         patch.object(email, "send"):
        client = AsyncClient(transport=transport, base_url="http://test")

        r = await client.get(
            "/auth?order=https://pretix.eu/nlnog/ev/ticket/AMBV9/2/bobsecret/",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/auth/ev/AMBV9/2/bobsecret", r.headers.get("location")
        ok("/auth redirects a ticket link to the ticket auth route")

        r = await client.get(r.headers["location"], follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard", r.headers.get("location")
        r = await client.get("/dashboard")
        assert "Bob" in r.text and "Alice" not in r.text
        ok("ticket link logs in its own attendee, skipping the picker")
        await client.aclose()

        # Notification emails are built from the stored secret, so the ticket
        # login must have stored the order secret, not the link's own.
        db = SessionLocal()
        bob = db.query(Attendee).filter_by(pretix_position_id=42).one()
        assert bob.pretix_order_secret == "ordersecret", bob.pretix_order_secret
        assert email.auth_url(bob, "/dashboard").startswith(
            f"{email.base_url}/auth/ev/AMBV9/ordersecret?redirect="
        ), email.auth_url(bob, "/dashboard")
        db.close()
        ok("ticket login stores the order secret for emailed auth links")

        # The order link for the same group order still shows the picker.
        picker = AsyncClient(transport=transport, base_url="http://test")
        r = await picker.get("/auth/ev/AMBV9/ordersecret", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/auth/pick", r.headers.get("location")
        ok("order link for a group order still shows the picker")
        await picker.aclose()

    # An unverifiable ticket link is turned away.
    with patch.object(pretix, "verify_ticket", new_callable=AsyncMock, return_value=None), \
         patch.object(email, "send"):
        client = AsyncClient(transport=transport, base_url="http://test")
        r = await client.get("/auth/ev/AMBV9/2/wrongsecret", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", r.headers.get("location")
        r = await client.get("/")
        assert "Invalid or expired ticket link." in r.text
        ok("rejected ticket link flashes an error")
        await client.aclose()

    engine.dispose()
    if os.path.exists("test_rideshare.db"):
        os.remove("test_rideshare.db")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n=== Model Tests ===")
    test_models()

    print("\n=== Cleanup Tests ===")
    asyncio.run(test_cleanup())

    print("\n=== Integration Tests ===")
    asyncio.run(test_app())

    print("\n=== Non-Admission Product Tests ===")
    asyncio.run(test_non_admission_positions())

    print("\n=== Auth Link Tests ===")
    test_auth_link_parsing()
    asyncio.run(test_verify_ticket())
    asyncio.run(test_auth_link_login())

    print("\n=== Direction Tests ===")
    test_directions()

    print(f"\n{'=' * 40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'=' * 40}")
    sys.exit(1 if failed else 0)
