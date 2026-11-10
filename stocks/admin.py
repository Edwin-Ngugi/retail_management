from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.forms.models import BaseInlineFormSet
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from analytics.services import AnalyticsService

from .models import (
    BranchItemPricing,
    CreditRepayment,
    CreditRepaymentSplit,
    Customer,
    CustomerReturn,
    DailyFinancialReport,
    DamageRecord,
    Expense,
    Item,
    MonthlyInventorySnapshot,
    MonthlyStockMovementArchive,
    MonthlyTransactionArchive,
    PendingApproval,
    Purchase,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReturn,
    RetailBranch,
    Sale,
    SaleItem,
    SalePayment,
    SaleTransaction,
    StockBatch,
    StockMovement,
    StockTransfer,
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
    updated_count = 0

    with transaction.atomic():
        for pricing in queryset.select_related("item", "branch"):
            metrics = AnalyticsService.get_item_metrics(
                pricing.item,
                branch_id=pricing.branch_id,
            )

            target_remaining = int(metrics.get("remaining_stock", 0))
            movements_offset = pricing.remaining_stock - pricing.opening_stock
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
    repaired_count = 0

    with transaction.atomic():
        for pricing in queryset.select_related("item", "branch"):
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
            msg = f"Successfully added {created_count} item(s) to '{branch.name}' with inherited cost prices."
        else:
            msg = f"No new items were added to '{branch.name}'."

        if existing_count > 0:
            msg += f" {existing_count} selected item(s) already existed in this branch."

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


@admin.action(description="Consolidate selected Purchase Orders into a single Batch Order")
def consolidate_into_batch_order(modeladmin, request, queryset):
    pos = list(queryset.order_by("purchase_date"))
    if len(pos) < 2:
        modeladmin.message_user(
            request,
            "Please select at least 2 purchase orders to consolidate.",
            messages.WARNING,
        )
        return

    # Ensure all selected POs belong to the same branch
    branch_ids = set(p.branch_id for p in pos)
    if len(branch_ids) > 1:
        modeladmin.message_user(
            request,
            "Selected orders must belong to the same branch.",
            messages.ERROR,
        )
        return

    with transaction.atomic():
        primary_po = pos[0]
        date_str = primary_po.purchase_date.strftime("%Y%m%d")
        batch_po_number = f"PO-{date_str}-BATCH-{primary_po.pk}"

        primary_po.po_number = batch_po_number
        primary_po.save(update_fields=["po_number"])

        items_moved = 0
        for secondary_po in pos[1:]:
            # Move line items to primary batch order
            updated_items = PurchaseOrderItem.objects.filter(purchase_order=secondary_po).update(purchase_order=primary_po)
            items_moved += updated_items
            
            # Update batch reference
            StockBatch.objects.filter(batch_number=secondary_po.po_number).update(batch_number=batch_po_number)
            
            # Delete redundant header
            secondary_po.delete()

        primary_po.recalculate_totals()

    modeladmin.message_user(
        request,
        f"Successfully merged {len(pos)} purchase orders ({items_moved} line items) into Batch Order '{batch_po_number}'.",
        messages.SUCCESS,
    )


# =============================================================================
# BASE ADMIN
# =============================================================================


class TenantAwareModelAdmin(admin.ModelAdmin):
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Any authenticated user has global management access across retailers
        if request.user.is_authenticated:
            return qs
        return qs.none()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Allow drop-down selection of all branches, items, customers, and suppliers
        if request.user.is_authenticated:
            if db_field.name in ("branch", "from_branch", "to_branch"):
                kwargs["queryset"] = RetailBranch.objects.all()
            elif db_field.name == "item":
                kwargs["queryset"] = Item.objects.all()
            elif db_field.name == "customer":
                kwargs["queryset"] = Customer.objects.all()
            elif db_field.name == "supplier":
                kwargs["queryset"] = Supplier.objects.all()
            elif db_field.name == "retailer":
                kwargs["queryset"] = Retailer.objects.all()

        return super().formfield_for_foreignkey(db_field, request, **kwargs)


# =============================================================================
# INLINES
# =============================================================================

from django.contrib import admin
from .models import Retailer  


@admin.register(Retailer)
class RetailerAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "created_at")
    search_fields = ("name", "code")  
    ordering = ("name",)

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


