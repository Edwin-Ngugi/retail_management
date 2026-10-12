import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction as db_transaction
from django.db.models import F, Q, Sum
from django.db.models import ExpressionWrapper, DecimalField
from django.db.models.functions import Coalesce
from django.utils import timezone


logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
MIN_QTY = Decimal("0.001")


def quantize_money(value):
    """Convert a Decimal value to 2 decimal places."""
    if value is None:
        return ZERO

    return Decimal(str(value)).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )


def generate_document_number(is_credit=False):
    """
    Generates a unique document identifier:
    - Invoices: INV-YYYYMMDD-8HEXDIGITS
    - Receipts: REC-YYYYMMDD-8HEXDIGITS
    """
    prefix = "INV" if is_credit else "REC"
    date_str = timezone.now().strftime("%Y%m%d")
    unique_suffix = uuid.uuid4().hex[:8].upper()
    return f"{prefix}-{date_str}-{unique_suffix}"


def generate_receipt_number():
    """Backwards compatibility wrapper for standard receipts."""
    return generate_document_number(is_credit=False)


# -----------------------------------------------------------------------------
# Base & Setup Models
# -----------------------------------------------------------------------------
class RetailBranch(models.Model):
    retailer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="branches",
        help_text="The retailer/client who owns this branch",
    )
    name = models.CharField(
        max_length=200,
        help_text="e.g. Nairobi Branch, Meru Branch",
    )
    location = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["retailer", "name"],
                name="unique_branch_per_retailer",
            )
        ]
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.retailer.username})"


class Customer(models.Model):
    retailer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="customers",
        help_text="The retailer/client who owns this customer entry",
    )
    customer_id = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Auto-generated unique customer identifier",
    )
    name = models.CharField(max_length=255)
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["retailer", "name", "phone_number"],
                name="unique_customer_per_retailer",
            ),
            models.UniqueConstraint(
                fields=["retailer", "customer_id"],
                name="unique_customer_id_per_retailer",
            ),
        ]
        ordering = ["name"]

    # ... keep existing save() and _assign_next_customer_id() methods ...

    @property
    def total_outstanding_balance(self):
        credit_sales = self.sales.filter(
            payment_method=SaleTransaction.PaymentMethod.CREDIT
        )
        total_balance = sum((sale.balance_due for sale in credit_sales), ZERO)
        return quantize_money(total_balance)

    @property
    def debt_balance(self):
        """
        Alias for total_outstanding_balance to support code expecting debt_balance.
        """
        return self.total_outstanding_balance

    def __str__(self):
        cid = f"[{self.customer_id}] " if self.customer_id else ""
        return f"{cid}{self.name} ({self.phone_number or 'No Phone'})"


class Supplier(models.Model):
    retailer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="suppliers",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class Item(models.Model):
    retailer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="items",
        null=True,
        blank=True,
        help_text="The retailer/client who owns this item catalog entry",
    )
    name = models.CharField(max_length=200)
    code = models.CharField(
        max_length=50,
        unique=True,
        null=True,
        blank=True,
    )
    sku = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Stock Keeping Unit",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["retailer", "name"],
                name="unique_item_per_retailer",
            )
        ]
        indexes = [
            models.Index(fields=["retailer", "sku"]),
        ]
        ordering = ["name"]

    @property
    def branch_pricing(self):
        return self.branch_pricings

    def get_branch_pricing(self, branch):
        branch_id = getattr(branch, "id", branch)
        return self.branch_pricings.filter(branch_id=branch_id).first()

    def __str__(self):
        retailer_name = (
            self.retailer.username
            if self.retailer
            else "No retailer"
        )
        return f"{self.name} ({retailer_name})"


# -----------------------------------------------------------------------------
# Pricing & WAC Calculation
# -----------------------------------------------------------------------------
class BranchItemPricing(models.Model):
    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="item_pricings",
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="branch_pricings",
    )

    opening_stock = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text="Initial baseline opening stock quantity.",
    )

    opening_stock_unit_cost_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text="Cost per unit of initial opening stock",
    )

    unit_cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text="Current weighted average cost per unit",
    )

    unit_selling_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(ZERO)],
        help_text="Current selling price per unit",
    )

    min_stock_threshold = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=Decimal("5.000"),
        validators=[MinValueValidator(ZERO)],
        help_text="Minimum stock level before low-stock warning",
    )

    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "item"],
                name="unique_item_per_branch",
            )
        ]

    @property
    def unit_retail_price(self):
        return self.unit_selling_price

    @unit_retail_price.setter
    def unit_retail_price(self, value):
        self.unit_selling_price = value

    def clean(self):
        super().clean()

        if hasattr(self, "branch") and self.branch and hasattr(self, "item") and self.item:
            if (
                self.item.retailer_id is None
                and self.branch.retailer_id is not None
            ):
                self.item.retailer = self.branch.retailer
                self.item.save(update_fields=["retailer"])

            elif self.branch.retailer_id != self.item.retailer_id:
                raise ValidationError(
                    "The branch and item must belong to the same retailer."
                )

    def save(self, *args, **kwargs):
        if (not self.unit_cost_price or self.unit_cost_price == ZERO) and self.opening_stock_unit_cost_price:
            self.unit_cost_price = self.opening_stock_unit_cost_price

        self.full_clean()
        super().save(*args, **kwargs)
        recalculate_wac_sales_cost(self.branch, self.item)

    def get_effective_opening_stock(self, year=None, month=None):
        """
        Retrieves opening stock from MonthlyInventorySnapshot if available.
        Falls back to the static opening_stock field if no snapshot exists.
        """
        if year and month:
            return MonthlyInventorySnapshot.get_opening_stock_for_period(
                self.branch, self.item, year, month
            )

        latest_snapshot = MonthlyInventorySnapshot.objects.filter(
            branch=self.branch,
            item=self.item
        ).order_by("-year", "-month").first()

        if latest_snapshot:
            return latest_snapshot.closing_stock

        return Decimal(str(self.opening_stock or ZERO))

    @classmethod
    def calculate_wac(cls, branch, item, before_date=None, year=None, month=None):
        pricing = cls.objects.filter(branch=branch, item=item).first()
        if not pricing:
            return ZERO

        # Determine opening quantity dynamically based on snapshot period or fallback baseline
        if year and month:
            opening_qty = Decimal(str(MonthlyInventorySnapshot.get_opening_stock_for_period(branch, item, year, month)))
        else:
            opening_qty = pricing.get_effective_opening_stock()

        opening_cost = Decimal(
            str(
                pricing.opening_stock_unit_cost_price
                if pricing.opening_stock_unit_cost_price is not None
                else ZERO
            )
        )

        if opening_qty > ZERO and opening_cost <= ZERO:
            fallback_cost = Decimal(str(pricing.unit_cost_price or ZERO))
            if fallback_cost > ZERO:
                opening_cost = fallback_cost

        current_qty = opening_qty
        current_value = opening_qty * opening_cost
        last_known_cost = opening_cost

        movements = StockMovement.objects.filter(branch=branch, item=item)

        if before_date is not None:
            movements = movements.filter(created_at__lt=before_date)

        movements = movements.order_by("created_at", "id")

        inbound_types = {
            StockMovement.MovementType.PURCHASE,
            StockMovement.MovementType.CUSTOMER_RETURN,
            StockMovement.MovementType.TRANSFER_IN,
            StockMovement.MovementType.ADJUSTMENT_IN,
        }

        outbound_types = {
            StockMovement.MovementType.SALE,
            StockMovement.MovementType.SUPPLIER_RETURN,
            StockMovement.MovementType.TRANSFER_OUT,
            StockMovement.MovementType.DAMAGE,
            StockMovement.MovementType.ADJUSTMENT_OUT,
        }

        for movement in movements:
            quantity = Decimal(str(abs(movement.quantity or ZERO)))
            if quantity <= ZERO:
                continue

            movement_type = movement.movement_type

            if movement_type in inbound_types:
                movement_cost = (
                    Decimal(str(movement.unit_cost_price))
                    if movement.unit_cost_price is not None
                    else ZERO
                )

                if movement_cost <= ZERO:
                    movement_cost = last_known_cost

                if current_qty <= ZERO:
                    current_qty = quantity
                    current_value = quantity * movement_cost
                    last_known_cost = movement_cost
                else:
                    current_value += quantity * movement_cost
                    current_qty += quantity
                    last_known_cost = current_value / current_qty

            elif movement_type in outbound_types:
                if current_qty <= ZERO:
                    continue

                current_wac = current_value / current_qty
                quantity_removed = min(quantity, current_qty)

                current_value -= quantity_removed * current_wac
                current_qty -= quantity_removed

                if current_qty > ZERO:
                    last_known_cost = current_value / current_qty
                else:
                    current_qty = ZERO
                    current_value = ZERO

        if current_qty > ZERO:
            return quantize_money(current_value / current_qty)

        return quantize_money(last_known_cost)

    def __str__(self):
        selling_display = (
            f"KSh {self.unit_selling_price}"
            if self.unit_selling_price is not None
            else "Unpriced"
        )
        return (
            f"{self.item.name} - "
            f"{self.branch.name} "
            f"(Selling: {selling_display})"
        )

    @property
    def total_stock_sold(self):
        return (
            StockMovement.objects.filter(
                branch=self.branch,
                item=self.item,
                movement_type=StockMovement.MovementType.SALE,
            ).aggregate(
                total=Coalesce(Sum("quantity"), ZERO, output_field=DecimalField())
            )["total"]
            or ZERO
        )

    @property
    def remaining_stock(self):
        totals = StockMovement.objects.filter(
            branch=self.branch,
            item=self.item,
        ).aggregate(
            stock_in=Coalesce(
                Sum(
                    "quantity",
                    filter=Q(
                        movement_type__in=[
                            StockMovement.MovementType.PURCHASE,
                            StockMovement.MovementType.CUSTOMER_RETURN,
                            StockMovement.MovementType.TRANSFER_IN,
                            StockMovement.MovementType.ADJUSTMENT_IN,
                        ]
                    ),
                ),
                ZERO,
                output_field=DecimalField(),
            ),
            stock_out=Coalesce(
                Sum(
                    "quantity",
                    filter=Q(
                        movement_type__in=[
                            StockMovement.MovementType.SALE,
                            StockMovement.MovementType.SUPPLIER_RETURN,
                            StockMovement.MovementType.TRANSFER_OUT,
                            StockMovement.MovementType.DAMAGE,
                            StockMovement.MovementType.ADJUSTMENT_OUT,
                        ]
                    ),
                ),
                ZERO,
                output_field=DecimalField(),
            ),
        )

        effective_opening = self.get_effective_opening_stock()

        return (
            effective_opening
            + Decimal(str(totals["stock_in"] or ZERO))
            - Decimal(str(totals["stock_out"] or ZERO))
        )

    @property
    def remaining_stock_value(self):
        return quantize_money(self.remaining_stock * self.unit_cost_price)

    @property
    def is_low_stock(self):
        return self.remaining_stock <= self.min_stock_threshold


