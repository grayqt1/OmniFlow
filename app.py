import json
import os
from datetime import datetime, timedelta, date
from pathlib import Path
from collections import Counter, defaultdict

from flask import Flask, jsonify, request, send_from_directory, redirect, abort, session, Response
from flask_cors import CORS

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
CORS(app)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-omniflow")

def load_data():
    with DATA_FILE.open() as f:
        return json.load(f)


def save_data(data):
    with DATA_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def today():
    return date.today()


def next_id(prefix, existing_ids):
    nums = [int(str(i).split("-")[-1]) for i in existing_ids if str(i).startswith(prefix + "-")]
    nxt = max(nums, default=0) + 1
    return f"{prefix}-{nxt:04d}"


def attach_client(data, order):
    client = next((c for c in data["clients"] if c["id"] == order["client_id"]), None)
    order = {**order}
    order["client_name"] = client["name"] if client else "Unknown"
    return order


def item_status(itm):
    if itm["stock"] == 0:
        return "out"
    if itm["stock"] <= itm["min_stock"]:
        return "low"
    return "ok"


def order_amount(order):
    """Return numeric amount regardless of stored type (string/number)."""
    try:
        return float(order.get("amount", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def ensure_shop(data):
    """Ensure shop profile exists."""
    if "shop" not in data:
        user = data.get("user", {})
        auth = data.get("auth", {})
        data["shop"] = {
            "name": user.get("name", "Omni Workshop"),
            "industry": "Manufacturing",
            "address": "",
            "email": auth.get("email", ""),
        }
    return data["shop"]


def ensure_users(data):
    """Ensure users list exists, seeded from primary user."""
    if "users" not in data or not data["users"]:
        user = data.get("user", {})
        auth = data.get("auth", {})
        data["users"] = [{
            "id": "U-0001",
            "name": user.get("name", "User"),
            "email": auth.get("email", "user@example.com"),
            "role": user.get("role", "User"),
        }]
    return data["users"]


def add_activity(data, message, typ="info"):
    existing_ids = [a.get("id", 0) for a in data.get("activity", [])]
    new_id = (max(existing_ids) + 1) if existing_ids else 1
    entry = {
        "id": new_id,
        "message": message,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "type": typ,
    }
    data.setdefault("activity", []).insert(0, entry)
    # keep the feed reasonably short
    if len(data["activity"]) > 100:
        data["activity"] = data["activity"][:100]


def build_production_jobs(data):
    """Derive production jobs from orders so statuses stay in sync."""
    orders_by_id = {o["id"]: attach_client(data, o) for o in data.get("orders", [])}
    meta = {p["order_id"]: p for p in data.get("production", [])}
    jobs = []
    for order_id, order in orders_by_id.items():
        m = meta.get(order_id, {})
        jobs.append({
            "order_id": order_id,
            "product": order.get("product", m.get("product", "Unknown")),
            "client_name": order.get("client_name") or m.get("client_name", "Unknown"),
            "status": order.get("status", m.get("status", "queued")),
            "priority": m.get("priority", "normal"),
            "due_date": order.get("due_date", m.get("due_date", today().isoformat())),
        })
    return jobs


def compute_home_stats(data):
    orders = data["orders"]
    inventory = data["inventory"]
    active_orders = sum(1 for o in orders if o["status"] in {"pending", "in-progress", "queued"})
    in_production = sum(1 for o in orders if o["status"] == "in-progress")
    total_clients = len(data["clients"])
    today_date = today()
    due_this_week = sum(1 for o in orders if parse_date(o["due_date"]) <= today_date + timedelta(days=7) and parse_date(o["due_date"]) >= today_date)
    low_stock_count = sum(1 for i in inventory if i["stock"] <= i["min_stock"])
    return {
        "active_orders": active_orders,
        "in_production": in_production,
        "total_clients": total_clients,
        "due_this_week": due_this_week,
        "low_stock_count": low_stock_count,
    }


def month_key(dt_obj):
    return dt_obj.strftime("%Y-%m")


def aggregate_monthly(orders, months=6):
    end = today().replace(day=1)
    months_list = []
    for i in range(months-1, -1, -1):
        month = (end - timedelta(days=30*i)).replace(day=1)
        months_list.append(month)
    labels = [m.strftime("%b %Y") for m in months_list]
    revenue = []
    count = []
    summary_rows = []
    grouped = defaultdict(list)
    for o in orders:
        d = parse_date(o["created_at"])
        grouped[month_key(d)].append(o)
    for m in months_list:
        key = month_key(m)
        items = grouped.get(key, [])
        rev = sum(order_amount(o) for o in items)
        revenue.append(rev)
        count.append(len(items))
        completed = sum(1 for o in items if o["status"] == "done")
        cancelled = sum(1 for o in items if o["status"] == "cancelled")
        avg_turnaround = round(sum(o.get("turnaround_days", 0) for o in items) / len(items), 1) if items else 0
        summary_rows.append({
            "month": m.strftime("%b %Y"),
            "orders": len(items),
            "revenue": rev,
            "completed": completed,
            "cancelled": cancelled,
            "avg_turnaround": avg_turnaround,
        })
    return labels, revenue, count, summary_rows


def build_report(data, report_type="revenue", date_from=None, date_to=None):
    orders = data["orders"]
    if date_from:
        df = parse_date(date_from)
        orders = [o for o in orders if parse_date(o["created_at"]) >= df]
    if date_to:
        dt = parse_date(date_to)
        orders = [o for o in orders if parse_date(o["created_at"]) <= dt]
    labels, revenue, orders_count, summary_rows = aggregate_monthly(orders, months=6)
    metric = orders_count if report_type == "orders" else revenue
    return {
        "labels": labels,
        "revenue": revenue,
        "orders": orders_count,
        "metric": metric,
        "summary": summary_rows,
    }


@app.route("/")
def index():
    return redirect("/home.html")


@app.route("/home")
def home_route():
    return send_from_directory(app.static_folder, "home.html")


@app.route("/login")
def login_page():
    return send_from_directory(app.static_folder, "login.html")


# ---------- Auth helpers ----------
PUBLIC_EXT = {".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".ttf", ".map"}
PUBLIC_PATHS = {"/", "/login", "/api/login"}


@app.before_request
def require_login():
    path = request.path
    if request.method == "OPTIONS":
        return
    if path in PUBLIC_PATHS or any(path.endswith(ext) for ext in PUBLIC_EXT):
        return
    if session.get("user"):
        return
    # allow direct access to data.json for now (static)
    if path.endswith("data.json"):
        return
    if path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect("/login")


@app.route("/api/login", methods=["POST"])
def api_login():
    payload = request.get_json(force=True)
    email = (payload.get("email") or "").lower()
    password = payload.get("password") or ""
    data = load_data()
    auth = data.get("auth", {})
    if email == (auth.get("email") or "").lower() and password == auth.get("password"):
        session["user"] = {"name": data.get("user", {}).get("name", "User"), "role": data.get("user", {}).get("role", "User")}
        return jsonify(session["user"])
    return jsonify({"error": "invalid credentials"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user", None)
    return "", 204


# ---------- API: Home ----------
@app.route("/api/home/stats")
def home_stats():
    data = load_data()
    return jsonify(compute_home_stats(data))


# ---------- API: Orders ----------
@app.route("/api/orders", methods=["GET", "POST"])
def orders_collection():
    data = load_data()
    if request.method == "GET":
        search = (request.args.get("search") or "").lower()
        status = request.args.get("status")
        client_id = request.args.get("client_id")
        orders = data["orders"]
        if search:
            orders = [o for o in orders if search in o["product"].lower() or search in o.get("id", "").lower()]
        if status:
            orders = [o for o in orders if o["status"] == status]
        if client_id:
            orders = [o for o in orders if o["client_id"] == client_id]
        orders = [attach_client(data, o) for o in orders]
        orders.sort(key=lambda o: o["due_date"])
        return jsonify(orders)

    payload = request.get_json(force=True)
    new_id = next_id("ORD", [o["id"] for o in data["orders"]])
    order = {
        "id": new_id,
        "client_id": payload.get("client_id"),
        "product": payload.get("product", "Unnamed Product"),
        "qty": int(payload.get("qty", 0)),
        "status": payload.get("status", "pending"),
        "due_date": payload.get("due_date") or today().isoformat(),
        "notes": payload.get("notes", ""),
        "amount": float(payload.get("amount", 0) or 0),
        "created_at": today().isoformat(),
        "turnaround_days": payload.get("turnaround_days", 7),
    }
    data["orders"].append(order)
    add_activity(data, f"Created order {order['id']}", "info")
    save_data(data)
    return jsonify(attach_client(data, order)), 201


@app.route("/api/orders/recent")
def orders_recent():
    data = load_data()
    orders = sorted(data["orders"], key=lambda o: o.get("created_at", ""), reverse=True)[:5]
    orders = [attach_client(data, o) for o in orders]
    return jsonify(orders)


@app.route("/api/orders/<order_id>", methods=["GET", "PUT", "DELETE"])
def order_detail(order_id):
    data = load_data()
    orders = data["orders"]
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        abort(404)

    if request.method == "GET":
        return jsonify(attach_client(data, order))

    if request.method == "DELETE":
        data["orders"] = [o for o in orders if o["id"] != order_id]
        add_activity(data, f"Deleted order {order_id}", "warning")
        save_data(data)
        return "", 204

    payload = request.get_json(force=True)
    for field in ["product", "qty", "status", "due_date", "notes", "amount", "client_id"]:
        if field in payload:
            if field == "qty":
                order[field] = int(payload[field])
            elif field == "amount":
                order[field] = float(payload[field] or 0)
            else:
                order[field] = payload[field]
    add_activity(data, f"Updated order {order_id}", "info")
    save_data(data)
    return jsonify(attach_client(data, order))


# ---------- API: Clients ----------
@app.route("/api/clients", methods=["GET", "POST"])
def clients_collection():
    data = load_data()
    if request.method == "GET":
        search = (request.args.get("search") or "").lower()
        clients = data["clients"]
        if search:
            clients = [c for c in clients if search in c["name"].lower() or search in c.get("contact", "").lower()]
        return jsonify(clients)

    payload = request.get_json(force=True)
    new_id = next_id("C", [c["id"] for c in data["clients"]])
    client = {
        "id": new_id,
        "name": payload.get("name", "New Client"),
        "contact": payload.get("contact", ""),
        "email": payload.get("email", ""),
        "phone": payload.get("phone", ""),
        "address": payload.get("address", ""),
        "total_orders": 0,
        "active_orders": 0,
        "since": str(today().year),
    }
    data["clients"].append(client)
    add_activity(data, f"Added client {client['name']}", "info")
    save_data(data)
    return jsonify(client), 201


@app.route("/api/clients/<client_id>", methods=["GET", "PUT", "DELETE"])
def client_detail(client_id):
    data = load_data()
    client = next((c for c in data["clients"] if c["id"] == client_id), None)
    if not client:
        abort(404)

    if request.method == "GET":
        return jsonify(client)

    if request.method == "DELETE":
        data["clients"] = [c for c in data["clients"] if c["id"] != client_id]
        add_activity(data, f"Deleted client {client['name']}", "warning")
        save_data(data)
        return "", 204

    payload = request.get_json(force=True)
    for field in ["name", "contact", "email", "phone", "address", "total_orders", "active_orders", "since"]:
        if field in payload:
            client[field] = payload[field]
    add_activity(data, f"Updated client {client['name']}", "info")
    save_data(data)
    return jsonify(client)


# ---------- API: Inventory ----------
@app.route("/api/inventory", methods=["GET", "POST"])
def inventory_collection():
    data = load_data()
    if request.method == "GET":
        search = (request.args.get("search") or "").lower()
        status = request.args.get("status")
        items = data["inventory"]
        if search:
            items = [i for i in items if search in i["name"].lower() or search in i.get("category", "").lower()]
        if status:
            items = [i for i in items if item_status(i) == status]
        return jsonify([{**i, "status": item_status(i)} for i in items])

    payload = request.get_json(force=True)
    new_id = next_id("INV", [i["id"] for i in data["inventory"]])
    item = {
        "id": new_id,
        "name": payload.get("name", "New Item"),
        "category": payload.get("category", "General"),
        "stock": int(payload.get("stock", 0)),
        "min_stock": int(payload.get("min_stock", 0)),
        "unit": payload.get("unit", "pcs"),
    }
    data["inventory"].append(item)
    add_activity(data, f"Added inventory item {item['name']}", "info")
    save_data(data)
    return jsonify({**item, "status": item_status(item)}), 201


@app.route("/api/inventory/<item_id>", methods=["GET", "PUT", "DELETE"])
def inventory_detail(item_id):
    data = load_data()
    item = next((i for i in data["inventory"] if i["id"] == item_id), None)
    if not item:
        abort(404)

    if request.method == "GET":
        return jsonify({**item, "status": item_status(item)})

    if request.method == "DELETE":
        data["inventory"] = [i for i in data["inventory"] if i["id"] != item_id]
        add_activity(data, f"Deleted inventory item {item['name']}", "warning")
        save_data(data)
        return "", 204

    payload = request.get_json(force=True)
    for field in ["name", "category", "stock", "min_stock", "unit"]:
        if field in payload:
            if field in ["stock", "min_stock"]:
                item[field] = int(payload[field])
            else:
                item[field] = payload[field]
    add_activity(data, f"Updated inventory item {item['name']}", "info")
    save_data(data)
    return jsonify({**item, "status": item_status(item)})


# ---------- API: Production ----------
@app.route("/api/production/board")
def production_board():
    data = load_data()
    board = {k: [] for k in ["pending", "queued", "in-progress", "review", "done", "cancelled"]}
    for job in build_production_jobs(data):
        status = job["status"]
        bucket = board.get(status, board["queued"])
        job_copy = dict(job)
        job_copy["is_overdue"] = parse_date(job_copy["due_date"]) < today()
        bucket.append(job_copy)
    return jsonify(board)


# ---------- API: Activity ----------
@app.route("/api/activity")
def activity():
    data = load_data()
    return jsonify(data["activity"])


# ---------- API: Dashboard ----------
@app.route("/api/dashboard/kpis")
def dashboard_kpis():
    data = load_data()
    period = request.args.get("period", "30D")
    days = {"7D": 7, "30D": 30, "90D": 90, "1Y": 365}.get(period, 30)
    cutoff = today() - timedelta(days=days)
    orders = [o for o in data["orders"] if parse_date(o["created_at"]) >= cutoff]
    revenue = sum(order_amount(o) for o in orders)
    completed = sum(1 for o in orders if o["status"] == "done")
    cancelled = sum(1 for o in orders if o["status"] == "cancelled")
    avg_turnaround = round(sum(o.get("turnaround_days", 0) for o in orders) / len(orders), 1) if orders else 0
    return jsonify({
        "revenue": revenue,
        "orders_completed": completed,
        "avg_turnaround": avg_turnaround,
        "cancelled": cancelled,
        "revenue_change": "+12% vs prior period",
        "orders_change": "+4% vs prior period",
        "turnaround_change": "-0.5 days",
        "cancelled_change": "-1 order",
    })


@app.route("/api/dashboard/revenue")
def dashboard_revenue():
    data = load_data()
    labels, revenue, _, _ = aggregate_monthly(data["orders"], months=6)
    return jsonify({"labels": labels, "values": revenue})


@app.route("/api/dashboard/order-status")
def dashboard_order_status():
    data = load_data()
    status_labels = ["Completed", "In Progress", "Pending", "Queued", "Cancelled"]
    mapping = {"Completed": "done", "In Progress": "in-progress", "Pending": "pending", "Queued": "queued", "Cancelled": "cancelled"}
    values = []
    for label in status_labels:
        values.append(sum(1 for o in data["orders"] if o["status"] == mapping[label]))
    return jsonify({"labels": status_labels, "values": values})


@app.route("/api/dashboard/throughput")
def dashboard_throughput():
    data = load_data()
    jobs = build_production_jobs(data)
    # jobs completed per week (last 8 weeks)
    today_date = today()
    labels = []
    values = []
    for i in range(7, -1, -1):
        start = today_date - timedelta(days=i*7 + 6)
        end = today_date - timedelta(days=i*7)
        label = f"Week {end.strftime('%W')}"
        count = sum(1 for j in jobs if j["status"] == "done" and start <= parse_date(j["due_date"]) <= end)
        labels.append(label)
        values.append(count)
    return jsonify({"labels": labels, "values": values})


@app.route("/api/dashboard/top-products")
def dashboard_top_products():
    data = load_data()
    counter = Counter(o["product"] for o in data["orders"])
    top = counter.most_common(5)
    return jsonify([{"name": name, "count": count} for name, count in top])


# ---------- API: Reports ----------
@app.route("/api/reports")
def reports():
    data = load_data()
    report_type = request.args.get("type", "revenue")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    return jsonify(build_report(data, report_type, date_from, date_to))


@app.route("/api/reports/forecast")
def reports_forecast():
    data = load_data()
    next_month_orders = int(sum(1 for o in data["orders"] if o["status"] in {"pending", "queued"}) * 1.1)
    low_stock = sum(1 for i in data["inventory"] if i["stock"] <= i["min_stock"])
    forecast = (
        f"Expected to ship ~{next_month_orders} orders next month based on current pipeline. "
        f"{low_stock} SKU(s) are at or below safety stock; prioritize replenishment to avoid delays."
    )
    return jsonify({"forecast_html": forecast})


@app.route("/api/reports/export")
def reports_export():
    fmt = request.args.get("format", "csv")
    report_type = request.args.get("type", "revenue")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    data = build_report(load_data(), report_type, date_from, date_to)
    lines = ["Label,Revenue,Orders"]
    for lbl, rev, cnt in zip(data["labels"], data["revenue"], data["orders"]):
        lines.append(f'"{lbl}",{rev},{cnt}')
    csv_bytes = "\n".join(lines).encode("utf-8")
    if fmt == "pdf":
        return Response(csv_bytes, mimetype="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=report.pdf"})
    return Response(csv_bytes, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=report.csv"})


# ---------- API: Settings ----------
@app.route("/api/settings/shop", methods=["GET", "PUT"])
def settings_shop():
    data = load_data()
    shop = ensure_shop(data)
    if request.method == "GET":
        return jsonify(shop)
    payload = request.get_json(force=True)
    for field in ["name", "industry", "address", "email"]:
        if field in payload:
            shop[field] = payload[field]
    data["shop"] = shop
    save_data(data)
    return jsonify(shop)


@app.route("/api/settings/users", methods=["GET", "POST"])
def settings_users():
    data = load_data()
    users = ensure_users(data)
    if request.method == "GET":
        return jsonify(users)
    payload = request.get_json(force=True)
    new_id = next_id("U", [u["id"] for u in users])
    user = {
        "id": new_id,
        "name": payload.get("name", "New User"),
        "email": payload.get("email", ""),
        "role": payload.get("role", "Member"),
    }
    users.append(user)
    add_activity(data, f"Invited user {user['name']}", "info")
    data["users"] = users
    save_data(data)
    return jsonify(user), 201


@app.route("/api/settings/users/<user_id>", methods=["DELETE"])
def settings_users_delete(user_id):
    data = load_data()
    users = ensure_users(data)
    before = len(users)
    users = [u for u in users if u["id"] != user_id]
    if len(users) == before:
        abort(404)
    data["users"] = users
    add_activity(data, f"Removed user {user_id}", "warning")
    save_data(data)
    return "", 204


# ---------- API: Me ----------
@app.route("/api/me", methods=["GET", "PUT"])
def me():
    data = load_data()
    if request.method == "GET":
        user = data.get("user", {"name": "Omni User", "role": "Manager"})
        return jsonify(user)
    payload = request.get_json(force=True)
    data.setdefault("user", {})
    data.setdefault("auth", {})
    if "name" in payload:
        data["user"]["name"] = payload["name"]
    if "role" in payload:
        data["user"]["role"] = payload["role"]
    if "email" in payload:
        data["auth"]["email"] = payload["email"]
    save_data(data)
    return jsonify(data["user"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
