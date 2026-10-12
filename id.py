import os
import sys
import django

# 1. Setup Django environment (Update 'your_project_name' to your Django project settings path)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')
django.setup()

from django.db import transaction
from stocks.models import SaleTransaction, Customer

def fix_sale_customers():
    # Inspect available fields on Customer model dynamically
    customer_fields = {field.name for field in Customer._meta.get_fields()}

    credit_sales = SaleTransaction.objects.filter(
        payment_method__in=["CREDIT", "SPLIT"]
    ).select_related("branch", "customer")

    updated_count = 0

    with transaction.atomic():
        for sale in credit_sales:
            current_customer = sale.customer
            branch = sale.branch
            branch_name = branch.name if branch else "Main"
            clean_name = f"Walk-in Credit ({branch_name})"

            # Case A: If customer already exists but has the receipt number in its name, clean it up
            if current_customer and ("#" in current_customer.name or "REC-" in current_customer.name):
                current_customer.name = clean_name
                current_customer.save(update_fields=["name"])
                updated_count += 1
                continue

            # Case B: Unassigned or attached to generic global default
            needs_fixing = (
                current_customer is None or
                getattr(current_customer, "customer_code", "") == "CUST-00001" or
                "Walk-in Customer" in getattr(current_customer, "name", "")
            )

            if needs_fixing:
                created_by_user = getattr(sale, "created_by", None)

                # Resolve retailer safely
                retailer = getattr(created_by_user, "retailer", None) if created_by_user else None
                if not retailer and hasattr(branch, "retailer"):
                    retailer = getattr(branch, "retailer", None)

                # Build dictionary without the receipt/ID ref
                customer_kwargs = {
                    "name": clean_name
                }

                # Attach existing fields safely
                if "branch" in customer_fields:
                    customer_kwargs["branch"] = branch
                if "retailer" in customer_fields:
                    customer_kwargs["retailer"] = retailer
                if "created_by" in customer_fields:
                    customer_kwargs["created_by"] = created_by_user

                # Create dedicated Customer record without receipt number
                new_customer = Customer.objects.create(**customer_kwargs)

                # Reassign transaction
                sale.customer = new_customer
                sale.save(update_fields=["customer"])
                updated_count += 1

    print(f"Successfully cleaned and reassigned {updated_count} customer records.")

if __name__ == "__main__":
    fix_sale_customers()