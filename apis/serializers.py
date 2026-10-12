from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from rest_framework import serializers

from analytics.services import AnalyticsService

from stocks.models import (
    BranchItemPricing,
    CreditRepayment,
    Customer,
    CustomerReturn,
    DailyFinancialReport,
    DamageRecord,
    Expense,
    RetailBranch,
    generate_receipt_number,
    Item,
    SaleItem,
    Purchase,
    RetailBranch,
    SalePayment,
    SaleTransaction,
    StockBatch,
    StockMovement,
    StockTransfer,
    Supplier,
    SupplierReturn,
    recalculate_wac_sales_cost,
)

from django.contrib.auth import get_user_model
User = get_user_model()
# ============================================================
# CONSTANTS
# ============================================================

ZERO = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")

# Standardized internal payment methods
VALID_PAYMENT_METHODS = [
    "CASH",
    "PAYBILL",
    "SEND_MONEY",
    "CREDIT",
]

CHECKOUT_PAYMENT_METHODS = [
    "CASH",
    "PAYBILL",
    "SEND_MONEY",
    "CREDIT",
    "SPLIT",
]

# Map incoming frontend payment method aliases to internal keys
PAYMENT_METHOD_ALIASES = {
    "M-PESA": "PAYBILL",
    "MPESA": "PAYBILL",
    "PAYBILL": "PAYBILL",
    "SEND_MONEY": "SEND_MONEY",
    "CASH": "CASH",
    "CREDIT": "CREDIT",
    "ON_CREDIT": "CREDIT",
    "SPLIT": "SPLIT",
}


# ============================================================
# COMMON ERROR HELPERS
# ============================================================

def _normalize_payment_method(method):
    if not method:
        return "CASH"
    clean_method = str(method).strip().upper()
    return PAYMENT_METHOD_ALIASES.get(clean_method, clean_method)


def _validation_error(error):
    if isinstance(error, DjangoValidationError):
        if hasattr(error, "message_dict"):
            return serializers.ValidationError(error.message_dict)
        if hasattr(error, "messages"):
            return serializers.ValidationError({"detail": error.messages})
        return serializers.ValidationError({"detail": str(error)})
    if isinstance(error, serializers.ValidationError):
        return error
    return serializers.ValidationError({"detail": str(error)})


def _integrity_error(error):
    message = str(error).lower()
    if "unique" in message or "duplicate" in message:
        return serializers.ValidationError(
            {"detail": "The value you entered already exists. Please use a different value."}
        )
    if "foreign key" in message:
        return serializers.ValidationError(
            {"detail": "One of the selected records is invalid or no longer exists."}
        )
    if "not-null" in message or "null value" in message:
        return serializers.ValidationError({"detail": "A required field cannot be empty."})
    return serializers.ValidationError(
        {"detail": "The operation could not be completed because of a database constraint."}
    )


def generate_branch_receipt_no(branch, dt=None):
    """Generates a structured, unique receipt number per branch and timestamp."""
    dt = dt or timezone.now()
    branch_id = getattr(branch, "id", branch) or "0"
    timestamp_str = dt.strftime("%Y%m%d%H%M%S")
    microsec = dt.strftime("%f")[:3]
    return f"RCP-B{branch_id}-{timestamp_str}{microsec}"


# ============================================================
# DATE / ID / DECIMAL HELPERS
# ============================================================

def _coerce_to_datetime(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    if isinstance(value, date):
        return timezone.make_aware(datetime.combine(value, datetime.min.time()))
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if timezone.is_aware(dt) else timezone.make_aware(dt)
        except ValueError:
            pass
        try:
            parsed_date = datetime.strptime(value.split("T")[0], "%Y-%m-%d").date()
            return timezone.make_aware(datetime.combine(parsed_date, datetime.min.time()))
        except ValueError:
            pass
    return value


def _coerce_to_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return timezone.localtime(parsed).date() if timezone.is_aware(parsed) else parsed.date()
        except ValueError:
            pass
        try:
            return datetime.strptime(value.split("T")[0], "%Y-%m-%d").date()
        except ValueError:
            pass
    return value


def _extract_id(value):
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value.get("id") or value.get("pk") or value.get("branch_id")
    if hasattr(value, "id"):
        return value.id
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit():
            return int(value)
        return value
    if isinstance(value, int):
        return value
    return value


def _resolve_customer(customer_input, retailer=None):
    """Resolves customer ID, string name, or customer_id into a Customer model object."""
    if not customer_input:
        return None
    if isinstance(customer_input, Customer):
        return customer_input

    extracted = _extract_id(customer_input)
    if not extracted:
        return None

    if isinstance(extracted, int):
        return Customer.objects.filter(pk=extracted).first()

    if isinstance(extracted, str):
        qs = Customer.objects.all()
        if retailer:
            qs = qs.filter(retailer=retailer)
        
        c = qs.filter(customer_id=extracted).first()
        if c:
            return c

        c = qs.filter(name__iexact=extracted).first()
        if c:
            return c

        if retailer:
            c, _ = Customer.objects.get_or_create(
                name=extracted,
                retailer=retailer,
                defaults={"phone_number": ""}
            )
            return c

    return None


def _safe_decimal(value, default=ZERO):
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _money(value):
    return _safe_decimal(value).quantize(MONEY_QUANT)


# ============================================================
# PRICING HELPERS
# ============================================================

def _get_pricing(item, branch):
    if not item or not branch:
        return None
    return (
        BranchItemPricing.objects
        .filter(item=item, branch=branch)
        .select_related("branch", "item")
        .first()
    )


def _get_current_wac(item, branch):
    if not item or not branch:
        return ZERO
    try:
        wac = BranchItemPricing.calculate_wac(branch, item)
        return _money(wac)
    except Exception:
        pricing = _get_pricing(item, branch)
        if pricing:
            return _money(pricing.unit_cost_price)
        return ZERO


# ============================================================
# REQUEST / OWNERSHIP HELPERS
# ============================================================

def _get_request_user(serializer):
    if not serializer or not hasattr(serializer, "context"):
        return None
    request = serializer.context.get("request")
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        return request.user
    return None


def _validate_item_ownership(item, user):
    if not item or not user:
        return
    retailer_id = getattr(item, "retailer_id", None)
    if retailer_id is not None and retailer_id != user.id:
        raise serializers.ValidationError({"detail": "You do not have permission to access this item."})


def _validate_branch_ownership(branch, user):
    if not branch or not user:
        return
    retailer_id = getattr(branch, "retailer_id", None)
    if retailer_id is not None and retailer_id != user.id:
        raise serializers.ValidationError({"detail": "You do not have permission to access this branch."})


# ============================================================
# RETAIL BRANCH
# ============================================================

# ============================================================
# USER & AUTHENTICATION
# ============================================================

class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=6)
    confirm_password = serializers.CharField(write_only=True)
    branch = serializers.PrimaryKeyRelatedField(
        queryset=RetailBranch.objects.all(),
        required=False,
        allow_null=True,
        write_only=True
    )

    class Meta:
        model = User
        fields = ("id", "username", "email", "password", "confirm_password", "branch")

    def validate(self, attrs):
        if attrs.get("password") != attrs.get("confirm_password"):
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs

    def create(self, validated_data):
        validated_data.pop("confirm_password", None)
        branch = validated_data.pop("branch", None)

        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data.get("email", ""),
            password=validated_data["password"],
        )

        # Connect branch to retailer user
        if branch:
            branch.retailer = user
            branch.save(update_fields=["retailer"])

        return user


class UserSerializer(serializers.ModelSerializer):
    branch = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "username", "email", "is_staff", "branch", "branch_name")

    def get_first_branch(self, obj):
        # Uses related_name="branches" defined on RetailBranch.retailer
        return obj.branches.first()

    def get_branch(self, obj):
        branch = self.get_first_branch(obj)
        return branch.id if branch else None

    def get_branch_name(self, obj):
        branch = self.get_first_branch(obj)
        return branch.name if branch else None



from rest_framework import serializers
from stocks.models import PendingApproval  # Import model from stocks app

class PendingApprovalSerializer(serializers.ModelSerializer):
    submitted_by_name = serializers.ReadOnlyField(source="submitted_by.username")
    reviewed_by_name = serializers.ReadOnlyField(source="reviewed_by.username")
    request_type_display = serializers.ReadOnlyField(source="get_request_type_display")

    class Meta:
        model = PendingApproval
        fields = [
            "id",
            "request_type",
            "request_type_display",
            "payload",
            "status",
            "rejection_reason",
            "submitted_by",
            "submitted_by_name",
            "reviewed_by",
            "reviewed_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "submitted_by",
            "reviewed_by",
            "rejection_reason",
            "created_at",
            "updated_at",
        ]


class RejectionReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, allow_blank=False, max_length=500)


from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from rest_framework import serializers

User = get_user_model()


class RetailBranchSerializer(serializers.ModelSerializer):
    created_at = serializers.DateTimeField(read_only=True, required=False)

    class Meta:
        model = RetailBranch
        fields = "__all__"
        read_only_fields = ["retailer"]

    def create(self, validated_data):
        # 1. Try custom helper function
        user = _get_request_user(self)

        # 2. Fall back to standard request context user if authenticated
        if not user:
            request = self.context.get("request")
            if request and hasattr(request, "user") and request.user.is_authenticated:
                user = request.user

        # 3. Fall back to default admin/system user if unauthenticated
        if not user:
            user = User.objects.filter(is_superuser=True).first() or User.objects.first()

        # 4. If still no user exists in DB, raise a readable DRF validation error
        if not user:
            raise serializers.ValidationError(
                {"retailer": "A valid retailer user is required to create a branch."}
            )

        if "retailer" not in validated_data:
            validated_data["retailer"] = user

        try:
            return super().create(validated_data)
        except DjangoValidationError as e:
            raise _validation_error(e)
        except IntegrityError as e:
            raise _integrity_error(e)


