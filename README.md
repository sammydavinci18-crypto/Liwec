# Light-Way International Virtual Academy — platform (Liwec)

*Elite Cambridge Pathways From Home · Lighting Paths. Empowering Futures.*

One Flask app that serves the pages **and** handles live-video signalling. Video
streams directly between browsers (WebRTC); the server never sees or stores it
unless the administrator records a session.

## The core rule: the administrator controls everything live

- **Only the admin hosts.** Every consultation, class and meeting is created by the admin.
  There is no "new meeting" button for anyone else and **no join-by-room-code**.
- **Only invited guests can enter,** and only after the admin lets them in from the lobby.
  Guests who arrive before the admin simply wait; nobody is ever auto-admitted. The lobby
  opens 15 minutes before the scheduled time.
- **Consultancy is the admin's alone.** No consultant directory, profiles, availability or
  peer-to-peer messaging. People *request* a consultation or meeting
  (`/consult`); the admin approves it with a date/time (which creates the session and invites
  the requester) or declines it.
- The server enforces this in routes **and** in the real-time layer (`sockets.py`): a user who
  tries to skip the lobby or relay signals without being admitted is ignored.

### Admin screens
| Screen | Path |
| --- | --- |
| Requests queue (approve / decline) | `/admin/requests` |
| All sessions | `/admin/meetings` |
| Schedule a session (guest list, class shortcut) | `/admin/meetings/new` |
| Manage one session (invite, reschedule, cancel) | `/admin/meetings/<id>` |
| Student / teacher review, placements, users | `/admin/students`, `/admin/teachers`, `/admin/placements`, `/admin/users` |

The **class shortcut** on the schedule form invites a teacher plus every student the admin
has placed with them, in one click.

## Everything is free and built in
- No plans or tiers: no premium flag, no call-time limit by default (`CALL_LIMIT_MINUTES=0`),
  recordings kept 90 days by default (`RECORDING_RETENTION_DAYS`, `0` = forever).
- Video, chat, screen sharing, notes, recording and playback are the platform's own features.
- No Google Meet, Moodle or FlexibleX. The virtual-background feature was removed because it
  relied on an outside model and stock photos.
- Fonts are system fonts (nothing fetched from Google Fonts).

### What still touches outside services
1. **Socket.IO browser library** is loaded from a CDN unless you run
   `python tools/fetch_vendor.py` once, which saves it to `static/vendor/` and is used automatically.
2. **STUN server** (helps browsers find each other behind home routers). Default is a free public
   one; set `ICE_SERVERS` (and `TURN_USERNAME` / `TURN_CREDENTIAL`) to point at your own coturn.
3. **Supabase Storage** is optional (documents & recordings). Without it, files are kept on local disk.

## Quick start (local, SQLite — no setup)

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
ADMIN_EMAIL=you@example.com ADMIN_PASSWORD=choose-one python app.py   # creates the admin on first boot
```
Visit http://localhost:5000. Log in as the admin, open **Schedule a session**, invite a second
(test) account, then open the room as both to see the lobby.
Existing deployments: `flask --app app db upgrade` (or just boot — it self-heals). The new
migration only *adds* tables/columns; old consultant tables are left untouched.

## Academy content (from the brand document)
Mission, vision, six objectives, four values (Excellence, Integrity, Resilience, Global-mindedness)
and the three programmes (Year 6–Year 8 Bridging; National Curriculum to Cambridge Transition;
Home Schooling) are on the landing page. Students apply to one of those three programmes.

## Roles
`admin` (the Director) · `registrar` (enrolment & parent links) · teachers and students (approved by the admin) · parents (linked to a student).

## Configuration (env vars)
`SECRET_KEY`, `DB_ENGINE` (`sqlite`|`postgres`|`mysql`), `DATABASE_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`,
`CALL_LIMIT_MINUTES`, `RECORDING_RETENTION_DAYS`, `ICE_SERVERS`, `TURN_USERNAME`, `TURN_CREDENTIAL`,
`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.

## School portal & public pages

- **My school** (`/school`): students and approved parents see the application tracker (with reference number such as `LW-2026-0001`), placed teachers, upcoming classes and progress reports.
- **Timetable** (`/timetable`): upcoming sessions grouped by day, plus school-calendar entries.
- **Announcements** (`/announcements`): the admin posts notices to everyone, students, teachers or parents; a dated notice also appears on the calendar.
- **Progress reports**: teachers write reports for students placed with them; the student, approved parents, registrars and the admin can read them (printable).
- **Public pages**: `/about` (the crest story), `/admissions`, `/safeguarding`. The Director's message on the home page is a **draft placeholder** — replace it with his own words and name in `templates/landing.html`.
- **Theme**: navy and gold, defined as CSS tokens at the top of `static/css/style.css`.

### Deploying this update
Run `flask db upgrade` (or let your Render start command do it). Two additive migrations apply: `b7e4a91c5d20` (admin-controlled sessions) and `c3f1d8a2e611` (portal features). Nothing is dropped.
