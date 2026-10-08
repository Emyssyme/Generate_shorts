"""Session login for the web UI."""
from flask import flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user

from config import ADMIN_PASS, ADMIN_USER


# --- login support (minimal) ---------------------------------------------
login_manager = LoginManager()
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id):
        self.id = id


@login_manager.user_loader
def load_user(user_id):
    if user_id == ADMIN_USER:
        return User(ADMIN_USER)
    return None

# simple login/logout routes
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USER and password == ADMIN_PASS:
            user = User(ADMIN_USER)
            login_user(user)
            flash("Logged in successfully", "success")
            return redirect(url_for('video_cut'))
        flash("Invalid credentials", "danger")
    return render_template('login.html')

@login_required
def logout():
    logout_user()
    flash("Logged out", "info")
    return redirect(url_for('login'))


def register(app):
    """Attach the routes to ``app`` (endpoint names = function names)."""
    app.add_url_rule('/login', methods=['GET', 'POST'], view_func=login)
    app.add_url_rule('/logout', view_func=logout)