# -----------------------------------------------------------------------------
# Stock Batch Model
# -----------------------------------------------------------------------------
class StockBatch(models.Model):
    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="stock_batches",
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="stock_batches",
    )
    batch_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )
    quantity_received = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(ZERO)],
    )
    quantity_remaining = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(ZERO)],
    )

    unit_cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(ZERO)],
    )

    received_at = models.DateTimeField(default=timezone.now)
    reference = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["received_at", "id"]
        indexes = [
            models.Index(
                fields=[
                    "branch",
                    "item",
                    "quantity_remaining",
                ]
            ),
        ]

    def clean(self):
        super().clean()

        if hasattr(self, "branch") and self.branch and hasattr(self, "item") and self.item:
            if (
                self.item.retailer_id is None
                and self.branch.retailer_id is not None
            ):
                self.item.retailer = self.branch.retailer
                self.item.save(update_fields=["retailer"])

            elif self.branch.retailer_id != self.item.retailer_id:
                raise ValidationError(
                    "The branch and item must belong to the same retailer."
                )

        if self.quantity_remaining is not None and self.quantity_received is not None:
            if self.quantity_remaining > self.quantity_received:
                raise ValidationError(
                    "Remaining quantity cannot exceed received quantity."
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.item.name} - "
            f"{self.quantity_remaining}/{self.quantity_received} "
            f"@ {self.unit_cost_price}"
        )

    @property
    def remaining_value(self):
        return Decimal(str(self.quantity_remaining)) * self.unit_cost_price

    @property
    def consumed_quantity(self):
        return Decimal(str(self.quantity_received)) - Decimal(str(self.quantity_remaining))


# -----------------------------------------------------------------------------
# Monthly Inventory Snapshot
# -----------------------------------------------------------------------------
class MonthlyInventorySnapshot(models.Model):
    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="monthly_snapshots",
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="monthly_snapshots",
    )

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    snapshot_date = models.DateField()

    opening_stock = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text="Opening stock carried over from the previous month's closing stock.",
    )

    closing_stock = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )

    unit_cost_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )

    total_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "branch",
                    "item",
                    "year",
                    "month",
                ],
                name="unique_monthly_snapshot_per_item",
            )
        ]
        indexes = [
            models.Index(
                fields=[
                    "branch",
                    "item",
                    "year",
                    "month",
                ]
            ),
            models.Index(
                fields=[
                    "year",
                    "month",
                ]
            ),
        ]
        ordering = ["-year", "-month"]

    @classmethod
    def get_opening_stock_for_period(cls, branch, item, year, month):
        """
        Retrieves opening stock for a target period by finding the previous month's
        closing stock snapshot. Falls back to BranchItemPricing.opening_stock if none exists.
        """
        prev_year = year - 1 if month == 1 else year
        prev_month = 12 if month == 1 else month - 1

        prev_snapshot = cls.objects.filter(
            branch=branch,
            item=item,
            year=prev_year,
            month=prev_month,
        ).first()

        if prev_snapshot:
            return prev_snapshot.closing_stock

        pricing = BranchItemPricing.objects.filter(branch=branch, item=item).first()
        return pricing.opening_stock if pricing else ZERO

    @classmethod
    def create_monthly_snapshot(cls, branch, item, year, month, closing_stock, unit_cost_price):
        """
        Creates or updates a monthly snapshot using the previous month's closing stock
        as the new period's opening stock.
        """
        opening_qty = cls.get_opening_stock_for_period(branch, item, year, month)
        
        snapshot, _ = cls.objects.update_or_create(
            branch=branch,
            item=item,
            year=year,
            month=month,
            defaults={
                "snapshot_date": date(year, month, 1),
                "opening_stock": opening_qty,
                "closing_stock": closing_stock,
                "unit_cost_price": unit_cost_price,
                "total_value": quantize_money(Decimal(str(closing_stock)) * Decimal(str(unit_cost_price))),
            },
        )
        return snapshot

    def __str__(self):
        return (
            f"{self.item.name} "
            f"({self.branch.name}) - "
            f"{self.year}/{self.month:02d}: "
            f"Opening {self.opening_stock} | Closing {self.closing_stock} units"
        )

