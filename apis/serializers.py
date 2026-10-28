from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
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
    Item,
    MonthlyInventorySnapshot,
    PendingApproval,
    PurchaseOrder, 
    PurchaseOrderItem,
    Purchase,
    PurchaseReturn,
    RetailBranch,
    SaleItem,
    SalePayment,
    SaleTransaction,
    StockBatch,
    StockMovement,
    StockTransfer,
    Supplier,
    SupplierReturn,
    generate_receipt_number,
    recalculate_wac_sales_cost,
)

User = get_user_model()

# ============================================================
# CONSTANTS & MONEY UTILS
# ============================================================

ZERO = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")

VALID_PAYMENT_METHODS = [
    "CASH",
    "PAYBILL",
    "SEND_MONEY",
    "CREDIT",
    "BANK_TRANSFER",
]

CHECKOUT_PAYMENT_METHODS = [
    "CASH",
    "PAYBILL",
    "SEND_MONEY",
    "CREDIT",
    "BANK_TRANSFER",
    "SPLIT",
]

PAYMENT_METHOD_ALIASES = {
    "M-PESA": "PAYBILL",
    "MPESA": "PAYBILL",
    "PAYBILL": "PAYBILL",
    "SEND_MONEY": "SEND_MONEY",
    "SENDMONEY": "SEND_MONEY",
    "TILL": "SEND_MONEY",
    "CASH": "CASH",
    "CREDIT": "CREDIT",
    "ON_CREDIT": "CREDIT",
    "BANK": "BANK_TRANSFER",
    "BANK_TRANSFER": "BANK_TRANSFER",
    "SPLIT": "SPLIT",
}


def _normalize_payment_method(method):
    if not method:
        return "CASH"
    clean_method = str(method).strip().upper().replace("-", "_").replace(" ", "_")
    return PAYMENT_METHOD_ALIASES.get(clean_method, clean_method)


def quantize_money(val):
    if val is None or val == "":
        return ZERO
    try:
        return Decimal(str(val)).quantize(MONEY_QUANT)
    except (InvalidOperation, TypeError, ValueError):
        return ZERO


def _safe_decimal(value, default=ZERO):
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _money(value):
    return quantize_money(value)


# ============================================================
# COMMON HELPERS
# ============================================================

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
    dt = dt or timezone.now()
    branch_id = getattr(branch, "id", branch) or "0"
    timestamp_str = dt.strftime("%Y%m%d%H%M%S")
    microsec = dt.strftime("%f")[:3]
    return f"RCP-B{branch_id}-{timestamp_str}{microsec}"


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
        return value.get("id") or value.get("pk") or value.get("branch_id") or value.get("item_id")
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
        return obj.branches.first() if hasattr(obj, "branches") else None

    def get_branch(self, obj):
        branch = self.get_first_branch(obj)
        return branch.id if branch else None

    def get_branch_name(self, obj):
        branch = self.get_first_branch(obj)
        return branch.name if branch else None


# ============================================================
# PENDING APPROVAL
# ============================================================

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


# ============================================================
# RETAIL BRANCH
# ============================================================

class RetailBranchSerializer(serializers.ModelSerializer):
    created_at = serializers.DateTimeField(read_only=True, required=False)

    class Meta:
        model = RetailBranch
        fields = "__all__"
        read_only_fields = ["retailer"]

    def create(self, validated_data):
        user = _get_request_user(self)
        if not user:
            request = self.context.get("request")
            if request and hasattr(request, "user") and request.user.is_authenticated:
                user = request.user
        if not user:
            user = User.objects.filter(is_superuser=True).first() or User.objects.first()

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
# CUSTOMER & DEBTORS
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

        if "transaction" not in data or not data["transaction"]:
            if "transaction_id" in data and data["transaction_id"]:
                data["transaction"] = _extract_id(data["transaction_id"])
            elif "receipt_number" in data and data["receipt_number"]:
                tx = SaleTransaction.objects.filter(
                    receipt_number=data["receipt_number"]
                ).first()
                if tx:
                    data["transaction"] = tx.pk

        raw_method = (
            data.get("payment_method")
            or data.get("method")
            or data.get("payment_type")
        )
        if raw_method:
            data["payment_method"] = _normalize_payment_method(raw_method)

        if data.get("date") not in (None, ""):
            data["date"] = _coerce_to_date(data["date"])
        elif data.get("transaction_date") not in (None, ""):
            data["date"] = _coerce_to_date(data["transaction_date"])

        return super().to_internal_value(data)

    def validate(self, attrs):
        transaction_obj = attrs.get("transaction")

        if not attrs.get("payment_method"):
            attrs["payment_method"] = "CASH"

        if transaction_obj:
            if not attrs.get("customer") and getattr(transaction_obj, "customer", None):
                attrs["customer"] = transaction_obj.customer
            if not attrs.get("branch") and getattr(transaction_obj, "branch", None):
                attrs["branch"] = transaction_obj.branch

        if not attrs.get("customer"):
            branch = attrs.get("branch") or (transaction_obj.branch if transaction_obj else None)
            retailer = getattr(branch, "retailer", None) if branch else None

            if retailer:
                cust_name = (
                    transaction_obj.customer_name if transaction_obj and transaction_obj.customer_name
                    else f"Walk-in Customer ({branch.name if branch else 'Branch'})"
                )
                customer, _ = Customer.objects.get_or_create(
                    retailer=retailer,
                    name=cust_name,
                    defaults={"phone_number": getattr(transaction_obj, "customer_phone", None)}
                )
                attrs["customer"] = customer

                if transaction_obj and not transaction_obj.customer:
                    transaction_obj.customer = customer
                    transaction_obj.save(update_fields=["customer"])

        if not attrs.get("customer"):
            raise serializers.ValidationError({"customer": "Customer ID is required."})

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
# ITEM & BRANCH PRICING
# ============================================================

