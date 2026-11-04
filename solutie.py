import sys
import os
import time
import math
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import requests

def norm(val: Decimal, min_val: Decimal, max_val: Decimal) -> Decimal:
    """norm(x) = (x - min) / (max - min), rotunjit half-up la 6 zecimale; 0 cand max == min."""
    if max_val == min_val:
        return Decimal("0.000000")
    raw = (val - min_val) / (max_val - min_val)
    return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

class HubClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def request(self, method: str, path: str, idempotency_key: str = None, json_body: dict = None, params: dict = None, is_notification: bool = False):
        url = f"{self.base_url}{path}"
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        attempts = 0
        rate_limits = 0

        while True:
            attempts += 1
            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=10
                )
                status = resp.status_code

                if status in (200, 201):
                    return resp.json()

                if status == 422:
                    data = resp.json() if resp.content else {}
                    err_code = data.get("error", {}).get("code", "")
                    if err_code == "IDEMPOTENCY_KEY_REUSED":
                        print(f"[{method} {path}] 422 IDEMPOTENCY_KEY_REUSED (replay / conflict)", file=sys.stderr)
                        return None
                    print(f"[{method} {path}] 422 VALIDATION_ERROR: {resp.text}", file=sys.stderr)
                    sys.exit(1)

                if status == 429:
                    err_code = resp.json().get("error", {}).get("code", "") if resp.content else ""
                    if err_code == "RUN_REQUEST_LIMIT":
                        print("Run request limit reached. Exiting.", file=sys.stderr)
                        sys.exit(1)
                    
                    rate_limits += 1
                    if rate_limits > 10:
                        print("Too many 429 retries. Exiting.", file=sys.stderr)
                        sys.exit(1)
                    
                    wait_sec = int(resp.headers.get("Retry-After", 1))
                    print(f"[429 RATE_LIMITED] Asteptam {wait_sec}s...", file=sys.stderr)
                    time.sleep(wait_sec)
                    attempts -= 1
                    continue

                if status in (500, 502, 503, 504):
                    err_data = resp.json() if resp.content else {}
                    err_code = err_data.get("error", {}).get("code", "")

                    if err_code == "RUN_PAUSED":
                        sys.exit(1)

                    if attempts >= 3:
                        if is_notification and status == 500:
                            print(f"[C-R14] 500 persistent la notificare {path}. O sarim.", file=sys.stderr)
                            return None
                        if path == "/purchase-orders" and status == 500:
                            return None
                        print(f"[{method} {path}] Esec dupa 3 incercari: {status} {resp.text}", file=sys.stderr)
                        sys.exit(1)

                    wait_sec = int(resp.headers.get("Retry-After", 1))
                    print(f"[{status}] Reincercare {attempts}/3 dupa {wait_sec}s...", file=sys.stderr)
                    time.sleep(wait_sec)
                    continue

                print(f"Eroare neasteptata {status} pe {path}: {resp.text}", file=sys.stderr)
                sys.exit(1)

            except (requests.RequestException, requests.Timeout) as e:
                if attempts >= 3:
                    print(f"Network error dupa 3 incercari pe {path}: {e}", file=sys.stderr)
                    sys.exit(1)
                print(f"Network error: {e}. Reincercare {attempts}/3...", file=sys.stderr)
                time.sleep(1)