class MonthlyStockMovementArchive(models.Model):
    branch = models.ForeignKey(
        RetailBranch, 
        on_delete=models.CASCADE,
        related_name="archived_movements",
    )
    item = models.ForeignKey(
        Item, 
        on_delete=models.CASCADE,
        related_name="archived_movements",
    )
    movement_type = models.CharField(max_length=50)
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(MIN_QTY)]
    )
    unit_cost_price = models.DecimalField(
        max_digits=12, 
        decimal_places=2,
        default=ZERO,
    )
    period_date = models.DateField(help_text="The month/year this movement belonged to")
    created_at = models.DateTimeField()
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['branch', 'period_date']),
            models.Index(fields=['branch', 'item', 'period_date']),
        ]

    def __str__(self):
        return f"Archived: {self.item.name} ({self.branch.name}) - {self.movement_type} - {self.quantity}"

class MonthlyTransactionArchive(models.Model):
    transaction_id = models.IntegerField(db_index=True)
    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="archived_transactions",
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="archived_transactions",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=ZERO,
    )
    unit_cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
    )
    transaction_type = models.CharField(
        max_length=50,
        default="SALE",
    )
    transaction_date = models.DateTimeField()
    notes = models.TextField(blank=True, default="")
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["branch", "transaction_date"]),
            models.Index(fields=["branch", "item", "transaction_date"]),
        ]
        ordering = ["-transaction_date"]

    def __str__(self):
        item_str = self.item.name if self.item else "General"
        return f"Archived Tx #{self.transaction_id}: {item_str} ({self.branch.name}) - KSh {self.amount}"    
# -----------------------------------------------------------------------------
# Stock Movement Ledger
# -----------------------------------------------------------------------------
class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        PURCHASE = "PURCHASE", "Purchase / Restock"
        SALE = "SALE", "Sale"
        CUSTOMER_RETURN = "CUSTOMER_RETURN", "Customer Return"
        SUPPLIER_RETURN = "SUPPLIER_RETURN", "Supplier Return"
        TRANSFER_IN = "TRANSFER_IN", "Stock Transfer In"
        TRANSFER_OUT = "TRANSFER_OUT", "Stock Transfer Out"
        DAMAGE = "DAMAGE", "Damaged / Lost Stock"
        ADJUSTMENT_IN = "ADJUSTMENT_IN", "Stock Adjustment In"
        ADJUSTMENT_OUT = "ADJUSTMENT_OUT", "Stock Adjustment Out"
        OPENING_STOCK = "OPENING_STOCK", "Opening Stock"

    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="stock_movements",
    )

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="stock_movements",
    )

    batch = models.ForeignKey(
        StockBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )

    movement_type = models.CharField(
        max_length=30,
        choices=MovementType.choices,
    )

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(MIN_QTY)]
    )

    unit_cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(ZERO)],
        help_text="Cost associated with this stock movement",
    )

    date = models.DateField(default=date.today)
    created_at = models.DateTimeField(default=timezone.now)

    reference = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    reference_id = models.PositiveIntegerField(
        null=True,
        blank=True,
    )

    notes = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements_created",
    )

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "item", "reference", "movement_type"],
                condition=~models.Q(reference=""),
                name="unique_movement_per_reference_item",
            )
        ]
        indexes = [
            models.Index(
                fields=[
                    "branch",
                    "item",
                    "movement_type",
                ]
            ),
            models.Index(
                fields=[
                    "branch",
                    "item",
                    "date",
                ]
            ),
            models.Index(
                fields=[
                    "branch",
                    "item",
                    "created_at",
                    "id",
                ]
            ),
        ]

    def clean(self):
        super().clean()

        if self.quantity is not None and Decimal(str(self.quantity)) <= ZERO:
            raise ValidationError(
                {"quantity": "Stock movement quantity must be greater than zero."}
            )

        if hasattr(self, "branch") and self.branch and hasattr(self, "item") and self.item:
            branch_retailer_id = getattr(self.branch, "retailer_id", None)
            item_retailer_id = getattr(self.item, "retailer_id", None)

            if item_retailer_id is None and branch_retailer_id is not None:
                self.item.retailer = self.branch.retailer
                self.item.save(update_fields=["retailer"])
            elif (
                branch_retailer_id is not None
                and item_retailer_id is not None
                and branch_retailer_id != item_retailer_id
            ):
                raise ValidationError(
                    "The branch and item must belong to the same retailer."
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def total_cost(self):
        if self.unit_cost_price is None:
            return ZERO

        return Decimal(abs(self.quantity)) * Decimal(str(self.unit_cost_price))

    def __str__(self):
        return (
            f"{self.item.name} "
            f"({self.branch.name}) - "
            f"{self.get_movement_type_display()} - "
            f"{self.quantity}"
        )

# -----------------------------------------------------------------------------
# Stock Transfer Model
# -----------------------------------------------------------------------------
class StockTransfer(models.Model):
    from_branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="transfers_sent",
    )

    to_branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="transfers_received",
    )

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="transfers",
    )

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(MIN_QTY)]
    )
    date = models.DateField(default=date.today)
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transfers_created",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()

        if self.from_branch_id and self.to_branch_id:
            if self.from_branch_id == self.to_branch_id:
                raise ValidationError(
                    "Source and destination branches must be different."
                )

            if hasattr(self, "from_branch") and hasattr(self, "to_branch"):
                if self.from_branch.retailer_id != self.to_branch.retailer_id:
                    raise ValidationError(
                        "Transfers are only allowed between branches "
                        "of the same retailer."
                    )

            if hasattr(self, "item") and self.item and hasattr(self, "from_branch") and self.from_branch:
                if self.item.retailer_id not in (
                    None,
                    self.from_branch.retailer_id,
                ):
                    raise ValidationError(
                        "The item must belong to the same retailer "
                        "as the branches."
                    )

    @db_transaction.atomic
    def save(self, *args, **kwargs):
        if isinstance(self.date, datetime):
            self.date = self.date.date()

        self.full_clean()

        is_new = self.pk is None

        if not is_new:
            raise ValidationError(
                "Existing stock transfers cannot be edited. "
                "Delete the transfer and create a new one."
            )

        source_pricing = (
            BranchItemPricing.objects
            .select_for_update()
            .filter(
                branch=self.from_branch,
                item=self.item,
            )
            .first()
        )

        if not source_pricing:
            raise ValidationError(
                f"Item '{self.item.name}' is not configured "
                f"for source branch "
                f"'{self.from_branch.name}'."
            )

        available_stock = source_pricing.remaining_stock

        if Decimal(str(self.quantity)) > available_stock:
            raise ValidationError(
                f"Insufficient stock at "
                f"{self.from_branch.name}. "
                f"Available: {available_stock}, "
                f"requested: {self.quantity}."
            )

        unit_cost = BranchItemPricing.calculate_wac(
            self.from_branch,
            self.item,
        )

        dest_pricing = (
            BranchItemPricing.objects
            .filter(
                branch=self.to_branch,
                item=self.item,
            )
            .first()
        )

        if not dest_pricing:
            dest_pricing = BranchItemPricing.objects.create(
                branch=self.to_branch,
                item=self.item,
                opening_stock=ZERO,
                opening_stock_unit_cost_price=ZERO,
                unit_cost_price=unit_cost,
                unit_selling_price=source_pricing.unit_selling_price,
            )

        super().save(*args, **kwargs)

        StockMovement.objects.create(
            branch=self.from_branch,
            item=self.item,
            movement_type=StockMovement.MovementType.TRANSFER_OUT,
            quantity=self.quantity,
            unit_cost_price=unit_cost,
            date=self.date,
            reference=f"TRANSFER-{self.pk}",
            reference_id=self.pk,
            notes=f"Transferred to {self.to_branch.name}",
            created_by=self.created_by,
        )

        StockMovement.objects.create(
            branch=self.to_branch,
            item=self.item,
            movement_type=StockMovement.MovementType.TRANSFER_IN,
            quantity=self.quantity,
            unit_cost_price=unit_cost,
            date=self.date,
            reference=f"TRANSFER-{self.pk}",
            reference_id=self.pk,
            notes=f"Transferred from {self.from_branch.name}",
            created_by=self.created_by,
        )

        recalculate_wac_sales_cost(self.from_branch, self.item)
        recalculate_wac_sales_cost(self.to_branch, self.item)

    def delete(self, *args, **kwargs):
        with db_transaction.atomic():
            from_branch = self.from_branch
            to_branch = self.to_branch
            item = self.item

            StockMovement.objects.filter(
                reference=f"TRANSFER-{self.pk}"
            ).delete()

            result = super().delete(*args, **kwargs)

            recalculate_wac_sales_cost(from_branch, item)
            recalculate_wac_sales_cost(to_branch, item)

            return result

    def __str__(self):
        return (
            f"Transfer ({self.quantity} "
            f"{self.item.name}): "
            f"{self.from_branch.name} -> "
            f"{self.to_branch.name}"
        )


