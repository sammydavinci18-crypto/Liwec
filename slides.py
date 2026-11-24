"""
Turning an uploaded presentation into pictures the whole class can be shown.

Why pictures: a browser can't draw a .pptx faithfully, but it can show an image
perfectly on every phone, tablet and laptop, with no plug-in, and the picture
is exactly what the teacher's file looks like (fonts, spacing, animations'
final state). So every upload becomes one JPEG per slide (plus a small
thumbnail for the teacher's slide strip):

    .pdf                       -> rendered straight to pictures (PyMuPDF)
    .pptx / .ppt / .odp        -> LibreOffice turns it into a PDF first, then
                                  the same step. Needs LibreOffice on the
                                  server; see office_available().

The work runs in a background thread (a big deck takes several seconds, and
PowerPoint conversion can take much longer) while the Slides panel polls its
status. Pictures are stored with storage.py under "slides/<deck id>/..." in the
recordings bucket, so they survive on hosts without a persistent disk, and are
deleted when the session ends (purge_meeting_decks).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import storage
from extensions import db
from models import SlideDeck

ALLOWED_EXTENSIONS = {".pdf", ".pptx", ".ppt", ".odp"}
OFFICE_EXTENSIONS = {".pptx", ".ppt", ".odp"}

# What a real file of each type starts with: catches a renamed or damaged file
# before any converter touches it.
MAGIC = {
    ".pdf": (b"%PDF",),
    ".pptx": (b"PK\x03\x04",),
    ".odp": (b"PK\x03\x04",),
    ".ppt": (b"\xd0\xcf\x11\xe0",),
}

SLIDE_WIDTH_PX = 1600      # sharp on a full-HD display, ~100-250 KB per slide
THUMB_WIDTH_PX = 320
JPEG_QUALITY = 84
THUMB_QUALITY = 70
CONVERT_TIMEOUT_SECONDS = 180
STALE_AFTER_SECONDS = 15 * 60   # a deck still "processing" after this was lost to a restart


# LibreOffice uses a few hundred MB; on a small host two conversions at once can
# run it out of memory. They queue up instead (the second teacher just waits).
_OFFICE_LOCK = threading.Lock()


class SlideError(Exception):
    """Something the teacher can act on. The message is shown to them as is."""


# ---------------------------------------------------------------------------
# LibreOffice (only used for PowerPoint / OpenDocument files)
# ---------------------------------------------------------------------------

def _soffice_candidates(configured):
    if configured:
        yield configured
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            yield found
    for path in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/lib/libreoffice/program/soffice",
        "/opt/libreoffice/program/soffice",
    ):
        if os.path.exists(path):
            yield path


def find_soffice(configured=""):
    for path in _soffice_candidates(configured):
        if path and os.path.exists(path):
            return path
    return None


def office_available(configured=""):
    return find_soffice(configured) is not None


def office_to_pdf(src_path, out_dir, configured=""):
    """Converts a PowerPoint/OpenDocument file to a PDF in out_dir and returns
    the PDF's path. Raises SlideError with a message fit to show the teacher."""
    soffice = find_soffice(configured)
    if not soffice:
        raise SlideError(
            "This server can't open PowerPoint files directly. In PowerPoint choose "
            "File > Save As > PDF, then upload that PDF instead."
        )

    # A throw-away profile per conversion: two conversions at once (or a
    # read-only home directory) can't then trip over each other.
    profile = tempfile.mkdtemp(prefix="lo-profile-")
    try:
        cmd = [
            soffice,
            f"-env:UserInstallation={Path(profile).resolve().as_uri()}",
            "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nologo",
            "--convert-to", "pdf",
            "--outdir", str(out_dir),
            str(src_path),
        ]
        try:
            with _OFFICE_LOCK:
                subprocess.run(
                    cmd, check=True, timeout=CONVERT_TIMEOUT_SECONDS,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env={**os.environ, "SAL_USE_VCLPLUGIN": "svp", "HOME": profile},
                )
        except subprocess.TimeoutExpired:
            raise SlideError("Converting this presentation took too long. Try saving it as a PDF and uploading that.")
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"[slides] LibreOffice failed: {exc}", file=sys.stderr)
            raise SlideError("This presentation could not be converted. Try saving it as a PDF and uploading that.")
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    pdf = Path(out_dir) / (Path(src_path).stem + ".pdf")
    if not pdf.exists():
        raise SlideError("This presentation could not be converted. Try saving it as a PDF and uploading that.")
    return pdf