# ============================================================
# CUSTOMER
# ============================================================

class CustomerSerializer(serializers.ModelSerializer):
    total_credit_sales = serializers.SerializerMethodField()
    total_repayments = serializers.SerializerMethodField()
    outstanding_balance = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            "id",
            "customer_id",
            "name",
            "phone_number",
            "created_at",
            "total_credit_sales",
            "total_repayments",
            "outstanding_balance",
        ]
        read_only_fields = ["retailer", "customer_id"]

    def create(self, validated_data):
        user = _get_request_user(self)
        if user and "retailer" not in validated_data:
            validated_data["retailer"] = user
        try:
            return super().create(validated_data)
        except DjangoValidationError as e:
            raise _validation_error(e)
        except IntegrityError as e:
            raise _integrity_error(e)

    def get_total_credit_sales(self, obj):
        # 1. Total from pure CREDIT transactions
        pure_credit_sales = getattr(obj, "sales_transactions", obj.sales).filter(
            payment_method=SaleTransaction.PaymentMethod.CREDIT
        )

        pure_credit_total = sum(
            (
                getattr(sale, "total_amount", None)
                or (
                    Decimal(str(sale.quantity_sold or 0))
                    * Decimal(
                        str(
                            max(
                                ZERO,
                                (
                                    getattr(
                                        sale,
                                        "unit_selling_price",
                                        getattr(sale, "unit_retail_price", ZERO),
                                    )
                                    or ZERO
                                )
                                - (sale.discount_per_item or ZERO),
                            )
                        )
                    )
                )
            )
            for sale in pure_credit_sales
        ) if pure_credit_sales else ZERO

        # 2. Total from CREDIT portions of SPLIT payment transactions
        split_credit_total = SalePayment.objects.filter(
            transaction__customer=obj,
            method="CREDIT",
        ).aggregate(
            total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField())
        )["total"]

        total = pure_credit_total + split_credit_total
        return str(_money(total))

    def get_total_repayments(self, obj):
        total = obj.repayments.aggregate(
            total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField())
        )["total"]
        return str(_money(total or ZERO))

    def get_outstanding_balance(self, obj):
        balance = Decimal(self.get_total_credit_sales(obj)) - Decimal(self.get_total_repayments(obj))
        return str(_money(max(ZERO, balance)))


# ============================================================
# CREDIT REPAYMENT
# ============================================================

class CreditRepaymentSerializer(serializers.ModelSerializer):
    customer = serializers.PrimaryKeyRelatedField(
        queryset=Customer.objects.all(), required=False, allow_null=True
    )
    branch = serializers.PrimaryKeyRelatedField(
        queryset=RetailBranch.objects.all(), required=False, allow_null=True
    )
    transaction = serializers.PrimaryKeyRelatedField(
        queryset=SaleTransaction.objects.all(), required=False, allow_null=True
    )

    customer_detail = CustomerSerializer(source="customer", read_only=True)
    branch_detail = RetailBranchSerializer(source="branch", read_only=True)
    date = serializers.DateField(required=False, allow_null=True)
    payment_method_display = serializers.CharField(
        source="get_payment_method_display", read_only=True
    )

    class Meta:
        model = CreditRepayment
        fields = "__all__"
        read_only_fields = ["created_by"]

    def validate_date(self, value):
        return _coerce_to_date(value)

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)

        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])
        if "customer" in data:
            data["customer"] = _extract_id(data["customer"])
        if "transaction" in data:
            data["transaction"] = _extract_id(data["transaction"])

        # Handle frontend sending transaction_id or receipt_number
        if "transaction" not in data or not data["transaction"]:
            if "transaction_id" in data and data["transaction_id"]:
                data["transaction"] = _extract_id(data["transaction_id"])
            elif "receipt_number" in data and data["receipt_number"]:
                tx = SaleTransaction.objects.filter(
                    receipt_number=data["receipt_number"]
                ).first()
                if tx:
                    data["transaction"] = tx.pk

        # Extract and format payment_method from payload variations
        raw_method = (
            data.get("payment_method")
            or data.get("method")
            or data.get("payment_type")
        )
        if raw_method:
            data["payment_method"] = (
                str(raw_method)
                .strip()
                .upper()
                .replace(" ", "_")
                .replace("-", "_")
            )

        if data.get("date") not in (None, ""):
            data["date"] = _coerce_to_date(data["date"])
        elif data.get("transaction_date") not in (None, ""):
            data["date"] = _coerce_to_date(data["transaction_date"])

        return super().to_internal_value(data)

    def validate(self, attrs):
        transaction = attrs.get("transaction")

        # 1. Ensure payment_method has a default if omitted
        if not attrs.get("payment_method"):
            attrs["payment_method"] = "CASH"

        # 2. Infer customer and branch from transaction
        if transaction:
            if not attrs.get("customer") and getattr(transaction, "customer", None):
                attrs["customer"] = transaction.customer
            if not attrs.get("branch") and getattr(transaction, "branch", None):
                attrs["branch"] = transaction.branch

        # 3. Fallback: If customer is still missing, attach/create a walk-in/default customer
        if not attrs.get("customer"):
            branch = attrs.get("branch") or (transaction.branch if transaction else None)
            retailer = getattr(branch, "retailer", None) if branch else None

            # Attempt to find or create a default customer for this retailer
            if retailer:
                cust_name = (
                    transaction.customer_name if transaction and transaction.customer_name
                    else f"Walk-in Customer ({branch.name if branch else 'Branch'})"
                )
                customer, _ = Customer.objects.get_or_create(
                    retailer=retailer,
                    name=cust_name,
                    defaults={"phone_number": getattr(transaction, "customer_phone", None)}
                )
                attrs["customer"] = customer

                # Link back to transaction for future requests
                if transaction and not transaction.customer:
                    transaction.customer = customer
                    transaction.save(update_fields=["customer"])

        # 4. Final validation guard
        if not attrs.get("customer"):
            raise serializers.ValidationError(
                {"customer": "Customer ID is required."}
            )

        return attrs

    def create(self, validated_data):
        user = _get_request_user(self)
        if user and "created_by" not in validated_data:
            validated_data["created_by"] = user
        if validated_data.get("date") is None:
            validated_data["date"] = timezone.localdate()

        try:
            return super().create(validated_data)
        except DjangoValidationError as e:
            raise _validation_error(e)
        except IntegrityError as e:
            raise _integrity_error(e)

# ============================================================
# ITEM
# ============================================================

