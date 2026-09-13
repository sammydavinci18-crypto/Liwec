from functools import wraps

from flask import abort
from flask_login import current_user


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def registrar_required(view):
    """Restricts a route to registrars — admins can also use registrar
    tools (a strictly higher permission level should never be locked out
    of a narrower one)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not (current_user.is_registrar or current_user.is_admin):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def approved_consultant_required(view):
    """Restricts a route to users with an approved ConsultantProfile —
    e.g. managing availability slots."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        profile = getattr(current_user, "consultant_profile", None)
        if not current_user.is_authenticated or not profile or profile.status != "approved":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def approved_teacher_required(view):
    """Restricts a route to users with an approved TeacherProfile."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        profile = getattr(current_user, "teacher_profile", None)
        if not current_user.is_authenticated or not profile or profile.status != "approved":
            abort(403)
        return view(*args, **kwargs)
    return wrapped
