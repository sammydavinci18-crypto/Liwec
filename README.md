# Light-Way International Virtual Academy — platform (Liwec)

*Elite Cambridge Pathways From Home · Lighting Paths. Empowering Futures.*

One Flask app that serves the pages **and** handles live-video signalling. Video
streams directly between browsers (WebRTC); the server never sees or stores it
unless the administrator records a session.

## What this platform is for now: live lessons, timetable, recordings

Study materials and students live on other platforms. This one runs **live lessons**,
the **timetable**, **consultations** and **recording review**. Its users are the admin and teachers.

**A lesson, start to finish**
1. Admin adds it to the **Timetable** (class + teacher + subject + time, optionally weekly).
2. The teacher opens **My lessons**, presses **Start session**, and sends students the **student link**
   (*Copy student link* on the lesson, or *Invite* inside the room).
3. Students open the link on the other platform, type their name and are in. No account.
4. The lesson is recorded automatically. Admin reviews it under **Recordings** (and can **Observe** a live lesson unseen).

**The student link** (`/join/<token>`, `routes/guest.py`)
- Every session has one, including ones created before this update (tokens are added on boot).
  Admin-hosted consultations and meetings have one too.
- The host controls it from **Invite** inside the room: *let students in automatically* (default) or
  admit each person; *close* the link to new people; *replace* it with a new link.
- Students arriving before the host wait and walk in when the host arrives. Early links show a "not open yet"
  page that refreshes itself; the room opens 15 minutes before the start time.
- A student the host removes cannot come back from that browser. A student whose connection dropped can
  return even after the link is closed.
- Guests are recorded like everyone else and appear in the session's **Attendance** (name, time in, time away).
  They never receive session notes, and cannot see recordings.
- `GUEST_LIMIT` (default 30) caps people connected through the link at once.

**Recordings page** (`/admin/recordings`): finished sessions with Watch / Saved / Expired / No recording, and a
**Needs closing** list for sessions whose host vanished without pressing End. *Close & save recording* saves what was received.

**Set aside, not removed.** Student/teacher applications, classes, placements, users, parent links, teacher directory
and announcements are under **More** in the admin menu and in "Other school tools" on the dashboard. Teachers
have a smaller **More** menu. Nothing was deleted, so they can be brought back to the main menu any time
(`templates/base.html`).

**Class size.** Calls are peer-to-peer: every camera is sent to every other browser, so quality drops as
more students turn their cameras on. It is fine for small groups; for whole classes of 15+ with video, plan to
move to a media server (SFU) such as LiveKit or mediasoup. Only the room's media layer would need to change.

Upgrading: `flask db upgrade` (or just start the app). Migration `f6c9d2e4b857` is additive.

> The sections below describe the platform as built before this change. Where they say "no join-by-room-code"
> and "only invited guests can enter", the student link is now the one deliberate exception.

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

## Classes, timetable and teacher-hosted sessions

* **Classes** (Admin > Classes): year groups such as Year 6-13 and Bridging (seeded on first start; rename, add or archive them). Every student is in one class, set when approving them, when creating the account, or on the Placements page.
* **Placements** stay admin-only (teacher <-> student). A teacher can have any number of students, from several classes. "Place a whole class" places a class with a teacher in one step.
* **Timetable** (Admin > Timetable): add a lesson = class + teacher + subject + time (+ length, + repeat weekly). It becomes a session **hosted by the teacher**; its guest list is only the students *of that class* placed with *that teacher*, and it updates automatically when placements, classes or bans change. Year 7 and Year 8 Physics with the same teacher are therefore separate lessons, and Bridging is its own class. A teacher or class can't be booked twice at the same time.
* **Teacher sessions**: Admin > Schedule a session > "A teacher" lets the admin set up a session for one teacher and any chosen students/guests. The teacher hosts; the admin is not part of it.
* Teachers open the room and admit guests from the waiting room (My teaching). The admin can reschedule, cancel or review recordings but cannot enter a live teacher-hosted room unless invited.

Upgrading: run `flask db upgrade` (or just start the app: missing columns are repaired and default classes seeded). Existing students have no class yet, so set it under Placements before timetabling.

## Compulsory lessons, automatic recording, silent oversight

* **Recording is automatic and can't be stopped.** It starts when the host enters the room; there is no Record/Stop button and the server announces "Recording" to everyone. Only *End lesson / End meeting* stops it. If a browser can't record, the host is blocked with a message instead of running an unrecorded session.
* **Compulsory attendance** (teacher-led lessons and teacher sessions): students have no Leave/Back button, can't turn their camera off, are asked to go full screen, and get a closing-the-tab warning. Leaving the lesson screen (other tab, minimised, left full screen, camera off) covers the lesson with a "return to the lesson" screen, tells the teacher, and is logged as "Away" (count + time) in the attendance list on the session page. A web page cannot physically stop someone closing a browser, so it blocks, logs and alerts rather than prevents.
* **Admin observe** (Observe button on live sessions in Sessions, Timetable, the dashboard and the session page): the admin receives everyone's audio and video but sends nothing (no camera/mic is even requested), is not listed to anyone, causes no join/leave notice, and has no attendance row. Each observation is written to the server log.

