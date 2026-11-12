import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")
django.setup()

from django.db import transaction
from stocks.models import SaleTransaction, SalePayment

with transaction.atomic():
    # 1. Remove bogus Split #1378 (KSh 58,670.00) from Tx #1507 and restore to CREDIT
    deleted_splits, _ = SalePayment.objects.filter(id=1378, transaction_id=1507).delete()
    tx_1507 = SaleTransaction.objects.filter(id=1507).first()
    if tx_1507:
        tx_1507.payment_method = "CREDIT"
        tx_1507.save(update_fields=["payment_method"])
        print(f"✅ Tx #1507 (INV 2_36): Deleted {deleted_splits} bogus SalePayment (#1378) and restored payment_method='CREDIT'.")

    # 2. Restore Tx #1255 (INV 1_11) and Tx #1321 (INV 1_43) back to CREDIT
    for tx_id in [1255, 1321]:
        tx = SaleTransaction.objects.filter(id=tx_id).first()
        if tx:
            tx.payment_method = "CREDIT"
            tx.save(update_fields=["payment_method"])
            print(f"✅ Tx #{tx.id} ({tx.receipt_number}): Restored payment_method='CREDIT'.")