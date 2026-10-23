from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Avg, Q
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from analytics.services import AnalyticsService

from .models import (
    BranchItemPricing,
    CreditRepayment,
    Customer,
    CustomerReturn,
    DailyFinancialReport,
    DamageRecord,
    Expense,
    Item,
    MonthlyInventorySnapshot,
    MonthlyStockMovementArchive,
    PendingApproval,
    Purchase,
    RetailBranch,
    Sale,
    SaleItem,
    SalePayment,
    SaleTransaction,
    StockBatch,
    StockMovement,
    StockTransfer,
    MonthlyTransactionArchive,
    Supplier,
    SupplierPayment,
    SupplierReturn,
    recalculate_wac_sales_cost,
)


# =============================================================================
# ADMIN ACTIONS
# =============================================================================


@admin.action(
    description="Rollover selected records (copy remaining stock to opening stock)"
)
def rollover_pricing_stock(modeladmin, request, queryset):
    """
    Carry the current remaining stock of selected branch/item pricing records
    into opening_stock without double-counting past transactions.
    """
    updated_count = 0

    with transaction.atomic():
        for pricing in queryset.select_related("item", "branch"):
            metrics = AnalyticsService.get_item_metrics(
                pricing.item,
                branch_id=pricing.branch_id,
            )

            target_remaining = int(metrics.get("remaining_stock", 0))

            # Calculate current non-opening stock movements (Purchases, Sales, Adjustments, etc.)
            # current_remaining = opening_stock + movements_offset
            movements_offset = pricing.remaining_stock - pricing.opening_stock

            # Adjust opening_stock so that new total remaining_stock equals target_remaining
            pricing.opening_stock = target_remaining - movements_offset
            pricing.save(update_fields=["opening_stock"])

            updated_count += 1

    modeladmin.message_user(
        request,
        f"Successfully rolled over opening stock for {updated_count} pricing record(s).",
        messages.SUCCESS,
    )


@admin.action(
    description="REVERSE: Fix doubled stock rollover on selected records"
)
def reverse_doubled_stock_rollover(modeladmin, request, queryset):
    """
    Reverses the doubling effect by adjusting opening_stock back to its correct base level.
    """
    repaired_count = 0

    with transaction.atomic():
        for pricing in queryset.select_related("item", "branch"):
            # The previous rollover set: new_opening = opening + movements_offset
            # Which made: total_remaining = opening + 2 * movements_offset
            # Subtracting movements_offset restores true opening stock
            movements_offset = pricing.remaining_stock - pricing.opening_stock

            if movements_offset != 0:
                pricing.opening_stock = max(0, pricing.opening_stock - movements_offset)
                pricing.save(update_fields=["opening_stock"])
                repaired_count += 1

    modeladmin.message_user(
        request,
        f"Successfully corrected stock doubling for {repaired_count} pricing record(s).",
        messages.SUCCESS,
    )


