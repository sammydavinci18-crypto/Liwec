"""
File storage for anything that needs to survive longer than the app
process: verification documents and call recordings.

Why this exists: local disk on the app server is NOT persistent on most
hosting free tiers (Render's free tier has no disk at all, and even a paid
disk only survives if you keep paying for it). Files written to local disk
disappear on redeploy/restart. So production uses Supabase Storage (same
project as the database — no new vendor to set up).

For local development/testing, if SUPABASE_URL / SUPABASE_SERVICE_KEY
aren't set, this transparently falls back to local disk under the
configured *_DIR folders — so `flask run` locally, and this app's own test
suite, don't need real Supabase credentials to exercise the surrounding
application logic (permission checks, upload/serve flow, etc).
"""

import uuid
from pathlib import Path

import requests
from flask import current_app


class StorageError(Exception):
    pass


def _supabase_configured():
    return bool(current_app.config.get("SUPABASE_URL") and current_app.config.get("SUPABASE_SERVICE_KEY"))


def _supabase_headers():
    key = current_app.config["SUPABASE_SERVICE_KEY"]
    return {"Authorization": f"Bearer {key}", "apikey": key}


def _local_dir(bucket):
    key = "VERIFICATION_DOCS_DIR" if bucket == current_app.config["VERIFICATION_BUCKET"] else "RECORDINGS_DIR"
    return Path(current_app.config[key])


def new_object_key(original_filename):
    """A random, collision-safe key to store an upload under."""
    suffix = Path(original_filename).suffix
    return f"{uuid.uuid4().hex}{suffix}"


def _create_bucket(bucket):
    """Creates a missing private bucket (the service key is allowed to). Returns
    True if it now exists, so a first-time setup needs no manual Supabase step."""
    try:
        r = requests.post(
            f"{current_app.config['SUPABASE_URL']}/storage/v1/bucket",
            headers={**_supabase_headers(), "Content-Type": "application/json"},
            json={"id": bucket, "name": bucket, "public": False},
            timeout=15,
        )
    except requests.RequestException:
        return False
    ok = r.status_code in (200, 201) or "already exists" in r.text
    current_app.logger.warning(f"[storage] bucket '{bucket}' was missing; create -> {r.status_code}")
    return ok


def upload_bytes(bucket, key, data, content_type="application/octet-stream"):
    """Uploads (or overwrites) an object. Returns the key on success."""
    if _supabase_configured():
        url = f"{current_app.config['SUPABASE_URL']}/storage/v1/object/{bucket}/{key}"
        headers = {**_supabase_headers(), "Content-Type": content_type, "x-upsert": "true"}
        r = requests.post(url, headers=headers, data=data, timeout=30)
        if r.status_code in (400, 404) and "Bucket not found" in r.text and _create_bucket(bucket):
            r = requests.post(url, headers=headers, data=data, timeout=30)  # retry once now it exists
        if r.status_code not in (200, 201):
            raise StorageError(f"Supabase upload failed ({r.status_code}): {r.text[:300]}")
        return key

    path = _local_dir(bucket) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, (bytes, bytearray)) else data.read())
    return key


def upload_file(bucket, key, path, content_type="application/octet-stream"):
    """Uploads a file from disk WITHOUT reading it into memory (a lesson can be
    hundreds of megabytes). Local-fallback mode: nothing to do, the file is
    already where playback looks for it. Raises StorageError on refusal."""
    if not _supabase_configured():
        return key
    url = f"{current_app.config['SUPABASE_URL']}/storage/v1/object/{bucket}/{key}"
    headers = {**_supabase_headers(), "Content-Type": content_type, "x-upsert": "true"}

    def _post():
        with open(path, "rb") as f:
            # (connect timeout, read timeout): big files need minutes, not seconds.
            return requests.post(url, headers=headers, data=f, timeout=(15, 1800))

    try:
        r = _post()
        if r.status_code in (400, 404) and "Bucket not found" in r.text and _create_bucket(bucket):
            r = _post()
    except requests.RequestException as exc:
        raise StorageError(f"Supabase upload failed: {exc}")
    if r.status_code not in (200, 201):
        raise StorageError(f"Supabase upload failed ({r.status_code}): {r.text[:300]}")
    return key


def upload_bytes_resilient(bucket, key, data, content_type="application/octet-stream"):
    """Like upload_bytes, but if cloud storage refuses (bucket missing, bad key,
    outage) the object is kept on the server's own disk instead of failing.
    Used for slide pictures, which only live for one session anyway. read_bytes
    and delete_objects look in both places. Returns "cloud" or "local"."""
    if _supabase_configured():
        try:
            upload_bytes(bucket, key, data, content_type=content_type)
            return "cloud"
        except (StorageError, requests.RequestException) as exc:
            current_app.logger.warning(f"[storage] cloud upload of {key} failed, keeping it on local disk: {exc}")
    path = _local_dir(bucket) / key
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, (bytes, bytearray)) else data.read())
    except OSError as exc:
        raise StorageError(f"Could not write {key} to local disk: {exc}")
    return "local"


