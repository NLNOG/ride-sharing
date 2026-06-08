"""Retention cleanup for finished events.

All data tied to a Pretix event (attendees, rides, requests, claims, offers) is
deleted once the event has been over for `RETENTION_DAYS` days. Event dates are
not stored locally, so they're fetched from Pretix at cleanup time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from models import Attendee, Ride, RideRequest
from pretix import PretixClient

logger = logging.getLogger("cleanup")

# Days to keep an event's data after it ends.
RETENTION_DAYS = 4


def _event_slugs(db: Session) -> set[str]:
    """Collect every event slug referenced anywhere in the database."""
    slugs: set[str] = set()
    slugs.update(s for (s,) in db.query(Attendee.pretix_event).distinct())
    slugs.update(s for (s,) in db.query(Ride.event).distinct())
    slugs.update(s for (s,) in db.query(RideRequest.event).distinct())
    return {s for s in slugs if s}


def _event_end(event_data: dict) -> datetime | None:
    """Return when the event ends: date_to if set, else date_from.

    Pretix timestamps are timezone-aware ISO 8601; we guard against naive or
    malformed values so a bad date never triggers (or blocks) a deletion.
    """
    raw = event_data.get("date_to") or event_data.get("date_from")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _delete_event_data(db: Session, event: str) -> None:
    """Delete all rides, requests, and attendees for an event.

    Rides and requests cascade to their claims and offers. Attendees have no
    cascade and are referenced by those rows, so they're removed last.
    """
    for ride in db.query(Ride).filter_by(event=event).all():
        db.delete(ride)
    for rr in db.query(RideRequest).filter_by(event=event).all():
        db.delete(rr)
    for attendee in db.query(Attendee).filter_by(pretix_event=event).all():
        db.delete(attendee)
    db.commit()


async def cleanup_expired_events(
    db: Session, pretix: PretixClient, retention_days: int = RETENTION_DAYS,
) -> list[str]:
    """Delete all data for events that ended more than `retention_days` ago.

    Events whose dates can't be fetched from Pretix are left untouched, so an
    API outage or misconfiguration never causes data loss. Returns the list of
    event slugs that were purged.
    """
    now = datetime.now(timezone.utc)
    cutoff_delta = timedelta(days=retention_days)
    purged: list[str] = []

    for event in _event_slugs(db):
        event_data = await pretix.get_event(event)
        if not event_data:
            logger.warning("could not fetch event %s from Pretix, skipping", event)
            continue

        end = _event_end(event_data)
        if end is None:
            logger.warning("event %s has no usable date, skipping", event)
            continue

        if now < end + cutoff_delta:
            continue

        _delete_event_data(db, event)
        purged.append(event)
        logger.info("purged all data for event %s (ended %s)", event, end.isoformat())

    return purged
