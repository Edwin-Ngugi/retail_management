import os
import django
from datetime import datetime, date
from decimal import Decimal

# -----------------------------------------------------------------------------
# 1. DJANGO INITIALIZATION
# -----------------------------------------------------------------------------
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")  # Adjust settings module
django.setup()

from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from stocks.models import (
    SaleTransaction,
    SaleItem,
    StockMovement,
    Purchase,
    BranchItemPricing,
    RetailBranch,
    Item,
    ZERO,
    quantize_money,
)


def calculate_ui_matching_report(
    start_date_str="2026-10-01",
    end_date_str="2026-10-02",
    branch_name="Sukawa"
):
    print("==================================================")
    print(f"  Inventory & COGS Report ({branch_name})")
    print(f"  Period: {start_date_str} to {end_date_str}")
    print("==================================================")

    # 1. Scope Branch
    branch_obj = RetailBranch.objects.filter(name=branch_name).first()
    if not branch_obj:
        print(f"Error: Branch '{branch_name}' not found.")
        return

    # Parse Dates
    start_d = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_d = datetime.strptime(end_date_str, "%Y-%m-%d").date()

    # 2. Query Movements & Purchases using both Date and DateTime fields
    inbound_types = [
        StockMovement.MovementType.PURCHASE,
        StockMovement.MovementType.TRANSFER_IN,
        StockMovement.MovementType.ADJUSTMENT_IN,
    ]

    movements_qs = StockMovement.objects.filter(
        branch=branch_obj,
        movement_type__in=inbound_types
    ).filter(
        Q(date__range=(start_d, end_d)) | Q(created_at__date__range=(start_d, end_d))
    )

    # Map restocks per item from StockMovement ledger
    restocks_map = {}
    for mv in movements_qs:
        qty = Decimal(str(mv.quantity or ZERO))
        restocks_map[mv.item_id] = restocks_map.get(mv.item_id, Decimal("0.000")) + qty

    # Fallback to Purchase entries if StockMovement records are absent
    purchases_qs = Purchase.objects.filter(
        branch=branch_obj,
        purchase_date__date__range=(start_d, end_d)
    )
    for p in purchases_qs:
        if p.item_id not in restocks_map or restocks_map[p.item_id] == ZERO:
            qty = Decimal(str(p.quantity or ZERO))
            restocks_map[p.item_id] = restocks_map.get(p.item_id, Decimal("0.000")) + qty

    # 3. Scope Sales
    sales_qs = SaleTransaction.objects.filter(
        branch=branch_obj
    ).filter(
        Q(sale_date__date__range=(start_d, end_d)) | Q(created_at__date__range=(start_d, end_d))
    )

    items_qs = Item.objects.filter(
        branch_pricings__branch=branch_obj
    ).distinct()

    item_stats = {}
    for item_obj in items_qs:
        pricing = BranchItemPricing.objects.filter(branch=branch_obj, item=item_obj).first()
        unit_cost = Decimal(str(pricing.unit_cost_price if pricing else ZERO))
        unit_price = Decimal(str(pricing.unit_selling_price if pricing and pricing.unit_selling_price else ZERO))
        opening_qty = Decimal(str(pricing.opening_stock if pricing and pricing.opening_stock else ZERO))

        item_stats[item_obj.id] = {
            "name": item_obj.name,
            "opening_stock": opening_qty,
            "restocks": restocks_map.get(item_obj.id, Decimal("0.000")),
            "units_sold": Decimal("0.000"),
            "unit_cost": unit_cost,
            "unit_price": unit_price,
            "physical_cogs": ZERO,
        }

    # Extract Line Items
    sale_items = SaleItem.objects.filter(transaction__in=sales_qs).select_related("item")

    for line in sale_items:
        item_id = line.item_id
        qty = Decimal(str(line.quantity_sold or Decimal("0.000")))
        cost = Decimal(str(line.unit_cost_price or ZERO))

        if item_id in item_stats:
            item_stats[item_id]["units_sold"] += qty
            item_stats[item_id]["physical_cogs"] += (qty * cost)

    # 4. Printed Output
    total_physical_cogs = ZERO
    total_stock_value = ZERO

    print(f"\n{'ITEM NAME':<30} | {'OPENING':<8} | {'RESTOCKS':<8} | {'SOLD':<6} | {'REMAINING':<10} | {'UNIT COST':<10} | {'PHYSICAL COGS':<12}")
    print("-" * 105)

    for item_id, stats in item_stats.items():
        sold = stats["units_sold"]
        restocks = stats["restocks"]
        opening = stats["opening_stock"]
        remaining = max(Decimal("0.000"), opening + restocks - sold)

        cogs = quantize_money(stats["physical_cogs"])
        total_physical_cogs += cogs
        total_stock_value += quantize_money(remaining * stats["unit_cost"])

        if opening > 0 or restocks > 0 or sold > 0:
            print(
                f"{stats['name']:<30} | "
                f"{float(opening):<8.1f} | "
                f"{float(restocks):<8.1f} | "
                f"{float(sold):<6.1f} | "
                f"{float(remaining):<10.1f} | "
                f"KSh {float(stats['unit_cost']):<6.2f} | "
                f"KSh {float(cogs):<10.2f}"
            )

    print("-" * 105)
    print(f"TOTAL PHYSICAL COGS (Units Sold x WAC) : KSh {quantize_money(total_physical_cogs):,}")
    print(f"TOTAL REMAINING STOCK VALUATION        : KSh {quantize_money(total_stock_value):,}")
    print("==================================================")


if __name__ == "__main__":
    calculate_ui_matching_report()