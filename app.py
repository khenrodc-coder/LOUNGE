from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from functools import wraps
from datetime import datetime, timedelta
import uuid
import os
import qrcode
import pymysql

app = Flask(__name__)
app.secret_key = "supersecretkey"

# -----------------------
# CONFIG
# -----------------------
QR_FOLDER = "static/qrcodes"
os.makedirs(QR_FOLDER, exist_ok=True)

ADMIN_USERNAME = "SuperAdmin001"
ADMIN_PASSWORD = "superadmin2026"

# -----------------------
# DATABASE CONNECTION
# -----------------------


def get_db_connection():
    return pymysql.connect(
        host="localhost",
        user="root",
        password="",
        database="lll_ms_db",
        cursorclass=pymysql.cursors.DictCursor
    )

# -----------------------
# UTILITIES
# -----------------------


def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user" not in session or session.get("role") != "admin":
            flash("Please log in as admin.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def log_activity(action):
    conn = get_db_connection()
    cursor = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO activity_logs (action) VALUES (%s)",
                   (f"{timestamp} - {action}",))
    conn.commit()
    cursor.close()
    conn.close()


def generate_order_id():
    return f"ORD-{uuid.uuid4().hex[:8].upper()}"


def generate_tracking_token():
    return uuid.uuid4().hex


def parse_weight(weight_str):
    """Convert weight string like '10 kg' to float 10.0"""
    try:
        return float(str(weight_str).replace("kg", "").strip())
    except ValueError:
        return None


def map_order(order):
    """Map DB order fields to JSON-friendly format"""
    return {
        "transaction_id": order["transaction_id"],
        "tracking_token": order["tracking_token"],
        "tracking_url": order["tracking_url"],
        "qr_code": order["qr_code_path"],
        "name": order["name"],
        "service": order["service"],
        "weight": float(order["weight"]),
        "amount": float(order["amount"]),
        "contact": order["contact"] or "-",
        "payment": order["payment_method"] or "-",
        "date": order["order_date"].strftime("%Y-%m-%d") if order["order_date"] else "-",
        "pickup": order["pickup_date"].strftime("%Y-%m-%d") if order["pickup_date"] else "-",
        "status": order["status"] or "New"
    }

# -----------------------
# ROUTES
# -----------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username")
    password = request.form.get("password")
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session["user"] = ADMIN_USERNAME
        session["role"] = "admin"
        log_activity(f"{ADMIN_USERNAME} logged in")
        return redirect(url_for("dashboard"))
    flash("Invalid username or password!", "error")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    user = session.pop("user", None)
    session.pop("role", None)
    if user:
        log_activity(f"{user} logged out")
    flash("Logged out successfully.", "success")
    return redirect(url_for("index"))

# -----------------------
# ADMIN PAGES
# -----------------------


@app.route("/dashboard")
@require_login
def dashboard():
    return render_template("dashboard.html", user=session["user"], page="dashboard")


@app.route("/orders")
@require_login
def orders():
    return render_template("orders.html", user=session["user"], page="orders")


@app.route("/customers")
@require_login
def customers():
    return render_template("customers.html", user=session["user"], page="customers")


@app.route("/reports")
@require_login
def reports():
    return render_template("reports.html", user=session["user"], page="reports")


@app.route("/settings")
@require_login
def settings():
    return render_template("settings.html", user=session["user"], page="settings")