class ItemSerializer(serializers.ModelSerializer):
    # Writable input fields for creation and updates
    unit_cost_price_input = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, write_only=True
    )
    unit_retail_price_input = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, write_only=True
    )
    opening_stock_input = serializers.IntegerField(
        required=False, write_only=True, min_value=0
    )

    # Computed representation fields (Read-Only output)
    unit_cost_price = serializers.SerializerMethodField()
    weighted_unit_cost = serializers.SerializerMethodField()
    latest_unit_cost = serializers.SerializerMethodField()
    unit_retail_price = serializers.SerializerMethodField()

    opening_stock = serializers.SerializerMethodField()
    remaining_stock = serializers.SerializerMethodField()
    total_stock_sold = serializers.SerializerMethodField()
    restocks = serializers.SerializerMethodField()
    remaining_stock_value = serializers.SerializerMethodField()
    is_low_stock = serializers.SerializerMethodField()

    branch = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Item
        fields = [
            "id",
            "retailer",
            "name",
            "code",
            "sku",
            "branch",
            "unit_cost_price",
            "weighted_unit_cost",
            "latest_unit_cost",
            "unit_retail_price",
            "opening_stock",
            "remaining_stock",
            "total_stock_sold",
            "restocks",
            "remaining_stock_value",
            "is_low_stock",
            "unit_cost_price_input",
            "unit_retail_price_input",
            "opening_stock_input",
            "created_at",
            "updated_at",
        ]

    def _get_branch_id(self):
        context = self.context or {}
        request = context.get("request")
        return context.get("branch") or (
            request.query_params.get("branch") if request else None
        )

    def _get_pricing_obj(self, obj):
        context = self.context or {}
        if "_pricing_cache" not in context:
            context["_pricing_cache"] = {}

        branch_id = self._get_branch_id()
        cache_key = (obj.id, branch_id)

        if cache_key not in context["_pricing_cache"]:
            pricing = None
            if branch_id:
                pricing = obj.branch_pricings.filter(branch_id=branch_id).first()
            if not pricing:
                pricing = obj.branch_pricings.first()

            context["_pricing_cache"][cache_key] = pricing

        return context["_pricing_cache"][cache_key]

    def _get_metrics(self, obj):
        context = self.context or {}
        if "_item_metrics_cache" not in context:
            context["_item_metrics_cache"] = {}

        branch_id = self._get_branch_id()
        cache_key = (obj.id, branch_id)

        if cache_key not in context["_item_metrics_cache"]:
            pricing = self._get_pricing_obj(obj)

            if pricing:
                remaining_stock = float(pricing.remaining_stock)
                remaining_stock_value = pricing.remaining_stock_value
                opening_stock = float(pricing.get_effective_opening_stock())
            else:
                remaining_stock = 0.0
                remaining_stock_value = ZERO
                opening_stock = 0.0

            context["_item_metrics_cache"][cache_key] = {
                "opening_stock": opening_stock,
                "remaining_stock": remaining_stock,
                "remaining_stock_value": remaining_stock_value,
            }

        return context["_item_metrics_cache"][cache_key]

    # Field Getters
    def get_unit_cost_price(self, obj):
        pricing = self._get_pricing_obj(obj)
        val = pricing.unit_cost_price if pricing else ZERO
        return str(_money(val))

    def get_weighted_unit_cost(self, obj):
        pricing = self._get_pricing_obj(obj)
        val = pricing.unit_cost_price if pricing else ZERO
        return str(_money(val))

    def get_latest_unit_cost(self, obj):
        pricing = self._get_pricing_obj(obj)
        val = pricing.unit_cost_price if pricing else ZERO
        return str(_money(val))

    def get_unit_retail_price(self, obj):
        pricing = self._get_pricing_obj(obj)
        val = pricing.unit_selling_price if pricing else ZERO
        return str(_money(val))

    def get_opening_stock(self, obj):
        metrics = self._get_metrics(obj)
        return metrics.get("opening_stock", 0.0)

    def get_remaining_stock(self, obj):
        metrics = self._get_metrics(obj)
        return metrics.get("remaining_stock", 0.0)

    def get_total_stock_sold(self, obj):
        branch_id = self._get_branch_id()
        pricing = self._get_pricing_obj(obj)

        if pricing:
            return float(pricing.total_stock_sold)

        movements_qs = StockMovement.objects.filter(
            item=obj,
            movement_type=StockMovement.MovementType.SALE,
        )
        if branch_id:
            movements_qs = movements_qs.filter(branch_id=branch_id)

        total_qty = movements_qs.aggregate(
            total=Coalesce(Sum("quantity"), ZERO, output_field=DecimalField())
        )["total"]

        return float(total_qty)

    def get_restocks(self, obj):
        branch_id = self._get_branch_id()

        # Check all inbound stock movement types (Purchases, Adjustments In, Transfers In)
        inbound_types = [
            StockMovement.MovementType.PURCHASE,
            StockMovement.MovementType.ADJUSTMENT_IN,
            StockMovement.MovementType.TRANSFER_IN,
        ]

        movements_qs = StockMovement.objects.filter(
            item=obj,
            movement_type__in=inbound_types,
        )
        if branch_id:
            movements_qs = movements_qs.filter(branch_id=branch_id)

        total_movement_restocked = movements_qs.aggregate(
            total=Coalesce(Sum("quantity"), ZERO, output_field=DecimalField())
        )["total"]

        if float(total_movement_restocked) > 0:
            return float(total_movement_restocked)

        # Fallback: check StockBatch initial received quantities
        batches_qs = StockBatch.objects.filter(item=obj)
        if branch_id:
            batches_qs = batches_qs.filter(branch_id=branch_id)

        total_batch_restocked = batches_qs.aggregate(
            total=Coalesce(Sum("quantity_received"), ZERO, output_field=DecimalField())
        )["total"]

        return float(total_batch_restocked)

    def get_remaining_stock_value(self, obj):
        metrics = self._get_metrics(obj)
        val = metrics.get("remaining_stock_value")
        return str(_money(val))

    def get_is_low_stock(self, obj):
        pricing = self._get_pricing_obj(obj)
        if pricing:
            return pricing.is_low_stock
        return False


# ============================================================
# BRANCH ITEM PRICING
# ============================================================

class BranchItemPricingSerializer(serializers.ModelSerializer):
    branch_detail = RetailBranchSerializer(source="branch", read_only=True)
    item_detail = ItemSerializer(source="item", read_only=True)

    remaining_stock = serializers.ReadOnlyField()
    remaining_stock_value = serializers.ReadOnlyField()
    is_low_stock = serializers.ReadOnlyField()

    class Meta:
        model = BranchItemPricing
        fields = "__all__"
        read_only_fields = [
            "unit_cost_price",
            "remaining_stock",
            "remaining_stock_value",
            "is_low_stock",
        ]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)

        # Extract IDs from object instances/dicts if passed
        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])
        if "item" in data:
            data["item"] = _extract_id(data["item"])

        # Map common field aliases (unit_selling_price <-> unit_retail_price)
        if "unit_selling_price" in data and "unit_retail_price" not in data:
            data["unit_retail_price"] = data["unit_selling_price"]

        if "cost_price" in data and "opening_stock_unit_cost_price" not in data:
            data["opening_stock_unit_cost_price"] = data["cost_price"]

        return super().to_internal_value(data)

    def validate(self, attrs):
        item = attrs.get("item") or getattr(self.instance, "item", None)
        branch = attrs.get("branch") or getattr(self.instance, "branch", None)
        user = _get_request_user(self)

        _validate_item_ownership(item, user)
        _validate_branch_ownership(branch, user)

        if (
            item
            and branch
            and getattr(item, "retailer_id", None)
            and getattr(branch, "retailer_id", None)
            and item.retailer_id != branch.retailer_id
        ):
            raise serializers.ValidationError(
                {"detail": "The branch and item must belong to the same retailer."}
            )

        attrs.pop("unit_cost_price", None)
        return attrs

    def create(self, validated_data):
        item = validated_data.get("item")
        branch = validated_data.get("branch")

        if not item:
            raise serializers.ValidationError({"item": "Item is required."})

        # Fallback to existing branch ONLY if branch is not explicitly passed
        if not branch:
            existing = (
                BranchItemPricing.objects.filter(item=item)
                .select_related("branch")
                .first()
            )
            if existing:
                branch = existing.branch
                validated_data["branch"] = branch

        if not branch:
            raise serializers.ValidationError({"branch": "Branch is required."})

        # Check if pricing record already exists for this specific item + branch pair
        existing = BranchItemPricing.objects.filter(item=item, branch=branch).first()
        if existing:
            validated_data.pop("opening_stock", None)
            validated_data.pop("opening_stock_unit_cost_price", None)
            
            if "unit_retail_price" in validated_data:
                existing.unit_retail_price = validated_data["unit_retail_price"]

            try:
                existing.save()
                recalculate_wac_sales_cost(branch, item)
                existing.refresh_from_db()
                return existing
            except DjangoValidationError as e:
                raise _validation_error(e)
            except IntegrityError as e:
                raise _integrity_error(e)

        # Handle brand new branch-item link creation
        opening_stock = int(validated_data.get("opening_stock", 0) or 0)
        opening_cost = _money(validated_data.get("opening_stock_unit_cost_price", ZERO))

        if opening_stock < 0:
            raise serializers.ValidationError(
                {"opening_stock": "Opening stock cannot be negative."}
            )

        validated_data["unit_cost_price"] = (
            opening_cost if (opening_cost > ZERO or opening_stock > 0) else ZERO
        )

        try:
            pricing = super().create(validated_data)
            recalculate_wac_sales_cost(branch, item)
            pricing.refresh_from_db()
            return pricing
        except DjangoValidationError as e:
            raise _validation_error(e)
        except IntegrityError as e:
            raise _integrity_error(e)


class BranchItemCopySerializer(serializers.Serializer):
    source_branch = serializers.IntegerField(
        required=True,
        help_text="ID or name of the source branch (e.g., Car Wash)."
    )
    target_branch = serializers.IntegerField(
        required=True,
        help_text="ID or name of the new/destination branch."
    )
    item_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        help_text="Specific item IDs to copy. If omitted/empty, copies all items from source branch."
    )
    copy_prices = serializers.BooleanField(
        default=True,
        help_text="Copy unit retail price and unit cost price from the source branch."
    )
    initial_opening_stock = serializers.IntegerField(
        default=0,
        min_value=0,
        help_text="Opening stock count to set for the new branch item records."
    )

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "source_branch" in data:
            data["source_branch"] = _extract_id(data["source_branch"])
        if "target_branch" in data:
            data["target_branch"] = _extract_id(data["target_branch"])
        return super().to_internal_value(data)

    def validate(self, attrs):
        user = _get_request_user(self)
        source_id = attrs.get("source_branch")
        target_id = attrs.get("target_branch")

        if source_id == target_id:
            raise serializers.ValidationError(
                {"target_branch": "Target branch must be different from source branch."}
            )

        # Resolve Source Branch
        try:
            source_branch = RetailBranch.objects.get(pk=source_id)
            _validate_branch_ownership(source_branch, user)
            attrs["_source_branch"] = source_branch
        except RetailBranch.DoesNotExist:
            raise serializers.ValidationError({"source_branch": "Source branch not found."})

        # Resolve Target Branch
        try:
            target_branch = RetailBranch.objects.get(pk=target_id)
            _validate_branch_ownership(target_branch, user)
            attrs["_target_branch"] = target_branch
        except RetailBranch.DoesNotExist:
            raise serializers.ValidationError({"target_branch": "Target branch not found."})

        return attrs

    @transaction.atomic
    def save(self):
        source_branch = self.validated_data["_source_branch"]
        target_branch = self.validated_data["_target_branch"]
        item_ids = self.validated_data.get("item_ids")
        copy_prices = self.validated_data.get("copy_prices", True)
        opening_stock = self.validated_data.get("initial_opening_stock", 0)

        # Query existing items in source branch
        source_pricings = BranchItemPricing.objects.filter(branch=source_branch).select_related("item")
        if item_ids:
            source_pricings = source_pricings.filter(item_id__in=item_ids)

        if not source_pricings.exists():
            raise serializers.ValidationError(
                {"item_ids": "No valid items found in the source branch to copy."}
            )

        created_pricings = []
        updated_pricings = []

        for source_pricing in source_pricings:
            item = source_pricing.item
            retail_price = source_pricing.unit_retail_price if copy_prices else ZERO
            cost_price = source_pricing.unit_cost_price if copy_prices else ZERO

            pricing, created = BranchItemPricing.objects.get_or_create(
                branch=target_branch,
                item=item,
                defaults={
                    "unit_retail_price": _money(retail_price),
                    "unit_cost_price": _money(cost_price),
                    "opening_stock": opening_stock,
                    "opening_stock_unit_cost_price": _money(cost_price),
                },
            )

            if not created and copy_prices:
                pricing.unit_retail_price = _money(retail_price)
                pricing.unit_cost_price = _money(cost_price)
                pricing.save()
                updated_pricings.append(pricing)
            elif created:
                created_pricings.append(pricing)

            # Recalculate WAC cost for newly initialized pricing in target branch
            try:
                recalculate_wac_sales_cost(target_branch, item)
            except Exception:
                pass

        return {
            "source_branch_id": source_branch.id,
            "target_branch_id": target_branch.id,
            "items_copied": len(created_pricings),
            "items_updated": len(updated_pricings),
            "total_processed": len(source_pricings),
        }