def main():
    base_url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BPA_BASE_URL", "")
    if not base_url:
        print("Lipseste BASE_URL!", file=sys.stderr)
        sys.exit(1)

    client = HubClient(base_url)

    # 1. Date context si config
    ctx = client.request("GET", "/context")
    run_id = ctx["run_id"]
    business_date_str = ctx["business_date"]
    business_date = datetime.strptime(business_date_str, "%Y-%m-%d").date()

    config = client.request("GET", "/config")
    horizon_days = int(config["horizon_days"])
    horizon_end = business_date + timedelta(days=horizon_days)
    price_increase_pct = Decimal(str(config["price_increase_review_pct"]))
    max_lead_time_days_map = config["max_lead_time_days"]
    notification_recipients = config["notification_recipients"]

    # CR-1: selection_method si selection_weights
    selection_method = config.get("selection_method", "LOWEST_COST")
    selection_weights = config.get("selection_weights", {})
    w_cost = Decimal(str(selection_weights.get("cost", "0"))) if selection_weights else Decimal("0")
    w_lead = Decimal(str(selection_weights.get("lead_time", "0"))) if selection_weights else Decimal("0")
    w_rel = Decimal(str(selection_weights.get("reliability", "0"))) if selection_weights else Decimal("0")

    # 2. Citire entitati principale
    items_list = client.request("GET", "/items")
    stock_list = client.request("GET", "/stock")
    demand_list = client.request("GET", "/demand")
    suppliers_list = client.request("GET", "/suppliers")
    cost_centers_list = client.request("GET", "/cost-centers")
    fx_rates_list = client.request("GET", "/fx-rates")
    po_list = client.request("GET", "/purchase-orders")

    # Indexare date
    stock_map = {s["sku"]: (s.get("on_hand", 0), s.get("reserved", 0)) for s in stock_list}
    demand_map = {d["sku"]: Decimal(str(d.get("avg_daily_demand", "0"))) for d in demand_list}
    suppliers_map = {s["supplier_id"]: s for s in suppliers_list}
    budget_map = {cc["cost_center"]: Decimal(str(cc["budget_ron"])) for cc in cost_centers_list}
    spent_map = {cc["cost_center"]: Decimal("0.00") for cc in cost_centers_list}

    # Calcul curs EUR -> RON la data de lucru sau cea mai recenta zi anterioara
    eur_rates = {}
    for fx in fx_rates_list:
        d = datetime.strptime(fx["date"], "%Y-%m-%d").date()
        eur_rates[d] = Decimal(str(fx["rate_ron"]))
    
    eur_rate_today = None
    for d in sorted(eur_rates.keys(), reverse=True):
        if d <= business_date:
            eur_rate_today = eur_rates[d]
            break

    # Incoming per SKU din comenzi OPEN cu eta <= horizon_end, ignorand run_id-ul curent
    incoming_map = {}
    for po in po_list:
        if po.get("status") == "OPEN" and po.get("run_id") != run_id:
            for line in po.get("lines", []):
                eta_str = line.get("eta")
                if eta_str:
                    line_eta = datetime.strptime(eta_str, "%Y-%m-%d").date()
                    if line_eta <= horizon_end:
                        sku = line["sku"]
                        rem = line.get("quantity", 0) - line.get("received_quantity", 0)
                        if rem > 0:
                            incoming_map[sku] = incoming_map.get(sku, 0) + rem

    # 3. Sortare canonica: Criticality (A, B, C) apoi SKU crescator
    crit_order = {"A": 0, "B": 1, "C": 2}
    sorted_items = sorted(items_list, key=lambda it: (crit_order.get(it["criticality"], 9), it["sku"]))

    decisions = {}
    orders_by_supplier = {}

    # 4. Decizie in memorie pentru fiecare articol
    for item in sorted_items:
        sku = item["sku"]
        pack_size = item["pack_size"]
        safety_stock = item["safety_stock"]
        max_stock = item["max_stock"]
        ref_lead = item["reference_lead_time_days"]
        criticality = item["criticality"]
        cost_center = item["cost_center"]
        last_price_str = item.get("last_purchase_price_ron")
        last_price_ron = Decimal(str(last_price_str)) if last_price_str is not None else None

        # C-R01: Discontinued
        if item.get("discontinued", False):
            decisions[sku] = {
                "sku": sku,
                "outcome": "NO_ACTION",
                "reason_codes": ["ITEM_DISCONTINUED"],
                "po_number": None,
                "order_fields": None,
                "criticality": criticality
            }
            continue

        # C-R02 & C-R03: Pozitie stoc si Punct de reaprovizionare
        on_hand, reserved = stock_map.get(sku, (0, 0))
        incoming = incoming_map.get(sku, 0)
        stock_pos = on_hand - reserved + incoming

        avg_demand = demand_map.get(sku, Decimal("0"))
        reorder_point = math.ceil(avg_demand * ref_lead) + safety_stock

        if stock_pos > reorder_point:
            decisions[sku] = {
                "sku": sku,
                "outcome": "NO_ACTION",
                "reason_codes": ["ABOVE_REORDER_POINT"],
                "po_number": None,
                "order_fields": None,
                "criticality": criticality
            }
            continue

        # C-R04: Calcul cantitate
        need = max_stock - stock_pos
        base_qty = math.ceil(need / pack_size) * pack_size

        # C-R05: Oferte eligibile
        offers_resp = client.request("GET", f"/offers?sku={sku}")
        eligible_offers = []

        for off in offers_resp:
            sup_id = off["supplier_id"]
            sup = suppliers_map.get(sup_id, {})
            if sup.get("status") != "ACTIVE":
                continue

            v_from = datetime.strptime(off["valid_from"], "%Y-%m-%d").date()
            v_to = datetime.strptime(off["valid_to"], "%Y-%m-%d").date() if off.get("valid_to") else None
            if not (v_from <= business_date and (v_to is None or business_date <= v_to)):
                continue

            max_allowed_lead = max_lead_time_days_map[criticality]
            if off["lead_time_days"] > max_allowed_lead:
                continue

            moq = off.get("moq", 1)
            off_qty = max(base_qty, math.ceil(moq / pack_size) * pack_size)
            if off_qty > off["max_qty"]:
                continue

            # C-R06: Pret treapta si cost
            tiers = sorted(off["price_tiers"], key=lambda t: t["min_qty"], reverse=True)
            chosen_tier = next((t for t in tiers if off_qty >= t["min_qty"]), tiers[-1])
            unit_price = Decimal(str(chosen_tier["unit_price"]))

            rate = Decimal("1.00") if off["currency"] == "RON" else eur_rate_today
            total_cost_ron = (Decimal(off_qty) * unit_price * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            eligible_offers.append({
                "supplier_id": sup_id,
                "currency": off["currency"],
                "quantity": off_qty,
                "unit_price": chosen_tier["unit_price"],
                "unit_price_dec": unit_price,
                "rate": rate,
                "total_cost_ron": total_cost_ron,
                "lead_time_days": off["lead_time_days"],
                "on_time_rate": Decimal(str(sup.get("on_time_rate", "0")))
            })

        if not eligible_offers:
            decisions[sku] = {
                "sku": sku,
                "outcome": "MANUAL_REVIEW",
                "reason_codes": ["NO_ELIGIBLE_SUPPLIER"],
                "po_number": None,
                "order_fields": None,
                "criticality": criticality
            }
            continue

        # C-R07: Alegere oferta (CR-1: WEIGHTED_SCORE vs LOWEST_COST)
        if selection_method == "WEIGHTED_SCORE":
            min_cost = min(o["total_cost_ron"] for o in eligible_offers)
            max_cost = max(o["total_cost_ron"] for o in eligible_offers)
            min_lead = Decimal(str(min(o["lead_time_days"] for o in eligible_offers)))
            max_lead = Decimal(str(max(o["lead_time_days"] for o in eligible_offers)))

            for o in eligible_offers:
                n_cost = norm(o["total_cost_ron"], min_cost, max_cost)
                n_lead = norm(Decimal(str(o["lead_time_days"])), min_lead, max_lead)
                # scor = w_cost * norm_cost + w_lead * norm_lead + w_reliability * (1 - on_time_rate)
                o["score"] = w_cost * n_cost + w_lead * n_lead + w_rel * (Decimal("1") - o["on_time_rate"])

            # Departajare: scor minim, apoi cost total, lead_time, -on_time_rate, supplier_id
            eligible_offers.sort(key=lambda o: (
                o["score"],
                o["total_cost_ron"],
                o["lead_time_days"],
                -o["on_time_rate"],
                o["supplier_id"]
            ))
        else:
            # LOWEST_COST standard
            eligible_offers.sort(key=lambda o: (
                o["total_cost_ron"],
                o["lead_time_days"],
                -o["on_time_rate"],
                o["supplier_id"]
            ))

        best = eligible_offers[0]

        # C-R08 & C-R09: Evaluare scumpire si buget
        reasons = []
        if last_price_ron is not None:
            threshold = last_price_ron * (Decimal("1") + price_increase_pct / Decimal("100"))
            if best["unit_price_dec"] * best["rate"] > threshold:
                reasons.append("PRICE_INCREASE_REVIEW")

        current_spent = spent_map[cost_center]
        total_budget = budget_map.get(cost_center, Decimal("0.00"))
        if current_spent + best["total_cost_ron"] > total_budget:
            reasons.append("BUDGET_EXCEEDED")

        order_fields = {
            "supplier_id": best["supplier_id"],
            "quantity": best["quantity"],
            "unit_price": str(best["unit_price"]),
            "currency": best["currency"],
            "total_cost_ron": str(best["total_cost_ron"])
        }

        if reasons:
            decisions[sku] = {
                "sku": sku,
                "outcome": "MANUAL_REVIEW",
                "reason_codes": reasons,
                "po_number": None,
                "order_fields": order_fields,
                "criticality": criticality
            }
        else:
            spent_map[cost_center] += best["total_cost_ron"]
            decisions[sku] = {
                "sku": sku,
                "outcome": "ORDER_CREATED",
                "reason_codes": [],
                "po_number": None,
                "order_fields": order_fields,
                "criticality": criticality
            }
            sup_id = best["supplier_id"]
            if sup_id not in orders_by_supplier:
                orders_by_supplier[sup_id] = []
            orders_by_supplier[sup_id].append((sku, best["quantity"]))

    # 5. POST /purchase-orders (C-R10, C-R12)
    po_numbers = {}
    failed_po_suppliers = set()

    for sup_id, lines in orders_by_supplier.items():
        lines_sorted = sorted(lines, key=lambda l: l[0])
        po_payload = {
            "supplier_id": sup_id,
            "lines": [{"sku": l[0], "quantity": l[1]} for l in lines_sorted]
        }
        res = client.request("POST", "/purchase-orders", idempotency_key=f"{sup_id}:po", json_body=po_payload)
        if res and "po_number" in res:
            po_numbers[sup_id] = res["po_number"]
        else:
            failed_po_suppliers.add(sup_id)

    # Actualizare decizii dupa comenzi
    for sku, dec in decisions.items():
        if dec["outcome"] == "ORDER_CREATED":
            sup_id = dec["order_fields"]["supplier_id"]
            if sup_id in failed_po_suppliers:
                dec["outcome"] = "MANUAL_REVIEW"
                dec["reason_codes"] = ["PO_CREATION_FAILED"]
                dec["po_number"] = None
            else:
                dec["po_number"] = po_numbers.get(sup_id)

    # 6. POST /replenishment-decisions (C-R13)
    for sku in [it["sku"] for it in items_list]:
        dec = decisions[sku]
        body = {
            "sku": sku,
            "outcome": dec["outcome"],
            "reason_codes": dec["reason_codes"]
        }
        if dec["order_fields"] is not None:
            body.update(dec["order_fields"])
            body["po_number"] = dec["po_number"]
        
        client.request("POST", "/replenishment-decisions", idempotency_key=f"{sku}:decision", json_body=body)

    # 7. POST /notifications (C-R14)
    for sup_id, po_num in po_numbers.items():
        sup_email = suppliers_map[sup_id].get("email")
        if sup_email:
            notif_body = {
                "template": "po_created",
                "recipient_role": "SUPPLIER",
                "recipient": sup_email,
                "po_number": po_num
            }
            client.request("POST", "/notifications", idempotency_key=f"{po_num}:po_created", json_body=notif_body, is_notification=True)

    proc_email = notification_recipients.get("PROCUREMENT_MANAGER")
    wh_email = notification_recipients.get("WAREHOUSE_MANAGER")

    for sku in [it["sku"] for it in items_list]:
        dec = decisions[sku]
        if dec["outcome"] == "MANUAL_REVIEW":
            if proc_email:
                client.request("POST", "/notifications", idempotency_key=f"{sku}:replenishment_review", json_body={
                    "template": "replenishment_review",
                    "recipient_role": "PROCUREMENT_MANAGER",
                    "recipient": proc_email,
                    "sku": sku
                }, is_notification=True)

            if dec["criticality"] == "A" and wh_email:
                client.request("POST", "/notifications", idempotency_key=f"{sku}:stockout_risk", json_body={
                    "template": "stockout_risk",
                    "recipient_role": "WAREHOUSE_MANAGER",
                    "recipient": wh_email,
                    "sku": sku
                }, is_notification=True)

    # 8. POST /report
    report_items = []
    for it in items_list:
        sku = it["sku"]
        dec = decisions[sku]
        report_items.append({
            "sku": sku,
            "outcome": dec["outcome"],
            "reason_codes": dec["reason_codes"],
            "po_number": dec["po_number"]
        })

    client.request("POST", "/report", idempotency_key="report", json_body={"items": report_items})
    print("Rulare finalizata cu succes!")
    sys.exit(0)

if __name__ == "__main__":
    main()