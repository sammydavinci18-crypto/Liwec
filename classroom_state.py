"""
Classroom teaching tools: the state and the validation, with no Flask in it.

sockets.py / sockets_tools.py are thin: they check WHO is asking (host or not),
call into here, and relay the answer. Keeping the rules in plain Python means
they can be tested without a server (tests/test_classroom_state.py).

Covers
  * ink operations   - what a pen / highlighter / eraser / shape / text / image
                       stroke looks like. Shared by the whiteboard and by the
                       drawing the teacher does on top of slides.
  * Board            - the whiteboard: several pages, a paper style per page,
                       a ruler, and which student (if any) may write.
  * SlideInk         - the teacher's drawing on slides, remembered per slide.
  * Poll             - "raise A, B, C or D": one question, live count.
  * Breakouts        - pairs / groups, then everyone back.
  * Timer            - a countdown everyone can see.
  * Break            - "we are taking a break, back in N minutes" for everyone.

Coordinates in ink operations are fractions (0..1) of the board or slide, so
every screen size shows the same picture.
"""
import random
import re
import time

# ---------------------------------------------------------------------------
# Ink operations
# ---------------------------------------------------------------------------

LINE_KINDS = ("pen", "hi", "erase")                 # hi = highlighter
SHAPE_KINDS = ("line", "arrow", "rect", "ellipse")
BACKGROUNDS = ("plain", "grid", "axes", "lines")
MAX_POINTS_PER_MESSAGE = 200
MAX_POINTS_PER_LINE = 5000
MAX_IMAGE_CHARS = 400_000                           # base64 text; about 300 KB of picture
MAX_IMAGES_PER_BOARD = 12
_IMAGE_RE = re.compile(r"^data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}$")

# What each kind of person may draw.
ALL_OPS = ("line", "more", "shape", "text", "image", "undo", "clear")
STUDENT_OPS = ("line", "more", "shape", "text")     # a student the teacher let write: no images, no undo/clear
SLIDE_OPS = ("line", "more", "shape", "undo", "clear")


def _num(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:                                       # NaN
        return default
    return min(hi, max(lo, f))


def _colour(v):
    v = v if isinstance(v, str) else ""
    ok = len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:])
    return v if ok else "#111111"


def _point(pt):
    if isinstance(pt, (list, tuple)) and len(pt) == 2:
        return [round(_num(pt[0], 0, 1, 0), 4), round(_num(pt[1], 0, 1, 0), 4)]
    return None


def _points(raw):
    out = []
    for pt in (raw if isinstance(raw, list) else [])[:MAX_POINTS_PER_MESSAGE]:
        p = _point(pt)
        if p:
            out.append(p)
    return out


def _id(v):
    return str(v if v is not None else "")[:24]


def clean_ink_op(op, allow=ALL_OPS):
    """A safe copy of an ink operation, or None if it is malformed or not allowed."""
    if not isinstance(op, dict):
        return None
    kind = op.get("t")
    if kind not in allow:
        return None

    if kind == "line":
        pts = _points(op.get("p"))
        if not pts:
            return None
        k = op.get("k") if op.get("k") in LINE_KINDS else "pen"
        return {"t": "line", "id": _id(op.get("id")), "k": k, "c": _colour(op.get("c")),
                "w": _num(op.get("w"), 0.001, 0.08, 0.006), "p": pts}

    if kind == "more":
        pts = _points(op.get("p"))
        return {"t": "more", "id": _id(op.get("id")), "p": pts} if pts else None

    if kind == "shape":
        a, b = _point(op.get("a")), _point(op.get("b"))
        shape = op.get("k")
        if not a or not b or shape not in SHAPE_KINDS:
            return None
        return {"t": "shape", "id": _id(op.get("id")), "k": shape, "c": _colour(op.get("c")),
                "w": _num(op.get("w"), 0.001, 0.05, 0.005), "a": a, "b": b}

    if kind == "text":
        text = str(op.get("s", ""))[:500]
        if not text.strip():
            return None
        return {"t": "text", "s": text, "c": _colour(op.get("c")),
                "x": _num(op.get("x"), 0, 1, 0), "y": _num(op.get("y"), 0, 1, 0),
                "size": _num(op.get("size"), 0.01, 0.2, 0.06)}

    if kind == "image":
        src = op.get("src")
        if not isinstance(src, str) or len(src) > MAX_IMAGE_CHARS or not _IMAGE_RE.match(src):
            return None
        return {"t": "image", "id": _id(op.get("id")), "src": src,
                "x": _num(op.get("x"), 0, 1, 0), "y": _num(op.get("y"), 0, 1, 0),
                "w": _num(op.get("w"), 0.02, 1, 0.3), "h": _num(op.get("h"), 0.02, 1, 0.3)}

    return {"t": kind}                               # undo / clear


