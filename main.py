from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from email_service import EmailService
from models import Attendee, Base, Ride, RideClaim, RideOffer, RideRequest
from pretix import PretixClient

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="NLNOG Ride Share")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "change-me"),
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

engine = create_engine(
    os.getenv("DATABASE_URL", "sqlite:///./rideshare.db"),
    connect_args={"check_same_thread": False},
)
Base.metadata.create_all(engine)

# Migrate existing ride_claims to include status column
with engine.connect() as conn:
    result = conn.execute(text("PRAGMA table_info(ride_claims)"))
    columns = [row[1] for row in result]
    if "status" not in columns:
        conn.execute(text(
            "ALTER TABLE ride_claims ADD COLUMN status VARCHAR NOT NULL DEFAULT 'approved'"
        ))
        conn.commit()

SessionLocal = sessionmaker(bind=engine)

pretix = PretixClient(
    api_url=os.getenv("PRETIX_API_URL", "https://pretix.eu/api/v1"),
    api_token=os.getenv("PRETIX_API_TOKEN", ""),
    organizer=os.getenv("PRETIX_ORGANIZER", ""),
)

email = EmailService()


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Attendee | None:
    attendee_id = request.session.get("attendee_id")
    if not attendee_id:
        return None
    return db.get(Attendee, attendee_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_attendee(db: Session, event: str, code: str, position: dict) -> Attendee:
    attendee = (
        db.query(Attendee)
        .filter_by(
            pretix_event=event,
            pretix_order_code=code,
            pretix_position_id=position["id"],
        )
        .first()
    )
    if not attendee:
        attendee = Attendee(
            pretix_event=event,
            pretix_order_code=code,
            pretix_position_id=position["id"],
            name=position.get("attendee_name") or "Attendee",
            email=position.get("attendee_email") or "",
        )
        db.add(attendee)
    else:
        attendee.name = position.get("attendee_name") or attendee.name
        attendee.email = position.get("attendee_email") or attendee.email
    db.commit()
    db.refresh(attendee)
    return attendee


def _flash(request: Request, message: str, category: str = "info"):
    request.session.setdefault("_flashes", []).append({"message": message, "category": category})


def _get_flashes(request: Request) -> list[dict]:
    return request.session.pop("_flashes", [])


def _tpl(request: Request, name: str, ctx: dict, db: Session | None = None) -> HTMLResponse:
    """Render a template with common context injected."""
    user = None
    if db:
        user = get_current_user(request, db)
    event_date = request.session.get("event_date", "")
    ctx.update({"user": user, "flashes": _get_flashes(request), "event_date": event_date})
    return templates.TemplateResponse(request, name, ctx)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return _tpl(request, "index.html", {}, db)


@app.get("/auth/{event}/{code}/{secret}")
async def auth(event: str, code: str, secret: str, request: Request, db: Session = Depends(get_db)):
    order = await pretix.verify_order(event, code, secret)
    if not order:
        _flash(request, "Invalid or expired ticket link.", "error")
        return RedirectResponse(url="/", status_code=303)

    positions = order.get("positions", [])
    if not positions:
        _flash(request, "No attendees found on this order.", "error")
        return RedirectResponse(url="/", status_code=303)

    event_data = await pretix.get_event(event)
    event_date = ""
    if event_data and event_data.get("date_from"):
        event_date = event_data["date_from"][:10]

    if len(positions) == 1:
        attendee = _get_or_create_attendee(db, event, code, positions[0])
        request.session["attendee_id"] = attendee.id
        request.session["event"] = event
        request.session["event_date"] = event_date
        return RedirectResponse(url="/dashboard", status_code=303)

    request.session["_pending"] = {
        "event": event,
        "code": code,
        "event_date": event_date,
        "positions": [
            {
                "id": p["id"],
                "name": p.get("attendee_name") or "Attendee",
                "email": p.get("attendee_email") or "",
            }
            for p in positions
        ],
    }
    return RedirectResponse(url="/auth/pick", status_code=303)


@app.get("/auth/pick", response_class=HTMLResponse)
def auth_pick(request: Request, db: Session = Depends(get_db)):
    pending = request.session.get("_pending")
    if not pending:
        return RedirectResponse(url="/", status_code=303)
    return _tpl(request, "pick_attendee.html", {"positions": pending["positions"]}, db)


@app.post("/auth/pick")
def auth_pick_submit(request: Request, position_id: int = Form(...), db: Session = Depends(get_db)):
    pending = request.session.get("_pending")
    if not pending:
        return RedirectResponse(url="/", status_code=303)

    pos = next((p for p in pending["positions"] if p["id"] == position_id), None)
    if not pos:
        raise HTTPException(status_code=400, detail="Invalid position")

    attendee = _get_or_create_attendee(db, pending["event"], pending["code"], pos)
    request.session["attendee_id"] = attendee.id
    request.session["event"] = pending["event"]
    request.session["event_date"] = pending.get("event_date", "")
    request.session.pop("_pending", None)
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    event = request.session.get("event")
    rides = (
        db.query(Ride)
        .filter_by(event=event)
        .order_by(Ride.departure_time)
        .all()
    )
    ride_requests = (
        db.query(RideRequest)
        .filter_by(event=event)
        .order_by(RideRequest.created_at.desc())
        .all()
    )

    my_claim_statuses = {
        c.ride_id: c.status
        for c in db.query(RideClaim).filter_by(passenger_id=user.id).all()
    }

    my_ride = db.query(Ride).filter_by(event=event, driver_id=user.id).first()

    # IDs of requests this user has already offered to
    my_offered_request_ids = set()
    if my_ride:
        my_offered_request_ids = {
            o.request_id
            for o in db.query(RideOffer).filter_by(ride_id=my_ride.id).all()
        }

    return _tpl(request, "dashboard.html", {
        "event": event,
        "rides": rides,
        "ride_requests": ride_requests,
        "my_claim_statuses": my_claim_statuses,
        "my_ride": my_ride,
        "my_offered_request_ids": my_offered_request_ids,
    }, db)


# ---------------------------------------------------------------------------
# Rides
# ---------------------------------------------------------------------------

@app.get("/rides/offer", response_class=HTMLResponse)
def offer_ride_form(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    event = request.session.get("event")
    existing = db.query(Ride).filter_by(event=event, driver_id=user.id).first()
    if existing:
        return RedirectResponse(url=f"/rides/{existing.id}/edit", status_code=303)

    return _tpl(request, "offer_ride.html", {"ride": None}, db)


@app.post("/rides/offer")
def offer_ride_submit(
    request: Request,
    departure_location: str = Form(...),
    departure_time: str = Form(...),
    return_time: str = Form(""),
    seats: int = Form(1),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    event = request.session.get("event")
    existing = db.query(Ride).filter_by(event=event, driver_id=user.id).first()
    if existing:
        _flash(request, "You already have a ride for this event. You can edit it instead.", "error")
        return RedirectResponse(url=f"/rides/{existing.id}/edit", status_code=303)

    ride = Ride(
        event=event,
        driver_id=user.id,
        departure_location=departure_location,
        departure_time=datetime.fromisoformat(departure_time),
        return_time=datetime.fromisoformat(return_time) if return_time else None,
        seats=seats,
        notes=notes,
    )
    db.add(ride)
    db.commit()
    _flash(request, "Ride offered!", "success")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/rides/{ride_id}/edit", response_class=HTMLResponse)
def edit_ride_form(ride_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    ride = db.get(Ride, ride_id)
    if not ride or ride.driver_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    return _tpl(request, "offer_ride.html", {"ride": ride}, db)


@app.post("/rides/{ride_id}/edit")
def edit_ride_submit(
    ride_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    departure_location: str = Form(...),
    departure_time: str = Form(...),
    return_time: str = Form(""),
    seats: int = Form(1),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    ride = db.get(Ride, ride_id)
    if not ride or ride.driver_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    if seats < len(ride.approved_claims):
        _flash(request, f"Can't reduce to {seats} seats - {len(ride.approved_claims)} already confirmed.", "error")
        return RedirectResponse(url=f"/rides/{ride_id}/edit", status_code=303)

    ride.departure_location = departure_location
    ride.departure_time = datetime.fromisoformat(departure_time)
    ride.return_time = datetime.fromisoformat(return_time) if return_time else None
    ride.seats = seats
    ride.notes = notes
    db.commit()

    # Notify approved passengers of changes
    for claim in ride.approved_claims:
        background_tasks.add_task(
            email.notify_ride_edited,
            claim.passenger.email, claim.passenger.name,
            user.name, ride,
        )

    _flash(request, "Ride updated!", "success")
    return RedirectResponse(url=f"/rides/{ride_id}", status_code=303)


@app.get("/rides/{ride_id}", response_class=HTMLResponse)
def ride_detail(ride_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    ride = db.get(Ride, ride_id)
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")

    is_driver = ride.driver_id == user.id
    my_claim = next((c for c in ride.claims if c.passenger_id == user.id), None)
    is_approved_passenger = my_claim is not None and my_claim.status == "approved"

    # Contact info only visible to driver and approved passengers
    show_contact = is_driver or is_approved_passenger

    return _tpl(request, "ride_detail.html", {
        "ride": ride,
        "is_driver": is_driver,
        "my_claim": my_claim,
        "show_contact": show_contact,
    }, db)


@app.post("/rides/{ride_id}/claim")
def claim_ride(
    ride_id: int, request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    ride = db.get(Ride, ride_id)
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")

    if ride.driver_id == user.id:
        _flash(request, "You can't claim your own ride.", "error")
        return RedirectResponse(url=f"/rides/{ride_id}", status_code=303)

    if ride.seats_available <= 0:
        _flash(request, "No seats available.", "error")
        return RedirectResponse(url=f"/rides/{ride_id}", status_code=303)

    existing = (
        db.query(RideClaim)
        .filter_by(ride_id=ride_id, passenger_id=user.id)
        .first()
    )
    if existing:
        _flash(request, "You already have a request for this ride.", "error")
        return RedirectResponse(url=f"/rides/{ride_id}", status_code=303)

    claim = RideClaim(ride_id=ride_id, passenger_id=user.id, status="pending")
    db.add(claim)
    db.commit()

    background_tasks.add_task(
        email.notify_seat_requested,
        ride.driver.email, ride.driver.name,
        user.name, ride,
    )

    _flash(request, "Seat requested! The driver will be notified.", "success")
    return RedirectResponse(url=f"/rides/{ride_id}", status_code=303)


@app.post("/rides/{ride_id}/claims/{claim_id}/approve")
def approve_claim(
    ride_id: int, claim_id: int, request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    ride = db.get(Ride, ride_id)
    if not ride or ride.driver_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    claim = db.get(RideClaim, claim_id)
    if not claim or claim.ride_id != ride_id or claim.status != "pending":
        raise HTTPException(status_code=400, detail="Invalid claim")

    if ride.seats_available <= 0:
        _flash(request, "No seats available. Increase your seat count first.", "error")
        return RedirectResponse(url=f"/rides/{ride_id}", status_code=303)

    claim.status = "approved"
    db.commit()

    background_tasks.add_task(
        email.notify_seat_approved,
        claim.passenger.email, claim.passenger.name,
        user.name, ride,
    )

    _flash(request, f"{claim.passenger.name} approved!", "success")
    return RedirectResponse(url=f"/rides/{ride_id}", status_code=303)


@app.post("/rides/{ride_id}/claims/{claim_id}/reject")
def reject_claim(
    ride_id: int, claim_id: int, request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    ride = db.get(Ride, ride_id)
    if not ride or ride.driver_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    claim = db.get(RideClaim, claim_id)
    if not claim or claim.ride_id != ride_id or claim.status != "pending":
        raise HTTPException(status_code=400, detail="Invalid claim")

    claim.status = "rejected"
    db.commit()

    background_tasks.add_task(
        email.notify_seat_rejected,
        claim.passenger.email, claim.passenger.name,
        user.name, ride,
    )

    _flash(request, f"{claim.passenger.name} rejected.", "success")
    return RedirectResponse(url=f"/rides/{ride_id}", status_code=303)


@app.post("/rides/{ride_id}/unclaim")
def unclaim_ride(
    ride_id: int, request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    claim = (
        db.query(RideClaim)
        .filter_by(ride_id=ride_id, passenger_id=user.id)
        .first()
    )
    if claim:
        was_approved = claim.status == "approved"
        ride = claim.ride
        db.delete(claim)
        db.commit()

        if was_approved:
            background_tasks.add_task(
                email.notify_passenger_left,
                ride.driver.email, ride.driver.name,
                user.name, ride,
            )
            _flash(request, "You left the ride.", "success")
        else:
            _flash(request, "Request cancelled.", "success")

    return RedirectResponse(url=f"/rides/{ride_id}", status_code=303)


@app.post("/rides/{ride_id}/delete")
def delete_ride(
    ride_id: int, request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    ride = db.get(Ride, ride_id)
    if not ride or ride.driver_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Notify all passengers before cascade delete
    departure_location = ride.departure_location
    driver_name = user.name
    for claim in ride.claims:
        if claim.status in ("pending", "approved"):
            background_tasks.add_task(
                email.notify_ride_cancelled,
                claim.passenger.email, claim.passenger.name,
                driver_name, departure_location,
            )

    db.delete(ride)
    db.commit()
    _flash(request, "Ride deleted.", "success")
    return RedirectResponse(url="/dashboard", status_code=303)


# ---------------------------------------------------------------------------
# Ride requests
# ---------------------------------------------------------------------------

@app.get("/requests/new", response_class=HTMLResponse)
def request_ride_form(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    return _tpl(request, "request_ride.html", {}, db)


@app.post("/requests/new")
def request_ride_submit(
    request: Request,
    location: str = Form(...),
    departure_time: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    rr = RideRequest(
        event=request.session.get("event"),
        requester_id=user.id,
        location=location,
        departure_time=datetime.fromisoformat(departure_time) if departure_time else None,
        notes=notes,
    )
    db.add(rr)
    db.commit()
    _flash(request, "Ride request posted!", "success")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/requests/{request_id}", response_class=HTMLResponse)
def request_detail(request_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    rr = db.get(RideRequest, request_id)
    if not rr:
        raise HTTPException(status_code=404, detail="Request not found")

    is_requester = rr.requester_id == user.id

    return _tpl(request, "request_detail.html", {
        "rr": rr,
        "is_requester": is_requester,
    }, db)


@app.post("/requests/{request_id}/offer")
def offer_ride_to_request(
    request_id: int, request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    rr = db.get(RideRequest, request_id)
    if not rr:
        raise HTTPException(status_code=404, detail="Request not found")

    if rr.requester_id == user.id:
        _flash(request, "You can't offer a ride to yourself.", "error")
        return RedirectResponse(url="/dashboard", status_code=303)

    event = request.session.get("event")
    my_ride = db.query(Ride).filter_by(event=event, driver_id=user.id).first()
    if not my_ride:
        _flash(request, "You need to offer a ride first.", "error")
        return RedirectResponse(url="/dashboard", status_code=303)

    if my_ride.seats_available <= 0:
        _flash(request, "Your ride has no seats available.", "error")
        return RedirectResponse(url="/dashboard", status_code=303)

    existing = (
        db.query(RideOffer)
        .filter_by(ride_id=my_ride.id, request_id=request_id)
        .first()
    )
    if existing:
        _flash(request, "You already offered a ride for this request.", "error")
        return RedirectResponse(url="/dashboard", status_code=303)

    offer = RideOffer(ride_id=my_ride.id, request_id=request_id, status="pending")
    db.add(offer)
    db.commit()

    background_tasks.add_task(
        email.notify_ride_offered,
        rr.requester.email, rr.requester.name,
        user.name, my_ride, request_id,
    )

    _flash(request, "Offer sent! The requester will be notified.", "success")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/requests/{request_id}/offers/{offer_id}/approve")
def approve_offer(
    request_id: int, offer_id: int, request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    rr = db.get(RideRequest, request_id)
    if not rr or rr.requester_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    offer = db.get(RideOffer, offer_id)
    if not offer or offer.request_id != request_id or offer.status != "pending":
        raise HTTPException(status_code=400, detail="Invalid offer")

    ride = offer.ride
    if ride.seats_available <= 0:
        _flash(request, "This ride has no seats available anymore.", "error")
        return RedirectResponse(url=f"/requests/{request_id}", status_code=303)

    # Accept the offer: create or update a claim and delete the request
    offer.status = "approved"
    existing_claim = (
        db.query(RideClaim)
        .filter_by(ride_id=ride.id, passenger_id=user.id)
        .first()
    )
    if existing_claim:
        existing_claim.status = "approved"
    else:
        db.add(RideClaim(ride_id=ride.id, passenger_id=user.id, status="approved"))

    # Reject all other pending offers on this request
    for other in rr.offers:
        if other.id != offer_id and other.status == "pending":
            other.status = "rejected"
            background_tasks.add_task(
                email.notify_offer_declined,
                other.ride.driver.email, other.ride.driver.name,
                user.name, other.ride,
            )

    db.delete(rr)
    db.commit()

    background_tasks.add_task(
        email.notify_offer_accepted,
        ride.driver.email, ride.driver.name,
        user.name, ride,
    )

    _flash(request, "Offer accepted! You're on the ride.", "success")
    return RedirectResponse(url=f"/rides/{ride.id}", status_code=303)


@app.post("/requests/{request_id}/offers/{offer_id}/reject")
def reject_offer(
    request_id: int, offer_id: int, request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    rr = db.get(RideRequest, request_id)
    if not rr or rr.requester_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    offer = db.get(RideOffer, offer_id)
    if not offer or offer.request_id != request_id or offer.status != "pending":
        raise HTTPException(status_code=400, detail="Invalid offer")

    offer.status = "rejected"
    db.commit()

    background_tasks.add_task(
        email.notify_offer_declined,
        offer.ride.driver.email, offer.ride.driver.name,
        user.name, offer.ride,
    )

    _flash(request, "Offer declined.", "success")
    return RedirectResponse(url=f"/requests/{request_id}", status_code=303)


@app.post("/requests/{request_id}/delete")
def delete_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    rr = db.get(RideRequest, request_id)
    if not rr or rr.requester_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    db.delete(rr)
    db.commit()
    _flash(request, "Request deleted.", "success")
    return RedirectResponse(url="/dashboard", status_code=303)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@app.get("/profile", response_class=HTMLResponse)
def profile(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    return _tpl(request, "profile.html", {}, db)


@app.post("/profile")
def profile_update(
    request: Request,
    phone: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/", status_code=303)

    user.phone = phone
    user.notes = notes
    db.commit()
    _flash(request, "Profile updated.", "success")
    return RedirectResponse(url="/profile", status_code=303)