class SalePaymentInlineFormSet(BaseInlineFormSet):
    """
    Formset validator enforcing that split transactions contain at least TWO 
    child payment methods whose total sum equals the sale total.
    """
    def clean(self):
        super().clean()
        if any(self.errors):
            return

        valid_forms_count = 0
        total_inline_amount = 0

        for form in self.forms:
            if form.cleaned_data and not form.cleaned_data.get("DELETE", False):
                valid_forms_count += 1
                total_inline_amount += form.cleaned_data.get("amount", 0)

        if self.instance and self.instance.payment_method in [
            SaleTransaction.PaymentMethod.SPLIT,
            "SPLIT",
            "SPLIT PAYMENT",
        ]:
            if valid_forms_count < 2:
                raise ValidationError(
                    "Split Payment transactions require at least TWO child entries in the SALE PAYMENTS section below."
                )
            if self.instance.total_amount and total_inline_amount != self.instance.total_amount:
                raise ValidationError(
                    f"Sum of split payments (KSh {total_inline_amount:,.2f}) must equal transaction total (KSh {self.instance.total_amount:,.2f})."
                )


class SalePaymentInline(admin.TabularInline):
    model = SalePayment
    formset = SalePaymentInlineFormSet
    extra = 1
    fields = ("method", "amount", "reference_code", "created_at")
    readonly_fields = ("created_at",)


# =============================================================================
# RETAIL BRANCH, CUSTOMER, SUPPLIER
# =============================================================================


@admin.register(RetailBranch)
class RetailBranchAdmin(TenantAwareModelAdmin):
    list_display = ("name", "retailer", "location", "created_at")
    list_filter = ("retailer", "created_at")
    search_fields = ("name", "location", "retailer__username")


@admin.register(Customer)
class CustomerAdmin(TenantAwareModelAdmin):
    list_display = ("customer_id", "name", "phone_number", "retailer", "created_at")
    list_filter = ("retailer", "created_at")
    search_fields = ("customer_id", "name", "phone_number", "retailer__username")
    readonly_fields = ("customer_id",)


@admin.register(Supplier)
class SupplierAdmin(TenantAwareModelAdmin):
    list_display = ("name", "retailer")
    list_filter = ("retailer",)
    search_fields = ("name", "retailer__username")