@app.route("/api/dashboard")
@require_login
def api_dashboard():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # -------------------------
        # SUMMARY STATS
        # -------------------------
        cursor.execute("SELECT COALESCE(SUM(amount),0) as total FROM orders")
        total_revenue = float(cursor.fetchone()["total"])

        cursor.execute(
            "SELECT COUNT(*) as total FROM orders WHERE status != 'Completed'")
        active_orders = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(*) as total 
            FROM orders 
            WHERE status IN ('New','Washing','Drying','Folding')
        """)
        pending_orders = cursor.fetchone()["total"]

        cursor.execute(
            "SELECT COUNT(DISTINCT customer_id) as total FROM orders")
        total_customers = cursor.fetchone()["total"]

        # -------------------------
        # REVENUE CHART (DAILY)
        # -------------------------
        cursor.execute("""
            SELECT DATE(order_date) as day, COALESCE(SUM(amount),0) as total
            FROM orders
            GROUP BY DATE(order_date)
            ORDER BY day ASC
            LIMIT 10
        """)
        revenue_rows = cursor.fetchall()

        revenue_chart = {
            "labels": [row["day"].strftime("%Y-%m-%d") for row in revenue_rows],
            "data": [float(row["total"]) for row in revenue_rows]
        }

        # -------------------------
        # ORDERS CHART (COUNT PER DAY)
        # -------------------------
        cursor.execute("""
            SELECT DATE(order_date) as day, COUNT(*) as total
            FROM orders
            GROUP BY DATE(order_date)
            ORDER BY day ASC
            LIMIT 10
        """)
        order_rows = cursor.fetchall()

        orders_chart = {
            "labels": [row["day"].strftime("%Y-%m-%d") for row in order_rows],
            "data": [int(row["total"]) for row in order_rows]
        }

        return jsonify({
            "summary": {
                "totalRevenue": total_revenue,
                "activeOrders": active_orders,
                "pendingOrders": pending_orders,
                "totalCustomers": total_customers
            },
            "charts": {
                "revenue": revenue_chart,
                "orders": orders_chart
            }
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

# -----------------------
# API - CUSTOMERS
# -----------------------


@app.route("/api/customers")
@require_login
def api_customers():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT c.customer_id, c.name, c.contact_number,
                   o.transaction_id, o.order_date, o.amount, o.status
            FROM customers c
            LEFT JOIN orders o ON c.customer_id = o.customer_id
            ORDER BY o.order_date DESC
        """)
        rows = cursor.fetchall()

        customers_map = {}
        for row in rows:
            cid = row["customer_id"]
            if cid not in customers_map:
                customers_map[cid] = {
                    "customer_id": cid,
                    "name": row["name"],
                    "phone": row["contact_number"] or "-",
                    "spent": float(row["amount"] or 0),
                    "lastOrder": row["order_date"].strftime("%Y-%m-%d") if row["order_date"] else "-",
                    "status": "Active" if row["order_date"] else "Inactive",
                    "orderId": row["transaction_id"] or "-"
                }
            else:
                customers_map[cid]["spent"] += float(row["amount"] or 0)
                if row["order_date"]:
                    if customers_map[cid]["lastOrder"] == "-" or row["order_date"] > datetime.strptime(customers_map[cid]["lastOrder"], "%Y-%m-%d"):
                        customers_map[cid]["lastOrder"] = row["order_date"].strftime(
                            "%Y-%m-%d")
                        customers_map[cid]["orderId"] = row["transaction_id"] or "-"

        return jsonify(list(customers_map.values()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# -----------------------
# API - ORDERS (POST/GET)
# -----------------------


@app.route("/api/orders", methods=["GET", "POST"])
@require_login
def api_orders():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if request.method == "POST":
            if not request.is_json:
                return jsonify({"error": "Request must be JSON"}), 400

            data = request.get_json()
            required_fields = ("name", "service", "weight",
                               "amount", "contact")
            if not all(field in data and data[field] for field in required_fields):
                return jsonify({"error": "Missing required fields"}), 400

            weight = parse_weight(data["weight"])
            if weight is None:
                return jsonify({"error": "Invalid weight format"}), 400

            try:
                amount = float(data["amount"])
            except ValueError:
                return jsonify({"error": "Amount must be numeric"}), 400

            contact = data["contact"].strip()

            # Check or create customer
            cursor.execute(
                "SELECT customer_id FROM customers WHERE contact_number=%s", (contact,))
            customer = cursor.fetchone()
            if customer:
                customer_id = customer["customer_id"]
            else:
                cursor.execute(
                    "INSERT INTO customers (name, contact_number) VALUES (%s, %s)", (data["name"], contact))
                conn.commit()
                customer_id = cursor.lastrowid

            # Create order
            transaction_id = generate_order_id()
            tracking_token = generate_tracking_token()
            tracking_url = f"http://192.168.1.221:5000/track/{tracking_token}"
            qr_path = os.path.join(QR_FOLDER, f"{transaction_id}.png")
            qrcode.make(tracking_url).save(qr_path)

            order_date = datetime.now()
            pickup_date = order_date + timedelta(days=2)

            cursor.execute("""
                INSERT INTO orders
                (customer_id, transaction_id, tracking_token, tracking_url, qr_code_path, name, service, weight, order_date, pickup_date, status, amount, contact, payment_method)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                customer_id, transaction_id, tracking_token, tracking_url, f"/static/qrcodes/{transaction_id}.png",
                data["name"], data["service"], weight, order_date, pickup_date, "New",
                amount, contact, data.get("payment")
            ))
            conn.commit()
            log_activity(
                f"Order {transaction_id} created for customer {customer_id}")

            return jsonify(map_order({
                "transaction_id": transaction_id,
                "tracking_token": tracking_token,
                "tracking_url": tracking_url,
                "qr_code_path": f"/static/qrcodes/{transaction_id}.png",
                "name": data["name"],
                "service": data["service"],
                "weight": weight,
                "amount": amount,
                "contact": contact,
                "payment_method": data.get("payment"),
                "order_date": order_date,
                "pickup_date": pickup_date,
                "status": "New"
            })), 201

        # GET all orders
        cursor.execute("SELECT * FROM orders ORDER BY order_date DESC")
        orders = cursor.fetchall()
        return jsonify([map_order(o) for o in orders])

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# -----------------------
# API - SINGLE ORDER (GET/PATCH/DELETE)
# -----------------------


@app.route("/api/orders/<transaction_id>", methods=["GET", "PATCH", "DELETE"])
@require_login
def api_order_detail(transaction_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM orders WHERE transaction_id=%s", (transaction_id,))
        order = cursor.fetchone()

        if not order:
            return jsonify({"error": "Order not found"}), 404

        if request.method == "GET":
            return jsonify(map_order(order))

        if request.method == "PATCH":
            data = request.get_json()
            if "status" not in data:
                return jsonify({"error": "Missing status"}), 400
            cursor.execute("UPDATE orders SET status=%s WHERE transaction_id=%s",
                           (data["status"], transaction_id))
            conn.commit()
            log_activity(
                f"Order {transaction_id} status updated to {data['status']}")
            return jsonify({"success": True})

        if request.method == "DELETE":
            cursor.execute(
                "DELETE FROM orders WHERE transaction_id=%s", (transaction_id,))
            conn.commit()
            log_activity(f"Order {transaction_id} deleted")
            return jsonify({"success": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# -----------------------
# QR TRACKING
# -----------------------


@app.route("/track/<token>")
def track_order(token):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE tracking_token=%s", (token,))
    order = cursor.fetchone()
    cursor.close()
    conn.close()
    if not order:
        return render_template("track_not_found.html"), 404
    return render_template("track_order.html", order=map_order(order))


@app.route("/api/track/<token>")
def api_track(token):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE tracking_token=%s", (token,))
    order = cursor.fetchone()
    cursor.close()
    conn.close()
    if not order:
        return jsonify({"error": "order not found"}), 404
    return jsonify(map_order(order))


@app.route("/api/reports/live")
@require_login
def api_reports_live():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        reports = {}

        # -------------------------
        # DAILY SALES (last 7 days)
        # -------------------------
        cursor.execute("""
            SELECT DATE(order_date) as day, COALESCE(SUM(amount),0) as total
            FROM orders
            GROUP BY DATE(order_date)
            ORDER BY day DESC
            LIMIT 7
        """)
        daily = cursor.fetchall()
        daily.reverse()

        reports["daily"] = {
            "labels": [row["day"].strftime("%Y-%m-%d") for row in daily],
            "data": [float(row["total"]) for row in daily]
        }

        # -------------------------
        # WEEKLY SALES (FIXED)
        # -------------------------
        cursor.execute("""
            SELECT YEAR(order_date) as year, WEEK(order_date) as week, COALESCE(SUM(amount),0) as total
            FROM orders
            GROUP BY YEAR(order_date), WEEK(order_date)
            ORDER BY year DESC, week DESC
            LIMIT 6
        """)
        weekly = cursor.fetchall()
        weekly.reverse()

        reports["weekly"] = {
            "labels": [f"{row['year']} - Week {row['week']}" for row in weekly],
            "data": [float(row["total"]) for row in weekly]
        }

        # -------------------------
        # MONTHLY SALES (FIXED)
        # -------------------------
        cursor.execute("""
            SELECT YEAR(order_date) as year, MONTH(order_date) as month, COALESCE(SUM(amount),0) as total
            FROM orders
            GROUP BY YEAR(order_date), MONTH(order_date)
            ORDER BY year DESC, month DESC
            LIMIT 6
        """)
        monthly = cursor.fetchall()
        monthly.reverse()

        reports["monthly"] = {
            "labels": [f"{row['year']}-{str(row['month']).zfill(2)}" for row in monthly],
            "data": [float(row["total"]) for row in monthly]
        }

        # -------------------------
        # SERVICE POPULARITY
        # -------------------------
        cursor.execute("""
            SELECT service, COUNT(*) as count
            FROM orders
            GROUP BY service
        """)
        popularity = cursor.fetchall()

        reports["popularity"] = {
            "labels": [row["service"] for row in popularity],
            "data": [int(row["count"]) for row in popularity]
        }

        return jsonify(reports)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()


# -----------------------
# RUN APP
# -----------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
