import os
import django

# Initialize Django environment settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")  # Replace 'your_project_name' with your actual Django settings module path
django.setup()

from django.db import transaction
from django.db.models import Sum
from stocks.models import PurchaseOrder, PurchaseOrderItem, StockBatch, StockMovement

with transaction.atomic():
    # Fetch all retro purchase orders that have PO numbers containing RETRO
    retro_pos = PurchaseOrder.objects.filter(po_number__icontains="RETRO")
    
    # Group them by branch, date, and supplier
    grouped_map = {}
    for po in retro_pos:
        date_str = po.purchase_date.strftime("%Y%m%d")
        branch_id = po.branch_id
        supplier_id = po.supplier_id or 0
        key = (branch_id, date_str, supplier_id)
        
        if key not in grouped_map:
            grouped_map[key] = []
        grouped_map[key].append(po)

    merged_count = 0
    for (branch_id, date_str, supplier_id), pos in grouped_map.items():
        if len(pos) <= 1:
            continue

        # Choose primary PO as batch container
        primary_po = pos[0]
        batch_po_number = f"PO-{date_str}-BATCH-{primary_po.branch.name[:3].upper()}"
        primary_po.po_number = batch_po_number
        primary_po.save()

        # Move line items, stock batches, and stock movements to the primary PO
        for po in pos[1:]:
            # Move items
            PurchaseOrderItem.objects.filter(purchase_order=po).update(purchase_order=primary_po)
            
            # Re-reference StockBatches & StockMovements
            old_ref_prefix = f"PO-{po.pk}-"
            new_ref_prefix = f"PO-{primary_po.pk}-"
            
            StockBatch.objects.filter(batch_number=po.po_number).update(batch_number=batch_po_number)
            
            # Delete empty duplicate header
            po.delete()
            merged_count += 1

        primary_po.recalculate_totals()

print(f"Successfully consolidated {merged_count} redundant purchase order headers into unified Batch Orders!")