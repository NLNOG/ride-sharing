from __future__ import annotations

import html as html_lib
import logging
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<]+")


class EmailService:
    def __init__(self):
        self.host = os.getenv("SMTP_HOST", "")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.from_addr = os.getenv("SMTP_FROM", "NLNOG Ride Share <noreply@nlnog.net>")
        self.starttls = os.getenv("SMTP_STARTTLS", "true").lower() == "true"
        self.base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
        self.enabled = bool(self.host)

    def send(self, to: str, subject: str, body: str):
        if not self.enabled:
            log.warning("Email skipped (SMTP_HOST not set): %s", subject)
            return
        if not to:
            log.warning("Email skipped (empty recipient address): %s", subject)
            return

        msg = MIMEMultipart("alternative")
        msg["From"] = self.from_addr
        msg["To"] = to
        msg["Subject"] = subject

        # Plain text version
        msg.attach(MIMEText(body, "plain"))

        # Simple HTML version
        escaped = html_lib.escape(body)
        linked = _URL_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)
        html_body = linked.replace("\n", "<br>\n")
        html = f"""\
<html>
<body style="font-family: sans-serif; color: #333; max-width: 600px;">
{html_body}
<hr style="border: none; border-top: 1px solid #ddd; margin-top: 2em;">
<p style="color: #999; font-size: 0.85em;">
This email was sent by NLNOG Ride Share.<br>
Log in with your Pretix ticket link to manage your rides.
</p>
</body>
</html>"""
        msg.attach(MIMEText(html, "html"))

        try:
            with smtplib.SMTP(self.host, self.port, timeout=10) as server:
                if self.starttls:
                    server.starttls()
                if self.user and self.password:
                    server.login(self.user, self.password)
                server.sendmail(self.from_addr, [to], msg.as_string())
            log.info("Email sent to %s: %s", to, subject)
        except Exception:
            log.exception("Failed to send email to %s: %s", to, subject)

    def auth_url(self, attendee, path: str) -> str:
        """Build a per-attendee auth-redirect URL.

        Recipients land logged in and then bounce to `path`. Falls back to a
        plain URL when we don't have the order secret stored yet (attendees
        created before the secret column existed).
        """
        if not getattr(attendee, "pretix_order_secret", ""):
            return f"{self.base_url}{path}"
        url = (
            f"{self.base_url}/auth/"
            f"{attendee.pretix_event}/{attendee.pretix_order_code}/{attendee.pretix_order_secret}"
        )
        if path:
            url += f"?redirect={quote(path, safe='')}"
        return url

    # --- Convenience methods for each notification type ---

    def notify_seat_requested(self, driver, passenger_name: str, ride):
        self.send(
            driver.email,
            f"New seat request from {passenger_name}",
            f"Hi {driver.name},\n\n"
            f"{passenger_name} has requested a seat on your ride from "
            f"{ride.departure_location}.\n\n"
            f"Please log in to approve or decline:\n"
            f"{self.auth_url(driver, f'/rides/{ride.id}')}\n",
        )

    def notify_seat_approved(self, passenger, driver_name: str, ride):
        self.send(
            passenger.email,
            f"Your seat on {driver_name}'s ride is confirmed!",
            f"Hi {passenger.name},\n\n"
            f"{driver_name} has approved your seat on the ride from "
            f"{ride.departure_location}.\n\n"
            f"View ride details and contact info:\n"
            f"{self.auth_url(passenger, f'/rides/{ride.id}')}\n",
        )

    def notify_seat_rejected(self, passenger, driver_name: str, ride):
        self.send(
            passenger.email,
            f"Seat request update for {driver_name}'s ride",
            f"Hi {passenger.name},\n\n"
            f"Unfortunately, {driver_name} was unable to approve your seat "
            f"request for the ride from {ride.departure_location}.\n\n"
            f"You can browse other available rides on the dashboard:\n"
            f"{self.auth_url(passenger, '/dashboard')}\n",
        )

    def notify_passenger_left(self, driver, passenger_name: str, ride):
        self.send(
            driver.email,
            f"{passenger_name} left your ride",
            f"Hi {driver.name},\n\n"
            f"{passenger_name} has left your ride from "
            f"{ride.departure_location}. You now have "
            f"{ride.seats_available} seat(s) available.\n\n"
            f"View your ride:\n"
            f"{self.auth_url(driver, f'/rides/{ride.id}')}\n",
        )

    def notify_ride_cancelled(self, passenger, driver_name: str, departure_location: str):
        self.send(
            passenger.email,
            f"Ride from {departure_location} has been cancelled",
            f"Hi {passenger.name},\n\n"
            f"The ride from {departure_location} offered by "
            f"{driver_name} has been cancelled.\n\n"
            f"You can browse other available rides on the dashboard:\n"
            f"{self.auth_url(passenger, '/dashboard')}\n",
        )

    def notify_ride_edited(self, passenger, driver_name: str, ride):
        self.send(
            passenger.email,
            f"Ride details updated for {ride.departure_location}",
            f"Hi {passenger.name},\n\n"
            f"{driver_name} has updated the details of the ride from "
            f"{ride.departure_location}.\n\n"
            f"View the updated details:\n"
            f"{self.auth_url(passenger, f'/rides/{ride.id}')}\n",
        )

    def notify_ride_offered(self, requester, driver_name: str, ride, request_id: int):
        self.send(
            requester.email,
            f"{driver_name} offered you a ride from {ride.departure_location}",
            f"Hi {requester.name},\n\n"
            f"{driver_name} has offered you a seat on their ride from "
            f"{ride.departure_location}.\n\n"
            f"Review the offer:\n"
            f"{self.auth_url(requester, f'/requests/{request_id}')}\n",
        )

    def notify_offer_accepted(self, driver, requester_name: str, ride):
        self.send(
            driver.email,
            f"{requester_name} accepted your ride offer!",
            f"Hi {driver.name},\n\n"
            f"{requester_name} has accepted your offer for the ride from "
            f"{ride.departure_location}.\n\n"
            f"View ride details and contact info:\n"
            f"{self.auth_url(driver, f'/rides/{ride.id}')}\n",
        )

    def notify_offer_declined(self, driver, requester_name: str, ride):
        self.send(
            driver.email,
            f"{requester_name} declined your ride offer",
            f"Hi {driver.name},\n\n"
            f"{requester_name} has declined your offer for the ride from "
            f"{ride.departure_location}.\n",
        )