def _append_more(ops, op):
    """Extend the line a 'more' message belongs to (merged, so a late joiner gets one tidy list).
    Returns True if there was such a line."""
    for stored in reversed(ops[-100:]):
        if stored["t"] == "line" and stored["id"] == op["id"]:
            room = MAX_POINTS_PER_LINE - len(stored["p"])
            if room <= 0:
                return False
            stored["p"].extend(op["p"][:room])
            return True
    return False


# ---------------------------------------------------------------------------
# Whiteboard
# ---------------------------------------------------------------------------

class Board:
    MAX_PAGES = 30
    MAX_OPS = 20000

    def __init__(self):
        self.open = False
        self.v = 0                                   # bumped on every change (the recorder polls this)
        self.page = 0
        self.pages = [{"ops": [], "bg": "plain"}]
        self.writer = None                           # {"sid", "name"}: the student allowed to write
        self.ruler = None                            # {"on", "x", "y", "a", "l"}

    # -- reading
    @property
    def ops(self):
        return self.pages[self.page]["ops"]

    @property
    def bg(self):
        return self.pages[self.page]["bg"]

    def has_content(self):
        return any(p["ops"] for p in self.pages) or len(self.pages) > 1

    def image_count(self):
        return sum(1 for p in self.pages for o in p["ops"] if o["t"] == "image")

    def payload(self):
        return {"open": self.open, "page": self.page, "pages": len(self.pages), "bg": self.bg,
                "ops": self.ops, "writer": self.writer, "ruler": self.ruler}

    # -- changing
    def set_open(self, is_open):
        self.open = bool(is_open)
        if not self.open:
            self.writer = None                       # a closed board has no student writer
        self.v += 1

    def apply(self, op):
        """Applies one (already cleaned) operation to the current page.
        Returns True when it should be relayed to everyone else."""
        ops = self.ops
        kind = op["t"]
        if kind == "clear":
            ops.clear()
        elif kind == "undo":
            if not ops:
                return False
            ops.pop()
        elif kind == "more":
            if not _append_more(ops, op):
                return False
        else:
            if len(ops) >= self.MAX_OPS:
                return False
            if kind == "image" and self.image_count() >= MAX_IMAGES_PER_BOARD:
                return False
            ops.append(op)
        self.v += 1
        return True

    def go_to(self, n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return False
        if not 0 <= n < len(self.pages):
            return False
        self.page = n
        self.v += 1
        return True

    def add_page(self):
        if len(self.pages) >= self.MAX_PAGES:
            return False
        self.pages.append({"ops": [], "bg": self.bg})   # a new page keeps the paper style
        self.page = len(self.pages) - 1
        self.v += 1
        return True

    def set_bg(self, bg):
        if bg not in BACKGROUNDS:
            return False
        self.pages[self.page]["bg"] = bg
        self.v += 1
        return True

    def set_writer(self, sid, name):
        self.writer = {"sid": sid, "name": name} if sid else None
        self.v += 1

    def set_ruler(self, raw):
        if not isinstance(raw, dict):
            return False
        if not raw.get("on"):
            self.ruler = None
        else:
            self.ruler = {"on": True, "x": round(_num(raw.get("x"), 0, 1, 0.5), 4),
                          "y": round(_num(raw.get("y"), 0, 1, 0.5), 4),
                          "a": round(_num(raw.get("a"), -7, 7, 0), 4),
                          "l": round(_num(raw.get("l"), 0.15, 1.2, 0.6), 4)}
        self.v += 1
        return True


# ---------------------------------------------------------------------------
# The teacher's drawing on slides: remembered for each slide
# ---------------------------------------------------------------------------

class SlideInk:
    MAX_OPS = 5000

    def __init__(self):
        self._by_slide = {}                          # (deck_id, index) -> [op]
        self.v = 0

    def ops(self, deck_id, index):
        return self._by_slide.setdefault((deck_id, index), [])

    def apply(self, deck_id, index, op):
        ops = self.ops(deck_id, index)
        kind = op["t"]
        if kind == "clear":
            ops.clear()
        elif kind == "undo":
            if not ops:
                return False
            ops.pop()
        elif kind == "more":
            if not _append_more(ops, op):
                return False
        else:
            if len(ops) >= self.MAX_OPS:
                return False
            ops.append(op)
        self.v += 1
        return True

    def drop_deck(self, deck_id):
        for key in [k for k in self._by_slide if k[0] == deck_id]:
            del self._by_slide[key]
        self.v += 1


# ---------------------------------------------------------------------------
# Quick poll: "raise A, B, C or D"
# ---------------------------------------------------------------------------

LETTERS = "ABCDEF"


class Poll:
    def __init__(self, poll_id, question="", n=4, correct=None):
        self.id = poll_id
        self.question = (question or "").strip()[:300]
        self.n = int(_num(n, 2, 6, 4))
        self.correct = correct if isinstance(correct, int) and 0 <= correct < self.n else None
        self.open = True
        self.shared = False                          # students may see the live count
        self.revealed = False                        # the correct answer is shown
        self.answers = {}                            # sid -> option index

    @property
    def is_quiz(self):
        return self.correct is not None

    def answer(self, sid, choice):
        if not self.open:
            return False
        try:
            choice = int(choice)
        except (TypeError, ValueError):
            return False
        if not 0 <= choice < self.n:
            return False
        self.answers[sid] = choice
        return True

    def counts(self):
        out = [0] * self.n
        for c in self.answers.values():
            out[c] += 1
        return out

    def close(self):
        self.open = False

    def reveal(self):
        self.open = False
        self.revealed = True
        self.shared = True

    def host_view(self, roster):
        """What the teacher sees. roster: {sid: name} of the students connected now."""
        waiting = sorted(name for sid, name in roster.items() if sid not in self.answers)
        right = None
        if self.is_quiz:
            right = sum(1 for c in self.answers.values() if c == self.correct)
        return {"id": self.id, "q": self.question, "n": self.n, "open": self.open,
                "shared": self.shared, "revealed": self.revealed, "correct": self.correct,
                "counts": self.counts(), "answered": len(self.answers), "total": len(roster),
                "waiting": waiting, "right": right}

    def public_view(self, sid=None):
        """What a student sees. The right answer stays secret until it is revealed."""
        show_counts = self.shared or self.revealed
        return {"id": self.id, "q": self.question, "n": self.n, "open": self.open,
                "mine": self.answers.get(sid), "shared": show_counts,
                "counts": self.counts() if show_counts else None,
                "revealed": self.revealed, "correct": self.correct if self.revealed else None,
                "quiz": self.is_quiz}


# ---------------------------------------------------------------------------
# Breakout rooms
# ---------------------------------------------------------------------------

class Breakouts:
    MAX_GROUPS = 12

    def __init__(self, clock=time.monotonic, rng=None):
        self._clock = clock
        self._rng = rng or random.Random()
        self.active = False
        self.token = 0                               # changes each time rooms open or close
        self.groups = []                             # [{"id", "name", "members": [sid]}]
        self.ends_at = None                          # clock() value, or None = until the teacher ends it
        self.visit = None                            # None | group id | "all": where the teacher's voice goes

    # -- planning
    def plan(self, sids, mode, value):
        """Splits the students into lists. mode 'pairs' (value ignored) or 'groups' (value = how many).
        Returns None if it cannot be done sensibly."""
        sids = list(sids)
        if len(sids) < 2:
            return None
        self._rng.shuffle(sids)
        if mode == "pairs":
            count = len(sids) // 2
        else:
            count = int(_num(value, 2, self.MAX_GROUPS, 2))
            count = min(count, len(sids))
        if count < 1:
            return None
        buckets = [[] for _ in range(count)]
        for i, sid in enumerate(sids):               # dealt like cards: sizes differ by at most one
            buckets[i % count].append(sid)
        return buckets

    def start(self, sids, mode, value, minutes=0):
        buckets = self.plan(sids, mode, value)
        if not buckets:
            return False
        self.groups = [{"id": i + 1, "name": "Pair %d" % (i + 1) if mode == "pairs" else "Group %d" % (i + 1),
                        "members": b} for i, b in enumerate(buckets)]
        self.active = True
        self.token += 1
        self.visit = None
        minutes = _num(minutes, 0, 120, 0)
        self.ends_at = self._clock() + minutes * 60 if minutes else None
        return True

    def end(self):
        self.active = False
        self.token += 1
        self.groups = []
        self.ends_at = None
        self.visit = None

    # -- who is where
    def group_of(self, sid):
        for g in self.groups:
            if sid in g["members"]:
                return g
        return None

    def sids_in(self, group_id):
        for g in self.groups:
            if g["id"] == group_id:
                return list(g["members"])
        return []

    def move(self, sid, group_id):
        target = next((g for g in self.groups if g["id"] == group_id), None)
        if not self.active or not target:
            return False
        for g in self.groups:
            if sid in g["members"]:
                g["members"].remove(sid)
        target["members"].append(sid)
        return True

    def remove(self, sid):
        for g in self.groups:
            if sid in g["members"]:
                g["members"].remove(sid)

    def add_late(self, sid):
        """Someone who arrives (or reconnects) while rooms are open goes to the smallest group."""
        if not self.active or not self.groups or self.group_of(sid):
            return self.group_of(sid)
        smallest = min(self.groups, key=lambda g: len(g["members"]))
        smallest["members"].append(sid)
        return smallest

    def set_visit(self, where):
        if where is None or where == "all" or any(g["id"] == where for g in self.groups):
            self.visit = where
            return True
        return False

    def extend(self, minutes):
        if not self.active:
            return False
        base = self.ends_at if self.ends_at else self._clock()
        self.ends_at = base + _num(minutes, 0, 60, 1) * 60
        return True

    def seconds_left(self):
        if not self.active or self.ends_at is None:
            return None
        return max(0, int(round(self.ends_at - self._clock())))

    # -- what people are told
    def host_view(self, names):
        return {"active": self.active, "visit": self.visit, "left": self.seconds_left(),
                "groups": [{"id": g["id"], "name": g["name"],
                            "members": [{"sid": s, "name": names.get(s, "Student")} for s in g["members"]]}
                           for g in self.groups]}

    def student_view(self, sid, names):
        g = self.group_of(sid)
        if not self.active or not g:
            return {"active": False}
        return {"active": True, "group": {"id": g["id"], "name": g["name"]},
                "mates": sorted(names.get(s, "Student") for s in g["members"] if s != sid),
                "mate_sids": [s for s in g["members"] if s != sid],
                "left": self.seconds_left(),
                "teacher_here": self.visit == "all" or self.visit == g["id"],
                "everyone": self.visit == "all"}


# ---------------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------------

class Timer:
    MIN_SECONDS = 5
    MAX_SECONDS = 3 * 3600
    LINGER = 20                                       # how long "time's up" stays on screen

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.reset()

    def reset(self):
        self.on = False
        self.label = ""
        self.total = 0
        self.focus = False
        self._ends = 0.0
        self._paused_left = None                     # seconds, while paused

    def start(self, seconds, label="", focus=False):
        self.reset()
        self.total = int(_num(seconds, self.MIN_SECONDS, self.MAX_SECONDS, 300))
        self.label = (label or "").strip()[:140]
        self.focus = bool(focus)
        self._ends = self._clock() + self.total
        self.on = True

    def remaining(self):
        if not self.on:
            return 0
        if self._paused_left is not None:
            return self._paused_left
        return max(0.0, self._ends - self._clock())

    def pause(self):
        if self.on and self._paused_left is None and self.remaining() > 0:
            self._paused_left = self.remaining()
            return True
        return False

    def resume(self):
        if self.on and self._paused_left is not None:
            self._ends = self._clock() + self._paused_left
            self._paused_left = None
            return True
        return False

    def add(self, seconds):
        if not self.is_showing():
            return False
        seconds = _num(seconds, -3600, 3600, 0)
        left = max(0.0, self.remaining()) + seconds
        left = min(max(left, 1.0), self.MAX_SECONDS)
        self.total = max(self.total, int(left)) if seconds > 0 else self.total
        if self._paused_left is not None:
            self._paused_left = left
        else:
            self._ends = self._clock() + left
        return True

    def stop(self):
        self.reset()

    def is_showing(self):
        """On screen: running, paused, or finished within the last LINGER seconds."""
        if not self.on:
            return False
        if self.remaining() > 0:
            return True
        return self._clock() - self._ends < self.LINGER

    def payload(self):
        if not self.is_showing():
            return {"active": False}
        return {"active": True, "label": self.label, "total": self.total,
                "remaining": round(self.remaining(), 1), "paused": self._paused_left is not None,
                "focus": self.focus}


# ---------------------------------------------------------------------------
# Break
# ---------------------------------------------------------------------------

class Break:
    """The teacher steps away for a few minutes. Everyone sees a message and a
    countdown to when the class resumes. Separate from Timer so a task timer
    and a break never overwrite each other."""
    MIN_SECONDS = 30
    MAX_SECONDS = 3 * 3600
    LINGER = 15                                       # "we're back" stays on screen this long
    GRACE = 60                                        # seconds after a break during which being away is excused

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._grace_until = 0.0
        self.reset()

    def reset(self):
        self.on = False
        self.message = ""
        self.total = 0
        self._ends = 0.0

    def start(self, minutes, message=""):
        self.reset()
        self._grace_until = 0.0
        seconds = _num(float(_num(minutes, 0.5, self.MAX_SECONDS / 60, 5)) * 60,
                       self.MIN_SECONDS, self.MAX_SECONDS, 300)
        self.total = int(seconds)
        self.message = (message or "").strip()[:200]
        self._ends = self._clock() + self.total
        self.on = True

    def remaining(self):
        if not self.on:
            return 0
        return max(0.0, self._ends - self._clock())

    def add(self, seconds):
        """Make the break longer (or a little shorter). Only while it is running."""
        if not self.on or self.remaining() <= 0:
            return False
        left = self.remaining() + _num(seconds, -3600, 3600, 0)
        left = min(max(left, 5.0), self.MAX_SECONDS)
        self._ends = self._clock() + left
        self.total = max(self.total, int(left))
        return True

    def end(self):
        self.reset()
        self._grace_until = self._clock() + self.GRACE    # students need a moment to come back

    def running(self):
        return self.on and self.remaining() > 0

    def excuses_absence(self):
        """True while students are free to leave the lesson screen: during the break, and for
        GRACE seconds after it (they may be in another tab and have not seen it end yet)."""
        if self.running():
            return True
        if self.on:
            return self._clock() < self._ends + self.GRACE
        return self._clock() < self._grace_until

    def is_showing(self):
        if not self.on:
            return False
        if self.remaining() > 0:
            return True
        return self._clock() - self._ends < self.LINGER

    def payload(self):
        if not self.is_showing():
            return {"active": False}
        return {"active": True, "message": self.message, "total": self.total,
                "remaining": round(self.remaining(), 1)}


# ---------------------------------------------------------------------------
# Everything for one room
# ---------------------------------------------------------------------------

class RoomTools:
    def __init__(self):
        self.board = Board()
        self.ink = SlideInk()
        self.poll = None
        self.poll_seq = 0
        self.breakouts = Breakouts()
        self.timer = Timer()
        self.rest = Break()


TOOLS = {}                                            # room_code -> RoomTools


def tools(room_code):
    return TOOLS.setdefault(room_code, RoomTools())


def peek(room_code):
    return TOOLS.get(room_code)


def drop(room_code):
    TOOLS.pop(room_code, None)
