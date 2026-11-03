import os
import sys
import django

# Set up Django environment settings
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")  # Replace 'retail.settings' with your actual settings module path if different
django.setup()

from django.db import transaction
from stocks.models import SaleTransaction

# Mapping: Current receipt_number -> Target invoice_number
updates = {
    "REC-20261003-E56526C5": "INV 1_29",
    "REC-20261003-455B1DC5": "INV 1_27",
}

def main():
    with transaction.atomic():
        for old_rec, new_inv in updates.items():
            sale = SaleTransaction.objects.filter(receipt_number=old_rec).first()
            if sale:
                sale.receipt_number = new_inv
                sale.invoice_number = new_inv
                # Save using update_fields to bypass triggering re-generation logic in save()
                sale.save(update_fields=["receipt_number", "invoice_number"])
                print(f"✅ Updated Sale ID {sale.id}: {old_rec} -> {new_inv}")
            else:
                print(f"❌ Transaction with receipt_number '{old_rec}' not found.")

if __name__ == "__main__":
    main()