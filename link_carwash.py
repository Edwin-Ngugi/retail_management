import os
import sys
import django

# 1. Point to your Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

# 2. Initialize Django
django.setup()

# 3. Import models AFTER calling django.setup()
from stocks.models import SaleTransaction
from django.db.models import Q

def reassign_invoices():
    # Fetch all CREDIT sales that currently have auto-generated numbers starting with 'INV-'
    auto_credit_sales = SaleTransaction.objects.filter(
        payment_method='CREDIT',
        receipt_number__icontains='INV-2026'
    ).order_by('id')

    if not auto_credit_sales.exists():
        # Fallback to check any CREDIT sale with long auto-generated receipt numbers
        auto_credit_sales = SaleTransaction.objects.filter(
            Q(payment_method='CREDIT') & 
            (Q(receipt_number__startswith='INV-') | Q(invoice_number__startswith='INV-'))
        ).order_by('id')

    total_found = auto_credit_sales.count()
    if total_found == 0:
        print("No auto-generated credit transactions found to update.")
        return

    # Set starting counter (e.g., starting after INV 1_19 -> INV 1_20)
    start_counter = 20
    updated_count = 0

    print(f"Found {total_found} credit transactions. Starting batch update...\n")

    for index, tx in enumerate(auto_credit_sales):
        new_inv_num = f"INV 1_{start_counter + index}"
        
        old_num = tx.receipt_number or tx.invoice_number
        tx.invoice_number = new_inv_num
        tx.receipt_number = new_inv_num
        tx.save(update_fields=['invoice_number', 'receipt_number'])
        
        print(f"Updated Transaction ID {tx.id}: '{old_num}' -> '{new_inv_num}'")
        updated_count += 1

    print(f"\nSuccessfully updated {updated_count} invoice numbers!")

if __name__ == "__main__":
    reassign_invoices()