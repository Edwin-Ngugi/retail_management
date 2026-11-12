import os
import sys
import django

# Auto-detect manage.py's DJANGO_SETTINGS_MODULE so it runs without manual path edits
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    manage_py = os.path.join(BASE_DIR, "manage.py")
    if os.path.exists(manage_py):
        with open(manage_py, "r", encoding="utf-8") as f:
            for line in f:
                if "DJANGO_SETTINGS_MODULE" in line and "setdefault" in line:
                    # Extract the second argument from os.environ.setdefault('DJANGO_SETTINGS_MODULE', '...')
                    parts = line.split(",")
                    if len(parts) >= 2:
                        mod = parts[1].strip().strip(")").strip().strip("'\"")
                        os.environ["DJANGO_SETTINGS_MODULE"] = mod
                        break
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

django.setup()

from datetime import date, datetime
from decimal import Decimal
from collections import defaultdict
from django.db.models import Q
from django.utils import timezone

from stocks.models import SaleTransaction, CreditRepayment

START = date(2026, 10, 1)
END = date(2026, 10, 31)
BRANCH_ID = 3

# Get October credit invoices
sales = SaleTransaction.objects.filter(
    sale_date__date__range=(START, END),
    branch_id=BRANCH_ID,
).filter(
    Q(payment_method__iexact="CREDIT")
    | Q(split_payments__method__iexact="CREDIT")
).distinct().prefetch_related("split_payments")

# Get October repayments
repayments = CreditRepayment.objects.filter(
    date__range=(START, END),
    branch_id=BRANCH_ID,
    transaction__isnull=False,
).select_related("transaction", "customer")

# Group repayments by invoice
repaid_by_sale = defaultdict(lambda: Decimal("0.00"))
repayment_details = defaultdict(list)

for r in repayments:
    amount = Decimal(str(r.amount or 0))
    repaid_by_sale[r.transaction_id] += amount

    repayment_details[r.transaction_id].append({
        "id": r.id,
        "amount": amount,
        "date": r.date,
        "method": r.payment_method,
        "notes": r.notes,
    })

# Audit each invoice
overpaid = []
all_invoices = []

total_principal = Decimal("0.00")
total_repaid = Decimal("0.00")
total_overpaid = Decimal("0.00")

for sale in sales:
    principal = Decimal(
        str(sale.credit_payment_amount or 0)
    )

    repaid = repaid_by_sale[sale.id]

    balance = principal - repaid

    total_principal += principal
    total_repaid += repaid

    row = {
        "id": sale.id,
        "customer": str(sale.customer),
        "date": sale.sale_date,
        "method": sale.payment_method,
        "total": Decimal(str(sale.total_amount or 0)),
        "amount_paid": Decimal(str(sale.amount_paid or 0)),
        "credit": principal,
        "repaid": repaid,
        "balance": balance,
    }

    all_invoices.append(row)

    if repaid > principal:
        excess = repaid - principal
        total_overpaid += excess
        overpaid.append(row)

# Print summary
print("\n========== INVOICE RECONCILIATION ==========")
print("Invoices:", len(all_invoices))
print("Total credit:", total_principal)
print("Total repaid:", total_repaid)
print("Overpaid invoices:", len(overpaid))
print("Total excess on overpaid invoices:", total_overpaid)

print("\n========== OVERPAID INVOICES ==========")

for x in sorted(
    overpaid,
    key=lambda x: x["balance"]
):
    print("\nInvoice ID:", x["id"])
    print("Customer:", x["customer"])
    print("Date:", x["date"])
    print("Payment method:", x["method"])
    print("Sale total:", x["total"])
    print("Checkout amount paid:", x["amount_paid"])
    print("Original credit:", x["credit"])
    print("Repaid:", x["repaid"])
    print("Excess:", x["repaid"] - x["credit"])

    print("Repayment records:")
    for detail in repayment_details[x["id"]]:
        print("  ", detail)

print("\n========== END AUDIT ==========")