# =============================================================================
# BRANCH ITEM PRICING & ITEM
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

    list_filter = ("branch", "branch__retailer")
    search_fields = ("item__name", "item__sku", "item__code", "branch__name")
    autocomplete_fields = ["item", "branch"]
    readonly_fields = ("display_remaining_stock_value",)
    actions = [rollover_pricing_stock, reverse_doubled_stock_rollover]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("item", "branch")

    @admin.display(description="Stock Level")
    def stock_level(self, obj):
        return obj.remaining_stock

    @admin.display(description="Stock Value")
    def display_remaining_stock_value(self, obj):
        return f"KSh {obj.remaining_stock_value:,.2f}"

    @admin.display(description="Status")
    def display_stock_status(self, obj):
        if obj.is_low_stock:
            return format_html('<span style="color: red; font-weight: bold;">Low Stock</span>')
        return format_html('<span style="color: green;">In Stock</span>')

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
                unit_cost = BranchItemPricing.calculate_wac(obj.branch, obj.item)

                StockMovement.objects.create(
                    branch=obj.branch,
                    item=obj.item,
                    quantity=abs(diff),
                    movement_type=movement_type,
                    unit_cost_price=unit_cost,
                    notes=f"Manual Admin Stock Level Adjustment by {request.user.username}",
                    created_by=request.user,
                )
                recalculate_wac_sales_cost(obj.branch, obj.item)


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
    list_filter = ("retailer", "created_at")
    search_fields = ("name", "code", "sku")
    readonly_fields = ("display_total_stock", "created_at", "updated_at")
    actions = [bulk_add_to_branch]
    inlines = [BranchItemPricingInline, PurchaseInline, StockMovementInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("retailer")
        if request.path.endswith("/autocomplete/"):
            return qs
        return qs.prefetch_related("branch_pricings").annotate(
            avg_cost=Avg("branch_pricings__unit_cost_price"),
            avg_retail=Avg("branch_pricings__unit_selling_price"),
        )

    @admin.display(description="Avg Cost Price", ordering="avg_cost")
    def display_avg_cost_price(self, obj):
        avg = getattr(obj, "avg_cost", None)
        return "N/A" if avg is None else f"KSh {avg:,.2f}"

    @admin.display(description="Avg Retail Price", ordering="avg_retail")
    def display_avg_retail_price(self, obj):
        avg = getattr(obj, "avg_retail", None)
        return "N/A" if avg is None else f"KSh {avg:,.2f}"

    @admin.display(description="Total Stock Across Branches")
    def display_total_stock(self, obj):
        return sum(pricing.remaining_stock for pricing in obj.branch_pricings.all())


# =============================================================================
# STOCK BATCH & ARCHIVES
# =============================================================================


@admin.register(StockBatch)
class StockBatchAdmin(TenantAwareModelAdmin):
    list_display = ("item", "branch", "batch_number", "quantity_received", "quantity_remaining", "unit_cost_price", "received_at", "reference")
    list_filter = ("branch", "received_at")
    search_fields = ("item__name", "item__sku", "item__code", "batch_number", "reference")
    autocomplete_fields = ["item", "branch"]


@admin.register(MonthlyInventorySnapshot)
class MonthlyInventorySnapshotAdmin(TenantAwareModelAdmin):
    list_display = ("item", "branch", "year", "month", "snapshot_date", "closing_stock", "unit_cost_price", "total_value")
    list_filter = ("year", "month", "branch")
    search_fields = ("item__name", "item__sku", "branch__name")
    autocomplete_fields = ["item", "branch"]


@admin.register(MonthlyStockMovementArchive)
class MonthlyStockMovementArchiveAdmin(TenantAwareModelAdmin):
    list_display = ("item", "branch", "movement_type", "quantity", "unit_cost_price", "period_date", "created_at", "archived_at")
    list_filter = ("branch", "period_date", "movement_type")
    search_fields = ("item__name", "branch__name", "movement_type")
    autocomplete_fields = ["item", "branch"]


@admin.register(StockMovement)
class StockMovementAdmin(TenantAwareModelAdmin):
    list_display = ("item", "branch", "batch", "movement_type", "quantity", "unit_cost_price", "date", "reference", "created_at", "created_by")
    list_filter = ("branch", "movement_type", "date", "created_at", "branch__retailer")
    search_fields = ("item__name", "item__sku", "branch__name", "reference", "notes")
    autocomplete_fields = ["item", "branch", "batch", "created_by"]

    def has_add_permission(self, request):
        return False


@admin.register(StockTransfer)
class StockTransferAdmin(TenantAwareModelAdmin):
    list_display = ("item", "from_branch", "to_branch", "quantity", "date", "created_by", "created_at")
    list_filter = ("from_branch", "to_branch", "date")
    search_fields = ("item__name", "item__sku", "from_branch__name", "to_branch__name", "notes")
    autocomplete_fields = ["item", "from_branch", "to_branch", "created_by"]


# =============================================================================
# PURCHASES
# =============================================================================


@admin.register(Purchase)
class PurchaseAdmin(TenantAwareModelAdmin):
    list_display = ("po_number_display", "item", "branch", "supplier_display", "quantity", "unit_cost_price", "total_cost_display", "amount_paid", "balance_due_display", "payment_method", "purchase_date", "created_by")
    list_filter = ("branch", "payment_method", "purchase_date", "created_at")
    search_fields = ("id", "item__name", "item__code", "item__sku", "supplier_name", "supplier__name", "branch__name")
    autocomplete_fields = ["item", "branch", "supplier", "created_by"]
    readonly_fields = ("total_cost_display", "balance_due_display", "created_at")
    inlines = [SupplierPaymentInline]
    ordering = ("-purchase_date", "-id")

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
    list_display = ("id", "purchase", "amount", "payment_method", "created_at")
    list_filter = ("payment_method", "created_at")
    search_fields = ("purchase__id", "purchase__item__name", "purchase__supplier_name")
    ordering = ("-created_at",)


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 1


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("po_number", "branch", "supplier_name", "total_amount", "amount_paid", "payment_method", "purchase_date")
    inlines = [PurchaseOrderItemInline]
    actions = [consolidate_into_batch_order]


@admin.register(PurchaseReturn)
class PurchaseReturnAdmin(admin.ModelAdmin):
    list_display = ("id", "branch", "item", "supplier_name", "quantity", "unit_cost_price", "total_refund_amount", "date", "created_by")
    list_filter = ("branch", "date", "created_at")
    search_fields = ("item__name", "supplier_name", "reason", "purchase__id")
    readonly_fields = ("total_refund_amount", "created_at")
    ordering = ("-date", "-created_at")

    def total_refund_amount(self, obj):
        return f"KSh {obj.total_refund_amount}"


@admin.register(MonthlyTransactionArchive)
class MonthlyTransactionArchiveAdmin(TenantAwareModelAdmin):
    list_display = ("transaction_id", "branch", "item", "transaction_type", "quantity", "amount", "unit_cost_price", "transaction_date", "archived_at")
    list_filter = ("branch", "transaction_type", "transaction_date", "archived_at")
    search_fields = ("transaction_id", "branch__name", "item__name", "transaction_type", "notes")
    autocomplete_fields = ["item", "branch"]
    readonly_fields = ("archived_at",)


# =============================================================================
# SALES & TRANSACTION MANAGEMENT
# =============================================================================


@admin.register(SaleTransaction)
class SaleTransactionAdmin(TenantAwareModelAdmin):
    list_display = (
        "receipt_number",
        "branch",
        "customer",
        "payment_method",
        "status_icon",
        "display_amount_paid",
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

    autocomplete_fields = ["branch", "customer", "created_by"]

    readonly_fields = (
        "receipt_number",
        "display_total_cost",
        "display_profit",
        "created_at",
    )

    inlines = [SaleItemInline, SalePaymentInline]
    actions = ["delete_sales_safely"]

    class Media:
        js = ("admin/js/sale_item_pricing.js",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("branch", "customer", "created_by")
        )

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            instance.save()
        formset.save_m2m()

        obj = form.instance
        if formset.model == SalePayment and obj.pk:
            split_payments = obj.split_payments.all()
            if split_payments.exists():
                total_split_paid = sum(sp.amount for sp in split_payments)
                is_paid = total_split_paid >= (obj.total_amount or 0)
                SaleTransaction.objects.filter(pk=obj.pk).update(
                    amount_paid=total_split_paid,
                    is_paid=is_paid,
                )

    @admin.display(description="Amount Paid", ordering="amount_paid")
    def display_amount_paid(self, obj):
        try:
            val = obj.amount_paid or 0
            return f"KSh {val:,.2f}"
        except Exception:
            return "KSh 0.00"

    @admin.display(description="Total Amount", ordering="total_amount")
    def display_total_amount(self, obj):
        try:
            val = obj.total_amount or 0
            return f"KSh {val:,.2f}"
        except Exception:
            return "KSh 0.00"

    @admin.display(description="Total Cost")
    def display_total_cost(self, obj):
        try:
            val = obj.total_cost or 0
            return f"KSh {val:,.2f}"
        except Exception:
            return "KSh 0.00"

    @admin.display(description="Profit")
    def display_profit(self, obj):
        try:
            val = (obj.total_amount or 0) - (obj.total_cost or 0)
            return f"KSh {val:,.2f}"
        except Exception:
            return "KSh 0.00"

    @admin.display(description="Is Paid", ordering="is_paid")
    def status_icon(self, obj):
        try:
            method = (obj.payment_method or "").strip().upper()
            balance_due = (obj.total_amount or 0) - (obj.amount_paid or 0)

            if method in ["SPLIT", "SPLIT PAYMENT"]:
                if balance_due > 0 or not obj.is_paid:
                    return mark_safe('<span style="color: #dc3545; font-weight: bold;">&#10008;</span>')
                return mark_safe('<span style="color: #28a745; font-weight: bold;">&#10004;</span>')

            if obj.is_paid or balance_due <= 0:
                return mark_safe('<span style="color: #28a745; font-weight: bold;">&#10004;</span>')

            return mark_safe('<span style="color: #dc3545; font-weight: bold;">&#10008;</span>')
        except Exception:
            return mark_safe('<span style="color: #888888;">?</span>')

    @admin.action(description="Delete selected sale transactions safely")
    def delete_sales_safely(self, request, queryset):
        sale_ids = list(queryset.values_list("id", flat=True))
        receipt_numbers = list(queryset.exclude(receipt_number__isnull=True).values_list("receipt_number", flat=True))

        with transaction.atomic():
            StockMovement.objects.filter(reference_id__in=sale_ids, movement_type=StockMovement.MovementType.SALE).delete()
            StockMovement.objects.filter(reference__in=[f"SALE-{sid}" for sid in sale_ids]).delete()
            if receipt_numbers:
                StockMovement.objects.filter(reference__in=[f"SALE-{r}" for r in receipt_numbers]).delete()
                StockMovement.objects.filter(reference__in=receipt_numbers).delete()

            CreditRepayment.objects.filter(transaction_id__in=sale_ids).delete()
            SalePayment.objects.filter(transaction_id__in=sale_ids).delete()
            Sale.objects.filter(transaction_id__in=sale_ids).delete()
            SaleItem.objects.filter(transaction_id__in=sale_ids).delete()

            deleted_count, _ = SaleTransaction.objects.filter(id__in=sale_ids).delete()

        self.message_user(request, f"Successfully deleted {deleted_count} sale transaction record(s).")

    def delete_model(self, request, obj):
        with transaction.atomic():
            StockMovement.objects.filter(reference_id=obj.pk, movement_type=StockMovement.MovementType.SALE).delete()
            StockMovement.objects.filter(reference=f"SALE-{obj.id}").delete()
            if obj.receipt_number:
                StockMovement.objects.filter(reference=f"SALE-{obj.receipt_number}").delete()
                StockMovement.objects.filter(reference=obj.receipt_number).delete()

            CreditRepayment.objects.filter(transaction=obj).delete()
            SalePayment.objects.filter(transaction=obj).delete()
            Sale.objects.filter(transaction=obj).delete()
            SaleItem.objects.filter(transaction=obj).delete()
            SaleTransaction.objects.filter(pk=obj.pk).delete()

    def delete_queryset(self, request, queryset):
        self.delete_sales_safely(request, queryset)


@admin.register(Sale)
class SaleAdmin(TenantAwareModelAdmin):
    list_display = ("receipt_number", "branch", "item", "quantity_sold", "unit_selling_price", "payment_method", "date")
    list_filter = ("branch", "payment_method", "date")
    search_fields = ("receipt_number", "item__name", "customer_name")


@admin.register(SalePayment)
class SalePaymentAdmin(TenantAwareModelAdmin):
    list_display = ("transaction", "method", "amount", "reference_code", "created_at")
    list_filter = ("method", "created_at")
    search_fields = ("reference_code", "transaction__receipt_number")


class CreditRepaymentSplitInline(admin.TabularInline):
    model = CreditRepaymentSplit
    extra = 1
    fields = ("method", "amount", "reference_code", "created_at")
    readonly_fields = ("created_at",)


@admin.register(CreditRepayment)
class CreditRepaymentAdmin(admin.ModelAdmin):
    ordering = ("-id",)
    
    list_display = (
        "id",
        "customer",
        "get_branch_name",
        "payment_method",
        "amount",
        "get_split_count",
        "date",
        "created_at",
    )
    list_filter = (
        "payment_method",
        "branch",
        "date",
        "created_at",
    )
    search_fields = (
        "customer__name",
        "customer__phone_number",
        "transaction__receipt_number",
        "transaction__invoice_number",
        "notes",
    )
    date_hierarchy = "date"
    readonly_fields = ("created_at",)
    autocomplete_fields = ("customer", "transaction", "branch", "created_by")
    inlines = [CreditRepaymentSplitInline]

    fieldsets = (
        (
            "Repayment Information",
            {
                "fields": (
                    "customer",
                    "transaction",
                    "branch",
                    "payment_method",
                    "amount",
                    "date",
                )
            },
        ),
        (
            "Additional Details",
            {
                "fields": (
                    "notes",
                    "created_by",
                    "created_at",
                )
            },
        ),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(split_count_anno=Count("splits"))

    @admin.display(description="Branch", ordering="branch__name")
    def get_branch_name(self, obj):
        if hasattr(obj, "branch") and obj.branch:
            return obj.branch.name
        if hasattr(obj, "transaction") and obj.transaction and getattr(obj.transaction, "branch", None):
            return f"{obj.transaction.branch.name} (via Invoice)"
        return "Unassigned Branch"

    @admin.display(description="Split Lines", ordering="split_count_anno")
    def get_split_count(self, obj):
        count = getattr(obj, "split_count_anno", 0)
        return f"{count} line(s)" if count > 0 else "-"


@admin.register(CreditRepaymentSplit)
class CreditRepaymentSplitAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "get_repayment_id",
        "get_customer_name",
        "method",
        "amount",
        "reference_code",
        "date",
        "created_at",
    )
    list_filter = (
        "method",
        "date",
        "created_at",
    )
    search_fields = (
        "reference_code",
        "repayment__customer__name",
        "repayment__transaction__receipt_number",
    )
    readonly_fields = ("created_at",)
    autocomplete_fields = ("repayment",)
    ordering = ("-created_at",)

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related(
            "repayment",
            "repayment__customer",
            "repayment__branch",
        )
        if request.user.is_superuser:
            return qs
        return qs.filter(repayment__branch__retailer=request.user)

    @admin.display(description="Repayment ID", ordering="repayment__id")
    def get_repayment_id(self, obj):
        return f"Repayment #{obj.repayment_id}" if obj.repayment_id else "-"

    @admin.display(description="Customer", ordering="repayment__customer__name")
    def get_customer_name(self, obj):
        if obj.repayment and getattr(obj.repayment, "customer", None):
            return getattr(obj.repayment.customer, "name", "-")
        return "-"


# =============================================================================
# RETURNS, DAMAGES, EXPENSES & REPORTS
# =============================================================================


@admin.register(CustomerReturn)
class CustomerReturnAdmin(TenantAwareModelAdmin):
    list_display = ("item", "branch", "quantity", "reason", "date", "created_by")
    list_filter = ("branch", "date")
    search_fields = ("item__name", "item__sku", "branch__name", "reason")
    autocomplete_fields = ["item", "branch", "created_by"]


@admin.register(SupplierReturn)
class SupplierReturnAdmin(TenantAwareModelAdmin):
    list_display = ("item", "branch", "quantity", "supplier_name", "reason", "date", "created_by")
    list_filter = ("branch", "date")
    search_fields = ("item__name", "item__sku", "branch__name", "supplier_name", "reason")
    autocomplete_fields = ["item", "branch", "created_by"]


@admin.register(DamageRecord)
class DamageRecordAdmin(TenantAwareModelAdmin):
    list_display = ("item", "branch", "quantity", "damage_type", "reason", "date", "created_by")
    list_filter = ("branch", "damage_type", "date")
    search_fields = ("item__name", "item__sku", "branch__name", "reason")
    autocomplete_fields = ["item", "branch", "created_by"]


@admin.register(Expense)
class ExpenseAdmin(TenantAwareModelAdmin):
    list_display = ("description", "amount", "branch", "retailer", "date")
    list_filter = ("date", "branch", "retailer")
    search_fields = ("description", "retailer__username", "branch__name")
    autocomplete_fields = ["branch", "retailer"]


@admin.register(PendingApproval)
class PendingApprovalAdmin(admin.ModelAdmin):
    list_display = ("id", "request_type", "status", "submitted_by", "reviewed_by", "created_at")
    list_filter = ("status", "request_type", "created_at")
    search_fields = ("id", "submitted_by__username", "reviewed_by__username")
    readonly_fields = ("id", "payload", "created_at", "updated_at")


@admin.register(DailyFinancialReport)
class DailyFinancialReportAdmin(TenantAwareModelAdmin):
    list_display = ("date", "branch", "retailer", "tax_rate_percentage", "display_total_sales_revenue", "display_gross_profit", "display_net_profit")
    list_filter = ("date", "branch", "retailer")
    autocomplete_fields = ["branch", "retailer"]

    @admin.display(description="Sales Revenue")
    def display_total_sales_revenue(self, obj):
        return f"KSh {obj.total_sales_revenue:,.2f}"

    @admin.display(description="Gross Profit")
    def display_gross_profit(self, obj):
        return f"KSh {obj.gross_profit:,.2f}"

    @admin.display(description="Net Profit")
    def display_net_profit(self, obj):
        return f"KSh {obj.net_profit:,.2f}"