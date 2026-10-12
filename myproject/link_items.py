import os
import django
from decimal import Decimal

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')
django.setup()

from django.db import transaction
from stocks.models import Item, RetailBranch, BranchItemPricing

TARGET_BRANCH_NAME = "Car Wash"
DEFAULT_COST_PRICE = Decimal("100.00")
DEFAULT_RETAIL_PRICE = Decimal("150.00")

@transaction.atomic
def link_existing_items_to_branch():
    try:
        branch = RetailBranch.objects.get(name=TARGET_BRANCH_NAME)
    except RetailBranch.DoesNotExist:
        print(f"Error: Branch '{TARGET_BRANCH_NAME}' was not found.")
        return

    unlinked_items = Item.objects.exclude(branch_pricings__branch=branch)

    if not unlinked_items.exists():
        print(f"All items are already linked to '{branch.name}'. No changes made.")
        return

    pricings_to_create = [
        BranchItemPricing(
            branch=branch,
            item=item,
            unit_cost_price=DEFAULT_COST_PRICE,
            unit_retail_price=DEFAULT_RETAIL_PRICE,
            opening_stock=0,
            min_stock_threshold=5,
        )
        for item in unlinked_items
    ]

    created_records = BranchItemPricing.objects.bulk_create(pricings_to_create)
    print(f"Success! Linked {len(created_records)} items to branch '{branch.name}'.")

if __name__ == "__main__":
    link_existing_items_to_branch()