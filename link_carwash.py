import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")
django.setup()

from datetime import date
from stocks.models import SaleTransaction, CreditRepayment

target_date = date(2026, 10, 16)

print(f"==================================================")
print(f" 1. DIRECT SALES TRANSACTIONS (SaleTransaction)   ")
print(f"==================================================")
sales = SaleTransaction.objects.filter(sale_date__date=target_date)
for sale in sales:
    payments = list(sale.split_payments.all())
    print(f"Tx #{sale.id} | Method: '{sale.payment_method}' | Amount Paid: KSh {sale.amount_paid}")
    for p in payments:
        print(f"   └─ Child SalePayment: method='{p.method}', amount={p.amount}")

print(f"\n==================================================")
print(f" 2. PRIOR CREDIT REPAYMENTS COLLECTED ONLY        ")
print(f"==================================================")
# Filter repayments on target_date whose credit invoice was created BEFORE target_date
# Or repayments linked to general customer balances (transaction__isnull=True)
repayments = CreditRepayment.objects.filter(
    date=target_date
).filter(
    transaction__sale_date__date__lt=target_date
) | CreditRepayment.objects.filter(
    date=target_date, 
    transaction__isnull=True
)

for rep in repayments.distinct():
    splits = list(rep.splits.all())
    customer_name = rep.customer.name if getattr(rep, 'customer', None) else "N/A"
    print(f"Repayment #{rep.id} | Customer: {customer_name} | Method: '{rep.payment_method}' | Amount: KSh {rep.amount}")
    for s in splits:
        print(f"   └─ Child CreditRepaymentSplit: method='{s.method}', amount={s.amount}")