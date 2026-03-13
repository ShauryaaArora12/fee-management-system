from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from typing import Any

import pymysql
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-change-this")

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)


def get_db_connection() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DB", "fee_management"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


class User(UserMixin):
    def __init__(self, user_id: int, username: str, role: str) -> None:
        self.id = str(user_id)
        self.username = username
        self.role = role


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, role FROM users WHERE id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return User(row["id"], row["username"], row["role"])
    finally:
        conn.close()


def seed_admin_user() -> None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE username = %s", ("admin",))
            existing = cursor.fetchone()
            if existing:
                return

            password_hash = generate_password_hash("admin123")
            cursor.execute(
                """
                INSERT INTO users (username, password_hash, role)
                VALUES (%s, %s, %s)
                """,
                ("admin", password_hash, "admin"),
            )
    finally:
        conn.close()


def is_admin() -> bool:
    return current_user.is_authenticated and getattr(current_user, "role", "") == "admin"


@app.route("/")
def home() -> Any:
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, username, password_hash, role
                    FROM users
                    WHERE username = %s
                    """,
                    (username,),
                )
                user_row = cursor.fetchone()

            if user_row and check_password_hash(user_row["password_hash"], password):
                user = User(user_row["id"], user_row["username"], user_row["role"])
                login_user(user)
                flash("Logged in successfully.", "success")
                return redirect(url_for("dashboard"))

            flash("Invalid username or password.", "danger")
        finally:
            conn.close()

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout() -> Any:
    logout_user()
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard() -> Any:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM students")
            total_students = cursor.fetchone()["count"]

            cursor.execute("SELECT IFNULL(SUM(amount_paid), 0) AS total_paid FROM payments")
            total_paid = cursor.fetchone()["total_paid"]

            cursor.execute(
                """
                SELECT s.name, s.email, p.amount_paid, p.payment_date
                FROM payments p
                JOIN students s ON s.id = p.student_id
                ORDER BY p.payment_date DESC
                LIMIT 5
                """
            )
            recent_payments = cursor.fetchall()
    finally:
        conn.close()

    return render_template(
        "dashboard.html",
        total_students=total_students,
        total_paid=total_paid,
        recent_payments=recent_payments,
    )


@app.route("/students", methods=["GET", "POST"])
@login_required
def students() -> Any:
    conn = get_db_connection()
    try:
        if request.method == "POST":
            if not is_admin():
                flash("Only admins can add students.", "danger")
                return redirect(url_for("students"))

            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()
            course = request.form.get("course", "").strip()
            total_fee = request.form.get("total_fee", "").strip()

            if not name or not email or not course or not total_fee:
                flash("All student fields are required.", "danger")
                return redirect(url_for("students"))

            try:
                total_fee_decimal = Decimal(total_fee)
            except Exception:
                flash("Total fee must be a valid number.", "danger")
                return redirect(url_for("students"))

            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO students (name, email, course, total_fee)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (name, email, course, total_fee_decimal),
                )
            flash("Student added successfully.", "success")
            return redirect(url_for("students"))

        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    s.id,
                    s.name,
                    s.email,
                    s.course,
                    s.total_fee,
                    IFNULL(SUM(p.amount_paid), 0) AS paid_amount,
                    (s.total_fee - IFNULL(SUM(p.amount_paid), 0)) AS due_amount
                FROM students s
                LEFT JOIN payments p ON s.id = p.student_id
                GROUP BY s.id, s.name, s.email, s.course, s.total_fee
                ORDER BY s.created_at DESC
                """
            )
            student_rows = cursor.fetchall()

        return render_template("students.html", students=student_rows, admin=is_admin())
    finally:
        conn.close()


@app.route("/payments", methods=["GET", "POST"])
@login_required
def payments() -> Any:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, name FROM students ORDER BY name")
            student_options = cursor.fetchall()

        if request.method == "POST":
            if not is_admin():
                flash("Only admins can add payments.", "danger")
                return redirect(url_for("payments"))

            student_id = request.form.get("student_id", "").strip()
            amount_paid = request.form.get("amount_paid", "").strip()
            payment_date = request.form.get("payment_date", "").strip()
            remarks = request.form.get("remarks", "").strip()

            if not student_id or not amount_paid or not payment_date:
                flash("Student, amount, and payment date are required.", "danger")
                return redirect(url_for("payments"))

            try:
                amount_decimal = Decimal(amount_paid)
                parsed_date = datetime.strptime(payment_date, "%Y-%m-%d").date()
            except Exception:
                flash("Enter a valid amount and date.", "danger")
                return redirect(url_for("payments"))

            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO payments (student_id, amount_paid, payment_date, remarks)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (student_id, amount_decimal, parsed_date, remarks),
                )
            flash("Payment added successfully.", "success")
            return redirect(url_for("payments"))

        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    p.id,
                    s.name AS student_name,
                    p.amount_paid,
                    p.payment_date,
                    p.remarks
                FROM payments p
                JOIN students s ON s.id = p.student_id
                ORDER BY p.payment_date DESC, p.id DESC
                """
            )
            payment_rows = cursor.fetchall()

        return render_template(
            "payments.html",
            payments=payment_rows,
            students=student_options,
            admin=is_admin(),
        )
    finally:
        conn.close()


@app.errorhandler(404)
def page_not_found(_: Exception) -> tuple[str, int]:
    return "<h2>404 - Page not found</h2>", 404


if __name__ == "__main__":
    seed_admin_user()
    app.run(debug=True)
