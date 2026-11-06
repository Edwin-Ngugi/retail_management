import os
import sys
import django
from decimal import Decimal

# 1. Setup Django standalone environment
# Set the DJANGO_SETTINGS_MODULE to your project's settings file (e.g., 'myproject.settings')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")  # 👈 Replace 'core.settings' with your settings module path if different
django.setup()

# 2. Import Django models AFTER django.setup() call
from stocks.models import CreditRepayment, CreditRepaymentSplit  # Adjust app name if needed


def run_backfill():
    count = 0
    # Find all repayments marked as SPLIT that have 0 child split records
    split_repayments = CreditRepayment.objects.filter(
        payment_method="SPLIT",
        splits__isnull=True
    ).distinct()

    for rep in split_repayments:
        if rep.amount and rep.amount > Decimal("0.00"):
            # Split the total amount evenly or assign to default channels for backfill
            half_amount = (rep.amount / Decimal("2.00")).quantize(Decimal("0.01"))
            remainder = rep.amount - half_amount

            # Create Line 1 (Cash)
            CreditRepaymentSplit.objects.create(
                repayment=rep,
                method="CASH",
                amount=half_amount,
                reference_code="BACKFILL-CASH"
            )

            # Create Line 2 (Paybill)
            CreditRepaymentSplit.objects.create(
                repayment=rep,
                method="PAYBILL",
                amount=remainder,
                reference_code="BACKFILL-PAYBILL"
            )
            count += 1

    print(f"Successfully backfilled split records for {count} existing split repayments!")


if __name__ == "__main__":
    run_backfill()