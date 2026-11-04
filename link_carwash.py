import os
import sys
import django

# 1. Add current project root directory to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 2. Set Django settings module (replace 'your_project_name' with your actual project package name)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

# 3. Initialize Django environment
django.setup()

from django.db import transaction
from stocks.models import Purchase, PurchaseOrder, PurchaseOrderItem, StockBatch, StockMovement

# Explicit ID list matching the selected items in your screenshot
TARGET_PURCHASE_IDS = [115, 114, 113, 112, 111]

with transaction.atomic():
    purchases = list(Purchase.objects.filter(id__in=TARGET_PURCHASE_IDS).order_by("id"))
    
    if not purchases:
        print("No matching purchase records found!")
    else:
        # Use details from the first purchase record for the batch header
        base_purchase = purchases[0]
        date_str = base_purchase.purchase_date.strftime("%Y%m%d")
        batch_po_number = f"PO-{date_str}-BATCH-OCT2"

        # 1. Create or get the parent PurchaseOrder batch header
        po_header, created = PurchaseOrder.objects.get_or_create(
            po_number=batch_po_number,
            defaults={
                "branch": base_purchase.branch,
                "supplier": base_purchase.supplier,
                "supplier_name": base_purchase.supplier_name or "Direct Supplier",
                "payment_method": base_purchase.payment_method,
                "purchase_date": base_purchase.purchase_date,
                "created_by": base_purchase.created_by,
            }
        )

        items_added = 0
        for p in purchases:
            # 2. Add as a line item to the batch header
            po_item, _ = PurchaseOrderItem.objects.get_or_create(
                purchase_order=po_header,
                item=p.item,
                defaults={
                    "quantity": p.quantity,
                    "unit_cost_price": p.unit_cost_price,
                }
            )

            # 3. Align references on StockBatches & StockMovements to point to the batch header
            StockBatch.objects.filter(reference=f"PURCHASE-{p.pk}").update(batch_number=batch_po_number)
            StockMovement.objects.filter(reference=f"PURCHASE-{p.pk}").update(
                notes=f"Purchase Order ({batch_po_number})"
            )

            items_added += 1

        # 4. Recalculate financial totals for the parent batch header
        po_header.recalculate_totals()

        print(f"Successfully consolidated {items_added} purchases (IDs {TARGET_PURCHASE_IDS}) into Batch Order '{batch_po_number}'!")