from rest_framework import serializers
from stocks.models import Item, Purchase, RetailBranch, Supplier


class PurchaseSerializer(serializers.ModelSerializer):
    """
    Serializer for the Purchase model.
    Handles primary key relationships for write operations along with read-only
    display attributes like names, usernames, and calculated total cost.
    """

    # Optional nested readable representations
    item_name = serializers.CharField(source="item.name", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    supplier_name_display = serializers.CharField(
        source="supplier.name", read_only=True, default=None
    )
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, default=None
    )
    total_cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )

    # Foreign key fields for write operations (allowing PK assignments or model instances)
    item = serializers.PrimaryKeyRelatedField(
        queryset=Item.objects.all(), required=True
    )
    branch = serializers.PrimaryKeyRelatedField(
        queryset=RetailBranch.objects.all(), required=True
    )
    supplier = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = Purchase
        fields = [
            "id",
            "branch",
            "branch_name",
            "item",
            "item_name",
            "supplier",
            "supplier_name",
            "supplier_name_display",
            "quantity",
            "unit_cost_price",
            "total_cost",
            "purchase_date",
            "created_by",
            "created_by_username",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "total_cost",
            "created_by",
            "created_at",
        ]

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        return value

    def validate_unit_cost_price(self, value):
        if value < 0:
            raise serializers.ValidationError("Unit cost price cannot be negative.")
        return value

# ============================================================
# STOCK MOVEMENT
# ============================================================

from rest_framework import serializers

def _extract_id(val):
    """Extract integer ID from integer, numeric string, or dict payload."""
    if val is None or val == "":
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, dict):
        return val.get("id") or val.get("pk") or val.get("branch_id") or val.get("item_id")
    if isinstance(val, str):
        val = val.strip()
        return int(val) if val.isdigit() else val
    return val


class StockMovementSerializer(serializers.ModelSerializer):
    branch_detail = RetailBranchSerializer(source="branch", read_only=True)
    item_detail = ItemSerializer(source="item", read_only=True)
    movement_type_display = serializers.CharField(source="get_movement_type_display", read_only=True)
    total_cost = serializers.ReadOnlyField()

    class Meta:
        model = StockMovement
        fields = "__all__"
        read_only_fields = ["created_by", "total_cost"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)

        # 1. Primary key extraction
        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])
        if "item" in data:
            data["item"] = _extract_id(data["item"])

        # 2. Movement type mapping & normalization
        movement_type = (
            data.get("movement_type")
            or data.get("adjustment_type")
            or data.get("activity_type")
        )

        if movement_type:
            str_type = str(movement_type).upper().strip()
            if str_type in ["INCREASE", "IN", "RESTOCK"]:
                data["movement_type"] = "ADJUSTMENT_IN"
            elif str_type in ["DECREASE", "OUT", "REDUCE"]:
                data["movement_type"] = "ADJUSTMENT_OUT"
            else:
                data["movement_type"] = str_type

        # 3. Unit cost fallback handling
        if "unit_cost_price" not in data or data["unit_cost_price"] is None:
            data["unit_cost_price"] = (
                data.get("unit_cost")
                or data.get("cost_price")
                or data.get("price")
            )

        return super().to_internal_value(data)


# ============================================================
# PURCHASE
# ============================================================

class PurchaseSerializer(serializers.ModelSerializer):
    branch_detail = RetailBranchSerializer(source="branch", read_only=True)
    item_detail = ItemSerializer(source="item", read_only=True)
    total_cost = serializers.ReadOnlyField()

    class Meta:
        model = Purchase
        fields = "__all__"
        read_only_fields = ["created_by", "total_cost"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])
        if "item" in data:
            data["item"] = _extract_id(data["item"])
        if "supplier" in data:
            data["supplier"] = _extract_id(data["supplier"])
        return super().to_internal_value(data)

    def create(self, validated_data):
        user = _get_request_user(self)
        if user and "created_by" not in validated_data:
            validated_data["created_by"] = user
        try:
            return super().create(validated_data)
        except DjangoValidationError as e:
            raise _validation_error(e)
        except IntegrityError as e:
            raise _integrity_error(e)



# ============================================================
# SALE ITEM SERIALIZER
# ============================================================

class SaleItemSerializer(serializers.ModelSerializer):
    item_detail = ItemSerializer(source="item", read_only=True)

    # Coerce float/string quantity inputs cleanly to Decimal
    quantity_sold = serializers.DecimalField(
        max_digits=12, decimal_places=2, coerce_to_string=False
    )

    # Input aliases mapping to DB column 'unit_selling_price'
    unit_retail_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, write_only=True, required=False
    )
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, write_only=True, required=False
    )

    # Read/Write model field
    unit_selling_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )
    discount_per_item = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=Decimal("0.00")
    )

    class Meta:
        model = SaleItem
        fields = [
            "id",
            "transaction",
            "item",
            "item_detail",
            "quantity_sold",
            "unit_cost_price",
            "unit_selling_price",
            "unit_retail_price",
            "unit_price",
            "discount_per_item",
        ]
        read_only_fields = ["id", "unit_cost_price"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)

        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])
        if "item" in data:
            data["item"] = _extract_id(data["item"])

        # Map input price aliases to 'unit_selling_price' column
        if data.get("unit_selling_price") in (None, ""):
            if data.get("unit_retail_price") not in (None, ""):
                data["unit_selling_price"] = data["unit_retail_price"]
            elif data.get("unit_price") not in (None, ""):
                data["unit_selling_price"] = data["unit_price"]

        return super().to_internal_value(data)

    def validate(self, attrs):
        # Clean up alias keys after mapping
        attrs.pop("unit_retail_price", None)
        attrs.pop("unit_price", None)

        if attrs.get("unit_selling_price") is None:
            raise serializers.ValidationError(
                {"unit_selling_price": "Unit selling price is required."}
            )

        # Enforce exact decimal rounding to prevent floating-point leaks
        attrs["unit_selling_price"] = _money(attrs["unit_selling_price"])
        if "discount_per_item" in attrs:
            attrs["discount_per_item"] = _money(attrs["discount_per_item"])

        if attrs["unit_selling_price"] < ZERO:
            raise serializers.ValidationError(
                {"unit_selling_price": "Unit selling price cannot be negative."}
            )

        if attrs.get("discount_per_item", ZERO) < ZERO:
            raise serializers.ValidationError(
                {"discount_per_item": "Discount cannot be negative."}
            )

        if attrs.get("quantity_sold", ZERO) <= ZERO:
            raise serializers.ValidationError(
                {"quantity_sold": "Quantity sold must be greater than zero."}
            )

        return attrs

class SalesCartItemSerializer(serializers.Serializer):
    item = serializers.IntegerField(required=False)
    item_id = serializers.IntegerField(required=False)

    # Replaced IntegerField with DecimalField to accept decimal quantities (e.g., 2.5)
    quantity_sold = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
    )
    quantity = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
    )

    unit_selling_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    unit_retail_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, write_only=True, allow_null=True
    )
    discount_per_item = serializers.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO, required=False
    )

    def to_internal_value(self, data):
        # Prevent side effects on Django QueryDict objects by creating a mutable copy
        if hasattr(data, "dict"):
            data = data.dict()
        else:
            data = data.copy() if hasattr(data, "copy") else dict(data)

        # Normalize item ID aliases
        if "item_id" in data and "item" not in data:
            data["item"] = _extract_id(data["item_id"])
        elif "item" in data:
            data["item"] = _extract_id(data["item"])

        # Normalize quantity aliases
        if "quantity" in data and "quantity_sold" not in data:
            data["quantity_sold"] = data["quantity"]

        # Normalize price field aliases if unit_selling_price is missing or empty
        selling_price = data.get("unit_selling_price")
        if selling_price in (None, ""):
            if data.get("unit_retail_price") not in (None, ""):
                data["unit_selling_price"] = data["unit_retail_price"]
            elif data.get("unit_price") not in (None, ""):
                data["unit_selling_price"] = data["unit_price"]

        return super().to_internal_value(data)

    def validate(self, attrs):
        if not attrs.get("item"):
            raise serializers.ValidationError({"item": "Item ID is required."})
        if attrs.get("quantity_sold") is None:
            raise serializers.ValidationError({"quantity_sold": "Quantity is required."})

        # Resolve unit_selling_price safely from fallbacks
        unit_selling_price = attrs.get("unit_selling_price")
        if unit_selling_price is None and attrs.get("unit_retail_price") is not None:
            unit_selling_price = attrs["unit_retail_price"]
            attrs["unit_selling_price"] = unit_selling_price

        if unit_selling_price is None:
            raise serializers.ValidationError({"unit_selling_price": "Unit selling price is required."})

        unit_price = _money(unit_selling_price)
        discount = _money(attrs.get("discount_per_item", ZERO))
        retail_price = attrs.get("unit_retail_price")

        if unit_price < ZERO:
            raise serializers.ValidationError({"unit_selling_price": "Unit selling price cannot be negative."})
        if retail_price is not None and _money(retail_price) < ZERO:
            raise serializers.ValidationError({"unit_retail_price": "Unit retail price cannot be negative."})
        if discount < ZERO:
            raise serializers.ValidationError({"discount_per_item": "Discount cannot be negative."})
        if discount > unit_price:
            raise serializers.ValidationError({"discount_per_item": "Discount per item cannot exceed the selling price."})

        # Purge non-canonical keys to prevent model pollution down the line
        attrs.pop("unit_retail_price", None)
        attrs.pop("unit_price", None)
        attrs.pop("quantity", None)
        attrs.pop("item_id", None)

        return attrs


