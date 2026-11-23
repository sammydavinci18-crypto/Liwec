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

## Reliability: connections, big classes, weak links, recording

Four things decide whether a live lesson actually works. All are built in; what
you turn on depends on how many students you have and where they connect from.

| Problem | What was wrong | What the platform does now |
|---|---|---|
| Some students can't connect | Only a public STUN server; phones and strict school Wi-Fi need a **TURN relay** | Set up TURN (below) or LiveKit (which includes one). The host sees a warning in the room until one is configured. Dropped connections are restarted (ICE restart) instead of being deleted. |
| A class of 30 can't have cameras on | Peer-to-peer: every browser uploads its video to everyone | Set up **LiveKit** (an SFU): each browser sends one stream; viewers get the quality they need. Without it, lessons with more than `MESH_VIDEO_LIMIT` (8) people let new students in with video off. |
| No low-data mode | A weak connection meant being kicked out | **Data** button in the room: Normal / Low data / Audio only. The teacher sees 📉 / 🎧 next to the name; compulsory-lesson students on audio-only are not logged as away. A suggestion appears when the link looks weak. |
| Recording depends on the teacher's browser | A crash lost up to 30 s; a slow laptop gave a poor file | With LiveKit + storage configured, **the server records** (slides and whiteboard included). Otherwise the browser records, hardened: 10 s numbered chunks, saved locally and retried, resumes after a reload, keeps its frame rate in a background tab. If the server recorder fails mid-lesson, the teacher's browser takes over automatically. |

### 1. Easiest win: a TURN relay (keeps the current peer-to-peer design)
Pick one (env vars on the Flask app; details in `config.py`):
* **coturn** on a small VPS: copy `deploy/turnserver.conf`, then set
  `TURN_URLS=turn:turn.example.org:3478,turn:turn.example.org:3478?transport=tcp,turns:turn.example.org:443?transport=tcp`
  and `TURN_SECRET=...`. Each browser gets its own credentials that expire after `TURN_TTL_SECONDS`.
* **Cloudflare Realtime TURN**: `CLOUDFLARE_TURN_KEY_ID` + `CLOUDFLARE_TURN_API_TOKEN`.
* Any provider with a fixed login: `ICE_SERVERS=...` plus `TURN_USERNAME` / `TURN_CREDENTIAL`.

The `turns:...:443` TCP/TLS entry is the important one for school networks.

### 2. Big classes: LiveKit (SFU)
1. Run LiveKit either on **LiveKit Cloud**, or on a VPS with `deploy/docker-compose.yml`
   (needs UDP ports; the Flask app can stay on Render). LiveKit's own TURN covers item 1.
2. Set `LIVEKIT_URL` (`wss://...`), `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` on the app.
3. Optional: `python tools/fetch_vendor.py` so browsers load the LiveKit library from your server.

Lobby, chat, hands, slides, whiteboard and admin oversight are unchanged. Students
receive the teacher, anyone presenting and the few people speaking (up to 8 cameras;
the teacher sees up to 24); everyone's audio is always received. An observing admin
joins as a *hidden*, receive-only participant.