Upgrading: `flask db upgrade` (adds `meeting_participants.away_count/away_seconds`).

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
`CALL_LIMIT_MINUTES`, `RECORDING_RETENTION_DAYS`, `GUEST_LIMIT`, `ICE_SERVERS`, `TURN_USERNAME`, `TURN_CREDENTIAL`,
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

## Whiteboard and enlarged screen share

- **Whiteboard (host only).** The *Whiteboard* button opens a board that covers the whole screen for everyone in the session. The teacher can write/draw (finger, pen or mouse), type text, change colour and thickness, erase, undo, clear, and *Close board* to return to the lesson. Students and an observing admin only watch. The board is kept server-side for the session, so reopening continues where it left off and late joiners see it. While it is open it is also painted into the session recording. Files: `static/js/whiteboard.js`, handlers `wb_open/wb_close/wb_op` in `sockets.py`.
- **Enlarge a shared screen.** When someone else shares their screen it fills the window; anyone can close it (✕, Esc or the *Enlarge* button) and reopen it while the share continues. File: `static/js/focusview.js`.

## Presenting slides (PowerPoint / PDF)

The host's control bar has a **Slides** button. The teacher uploads a PowerPoint
(`.pptx`, `.ppt`, `.odp`) or a PDF, waits a few seconds while it is prepared,
and presses **Present**.

* **Everyone else** gets the slide on their whole display (real browser full
  screen: the first tap completes it, as with a shared screen), following the
  teacher live. Students can raise a hand without leaving the slide. On a phone
  held upright a **Rotate** button turns the slide sideways.
* **The teacher** gets a presenter view: the current slide, a preview of the next
  one, Previous / Next buttons, a "go to slide" box, a strip of every slide, and
  any raised hands. Arrow keys, Space, Page Up/Down, Home/End and a tablet swipe
  also work. Only the host can start, stop or move the presentation (the server
  checks every message).
* People who join or reload mid-presentation land on the current slide. The
  slide is also painted into the session recording. Presenting a deck again
  resumes where the teacher stopped.

How it works: each upload becomes one picture per slide (`slides.py`, PyMuPDF),
stored in the recordings bucket under `slides/<deck id>/`, and **deleted when
the session ends** (the recording already holds what the class saw). Up to 10
decks per session. Limits: `SLIDES_MAX_MB` (40), `SLIDES_MAX_PAGES` (150).

### PowerPoint files need LibreOffice on the server

PDFs work everywhere with no extra setup. Turning a `.pptx` into pictures
needs LibreOffice (`soffice`) on the server. It is found automatically, or set
`SOFFICE_PATH`. **If it isn't there, the Slides panel says so and accepts PDF
only** ("In PowerPoint: File > Save As > PDF"), so nothing breaks.

* **Local (Windows/Mac/Linux):** install LibreOffice from libreoffice.org.
* **Render:** the standard Python runtime has no LibreOffice and cannot
  `apt-get`. To accept `.pptx` in production, deploy with the included
  `Dockerfile` instead (in Render choose Environment: Docker). The Dockerfile has
  not been built or run by the author of this change: treat it as a starting
  point and check the first deploy. Converting a deck uses a few hundred MB of
  memory, so avoid the smallest instance size.
* Only one conversion runs at a time; a second teacher's upload waits its turn.

## Interface conventions (usability)

Shared behaviour lives in `static/js/ui.js` (loaded on every page) and the "Usability pass" block at the end of `static/css/style.css`.

- **Confirm risky actions** by adding attributes to the `<form>`; no extra JavaScript is needed:
  `data-confirm="What will happen."` `data-confirm-title="Cancel this session?"` `data-confirm-action="Yes, cancel"` `data-confirm-tone="danger"` (red button, focus starts on "Go back").
  Use it for anything that removes access, cancels, bans, rejects or deletes.
- **Double-submit protection** is automatic for every POST form (buttons show "Working…"). Opt out with `data-no-lock` on the form.
- **Guest/people pickers**: wrap in `data-picker`, mark rows `data-picker-item`, and add `data-picker-all`, `data-picker-none`, `data-picker-count` for the Select/Clear buttons and live count.
- **Navigation**: `templates/base.html` highlights the current page from `request.endpoint`. When you add an admin page, add its endpoint to the matching link (or to `people_eps` for the People menu).
- **Dates**: any admin `datetime-local` field must carry `data-utc` so it is typed in local time and stored as UTC.
- Hints under a field: `<span class="field-hint">…</span>`. Group long forms with `<fieldset class="form-section"><legend>…</legend>`.