# ============================================================
# CHECKOUT PAYMENT INPUT
# ============================================================

class SalesPaymentSerializer(serializers.Serializer):
    method = serializers.CharField(required=True)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=True)
    reference_code = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate_method(self, value):
        normalized = _normalize_payment_method(value)
        if normalized not in VALID_PAYMENT_METHODS:
            raise serializers.ValidationError(
                f"Invalid payment method '{value}'. Choose from: {', '.join(VALID_PAYMENT_METHODS)}"
            )
        return normalized

    def validate_amount(self, value):
        value = _money(value)
        if value <= ZERO:
            raise serializers.ValidationError("Payment amount must be greater than zero.")
        return value

    def validate(self, attrs):
        method = attrs.get("method", "CASH")
        reference_code = (attrs.get("reference_code") or "").strip()

        if method in {"PAYBILL", "SEND_MONEY"} and not reference_code:
            raise serializers.ValidationError(
                {"reference_code": f"A reference code is required for {method} payments."}
            )

        attrs["method"] = method
        if reference_code:
            attrs["reference_code"] = reference_code

        return attrs


# ============================================================
# SALES CHECKOUT
# ============================================================

class SalesCheckoutSerializer(serializers.Serializer):
    branch = serializers.IntegerField(required=True)
    payment_method = serializers.CharField(required=True)
    customer = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    customer_name = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    customer_phone = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    
    # Manual reference field: Never used for system document generation
    invoice_number = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    credit_notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    sale_date = serializers.DateTimeField(required=False, allow_null=True)
    date = serializers.CharField(required=False, allow_blank=True, write_only=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    items = SalesCartItemSerializer(many=True, required=True)
    payments = SalesPaymentSerializer(many=True, required=False, default=list)

    def validate_payment_method(self, value):
        normalized = _normalize_payment_method(value)
        if normalized not in CHECKOUT_PAYMENT_METHODS:
            raise serializers.ValidationError(
                f"Invalid checkout payment method '{value}'. Choose from: {', '.join(CHECKOUT_PAYMENT_METHODS)}"
            )
        return normalized

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)

        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])
        if "customer" in data:
            data["customer"] = _extract_id(data["customer"])

        date_val = None
        for key in ("sale_date", "date", "transaction_date"):
            if data.get(key) not in (None, ""):
                date_val = data.pop(key, None)
                break

        data["sale_date"] = _coerce_to_datetime(date_val) if date_val else timezone.now()

        if "payment_method" in data:
            data["payment_method"] = _normalize_payment_method(data["payment_method"])

        return super().to_internal_value(data)

    def validate(self, attrs):
        user = _get_request_user(self)
        branch_id = attrs.get("branch")
        raw_customer = attrs.get("customer")
        payment_method = attrs.get("payment_method", "CASH")
        customer_name = (attrs.get("customer_name") or "").strip()
        customer_phone = (attrs.get("customer_phone") or "").strip()
        items = attrs.get("items", [])
        payments = attrs.get("payments", [])

        if not items:
            raise serializers.ValidationError({"items": "Sales cart must contain at least one item."})

        try:
            branch_obj = RetailBranch.objects.get(pk=branch_id)
            _validate_branch_ownership(branch_obj, user)
            attrs["_branch_obj"] = branch_obj
        except RetailBranch.DoesNotExist:
            raise serializers.ValidationError({"branch": "Invalid branch specified."})

        customer_obj = None
        if raw_customer:
            customer_obj = _resolve_customer(raw_customer, retailer=user)
            if not customer_obj and customer_name:
                customer_obj = _resolve_customer(customer_name, retailer=user)
        elif customer_name:
            customer_obj = _resolve_customer(customer_name, retailer=user)

        if customer_obj:
            attrs["_customer_obj"] = customer_obj

        credit_amount = ZERO
        for payment in payments:
            normalized_method = _normalize_payment_method(payment.get("method", ""))
            payment["method"] = normalized_method
            if normalized_method == "CREDIT":
                credit_amount += _money(payment.get("amount", ZERO))

        credit_is_involved = (
            payment_method == "CREDIT" or (payment_method == "SPLIT" and credit_amount > ZERO)
        )

        # Fallback customer name if none was supplied for a credit transaction
        if credit_is_involved and not customer_obj and not customer_name:
            attrs["customer_name"] = (
                f"Credit Customer ({customer_phone})" if customer_phone else "Walk-in Credit Customer"
            )

        if payment_method != "SPLIT":
            for payment in payments:
                if payment["method"] != payment_method:
                    raise serializers.ValidationError(
                        {"payments": "Payment methods must match the selected checkout payment method."}
                    )

        if payment_method == "SPLIT" and not payments:
            raise serializers.ValidationError(
                {"payments": "At least one payment is required for a split checkout."}
            )

        item_ids = [item["item"] for item in items]
        if len(item_ids) != len(set(item_ids)):
            raise serializers.ValidationError(
                {"items": "The same item cannot appear more than once in the checkout."}
            )

        for cart_item in items:
            try:
                item_obj = Item.objects.get(pk=cart_item["item"])
                _validate_item_ownership(item_obj, user)
                
                metrics = AnalyticsService.get_item_metrics(item_obj, branch_id=branch_obj.id)
                available_stock = int(metrics.get("remaining_stock", 0))
            except Exception:
                pricing = _get_pricing(item_obj, branch_obj)
                available_stock = int(getattr(pricing, "remaining_stock", 0) if pricing else 0)

            if cart_item["quantity_sold"] > available_stock:
                raise serializers.ValidationError(
                    {
                        "items": (
                            f"Insufficient stock for '{getattr(item_obj, 'name', 'Item')}'. "
                            f"Available: {available_stock}, Requested: {cart_item['quantity_sold']}"
                        )
                    }
                )
            cart_item["_item_obj"] = item_obj

        checkout_total = ZERO
        for cart_item in items:
            quantity = Decimal(str(cart_item["quantity_sold"]))
            unit_price = _money(cart_item["unit_selling_price"])
            discount = _money(cart_item.get("discount_per_item", ZERO))
            effective_price = max(ZERO, unit_price - discount)
            checkout_total += quantity * effective_price

        checkout_total = _money(checkout_total)

        payment_total = ZERO
        for payment in payments:
            payment_total += _money(payment.get("amount", ZERO))
        payment_total = _money(payment_total)

        if payment_method != "SPLIT" and not payments:
            payments = [
                {
                    "method": payment_method,
                    "amount": checkout_total,
                    "reference_code": "",
                }
            ]
            attrs["payments"] = payments
            payment_total = checkout_total

        if payment_method == "SPLIT":
            if abs(payment_total - checkout_total) > MONEY_QUANT:
                raise serializers.ValidationError(
                    {
                        "payments": (
                            f"Payment total must equal the checkout total. "
                            f"Checkout total: {checkout_total}, Payment total: {payment_total}"
                        )
                    }
                )
        else:
            if payments and abs(payment_total - checkout_total) > MONEY_QUANT:
                raise serializers.ValidationError(
                    {
                        "payments": (
                            f"Payment amount must equal the checkout total. "
                            f"Checkout total: {checkout_total}, Payment total: {payment_total}"
                        )
                    }
                )

        attrs["payment_method"] = payment_method
        attrs["_checkout_total"] = checkout_total
        attrs["_payment_total"] = payment_total
        attrs["_credit_amount"] = credit_amount

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        user = _get_request_user(self)
        branch = validated_data["_branch_obj"]
        customer = validated_data.get("_customer_obj")
        customer_name = validated_data.get("customer_name")
        customer_phone = validated_data.get("customer_phone", "")
        manual_invoice_no = validated_data.get("invoice_number", "")
        items = validated_data.get("items", [])
        payments = validated_data.get("payments", [])
        sale_date = validated_data.get("sale_date") or timezone.now()
        payment_method = validated_data.get("payment_method")
        credit_amount = validated_data.get("_credit_amount", ZERO)
        checkout_total = validated_data["_checkout_total"]

        # Safely acquire or create customer using Customer._default_manager
        if not customer and customer_name:
            retailer_user = user or getattr(branch, "retailer", None) or getattr(branch, "user", None)
            phone_val = customer_phone or ""

            customer = Customer._default_manager.filter(
                retailer=retailer_user,
                name__iexact=customer_name
            ).first()

            if not customer:
                customer = Customer._default_manager.create(
                    retailer=retailer_user,
                    name=customer_name,
                    phone_number=phone_val,
                )

        # Internal system receipt generator
        receipt_no = generate_branch_receipt_no(branch, sale_date)

        is_paid_status = (payment_method != "CREDIT") and (credit_amount == ZERO)
        amount_paid = checkout_total if is_paid_status else (checkout_total - credit_amount)

        # Map dynamic payment breakdown fields onto the transaction model
        split_map = {
            "cash_amount": ZERO,
            "mpesa_amount": ZERO,
            "paybill_amount": ZERO,
            "send_money_amount": ZERO,
            "bank_amount": ZERO,
            "credit_amount": ZERO,
        }

        split_json = {}
        for p in payments:
            m = p.get("method", "").upper()
            amt = _money(p.get("amount", ZERO))
            split_json[m.lower()] = float(amt)

            if m in ("CASH",):
                split_map["cash_amount"] += amt
            elif m in ("MPESA", "M_PESA", "MOBILE_MONEY"):
                split_map["mpesa_amount"] += amt
            elif m in ("PAYBILL", "MPESA_PAYBILL"):
                split_map["paybill_amount"] += amt
            elif m in ("SEND_MONEY", "SENDMONEY"):
                split_map["send_money_amount"] += amt
            elif m in ("BANK", "BANK_TRANSFER", "TRANSFER"):
                split_map["bank_amount"] += amt
            elif m in ("CREDIT", "ON_CREDIT"):
                split_map["credit_amount"] += amt

        txn_kwargs = {
            "branch": branch,
            "customer": customer,
            "payment_method": payment_method,
            "total_amount": checkout_total,
            "amount_paid": amount_paid,
            "is_paid": is_paid_status,
            "sale_date": sale_date,
            "receipt_number": receipt_no,
            "invoice_number": manual_invoice_no,
            "created_by": user,
        }

        # Safely attach payment breakdown fields if present on the model
        model_fields = [f.name for f in SaleTransaction._meta.get_fields() if f.concrete]
        for field_name, val in split_map.items():
            if field_name in model_fields:
                txn_kwargs[field_name] = val

        for json_field in ("payment_details", "split_payments", "split_breakdown"):
            if json_field in model_fields:
                txn_kwargs[json_field] = split_json

        # 1. Create Parent Transaction (bypassing custom scoped managers)
        sale_txn = SaleTransaction._default_manager.create(**txn_kwargs)

        # 2. Add Split Payments attached to single Parent
        for p in payments:
            SalePayment._default_manager.create(
                transaction=sale_txn,
                method=p.get("method"),
                amount=_money(p.get("amount", ZERO)),
                reference_code=p.get("reference_code", ""),
            )

        # 3. Create Child Line Items
        for cart_item in items:
            item_obj = cart_item["_item_obj"]
            quantity_sold = cart_item["quantity_sold"]
            unit_selling_price = _money(cart_item["unit_selling_price"])
            discount_per_item = _money(cart_item.get("discount_per_item", ZERO))
            wac_cost = _get_current_wac(item_obj, branch)

            SaleItem._default_manager.create(
                transaction=sale_txn,
                item=item_obj,
                quantity_sold=quantity_sold,
                unit_cost_price=wac_cost,
                unit_selling_price=unit_selling_price,
                discount_per_item=discount_per_item,
            )

            recalculate_wac_sales_cost(branch, item_obj)

        return sale_txn

