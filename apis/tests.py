from django.contrib.auth.models import User
from stocks.models import (
    RetailBranch, Item, BranchItemPricing, StockMovement, 
    Purchase, SaleTransaction, CustomerReturn, SupplierReturn, 
    DamageRecord, StockAdjustment, Expense, DailyFinancialReport
)


user = User.objects.first()

if not user:
    print("Error: No User found in database.")
else:
    
    branch, created = RetailBranch.objects.get_or_create(
        retailer=user,
        name="Car Wash",
        defaults={"location": "Main Location"}
    )
    print(f"Target Branch: {branch.name} (ID: {branch.id})")

    
    items_linked = 0
    for item in Item.objects.filter(retailer=user):
        # Default pricing set to 0.00 if historical prices weren't on Item model; update as needed
        pricing, created_pricing = BranchItemPricing.objects.get_or_create(
            branch=branch,
            item=item,
            defaults={
                "opening_stock": 0,
                "unit_cost_price": 0.00,
                "unit_retail_price": 0.00,
                "min_stock_threshold": 5,
            }
        )
        if created_pricing:
            items_linked += 1

    print(f"Successfully linked {items_linked} item(s) to '{branch.name}'.")

    # 4. Assign existing orphaned records to the 'Car Wash' branch
    models_to_update = [
        StockMovement, Purchase, SaleTransaction, CustomerReturn, 
        SupplierReturn, DamageRecord, StockAdjustment, Expense, DailyFinancialReport
    ]

    for model in models_to_update:
        updated_count = model.objects.filter(branch__isnull=True).update(branch=branch)
        if updated_count > 0:
            print(f"Updated {updated_count} record(s) in {model.__name__} to '{branch.name}'.")

    print("Data migration complete!")