# tasks.py
from celery import shared_task
from django.utils import timezone
from stocks.models import BranchItemPricing, MonthlyInventorySnapshot

@shared_task
def create_monthly_stock_snapshot(year=None, month=None):
    """
    Creates or updates monthly inventory snapshots for all branch item pricings.
    Defaults to the previous month when executed automatically on the 1st.
    """
    if year is None or month is None:
        today = timezone.localdate()
        # Default to closing out the previous month
        if today.month == 1:
            year = today.year - 1
            month = 12
        else:
            year = today.year
            month = today.month - 1

    # Fetch all item-branch configurations in a single q# tasks.py
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from stocks.models import BranchItemPricing, MonthlyInventorySnapshot


@shared_task
def create_monthly_stock_snapshot(year=None, month=None):
    """Creates or updates monthly inventory snapshots for all branch item pricings

    and rolls over closing stock to become the new month's opening stock.
    Defaults to the previous month when executed automatically on the 1st.
    """
    if year is None or month is None:
        today = timezone.localdate()
        # Default to closing out the previous month
        if today.month == 1:
            year = today.year - 1
            month = 12
        else:
            year = today.year
            month = today.month - 1

    # Fetch all item-branch configurations in a single query with select_related
    pricings = BranchItemPricing.objects.select_related("branch", "item").all()
    created_count = 0

    with transaction.atomic():
        for pricing in pricings:
            # 1. Save closing stock snapshot for the ended month
            MonthlyInventorySnapshot.create_monthly_snapshot(
                branch=pricing.branch,
                item=pricing.item,
                year=year,
                month=month,
                closing_stock=pricing.remaining_stock,
                unit_cost_price=pricing.unit_cost_price,
            )

            # 2. Set closing stock as opening stock for the new month
            pricing.opening_stock = pricing.remaining_stock
            pricing.save(update_fields=["opening_stock"])

            created_count += 1

    return f"Successfully processed {created_count} snapshots and updated opening stocks for {year}-{month:02d}."uery with select_related
    pricings = BranchItemPricing.objects.select_related("branch", "item").all()
    created_count = 0

    for pricing in pricings:
        # Uses the model helper to maintain consistency across the app
        MonthlyInventorySnapshot.create_monthly_snapshot(
            branch=pricing.branch,
            item=pricing.item,
            year=year,
            month=month,
            closing_stock=pricing.remaining_stock,
            unit_cost_price=pricing.unit_cost_price,
        )
        created_count += 1

    return f"Successfully processed {created_count} snapshots for {year}-{month:02d}."