# ============================================================
# STOCK TRANSFER
# ============================================================

class StockTransferSerializer(serializers.ModelSerializer):
    from_branch_detail = RetailBranchSerializer(source="from_branch", read_only=True)
    to_branch_detail = RetailBranchSerializer(source="to_branch", read_only=True)
    item_detail = ItemSerializer(source="item", read_only=True)

    class Meta:
        model = StockTransfer
        fields = "__all__"
        read_only_fields = ["created_by"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "from_branch" in data:
            data["from_branch"] = _extract_id(data["from_branch"])
        if "to_branch" in data:
            data["to_branch"] = _extract_id(data["to_branch"])
        if "item" in data:
            data["item"] = _extract_id(data["item"])
        return super().to_internal_value(data)

    def create(self, validated_data):
        user = _get_request_user(self)
        if user and "created_by" not in validated_data:
            validated_data["created_by"] = user
        try:
            return super().create(validated_data)
        except DjangoValidationError as e:
            raise _validation_error(e)
        except IntegrityError as e:
            raise _integrity_error(e)


# ============================================================
# SALE TRANSACTION
# ============================================================

class SaleTransactionSerializer(serializers.ModelSerializer):
    branch_detail = RetailBranchSerializer(source="branch", read_only=True)
    customer_detail = CustomerSerializer(source="customer", read_only=True)
    customer_name_input = serializers.CharField(
        source="customer_name", required=False, allow_blank=True, allow_null=True, write_only=True
    )

    customer = serializers.CharField(required=False, allow_null=True)
    invoice_number = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    receipt_number = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    items = SalesCartItemSerializer(
        many=True, write_only=True, required=False, default=list
    )
    payments = SalesPaymentSerializer(
        many=True, write_only=True, required=False, default=list
    )

    item = serializers.PrimaryKeyRelatedField(
        queryset=Item.objects.all(), required=False, write_only=True
    )
    quantity_sold = serializers.IntegerField(
        min_value=1, required=False, write_only=True
    )
    unit_selling_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, write_only=True
    )
    discount_per_item = serializers.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO, required=False, write_only=True
    )

    payment_method = serializers.CharField(required=False, default="CASH")
    sale_date = serializers.DateTimeField(required=False, allow_null=True)

    # Allow total_amount to be accepted from request while remaining read-only on output
    total_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )
    total_discount = serializers.ReadOnlyField()
    total_cost = serializers.ReadOnlyField()
    profit = serializers.ReadOnlyField()

    balance_due = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()

    class Meta:
        model = SaleTransaction
        fields = [
            "id",
            "receipt_number",
            "branch",
            "branch_detail",
            "customer",
            "customer_detail",
            "customer_name",
            "customer_name_input",
            "payment_method",
            "payment_status",
            "invoice_number",
            "is_paid",
            "amount_paid",
            "balance_due",
            "sale_date",
            "item",
            "quantity_sold",
            "unit_selling_price",
            "discount_per_item",
            "items",
            "payments",
            "total_amount",
            "total_discount",
            "total_cost",
            "profit",
            "created_at",
        ]
        read_only_fields = [
            "is_paid",
            "amount_paid",
            "balance_due",
            "payment_status",
            "total_discount",
            "total_cost",
            "profit",
            "created_at",
        ]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)

        # Map receipt number variations to receipt_number
        receipt_val = (
            data.get("receipt_number")
            or data.get("receipt_no")
            or data.get("receiptNumber")
            or data.get("receipt_id")
        )
        if receipt_val:
            data["receipt_number"] = str(receipt_val).strip()

        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])

        # Normalize incoming total_amount to 2 decimal places to prevent DecimalField failures
        if "total_amount" in data and data["total_amount"] is not None:
            try:
                data["total_amount"] = str(_money(data["total_amount"]))
            except Exception:
                pass

        # Purge non-model price keys passed at root level
        data.pop("unit_retail_price", None)
        data.pop("retail_price", None)
        data.pop("unit_price", None)

        if "items" in data and isinstance(data["items"], list):
            sanitised_items = []
            for item in data["items"]:
                if isinstance(item, dict):
                    item_copy = item.copy()

                    if "unit_selling_price" not in item_copy:
                        price_val = (
                            item_copy.get("unit_retail_price")
                            or item_copy.get("retail_price")
                            or item_copy.get("unit_price")
                        )
                        if price_val is not None:
                            item_copy["unit_selling_price"] = price_val

                    # Normalize nested item totals/prices to 2 decimal places
                    for price_key in ["unit_selling_price", "discount_per_item", "total_amount"]:
                        if price_key in item_copy and item_copy[price_key] is not None:
                            try:
                                item_copy[price_key] = str(_money(item_copy[price_key]))
                            except Exception:
                                pass

                    # Purge aliases from nested items explicitly
                    item_copy.pop("unit_retail_price", None)
                    item_copy.pop("retail_price", None)
                    item_copy.pop("unit_price", None)

                    sanitised_items.append(item_copy)
                else:
                    sanitised_items.append(item)
            data["items"] = sanitised_items

        return super().to_internal_value(data)

    def _get_credit_portion(self, obj):
        """Return the amount of this sale that was actually sold on credit."""
        total = _money(getattr(obj, "total_amount", ZERO) or ZERO)

        # A split sale can contain a CREDIT SalePayment alongside cash,
        # paybill, send-money, etc. The credit portion is stored on SalePayment.
        if getattr(obj, "payment_method", None) == "SPLIT":
            credit_total = SalePayment.objects.filter(
                transaction=obj,
                method="CREDIT",
            ).aggregate(
                total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField())
            )["total"] or ZERO
            return _money(credit_total)

        if _normalize_payment_method(getattr(obj, "payment_method", None)) == "CREDIT":
            return total

        return ZERO

    def _get_repayments_total(self, obj):
        if not hasattr(obj, "repayments"):
            return ZERO
        total = obj.repayments.aggregate(
            total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField())
        )["total"] or ZERO
        return _money(total)

    def get_balance_due(self, obj):
        credit_portion = self._get_credit_portion(obj)
        if credit_portion <= ZERO:
            return "0.00"

        repayments = self._get_repayments_total(obj)
        balance = max(ZERO, credit_portion - repayments)
        return str(_money(balance))

    def get_payment_status(self, obj):
        total = _money(getattr(obj, "total_amount", ZERO) or ZERO)
        balance = _money(self.get_balance_due(obj))

        if balance <= ZERO:
            return "PAID"

        if balance < total:
            return "PARTIAL"

        return "PENDING"

    def validate_payment_method(self, value):
        return _normalize_payment_method(value)

    def validate_sale_date(self, value):
        return _coerce_to_datetime(value) if value else timezone.now()

    def validate(self, attrs):
        user = _get_request_user(self)
        branch = attrs.get("branch")
        items = attrs.get("items", [])
        payment_method = attrs.get("payment_method", "CASH")

        raw_customer = (
            attrs.get("customer")
            or self.initial_data.get("customer")
            or self.initial_data.get("customer_id")
        )
        customer_obj = None

        if isinstance(raw_customer, Customer):
            customer_obj = raw_customer
        elif isinstance(raw_customer, dict):
            cust_id = raw_customer.get("id") or raw_customer.get("pk")
            if cust_id:
                customer_obj = Customer._default_manager.filter(pk=cust_id).first()
        elif isinstance(raw_customer, (int, str)) and str(raw_customer).isdigit():
            customer_obj = Customer._default_manager.filter(pk=int(raw_customer)).first()
        elif isinstance(raw_customer, str) and raw_customer.strip():
            customer_obj = _resolve_customer(raw_customer, retailer=user)

        attrs["customer"] = customer_obj

        flat_item = attrs.get("item")
        if not items and flat_item:
            quantity_sold = attrs.get("quantity_sold")
            unit_selling_price = attrs.get("unit_selling_price")

            if not quantity_sold:
                raise serializers.ValidationError({"quantity_sold": "Quantity sold is required."})
            if unit_selling_price is None:
                raise serializers.ValidationError({"unit_selling_price": "Unit selling price is required."})

            items = [{
                "item": flat_item,
                "quantity_sold": quantity_sold,
                "unit_selling_price": unit_selling_price,
                "discount_per_item": attrs.get("discount_per_item", ZERO),
            }]
            attrs["items"] = items

        if not items:
            raise serializers.ValidationError({"items": "At least one sale item or item payload must be provided."})

        _validate_branch_ownership(branch, user)

        # Determine whether this sale contains any credit portion.
        # Split sales may have cash/paybill/send-money plus a credit remainder.
        payments = attrs.get("payments", []) or []
        credit_amount = ZERO
        payment_total = ZERO
        for payment in payments:
            method = _normalize_payment_method(payment.get("method"))
            amount = _money(payment.get("amount", ZERO))
            payment["method"] = method
            payment["amount"] = amount
            payment_total += amount
            if method == "CREDIT":
                credit_amount += amount

        if payment_method == "CREDIT":
            credit_amount = credit_amount or _money(
                attrs.get("total_amount", ZERO) or ZERO
            )
        elif payment_method != "SPLIT":
            credit_amount = ZERO

        # Validate explicit payment rows when supplied. For split sales the
        # rows must equal the sale total; for non-split sales they must equal
        # the selected payment method and sale total.
        if payments:
            calculated_total = ZERO
            for item_data in items:
                qty = Decimal(str(item_data["quantity_sold"]))
                price = _money(item_data["unit_selling_price"])
                discount = _money(item_data.get("discount_per_item", ZERO))
                calculated_total += qty * max(ZERO, price - discount)
            calculated_total = _money(calculated_total)

            if abs(payment_total - calculated_total) > MONEY_QUANT:
                raise serializers.ValidationError({
                    "payments": (
                        f"Payment total must equal the sale total. "
                        f"Sale total: {calculated_total}, Payment total: {payment_total}"
                    )
                })

            if payment_method != "SPLIT":
                invalid_methods = [
                    p["method"] for p in payments
                    if p["method"] != payment_method
                ]
                if invalid_methods:
                    raise serializers.ValidationError({
                        "payments": "Payment methods must match the selected payment method."
                    })

        # Generate an invoice for both pure-credit and split-with-credit sales.
        if credit_amount > ZERO:
            invoice_number = attrs.get("invoice_number") or self.initial_data.get("invoice_number")
            if not invoice_number:
                invoice_number = f"INV-{timezone.now().strftime('%Y%m%d%H%M%S')}"
            attrs["invoice_number"] = invoice_number
        else:
            attrs["invoice_number"] = None

        attrs["_credit_amount"] = _money(credit_amount)

        branch_id = _extract_id(branch)

        for item_data in items:
            raw_item = item_data["item"]
            item_id = raw_item.id if hasattr(raw_item, "id") else raw_item
            quantity_sold = item_data["quantity_sold"]

            try:
                item_obj = raw_item if isinstance(raw_item, Item) else Item._default_manager.get(pk=item_id)
                _validate_item_ownership(item_obj, user)

                metrics = AnalyticsService.get_item_metrics(item_obj, branch_id=branch_id)
                available_stock = int(metrics.get("remaining_stock", 0))
            except Exception:
                pricing = _get_pricing(item_obj, branch)
                available_stock = int(getattr(pricing, "remaining_stock", 0) if pricing else 0)

            if quantity_sold > available_stock:
                raise serializers.ValidationError(
                    {"items": f"Insufficient stock for item ID {item_id}. Available: {available_stock}, Requested: {quantity_sold}"}
                )

            item_data["_item_obj"] = item_obj

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        user = _get_request_user(self)

        branch = validated_data.pop("branch")
        customer = validated_data.pop("customer", None)
        items_data = validated_data.pop("items")
        payments_data = validated_data.pop("payments", [])
        payment_method = validated_data.pop("payment_method", "CASH")
        sale_date = validated_data.pop("sale_date", None) or timezone.now()
        invoice_number = validated_data.pop("invoice_number", None)

        # Pop total_amount from request payload to prevent duplicate keyword arguments
        validated_data.pop("total_amount", None)

        # Use explicitly passed receipt_number or auto-generate
        receipt_number = validated_data.pop("receipt_number", None) or generate_receipt_number()

        # Remove non-model keys and retailer explicitly to avoid manager collisions
        non_model_keys = [
            "created_by",
            "retailer",
            "item",
            "quantity_sold",
            "unit_selling_price",
            "discount_per_item",
            "unit_retail_price",
            "retail_price",
            "unit_price",
        ]
        for key in non_model_keys:
            validated_data.pop(key, None)

        # Explicitly compute total_amount using sanitized Decimal representation
        calculated_total = ZERO
        for item_data in items_data:
            qty = Decimal(str(item_data["quantity_sold"]))
            price = _money(item_data["unit_selling_price"])
            discount = _money(item_data.get("discount_per_item", ZERO))
            calculated_total += qty * (price - discount)

        total_amount = _money(calculated_total)

        # Only non-credit portions are considered paid at the time of sale.
        # A split sale such as 500 CASH + 400 PAYBILL + 300 SEND_MONEY +
        # 800 CREDIT therefore stores amount_paid=1200 and is_paid=False.
        credit_amount = ZERO
        amount_paid = ZERO

        if payments_data:
            for payment in payments_data:
                method = _normalize_payment_method(payment.get("method"))
                amount = _money(payment.get("amount", ZERO))
                if method == "CREDIT":
                    credit_amount += amount
                else:
                    amount_paid += amount
        elif payment_method == "CREDIT":
            credit_amount = total_amount
        else:
            amount_paid = total_amount

        amount_paid = _money(min(total_amount, amount_paid))
        credit_amount = _money(min(total_amount, credit_amount))
        is_paid = credit_amount <= ZERO and amount_paid >= total_amount

        if payment_method == "CREDIT":
            amount_paid = ZERO
            credit_amount = total_amount
            is_paid = False

        # Safely capture ONLY direct DB column fields
        concrete_db_fields = {
            f.name for f in SaleTransaction._meta.get_fields()
            if getattr(f, "column", None) is not None
        }
        
        # Remove fields managed implicitly by custom managers or explicitly set
        excluded_keys = {"total_amount", "retailer"}
        extra_kwargs = {
            k: v for k, v in validated_data.items() 
            if k in concrete_db_fields and k not in excluded_keys
        }

        # Use _default_manager to bypass scoped managers that inject duplicate kwargs
        sale_txn = SaleTransaction._default_manager.create(
            branch=branch,
            customer=customer,
            payment_method=payment_method,
            invoice_number=invoice_number,
            total_amount=total_amount,
            amount_paid=amount_paid,
            is_paid=is_paid,
            sale_date=sale_date,
            receipt_number=receipt_number,
            created_by=user,
            **extra_kwargs
        )

        if payments_data:
            for payment in payments_data:
                SalePayment._default_manager.create(
                    transaction=sale_txn,
                    method=payment["method"],
                    amount=_money(payment["amount"]),
                    reference_code=payment.get("reference_code"),
                )

        created_items = []

        for item_data in items_data:
            item_obj = item_data["_item_obj"]
            quantity_sold = item_data["quantity_sold"]
            unit_selling_price = _money(item_data["unit_selling_price"])
            discount_per_item = _money(item_data.get("discount_per_item", ZERO))
            wac_cost = _get_current_wac(item_obj, branch)

            sale_item = SaleItem._default_manager.create(
                transaction=sale_txn,
                item=item_obj,
                quantity_sold=quantity_sold,
                unit_cost_price=wac_cost,
                unit_selling_price=unit_selling_price,
                discount_per_item=discount_per_item,
            )

            recalculate_wac_sales_cost(branch, item_obj)
            created_items.append(sale_item)

        sale_txn._created_items = created_items
        return sale_txn

    def to_representation(self, instance):
        representation = super().to_representation(instance)

        # A split sale with a CREDIT component also has an invoice.
        credit_portion = self._get_credit_portion(instance)
        if credit_portion > ZERO:
            representation["display_invoice_number"] = instance.invoice_number or instance.receipt_number
        else:
            representation["display_invoice_number"] = instance.receipt_number

        line_items = getattr(instance, "_created_items", None)
        if line_items is None:
            line_items = instance.items.all() if hasattr(instance, "items") else []

        items_list = []
        for line in line_items:
            total = line.quantity_sold * (line.unit_selling_price - line.discount_per_item)
            items_list.append({
                "item_id": line.item_id,
                "item_name": getattr(line.item, "name", ""),
                "quantity": line.quantity_sold,
                "unit_cost_price": str(_money(line.unit_cost_price)),
                "unit_selling_price": str(_money(line.unit_selling_price)),
                "discount_per_item": str(_money(line.discount_per_item)),
                "total_price": str(_money(getattr(line, "total_amount", total))),
            })

        representation["items"] = items_list
        return representation