def append_bytes(bucket, key, chunk):
    """Appends to an object — used only for the local fallback, while a
    recording is still being streamed in during a live call. Supabase mode
    never calls this; see routes/meetings.py, which buffers chunks locally
    during the call and does a single upload_bytes() at the end instead,
    since Storage's simple upload API doesn't support append."""
    path = _local_dir(bucket) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as f:
        f.write(chunk)
    return key


def local_path_if_exists(bucket, key):
    """Only meaningful in local-fallback mode — used by the recording
    pipeline to find the in-progress local buffer file. Returns None in
    Supabase mode (nothing to look at locally)."""
    path = _local_dir(bucket) / key
    return path if path.exists() else None


def signed_url(bucket, key, expires_in=60):
    """A short-lived URL the browser can be redirected to directly — files
    never pass through our own server for viewing/downloading. Only
    meaningful in Supabase mode; local-fallback mode returns None, and the
    caller (see routes/admin.py, routes/meetings.py) should serve the file
    itself via Flask instead."""
    if not _supabase_configured():
        return None
    url = f"{current_app.config['SUPABASE_URL']}/storage/v1/object/sign/{bucket}/{key}"
    r = requests.post(url, headers=_supabase_headers(), json={"expiresIn": expires_in}, timeout=15)
    if r.status_code != 200:
        raise StorageError(f"Supabase signed URL failed ({r.status_code}): {r.text[:300]}")
    signed_path = r.json().get("signedURL", "")
    return f"{current_app.config['SUPABASE_URL']}/storage/v1{signed_path}"


def object_exists(bucket, key):
    """True if the object is in storage (used to confirm a server-side recording
    really landed before it is offered for playback)."""
    if _supabase_configured():
        url = f"{current_app.config['SUPABASE_URL']}/storage/v1/object/info/{bucket}/{key}"
        try:
            r = requests.get(url, headers=_supabase_headers(), timeout=15)
        except requests.RequestException:
            return False
        return r.status_code == 200
    return (_local_dir(bucket) / key).exists()


def delete_object(bucket, key):
    if _supabase_configured():
        url = f"{current_app.config['SUPABASE_URL']}/storage/v1/object/{bucket}/{key}"
        try:
            r = requests.delete(url, headers=_supabase_headers(), timeout=15)
            return r.status_code in (200, 204)
        except requests.RequestException:
            return False

    path = _local_dir(bucket) / key
    try:
        if path.exists():
            path.unlink()
        return True
    except OSError:
        return False


def read_bytes(bucket, key):
    """Returns an object's bytes, or None if it doesn't exist. Used for slide
    images, which are served through our own server (same origin) rather than
    by redirecting to a signed URL: the host's browser paints the slide into
    the session recording, and a picture from another origin would make that
    canvas unusable. Also finds objects that upload_bytes_resilient kept on
    local disk."""
    local = _read_local(bucket, key)
    if local is not None:
        return local
    if _supabase_configured():
        url = f"{current_app.config['SUPABASE_URL']}/storage/v1/object/{bucket}/{key}"
        try:
            r = requests.get(url, headers=_supabase_headers(), timeout=30)
        except requests.RequestException as exc:
            raise StorageError(f"Supabase download failed: {exc}")
        if r.status_code == 404 or r.status_code == 400:
            return None
        if r.status_code != 200:
            raise StorageError(f"Supabase download failed ({r.status_code}): {r.text[:300]}")
        return r.content
    return None


def _read_local(bucket, key):
    path = _local_dir(bucket) / key
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        return None


def delete_objects(bucket, keys):
    """Deletes several objects (cloud and any local copy). Missing ones are
    ignored. Returns True if nothing went wrong."""
    keys = list(keys)
    if not keys:
        return True
    for k in keys:  # local copies first; harmless if there are none
        try:
            p = _local_dir(bucket) / k
            if p.exists():
                p.unlink()
        except OSError:
            pass
    if _supabase_configured():
        url = f"{current_app.config['SUPABASE_URL']}/storage/v1/object/{bucket}"
        try:
            r = requests.delete(url, headers=_supabase_headers(), json={"prefixes": keys}, timeout=30)
            if r.status_code in (200, 204):
                return True
        except requests.RequestException:
            pass
        # The bulk call failed: fall back to one at a time.
        return all([delete_object(bucket, k) for k in keys])
    return True
