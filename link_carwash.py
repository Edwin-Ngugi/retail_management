import os
import django

# Initialize Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")  # Adjust to your project's settings path
django.setup()

from django.db import transaction
from django.contrib.auth import get_user_model
from stocks.models import RetailBranch, Retailer

User = get_user_model()


def fix_retailer_foreign_keys():
    print("--- Fixing Retailer Foreign Key References ---")
    
    with transaction.atomic():
        # 1. Ensure a Retailer entity exists for every branch name
        for branch in RetailBranch.objects.all():
            clean_name = branch.name.strip()
            
            # Get or create the matching Retailer entity in stocks_retailer
            retailer_obj, created = Retailer.objects.get_or_create(
                name=clean_name,
                defaults={"code": f"RET-{branch.id}"}
            )
            
            # Explicitly assign and update foreign key reference
            branch.retailer = retailer_obj
            branch.save(update_fields=["retailer"])
            print(f"Updated Branch '{branch.name}' (ID: {branch.id}) -> Linked to Retailer '{retailer_obj.name}' (ID: {retailer_obj.id})")

        # 2. Clean up old synthetic accounts from auth_user table
        deleted_users, _ = User.objects.filter(username__startswith="retailer_").delete()
        if deleted_users:
            print(f"Cleaned up {deleted_users} synthetic user accounts from auth_user table.")

    print("\n--- Foreign key references fixed successfully! ---")


if __name__ == "__main__":
    fix_retailer_foreign_keys()