# ============================================================
# STOCK ADJUSTMENT
# ============================================================




# ============================================================
# EXPENSE
# ============================================================

class ExpenseSerializer(serializers.ModelSerializer):
    branch_detail = RetailBranchSerializer(source="branch", read_only=True)

    class Meta:
        model = Expense
        fields = "__all__"
        read_only_fields = ["retailer"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])
        return super().to_internal_value(data)

    def create(self, validated_data):
        user = _get_request_user(self)
        if user and "retailer" not in validated_data:
            validated_data["retailer"] = user
        try:
            return super().create(validated_data)
        except DjangoValidationError as e:
            raise _validation_error(e)
        except IntegrityError as e:
            raise _integrity_error(e)


# ============================================================
# DAILY FINANCIAL REPORT
# ============================================================

def _normalize_payment_method(method_name):
    """Helper to standardize payment channels."""
    if not method_name:
        return "CASH"

    clean = str(method_name).strip().upper().replace("-", "_").replace(" ", "_")

    if "CASH" in clean:
        return "CASH"
    if "PAYBILL" in clean or "PAY_BILL" in clean:
        return "PAYBILL"
    if "SEND_MONEY" in clean or "SENDMONEY" in clean or "TILL" in clean:
        return "SEND_MONEY"
    if "MPESA" in clean or "M_PESA" in clean or "MOBILE" in clean:
        return "MPESA"
    if "CREDIT" in clean or "DEBT" in clean or "ON_ACCOUNT" in clean:
        return "CREDIT"
    if "BANK" in clean or "TRANSFER" in clean or "EFT" in clean:
        return "BANK_TRANSFER"

    return clean