@admin.action(description="Add selected items to a branch")
def bulk_add_to_branch(modeladmin, request, queryset):
    """
    Add selected items to another branch with inherited cost prices.
    """

    if "apply" in request.POST:
        branch_id = request.POST.get("branch")

        if not branch_id:
            modeladmin.message_user(
                request,
                "Please select a target branch.",
                messages.ERROR,
            )
            return HttpResponseRedirect(request.get_full_path())

        branch_qs = (
            RetailBranch.objects.all()
            if request.user.is_superuser
            else RetailBranch.objects.filter(retailer=request.user)
        )

        try:
            branch = branch_qs.get(pk=branch_id)
        except RetailBranch.DoesNotExist:
            modeladmin.message_user(
                request,
                "Selected branch does not exist or is unauthorized.",
                messages.ERROR,
            )
            return HttpResponseRedirect(request.get_full_path())

        with transaction.atomic():
            existing_item_ids = set(
                BranchItemPricing.objects.filter(
                    branch=branch,
                    item__in=queryset,
                ).values_list(
                    "item_id",
                    flat=True,
                )
            )

            new_pricings = []

            for item in queryset:
                if item.pk in existing_item_ids:
                    continue

                existing_pricing = (
                    BranchItemPricing.objects.filter(item=item)
                    .exclude(unit_cost_price=0)
                    .order_by("id")
                    .first()
                )

                cost_price = (
                    existing_pricing.unit_cost_price
                    if existing_pricing is not None
                    else 0
                )

                new_pricings.append(
                    BranchItemPricing(
                        branch=branch,
                        item=item,
                        unit_cost_price=cost_price,
                        unit_retail_price=0,
                    )
                )

            if new_pricings:
                BranchItemPricing.objects.bulk_create(
                    new_pricings,
                    ignore_conflicts=True,
                )

            created_count = len(new_pricings)
            existing_count = len(existing_item_ids)

        if created_count > 0:
            msg = (
                f"Successfully added {created_count} item(s) "
                f"to '{branch.name}' with inherited cost prices."
            )
        else:
            msg = f"No new items were added to '{branch.name}'."

        if existing_count > 0:
            msg += (
                f" {existing_count} selected item(s) already "
                f"existed in this branch."
            )

        modeladmin.message_user(
            request,
            msg,
            messages.SUCCESS if created_count > 0 else messages.WARNING,
        )

        return HttpResponseRedirect(request.get_full_path())

    branches = (
        RetailBranch.objects.all()
        if request.user.is_superuser
        else RetailBranch.objects.filter(retailer=request.user)
    )

    return render(
        request,
        "admin/bulk_add_to_branch.html",
        {
            "items": queryset,
            "branches": branches,
            "opts": modeladmin.model._meta,
            "title": "Select Branch to Transfer Cost Prices",
            "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        },
    )


# =============================================================================
# BASE ADMIN
# =============================================================================


class TenantAwareModelAdmin(admin.ModelAdmin):
    """
    Restricts non-superusers to records belonging to their retailer.
    """

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        if request.user.is_superuser:
            return qs

        field_names = [field.name for field in self.model._meta.get_fields()]

        if self.model == Item or "retailer" in field_names:
            return qs.filter(Q(retailer=request.user) | Q(retailer__isnull=True))

        if "branch" in field_names:
            return qs.filter(branch__retailer=request.user)

        if "from_branch" in field_names:
            return qs.filter(from_branch__retailer=request.user)

        return qs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_superuser:
            if db_field.name in ("branch", "from_branch", "to_branch"):
                kwargs["queryset"] = RetailBranch.objects.filter(
                    retailer=request.user
                )

            elif db_field.name == "item":
                kwargs["queryset"] = Item.objects.filter(
                    Q(retailer=request.user) | Q(retailer__isnull=True)
                )

            elif db_field.name == "customer":
                kwargs["queryset"] = Customer.objects.filter(
                    retailer=request.user
                )

            elif db_field.name == "supplier":
                kwargs["queryset"] = Supplier.objects.filter(
                    Q(retailer=request.user) | Q(retailer__isnull=True)
                )

            elif db_field.name == "retailer":
                kwargs["queryset"] = request.user.__class__.objects.filter(
                    pk=request.user.pk
                )

        return super().formfield_for_foreignkey(
            db_field,
            request,
            **kwargs,
        )


# =============================================================================
# INLINES
# =============================================================================


class BranchItemPricingInline(admin.TabularInline):
    model = BranchItemPricing
    extra = 1
    autocomplete_fields = ["branch"]

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        if request.user.is_superuser:
            return qs

        return qs.filter(branch__retailer=request.user)


class StockMovementInline(admin.TabularInline):
    model = StockMovement
    extra = 0
    can_delete = False
    ordering = ("-created_at",)

    readonly_fields = (
        "branch",
        "batch",
        "movement_type",
        "quantity",
        "unit_cost_price",
        "date",
        "reference",
        "notes",
        "created_by",
        "created_at",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        if request.user.is_superuser:
            return qs

        return qs.filter(branch__retailer=request.user)


class SupplierPaymentInline(admin.TabularInline):
    model = SupplierPayment
    extra = 0
    readonly_fields = ("created_at",)
    fields = ("amount", "payment_method", "notes", "created_at")


class PurchaseInline(admin.TabularInline):
    model = Purchase
    extra = 0
    autocomplete_fields = ["branch", "supplier", "created_by"]

    fields = (
        "branch",
        "supplier",
        "supplier_name",
        "quantity",
        "unit_cost_price",
        "purchase_date",
        "created_by",
    )

    readonly_fields = ("created_at",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        if request.user.is_superuser:
            return qs

        return qs.filter(branch__retailer=request.user)


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 1
    autocomplete_fields = ["item"]
    fields = (
        "item",
        "quantity_sold",
        "unit_selling_price",
        "discount_per_item",
        "unit_cost_price",
    )
    readonly_fields = ("unit_cost_price",)


class SalePaymentInline(admin.TabularInline):
    model = SalePayment
    extra = 0
    fields = ("method", "amount", "reference_code", "created_at")
    readonly_fields = ("created_at",)


# =============================================================================
# RETAIL BRANCH
# =============================================================================


@admin.register(RetailBranch)
class RetailBranchAdmin(TenantAwareModelAdmin):
    list_display = (
        "name",
        "retailer",
        "location",
        "created_at",
    )

    list_filter = (
        "retailer",
        "created_at",
    )

    search_fields = (
        "name",
        "location",
        "retailer__username",
    )


# =============================================================================
# CUSTOMER & SUPPLIER
# =============================================================================


@admin.register(Customer)
class CustomerAdmin(TenantAwareModelAdmin):
    list_display = (
        "customer_id",
        "name",
        "phone_number",
        "retailer",
        "created_at",
    )

    list_filter = (
        "retailer",
        "created_at",
    )

    search_fields = (
        "customer_id",
        "name",
        "phone_number",
        "retailer__username",
    )

    readonly_fields = ("customer_id",)


@admin.register(Supplier)
class SupplierAdmin(TenantAwareModelAdmin):
    list_display = (
        "name",
        "retailer",
    )

    list_filter = ("retailer",)

    search_fields = (
        "name",
        "retailer__username",
    )


# =============================================================================
# BRANCH ITEM PRICING FORM & ADMIN
# =============================================================================


class BranchItemPricingAdminForm(forms.ModelForm):
    stock_level = forms.IntegerField(
        label="Stock Level",
        required=False,
        help_text="Directly edit current stock level. Saving creates an automatic Stock Adjustment record.",
    )

    class Meta:
        model = BranchItemPricing
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["stock_level"].initial = getattr(
                self.instance, "remaining_stock", 0
            )


@admin.register(BranchItemPricing)
class BranchItemPricingAdmin(TenantAwareModelAdmin):
    form = BranchItemPricingAdminForm

    list_display = (
        "item",
        "branch",
        "opening_stock",
        "opening_stock_unit_cost_price",
        "unit_cost_price",
        "unit_selling_price",
        "min_stock_threshold",
        "stock_level",
        "display_stock_status",
        "updated_at",
    )

    list_filter = (
        "branch",
        "branch__retailer",
    )

    search_fields = (
        "item__name",
        "item__sku",
        "item__code",
        "branch__name",
    )

    autocomplete_fields = [
        "item",
        "branch",
    ]

    readonly_fields = (
        "display_remaining_stock_value",
    )

    actions = [
        rollover_pricing_stock,
        reverse_doubled_stock_rollover,
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "item",
                "branch",
            )
        )

    @admin.display(description="Stock Level")
    def stock_level(self, obj):
        return obj.remaining_stock

    @admin.display(description="Stock Value")
    def display_remaining_stock_value(self, obj):
        return f"KSh {obj.remaining_stock_value:,.2f}"

    @admin.display(description="Status")
    def display_stock_status(self, obj):
        if obj.is_low_stock:
            return format_html(
                '<span style="color: red; font-weight: bold;">{}</span>',
                "Low Stock",
            )

        return format_html(
            '<span style="color: green;">{}</span>',
            "In Stock",
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

        new_stock = form.cleaned_data.get("stock_level")
        if new_stock is not None:
            current_stock = getattr(obj, "remaining_stock", 0)
            diff = new_stock - current_stock

            if diff != 0:
                movement_type = (
                    StockMovement.MovementType.ADJUSTMENT_IN
                    if diff > 0
                    else StockMovement.MovementType.ADJUSTMENT_OUT
                )

                unit_cost = BranchItemPricing.calculate_wac(
                    obj.branch,
                    obj.item,
                )

                StockMovement.objects.create(
                    branch=obj.branch,
                    item=obj.item,
                    quantity=abs(diff),
                    movement_type=movement_type,
                    unit_cost_price=unit_cost,
                    notes=f"Manual Admin Stock Level Adjustment by {request.user.username}",
                    created_by=request.user,
                )

                # Recalculate WAC and sales costs
                recalculate_wac_sales_cost(obj.branch, obj.item)


# =============================================================================
# ITEM
# =============================================================================


@admin.register(Item)
class ItemAdmin(TenantAwareModelAdmin):
    list_display = (
        "name",
        "code",
        "sku",
        "retailer",
        "display_avg_cost_price",
        "display_avg_retail_price",
        "display_total_stock",
    )

    list_filter = (
        "retailer",
        "created_at",
    )

    search_fields = (
        "name",
        "code",
        "sku",
    )

    readonly_fields = (
        "display_total_stock",
        "created_at",
        "updated_at",
    )

    actions = [
        bulk_add_to_branch,
    ]

    inlines = [
        BranchItemPricingInline,
        PurchaseInline,
        StockMovementInline,
    ]

    fieldsets = (
        (
            "Basic Information",
            {
                "fields": (
                    "retailer",
                    "name",
                    "code",
                    "sku",
                ),
            },
        ),
        (
            "Global Stock Overview",
            {
                "fields": ("display_total_stock",),
            },
        ),
        (
            "System Timestamps",
            {
                "classes": ("collapse",),
                "fields": (
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("retailer")

        if request.path.endswith("/autocomplete/"):
            return qs

        return qs.prefetch_related("branch_pricings").annotate(
            avg_cost=Avg("branch_pricings__unit_cost_price"),
            avg_retail=Avg("branch_pricings__unit_selling_price"),
        )

    @admin.display(
        description="Avg Cost Price",
        ordering="avg_cost",
    )
    def display_avg_cost_price(self, obj):
        avg = getattr(obj, "avg_cost", None)
        if avg is None:
            return "N/A"
        return f"KSh {avg:,.2f}"

    @admin.display(
        description="Avg Retail Price",
        ordering="avg_retail",
    )
    def display_avg_retail_price(self, obj):
        avg = getattr(obj, "avg_retail", None)
        if avg is None:
            return "N/A"
        return f"KSh {avg:,.2f}"

    @admin.display(description="Total Stock Across Branches")
    def display_total_stock(self, obj):
        return sum(
            pricing.remaining_stock for pricing in obj.branch_pricings.all()
        )


# =============================================================================
# STOCK BATCH & MONTHLY SNAPSHOTS / ARCHIVES
# =============================================================================


@admin.register(StockBatch)
class StockBatchAdmin(TenantAwareModelAdmin):
    list_display = (
        "item",
        "branch",
        "batch_number",
        "quantity_received",
        "quantity_remaining",
        "unit_cost_price",
        "received_at",
        "reference",
    )

    list_filter = (
        "branch",
        "received_at",
    )

    search_fields = (
        "item__name",
        "item__sku",
        "item__code",
        "batch_number",
        "reference",
    )

    autocomplete_fields = [
        "item",
        "branch",
    ]


@admin.register(MonthlyInventorySnapshot)
class MonthlyInventorySnapshotAdmin(TenantAwareModelAdmin):
    list_display = (
        "item",
        "branch",
        "year",
        "month",
        "snapshot_date",
        "closing_stock",
        "unit_cost_price",
        "total_value",
    )

    list_filter = (
        "year",
        "month",
        "branch",
    )

    search_fields = (
        "item__name",
        "item__sku",
        "branch__name",
    )

    autocomplete_fields = [
        "item",
        "branch",
    ]


@admin.register(MonthlyStockMovementArchive)
class MonthlyStockMovementArchiveAdmin(TenantAwareModelAdmin):
    list_display = (
        "item",
        "branch",
        "movement_type",
        "quantity",
        "unit_cost_price",
        "period_date",
        "created_at",
        "archived_at",
    )

    list_filter = (
        "branch",
        "period_date",
        "movement_type",
    )

    search_fields = (
        "item__name",
        "branch__name",
        "movement_type",
    )

    autocomplete_fields = [
        "item",
        "branch",
    ]


# =============================================================================
# STOCK MOVEMENT & TRANSFER
# =============================================================================


@admin.register(StockMovement)
class StockMovementAdmin(TenantAwareModelAdmin):
    list_display = (
        "item",
        "branch",
        "batch",
        "movement_type",
        "quantity",
        "unit_cost_price",
        "date",
        "reference",
        "created_at",
        "created_by",
    )

    list_filter = (
        "branch",
        "movement_type",
        "date",
        "created_at",
        "branch__retailer",
    )

    search_fields = (
        "item__name",
        "item__sku",
        "branch__name",
        "reference",
        "notes",
    )

    autocomplete_fields = [
        "item",
        "branch",
        "batch",
        "created_by",
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "item",
                "branch",
                "batch",
                "created_by",
            )
        )

    def has_add_permission(self, request):
        return False


@admin.register(StockTransfer)
class StockTransferAdmin(TenantAwareModelAdmin):
    list_display = (
        "item",
        "from_branch",
        "to_branch",
        "quantity",
        "date",
        "created_by",
        "created_at",
    )

    list_filter = (
        "from_branch",
        "to_branch",
        "date",
    )

    search_fields = (
        "item__name",
        "item__sku",
        "from_branch__name",
        "to_branch__name",
        "notes",
    )

    autocomplete_fields = [
        "item",
        "from_branch",
        "to_branch",
        "created_by",
    ]


# =============================================================================
# PURCHASE & SUPPLIER PAYMENTS
# =============================================================================


@admin.register(Purchase)
class PurchaseAdmin(TenantAwareModelAdmin):
    list_display = (
        "po_number_display",
        "item",
        "branch",
        "supplier_display",
        "quantity",
        "unit_cost_price",
        "total_cost_display",
        "amount_paid",
        "balance_due_display",
        "payment_method",
        "purchase_date",
        "created_by",
    )

    list_filter = (
        "branch",
        "payment_method",
        "purchase_date",
        "created_at",
    )

    search_fields = (
        "id",
        "item__name",
        "item__code",
        "item__sku",
        "supplier_name",
        "supplier__name",
        "branch__name",
    )

    autocomplete_fields = [
        "item",
        "branch",
        "supplier",
        "created_by",
    ]

    readonly_fields = (
        "total_cost_display",
        "balance_due_display",
        "created_at",
    )

    inlines = [SupplierPaymentInline]

    ordering = ("-purchase_date", "-id")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "item",
                "branch",
                "supplier",
                "created_by",
            )
        )

    @admin.display(description="PO Number")
    def po_number_display(self, obj):
        return f"PO-{obj.id}"

    @admin.display(description="Supplier")
    def supplier_display(self, obj):
        return obj.supplier_name or (obj.supplier.name if obj.supplier else "Direct Supplier")

    @admin.display(description="Total Cost (KSh)")
    def total_cost_display(self, obj):
        return f"KSh {obj.total_cost:,.2f}"

    @admin.display(description="Balance Due (KSh)")
    def balance_due_display(self, obj):
        return f"KSh {obj.balance_due:,.2f}"


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(TenantAwareModelAdmin):
    list_display = (
        "id",
        "purchase",
        "amount",
        "payment_method",
        "created_at",
    )
    list_filter = ("payment_method", "created_at")
    search_fields = (
        "purchase__id",
        "purchase__item__name",
        "purchase__supplier_name",
    )
    ordering = ("-created_at",)


@admin.register(MonthlyTransactionArchive)
class MonthlyTransactionArchiveAdmin(TenantAwareModelAdmin):
    list_display = (
        "transaction_id",
        "branch",
        "item",
        "transaction_type",
        "quantity",
        "amount",
        "unit_cost_price",
        "transaction_date",
        "archived_at",
    )

    list_filter = (
        "branch",
        "transaction_type",
        "transaction_date",
        "archived_at",
    )

    search_fields = (
        "transaction_id",
        "branch__name",
        "item__name",
        "transaction_type",
        "notes",
    )

    autocomplete_fields = [
        "item",
        "branch",
    ]

    readonly_fields = ("archived_at",)


# =============================================================================
# SALES & CREDIT REPAYMENTS
# =============================================================================


@admin.register(SaleTransaction)
class SaleTransactionAdmin(TenantAwareModelAdmin):
    list_display = (
        "receipt_number",
        "branch",
        "customer",
        "payment_method",
        "status_icon",
        "amount_paid",
        "display_total_amount",
        "display_profit",
        "sale_date",
    )

    list_filter = (
        "branch",
        "payment_method",
        "is_paid",
        "sale_date",
        "branch__retailer",
    )

    search_fields = (
        "receipt_number",
        "invoice_number",
        "branch__name",
        "customer__name",
        "customer__phone_number",
        "items__item__name",
    )

    autocomplete_fields = [
        "branch",
        "customer",
        "created_by",
    ]

    readonly_fields = (
        "receipt_number",
        "display_total_cost",
        "display_profit",
        "created_at",
    )

    inlines = [
        SaleItemInline,
        SalePaymentInline,
    ]

    class Media:
        js = ("admin/js/sale_item_pricing.js",)

    @admin.display(description="Is Paid", ordering="is_paid")
    def status_icon(self, obj):
        method = (obj.payment_method or "").strip().upper()
        balance_due = obj.total_amount - obj.amount_paid

        # 1. Split Payment with an outstanding credit balance -> Grey Check Mark
        if method in ["SPLIT", "SPLIT PAYMENT"]:
            if balance_due > 0 or not obj.is_paid:
                return mark_safe(
                    '<span style="color: #888888; font-size: 16px; font-weight: bold;" title="Split Payment (Outstanding Balance)">&#10004;</span>'
                )
            return mark_safe(
                '<span style="color: #28a745; font-size: 16px; font-weight: bold;" title="Fully Paid">&#10004;</span>'
            )

        # 2. Fully Paid Non-Split Sales -> Green Check Mark
        if obj.is_paid or balance_due <= 0:
            return mark_safe(
                '<span style="color: #28a745; font-size: 16px; font-weight: bold;" title="Fully Paid">&#10004;</span>'
            )

        # 3. Full Credit / Unpaid Sales -> Red Cross Icon
        return mark_safe(
            '<span style="color: #dc3545; font-size: 16px; font-weight: bold;" title="Unpaid / Full Credit">&#10008;</span>'
        )

    def save_related(self, request, form, formsets, change):
        # Save inlines first so database contains all SaleItem and SalePayment rows
        super().save_related(request, form, formsets, change)
        obj = form.instance

        with transaction.atomic():
            # 1. Sync StockMovements for all items
            for item in obj.items.all():
                cost_price = item.unit_cost_price
                if not cost_price:
                    pricing = BranchItemPricing.objects.filter(
                        branch=obj.branch,
                        item=item.item
                    ).first()
                    cost_price = pricing.unit_cost_price if pricing else 0

                sale_date = obj.sale_date.date() if hasattr(obj.sale_date, "date") else obj.sale_date

                StockMovement.objects.update_or_create(
                    branch=obj.branch,
                    item=item.item,
                    movement_type=StockMovement.MovementType.SALE,
                    reference_id=obj.pk,
                    defaults={
                        "quantity": item.quantity_sold,
                        "unit_cost_price": cost_price,
                        "date": sale_date,
                        "reference": f"SALE-{obj.receipt_number or obj.pk}",
                        "created_by": request.user,
                    },
                )

                recalculate_wac_sales_cost(obj.branch, item.item)

            # 2. Recalculate transaction total strictly from saved items
            total = sum(
                (item.quantity_sold * item.unit_selling_price) - (item.quantity_sold * item.discount_per_item)
                for item in obj.items.all()
            )

            method = (obj.payment_method or "").strip().upper()
            payments_qs = getattr(obj, "split_payments", None) or getattr(obj, "payments", None)

            if method == "CREDIT":
                amount_paid = 0
                is_paid = False
                if payments_qs is not None:
                    payments_qs.all().delete()

            elif method in ["SPLIT", "SPLIT PAYMENT"]:
                # Fetch fresh payments directly from DB after inline saving
                if payments_qs is not None:
                    actual_payments = [
                        p.amount for p in payments_qs.all()
                        if str(p.method).strip().upper() not in ["CREDIT", "CREDIT SALE"]
                    ]
                else:
                    actual_payments = []

                paid_sum = sum(actual_payments)
                amount_paid = paid_sum

                # STRICT ACCURACY: is_paid is ONLY True if actual paid sum meets or exceeds total
                is_paid = bool(total > 0 and paid_sum >= total)

            else:
                amount_paid = total
                is_paid = True

                if payments_qs is not None:
                    updated_count = payments_qs.filter(method=obj.payment_method).update(amount=total)
                    if updated_count == 0 and total > 0:
                        payments_qs.model.objects.create(
                            transaction=obj,
                            method=obj.payment_method,
                            amount=total,
                            reference_code="",
                        )

            # 3. Direct DB update
            SaleTransaction.objects.filter(pk=obj.pk).update(
                total_amount=total,
                amount_paid=amount_paid,
                is_paid=is_paid,
            )

            # 4. Mutate memory instance for current response display
            obj.total_amount = total
            obj.amount_paid = amount_paid
            obj.is_paid = is_paid

    def response_change(self, request, obj):
        # Refresh instance from DB before rendering change view response
        obj.refresh_from_db()
        return super().response_change(request, obj)

    def response_add(self, request, obj, post_url_continue=None):
        # Refresh instance from DB before rendering add view response
        obj.refresh_from_db()
        return super().response_add(request, obj, post_url_continue)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "branch",
                "customer",
                "created_by",
            )
            .prefetch_related("items", "split_payments")
        )

    @admin.display(description="Total Amount")
    def display_total_amount(self, obj):
        return f"KSh {obj.total_amount:,.2f}"

    @admin.display(description="Total Cost")
    def display_total_cost(self, obj):
        return f"KSh {obj.total_cost:,.2f}"

    @admin.display(description="Profit")
    def display_profit(self, obj):
        return f"KSh {obj.profit:,.2f}"


@admin.register(Sale)
class SaleAdmin(TenantAwareModelAdmin):
    list_display = (
        "receipt_number",
        "branch",
        "item",
        "quantity_sold",
        "unit_selling_price",
        "payment_method",
        "date",
    )
    list_filter = ("branch", "payment_method", "date")
    search_fields = ("receipt_number", "item__name", "customer_name")


@admin.register(SalePayment)
class SalePaymentAdmin(TenantAwareModelAdmin):
    list_display = ("transaction", "method", "amount", "reference_code", "created_at")
    list_filter = ("method", "created_at")
    search_fields = ("reference_code", "transaction__receipt_number")


@admin.register(CreditRepayment)
class CreditRepaymentAdmin(TenantAwareModelAdmin):
    list_display = (
        "customer",
        "transaction",
        "branch",
        "payment_method",
        "amount",
        "date",
        "created_by",
        "created_at",
    )

    list_filter = (
        "branch",
        "date",
        "payment_method",
    )

    search_fields = (
        "customer__name",
        "transaction__receipt_number",
        "branch__name",
        "notes",
    )

    autocomplete_fields = [
        "customer",
        "transaction",
        "branch",
        "created_by",
    ]


# =============================================================================
# CUSTOMER & SUPPLIER RETURNS
# =============================================================================


@admin.register(CustomerReturn)
class CustomerReturnAdmin(TenantAwareModelAdmin):
    list_display = (
        "item",
        "branch",
        "quantity",
        "reason",
        "date",
        "created_by",
    )

    list_filter = (
        "branch",
        "date",
    )

    search_fields = (
        "item__name",
        "item__sku",
        "branch__name",
        "reason",
    )

    autocomplete_fields = [
        "item",
        "branch",
        "created_by",
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "item",
                "branch",
                "created_by",
            )
        )


@admin.register(SupplierReturn)
class SupplierReturnAdmin(TenantAwareModelAdmin):
    list_display = (
        "item",
        "branch",
        "quantity",
        "supplier_name",
        "reason",
        "date",
        "created_by",
    )

    list_filter = (
        "branch",
        "date",
    )

    search_fields = (
        "item__name",
        "item__sku",
        "branch__name",
        "supplier_name",
        "reason",
    )

    autocomplete_fields = [
        "item",
        "branch",
        "created_by",
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "item",
                "branch",
                "created_by",
            )
        )


