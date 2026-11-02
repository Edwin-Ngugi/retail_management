import os
import sys
import django

# 1. Add project root directory to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 2. Point to your Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

# 3. Initialize Django setup
django.setup()

# 4. Imports after django.setup()
from django.db import transaction
from stocks.models import (
    SaleTransaction,
    SaleItem,
    SalePayment,
    Sale,
    CreditRepayment,
    StockMovement,
)


def run_deletion():
    # Exact Primary Keys matching your output:
    target_ids = [1261, 1262]

    sales = SaleTransaction.objects.filter(id__in=target_ids)

    if not sales.exists():
        print("No matching transactions found for IDs:", target_ids)
        return

    found_list = list(sales.values("id", "receipt_number"))
    print(f"Found {len(found_list)} transaction(s) to delete: {found_list}")

    sale_ids = [item["id"] for item in found_list]
    receipt_nos = [item["receipt_number"] for item in found_list if item["receipt_number"]]

    with transaction.atomic():
        # 1. Clean up Stock Movements
        StockMovement.objects.filter(
            reference_id__in=sale_ids,
            movement_type=StockMovement.MovementType.SALE,
        ).delete()
        StockMovement.objects.filter(
            reference__in=[f"SALE-{sid}" for sid in sale_ids]
        ).delete()

        if receipt_nos:
            StockMovement.objects.filter(
                reference__in=[f"SALE-{r}" for r in receipt_nos]
            ).delete()
            StockMovement.objects.filter(reference__in=receipt_nos).delete()

        # 2. Direct QuerySet deletions on dependent models
        CreditRepayment.objects.filter(transaction_id__in=sale_ids).delete()
        SalePayment.objects.filter(transaction_id__in=sale_ids).delete()
        Sale.objects.filter(transaction_id__in=sale_ids).delete()
        SaleItem.objects.filter(transaction_id__in=sale_ids).delete()

        # 3. Direct QuerySet deletion on SaleTransaction parent records
        deleted_count, _ = SaleTransaction.objects.filter(id__in=sale_ids).delete()

    print(f"\nSUCCESS: Deleted {deleted_count} sale transaction(s) and all dependent records!")


if __name__ == "__main__":
    run_deletion()