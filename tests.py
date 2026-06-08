"""Tests for the NLNOG Ride Share app."""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

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

        # -- Alice offers a ride --
        r = await alice.post("/rides/offer", data={
            "departure_location": "Amsterdam",
            "departure_time": "2026-09-15T08:00",
            "return_time": "2026-09-15T17:00",
            "seats": "3",
            "notes": "test ride",
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
            "notes": "need a lift",
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
            "seats": "4",
            "notes": "updated",
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

if __name__ == "__main__":
    print("\n=== Model Tests ===")
    test_models()

    print("\n=== Cleanup Tests ===")
    asyncio.run(test_cleanup())

    print("\n=== Integration Tests ===")
    asyncio.run(test_app())

    print(f"\n{'=' * 40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'=' * 40}")
    sys.exit(1 if failed else 0)
