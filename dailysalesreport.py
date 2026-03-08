import os
import argparse
from square import Square
from square.environment import SquareEnvironment
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client as SupabaseClient

load_dotenv()

def main():
    parser = argparse.ArgumentParser(description="Generate and upload daily sales report to Supabase.")
    parser.add_argument("--date", type=str, help="The date for which to search (YYYY-MM-DD). Defaults to yesterday.")
    args = parser.parse_args()

    # --- Date Calculation ---
    if args.date:
        try:
            selected_date = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print("Invalid date format. Please use YYYY-MM-DD.")
            exit(1)
    else:
        # Default to yesterday
        selected_date = datetime.now(timezone.utc) - timedelta(days=1)

    user_date_str = selected_date.strftime("%Y-%m-%d")
    start_of_day = selected_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    start_of_next_day = start_of_day + timedelta(days=1)

    start_at_iso = start_of_day.isoformat().replace('+00:00', 'Z')
    end_at_iso = start_of_next_day.isoformat().replace('+00:00', 'Z')

    # --- Connect to Supabase & Load COGS ---
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SECRET_KEY")

    supabase: SupabaseClient = None
    COGS_MAPPING = {}

    if supabase_url and supabase_key:
        supabase = create_client(supabase_url, supabase_key)
        try:
            products_result = supabase.table("products").select("name, cogs").execute()
            COGS_MAPPING = {p["name"]: float(p["cogs"]) for p in products_result.data}
            print(f"Loaded {len(COGS_MAPPING)} products from Supabase.")
        except Exception as e:
            print(f"Warning: Could not load products from Supabase: {e}. COGS will be $0.")
    else:
        print("Warning: Supabase credentials missing. COGS will be $0.")

    client = Square(
        token=os.getenv("SQUARE_ACCESS_TOKEN"),
        environment=SquareEnvironment.PRODUCTION
    )

    location_id = os.getenv("SQUARE_LOCATION_ID")

    # --- 1. Search Orders ---
    order_result = client.orders.search(
        location_ids=[location_id],
        query={
            "filter": {
                "date_time_filter": {
                    "created_at": {
                        "start_at": start_at_iso,
                        "end_at": end_at_iso
                    }
                }
            }
        }
    )

    # --- 2. List Refunds ---
    refund_result = client.refunds.list(
        begin_time=start_at_iso,
        end_time=end_at_iso,
        location_id=location_id
    )

    # --- 3. List Payments (for Processing Fees) ---
    payment_result = client.payments.list(
        begin_time=start_at_iso,
        end_time=end_at_iso,
        location_id=location_id
    )

    gross_revenue = 0.0
    total_cogs = 0.0
    total_refunded = 0.0
    total_discounts = 0.0
    total_fees = 0.0

    # --- Process Orders (Gross, Discounts, COGS) ---
    if order_result.orders:
        for order in order_result.orders:
            order_discount = 0.0
            if order.total_discount_money:
                order_discount = float(order.total_discount_money.amount or 0) / 100.0
                total_discounts += order_discount

            if order.total_money:
                net_paid = float(order.total_money.amount or 0) / 100.0
                gross_revenue += (net_paid + order_discount)

            if order.line_items:
                for item in order.line_items:
                    name = item.name
                    qty = float(item.quantity or 0)
                    if name in COGS_MAPPING:
                        total_cogs += (COGS_MAPPING[name] * qty)

    # --- Process Refunds ---
    for refund in refund_result:
        if refund.status == 'COMPLETED':
            total_refunded += (float(refund.amount_money.amount or 0) / 100.0)

    # --- Process Processing Fees from Payments ---
    for payment in payment_result:
        if payment.status == 'COMPLETED' and payment.processing_fee:
            for fee in payment.processing_fee:
                total_fees += (float(fee.amount_money.amount or 0) / 100.0)

    # --- Final Summary Calculations ---
    # Net Revenue = Gross - Discounts - Refunds
    net_revenue = gross_revenue - total_refunded
    gross_profit = net_revenue - total_cogs
    net_income = gross_profit - total_discounts - total_fees

    print(f"\n{'='*40}")
    print(f"FINANCIAL REPORT: {user_date_str}")
    print(f"{'='*40}")
    print(f"Gross Sales:      ${gross_revenue:,.2f}")
    print(f"Total Refunds:   -${total_refunded:,.2f}")
    print(f"----------------------------------------")
    print(f"Net Revenue:      ${net_revenue:,.2f}")
    print(f"Total COGS:      -${total_cogs:,.2f}")
    print(f"----------------------------------------")
    print(f"GROSS INCOME:     ${gross_profit:,.2f}")
    print(f"Comps/Discounts: -${total_discounts:,.2f}")
    print(f"Processing Fees: -${total_fees:,.2f}")
    print(f"----------------------------------------")
    print(f"NET INCOME:       ${net_income:,.2f}")
    print(f"{'='*40}")

    # --- Upload to Supabase ---
    if not supabase:
        print("Supabase credentials missing. Skipping upload.")
        return

    data = {
        "date": user_date_str,
        "revenue": f"{gross_revenue:.2f}",
        "cogs": f"{total_cogs:.2f}",
        "processing": f"{total_fees:.2f}",
        "comps": f"{total_discounts:.2f}",
        "net_income": f"{net_income:.2f}"
    }

    try:
        # Upsert based on date to avoid duplicates if rerun
        response = supabase.table("daily_kpis").upsert(data, on_conflict="date").execute()
        print(f"Successfully uploaded data to Supabase for {user_date_str}")
    except Exception as e:
        print(f"Error uploading to Supabase: {e}")

if __name__ == "__main__":
    main()