class DailyFinancialReportSerializer(serializers.ModelSerializer):
    branch_detail = RetailBranchSerializer(source="branch", read_only=True)

    total_sales_revenue = serializers.ReadOnlyField()
    total_cogs = serializers.ReadOnlyField()
    total_expenses = serializers.ReadOnlyField()
    gross_profit = serializers.ReadOnlyField()
    net_profit = serializers.ReadOnlyField()

    total_remaining_stock = serializers.SerializerMethodField()
    total_inventory_value = serializers.SerializerMethodField()
    total_units_sold = serializers.SerializerMethodField()
    payment_breakdown = serializers.SerializerMethodField()

    class Meta:
        model = DailyFinancialReport
        fields = [
            "id",
            "branch",
            "branch_detail",
            "date",
            "total_sales_revenue",
            "total_cogs",
            "total_expenses",
            "gross_profit",
            "net_profit",
            "total_remaining_stock",
            "total_inventory_value",
            "total_units_sold",
            "payment_breakdown",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "retailer",
            "total_sales_revenue",
            "total_cogs",
            "total_expenses",
            "gross_profit",
            "net_profit",
            "total_remaining_stock",
            "total_inventory_value",
            "total_units_sold",
            "payment_breakdown",
        ]

    def get_total_remaining_stock(self, obj):
        if not obj.branch or not obj.date:
            return 0

        now = timezone.localdate()
        # If viewing today's live stock, query live BranchItemPricing directly
        if obj.date >= now:
            result = BranchItemPricing.objects.filter(branch=obj.branch).aggregate(
                total=Coalesce(Sum("remaining_stock"), Decimal("0.000"))
            )["total"]
            return float(result)

        # For past months, try using the pre-calculated MonthlyInventorySnapshot first
        is_current_month = (obj.date.year == now.year and obj.date.month == now.month)
        if not is_current_month:
            snapshot_total = MonthlyInventorySnapshot.objects.filter(
                branch=obj.branch,
                year=obj.date.year,
                month=obj.date.month,
            ).aggregate(total=Coalesce(Sum("closing_stock"), Decimal("0.000")))["total"]

            if snapshot_total:
                return float(snapshot_total)

        # Fallback: Calculate historical stock by deducting stock movements occurred after obj.date
        tz = timezone.get_current_timezone()
        cutoff_dt = timezone.make_aware(datetime.combine(obj.date, datetime.max.time()), tz)

        current_stock = BranchItemPricing.objects.filter(branch=obj.branch).aggregate(
            total=Coalesce(Sum("remaining_stock"), Decimal("0.000"))
        )["total"] or Decimal("0.000")

        inbound_types = [
            StockMovement.MovementType.PURCHASE,
            StockMovement.MovementType.CUSTOMER_RETURN,
            StockMovement.MovementType.TRANSFER_IN,
            StockMovement.MovementType.ADJUSTMENT_IN,
        ]
        outbound_types = [
            StockMovement.MovementType.SALE,
            StockMovement.MovementType.SUPPLIER_RETURN,
            StockMovement.MovementType.TRANSFER_OUT,
            StockMovement.MovementType.DAMAGE,
            StockMovement.MovementType.ADJUSTMENT_OUT,
        ]

        movements = StockMovement.objects.filter(
            branch=obj.branch,
            created_at__gt=cutoff_dt
        ).aggregate(
            stock_in=Coalesce(
                Sum("quantity", filter=Q(movement_type__in=inbound_types)),
                Decimal("0.000"),
                output_field=models.DecimalField()
            ),
            stock_out=Coalesce(
                Sum("quantity", filter=Q(movement_type__in=outbound_types)),
                Decimal("0.000"),
                output_field=models.DecimalField()
            ),
        )

        stock_in_after = Decimal(str(movements["stock_in"] or "0.000"))
        stock_out_after = Decimal(str(movements["stock_out"] or "0.000"))

        historical_stock = Decimal(str(current_stock)) - stock_in_after + stock_out_after
        return max(0.0, float(historical_stock))

    def get_total_inventory_value(self, obj):
        if not obj.branch or not obj.date:
            return "0.00"

        now = timezone.localdate()
        if obj.date >= now:
            total_val = BranchItemPricing.objects.filter(branch=obj.branch).aggregate(
                total=Coalesce(Sum("remaining_stock_value"), Decimal("0.00"))
            )["total"]
            return str(quantize_money(total_val))

        # Valuation for past days based on remaining stock * unit cost
        pricing_qs = BranchItemPricing.objects.filter(branch=obj.branch)
        total_val = sum(
            (item.remaining_stock * (item.unit_cost_price or Decimal("0.00")) for item in pricing_qs),
            Decimal("0.00")
        )
        return str(quantize_money(total_val))

    def get_total_units_sold(self, obj):
        if not obj.branch or not obj.date:
            return 0.0

        # Concrete quantity field aggregation fallback
        qty_field = "quantity" if hasattr(SaleItem, "quantity") else "quantity_sold"
        result = SaleItem.objects.filter(
            transaction__branch=obj.branch,
            transaction__sale_date__date=obj.date
        ).aggregate(total=Coalesce(Sum(qty_field), Decimal("0.000")))["total"]

        return float(result or 0.0)

    def get_payment_breakdown(self, obj):
        if not obj.branch or not obj.date:
            return {}

        breakdown = {
            "CASH": Decimal("0.00"),
            "PAYBILL": Decimal("0.00"),
            "SEND_MONEY": Decimal("0.00"),
            "MPESA": Decimal("0.00"),
            "CREDIT": Decimal("0.00"),
            "BANK_TRANSFER": Decimal("0.00"),
        }

        # 1. Aggregate explicit SalePayment child records
        split_payments = (
            SalePayment.objects.filter(
                transaction__branch=obj.branch,
                transaction__sale_date__date=obj.date,
            )
            .values("method")
            .annotate(total=Coalesce(Sum("amount"), Decimal("0.00")))
        )

        for item in split_payments:
            method = _normalize_payment_method(item["method"])
            amount = quantize_money(item["total"])
            breakdown[method] = breakdown.get(method, Decimal("0.00")) + amount

        processed_txn_ids = set(
            SalePayment.objects.filter(
                transaction__branch=obj.branch,
                transaction__sale_date__date=obj.date,
            ).values_list("transaction_id", flat=True)
        )

        # 2. Process non-split direct transactions
        direct_txns = (
            SaleTransaction.objects.filter(
                branch=obj.branch,
                sale_date__date=obj.date,
            )
            .exclude(id__in=processed_txn_ids)
            .exclude(payment_method="SPLIT")
            .values("payment_method")
            .annotate(total=Coalesce(Sum("total_amount"), Decimal("0.00")))
        )

        for item in direct_txns:
            method = _normalize_payment_method(item["payment_method"])
            amount = quantize_money(item["total"])
            breakdown[method] = breakdown.get(method, Decimal("0.00")) + amount

        return {k: str(quantize_money(v)) for k, v in breakdown.items()}

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "branch" in data and isinstance(data["branch"], dict):
            data["branch"] = data["branch"].get("id")
        return super().to_internal_value(data)

    def create(self, validated_data):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None

        if user and user.is_authenticated and "retailer" not in validated_data:
            validated_data["retailer"] = user

        try:
            return super().create(validated_data)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.message_dict)
        except IntegrityError as e:
            raise serializers.ValidationError({"detail": str(e)})