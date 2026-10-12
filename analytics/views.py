# restore_ledger.py
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")  # Replace 'your_project_name' with your actual settings folder name
django.setup()

from django.db import transaction
from stocks.models import Purchase, SaleTransaction, StockMovement, StockBatch, BranchItemPricing, recalculate_wac_sales_cost

purchases_restored = 0
sales_restored = 0

with transaction.atomic():
    # 1. Restore missing PURCHASE StockMovements
    for purchase in Purchase.objects.select_related("branch", "item", "created_by"):
        ref = f"PURCHASE-{purchase.pk}"
        
        batch, _ = StockBatch.objects.get_or_create(
            reference=ref,
            defaults={
                "branch": purchase.branch,
                "item": purchase.item,
                "batch_number": f"PO-{purchase.pk}",
                "quantity_received": purchase.quantity,
                "quantity_remaining": purchase.quantity,
                "unit_cost_price": purchase.unit_cost_price,
                "received_at": purchase.purchase_date,
            }
        )
        
        if not StockMovement.objects.filter(reference=ref).exists():
            StockMovement.objects.create(
                branch=purchase.branch,
                item=purchase.item,
                batch=batch,
                movement_type=StockMovement.MovementType.PURCHASE,
                quantity=purchase.quantity,
                unit_cost_price=purchase.unit_cost_price,
                date=purchase.purchase_date.date(),
                created_at=purchase.purchase_date,
                reference=ref,
                reference_id=purchase.pk,
                notes=f"Restored restock from purchase record #{purchase.pk}",
                created_by=purchase.created_by,
            )
            purchases_restored += 1

    # 2. Restore missing SALE StockMovements
    for sale in SaleTransaction.objects.select_related("branch", "created_by").prefetch_related("items__item"):
        ref = f"SALE-{sale.pk}"
        
        if not StockMovement.objects.filter(reference=ref).exists():
            for sale_item in sale.items.all():
                StockMovement.objects.create(
                    branch=sale.branch,
                    item=sale_item.item,
                    movement_type=StockMovement.MovementType.SALE,
                    quantity=sale_item.quantity,
                    unit_cost_price=sale_item.unit_cost_price or 0,
                    date=sale.created_at.date(),
                    created_at=sale.created_at,
                    reference=ref,
                    reference_id=sale.pk,
                    notes=f"Restored sale movement from sale #{sale.pk}",
                    created_by=sale.created_by,
                )
            sales_restored += 1

    # 3. Recalculate WAC & update remaining stock totals
    for pricing in BranchItemPricing.objects.all():
        recalculate_wac_sales_cost(pricing.branch, pricing.item)

print(f"Restored {purchases_restored} purchase movements and {sales_restored} sale movements.")
print("Recalculated WAC and stock levels across all branches successfully.")