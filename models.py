from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Attendee(Base):
    __tablename__ = "attendees"

    id = Column(Integer, primary_key=True)
    pretix_event = Column(String, nullable=False)
    pretix_order_code = Column(String, nullable=False)
    pretix_position_id = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, default="")
    phone = Column(String, default="")
    notes = Column(String, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint(
            "pretix_event", "pretix_order_code", "pretix_position_id",
            name="uq_attendee_position",
        ),
    )

    rides_offered = relationship("Ride", back_populates="driver")
    ride_requests = relationship("RideRequest", back_populates="requester")
    ride_claims = relationship("RideClaim", back_populates="passenger")


class Ride(Base):
    __tablename__ = "rides"

    id = Column(Integer, primary_key=True)
    event = Column(String, nullable=False, index=True)
    driver_id = Column(Integer, ForeignKey("attendees.id"), nullable=False)
    departure_location = Column(String, nullable=False)
    departure_time = Column(DateTime, nullable=False)
    return_time = Column(DateTime, nullable=True)
    seats = Column(Integer, nullable=False, default=1)
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("event", "driver_id", name="uq_one_ride_per_event"),
    )

    driver = relationship("Attendee", back_populates="rides_offered")
    claims = relationship("RideClaim", back_populates="ride", cascade="all, delete-orphan")
    offers = relationship("RideOffer", back_populates="ride", cascade="all, delete-orphan")

    @property
    def approved_claims(self):
        return [c for c in self.claims if c.status == "approved"]

    @property
    def pending_claims(self):
        return [c for c in self.claims if c.status == "pending"]

    @property
    def seats_available(self):
        return self.seats - len(self.approved_claims)


class RideRequest(Base):
    __tablename__ = "ride_requests"

    id = Column(Integer, primary_key=True)
    event = Column(String, nullable=False, index=True)
    requester_id = Column(Integer, ForeignKey("attendees.id"), nullable=False)
    location = Column(String, nullable=False)
    departure_time = Column(DateTime, nullable=True)
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requester = relationship("Attendee", back_populates="ride_requests")
    offers = relationship("RideOffer", back_populates="request", cascade="all, delete-orphan")


class RideClaim(Base):
    __tablename__ = "ride_claims"

    id = Column(Integer, primary_key=True)
    ride_id = Column(Integer, ForeignKey("rides.id"), nullable=False)
    passenger_id = Column(Integer, ForeignKey("attendees.id"), nullable=False)
    status = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("ride_id", "passenger_id", name="uq_ride_passenger"),
    )

    ride = relationship("Ride", back_populates="claims")
    passenger = relationship("Attendee", back_populates="ride_claims")


class RideOffer(Base):
    __tablename__ = "ride_offers"

    id = Column(Integer, primary_key=True)
    ride_id = Column(Integer, ForeignKey("rides.id"), nullable=False)
    request_id = Column(Integer, ForeignKey("ride_requests.id"), nullable=False)
    status = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("ride_id", "request_id", name="uq_ride_offer"),
    )

    ride = relationship("Ride", back_populates="offers")
    request = relationship("RideRequest", back_populates="offers")