# ---------------------------------------------------------------------------
# PDF -> pictures
# ---------------------------------------------------------------------------

def render_pdf(pdf_path, max_pages):
    """Yields (n, slide_jpeg_bytes, thumb_jpeg_bytes) for each page, one page
    in memory at a time. Raises SlideError for a PDF that can't be shown."""
    try:
        import fitz  # PyMuPDF; imported here so the rest of the app doesn't need it to boot
    except ImportError as exc:
        print(f"[slides] PyMuPDF is not installed: {exc}", file=sys.stderr)
        raise SlideError("This server is missing its PDF tool (PyMuPDF), so slides can't be prepared. Please tell the administrator.")

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        print(f"[slides] fitz.open failed: {exc!r}", file=sys.stderr)
        raise SlideError("That file could not be opened. Check it isn't damaged.")

    try:
        if doc.needs_pass:
            # Many PDFs are "locked" only against editing and open with an empty password.
            if not doc.authenticate(""):
                raise SlideError("That file is password-protected. Remove the password and upload it again.")
        count = doc.page_count
        if count < 1:
            raise SlideError("That file has no slides in it.")
        if count > max_pages:
            raise SlideError(f"That presentation has {count} slides; the limit is {max_pages}. Split it into smaller files.")

        for index in range(count):
            try:
                page = doc.load_page(index)
                width = page.rect.width or 1
                scale = SLIDE_WIDTH_PX / width
                # Guard against a freakishly tall page producing a giant bitmap.
                scale = min(scale, 4000 / (page.rect.height or 1))
                big = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                thumb_scale = THUMB_WIDTH_PX / width
                small = page.get_pixmap(matrix=fitz.Matrix(thumb_scale, thumb_scale), alpha=False)
                big_jpg = big.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
                small_jpg = small.tobytes("jpeg", jpg_quality=THUMB_QUALITY)
            except SlideError:
                raise
            except Exception as exc:
                print(f"[slides] page {index + 1} could not be drawn: {exc!r}", file=sys.stderr)
                raise SlideError(f"Slide {index + 1} could not be drawn. Re-save the file as a PDF from PowerPoint (File > Save As > PDF) and try again.")
            yield index + 1, big_jpg, small_jpg
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# The background job
# ---------------------------------------------------------------------------

