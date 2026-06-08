# NLNOG Ride Share

Carpooling web app for [NLNOG](https://nlnog.net) events. Attendees can offer rides, request rides, and claim seats - all authenticated via [Pretix](https://pretix.eu) ticket codes.

## Features

- **Pretix authentication** - log in with your order code and secret, no separate account needed
- **Offer a ride** - post your departure location, time, available seats, and return time
- **Request a ride** - post where you need a pickup from
- **Claim seats** - request a seat on an offered ride (requires driver approval)
- **Ride offers** - drivers can offer their ride to open requests (requires requester approval)
- **Contact exchange** - phone/Telegram/Signal details are shared only between confirmed driver and passengers
- **Email notifications** - SMTP notifications for seat requests, approvals, ride changes, and cancellations
- **Multi-event support** - each Pretix event gets its own ride board
- **Multi-attendee orders** - group ticket holders pick which attendee they are

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Pretix API token, organizer slug, and a secret key
uvicorn main:app --reload
```

Then distribute auth links to attendees:

```
https://ride.nlnog.net/auth/<event-slug>/<order-code>/<order-secret>
```

These can be included in Pretix confirmation emails using placeholder variables.

## Configuration

All configuration is via environment variables (or a `.env` file):

| Variable | Description |
|---|---|
| `PRETIX_API_URL` | Pretix API base URL (default: `https://pretix.eu/api/v1`) |
| `PRETIX_API_TOKEN` | Pretix API token with read access to orders and events |
| `PRETIX_ORGANIZER` | Pretix organizer slug |
| `SECRET_KEY` | Random string for signing session cookies |
| `DATABASE_URL` | SQLAlchemy database URL (default: `sqlite:///./rideshare.db`) |
| `BASE_URL` | Public URL of the app, used in email links |
| `SMTP_HOST` | SMTP server hostname (emails disabled if empty) |
| `SMTP_PORT` | SMTP port (default: `587`) |
| `SMTP_USER` | SMTP username |
| `SMTP_PASSWORD` | SMTP password |
| `SMTP_FROM` | From address for emails |
| `SMTP_STARTTLS` | Use STARTTLS (default: `true`) |
| `CLEANUP_INTERVAL_SECONDS` | How often to purge data for finished events (default: `86400`) |

## Running tests

```bash
python tests.py
```

## Tech stack

- [FastAPI](https://fastapi.tiangolo.com/) with Jinja2 templates
- [SQLAlchemy](https://www.sqlalchemy.org/) with SQLite
- [Pico CSS](https://picocss.com/) for styling
- [httpx](https://www.python-httpx.org/) for Pretix API calls

## License

[MIT](LICENSE)
