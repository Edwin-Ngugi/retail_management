import calendar
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import FieldError, ValidationError as DjangoValidationError
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import IntegrityError, models, transaction
from django.db.models import (
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, TruncDate
from django.http import HttpResponse
from django.utils import timezone

from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from analytics.services import AnalyticsService, InventoryService

from stocks.models import (
    BranchItemPricing,
    CreditRepayment,
    Customer,
    CustomerReturn,
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
    Supplier,
    SupplierReturn,
    recalculate_wac_sales_cost,
)

from .serializers import (
    BranchItemCopySerializer,
    CreditRepaymentSerializer,
    CustomerSerializer,
    ExpenseSerializer,
    ItemSerializer,
    RetailBranchSerializer,
    SaleTransactionSerializer,
    StockMovementSerializer,
    UserRegistrationSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)




User = get_user_model()

# ============================================================
# HELPERS
# ============================================================

def _parse_business_date(raw_value, default=None):
    if raw_value is None or raw_value == "":
        return default or timezone.localdate()

    if isinstance(raw_value, datetime):
        if timezone.is_aware(raw_value):
            raw_value = timezone.localtime(raw_value)
        return raw_value.date()

    if isinstance(raw_value, date):
        return raw_value

    raw_value = str(raw_value).strip()

    if not raw_value:
        return default or timezone.localdate()

    try:
        parsed = datetime.fromisoformat(
            raw_value.replace("Z", "+00:00")
        )

        if timezone.is_aware(parsed):
            parsed = timezone.localtime(parsed)

        return parsed.date()

    except (ValueError, TypeError):
        pass

    try:
        return datetime.strptime(
            raw_value[:10],
            "%Y-%m-%d",
        ).date()

    except (ValueError, TypeError):
        raise DRFValidationError(
            {
                "date": (
                    "Invalid date format. "
                    "Use YYYY-MM-DD."
                )
            }
        )


def _resolve_branch(branch_value):
    if not branch_value:
        return None

    if isinstance(branch_value, RetailBranch):
        return branch_value

    if isinstance(branch_value, dict):
        branch_value = (
            branch_value.get("id")
            or branch_value.get("pk")
            or branch_value.get("branch_id")
        )

    if not branch_value:
        return None

    if str(branch_value).isdigit():
        return (
            RetailBranch.objects
            .filter(id=int(branch_value))
            .first()
        )

    return (
        RetailBranch.objects
        .filter(
            name__iexact=str(branch_value).strip()
        )
        .first()
    )


def _recalculate_wac(item, branch):
    """
    Recalculate authoritative WAC from the stock ledger.
    """
    if not item or not branch:
        return Decimal("0.00")

    return recalculate_wac_sales_cost(
        branch=branch,
        item=item,
    )


def _cache_active_pricing(item, pricing):
    """
    Attach branch pricing to an Item instance without inventing a
    non-existent ``branch_pricing`` model relationship.
    """
    if item is None:
        return

    item.active_branch_pricing = [pricing] if pricing is not None else []


def _get_branch_pricing(item, branch):
    if not item or not branch:
        return None

    pricing = (
        BranchItemPricing.objects
        .filter(
            item=item,
            branch=branch,
        )
        .first()
    )

    if pricing and item:
        _cache_active_pricing(item, pricing)

    return pricing


def _get_current_wac(item, branch):
    """
    Get current WAC. First attempts to reconstruct WAC from ledger,
    falling back to persisted BranchItemPricing.
    """
    if not item or not branch:
        return Decimal("0.00")

    try:
        wac = BranchItemPricing.calculate_wac(
            branch=branch,
            item=item,
        )

        if wac is not None:
            return Decimal(str(wac))

    except Exception:
        pass

    pricing = _get_branch_pricing(
        item,
        branch,
    )

    if pricing and pricing.unit_cost_price is not None:
        return Decimal(
            str(pricing.unit_cost_price)
        )

    return Decimal("0.00")


def _refresh_pricing_wac(item, branch):
    """
    Recalculate WAC and persist it on BranchItemPricing.
    """
    if not item or not branch:
        return Decimal("0.00")

    _recalculate_wac(
        item,
        branch,
    )

    wac = _get_current_wac(
        item,
        branch,
    )

    pricing = (
        BranchItemPricing.objects
        .filter(
            item=item,
            branch=branch,
        )
        .first()
    )

    if pricing:
        pricing.unit_cost_price = wac
        pricing.save(
            update_fields=["unit_cost_price"]
        )
        if item:
            _cache_active_pricing(item, pricing)

    return wac


def _authenticated_user(request):
    if (
        request
        and getattr(request, "user", None)
        and request.user.is_authenticated
    ):
        return request.user

    return None


# ============================================================
# RETAIL BRANCH
# ============================================================
from django.contrib.auth import get_user_model
from rest_framework import permissions, viewsets
from rest_framework.exceptions import ValidationError



def generate_30min_tokens_for_user(user, portal_type="user"):
    """
    Utility function to create JWT tokens with explicit 30-minute lifetime.
    """
    refresh = RefreshToken.for_user(user)
    # Enforce 30-minute token lifetime for session security
    refresh.access_token.set_exp(lifetime=timedelta(minutes=30))
    
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "expires_in_minutes": 30,
        "portal_type": portal_type,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
        }
    }


class UserLoginView(APIView):
    """
    1. Normal User Portal Login Endpoint.
    Accessible by: Regular Users & Superadmins.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")

        user = authenticate(request, username=username, password=password)

        if not user:
            return Response(
                {"error": "Invalid username or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Allow regular users AND superadmin
        if user.is_staff and not user.is_superuser:
            return Response(
                {"error": "Administrative staff should use the Management or Admin login page."},
                status=status.HTTP_403_FORBIDDEN,
            )

        token_data = generate_30min_tokens_for_user(user, portal_type="user")
        return Response(token_data, status=status.HTTP_200_OK)


class ManagementLoginView(APIView):
    """
    2. Management Portal Login Endpoint.
    Accessible by: Management/Staff Users & Superadmins.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")

        user = authenticate(request, username=username, password=password)

        if not user:
            return Response(
                {"error": "Invalid management credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Enforce management / staff privilege (or superadmin)
        if not (user.is_staff or user.is_superuser):
            return Response(
                {"error": "Access denied. Management authorization required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        token_data = generate_30min_tokens_for_user(user, portal_type="management")
        return Response(token_data, status=status.HTTP_200_OK)


class AdminLoginView(APIView):
    """
    3. Dedicated Admin Portal Login Endpoint.
    Accessible by: Staff/Admins & Superadmins.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")

        user = authenticate(request, username=username, password=password)

        if not user:
            return Response(
                {"error": "Invalid admin credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Enforce admin privilege (is_staff or is_superuser)
        if not (user.is_staff or user.is_superuser):
            return Response(
                {"error": "Access denied. Superadmin or Admin privileges required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        token_data = generate_30min_tokens_for_user(user, portal_type="admin")
        return Response(token_data, status=status.HTTP_200_OK)


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Customizes JWT login response to return user profile info along with 30-minute tokens.
    """
    def validate(self, attrs):
        data = super().validate(attrs)
        refresh = self.get_token(self.user)
        refresh.access_token.set_exp(lifetime=timedelta(minutes=30))
        
        data["access"] = str(refresh.access_token)
        data["refresh"] = str(refresh)
        data["user"] = UserSerializer(self.user).data
        return data


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


class RegisterView(generics.CreateAPIView):
    """
    API view for registering a new user.
    """
    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        user_data = UserSerializer(user).data
        return Response(
            {
                "message": "User registered successfully.",
                "user": user_data,
            },
            status=status.HTTP_201_CREATED,
        )


class UserProfileView(generics.RetrieveAPIView):
    """
    API view to retrieve the profile of the currently authenticated user.
    """
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

class RetailBranchViewSet(viewsets.ModelViewSet):
    serializer_class = RetailBranchSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        # Return all branches so every user sees the complete branch list
        return RetailBranch.objects.all().order_by("name")

    def perform_create(self, serializer):
        user = _authenticated_user(self.request)

        if not user and self.request.user.is_authenticated:
            user = self.request.user

        if not user:
            user = User.objects.filter(is_superuser=True).first() or User.objects.first()

        if not user:
            raise ValidationError(
                {"retailer": "A valid retailer user is required to create a branch."}
            )

        serializer.save(retailer=user)

class CopyBranchItemsView(APIView):
    def post(self, request, *args, **kwargs):
        serializer = BranchItemCopySerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        result = serializer.save()
        return Response(result, status=status.HTTP_201_CREATED)

# ============================================================
# CUSTOMER
# ============================================================

class CustomerViewSet(viewsets.ModelViewSet):
    serializer_class = CustomerSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        queryset = (
            Customer.objects
            .all()
            .order_by("name")
        )

        user = _authenticated_user(self.request)

        if user:
            queryset = queryset.filter(
                retailer=user
            )

        return queryset

    def perform_create(self, serializer):
        user = _authenticated_user(self.request)

        if user:
            serializer.save(retailer=user)
        else:
            serializer.save()


# ============================================================
# CREDIT REPAYMENT
# ============================================================

# ============================================================
# CREDIT REPAYMENT
# ============================================================

class CreditRepaymentViewSet(viewsets.ModelViewSet):
    serializer_class = CreditRepaymentSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        queryset = (
            CreditRepayment.objects
            .select_related(
                "customer",
                "branch",
                "transaction",
            )
            .all()
            .order_by("-id")
        )

        customer_id = self.request.query_params.get("customer")
        branch_id = (
            self.request.query_params.get("branch")
            or self.request.query_params.get("branch_id")
        )

        month_param = self.request.query_params.get("month")
        year_param = self.request.query_params.get("year")
        date_param = self.request.query_params.get("date") or self.request.query_params.get("selectedDate")
        period_type = (
            self.request.query_params.get("period_type")
            or self.request.query_params.get("periodType")
            or "day"
        ).lower().strip()

        # 1. Branch Filter (Global Unfiltered by User)
        if branch_id and str(branch_id).lower() != "all":
            if str(branch_id).isdigit():
                queryset = queryset.filter(
                    Q(branch_id=int(branch_id)) | Q(transaction__branch_id=int(branch_id))
                )
            else:
                queryset = queryset.filter(
                    Q(branch__name__iexact=str(branch_id).strip())
                    | Q(transaction__branch__name__iexact=str(branch_id).strip())
                )

        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        # 2. Date Range Filter
        today = timezone.localdate()
        if period_type == "month" or (month_param and year_param):
            yr = int(year_param) if year_param else today.year
            mth = int(month_param) if month_param else today.month
            _, last_day = calendar.monthrange(yr, mth)
            start_date = date(yr, mth, 1)
            end_date = date(yr, mth, last_day)

            tz = timezone.get_current_timezone()
            start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()), tz)
            end_dt = timezone.make_aware(datetime.combine(end_date, datetime.max.time()), tz)

            queryset = queryset.filter(
                Q(date__range=(start_date, end_date)) | Q(created_at__range=(start_dt, end_dt))
            )

        elif period_type == "year" and year_param:
            yr = int(year_param)
            start_date = date(yr, 1, 1)
            end_date = date(yr, 12, 31)

            tz = timezone.get_current_timezone()
            start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()), tz)
            end_dt = timezone.make_aware(datetime.combine(end_date, datetime.max.time()), tz)

            queryset = queryset.filter(
                Q(date__range=(start_date, end_date)) | Q(created_at__range=(start_dt, end_dt))
            )

        return queryset

    def perform_create(self, serializer):
        user = _authenticated_user(self.request)
        
        raw_method = (
            self.request.data.get("payment_method") 
            or self.request.data.get("method") 
            or "CASH"
        )
        
        payment_method = (
            str(raw_method)
            .strip()
            .upper()
            .replace(" ", "_")
            .replace("-", "_")
        )

        try:
            repayment = serializer.save(
                created_by=user,
                payment_method=payment_method
            )

        except DjangoValidationError as e:
            raise DRFValidationError(
                e.message_dict if hasattr(e, "message_dict") else str(e)
            )

        if repayment.branch_id:
            channel_layer = get_channel_layer()
            if channel_layer:
                try:
                    async_to_sync(channel_layer.group_send)(
                        f"analytics_branch_{repayment.branch_id}",
                        {
                            "type": "analytics_update",
                            "event": "REPAYMENT_RECORDED",
                            "customer_id": repayment.customer_id,
                            "payment_method": repayment.payment_method,
                            "amount": float(repayment.amount),
                        },
                    )
                except Exception:
                    pass


# ============================================================
# ITEM
# ============================================================

