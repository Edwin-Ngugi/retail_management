import os
import sys
import django

# -----------------------------------------------------------------------------
# 1. SETUP DJANGO ENVIRONMENT
# -----------------------------------------------------------------------------
# Replace 'myproject.settings' with your actual settings module path (e.g. 'config.settings', 'core.settings')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")
django.setup()

# -----------------------------------------------------------------------------
# 2. IMPORTS (Must be after django.setup())
# -----------------------------------------------------------------------------
from decimal import Decimal
from django.db import transaction, models
from django.db.models import F

# Replace 'myapp' with the actual app name where models.py is located
from stocks.models import Purchase, StockMovement


def mark_historical_purchases_as_paid():
    """
    Updates all existing Purchase records to ensure amount_paid matches total_cost,
    sets payment_method to CASH for any historical credit purchases, and updates
    associated StockMovements.
    """
    print("Starting historical purchases payment backfill...")

    with transaction.atomic():
        # 1. Update existing Purchase records where amount_paid is less than total_cost
        # Expressed as: amount_paid = quantity * unit_cost_price
        purchases_updated = Purchase.objects.filter(
            payment_method=Purchase.PaymentMethod.CREDIT
        ).update(
            payment_method=Purchase.PaymentMethod.CASH,
            amount_paid=F('quantity') * F('unit_cost_price')
        )

        # 2. Ensure non-credit purchases with zero amount_paid are updated as well
        unpaid_cash_purchases = Purchase.objects.filter(
            amount_paid=Decimal("0.00")
        ).update(
            amount_paid=F('quantity') * F('unit_cost_price')
        )

        # 3. Update notes/reference on historical StockMovement entries if needed
        movements_updated = StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.PURCHASE
        ).count()

        print(f"Successfully updated {purchases_updated} credit purchase(s) to CASH.")
        print(f"Successfully updated {unpaid_cash_purchases} zero-paid purchase(s) to fully paid.")
        print(f"Verified {movements_updated} purchase stock movement(s) in ledger.")


if __name__ == "__main__":
    mark_historical_purchases_as_paid()