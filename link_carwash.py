import os
import sys
import django

# Initialize Django environment
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")  # <-- Change to your settings module name
django.setup()

from decimal import Decimal
from stocks.models import SaleTransaction, SalePayment

print("--- Fixing Typo IN 1_82 (Tx #1382) to CREDIT ---")

# 1. Fetch by primary key 1382 and string variants 'IN 1_82' / 'INV 1_82'
tx_1382 = (
    SaleTransaction.objects.filter(pk=1382).first() 
    or SaleTransaction.objects.filter(invoice_number__icontains="1_82").first()
)

if tx_1382:
    # Force payment_method to CREDIT and set amount_paid to 0.00
    tx_1382.payment_method = "CREDIT"
    tx_1382.amount_paid = Decimal("0.00")
    tx_1382.is_paid = False
    tx_1382.total_amount = Decimal("2500.00")
    tx_1382.save(update_fields=["payment_method", "amount_paid", "is_paid", "total_amount"])
    print(f"✔ Successfully updated Tx #{tx_1382.pk} ({tx_1382.invoice_number}) to CREDIT!")

    # Delete all child SalePayment entries associated with Tx #1382
    deleted_count, _ = SalePayment.objects.filter(transaction=tx_1382).delete()
    print(f"✔ Purged {deleted_count} child payment records.")

# 2. Safety sweep for any remaining credit transactions on 2026-10-12
ghost_deleted, _ = SalePayment.objects.filter(
    transaction__sale_date__date="2026-10-12",
    transaction__payment_method="CREDIT"
).delete()
print(f"✔ Swept {ghost_deleted} residual ghost child payments.")

print("--- Update Complete! Refresh your browser page now. ---")