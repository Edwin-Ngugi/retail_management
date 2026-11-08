import os
import sys
import django

# Set up Django environment settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")  # Replace 'your_project_name' with your actual Django project directory name
django.setup()

from stocks.models import SaleTransaction
from decimal import Decimal

def fix_unpaid_credit_sales():
    # Find all credit invoices
    credit_sales = SaleTransaction.objects.filter(
        payment_method="CREDIT"
    ) | SaleTransaction.objects.filter(
        receipt_number__startswith="INV"
    )

    updated_count = 0
    for sale in credit_sales.distinct():
        # Enforce CREDIT method and reset paid fields
        sale.payment_method = "CREDIT"
        sale.amount_paid = Decimal("0.00")
        sale.is_paid = False
        sale.save(update_fields=["payment_method", "amount_paid", "is_paid"])
        
        # Remove any spurious auto-generated default payment splits for unpaid credit invoices
        sale.split_payments.filter(method="CREDIT").delete()
        updated_count += 1

    print(f"Successfully updated {updated_count} credit invoices to UNPAID (is_paid=False, amount_paid=0.00).")

if __name__ == "__main__":
    fix_unpaid_credit_sales()