class ItemViewSet(viewsets.ModelViewSet):
    serializer_class = ItemSerializer
    permission_classes = [permissions.AllowAny]

    def _broadcast_websocket_event(
        self,
        branch_id,
        event_type,
        item_id=None,
        updated_item=None,
        updated_items=None,
    ):
        if not branch_id:
            return

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        payload = {
            "type": "analytics_update",
            "event": event_type,
        }

        if item_id is not None:
            payload["item_id"] = item_id
        if updated_item is not None:
            payload["updated_item"] = updated_item
        if updated_items is not None:
            payload["updated_items"] = updated_items

        try:
            async_to_sync(channel_layer.group_send)(
                f"analytics_branch_{branch_id}",
                payload,
            )
        except Exception:
            pass

    def _get_branch_id_from_request(self, request):
        try:
            data = request.data if isinstance(request.data, dict) else {}
        except Exception:
            data = {}

        return (
            data.get("branch")
            or data.get("branch_id")
            or request.query_params.get("branch")
            or request.query_params.get("branch_id")
        )

    def _get_item_pricing(self, item, branch_id=None):
        if not item:
            return None

        queryset = BranchItemPricing.objects.filter(item=item).select_related("branch")

        if branch_id:
            if str(branch_id).isdigit():
                queryset = queryset.filter(branch_id=int(branch_id))
            else:
                queryset = queryset.filter(branch__name__iexact=str(branch_id).strip())

        pricing = queryset.first()
        if pricing:
            _cache_active_pricing(item, pricing)

        return pricing

    def _serialize_item(self, item, branch_id=None):
        if not item:
            return None

        pricing = self._get_item_pricing(item, branch_id=branch_id)
        if pricing:
            _cache_active_pricing(item, pricing)

        context = self.get_serializer_context()
        if branch_id:
            context["branch_id"] = branch_id

        try:
            return ItemSerializer(item, context=context).data
        except Exception as serialization_error:
            opening_stock = getattr(pricing, "opening_stock", 0) or 0
            unit_cost = Decimal(str(getattr(pricing, "unit_cost_price", ZERO) or ZERO))
            unit_retail = Decimal(str(getattr(pricing, "unit_retail_price", ZERO) or ZERO))
            threshold = getattr(pricing, "min_stock_threshold", 5) or 5
            remaining_stock = getattr(pricing, "remaining_stock", opening_stock)
            remaining_value = Decimal(str(remaining_stock)) * unit_cost

            branch_value = None
            if branch_id and str(branch_id).isdigit():
                branch_value = int(branch_id)
            elif pricing:
                branch_value = pricing.branch_id

            return {
                "id": item.id,
                "retailer": item.retailer_id,
                "name": item.name,
                "code": item.code,
                "sku": item.sku,
                "branch": branch_value,
                "opening_stock": opening_stock,
                "unit_cost_price": str(unit_cost),
                "weighted_unit_cost": str(unit_cost),
                "latest_unit_cost": str(unit_cost),
                "unit_retail_price": str(unit_retail),
                "remaining_stock": remaining_stock,
                "total_stock_sold": 0,
                "remaining_stock_value": str(remaining_value),
                "is_low_stock": remaining_stock <= threshold if pricing else False,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "_serialization_warning": str(serialization_error),
            }

    def get_serializer_context(self):
        context = super().get_serializer_context()
        try:
            data = self.request.data if isinstance(self.request.data, dict) else {}
        except Exception:
            data = {}

        branch_id = (
            self.request.query_params.get("branch")
            or self.request.query_params.get("branch_id")
            or data.get("branch")
            or data.get("branch_id")
        )

        if branch_id:
            context["branch_id"] = branch_id

        return context

    def get_queryset(self):
        # Fetch all base items with retailer relation preloaded
        queryset = Item.objects.select_related("retailer").all().order_by("name")

        query_params = getattr(self.request, "query_params", {}) if hasattr(self, "request") else {}
        try:
            data = self.request.data if hasattr(self, "request") and isinstance(getattr(self.request, "data", None), dict) else {}
        except Exception:
            data = {}

        # Extract branch identification
        branch_param = (
            query_params.get("branch")
            or query_params.get("branch_id")
            or data.get("branch")
            or data.get("branch_id")
        )

        year_param = query_params.get("year") or data.get("year")
        month_param = query_params.get("month") or data.get("month")

        date_filter = Q()
        if year_param and month_param:
            try:
                year = int(year_param)
                month = int(month_param)
                _, last_day = calendar.monthrange(year, month)
                start_date = datetime(year, month, 1, 0, 0, 0)
                end_date = datetime(year, month, last_day, 23, 59, 59)
                date_filter = Q(created_at__gte=start_date, created_at__lte=end_date)
            except ValueError:
                pass

        # Apply Branch and Date filtering with prefetching to avoid N+1 queries
        if branch_param:
            branch_param_str = str(branch_param).strip()
            pricing_filter = date_filter

            if branch_param_str.isdigit():
                branch_id = int(branch_param_str)
                pricing_filter &= Q(branch_id=branch_id)
                
                queryset = queryset.filter(
                    branch_pricings__branch_id=branch_id
                ).prefetch_related(
                    Prefetch(
                        "branch_pricings",
                        queryset=BranchItemPricing.objects.filter(pricing_filter).select_related("branch"),
                        to_attr="active_branch_pricing",
                    )
                )
            else:
                pricing_filter &= Q(branch__name__iexact=branch_param_str)
                
                queryset = queryset.filter(
                    branch_pricings__branch__name__iexact=branch_param_str
                ).prefetch_related(
                    Prefetch(
                        "branch_pricings",
                        queryset=BranchItemPricing.objects.filter(pricing_filter).select_related("branch"),
                        to_attr="active_branch_pricing",
                    )
                )

        return queryset.distinct()

    def _fetch_item_with_pricing(self, item_id, branch_id=None):
        queryset = Item.objects.select_related("retailer").filter(id=item_id)

        if branch_id:
            if str(branch_id).isdigit():
                pricing_queryset = BranchItemPricing.objects.filter(branch_id=int(branch_id)).select_related("branch")
            else:
                pricing_queryset = BranchItemPricing.objects.filter(branch__name__iexact=str(branch_id).strip()).select_related("branch")

            queryset = queryset.prefetch_related(
                Prefetch("branch_pricings", queryset=pricing_queryset, to_attr="active_branch_pricing")
            )
        else:
            queryset = queryset.prefetch_related(
                Prefetch("branch_pricings", queryset=BranchItemPricing.objects.select_related("branch"), to_attr="active_branch_pricing")
            )

        item = queryset.first()
        if item:
            pricing = getattr(item, "active_branch_pricing", [])
            if pricing:
                _cache_active_pricing(item, pricing[0])
            else:
                pricing_obj = self._get_item_pricing(item, branch_id=branch_id)
                if pricing_obj:
                    _cache_active_pricing(item, pricing_obj)

        return item

    def _align_retailer(self, item, branch_id=None):
        if not branch_id:
            return

        branch = _resolve_branch(branch_id)
        if not branch:
            return

        target_retailer = branch.retailer or item.retailer
        user = _authenticated_user(self.request)

        if not target_retailer and user:
            target_retailer = user

        if not target_retailer:
            return

        if item.retailer_id != target_retailer.id:
            item.retailer = target_retailer
            item.save(update_fields=["retailer", "updated_at"])

        if branch.retailer_id != target_retailer.id:
            branch.retailer = target_retailer
            branch.save(update_fields=["retailer"])

    def create(self, request, *args, **kwargs):
        data = request.data if isinstance(request.data, dict) else {}
        branch_id = data.get("branch") or data.get("branch_id")

        try:
            opening_stock = int(data.get("opening_stock", 0) or 0)
        except (TypeError, ValueError):
            return Response({"opening_stock": "opening_stock must be a valid integer."}, status=status.HTTP_400_BAD_REQUEST)

        if opening_stock < 0:
            return Response({"opening_stock": "opening_stock cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            unit_cost_raw = data.get("unit_cost_price")
            unit_cost = Decimal("0.00") if unit_cost_raw in (None, "") else Decimal(str(unit_cost_raw))
        except Exception:
            return Response({"unit_cost_price": "unit_cost_price must be a valid number."}, status=status.HTTP_400_BAD_REQUEST)

        if unit_cost < 0:
            return Response({"unit_cost_price": "unit_cost_price cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            unit_retail_raw = data.get("unit_retail_price")
            unit_retail = Decimal(str(unit_retail_raw if unit_retail_raw not in (None, "") else "0.00"))
        except Exception:
            return Response({"unit_retail_price": "unit_retail_price must be a valid number."}, status=status.HTTP_400_BAD_REQUEST)

        if unit_retail < 0:
            return Response({"unit_retail_price": "unit_retail_price cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)

        branch_obj = None
        if branch_id:
            branch_obj = _resolve_branch(branch_id)
            if not branch_obj:
                return Response({"branch": "Valid branch is required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except DRFValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        except DjangoValidationError as e:
            return Response(e.message_dict if hasattr(e, "message_dict") else {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                user = _authenticated_user(request)
                save_kwargs = {"retailer": user} if user else {}
                item = serializer.save(**save_kwargs)

                if branch_obj:
                    self._align_retailer(item, branch_obj.id)
                    item.refresh_from_db()

                    pricing = BranchItemPricing.objects.select_for_update().filter(item=item, branch=branch_obj).first()

                    if pricing:
                        has_movements = StockMovement.objects.filter(item=item, branch=branch_obj).exists()
                        pricing.unit_retail_price = unit_retail

                        if not has_movements and pricing.remaining_stock == 0:
                            pricing.unit_cost_price = unit_cost
                            pricing.opening_stock_unit_cost_price = unit_cost

                        pricing.save()
                    else:
                        pricing = BranchItemPricing.objects.create(
                            item=item,
                            branch=branch_obj,
                            opening_stock=opening_stock,
                            opening_stock_unit_cost_price=unit_cost,
                            unit_cost_price=unit_cost,
                            unit_retail_price=unit_retail,
                        )

                    _cache_active_pricing(item, pricing)

        except IntegrityError as e:
            error_text = str(e).lower()
            if "unique_item_per_retailer" in error_text:
                return Response({"name": "An item with this name already exists for this retailer."}, status=status.HTTP_400_BAD_REQUEST)
            if "stocks_item_code_key" in error_text or "item.code" in error_text or "code_key" in error_text:
                return Response({"code": "An item with this code already exists."}, status=status.HTTP_400_BAD_REQUEST)
            return Response({"error": "The item could not be created because of a database constraint.", "details": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": "Unable to create item.", "details": str(e), "exception_type": type(e).__name__}, status=status.HTTP_400_BAD_REQUEST)

        refetched_item = self._fetch_item_with_pricing(item.id, branch_id) or item
        response_data = self._serialize_item(refetched_item, branch_id=branch_id)

        if branch_obj:
            self._broadcast_websocket_event(
                branch_id=branch_obj.id,
                event_type="ITEM_CREATED",
                item_id=item.id,
                updated_item=response_data,
            )

        headers = self.get_success_headers(response_data)
        return Response(response_data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)

        try:
            instance = self.get_object()
        except Item.DoesNotExist:
            return Response({"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": "Unable to retrieve item.", "details": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        data = request.data if isinstance(request.data, dict) else {}
        branch_id = self._get_branch_id_from_request(request)

        branch_obj = None
        if branch_id:
            branch_obj = _resolve_branch(branch_id)
            if not branch_obj:
                return Response({"branch": "The specified branch does not exist."}, status=status.HTTP_400_BAD_REQUEST)

        unit_cost = None
        if "unit_cost_price" in data and data.get("unit_cost_price") not in (None, ""):
            try:
                unit_cost = Decimal(str(data.get("unit_cost_price")))
                if unit_cost < 0:
                    return Response({"unit_cost_price": "unit_cost_price cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)
            except Exception:
                return Response({"unit_cost_price": "unit_cost_price must be a valid number."}, status=status.HTTP_400_BAD_REQUEST)

        unit_retail = None
        if "unit_retail_price" in data and data.get("unit_retail_price") not in (None, ""):
            try:
                unit_retail = Decimal(str(data.get("unit_retail_price")))
                if unit_retail < 0:
                    return Response({"unit_retail_price": "unit_retail_price cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)
            except Exception:
                return Response({"unit_retail_price": "unit_retail_price must be a valid number."}, status=status.HTTP_400_BAD_REQUEST)

        opening_stock = None
        if "opening_stock" in data and data.get("opening_stock") not in (None, ""):
            try:
                opening_stock = int(data.get("opening_stock"))
                if opening_stock < 0:
                    return Response({"opening_stock": "opening_stock cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)
            except (TypeError, ValueError):
                return Response({"opening_stock": "opening_stock must be a valid integer."}, status=status.HTTP_400_BAD_REQUEST)

        remaining_stock_val = None
        if "remaining_stock" in data and data.get("remaining_stock") not in (None, ""):
            try:
                remaining_stock_val = Decimal(str(data.get("remaining_stock")))
                if remaining_stock_val < 0:
                    return Response({"remaining_stock": "remaining_stock cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)
            except (TypeError, ValueError):
                return Response({"remaining_stock": "remaining_stock must be a valid number."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        try:
            serializer.is_valid(raise_exception=True)
        except DRFValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        except DjangoValidationError as e:
            return Response(e.message_dict if hasattr(e, "message_dict") else {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                item = Item.objects.select_for_update().get(pk=instance.pk)
                item = serializer.save()

                if branch_obj:
                    self._align_retailer(item, branch_obj.id)
                    item.refresh_from_db()

                    pricing = BranchItemPricing.objects.select_for_update().filter(item=item, branch=branch_obj).first()

                    if pricing:
                        pricing_changed = False
                        if unit_retail is not None:
                            pricing.unit_retail_price = unit_retail
                            pricing_changed = True

                        if unit_cost is not None:
                            pricing.unit_cost_price = unit_cost
                            pricing_changed = True

                        has_movements = StockMovement.objects.filter(item=item, branch=branch_obj).exists()

                        if opening_stock is not None and not has_movements:
                            pricing.opening_stock = opening_stock
                            pricing_changed = True

                        # Adjust remaining stock directly without affecting purchase restock movement counts
                        if remaining_stock_val is not None:
                            totals = StockMovement.objects.filter(
                                branch=branch_obj, item=item
                            ).aggregate(
                                stock_in=Coalesce(
                                    Sum("quantity", filter=Q(movement_type__in=[
                                        StockMovement.MovementType.PURCHASE,
                                        StockMovement.MovementType.CUSTOMER_RETURN,
                                        StockMovement.MovementType.TRANSFER_IN,
                                        StockMovement.MovementType.ADJUSTMENT_IN,
                                    ])),
                                    ZERO, output_field=DecimalField()
                                ),
                                stock_out=Coalesce(
                                    Sum("quantity", filter=Q(movement_type__in=[
                                        StockMovement.MovementType.SALE,
                                        StockMovement.MovementType.SUPPLIER_RETURN,
                                        StockMovement.MovementType.TRANSFER_OUT,
                                        StockMovement.MovementType.DAMAGE,
                                        StockMovement.MovementType.ADJUSTMENT_OUT,
                                    ])),
                                    ZERO, output_field=DecimalField()
                                )
                            )
                            net_movements = Decimal(str(totals["stock_in"] or ZERO)) - Decimal(str(totals["stock_out"] or ZERO))
                            pricing.opening_stock = remaining_stock_val - net_movements
                            pricing_changed = True

                        if pricing_changed:
                            pricing.save()

                        _cache_active_pricing(item, pricing)
                    else:
                        pricing = BranchItemPricing.objects.create(
                            item=item,
                            branch=branch_obj,
                            opening_stock=opening_stock or remaining_stock_val or 0,
                            opening_stock_unit_cost_price=unit_cost or Decimal("0.00"),
                            unit_cost_price=unit_cost or Decimal("0.00"),
                            unit_retail_price=unit_retail or Decimal("0.00"),
                        )
                        _cache_active_pricing(item, pricing)

        except IntegrityError as e:
            error_text = str(e).lower()
            if "unique_item_per_retailer" in error_text:
                return Response({"name": "Another item with this name already exists for this retailer."}, status=status.HTTP_400_BAD_REQUEST)
            if "stocks_item_code_key" in error_text or "item.code" in error_text or "code_key" in error_text:
                return Response({"code": "Another item with this code already exists."}, status=status.HTTP_400_BAD_REQUEST)
            return Response({"error": "The item could not be updated because of a database constraint.", "details": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": "Unable to update item.", "details": str(e), "exception_type": type(e).__name__}, status=status.HTTP_400_BAD_REQUEST)

        refetched_item = self._fetch_item_with_pricing(item.id, branch_id) or item
        response_data = self._serialize_item(refetched_item, branch_id=branch_id)

        if branch_obj:
            self._broadcast_websocket_event(
                branch_id=branch_obj.id,
                event_type="ITEM_UPDATED",
                item_id=item.id,
                updated_item=response_data,
            )

        return Response(response_data, status=status.HTTP_200_OK)

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    @action(
        detail=False,
        methods=["post", "patch"],
        url_path="bulk-update",
        permission_classes=[permissions.AllowAny],
    )
    def bulk_update(self, request):
        """
        Batch updates price (cost/retail) or modifies remaining stock across multiple items.
        Modifying remaining stock recalculates opening stock baseline so restock history remains untouched.
        """
        branch_id = self._get_branch_id_from_request(request)
        if not branch_id:
            return Response(
                {"error": "Branch ID is required for bulk update."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        branch_obj = _resolve_branch(branch_id)
        if not branch_obj:
            return Response(
                {"error": "Valid branch is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        items_updates = request.data.get("items", [])
        if not isinstance(items_updates, list) or not items_updates:
            return Response(
                {"error": "A list of item updates is required under key 'items'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated_items = []

        try:
            with transaction.atomic():
                for update_data in items_updates:
                    item_id = update_data.get("id") or update_data.get("item_id")
                    if not item_id:
                        continue

                    pricing = BranchItemPricing.objects.select_for_update().filter(
                        item_id=item_id, branch=branch_obj
                    ).first()

                    if not pricing:
                        continue

                    # Update Retail Price
                    if "unit_retail_price" in update_data and update_data["unit_retail_price"] not in (None, ""):
                        pricing.unit_retail_price = Decimal(str(update_data["unit_retail_price"]))

                    # Update Cost Price
                    if "unit_cost_price" in update_data and update_data["unit_cost_price"] not in (None, ""):
                        pricing.unit_cost_price = Decimal(str(update_data["unit_cost_price"]))

                    # Modify Remaining Stock directly without altering purchase restock history
                    if "remaining_stock" in update_data and update_data["remaining_stock"] not in (None, ""):
                        new_remaining = Decimal(str(update_data["remaining_stock"]))
                        
                        totals = StockMovement.objects.filter(
                            branch=branch_obj, item_id=item_id
                        ).aggregate(
                            stock_in=Coalesce(
                                Sum("quantity", filter=Q(movement_type__in=[
                                    StockMovement.MovementType.PURCHASE,
                                    StockMovement.MovementType.CUSTOMER_RETURN,
                                    StockMovement.MovementType.TRANSFER_IN,
                                    StockMovement.MovementType.ADJUSTMENT_IN,
                                ])),
                                ZERO, output_field=DecimalField()
                            ),
                            stock_out=Coalesce(
                                Sum("quantity", filter=Q(movement_type__in=[
                                    StockMovement.MovementType.SALE,
                                    StockMovement.MovementType.SUPPLIER_RETURN,
                                    StockMovement.MovementType.TRANSFER_OUT,
                                    StockMovement.MovementType.DAMAGE,
                                    StockMovement.MovementType.ADJUSTMENT_OUT,
                                ])),
                                ZERO, output_field=DecimalField()
                            )
                        )
                        net_movements = Decimal(str(totals["stock_in"] or ZERO)) - Decimal(str(totals["stock_out"] or ZERO))
                        pricing.opening_stock = new_remaining - net_movements

                    pricing.save()

                    refetched = self._fetch_item_with_pricing(item_id, branch_obj.id)
                    serialized = self._serialize_item(refetched, branch_id=branch_obj.id)
                    if serialized:
                        updated_items.append(serialized)

            self._broadcast_websocket_event(
                branch_id=branch_obj.id,
                event_type="ITEMS_BULK_UPDATED",
                updated_items=updated_items,
            )

            return Response(
                {"message": "Bulk updates applied successfully.", "items": updated_items},
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            return Response(
                {"error": "Failed to complete bulk update.", "details": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(
        detail=False,
        methods=["post"],
        url_path="rollover-stock",
        permission_classes=[permissions.AllowAny],
    )
    def rollover_stock(self, request):
        branch_id = self._get_branch_id_from_request(request)

        if not branch_id:
            return Response(
                {"error": "Branch ID is required to copy remaining stock to opening stock."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        branch_obj = _resolve_branch(branch_id)
        if not branch_obj:
            return Response(
                {"branch": "The specified branch does not exist."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                pricings = BranchItemPricing.objects.filter(branch=branch_obj).select_related('item')

                # 1. Update opening stock to equal current remaining stock
                updated_pricings = []
                for pricing in pricings:
                    pricing.opening_stock = pricing.remaining_stock
                    updated_pricings.append(pricing)

                if updated_pricings:
                    BranchItemPricing.objects.bulk_update(updated_pricings, fields=['opening_stock'])

                # 2. Archive active stock movements for the previous month
                active_movements = StockMovement.objects.filter(branch=branch_obj)
                previous_month_date = timezone.now().date()

                archive_objects = [
                    MonthlyStockMovementArchive(
                        branch=movement.branch,
                        item=movement.item,
                        movement_type=movement.movement_type,
                        quantity=movement.quantity,
                        unit_cost_price=getattr(movement, 'unit_cost_price', Decimal("0.00")),
                        period_date=previous_month_date,
                        created_at=movement.created_at,
                    )
                    for movement in active_movements
                ]

                if archive_objects:
                    MonthlyStockMovementArchive.objects.bulk_create(archive_objects)

                # 3. Delete active movements so the new month starts fresh (prevents double-counting)
                active_movements.delete()

                # 4. Serialize updated items for response payload
                updated_items = []
                for pricing in updated_pricings:
                    refetched_item = self._fetch_item_with_pricing(
                        pricing.item_id, 
                        branch_id=branch_obj.id
                    )
                    serialized = self._serialize_item(
                        refetched_item, 
                        branch_id=branch_obj.id
                    )
                    if serialized:
                        updated_items.append(serialized)

            self._broadcast_websocket_event(
                branch_id=branch_obj.id,
                event_type="STOCK_ROLLOVER",
                updated_items=updated_items,
            )

            return Response(
                {
                    "message": "Stock successfully rolled over and previous movements archived.",
                    "updated_count": len(updated_pricings),
                    "items": updated_items,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            return Response(
                {
                    "error": "Failed to complete stock rollover.",
                    "details": str(e),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
# ============================================================
# PURCHASES / RESTOCKING
# ============================================================

from stocks.models import Purchase
from .serializers import PurchaseSerializer


def _authenticated_user(request):
    """Utility to retrieve the authenticated user from the request context."""
    return request.user if request and request.user and request.user.is_authenticated else None


def _resolve_branch(branch_param):
    """Utility to resolve a RetailBranch object by ID or name."""
    if not branch_param:
        return None
    if str(branch_param).isdigit():
        return RetailBranch.objects.filter(id=int(branch_param)).first()
    return RetailBranch.objects.filter(name__iexact=str(branch_param).strip()).first()


def _parse_business_date(date_val):
    """Utility to parse or format input values into proper datetime/date formats."""
    if not date_val:
        return timezone.now()
    if isinstance(date_val, str):
        try:
            return timezone.datetime.fromisoformat(date_val.replace("Z", "+00:00"))
        except ValueError:
            return timezone.now()
    return date_val


class PurchaseViewSet(viewsets.ModelViewSet):
    """
    ViewSet for recording, viewing, and managing inventory purchases/restocks.
    Handles single and batch purchase entries, payment methods, supplier linkages,
    and automated Weighted Average Cost (WAC) updates.
    """
    serializer_class = PurchaseSerializer
    permission_classes = [permissions.AllowAny]

    def _broadcast_websocket_event(
        self,
        branch_id,
        event_type,
        purchase_id=None,
        item_id=None,
    ):
        if not branch_id:
            return

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        payload = {
            "type": "analytics_update",
            "event": event_type,
        }

        if purchase_id is not None:
            payload["purchase_id"] = purchase_id

        if item_id is not None:
            payload["item_id"] = item_id

        try:
            async_to_sync(channel_layer.group_send)(
                f"analytics_branch_{branch_id}",
                payload,
            )
        except Exception:
            pass

    def _align_retailer(self, item, branch, supplier=None):
        if not branch:
            return

        target_retailer = branch.retailer or (item.retailer if item else None)
        user = _authenticated_user(self.request)

        if not target_retailer and user:
            target_retailer = user

        if target_retailer:
            if item and item.retailer_id != target_retailer.id:
                item.retailer = target_retailer
                item.save(update_fields=["retailer"])

            if branch.retailer_id != target_retailer.id:
                branch.retailer = target_retailer
                branch.save(update_fields=["retailer"])

            if supplier and hasattr(supplier, "retailer_id") and supplier.retailer_id != target_retailer.id:
                supplier.retailer = target_retailer
                supplier.save(update_fields=["retailer"])

    def get_queryset(self):
        queryset = (
            Purchase.objects
            .select_related(
                "item",
                "branch",
                "supplier",
                "created_by",
            )
            .all()
            .order_by("-purchase_date", "-id")
        )

        user = _authenticated_user(self.request)
        if user:
            queryset = queryset.filter(
                Q(branch__retailer=user) | Q(item__retailer=user)
            )

        branch_param = (
            self.request.query_params.get("branch")
            or self.request.query_params.get("branch_id")
        )

        if branch_param:
            if str(branch_param).isdigit():
                queryset = queryset.filter(
                    Q(branch_id=int(branch_param))
                    | Q(branch__name__iexact=str(branch_param))
                )
            else:
                queryset = queryset.filter(
                    branch__name__iexact=str(branch_param)
                )

        item_id = self.request.query_params.get("item") or self.request.query_params.get("item_id")
        if item_id and str(item_id).isdigit():
            queryset = queryset.filter(item_id=int(item_id))

        supplier_id = self.request.query_params.get("supplier") or self.request.query_params.get("supplier_id")
        if supplier_id and str(supplier_id).isdigit():
            queryset = queryset.filter(supplier_id=int(supplier_id))

        payment_method = self.request.query_params.get("payment_method")
        if payment_method:
            queryset = queryset.filter(payment_method__iexact=payment_method.strip())

        return queryset

    def create(self, request, *args, **kwargs):
        is_batch = isinstance(request.data, list)
        items_payload = request.data if is_batch else [request.data]

        if not items_payload:
            return Response(
                {"error": "At least one purchase record is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created_purchases = []
        user = _authenticated_user(request)

        try:
            with transaction.atomic():
                for data in items_payload:
                    item_id = data.get("item") or data.get("item_id")
                    if not item_id:
                        continue

                    branch_param = (
                        data.get("branch")
                        or data.get("branch_id")
                        or self.request.query_params.get("branch")
                        or self.request.query_params.get("branch_id")
                    )

                    quantity = Decimal(str(data.get("quantity", 0) or 0))
                    if quantity <= ZERO:
                        continue

                    unit_cost_raw = (
                        data.get("unit_cost_price")
                        or data.get("cost_price")
                        or data.get("unit_cost")
                    )
                    unit_cost = Decimal(
                        str(unit_cost_raw if unit_cost_raw not in (None, "") else "0.00")
                    )

                    purchase_date = _parse_business_date(
                        data.get("purchase_date")
                        or data.get("date")
                        or data.get("transaction_date")
                    )

                    payment_method = (
                        str(data.get("payment_method") or "CASH")
                        .strip()
                        .upper()
                        .replace(" ", "_")
                        .replace("-", "_")
                    )

                    supplier_id = data.get("supplier") or data.get("supplier_id")
                    supplier_obj = None
                    if supplier_id:
                        if str(supplier_id).isdigit():
                            supplier_obj = Supplier.objects.filter(id=int(supplier_id)).first()
                        else:
                            supplier_obj = Supplier.objects.filter(name__iexact=str(supplier_id).strip()).first()

                    item = Item.objects.select_for_update().get(id=item_id)
                    branch = _resolve_branch(branch_param)

                    if not branch:
                        pricing = BranchItemPricing.objects.filter(item=item).first()
                        if pricing:
                            branch = pricing.branch

                    if not branch:
                        continue

                    self._align_retailer(item, branch, supplier_obj)

                    BranchItemPricing.objects.get_or_create(
                        item=item,
                        branch=branch,
                        defaults={
                            "opening_stock": ZERO,
                            "unit_cost_price": unit_cost,
                            "unit_selling_price": ZERO,
                        },
                    )

                    supplier_name = (
                        data.get("supplier_name")
                        or (supplier_obj.name if supplier_obj else "")
                    )

                    purchase = Purchase(
                        item=item,
                        branch=branch,
                        supplier=supplier_obj,
                        supplier_name=supplier_name,
                        quantity=quantity,
                        unit_cost_price=unit_cost,
                        payment_method=payment_method,
                        purchase_date=purchase_date,
                        created_by=user,
                    )
                    purchase.save()

                    recalculate_wac_sales_cost(branch, item)
                    created_purchases.append(purchase)

                if not created_purchases:
                    return Response(
                        {"error": "No valid purchase entries were processed."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                self._broadcast_websocket_event(
                    branch_id=created_purchases[0].branch_id,
                    event_type="PURCHASE_RECORDED",
                    purchase_id=created_purchases[0].id,
                    item_id=created_purchases[0].item_id,
                )

                if is_batch:
                    serializer = self.get_serializer(created_purchases, many=True)
                    return Response(serializer.data, status=status.HTTP_201_CREATED)
                else:
                    serializer = self.get_serializer(created_purchases[0])
                    response_data = serializer.data
                    response_data["calculated_wac"] = str(
                        recalculate_wac_sales_cost(
                            created_purchases[0].branch,
                            created_purchases[0].item
                        )
                    )
                    return Response(response_data, status=status.HTTP_201_CREATED)

        except Item.DoesNotExist:
            return Response(
                {"error": "Item not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except DjangoValidationError as e:
            return Response(
                e.message_dict if hasattr(e, "message_dict") else {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except DRFValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {
                    "error": "Purchase creation failed.",
                    "details": str(e),
                    "exception_type": type(e).__name__,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    def destroy(self, request, *args, **kwargs):
        """
        Deletes a purchase record, reversing its StockBatch, StockMovement, 
        and Expense entries via Purchase.delete(), then triggers WAC recalculation.
        """
        instance = self.get_object()
        branch = instance.branch
        item = instance.item

        instance.delete()

        self._broadcast_websocket_event(
            branch_id=branch.id,
            event_type="PURCHASE_DELETED",
            item_id=item.id,
        )

        return Response(status=status.HTTP_204_NO_CONTENT)
# ============================================================
# STOCK ADJUSTMENT
# ============================================================




# ============================================================
# STOCK MOVEMENT
# ============================================================

class StockMovementViewSet(viewsets.ModelViewSet):
    serializer_class = StockMovementSerializer
    permission_classes = [permissions.AllowAny]

    def _broadcast_websocket_event(
        self,
        branch_id,
        event_type,
        movement_id=None,
        item_id=None,
        updated_item=None,
    ):
        if not branch_id:
            return

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        payload = {
            "type": "analytics_update",
            "event": event_type,
        }

        if movement_id is not None:
            payload["movement_id"] = movement_id
        if item_id is not None:
            payload["item_id"] = item_id
        if updated_item is not None:
            payload["updated_item"] = updated_item

        try:
            async_to_sync(channel_layer.group_send)(
                f"analytics_branch_{branch_id}",
                payload,
            )
        except Exception:
            pass

    def get_queryset(self):
        queryset = (
            StockMovement.objects
            .select_related("item", "created_by", "branch", "batch")
            .all()
            .order_by("-id")
        )

        user = _authenticated_user(self.request)
        if user:
            queryset = queryset.filter(item__retailer=user)

        branch_param = (
            self.request.query_params.get("branch")
            or self.request.query_params.get("branch_id")
        )

        if branch_param:
            if str(branch_param).isdigit():
                queryset = queryset.filter(
                    Q(branch_id=int(branch_param))
                    | Q(branch__name__iexact=str(branch_param))
                )
            else:
                queryset = queryset.filter(
                    branch__name__iexact=str(branch_param)
                )

        return queryset

    @action(
        detail=False,
        methods=["get"],
        url_path="activity-history",
        permission_classes=[permissions.AllowAny],
    )
    def activity_history(self, request):
        branch_param = (
            request.query_params.get("branch")
            or request.query_params.get("branch_id")
        )

        movements = (
            StockMovement.objects
            .select_related("item", "branch")
            .all()
            .order_by("-id")
        )

        user = _authenticated_user(request)
        if user:
            movements = movements.filter(item__retailer=user)

        if branch_param:
            if str(branch_param).isdigit():
                branch_filter = (
                    Q(branch_id=int(branch_param))
                    | Q(branch__name__iexact=str(branch_param))
                )
            else:
                branch_filter = Q(branch__name__iexact=str(branch_param))

            movements = movements.filter(branch_filter)

        history_items = []

        for movement in movements:
            raw_date = (
                getattr(movement, "date", None)
                or getattr(movement, "created_at", None)
            )

            date_str = (
                raw_date.isoformat()
                if isinstance(raw_date, (datetime, date))
                else (str(raw_date) if raw_date else "")
            )

            item_name = movement.item.name if movement.item else "Unknown"
            ref_val = getattr(movement, "reference", None) or "-"
            notes_val = getattr(movement, "notes", "") or "-"

            movement_display = (
                movement.get_movement_type_display()
                if hasattr(movement, "get_movement_type_display")
                else str(movement.movement_type)
            )

            history_items.append(
                {
                    "id": f"movement_{movement.id}",
                    "date": date_str,
                    "created_at": date_str,
                    "item": item_name,
                    "item_name": item_name,
                    "activity_type": movement_display,
                    "movement_type_display": movement_display,
                    "quantity": movement.quantity,
                    "quantity_level": movement.quantity,
                    "unit_cost_price": (
                        str(movement.unit_cost_price)
                        if getattr(movement, "unit_cost_price", None) is not None
                        else "0.00"
                    ),
                    "reference": ref_val,
                    "target": ref_val,
                    "notes": notes_val,
                }
            )

        return Response(history_items, status=status.HTTP_200_OK)

    def create(self, request, *args, **kwargs):
        if not isinstance(request.data, dict):
            return Response(
                {"error": "Expected a request body object."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = request.data.copy()

        item_id = data.get("item") or data.get("item_id")
        branch_param = (
            data.get("branch")
            or data.get("branch_id")
            or request.query_params.get("branch")
            or request.query_params.get("branch_id")
        )

        try:
            quantity = int(data.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            return Response(
                {"error": "Quantity must be a valid integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if quantity <= 0:
            return Response(
                {"error": "Quantity must be greater than zero."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not item_id:
            return Response(
                {"error": "Item ID is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                item = Item.objects.select_for_update().get(id=item_id)
                branch = _resolve_branch(branch_param)

                if not branch:
                    pricing = BranchItemPricing.objects.filter(item=item).first()
                    if pricing:
                        branch = pricing.branch

                if not branch:
                    return Response(
                        {"error": "Valid Branch is required for stock movements."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                branch_id = branch.id
                target_retailer = branch.retailer or getattr(item, "retailer", None)
                user = _authenticated_user(request)

                if target_retailer:
                    if getattr(item, "retailer_id", None) != target_retailer.id:
                        item.retailer = target_retailer
                        item.save(update_fields=["retailer"])

                    if getattr(branch, "retailer_id", None) != target_retailer.id:
                        branch.retailer = target_retailer
                        branch.save(update_fields=["retailer"])

                # Normalize incoming movement_type / adjustment_type / activity_type
                raw_movement = str(
                    data.get("movement_type") or data.get("activity_type") or ""
                ).strip().upper()

                raw_adjustment = str(
                    data.get("adjustment_type") or ""
                ).strip().upper()

                # Robust mapping strategy to enum values
                if raw_adjustment in {"IN", "INCREASE", "RESTOCK", "ADD", "INCREASE STOCK (+)"}:
                    movement_type = StockMovement.MovementType.ADJUSTMENT_IN
                elif raw_adjustment in {"OUT", "DECREASE", "REMOVE", "DECREASE STOCK (-)"}:
                    movement_type = StockMovement.MovementType.ADJUSTMENT_OUT
                elif raw_movement in {"STOCK_IN", "ADJUSTMENT_IN", "INCREASE", "RESTOCK", "IN", "ADD", "INCREASE STOCK (+)"}:
                    movement_type = getattr(StockMovement.MovementType, "STOCK_IN", StockMovement.MovementType.ADJUSTMENT_IN)
                elif raw_movement in {"STOCK_OUT", "ADJUSTMENT_OUT", "DECREASE", "OUT", "REMOVE", "DECREASE STOCK (-)"}:
                    movement_type = getattr(StockMovement.MovementType, "STOCK_OUT", StockMovement.MovementType.ADJUSTMENT_OUT)
                elif raw_movement in {"PURCHASE", "BUY", "OPENING_STOCK"}:
                    movement_type = raw_movement
                elif not raw_movement:
                    movement_type = StockMovement.MovementType.PURCHASE
                else:
                    movement_type = raw_movement

                # Fallback check against model enum choices
                valid_movement_types = {
                    choice[0] for choice in StockMovement.MovementType.choices
                }

                if movement_type not in valid_movement_types:
                    movement_type = StockMovement.MovementType.ADJUSTMENT_IN

                # Retrieve or instantiate pricing record
                branch_pricing, _ = (
                    BranchItemPricing.objects
                    .select_for_update()
                    .get_or_create(item=item, branch=branch)
                )

                if callable(globals().get("_cache_active_pricing")):
                    _cache_active_pricing(item, branch_pricing)

                unit_cost_raw = (
                    data.get("unit_cost_price")
                    or data.get("cost_price")
                    or data.get("unit_cost")
                )

                if unit_cost_raw in (None, ""):
                    unit_cost = (
                        branch_pricing.unit_cost_price
                        if (branch_pricing and branch_pricing.unit_cost_price is not None)
                        else Decimal("0.00")
                    )
                else:
                    try:
                        unit_cost = Decimal(str(unit_cost_raw))
                    except Exception:
                        return Response(
                            {"error": "unit_cost_price must be a valid number."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )

                if unit_cost < 0:
                    return Response(
                        {"error": "unit_cost_price cannot be negative."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                movement_date = _parse_business_date(
                    data.get("date")
                    or data.get("transaction_date")
                    or data.get("movement_date")
                )

                reference_val = (
                    data.get("reference")
                    or (
                        "MANUAL_RESTOCK"
                        if movement_type in {
                            StockMovement.MovementType.PURCHASE,
                            StockMovement.MovementType.ADJUSTMENT_IN,
                            getattr(StockMovement.MovementType, "STOCK_IN", "STOCK_IN"),
                        }
                        else "MANUAL_ADJUSTMENT"
                    )
                )

                notes_val = data.get("notes") or data.get("reason") or ""

                stock_movement = StockMovement.objects.create(
                    item=item,
                    branch=branch,
                    movement_type=movement_type,
                    quantity=quantity,
                    unit_cost_price=unit_cost,
                    date=movement_date,
                    reference=reference_val,
                    notes=notes_val,
                    created_by=user,
                )

                # DO NOT update opening_stock unless explicitly setting an OPENING_STOCK movement!
                if movement_type == getattr(StockMovement.MovementType, "OPENING_STOCK", "OPENING_STOCK"):
                    branch_pricing.opening_stock = quantity
                    branch_pricing.save()

                new_wac = (
                    _refresh_pricing_wac(item, branch)
                    if callable(globals().get("_refresh_pricing_wac"))
                    else Decimal("0.00")
                )

                # Isolated item serialization
                serialized_item = None
                try:
                    refetched_item = (
                        Item.objects
                        .select_related("retailer")
                        .filter(id=item.id)
                        .first()
                    )
                    if refetched_item:
                        item_context = {
                            "request": request,
                            "branch_id": branch_id,
                            "format": self.format_kwarg,
                            "view": self,
                        }
                        serialized_item = ItemSerializer(
                            refetched_item, context=item_context
                        ).data
                except Exception:
                    serialized_item = {"id": item.id, "name": item.name}

                self._broadcast_websocket_event(
                    branch_id=branch_id,
                    event_type="STOCK_ADDED",
                    movement_id=stock_movement.id,
                    item_id=item.id,
                    updated_item=serialized_item,
                )

                serializer = self.get_serializer(stock_movement)
                response_data = serializer.data
                response_data["calculated_wac"] = str(new_wac)
                response_data["remaining_stock"] = getattr(
                    branch_pricing, "remaining_stock", None
                )

                return Response(
                    response_data,
                    status=status.HTTP_201_CREATED,
                )

        except Item.DoesNotExist:
            return Response(
                {"error": "Item not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except DjangoValidationError as e:
            return Response(
                e.message_dict if hasattr(e, "message_dict") else {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except DRFValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {
                    "error": "Stock movement failed.",
                    "details": str(e),
                    "exception_type": type(e).__name__,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


# ============================================================
# SALES
# ============================================================

class SaleTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = SaleTransactionSerializer
    permission_classes = [permissions.AllowAny]

    PAYMENT_METHODS = [
        {"code": "CASH", "label": "Cash"},
        {"code": "MPESA", "label": "M-Pesa"},
        {"code": "PAYBILL", "label": "Paybill"},
        {"code": "SEND_MONEY", "label": "Send Money"},
        {"code": "BANK_TRANSFER", "label": "Bank Transfer"},
        {"code": "CREDIT", "label": "Credit"},
        {"code": "SPLIT", "label": "Split Payment"},
    ]

    def _normalize_sale_date(self, raw_date):
        if raw_date in (None, ""):
            return timezone.now()

        tz = timezone.get_current_timezone()

        if isinstance(raw_date, datetime):
            return timezone.make_aware(raw_date, tz) if timezone.is_naive(raw_date) else raw_date

        if isinstance(raw_date, date):
            current_time = (
                timezone.localtime().time()
                if raw_date == timezone.localdate()
                else datetime.min.time()
            )
            return timezone.make_aware(datetime.combine(raw_date, current_time), tz)

        if isinstance(raw_date, str):
            raw_date = raw_date.strip()

            try:
                parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                return timezone.make_aware(parsed, tz) if timezone.is_naive(parsed) else parsed
            except ValueError:
                pass

            try:
                parsed_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
                current_time = (
                    timezone.localtime().time()
                    if parsed_date == timezone.localdate()
                    else datetime.min.time()
                )
                return timezone.make_aware(datetime.combine(parsed_date, current_time), tz)
            except ValueError:
                raise DRFValidationError({
                    "sale_date": "Invalid date format. Use YYYY-MM-DD or ISO datetime format."
                })

        raise DRFValidationError({"sale_date": "Invalid sale date value."})

    def _normalize_payment_method(self, raw_method):
        if not raw_method:
            return "CASH"

        normalized = str(raw_method).strip().upper()
        normalized = normalized.replace("-", "_").replace(" ", "_")
        normalized = normalized.replace("/", "_").replace("\\", "_")
        normalized = "_".join(part for part in normalized.split("_") if part)

        mappings = {
            ("CASH", "CASH_PAYMENT"): "CASH",
            ("MPESA", "M_PESA", "MOBILE_MONEY", "MOBILE_PAYMENT", "M_PESA_MONEY"): "MPESA",
            ("PAYBILL", "M_PESA_PAYBILL", "MPESA_PAYBILL", "M_PESA_PAY_BILL", "MPESA_PAY_BILL"): "PAYBILL",
            ("SEND_MONEY", "M_PESA_SEND_MONEY", "MPESA_SEND_MONEY", "SENDMONEY", "SEND_MONEY_MPESA", "MPESA_SENDMONEY"): "SEND_MONEY",
            ("BANK_TRANSFER", "BANK", "TRANSFER", "BANK_PAYMENT"): "BANK_TRANSFER",
            ("CREDIT", "ON_CREDIT", "CREDIT_SALE"): "CREDIT",
            ("SPLIT", "SPLIT_PAYMENT", "SPLIT_PAYMENTS", "MULTIPLE"): "SPLIT",
        }

        for keys, target in mappings.items():
            if normalized in keys:
                return target

        if "PAYBILL" in normalized or "PAY_BILL" in normalized:
            return "PAYBILL"
        if "SEND_MONEY" in normalized or "SENDMONEY" in normalized:
            return "SEND_MONEY"
        if "BANK" in normalized or "TRANSFER" in normalized:
            return "BANK_TRANSFER"
        if "CREDIT" in normalized:
            return "CREDIT"
        if "CASH" in normalized:
            return "CASH"
        if "MPESA" in normalized or "M_PESA" in normalized or "MOBILE" in normalized:
            return "MPESA"

        return normalized

    def get_queryset(self):
        queryset = (
            SaleTransaction.objects
            .select_related("branch", "customer")
            .prefetch_related("items__item", "split_payments")
            .all()
            .order_by("-sale_date")
        )

        branch_param = (
            self.request.query_params.get("branch")
            or self.request.query_params.get("branch_id")
        )
        payment_method = self.request.query_params.get("payment_method")
        customer_id = self.request.query_params.get("customer")

        if branch_param and str(branch_param).lower() != "all":
            if str(branch_param).isdigit():
                queryset = queryset.filter(branch_id=int(branch_param))
            else:
                queryset = queryset.filter(branch__name__iexact=str(branch_param).strip())

        if payment_method:
            normalized_pm = self._normalize_payment_method(payment_method)
            pm_filters = {
                "CASH": Q(payment_method__iexact="CASH") | Q(split_payments__method__iexact="CASH"),
                "MPESA": Q(payment_method__iexact="MPESA") | Q(payment_method__iexact="M-PESA") | Q(payment_method__iexact="MOBILE_MONEY") | Q(split_payments__method__iexact="MPESA"),
                "PAYBILL": Q(payment_method__iexact="PAYBILL") | Q(split_payments__method__iexact="PAYBILL"),
                "SEND_MONEY": Q(payment_method__iexact="SEND_MONEY") | Q(payment_method__iexact="SEND MONEY") | Q(split_payments__method__iexact="SEND_MONEY"),
                "BANK_TRANSFER": Q(payment_method__iexact="BANK_TRANSFER") | Q(payment_method__iexact="BANK") | Q(payment_method__iexact="TRANSFER") | Q(split_payments__method__iexact="BANK_TRANSFER"),
                "CREDIT": Q(payment_method__iexact="CREDIT") | Q(payment_method__iexact="ON_CREDIT") | Q(payment_method__iexact="CREDIT SALE") | Q(split_payments__method__iexact="CREDIT"),
                "SPLIT": Q(payment_method__iexact="SPLIT"),
            }
            queryset = queryset.filter(pm_filters.get(normalized_pm, Q(payment_method__iexact=payment_method))).distinct()

        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        return queryset


class SupplierPaymentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        purchase_id = request.data.get("purchase")
        amount = Decimal(str(request.data.get("amount", 0)))
        payment_method = request.data.get("payment_method", "CASH")
        notes = request.data.get("notes", "")

        try:
            with transaction.atomic():
                purchase = Purchase.objects.select_for_update().get(id=purchase_id)
                purchase.amount_paid = Decimal(str(getattr(purchase, "amount_paid", 0))) + amount
                purchase.save()

                # Record an expense entry for ledger reconciliation
                Expense.objects.create(
                    branch=purchase.branch,
                    title=f"Supplier Repayment: {purchase.item.name}",
                    amount=amount,
                    payment_method=payment_method,
                    description=notes,
                    date=timezone.localdate(),
                )

                return Response({"message": "Supplier payment recorded successfully"}, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

# EXPENSE


class ExpenseViewSet(viewsets.ModelViewSet):
    serializer_class = ExpenseSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        queryset = (
            Expense.objects
            .select_related("branch")
            .all()
            .order_by("-date", "-id")
        )

        branch_param = (
            self.request.query_params.get("branch")
            or self.request.query_params.get("branch_id")
        )

        if branch_param and str(branch_param).lower() != "all":
            if str(branch_param).isdigit():
                queryset = queryset.filter(branch_id=int(branch_param))
            else:
                queryset = queryset.filter(branch__name__iexact=str(branch_param).strip())

        return queryset

    def perform_create(self, serializer):
        user = _authenticated_user(self.request)

        data = (
            self.request.data
            if isinstance(self.request.data, dict)
            else {}
        )

        branch_param = (
            data.get("branch")
            or data.get("branch_id")
            or self.request.query_params.get("branch")
            or self.request.query_params.get("branch_id")
        )

        validated_date = serializer.validated_data.get("date")

        if isinstance(validated_date, datetime):
            validated_date = validated_date.date()

        save_kwargs = {
            "date": (validated_date or timezone.localdate())
        }

        if user:
            save_kwargs["retailer"] = user

        if branch_param and str(branch_param).lower() != "all":
            if str(branch_param).isdigit():
                save_kwargs["branch_id"] = int(branch_param)
            else:
                branch_obj = (
                    RetailBranch.objects
                    .filter(name__iexact=str(branch_param).strip())
                    .first()
                )
                if branch_obj:
                    save_kwargs["branch"] = branch_obj

        try:
            expense = serializer.save(**save_kwargs)
        except DjangoValidationError as e:
            raise DRFValidationError(
                e.message_dict if hasattr(e, "message_dict") else str(e)
            )

        target_branch_id = expense.branch_id

        if target_branch_id:
            channel_layer = get_channel_layer()
            if channel_layer:
                try:
                    async_to_sync(channel_layer.group_send)(
                        f"analytics_branch_{target_branch_id}",
                        {
                            "type": "analytics_update",
                            "event": "EXPENSE_RECORDED",
                        },
                    )
                except Exception:
                    pass

    @action(
        detail=False,
        methods=["get"],
        url_path="summary",
    )
    def summary(self, request):
        queryset = self.get_queryset()

        total_amount = sum(
            (expense.amount for expense in queryset),
            Decimal("0.00"),
        )

        return Response(
            {
                "count": queryset.count(),
                "total_amount": float(total_amount),
            },
            status=status.HTTP_200_OK,
        )


from apis.serializers import PendingApprovalSerializer, RejectionReasonSerializer
from apis.emails import send_management_review_email, send_submitter_status_email


from django.db import transaction
from apis.tasks import (
    send_management_review_email_task,
    send_submitter_status_email_task,
)

from .emails import send_management_review_email, send_submitter_status_email


signer = TimestampSigner()


class PendingApprovalViewSet(viewsets.ModelViewSet):
    queryset = PendingApproval.objects.all().order_by("-created_at")
    serializer_class = PendingApprovalSerializer
    permission_classes = [permissions.IsAuthenticated]

    def create(self, request, *args, **kwargs):
        """
        Create a new pending approval request (e.g., NEW_REGISTERED_ITEMS, SALE_ENTRY, BRANCH_CREATION, RESTOCK_PURCHASE, STOCK_OPERATION).
        """
        request_type = request.data.get(
            "request_type", 
            getattr(PendingApproval.RequestType, "NEW_REGISTERED_ITEMS", "NEW_REGISTERED_ITEMS")
        )
        payload = request.data.get("payload", request.data)

        approval = PendingApproval.objects.create(
            request_type=request_type,
            payload=payload,
            submitted_by=request.user,
            status=PendingApproval.Status.PENDING,
        )

        send_management_review_email(approval)

        serializer = self.get_serializer(approval)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        """
        Overrides DRF update (PATCH/PUT) so that editing & resending or changing status
        back to PENDING triggers the review notification email.
        """
        instance = serializer.save()

        if instance.status == PendingApproval.Status.PENDING:
            send_management_review_email(instance)

    def perform_destroy(self, instance):
        """
        Custom handling prior to deleting a pending or rejected approval request.
        """
        instance.delete()

    # ------------------------------------------------------------------
    # AUTHENTICATED DASHBOARD ACTIONS
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"])
    def resend(self, request, pk=None):
        approval = self.get_object()

        if approval.status == PendingApproval.Status.APPROVED:
            return Response(
                {"error": "Cannot resend a request that has already been approved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if approval.status == PendingApproval.Status.REJECTED:
            approval.status = PendingApproval.Status.PENDING
            approval.rejection_reason = ""
            approval.save()

        send_management_review_email(approval)

        return Response(
            {"message": f"Approval request #{approval.id} resent to management successfully."},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        approval = self.get_object()

        if approval.status != PendingApproval.Status.PENDING:
            return Response(
                {"error": f"Request already processed with status '{approval.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            self._process_approval(approval, reviewer=request.user)
            return Response(
                {"message": "Request approved and committed to database successfully."},
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            return Response(
                {"error": "Failed to commit record to live models.", "details": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        approval = self.get_object()

        if approval.status != PendingApproval.Status.PENDING:
            return Response(
                {"error": f"Request already processed with status '{approval.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = request.data.get("reason", "No reason provided.")
        self._process_rejection(approval, reviewer=request.user, reason=reason)

        return Response(
            {"message": "Request rejected successfully."},
            status=status.HTTP_200_OK,
        )

    def get_queryset(self):
        """
        Custom query filtering:
        - Admins / Superusers / Management see ALL approval requests.
        - Regular non-staff users only see requests submitted by non-staff/regular users.
        """
        user = self.request.user
        queryset = PendingApproval.objects.all().order_by("-created_at")

        if user.is_staff or user.is_superuser:
            return queryset

        return queryset.filter(
            submitted_by__is_staff=False,
            submitted_by__is_superuser=False
        )

    # ------------------------------------------------------------------
    # ONE-CLICK EMAIL ACTIONS (NO LOGIN REQUIRED)
    # ------------------------------------------------------------------

    @action(detail=True, methods=["get"], permission_classes=[permissions.AllowAny])
    def approve_via_email(self, request, pk=None):
        token = request.query_params.get("token")
        
        try:
            valid_id = signer.unsign(token, max_age=604800)
            if str(valid_id) != str(pk):
                return HttpResponse("<h1>Error:</h1><p>Invalid authorization token.</p>", status=400)
        except (BadSignature, SignatureExpired):
            return HttpResponse("<h1>Link Expired</h1><p>This approval link is invalid or has expired.</p>", status=400)

        approval = PendingApproval.objects.filter(pk=pk).first()
        if not approval:
            return HttpResponse("<h1>Error:</h1><p>Approval request not found.</p>", status=400)

        if approval.status != PendingApproval.Status.PENDING:
            return HttpResponse(
                f"<h1>Notice</h1><p>This request has already been processed (Current Status: <strong>{approval.status}</strong>).</p>",
                status=400,
            )

        try:
            self._process_approval(approval, reviewer=None)
            return HttpResponse(
                "<div style='font-family:sans-serif; text-align:center; padding:50px;'>"
                "<h1 style='color:#059669;'>✅ Request Approved!</h1>"
                f"<p>Request #{approval.id} has been committed to live records and the submitter was notified.</p>"
                "</div>"
            )
        except Exception as e:
            return HttpResponse(f"<h1>Commit Failed</h1><p>Error: {str(e)}</p>", status=400)

    @action(detail=True, methods=["get"], permission_classes=[permissions.AllowAny])
    def reject_via_email(self, request, pk=None):
        token = request.query_params.get("token")

        try:
            valid_id = signer.unsign(token, max_age=604800)
            if str(valid_id) != str(pk):
                return HttpResponse("<h1>Error:</h1><p>Invalid authorization token.</p>", status=400)
        except (BadSignature, SignatureExpired):
            return HttpResponse("<h1>Link Expired</h1><p>This link is invalid or has expired.</p>", status=400)

        approval = PendingApproval.objects.filter(pk=pk).first()
        if not approval:
            return HttpResponse("<h1>Error:</h1><p>Approval request not found.</p>", status=400)

        if approval.status != PendingApproval.Status.PENDING:
            return HttpResponse(
                f"<h1>Notice</h1><p>This request has already been processed (Current Status: <strong>{approval.status}</strong>).</p>",
                status=400,
            )

        self._process_rejection(approval, reviewer=None, reason="Rejected directly via management email link.")
        
        return HttpResponse(
            "<div style='font-family:sans-serif; text-align:center; padding:50px;'>"
            "<h1 style='color:#dc2626;'>❌ Request Rejected</h1>"
            f"<p>Request #{approval.id} has been marked as REJECTED.</p>"
            "</div>"
        )

    # ------------------------------------------------------------------
    # INTERNAL PROCESSING LOGIC
    # ------------------------------------------------------------------

    def _process_approval(self, approval, reviewer=None):
        """Core transactional logic for handling approval."""
        with transaction.atomic():
            req_type = approval.request_type

            if req_type in [
                getattr(PendingApproval.RequestType, "NEW_REGISTERED_ITEMS", "NEW_REGISTERED_ITEMS"),
                getattr(PendingApproval.RequestType, "NEW_ITEMS", "NEW_ITEMS"),
            ]:
                self._commit_new_registered_items(approval.payload)

            elif req_type == getattr(PendingApproval.RequestType, "BRANCH_CREATION", "BRANCH_CREATION"):
                self._commit_branch_creation(approval.payload)

            elif req_type == getattr(PendingApproval.RequestType, "SALE_ENTRY", "SALE_ENTRY"):
                self._commit_sale_entry(approval.payload)

            elif req_type in [
                getattr(PendingApproval.RequestType, "RESTOCK_PURCHASE", "RESTOCK_PURCHASE"),
                getattr(PendingApproval.RequestType, "PURCHASE_ORDER", "PURCHASE_ORDER"),
            ]:
                self._commit_restock_purchase(approval.payload)

            elif req_type == getattr(PendingApproval.RequestType, "STOCK_OPERATION", "STOCK_OPERATION"):
                self._commit_stock_operation(approval.payload)

            approval.status = PendingApproval.Status.APPROVED
            if reviewer:
                approval.reviewed_by = reviewer
            approval.save()

            transaction.on_commit(
                lambda: send_submitter_status_email(approval, approved=True)
            )

    def _process_rejection(self, approval, reviewer=None, reason="No reason provided."):
        approval.status = PendingApproval.Status.REJECTED
        if reviewer:
            approval.reviewed_by = reviewer
        approval.rejection_reason = reason
        approval.save()

        send_submitter_status_email(approval, approved=False, reason=reason)

    # ------------------------------------------------------------------
    # COMMIT HELPER METHODS
    # ------------------------------------------------------------------

    def _get_target_branch(self, payload):
        branch_identifier = (
            payload.get("target_branch") 
            or payload.get("branch_id") 
            or payload.get("branch")
        )
        if not branch_identifier:
            return RetailBranch.objects.first()

        if isinstance(branch_identifier, dict):
            b_id = branch_identifier.get("id")
            b_name = branch_identifier.get("name")
            return RetailBranch.objects.filter(id=b_id).first() or RetailBranch.objects.filter(name__iexact=b_name).first()
        elif str(branch_identifier).isdigit():
            return RetailBranch.objects.filter(id=int(branch_identifier)).first()
        else:
            return RetailBranch.objects.filter(name__iexact=str(branch_identifier).strip()).first()

    def _commit_restock_purchase(self, payload):
        """
        Commits approved restock purchase orders to StockMovement and updates pricing.
        """
        purchases = payload.get("purchases", [])
        if not purchases and "item" in payload:
            purchases = [payload]

        branch = self._get_target_branch(payload)
        if not branch:
            raise ValueError("No valid branch found to commit restock purchase order.")

        for p_data in purchases:
            item_id = p_data.get("item") or p_data.get("item_id")
            item_obj = Item.objects.filter(id=item_id).first()
            if not item_obj:
                continue

            quantity = int(p_data.get("quantity", 0))
            cost_price = Decimal(str(p_data.get("unit_cost_price") or "0.00"))
            payment_method = p_data.get("payment_method", "CASH")

            # Create purchase movement record if StockMovement model is present
            if 'StockMovement' in globals():
                StockMovement.objects.create(
                    branch=branch,
                    item=item_obj,
                    movement_type="PURCHASE",
                    quantity=quantity,
                    unit_cost_price=cost_price,
                    payment_method=payment_method,
                )

            # Increase branch stock and update cost price
            pricing, _ = BranchItemPricing.objects.get_or_create(
                branch=branch,
                item=item_obj,
                defaults={"remaining_stock": 0, "unit_cost_price": cost_price}
            )
            pricing.remaining_stock += quantity
            pricing.unit_cost_price = cost_price
            pricing.save()

    def _commit_stock_operation(self, payload):
        """
        Commits batch inline stock modifications and pricing updates.
        """
        items_data = payload.get("items", [])
        branch = self._get_target_branch(payload)

        if not branch:
            raise ValueError("No valid branch found to perform stock operations.")

        for item_data in items_data:
            item_id = item_data.get("id") or item_data.get("item_id")
            pricing = BranchItemPricing.objects.filter(branch=branch, item_id=item_id).first()

            if not pricing:
                continue

            if "unit_retail_price" in item_data and str(item_data["unit_retail_price"]).strip() != "":
                unit_retail = Decimal(str(item_data["unit_retail_price"]))
                if hasattr(pricing, "unit_retail_price"):
                    pricing.unit_retail_price = unit_retail
                if hasattr(pricing, "unit_selling_price"):
                    pricing.unit_selling_price = unit_retail

            if "unit_cost_price" in item_data and str(item_data["unit_cost_price"]).strip() != "":
                pricing.unit_cost_price = Decimal(str(item_data["unit_cost_price"]))

            if "remaining_stock" in item_data and str(item_data["remaining_stock"]).strip() != "":
                pricing.remaining_stock = int(item_data["remaining_stock"])

            pricing.save()

    def _commit_new_registered_items(self, payload):
        """
        Commits approved items and links them directly to the target branch via BranchItemPricing.
        """
        items_data = payload.get("items", [])
        
        if not items_data and ("name" in payload or "item_name" in payload or "item_id" in payload):
            items_data = [payload]

        target_branch = self._get_target_branch(payload)

        if not target_branch:
            raise ValueError("No valid branch found in system to assign items.")

        for item_data in items_data:
            item = None
            item_id = item_data.get("item_id") or item_data.get("id")
            item_name = item_data.get("name") or item_data.get("item_name")

            if item_id:
                item = Item.objects.filter(id=item_id).first()

            unit_cost = Decimal(str(
                item_data.get("unit_cost_price") 
                or item_data.get("cost_price") 
                or "0.00"
            ))
            
            unit_retail = Decimal(str(
                item_data.get("unit_retail_price") 
                or item_data.get("unit_selling_price") 
                or item_data.get("retail_price") 
                or "0.00"
            ))

            if not item and item_name:
                item, _ = Item.objects.get_or_create(
                    name=item_name.strip(),
                    defaults={
                        "unit_cost_price": unit_cost,
                        "unit_retail_price": unit_retail,
                    },
                )

            if not item:
                continue

            opening_stock = int(
                item_data.get("opening_stock") 
                or item_data.get("remaining_stock") 
                or item_data.get("quantity") 
                or 0
            )
            
            min_threshold = int(item_data.get("min_stock_threshold", 5))

            pricing_defaults = {
                "unit_cost_price": unit_cost,
                "remaining_stock": opening_stock,
                "min_stock_threshold": min_threshold,
            }

            if hasattr(BranchItemPricing, "unit_selling_price"):
                pricing_defaults["unit_selling_price"] = unit_retail
            if hasattr(BranchItemPricing, "unit_retail_price"):
                pricing_defaults["unit_retail_price"] = unit_retail

            BranchItemPricing.objects.update_or_create(
                branch=target_branch,
                item=item,
                defaults=pricing_defaults,
            )

    def _commit_sale_entry(self, payload):
        items_data = payload.get("items", [])
        branch = self._get_target_branch(payload)

        if not branch:
            return

        for item_data in items_data:
            item_id = item_data.get("item_id")
            qty_sold = int(item_data.get("quantity", 0))

            pricing = BranchItemPricing.objects.filter(branch=branch, item_id=item_id).first()
            if pricing:
                pricing.remaining_stock = max(0, pricing.remaining_stock - qty_sold)
                pricing.save()

    def _commit_branch_creation(self, payload):
        branch_data = payload.get("branch", {})
        items_data = payload.get("items", [])

        branch = RetailBranch.objects.create(**branch_data)

        for item_data in items_data:
            item_info = item_data.copy()
            pricing_info = item_info.pop("branch_pricing", {}) or {}

            unit_cost = Decimal(str(pricing_info.get("unit_cost_price") or item_info.get("unit_cost_price") or "0.00"))
            unit_retail = Decimal(str(pricing_info.get("unit_selling_price") or item_info.get("unit_retail_price") or "0.00"))
            stock = int(pricing_info.get("remaining_stock") or item_info.get("opening_stock") or 0)
            min_threshold = int(pricing_info.get("min_stock_threshold", 5))

            item, _ = Item.objects.get_or_create(
                name=item_info.get("name"),
                defaults={
                    "unit_cost_price": unit_cost,
                    "unit_retail_price": unit_retail,
                },
            )

            pricing_defaults = {
                "unit_cost_price": unit_cost,
                "remaining_stock": stock,
                "min_stock_threshold": min_threshold,
            }

            if hasattr(BranchItemPricing, "unit_selling_price"):
                pricing_defaults["unit_selling_price"] = unit_retail
            if hasattr(BranchItemPricing, "unit_retail_price"):
                pricing_defaults["unit_retail_price"] = unit_retail

            BranchItemPricing.objects.update_or_create(
                branch=branch,
                item=item,
                defaults=pricing_defaults,
            )



# CASHBOOK

def _authenticated_user(request):
    return (
        request.user
        if getattr(request, "user", None) and request.user.is_authenticated
        else None
    )


def _parse_business_date(date_str):
    if not date_str:
        return timezone.localdate()
    try:
        return datetime.strptime(str(date_str).split("T")[0], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return timezone.localdate()


def _resolve_branch(branch_param):
    if not branch_param:
        return None
    from stocks.models import RetailBranch

    if str(branch_param).isdigit():
        return RetailBranch.objects.filter(id=branch_param).first()
    return RetailBranch.objects.filter(name=branch_param).first()


import calendar
from datetime import date, datetime
from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.response import Response

from stocks.models import (
    BranchItemPricing,
    CreditRepayment,
    Customer,
    Expense,
    Item,
    Purchase,
    RetailBranch,
    SaleItem,
    SaleTransaction,
    StockMovement,
)

ZERO = Decimal("0.00")


def quantize_money(val):
    if val is None:
        return ZERO
    return Decimal(str(val)).quantize(Decimal("0.01"))


def _authenticated_user(request):
    return (
        request.user
        if getattr(request, "user", None) and request.user.is_authenticated
        else None
    )


def _resolve_branch(branch_param, user=None):
    if not branch_param:
        return None

    qs = RetailBranch.objects.all()
    if user and getattr(user, "is_authenticated", False):
        qs = qs.filter(retailer=user)

    try:
        if str(branch_param).isdigit():
            return qs.filter(id=int(branch_param)).first()
        return qs.filter(name__iexact=str(branch_param).strip()).first()
    except (TypeError, ValueError):
        return None


def _parse_business_date(date_str):
    if not date_str:
        return timezone.localdate()
    try:
        return datetime.strptime(str(date_str).split("T")[0], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return timezone.localdate()


class CashbookViewSet(viewsets.ViewSet):
    """ViewSet for managing and viewing cashbook entries, balancing cash in hand,
    and reconciling daily/monthly aggregated inflows and outflows per branch.
    """

    permission_classes = [permissions.AllowAny]

    def _normalize_payment_method(self, method):
        if not method:
            return "CASH"

        clean = str(method).strip().upper().replace("-", "_").replace(" ", "_")

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
        if "SPLIT" in clean:
            return "SPLIT"

        return clean

    def list(self, request):
        user = _authenticated_user(request)
        branch_param = request.query_params.get("branch") or request.query_params.get("branch_id")
        date_param = request.query_params.get("date") or request.query_params.get("selectedDate")
        period_type = (
            request.query_params.get("period_type")
            or request.query_params.get("periodType")
            or "day"
        ).lower().strip()

        month_param = request.query_params.get("month")
        year_param = request.query_params.get("year")
        payment_method_param = request.query_params.get("payment_method")

        today = timezone.localdate()

        if period_type == "month" or (month_param and year_param):
            yr = int(year_param) if year_param else today.year
            mth = int(month_param) if month_param else today.month
            _, last_day = calendar.monthrange(yr, mth)
            start_date = date(yr, mth, 1)
            end_date = date(yr, mth, last_day)
        elif period_type == "year" and year_param:
            yr = int(year_param)
            start_date = date(yr, 1, 1)
            end_date = date(yr, 12, 31)
        else:
            business_date = _parse_business_date(date_param) if date_param else today
            start_date = business_date
            end_date = business_date

        tz = timezone.get_current_timezone()
        start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()), tz)
        end_dt = timezone.make_aware(datetime.combine(end_date, datetime.max.time()), tz)

        branch_obj = None
        if branch_param and str(branch_param).lower() != "all":
            branch_obj = _resolve_branch(branch_param, user)

        # 1. Fetch Sales Inflows
        sales_qs = SaleTransaction.objects.filter(
            sale_date__range=(start_dt, end_dt)
        ).prefetch_related("split_payments", "items")

        # 2. Fetch Expense Outflows
        expenses_qs = Expense.objects.filter(
            Q(date__range=(start_date, end_date)) | Q(created_at__range=(start_dt, end_dt))
        )

        # 3. Fetch Credit Repayments Inflows
        repayments_qs = CreditRepayment.objects.filter(
            Q(date__range=(start_date, end_date)) | Q(created_at__range=(start_dt, end_dt))
        )

        # 4. Fetch Purchase Orders / Supplier Transactions
        purchases_qs = Purchase.objects.filter(
            Q(purchase_date__range=(start_dt, end_dt)) | Q(created_at__range=(start_dt, end_dt))
        )

        # 5. Fetch Inbound Stock Movements
        inbound_types = [
            StockMovement.MovementType.PURCHASE,
            StockMovement.MovementType.TRANSFER_IN,
            StockMovement.MovementType.CUSTOMER_RETURN,
            StockMovement.MovementType.ADJUSTMENT_IN,
        ]
        stock_movements_qs = StockMovement.objects.filter(
            movement_type__in=inbound_types,
            created_at__range=(start_dt, end_dt),
        ).select_related("item", "branch")

        # --- BRANCH SCOPE FILTERING (GLOBAL UNFILTERED BY USER) ---
        if branch_obj:
            sales_qs = sales_qs.filter(branch=branch_obj)
            expenses_qs = expenses_qs.filter(branch=branch_obj)
            repayments_qs = repayments_qs.filter(
                Q(branch=branch_obj) | Q(transaction__branch=branch_obj)
            )
            purchases_qs = purchases_qs.filter(branch=branch_obj)
            stock_movements_qs = stock_movements_qs.filter(branch=branch_obj)

        sales_by_method = {}
        repayments_by_method = {}
        expenses_by_method = {}
        purchases_by_method = {}

        total_inflow = ZERO
        total_outflow = ZERO
        calculated_cogs = ZERO

        # Calculate COGS for sold items in the period
        sale_items = SaleItem.objects.filter(transaction__in=sales_qs).select_related("transaction")
        for line in sale_items:
            sale = line.transaction
            qty = Decimal(str(getattr(line, "quantity", getattr(line, "quantity_sold", 1)) or 1))
            unit_cost = Decimal(str(line.unit_cost_price or ZERO))
            line_cost = qty * unit_cost

            method = self._normalize_payment_method(sale.payment_method)
            split_records = list(sale.split_payments.all()) if hasattr(sale, "split_payments") else []
            sale_total = Decimal(str(sale.total_amount or ZERO))

            if method == "SPLIT" or split_records:
                paid_split_amount = ZERO
                for sp in split_records:
                    sp_method = self._normalize_payment_method(getattr(sp, "method", getattr(sp, "payment_method", None)))
                    if sp_method != "CREDIT":
                        paid_split_amount += Decimal(str(getattr(sp, "amount", ZERO) or ZERO))
                paid_ratio = min(Decimal("1.00"), max(ZERO, paid_split_amount / sale_total)) if sale_total > ZERO else ZERO
            elif method == "CREDIT":
                paid_ratio = ZERO
            else:
                paid_ratio = Decimal("1.00")

            calculated_cogs += line_cost * paid_ratio

        # --- Aggregate Sales Inflows ---
        for sale in sales_qs:
            method = self._normalize_payment_method(sale.payment_method)
            amount = Decimal(str(sale.total_amount or ZERO))
            split_records = list(sale.split_payments.all()) if hasattr(sale, "split_payments") else []

            if method == "SPLIT" or split_records:
                for sp in split_records:
                    sp_method = self._normalize_payment_method(getattr(sp, "method", getattr(sp, "payment_method", None)))
                    if sp_method == "CREDIT":
                        continue

                    sp_amount = Decimal(str(getattr(sp, "amount", ZERO) or ZERO))
                    if payment_method_param and sp_method != self._normalize_payment_method(payment_method_param):
                        continue

                    total_inflow += sp_amount
                    if sp_method not in sales_by_method:
                        sales_by_method[sp_method] = {"amount": ZERO, "count": 0}
                    sales_by_method[sp_method]["amount"] += sp_amount
                    sales_by_method[sp_method]["count"] += 1
            else:
                if method == "CREDIT":
                    continue

                if payment_method_param and method != self._normalize_payment_method(payment_method_param):
                    continue

                total_inflow += amount
                if method not in sales_by_method:
                    sales_by_method[method] = {"amount": ZERO, "count": 0}
                sales_by_method[method]["amount"] += amount
                sales_by_method[method]["count"] += 1

        # --- Aggregate Credit Repayments ---
        for repayment in repayments_qs:
            raw_method = getattr(repayment, "payment_method", None) or "CASH"
            method = self._normalize_payment_method(raw_method)

            if payment_method_param and method != self._normalize_payment_method(payment_method_param):
                continue

            amount = Decimal(str(repayment.amount or ZERO))
            total_inflow += amount

            if method not in repayments_by_method:
                repayments_by_method[method] = {"amount": ZERO, "count": 0}
            repayments_by_method[method]["amount"] += amount
            repayments_by_method[method]["count"] += 1

        # --- Aggregate Expenses Outflows ---
        for expense in expenses_qs:
            raw_method = getattr(expense, "payment_method", None) or "CASH"
            method = self._normalize_payment_method(raw_method)

            if payment_method_param and method != self._normalize_payment_method(payment_method_param):
                continue

            amount = Decimal(str(expense.amount or ZERO))
            total_outflow += amount

            if method not in expenses_by_method:
                expenses_by_method[method] = {"amount": ZERO, "count": 0}
            expenses_by_method[method]["amount"] += amount
            expenses_by_method[method]["count"] += 1

        # --- Aggregate Supplier Purchases and Stock Movements ---
        has_explicit_purchases = False

        purchase_payment_map = {}
        for purchase in purchases_qs:
            raw_method = getattr(purchase, "payment_method", None) or "CASH"
            purchase_payment_map[purchase.pk] = self._normalize_payment_method(raw_method)

        for sm in stock_movements_qs:
            method = "CASH"
            if sm.reference_id and sm.reference_id in purchase_payment_map:
                method = purchase_payment_map[sm.reference_id]

            if payment_method_param and method != self._normalize_payment_method(payment_method_param):
                continue

            cost_amount = Decimal(str(getattr(sm, "total_cost", ZERO) or ZERO))
            if cost_amount > ZERO:
                has_explicit_purchases = True
                total_outflow += cost_amount

                if method not in purchases_by_method:
                    purchases_by_method[method] = {"amount": ZERO, "count": 0}
                purchases_by_method[method]["amount"] += cost_amount
                purchases_by_method[method]["count"] += 1

        if not has_explicit_purchases:
            for purchase in purchases_qs:
                method = purchase_payment_map.get(purchase.pk, "CASH")
                if payment_method_param and method != self._normalize_payment_method(payment_method_param):
                    continue

                amount = Decimal(str(getattr(purchase, "total_cost", ZERO) or ZERO))
                if amount > ZERO:
                    has_explicit_purchases = True
                    total_outflow += amount

                    if method not in purchases_by_method:
                        purchases_by_method[method] = {"amount": ZERO, "count": 0}
                    purchases_by_method[method]["amount"] += amount
                    purchases_by_method[method]["count"] += 1

        if not has_explicit_purchases and calculated_cogs > ZERO:
            calculated_cogs = calculated_cogs.quantize(Decimal("0.01"))
            total_outflow += calculated_cogs
            purchases_by_method["CASH"] = {
                "amount": calculated_cogs,
                "count": len(sale_items),
            }

        entries = []
        date_iso = end_date.isoformat()

        for method, data in sales_by_method.items():
            entries.append(
                {
                    "id": f"summary_sale_{method.lower()}",
                    "date": date_iso,
                    "type": "INFLOW",
                    "category": "Sales",
                    "reference": f"TOTAL-SALES-{method}",
                    "description": f"Total Cash/Realized Sales via {method} ({data['count']} txns)",
                    "payment_method": method,
                    "amount": float(data["amount"]),
                    "transaction_count": data["count"],
                }
            )

        for method, data in repayments_by_method.items():
            entries.append(
                {
                    "id": f"summary_repayment_{method.lower()}",
                    "date": date_iso,
                    "type": "INFLOW",
                    "category": "Credit Repayment",
                    "reference": f"TOTAL-REPAY-{method}",
                    "description": f"Total Debt Repayments received via {method} ({data['count']} txns)",
                    "payment_method": method,
                    "amount": float(data["amount"]),
                    "transaction_count": data["count"],
                }
            )

        for method, data in purchases_by_method.items():
            entries.append(
                {
                    "id": f"summary_purchases_{method.lower()}",
                    "date": date_iso,
                    "type": "OUTFLOW",
                    "category": "Supplier Purchases / Restocks",
                    "reference": f"TOTAL-RESTOCKS-{method}",
                    "description": f"Total Stock Restocks & Movements via {method} ({data['count']} items/txns)",
                    "payment_method": method,
                    "amount": float(data["amount"]),
                    "transaction_count": data["count"],
                }
            )

        for method, data in expenses_by_method.items():
            entries.append(
                {
                    "id": f"summary_expense_{method.lower()}",
                    "date": date_iso,
                    "type": "OUTFLOW",
                    "category": "Expenses",
                    "reference": f"TOTAL-EXP-{method}",
                    "description": f"Total Expenses via {method} ({data['count']} txns)",
                    "payment_method": method,
                    "amount": float(data["amount"]),
                    "transaction_count": data["count"],
                }
            )

        net_cash_flow = total_inflow - total_outflow

        return Response(
            {
                "date": date_iso,
                "branch": branch_obj.id if branch_obj else None,
                "summary": {
                    "total_inflow": float(total_inflow),
                    "total_outflow": float(total_outflow),
                    "net_cash_flow": float(net_cash_flow),
                    "total_groups": len(entries),
                },
                "entries": entries,
            },
            status=status.HTTP_200_OK,
        )


class BalanceSheetViewSet(viewsets.ViewSet):
    """
    Statement of Financial Position as at the selected day/month/year.
    """

    permission_classes = [permissions.AllowAny]

    def _date_range(self, request):
        today = timezone.localdate()
        period_type = (
            request.query_params.get("period_type")
            or request.query_params.get("periodType")
            or "day"
        ).lower()

        date_param = request.query_params.get("date") or request.query_params.get("selectedDate")
        month_param = request.query_params.get("month")
        year_param = request.query_params.get("year")

        try:
            if period_type == "month":
                year = int(year_param) if year_param else today.year
                month = int(month_param) if month_param else today.month
                _, last_day = calendar.monthrange(year, month)
                start_date = date(year, month, 1)
                end_date = date(year, month, last_day)

            elif period_type == "year":
                year = int(year_param) if year_param else today.year
                start_date = date(year, 1, 1)
                end_date = date(year, 12, 31)

            else:
                if date_param:
                    start_date = datetime.strptime(str(date_param).split("T")[0], "%Y-%m-%d").date()
                else:
                    start_date = today
                end_date = start_date
                period_type = "day"

        except (TypeError, ValueError):
            start_date = today
            end_date = today
            period_type = "day"

        return period_type, start_date, end_date

    def _sale_cash_and_receivable(self, sales_qs):
        cash = ZERO
        receivable = ZERO

        for sale in sales_qs:
            total = Decimal(str(getattr(sale, "total_amount", ZERO) or ZERO))
            method = str(getattr(sale, "payment_method", "CASH") or "CASH").upper()

            split_records = list(sale.split_payments.all()) if hasattr(sale, "split_payments") else []

            if "SPLIT" in method or split_records:
                split_total = ZERO
                for payment in split_records:
                    amount = Decimal(str(getattr(payment, "amount", ZERO) or ZERO))
                    split_method = str(
                        getattr(payment, "method", getattr(payment, "payment_method", "CASH"))
                        or "CASH"
                    ).upper()

                    if "CREDIT" in split_method or "DEBT" in split_method:
                        receivable += amount
                    else:
                        cash += amount

                    split_total += amount

                if split_total == ZERO and total:
                    receivable += total

            elif "CREDIT" in method or "DEBT" in method or "ON_ACCOUNT" in method:
                receivable += total
            else:
                cash += total

        return cash, receivable

    def list(self, request):
        user = _authenticated_user(request)
        branch_param = (
            request.query_params.get("branch")
            or request.query_params.get("branch_id")
        )

        period_type, start_date, end_date = self._date_range(request)
        branch_obj = None
        if branch_param and str(branch_param).lower() != "all":
            branch_obj = _resolve_branch(branch_param, user)

        tz = timezone.get_current_timezone()
        cutoff_dt = timezone.make_aware(
            datetime.combine(end_date, datetime.max.time()), tz
        )

        # 1. Base Querysets (Global Unfiltered by User)
        sales_qs = SaleTransaction.objects.filter(sale_date__lte=cutoff_dt).prefetch_related("split_payments")

        repayments_qs = CreditRepayment.objects.filter(
            Q(created_at__lte=cutoff_dt) | Q(date__lte=end_date)
        )

        expenses_qs = Expense.objects.filter(
            Q(date__lte=end_date) | Q(created_at__lte=cutoff_dt)
        )

        purchases_qs = Purchase.objects.filter(
            Q(purchase_date__lte=cutoff_dt) | Q(created_at__lte=cutoff_dt)
        ).select_related("item", "branch")

        supplier_payments_qs = SupplierPayment.objects.filter(
            created_at__lte=cutoff_dt
        )

        inbound_types = [
            StockMovement.MovementType.PURCHASE,
            StockMovement.MovementType.TRANSFER_IN,
            StockMovement.MovementType.CUSTOMER_RETURN,
            StockMovement.MovementType.ADJUSTMENT_IN,
        ]
        stock_movements_qs = StockMovement.objects.filter(
            movement_type__in=inbound_types,
            created_at__lte=cutoff_dt,
        )

        customers_qs = Customer.objects.all()

        if branch_obj:
            pricing_qs = BranchItemPricing.objects.filter(branch=branch_obj)
            if not pricing_qs.exists():
                pricing_qs = BranchItemPricing.objects.all()
        else:
            pricing_qs = BranchItemPricing.objects.all()

        # --- BRANCH SCOPE FILTERING ---
        if branch_obj:
            sales_qs = sales_qs.filter(branch=branch_obj)
            repayments_qs = repayments_qs.filter(
                Q(branch=branch_obj) | Q(transaction__branch=branch_obj)
            )
            expenses_qs = expenses_qs.filter(branch=branch_obj)
            purchases_qs = purchases_qs.filter(branch=branch_obj)
            supplier_payments_qs = supplier_payments_qs.filter(purchase__branch=branch_obj)
            stock_movements_qs = stock_movements_qs.filter(branch=branch_obj)

        # Calculate Customer Inflows
        sales_cash, sales_receivable = self._sale_cash_and_receivable(sales_qs)

        repayment_cash = sum(
            (Decimal(str(getattr(r, "amount", ZERO) or ZERO)) for r in repayments_qs),
            ZERO,
        )

        cash_expenses = sum(
            (Decimal(str(getattr(e, "amount", ZERO) or ZERO)) for e in expenses_qs),
            ZERO,
        )

        # Calculate Outflows & Liabilities from Purchases
        cash_purchases = ZERO
        accounts_payable = ZERO

        for purchase in purchases_qs:
            total_cost = Decimal(str(getattr(purchase, "total_cost", ZERO) or ZERO))
            amount_paid = Decimal(str(getattr(purchase, "amount_paid", ZERO) or ZERO))
            method = str(getattr(purchase, "payment_method", "CASH") or "CASH").upper()

            if "CREDIT" in method or "ON_ACCOUNT" in method or "DEBT" in method:
                # Add outstanding balance to Payables
                balance = getattr(purchase, "balance_due", total_cost - amount_paid)
                accounts_payable += Decimal(str(balance or ZERO))
                # Add upfront down-payment/deposit paid to cash outflows
                cash_purchases += amount_paid
            else:
                cash_purchases += total_cost

        # Calculate Supplier Repayments Outflow
        supplier_repayments_cash = sum(
            (Decimal(str(getattr(sp, "amount", ZERO) or ZERO)) for sp in supplier_payments_qs),
            ZERO,
        )

        # Net Closing Cash Position
        cash_balance = quantize_money(
            sales_cash
            + repayment_cash
            - cash_expenses
            - cash_purchases
            - supplier_repayments_cash
        )

        # Accounts Receivable
        try:
            accounts_receivable = sum(
                (Decimal(str(getattr(c, "total_outstanding_balance", ZERO) or ZERO)) for c in customers_qs),
                ZERO,
            )
        except Exception:
            accounts_receivable = sales_receivable

        accounts_receivable = quantize_money(max(accounts_receivable, ZERO))
        accounts_payable = quantize_money(max(accounts_payable, ZERO))

        # Inventory Value with Fallback Pricing
        inventory_value = ZERO
        for p in pricing_qs:
            rem_stock = Decimal(str(getattr(p, "remaining_stock", ZERO) or ZERO))
            cost_p = Decimal(str(getattr(p, "unit_cost_price", ZERO) or ZERO))
            inventory_value += (rem_stock * cost_p)

        inventory_value = quantize_money(inventory_value)

        fixed_assets = ZERO

        # Bank Overdraft vs Positive Cash Asset
        if cash_balance < ZERO:
            bank_overdraft = quantize_money(abs(cash_balance))
            cash_asset = ZERO
        else:
            bank_overdraft = ZERO
            cash_asset = quantize_money(cash_balance)

        current_liabilities = quantize_money(accounts_payable + bank_overdraft)
        total_liabilities = current_liabilities

        total_current_assets = quantize_money(
            cash_asset + accounts_receivable + inventory_value
        )

        total_assets = quantize_money(total_current_assets + fixed_assets)

        owner_equity = quantize_money(total_assets - total_liabilities)
        total_liabilities_and_equity = quantize_money(total_liabilities + owner_equity)
        difference = quantize_money(total_assets - total_liabilities_and_equity)

        period_label = (
            str(end_date)
            if start_date == end_date
            else f"{start_date} to {end_date}"
        )

        return Response(
            {
                "period": {
                    "period_type": period_type,
                    "start_date": str(start_date),
                    "end_date": str(end_date),
                    "label": period_label,
                    "as_at": str(end_date),
                },
                "branch": branch_obj.id if branch_obj else None,
                "assets": {
                    "current_assets": {
                        "cash_and_equivalents": float(cash_asset),
                        "accounts_receivable": float(accounts_receivable),
                        "inventory_value": float(inventory_value),
                        "total": float(total_current_assets),
                    },
                    "fixed_assets": {
                        "property_equipment": float(fixed_assets),
                        "total": float(fixed_assets),
                    },
                    "total_assets": float(total_assets),
                },
                "liabilities": {
                    "current_liabilities": {
                        "accounts_payable": float(accounts_payable),
                        "bank_overdraft": float(bank_overdraft),
                        "total": float(current_liabilities),
                    },
                    "non_current_liabilities": {
                        "total": 0.0,
                    },
                    "total_liabilities": float(total_liabilities),
                },
                "equity": {
                    "owner_equity": float(owner_equity),
                    "owner_capital": float(owner_equity),
                    "retained_earnings": 0.0,
                    "current_period_profit": 0.0,
                    "drawings": 0.0,
                    "total_equity": float(owner_equity),
                },
                "total_liabilities_and_equity": float(total_liabilities_and_equity),
                "accounting_check": {
                    "difference": float(difference),
                    "balanced": abs(difference) < Decimal("0.01"),
                },
                "cash_reconciliation": {
                    "sales_cash": float(quantize_money(sales_cash)),
                    "customer_repayments": float(quantize_money(repayment_cash)),
                    "cash_expenses": float(quantize_money(cash_expenses)),
                    "cash_purchases": float(quantize_money(cash_purchases)),
                    "supplier_repayments": float(quantize_money(supplier_repayments_cash)),
                    "closing_cash_before_overdraft": float(cash_balance),
                },
            },
            status=status.HTTP_200_OK,
        )
    
      

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

# Import models & helper constants from your application
from stocks.models import (
    BranchItemPricing,
    CreditRepayment,
    Expense,
    Item,
    Purchase,
    RetailBranch,
    SaleItem,
    SaleTransaction,
    StockMovement,
)

ZERO = Decimal("0.00")


def quantize_money(val):
    if val is None:
        return ZERO
    return Decimal(str(val)).quantize(Decimal("0.01"))


class FinancialReportViewSet(viewsets.ViewSet):
    permission_classes = [permissions.AllowAny]

    def _normalize_payment_method(self, method_name):
        if not method_name:
            return "UNKNOWN"

        clean = (
            str(method_name)
            .strip()
            .upper()
            .replace("-", "_")
            .replace(" ", "_")
        )

        if any(k in clean for k in ["PAYBILL", "PAY_BILL", "C2B"]):
            return "PAYBILL"

        if any(k in clean for k in [
            "SEND_MONEY",
            "SENDMONEY",
            "TILL",
            "BUY_GOODS",
            "BUYGOODS",
            "P2P",
        ]):
            return "SEND_MONEY"

        if "CASH" in clean:
            return "CASH"

        if (
            "CREDIT" in clean
            or "DEBT" in clean
            or "ON_ACCOUNT" in clean
        ):
            return "CREDIT"

        if (
            "BANK" in clean
            or "EFT" in clean
            or "TRANSFER" in clean
        ):
            return "BANK_TRANSFER"

        if (
            "MPESA" in clean
            or "M_PESA" in clean
            or "MOBILE" in clean
        ):
            return "MPESA"

        if "SPLIT" in clean:
            return "SPLIT"

        return clean

    def _extract_sp_method(self, sp):
        for attr in [
            "method",
            "payment_method",
            "payment_mode",
            "channel",
            "mode",
            "type",
        ]:
            val = getattr(sp, attr, None)
            if val:
                return val

        return None

    def _get_split_payments(self, sale):
        relation = getattr(sale, "split_payments", None)

        if relation is not None:
            try:
                return list(relation.all())
            except Exception:
                pass

        return []

    def _extract_qty(self, obj, default=Decimal("1.000")):
        for attr in [
            "quantity",
            "quantity_sold",
            "qty",
            "count",
        ]:
            val = getattr(obj, attr, None)

            if val is not None:
                try:
                    return Decimal(str(val))
                except Exception:
                    continue

        return Decimal(str(default))

    def _get_date_fieldname(self, model):
        concrete_fields = [
            f.name
            for f in model._meta.get_fields()
            if f.concrete and not f.is_relation
        ]

        for name in [
            "date",
            "payment_date",
            "sale_date",
            "purchase_date",
            "transaction_date",
            "created_at",
            "updated_at",
        ]:
            if name in concrete_fields:
                return name

        return None

    def _get_date_range(
        self,
        target_date,
        period_type="day",
        custom_start_date=None,
        custom_end_date=None,
    ):
        tz = timezone.get_current_timezone()

        if period_type == "custom":
            start_date = custom_start_date or target_date
            end_date = custom_end_date or target_date
            if start_date > end_date:
                raise ValueError("Start date cannot be after end date.")

        elif period_type == "day":
            start_date = target_date
            end_date = target_date

        elif period_type in ["weekly", "week"]:
            start_date = target_date - timedelta(days=target_date.weekday())
            end_date = start_date + timedelta(days=6)

        elif period_type == "month":
            start_date = date(
                target_date.year,
                target_date.month,
                1,
            )

            _, last_day = calendar.monthrange(
                target_date.year,
                target_date.month,
            )

            end_date = date(
                target_date.year,
                target_date.month,
                last_day,
            )

        elif period_type == "year":
            start_date = date(
                target_date.year,
                1,
                1,
            )

            end_date = date(
                target_date.year,
                12,
                31,
            )

        else:
            start_date = target_date
            end_date = target_date

        start_dt = timezone.make_aware(
            datetime.combine(
                start_date,
                datetime.min.time(),
            ),
            tz,
        )

        end_dt = timezone.make_aware(
            datetime.combine(
                end_date,
                datetime.max.time(),
            ),
            tz,
        )

        return start_dt, end_dt

    def _get_historical_stock(
        self,
        item_obj,
        branch_obj,
        cutoff_dt,
        target_date=None,
        period_type="day",
    ):
        pricing = None
        if branch_obj:
            pricing = BranchItemPricing.objects.filter(
                branch=branch_obj,
                item=item_obj,
            ).first()

        # Fallback to any available pricing record if branch pricing doesn't exist
        if not pricing:
            pricing = BranchItemPricing.objects.filter(item=item_obj).first()

        current_qty = (
            pricing.remaining_stock
            if pricing
            else Decimal("0.000")
        )

        cost_price = (
            pricing.unit_cost_price
            if pricing
            else Decimal("0.00")
        )

        min_threshold = (
            pricing.min_stock_threshold
            if pricing
            else Decimal("5.000")
        )

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

        mv_date_field = self._get_date_fieldname(StockMovement) or "created_at"
        mv_field_obj = StockMovement._meta.get_field(mv_date_field)

        if mv_field_obj.get_internal_type() == "DateTimeField":
            mv_filter = Q(**{f"{mv_date_field}__gt": cutoff_dt})
        else:
            mv_filter = Q(**{f"{mv_date_field}__gt": cutoff_dt.date()})

        mv_filter &= Q(item=item_obj)

        if branch_obj:
            mv_filter &= Q(branch=branch_obj)

        totals = StockMovement.objects.filter(
            mv_filter
        ).aggregate(
            stock_in=Coalesce(
                Sum(
                    "quantity",
                    filter=Q(
                        movement_type__in=inbound_types
                    ),
                ),
                Decimal("0.000"),
                output_field=models.DecimalField(),
            ),
            stock_out=Coalesce(
                Sum(
                    "quantity",
                    filter=Q(
                        movement_type__in=outbound_types
                    ),
                ),
                Decimal("0.000"),
                output_field=models.DecimalField(),
            ),
        )

        stock_in_after = Decimal(
            str(totals["stock_in"] or "0.000")
        )

        stock_out_after = Decimal(
            str(totals["stock_out"] or "0.000")
        )

        historical_qty = max(
            Decimal("0.000"),
            Decimal(str(current_qty))
            - stock_in_after
            + stock_out_after,
        )

        return (
            historical_qty,
            Decimal(str(cost_price)),
            Decimal(str(min_threshold)),
        )

    def _filter_date_queryset(
        self,
        qs,
        model,
        start_dt,
        end_dt,
        target_date,
        period_type,
    ):
        date_field_name = self._get_date_fieldname(model)

        if not date_field_name:
            return qs

        model_field = model._meta.get_field(date_field_name)
        is_datetime = model_field.get_internal_type() == "DateTimeField"

        if is_datetime:
            return qs.filter(
                **{
                    f"{date_field_name}__range": (
                        start_dt,
                        end_dt,
                    )
                }
            )

        # For DateField models, filter using start_date to end_date range to avoid timezone bounds mismatches
        return qs.filter(
            **{
                f"{date_field_name}__range": (
                    start_dt.date(),
                    end_dt.date(),
                )
            }
        )

    def _is_credit_sale(
        self,
        sale,
        credit_transaction_ids=None,
    ):
        method = self._normalize_payment_method(
            getattr(sale, "payment_method", None)
        )

        if method == "CREDIT":
            return True

        if getattr(sale, "is_invoice", False):
            return True

        sale_id = getattr(sale, "pk", None)

        if (
            credit_transaction_ids is not None
            and sale_id in credit_transaction_ids
        ):
            return True

        balance_due = getattr(
            sale,
            "balance_due",
            None,
        )

        if balance_due is not None:
            try:
                if Decimal(
                    str(balance_due or ZERO)
                ) > ZERO:
                    return True
            except Exception:
                pass

        return False

    def _get_sale_credit_amount(
        self,
        sale,
        credit_transaction_ids=None,
    ):
        method = self._normalize_payment_method(
            getattr(sale, "payment_method", None)
        )

        sale_amount = quantize_money(
            sale.total_amount or ZERO
        )

        split_records = self._get_split_payments(sale)

        if method == "SPLIT" or split_records:
            credit_total = ZERO

            for sp in split_records:
                sp_method = self._normalize_payment_method(
                    self._extract_sp_method(sp)
                )

                if sp_method == "CREDIT":
                    credit_total += quantize_money(
                        sp.amount or ZERO
                    )

            return quantize_money(
                credit_total
            )

        if self._is_credit_sale(
            sale,
            credit_transaction_ids,
        ):
            return sale_amount

        return ZERO

    @action(
        detail=False,
        methods=["get"],
        url_path="summary",
    )
    def summary(self, request):

        try:
            # ==========================================================
            # 1. PERIOD
            # ==========================================================

            raw_period = (
                request.query_params.get("period_type") or "day"
            ).lower().strip()

            period_aliases = {
                "daily": "day", "day": "day",
                "weekly": "weekly", "week": "weekly",
                "monthly": "month", "month": "month",
                "yearly": "year", "year": "year",
                "custom": "custom",
            }
            period_type = period_aliases.get(raw_period, "day")

            branch_param = (
                request.query_params.get("branch")
                or request.query_params.get("branch_id")
            )
            date_str = (
                request.query_params.get("date")
                or request.query_params.get("selectedDate")
            )
            start_date_str = (
                request.query_params.get("startDate")
                or request.query_params.get("start_date")
            )
            end_date_str = (
                request.query_params.get("endDate")
                or request.query_params.get("end_date")
            )
            year_str = request.query_params.get("year")
            month_str = request.query_params.get("month")

            def parse_report_date(value):
                if not value:
                    return None
                try:
                    return datetime.strptime(
                        str(value).split("T")[0], "%Y-%m-%d"
                    ).date()
                except (ValueError, TypeError):
                    return None

            target_date = timezone.localdate()
            custom_start_date = None
            custom_end_date = None

            if period_type == "custom":
                custom_start_date = parse_report_date(start_date_str)
                custom_end_date = parse_report_date(end_date_str)
                if not custom_start_date or not custom_end_date:
                    return Response(
                        {"error": "Custom range requires valid startDate and endDate values in YYYY-MM-DD format."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if custom_start_date > custom_end_date:
                    return Response(
                        {"error": "startDate cannot be after endDate."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                target_date = custom_end_date
            else:
                parsed_date = parse_report_date(date_str)
                if parsed_date:
                    target_date = parsed_date
                elif year_str and month_str:
                    try:
                        target_date = date(int(year_str), int(month_str), 1)
                    except (ValueError, TypeError):
                        target_date = timezone.localdate()

            branch_obj = None

            if branch_param and str(branch_param).lower() != "all":
                branch_obj = RetailBranch.objects.filter(
                    Q(id=branch_param)
                    if str(branch_param).isdigit()
                    else Q(name=branch_param)
                ).first()

            # ==========================================================
            # 2. BASE QUERYSETS (GLOBAL UNFILTERED BY USER)
            # ==========================================================

            expense_qs = Expense.objects.all()
            sales_qs = SaleTransaction.objects.all()
            movements_qs = StockMovement.objects.all()
            repayment_qs = CreditRepayment.objects.all()

            if branch_obj:
                items_qs = Item.objects.filter(
                    Q(branch_pricings__branch=branch_obj) | Q(stock_movements__branch=branch_obj)
                ).distinct()
            else:
                items_qs = Item.objects.all()

            if not items_qs.exists():
                items_qs = Item.objects.all()

            purchases_qs = (
                Purchase.objects.all()
                if Purchase is not None
                else None
            )

            # ==========================================================
            # 3. BRANCH SCOPE (IF SPECIFIED)
            # ==========================================================

            if branch_obj:
                expense_qs = expense_qs.filter(
                    branch=branch_obj
                )

                sales_qs = sales_qs.filter(
                    branch=branch_obj
                )

                movements_qs = movements_qs.filter(
                    branch=branch_obj
                )

                if hasattr(
                    CreditRepayment,
                    "branch",
                ):
                    repayment_qs = repayment_qs.filter(
                        Q(branch=branch_obj)
                        | Q(
                            transaction__branch=branch_obj
                        )
                        | Q(
                            branch__isnull=True
                        )
                    )

                if purchases_qs is not None:
                    purchases_qs = purchases_qs.filter(
                        branch=branch_obj
                    )

            # ==========================================================
            # 4. PERIOD RANGE
            # ==========================================================

            start_dt, end_dt = self._get_date_range(
                target_date,
                period_type,
                custom_start_date=custom_start_date,
                custom_end_date=custom_end_date,
            )

            expense_qs = self._filter_date_queryset(
                expense_qs,
                Expense,
                start_dt,
                end_dt,
                target_date,
                period_type,
            )

            sales_qs = self._filter_date_queryset(
                sales_qs,
                SaleTransaction,
                start_dt,
                end_dt,
                target_date,
                period_type,
            )

            movements_qs = self._filter_date_queryset(
                movements_qs,
                StockMovement,
                start_dt,
                end_dt,
                target_date,
                period_type,
            )

            repayment_scope_qs = repayment_qs

            repayment_qs = repayment_scope_qs.filter(
                date__range=(
                    start_dt.date(),
                    end_dt.date(),
                )
            )

            if purchases_qs is not None:
                purchases_qs = self._filter_date_queryset(
                    purchases_qs,
                    Purchase,
                    start_dt,
                    end_dt,
                    target_date,
                    period_type,
                )

            # ==========================================================
            # 5. CREDIT TRANSACTION IDENTIFICATION
            # ==========================================================

            credit_transaction_ids = set(
                repayment_scope_qs
                .exclude(transaction_id=None)
                .values_list(
                    "transaction_id",
                    flat=True,
                )
                .distinct()
            )

            # ==========================================================
            # 6. PURCHASES
            # ==========================================================

            purchases_cost_val = ZERO

            if (
                purchases_qs is not None
                and purchases_qs.exists()
            ):
                for purchase in purchases_qs:
                    amount = getattr(
                        purchase,
                        "total_cost",
                        getattr(
                            purchase,
                            "amount",
                            ZERO,
                        ),
                    )

                    purchases_cost_val += Decimal(
                        str(amount or ZERO)
                    )

            else:
                purchase_movements = movements_qs.filter(
                    movement_type=
                    StockMovement.MovementType.PURCHASE
                )

                for mv in purchase_movements:
                    qty = self._extract_qty(
                        mv,
                        default=ZERO,
                    )

                    cost = getattr(
                        mv,
                        "unit_cost_price",
                        ZERO,
                    ) or ZERO

                    purchases_cost_val += (
                        qty * Decimal(str(cost))
                    )

            purchases_cost_val = quantize_money(
                purchases_cost_val
            )

            # ==========================================================
            # 7. EXPENSES
            # ==========================================================

            expenses_breakdown = []
            expenses_val = ZERO

            for exp in expense_qs:

                exp_date = getattr(
                    exp,
                    "date",
                    getattr(
                        exp,
                        "created_at",
                        None,
                    ),
                )

                amt = quantize_money(
                    exp.amount or ZERO
                )

                expenses_val += amt

                expenses_breakdown.append(
                    {
                        "id": exp.id,
                        "name": (
                            getattr(
                                exp,
                                "title",
                                None,
                            )
                            or getattr(
                                exp,
                                "category",
                                "Expense",
                            )
                        ),
                        "amount": float(amt),
                        "receipt_number": (
                            f"EXP-{exp.id}"
                        ),
                        "date": (
                            exp_date.isoformat()
                            if hasattr(
                                exp_date,
                                "isoformat",
                            )
                            else str(
                                exp_date or ""
                            )
                        ),
                        "description": (
                            getattr(
                                exp,
                                "description",
                                "",
                            )
                            or ""
                        ),
                        "payment_method": (
                            getattr(
                                exp,
                                "payment_method",
                                "CASH",
                            )
                        ),
                    }
                )

            # ==========================================================
            # 8. INITIAL CHANNEL TOTALS & REPAYMENT TRACKING
            # ==========================================================

            cash_total = ZERO
            sales_revenue_total = ZERO
            period_credit_sales_total = ZERO
            mpesa_std_total = ZERO
            paybill_total = ZERO
            send_money_total = ZERO
            bank_total = ZERO
            other_total = ZERO

            period_credit_repayments_total = ZERO
            same_day_credit_recovered_total = ZERO
            prior_period_credit_repayments_total = ZERO

            cogs_repayment_additions = ZERO
            item_repayment_costs = {}

            # ==========================================================
            # 9. SALES / RECEIPTS
            # ==========================================================

            receipt_details = []

            sales_with_payments = (
                sales_qs
                .select_related(
                    "customer",
                    "branch",
                )
                .prefetch_related(
                    "split_payments",
                )
            )

            for sale in sales_with_payments:

                sale_date = getattr(
                    sale,
                    "sale_date",
                    getattr(
                        sale,
                        "created_at",
                        None,
                    ),
                )

                sale_amt = Decimal(
                    str(
                        sale.total_amount
                        or ZERO
                    )
                )

                if (
                    sale_amt == ZERO
                    and hasattr(sale, "items")
                ):
                    for line in sale.items.all():

                        line_qty = self._extract_qty(
                            line,
                            default=ZERO,
                        )

                        line_price = (
                            line.unit_selling_price
                            or ZERO
                        )

                        line_disc = (
                            getattr(
                                line,
                                "discount_per_item",
                                None,
                            )
                            or ZERO
                        )

                        sale_amt += (
                            line_qty
                            * (
                                line_price
                                - line_disc
                            )
                        )

                amt = quantize_money(sale_amt)

                receipt_details.append(
                    {
                        "id": sale.id,
                        "receipt_number": (
                            getattr(
                                sale,
                                "receipt_number",
                                f"REC-{sale.id}",
                            )
                        ),
                        "date": (
                            sale_date.isoformat()
                            if hasattr(
                                sale_date,
                                "isoformat",
                            )
                            else str(
                                sale_date or ""
                            )
                        ),
                        "customer_name": (
                            sale.customer.name
                            if getattr(
                                sale,
                                "customer",
                                None,
                            )
                            else (
                                getattr(
                                    sale,
                                    "customer_name",
                                    None,
                                )
                                or "Walk-in Customer"
                            )
                        ),
                        "amount": float(amt),
                        "payment_method": getattr(
                            sale,
                            "payment_method",
                            "CASH",
                        ),
                        "type": "Sale",
                    }
                )

                method = self._normalize_payment_method(
                    sale.payment_method
                )

                split_records = self._get_split_payments(
                    sale
                )

                if method == "SPLIT" or split_records:
                    paid_sale_revenue = ZERO
                    for sp in split_records:
                        sp_method = self._normalize_payment_method(
                            self._extract_sp_method(sp)
                        )
                        if sp_method != "CREDIT":
                            paid_sale_revenue += quantize_money(
                                sp.amount or ZERO
                            )
                    sales_revenue_total += quantize_money(
                        paid_sale_revenue
                    )
                elif self._is_credit_sale(
                    sale, credit_transaction_ids
                ):
                    sales_revenue_total += ZERO
                else:
                    sales_revenue_total += amt

                if method == "SPLIT" or split_records:

                    for sp in split_records:

                        sp_method = (
                            self._normalize_payment_method(
                                self._extract_sp_method(sp)
                            )
                        )

                        sp_amount = quantize_money(
                            sp.amount or ZERO
                        )

                        if sp_method == "CASH":
                            cash_total += sp_amount

                        elif sp_method == "PAYBILL":
                            paybill_total += sp_amount

                        elif sp_method == "SEND_MONEY":
                            send_money_total += sp_amount

                        elif sp_method == "MPESA":
                            mpesa_std_total += sp_amount

                        elif sp_method == "BANK_TRANSFER":
                            bank_total += sp_amount

                        elif sp_method == "CREDIT":
                            period_credit_sales_total += sp_amount

                        else:
                            other_total += sp_amount

                else:

                    is_credit = self._is_credit_sale(
                        sale,
                        credit_transaction_ids,
                    )

                    if is_credit:
                        period_credit_sales_total += amt

                    elif method == "CASH":
                        cash_total += amt

                    elif method == "PAYBILL":
                        paybill_total += amt

                    elif method == "SEND_MONEY":
                        send_money_total += amt

                    elif method == "MPESA":
                        mpesa_std_total += amt

                    elif method == "BANK_TRANSFER":
                        bank_total += amt

                    elif method == "CREDIT":
                        period_credit_sales_total += amt

                    else:
                        other_total += amt

            # ==========================================================
            # 10. CREDIT REPAYMENTS / CASH FLOW / COGS ADJUSTMENT
            # ==========================================================

            for rep in repayment_qs.select_related(
                "customer",
                "transaction",
            ):

                rep_amt = quantize_money(
                    rep.amount or ZERO
                )

                if rep_amt <= ZERO:
                    continue

                rep_date = rep.date

                if isinstance(
                    rep_date,
                    datetime,
                ):
                    rep_date = rep_date.date()

                rep_method = self._normalize_payment_method(
                    getattr(
                        rep,
                        "payment_method",
                        None,
                    )
                )

                period_credit_repayments_total += rep_amt
                same_day_credit_recovered_total += rep_amt
                prior_period_credit_repayments_total += ZERO

                sales_revenue_total += rep_amt

                # Calculate proportional COGS for the repaid credit amount
                if rep.transaction:
                    rep_sale = rep.transaction
                    rep_sale_total = Decimal(str(getattr(rep_sale, "total_amount", ZERO) or ZERO))

                    if rep_sale_total > ZERO and hasattr(rep_sale, "items"):
                        rep_ratio = min(Decimal("1.00"), rep_amt / rep_sale_total)

                        for line in rep_sale.items.all():
                            line_qty = self._extract_qty(line, default=Decimal("0.000"))
                            line_cost = line_qty * Decimal(str(line.unit_cost_price or ZERO))
                            repayment_line_cost = line_cost * rep_ratio

                            cogs_repayment_additions += repayment_line_cost

                            item_id = line.item_id if getattr(line, "item_id", None) else None
                            if item_id:
                                item_repayment_costs[item_id] = item_repayment_costs.get(item_id, ZERO) + repayment_line_cost

                if rep_method == "CASH":
                    cash_total += rep_amt

                elif rep_method == "PAYBILL":
                    paybill_total += rep_amt

                elif rep_method == "SEND_MONEY":
                    send_money_total += rep_amt

                elif rep_method == "BANK_TRANSFER":
                    bank_total += rep_amt

                elif rep_method == "MPESA":
                    mpesa_std_total += rep_amt

                elif rep_method == "SPLIT":
                    other_total += rep_amt

                else:
                    other_total += rep_amt

                customer_name = (
                    rep.customer.name
                    if getattr(
                        rep,
                        "customer",
                        None,
                    )
                    and hasattr(
                        rep.customer,
                        "name",
                    )
                    else "Debt Repayment"
                )

                receipt_details.append(
                    {
                        "id": f"REP-{rep.id}",
                        "receipt_number": f"REP-{rep.id}",
                        "date": (
                            rep_date.isoformat()
                            if hasattr(
                                rep_date,
                                "isoformat",
                            )
                            else str(
                                rep_date or ""
                            )
                        ),
                        "customer_name": customer_name,
                        "amount": float(rep_amt),
                        "payment_method": rep_method,
                        "type": "Repayment",
                    }
                )

            # ==========================================================
            # 11. LIQUID COLLECTIONS / CASH FLOW
            # ==========================================================

            total_liquid_collections = (
                cash_total
                + mpesa_std_total
                + paybill_total
                + send_money_total
                + bank_total
                + other_total
            )

            total_liquid_collections = quantize_money(
                total_liquid_collections
            )

            actual_revenue_val = sales_revenue_total

            # ==========================================================
            # 12. HISTORICAL STOCK AT PERIOD END
            # ==========================================================

            cutoff_dt = end_dt

            stock_val = ZERO
            stock_qty = Decimal("0.000")

            low_stock_cnt = 0
            out_of_stock_cnt = 0

            historical_stock_map = {}

            for item in items_qs:

                (
                    rem_stock,
                    cost_price,
                    min_threshold,
                ) = self._get_historical_stock(
                    item,
                    branch_obj,
                    cutoff_dt,
                    target_date=target_date,
                    period_type=period_type,
                )

                historical_stock_map[
                    item.id
                ] = rem_stock

                stock_qty += rem_stock

                stock_val += (
                    rem_stock * cost_price
                )

                if rem_stock <= Decimal("0.000"):
                    out_of_stock_cnt += 1

                elif rem_stock <= min_threshold:
                    low_stock_cnt += 1

            # ==========================================================
            # 13. STOCK RETURNS / DAMAGE
            # ==========================================================

            damaged_stock_val = ZERO
            returned_stock_val = ZERO

            for mv in movements_qs:

                mv_type = mv.movement_type

                qty = self._extract_qty(
                    mv,
                    default=ZERO,
                )

                cost = Decimal(
                    str(
                        mv.unit_cost_price
                        or ZERO
                    )
                )

                mv_val = qty * cost

                if mv_type in [
                    StockMovement.MovementType.DAMAGE,
                    "DAMAGED",
                    "EXPIRED",
                    "WASTE",
                ]:
                    damaged_stock_val += mv_val

                elif mv_type in [
                    StockMovement.MovementType.CUSTOMER_RETURN,
                    "RETURN_IN",
                ]:
                    returned_stock_val += mv_val

            # ==========================================================
            # 14. ITEM STATISTICS / COGS / DYNAMIC MOVEMENTS
            # ==========================================================

            cogs_val = ZERO
            item_stats = {}

            restocks_map = {}
            inbound_types = [
                StockMovement.MovementType.PURCHASE,
                StockMovement.MovementType.TRANSFER_IN,
                StockMovement.MovementType.ADJUSTMENT_IN,
            ]

            period_restocks = movements_qs.filter(movement_type__in=inbound_types)
            for mv in period_restocks:
                item_id = mv.item_id
                if item_id:
                    qty = self._extract_qty(mv, default=ZERO)
                    restocks_map[item_id] = restocks_map.get(item_id, Decimal("0.000")) + qty

            for item_obj in items_qs:

                pricing = (
                    BranchItemPricing.objects.filter(
                        branch=branch_obj,
                        item=item_obj,
                    ).first()
                    if branch_obj
                    else None
                )

                if not pricing:
                    pricing = BranchItemPricing.objects.filter(item=item_obj).first()

                c_price = (
                    pricing.unit_cost_price
                    if pricing
                    else ZERO
                )

                r_price = (
                    pricing.unit_selling_price
                    if (
                        pricing
                        and pricing.unit_selling_price
                    )
                    else ZERO
                )

                opening_cutoff = start_dt - timedelta(microseconds=1)
                if (
                    pricing
                    and start_dt.date().day == 1
                    and period_type in ("day", "month")
                ):
                    opening_stock_val = Decimal(
                        str(pricing.opening_stock or ZERO)
                    )
                else:
                    opening_stock_val, _, _ = self._get_historical_stock(
                        item_obj,
                        branch_obj,
                        opening_cutoff,
                        target_date=target_date,
                        period_type=period_type,
                    )

                ending_stock_val, _, _ = self._get_historical_stock(
                    item_obj,
                    branch_obj,
                    end_dt,
                    target_date=target_date,
                    period_type=period_type,
                )

                item_stats[item_obj.id] = {
                    "id": item_obj.id,
                    "name": item_obj.name,
                    "code": item_obj.code or "",
                    "opening_stock": opening_stock_val,
                    "restocks": restocks_map.get(item_obj.id, Decimal("0.000")),
                    "units_sold": Decimal("0.000"),
                    "cost_price": float(c_price),
                    "retail_price": float(r_price),
                    "current_stock": float(ending_stock_val),
                    "total_revenue": ZERO,
                    "total_cost": item_repayment_costs.get(item_obj.id, ZERO),
                }

            sale_items = SaleItem.objects.filter(
                transaction__in=sales_qs
            ).select_related(
                "transaction",
                "item",
            )

            for line in sale_items:

                sale = line.transaction
                item_obj = line.item

                item_id = (
                    item_obj.id
                    if item_obj
                    else None
                )

                qty = self._extract_qty(
                    line,
                    default=Decimal("0.000"),
                )

                unit_price = (
                    line.unit_selling_price
                    or ZERO
                )

                discount = (
                    getattr(
                        line,
                        "discount_per_item",
                        None,
                    )
                    or ZERO
                )

                line_rev = (
                    qty
                    * (
                        unit_price
                        - discount
                    )
                )

                unit_cost = (
                    line.unit_cost_price
                    or ZERO
                )

                line_cost = (
                    qty * unit_cost
                )

                method = self._normalize_payment_method(
                    getattr(sale, "payment_method", None)
                )
                split_records = self._get_split_payments(sale)
                sale_total = Decimal(
                    str(getattr(sale, "total_amount", ZERO) or ZERO)
                )

                if method == "SPLIT" or split_records:
                    paid_split_amount = ZERO
                    for sp in split_records:
                        sp_method = self._normalize_payment_method(
                            self._extract_sp_method(sp)
                        )
                        if sp_method != "CREDIT":
                            paid_split_amount += Decimal(
                                str(sp.amount or ZERO)
                            )
                    if sale_total > ZERO:
                        paid_ratio = min(
                            Decimal("1.00"),
                            max(ZERO, paid_split_amount / sale_total),
                        )
                    else:
                        paid_ratio = ZERO
                elif self._is_credit_sale(
                    sale, credit_transaction_ids
                ):
                    paid_ratio = ZERO
                else:
                    paid_ratio = Decimal("1.00")

                paid_line_revenue = line_rev * paid_ratio
                paid_cost_share = line_cost * paid_ratio

                if (
                    item_id
                    and item_id in item_stats
                ):

                    item_stats[
                        item_id
                    ]["units_sold"] += qty

                    item_stats[
                        item_id
                    ]["total_revenue"] += paid_line_revenue

                    item_stats[
                        item_id
                    ]["total_cost"] += paid_cost_share

                    cogs_val += paid_cost_share

                else:

                    fallback_id = (
                        f"item_{item_id or line.id}"
                    )

                    item_name = (
                        item_obj.name
                        if item_obj
                        else f"Item #{line.id}"
                    )

                    item_stats[
                        fallback_id
                    ] = {
                        "id": line.id,
                        "name": item_name,
                        "code": "",
                        "opening_stock": Decimal("0.000"),
                        "restocks": Decimal("0.000"),
                        "units_sold": qty,
                        "cost_price": float(
                            unit_cost
                        ),
                        "retail_price": float(
                            unit_price
                        ),
                        "current_stock": 0.0,
                        "total_revenue": paid_line_revenue,
                        "total_cost": paid_cost_share,
                    }

                    cogs_val += paid_cost_share

            # Include the recognized COGS additions from credit repayments
            cogs_val += cogs_repayment_additions

            # ==========================================================
            # 15. ITEM ANALYTICS
            # ==========================================================

            formatted_item_analytics = []

            for _, stats in item_stats.items():

                units_sold_num = stats["units_sold"]
                restocks_num = stats["restocks"]
                opening_stock_num = stats["opening_stock"]

                remaining_stock_num = max(
                    Decimal("0.000"),
                    Decimal(str(stats["current_stock"] or 0))
                )

                profit = (
                    stats["total_revenue"]
                    - stats["total_cost"]
                )

                margin = (
                    (
                        profit
                        / stats["total_revenue"]
                        * Decimal("100")
                    )
                    if stats["total_revenue"] > ZERO
                    else ZERO
                )

                formatted_item_analytics.append(
                    {
                        "id": stats["id"],
                        "name": stats["name"],
                        "code": stats["code"],
                        "opening_stock": float(opening_stock_num),
                        "restocks": float(restocks_num),
                        "units_sold": float(units_sold_num),
                        "remaining_stock": float(remaining_stock_num),
                        "current_stock": float(remaining_stock_num),
                        "latest_unit_cost": stats["cost_price"],
                        "cost_price": stats["cost_price"],
                        "unit_retail_price": stats["retail_price"],
                        "retail_price": stats["retail_price"],
                        "total_revenue": float(
                            quantize_money(
                                stats["total_revenue"]
                            )
                        ),
                        "profit": float(
                            quantize_money(
                                profit
                            )
                        ),
                        "profit_margin_percent": round(
                            float(margin),
                            2,
                        ),
                    }
                )

            fast_moving_items = sorted(
                [
                    x
                    for x in formatted_item_analytics
                    if x["units_sold"] > 0
                ],
                key=lambda x: x["units_sold"],
                reverse=True,
            )[:10]

            fast_ids = {
                x["id"]
                for x in fast_moving_items
            }

            slow_candidates = [
                x
                for x in formatted_item_analytics
                if x["id"] not in fast_ids
            ]

            slow_moving_items = sorted(
                slow_candidates,
                key=lambda x: (
                    x["units_sold"],
                    -x["current_stock"],
                ),
            )[:10]

            # ==========================================================
            # 16. PROFITABILITY
            # ==========================================================

            revenue_val = quantize_money(
                sales_revenue_total
            )

            cogs_final = quantize_money(
                cogs_val
            )

            gross_profit_val = quantize_money(
                revenue_val - cogs_final
            )

            net_profit_val = quantize_money(
                gross_profit_val - expenses_val
            )

            margin_val = (
                round(
                    float(
                        (
                            net_profit_val
                            / revenue_val
                        )
                        * Decimal("100")
                    ),
                    2,
                )
                if revenue_val > ZERO
                else 0.0
            )

            gross_margin_val = (
                round(
                    float(
                        (
                            gross_profit_val
                            / revenue_val
                        )
                        * Decimal("100")
                    ),
                    2,
                )
                if revenue_val > ZERO
                else 0.0
            )

            cogs_ratio_val = (
                round(
                    float(
                        (
                            cogs_final
                            / revenue_val
                        )
                        * Decimal("100")
                    ),
                    2,
                )
                if revenue_val > ZERO
                else 0.0
            )

            opex_ratio_val = (
                round(
                    float(
                        (
                            expenses_val
                            / revenue_val
                        )
                        * Decimal("100")
                    ),
                    2,
                )
                if revenue_val > ZERO
                else 0.0
            )

            # ==========================================================
            # 17. HISTORICAL CREDIT OUTSTANDING
            # ==========================================================

            credit_outstanding = ZERO

            try:
                all_sales = SaleTransaction.objects.all()
                all_repayments = CreditRepayment.objects.all()

                if branch_obj:
                    all_sales = all_sales.filter(
                        branch=branch_obj
                    )

                    if hasattr(
                        CreditRepayment,
                        "branch",
                    ):
                        all_repayments = (
                            all_repayments.filter(
                                Q(
                                    branch=branch_obj
                                )
                                | Q(
                                    transaction__branch=
                                    branch_obj
                                )
                                | Q(
                                    branch__isnull=True
                                )
                            )
                        )

                all_credit_transaction_ids = set(
                    all_repayments
                    .exclude(transaction_id=None)
                    .values_list(
                        "transaction_id",
                        flat=True,
                    )
                    .distinct()
                )

                sales_date_field = (
                    self._get_date_fieldname(
                        SaleTransaction
                    )
                )

                if sales_date_field:

                    sales_field = (
                        SaleTransaction._meta.get_field(
                            sales_date_field
                        )
                    )

                    if (
                        sales_field.get_internal_type()
                        == "DateTimeField"
                    ):
                        all_sales = all_sales.filter(
                            **{
                                f"{sales_date_field}__lte":
                                    end_dt
                            }
                        )
                    else:
                        all_sales = all_sales.filter(
                            **{
                                f"{sales_date_field}__lte":
                                    target_date
                            }
                        )

                all_repayments = all_repayments.filter(
                    date__lte=target_date
                )

                cumulative_credit_sales = ZERO

                all_sales = all_sales.prefetch_related(
                    "split_payments",
                )

                for credit_sale in all_sales:

                    credit_amount = (
                        self._get_sale_credit_amount(
                            credit_sale,
                            all_credit_transaction_ids,
                        )
                    )

                    cumulative_credit_sales += (
                        credit_amount
                    )

                cumulative_credit_sales = (
                    quantize_money(
                        cumulative_credit_sales
                    )
                )

                cumulative_repayments = ZERO

                for rep in all_repayments:

                    cumulative_repayments += (
                        quantize_money(
                            rep.amount or ZERO
                        )
                    )

                cumulative_repayments = (
                    quantize_money(
                        cumulative_repayments
                    )
                )

                credit_outstanding = quantize_money(
                    max(
                        cumulative_credit_sales
                        - cumulative_repayments,
                        ZERO,
                    )
                )

            except Exception:
                credit_outstanding = ZERO

            # ==========================================================
            # 18. RESPONSE
            # ==========================================================

            return Response(
                {
                    "summary": {
                        "period_type": period_type,
                        "target_date": target_date.isoformat(),
                        "branch_name": (
                            branch_obj.name
                            if branch_obj
                            else "All Branches"
                        ),

                        "revenue": float(
                            revenue_val
                        ),

                        "cogs": float(
                            cogs_final
                        ),

                        "gross_profit": float(
                            gross_profit_val
                        ),

                        "expenses": float(
                            quantize_money(
                                expenses_val
                            )
                        ),

                        "purchases_cost": float(
                            purchases_cost_val
                        ),

                        "net_profit": float(
                            net_profit_val
                        ),

                        "gross_margin_percent":
                            gross_margin_val,

                        "net_margin_percent":
                            margin_val,

                        "cogs_ratio_percent":
                            cogs_ratio_val,

                        "opex_ratio_percent":
                            opex_ratio_val,

                        "total_liquid_collections":
                            float(
                                quantize_money(
                                    total_liquid_collections
                                )
                            ),

                        "new_credit_sales":
                            float(
                                quantize_money(
                                    period_credit_sales_total
                                )
                            ),

                        "credit_repayments":
                            float(
                                quantize_money(
                                    period_credit_repayments_total
                                )
                            ),

                        "total_credit_repayments":
                            float(
                                quantize_money(
                                    period_credit_repayments_total
                                )
                            ),

                        "prior_period_credit_repayments":
                            float(
                                quantize_money(
                                    prior_period_credit_repayments_total
                                )
                            ),

                        "credit_outstanding":
                            float(
                                credit_outstanding
                            ),

                        "net_receivable_change":
                            float(
                                quantize_money(
                                    period_credit_sales_total
                                    - period_credit_repayments_total
                                )
                            ),

                        "same_day_credit_net_change":
                            float(
                                quantize_money(
                                    period_credit_sales_total
                                    - period_credit_repayments_total
                                )
                            ),
                    },

                    "payment_channel_breakdown": {

                        "cash": float(
                            quantize_money(
                                cash_total
                            )
                        ),

                        "credit": float(
                            quantize_money(
                                period_credit_sales_total
                            )
                        ),

                        "mpesa_std": float(
                            quantize_money(
                                mpesa_std_total
                            )
                        ),

                        "paybill": float(
                            quantize_money(
                                paybill_total
                            )
                        ),

                        "send_money": float(
                            quantize_money(
                                send_money_total
                            )
                        ),

                        "bank_transfer": float(
                            quantize_money(
                                bank_total
                            )
                        ),

                        "other": float(
                            quantize_money(
                                other_total
                            )
                        ),

                        "credit_repayments": float(
                            quantize_money(
                                period_credit_repayments_total
                            )
                        ),

                        "total_credit_repayments": float(
                            quantize_money(
                                period_credit_repayments_total
                            )
                        ),

                        "prior_period_credit_repayments": float(
                            quantize_money(
                                prior_period_credit_repayments_total
                            )
                        ),

                        "credit_outstanding": float(
                            credit_outstanding
                        ),
                    },

                    "inventory_summary": {
                        "total_stock_value": float(
                            quantize_money(
                                stock_val
                            )
                        ),

                        "total_stock_quantity": float(
                            stock_qty
                        ),

                        "low_stock_count":
                            low_stock_cnt,

                        "out_of_stock_count":
                            out_of_stock_cnt,

                        "damaged_stock_value": float(
                            quantize_money(
                                damaged_stock_val
                            )
                        ),

                        "returned_stock_value": float(
                            quantize_money(
                                returned_stock_val
                            )
                        ),
                    },

                    "item_analytics":
                        formatted_item_analytics,

                    "fast_moving_items":
                        fast_moving_items,

                    "slow_moving_items":
                        slow_moving_items,

                    "expenses_breakdown":
                        expenses_breakdown,

                    "receipt_details":
                        receipt_details,
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

class ManagementAnalyticsViewSet(viewsets.ViewSet):
    """
    Lightweight, high-performance API endpoints for executive management dashboards
    and detailed operational reporting tables.
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_branch_and_dates(self, request):
        branch_id = request.query_params.get('branch_id') or request.query_params.get('branch')
        start_date_str = request.query_params.get('start_date') or request.query_params.get('startDate')
        end_date_str = request.query_params.get('end_date') or request.query_params.get('endDate')
        month_str = request.query_params.get('month')
        year_str = request.query_params.get('year')

        branch = None
        if branch_id:
            if str(branch_id).isdigit():
                branch = RetailBranch.objects.filter(id=int(branch_id)).first()
            else:
                branch = RetailBranch.objects.filter(name__iexact=str(branch_id).strip()).first()

        start_date = None
        end_date = None

        if start_date_str and end_date_str:
            try:
                start_date = datetime.strptime(str(start_date_str).split('T')[0], "%Y-%m-%d").date()
                end_date = datetime.strptime(str(end_date_str).split('T')[0], "%Y-%m-%d").date()
            except ValueError:
                pass
        elif month_str and year_str and month_str.isdigit() and year_str.isdigit():
            m = int(month_str)
            y = int(year_str)
            start_date = date(y, m, 1)
            if m == 12:
                end_date = date(y, 12, 31)
            else:
                end_date = date(y, m + 1, 1) - timedelta(days=1)

        return branch, start_date, end_date

    def _normalize_payment_method(self, method_name):
        if not method_name:
            return "UNKNOWN"

        clean = (
            str(method_name)
            .strip()
            .upper()
            .replace("-", "_")
            .replace(" ", "_")
        )

        if any(k in clean for k in ["PAYBILL", "PAY_BILL", "C2B"]):
            return "PAYBILL"

        if any(k in clean for k in ["SEND_MONEY", "SENDMONEY", "TILL", "BUY_GOODS", "BUYGOODS", "P2P"]):
            return "SEND_MONEY"

        if "CASH" in clean:
            return "CASH"

        if any(k in clean for k in ["CREDIT", "DEBT", "ON_ACCOUNT"]):
            return "CREDIT"

        if any(k in clean for k in ["BANK", "EFT", "TRANSFER"]):
            return "BANK_TRANSFER"

        if any(k in clean for k in ["MPESA", "M_PESA", "MOBILE"]):
            return "MPESA"

        if "SPLIT" in clean:
            return "SPLIT"

        return clean

    def _get_split_payments(self, sale):
        relation = getattr(sale, "split_payments", None)
        if relation is not None:
            try:
                return list(relation.all())
            except Exception:
                pass
        return []

    def _extract_sp_method(self, sp):
        for attr in ["method", "payment_method", "payment_mode", "channel", "mode", "type"]:
            val = getattr(sp, attr, None)
            if val:
                return val
        return None

    @action(detail=False, methods=['get'], url_path='dashboard-cards')
    def dashboard_cards(self, request):
        branch, start_date, end_date = self._get_branch_and_dates(request)
        if not branch:
            branch = RetailBranch.objects.first()

        today = timezone.localdate()

        sale_items_qs = SaleItem.objects.filter(transaction__branch=branch)
        expenses_qs = Expense.objects.filter(branch=branch)

        if start_date and end_date:
            sale_items_qs = sale_items_qs.filter(transaction__sale_date__date__range=(start_date, end_date))
            expenses_qs = expenses_qs.filter(date__range=(start_date, end_date))
        else:
            sale_items_qs = sale_items_qs.filter(
                transaction__sale_date__year=today.year,
                transaction__sale_date__month=today.month
            )
            expenses_qs = expenses_qs.filter(
                date__year=today.year,
                date__month=today.month
            )

        fin_agg = sale_items_qs.aggregate(
            revenue=Coalesce(
                Sum(
                    ExpressionWrapper(
                        F('quantity_sold') * (F('unit_selling_price') - F('discount_per_item')),
                        output_field=MONEY_FIELD
                    ),
                    output_field=MONEY_FIELD
                ),
                ZERO, output_field=MONEY_FIELD
            ),
            cogs=Coalesce(
                Sum(
                    ExpressionWrapper(
                        F('quantity_sold') * F('unit_cost_price'),
                        output_field=MONEY_FIELD
                    ),
                    output_field=MONEY_FIELD
                ),
                ZERO, output_field=MONEY_FIELD
            )
        )

        total_expenses = expenses_qs.aggregate(
            total=Coalesce(Sum('amount', output_field=MONEY_FIELD), ZERO, output_field=MONEY_FIELD)
        )['total']

        revenue = fin_agg['revenue']
        cogs = fin_agg['cogs']
        gross_profit = revenue - cogs
        net_profit = gross_profit - total_expenses
        gross_margin = float((gross_profit / revenue * 100) if revenue > ZERO else Decimal("0.0"))

        pricings = BranchItemPricing.objects.filter(branch=branch).select_related('item')

        total_stock_value = ZERO
        low_stock_count = 0
        out_of_stock_count = 0

        for p in pricings:
            rem_stock = p.remaining_stock
            threshold = p.min_stock_threshold or Decimal("5.000")
            unit_cost = p.unit_cost_price or ZERO

            total_stock_value += (rem_stock * unit_cost)

            if rem_stock <= ZERO:
                out_of_stock_count += 1
            elif rem_stock <= threshold:
                low_stock_count += 1

        today_movements = StockMovement.objects.filter(branch=branch, date=today)

        stock_received_today = today_movements.filter(
            movement_type__in=[
                StockMovement.MovementType.PURCHASE,
                StockMovement.MovementType.TRANSFER_IN,
                StockMovement.MovementType.ADJUSTMENT_IN
            ]
        ).aggregate(
            total=Coalesce(
                Sum(
                    ExpressionWrapper(F('quantity') * F('unit_cost_price'), output_field=MONEY_FIELD),
                    output_field=MONEY_FIELD
                ), ZERO, output_field=MONEY_FIELD
            )
        )['total']

        stock_issued_today = today_movements.filter(
            movement_type__in=[
                StockMovement.MovementType.SALE,
                StockMovement.MovementType.TRANSFER_OUT,
                StockMovement.MovementType.DAMAGE,
                StockMovement.MovementType.ADJUSTMENT_OUT
            ]
        ).aggregate(
            total=Coalesce(
                Sum(
                    ExpressionWrapper(F('quantity') * F('unit_cost_price'), output_field=MONEY_FIELD),
                    output_field=MONEY_FIELD
                ), ZERO, output_field=MONEY_FIELD
            )
        )['total']

        stock_variances_count = today_movements.filter(
            movement_type__in=[
                StockMovement.MovementType.DAMAGE,
                StockMovement.MovementType.ADJUSTMENT_IN,
                StockMovement.MovementType.ADJUSTMENT_OUT
            ]
        ).values('item_id').distinct().count()

        today_sales_cash = SaleTransaction.objects.filter(
            branch=branch, sale_date__date=today
        ).aggregate(
            total=Coalesce(Sum('amount_paid', output_field=MONEY_FIELD), ZERO, output_field=MONEY_FIELD)
        )['total']

        today_repayments = CreditRepayment.objects.filter(
            branch=branch, date=today
        ).aggregate(
            total=Coalesce(Sum('amount', output_field=MONEY_FIELD), ZERO, output_field=MONEY_FIELD)
        )['total']

        todays_receipts = today_sales_cash + today_repayments

        today_expenses = Expense.objects.filter(
            branch=branch, date=today
        ).aggregate(
            total=Coalesce(Sum('amount', output_field=MONEY_FIELD), ZERO, output_field=MONEY_FIELD)
        )['total']

        today_supplier_payments = SupplierPayment.objects.filter(
            purchase__branch=branch, created_at__date=today
        ).aggregate(
            total=Coalesce(Sum('amount', output_field=MONEY_FIELD), ZERO, output_field=MONEY_FIELD)
        )['total']

        today_cash_purchases = Purchase.objects.filter(
            branch=branch, purchase_date__date=today
        ).exclude(
            payment_method=Purchase.PaymentMethod.CREDIT
        ).aggregate(
            total=Coalesce(
                Sum(
                    ExpressionWrapper(F('quantity') * F('unit_cost_price'), output_field=MONEY_FIELD),
                    output_field=MONEY_FIELD
                ), ZERO, output_field=MONEY_FIELD
            )
        )['total']

        todays_payments = today_expenses + today_supplier_payments + today_cash_purchases
        net_cash_movement = todays_receipts - todays_payments

        all_sales = SaleTransaction.objects.filter(branch=branch)

        cash_balance = all_sales.filter(
            Q(payment_method__iexact='CASH')
        ).aggregate(total=Coalesce(Sum('amount_paid'), ZERO))['total']

        total_outflows_historical = Expense.objects.filter(branch=branch).aggregate(
            t=Coalesce(Sum('amount'), ZERO)
        )['t']
        cash_balance = max(ZERO, cash_balance - total_outflows_historical)

        paybill_balance = all_sales.filter(
            Q(payment_method__icontains='PAYBILL') | Q(payment_method__icontains='TILL')
        ).aggregate(total=Coalesce(Sum('amount_paid'), ZERO))['total']

        send_money_balance = all_sales.filter(
            Q(payment_method__icontains='SEND_MONEY') | Q(payment_method__icontains='SEND MONEY')
        ).aggregate(total=Coalesce(Sum('amount_paid'), ZERO))['total']

        mpesa_general = all_sales.filter(
            Q(payment_method__iexact='MPESA') | Q(payment_method__iexact='M-PESA')
        ).aggregate(total=Coalesce(Sum('amount_paid'), ZERO))['total']
        paybill_balance += mpesa_general

        total_available_funds = cash_balance + paybill_balance + send_money_balance

        return Response({
            "branch_id": branch.id if branch else None,
            "branch_name": branch.name if branch else "",
            "profitability": {
                "total_sales": float(revenue),
                "total_sales_formatted": format_currency(revenue),
                "gross_profit": float(gross_profit),
                "gross_profit_formatted": format_currency(gross_profit),
                "gross_margin_percentage": round(gross_margin, 1),
                "operating_expenses": float(total_expenses),
                "operating_expenses_formatted": format_currency(total_expenses),
                "net_profit": float(net_profit),
                "net_profit_formatted": format_currency(net_profit)
            },
            "inventory": {
                "total_stock_value": float(total_stock_value),
                "total_stock_value_formatted": format_currency(total_stock_value),
                "product_skus": pricings.count(),
                "low_stock_items": low_stock_count,
                "out_of_stock_items": out_of_stock_count,
                "stock_received_today": float(stock_received_today),
                "stock_received_today_formatted": f"KSh {stock_received_today:,.0f}",
                "stock_issued_today": float(stock_issued_today),
                "stock_issued_today_formatted": f"KSh {stock_issued_today:,.0f}",
                "pending_transfers": 0,
                "stock_variances": stock_variances_count
            },
            "cash_flow": {
                "total_available_funds": float(total_available_funds),
                "total_available_funds_formatted": format_currency(total_available_funds),
                "todays_receipts": float(todays_receipts),
                "todays_receipts_formatted": format_currency(todays_receipts),
                "todays_payments": float(todays_payments),
                "todays_payments_formatted": format_currency(todays_payments),
                "net_cash_movement": float(net_cash_movement),
                "net_cash_movement_formatted": f"{'+' if net_cash_movement >= ZERO else ''}{format_currency(net_cash_movement)}"
            },
            "account_balances": {
                "cash_at_hand": float(cash_balance),
                "cash_at_hand_formatted": f"KSh {cash_balance:,.0f}",
                "paybill": float(paybill_balance),
                "paybill_formatted": f"KSh {paybill_balance:,.0f}",
                "send_money": float(send_money_balance),
                "send_money_formatted": f"KSh {send_money_balance:,.0f}"
            }
        })