# -----------------------------------------------------------------------------
# Purchase Entry Model
# -----------------------------------------------------------------------------
class Purchase(models.Model):
    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="purchases",
    )

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="purchases",
    )

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchases",
    )

    supplier_name = models.CharField(
        max_length=200,
        blank=True,
    )

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(MIN_QTY)],
    )

    unit_cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(ZERO)],
    )

    purchase_date = models.DateTimeField(default=timezone.now)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchases_created",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def total_cost(self):
        return quantize_money(Decimal(str(self.quantity)) * self.unit_cost_price)

    def clean(self):
        super().clean()

        if hasattr(self, "branch") and self.branch and hasattr(self, "item") and self.item:
            branch_retailer_id = getattr(self.branch, "retailer_id", None)
            item_retailer_id = getattr(self.item, "retailer_id", None)

            if item_retailer_id is None and branch_retailer_id is not None:
                self.item.retailer = self.branch.retailer
                self.item.save(update_fields=["retailer"])

            elif branch_retailer_id != item_retailer_id:
                raise ValidationError(
                    "The branch and item must belong to the same retailer."
                )

        if self.supplier and self.supplier.retailer_id and hasattr(self, "branch") and self.branch:
            if self.supplier.retailer_id != self.branch.retailer_id:
                raise ValidationError(
                    "The selected supplier must belong to the same retailer as the branch."
                )

    @db_transaction.atomic
    def save(self, *args, **kwargs):
        if isinstance(self.purchase_date, datetime) is False and isinstance(self.purchase_date, date):
            self.purchase_date = timezone.make_aware(
                datetime.combine(self.purchase_date, datetime.min.time())
            )

        self.full_clean()

        is_new = self.pk is None

        if not is_new:
            # Revert old stock movement and batch before updating
            StockMovement.objects.filter(reference=f"PURCHASE-{self.pk}").delete()
            StockBatch.objects.filter(reference=f"PURCHASE-{self.pk}").delete()

        super().save(*args, **kwargs)

        supplier_disp_name = (
            self.supplier_name
            or (self.supplier.name if self.supplier else "")
        )

        batch = StockBatch.objects.create(
            branch=self.branch,
            item=self.item,
            batch_number=f"PO-{self.pk}",
            quantity_received=self.quantity,
            quantity_remaining=self.quantity,
            unit_cost_price=self.unit_cost_price,
            received_at=self.purchase_date,
            reference=f"PURCHASE-{self.pk}",
        )

        StockMovement.objects.create(
            branch=self.branch,
            item=self.item,
            batch=batch,
            movement_type=StockMovement.MovementType.PURCHASE,
            quantity=self.quantity,
            unit_cost_price=self.unit_cost_price,
            date=self.purchase_date.date(),
            created_at=self.purchase_date,
            reference=f"PURCHASE-{self.pk}",
            reference_id=self.pk,
            notes=f"Purchase from {supplier_disp_name}".strip(),
            created_by=self.created_by,
        )

        recalculate_wac_sales_cost(self.branch, self.item)

    def delete(self, *args, **kwargs):
        with db_transaction.atomic():
            branch = self.branch
            item = self.item

            StockMovement.objects.filter(
                reference=f"PURCHASE-{self.pk}"
            ).delete()

            StockBatch.objects.filter(
                reference=f"PURCHASE-{self.pk}"
            ).delete()

            result = super().delete(*args, **kwargs)

            recalculate_wac_sales_cost(branch, item)

            return result

    def __str__(self):
        return (
            f"{self.item.name} "
            f"({self.branch.name}) - "
            f"Purchase ({self.quantity})"
        )



