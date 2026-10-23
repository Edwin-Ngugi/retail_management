import os
import sys
import django
from decimal import Decimal, ROUND_HALF_UP

# Set up Django environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")  # Adjust settings module path if needed
django.setup()

from django.db import transaction
from stocks.models import Purchase, StockMovement, SupplierPayment  # Adjust app name if needed


def quantize_money(value):
    """Ensure Decimal has 2 decimal places with proper rounding."""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def create_purchases_and_payments_from_movements():
    """
    Converts standalone PURCHASE StockMovements into Purchase and SupplierPayment records.
    """
    print("Starting conversion of StockMovement restocks to Purchase & SupplierPayment records...")

    purchase_movements = StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.PURCHASE
    )

    purchases_created = 0
    payments_created = 0

    with transaction.atomic():
        for mov in purchase_movements:
            unit_cost = quantize_money(mov.unit_cost_price or 0)
            qty = Decimal(str(mov.quantity or 0))
            calc_total = quantize_money(qty * unit_cost)

            if calc_total <= Decimal("0.00"):
                continue

            # 1. Find or create Purchase record
            purchase_obj = Purchase.objects.filter(
                branch=mov.branch,
                item=mov.item,
                quantity=mov.quantity,
                purchase_date=mov.date
            ).first()

            if not purchase_obj:
                # Set payment_method to CREDIT and amount_paid to 0 so balance_due == calc_total
                purchase_obj = Purchase.objects.create(
                    branch=mov.branch,
                    item=mov.item,
                    quantity=mov.quantity,
                    unit_cost_price=unit_cost,
                    payment_method=Purchase.PaymentMethod.CREDIT,
                    amount_paid=Decimal("0.00"),
                    purchase_date=mov.date,
                    supplier_name="Direct Supplier",
                    created_by=mov.created_by
                )
                purchases_created += 1
            else:
                # Reset amount_paid to 0 temporarily if SupplierPayment doesn't exist yet
                if not SupplierPayment.objects.filter(purchase=purchase_obj).exists():
                    purchase_obj.payment_method = Purchase.PaymentMethod.CREDIT
                    purchase_obj.amount_paid = Decimal("0.00")
                    purchase_obj.save(update_fields=["payment_method", "amount_paid"])

            # 2. Create SupplierPayment record
            if not SupplierPayment.objects.filter(purchase=purchase_obj).exists():
                SupplierPayment.objects.create(
                    purchase=purchase_obj,
                    amount=purchase_obj.total_cost,
                    payment_method=SupplierPayment.PaymentMethod.CASH,
                    notes=f"Auto-backfilled historical restock payment for {mov.item.name}"
                )
                payments_created += 1

                # Update payment_method to CASH once fully paid
                purchase_obj.payment_method = Purchase.PaymentMethod.CASH
                purchase_obj.save(update_fields=["payment_method"])

    print(f"Successfully created {purchases_created} Purchase record(s).")
    print(f"Successfully generated {payments_created} Supplier Payment(s).")


if __name__ == "__main__":
    create_purchases_and_payments_from_movements()