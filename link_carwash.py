import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")  # Replace with your settings module
django.setup()

from django.db import transaction
from stocks.models import BranchItemPricing, StockMovement, recalculate_wac_sales_cost  # Replace 'myapp' with your app name


def repair_carwash_costs():
    print("1. Copying valid cost prices to zero-cost branch items...")

    zero_pricings = BranchItemPricing.objects.filter(opening_stock_unit_cost_price=0)

    for p in zero_pricings:
        # Find if this item has a valid cost price in any other branch
        valid_pricing = BranchItemPricing.objects.filter(
            item_id=p.item_id, 
            opening_stock_unit_cost_price__gt=0
        ).first()

        if valid_pricing:
            p.opening_stock_unit_cost_price = valid_pricing.opening_stock_unit_cost_price
            p.unit_cost_price = valid_pricing.opening_stock_unit_cost_price
            p.save(update_fields=["opening_stock_unit_cost_price", "unit_cost_price"])

    print("2. Backfilling Stock Movements with valid cost prices...")
    zero_movements = StockMovement.objects.filter(unit_cost_price__isnull=True) | StockMovement.objects.filter(unit_cost_price=0)

    for m in zero_movements:
        pricing = BranchItemPricing.objects.filter(branch_id=m.branch_id, item_id=m.item_id).first()
        if pricing and pricing.opening_stock_unit_cost_price > 0:
            m.unit_cost_price = pricing.opening_stock_unit_cost_price
            m.save(update_fields=["unit_cost_price"])

    print("3. Recalculating WAC for all items...")
    all_pricings = BranchItemPricing.objects.select_related("branch", "item").all()
    total = all_pricings.count()

    for idx, p in enumerate(all_pricings, start=1):
        with transaction.atomic():
            wac = recalculate_wac_sales_cost(p.branch, p.item)
            print(f"[{idx}/{total}] {p.item.name} @ {p.branch.name} -> Restored WAC: KSh {wac}")

    print("Repair process finished successfully!")


if __name__ == "__main__":
    repair_carwash_costs()