# -----------------------------------------------------------------------------
# Single Receipt & Credit Sale Models (Parent-Child)
# -----------------------------------------------------------------------------
class SaleTransaction(models.Model):

    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash"
        PAYBILL = "PAYBILL", "Paybill"
        SEND_MONEY = "SEND_MONEY", "Send Money"
        CREDIT = "CREDIT", "Credit"
        SPLIT = "SPLIT", "Split Payment"

    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="sale_transactions",
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales",
    )

    customer_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Name of the customer associated with this sale",
    )

    customer_phone = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Phone number of the customer",
    )

    credit_notes = models.TextField(
        blank=True,
        null=True,
        help_text="Additional notes for credit sales or transactions",
    )

    receipt_number = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        blank=True,
        help_text="System-generated internal identifier (REC-... or INV-...)",
    )

    invoice_number = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Optional manual/external reference number entered by user to track credit sales.",
    )

    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
    )

    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
    )

    is_paid = models.BooleanField(default=True)

    amount_paid = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )

    due_date = models.DateField(
        null=True,
        blank=True,
        help_text="Expected payment date for credit sales.",
    )

    credit_days = models.PositiveIntegerField(
        default=30,
        help_text="Number of credit allowance days for generating due date.",
    )

    sale_date = models.DateTimeField(default=timezone.now)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sale_transactions_created",
    )

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-sale_date", "-id"]

    # ------------------------------------------------------------------
    # TOTALS
    # ------------------------------------------------------------------

    def recalculate_totals(self):
        """Aggregate sale items and update the transaction total."""
        if not self.pk:
            return

        aggregated = self.items.aggregate(
            total=Sum(
                F("quantity_sold")
                * (F("unit_selling_price") - F("discount_per_item")),
                output_field=models.DecimalField(
                    max_digits=12,
                    decimal_places=2,
                ),
            )
        )

        self.total_amount = quantize_money(
            aggregated["total"] or ZERO
        )

    @property
    def total_cost(self):
        items_qs = (
            getattr(self, "items", None)
            or getattr(self, "sale_items", None)
        )

        if items_qs is not None:
            return sum(
                (item.total_cost for item in items_qs.all()),
                ZERO,
            )

        return ZERO

    @property
    def profit(self):
        return (
            self.total_amount or ZERO
        ) - self.total_cost

    # ------------------------------------------------------------------
    # CREDIT / RECEIVABLE CALCULATIONS
    # ------------------------------------------------------------------

    @property
    def credit_payment_amount(self):
        
        if self.payment_method == self.PaymentMethod.CREDIT:
            return quantize_money(
                self.total_amount or ZERO
            )

        # Split payment may contain a CREDIT component
        if self.payment_method == self.PaymentMethod.SPLIT:
            payments_qs = getattr(self, "payments", None)

            if payments_qs is not None:
                credit_total = payments_qs.filter(
                    method=self.PaymentMethod.CREDIT
                ).aggregate(
                    total=Coalesce(
                        Sum("amount"),
                        ZERO,
                    )
                )["total"]

                return quantize_money(
                    credit_total or ZERO
                )

        return ZERO

    @property
    def credit_repayments(self):
        

        repayment_total = self.repayments.aggregate(
            total=Coalesce(
                Sum("amount"),
                ZERO,
            )
        )["total"]

        return quantize_money(
            repayment_total or ZERO
        )

    @property
    def is_invoice(self):
        
        
        return self.credit_payment_amount > ZERO

    @property
    def total_repayments(self):
        
        return self.credit_repayments

    @property
    def balance_due(self):


        credit_amount = self.credit_payment_amount

        if credit_amount <= ZERO:
            return ZERO

        balance = (
            credit_amount
            - self.credit_repayments
        )

        return quantize_money(
            max(ZERO, balance)
        )

    @property
    def invoice_status(self):
        

        if not self.is_invoice:
            return "PAID"

        if self.balance_due <= ZERO:
            return "PAID"

        if self.credit_repayments > ZERO:
            return "PARTIALLY_PAID"

        return "UNPAID"

    # ------------------------------------------------------------------
    # VALIDATION
    # ------------------------------------------------------------------

    def clean(self):
        super().clean()

        
        if self.payment_method == self.PaymentMethod.CREDIT:
            self.is_paid = False
        else:
            self.is_paid = True

        if (
            self.amount_paid is not None
            and self.amount_paid < ZERO
        ):
            raise ValidationError(
                {
                    "amount_paid":
                        "Amount paid cannot be negative."
                }
            )

    # ------------------------------------------------------------------
    # SAVE
    # ------------------------------------------------------------------

    def save(self, *args, **kwargs):

        is_credit = (
            self.payment_method
            == self.PaymentMethod.CREDIT
        )

        # --------------------------------------------------------------
        # CREATE DEFAULT CUSTOMER FOR DIRECT CREDIT SALE
        # --------------------------------------------------------------

        if is_credit and not self.customer:

            retailer_user = self.created_by

            if (
                not retailer_user
                and hasattr(self, "branch")
                and self.branch
            ):
                if hasattr(self.branch, "retailer"):
                    retailer_user = self.branch.retailer

                elif hasattr(self.branch, "user"):
                    retailer_user = self.branch.user

            branch_name = (
                self.branch.name
                if hasattr(self, "branch")
                and self.branch
                else "Branch"
            )

            cust_name = (
                self.customer_name
                or f"Walk-in Customer ({branch_name})"
            )

            customer_kwargs = {
                "name": cust_name,
            }

            if retailer_user:
                customer_kwargs["retailer"] = retailer_user

            if self.customer_phone:
                customer_kwargs["phone_number"] = (
                    self.customer_phone
                )

            default_customer = Customer.objects.filter(
                **customer_kwargs
            ).first()

            if not default_customer:
                default_customer = Customer.objects.create(
                    **customer_kwargs
                )

            self.customer = default_customer

        # --------------------------------------------------------------
        # DOCUMENT NUMBER
        # --------------------------------------------------------------

        if not self.receipt_number:
            self.receipt_number = generate_document_number(
                is_credit=is_credit
            )

        # --------------------------------------------------------------
        # CREDIT DUE DATE
        # --------------------------------------------------------------

        if (
            is_credit
            and not self.due_date
            and self.sale_date
        ):
            base_date = (
                self.sale_date.date()
                if isinstance(self.sale_date, datetime)
                else self.sale_date
            )

            self.due_date = (
                base_date
                + timedelta(
                    days=self.credit_days or 30
                )
            )

        # --------------------------------------------------------------
        # FINAL VALIDATION AND SAVE
        # --------------------------------------------------------------

        self.full_clean()

        super().save(*args, **kwargs)


class SaleItem(models.Model):
    transaction = models.ForeignKey(
        SaleTransaction,
        on_delete=models.CASCADE,
        related_name="items",
    )

    item = models.ForeignKey(
        Item,
        on_delete=models.PROTECT,
        related_name="sale_items",
    )

    quantity_sold = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(MIN_QTY)]
    )

    unit_cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
    )

    unit_selling_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
    )

    discount_per_item = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )

    @property
    def total_before_discount(self):
        return Decimal(str(self.quantity_sold)) * (self.unit_selling_price or ZERO)

    @property
    def total_discount(self):
        return Decimal(str(self.quantity_sold)) * self.discount_per_item

    @property
    def total_amount(self):
        return self.total_before_discount - self.total_discount

    @property
    def total_cost(self):
        return Decimal(str(self.quantity_sold)) * (self.unit_cost_price or ZERO)

    @property
    def profit(self):
        return self.total_amount - self.total_cost

    def clean(self):
        super().clean()

        if self.quantity_sold is not None and Decimal(str(self.quantity_sold)) <= ZERO:
            raise ValidationError("Quantity sold must be greater than zero.")

        if (
            self.discount_per_item is not None
            and self.unit_selling_price is not None
            and self.discount_per_item > self.unit_selling_price
        ):
            raise ValidationError("Discount per item cannot exceed the unit selling price.")

    @db_transaction.atomic
    def save(self, *args, **kwargs):
        is_new = self.pk is None

        # 1. Fetch current WAC cost price if not set
        if (not self.unit_cost_price or self.unit_cost_price == ZERO) and self.item_id and self.transaction_id and self.transaction.branch_id:
            pricing = BranchItemPricing.objects.filter(
                branch_id=self.transaction.branch_id,
                item_id=self.item_id
            ).first()
            if pricing:
                self.unit_cost_price = pricing.unit_cost_price

        super().save(*args, **kwargs)

        # 2. Automatically record outbound StockMovement on creation
        if is_new and self.transaction.branch_id:
            StockMovement.objects.create(
                branch_id=self.transaction.branch_id,
                item=self.item,
                movement_type=StockMovement.MovementType.SALE,
                quantity=self.quantity_sold,
                unit_cost_price=self.unit_cost_price,
                date=self.transaction.sale_date.date() if hasattr(self.transaction.sale_date, 'date') else self.transaction.sale_date,
                reference=f"SALE-{self.transaction.pk}",
                reference_id=self.transaction.pk,
                notes=f"Sale Receipt #{self.transaction.receipt_number}",
                created_by=self.transaction.created_by,
            )

            # 3. Recalculate WAC & update unit costs across ledger
            recalculate_wac_sales_cost(self.transaction.branch, self.item)

        # 4. Recalculate parent transaction totals
        self.transaction.recalculate_totals()
        SaleTransaction.objects.filter(pk=self.transaction.pk).update(
            total_amount=self.transaction.total_amount
        )

    def delete(self, *args, **kwargs):
        with db_transaction.atomic():
            branch = self.transaction.branch
            item = self.item

            # Remove associated stock movement when sale item is deleted
            StockMovement.objects.filter(
                reference=f"SALE-{self.transaction.pk}",
                item=item,
                movement_type=StockMovement.MovementType.SALE
            ).delete()

            result = super().delete(*args, **kwargs)

            # Recalculate stock & WAC
            if branch and item:
                recalculate_wac_sales_cost(branch, item)

            return result

    def __str__(self):
        return f"{self.item.name} x {self.quantity_sold} (Doc: {self.transaction.receipt_number})"


