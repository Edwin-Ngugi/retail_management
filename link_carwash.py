import os
import django
import uuid
from datetime import datetime
from django.utils import timezone
from django.db import transaction

# Setup Django environment using django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')  # Adjust 'core.settings' if your settings file differs
django.setup()

from stocks.models import Purchase, StockBatch, StockMovement

@transaction.atomic
def reorganize_purchase_orders():
    # -------------------------------------------------------------------------
    # GROUP 1: The 13 selected purchases (PO-93 to PO-105)
    # Action: Group into one order & Change date from Oct 26th to Oct 2nd
    # -------------------------------------------------------------------------
    selected_ids = list(range(93, 106))  # IDs 93 through 105
    group1_po = "PO-20261002-BATCH01"

    group1_purchases = Purchase.objects.filter(id__in=selected_ids)
    
    print(f"Processing Group 1: {group1_purchases.count()} items...")

    for p in group1_purchases:
        # Update purchase_date and created_at to Oct 2nd, 2026 keeping exact time
        original_dt = p.purchase_date or timezone.now()
        new_dt = original_dt.replace(year=2026, month=10, day=2)

        # 1. Update Purchase model
        p.purchase_date = new_dt
        p.created_at = new_dt
        p.save(update_fields=['purchase_date', 'created_at'])

        # 2. Update StockBatch
        StockBatch.objects.filter(reference=f"PURCHASE-{p.id}").update(
            batch_number=group1_po,
            received_at=new_dt,
        )

        # 3. Update StockMovement
        StockMovement.objects.filter(reference=f"PURCHASE-{p.id}").update(
            date=new_dt.date(),
            created_at=new_dt,
            notes=f"Purchase Order ({group1_po}) - {p.item.name}"
        )

    print(f"✅ Group 1 updated! {len(selected_ids)} items grouped under '{group1_po}' and date shifted to Oct 2, 2026.")

    # -------------------------------------------------------------------------
    # GROUP 2: The remaining 9 purchases (PO-84 to PO-92)
    # Action: Group into another order WITHOUT changing the dates
    # -------------------------------------------------------------------------
    remaining_ids = list(range(84, 93))  # IDs 84 through 92
    group2_po = "PO-20261001-BATCH02"

    group2_purchases = Purchase.objects.filter(id__in=remaining_ids)

    print(f"\nProcessing Group 2: {group2_purchases.count()} items...")

    for p in group2_purchases:
        # Keep original date intact, only update batch grouping references
        StockBatch.objects.filter(reference=f"PURCHASE-{p.id}").update(
            batch_number=group2_po
        )

        StockMovement.objects.filter(reference=f"PURCHASE-{p.id}").update(
            notes=f"Purchase Order ({group2_po}) - {p.item.name}"
        )

    print(f"✅ Group 2 updated! {len(remaining_ids)} items grouped under '{group2_po}' (dates kept intact).")

if __name__ == "__main__":
    reorganize_purchase_orders()