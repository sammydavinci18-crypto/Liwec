from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user

from extensions import db
from models import User

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not name or not email or not password:
            flash("Please fill in every field.", "error")
            return render_template("register.html")

        if password != confirm:
            flash("Passwords don't match.", "error")
            return render_template("register.html")

        if len(password) < 6:
            flash("Password should be at least 6 characters.", "error")
            return render_template("register.html")

        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
            return render_template("register.html")

        user = User(name=name, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        flash("Welcome! Your account is ready.", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()
        if user is None or not user.check_password(password):
            flash("Incorrect email or password.", "error")
            return render_template("login.html")

        if user.is_banned:
            flash("This account has been suspended. Contact support if you think this is a mistake.", "error")
            return render_template("login.html")

        login_user(user, remember=True)
        next_page = request.args.get("next")
        return redirect(next_page or url_for("main.dashboard"))

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You've been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_user.check_password(current_password):
            flash("Your current password wasn't correct.", "error")
            return render_template("account.html")

        if len(new_password) < 6:
            flash("New password should be at least 6 characters.", "error")
            return render_template("account.html")

        if new_password != confirm_password:
            flash("New password and confirmation don't match.", "error")
            return render_template("account.html")

        if current_user.check_password(new_password):
            flash("That's your current password — pick a different one.", "error")
            return render_template("account.html")

        current_user.set_password(new_password)
        db.session.commit()
        flash("Password updated.", "success")
        return redirect(url_for("auth.account"))

    return render_template("account.html")