class Sale(models.Model):
    PAYMENT_METHOD_CHOICES = [
        ('CASH', 'Cash'),
        ('PAYBILL', 'Paybill'),
        ('SEND_MONEY', 'Send Money'),
        ('CREDIT', 'Credit Sale'),
        ('SPLIT', 'Split Payment'),
    ]

    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name='sales'
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='sales'
    )
    transaction = models.ForeignKey(
        SaleTransaction,
        on_delete=models.CASCADE,
        related_name='legacy_sales',
        null=True,
        blank=True
    )
    
    receipt_number = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Unique receipt/invoice identifier for the transaction"
    )

    unit_cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO
    )
    unit_selling_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO
    )
    discount_per_item = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO
    )
    quantity_sold = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(MIN_QTY)]
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default='CASH'
    )
    date = models.DateField()

    customer_name = models.CharField(max_length=255, blank=True, null=True)
    customer_phone = models.CharField(max_length=50, blank=True, null=True)
    invoice_number = models.CharField(max_length=100, blank=True, null=True)
    credit_notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def unit_retail_price(self):
        return self.unit_selling_price

    @unit_retail_price.setter
    def unit_retail_price(self, value):
        self.unit_selling_price = value

    @property
    def unit_price(self):
        return self.unit_selling_price

    @unit_price.setter
    def unit_price(self, value):
        self.unit_selling_price = value

    @property
    def total_selling_price(self):
        effective_price = max(ZERO, Decimal(str(self.unit_selling_price)) - Decimal(str(self.discount_per_item)))
        return quantize_money(effective_price * Decimal(str(self.quantity_sold)))

    def __str__(self):
        return f"Sale {self.receipt_number or f'#{self.id}'} - {self.item}"


class SalePayment(models.Model):
    METHOD_CHOICES = [
        ('CASH', 'Cash'),
        ('PAYBILL', 'Paybill'),
        ('SEND_MONEY', 'Send Money'),
        ('CREDIT', 'Credit Sale'),
        ("SPLIT", "Split Payment")
    ]

    transaction = models.ForeignKey(
        SaleTransaction,
        on_delete=models.CASCADE,
        related_name='split_payments',
        null=True,
        blank=True
    )
    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name='split_payments',
        null=True,
        blank=True
    )
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference_code = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.method}: KSh {self.amount}"


# -----------------------------------------------------------------------------
# Additional Operating Models
# -----------------------------------------------------------------------------
class CreditRepayment(models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash"
        PAYBILL = "PAYBILL", "Paybill"
        SEND_MONEY = "SEND_MONEY", "Send Money"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
        SPLIT = "SPLIT", "Split Payment"

    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name="repayments",
    )

    transaction = models.ForeignKey(
        SaleTransaction,
        on_delete=models.CASCADE,
        related_name="repayments",
        null=True,
        blank=True,
        help_text="Specific invoice this repayment is applied against.",
    )

    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="repayments",
    )

    payment_method = models.CharField(
        max_length=30,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
        help_text="Payment method used for the repayment.",
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    date = models.DateField(default=date.today)
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credit_repayments_created",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]

    def clean(self):
        super().clean()

        if hasattr(self, "customer") and self.customer and hasattr(self, "branch") and self.branch:
            if self.customer.retailer_id != self.branch.retailer_id:
                raise ValidationError(
                    "The customer and branch must belong to the same retailer."
                )

        if hasattr(self, "transaction") and self.transaction and self.transaction.customer_id != self.customer_id:
            raise ValidationError(
                "The selected invoice does not belong to this customer."
            )

    def save(self, *args, **kwargs):
        if isinstance(self.date, datetime):
            self.date = self.date.date()

        self.full_clean()
        super().save(*args, **kwargs)

        if self.transaction and self.transaction.balance_due <= ZERO:
            self.transaction.is_paid = True
            self.transaction.save(update_fields=["is_paid"])

    def __str__(self):
        doc_str = f" for {self.transaction.receipt_number}" if self.transaction else ""
        return f"Repayment - {self.customer.name}: KSh {self.amount} via {self.get_payment_method_display()}{doc_str}"


# -----------------------------------------------------------------------------
# Customer Return
# -----------------------------------------------------------------------------
class CustomerReturn(models.Model):
    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="customer_returns",
    )

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="customer_returns",
    )

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(MIN_QTY)]
    )
    reason = models.CharField(max_length=255)
    date = models.DateField(default=date.today)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_returns_created",
    )

    def clean(self):
        super().clean()

        if hasattr(self, "branch") and self.branch and hasattr(self, "item") and self.item:
            if (
                self.item.retailer_id is None
                and self.branch.retailer_id is not None
            ):
                self.item.retailer = self.branch.retailer
                self.item.save(update_fields=["retailer"])

            elif self.branch.retailer_id != self.item.retailer_id:
                raise ValidationError(
                    "The branch and item must belong to the same retailer."
                )

    @db_transaction.atomic
    def save(self, *args, **kwargs):
        if isinstance(self.date, datetime):
            self.date = self.date.date()

        self.full_clean()

        is_new = self.pk is None

        pricing = (
            BranchItemPricing.objects
            .select_for_update()
            .filter(
                branch=self.branch,
                item=self.item,
            )
            .first()
        )

        if not pricing:
            raise ValidationError(
                f"Item '{self.item.name}' is not configured for branch '{self.branch.name}'."
            )

        unit_cost = BranchItemPricing.calculate_wac(
            self.branch,
            self.item,
        )

        if is_new:
            super().save(*args, **kwargs)

            StockMovement.objects.create(
                branch=self.branch,
                item=self.item,
                movement_type=StockMovement.MovementType.CUSTOMER_RETURN,
                quantity=self.quantity,
                unit_cost_price=unit_cost,
                date=self.date,
                reference=f"RETURN-IN-{self.pk}",
                reference_id=self.pk,
                notes=self.reason,
                created_by=self.created_by,
            )

        else:
            super().save(*args, **kwargs)

            movement = StockMovement.objects.filter(
                reference=f"RETURN-IN-{self.pk}"
            ).order_by('-id').first()

            if movement:
                movement.quantity = self.quantity
                movement.unit_cost_price = unit_cost
                movement.date = self.date
                movement.branch = self.branch
                movement.item = self.item
                movement.notes = self.reason
                movement.save()

        recalculate_wac_sales_cost(self.branch, self.item)

    def delete(self, *args, **kwargs):
        with db_transaction.atomic():
            branch = self.branch
            item = self.item

            StockMovement.objects.filter(
                reference=f"RETURN-IN-{self.pk}"
            ).delete()

            result = super().delete(*args, **kwargs)

            recalculate_wac_sales_cost(branch, item)

            return result

    def __str__(self):
        return f"{self.item.name} ({self.branch.name}) - Customer Return ({self.quantity})"


