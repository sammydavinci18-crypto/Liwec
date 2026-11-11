"""Public information pages (no login needed): the crest story, admissions,
and safeguarding & privacy."""
from flask import Blueprint, render_template

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/about")
def about():
    return render_template("pages/about.html")


@pages_bp.route("/admissions")
def admissions():
    return render_template("pages/admissions.html")


@pages_bp.route("/safeguarding")
def safeguarding():
    return render_template("pages/safeguarding.html")
