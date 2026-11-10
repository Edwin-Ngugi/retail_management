from django.db import migrations, models
import django.db.models.deletion


def migrate_users_to_retailers(apps, schema_editor):
    """
    1. Create a Retailer entry for every unique branch.
    2. Map items, customers, suppliers, expenses to their respective Retailers.
    3. Resolve duplicate item names under the same Retailer to avoid unique constraint collisions.
    """
    Retailer = apps.get_model("stocks", "Retailer")
    RetailBranch = apps.get_model("stocks", "RetailBranch")
    Customer = apps.get_model("stocks", "Customer")
    Supplier = apps.get_model("stocks", "Supplier")
    Item = apps.get_model("stocks", "Item")
    Expense = apps.get_model("stocks", "Expense")
    DailyFinancialReport = apps.get_model("stocks", "DailyFinancialReport")
    BranchStock = apps.get_model("stocks", "BranchStock")

    # Step 1: Ensure a Retailer entity exists for every branch
    branch_retailer_map = {}
    for branch in RetailBranch.objects.all():
        clean_name = branch.name.strip()
        retailer_obj, _ = Retailer.objects.get_or_create(name=clean_name)
        RetailBranch.objects.filter(pk=branch.pk).update(retailer_id=retailer_obj.id)
        branch_retailer_map[branch.id] = retailer_obj.id

    default_retailer = Retailer.objects.first()
    if not default_retailer:
        default_retailer = Retailer.objects.create(name="Default Business")

    # Step 2: Assign Items to Retailers based on their BranchStock association
    for item in Item.objects.all():
        stock_entry = BranchStock.objects.filter(item_id=item.id).first()
        target_retailer_id = (
            branch_retailer_map.get(stock_entry.branch_id)
            if stock_entry and stock_entry.branch_id in branch_retailer_map
            else default_retailer.id
        )

        # Check for duplicate item name under the same retailer
        duplicate_exists = Item.objects.filter(
            retailer_id=target_retailer_id, 
            name=item.name
        ).exclude(pk=item.pk).exists()

        if duplicate_exists:
            # Suffix the duplicate item to preserve constraint
            new_name = f"{item.name} (Alt {item.id})"
            Item.objects.filter(pk=item.pk).update(retailer_id=target_retailer_id, name=new_name)
        else:
            Item.objects.filter(pk=item.pk).update(retailer_id=target_retailer_id)

    # Step 3: Map remaining entities cleanly
    Customer.objects.filter(retailer_id__isnull=True).update(retailer_id=default_retailer.id)
    Supplier.objects.filter(retailer_id__isnull=True).update(retailer_id=default_retailer.id)
    Expense.objects.filter(retailer_id__isnull=True).update(retailer_id=default_retailer.id)
    DailyFinancialReport.objects.filter(retailer_id__isnull=True).update(retailer_id=default_retailer.id)


class Migration(migrations.Migration):

    dependencies = [
        ("stocks", "0050_alter_retailbranch_retailer"),
    ]

    operations = [
        # 1. Create the Retailer table
        migrations.CreateModel(
            name="Retailer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="e.g., Melvex Enterprises, Car Wash Ltd", max_length=255, unique=True)),
                ("code", models.CharField(blank=True, help_text="Optional unique retailer identification code", max_length=50, null=True, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),

        # 2. Safely populate Retailers and re-link FKs + resolve duplicates
        migrations.RunPython(migrate_users_to_retailers, reverse_code=migrations.RunPython.noop),

        # 3. Apply Schema Alterations
        migrations.AlterField(
            model_name="retailbranch",
            name="retailer",
            field=models.ForeignKey(blank=True, help_text="The retailer/business entity that owns this branch", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="branches", to="stocks.retailer"),
        ),
        migrations.AlterField(
            model_name="customer",
            name="retailer",
            field=models.ForeignKey(blank=True, help_text="The retailer/client who owns this customer entry", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="customers", to="stocks.retailer"),
        ),
        migrations.AlterField(
            model_name="supplier",
            name="retailer",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="suppliers", to="stocks.retailer"),
        ),
        migrations.AlterField(
            model_name="item",
            name="retailer",
            field=models.ForeignKey(blank=True, help_text="The retailer/client who owns this item catalog entry", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="items", to="stocks.retailer"),
        ),
        migrations.AlterField(
            model_name="expense",
            name="retailer",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="expenses", to="stocks.retailer"),
        ),
        migrations.AlterField(
            model_name="dailyfinancialreport",
            name="retailer",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="financial_reports", to="stocks.retailer"),
        ),
    ]