class ItemSerializer(serializers.ModelSerializer):
    unit_cost_price_input = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, write_only=True
    )
    unit_retail_price_input = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, write_only=True
    )
    opening_stock_input = serializers.IntegerField(
        required=False, write_only=True, min_value=0
    )

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
            request.query_params.get("branch") or request.query_params.get("branch_id") if request else None
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
        return pricing.is_low_stock if pricing else False


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

        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])
        if "item" in data:
            data["item"] = _extract_id(data["item"])

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


# ============================================================
# STOCK MOVEMENT & PURCHASES
# ============================================================

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

        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])
        if "item" in data:
            data["item"] = _extract_id(data["item"])

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

        if "unit_cost_price" not in data or data["unit_cost_price"] is None:
            data["unit_cost_price"] = (
                data.get("unit_cost")
                or data.get("cost_price")
                or data.get("price")
            )

        return super().to_internal_value(data)


class PurchaseSerializer(serializers.ModelSerializer):
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

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if "branch" in data:
            data["branch"] = _extract_id(data["branch"])
        if "item" in data:
            data["item"] = _extract_id(data["item"])
        if "supplier" in data:
            data["supplier"] = _extract_id(data["supplier"])
        return super().to_internal_value(data)

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        return value

    def validate_unit_cost_price(self, value):
        if value < 0:
            raise serializers.ValidationError("Unit cost price cannot be negative.")
        return value

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
# SALES & CHECKOUT
# ============================================================

class SaleItemSerializer(serializers.ModelSerializer):
    item_detail = ItemSerializer(source="item", read_only=True)

    quantity_sold = serializers.DecimalField(
        max_digits=12, decimal_places=2, coerce_to_string=False
    )

    unit_retail_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, write_only=True, required=False
    )
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, write_only=True, required=False
    )

    unit_selling_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )
    discount_per_item = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=ZERO
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

        if data.get("unit_selling_price") in (None, ""):
            if data.get("unit_retail_price") not in (None, ""):
                data["unit_selling_price"] = data["unit_retail_price"]
            elif data.get("unit_price") not in (None, ""):
                data["unit_selling_price"] = data["unit_price"]

        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs.pop("unit_retail_price", None)
        attrs.pop("unit_price", None)

        if attrs.get("unit_selling_price") is None:
            raise serializers.ValidationError(
                {"unit_selling_price": "Unit selling price is required."}
            )

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
        if hasattr(data, "dict"):
            data = data.dict()
        else:
            data = data.copy() if hasattr(data, "copy") else dict(data)

        if "item_id" in data and "item" not in data:
            data["item"] = _extract_id(data["item_id"])
        elif "item" in data:
            data["item"] = _extract_id(data["item"])

        if "quantity" in data and "quantity_sold" not in data:
            data["quantity_sold"] = data["quantity"]

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

        attrs.pop("unit_retail_price", None)
        attrs.pop("unit_price", None)
        attrs.pop("quantity", None)
        attrs.pop("item_id", None)

        return attrs


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

    payment_method = serializers.CharField(required=False, default="CASH")
    sale_date = serializers.DateTimeField(required=False, allow_null=True)

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

        if "total_amount" in data and data["total_amount"] is not None:
            try:
                data["total_amount"] = str(_money(data["total_amount"]))
            except Exception:
                pass

        data.pop("unit_retail_price", None)
        data.pop("retail_price", None)
        data.pop("unit_price", None)

        return super().to_internal_value(data)

    def _get_credit_portion(self, obj):
        total = _money(getattr(obj, "total_amount", ZERO) or ZERO)

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

    def get_balance_due(self, obj):
        credit_portion = self._get_credit_portion(obj)
        if credit_portion <= ZERO:
            return "0.00"

        repayments = obj.repayments.aggregate(
            total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField())
        )["total"] or ZERO if hasattr(obj, "repayments") else ZERO

        balance = max(ZERO, credit_portion - _money(repayments))
        return str(_money(balance))

    def get_payment_status(self, obj):
        total = _money(getattr(obj, "total_amount", ZERO) or ZERO)
        balance = _money(self.get_balance_due(obj))

        if balance <= ZERO:
            return "PAID"
        if balance < total:
            return "PARTIAL"
        return "PENDING"


# ============================================================
# EXPENSE & STOCK TRANSFERS
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
# DAILY FINANCIAL REPORT
# ============================================================

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
        if obj.date >= now:
            result = BranchItemPricing.objects.filter(branch=obj.branch).aggregate(
                total=Coalesce(Sum("remaining_stock"), Decimal("0.000"))
            )["total"]
            return float(result)

        is_current_month = (obj.date.year == now.year and obj.date.month == now.month)
        if not is_current_month:
            snapshot_total = MonthlyInventorySnapshot.objects.filter(
                branch=obj.branch,
                year=obj.date.year,
                month=obj.date.month,
            ).aggregate(total=Coalesce(Sum("closing_stock"), Decimal("0.000")))["total"]

            if snapshot_total:
                return float(snapshot_total)

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
                output_field=DecimalField()
            ),
            stock_out=Coalesce(
                Sum("quantity", filter=Q(movement_type__in=outbound_types)),
                Decimal("0.000"),
                output_field=DecimalField()
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

        pricing_qs = BranchItemPricing.objects.filter(branch=obj.branch)
        total_val = sum(
            (item.remaining_stock * (item.unit_cost_price or Decimal("0.00")) for item in pricing_qs),
            Decimal("0.00")
        )
        return str(quantize_money(total_val))

    def get_total_units_sold(self, obj):
        if not obj.branch or not obj.date:
            return 0.0

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