# =============================================================================
# DAMAGE RECORDS
# =============================================================================


@admin.register(DamageRecord)
class DamageRecordAdmin(TenantAwareModelAdmin):
    list_display = (
        "item",
        "branch",
        "quantity",
        "damage_type",
        "reason",
        "date",
        "created_by",
    )

    list_filter = (
        "branch",
        "damage_type",
        "date",
    )

    search_fields = (
        "item__name",
        "item__sku",
        "branch__name",
        "reason",
    )

    autocomplete_fields = [
        "item",
        "branch",
        "created_by",
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "item",
                "branch",
                "created_by",
            )
        )


# =============================================================================
# EXPENSES
# =============================================================================


@admin.register(Expense)
class ExpenseAdmin(TenantAwareModelAdmin):
    list_display = (
        "description",
        "amount",
        "branch",
        "retailer",
        "date",
    )

    list_filter = (
        "date",
        "branch",
        "retailer",
    )

    search_fields = (
        "description",
        "retailer__username",
        "branch__name",
    )

    autocomplete_fields = [
        "branch",
        "retailer",
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "branch",
                "retailer",
            )
        )


# =============================================================================
# PENDING APPROVALS
# =============================================================================


@admin.register(PendingApproval)
class PendingApprovalAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "request_type",
        "status",
        "submitted_by",
        "reviewed_by",
        "created_at",
    )

    list_filter = (
        "status",
        "request_type",
        "created_at",
    )

    search_fields = (
        "id",
        "submitted_by__username",
        "reviewed_by__username",
    )

    readonly_fields = (
        "id",
        "payload",
        "created_at",
        "updated_at",
    )


# =============================================================================
# DAILY FINANCIAL REPORTS
# =============================================================================


@admin.register(DailyFinancialReport)
class DailyFinancialReportAdmin(TenantAwareModelAdmin):
    list_display = (
        "date",
        "branch",
        "retailer",
        "tax_rate_percentage",
        "display_total_sales_revenue",
        "display_gross_profit",
        "display_net_profit",
    )

    list_filter = (
        "date",
        "branch",
        "retailer",
    )

    autocomplete_fields = [
        "branch",
        "retailer",
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "branch",
                "retailer",
            )
        )

    @admin.display(description="Sales Revenue")
    def display_total_sales_revenue(self, obj):
        return f"KSh {obj.total_sales_revenue:,.2f}"

    @admin.display(description="Gross Profit")
    def display_gross_profit(self, obj):
        return f"KSh {obj.gross_profit:,.2f}"

    @admin.display(description="Net Profit")
    def display_net_profit(self, obj):
        return f"KSh {obj.net_profit:,.2f}"