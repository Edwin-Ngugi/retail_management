import os
import sys
import django

# 1. Setup Django environment settings
# Replace 'myproject.settings' with your actual Django settings module path
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")
django.setup()

# 2. Imports after django.setup()
from django.db import transaction
from stocks.models import StockMovement, RetailBranch, Item, recalculate_wac_sales_cost


@transaction.atomic
def convert_adjustments_to_purchases():
    # Fetch all StockMovements with movement_type = ADJUSTMENT_IN
    adjustments = StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.ADJUSTMENT_IN
    )

    count = adjustments.count()
    print(f"Found {count} ADJUSTMENT_IN movements to convert.")

    if count == 0:
        print("No movements to convert.")
        return

    # Track affected (branch, item) pairs to recalculate WAC afterwards
    affected_pairs = set()

    for movement in adjustments:
        affected_pairs.add((movement.branch_id, movement.item_id))
        
        # If reference isn't set, build a reference identifier
        if not movement.reference:
            movement.reference = f"PURCHASE-CONVERTED-{movement.pk}"
            
        movement.movement_type = StockMovement.MovementType.PURCHASE
        movement.save()

    print("Updated movement types to PURCHASE.")

    # Recalculate WAC & COGS for all affected branch-item combinations
    print("Recalculating WAC for affected items...")
    for branch_id, item_id in affected_pairs:
        branch = RetailBranch.objects.get(pk=branch_id)
        item = Item.objects.get(pk=item_id)
        recalculate_wac_sales_cost(branch, item)

    print("Conversion and WAC recalculation completed successfully!")


if __name__ == "__main__":
    convert_adjustments_to_purchases()