### 3. Server-side recording (needs step 2)
Set `PUBLIC_BASE_URL` (the app's public https address) and the S3 access key that
Supabase gives you (Storage > S3 Connection): `EGRESS_S3_ENDPOINT`, `EGRESS_S3_REGION`,
`EGRESS_S3_ACCESS_KEY`, `EGRESS_S3_SECRET`. The recorder opens `/egress/<room>/<token>/view`
(see `routes/egress.py`), so the file shows the teacher, the class, slides and whiteboard.
Server recordings are `.mp4`; browser recordings `.webm`. If any setting is missing, the
browser records instead.

### Known limits
* If the server recorder dies and the teacher's browser takes over, the first part is
  the (possibly partial) server file and the rest is the browser file; only the latter is
  kept as the recording.
* A browser recording interrupted by a reload contains two video segments back to back;
  VLC plays both, some browsers play only the first.
* Audio-only is chosen by the student. Teachers see it labelled, but it is not approved.
* None of the LiveKit pieces could be run against a live server while this was written:
  test a short lesson end to end (join, present, share a screen, end, play the recording)
  before relying on it.

## Seeing the class and calling on students

* **Presenter view (teacher):** while slides are up, the side panel shows a grid of
  student faces (raised hands first, in the order they went up) and the chat, with a
  box to reply. Each face has *Call on* and *Spotlight* buttons.
* **Teacher video (students):** a small picture of the teacher stays in the corner of the
  slide. A student can hide it (and it disappears by itself in audio-only mode).
* **Calling on a student:** *Spotlight* puts the student's video large on everyone's screen.
  *Call on* does that and sends the student an invitation; their microphone is only turned
  on when they press **Unmute and speak**, and returns to how it was when the turn ends
  (the teacher presses *Done*, or the student presses *Stop speaking*). Calling on a student
  lowers their raised hand. The same buttons are in the Participants panel. With the media
  server, the spotlighted student is always received at full quality.
* Not covered: the spotlight card is not painted into the session recording (the recording
  keeps its normal layout); the teacher's presenter-view faces are not recorded either.

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

- **Whiteboard.** The *Whiteboard* button opens a board that covers the whole screen for everyone. The teacher has pen, highlighter (see-through, never hides writing), eraser, typed text, shapes (line, arrow, box, oval), a **ruler** (drag to move, drag an end to turn; start a pen line near its edge and it comes out straight), **pictures** (shrunk in the browser before sending), **several pages**, and a choice of paper: plain, graph, graph with axes, lined. The teacher can let **one student write** (pen, highlighter, eraser, text, shapes only: no clearing, undo or pictures); the student hands it back with *I'm done*, and it ends if they leave or the board closes. The board is kept server-side for the session, so reopening continues and late joiners see every page. It is painted into the recording, ruler included. Files: `static/js/ink.js` (drawing engine shared with slides and the recorder page), `static/js/whiteboard.js`, handlers `wb_*` in `sockets_tools.py`.
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

## Teaching tools (draw on slides, polls, breakout rooms, timer)

All of these are started by the host only; the server checks that on every message (`sockets_tools.py`), and the rules live in plain Python in `classroom_state.py` so they can be tested without a server.

- **Draw on slides.** While presenting, *Draw on slide* gives the teacher pen, highlighter, **laser pointer**, eraser, and underline / arrow / box / circle. Strokes appear on every student's slide as they are drawn, belong to the slide they were made on (go to another slide and it is clean; come back and it is still there, for the session), and are in the recording. The laser is never stored. Files: `static/js/slideink.js`, `ink_op` / `laser` handlers.
- **Quick poll.** *Tools → Quick poll*: ask with 2 to 6 choices (A, B, C, D…), optionally typing the question and marking the right answer (a quiz). Students tap big buttons or press a key and may change their answer until voting closes. The teacher sees a live count and who has not answered, and can close voting, show the counts to the class, and reveal the answer. The server never sends the right answer to students before it is revealed. Results are not saved after the session. File: `static/js/polls.js`.
- **Breakout rooms.** *Tools → Breakout rooms*: pairs or a number of groups, dealt at random, with an optional time limit (the server brings everyone back when it runs out). The teacher can move students, **visit** a group (hear it and be heard), talk to everyone, message every group, add a minute, and end it. Students see their group and can ask for help. Late joiners go to the smallest group. File: `static/js/breakout.js`.
  - *Who hears whom is applied in each browser* (other groups' tiles are muted and hidden), like the rest of this platform's media rules. A determined student with browser developer tools could still listen in; it is a classroom aid, not a security boundary.
  - *Breakout conversations are recorded.* The recording is made from the streams, not from what each person's speakers play, so every group is in the lesson recording, consistent with the compulsory-recording rule.
- **Timer and focus bar.** *Tools → Timer*: a countdown (1 to 20 min or custom) with the task written on it. Everyone sees a bar across the top that drains, turns amber then red, chimes and says "Time's up". Pause, +1 minute and stop are on the bar for the teacher. The server sends seconds left, so wrong student clocks do not matter. File: `static/js/timer.js`.

The *Tools* button is in the control bar, the presenter view and the whiteboard. The poll, groups and timer sit in the same top-of-everything layer as the class view (`window.ClassLayer`), so they stay visible over full-screen slides and the whiteboard.

### Testing

- `python -m unittest discover -s tests`: 44 checks of the rules (no server needed).
- `python tests/browser_test.py`: renders the real room page for a teacher and four students in Chromium and runs every tool above end to end against the real handlers (needs `jinja2` and `playwright`; Flask is stubbed). Screenshots go to `tests/shots/`.

Not covered by those: real WebRTC / LiveKit audio (the hearing rules are tested by checking which tiles are muted), and the server-side LiveKit recorder page (`egress_view.html`), which now uses the same `ink.js` but has only been syntax-checked here, not run against LiveKit.
