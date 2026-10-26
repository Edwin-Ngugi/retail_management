import os
import django
from decimal import Decimal
from django.db import transaction

# Initialize Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'my.settings')  # Adjust if settings module differs
django.setup()

from stocks.models import Purchase, PurchaseOrder, PurchaseOrderItem

@transaction.atomic
def create_parent_purchase_orders():
    # -------------------------------------------------------------------------
    # 1. Migrate Group 1 (PO-93 to PO-105) -> PO-20261002-BATCH01
    # -------------------------------------------------------------------------
    group1_po_num = "PO-20261002-BATCH01"
    purchases_g1 = Purchase.objects.filter(id__in=range(93, 106))

    if purchases_g1.exists():
        first_p = purchases_g1.first()
        po1, created = PurchaseOrder.objects.get_or_create(
            po_number=group1_po_num,
            defaults={
                'branch': first_p.branch,
                'supplier': first_p.supplier,
                'supplier_name': first_p.supplier_name or (first_p.supplier.name if first_p.supplier else "Direct Supplier"),
                'payment_method': first_p.payment_method,
                'purchase_date': first_p.purchase_date,
                'created_by': first_p.created_by,
            }
        )

        for p in purchases_g1:
            PurchaseOrderItem.objects.get_or_create(
                purchase_order=po1,
                item=p.item,
                defaults={
                    'quantity': p.quantity,
                    'unit_cost_price': p.unit_cost_price,
                }
            )
        po1.recalculate_totals()
        print(f"✅ Group 1 created in PurchaseOrder table: '{group1_po_num}' (Total: KSh {po1.total_amount}).")

    # -------------------------------------------------------------------------
    # 2. Migrate Group 2 (PO-84 to PO-92) -> PO-20261001-BATCH02
    # -------------------------------------------------------------------------
    group2_po_num = "PO-20261001-BATCH02"
    purchases_g2 = Purchase.objects.filter(id__in=range(84, 93))

    if purchases_g2.exists():
        first_p = purchases_g2.first()
        po2, created = PurchaseOrder.objects.get_or_create(
            po_number=group2_po_num,
            defaults={
                'branch': first_p.branch,
                'supplier': first_p.supplier,
                'supplier_name': first_p.supplier_name or (first_p.supplier.name if first_p.supplier else "Direct Supplier"),
                'payment_method': first_p.payment_method,
                'purchase_date': first_p.purchase_date,
                'created_by': first_p.created_by,
            }
        )

        for p in purchases_g2:
            PurchaseOrderItem.objects.get_or_create(
                purchase_order=po2,
                item=p.item,
                defaults={
                    'quantity': p.quantity,
                    'unit_cost_price': p.unit_cost_price,
                }
            )
        po2.recalculate_totals()
        print(f"✅ Group 2 created in PurchaseOrder table: '{group2_po_num}' (Total: KSh {po2.total_amount}).")

if __name__ == "__main__":
    create_parent_purchase_orders()