# -----------------------------------------------------------------------------
# Supplier Return
# -----------------------------------------------------------------------------
class SupplierReturn(models.Model):
    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="supplier_returns",
    )

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="supplier_returns",
    )

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(MIN_QTY)]
    )
    reason = models.CharField(max_length=255)
    supplier_name = models.CharField(max_length=200, blank=True)
    date = models.DateField(default=date.today)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplier_returns_created",
    )

    def clean(self):
        super().clean()

        if hasattr(self, "branch") and self.branch and hasattr(self, "item") and self.item:
            if (
                self.item.retailer_id is None
                and self.branch.retailer_id is not None
            ):
                self.item.retailer = self.branch.retailer
                self.item.save(update_fields=["retailer"])

            elif self.branch.retailer_id != self.item.retailer_id:
                raise ValidationError(
                    "The branch and item must belong to the same retailer."
                )

    @db_transaction.atomic
    def save(self, *args, **kwargs):
        if isinstance(self.date, datetime):
            self.date = self.date.date()

        self.full_clean()

        is_new = self.pk is None

        unit_cost = BranchItemPricing.calculate_wac(
            self.branch,
            self.item,
        )

        if is_new:
            super().save(*args, **kwargs)

            StockMovement.objects.create(
                branch=self.branch,
                item=self.item,
                movement_type=StockMovement.MovementType.SUPPLIER_RETURN,
                quantity=self.quantity,
                unit_cost_price=unit_cost,
                date=self.date,
                reference=f"RETURN-OUT-{self.pk}",
                reference_id=self.pk,
                notes=self.reason,
                created_by=self.created_by,
            )

        else:
            super().save(*args, **kwargs)

            movement = StockMovement.objects.filter(
                reference=f"RETURN-OUT-{self.pk}"
            ).first()

            if movement:
                movement.quantity = self.quantity
                movement.unit_cost_price = unit_cost
                movement.date = self.date
                movement.branch = self.branch
                movement.item = self.item
                movement.notes = self.reason
                movement.save()

        recalculate_wac_sales_cost(self.branch, self.item)

    def delete(self, *args, **kwargs):
        with db_transaction.atomic():
            branch = self.branch
            item = self.item

            StockMovement.objects.filter(
                reference=f"RETURN-OUT-{self.pk}"
            ).delete()

            result = super().delete(*args, **kwargs)

            recalculate_wac_sales_cost(branch, item)

            return result

    def __str__(self):
        return f"{self.item.name} ({self.branch.name}) - Supplier Return ({self.quantity})"


# -----------------------------------------------------------------------------
# Damage Record
# -----------------------------------------------------------------------------
class DamageRecord(models.Model):
    class DamageType(models.TextChoices):
        DAMAGED = "DAMAGED", "Damaged"
        EXPIRED = "EXPIRED", "Expired"
        LOST = "LOST", "Lost"
        OTHER = "OTHER", "Other"

    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="damage_records",
    )

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="damage_records",
    )

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(MIN_QTY)]
    )

    damage_type = models.CharField(
        max_length=20,
        choices=DamageType.choices,
        default=DamageType.DAMAGED,
    )

    reason = models.CharField(max_length=255)
    date = models.DateField(default=date.today)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="damage_records_created",
    )

    def clean(self):
        super().clean()

        if hasattr(self, "branch") and self.branch and hasattr(self, "item") and self.item:
            if (
                self.item.retailer_id is None
                and self.branch.retailer_id is not None
            ):
                self.item.retailer = self.branch.retailer
                self.item.save(update_fields=["retailer"])

            elif self.branch.retailer_id != self.item.retailer_id:
                raise ValidationError(
                    "The branch and item must belong to the same retailer."
                )

    @db_transaction.atomic
    def save(self, *args, **kwargs):
        if isinstance(self.date, datetime):
            self.date = self.date.date()

        self.full_clean()

        is_new = self.pk is None

        unit_cost = BranchItemPricing.calculate_wac(
            self.branch,
            self.item,
        )

        if is_new:
            super().save(*args, **kwargs)

            StockMovement.objects.create(
                branch=self.branch,
                item=self.item,
                movement_type=StockMovement.MovementType.DAMAGE,
                quantity=self.quantity,
                unit_cost_price=unit_cost,
                date=self.date,
                reference=f"DAMAGE-{self.pk}",
                reference_id=self.pk,
                notes=f"{self.get_damage_type_display()}: {self.reason}",
                created_by=self.created_by,
            )

        else:
            super().save(*args, **kwargs)

            movement = StockMovement.objects.filter(
                reference=f"DAMAGE-{self.pk}"
            ).first()

            if movement:
                movement.quantity = self.quantity
                movement.unit_cost_price = unit_cost
                movement.date = self.date
                movement.branch = self.branch
                movement.item = self.item
                movement.notes = f"{self.get_damage_type_display()}: {self.reason}"
                movement.save()

        recalculate_wac_sales_cost(self.branch, self.item)

    def delete(self, *args, **kwargs):
        with db_transaction.atomic():
            branch = self.branch
            item = self.item

            StockMovement.objects.filter(
                reference=f"DAMAGE-{self.pk}"
            ).delete()

            result = super().delete(*args, **kwargs)

            recalculate_wac_sales_cost(branch, item)

            return result

    def __str__(self):
        return f"{self.item.name} ({self.branch.name}) - {self.get_damage_type_display()} ({self.quantity})"


# -----------------------------------------------------------------------------
# Stock Adjustment
# -----------------------------------------------------------------------------



# -----------------------------------------------------------------------------
# Expense
# -----------------------------------------------------------------------------
class Expense(models.Model):
    retailer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="expenses",
        null=True,
        blank=True,
    )

    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses",
    )

    date = models.DateField(default=date.today)
    description = models.CharField(max_length=255)
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(ZERO)],
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()

        if hasattr(self, "branch") and self.branch and hasattr(self, "retailer") and self.retailer:
            if self.branch.retailer_id != self.retailer_id:
                raise ValidationError(
                    "The branch must belong to the selected retailer."
                )

    def save(self, *args, **kwargs):
        if isinstance(self.date, datetime):
            self.date = self.date.date()

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        branch_name = self.branch.name if self.branch else "General"
        return f"{self.description} - KSh {self.amount} ({branch_name})"


class PendingApproval(models.Model):
    class RequestType(models.TextChoices):
        SALE_ENTRY = "SALE_ENTRY", "Sale Entry"
        BRANCH_CREATION = "BRANCH_CREATION", "Branch & Items"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending Approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_type = models.CharField(max_length=30, choices=RequestType.choices)
    payload = models.JSONField(help_text="Stores the submitted payload until approved")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name="submitted_approvals"
    )
    status = models.CharField(
        max_length=20, 
        choices=Status.choices, 
        default=Status.PENDING
    )
    rejection_reason = models.TextField(blank=True, null=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        null=True, 
        blank=True, 
        on_delete=models.SET_NULL, 
        related_name="reviewed_approvals"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_request_type_display()} - {self.status} ({self.id})"