def process_deck(app, deck_id, upload_path, ext):
    """Runs in a background thread. Converts, renders, stores, and finally
    marks the deck 'ready' (or 'failed' with a reason the teacher can read).
    Always removes the temporary upload."""
    workdir = Path(upload_path).parent
    try:
        with app.app_context():
            deck = SlideDeck.query.get(deck_id)
            if deck is None:
                return
            bucket = app.config["RECORDINGS_BUCKET"]
            stored = []
            try:
                pdf_path = upload_path
                if ext in OFFICE_EXTENSIONS:
                    pdf_path = office_to_pdf(upload_path, workdir, app.config.get("SOFFICE_PATH", ""))

                pages = 0
                for n, big, small in render_pdf(pdf_path, app.config["SLIDES_MAX_PAGES"]):
                    storage.upload_bytes_resilient(bucket, deck.page_key(n), big, content_type="image/jpeg")
                    stored.append(deck.page_key(n))
                    storage.upload_bytes_resilient(bucket, deck.page_key(n, thumb=True), small, content_type="image/jpeg")
                    stored.append(deck.page_key(n, thumb=True))
                    pages = n

                deck.page_count = pages
                deck.status = "ready"
                deck.error = None
                db.session.commit()
            except SlideError as exc:
                _fail(deck, bucket, stored, str(exc))
            except storage.StorageError as exc:
                print(f"[slides] storage failed for deck {deck_id}: {exc}", file=sys.stderr)
                _fail(deck, bucket, stored, "The slides were read but could not be saved on the server. Please try again, and tell the administrator if it keeps happening (run: flask --app app slides-check).")
            except Exception as exc:  # never leave a deck stuck on "processing"
                import traceback
                traceback.print_exc()
                print(f"[slides] unexpected failure for deck {deck_id}: {exc!r}", file=sys.stderr)
                _fail(deck, bucket, stored, f"Something went wrong preparing these slides ({type(exc).__name__}). Try saving them as a PDF and uploading that.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _fail(deck, bucket, stored_keys, message):
    db.session.rollback()
    storage.delete_objects(bucket, stored_keys)
    deck.status = "failed"
    deck.error = message[:300]
    deck.page_count = 0
    db.session.commit()


def mark_stale_decks(meeting_id):
    """A deck that has been 'processing' far longer than any conversion takes
    was lost when the server restarted; say so instead of spinning forever."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(seconds=STALE_AFTER_SECONDS)
    stale = SlideDeck.query.filter(
        SlideDeck.meeting_id == meeting_id,
        SlideDeck.status == "processing",
        SlideDeck.created_at < cutoff,
    ).all()
    for deck in stale:
        deck.status = "failed"
        deck.error = "This upload was interrupted. Please upload it again."
    if stale:
        db.session.commit()


# ---------------------------------------------------------------------------
# Removing slides
# ---------------------------------------------------------------------------

def delete_deck_files(app, deck):
    """Deletes every stored picture of a deck (not the database row)."""
    bucket = app.config["RECORDINGS_BUCKET"]
    keys = deck.all_keys()
    storage.delete_objects(bucket, keys)
    # Local-disk mode leaves an empty folder behind; tidy it.
    local = storage.local_path_if_exists(bucket, f"slides/{deck.id}")
    if local:
        shutil.rmtree(local, ignore_errors=True)


def purge_meeting_decks(app, meeting_id):
    """When a session ends the pictures are no longer needed (the recording
    already holds what the class saw), so free the storage. Safe to run in a
    background thread; never raises."""
    try:
        with app.app_context():
            for deck in SlideDeck.query.filter_by(meeting_id=meeting_id).all():
                delete_deck_files(app, deck)
                db.session.delete(deck)
            db.session.commit()
    except Exception as exc:
        print(f"[slides] purge failed for meeting {meeting_id}: {exc!r}", file=sys.stderr)


def self_check(app):
    """Used by `flask slides-check`: tries each step a slide upload needs and
    returns a list of (ok, message) so a problem is named instead of guessed."""
    results = []
    try:
        import fitz
        results.append((True, f"PyMuPDF {fitz.__doc__.splitlines()[0] if fitz.__doc__ else 'installed'}"))
        doc = fitz.open()
        doc.new_page(width=800, height=450)
        pix = doc.load_page(0).get_pixmap(alpha=False)
        pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
        results.append((True, "Can draw a PDF page to a JPEG"))
    except Exception as exc:
        results.append((False, f"PDF rendering is broken: {exc!r}"))
    soffice = find_soffice(app.config.get("SOFFICE_PATH", ""))
    results.append((bool(soffice), f"LibreOffice (for .pptx): {soffice}" if soffice else
                    "LibreOffice NOT found: only PDFs can be uploaded (deploy with the Dockerfile to accept .pptx)"))
    with app.app_context():
        bucket = app.config["RECORDINGS_BUCKET"]
        key = f"slides/_check/{uuid.uuid4().hex}.jpg"
        try:
            where = storage.upload_bytes_resilient(bucket, key, b"check", content_type="image/jpeg")
            ok = storage.read_bytes(bucket, key) == b"check"
            storage.delete_objects(bucket, [key])
            results.append((ok, f"Slide storage works ({where}" + (")" if where == "cloud" else
                            ": cloud storage refused it, check SUPABASE_URL / SUPABASE_SERVICE_KEY and that the "
                            f"'{bucket}' bucket exists)")))
        except Exception as exc:
            results.append((False, f"Slide storage is broken: {exc}"))
    return results


def new_workdir():
    return Path(tempfile.mkdtemp(prefix=f"deck-{uuid.uuid4().hex[:8]}-"))