# -----------------------------------------------------------------------------
# Daily Financial Report
# -----------------------------------------------------------------------------
class DailyFinancialReport(models.Model):
    retailer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="financial_reports",
        null=True,
        blank=True,
    )

    branch = models.ForeignKey(
        RetailBranch,
        on_delete=models.CASCADE,
        related_name="financial_reports",
        null=True,
        blank=True,
    )

    date = models.DateField(default=date.today)
    tax_rate_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "retailer",
                    "branch",
                    "date",
                ],
                name="unique_daily_report_per_branch",
            )
        ]

    def clean(self):
        super().clean()

        if hasattr(self, "branch") and self.branch and hasattr(self, "retailer") and self.retailer:
            if self.branch.retailer_id != self.retailer_id:
                raise ValidationError(
                    "The branch must belong to the selected retailer."
                )

    def save(self, *args, **kwargs):
        if isinstance(self.date, datetime):
            self.date = self.date.date()

        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def total_sales_revenue(self):
        sales = SaleTransaction.objects.filter(sale_date__date=self.date)

        if self.branch:
            sales = sales.filter(branch=self.branch)
        elif self.retailer:
            sales = sales.filter(branch__retailer=self.retailer)

        return sum((sale.total_amount for sale in sales), ZERO)

    @property
    def total_cogs(self):
        sales = SaleTransaction.objects.filter(sale_date__date=self.date)

        if self.branch:
            sales = sales.filter(branch=self.branch)
        elif self.retailer:
            sales = sales.filter(branch__retailer=self.retailer)

        return sum((sale.total_cost for sale in sales), ZERO)

    @property
    def total_expenses(self):
        expenses = Expense.objects.filter(date=self.date)

        if self.branch:
            expenses = expenses.filter(branch=self.branch)
        elif self.retailer:
            expenses = expenses.filter(retailer=self.retailer)

        return (
            expenses.aggregate(
                total=Coalesce(Sum("amount"), ZERO)
            )["total"]
            or ZERO
        )

    @property
    def gross_profit(self):
        return self.total_sales_revenue - self.total_cogs

    @property
    def net_profit(self):
        tax = self.gross_profit * (
            self.tax_rate_percentage / Decimal("100.00")
        )
        return self.gross_profit - self.total_expenses - tax

    def __str__(self):
        branch_name = self.branch.name if self.branch else "All Branches"
        return f"Report {self.date} ({branch_name})"


# =============================================================================
# WAC RECALCULATION
# =============================================================================
@db_transaction.atomic
def recalculate_wac_sales_cost(branch, item):
    pricing = (
        BranchItemPricing.objects
        .select_for_update()
        .filter(
            branch=branch,
            item=item,
        )
        .first()
    )

    if not pricing:
        return ZERO

    opening_qty = Decimal(str(pricing.opening_stock or ZERO))

    opening_cost = (
        pricing.opening_stock_unit_cost_price
        if pricing.opening_stock_unit_cost_price is not None
        else (pricing.unit_cost_price or ZERO)
    )

    opening_cost = Decimal(str(opening_cost))
    if opening_cost < ZERO:
        opening_cost = ZERO

    if opening_qty > ZERO and opening_cost <= ZERO:
        fallback_cost = Decimal(str(pricing.unit_cost_price or ZERO))
        if fallback_cost > ZERO:
            opening_cost = fallback_cost

    total_qty = opening_qty
    total_value = opening_qty * opening_cost
    last_known_cost = opening_cost

    inbound_types = {
        StockMovement.MovementType.OPENING_STOCK,
        StockMovement.MovementType.PURCHASE,
        StockMovement.MovementType.CUSTOMER_RETURN,
        StockMovement.MovementType.TRANSFER_IN,
        StockMovement.MovementType.ADJUSTMENT_IN,
    }

    outbound_types = {
        StockMovement.MovementType.SALE,
        StockMovement.MovementType.SUPPLIER_RETURN,
        StockMovement.MovementType.TRANSFER_OUT,
        StockMovement.MovementType.DAMAGE,
        StockMovement.MovementType.ADJUSTMENT_OUT,
    }

    # Exclude OPENING_STOCK movement records if initial pricing.opening_stock is already set
    # to avoid double-counting opening inventory.
    movements_qs = StockMovement.objects.filter(
        branch=branch,
        item=item,
    )
    if opening_qty > ZERO:
        movements_qs = movements_qs.exclude(
            movement_type=StockMovement.MovementType.OPENING_STOCK
        )

    movements = movements_qs.order_by("created_at", "id")

    sales_to_update = {}

    for movement in movements:
        try:
            quantity = Decimal(str(movement.quantity or ZERO))
        except (TypeError, ValueError, ArithmeticError):
            quantity = ZERO

        if quantity <= ZERO:
            continue

        if movement.movement_type in inbound_types:
            movement_cost = movement.unit_cost_price or ZERO
            movement_cost = Decimal(str(movement_cost))
            if movement_cost <= ZERO:
                movement_cost = last_known_cost

            if total_qty <= ZERO:
                total_qty = quantity
                total_value = quantity * movement_cost
                last_known_cost = movement_cost
            else:
                total_value += quantity * movement_cost
                total_qty += quantity
                last_known_cost = total_value / total_qty

        elif movement.movement_type in outbound_types:
            if total_qty > ZERO:
                current_wac = total_value / total_qty
            else:
                current_wac = last_known_cost

            current_wac_quantized = quantize_money(current_wac)

            if movement.movement_type == StockMovement.MovementType.SALE:
                if movement.unit_cost_price != current_wac_quantized:
                    StockMovement.objects.filter(pk=movement.pk).update(
                        unit_cost_price=current_wac_quantized
                    )
                if movement.reference_id:
                    sales_to_update[movement.reference_id] = current_wac_quantized

            total_value -= quantity * current_wac
            total_qty -= quantity

            if total_qty <= ZERO:
                total_qty = ZERO
                total_value = ZERO
            else:
                total_value = max(ZERO, total_value)
                last_known_cost = total_value / total_qty

    if total_qty > ZERO:
        final_wac = total_value / total_qty
    else:
        final_wac = last_known_cost

    final_wac = quantize_money(final_wac)

    # Safely update ONLY unit_cost_price without touching opening_stock
    BranchItemPricing.objects.filter(pk=pricing.pk).update(unit_cost_price=final_wac)

    for sale_id, new_unit_cost in sales_to_update.items():
        SaleItem.objects.filter(transaction_id=sale_id, item=item).update(
            unit_cost_price=new_unit_cost
        )
        Sale.objects.filter(transaction_id=sale_id, item=item).update(
            unit_cost_price=new_unit_cost
        )

    return final_wac

# Add these property methods inside DailyFinancialReport model in models.py:

@property
def total_cash_inflows(self):
    """Calculates all actual cash received on this report's date."""
    # 1. Direct and Split Cash Sales
    cash_sales = SalePayment.objects.filter(
        method='CASH',
        transaction__sale_date__date=self.date
    )
    if self.branch:
        cash_sales = cash_sales.filter(transaction__branch=self.branch)
    elif self.retailer:
        cash_sales = cash_sales.filter(transaction__branch__retailer=self.retailer)
    
    cash_sales_amount = cash_sales.aggregate(total=Coalesce(Sum('amount'), ZERO))['total']

    # 2. Customer Credit Repayments
    repayments = CreditRepayment.objects.filter(date=self.date)
    if self.branch:
        repayments = repayments.filter(branch=self.branch)
    elif self.retailer:
        repayments = repayments.filter(customer__retailer=self.retailer)

    repayments_amount = repayments.aggregate(total=Coalesce(Sum('amount'), ZERO))['total']

    return quantize_money(cash_sales_amount + repayments_amount)

@property
def total_cash_outflows(self):
    """Calculates all cash paid out on this report's date."""
    # 1. Expenses
    expenses_amount = self.total_expenses

    # 2. Purchases
    purchases = Purchase.objects.filter(purchase_date__date=self.date)
    if self.branch:
        purchases = purchases.filter(branch=self.branch)
    elif self.retailer:
        purchases = purchases.filter(branch__retailer=self.retailer)

    purchases_amount = sum((p.total_cost for p in purchases), ZERO)

    return quantize_money(expenses_amount + purchases_amount)

@property
def net_cash_flow(self):
    return quantize_money(self.total_cash_inflows - self.total_cash_outflows)