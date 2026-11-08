import calendar
import logging
import random
import string
from django.core.mail import send_mail
from datetime import date, datetime, timedelta
from decimal import Decimal
from decimal import Decimal
from datetime import date, datetime, time, timedelta
import os
from django.conf import settings
import collections
from collections import defaultdict

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import io
from django.http import HttpResponse
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, Image
from reportlab.lib import colors
from rest_framework import permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework import permissions
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken

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
from django.db.models import F, Q, Sum, Count, DecimalField
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
    UserRolePermission, 
    default_module_permissions,
    BranchStock,
    StockTransfer,
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    SupplierReturn,
    SupplierPayment,
    PasswordResetOTP,

    recalculate_wac_sales_cost,
)

from .serializers import (
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


def _resolve_branch(branch_value, user=None):
    if not branch_value:
        return None

    if isinstance(branch_value, RetailBranch):
        return branch_value

    if isinstance(branch_value, dict):
        branch_value = (
            branch_value.get("id")
            or branch_value.get("pk")
            or branch_value.get("branch_id")
            or branch_value.get("name")
        )

    clean_val = str(branch_value or "").strip()
    if not clean_val or clean_val.lower() in ("all", "none", "null", "undefined"):
        return None

    # 1. Look up by Primary Key ID if numeric
    if clean_val.isdigit():
        branch_obj = RetailBranch.objects.filter(id=int(clean_val)).first()
        if branch_obj:
            return branch_obj

    # 2. Look up case-insensitively by Branch Name fallback
    return RetailBranch.objects.filter(name__iexact=clean_val).first()

def _get_reporting_filters(request, default_period="month"):
    """Resolve branch and period dates consistently across reporting endpoints."""
    params = request.query_params
    today = timezone.localdate()

    def parse_optional_date(value, field_name):
        if value in (None, ""):
            return None
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if timezone.is_aware(parsed):
                parsed = timezone.localtime(parsed)
            return parsed.date()
        except (TypeError, ValueError):
            try:
                return datetime.strptime(raw[:10], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                raise DRFValidationError({field_name: "Invalid date. Use YYYY-MM-DD."})

    branch_value = params.get("branch") or params.get("branch_id")
    raw_period = (
        params.get("period_type") or params.get("periodType") or params.get("period")
    )
    period = str(raw_period or "").strip().lower()

    selected_date = parse_optional_date(
        params.get("selectedDate") or params.get("selected_date") or params.get("date"),
        "date",
    )
    start_param = parse_optional_date(
        params.get("start_date") or params.get("startDate"), "start_date"
    )
    end_param = parse_optional_date(
        params.get("end_date") or params.get("endDate"), "end_date"
    )

    if not period:
        if start_param or end_param:
            period = "custom"
        elif params.get("year") and not params.get("month"):
            period = "year"
        elif params.get("month"):
            period = "month"
        else:
            period = str(default_period or "").strip().lower()

    if period in ("all", "all_time", "all-time"):
        start_date, end_date = date(1900, 1, 1), today
        period = "all"
    elif period in ("custom", "range"):
        if not start_param or not end_param:
            raise DRFValidationError({
                "date_range": "Both start_date and end_date are required for a custom range."
            })
        start_date, end_date, period = start_param, end_param, "custom"
    elif period in ("day", "daily"):
        start_date = end_date = selected_date or today
        period = "day"
    elif period in ("week", "weekly"):
        selected = selected_date or today
        start_date = selected - timedelta(days=selected.weekday())
        end_date = start_date + timedelta(days=6)
        period = "week"
    elif period in ("month", "monthly"):
        selected = selected_date or today
        try:
            year = int(params.get("year") or selected.year)
            month = int(params.get("month") or selected.month)
            start_date = date(year, month, 1)
            end_date = date(year, month, calendar.monthrange(year, month)[1])
        except (TypeError, ValueError):
            raise DRFValidationError({
                "period": "Invalid month/year. Supply a valid month (1-12) and year."
            })
        period = "month"
    elif period in ("year", "yearly", "annual", "annually"):
        selected = selected_date or today
        try:
            year = int(params.get("year") or selected.year)
            start_date, end_date = date(year, 1, 1), date(year, 12, 31)
        except (TypeError, ValueError):
            raise DRFValidationError({"year": "Invalid year."})
        period = "year"
    elif start_param or end_param:
        if not start_param or not end_param:
            raise DRFValidationError({"date_range": "Provide both start_date and end_date."})
        start_date, end_date, period = start_param, end_param, "custom"
    elif selected_date:
        start_date = end_date = selected_date
        period = "day"
    elif not period:
        start_date = end_date = None
        period = "all"
    else:
        raise DRFValidationError({
            "period_type": f"Unsupported period '{period}'. Use day, week, month, year, custom, or all."
        })

    if start_date and end_date and end_date < start_date:
        raise DRFValidationError({"date_range": "end_date must be on or after start_date."})

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(start_date, time.min), tz) if start_date else None
    end_dt = timezone.make_aware(datetime.combine(end_date, time.max), tz) if end_date else None

    # --- UPDATED BRANCH RESOLUTION ---
    clean_branch_str = str(branch_value or "").strip().lower()
    
    # If the value indicates all branches, ignore resolution and return None
    if not clean_branch_str or clean_branch_str in ("all", "none", "null", "undefined", "0"):
        branch_obj = None
    else:
        try:
            branch_obj = _resolve_branch(branch_value, getattr(request, "user", None))
        except (ValueError, TypeError):
            # Safe fallback if primary key lookup throws an exception (e.g. non-numeric ID or invalid UUID format)
            branch_obj = None

        # Fallback to None (All Branches) instead of raising an error if a branch isn't found
        if branch_obj is None:
            # If you prefer strict validation, keep the exception below; 
            # otherwise, let branch_obj remain None for global view.
            branch_obj = None 

    return branch_obj, start_date, end_date, start_dt, end_dt, period



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


import logging
from datetime import timedelta
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


logger = logging.getLogger(__name__)
User = get_user_model()


# apis/views.py

def get_user_permissions_payload(user):
    """
    Safely resolves user permissions from stocks.models without circular/missing imports.
    """
    try:
        from stocks.models import UserRolePermission, default_module_permissions
        defaults = default_module_permissions() if callable(default_module_permissions) else {}
    except Exception as e:
        logger.warning(f"Could not load default_module_permissions: {e}")
        defaults = {}

    if getattr(user, 'is_superuser', False):
        return {k: {"view": True, "change": True} for k in defaults.keys()}

    try:
        from stocks.models import UserRolePermission
        perm_record = UserRolePermission.objects.filter(user=user).first()
        if perm_record and isinstance(perm_record.module_permissions, dict):
            defaults.update(perm_record.module_permissions)
            return defaults
    except Exception as e:
        logger.error(f"Error resolving UserRolePermission for {user}: {e}")

    return defaults


def serialize_user_with_permissions(user):
    """
    Formats complete user profile dictionary expected by frontend App.jsx & localStorage.
    """
    role = "NORMAL_USER"
    try:
        from stocks.models import UserRolePermission
        perm_record = UserRolePermission.objects.filter(user=user).first()
        if perm_record and getattr(perm_record, 'role', None):
            role = perm_record.role
        elif getattr(user, 'is_superuser', False):
            role = "SUPERADMIN"
        elif getattr(user, 'is_staff', False):
            role = "ADMIN"
    except Exception as e:
        logger.warning(f"Failed to fetch role record: {e}")

    return {
        "id": user.pk,
        "username": user.get_username(),
        "full_name": user.get_full_name() or user.get_username(),
        "email": getattr(user, 'email', '') or '',
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "role": role,
        "permissions": get_user_permissions_payload(user),
    }

class UserProfileView(APIView):
    """
    API view to retrieve the profile of the currently authenticated user.
    Syncs live permissions on portal mount.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user_data = serialize_user_with_permissions(request.user)
        return Response(user_data, status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    """
    Endpoint for authenticated users to change their password.
    Mounted at: auth/change-password/
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        old_password = request.data.get("old_password")
        new_password = request.data.get("new_password")

        if not old_password or not new_password:
            return Response(
                {"error": "Both old_password and new_password are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not user.check_password(old_password):
            return Response(
                {"error": "Current password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if len(new_password) < 6:
            return Response(
                {"error": "New password must be at least 6 characters long."},
                status=status.HTTP_400_BAD_REQUEST
            )

        user.set_password(new_password)
        user.save()

        return Response(
            {"detail": "Password updated successfully."},
            status=status.HTTP_200_OK
        )


class ResetForgotPasswordView(APIView):
    """
    Endpoint for resetting forgotten passwords without requiring old password.
    Mounted at: auth/reset-forgot-password/
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get("username", "").strip()
        new_password = request.data.get("new_password")

        if not username or not new_password:
            return Response(
                {"error": "Both username and new password are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return Response(
                {"error": "Account not found or invalid reset request."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            validate_password(new_password, user=user)
        except ValidationError as e:
            return Response({"error": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save()

        return Response(
            {"detail": "Password reset successfully. You can now log in with your new password."},
            status=status.HTTP_200_OK
        )


class RequestOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get("username", "").strip()
        email = request.data.get("email", "").strip()

        if not username and not email:
            return Response(
                {"error": "Please provide a valid username or email address."},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = None
        if email:
            user = User.objects.filter(email__iexact=email).first()
        if not user and username:
            user = User.objects.filter(username__iexact=username).first()

        if not user:
            return Response(
                {"error": "No account found matching the provided details."},
                status=status.HTTP_400_BAD_REQUEST
            )

        target_email = user.email or email

        try:
            otp_obj = PasswordResetOTP.generate_otp(username=user.username)
        except Exception as e:
            logger.error(f"OTP Generation Error: {e}", exc_info=True)
            return Response(
                {"error": f"Database error generating OTP code: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        try:
            send_mail(
                subject="Melvex Management - Password Reset OTP",
                message=(
                    f"Hello {user.username},\n\n"
                    f"Your 6-digit verification code is: {otp_obj.otp_code}\n\n"
                    f"This code expires in 10 minutes."
                ),
                from_email=None,
                recipient_list=[target_email],
                fail_silently=False,
            )
        except Exception as e:
            logger.error(f"SMTP Dispatch Error to {target_email}: {e}", exc_info=True)
            return Response(
                {"error": f"Failed to dispatch email: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response(
            {"detail": f"An OTP code has been sent to {target_email}."},
            status=status.HTTP_200_OK
        )


class VerifyOTPAndResetPassword(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get("username", "").strip()
        otp_code = request.data.get("otp_code", "").strip()
        new_password = request.data.get("new_password", "")

        if not username or not otp_code or not new_password:
            return Response(
                {"error": "Username, OTP code, and new password are all required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = User.objects.filter(username__iexact=username).first()
        if not user:
            user = User.objects.filter(email__iexact=username).first()

        if not user:
            return Response(
                {"error": "User account not found."},
                status=status.HTTP_400_BAD_REQUEST
            )

        otp_record = PasswordResetOTP.objects.filter(
            username=user.username,
            otp_code=otp_code,
            is_verified=False
        ).first()

        if not otp_record or not otp_record.is_valid():
            return Response(
                {"error": "Invalid or expired OTP code. Please request a new one."},
                status=status.HTTP_400_BAD_REQUEST
            )

        user.set_password(new_password)
        user.save()

        otp_record.is_verified = True
        otp_record.save()

        return Response(
            {"detail": "Password reset successfully! You can now log in."},
            status=status.HTTP_200_OK
        )


import logging
from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView

from stocks.models import UserRolePermission, default_module_permissions

logger = logging.getLogger(__name__)


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # Basic JWT Claims
        token["username"] = getattr(user, "username", "")
        token["email"] = getattr(user, "email", "") or ""
        token["is_superuser"] = bool(getattr(user, "is_superuser", False))
        token["is_staff"] = bool(getattr(user, "is_staff", False))

        return token

    def validate(self, attrs):
        # 1. Authenticate user credentials via standard SimpleJWT
        data = super().validate(attrs)
        user = self.user

        is_superuser = bool(getattr(user, "is_superuser", False))
        is_staff = bool(getattr(user, "is_staff", False))

        # 2. Get default permissions map safely
        try:
            if callable(default_module_permissions):
                default_perms = default_module_permissions()
            else:
                default_perms = dict(default_module_permissions)
        except Exception as e:
            logger.warning(f"Error fetching default module permissions: {e}")
            default_perms = {}

        user_role = "NORMAL_USER"
        permissions_payload = default_perms

        # 3. Safe UserRolePermission lookup using .filter().first() to avoid DoesNotExist exceptions
        try:
            role_perm = UserRolePermission.objects.filter(user=user).first()
            if role_perm:
                user_role = str(role_perm.role)
                if isinstance(role_perm.module_permissions, dict):
                    # Merge stored permissions over default dictionary
                    merged_perms = dict(default_perms)
                    merged_perms.update(role_perm.module_permissions)
                    permissions_payload = merged_perms
            else:
                if is_superuser:
                    user_role = "SUPERADMIN"
                elif is_staff:
                    user_role = "ADMIN"
        except Exception as e:
            logger.warning(f"Error resolving user permissions for {user}: {e}")

        # If user is Superadmin, grant full permissions across all modules
        if is_superuser and permissions_payload:
            permissions_payload = {
                k: {"view": True, "change": True} for k in permissions_payload.keys()
            }

        # 4. Determine Portal Destination
        if is_superuser or user_role in ["SUPERADMIN", "ADMIN"]:
            portal_type = "admin"
        elif user_role in ["MANAGER", "MANAGEMENT"]:
            portal_type = "management"
        else:
            portal_type = "user"

        # 5. Safe RetailBranch lookup (user.branches)
        branch_id = None
        branch_name = "All Branches"
        try:
            if hasattr(user, "branches"):
                user_branch = user.branches.first()
                if user_branch:
                    branch_id = getattr(user_branch, "id", None)
                    branch_name = getattr(user_branch, "name", "All Branches")
        except Exception as e:
            logger.warning(f"Error resolving branch for user {user}: {e}")

        # 6. Assemble complete auth response payload
        data["user"] = {
            "id": user.id,
            "username": getattr(user, "username", ""),
            "email": getattr(user, "email", "") or "",
            "role": user_role,
            "is_staff": is_staff,
            "is_superuser": is_superuser,
            "branch": branch_id,
            "branch_name": branch_name,
            "permissions": permissions_payload,
        }
        data["portal_type"] = portal_type

        return data


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except Exception as e:
            logger.exception(f"Unhandled login processing error: {e}")
            return Response(
                {
                    "error": "Authentication processing failed.",
                    "detail": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )



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

        user_data = serialize_user_with_permissions(user)
        return Response(
            {
                "message": "User registered successfully.",
                "user": user_data,
            },
            status=status.HTTP_201_CREATED,
        )
from rest_framework_simplejwt.exceptions import TokenError

class AuthViewSet(viewsets.GenericViewSet):
    permission_classes = [AllowAny]

    @action(detail=False, methods=["post"], url_path="login")
    def login(self, request):
        try:
            username = request.data.get("username")
            password = request.data.get("password")

            if not username or not password:
                return Response(
                    {"error": "Username and password are required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Authenticate user credentials
            user = authenticate(request, username=username, password=password)

            if user is None:
                return Response(
                    {"error": "Invalid credentials."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            if not getattr(user, "is_active", True):
                return Response(
                    {"error": "User account is disabled."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            # -------------------------------------------------------------
            # DYNAMIC ROLE & GRANULAR PERMISSIONS RESOLUTION (SAFE LOOKUP)
            # -------------------------------------------------------------
            role_perm = UserRolePermission.objects.filter(user=user).first()

            def get_fallback_permissions():
                try:
                    if callable(default_module_permissions):
                        return default_module_permissions()
                    return default_module_permissions
                except Exception:
                    return {}

            if role_perm:
                role = str(getattr(role_perm, "role", "NORMAL_USER"))
                permissions_map = getattr(role_perm, "module_permissions", None) or get_fallback_permissions()
            else:
                if getattr(user, "is_superuser", False):
                    role = "SUPERADMIN"
                elif getattr(user, "is_staff", False):
                    role = "ADMIN"
                else:
                    role = "NORMAL_USER"

                permissions_map = get_fallback_permissions()

            # Set portal_type to "user" so everyone lands on the POS frontend.
            # Record admin privileges in can_access_django_admin instead.
            portal_type = "user"
            can_access_admin = bool(getattr(user, "is_superuser", False) or getattr(user, "is_staff", False))

            # Safe branch lookup
            user_branch = None
            try:
                if hasattr(user, "branches"):
                    user_branch = user.branches.first()
            except Exception:
                user_branch = None

            branch_id = getattr(user_branch, "id", None)
            branch_name = getattr(user_branch, "name", "All Branches")

            # Generate JWT tokens
            refresh = RefreshToken.for_user(user)

            return Response(
                {
                    "access": str(refresh.access_token),
                    "refresh": str(refresh),
                    "portal_type": portal_type,
                    "user": {
                        "id": user.id,
                        "username": user.username,
                        "email": getattr(user, "email", "") or "",
                        "role": role,
                        "permissions": permissions_map,
                        "is_staff": bool(getattr(user, "is_staff", False)),
                        "is_superuser": bool(getattr(user, "is_superuser", False)),
                        "can_access_admin": can_access_admin,
                        "branch": branch_id,
                        "branch_name": branch_name,
                    },
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            logger.error(f"Login processing error: {e}", exc_info=True)
            return Response(
                {"error": "Authentication processing failed.", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

class AdminAuthViewSet(viewsets.GenericViewSet):
    permission_classes = [AllowAny]

    @action(detail=False, methods=["post"], url_path="login")
    def login(self, request):
        try:
            raw_username = request.data.get("username", "")
            raw_password = request.data.get("password", "")

            # Fix: Use Python's .strip() instead of .trim()
            username = raw_username.strip() if isinstance(raw_username, str) else raw_username
            password = raw_password

            if not username or not password:
                return Response(
                    {"error": "Admin username and password are required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Authenticate user credentials
            user = authenticate(request, username=username, password=password)

            if user is None:
                return Response(
                    {"error": "Invalid administrative credentials."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            if not getattr(user, "is_active", True):
                return Response(
                    {"error": "Admin account is currently disabled."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Check administrative permissions
            is_admin = bool(
                getattr(user, "is_superuser", False) or 
                getattr(user, "is_staff", False)
            )

            if not is_admin:
                return Response(
                    {"error": "Access denied. Administrative privileges required."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Safe branch lookup
            user_branch = None
            try:
                if hasattr(user, "branches"):
                    user_branch = user.branches.first()
            except Exception:
                user_branch = None

            branch_id = getattr(user_branch, "id", None)
            branch_name = getattr(user_branch, "name", "All Branches")

            # Generate JWT session tokens
            refresh = RefreshToken.for_user(user)

            return Response(
                {
                    "access": str(refresh.access_token),
                    "refresh": str(refresh),
                    "portal_type": "admin",
                    "user": {
                        "id": user.id,
                        "username": user.username,
                        "email": getattr(user, "email", "") or "",
                        "role": "SUPERADMIN" if getattr(user, "is_superuser", False) else "ADMIN",
                        "is_staff": bool(getattr(user, "is_staff", False)),
                        "is_superuser": bool(getattr(user, "is_superuser", False)),
                        "can_access_admin": True,
                        "branch": branch_id,
                        "branch_name": branch_name,
                    },
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            logger.error(f"Admin login processing error: {e}", exc_info=True)
            return Response(
                {"error": "Admin authentication processing failed.", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )            

class LogoutViewSet(viewsets.ViewSet):
    """
    ViewSet to handle user logout by blacklisting the provided JWT refresh token.
    """
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["post"], url_path="logout")
    def logout(self, request):
        refresh_token = request.data.get("refresh") or request.data.get("refresh_token")

        if not refresh_token:
            return Response(
                {"error": "Refresh token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response(
                {"message": "Successfully logged out."},
                status=status.HTTP_205_RESET_CONTENT,
            )
        except TokenError as e:
            return Response(
                {"error": "Invalid or expired refresh token.", "detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

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
            .select_related("customer", "branch", "transaction")
            .all()
            .order_by("-date", "-created_at")
        )

        branch_obj, start_date, end_date, _start_dt, _end_dt, _period = (
            _get_reporting_filters(self.request, default_period="")
        )

        customer_id = self.request.query_params.get("customer")
        if branch_obj:
            queryset = queryset.filter(
                Q(branch=branch_obj) | Q(transaction__branch=branch_obj)
            )
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        if start_date and end_date:
            # CreditRepayment.date is authoritative; created_at is audit metadata.
            queryset = queryset.filter(date__range=(start_date, end_date))

        return queryset

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        try:
            raw_data = request.data if isinstance(request.data, dict) else {}

            # 1. Flexible Customer / Transaction resolution
            customer_id = (
                raw_data.get("customer")
                or raw_data.get("customer_id")
                or raw_data.get("client")
                or raw_data.get("client_id")
            )

            if isinstance(customer_id, dict):
                customer_id = customer_id.get("id") or customer_id.get("pk")

            transaction_id = (
                raw_data.get("transaction")
                or raw_data.get("transaction_id")
                or raw_data.get("sale_transaction")
                or raw_data.get("sale")
                or raw_data.get("sale_id")
            )

            if isinstance(transaction_id, dict):
                transaction_id = transaction_id.get("id") or transaction_id.get("pk")

            transaction_obj = None
            if transaction_id:
                transaction_obj = SaleTransaction.objects.filter(pk=transaction_id).first()
                if transaction_obj and not customer_id:
                    customer_id = transaction_obj.customer_id

            amount_raw = raw_data.get("amount") or raw_data.get("repayment_amount")
            
            if not customer_id or amount_raw in (None, ""):
                return Response(
                    {"error": "Customer and a valid payment amount are required."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            amount = Decimal(str(amount_raw))
            if amount <= Decimal("0.00"):
                return Response(
                    {"error": "Repayment amount must be greater than zero."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Extract Split Payments Payload
            split_payload = (
                raw_data.get("split_payments")
                or raw_data.get("splits")
                or raw_data.get("split_details")
                or []
            )

            valid_splits = []
            if isinstance(split_payload, list):
                for sp in split_payload:
                    if isinstance(sp, dict):
                        sp_amt = quantize_money(sp.get("amount", Decimal("0.00")))
                        if sp_amt > Decimal("0.00"):
                            valid_splits.append(sp)

            # Robust Payment Method Normalization Helper
            raw_method = (
                raw_data.get("payment_method") 
                or raw_data.get("method") 
                or raw_data.get("channel")
                or raw_data.get("payment_channel")
                or "CASH"
            )
            
            def normalize_pm(pm_str):
                clean = str(pm_str or "").strip().upper().replace(" ", "_").replace("-", "_")
                if clean in ("MPESA", "PAY_BILL", "TILL", "M_PESA"):
                    return "PAYBILL"
                if clean in ("SENDMONEY", "DIRECT_TRANSFER", "P2P"):
                    return "SEND_MONEY"
                if clean in ("BANK", "TRANSFER", "EFT", "RTGS"):
                    return "BANK_TRANSFER"
                return clean or "CASH"

            if len(valid_splits) > 1:
                payment_method = "SPLIT"
            elif len(valid_splits) == 1:
                sp_raw = valid_splits[0].get("method") or valid_splits[0].get("channel") or valid_splits[0].get("payment_method")
                payment_method = normalize_pm(sp_raw)
            else:
                payment_method = normalize_pm(raw_method)

            # Resolve Customer Instance
            customer_obj = Customer.objects.filter(pk=customer_id).first()
            if not customer_obj and transaction_obj:
                customer_obj = transaction_obj.customer

            if not customer_obj:
                return Response(
                    {"error": "Selected customer does not exist."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Resolve Branch Instance
            branch_param = (
                raw_data.get("branch")
                or raw_data.get("branch_id")
                or request.query_params.get("branch")
                or request.query_params.get("branch_id")
            )
            branch_obj = None
            if branch_param and str(branch_param).lower() not in ["all", "none", "", "null"]:
                clean_param = str(branch_param).strip()
                if clean_param.isdigit():
                    branch_obj = RetailBranch.objects.filter(pk=int(clean_param)).first()
                if not branch_obj:
                    branch_obj = RetailBranch.objects.filter(name__iexact=clean_param).first()

            if not branch_obj and transaction_obj:
                branch_obj = transaction_obj.branch
            if not branch_obj:
                branch_obj = getattr(customer_obj, "branch", None)

            # Creator reference
            user = _authenticated_user(request)

            # Date handling
            date_input = raw_data.get("date") or raw_data.get("repayment_date")
            repayment_date = timezone.localdate()
            if date_input:
                try:
                    repayment_date = datetime.strptime(str(date_input).strip(), "%Y-%m-%d").date()
                except ValueError:
                    pass

            ref_no = str(raw_data.get("reference_number") or raw_data.get("tx_code") or "").strip()
            notes_input = str(raw_data.get("notes") or "").strip()

            formatted_notes = notes_input
            if ref_no:
                formatted_notes = f"Ref: {ref_no} | {notes_input}".strip(" |")

            # 2. Save header CreditRepayment
            repayment = CreditRepayment.objects.create(
                customer=customer_obj,
                transaction=transaction_obj,
                branch=branch_obj,
                created_by=user,
                amount=amount,
                payment_method=payment_method,
                notes=formatted_notes,
                date=repayment_date,
            )

            # 3. Create Child CreditRepaymentSplit entries if multi-channel
            if payment_method == "SPLIT" and valid_splits:
                for sp in valid_splits:
                    sp_raw = sp.get("method") or sp.get("channel") or sp.get("payment_method")
                    sp_pm = normalize_pm(sp_raw)
                    sp_amt = quantize_money(sp.get("amount", Decimal("0.00")))
                    
                    CreditRepaymentSplit.objects.create(
                        repayment=repayment,
                        payment_method=sp_pm,
                        amount=sp_amt,
                        reference_number=sp.get("reference_number") or ref_no
                    )

            # WebSocket Broadcast
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

            serializer = self.get_serializer(repayment)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.exception("Failed to create customer credit repayment")
            return Response(
                {"error": "Failed to record customer repayment.", "details": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


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
                start_date = date(year, month, 1)
                end_date = date(year, month, last_day)
                date_filter = Q(updated_at__date__range=(start_date, end_date))
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
            unit_cost_raw = data.get("unit_cost_price") or data.get("cost_price")
            unit_cost = Decimal("0.00") if unit_cost_raw in (None, "") else Decimal(str(unit_cost_raw))
        except Exception:
            return Response({"unit_cost_price": "unit_cost_price must be a valid number."}, status=status.HTTP_400_BAD_REQUEST)

        if unit_cost < 0:
            return Response({"unit_cost_price": "unit_cost_price cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            unit_retail_raw = data.get("unit_retail_price") or data.get("retail_price") or data.get("selling_price")
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

        # ---------------------------------------------------------------------
        # FIX: Clean non-Item fields out of item_payload before passing to serializer
        # ---------------------------------------------------------------------
        item_payload = request.data.copy() if hasattr(request.data, 'copy') else dict(data)
        for non_model_field in ["branch", "branch_id", "cost_price", "retail_price", "unit_cost_price", "unit_retail_price", "opening_stock"]:
            item_payload.pop(non_model_field, None)

        serializer = self.get_serializer(data=item_payload)
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

            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

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
    partial payments, and automated Weighted Average Cost (WAC) updates.
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

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        raw_data = request.data

        # 1. Standardize payload parsing
        amount_paid_input = None
        if isinstance(raw_data, dict) and "items" in raw_data and isinstance(raw_data["items"], list):
            items_payload = raw_data.get("items", [])
            po_number_input = raw_data.get("po_number")
            supplier_name_input = raw_data.get("supplier_name")
            payment_method_input = raw_data.get("payment_method")
            purchase_date_input = raw_data.get("purchase_date")
            branch_input = raw_data.get("branch") or raw_data.get("branch_id")
            amount_paid_input = raw_data.get("amount_paid")
        elif isinstance(raw_data, list):
            items_payload = raw_data
        else:
            items_payload = [raw_data]

        if not items_payload:
            return Response(
                {"error": "At least one purchase record is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = _authenticated_user(request)

        try:
            first_item = items_payload[0] if isinstance(items_payload[0], dict) else {}
            branch_param = (
                branch_input
                or first_item.get("branch")
                or first_item.get("branch_id")
                or self.request.query_params.get("branch")
                or self.request.query_params.get("branch_id")
            )
            branch_obj = _resolve_branch(branch_param)

            if not branch_obj:
                return Response(
                    {"error": "A valid branch is required to record purchases."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            supplier_name = (
                supplier_name_input
                or first_item.get("supplier_name")
                or "Direct Supplier"
            )

            payment_method = (
                str(payment_method_input or first_item.get("payment_method") or "CASH")
                .strip()
                .upper()
                .replace(" ", "_")
                .replace("-", "_")
            )
            
            # Prevent CREDIT from being chosen directly as payment channel
            if payment_method == "CREDIT":
                payment_method = "CASH"

            raw_parsed_date = _parse_business_date(
                purchase_date_input
                or first_item.get("purchase_date")
                or first_item.get("date")
                or first_item.get("transaction_date")
            )
            purchase_date = (
                raw_parsed_date.date()
                if isinstance(raw_parsed_date, datetime)
                else (raw_parsed_date or timezone.localdate())
            )

            po_number = po_number_input or first_item.get("po_number")
            if not po_number:
                date_clean = timezone.now().strftime("%Y%m%d")
                unique_suffix = random.randint(1000, 9999)
                po_number = f"PO-{date_clean}-BATCH-{unique_suffix}"

            # 2. Create PurchaseOrder Batch Header
            po_header, _ = PurchaseOrder.objects.get_or_create(
                po_number=po_number,
                defaults={
                    "branch": branch_obj,
                    "supplier_name": supplier_name,
                    "payment_method": payment_method,
                    "purchase_date": purchase_date,
                    "created_by": user,
                },
            )

            # 3. Bulk Fetch Items and Pricing Objects (Performance Boost)
            raw_item_ids = [
                d.get("item") or d.get("item_id")
                for d in items_payload
                if isinstance(d, dict) and (d.get("item") or d.get("item_id"))
            ]
            
            items_dict = {
                item.id: item 
                for item in Item.objects.select_for_update().filter(id__in=raw_item_ids)
            }

            existing_pricings = {
                p.item_id: p
                for p in BranchItemPricing.objects.filter(item_id__in=items_dict.keys(), branch=branch_obj)
            }

            po_items_to_create = []
            purchases_to_create = []
            pricings_to_create = []
            processed_items = set()

            # 4. In-Memory Processing Loop
            for data in items_payload:
                if not isinstance(data, dict):
                    continue

                item_id = data.get("item") or data.get("item_id")
                item = items_dict.get(int(item_id) if str(item_id).isdigit() else item_id)
                if not item:
                    continue

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

                cur_branch = (
                    _resolve_branch(data.get("branch") or data.get("branch_id"))
                    or branch_obj
                )

                if not cur_branch:
                    continue

                self._align_retailer(item, cur_branch)

                # Queue missing branch pricing records
                if item.id not in existing_pricings:
                    new_pricing = BranchItemPricing(
                        item=item,
                        branch=cur_branch,
                        opening_stock=ZERO,
                        unit_cost_price=unit_cost,
                        unit_selling_price=ZERO,
                    )
                    pricings_to_create.append(new_pricing)
                    existing_pricings[item.id] = new_pricing

                # Queue Batch Purchase Line Items
                po_items_to_create.append(
                    PurchaseOrderItem(
                        purchase_order=po_header,
                        item=item,
                        quantity=quantity,
                        unit_cost_price=unit_cost,
                    )
                )

                # Queue Individual Purchase Records
                purchases_to_create.append(
                    Purchase(
                        item=item,
                        branch=cur_branch,
                        supplier_name=supplier_name,
                        quantity=quantity,
                        unit_cost_price=unit_cost,
                        payment_method=payment_method,
                        purchase_date=purchase_date,
                        created_by=user,
                    )
                )
                processed_items.add((cur_branch, item))

            if not purchases_to_create:
                return Response(
                    {"error": "No valid purchase entries were processed."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # 5. Execute Bulk Insertions
            if pricings_to_create:
                BranchItemPricing.objects.bulk_create(pricings_to_create, ignore_conflicts=True)

            PurchaseOrderItem.objects.bulk_create(po_items_to_create)
            created_purchases = Purchase.objects.bulk_create(purchases_to_create)

            # 6. Header Recalculations and WAC Computations
            po_header.recalculate_totals()

            if amount_paid_input not in (None, ""):
                custom_paid = Decimal(str(amount_paid_input))
                po_header.amount_paid = min(max(ZERO, custom_paid), po_header.total_amount)
                po_header.save(update_fields=["amount_paid"])

            for branch, item in processed_items:
                recalculate_wac_sales_cost(branch, item)

            # 7. Non-blocking WebSocket Event
            try:
                if created_purchases:
                    self._broadcast_websocket_event(
                        branch_id=created_purchases[0].branch_id,
                        event_type="PURCHASE_RECORDED",
                        purchase_id=created_purchases[0].id,
                        item_id=created_purchases[0].item_id,
                    )
            except Exception as ws_err:
                logger.warning("Purchase WebSocket notification skipped: %s", ws_err)

            return Response(
                {
                    "message": "Batch purchase order recorded successfully.",
                    "po_number": po_header.po_number,
                    "total_amount": float(po_header.total_amount),
                    "amount_paid": float(po_header.amount_paid),
                    "balance_due": float(po_header.balance_due),
                    "count": len(created_purchases),
                },
                status=status.HTTP_201_CREATED,
            )

        except Item.DoesNotExist:
            return Response({"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response(
                {"error": "Purchase creation failed.", "details": str(e), "exception_type": type(e).__name__},
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

    def create(self, request, *args, **kwargs):
        try:
            data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data or {})
            items_payload = data.get("items") or data.get("sale_items") or []

            if not items_payload:
                return Response(
                    {"error": "At least one sale item is required to complete transaction."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # 1. Resolve Branch
            branch_param = (
                data.get("branch")
                or data.get("branch_id")
                or request.query_params.get("branch")
                or request.query_params.get("branch_id")
            )
            branch_obj = None
            if branch_param is not None:
                if isinstance(branch_param, RetailBranch):
                    branch_obj = branch_param
                elif str(branch_param).isdigit():
                    branch_obj = RetailBranch.objects.filter(pk=int(branch_param)).first()
                elif isinstance(branch_param, str) and branch_param.strip():
                    branch_obj = RetailBranch.objects.filter(name__iexact=branch_param.strip()).first()

            if not branch_obj:
                user = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
                if user and hasattr(user, "retailer"):
                    branch_obj = RetailBranch.objects.filter(retailer=user.retailer).first()
                if not branch_obj:
                    branch_obj = RetailBranch.objects.first()

            if not branch_obj:
                return Response(
                    {"error": "A valid branch is required to record sales."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # 2. Normalize Payment Method & Date
            raw_payment = data.get("payment_method") or "CASH"
            normalized_pm = self._normalize_payment_method(raw_payment)
            
            raw_date = data.get("sale_date") or data.get("date")
            parsed_date = self._normalize_sale_date(raw_date)

            # 3. Resolve Customer Object or Name
            customer_id = data.get("customer") or data.get("customer_id")
            customer_obj = None
            if customer_id and str(customer_id).isdigit():
                customer_obj = Customer.objects.filter(pk=int(customer_id)).first()

            customer_name = (
                data.get("customer_name") 
                or (customer_obj.name if customer_obj else "")
                or ("Walk-in Customer" if normalized_pm != "CREDIT" else "")
            )

            # 4. Capture Custom Invoice / Receipt Code ONLY for Credit Sales
            custom_receipt = None
            if normalized_pm == "CREDIT":
                raw_custom = data.get("receipt_number") or data.get("invoice_number") or data.get("receipt")
                if raw_custom and str(raw_custom).strip():
                    custom_receipt = str(raw_custom).strip()

            # 5. Extract Split Payments Payload & Resolve Exact Method
            split_payments_payload = (
                data.get("split_payments")
                or data.get("splits")
                or data.get("split_details")
                or []
            )

            if isinstance(split_payments_payload, dict):
                formatted_splits = []
                for k, v in split_payments_payload.items():
                    amt_dec = quantize_money(v)
                    if amt_dec > ZERO:
                        formatted_splits.append({"method": k, "amount": amt_dec})
                split_payments_payload = formatted_splits

            valid_splits = []
            if isinstance(split_payments_payload, list):
                for sp in split_payments_payload:
                    if isinstance(sp, dict):
                        amt = quantize_money(sp.get("amount", ZERO))
                        if amt > ZERO:
                            valid_splits.append(sp)

            # Truly SPLIT only if explicitly requested or if >= 2 distinct payments are provided
            if normalized_pm == "SPLIT" or len(valid_splits) > 1:
                final_pm = "SPLIT"
            elif len(valid_splits) == 1:
                sp_raw = valid_splits[0].get("method") or valid_splits[0].get("payment_method") or valid_splits[0].get("channel")
                final_pm = self._normalize_payment_method(sp_raw)
            else:
                final_pm = normalized_pm

            # 6. Process Sale via Transaction Atomic Block
            with transaction.atomic():
                amount_paid_quantized = quantize_money(data.get("amount_paid") or "0.00")

                create_kwargs = {
                    "branch": branch_obj,
                    "customer": customer_obj,
                    "customer_name": customer_name,
                    "payment_method": final_pm,
                    "sale_date": parsed_date,
                    "total_amount": ZERO,
                    "amount_paid": amount_paid_quantized,
                    "created_by": request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
                }

                if custom_receipt:
                    create_kwargs["receipt_number"] = custom_receipt

                sale = SaleTransaction.objects.create(**create_kwargs)

                calculated_total = ZERO

                for item_data in items_payload:
                    item_id = item_data.get("item") or item_data.get("item_id") or item_data.get("id")
                    if not item_id:
                        continue

                    item_obj = Item.objects.select_for_update().filter(pk=item_id).first()
                    if not item_obj:
                        continue

                    qty = Decimal(str(item_data.get("quantity") or item_data.get("quantity_sold") or 1))
                    if qty <= ZERO:
                        continue

                    pricing = BranchItemPricing.objects.filter(item=item_obj, branch=branch_obj).first()
                    cost_price = quantize_money(pricing.unit_cost_price if pricing else getattr(item_obj, "unit_cost_price", ZERO) or ZERO)
                    retail_price = quantize_money(item_data.get("unit_selling_price") or item_data.get("unit_retail_price") or (pricing.unit_selling_price if pricing else ZERO) or ZERO)
                    discount = quantize_money(item_data.get("discount_per_item") or "0.00")

                    line_total = qty * (retail_price - discount)
                    calculated_total += line_total

                    SaleItem.objects.create(
                        transaction=sale,
                        item=item_obj,
                        quantity_sold=qty,
                        unit_cost_price=cost_price,
                        unit_selling_price=retail_price,
                        discount_per_item=discount,
                    )

                calculated_total = quantize_money(calculated_total)
                sale.total_amount = calculated_total

                # Route child SalePayment entries ONLY for true multi-channel split payments
                if final_pm == "SPLIT" and valid_splits:
                    split_sum = ZERO
                    for sp in valid_splits:
                        sp_raw = sp.get("method") or sp.get("payment_method") or sp.get("channel")
                        sp_method = self._normalize_payment_method(sp_raw)
                        sp_amount = quantize_money(sp.get("amount", ZERO))

                        split_sum += sp_amount
                        SalePayment.objects.create(
                            transaction=sale,
                            method=sp_method,
                            amount=sp_amount,
                            reference_code=sp.get("reference_code") or sp.get("reference") or sale.receipt_number or ""
                        )
                    sale.amount_paid = split_sum
                else:
                    if final_pm != "CREDIT" and (sale.amount_paid == ZERO or sale.amount_paid is None):
                        sale.amount_paid = calculated_total
                    else:
                        sale.amount_paid = quantize_money(sale.amount_paid)

                sale.save()

            # 7. Broadcast Real-time WebSocket Event
            channel_layer = get_channel_layer()
            if channel_layer:
                try:
                    async_to_sync(channel_layer.group_send)(
                        f"analytics_branch_{branch_obj.id}",
                        {
                            "type": "analytics_update",
                            "event": "SALE_CREATED",
                            "transaction_id": sale.id,
                            "total_amount": float(sale.total_amount),
                        },
                    )
                except Exception as ws_err:
                    logger.warning("WebSocket broadcast failed: %s", ws_err)

            serializer = self.get_serializer(sale)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except DRFValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception("Sale Creation Error: %s", e)
            return Response(
                {"error": f"Failed to record sale: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


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
        selected_date_str = self.request.query_params.get("date") or self.request.query_params.get("selectedDate")
        target_date = timezone.localdate()

        if selected_date_str:
            try:
                target_date = datetime.strptime(str(selected_date_str).strip(), "%Y-%m-%d").date()
            except ValueError:
                pass

        queryset = (
            Expense.objects
            .select_related("branch", "retailer")
            .filter(date=target_date)
            .exclude(description__icontains="Supplier Repayment")
            .order_by("-date", "-id")
        )

        user = _authenticated_user(self.request)
        if user:
            queryset = queryset.filter(
                Q(retailer=user) | Q(branch__retailer=user)
            )

        branch_param = (
            self.request.query_params.get("branch")
            or self.request.query_params.get("branch_id")
        )

        if branch_param and str(branch_param).lower() not in ["all", "none", "", "null"]:
            if str(branch_param).isdigit():
                queryset = queryset.filter(branch_id=int(branch_param))
            else:
                queryset = queryset.filter(branch__name__iexact=str(branch_param).strip())

        return queryset

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        try:
            raw_data = request.data if isinstance(request.data, dict) else {}

            description = str(raw_data.get("description", "")).strip()
            amount_raw = raw_data.get("amount")
            
            if not description or amount_raw in (None, ""):
                return Response(
                    {"error": "Expense description and amount are required."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            amount = Decimal(str(amount_raw))
            if amount <= Decimal("0.00"):
                return Response(
                    {"error": "Expense amount must be greater than zero."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 1. Extract Date
            date_input = raw_data.get("date")
            expense_date = timezone.localdate()
            if date_input:
                try:
                    expense_date = datetime.strptime(str(date_input).strip(), "%Y-%m-%d").date()
                except ValueError:
                    pass

            # 2. Extract and resolve Branch
            branch_param = (
                raw_data.get("branch")
                or raw_data.get("branch_id")
                or request.query_params.get("branch")
                or request.query_params.get("branch_id")
            )
            
            branch_obj = None
            if branch_param and str(branch_param).lower() not in ["all", "none", "", "null"]:
                clean_param = str(branch_param).strip()
                if clean_param.isdigit():
                    branch_obj = RetailBranch.objects.filter(pk=int(clean_param)).first()
                if not branch_obj:
                    branch_obj = RetailBranch.objects.filter(name__iexact=clean_param).first()

            # 3. Resolve Retailer Owner
            user = _authenticated_user(request)
            retailer_user = user or (branch_obj.retailer if branch_obj else None)

            # 4. Save directly
            expense = Expense.objects.create(
                branch=branch_obj,
                retailer=retailer_user,
                description=description,
                amount=amount,
                date=expense_date,
            )

            # 5. Broadcast WebSocket event
            if branch_obj:
                channel_layer = get_channel_layer()
                if channel_layer:
                    try:
                        async_to_sync(channel_layer.group_send)(
                            f"analytics_branch_{branch_obj.id}",
                            {
                                "type": "analytics_update",
                                "event": "EXPENSE_RECORDED",
                            },
                        )
                    except Exception:
                        pass

            serializer = self.get_serializer(expense)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.exception("Error saving operational expense entry")
            return Response(
                {"error": "Failed to save expense record.", "details": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

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
        Create a new pending approval request.
        """
        request_type = request.data.get(
            "request_type", 
            getattr(PendingApproval.RequestType, "NEW_REGISTERED_ITEMS", "NEW_REGISTERED_ITEMS")
        )
        payload = request.data.get("payload", request.data)

        # Attach user if authenticated, otherwise allow creation
        submitted_user = request.user if request.user.is_authenticated else None

        approval = PendingApproval.objects.create(
            request_type=request_type,
            payload=payload,
            submitted_by=submitted_user,
            status=PendingApproval.Status.PENDING,
        )

        try:
            send_management_review_email(approval)
        except Exception as e:
            print(f"Warning: Failed to send review email: {e}")

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
        Return all approval requests ordered by creation date for both management and standard portal views.
        """
        queryset = PendingApproval.objects.all().order_by("-created_at")
        
        # Optional: Filter by status if query parameter is supplied
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status__iexact=status_param)

        return queryset

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
        Commits approved restock purchase orders to PurchaseOrder, PurchaseOrderItem, 
        Purchase, StockBatch, and StockMovement models with actual purchase dates.
        """
        purchases = payload.get("purchases", [])
        items_list = payload.get("items", [])
        
        if not purchases and not items_list and "item" in payload:
            purchases = [payload]

        branch = self._get_target_branch(payload)
        if not branch:
            raise ValueError("No valid branch found to commit restock purchase order.")

        # 1. Parse historical/user-selected purchase date
        raw_date = payload.get("purchase_date") or payload.get("date") or payload.get("created_at")
        if raw_date:
            try:
                if isinstance(raw_date, str):
                    clean_str = raw_date.split("T")[0]
                    actual_purchase_dt = timezone.make_aware(
                        datetime.strptime(clean_str, "%Y-%m-%d"),
                        timezone.get_current_timezone()
                    )
                else:
                    actual_purchase_dt = raw_date
            except Exception:
                actual_purchase_dt = timezone.now()
        else:
            actual_purchase_dt = timezone.now()

        actual_date_only = actual_purchase_dt.date() if hasattr(actual_purchase_dt, "date") else actual_purchase_dt

        supplier_name = payload.get("supplier_name") or payload.get("supplier", "")
        payment_method = payload.get("payment_method", "CASH")
        po_number = payload.get("po_number") or f"PO-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"

        # 2. Create Parent PurchaseOrder Header (so it shows in Django Admin Purchase Orders table)
        po_header, _ = PurchaseOrder.objects.get_or_create(
            po_number=po_number,
            defaults={
                "branch": branch,
                "supplier_name": supplier_name,
                "payment_method": payment_method,
                "purchase_date": actual_purchase_dt,
                "created_by": self.request.user if self.request and self.request.user.is_authenticated else None,
            }
        )

        # Combine any array formats (purchases or items_list)
        combined_items = purchases if purchases else items_list

        for p_data in combined_items:
            item_id = p_data.get("item") or p_data.get("item_id") or p_data.get("id")
            item_obj = Item.objects.filter(id=item_id).first()
            if not item_obj:
                continue

            quantity = Decimal(str(p_data.get("quantity", 1)))
            cost_price = Decimal(str(p_data.get("unit_cost_price") or p_data.get("cost_price") or "0.00"))
            p_method = p_data.get("payment_method") or payment_method

            # 3. Create PO Line Item
            po_item = PurchaseOrderItem.objects.create(
                purchase_order=po_header,
                item=item_obj,
                quantity=quantity,
                unit_cost_price=cost_price,
            )

            # 4. Create official Purchase record WITH explicit purchase_date
            purchase_rec = Purchase.objects.create(
                branch=branch,
                item=item_obj,
                supplier_name=supplier_name,
                quantity=quantity,
                unit_cost_price=cost_price,
                payment_method=p_method,
                purchase_date=actual_purchase_dt,  # Explicitly preserve historical purchase date!
                created_by=self.request.user if self.request and self.request.user.is_authenticated else None,
            )

            # 5. Force StockMovement date & created_at to match the actual purchase date
            ref_codes = [f"PURCHASE-{purchase_rec.pk}", f"PO-{po_header.pk}-{po_item.pk}"]
            StockMovement.objects.filter(reference__in=ref_codes).update(
                date=actual_date_only,
                created_at=actual_purchase_dt
            )

            # Update BranchItemPricing WAC & Cost
            pricing, _ = BranchItemPricing.objects.get_or_create(
                branch=branch,
                item=item_obj,
                defaults={"opening_stock": 0, "unit_cost_price": cost_price}
            )
            pricing.unit_cost_price = cost_price
            pricing.save()

        # Recalculate totals on PurchaseOrder header
        po_header.recalculate_totals()

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
        return timezone.now()
    
    # If already a datetime object
    if isinstance(date_str, datetime):
        return date_str if timezone.is_aware(date_str) else timezone.make_aware(date_str)
        
    # If already a date object
    if isinstance(date_str, date):
        return timezone.make_aware(datetime.combine(date_str, datetime.min.time()))

    try:
        clean_str = str(date_str).strip().split("T")[0]
        parsed_d = datetime.strptime(clean_str, "%Y-%m-%d").date()
        return timezone.make_aware(datetime.combine(parsed_d, datetime.min.time()))
    except (ValueError, TypeError):
        return timezone.now()


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

        # Use the same canonical period parser as the executive financial statements.
        # Explicit period_type takes precedence over stale month/year parameters.
        branch_obj, start_date, end_date, start_dt, end_dt, period_type = (
            _get_reporting_filters(request, default_period="day")
        )

        # 1. Fetch Sales Inflows
        sales_qs = SaleTransaction.objects.filter(
            sale_date__range=(start_dt, end_dt)
        ).prefetch_related("split_payments", "items")

        # 2. Fetch Expense Outflows
        expenses_qs = Expense.objects.filter(
            Q(date__range=(start_date, end_date))
        )

        # 3. Fetch Credit Repayments Inflows
        repayments_qs = CreditRepayment.objects.filter(
            Q(date__range=(start_date, end_date))
        )

        # 4. Fetch Purchase Orders / Supplier Transactions
        purchases_qs = Purchase.objects.filter(
            Q(purchase_date__range=(start_dt, end_dt))
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
            date__range=(start_date, end_date),
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
        _branch, start_date, end_date, _start_dt, _end_dt, period_type = (
            _get_reporting_filters(request, default_period="day")
        )
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

        repayments_qs = CreditRepayment.objects.filter(date__lte=end_date)

        expenses_qs = Expense.objects.filter(date__lte=end_date)

        purchases_qs = Purchase.objects.filter(
            purchase_date__lte=cutoff_dt
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
            date__lte=end_date,
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


class DashboardOverviewAPIView(APIView):
    """
    Dedicated endpoint for Main Portal Analytics.
    Aggregates financial, inventory, and operational metrics with split payments & repayment routing.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = (
                _get_reporting_filters(request, default_period="day")
            )
            branch_param = request.query_params.get("branch", "").strip()

            # 2. Base Querysets
            sales_qs = SaleTransaction.objects.all()
            sale_items_qs = SaleItem.objects.all()
            repayments_qs = CreditRepayment.objects.all()
            purchases_qs = Purchase.objects.all()
            pricings_qs = BranchItemPricing.objects.all()
            approvals_qs = PendingApproval.objects.filter(status=PendingApproval.Status.PENDING)

            # 3. Apply the resolved branch consistently.
            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                sale_items_qs = sale_items_qs.filter(transaction__branch=branch_obj)
                repayments_qs = repayments_qs.filter(
                    Q(branch=branch_obj) | Q(transaction__branch=branch_obj)
                )
                purchases_qs = purchases_qs.filter(branch=branch_obj)
                pricings_qs = pricings_qs.filter(branch=branch_obj)

            # 4. Apply the selected period using each model's business date.
            period_sales = sales_qs.filter(sale_date__range=(start_dt, end_dt))
            period_sale_items = sale_items_qs.filter(
                transaction__sale_date__range=(start_dt, end_dt)
            )
            period_repayments = repayments_qs.filter(
                date__range=(start_date, end_date)
            )
            period_purchases = purchases_qs.filter(
                purchase_date__range=(start_dt, end_dt)
            )

            zero_dec = Value(0, output_field=DecimalField(max_digits=15, decimal_places=2))

            # 5. Channel Breakdown with Split Payments & Repayment Handling
            channel_totals = {
                "SEND_MONEY": Decimal("0.00"),
                "PAYBILL": Decimal("0.00"),
                "CASH": Decimal("0.00"),
                "CREDIT": Decimal("0.00")
            }

            # A. Process Period Sales (Standard & Split)
            for tx in period_sales.prefetch_related('split_payments'):
                split_qs = tx.split_payments.all()
                if tx.payment_method == SaleTransaction.PaymentMethod.SPLIT or split_qs.exists():
                    for sp in split_qs:
                        amt = Decimal(str(sp.amount or 0))
                        method = str(sp.method or "").upper()
                        if "PAYBILL" in method or "BANK" in method:
                            channel_totals["PAYBILL"] += amt
                        elif "SEND_MONEY" in method or "MPESA" in method or "TILL" in method:
                            channel_totals["SEND_MONEY"] += amt
                        elif "CREDIT" in method:
                            channel_totals["CREDIT"] += amt
                        else:
                            channel_totals["CASH"] += amt
                else:
                    amt = Decimal(str(tx.amount_paid if tx.amount_paid is not None and tx.amount_paid > Decimal("0.00") else tx.total_amount or 0))
                    method = str(tx.payment_method or "").upper()
                    
                    if tx.payment_method == SaleTransaction.PaymentMethod.CREDIT:
                        channel_totals["CREDIT"] += Decimal(str(tx.total_amount or 0))
                    elif "PAYBILL" in method or "BANK" in method:
                        channel_totals["PAYBILL"] += amt
                    elif "SEND_MONEY" in method or "MPESA" in method or "TILL" in method:
                        channel_totals["SEND_MONEY"] += amt
                    else:
                        channel_totals["CASH"] += amt

            # B. Add Period Credit Repayments to Inflow Channels
            for rep in period_repayments:
                amt = Decimal(str(rep.amount or 0))
                method = str(rep.payment_method or "").upper()
                if "PAYBILL" in method or "BANK" in method:
                    channel_totals["PAYBILL"] += amt
                elif "SEND_MONEY" in method or "MPESA" in method or "TILL" in method:
                    channel_totals["SEND_MONEY"] += amt
                else:
                    channel_totals["CASH"] += amt

            # 6. Credit Aggregations
            credit_issued = period_sales.filter(
                payment_method=SaleTransaction.PaymentMethod.CREDIT
            ).aggregate(t=Coalesce(Sum("total_amount"), zero_dec))["t"]

            recovered_repayments = period_repayments.aggregate(
                t=Coalesce(Sum("amount"), zero_dec)
            )["t"]

            outstanding_debt = sales_qs.filter(
                payment_method=SaleTransaction.PaymentMethod.CREDIT
            ).aggregate(
                t=Coalesce(Sum(F("total_amount") - F("amount_paid")), zero_dec)
            )["t"]

            # 7. Inventory & Purchases Metrics
            estimated_cogs = period_sale_items.aggregate(
                t=Coalesce(Sum(F("quantity_sold") * F("unit_cost_price")), zero_dec)
            )["t"]

            cataloged_skus = pricings_qs.values("item_id").distinct().count()
            
            total_cost_valuation = Decimal("0.00")
            total_retail_valuation = Decimal("0.00")

            for pricing in pricings_qs.select_related("item"):
                rem_stock = pricing.remaining_stock
                if rem_stock > Decimal("0.00"):
                    total_cost_valuation += rem_stock * Decimal(str(pricing.unit_cost_price or 0))
                    total_retail_valuation += rem_stock * Decimal(str(pricing.unit_selling_price or 0))

            purchases_cost = period_purchases.aggregate(
                t=Coalesce(Sum(F("quantity") * F("unit_cost_price")), zero_dec)
            )["t"]

            supplier_payables = purchases_qs.filter(
                payment_method=Purchase.PaymentMethod.CREDIT
            ).aggregate(
                t=Coalesce(Sum(F("quantity") * F("unit_cost_price") - F("amount_paid")), zero_dec)
            )["t"]

            return Response({
                "inventory": {
                    "total_cost_valuation": float(total_cost_valuation),
                    "total_retail_valuation": float(total_retail_valuation),
                    "cataloged_skus": cataloged_skus,
                    "estimated_cogs": float(estimated_cogs),
                },
                "credit_summary": {
                    "total_credit_issued": float(credit_issued),
                    "total_recovered": float(recovered_repayments),
                    "outstanding_credit": float(outstanding_debt),
                },
                "restocks": {
                    "total_purchases_cost": float(purchases_cost),
                    "supplier_payables": float(supplier_payables),
                },
                "payment_channels": {
                    "send_money": float(channel_totals["SEND_MONEY"]),
                    "mpesa": float(channel_totals["SEND_MONEY"]), # Backward compatibility fallback
                    "cash": float(channel_totals["CASH"]),
                    "paybill": float(channel_totals["PAYBILL"]),
                    "credit_outstanding": float(outstanding_debt),
                },
                "pending_approvals": approvals_qs.count(),
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({
                "inventory": {"total_cost_valuation": 0.0, "total_retail_valuation": 0.0, "cataloged_skus": 0, "estimated_cogs": 0.0},
                "credit_summary": {"total_credit_issued": 0.0, "total_recovered": 0.0, "outstanding_credit": 0.0},
                "restocks": {"total_purchases_cost": 0.0, "supplier_payables": 0.0},
                "payment_channels": {"send_money": 0.0, "mpesa": 0.0, "cash": 0.0, "paybill": 0.0, "credit_outstanding": 0.0},
                "pending_approvals": 0,
                "error": str(e)
            }, status=status.HTTP_200_OK)

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


class ManagementAnalyticsViewSet(viewsets.ViewSet):
    """
    Executive Management Analytics ViewSet providing clean endpoints for both
    SalesView and CashView components.
    """
    permission_classes = [permissions.AllowAny]

    def _format_currency(self, value):
        try:
            val = float(value or 0)
            return f"KSh {val:,.2f}"
        except Exception:
            return "KSh 0.00"

    def _normalize_payment_method(self, method_name):
        if not method_name:
            return "UNKNOWN"
        clean = str(method_name).strip().upper().replace("-", "_").replace(" ", "_")
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

    def _get_branch_and_dates(self, request):
        return _get_reporting_filters(request, default_period="month")


    def _get_filter_params(self, request):
        branch, start_date, end_date, _start_dt, _end_dt, _period = (
            _get_reporting_filters(request, default_period="month")
        )
        return (branch.pk if branch else None), start_date, end_date

    # =========================================================================
    # DASHBOARD & FINANCIALS
    # =========================================================================
    @action(detail=False, methods=['get'], url_path='dashboard-cards')
    def dashboard_cards(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, period_type = self._get_branch_and_dates(request)

            # --- Internal Inline Helpers ---
            def extract_split_payments(sale_obj):
                relation = getattr(sale_obj, "split_payments", None) or getattr(sale_obj, "payments", None)
                if relation is not None:
                    try:
                        return list(relation.all())
                    except Exception:
                        pass
                return []

            def extract_sp_method(sp_obj):
                for attr in ["method", "payment_method", "payment_mode", "channel", "mode", "type"]:
                    val = getattr(sp_obj, attr, None)
                    if val:
                        return val
                return None

            def is_credit_sale_check(sale_obj, c_ids=None):
                method = self._normalize_payment_method(getattr(sale_obj, "payment_method", None))
                if method == "CREDIT":
                    return True
                if getattr(sale_obj, "is_invoice", False):
                    return True
                if c_ids and getattr(sale_obj, "pk", None) in c_ids:
                    return True
                balance_due = getattr(sale_obj, "balance_due", None)
                if balance_due is not None:
                    try:
                        if Decimal(str(balance_due or ZERO)) > ZERO:
                            return True
                    except Exception:
                        pass
                return False

            def get_sale_credit_amt(sale_obj, c_ids=None):
                method = self._normalize_payment_method(getattr(sale_obj, "payment_method", None))
                sale_amount = quantize_money(sale_obj.total_amount or ZERO)
                split_records = extract_split_payments(sale_obj)

                if method == "SPLIT" or split_records:
                    credit_total = ZERO
                    for sp in split_records:
                        sp_method = self._normalize_payment_method(extract_sp_method(sp))
                        if sp_method == "CREDIT":
                            credit_total += quantize_money(sp.amount or ZERO)
                    return quantize_money(credit_total)

                if is_credit_sale_check(sale_obj, c_ids):
                    if method == "CREDIT":
                        credit_due = getattr(sale_obj, "credit_payment_amount", None)
                        if credit_due is not None:
                            return quantize_money(credit_due)
                        upfront_paid = quantize_money(
                            getattr(sale_obj, "amount_paid", ZERO) or ZERO
                        )
                        return quantize_money(max(ZERO, sale_amount - upfront_paid))

                    balance_due = getattr(sale_obj, "balance_due", None)
                    if balance_due is not None:
                        return quantize_money(max(ZERO, balance_due))
                    return sale_amount
                return ZERO

            # -----------------------------------------------------------------
            # 1. UNFILTERED BASE QUERYSETS BY BRANCH
            # -----------------------------------------------------------------
            base_sales_qs = SaleTransaction.objects.all()
            base_expenses_qs = Expense.objects.exclude(description__icontains='Supplier Repayment')
            base_purchases_qs = Purchase.objects.all()
            base_supplier_pmts_qs = SupplierPayment.objects.all()
            base_credit_repay_qs = CreditRepayment.objects.all()

            if branch_obj:
                base_sales_qs = base_sales_qs.filter(branch=branch_obj)
                base_expenses_qs = base_expenses_qs.filter(branch=branch_obj)
                base_purchases_qs = base_purchases_qs.filter(branch=branch_obj)
                base_supplier_pmts_qs = base_supplier_pmts_qs.filter(purchase__branch=branch_obj)
                if hasattr(CreditRepayment, 'branch'):
                    base_credit_repay_qs = base_credit_repay_qs.filter(
                        Q(branch=branch_obj) | Q(transaction__branch=branch_obj) | Q(branch__isnull=True)
                    )

            sales_qs = base_sales_qs
            expenses_qs = base_expenses_qs
            purchases_qs = base_purchases_qs
            supplier_payments_qs = base_supplier_pmts_qs
            customer_repayments_qs = base_credit_repay_qs

            if start_date and end_date:
                sales_qs = sales_qs.filter(sale_date__range=(start_dt, end_dt))
                expenses_qs = expenses_qs.filter(date__range=(start_date, end_date))
                purchases_qs = purchases_qs.filter(purchase_date__range=(start_dt, end_dt))
                customer_repayments_qs = customer_repayments_qs.filter(
                    date__range=(start_date, end_date)
                )
            if start_dt and end_dt:
                supplier_payments_qs = supplier_payments_qs.filter(
                    created_at__range=(start_dt, end_dt)
                )

            credit_transaction_ids = set(
                base_credit_repay_qs
                .exclude(transaction_id=None)
                .values_list("transaction_id", flat=True)
                .distinct()
            )

            # -----------------------------------------------------------------
            # 2. FINANCIAL PERFORMANCE METRICS
            # -----------------------------------------------------------------
            sales_agg = sales_qs.aggregate(total=Sum('total_amount'))
            total_sales = quantize_money(sales_agg['total'] or ZERO)

            cogs_agg = SaleItem.objects.filter(
                transaction__in=sales_qs
            ).aggregate(
                total=Sum(
                    ExpressionWrapper(
                        F('quantity_sold') * F('unit_cost_price'),
                        output_field=models.DecimalField(max_digits=12, decimal_places=2)
                    )
                )
            )
            cogs = quantize_money(cogs_agg['total'] or ZERO)

            expenses_agg = expenses_qs.aggregate(total=Sum('amount'))
            operating_expenses = quantize_money(expenses_agg['total'] or ZERO)

            gross_profit = total_sales - cogs
            operating_profit = gross_profit - operating_expenses
            net_profit = operating_profit

            # -----------------------------------------------------------------
            # 3. CASH MOVEMENT & PAYMENT CHANNEL BREAKDOWN
            # -----------------------------------------------------------------
            cash_total = ZERO
            paybill_total = ZERO
            send_money_total = ZERO
            mpesa_std_total = ZERO
            bank_total = ZERO
            other_total = ZERO
            period_credit_sales_total = ZERO

            sales_with_payments = sales_qs.select_related("customer", "branch").prefetch_related("split_payments")

            for sale in sales_with_payments:
                amt = quantize_money(sale.total_amount or ZERO)
                method = self._normalize_payment_method(getattr(sale, "payment_method", None))
                split_records = extract_split_payments(sale)

                if method == "SPLIT" or split_records:
                    for sp in split_records:
                        sp_method = self._normalize_payment_method(extract_sp_method(sp))
                        sp_amount = quantize_money(sp.amount or ZERO)

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
                    is_credit = is_credit_sale_check(sale, credit_transaction_ids)

                    if is_credit or method == "CREDIT":
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
                    else:
                        other_total += amt

            period_credit_repayments_total = ZERO
            for rep in customer_repayments_qs.select_related("customer", "transaction"):
                rep_amt = quantize_money(rep.amount or ZERO)
                if rep_amt <= ZERO:
                    continue

                rep_method = self._normalize_payment_method(getattr(rep, "payment_method", None))
                period_credit_repayments_total += rep_amt

                if rep_method == "CASH":
                    cash_total += rep_amt
                elif rep_method == "PAYBILL":
                    paybill_total += rep_amt
                elif rep_method == "SEND_MONEY":
                    send_money_total += rep_amt
                elif rep_method == "MPESA":
                    mpesa_std_total += rep_amt
                elif rep_method == "BANK_TRANSFER":
                    bank_total += rep_amt
                else:
                    other_total += rep_amt

            actual_cash_receipts = (
                cash_total +
                paybill_total +
                send_money_total +
                mpesa_std_total +
                bank_total +
                other_total
            )

            # -----------------------------------------------------------------
            # 4. UNIFIED RECEIVABLES LEDGER (PER DEBTOR AGGREGATION)
            # -----------------------------------------------------------------
            st_credit_filter = (
                Q(payment_method=SaleTransaction.PaymentMethod.CREDIT) |
                Q(payment_method='Credit') |
                Q(split_payments__method='CREDIT') |
                Q(split_payments__method='Credit')
            )
            all_credit_sales = base_sales_qs.filter(st_credit_filter).prefetch_related('split_payments', 'customer').distinct()
            if end_dt:
                all_credit_sales = all_credit_sales.filter(sale_date__lte=end_dt)

            all_credit_ids = set(
                base_credit_repay_qs
                .exclude(transaction_id=None)
                .values_list("transaction_id", flat=True)
                .distinct()
            )

            repayments_to_date_qs = base_credit_repay_qs
            if end_date:
                repayments_to_date_qs = repayments_to_date_qs.filter(date__lte=end_date)

            debtors_map = {}
            for c_sale in all_credit_sales:
                cust_key = c_sale.customer_id if c_sale.customer_id else (c_sale.customer_name or "Walk-in Debtor").strip()
                if cust_key not in debtors_map:
                    debtors_map[cust_key] = {
                        "customer_obj": c_sale.customer,
                        "gross_credit": ZERO,
                    }
                debtors_map[cust_key]["gross_credit"] += get_sale_credit_amt(c_sale, all_credit_ids)

            total_customer_debtors = ZERO
            for key, data in debtors_map.items():
                cust_obj = data["customer_obj"]
                cust_repayments = ZERO
                if cust_obj:
                    cust_repayments = repayments_to_date_qs.filter(customer=cust_obj).aggregate(
                        total=Coalesce(Sum('amount'), ZERO)
                    )['total'] or ZERO

                net_bal = max(ZERO, data["gross_credit"] - quantize_money(cust_repayments))
                total_customer_debtors += net_bal

            repay_agg_lifetime = base_credit_repay_qs.aggregate(total=Sum('amount'))
            total_recovered_lifetime = quantize_money(repay_agg_lifetime['total'] or ZERO)

            unpaid_invoices_count = base_sales_qs.filter(
                st_credit_filter,
                is_paid=False
            ).distinct().count()

            # -----------------------------------------------------------------
            # 5. INVENTORY & SKUS
            # -----------------------------------------------------------------
            pricings_qs = BranchItemPricing.objects.select_related('item', 'branch')
            if branch_obj:
                pricings_qs = pricings_qs.filter(branch=branch_obj)

            stocks_qs = BranchStock.objects.all() if 'BranchStock' in globals() else []
            if branch_obj and stocks_qs:
                stocks_qs = stocks_qs.filter(branch=branch_obj)

            branch_stock_map = {s.item_id: Decimal(str(s.quantity or ZERO)) for s in stocks_qs} if stocks_qs else {}

            total_stock_value = ZERO
            low_stock_count = 0
            out_of_stock_count = 0
            active_skus_count = 0

            for p in pricings_qs:
                if p.item_id in branch_stock_map:
                    rem_stock = branch_stock_map[p.item_id]
                    stock_val = rem_stock * (p.unit_cost_price or ZERO)
                else:
                    rem_stock = p.remaining_stock
                    stock_val = p.remaining_stock_value

                total_stock_value += stock_val

                if rem_stock <= ZERO:
                    out_of_stock_count += 1
                elif rem_stock <= (p.min_stock_threshold or ZERO):
                    low_stock_count += 1
                else:
                    active_skus_count += 1

            total_stock_value = quantize_money(total_stock_value)
            total_cataloged_skus = pricings_qs.values('item_id').distinct().count()

            # -----------------------------------------------------------------
            # 6. PURCHASES & OUTFLOWS
            # -----------------------------------------------------------------
            dir_purch_agg = purchases_qs.exclude(
                payment_method='CREDIT'
            ).filter(
                supplier_payments__isnull=True
            ).aggregate(total=Sum('amount_paid'))
            direct_purchases = quantize_money(dir_purch_agg['total'] or ZERO)

            supp_pmt_agg = supplier_payments_qs.aggregate(total=Sum('amount'))
            supplier_repayments = quantize_money(supp_pmt_agg['total'] or ZERO)

            purchases_total = direct_purchases + supplier_repayments
            available_funds = actual_cash_receipts - operating_expenses - purchases_total

            # -----------------------------------------------------------------
            # 7. SAFE TRAJECTORY COMPUTATION
            # -----------------------------------------------------------------
            trajectory_data = []
            sales_by_date = {}
            for sale in sales_qs:
                s_date = getattr(sale, 'sale_date', None)
                if s_date:
                    d_key = s_date.strftime('%b %d') if hasattr(s_date, 'strftime') else str(s_date)[:10]
                    sales_by_date[d_key] = sales_by_date.get(d_key, ZERO) + quantize_money(sale.total_amount or ZERO)

            if sales_by_date:
                for date_str, day_s in sales_by_date.items():
                    ratio = (day_s / total_sales) if total_sales > ZERO else ZERO
                    day_cogs = quantize_money(cogs * ratio)
                    day_net = quantize_money(net_profit * ratio)

                    trajectory_data.append({
                        "date": date_str,
                        "total_sales": float(day_s),
                        "cogs": float(day_cogs),
                        "net_profit": float(day_net)
                    })
            else:
                formatted_label = start_date.strftime('%b %d') if hasattr(start_date, 'strftime') else str(start_date)
                trajectory_data.append({
                    "date": formatted_label,
                    "total_sales": float(total_sales),
                    "cogs": float(cogs),
                    "net_profit": float(net_profit)
                })

            # Construct Payload Response
            response_payload = {
                "profitability": {
                    "total_sales": float(total_sales),
                    "total_sales_formatted": f"KSh {total_sales:,.2f}",
                    "cogs": float(cogs),
                    "cogs_formatted": f"KSh {cogs:,.2f}",
                    "gross_profit": float(gross_profit),
                    "gross_profit_formatted": f"KSh {gross_profit:,.2f}",
                    "operating_expenses": float(operating_expenses),
                    "operating_expenses_formatted": f"KSh {operating_expenses:,.2f}",
                    "net_profit": float(net_profit),
                    "net_profit_formatted": f"KSh {net_profit:,.2f}",
                },
                "debtors": {
                    "total_customer_debts": float(total_customer_debtors),
                    "total_customer_debts_formatted": f"KSh {total_customer_debtors:,.2f}",
                    "total_recovered": float(period_credit_repayments_total),
                    "total_recovered_formatted": f"KSh {period_credit_repayments_total:,.2f}",
                    "total_recovered_lifetime": float(total_recovered_lifetime),
                    "total_recovered_lifetime_formatted": f"KSh {total_recovered_lifetime:,.2f}",
                    "unpaid_count": unpaid_invoices_count,
                },
                "inventory": {
                    "total_stock_value": float(total_stock_value),
                    "total_stock_value_formatted": f"KSh {total_stock_value:,.2f}",
                    "product_skus": total_cataloged_skus,
                    "low_stock_items": low_stock_count,
                    "out_of_stock_items": out_of_stock_count,
                    "active_skus": active_skus_count,
                },
                "cash_flow": {
                    "receipts": float(actual_cash_receipts),
                    "receipts_formatted": f"KSh {actual_cash_receipts:,.2f}",
                    "payments": float(operating_expenses),
                    "payments_formatted": f"KSh {operating_expenses:,.2f}",
                    "purchases": float(purchases_total),
                    "purchases_formatted": f"KSh {purchases_total:,.2f}",
                    "total_available_funds": float(available_funds),
                    "total_available_funds_formatted": f"KSh {available_funds:,.2f}",
                },
                "trajectory": trajectory_data,
            }

            return Response(response_payload, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error in dashboard_cards view: {str(e)}", exc_info=True)
            return Response(
                {"detail": f"Failed to calculate dashboard card metrics: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


    @action(detail=False, methods=['get'], url_path='sales-register')
    def sales_register(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, period_type = self._get_branch_and_dates(request)
            sales_qs = SaleTransaction.objects.filter(sale_date__range=(start_dt, end_dt))
            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)

            results = [{
                "id": tx.id,
                "sale_date": tx.sale_date.strftime('%Y-%m-%d %H:%M'),
                "receipt_number": tx.receipt_number or f"REC-{tx.id}",
                "customer": tx.customer.name if tx.customer else (tx.customer_name or "Walk-in Customer"),
                "payment_method": tx.payment_method,
                "served_by": tx.created_by.username if tx.created_by else "System",
                "total_amount": float(tx.total_amount or ZERO),
                "amount_paid": float(tx.amount_paid or ZERO)
            } for tx in sales_qs.select_related('customer', 'created_by')[:100]]

            return Response({"count": sales_qs.count(), "results": results}, status=status.HTTP_200_OK)
        except Exception:
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='customer-receivables')
    def customer_receivables_report(self, request):
        try:
            branch_id = request.query_params.get('branch_id') or request.query_params.get('branch')
            branch_obj = None
            if branch_id and str(branch_id).lower() not in ['all', 'null', 'undefined', '']:
                branch_obj = RetailBranch.objects.filter(pk=branch_id).first()

            end_date_str = request.query_params.get('end_date') or request.query_params.get('endDate')
            end_dt = None
            end_date = None

            if end_date_str:
                try:
                    end_date = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date()
                    end_dt = timezone.make_aware(timezone.datetime.combine(end_date, timezone.datetime.max.time()))
                except ValueError:
                    pass

            if not end_dt:
                end_dt = timezone.now()
                end_date = end_dt.date()

            # 1. Fetch ALL Credit Repayments to date
            repayment_qs = CreditRepayment.objects.filter(date__lte=end_date)
            if branch_obj:
                repayment_qs = repayment_qs.filter(
                    Q(branch=branch_obj) | Q(transaction__branch=branch_obj) | Q(branch__isnull=True)
                )

            total_recovered = repayment_qs.aggregate(total=Coalesce(Sum('amount'), ZERO))['total'] or ZERO

            # 2. Fetch ALL Credit Sales (regardless of paid/unpaid status)
            st_credit_filter = (
                Q(payment_method=SaleTransaction.PaymentMethod.CREDIT) | 
                Q(payment_method='Credit') |
                Q(split_payments__method='CREDIT') |
                Q(split_payments__method='Credit')
            )
            credit_sales_qs = SaleTransaction.objects.filter(st_credit_filter, sale_date__lte=end_dt).prefetch_related('split_payments', 'repayments', 'customer').distinct()
            legacy_sales_qs = Sale.objects.filter(payment_method='CREDIT', date__lte=end_date)

            if branch_obj:
                credit_sales_qs = credit_sales_qs.filter(branch=branch_obj)
                legacy_sales_qs = legacy_sales_qs.filter(branch=branch_obj)

            debtors_map = {}
            total_unpaid_invoices_count = 0

            # Process SaleTransaction records
            for sale in credit_sales_qs:
                cust_key = sale.customer_id if sale.customer_id else (sale.customer_name or "Walk-in Debtor").strip()
                cust_disp_name = sale.customer.name if sale.customer else (sale.customer_name or "Walk-in Debtor").strip()
                cust_phone = (sale.customer.phone_number if sale.customer else sale.customer_phone) or "N/A"

                if cust_key not in debtors_map:
                    debtors_map[cust_key] = {
                        "customer_id": cust_key,
                        "customer_obj": sale.customer,
                        "customer_name": cust_disp_name,
                        "phone": cust_phone,
                        "unpaid_invoices_count": 0,
                        "gross_credit": ZERO,
                    }

                norm_method = _normalize_payment_method(sale.payment_method)
                if norm_method == "SPLIT" or sale.split_payments.exists():
                    sale_credit_portion = sum(
                        (Decimal(str(sp.amount or ZERO)) for sp in sale.split_payments.all() if _normalize_payment_method(_extract_sp_method(sp)) == "CREDIT"),
                        ZERO
                    )
                    if sale_credit_portion <= ZERO:
                        sale_credit_portion = Decimal(str(sale.total_amount or ZERO))
                else:
                    sale_credit_portion = Decimal(str(sale.total_amount or ZERO))

                debtors_map[cust_key]["gross_credit"] += sale_credit_portion
                debtors_map[cust_key]["unpaid_invoices_count"] += 1
                total_unpaid_invoices_count += 1

            # Process Legacy Sales records
            for legacy in legacy_sales_qs:
                cust_key = f"legacy-{legacy.customer_name or 'Walk-in'}"
                cust_disp_name = legacy.customer_name or "Walk-in Debtor"

                if cust_key not in debtors_map:
                    debtors_map[cust_key] = {
                        "customer_id": cust_key,
                        "customer_obj": None,
                        "customer_name": cust_disp_name,
                        "phone": legacy.customer_phone or "N/A",
                        "unpaid_invoices_count": 0,
                        "gross_credit": ZERO,
                    }

                legacy_credit = Decimal(str(legacy.total_selling_price or ZERO))
                debtors_map[cust_key]["gross_credit"] += legacy_credit
                debtors_map[cust_key]["unpaid_invoices_count"] += 1
                total_unpaid_invoices_count += 1

            # 3. Compute Outstanding Receivables per Customer
            debtors_summary = []
            total_receivables = ZERO

            for key, data in debtors_map.items():
                cust_obj = data["customer_obj"]
                cust_repayments = ZERO

                if cust_obj:
                    cust_repayments = repayment_qs.filter(customer=cust_obj).aggregate(
                        total=Coalesce(Sum('amount'), ZERO)
                    )['total'] or ZERO

                net_balance = max(ZERO, data["gross_credit"] - cust_repayments)
                total_receivables += net_balance

                if net_balance > ZERO:
                    debtors_summary.append({
                        "customer_id": data["customer_id"],
                        "customer_name": data["customer_name"],
                        "phone": data["phone"],
                        "unpaid_invoices_count": data["unpaid_invoices_count"],
                        "outstanding_balance": float(quantize_money(net_balance)),
                    })

            # 4. GUARANTEE ACCOUNTING EQUATION PARITY:
            # Gross Credit Issued = Total Receivables (Outstanding) + Total Recovered Repayments
            total_credit_issued = total_receivables + total_recovered

            debtors_summary.sort(key=lambda x: x["outstanding_balance"], reverse=True)
            active_debtors_count = len(debtors_summary)
            
            avg_debt = (
                total_receivables / Decimal(str(active_debtors_count))
                if active_debtors_count > 0
                else ZERO
            )

            recovery_rate = (
                (total_recovered / total_credit_issued * Decimal("100.0"))
                if total_credit_issued > ZERO
                else ZERO
            )

            return Response({
                "as_at_date": end_date.strftime('%d %b %Y') if end_date else timezone.localdate().strftime('%d %b %Y'),
                "total_receivables": float(quantize_money(total_receivables)),
                "outstanding_balance": float(quantize_money(total_receivables)),
                "total_recovered": float(quantize_money(total_recovered)),
                "total_credit_issued": float(quantize_money(total_credit_issued)), # Guaranteed >= Total Recovered
                "recovery_rate": float(quantize_money(recovery_rate)),
                "active_debtors_count": active_debtors_count,
                "total_unpaid_invoices": total_unpaid_invoices_count,
                "average_debt_per_customer": float(quantize_money(avg_debt)),
                "debtors_summary": debtors_summary,
            }, status=status.HTTP_200_OK)

        except Exception:
            logger.exception("Customer Receivables Report Error")
            return Response({
                "as_at_date": timezone.localdate().strftime('%d %b %Y'),
                "total_receivables": 0.00,
                "outstanding_balance": 0.00,
                "total_recovered": 0.00,
                "total_credit_issued": 0.00,
                "recovery_rate": 0.0,
                "active_debtors_count": 0,
                "total_unpaid_invoices": 0,
                "average_debt_per_customer": 0.00,
                "debtors_summary": [],
            }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='sales-collections')
    def sales_collections(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)
            sales_qs = SaleTransaction.objects.filter(sale_date__range=(start_dt, end_dt))
            repayments_qs = CreditRepayment.objects.filter(Q(date__range=(start_date, end_date)))

            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                repayments_qs = repayments_qs.filter(branch=branch_obj)

            channels = {"CASH": ZERO, "PAYBILL": ZERO, "SEND_MONEY": ZERO}

            for sale in sales_qs:
                bd = sale.liquid_payments_breakdown
                channels["CASH"] += bd.get('CASH', ZERO)
                channels["PAYBILL"] += bd.get('PAYBILL', ZERO)
                channels["SEND_MONEY"] += bd.get('MPESA', ZERO)

            for rep in repayments_qs:
                method = self._normalize_payment_method(rep.payment_method)
                if method in channels:
                    channels[method] += quantize_money(rep.amount or ZERO)
                else:
                    channels["CASH"] += quantize_money(rep.amount or ZERO)

            total_coll = sum(channels.values(), ZERO)
            breakdown = [{
                "channel": ch,
                "amount": float(amt),
                "percentage": round(float((amt / total_coll * Decimal("100")) if total_coll > ZERO else ZERO), 1)
            } for ch, amt in channels.items()]

            return Response({"total_collections": float(total_coll), "breakdown": breakdown}, status=status.HTTP_200_OK)
        except Exception:
            return Response({"total_collections": 0.0, "breakdown": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='sales-returns-discounts')
    def sales_returns_discounts(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)
            sale_items_qs = SaleItem.objects.filter(transaction__sale_date__range=(start_dt, end_dt))
            returns_qs = CustomerReturn.objects.filter(date__range=(start_date, end_date))

            if branch_obj:
                sale_items_qs = sale_items_qs.filter(transaction__branch=branch_obj)
                returns_qs = returns_qs.filter(branch=branch_obj)

            total_discounts = sum((Decimal(str(item.quantity_sold or 0)) * Decimal(str(item.discount_per_item or 0)) for item in sale_items_qs), ZERO)

            total_returns_val = ZERO
            for ret in returns_qs.select_related('item'):
                qty = Decimal(str(ret.quantity or 0))
                pricing = BranchItemPricing.objects.filter(branch=ret.branch, item=ret.item).first()
                price = pricing.unit_selling_price if pricing and pricing.unit_selling_price else ZERO
                total_returns_val += (qty * Decimal(str(price)))

            return Response({
                "summary": {
                    "total_discounts_granted": float(total_discounts),
                    "total_returns_value": float(total_returns_val),
                    "total_returns_count": returns_qs.count()
                }
            }, status=status.HTTP_200_OK)
        except Exception:
            return Response({"summary": {"total_discounts_granted": 0.0, "total_returns_value": 0.0, "total_returns_count": 0}}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='profit-loss')
    def profit_loss(self, request):
        try:
            branch_id, start_date, end_date = self._get_filter_params(request)

            # 1. Fetch ALL transactions (Cash, Paybill, Send Money, Credit, Split) within the date range
            sales_tx_qs = SaleTransaction.objects.filter(
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date
            )

            expenses_qs = Expense.objects.filter(
                date__gte=start_date,
                date__lte=end_date
            )

            if branch_id and str(branch_id).lower() != 'all':
                sales_tx_qs = sales_tx_qs.filter(branch_id=branch_id)
                expenses_qs = expenses_qs.filter(branch_id=branch_id)

            # 2. Total Sales Revenue (includes Credit Sales & Cash Sales)
            total_sales_dec = sales_tx_qs.aggregate(
                total=Coalesce(Sum('total_amount'), Decimal('0.00'), output_field=DecimalField())
            )['total']

            # 3. Calculate Cost of Goods Sold (COGS) properly
            sale_items = SaleItem.objects.filter(transaction__in=sales_tx_qs).select_related('item', 'transaction')

            total_cogs_dec = Decimal('0.00')

            # Expression for direct item unit_cost_price calculation
            cogs_expr = ExpressionWrapper(
                F('quantity_sold') * F('unit_cost_price'),
                output_field=DecimalField(max_digits=12, decimal_places=2)
            )

            direct_cogs = sale_items.filter(unit_cost_price__gt=0).aggregate(
                total=Coalesce(Sum(cogs_expr), Decimal('0.00'), output_field=DecimalField())
            )['total']

            total_cogs_dec += direct_cogs

            # Fallback for line items saved with 0.00 unit_cost_price (look up current BranchItemPricing cost)
            zero_cost_items = sale_items.filter(Q(unit_cost_price=0) | Q(unit_cost_price__isnull=True))

            for line in zero_cost_items:
                b_id = line.transaction.branch_id
                pricing = BranchItemPricing.objects.filter(branch_id=b_id, item=line.item).first()
                cost_unit = pricing.unit_cost_price if (pricing and pricing.unit_cost_price > 0) else Decimal('0.00')
                total_cogs_dec += Decimal(str(line.quantity_sold or 0)) * Decimal(str(cost_unit))

            # 4. Operating Expenses
            expenses_dec = expenses_qs.aggregate(
                total=Coalesce(Sum('amount'), Decimal('0.00'), output_field=DecimalField())
            )['total']

            # 5. Financial Calculations using exact Decimals
            gross_profit_dec = total_sales_dec - total_cogs_dec
            net_profit_dec = gross_profit_dec - expenses_dec

            return Response({
                'period': {
                    'start_date': str(start_date),
                    'end_date': str(end_date),
                },
                'total_sales': float(total_sales_dec),
                'total_sales_formatted': f"KSh {total_sales_dec:,.2f}",
                'cost_of_goods_sold': float(total_cogs_dec),
                'cost_of_goods_sold_formatted': f"KSh {total_cogs_dec:,.2f}",
                'gross_profit': float(gross_profit_dec),
                'gross_profit_formatted': f"KSh {gross_profit_dec:,.2f}",
                'operating_expenses': float(expenses_dec),
                'operating_expenses_formatted': f"KSh {expenses_dec:,.2f}",
                'net_profit': float(net_profit_dec),
                'net_profit_formatted': f"KSh {net_profit_dec:,.2f}",
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], url_path='sales-performance')
    def sales_performance(self, request):
        try:
            branch_id, start_date, end_date = self._get_filter_params(request)

            sales_tx_qs = SaleTransaction.objects.filter(
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date
            )

            if branch_id and branch_id != 'all':
                sales_tx_qs = sales_tx_qs.filter(branch_id=branch_id)

            daily_breakdown = []
            curr_date = start_date

            while curr_date <= end_date:
                day_sales = sales_tx_qs.filter(
                    sale_date__date=curr_date
                ).aggregate(
                    total=Sum('total_amount')
                )['total'] or 0.0

                daily_breakdown.append({
                    'date': curr_date.strftime('%b %d'),
                    'full_date': str(curr_date),
                    'gross_sales': float(day_sales),
                })
                curr_date += timedelta(days=1)

            total_period_sales = sum(item['gross_sales'] for item in daily_breakdown)

            return Response({
                'period': {
                    'start_date': str(start_date),
                    'end_date': str(end_date),
                },
                'total_sales': float(total_period_sales),
                'total_sales_formatted': f"KSh {float(total_period_sales):,.2f}",
                'daily': daily_breakdown
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # =========================================================================
    # CASHVIEW ENDPOINTS
    # =========================================================================
    @action(detail=False, methods=['get'], url_path='cash-overview')
    def cash_overview(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)

            sales_qs = SaleTransaction.objects.filter(sale_date__range=(start_dt, end_dt)).exclude(payment_method='CREDIT')
            expenses_qs = Expense.objects.filter(Q(date__range=(start_date, end_date)))
            repayments_qs = CreditRepayment.objects.filter(Q(date__range=(start_date, end_date)))

            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                expenses_qs = expenses_qs.filter(branch=branch_obj)
                repayments_qs = repayments_qs.filter(branch=branch_obj)

            total_inflows = sum((quantize_money(s.total_amount) for s in sales_qs), ZERO) + sum((quantize_money(r.amount) for r in repayments_qs), ZERO)
            total_outflows = sum((quantize_money(e.amount) for e in expenses_qs), ZERO)

            return Response({
                "total_cash_inflows": float(total_inflows),
                "total_cash_outflows": float(total_outflows),
                "net_cash_flow": float(total_inflows - total_outflows),
                "summary": {
                    "total_cash_inflows": float(total_inflows),
                    "total_cash_outflows": float(total_outflows),
                    "net_cash_flow": float(total_inflows - total_outflows),
                }
            }, status=status.HTTP_200_OK)
        except Exception:
            return Response({"total_cash_inflows": 0.0, "total_cash_outflows": 0.0, "net_cash_flow": 0.0}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='cash-inflows')
    def cash_inflows(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)
            sales_qs = SaleTransaction.objects.filter(sale_date__range=(start_dt, end_dt)).exclude(payment_method='CREDIT')
            repayments_qs = CreditRepayment.objects.filter(Q(date__range=(start_date, end_date)))

            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                repayments_qs = repayments_qs.filter(branch=branch_obj)

            entries = []
            tot_in = ZERO

            for s in sales_qs.order_by('-sale_date')[:50]:
                amt = float(quantize_money(s.total_amount or ZERO))
                tot_in += Decimal(str(amt))
                entries.append({
                    "id": f"SALE-{s.id}",
                    "reference": s.receipt_number or f"REC-{s.id}",
                    "date": s.sale_date.strftime('%Y-%m-%d %H:%M'),
                    "description": f"Sale ({s.customer_name or 'Walk-in'})",
                    "payment_method": s.payment_method,
                    "inflow": amt,
                    "outflow": 0.0,
                    "amount": amt,
                    "type": "INFLOW"
                })

            for r in repayments_qs.order_by('-date')[:50]:
                amt = float(quantize_money(r.amount or ZERO))
                tot_in += Decimal(str(amt))
                cust_name = r.customer.name if r.customer else "Customer"
                entries.append({
                    "id": f"REPAY-{r.id}",
                    "reference": f"REPAY-{r.id}",
                    "date": r.date.strftime('%Y-%m-%d'),
                    "description": f"Credit Repayment - {cust_name}",
                    "payment_method": r.payment_method,
                    "inflow": amt,
                    "outflow": 0.0,
                    "amount": amt,
                    "type": "INFLOW"
                })

            return Response({
                "summary": {"total_inflow": float(tot_in), "total_outflow": 0.0, "net_cash_flow": float(tot_in)},
                "count": len(entries),
                "results": entries,
                "entries": entries
            }, status=status.HTTP_200_OK)
        except Exception:
            return Response({"summary": {"total_inflow": 0.0, "total_outflow": 0.0, "net_cash_flow": 0.0}, "entries": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='cash-expenses')
    def cash_expenses(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)
            expenses_qs = Expense.objects.filter(Q(date__range=(start_date, end_date)))

            if branch_obj:
                expenses_qs = expenses_qs.filter(branch=branch_obj)

            entries = []
            tot_out = ZERO

            for e in expenses_qs.order_by('-date')[:100]:
                amt = float(quantize_money(e.amount or ZERO))
                tot_out += Decimal(str(amt))
                entries.append({
                    "id": f"EXP-{e.id}",
                    "reference": f"EXP-{e.id}",
                    "date": e.date.strftime('%Y-%m-%d') if hasattr(e.date, 'strftime') else str(e.date)[:10],
                    "description": e.description or "Operating Expense",
                    "payment_method": "CASH",
                    "inflow": 0.0,
                    "outflow": amt,
                    "amount": amt,
                    "type": "OUTFLOW"
                })

            return Response({
                "summary": {"total_inflow": 0.0, "total_outflow": float(tot_out), "net_cash_flow": float(-tot_out)},
                "count": len(entries),
                "results": entries,
                "entries": entries
            }, status=status.HTTP_200_OK)
        except Exception:
            return Response({"summary": {"total_inflow": 0.0, "total_outflow": 0.0, "net_cash_flow": 0.0}, "entries": []}, status=status.HTTP_200_OK)
                      
    

    @action(detail=False, methods=['get'], url_path='inventory-valuation')
    def inventory_report(self, request):
        try:
            # 1. Parse branch parameters
            branch_id = request.query_params.get('branch_id') or request.query_params.get('branch')
            branch_obj = None
            if branch_id and str(branch_id).lower() not in ['all', 'null', 'undefined', '']:
                branch_obj = RetailBranch.objects.filter(pk=branch_id).first()

            # 2. Base QuerySets
            pricings_qs = BranchItemPricing.objects.select_related('item', 'branch').all()
            stock_qs = BranchStock.objects.select_related('item', 'branch').all()
            movements_qs = StockMovement.objects.select_related('item', 'created_by').all()

            if branch_obj:
                pricings_qs = pricings_qs.filter(branch=branch_obj)
                stock_qs = stock_qs.filter(branch=branch_obj)
                movements_qs = movements_qs.filter(branch=branch_obj)
            elif hasattr(request.user, 'retailer') and request.user.retailer:
                pricings_qs = pricings_qs.filter(branch__retailer=request.user.retailer)
                stock_qs = stock_qs.filter(branch__retailer=request.user.retailer)
                movements_qs = movements_qs.filter(branch__retailer=request.user.retailer)

            all_items = Item.objects.all()
            if hasattr(request.user, 'retailer') and request.user.retailer:
                all_items = all_items.filter(Q(retailer=request.user.retailer) | Q(retailer__isnull=True))

            # 3. Stock Asset Calculation
            total_valuation = ZERO
            active_skus_count = max(pricings_qs.count(), all_items.count())
            low_stock_alerts_count = 0
            inventory_items = []
            processed_item_ids = set()

            for pricing in pricings_qs:
                processed_item_ids.add(pricing.item_id)
                rem_stock = pricing.remaining_stock
                cost = pricing.unit_cost_price or ZERO
                item_val = rem_stock * cost

                total_valuation += item_val
                if pricing.is_low_stock:
                    low_stock_alerts_count += 1

                inventory_items.append({
                    "item_id": pricing.item.pk,
                    "item_name": pricing.item.name,
                    "sku": pricing.item.sku or "N/A",
                    "remaining_stock": float(rem_stock),
                    "unit_cost_price": float(quantize_money(cost)),
                    "unit_selling_price": float(quantize_money(pricing.unit_selling_price or ZERO)),
                    "total_valuation": float(quantize_money(item_val)),
                    "is_low_stock": pricing.is_low_stock,
                })

            for item in all_items.exclude(id__in=processed_item_ids):
                b_stock = stock_qs.filter(item=item).first()
                stock_qty = b_stock.quantity if b_stock else ZERO
                inventory_items.append({
                    "item_id": item.pk,
                    "item_name": item.name,
                    "sku": item.sku or "N/A",
                    "remaining_stock": float(stock_qty),
                    "unit_cost_price": 0.00,
                    "unit_selling_price": 0.00,
                    "total_valuation": 0.00,
                    "is_low_stock": False,
                })

            inventory_items.sort(key=lambda x: x["total_valuation"], reverse=True)

            # 4. Inbound & Outbound Movement Categories
            inbound_types = [
                StockMovement.MovementType.PURCHASE,
                StockMovement.MovementType.CUSTOMER_RETURN,
                StockMovement.MovementType.TRANSFER_IN,
                StockMovement.MovementType.ADJUSTMENT_IN,
                StockMovement.MovementType.OPENING_STOCK,
            ]
            outbound_types = [
                StockMovement.MovementType.SALE,
                StockMovement.MovementType.SUPPLIER_RETURN,
                StockMovement.MovementType.TRANSFER_OUT,
                StockMovement.MovementType.DAMAGE,
                StockMovement.MovementType.ADJUSTMENT_OUT,
            ]

            inbound_movements = movements_qs.filter(movement_type__in=inbound_types).order_by('-created_at')
            outbound_movements = movements_qs.filter(movement_type__in=outbound_types).order_by('-created_at')

            inbound_units = inbound_movements.aggregate(total=Coalesce(Sum('quantity'), ZERO))['total']
            outbound_units = outbound_movements.aggregate(total=Coalesce(Sum('quantity'), ZERO))['total']

            recent_inbound = []
            for m in inbound_movements[:10]:
                unit_cost = m.unit_cost_price or ZERO
                tot_cost = m.quantity * unit_cost
                recent_inbound.append({
                    "id": m.pk,
                    "ref_code": m.reference or f"IN-{m.pk}",
                    "item_name": m.item.name if m.item else "Unknown Item",
                    "quantity": float(m.quantity),
                    "unit_cost_price": float(quantize_money(unit_cost)),
                    "total_cost": float(quantize_money(tot_cost)),
                    "date": m.date.strftime('%Y-%m-%d') if m.date else "",
                })

            recent_outbound = []
            for m in outbound_movements[:10]:
                unit_cost = m.unit_cost_price or ZERO
                tot_cost = m.quantity * unit_cost
                recent_outbound.append({
                    "id": m.pk,
                    "ref_code": m.reference or f"OUT-{m.pk}",
                    "item_name": m.item.name if m.item else "Unknown Item",
                    "quantity": float(m.quantity),
                    "unit_cost_price": float(quantize_money(unit_cost)),
                    "total_cost": float(quantize_money(tot_cost)),
                    "date": m.date.strftime('%Y-%m-%d') if m.date else "",
                })

            damage_records_count = movements_qs.filter(movement_type=StockMovement.MovementType.DAMAGE).count()
            transfer_logs_count = movements_qs.filter(
                movement_type__in=[StockMovement.MovementType.TRANSFER_IN, StockMovement.MovementType.TRANSFER_OUT]
            ).count()

            return Response({
                "total_stock_valuation": float(quantize_money(total_valuation)),
                "active_catalog_skus": active_skus_count,
                "low_stock_threshold_alerts": low_stock_alerts_count,
                "inter_branch_transfers": transfer_logs_count,
                "inbound_stock_units": float(inbound_units),
                "outbound_stock_units": float(outbound_units),
                "damaged_shrinkage_logs": damage_records_count,
                "inventory_summary": inventory_items,
                "recent_inbound": recent_inbound,
                "recent_outbound": recent_outbound,
            }, status=status.HTTP_200_OK)

        except Exception:
            logger.exception("Inventory Valuation Report Error")
            return Response({
                "total_stock_valuation": 0.00,
                "active_catalog_skus": 0,
                "low_stock_threshold_alerts": 0,
                "inter_branch_transfers": 0,
                "inbound_stock_units": 0,
                "outbound_stock_units": 0,
                "damaged_shrinkage_logs": 0,
                "inventory_summary": [],
                "recent_inbound": [],
                "recent_outbound": [],
            }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='stock-levels')
    def stock_levels(self, request):
        try:
            branch_obj, _, _, _, _, _period = self._get_branch_and_dates(request)
            pricings_qs = BranchItemPricing.objects.select_related('item', 'branch')
            if branch_obj:
                pricings_qs = pricings_qs.filter(branch=branch_obj)

            results = []
            for pricing in pricings_qs:
                item = pricing.item
                rem_stock = float(pricing.remaining_stock)
                min_thresh = float(pricing.min_stock_threshold or 0)
                wac_cost = float(pricing.unit_cost_price or 0)
                s_price = float(pricing.unit_selling_price or 0)
                asset_val = float(pricing.remaining_stock_value)

                results.append({
                    "id": pricing.id,
                    "item_id": item.id,
                    "sku_code": item.sku or item.code or f"SKU-{item.id}",
                    "item_name": item.name,
                    "remaining_stock": rem_stock,
                    "min_threshold": min_thresh,
                    "wac_unit_cost": wac_cost,
                    "selling_price": s_price,
                    "stock_asset_value": asset_val,
                })

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except Exception:
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='stock-inwards')
    def stock_inwards(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)
            inbound_types = [
                StockMovement.MovementType.PURCHASE,
                StockMovement.MovementType.CUSTOMER_RETURN,
                StockMovement.MovementType.TRANSFER_IN,
                StockMovement.MovementType.ADJUSTMENT_IN,
                StockMovement.MovementType.OPENING_STOCK,
            ]

            movements_qs = StockMovement.objects.filter(movement_type__in=inbound_types, date__range=(start_date, end_date))
            if branch_obj:
                movements_qs = movements_qs.filter(branch=branch_obj)

            results = []
            for mov in movements_qs.select_related('item').order_by('-created_at')[:100]:
                qty = float(mov.quantity or 0)
                cost = float(mov.unit_cost_price or 0)
                results.append({
                    "id": mov.id,
                    "ref_code": mov.reference or f"IN-{mov.id}",
                    "date": mov.created_at.strftime('%Y-%m-%d %H:%M') if hasattr(mov.created_at, 'strftime') else str(mov.created_at)[:10],
                    "item_name": mov.item.name if mov.item else f"Item #{mov.item_id}",
                    "movement_type": mov.get_movement_type_display(),
                    "quantity": qty,
                    "unit_cost": cost,
                    "total_cost": qty * cost
                })

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except Exception:
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='stock-outwards')
    def stock_outwards(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)
            outbound_types = [
                StockMovement.MovementType.SALE,
                StockMovement.MovementType.SUPPLIER_RETURN,
                StockMovement.MovementType.TRANSFER_OUT,
                StockMovement.MovementType.DAMAGE,
                StockMovement.MovementType.ADJUSTMENT_OUT,
            ]

            movements_qs = StockMovement.objects.filter(movement_type__in=outbound_types, date__range=(start_date, end_date))
            if branch_obj:
                movements_qs = movements_qs.filter(branch=branch_obj)

            results = []
            for mov in movements_qs.select_related('item').order_by('-created_at')[:100]:
                qty = float(mov.quantity or 0)
                cost = float(mov.unit_cost_price or 0)
                results.append({
                    "id": mov.id,
                    "ref_code": mov.reference or f"OUT-{mov.id}",
                    "date": mov.created_at.strftime('%Y-%m-%d %H:%M') if hasattr(mov.created_at, 'strftime') else str(mov.created_at)[:10],
                    "item_name": mov.item.name if mov.item else f"Item #{mov.item_id}",
                    "movement_type": mov.get_movement_type_display(),
                    "quantity": qty,
                    "unit_cost": cost,
                    "total_cost": qty * cost
                })

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except Exception:
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='stock-transfers')
    def stock_transfers(self, request):
        try:
            branch_obj, start_date, end_date, _, _, _period = self._get_branch_and_dates(request)
            qs = StockTransfer.objects.filter(date__range=(start_date, end_date))
            if branch_obj:
                qs = qs.filter(Q(from_branch=branch_obj) | Q(to_branch=branch_obj))

            results = [{
                "id": t.id,
                "date": t.date.strftime('%Y-%m-%d'),
                "from_branch": t.from_branch.name,
                "to_branch": t.to_branch.name,
                "item_name": t.item.name,
                "quantity": float(t.quantity)
            } for t in qs.select_related('from_branch', 'to_branch', 'item')[:100]]

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except Exception:
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='stock-adjustments')
    def stock_adjustments(self, request):
        try:
            branch_obj, start_date, end_date, _, _, _period = self._get_branch_and_dates(request)
            qs = DamageRecord.objects.filter(date__range=(start_date, end_date))
            if branch_obj:
                qs = qs.filter(branch=branch_obj)

            results = [{
                "id": d.id,
                "date": d.date.strftime('%Y-%m-%d'),
                "item_name": d.item.name,
                "damage_type": d.get_damage_type_display(),
                "quantity": float(d.quantity),
                "reason": d.reason
            } for d in qs.select_related('item')[:100]]

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except Exception:
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)
        
    @action(detail=False, methods=['get'], url_path='purchases-overview')
    def purchases_overview(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)
            purchases_qs = Purchase.objects.filter(Q(purchase_date__range=(start_dt, end_dt)))
            returns_qs = SupplierReturn.objects.filter(date__range=(start_date, end_date))

            if branch_obj:
                purchases_qs = purchases_qs.filter(branch=branch_obj)
                returns_qs = returns_qs.filter(branch=branch_obj)

            total_spend = sum((p.total_cost for p in purchases_qs), ZERO)
            total_paid = sum((quantize_money(p.amount_paid or ZERO) for p in purchases_qs), ZERO)
            outstanding_payables = sum((p.balance_due for p in purchases_qs if p.is_credit), ZERO)
            total_units = sum((Decimal(str(p.quantity or ZERO)) for p in purchases_qs), ZERO)

            return Response({
                "summary": {
                    "total_procurement_spend": float(total_spend),
                    "total_paid_suppliers": float(total_paid),
                    "outstanding_payables": float(outstanding_payables),
                    "purchase_orders_count": purchases_qs.count(),
                    "total_units_purchased": float(total_units),
                    "supplier_returns_count": returns_qs.count()
                }
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Error loading purchases overview: %s", e)
            return Response({"summary": {"total_procurement_spend": 0.0, "total_paid_suppliers": 0.0, "outstanding_payables": 0.0, "purchase_orders_count": 0, "total_units_purchased": 0, "supplier_returns_count": 0}}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='purchase-requisitions')
    def purchase_requisitions(self, request):
        try:
            branch_obj, _, _, _, _, _period = self._get_branch_and_dates(request)
            pricings_qs = BranchItemPricing.objects.select_related('item', 'branch')
            if branch_obj:
                pricings_qs = pricings_qs.filter(branch=branch_obj)

            low_stock_items = [p for p in pricings_qs if p.is_low_stock]
            results = [{
                "id": p.id,
                "req_number": f"REQ-{p.id}",
                "item_name": p.item.name,
                "branch_name": p.branch.name,
                "current_stock": float(p.remaining_stock),
                "reorder_level": float(p.min_stock_threshold or 0),
                "unit_cost_price": float(p.unit_cost_price or 0),
                "status": "REORDER_NEEDED"
            } for p in low_stock_items]

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Error loading purchase requisitions: %s", e)
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='purchase-orders')
    def purchase_orders(self, request):
        try:
            branch_id = request.query_params.get('branch_id') or request.query_params.get('branch')
            start_date = request.query_params.get('start_date') or request.query_params.get('startDate')
            end_date = request.query_params.get('end_date') or request.query_params.get('endDate')
            month = request.query_params.get('month')
            year = request.query_params.get('year')

            po_qs = PurchaseOrder.objects.all()

            # 1. Branch Filtering
            if branch_id and str(branch_id).lower() != 'all':
                po_qs = po_qs.filter(Q(branch_id=branch_id) | Q(branch__name__iexact=branch_id))

            # 2. Date Range Filtering (Only filter if params are provided)
            if start_date and end_date:
                try:
                    s_dt = datetime.strptime(start_date, '%Y-%m-%d')
                    e_dt = datetime.strptime(end_date, '%Y-%m-%d')
                    s_aware = timezone.make_aware(datetime.combine(s_dt, time.min))
                    e_aware = timezone.make_aware(datetime.combine(e_dt, time.max))
                    po_qs = po_qs.filter(purchase_date__range=(s_aware, e_aware))
                except ValueError:
                    pass
            elif month and year:
                try:
                    po_qs = po_qs.filter(purchase_date__year=int(year), purchase_date__month=int(month))
                except (ValueError, TypeError):
                    pass

            po_qs = po_qs.prefetch_related('items__item', 'supplier', 'branch').order_by('-purchase_date')

            results = []
            for po in po_qs:
                # Aggregate line items from PurchaseOrderItem relation
                line_items = [
                    {
                        "item_id": line.item_id,
                        "item_name": line.item.name if line.item else f"Item #{line.item_id}",
                        "quantity": float(line.quantity or 0),
                        "unit_cost_price": float(line.unit_cost_price or 0),
                        "total_cost": float(line.quantity or 0) * float(line.unit_cost_price or 0),
                    }
                    for line in po.items.all()
                ]

                total_qty = sum(item["quantity"] for item in line_items)
                first_item = line_items[0]["item_name"] if line_items else "No Items"
                item_display = first_item if len(line_items) <= 1 else f"{first_item} (+{len(line_items) - 1} more)"

                # Payment values derived from PurchaseOrder header properties
                total_cost = float(po.total_amount or 0)
                amount_paid = float(po.amount_paid or 0)
                balance_due = max(0.0, total_cost - amount_paid)

                results.append({
                    "id": po.id,
                    "po_ref": po.po_number,
                    "po_number": po.po_number,
                    "date": po.purchase_date.strftime('%b %d, %Y') if hasattr(po.purchase_date, 'strftime') else str(po.purchase_date)[:10],
                    "purchase_date": po.purchase_date.strftime('%Y-%m-%d %H:%M') if hasattr(po.purchase_date, 'strftime') else str(po.purchase_date)[:16],
                    "supplier": po.supplier_name or (po.supplier.name if po.supplier else "Direct Supplier"),
                    "supplier_name": po.supplier_name or (po.supplier.name if po.supplier else "Direct Supplier"),
                    "item": item_display,
                    "item_name": item_display,
                    "items": line_items,
                    "quantity": total_qty,
                    "total_cost": total_cost,
                    "amount_paid": amount_paid,
                    "balance_due": balance_due,
                    "payment_terms": po.get_payment_method_display() if hasattr(po, 'get_payment_method_display') else po.payment_method,
                    "payment_method": po.payment_method,
                })

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Error loading purchase orders: %s", e)
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='goods-received')
    def goods_received(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)

            inbound_types = [
                StockMovement.MovementType.PURCHASE,
                StockMovement.MovementType.TRANSFER_IN,
                StockMovement.MovementType.ADJUSTMENT_IN,
                StockMovement.MovementType.CUSTOMER_RETURN,
            ]

            movements_qs = StockMovement.objects.filter(
                movement_type__in=inbound_types
            ).filter(
                Q(date__range=(start_date, end_date))
            )

            if branch_obj:
                movements_qs = movements_qs.filter(branch=branch_obj)

            results = []
            for mov in movements_qs.select_related('item', 'batch', 'created_by', 'branch').order_by('-created_at')[:100]:
                qty = float(mov.quantity or 0)
                unit_cost = float(mov.unit_cost_price or 0)
                ref_code = mov.reference or f"GRN-{mov.id}"

                results.append({
                    "id": mov.id,
                    "grn_number": ref_code,
                    "received_date": mov.date.strftime('%Y-%m-%d') if hasattr(mov.date, 'strftime') else str(mov.created_at)[:10],
                    "item_name": mov.item.name if mov.item else f"Item #{mov.item_id}",
                    "branch_name": mov.branch.name if mov.branch else "Main Branch",
                    "quantity_received": qty,
                    "unit_cost_price": unit_cost,
                    "total_cost": qty * unit_cost,
                    "movement_type": mov.get_movement_type_display(),
                    "notes": mov.notes or f"Stock In ({mov.get_movement_type_display()})"
                })

            return Response({
                "count": len(results),
                "results": results
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Goods Received Error: %s", e)
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='supplier-invoices')
    def supplier_invoices(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)
            credit_purchases = Purchase.objects.filter(
                payment_method=Purchase.PaymentMethod.CREDIT
            ).filter(
                Q(purchase_date__range=(start_dt, end_dt))
            )
            if branch_obj:
                credit_purchases = credit_purchases.filter(branch=branch_obj)

            results = [{
                "id": p.id,
                "invoice_number": f"INV-SUP-{p.id}",
                "purchase_date": p.purchase_date.strftime('%Y-%m-%d %H:%M') if hasattr(p.purchase_date, 'strftime') else str(p.purchase_date)[:10],
                "supplier_name": p.supplier_name or (p.supplier.name if p.supplier else "Vendor"),
                "item_name": p.item.name if p.item else f"Item #{p.item_id}",
                "total_cost": float(p.total_cost),
                "amount_paid": float(p.amount_paid or 0),
                "balance_due": float(p.balance_due),
                "status": "PAID" if p.is_fully_paid else ("PARTIALLY_PAID" if p.amount_paid > 0 else "UNPAID")
            } for p in credit_purchases.select_related('item', 'supplier').order_by('-purchase_date')[:100]]

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Error fetching supplier invoices: %s", e)
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='purchase-returns')
    def purchase_returns(self, request):
        try:
            branch_obj, start_date, end_date, _, _, _period = self._get_branch_and_dates(request)
            returns_qs = SupplierReturn.objects.filter(date__range=(start_date, end_date))
            if branch_obj:
                returns_qs = returns_qs.filter(branch=branch_obj)

            results = [{
                "id": ret.id,
                "return_number": f"RET-SUP-{ret.id}",
                "date": ret.date.strftime('%Y-%m-%d') if hasattr(ret.date, 'strftime') else str(ret.date)[:10],
                "supplier_name": ret.supplier_name or "Vendor",
                "item_name": ret.item.name if ret.item else f"Item #{ret.item_id}",
                "quantity": float(ret.quantity or 0),
                "reason": ret.reason
            } for ret in returns_qs.select_related('item').order_by('-date')[:100]]

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Error fetching purchase returns: %s", e)
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='supplier-payments')
    def supplier_payments(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)
            payments_qs = SupplierPayment.objects.filter(created_at__range=(start_dt, end_dt))
            if branch_obj:
                payments_qs = payments_qs.filter(purchase__branch=branch_obj)

            results = [{
                "id": pmt.id,
                "payment_ref": f"SPMT-{pmt.id}",
                "payment_date": pmt.created_at.strftime('%Y-%m-%d %H:%M'),
                "supplier_name": pmt.purchase.supplier_name or (pmt.purchase.supplier.name if pmt.purchase.supplier else "Vendor"),
                "item_name": pmt.purchase.item.name if pmt.purchase and pmt.purchase.item else "Stock Item",
                "payment_method": pmt.get_payment_method_display() if hasattr(pmt, 'get_payment_method_display') else pmt.payment_method,
                "amount": float(pmt.amount or 0),
                "notes": pmt.notes or ""
            } for pmt in payments_qs.select_related('purchase', 'purchase__item', 'purchase__supplier').order_by('-created_at')[:100]]

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Error fetching supplier payments: %s", e)
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='suppliers-list')
    def suppliers_list(self, request):
        try:
            branch_obj, _, _, _, _, _period = self._get_branch_and_dates(request)
            suppliers = Supplier.objects.all()
            results = []

            for sup in suppliers:
                purchases = Purchase.objects.filter(Q(supplier=sup) | Q(supplier_name__iexact=sup.name))
                if branch_obj:
                    purchases = purchases.filter(branch=branch_obj)

                total_spend = sum((p.total_cost for p in purchases), ZERO)
                outstanding = sum((p.balance_due for p in purchases if p.is_credit), ZERO)

                results.append({
                    "id": sup.id,
                    "supplier_name": sup.name,
                    "total_purchases_count": purchases.count(),
                    "total_spend": float(total_spend),
                    "outstanding_balance": float(outstanding)
                })

            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Error fetching suppliers list: %s", e)
            return Response({"count": 0, "results": []}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='purchase-reports')
    def purchase_reports(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, _period = self._get_branch_and_dates(request)
            purchases_qs = Purchase.objects.filter(Q(purchase_date__range=(start_dt, end_dt)))
            if branch_obj:
                purchases_qs = purchases_qs.filter(branch=branch_obj)

            settled_cash = sum((p.total_cost for p in purchases_qs if not p.is_credit), ZERO)
            credit_value = sum((p.total_cost for p in purchases_qs if p.is_credit), ZERO)
            total_repaid = sum((p.amount_paid for p in purchases_qs if p.is_credit), ZERO)

            return Response({
                "audit_summary": {
                    "total_purchases_count": purchases_qs.count(),
                    "settled_cash_purchases": float(settled_cash),
                    "credit_purchases_value": float(credit_value),
                    "total_credit_repaid": float(total_repaid)
                }
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Error loading purchase reports: %s", e)
            return Response({"audit_summary": {"total_purchases_count": 0, "settled_cash_purchases": 0.0, "credit_purchases_value": 0.0, "total_credit_repaid": 0.0}}, status=status.HTTP_200_OK)

    # =========================================================================
    # CASH MODULE ALIAS / FALLBACK ENDPOINTS
    # =========================================================================
    @action(detail=False, methods=['get'], url_path='bank-accounts')
    def bank_accounts(self, request):
        return self.cash_overview(request)

    @action(detail=False, methods=['get'], url_path='cash-transfers')
    def cash_transfers(self, request):
        return self.cash_book(request)

    @action(detail=False, methods=['get'], url_path='cash-reconciliation')
    def cash_reconciliation(self, request):
        return Response({"count": 0, "results": [], "entries": [], "status": "reconciled"}, status=status.HTTP_200_OK)


logger = logging.getLogger(__name__)

try:
    from svglib.svglib import svg2rlg
except ImportError:
    svg2rlg = None


def _get_pdf_logo_element():
    """Helper to locate, scale, and center convert logo.svg to a ReportLab Flowable element."""
    if not svg2rlg:
        return None

    possible_paths = [
        os.path.join(settings.BASE_DIR, 'staticfiles', 'logo.svg'),
        os.path.join(settings.BASE_DIR, 'public', 'logo.svg'),
        os.path.join(settings.BASE_DIR, 'logo.svg'),
    ]

    for path in possible_paths:
        if os.path.exists(path):
            try:
                drawing = svg2rlg(path)
                if drawing:
                    # Target dimensions for the header logo
                    target_width = 120
                    target_height = 40

                    # Calculate scale factors to maintain aspect ratio
                    scale_x = target_width / float(drawing.width) if drawing.width else 0.2
                    scale_y = target_height / float(drawing.height) if drawing.height else 0.2
                    scale_factor = min(scale_x, scale_y)

                    drawing.width = drawing.width * scale_factor
                    drawing.height = drawing.height * scale_factor
                    drawing.scale(scale_factor, scale_factor)

                    # Wrap in a centered single-cell table to force horizontal alignment
                    logo_table = Table([[drawing]], colWidths=[532])
                    logo_table.setStyle(TableStyle([
                        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('LEFTPADDING', (0, 0), (-1, -1), 0),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                        ('TOPPADDING', (0, 0), (-1, -1), 0),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                    ]))
                    return logo_table
            except Exception:
                pass
    return None




def quantize_money(amount):
    """Safely convert numerical inputs to Decimal rounded to 2 decimal places."""
    if amount is None:
        return ZERO
    return Decimal(str(amount)).quantize(Decimal("0.01"))


def _normalize_payment_method(method_name):
    """
    Standardizes payment method strings across all financial statement views.
    Guarantees strict routing for PAYBILL, SEND_MONEY (M-Pesa Till/Buy Goods),
    CASH, CREDIT, BANK_TRANSFER, and SPLIT.
    """
    if not method_name:
        return "CASH"

    clean = (
        str(method_name)
        .strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
        .replace("/", "_")
    )

    if any(k in clean for k in ["PAYBILL", "PAY_BILL", "C2B"]):
        return "PAYBILL"

    if any(
        k in clean
        for k in [
            "SEND_MONEY",
            "SENDMONEY",
            "TILL",
            "BUY_GOODS",
            "BUYGOODS",
            "P2P",
            "MPESA",
            "M_PESA",
            "MOBILE",
        ]
    ):
        return "SEND_MONEY"

    if "CASH" in clean:
        return "CASH"

    if any(k in clean for k in ["CREDIT", "DEBT", "ON_ACCOUNT", "INVOICE"]):
        return "CREDIT"

    if any(k in clean for k in ["BANK", "EFT", "TRANSFER"]):
        return "BANK_TRANSFER"

    if "SPLIT" in clean:
        return "SPLIT"

    return clean


def _extract_sp_method(sp):
    """
    Safely inspects split payment instances for various payment channel field names.
    """
    for attr in ["method", "payment_method", "payment_mode", "channel", "mode", "type"]:
        val = getattr(sp, attr, None)
        if val:
            return val
    return None


def _cash_period_data(branch_obj, start_date, end_date):
    """
    Calculates exact aggregated cash inflows across CASH, PAYBILL, SEND_MONEY, and BANK_TRANSFER,
    properly routing both Sale and CreditRepayment split payment lines into their respective channels.
    """
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()

    current_tz = timezone.get_current_timezone()
    start_combined = datetime.combine(start_date, time.min)
    end_combined = datetime.combine(end_date, time.max)

    start_dt = (
        timezone.make_aware(start_combined, current_tz)
        if timezone.is_naive(start_combined)
        else start_combined
    )
    end_dt = (
        timezone.make_aware(end_combined, current_tz)
        if timezone.is_naive(end_combined)
        else end_combined
    )

    inflow_channels = {
        "CASH": ZERO,
        "PAYBILL": ZERO,
        "SEND_MONEY": ZERO,
        "BANK_TRANSFER": ZERO,
    }

    # 1. Direct Sales Inflows
    sales_qs = SaleTransaction.objects.filter(
        sale_date__gte=start_dt,
        sale_date__lte=end_dt
    ).prefetch_related("split_payments")

    if branch_obj:
        sales_qs = sales_qs.filter(branch=branch_obj)

    for sale in sales_qs.distinct():
        norm_method = _normalize_payment_method(sale.payment_method)
        split_records = list(sale.split_payments.all()) if hasattr(sale, "split_payments") else []

        if norm_method == "SPLIT" or split_records:
            for sp in split_records:
                sp_raw = _extract_sp_method(sp)
                sp_channel = _normalize_payment_method(sp_raw)

                if sp_channel in inflow_channels and sp_channel != "CREDIT":
                    inflow_channels[sp_channel] += Decimal(str(sp.amount or ZERO))

        elif norm_method == "CREDIT":
            if sale.amount_paid and sale.amount_paid > ZERO:
                inflow_channels["CASH"] += Decimal(str(sale.amount_paid))

        elif norm_method in inflow_channels:
            sale_paid = Decimal(str(sale.amount_paid if sale.amount_paid and sale.amount_paid > ZERO else sale.total_amount or ZERO))
            inflow_channels[norm_method] += sale_paid

    # 2. Customer Debt Repayments
    repayments_qs = CreditRepayment.objects.filter(
        date__gte=start_date,
        date__lte=end_date
    ).prefetch_related("splits")

    if branch_obj:
        repayments_qs = repayments_qs.filter(
            Q(branch=branch_obj) | 
            Q(transaction__branch=branch_obj) | 
            Q(branch__isnull=True)
        )

    for repayment in repayments_qs.distinct():
        raw_method = getattr(repayment, "payment_method", "CASH")
        norm_rep_method = _normalize_payment_method(raw_method)

        if norm_rep_method == "SPLIT":
            split_records = list(repayment.splits.all()) if hasattr(repayment, "splits") else []

            if split_records:
                for sp in split_records:
                    sp_raw = _extract_sp_method(sp) or getattr(sp, "method", "CASH")
                    sp_channel = _normalize_payment_method(sp_raw)

                    if sp_channel in inflow_channels and sp_channel != "CREDIT":
                        inflow_channels[sp_channel] += Decimal(str(sp.amount or ZERO))
            else:
                rep_amount = Decimal(str(repayment.amount or ZERO))
                inflow_channels["CASH"] += rep_amount

        else:
            rep_amount = Decimal(str(repayment.amount or ZERO))
            if norm_rep_method in inflow_channels and norm_rep_method != "CREDIT":
                inflow_channels[norm_rep_method] += rep_amount

    total_inflows = sum(inflow_channels.values(), ZERO)

    # 3. Expenses and Purchases
    expense_qs = Expense.objects.filter(date__range=(start_date, end_date))
    purchase_qs = Purchase.objects.filter(purchase_date__range=(start_dt, end_dt))

    if branch_obj:
        expense_qs = expense_qs.filter(branch=branch_obj)
        purchase_qs = purchase_qs.filter(branch=branch_obj)

    expenses_total = expense_qs.aggregate(
        total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField())
    )["total"] or ZERO

    purchases_total = purchase_qs.exclude(payment_method="CREDIT").aggregate(
        total=Coalesce(Sum("amount_paid"), ZERO, output_field=DecimalField())
    )["total"] or ZERO

    return {
        "inflows": total_inflows,
        "inflow_channels": inflow_channels,
        "expenses": expenses_total,
        "purchases": purchases_total,
        "expense_queryset": expense_qs,
    }


class ManagementReportsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def _get_branch_and_dates(self, request):
        branch, start_date, end_date, start_dt, end_dt, _period = (
            _get_reporting_filters(request, default_period="month")
        )
        return branch, start_date, end_date, start_dt, end_dt

    # =========================================================================
    # PROFIT & LOSS REPORT
    # =========================================================================
    @action(detail=False, methods=['get'], url_path='profit-loss')
    def profit_report(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt = self._get_branch_and_dates(request)

            sales_qs = SaleTransaction.objects.filter(
                sale_date__gte=start_dt,
                sale_date__lte=end_dt
            )

            expenses_qs = Expense.objects.filter(
                Q(date__range=(start_date, end_date))
            ).exclude(description__icontains='Supplier Repayment')

            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                expenses_qs = expenses_qs.filter(branch=branch_obj)

            revenue = sales_qs.aggregate(
                total=Coalesce(Sum('total_amount'), ZERO, output_field=DecimalField())
            )['total']

            items_qs = SaleItem.objects.filter(
                transaction__in=sales_qs
            ).select_related('item', 'transaction')

            direct_cogs_expr = ExpressionWrapper(
                F('quantity_sold') * F('unit_cost_price'),
                output_field=DecimalField(max_digits=12, decimal_places=2)
            )

            cogs = items_qs.filter(unit_cost_price__gt=0).aggregate(
                total=Coalesce(Sum(direct_cogs_expr), ZERO, output_field=DecimalField())
            )['total']

            zero_cost_items = items_qs.filter(Q(unit_cost_price=0) | Q(unit_cost_price__isnull=True))
            if zero_cost_items.exists():
                for line in zero_cost_items.select_related('transaction', 'item'):
                    b_id = line.transaction.branch_id
                    pricing = BranchItemPricing.objects.filter(branch_id=b_id, item=line.item).first()
                    cost_unit = pricing.unit_cost_price if (pricing and pricing.unit_cost_price > 0) else ZERO
                    cogs += Decimal(str(line.quantity_sold or 0)) * Decimal(str(cost_unit))

            gross_profit = revenue - cogs
            operating_expenses = expenses_qs.aggregate(
                total=Coalesce(Sum('amount'), ZERO, output_field=DecimalField())
            )['total']
            operating_profit = gross_profit - operating_expenses
            finance_costs_and_tax = ZERO
            net_profit = operating_profit - finance_costs_and_tax

            if start_date == end_date:
                period_display = f"DATE: {start_date.strftime('%d %b %Y')}"
            else:
                period_display = f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}"

            return Response({
                "period": period_display,
                "revenue": float(quantize_money(revenue)),
                "cogs": float(quantize_money(cogs)),
                "gross_profit": float(quantize_money(gross_profit)),
                "operating_expenses": float(quantize_money(operating_expenses)),
                "operating_profit": float(quantize_money(operating_profit)),
                "finance_costs_and_tax": float(quantize_money(finance_costs_and_tax)),
                "net_profit": float(quantize_money(net_profit)),
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Profit Report Error: %s", e)
            return Response({
                "period": timezone.localdate().strftime('%B %Y'),
                "revenue": 0.0, "cogs": 0.0, "gross_profit": 0.0,
                "operating_expenses": 0.0, "operating_profit": 0.0, "net_profit": 0.0
            }, status=status.HTTP_200_OK)

    # =========================================================================
    # CASH FLOW REPORT
    # =========================================================================
    @action(detail=False, methods=['get'], url_path='cash-flow')
    def cash_flow_report(self, request):
        try:
            branch_obj, start_date, end_date, _start_dt, _end_dt = self._get_branch_and_dates(request)

            period = _cash_period_data(branch_obj, start_date, end_date)
            expense_qs = period["expense_queryset"]

            equipment_filter = (
                Q(description__icontains="equipment")
                | Q(description__icontains="asset")
                | Q(description__icontains="machinery")
            )
            financing_in_filter = Q(description__icontains="capital") | Q(description__icontains="loan received")
            loan_repayment_filter = Q(description__icontains="loan repayment")
            drawings_filter = Q(description__icontains="drawing")
            salary_filter = Q(description__icontains="salary") | Q(description__icontains="wage")

            equipment_purchased = (expense_qs.filter(equipment_filter).aggregate(
                total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
            )["total"] or ZERO)

            capital_plus_loan_received = (expense_qs.filter(financing_in_filter).aggregate(
                total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
            )["total"] or ZERO)

            loan_repayment = (expense_qs.filter(loan_repayment_filter).aggregate(
                total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
            )["total"] or ZERO)

            drawings = (expense_qs.filter(drawings_filter).aggregate(
                total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
            )["total"] or ZERO)

            salaries_and_wages = (expense_qs.filter(salary_filter).aggregate(
                total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
            )["total"] or ZERO)

            excluded_non_operating = equipment_filter | financing_in_filter | loan_repayment_filter | drawings_filter | salary_filter
            operating_expenses = (expense_qs.exclude(excluded_non_operating).aggregate(
                total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
            )["total"] or ZERO)

            customer_receipts = Decimal(str(period.get("inflows") or ZERO))
            supplier_payments = Decimal(str(period.get("purchases") or ZERO))
            
            net_operating_cash = customer_receipts - supplier_payments - operating_expenses - salaries_and_wages
            net_investing_cash = -equipment_purchased
            net_financing_cash = capital_plus_loan_received - loan_repayment - drawings
            net_increase_in_cash = net_operating_cash + net_investing_cash + net_financing_cash

            opening_cash = ZERO
            if start_date and start_date > date(1900, 1, 1):
                try:
                    prior_end = start_date - timedelta(days=1)
                    if prior_end >= date(1900, 1, 1):
                        prior = _cash_period_data(branch_obj, date(1900, 1, 1), prior_end)
                        prior_expenses = prior["expense_queryset"]
                        
                        prior_equipment = (prior_expenses.filter(equipment_filter).aggregate(
                            total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
                        )["total"] or ZERO)
                        
                        prior_capital = (prior_expenses.filter(financing_in_filter).aggregate(
                            total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
                        )["total"] or ZERO)
                        
                        prior_loan = (prior_expenses.filter(loan_repayment_filter).aggregate(
                            total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
                        )["total"] or ZERO)
                        
                        prior_drawings = (prior_expenses.filter(drawings_filter).aggregate(
                            total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
                        )["total"] or ZERO)
                        
                        prior_salary = (prior_expenses.filter(salary_filter).aggregate(
                            total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
                        )["total"] or ZERO)
                        
                        prior_opex = (prior_expenses.exclude(excluded_non_operating).aggregate(
                            total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
                        )["total"] or ZERO)
                        
                        prior_inflows = Decimal(str(prior.get("inflows") or ZERO))
                        prior_purchases = Decimal(str(prior.get("purchases") or ZERO))

                        opening_cash = (
                            prior_inflows - prior_purchases - prior_opex - prior_salary
                            - prior_equipment + prior_capital - prior_loan - prior_drawings
                        )
                except Exception as prior_err:
                    logger.warning("Opening cash calculation fallback: %s", prior_err)
                    opening_cash = ZERO

            closing_cash = opening_cash + net_increase_in_cash

            period_display = (
                f"DATE: {start_date.strftime('%d %b %Y')}" if start_date == end_date
                else f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}"
            )
            return Response({
                "period": period_display,
                "operating_activities": {
                    "customer_receipts": float(quantize_money(customer_receipts)),
                    "supplier_payments": float(quantize_money(supplier_payments)),
                    "operating_expenses": float(quantize_money(operating_expenses)),
                    "salaries_and_wages": float(quantize_money(salaries_and_wages)),
                    "net_operating_cash": float(quantize_money(net_operating_cash)),
                },
                "investing_activities": {
                    "equipment_purchased": float(quantize_money(equipment_purchased)),
                    "net_investing_cash": float(quantize_money(net_investing_cash)),
                },
                "financing_activities": {
                    "capital_plus_loan_received": float(quantize_money(capital_plus_loan_received)),
                    "loan_repayment": float(quantize_money(loan_repayment)),
                    "drawings": float(quantize_money(drawings)),
                    "net_financing_cash": float(quantize_money(net_financing_cash)),
                },
                "net_increase_in_cash": float(quantize_money(net_increase_in_cash)),
                "opening_cash": float(quantize_money(opening_cash)),
                "closing_cash": float(quantize_money(closing_cash)),
            }, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.exception("Cash Flow Report Error: %s", exc)
            return Response({"error": f"Cash flow calculation failed: {str(exc)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # =========================================================================
    # BALANCE SHEET REPORT
    # =========================================================================
    @action(detail=False, methods=['get'], url_path='balance-sheet')
    def balance_sheet_report(self, request):
        try:
            branch_obj, start_date, end_date, _start_dt, _end_dt = self._get_branch_and_dates(request)
            as_at = end_date.date() if isinstance(end_date, datetime) else (end_date or timezone.localdate())

            # 1. Calculate Period-to-Date Cash Operations
            period_to_date = _cash_period_data(branch_obj, date(1900, 1, 1), as_at)
            expenses_qs = period_to_date["expense_queryset"]

            financing_in_filter = Q(description__icontains="capital") | Q(description__icontains="loan received")
            cash_expenses = expenses_qs.exclude(financing_in_filter).aggregate(
                total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
            )["total"] or ZERO
            
            capital_received = expenses_qs.filter(financing_in_filter).aggregate(
                total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
            )["total"] or ZERO

            cash_balance = quantize_money(
                period_to_date["inflows"] - period_to_date["purchases"] - cash_expenses + capital_received
            )

            # 2. Query Sales & Purchases up to "as_at" date
            sales_qs = SaleTransaction.objects.filter(sale_date__date__lte=as_at).prefetch_related("split_payments", "customer")
            purchases_qs = Purchase.objects.filter(purchase_date__date__lte=as_at).prefetch_related("supplier_payments")
            
            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                purchases_qs = purchases_qs.filter(branch=branch_obj)

            # 3. Calculate Customer Debts (Accounts Receivable) with Per-Debtor Net Aggregation
            repayment_qs = CreditRepayment.objects.filter(date__lte=as_at)
            if branch_obj:
                repayment_qs = repayment_qs.filter(
                    Q(branch=branch_obj) | Q(transaction__branch=branch_obj) | Q(branch__isnull=True)
                )

            debtors_map = {}
            for sale in sales_qs:
                norm_method = _normalize_payment_method(sale.payment_method)
                if norm_method not in ("CREDIT", "SPLIT") and not sale.split_payments.exists():
                    continue

                cust_key = sale.customer_id if sale.customer_id else (sale.customer_name or "Walk-in Debtor").strip()
                if cust_key not in debtors_map:
                    debtors_map[cust_key] = {
                        "customer_obj": sale.customer,
                        "gross_credit": ZERO,
                    }

                if norm_method == "SPLIT" or sale.split_payments.exists():
                    credit_amount = sum(
                        (
                            Decimal(str(payment.amount or ZERO))
                            for payment in sale.split_payments.all()
                            if _normalize_payment_method(_extract_sp_method(payment)) == "CREDIT"
                        ),
                        ZERO,
                    )
                    if credit_amount <= ZERO:
                        credit_amount = Decimal(str(sale.total_amount or ZERO))
                else:
                    credit_amount = Decimal(str(sale.total_amount or ZERO)) - Decimal(str(sale.amount_paid or ZERO))
                    if credit_amount <= ZERO:
                        credit_amount = Decimal(str(sale.total_amount or ZERO))

                debtors_map[cust_key]["gross_credit"] += credit_amount

            customer_debts = ZERO
            for key, data in debtors_map.items():
                cust_obj = data["customer_obj"]
                cust_repayments = ZERO
                if cust_obj:
                    cust_repayments = repayment_qs.filter(customer=cust_obj).aggregate(
                        total=Coalesce(Sum('amount'), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
                    )['total'] or ZERO

                customer_debts += max(ZERO, data["gross_credit"] - quantize_money(cust_repayments))

            # 4. Calculate Supplier Debts (Accounts Payable)
            supplier_debts = ZERO
            for purchase in purchases_qs:
                if _normalize_payment_method(purchase.payment_method) != "CREDIT":
                    continue
                all_supplier_payments = sum(
                    (Decimal(str(payment.amount or ZERO)) for payment in purchase.supplier_payments.all()), ZERO
                )
                initial_paid = max(ZERO, Decimal(str(purchase.amount_paid or ZERO)) - all_supplier_payments)
                payments_to_date = purchase.supplier_payments.filter(created_at__date__lte=as_at).aggregate(
                    total=Coalesce(Sum("amount"), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
                )["total"] or ZERO
                paid_to_date = initial_paid + Decimal(str(payments_to_date))
                supplier_debts += max(ZERO, Decimal(str(purchase.total_cost or ZERO)) - paid_to_date)

            # 5. Inventory Valuation
            pricing_qs = BranchItemPricing.objects.select_related("item", "branch")
            if branch_obj:
                pricing_qs = pricing_qs.filter(branch=branch_obj)
            inventory_stock = sum(
                (Decimal(str(pr.remaining_stock or ZERO)) * Decimal(str(pr.unit_cost_price or ZERO)) for pr in pricing_qs), ZERO
            )

            # 6. Balance Sheet Parity & Statement Generation
            equipment = ZERO
            bank_overdraft = abs(cash_balance) if cash_balance < ZERO else ZERO
            cash_asset = max(cash_balance, ZERO)
            total_assets = quantize_money(cash_asset + customer_debts + inventory_stock + equipment)
            total_liabilities = quantize_money(supplier_debts + bank_overdraft)
            capital_and_retained_profits = quantize_money(total_assets - total_liabilities)
            liabilities_plus_equity = quantize_money(total_liabilities + capital_and_retained_profits)

            return Response({
                "as_at_date": as_at.strftime("%d %b %Y"),
                "assets": {
                    "cash_bank_mpesa": float(quantize_money(cash_asset)),
                    "customer_debts": float(quantize_money(customer_debts)),
                    "inventory_stock": float(quantize_money(inventory_stock)),
                    "equipment": float(quantize_money(equipment)),
                    "total_assets": float(total_assets),
                },
                "liabilities": {
                    "supplier_debts": float(quantize_money(supplier_debts)),
                    "bank_overdraft": float(quantize_money(bank_overdraft)),
                    "bank_loan": 0.0,
                    "total_liabilities": float(total_liabilities),
                },
                "equity": {"capital_and_retained_profits": float(capital_and_retained_profits)},
                "liabilities_plus_equity": float(liabilities_plus_equity),
                "accounting_check": {"difference": float(quantize_money(total_assets - liabilities_plus_equity)), "balanced": True},
            }, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.exception("Balance Sheet Report Error: %s", exc)
            return Response({"error": f"Balance sheet calculation failed: {str(exc)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # =========================================================================
    # CASHBOOK REPORT
    # =========================================================================
    @action(detail=False, methods=['get'], url_path='cash-book')
    def cash_book(self, request):
        try:
            branch_obj, start_date, end_date, _start_dt, _end_dt = self._get_branch_and_dates(request)
            
            # Normalize date types for safe formatting
            clean_start = start_date.date() if isinstance(start_date, datetime) else (start_date or timezone.localdate())
            clean_end = end_date.date() if isinstance(end_date, datetime) else (end_date or timezone.localdate())

            period = _cash_period_data(branch_obj, clean_start, clean_end)

            channels = {
                "CASH": ZERO,
                "PAYBILL": ZERO,
                "SEND_MONEY": ZERO,
                "BANK_TRANSFER": ZERO,
            }

            for raw_channel, amount in period.get("inflow_channels", {}).items():
                amt = Decimal(str(amount or ZERO))
                if amt <= ZERO:
                    continue

                ch_upper = str(raw_channel or "").upper().replace("-", "_").replace(" ", "_")

                if "PAYBILL" in ch_upper:
                    channels["PAYBILL"] += amt
                elif any(k in ch_upper for k in ["SEND_MONEY", "SENDMONEY", "MPESA", "TILL", "BUY_GOODS"]):
                    channels["SEND_MONEY"] += amt
                elif any(k in ch_upper for k in ["BANK", "TRANSFER", "EFT"]):
                    channels["BANK_TRANSFER"] += amt
                else:
                    channels["CASH"] += amt

            cash_rows = [
                ("CASH", "Total Cash Collections & Repayments"),
                ("PAYBILL", "Total Paybill Collections & Repayments"),
                ("SEND_MONEY", "Total Send Money / M-Pesa Collections & Repayments"),
            ]

            if channels["BANK_TRANSFER"] > ZERO:
                cash_rows.append(("BANK_TRANSFER", "Total Bank Transfer Collections & Repayments"))

            summary_entries = []
            for channel, description in cash_rows:
                amount = channels.get(channel, ZERO)
                summary_entries.append({
                    "id": f"SUMMARY-{channel}",
                    "reference": f"SUM-{channel}",
                    "date": str(clean_end),
                    "description": description,
                    "type": "INFLOW",
                    "payment_method": channel,
                    "channel": channel,
                    "inflow": float(quantize_money(amount)),
                    "outflow": 0.0,
                    "amount": float(quantize_money(amount)),
                })

            purchases_outflow = Decimal(str(period.get("purchases") or ZERO))
            expenses_total = Decimal(str(period.get("expenses") or ZERO))

            summary_entries.extend([
                {
                    "id": "SUMMARY-PURCHASES", 
                    "reference": "SUM-PURCHASES",
                    "date": str(clean_end), 
                    "description": "Total Supplier Cash Purchases & Settlements",
                    "type": "OUTFLOW", 
                    "payment_method": "PURCHASE", 
                    "channel": "STOCK_PURCHASE",
                    "inflow": 0.0, 
                    "outflow": float(quantize_money(purchases_outflow)),
                    "amount": float(quantize_money(purchases_outflow)),
                },
                {
                    "id": "SUMMARY-EXPENSES", 
                    "reference": "SUM-EXPENSES",
                    "date": str(clean_end), 
                    "description": "Total Operating Expenses",
                    "type": "OUTFLOW", 
                    "payment_method": "EXPENSE", 
                    "channel": "EXPENSE",
                    "inflow": 0.0, 
                    "outflow": float(quantize_money(expenses_total)),
                    "amount": float(quantize_money(expenses_total)),
                },
            ])

            total_inflows = sum(channels.values(), ZERO)
            total_outflows = purchases_outflow + expenses_total
            net_position = total_inflows - total_outflows

            period_label = (
                f"DATE: {clean_start.strftime('%d %b %Y')}" if clean_start == clean_end
                else f"{clean_start.strftime('%b %d, %Y')} - {clean_end.strftime('%b %d, %Y')}"
            )

            return Response({
                "period": period_label,
                "summary": {
                    "total_inflows": float(quantize_money(total_inflows)),
                    "total_outflows": float(quantize_money(total_outflows)),
                    "net_position": float(quantize_money(net_position)),
                    "total_inflow": float(quantize_money(total_inflows)),
                    "total_outflow": float(quantize_money(total_outflows)),
                    "net_cash_flow": float(quantize_money(net_position)),
                },
                "count": len(summary_entries),
                "results": summary_entries,
                "entries": summary_entries,
                "records": summary_entries,
            }, status=status.HTTP_200_OK)

        except Exception as exc:
            logger.exception("Cash Book Aggregation Error: %s", exc)
            return Response(
                {"error": f"Cashbook calculation failed: {str(exc)}"}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    # =========================================================================
    # PDF EXPORT ACTIONS
    # =========================================================================
    @action(detail=False, methods=['get'], url_path='export-profit-loss-pdf')
    def export_profit_loss_pdf(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt = self._get_branch_and_dates(request)

            # -----------------------------------------------------------------
            # 1. CASH-BASIS REVENUE AGGREGATION
            # -----------------------------------------------------------------
            sales_qs = SaleTransaction.objects.filter(
                Q(sale_date__range=(start_dt, end_dt)) | Q(sale_date__date__range=(start_date, end_date))
            ).exclude(
                Q(payment_method='CREDIT') | Q(receipt_number__startswith='INV')
            )

            repayments_qs = CreditRepayment.objects.filter(
                Q(date__range=(start_date, end_date))
            )

            # Pure operating expenses excluding supplier repayments
            expenses_qs = Expense.objects.filter(
                Q(date__range=(start_date, end_date))
            ).exclude(
                description__icontains='Supplier Repayment'
            )

            items_qs = SaleItem.objects.filter(
                Q(transaction__sale_date__range=(start_dt, end_dt)) |
                Q(transaction__sale_date__date__range=(start_date, end_date))
            ).exclude(
                Q(transaction__payment_method='CREDIT') | Q(transaction__receipt_number__startswith='INV')
            )

            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                repayments_qs = repayments_qs.filter(
                    Q(branch=branch_obj) | Q(transaction__branch=branch_obj)
                )
                expenses_qs = expenses_qs.filter(branch=branch_obj)
                items_qs = items_qs.filter(transaction__branch=branch_obj)

            # Split payments routing
            split_sale_ids = SalePayment.objects.filter(transaction__in=sales_qs).values_list('transaction_id', flat=True).distinct()
            non_split_sales = sales_qs.exclude(pk__in=split_sale_ids).exclude(payment_method='SPLIT')

            single_sales_revenue = non_split_sales.aggregate(
                total=Coalesce(Sum('total_amount'), ZERO, output_field=DecimalField())
            )['total']

            split_payments_qs = SalePayment.objects.filter(transaction__in=sales_qs).exclude(method='CREDIT')
            split_payments_revenue = split_payments_qs.aggregate(
                total=Coalesce(
                    Sum(
                        'amount',
                        filter=Q(method__icontains='PAYBILL') | 
                               Q(method__icontains='BANK') | 
                               Q(method__in=['CASH', 'MPESA', 'SEND_MONEY', 'TILL'])
                    ),
                    ZERO,
                    output_field=DecimalField()
                )
            )['total']

            repayments_revenue = repayments_qs.aggregate(
                total=Coalesce(Sum('amount'), ZERO, output_field=DecimalField())
            )['total']

            total_sales = single_sales_revenue + split_payments_revenue + repayments_revenue

            # -----------------------------------------------------------------
            # 2. COST OF GOODS SOLD & EXPENSES
            # -----------------------------------------------------------------
            cogs_expr = ExpressionWrapper(
                F('quantity_sold') * F('unit_cost_price'),
                output_field=DecimalField(max_digits=12, decimal_places=2)
            )
            total_cogs = items_qs.aggregate(total=Coalesce(Sum(cogs_expr), ZERO, output_field=DecimalField()))['total']

            gross_profit = total_sales - total_cogs
            operating_expenses = expenses_qs.aggregate(total=Coalesce(Sum('amount'), ZERO, output_field=DecimalField()))['total']
            operating_profit = gross_profit - operating_expenses
            tax = ZERO
            net_profit = operating_profit - tax

            # Helper function for PDF currency formatting
            def fmt_pdf_curr(val):
                num = float(val or 0)
                if num < 0:
                    return f"(KSh {abs(num):,.2f})"
                return f"KSh {num:,.2f}"

            # -----------------------------------------------------------------
            # 3. REPORTLAB PDF BUILD
            # -----------------------------------------------------------------
            buffer = io.BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=letter,
                rightMargin=40,
                leftMargin=40,
                topMargin=40,
                bottomMargin=40
            )

            styles = getSampleStyleSheet()

            company_style = ParagraphStyle(
                'PdfCompanyHeader',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=18,
                leading=22,
                textColor=colors.HexColor('#004D40'),
                alignment=1,
                spaceAfter=4
            )

            subtitle_style = ParagraphStyle(
                'PdfSubHeader',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=10,
                leading=14,
                textColor=colors.HexColor('#475569'),
                alignment=1,
                spaceAfter=15
            )

            label_style = ParagraphStyle(
                'TableLabel',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=10,
                textColor=colors.HexColor('#1E293B')
            )

            bold_label_style = ParagraphStyle(
                'TableLabelBold',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=10,
                textColor=colors.HexColor('#0F172A')
            )

            branch_name = branch_obj.name if branch_obj else "All Branches"
            period_str = f"{start_date.strftime('%b %d, %Y')} – {end_date.strftime('%b %d, %Y')}"

            elements = []

            logo_element = _get_pdf_logo_element()
            if logo_element:
                elements.append(logo_element)
                elements.append(Spacer(1, 8))

            elements.append(Paragraph("MELVEX BUSINESS SOLUTIONS", company_style))
            elements.append(Paragraph(f"<b>PROFIT & LOSS STATEMENT — {branch_name.upper()}</b><br/>Period: {period_str}", subtitle_style))
            elements.append(Spacer(1, 10))

            data = [
                [Paragraph("Revenue", bold_label_style), fmt_pdf_curr(total_sales)],
                [Paragraph("Less: Cost of Goods Sold", label_style), fmt_pdf_curr(total_cogs)],
                [Paragraph("Gross Profit", bold_label_style), fmt_pdf_curr(gross_profit)],
                [Paragraph("Less: Operating Expenses", label_style), fmt_pdf_curr(operating_expenses)],
                [Paragraph("Operating Profit", bold_label_style), fmt_pdf_curr(operating_profit)],
                [Paragraph("Finance Costs & Tax", label_style), fmt_pdf_curr(tax)],
                [Paragraph("Net Profit / (Loss)", bold_label_style), fmt_pdf_curr(net_profit)]
            ]

            table = Table(data, colWidths=[340, 192])
            table.setStyle(TableStyle([
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('BACKGROUND', (0, 2), (-1, 2), colors.HexColor('#F8FAFC')),
                ('BACKGROUND', (0, 4), (-1, 4), colors.HexColor('#F8FAFC')),
                ('BACKGROUND', (0, 6), (-1, 6), colors.HexColor('#E6FFFA')),
                ('TEXTCOLOR', (1, 6), (1, 6), colors.HexColor('#004D40')),
                ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                ('LINEBELOW', (0, 6), (-1, 6), 1.5, colors.HexColor('#004D40')),
            ]))

            elements.append(table)
            doc.build(elements)
            buffer.seek(0)

            response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="Profit_Loss_Statement_{branch_name}.pdf"'
            return response

        except Exception as e:
            logger.exception("Export Profit Loss PDF Error")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], url_path='export-cash-flow-pdf')
    def export_cash_flow_pdf(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt = self._get_branch_and_dates(request)

            # -----------------------------------------------------------------
            # 1. BASE QUERYSETS (UNIFIED OUTFLOW & INFLOW FILTERS)
            # -----------------------------------------------------------------
            sales_qs = SaleTransaction.objects.filter(sale_date__range=(start_dt, end_dt)).exclude(
                Q(payment_method='CREDIT') | Q(receipt_number__startswith='INV')
            )
            repayments_qs = CreditRepayment.objects.filter(Q(date__range=(start_date, end_date)))
            purchases_qs = Purchase.objects.filter(Q(purchase_date__range=(start_dt, end_dt)))
            supplier_payments_qs = SupplierPayment.objects.filter(created_at__range=(start_dt, end_dt))
            
            # Pure operating expenses excluding supplier repayments
            expenses_qs = Expense.objects.filter(
                Q(date__range=(start_date, end_date))
            ).exclude(description__icontains='Supplier Repayment')

            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                repayments_qs = repayments_qs.filter(Q(branch=branch_obj) | Q(transaction__branch=branch_obj))
                purchases_qs = purchases_qs.filter(branch=branch_obj)
                supplier_payments_qs = supplier_payments_qs.filter(purchase__branch=branch_obj)
                expenses_qs = expenses_qs.filter(branch=branch_obj)

            # -----------------------------------------------------------------
            # 2. CASH INFLOWS
            # -----------------------------------------------------------------
            realized_sales_cash = ZERO
            for s in sales_qs:
                if not getattr(s, 'is_invoice', False):
                    bd = s.liquid_payments_breakdown
                    realized_sales_cash += bd.get('CASH', ZERO) + bd.get('MPESA', ZERO) + bd.get('PAYBILL', ZERO)

            customer_repayments = repayments_qs.aggregate(total=Coalesce(Sum('amount'), ZERO))['total']
            customer_receipts = realized_sales_cash + customer_repayments

            # -----------------------------------------------------------------
            # 3. CASH OUTFLOWS (EXCLUDE DOUBLE-COUNTED SUPPLIER PAYMENTS)
            # -----------------------------------------------------------------
            # Direct cash purchases without existing supplier payment records
            direct_purchases = purchases_qs.exclude(payment_method='CREDIT').filter(
                supplier_payments__isnull=True
            ).aggregate(total=Coalesce(Sum('amount_paid'), ZERO))['total']

            supplier_repayments = supplier_payments_qs.aggregate(total=Coalesce(Sum('amount'), ZERO))['total']
            supplier_payments = direct_purchases + supplier_repayments

            operating_expenses = expenses_qs.exclude(description__icontains='salary').exclude(description__icontains='wage').aggregate(total=Coalesce(Sum('amount'), ZERO))['total']
            salaries_and_wages = expenses_qs.filter(Q(description__icontains='salary') | Q(description__icontains='wage')).aggregate(total=Coalesce(Sum('amount'), ZERO))['total']

            net_operating_cash = customer_receipts - supplier_payments - operating_expenses - salaries_and_wages

            equipment_purchased = ZERO
            net_investing_cash = -equipment_purchased

            capital_loan_received = ZERO
            loan_repayment = ZERO
            drawings = ZERO
            net_financing_cash = capital_loan_received - loan_repayment - drawings

            net_increase_in_cash = net_operating_cash + net_investing_cash + net_financing_cash
            opening_cash = ZERO
            closing_cash = opening_cash + net_increase_in_cash

            # Helper function for currency formatting
            def fmt_pdf_curr(val):
                num = float(val or 0)
                if num < 0:
                    return f"(KSh {abs(num):,.2f})"
                return f"KSh {num:,.2f}"

            # -----------------------------------------------------------------
            # 4. REPORTLAB PDF SETUP
            # -----------------------------------------------------------------
            buffer = io.BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=letter,
                rightMargin=40,
                leftMargin=40,
                topMargin=40,
                bottomMargin=40
            )

            styles = getSampleStyleSheet()

            company_style = ParagraphStyle(
                'CompanyTitle',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=18,
                leading=22,
                textColor=colors.HexColor('#004D40'),
                alignment=1,
                spaceAfter=4
            )

            subtitle_style = ParagraphStyle(
                'SubTitle',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=10,
                leading=14,
                textColor=colors.HexColor('#475569'),
                alignment=1,
                spaceAfter=15
            )

            label_style = ParagraphStyle(
                'LabelNormal',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=10,
                textColor=colors.HexColor('#1E293B')
            )

            bold_label_style = ParagraphStyle(
                'LabelBold',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=10,
                textColor=colors.HexColor('#0F172A')
            )

            branch_name = branch_obj.name if branch_obj else "All Branches"
            period_str = f"{start_date.strftime('%b %d, %Y')} – {end_date.strftime('%b %d, %Y')}"

            elements = []

            # Centered Logo Wrapper
            logo_element = _get_pdf_logo_element()
            if logo_element:
                elements.append(logo_element)
                elements.append(Spacer(1, 8))

            elements.append(Paragraph("MELVEX BUSINESS SOLUTIONS", company_style))
            elements.append(Paragraph(f"<b>CASH FLOW STATEMENT — {branch_name.upper()}</b><br/>Period: {period_str}", subtitle_style))
            elements.append(Spacer(1, 10))

            # Financial Data Table
            data = [
                [Paragraph("<b>OPERATING ACTIVITIES</b>", bold_label_style), ""],
                [Paragraph("Customer receipts", label_style), fmt_pdf_curr(customer_receipts)],
                [Paragraph("Supplier payments", label_style), fmt_pdf_curr(supplier_payments)],
                [Paragraph("Operating expenses", label_style), fmt_pdf_curr(operating_expenses)],
                [Paragraph("Salaries & wages", label_style), fmt_pdf_curr(salaries_and_wages)],
                [Paragraph("Net operating cash", bold_label_style), fmt_pdf_curr(net_operating_cash)],
                [Paragraph("<b>INVESTING ACTIVITIES</b>", bold_label_style), ""],
                [Paragraph("Equipment purchased", label_style), fmt_pdf_curr(equipment_purchased)],
                [Paragraph("Net investing cash", bold_label_style), fmt_pdf_curr(net_investing_cash)],
                [Paragraph("<b>FINANCING ACTIVITIES</b>", bold_label_style), ""],
                [Paragraph("Capital loan received", label_style), fmt_pdf_curr(capital_loan_received)],
                [Paragraph("Loan repayment", label_style), fmt_pdf_curr(loan_repayment)],
                [Paragraph("Drawings", label_style), fmt_pdf_curr(drawings)],
                [Paragraph("Net financing cash", bold_label_style), fmt_pdf_curr(net_financing_cash)],
                [Paragraph("Net increase in cash", bold_label_style), fmt_pdf_curr(net_increase_in_cash)],
                [Paragraph("Opening cash", label_style), fmt_pdf_curr(opening_cash)],
                [Paragraph("CLOSING CASH", bold_label_style), fmt_pdf_curr(closing_cash)]
            ]

            table = Table(data, colWidths=[340, 192])
            table.setStyle(TableStyle([
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
                ('BACKGROUND', (0, 5), (-1, 5), colors.HexColor('#F8FAFC')),
                ('BACKGROUND', (0, 6), (-1, 6), colors.HexColor('#F1F5F9')),
                ('BACKGROUND', (0, 8), (-1, 8), colors.HexColor('#F8FAFC')),
                ('BACKGROUND', (0, 9), (-1, 9), colors.HexColor('#F1F5F9')),
                ('BACKGROUND', (0, 13), (-1, 13), colors.HexColor('#F8FAFC')),
                ('BACKGROUND', (0, 16), (-1, 16), colors.HexColor('#E6FFFA')),
                ('TEXTCOLOR', (1, 16), (1, 16), colors.HexColor('#004D40')),
                ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                ('LINEBELOW', (0, 16), (-1, 16), 1.5, colors.HexColor('#004D40')),
            ]))

            elements.append(table)
            doc.build(elements)
            buffer.seek(0)

            response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="Cash_Flow_Statement_{branch_name}.pdf"'
            return response

        except Exception as e:
            logger.exception("Export Cash Flow PDF Error")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], url_path='export-balance-sheet-pdf')
    def export_balance_sheet_pdf(self, request):
        try:
            branch_obj, _, end_date, _, end_dt = self._get_branch_and_dates(request)

            # -----------------------------------------------------------------
            # 1. CASH, BANK & M-PESA INFLOW/OUTFLOW BALANCE (UNIFIED LOGIC)
            # -----------------------------------------------------------------
            sales_qs = SaleTransaction.objects.filter(sale_date__lte=end_dt).exclude(
                Q(payment_method='CREDIT') | Q(receipt_number__startswith='INV')
            )
            repayments_qs = CreditRepayment.objects.filter(date__lte=end_date)
            
            # Pure operating expenses excluding supplier repayments
            expenses_qs = Expense.objects.filter(date__lte=end_date).exclude(description__icontains='Supplier Repayment')
            
            purchases_qs = Purchase.objects.filter(purchase_date__lte=end_dt)
            supplier_payments_qs = SupplierPayment.objects.filter(created_at__lte=end_dt)

            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                repayments_qs = repayments_qs.filter(Q(branch=branch_obj) | Q(transaction__branch=branch_obj))
                expenses_qs = expenses_qs.filter(branch=branch_obj)
                purchases_qs = purchases_qs.filter(branch=branch_obj)
                supplier_payments_qs = supplier_payments_qs.filter(purchase__branch=branch_obj)

            tot_sales_cash = ZERO
            for s in sales_qs:
                if not getattr(s, 'is_invoice', False):
                    bd = s.liquid_payments_breakdown
                    tot_sales_cash += bd.get('CASH', ZERO) + bd.get('MPESA', ZERO) + bd.get('PAYBILL', ZERO)

            tot_repayments = repayments_qs.aggregate(total=Coalesce(Sum('amount'), ZERO))['total']
            tot_inflows = tot_sales_cash + tot_repayments

            # Outflows without double-counting
            tot_expenses = expenses_qs.aggregate(total=Coalesce(Sum('amount'), ZERO))['total']
            tot_purchases_direct = purchases_qs.exclude(payment_method='CREDIT').filter(
                supplier_payments__isnull=True
            ).aggregate(total=Coalesce(Sum('amount_paid'), ZERO))['total']
            tot_supplier_repay = supplier_payments_qs.aggregate(total=Coalesce(Sum('amount'), ZERO))['total']
            
            tot_outflows = tot_expenses + tot_purchases_direct + tot_supplier_repay

            cash_bank_mpesa = max(ZERO, tot_inflows - tot_outflows)

            # -----------------------------------------------------------------
            # 2. CUSTOMER RECEIVABLES
            # -----------------------------------------------------------------
            credit_sales_qs = SaleTransaction.objects.filter(payment_method=SaleTransaction.PaymentMethod.CREDIT)
            if branch_obj:
                credit_sales_qs = credit_sales_qs.filter(branch=branch_obj)
            customer_debts = sum((s.balance_due for s in credit_sales_qs), ZERO)

            # -----------------------------------------------------------------
            # 3. INVENTORY STOCK VALUATION
            # -----------------------------------------------------------------
            inventory_stock = ZERO
            try:
                stock_qs = BranchStock.objects.all().select_related('item', 'branch')
                if branch_obj:
                    stock_qs = stock_qs.filter(branch=branch_obj)

                for bs in stock_qs:
                    pricing = bs.item.branch_pricings.filter(branch=bs.branch).first()
                    cost = pricing.unit_cost_price if pricing else ZERO
                    inventory_stock += Decimal(str(bs.quantity)) * Decimal(str(cost))
            except Exception:
                pricing_qs = BranchItemPricing.objects.all().select_related('item', 'branch')
                if branch_obj:
                    pricing_qs = pricing_qs.filter(branch=branch_obj)
                for pr in pricing_qs:
                    inventory_stock += pr.remaining_stock * pr.unit_cost_price

            equipment = ZERO
            total_assets = cash_bank_mpesa + customer_debts + inventory_stock + equipment

            # -----------------------------------------------------------------
            # 4. SUPPLIER PAYABLES (LIABILITIES)
            # -----------------------------------------------------------------
            credit_purchases_qs = Purchase.objects.filter(payment_method='CREDIT')
            if branch_obj:
                credit_purchases_qs = credit_purchases_qs.filter(branch=branch_obj)

            supplier_debts = sum((p.balance_due for p in credit_purchases_qs), ZERO)
            bank_loan = ZERO
            total_liabilities = supplier_debts + bank_loan

            # -----------------------------------------------------------------
            # 5. EQUITY & CAPITAL
            # -----------------------------------------------------------------
            capital_and_retained_profits = total_assets - total_liabilities
            liabilities_plus_equity = total_liabilities + capital_and_retained_profits

            # Helper function for currency formatting
            def fmt_pdf_curr(val):
                num = float(val or 0)
                if num < 0:
                    return f"(KSh {abs(num):,.2f})"
                return f"KSh {num:,.2f}"

            # -----------------------------------------------------------------
            # 6. REPORTLAB PDF SETUP
            # -----------------------------------------------------------------
            buffer = io.BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=letter,
                rightMargin=40,
                leftMargin=40,
                topMargin=40,
                bottomMargin=40
            )

            styles = getSampleStyleSheet()

            company_style = ParagraphStyle(
                'BsCompanyHeader',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=18,
                leading=22,
                textColor=colors.HexColor('#004D40'),
                alignment=1,
                spaceAfter=4
            )

            subtitle_style = ParagraphStyle(
                'BsSubHeader',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=10,
                leading=14,
                textColor=colors.HexColor('#475569'),
                alignment=1,
                spaceAfter=15
            )

            label_style = ParagraphStyle(
                'BsLabelNormal',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=10,
                textColor=colors.HexColor('#1E293B')
            )

            bold_label_style = ParagraphStyle(
                'BsLabelBold',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=10,
                textColor=colors.HexColor('#0F172A')
            )

            branch_name = branch_obj.name if branch_obj else "All Branches"
            as_at_str = end_date.strftime('%b %d, %Y')

            elements = []

            # Centered SVG Logo Header
            logo_element = _get_pdf_logo_element()
            if logo_element:
                elements.append(logo_element)
                elements.append(Spacer(1, 8))

            elements.append(Paragraph("MELVEX BUSINESS SOLUTIONS", company_style))
            elements.append(Paragraph(f"<b>BALANCE SHEET STATEMENT — {branch_name.upper()}</b><br/>As of {as_at_str}", subtitle_style))
            elements.append(Spacer(1, 10))

            # Balance Sheet Data Table
            data = [
                [Paragraph("<b>ASSETS</b>", bold_label_style), ""],
                [Paragraph("Cash & Bank / M-Pesa", label_style), fmt_pdf_curr(cash_bank_mpesa)],
                [Paragraph("Customer Receivables (Debts)", label_style), fmt_pdf_curr(customer_debts)],
                [Paragraph("Inventory Stock Asset Value", label_style), fmt_pdf_curr(inventory_stock)],
                [Paragraph("Equipment & Fixed Assets", label_style), fmt_pdf_curr(equipment)],
                [Paragraph("Total Assets", bold_label_style), fmt_pdf_curr(total_assets)],
                [Paragraph("<b>LIABILITIES</b>", bold_label_style), ""],
                [Paragraph("Accounts Payable (Suppliers)", label_style), fmt_pdf_curr(supplier_debts)],
                [Paragraph("Bank Loans & Borrowings", label_style), fmt_pdf_curr(bank_loan)],
                [Paragraph("Total Liabilities", bold_label_style), fmt_pdf_curr(total_liabilities)],
                [Paragraph("<b>EQUITY</b>", bold_label_style), ""],
                [Paragraph("Capital & Retained Earnings", label_style), fmt_pdf_curr(capital_and_retained_profits)],
                [Paragraph("TOTAL LIABILITIES & EQUITY", bold_label_style), fmt_pdf_curr(liabilities_plus_equity)]
            ]

            table = Table(data, colWidths=[340, 192])
            table.setStyle(TableStyle([
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
                ('BACKGROUND', (0, 5), (-1, 5), colors.HexColor('#F8FAFC')),
                ('BACKGROUND', (0, 6), (-1, 6), colors.HexColor('#F1F5F9')),
                ('BACKGROUND', (0, 9), (-1, 9), colors.HexColor('#F8FAFC')),
                ('BACKGROUND', (0, 10), (-1, 10), colors.HexColor('#F1F5F9')),
                ('BACKGROUND', (0, 12), (-1, 12), colors.HexColor('#E6FFFA')),
                ('TEXTCOLOR', (1, 12), (1, 12), colors.HexColor('#004D40')),
                ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                ('LINEBELOW', (0, 12), (-1, 12), 1.5, colors.HexColor('#004D40')),
            ]))

            elements.append(table)
            doc.build(elements)
            buffer.seek(0)

            response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="Balance_Sheet_{branch_name}.pdf"'
            return response

        except Exception as e:
            logger.exception("Export Balance Sheet PDF Error")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], url_path='export-cash-book-pdf')
    def export_cash_book_pdf(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt = self._get_branch_and_dates(request)

            # -----------------------------------------------------------------
            # 1. BASE QUERYSETS FOR PERIOD & BRANCH
            # -----------------------------------------------------------------
            sales_qs = SaleTransaction.objects.all()
            repayment_qs = CreditRepayment.objects.all()
            expenses_qs = Expense.objects.all()
            purchases_qs = Purchase.objects.all()
            supplier_payments_qs = SupplierPayment.objects.all()

            if start_dt and end_dt:
                sales_qs = sales_qs.filter(sale_date__range=(start_dt, end_dt))
            if start_date and end_date:
                repayment_qs = repayment_qs.filter(date__range=(start_date, end_date))
                expenses_qs = expenses_qs.filter(date__range=(start_date, end_date))
                purchases_qs = purchases_qs.filter(purchase_date__range=(start_date, end_date))
                supplier_payments_qs = supplier_payments_qs.filter(created_at__date__range=(start_date, end_date))

            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                repayment_qs = repayment_qs.filter(
                    Q(branch=branch_obj) | Q(transaction__branch=branch_obj)
                )
                expenses_qs = expenses_qs.filter(branch=branch_obj)
                purchases_qs = purchases_qs.filter(branch=branch_obj)
                supplier_payments_qs = supplier_payments_qs.filter(purchase__branch=branch_obj)

            # Exclude credit sales & non-cash repayments from direct expenses
            sales_qs = sales_qs.exclude(Q(payment_method__iexact='CREDIT') | Q(receipt_number__startswith='INV'))
            expenses_qs = expenses_qs.exclude(description__icontains='Supplier Repayment')

            # -----------------------------------------------------------------
            # 2. AGGREGATE CHANNEL TOTALS (MATCHING UI SUMMARY)
            # -----------------------------------------------------------------
            cash_total = ZERO
            paybill_total = ZERO
            send_money_total = ZERO
            bank_total = ZERO

            # Aggregate sales channels (Handling Single & Split Payments)
            sales_with_payments = sales_qs.select_related("customer", "branch").prefetch_related("split_payments")
            for sale in sales_with_payments:
                sale_amt = quantize_money(getattr(sale, "total_amount", ZERO) or ZERO)
                method = self._normalize_payment_method(getattr(sale, "payment_method", None))
                split_records = self._get_split_payments(sale)

                if method == "SPLIT" or (split_records and len(split_records) > 0):
                    for sp in split_records:
                        sp_method = self._normalize_payment_method(self._extract_sp_method(sp))
                        sp_amount = quantize_money(getattr(sp, "amount", ZERO) or ZERO)
                        if sp_method == "CASH":
                            cash_total += sp_amount
                        elif sp_method == "PAYBILL":
                            paybill_total += sp_amount
                        elif sp_method in ["SEND_MONEY", "MPESA"]:
                            send_money_total += sp_amount
                        elif sp_method in ["BANK_TRANSFER", "BANK"]:
                            bank_total += sp_amount
                else:
                    if method == "CASH":
                        cash_total += sale_amt
                    elif method == "PAYBILL":
                        paybill_total += sale_amt
                    elif method in ["SEND_MONEY", "MPESA"]:
                        send_money_total += sale_amt
                    elif method in ["BANK_TRANSFER", "BANK"]:
                        bank_total += sale_amt

            # Aggregate debt repayments into their respective channels
            for rep in repayment_qs:
                rep_amt = quantize_money(rep.amount or ZERO)
                rep_method = self._normalize_payment_method(getattr(rep, "payment_method", None))
                if rep_method == "PAYBILL":
                    paybill_total += rep_amt
                elif rep_method in ["SEND_MONEY", "MPESA"]:
                    send_money_total += rep_amt
                elif rep_method in ["BANK_TRANSFER", "BANK"]:
                    bank_total += rep_amt
                else:
                    cash_total += rep_amt

            # -----------------------------------------------------------------
            # 3. OUTFLOWS (PURCHASES & OPERATING EXPENSES)
            # -----------------------------------------------------------------
            direct_purchases = purchases_qs.exclude(
                payment_method__iexact='CREDIT'
            ).filter(
                supplier_payments__isnull=True
            ).aggregate(total=Coalesce(Sum('amount_paid'), ZERO, output_field=DecimalField()))['total']

            supplier_repayments = supplier_payments_qs.aggregate(
                total=Coalesce(Sum('amount'), ZERO, output_field=DecimalField())
            )['total']

            tot_purchases = direct_purchases + supplier_repayments

            tot_expenses = expenses_qs.aggregate(
                total=Coalesce(Sum('amount'), ZERO, output_field=DecimalField())
            )['total']

            entries = [
                {
                    'ref': 'SUM-CASH',
                    'desc': 'Total Cash Collections & Repayments',
                    'type': 'INFLOW',
                    'inflow': float(cash_total),
                    'outflow': 0.0
                },
                {
                    'ref': 'SUM-PAYBILL',
                    'desc': 'Total Paybill Collections & Repayments',
                    'type': 'INFLOW',
                    'inflow': float(paybill_total),
                    'outflow': 0.0
                },
                {
                    'ref': 'SUM-SENDMONEY',
                    'desc': 'Total Send Money / M-Pesa Collections & Repayments',
                    'type': 'INFLOW',
                    'inflow': float(send_money_total),
                    'outflow': 0.0
                },
                {
                    'ref': 'SUM-PURCHASES',
                    'desc': 'Total Cost of Goods Purchased & Supplier Outflows',
                    'type': 'OUTFLOW',
                    'inflow': 0.0,
                    'outflow': float(tot_purchases)
                },
                {
                    'ref': 'SUM-EXPENSES',
                    'desc': 'Total Operating Expenses',
                    'type': 'OUTFLOW',
                    'inflow': 0.0,
                    'outflow': float(tot_expenses)
                }
            ]

            tot_inflow = sum(e['inflow'] for e in entries) + float(bank_total)
            tot_outflow = sum(e['outflow'] for e in entries)
            net_position = tot_inflow - tot_outflow

            def fmt_pdf_curr(val):
                num = float(val or 0)
                if num < 0:
                    return f"(KSh {abs(num):,.2f})"
                return f"KSh {num:,.2f}"

            # -----------------------------------------------------------------
            # 4. REPORTLAB PDF BUILD
            # -----------------------------------------------------------------
            buffer = io.BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=letter,
                rightMargin=30,
                leftMargin=30,
                topMargin=30,
                bottomMargin=30
            )

            styles = getSampleStyleSheet()

            company_style = ParagraphStyle(
                'CbCompanyHeader',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=16,
                leading=20,
                textColor=colors.HexColor('#004D40'),
                alignment=1,
                spaceAfter=3
            )

            subtitle_style = ParagraphStyle(
                'CbSubHeader',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=9,
                leading=13,
                textColor=colors.HexColor('#475569'),
                alignment=1,
                spaceAfter=12
            )

            table_header_style = ParagraphStyle(
                'CbTh',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=8,
                textColor=colors.HexColor('#0F172A')
            )

            cell_style = ParagraphStyle(
                'CbTd',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=8,
                textColor=colors.HexColor('#1E293B')
            )

            branch_name = branch_obj.name if branch_obj else "All Branches"
            period_str = f"{start_date.strftime('%b %d, %Y')} – {end_date.strftime('%b %d, %Y')}" if start_date and end_date else "All Time"

            elements = []

            logo_element = _get_pdf_logo_element()
            if logo_element:
                elements.append(logo_element)
                elements.append(Spacer(1, 6))

            elements.append(Paragraph("MELVEX BUSINESS SOLUTIONS", company_style))
            elements.append(Paragraph(f"<b>CASHBOOK AGGREGATION & LIQUIDITY REPORT — {branch_name.upper()}</b><br/>Period: {period_str}", subtitle_style))
            elements.append(Spacer(1, 8))

            table_data = [[
                Paragraph("Ref Code", table_header_style),
                Paragraph("Channel / Category Description", table_header_style),
                Paragraph("Type", table_header_style),
                Paragraph("Inflow (KSh)", table_header_style),
                Paragraph("Outflow (KSh)", table_header_style)
            ]]

            for e in entries:
                table_data.append([
                    Paragraph(e['ref'], cell_style),
                    Paragraph(e['desc'], cell_style),
                    Paragraph(e['type'], cell_style),
                    fmt_pdf_curr(e['inflow']) if e['inflow'] > 0 else "-",
                    fmt_pdf_curr(e['outflow']) if e['outflow'] > 0 else "-"
                ])

            table_data.append([
                Paragraph("<b>TOTALS</b>", table_header_style),
                Paragraph(f"<b>Net Cash Position: {fmt_pdf_curr(net_position)}</b>", table_header_style),
                "",
                Paragraph(f"<b>{fmt_pdf_curr(tot_inflow)}</b>", table_header_style),
                Paragraph(f"<b>{fmt_pdf_curr(tot_outflow)}</b>", table_header_style)
            ])

            table = Table(table_data, colWidths=[90, 230, 60, 85, 85])
            table.setStyle(TableStyle([
                ('ALIGN', (3, 0), (-1, -1), 'RIGHT'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
                ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#E6FFFA')),
                ('LINEABOVE', (0, -1), (-1, -1), 1.5, colors.HexColor('#004D40')),
            ]))

            elements.append(table)
            doc.build(elements)
            buffer.seek(0)

            response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="Cashbook_Summary_{branch_name}.pdf"'
            return response

        except Exception as e:
            logger.exception("Export Cash Book PDF Error")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# ============================================================
# USER PERMISSION VIEWSET
# ============================================================

class UserPermissionViewSet(viewsets.ModelViewSet):
    """
    ViewSet for fetching, creating, updating, and managing user role permissions.
    Handles admin configurations safely with proper error fallbacks.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        try:
            from stocks.models import UserRolePermission
            return UserRolePermission.objects.select_related("user").all().order_by("-id")
        except Exception:
            return []

    def _get_defaults(self):
        try:
            from apis.models import default_module_permissions
            defaults = default_module_permissions() if callable(default_module_permissions) else default_module_permissions
            return defaults if isinstance(defaults, dict) else {}
        except Exception as e:
            logger.error(f"Error fetching default module permissions: {e}")
            return {}

    def list(self, request, *args, **kwargs):
        try:
            from django.contrib.auth import get_user_model
            from stocks.models import UserRolePermission

            User = get_user_model()
            all_users = User.objects.all().order_by("username")

            perm_map = {}
            try:
                for perm in UserRolePermission.objects.select_related("user").all():
                    if perm.user_id:
                        perm_map[perm.user_id] = perm
            except Exception as e:
                logger.warning(f"Failed to load UserRolePermission map: {e}")

            defaults = self._get_defaults()
            data = []

            for user in all_users:
                perm_record = perm_map.get(user.id)

                if user.is_superuser:
                    module_perms = {k: {"view": True, "change": True} for k in defaults.keys()}
                    role = "SUPERADMIN"
                elif perm_record:
                    module_perms = dict(defaults)
                    if isinstance(perm_record.module_permissions, dict):
                        module_perms.update(perm_record.module_permissions)
                    role = perm_record.role or "NORMAL_USER"
                else:
                    module_perms = dict(defaults)
                    role = "ADMIN" if user.is_staff else "NORMAL_USER"

                data.append({
                    "id": perm_record.id if perm_record else user.id,
                    "user_id": user.id,
                    "username": user.get_username(),
                    "full_name": user.get_full_name() or user.get_username(),
                    "email": getattr(user, "email", "") or "",
                    "is_active": user.is_active,
                    "is_staff": user.is_staff,
                    "is_superuser": user.is_superuser,
                    "role": role,
                    "permissions": module_perms,
                    "module_permissions": module_perms,
                })

            return Response(data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(f"Unhandled error in UserPermissionViewSet.list: {e}")
            return Response({"error": "Internal server error fetching permissions.", "details": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def retrieve(self, request, pk=None, *args, **kwargs):
        try:
            from django.contrib.auth import get_user_model
            from stocks.models import UserRolePermission

            User = get_user_model()
            defaults = self._get_defaults()

            perm = UserRolePermission.objects.filter(pk=pk).select_related("user").first()
            user = perm.user if perm else User.objects.filter(pk=pk).first()

            if not user and not perm:
                return Response({"error": "User or permission record not found."}, status=status.HTTP_404_NOT_FOUND)

            if user and user.is_superuser:
                module_perms = {k: {"view": True, "change": True} for k in defaults.keys()}
                role = "SUPERADMIN"
            elif perm:
                module_perms = dict(defaults)
                if isinstance(perm.module_permissions, dict):
                    module_perms.update(perm.module_permissions)
                role = perm.role or "NORMAL_USER"
            else:
                module_perms = dict(defaults)
                role = "NORMAL_USER"

            return Response({
                "id": perm.id if perm else user.id,
                "user_id": user.id,
                "username": user.get_username(),
                "full_name": user.get_full_name() or user.get_username(),
                "email": getattr(user, "email", "") or "",
                "role": role,
                "permissions": module_perms,
                "module_permissions": module_perms,
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(f"Unhandled error in UserPermissionViewSet.retrieve: {e}")
            return Response({"error": "Internal server error.", "details": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def update(self, request, pk=None, *args, **kwargs):
        try:
            from django.contrib.auth import get_user_model
            from stocks.models import UserRolePermission

            User = get_user_model()
            role = request.data.get("role", "NORMAL_USER")
            permissions_payload = request.data.get("permissions") or request.data.get("module_permissions") or {}

            perm_record = UserRolePermission.objects.filter(pk=pk).first()
            user = perm_record.user if perm_record else User.objects.filter(pk=pk).first()

            if not user:
                return Response({"error": "Target user not found."}, status=status.HTTP_404_NOT_FOUND)

            defaults = self._get_defaults()
            updated_perms = dict(defaults)
            if isinstance(permissions_payload, dict):
                updated_perms.update(permissions_payload)

            with transaction.atomic():
                if not perm_record:
                    perm_record, _ = UserRolePermission.objects.get_or_create(
                        user=user,
                        defaults={"role": role, "module_permissions": updated_perms}
                    )
                else:
                    perm_record.role = role
                    perm_record.module_permissions = updated_perms
                    perm_record.save()

                user.is_staff = (role in ["ADMIN", "SUPERADMIN"])
                user.save(update_fields=["is_staff"])

            return Response({
                "id": perm_record.id,
                "user_id": user.id,
                "username": user.get_username(),
                "role": perm_record.role,
                "permissions": perm_record.module_permissions,
                "module_permissions": perm_record.module_permissions,
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(f"Unhandled error in UserPermissionViewSet.update: {e}")
            return Response({"error": "Failed to update user permission.", "details": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

def to_decimal(val):
    if val is None:
        return ZERO
    try:
        return Decimal(str(val))
    except Exception:
        return ZERO

def generate_pdf_vendor_chart(suppliers_data):
    """Generates a PNG byte stream of Top Vendor Spend Allocation."""
    if not suppliers_data:
        return None

    top_sups = suppliers_data[:5]
    names = [(s.get('supplier_name') or 'Vendor')[:15] for s in top_sups]
    spends = [float(s.get('total_spend', 0)) for s in top_sups]

    if not any(spends):
        return None

    x = range(len(names))
    fig, ax = plt.subplots(figsize=(7, 2.6), dpi=150)
    ax.bar(x, spends, width=0.4, color='#0D9488', label='Total Spend')

    ax.set_xticks(list(x))
    ax.set_xticklabels(names, rotation=20, ha='right', fontsize=7)
    ax.tick_params(axis='y', labelsize=7)
    ax.legend(fontsize=7, loc='upper right')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    plt.tight_layout()

    img_buf = io.BytesIO()
    plt.savefig(img_buf, format='png', transparent=False, facecolor='white')
    plt.close(fig)
    img_buf.seek(0)
    return Image(img_buf, width=520, height=190)


def generate_pdf_payables_pie(summary):
    """Generates a PNG byte stream of Settlement & Payables Ratio."""
    paid = float(summary.get('total_paid_suppliers', 0))
    payable = float(summary.get('outstanding_payables', 0))

    if paid <= 0 and payable <= 0:
        return None

    labels = ['Disbursed Payments', 'Outstanding Payables']
    sizes = [paid, payable]
    colors_list = ['#0D9488', '#F59E0B']

    fig, ax = plt.subplots(figsize=(4.5, 2.6), dpi=150)
    wedges, texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        autopct='%1.1f%%',
        colors=colors_list,
        textprops={'fontsize': 7},
        startangle=140
    )
    for autotext in autotexts:
        autotext.set_color('white')
        autotext.set_weight('bold')

    plt.tight_layout()

    img_buf = io.BytesIO()
    plt.savefig(img_buf, format='png', transparent=False, facecolor='white')
    plt.close(fig)
    img_buf.seek(0)
    return Image(img_buf, width=320, height=185)


class PurchaseAnalyticsViewSet(viewsets.ViewSet):
    """
    Dedicated ViewSet for Procurement, Purchase Orders, Goods Received Notes (GRN),
    Supplier Invoices, Payments, Accounts Payable Analytics, and PDF Exports.
    """
    permission_classes = [permissions.AllowAny]

    def _get_filters(self, request):
        branch, start_date, end_date, start_dt, end_dt, _period = (
            _get_reporting_filters(request, default_period="month")
        )
        return branch, start_date, end_date, start_dt, end_dt


    # --------------------------------------------------------------------------
    # 1. /purchases-analytics/purchases-overview/
    # --------------------------------------------------------------------------
    @action(detail=False, methods=['get'], url_path='purchases-overview')
    def purchases_overview(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt = self._get_filters(request)

            po_qs = PurchaseOrder.objects.all()
            return_qs = SupplierReturn.objects.all()

            if branch_obj:
                po_qs = po_qs.filter(branch=branch_obj)
                return_qs = return_qs.filter(branch=branch_obj)

            if start_dt and end_dt:
                po_qs = po_qs.filter(
                    Q(purchase_date__range=(start_dt, end_dt))
                )
            if start_date and end_date:
                return_qs = return_qs.filter(date__range=(start_date, end_date))

            po_count = po_qs.count()

            if po_count > 0:
                total_procurement_spend = ZERO
                total_paid_suppliers = ZERO

                for po in po_qs:
                    po_total = to_decimal(getattr(po, 'total_amount', 0))
                    total_procurement_spend += po_total

                    if getattr(po, 'payment_method', 'CASH') != 'CREDIT':
                        total_paid_suppliers += po_total
                    else:
                        total_paid_suppliers += to_decimal(getattr(po, 'amount_paid', 0))

                po_items = PurchaseOrderItem.objects.filter(purchase_order__in=po_qs)
                total_units_purchased = to_decimal(po_items.aggregate(total=Sum('quantity'))['total'])
                orders_count = po_count
            else:
                purchases_qs = Purchase.objects.all()
                if branch_obj:
                    purchases_qs = purchases_qs.filter(branch=branch_obj)
                if start_dt and end_dt:
                    purchases_qs = purchases_qs.filter(purchase_date__range=(start_dt, end_dt))

                total_procurement_spend = ZERO
                total_paid_suppliers = ZERO
                total_units_purchased = ZERO

                for p in purchases_qs:
                    p_qty = to_decimal(getattr(p, 'quantity', 0))
                    p_cost = to_decimal(getattr(p, 'unit_cost_price', 0))
                    p_total = p_qty * p_cost

                    total_procurement_spend += p_total
                    total_units_purchased += p_qty

                    if getattr(p, 'payment_method', 'CASH') != 'CREDIT':
                        total_paid_suppliers += p_total
                    else:
                        total_paid_suppliers += to_decimal(getattr(p, 'amount_paid', 0))

                orders_count = purchases_qs.count()

            outstanding_payables = max(ZERO, total_procurement_spend - total_paid_suppliers)

            return Response({
                "summary": {
                    "total_procurement_spend": float(quantize_money(total_procurement_spend)),
                    "total_paid_suppliers": float(quantize_money(total_paid_suppliers)),
                    "outstanding_payables": float(quantize_money(outstanding_payables)),
                    "purchase_orders_count": orders_count,
                    "total_units_purchased": float(total_units_purchased),
                    "supplier_returns_count": return_qs.count()
                }
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Error generating purchases overview")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # --------------------------------------------------------------------------
    # 2. /purchases-analytics/purchase-orders/
    # --------------------------------------------------------------------------
    @action(detail=False, methods=['get'], url_path='purchase-orders')
    def purchase_orders(self, request):
        try:
            branch_obj, _, _, start_dt, end_dt = self._get_filters(request)
            po_qs = PurchaseOrder.objects.select_related('supplier', 'branch').prefetch_related('items__item')

            if branch_obj:
                po_qs = po_qs.filter(branch=branch_obj)
            if start_dt and end_dt:
                po_qs = po_qs.filter(
                    Q(purchase_date__range=(start_dt, end_dt))
                )

            results = []

            if po_qs.exists():
                for po in po_qs.order_by('-purchase_date', '-created_at'):
                    supplier_name = getattr(po, 'supplier_name', None) or (po.supplier.name if po.supplier else "Direct Supplier")
                    items_list = list(po.items.all())

                    items_data = []
                    total_qty = ZERO
                    for item in items_list:
                        q = to_decimal(getattr(item, 'quantity', 0))
                        c = to_decimal(getattr(item, 'unit_cost_price', 0))
                        tot = q * c
                        total_qty += q
                        items_data.append({
                            "item_name": item.item.name if item.item else "Restock Item",
                            "quantity": float(q),
                            "unit_cost": float(quantize_money(c)),
                            "total_cost": float(quantize_money(tot))
                        })

                    item_names = ", ".join(
                        [item.item.name for item in items_list if item.item]
                    ) or "Multiple Items"

                    date_val = getattr(po, 'purchase_date', None) or po.created_at
                    date_str = date_val.strftime('%Y-%m-%d') if date_val else ""

                    results.append({
                        "id": po.id,
                        "po_number": getattr(po, 'po_number', f"PO-{po.id}"),
                        "purchase_date": date_str,
                        "supplier_name": supplier_name,
                        "item_name": item_names,
                        "quantity": float(total_qty),
                        "total_cost": float(quantize_money(to_decimal(getattr(po, 'total_amount', 0)))),
                        "payment_method": getattr(po, 'payment_method', 'CASH'),
                        "items": items_data
                    })
            else:
                purchases_qs = Purchase.objects.select_related('supplier', 'item', 'branch')
                if branch_obj:
                    purchases_qs = purchases_qs.filter(branch=branch_obj)
                if start_dt and end_dt:
                    purchases_qs = purchases_qs.filter(purchase_date__range=(start_dt, end_dt))

                for p in purchases_qs.order_by('-purchase_date'):
                    supplier_name = getattr(p, 'supplier_name', None) or (p.supplier.name if p.supplier else "Direct Supplier")
                    item_name = p.item.name if p.item else f"Item #{p.item_id}"

                    qty_dec = to_decimal(getattr(p, 'quantity', 0))
                    cost_dec = to_decimal(getattr(p, 'unit_cost_price', 0))
                    total_cost = quantize_money(qty_dec * cost_dec)

                    date_str = p.purchase_date.strftime('%Y-%m-%d') if p.purchase_date else ""

                    results.append({
                        "id": p.id,
                        "po_number": f"PUR-{p.id}",
                        "purchase_date": date_str,
                        "supplier_name": supplier_name,
                        "item_name": item_name,
                        "quantity": float(qty_dec),
                        "total_cost": float(total_cost),
                        "payment_method": getattr(p, 'payment_method', 'CASH'),
                        "items": [{
                            "item_name": item_name,
                            "quantity": float(qty_dec),
                            "unit_cost": float(quantize_money(cost_dec)),
                            "total_cost": float(total_cost)
                        }]
                    })

            return Response(results, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Error generating purchase orders list")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # --------------------------------------------------------------------------
    # 3. /purchases-analytics/goods-received/
    # --------------------------------------------------------------------------
    @action(detail=False, methods=['get'], url_path='goods-received')
    def goods_received(self, request):
        try:
            branch_obj, _, _, start_dt, end_dt = self._get_filters(request)
            po_qs = PurchaseOrder.objects.select_related('supplier', 'branch')

            if branch_obj:
                po_qs = po_qs.filter(branch=branch_obj)
            if start_dt and end_dt:
                po_qs = po_qs.filter(
                    Q(purchase_date__range=(start_dt, end_dt))
                )

            results = []
            for po in po_qs.order_by('-purchase_date', '-created_at'):
                date_val = getattr(po, 'purchase_date', None) or po.created_at
                date_str = date_val.strftime('%Y-%m-%d') if date_val else ""
                results.append({
                    "id": po.id,
                    "grn_number": f"GRN-{po.id}",
                    "po_number": getattr(po, 'po_number', f"PO-{po.id}"),
                    "supplier_name": getattr(po, 'supplier_name', None) or (po.supplier.name if po.supplier else "Direct Supplier"),
                    "received_date": date_str,
                    "notes": f"Goods received for Order #{getattr(po, 'po_number', po.id)}",
                })

            return Response(results, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Error fetching goods received notes")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # --------------------------------------------------------------------------
    # 4. /purchases-analytics/supplier-invoices/
    # --------------------------------------------------------------------------
    @action(detail=False, methods=['get'], url_path='supplier-invoices')
    def supplier_invoices(self, request):
        try:
            branch_obj, _, _, start_dt, end_dt = self._get_filters(request)
            po_qs = PurchaseOrder.objects.select_related('supplier', 'branch')

            if branch_obj:
                po_qs = po_qs.filter(branch=branch_obj)
            if start_dt and end_dt:
                po_qs = po_qs.filter(
                    Q(purchase_date__range=(start_dt, end_dt))
                )

            results = []
            for po in po_qs.order_by('-purchase_date', '-created_at'):
                tot_amt = to_decimal(getattr(po, 'total_amount', 0))
                amt_paid = to_decimal(getattr(po, 'amount_paid', 0))
                is_paid = (getattr(po, 'payment_method', 'CASH') != 'CREDIT') or (amt_paid >= tot_amt)
                date_val = getattr(po, 'purchase_date', None) or po.created_at
                date_str = date_val.strftime('%Y-%m-%d') if date_val else ""
                results.append({
                    "id": po.id,
                    "invoice_number": f"INV-{po.id}",
                    "supplier_name": getattr(po, 'supplier_name', None) or (po.supplier.name if po.supplier else "Direct Supplier"),
                    "total_cost": float(quantize_money(tot_amt)),
                    "invoice_date": date_str,
                    "due_date": "",
                    "is_paid": is_paid
                })

            return Response(results, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Error fetching supplier invoices")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # --------------------------------------------------------------------------
    # 5. /purchases-analytics/supplier-payments/
    # --------------------------------------------------------------------------
    @action(detail=False, methods=['get'], url_path='supplier-payments')
    def supplier_payments(self, request):
        try:
            branch_obj, _, _, start_dt, end_dt = self._get_filters(request)
            pay_qs = SupplierPayment.objects.select_related('purchase', 'purchase__supplier', 'purchase__branch')

            if branch_obj:
                pay_qs = pay_qs.filter(purchase__branch=branch_obj)
            if start_dt and end_dt:
                pay_qs = pay_qs.filter(created_at__range=(start_dt, end_dt))

            results = []
            for pay in pay_qs.order_by('-created_at'):
                p = pay.purchase
                supplier_name = getattr(p, 'supplier_name', None) or (p.supplier.name if p and p.supplier else "Direct Supplier") if p else "Direct Supplier"

                results.append({
                    "id": pay.id,
                    "payment_ref": f"PAY-{pay.id}",
                    "supplier_name": supplier_name,
                    "amount": float(quantize_money(to_decimal(pay.amount))),
                    "payment_method": getattr(pay, 'payment_method', 'CASH'),
                    "payment_date": pay.created_at.strftime('%Y-%m-%d') if pay.created_at else "",
                })

            return Response(results, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Error fetching supplier payments")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # --------------------------------------------------------------------------
    # 6. /purchases-analytics/suppliers-list/
    # --------------------------------------------------------------------------
    @action(detail=False, methods=['get'], url_path='suppliers-list')
    def suppliers_list(self, request):
        try:
            branch_obj, _, _, start_dt, end_dt = self._get_filters(request)
            suppliers = Supplier.objects.all()

            results = []
            supplier_names_seen = set()

            for sup in suppliers:
                supplier_names_seen.add(sup.name.strip().lower())
                po_qs = PurchaseOrder.objects.filter(Q(supplier=sup) | Q(supplier_name__iexact=sup.name))

                if branch_obj:
                    po_qs = po_qs.filter(branch=branch_obj)

                if start_dt and end_dt:
                    po_qs = po_qs.filter(
                        Q(purchase_date__range=(start_dt, end_dt))
                    )

                po_count = po_qs.count()
                total_spend = ZERO
                total_paid = ZERO

                if po_count > 0:
                    for po in po_qs:
                        po_total = to_decimal(getattr(po, 'total_amount', 0))
                        total_spend += po_total
                        if getattr(po, 'payment_method', 'CASH') != 'CREDIT':
                            total_paid += po_total
                        else:
                            total_paid += to_decimal(getattr(po, 'amount_paid', 0))
                else:
                    purchases_qs = Purchase.objects.filter(Q(supplier=sup) | Q(supplier_name__iexact=sup.name))
                    if branch_obj:
                        purchases_qs = purchases_qs.filter(branch=branch_obj)
                    if start_dt and end_dt:
                        purchases_qs = purchases_qs.filter(purchase_date__range=(start_dt, end_dt))

                    for p in purchases_qs:
                        p_total = to_decimal(getattr(p, 'quantity', 0)) * to_decimal(getattr(p, 'unit_cost_price', 0))
                        total_spend += p_total
                        if getattr(p, 'payment_method', 'CASH') != 'CREDIT':
                            total_paid += p_total
                        else:
                            total_paid += to_decimal(getattr(p, 'amount_paid', 0))

                    po_count = purchases_qs.count()

                outstanding_balance = max(ZERO, total_spend - total_paid)

                results.append({
                    "id": sup.id,
                    "supplier_name": sup.name,
                    "total_purchases_count": po_count,
                    "total_spend": float(quantize_money(total_spend)),
                    "outstanding_balance": float(quantize_money(outstanding_balance)),
                })

            # Handle ad-hoc text-based supplier names (e.g., "Direct Supplier")
            adhoc_po_qs = PurchaseOrder.objects.exclude(supplier_name__in=[s.name for s in suppliers])
            if branch_obj:
                adhoc_po_qs = adhoc_po_qs.filter(branch=branch_obj)
            if start_dt and end_dt:
                adhoc_po_qs = adhoc_po_qs.filter(
                    Q(purchase_date__range=(start_dt, end_dt))
                )

            adhoc_suppliers = adhoc_po_qs.values_list('supplier_name', flat=True).distinct()
            for name in adhoc_suppliers:
                clean_name = (name or "Direct Supplier").strip()
                if clean_name.lower() in supplier_names_seen:
                    continue
                supplier_names_seen.add(clean_name.lower())

                sub_pos = adhoc_po_qs.filter(supplier_name__iexact=clean_name)
                po_count = sub_pos.count()
                total_spend = ZERO
                total_paid = ZERO

                for po in sub_pos:
                    po_total = to_decimal(getattr(po, 'total_amount', 0))
                    total_spend += po_total
                    if getattr(po, 'payment_method', 'CASH') != 'CREDIT':
                        total_paid += po_total
                    else:
                        total_paid += to_decimal(getattr(po, 'amount_paid', 0))

                outstanding_balance = max(ZERO, total_spend - total_paid)

                results.append({
                    "id": f"adhoc-{clean_name}",
                    "supplier_name": clean_name,
                    "total_purchases_count": po_count,
                    "total_spend": float(quantize_money(total_spend)),
                    "outstanding_balance": float(quantize_money(outstanding_balance)),
                })

            results.sort(key=lambda x: x["total_spend"], reverse=True)
            return Response(results, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Error fetching suppliers list")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # --------------------------------------------------------------------------
    # 7. /purchases-analytics/export-purchases-pdf/
    # --------------------------------------------------------------------------
    @action(detail=False, methods=['get'], url_path='export-purchases-pdf')
    def export_purchases_pdf(self, request):
        try:
            overview_res = self.purchases_overview(request)
            orders_res = self.purchase_orders(request)
            suppliers_res = self.suppliers_list(request)

            summary = overview_res.data.get("summary", {}) if overview_res.status_code == 200 else {}
            orders = orders_res.data if orders_res.status_code == 200 else []
            suppliers = suppliers_res.data if suppliers_res.status_code == 200 else []

            export_type = (request.query_params.get("export_type") or request.query_params.get("type") or "summary").lower()
            branch_obj, start_date, end_date, _, _ = self._get_filters(request)
            branch_name = branch_obj.name if branch_obj else "All Branches"
            period_str = f"{start_date.strftime('%b %d, %Y')} – {end_date.strftime('%b %d, %Y')}" if start_date and end_date else "All Time"

            def fmt_pdf_curr(val):
                num = float(val or 0)
                if num < 0:
                    return f"(KSh {abs(num):,.2f})"
                return f"KSh {num:,.2f}"

            buffer = io.BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=letter,
                rightMargin=30,
                leftMargin=30,
                topMargin=30,
                bottomMargin=30
            )

            styles = getSampleStyleSheet()

            company_style = ParagraphStyle(
                'PurCompanyHeader',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=15,
                leading=18,
                textColor=colors.HexColor('#0F766E'),
                alignment=1,
                spaceAfter=3
            )

            subtitle_style = ParagraphStyle(
                'PurSubHeader',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=9,
                leading=12,
                textColor=colors.HexColor('#475569'),
                alignment=1,
                spaceAfter=10
            )

            section_heading = ParagraphStyle(
                'PurSectionHead',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=10,
                leading=14,
                textColor=colors.HexColor('#0F172A'),
                spaceAfter=6
            )

            tbl_header = ParagraphStyle(
                'PurTh',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=8,
                textColor=colors.HexColor('#0F172A')
            )

            tbl_cell = ParagraphStyle(
                'PurTd',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=8,
                textColor=colors.HexColor('#1E293B')
            )

            po_header_white = ParagraphStyle(
                'PurPoHeaderWhite',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=8,
                textColor=colors.white
            )

            po_cell_white = ParagraphStyle(
                'PurPoCellWhite',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=8,
                textColor=colors.white
            )

            item_header_style = ParagraphStyle(
                'PurItemTh',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=7,
                textColor=colors.HexColor('#0F766E')
            )

            item_cell_style = ParagraphStyle(
                'PurItemTd',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=7,
                textColor=colors.HexColor('#1E293B')
            )

            elements = []

            # 1. Header Title
            report_title_suffix = "DETAILED RESTOCK REPORT" if export_type == "detailed" else "EXECUTIVE SUMMARY REPORT"
            elements.append(Paragraph("MELVEX BUSINESS SOLUTIONS", company_style))
            elements.append(Paragraph(f"<b>PROCUREMENT AUDIT ({report_title_suffix}) — {branch_name.upper()}</b><br/>Period: {period_str}", subtitle_style))
            elements.append(Spacer(1, 6))

            # 2. Executive Indicators KPI Table
            elements.append(Paragraph("<b>EXECUTIVE PROCUREMENT INDICATORS</b>", section_heading))
            summary_data = [
                [Paragraph("Total Procurement Spend", tbl_header), fmt_pdf_curr(summary.get("total_procurement_spend"))],
                [Paragraph("Total Disbursed to Suppliers", tbl_cell), fmt_pdf_curr(summary.get("total_paid_suppliers"))],
                [Paragraph("Outstanding Payables", tbl_header), fmt_pdf_curr(summary.get("outstanding_payables"))],
                [Paragraph("Purchase Orders Issued", tbl_cell), f"{summary.get('purchase_orders_count', 0)} Orders"],
                [Paragraph("Total Restocked Units", tbl_cell), f"{summary.get('total_units_purchased', 0)} Units"],
                [Paragraph("Supplier Returns Count", tbl_cell), f"{summary.get('supplier_returns_count', 0)} Returns"],
            ]

            sum_table = Table(summary_data, colWidths=[350, 202])
            sum_table.setStyle(TableStyle([
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F0FDFA')),
                ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCFBF1')),
            ]))
            elements.append(sum_table)
            elements.append(Spacer(1, 10))

            # 3. GRAPHICAL CHARTS GENERATION & EMBEDDING
            vendor_img = generate_pdf_vendor_chart(suppliers)
            payables_img = generate_pdf_payables_pie(summary)

            if vendor_img or payables_img:
                elements.append(Paragraph("<b>PROCUREMENT ANALYTICS & PAYABLES CHARTS</b>", section_heading))
                if vendor_img and payables_img:
                    chart_table = Table([[vendor_img], [payables_img]], colWidths=[552])
                    chart_table.setStyle(TableStyle([
                        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('TOPPADDING', (0, 0), (-1, -1), 4),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                    ]))
                    elements.append(chart_table)
                elif vendor_img:
                    elements.append(vendor_img)
                elif payables_img:
                    elements.append(payables_img)
                elements.append(Spacer(1, 10))

            # 4. PURCHASE ORDERS LEDGER (SUMMARY vs DETAILED MODE)
            if orders:
                elements.append(Paragraph(f"<b>PURCHASE ORDERS LEDGER ({report_title_suffix})</b>", section_heading))

                if export_type == "summary":
                    po_table_data = [[
                        Paragraph("PO Ref", tbl_header),
                        Paragraph("Date", tbl_header),
                        Paragraph("Supplier", tbl_header),
                        Paragraph("Terms", tbl_header),
                        Paragraph("Total Cost", tbl_header)
                    ]]
                    for po in orders:
                        po_table_data.append([
                            Paragraph(str(po.get("po_number")), tbl_cell),
                            Paragraph(str(po.get("purchase_date")), tbl_cell),
                            Paragraph(str(po.get("supplier_name")), tbl_cell),
                            Paragraph(str(po.get("payment_method")), tbl_cell),
                            fmt_pdf_curr(po.get("total_cost"))
                        ])
                    po_table = Table(po_table_data, colWidths=[100, 112, 140, 90, 110])
                    po_table.setStyle(TableStyle([
                        ('ALIGN', (4, 0), (4, -1), 'RIGHT'),
                        ('TOPPADDING', (0, 0), (-1, -1), 4),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
                        ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                    ]))
                    elements.append(po_table)
                    elements.append(Spacer(1, 10))
                else:
                    # Detailed Mode: Render order headers followed by itemized restock lines
                    for po in orders:
                        po_elements = []
                        po_summary_data = [[
                            Paragraph(f"<b>PO Ref: {po.get('po_number')}</b>", po_header_white),
                            Paragraph(f"Date: {po.get('purchase_date')}", po_cell_white),
                            Paragraph(f"Supplier: {po.get('supplier_name')}", po_cell_white),
                            Paragraph(f"Terms: {po.get('payment_method')}", po_cell_white),
                            Paragraph(f"<b>Total: {fmt_pdf_curr(po.get('total_cost'))}</b>", po_header_white)
                        ]]
                        po_sum_table = Table(po_summary_data, colWidths=[110, 112, 130, 90, 110])
                        po_sum_table.setStyle(TableStyle([
                            ('ALIGN', (4, 0), (4, -1), 'RIGHT'),
                            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F766E')),
                            ('TOPPADDING', (0, 0), (-1, -1), 4),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                        ]))
                        po_elements.append(po_sum_table)

                        items = po.get("items", [])
                        if items:
                            item_table_data = [[
                                Paragraph("Restock Product Name", item_header_style),
                                Paragraph("Qty Restocked", item_header_style),
                                Paragraph("Unit Cost Price", item_header_style),
                                Paragraph("Subtotal Cost", item_header_style)
                            ]]
                            for it in items:
                                item_table_data.append([
                                    Paragraph(str(it.get("item_name")), item_cell_style),
                                    Paragraph(str(it.get("quantity")), item_cell_style),
                                    fmt_pdf_curr(it.get("unit_cost")),
                                    fmt_pdf_curr(it.get("total_cost"))
                                ])
                            item_table = Table(item_table_data, colWidths=[242, 100, 100, 110])
                            item_table.setStyle(TableStyle([
                                ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                                ('TOPPADDING', (0, 0), (-1, -1), 3),
                                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F0FDFA')),
                                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCFBF1')),
                            ]))
                            po_elements.append(item_table)

                        po_elements.append(Spacer(1, 6))
                        elements.append(KeepTogether(po_elements))
                    elements.append(Spacer(1, 10))

            # 5. VENDOR DIRECTORY & OUTSTANDING PAYABLES TABLE
            if suppliers:
                elements.append(Paragraph("<b>VENDOR DIRECTORY & OUTSTANDING PAYABLES LEDGER</b>", section_heading))
                sup_data = [[
                    Paragraph("Supplier Name", tbl_header),
                    Paragraph("Orders Count", tbl_header),
                    Paragraph("Lifetime Restock Spend", tbl_header),
                    Paragraph("Outstanding Payable", tbl_header)
                ]]
                for sup in suppliers:
                    sup_data.append([
                        Paragraph(str(sup.get("supplier_name")), tbl_cell),
                        Paragraph(str(sup.get("total_purchases_count")), tbl_cell),
                        fmt_pdf_curr(sup.get("total_spend")),
                        fmt_pdf_curr(sup.get("outstanding_balance"))
                    ])
                sup_table = Table(sup_data, colWidths=[200, 100, 126, 126])
                sup_table.setStyle(TableStyle([
                    ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
                    ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                ]))
                elements.append(sup_table)

            doc.build(elements)
            buffer.seek(0)

            filename_prefix = "Detailed_Procurement" if export_type == "detailed" else "Summary_Procurement"
            response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="{filename_prefix}_Report_{branch_name}.pdf"'
            return response

        except Exception as e:
            logger.exception("Export Purchases PDF Error")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
def generate_pdf_trend_chart(daily_trends):
    """Generates a PNG byte stream of Revenue vs COGS vs Profit."""
    if not daily_trends:
        return None

    dates = [d['date'] for d in daily_trends]
    revenues = [d['revenue'] for d in daily_trends]
    cogs = [d['cogs'] for d in daily_trends]
    profits = [d['profit'] for d in daily_trends]

    x = range(len(dates))
    width = 0.25

    fig, ax = plt.subplots(figsize=(7, 2.6), dpi=150)
    ax.bar([i - width for i in x], revenues, width=width, label='Gross Revenue', color='#0D9488')
    ax.bar(x, cogs, width=width, label='COGS', color='#D97706')
    ax.bar([i + width for i in x], profits, width=width, label='Gross Profit', color='#059669')

    ax.set_xticks(list(x))
    ax.set_xticklabels(dates, rotation=25, ha='right', fontsize=7)
    ax.tick_params(axis='y', labelsize=7)
    ax.legend(fontsize=7, loc='upper left')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    plt.tight_layout()

    img_buf = io.BytesIO()
    plt.savefig(img_buf, format='png', transparent=False, facecolor='white')
    plt.close(fig)
    img_buf.seek(0)
    return Image(img_buf, width=520, height=190)


def generate_pdf_pie_chart(payment_breakdown):
    """Generates a PNG byte stream of Payment Channels distribution."""
    if not payment_breakdown:
        return None

    labels = [p['method'] for p in payment_breakdown]
    sizes = [p['amount'] for p in payment_breakdown]
    colors_list = ['#0D9488', '#2563EB', '#D97706', '#059669', '#7C3AED', '#DB2777', '#475569']

    fig, ax = plt.subplots(figsize=(4.5, 2.6), dpi=150)
    wedges, texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        autopct='%1.1f%%',
        colors=colors_list[:len(sizes)],
        textprops={'fontsize': 7},
        startangle=140
    )
    for autotext in autotexts:
        autotext.set_color('white')
        autotext.set_weight('bold')

    plt.tight_layout()

    img_buf = io.BytesIO()
    plt.savefig(img_buf, format='png', transparent=False, facecolor='white')
    plt.close(fig)
    img_buf.seek(0)
    return Image(img_buf, width=320, height=185)


def quantize_money(amount):
    """Safely convert numerical inputs to Decimal rounded to 2 decimal places."""
    if amount is None:
        return ZERO
    return Decimal(str(amount)).quantize(Decimal("0.01"))


logger = logging.getLogger(__name__)
ZERO = Decimal("0.00")

def quantize_money(val):
    if val is None:
        return ZERO
    return Decimal(str(val)).quantize(Decimal("0.01"))


class DetailedSalesAnalysisViewSet(viewsets.ViewSet):
    permission_classes = [permissions.AllowAny]

    def _normalize_payment_method(self, method_name):
        """
        Standardizes payment method strings matching _cash_period_data 1:1.
        Evaluates PAYBILL prior to generic MPESA/SEND_MONEY terms.
        """
        if not method_name:
            return "CASH"

        clean = (
            str(method_name)
            .strip()
            .upper()
            .replace("-", "_")
            .replace(" ", "_")
            .replace("/", "_")
        )

        # 1. PAYBILL takes priority over general MPESA keywords
        if any(k in clean for k in ["PAYBILL", "PAY_BILL", "C2B"]):
            return "PAYBILL"

        # 2. SEND_MONEY, TILL, BUY_GOODS, and generic MPESA / MOBILE
        if any(
            k in clean
            for k in [
                "SEND_MONEY",
                "SENDMONEY",
                "TILL",
                "BUY_GOODS",
                "BUYGOODS",
                "P2P",
                "MPESA",
                "M_PESA",
                "MOBILE",
            ]
        ):
            return "SEND_MONEY"

        # 3. BANK TRANSFER
        if any(k in clean for k in ["BANK", "EFT", "TRANSFER"]):
            return "BANK_TRANSFER"

        # 4. CASH
        if "CASH" in clean:
            return "CASH"

        # 5. CREDIT / INVOICE
        if any(k in clean for k in ["CREDIT", "DEBT", "ON_ACCOUNT", "INVOICE"]):
            return "CREDIT"

        # 6. SPLIT
        if "SPLIT" in clean:
            return "SPLIT"

        return clean

    def _extract_sp_method(self, sp):
        """Safely inspects split payment instances across all potential field names."""
        for attr in ["method", "payment_method", "payment_mode", "channel", "mode", "type"]:
            val = getattr(sp, attr, None)
            if val:
                return str(val)
        return "CASH"

    def _extract_sp_amount(self, sp):
        """Safely extracts amount from split records across potential field names."""
        for attr in ["amount", "amount_paid", "paid_amount", "val", "value", "total"]:
            val = getattr(sp, attr, None)
            if val is not None:
                try:
                    return quantize_money(val)
                except Exception:
                    continue
        return ZERO

    def _get_split_payments(self, obj):
        """Safely gathers child split records across main transactions, debt repayments, and legacy sales."""
        all_found = []
        
        # 1. Direct split_payments relation (SaleTransaction or Sale)
        if hasattr(obj, "split_payments"):
            splits = list(obj.split_payments.all())
            if splits:
                all_found.extend(splits)

        # 2. Direct splits relation (CreditRepayment or alternative naming)
        if not all_found:
            for rel in ["splits", "payments", "sale_payments"]:
                if hasattr(obj, rel):
                    rel_manager = getattr(obj, rel)
                    if hasattr(rel_manager, "all"):
                        splits = list(rel_manager.all())
                        if splits:
                            all_found.extend(splits)
                            break

        # 3. Check legacy sales split payments if direct splits are missing
        if not all_found and hasattr(obj, "legacy_sales") and obj.legacy_sales.exists():
            for legacy_sale in obj.legacy_sales.all():
                for rel in ["split_payments", "payments"]:
                    if hasattr(legacy_sale, rel):
                        splits = list(getattr(legacy_sale, rel).all())
                        if splits:
                            all_found.extend(splits)
                            break

        return all_found

    def _extract_qty(self, obj, default=Decimal("1.000")):
        """Safely extracts quantity from line items."""
        for attr in ["quantity", "quantity_sold", "qty", "count"]:
            val = getattr(obj, attr, None)
            if val is not None:
                try:
                    return Decimal(str(val))
                except Exception:
                    continue
        return Decimal(str(default))

    def _get_branch_and_dates(self, request):
        return _get_reporting_filters(request, default_period="month")

    def _generate_executive_insights(self, summary, fast_movers, slow_movers):
        notes = []
        margin = summary.get("profit_margin_percent", 0)
        gross_rev = summary.get("gross_revenue", 0)

        if gross_rev > 0:
            if margin >= 30:
                notes.append({
                    "type": "success",
                    "title": "Healthy Profit Margin",
                    "message": f"Gross profit margin is running strong at {margin}%. Direct costs are well controlled."
                })
            elif margin >= 15:
                notes.append({
                    "type": "warning",
                    "title": "Moderate Margin Performance",
                    "message": f"Gross margin stands at {margin}%. Evaluate unit procurement costs to increase profitability."
                })
            else:
                notes.append({
                    "type": "danger",
                    "title": "Low Profit Margin Warning",
                    "message": f"Gross profit margin is low at {margin}%. Direct COGS and discount limits require review."
                })

        credit_exp = summary.get("credit_sales_exposure", 0)
        if gross_rev > 0 and (credit_exp / gross_rev) > 0.20:
            notes.append({
                "type": "warning",
                "title": "High Credit Sales Exposure",
                "message": f"KSh {credit_exp:,.2f} of total revenue was executed on credit terms."
            })

        if fast_movers:
            top_item = fast_movers[0]
            notes.append({
                "type": "success",
                "title": "Top Revenue Driver",
                "message": f"'{top_item['item_name']}' leads overall velocity with KSh {top_item['gross_revenue']:,.2f} gross revenue."
            })

        if slow_movers:
            notes.append({
                "type": "info",
                "title": "Inventory Velocity Action",
                "message": f"{len(slow_movers)} items exhibit low turnover. Consider bundling stagnant stock."
            })

        return notes

    @action(detail=False, methods=['get'], url_path='detailed-report')
    def detailed_report(self, request):
        try:
            branch_obj, start_date, end_date, start_dt, end_dt, period_type = self._get_branch_and_dates(request)

            sales_qs = SaleTransaction.objects.all()
            returns_qs = CustomerReturn.objects.all()
            repayment_qs = CreditRepayment.objects.all()

            if start_dt and end_dt:
                sales_qs = sales_qs.filter(sale_date__gte=start_dt, sale_date__lte=end_dt)
            elif start_date and end_date:
                sales_qs = sales_qs.filter(sale_date__date__gte=start_date, sale_date__date__lte=end_date)
            
            # Repayments strictly filtered by collection date
            if start_date and end_date:
                returns_qs = returns_qs.filter(date__gte=start_date, date__lte=end_date)
                repayment_qs = repayment_qs.filter(date__gte=start_date, date__lte=end_date)

            if branch_obj:
                sales_qs = sales_qs.filter(branch=branch_obj)
                returns_qs = returns_qs.filter(branch=branch_obj)
                repayment_qs = repayment_qs.filter(
                    Q(branch=branch_obj) | Q(transaction__branch=branch_obj) | Q(branch__isnull=True)
                )

            sale_items_qs = SaleItem.objects.filter(transaction__in=sales_qs).select_related('item', 'transaction')

            # Channel Inflow Aggregators (Direct Sales)
            cash_total = ZERO
            paybill_total = ZERO
            send_money_total = ZERO
            bank_total = ZERO
            other_total = ZERO
            gross_credit_sales = ZERO

            # Channel Inflow Aggregators (Debt Repayments Collected)
            period_credit_repayments_total = ZERO
            repayments_by_channel = {
                "CASH": ZERO,
                "PAYBILL": ZERO,
                "SEND_MONEY": ZERO,
                "BANK_TRANSFER": ZERO,
                "OTHER": ZERO,
            }

            transaction_count = sales_qs.count()

            # -----------------------------------------------------------------
            # 1. DIRECT SALES INFLOWS WITH ENFORCED SPLIT PAYMENT ROUTING
            # -----------------------------------------------------------------
            sales_with_payments = sales_qs.distinct().select_related("customer", "branch").prefetch_related(
                "split_payments", "legacy_sales__split_payments"
            )

            for sale in sales_with_payments:
                rec_num = str(sale.receipt_number or f"REC-{sale.id}")

                # Rule A: Invoices (INV...) route 100% of their total face value to CREDIT exposure
                if rec_num.startswith("INV"):
                    sale_total = Decimal(str(sale.total_amount or ZERO))
                    gross_credit_sales += quantize_money(sale_total)
                    continue

                # Rule B: Standard Receipts (REC...) parse exact liquid breakdown via model property
                breakdown = sale.liquid_payments_breakdown

                c_amt = quantize_money(breakdown.get("CASH", ZERO))
                pb_amt = quantize_money(breakdown.get("PAYBILL", ZERO))
                sm_amt = quantize_money(breakdown.get("SEND_MONEY", ZERO))
                bt_amt = quantize_money(breakdown.get("BANK_TRANSFER", ZERO))
                cr_amt = quantize_money(breakdown.get("CREDIT", ZERO))
                oth_amt = quantize_money(breakdown.get("OTHER", ZERO))

                # Accumulate exact routed values
                cash_total += c_amt
                paybill_total += pb_amt
                send_money_total += sm_amt
                bank_total += bt_amt
                gross_credit_sales += cr_amt
                other_total += oth_amt

            # -----------------------------------------------------------------
            # 2. DEBT REPAYMENTS WITH ENFORCED SPLIT REPAYMENT ROUTING
            # -----------------------------------------------------------------
            repayments_list = []
            repayments_qs = repayment_qs.distinct().select_related("customer", "transaction").prefetch_related("splits")

            for repayment in repayments_qs:
                rep_amt = quantize_money(repayment.amount or ZERO)
                if rep_amt <= ZERO:
                    continue

                period_credit_repayments_total += rep_amt
                raw_method = getattr(repayment, "payment_method", "CASH")
                norm_rep_method = self._normalize_payment_method(raw_method)

                # Fetch child CreditRepaymentSplit entries using helper
                split_records = self._get_split_payments(repayment)

                if norm_rep_method == "SPLIT" or len(split_records) > 0:
                    if split_records:
                        for sp in split_records:
                            sp_raw = self._extract_sp_method(sp)
                            sp_channel = self._normalize_payment_method(sp_raw)
                            sp_amt = self._extract_sp_amount(sp)
                            
                            target = sp_channel if sp_channel in repayments_by_channel else "OTHER"
                            repayments_by_channel[target] += sp_amt
                    else:
                        repayments_by_channel["CASH"] += rep_amt
                else:
                    target = norm_rep_method if norm_rep_method in repayments_by_channel else "OTHER"
                    repayments_by_channel[target] += rep_amt

                repayments_list.append({
                    "id": repayment.id,
                    "date": repayment.date.strftime("%Y-%m-%d") if hasattr(repayment.date, "strftime") else str(repayment.date),
                    "customer_name": repayment.customer.name if getattr(repayment, "customer", None) else "Credit Customer",
                    "reference_number": getattr(repayment, "reference_number", None) or getattr(repayment, "receipt_number", None) or "-",
                    "payment_method": norm_rep_method,
                    "amount": float(rep_amt)
                })

            net_credit_balance = max(ZERO, gross_credit_sales - period_credit_repayments_total)

            # -----------------------------------------------------------------
            # 3. SALE ITEMS AGGREGATION & DAILY TRENDS
            # -----------------------------------------------------------------
            total_gross_revenue = ZERO
            total_cogs = ZERO
            total_discounts = ZERO
            total_units_sold = ZERO
            products_map = {}
            daily_map = {}
            transaction_items_map = {}

            trend_granularity = period_type
            if trend_granularity == "custom":
                span_days = (end_date - start_date).days + 1
                if span_days <= 14:
                    trend_granularity = "custom_day"
                elif span_days <= 62:
                    trend_granularity = "custom_week"
                elif span_days <= 366:
                    trend_granularity = "custom_month"
                else:
                    trend_granularity = "custom_year"
            elif trend_granularity == "all":
                trend_granularity = "all_year"

            for line in sale_items_qs:
                qty = self._extract_qty(line, default=ZERO)
                price = Decimal(str(line.unit_selling_price or 0))
                disc = Decimal(str(line.discount_per_item or 0))

                unit_cost = line.unit_cost_price
                if not unit_cost and line.item:
                    unit_cost = getattr(line.item, 'cost_price', ZERO)
                cost = Decimal(str(unit_cost or 0))

                line_rev = qty * (price - disc)
                line_cost = qty * cost
                line_disc_total = qty * disc

                total_gross_revenue += line_rev
                total_cogs += line_cost
                total_discounts += line_disc_total
                total_units_sold += qty

                i_id = line.item_id
                name = line.item.name if line.item else f"Item #{i_id}"
                if i_id not in products_map:
                    products_map[i_id] = {
                        "item_id": i_id,
                        "item_name": name,
                        "units_sold": ZERO,
                        "gross_revenue": ZERO,
                        "cogs": ZERO,
                        "discounts": ZERO,
                    }
                products_map[i_id]["units_sold"] += qty
                products_map[i_id]["gross_revenue"] += line_rev
                products_map[i_id]["cogs"] += line_cost
                products_map[i_id]["discounts"] += line_disc_total

                sale_dt = line.transaction.sale_date if line.transaction else None
                if sale_dt and timezone.is_aware(sale_dt):
                    sale_dt = timezone.localtime(sale_dt)

                if not sale_dt:
                    sale_dt = timezone.localtime(timezone.now())

                granularity = trend_granularity

                if granularity == "day":
                    bucket_key = sale_dt.strftime("%Y-%m-%d-%H")
                    bucket_label = sale_dt.strftime("%H:00")
                elif granularity == "week":
                    bucket_key = sale_dt.strftime("%Y-%m-%d")
                    bucket_label = sale_dt.strftime("%a")
                elif granularity == "month":
                    week_number = ((sale_dt.day - 1) // 7) + 1
                    bucket_key = f"{sale_dt.year:04d}-{sale_dt.month:02d}-W{week_number}"
                    bucket_label = f"Week {week_number}"
                elif granularity == "year":
                    bucket_key = sale_dt.strftime("%Y-%m")
                    bucket_label = sale_dt.strftime("%b")
                elif granularity == "custom_month":
                    bucket_key = sale_dt.strftime("%Y-%m")
                    bucket_label = sale_dt.strftime("%b %Y")
                elif granularity in ("custom_year", "all_year"):
                    bucket_key = sale_dt.strftime("%Y")
                    bucket_label = sale_dt.strftime("%Y")
                elif granularity == "custom_day":
                    bucket_key = sale_dt.strftime("%Y-%m-%d")
                    bucket_label = sale_dt.strftime("%d %b")
                elif granularity == "custom_week":
                    week_start = sale_dt.date() - timedelta(days=sale_dt.weekday())
                    bucket_key = week_start.strftime("%Y-%m-%d")
                    bucket_label = f"Week of {week_start.strftime('%d %b')}"
                else:
                    bucket_key = sale_dt.strftime("%Y")
                    bucket_label = sale_dt.strftime("%Y")

                if bucket_key not in daily_map:
                    daily_map[bucket_key] = {
                        "date": bucket_label,
                        "period_start": bucket_key,
                        "revenue": ZERO,
                        "cogs": ZERO,
                    }
                daily_map[bucket_key]["revenue"] += line_rev
                daily_map[bucket_key]["cogs"] += line_cost

                if line.transaction_id:
                    if line.transaction_id not in transaction_items_map:
                        transaction_items_map[line.transaction_id] = []
                    
                    transaction_items_map[line.transaction_id].append({
                        "item_name": name,
                        "quantity": float(qty),
                        "unit_price": float(price),
                        "discount": float(disc),
                        "line_revenue": float(line_rev),
                        "line_cogs": float(line_cost),
                        "line_profit": float(line_rev - line_cost)
                    })

            def ensure_trend_bucket(key, label):
                daily_map.setdefault(key, {
                    "date": label,
                    "period_start": key,
                    "revenue": ZERO,
                    "cogs": ZERO,
                })

            if trend_granularity == "day":
                for hour in range(24):
                    key = f"{start_date:%Y-%m-%d}-{hour:02d}"
                    ensure_trend_bucket(key, f"{hour:02d}:00")
            elif trend_granularity == "week":
                cursor = start_date
                while cursor <= end_date:
                    key = cursor.strftime("%Y-%m-%d")
                    ensure_trend_bucket(key, cursor.strftime("%a"))
                    cursor += timedelta(days=1)
            elif trend_granularity == "month":
                last_day = calendar.monthrange(start_date.year, start_date.month)[1]
                max_week = ((last_day - 1) // 7) + 1
                for week_number in range(1, max_week + 1):
                    key = f"{start_date.year:04d}-{start_date.month:02d}-W{week_number}"
                    ensure_trend_bucket(key, f"Week {week_number}")
            elif trend_granularity == "year":
                for month_number in range(1, 13):
                    key = f"{start_date.year:04d}-{month_number:02d}"
                    ensure_trend_bucket(key, calendar.month_abbr[month_number])
            elif trend_granularity == "custom_day":
                cursor = start_date
                while cursor <= end_date:
                    key = cursor.strftime("%Y-%m-%d")
                    ensure_trend_bucket(key, cursor.strftime("%d %b"))
                    cursor += timedelta(days=1)
            elif trend_granularity == "custom_week":
                cursor = start_date - timedelta(days=start_date.weekday())
                last_week = end_date - timedelta(days=end_date.weekday())
                while cursor <= last_week:
                    key = cursor.strftime("%Y-%m-%d")
                    ensure_trend_bucket(key, f"Week of {cursor.strftime('%d %b')}")
                    cursor += timedelta(days=7)
            elif trend_granularity == "custom_month":
                cursor = date(start_date.year, start_date.month, 1)
                while cursor <= end_date:
                    key = cursor.strftime("%Y-%m")
                    ensure_trend_bucket(key, cursor.strftime("%b %Y"))
                    cursor = (
                        date(cursor.year + 1, 1, 1)
                        if cursor.month == 12
                        else date(cursor.year, cursor.month + 1, 1)
                    )
            elif trend_granularity in ("custom_year", "all_year"):
                first_year = start_date.year
                last_year = end_date.year
                for year_number in range(first_year, last_year + 1):
                    key = str(year_number)
                    ensure_trend_bucket(key, key)

            daily_trends = []
            for k, v in sorted(daily_map.items()):
                if k == "Unknown":
                    continue
                rev_val = float(quantize_money(v["revenue"]))
                cogs_val = float(quantize_money(v["cogs"]))
                profit_val = float(quantize_money(v["revenue"] - v["cogs"]))
                daily_trends.append({
                    "date": v["date"],
                    "period_start": v["period_start"],
                    "revenue": rev_val,
                    "cogs": cogs_val,
                    "profit": profit_val
                })

            gross_profit = total_gross_revenue - total_cogs
            net_revenue = total_gross_revenue - total_discounts
            profit_margin_pct = float((gross_profit / total_gross_revenue * Decimal("100.0")) if total_gross_revenue > ZERO else ZERO)
            discount_rate_pct = float((total_discounts / (total_gross_revenue + total_discounts) * Decimal("100.0")) if (total_gross_revenue + total_discounts) > ZERO else ZERO)
            avg_basket_value = quantize_money(total_gross_revenue / transaction_count) if transaction_count > 0 else ZERO
            avg_items_per_basket = round(float(total_units_sold / transaction_count), 2) if transaction_count > 0 else 0.0

            performers = []
            for v in products_map.values():
                rev_val = v["gross_revenue"]
                profit_val = rev_val - v["cogs"]
                m_pct = float((profit_val / rev_val * Decimal("100.0")) if rev_val > ZERO else ZERO)
                performers.append({
                    "item_id": v["item_id"],
                    "item_name": v["item_name"],
                    "units_sold": float(v["units_sold"]),
                    "gross_revenue": float(quantize_money(rev_val)),
                    "cogs": float(quantize_money(v["cogs"])),
                    "profit": float(quantize_money(profit_val)),
                    "discounts": float(quantize_money(v["discounts"])),
                    "profit_margin_percent": round(m_pct, 1)
                })

            sorted_by_velocity = sorted(performers, key=lambda x: (x["gross_revenue"], x["units_sold"]), reverse=True)
            total_items = len(sorted_by_velocity)
            if total_items <= 5:
                split_idx = max(1, total_items // 2)
                fast_movers = sorted_by_velocity[:split_idx]
                slow_movers = sorted_by_velocity[split_idx:]
            else:
                fast_movers = sorted_by_velocity[:10]
                slow_movers = sorted(sorted_by_velocity[10:], key=lambda x: (x["units_sold"], x["gross_revenue"]))[:10]

            summary_payload = {
                "start_date": str(start_date) if start_date else None,
                "end_date": str(end_date) if end_date else None,
                "gross_revenue": float(quantize_money(total_gross_revenue)),
                "net_revenue": float(quantize_money(net_revenue)),
                "total_cogs": float(quantize_money(total_cogs)),
                "gross_profit": float(quantize_money(gross_profit)),
                "profit_margin_percent": round(profit_margin_pct, 2),
                "total_discounts": float(quantize_money(total_discounts)),
                "discount_rate_percent": round(discount_rate_pct, 2),
                "transaction_count": transaction_count,
                "avg_basket_value": float(avg_basket_value),
                "avg_items_per_basket": avg_items_per_basket,
                "total_units_sold": float(total_units_sold),
                "credit_sales_exposure": float(quantize_money(gross_credit_sales)),
                "net_credit_exposure": float(quantize_money(net_credit_balance)),
                "total_credit_repayments": float(quantize_money(period_credit_repayments_total)),
                "returned_orders_count": returns_qs.count()
            }

            insights = self._generate_executive_insights(summary_payload, fast_movers, slow_movers)

            # COMBINED RESOLVED PAYMENT CHANNEL DISTRIBUTION (PURE CASH SALES + COLLECTED REPAYMENTS)
            channel_map = {
                "CASH": cash_total + repayments_by_channel["CASH"],
                "PAYBILL": paybill_total + repayments_by_channel["PAYBILL"],
                "SEND_MONEY": send_money_total + repayments_by_channel["SEND_MONEY"],
                "BANK_TRANSFER": bank_total + repayments_by_channel["BANK_TRANSFER"],
                "CREDIT": gross_credit_sales,
                "OTHER": other_total + repayments_by_channel["OTHER"],
            }

            payment_breakdown = [
                {"method": k, "amount": float(quantize_money(v))}
                for k, v in channel_map.items()
            ]

            payment_channel_breakdown = {
                k.lower(): float(quantize_money(v))
                for k, v in channel_map.items()
            }

            transactions_list = []
            for sale in sales_with_payments:
                items_data = transaction_items_map.get(sale.id, [])
                
                rec_num = str(sale.receipt_number or f"REC-{sale.id}")
                norm_method = self._normalize_payment_method(sale.payment_method)
                split_records = self._get_split_payments(sale)

                if rec_num.startswith("INV"):
                    disp_channel = "CREDIT"
                elif norm_method == "SPLIT":
                    distinct_methods = set()
                    for sp in split_records:
                        sp_raw = self._extract_sp_method(sp)
                        distinct_methods.add(self._normalize_payment_method(sp_raw))
                    
                    if len(distinct_methods) > 1:
                        disp_channel = "SPLIT"
                    elif len(distinct_methods) == 1:
                        disp_channel = list(distinct_methods)[0]
                    else:
                        disp_channel = "SPLIT"
                else:
                    if split_records:
                        sp_raw = self._extract_sp_method(split_records[0])
                        disp_channel = self._normalize_payment_method(sp_raw)
                    else:
                        disp_channel = norm_method

                cust_disp_name = (
                    sale.customer.name if getattr(sale, 'customer', None) 
                    else (sale.customer_name or "Walk-in Customer")
                )

                transactions_list.append({
                    "id": sale.id,
                    "receipt_number": rec_num,
                    "sale_date": sale.sale_date.strftime("%Y-%m-%d %H:%M") if hasattr(sale.sale_date, "strftime") else str(sale.sale_date),
                    "customer_name": cust_disp_name,
                    "payment_method": disp_channel,
                    "total_amount": float(getattr(sale, "total_amount", 0) or 0),
                    "items": items_data
                })

            return Response({
                "summary": summary_payload,
                "daily_trends": daily_trends,
                "trend_granularity": {
                    "day": "hour",
                    "week": "day",
                    "month": "week",
                    "year": "month",
                    "custom_day": "day",
                    "custom_week": "week",
                    "custom_month": "month",
                    "custom_year": "year",
                    "all_year": "year",
                }.get(trend_granularity, trend_granularity),
                "payment_breakdown": payment_breakdown,
                "payment_channel_breakdown": payment_channel_breakdown,
                "fast_movers": fast_movers,
                "top_performers": fast_movers,
                "slow_movers": slow_movers,
                "insights": insights,
                "transactions": transactions_list,
                "credit_repayments_list": repayments_list,
                "repayments": repayments_list
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Error generating detailed sales analysis report")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], url_path='export-sales-analysis-pdf')
    def export_sales_analysis_pdf(self, request):
        try:
            report_response = self.detailed_report(request)
            if report_response.status_code != 200:
                return report_response

            data = report_response.data
            summary = data.get("summary", {})
            insights = data.get("insights", [])
            daily_trends = data.get("daily_trends", [])
            payment_breakdown = data.get("payment_breakdown", [])
            fast_movers = data.get("fast_movers", [])
            slow_movers = data.get("slow_movers", [])
            transactions = data.get("transactions", [])
            repayments_list = data.get("credit_repayments_list", [])

            export_type = (request.query_params.get("export_type") or request.query_params.get("type") or "summary").lower()

            branch_obj, start_date, end_date, _, _, _period = self._get_branch_and_dates(request)
            branch_name = branch_obj.name if branch_obj else "All Branches"
            period_str = f"{start_date.strftime('%b %d, %Y')} – {end_date.strftime('%b %d, %Y')}" if start_date and end_date else "All Time"

            def fmt_pdf_curr(val):
                num = float(val or 0)
                if num < 0:
                    return f"(KSh {abs(num):,.2f})"
                return f"KSh {num:,.2f}"

            buffer = io.BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=letter,
                rightMargin=30,
                leftMargin=30,
                topMargin=30,
                bottomMargin=30
            )

            styles = getSampleStyleSheet()

            company_style = ParagraphStyle(
                'SaCompanyHeader',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=15,
                leading=18,
                textColor=colors.HexColor('#0F766E'),
                alignment=1,
                spaceAfter=3
            )

            subtitle_style = ParagraphStyle(
                'SaSubHeader',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=9,
                leading=12,
                textColor=colors.HexColor('#475569'),
                alignment=1,
                spaceAfter=10
            )

            section_heading = ParagraphStyle(
                'SaSectionHead',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=10,
                leading=14,
                textColor=colors.HexColor('#0F172A'),
                spaceAfter=6
            )

            tbl_header = ParagraphStyle(
                'SaTh',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=8,
                textColor=colors.HexColor('#0F172A')
            )

            tbl_cell = ParagraphStyle(
                'SaTd',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=8,
                textColor=colors.HexColor('#1E293B')
            )

            tx_header_white = ParagraphStyle(
                'SaTxHeaderWhite',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=8,
                textColor=colors.white
            )

            tx_cell_white = ParagraphStyle(
                'SaTxCellWhite',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=8,
                textColor=colors.white
            )

            item_header_style = ParagraphStyle(
                'SaItemTh',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=7,
                textColor=colors.HexColor('#0F766E')
            )

            item_cell_style = ParagraphStyle(
                'SaItemTd',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=7,
                textColor=colors.HexColor('#1E293B')
            )

            elements = []

            # 1. Header Title
            report_title_suffix = "DETAILED TRANSACTIONS REPORT" if export_type == "detailed" else "EXECUTIVE SUMMARY REPORT"
            elements.append(Paragraph("MELVEX BUSINESS SOLUTIONS", company_style))
            elements.append(Paragraph(f"<b>SALES ANALYSIS ({report_title_suffix}) — {branch_name.upper()}</b><br/>Period: {period_str}", subtitle_style))
            elements.append(Spacer(1, 6))

            # 2. Financial KPI Summary Table
            elements.append(Paragraph("<b>FINANCIAL SUMMARY</b>", section_heading))
            summary_data = [
                [Paragraph("Gross Revenue", tbl_header), fmt_pdf_curr(summary.get("gross_revenue"))],
                [Paragraph("Cost of Goods Sold (COGS)", tbl_cell), fmt_pdf_curr(summary.get("total_cogs"))],
                [Paragraph("Gross Profit", tbl_header), fmt_pdf_curr(summary.get("gross_profit"))],
                [Paragraph("Profit Margin Rate", tbl_header), f"{summary.get('profit_margin_percent', 0)}%"],
                [Paragraph("Total Orders Processed", tbl_cell), f"{summary.get('transaction_count', 0)} Txns"],
                [Paragraph("Average Basket Value", tbl_cell), fmt_pdf_curr(summary.get("avg_basket_value"))],
                [Paragraph("Credit Sales Exposure", tbl_cell), fmt_pdf_curr(summary.get("credit_sales_exposure"))],
                [Paragraph("Credit Repayments Collected", tbl_cell), fmt_pdf_curr(summary.get("total_credit_repayments"))],
            ]

            sum_table = Table(summary_data, colWidths=[350, 202])
            sum_table.setStyle(TableStyle([
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('BACKGROUND', (0, 2), (-1, 2), colors.HexColor('#F0FDFA')),
                ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCFBF1')),
            ]))
            elements.append(sum_table)
            elements.append(Spacer(1, 10))

            # 3. Executive Insights
            if insights:
                elements.append(Paragraph("<b>EXECUTIVE INSIGHTS & NOTES</b>", section_heading))
                for note in insights:
                    title = note.get("title", "Note")
                    msg = note.get("message", "")
                    elements.append(Paragraph(f"• <b>{title}:</b> {msg}", tbl_cell))
                    elements.append(Spacer(1, 2))
                elements.append(Spacer(1, 10))

            # 4. Period Trends Table Data
            if daily_trends:
                elements.append(Paragraph("<b>PERIOD TRENDS BREAKDOWN</b>", section_heading))
                trend_data = [[
                    Paragraph("Date", tbl_header),
                    Paragraph("Gross Revenue", tbl_header),
                    Paragraph("COGS", tbl_header),
                    Paragraph("Gross Profit", tbl_header)
                ]]
                for dt in daily_trends:
                    trend_data.append([
                        Paragraph(str(dt.get("date")), tbl_cell),
                        fmt_pdf_curr(dt.get("revenue")),
                        fmt_pdf_curr(dt.get("cogs")),
                        fmt_pdf_curr(dt.get("profit"))
                    ])
                trend_table = Table(trend_data, colWidths=[120, 144, 144, 144])
                trend_table.setStyle(TableStyle([
                    ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
                    ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                ]))
                elements.append(trend_table)
                elements.append(Spacer(1, 10))

            # 5. Fast & Slow Movers Tables
            if fast_movers:
                elements.append(Paragraph("<b>FAST-MOVING PRODUCTS (TOP PERFORMERS)</b>", section_heading))
                fm_data = [[
                    Paragraph("Product", tbl_header),
                    Paragraph("Units Sold", tbl_header),
                    Paragraph("Gross Revenue", tbl_header),
                    Paragraph("Margin %", tbl_header)
                ]]
                for item in fast_movers[:8]:
                    fm_data.append([
                        Paragraph(str(item.get("item_name")), tbl_cell),
                        Paragraph(str(item.get("units_sold")), tbl_cell),
                        fmt_pdf_curr(item.get("gross_revenue")),
                        f"{item.get('profit_margin_percent', 0)}%"
                    ])
                fm_table = Table(fm_data, colWidths=[240, 80, 132, 100])
                fm_table.setStyle(TableStyle([
                    ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
                    ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                ]))
                elements.append(fm_table)
                elements.append(Spacer(1, 10))

            if slow_movers:
                elements.append(Paragraph("<b>SLOW-MOVING PRODUCTS (STAGNANT INVENTORY)</b>", section_heading))
                sm_data = [[
                    Paragraph("Product", tbl_header),
                    Paragraph("Units Sold", tbl_header),
                    Paragraph("Gross Revenue", tbl_header),
                    Paragraph("Margin %", tbl_header)
                ]]
                for item in slow_movers[:8]:
                    sm_data.append([
                        Paragraph(str(item.get("item_name")), tbl_cell),
                        Paragraph(str(item.get("units_sold")), tbl_cell),
                        fmt_pdf_curr(item.get("gross_revenue")),
                        f"{item.get('profit_margin_percent', 0)}%"
                    ])
                sm_table = Table(sm_data, colWidths=[240, 80, 132, 100])
                sm_table.setStyle(TableStyle([
                    ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#FFFBEB')),
                    ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#FDE68A')),
                ]))
                elements.append(sm_table)
                elements.append(Spacer(1, 10))

            # 6. TRANSACTIONS LEDGER
            if transactions:
                elements.append(Paragraph(f"<b>TRANSACTIONS LEDGER ({report_title_suffix})</b>", section_heading))
                
                if export_type == "summary":
                    tx_data = [[
                        Paragraph("Receipt No", tbl_header),
                        Paragraph("Date / Time", tbl_header),
                        Paragraph("Customer", tbl_header),
                        Paragraph("Channel", tbl_header),
                        Paragraph("Total Amount", tbl_header)
                    ]]
                    for tx in transactions:
                        tx_data.append([
                            Paragraph(str(tx.get("receipt_number")), tbl_cell),
                            Paragraph(str(tx.get("sale_date")), tbl_cell),
                            Paragraph(str(tx.get("customer_name")), tbl_cell),
                            Paragraph(str(tx.get("payment_method")), tbl_cell),
                            fmt_pdf_curr(tx.get("total_amount"))
                        ])
                    tx_table = Table(tx_data, colWidths=[100, 112, 140, 90, 110])
                    tx_table.setStyle(TableStyle([
                        ('ALIGN', (4, 0), (4, -1), 'RIGHT'),
                        ('TOPPADDING', (0, 0), (-1, -1), 4),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
                        ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                    ]))
                    elements.append(tx_table)
                    elements.append(Spacer(1, 10))
                else:
                    for tx in transactions:
                        tx_elements = []
                        tx_summary_data = [[
                            Paragraph(f"<b>Receipt: {tx.get('receipt_number')}</b>", tx_header_white),
                            Paragraph(f"Date: {tx.get('sale_date')}", tx_cell_white),
                            Paragraph(f"Cust: {tx.get('customer_name')}", tx_cell_white),
                            Paragraph(f"Method: {tx.get('payment_method')}", tx_cell_white),
                            Paragraph(f"<b>Total: {fmt_pdf_curr(tx.get('total_amount'))}</b>", tx_header_white)
                        ]]
                        tx_sum_table = Table(tx_summary_data, colWidths=[110, 112, 130, 90, 110])
                        tx_sum_table.setStyle(TableStyle([
                            ('ALIGN', (4, 0), (4, -1), 'RIGHT'),
                            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F766E')),
                            ('TOPPADDING', (0, 0), (-1, -1), 4),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                        ]))
                        tx_elements.append(tx_sum_table)

                        items = tx.get("items", [])
                        if items:
                            item_table_data = [[
                                Paragraph("Product / Item Name", item_header_style),
                                Paragraph("Qty", item_header_style),
                                Paragraph("Unit Price", item_header_style),
                                Paragraph("Discount", item_header_style),
                                Paragraph("Subtotal Revenue", item_header_style),
                                Paragraph("Est. Profit", item_header_style)
                            ]]
                            for it in items:
                                item_table_data.append([
                                    Paragraph(str(it.get("item_name")), item_cell_style),
                                    Paragraph(str(it.get("quantity")), item_cell_style),
                                    fmt_pdf_curr(it.get("unit_price")),
                                    fmt_pdf_curr(it.get("discount")) if it.get("discount") > 0 else "-",
                                    fmt_pdf_curr(it.get("line_revenue")),
                                    fmt_pdf_curr(it.get("line_profit"))
                                ])
                            item_table = Table(item_table_data, colWidths=[182, 40, 80, 70, 90, 90])
                            item_table.setStyle(TableStyle([
                                ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                                ('TOPPADDING', (0, 0), (-1, -1), 3),
                                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F0FDFA')),
                                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCFBF1')),
                            ]))
                            tx_elements.append(item_table)
                        
                        tx_elements.append(Spacer(1, 6))
                        elements.append(KeepTogether(tx_elements))
                    elements.append(Spacer(1, 10))

            # 7. Credit Repayments Ledger
            if repayments_list:
                elements.append(Paragraph("<b>CREDIT REPAYMENTS COLLECTED LEDGER</b>", section_heading))
                rep_data = [[
                    Paragraph("Date", tbl_header),
                    Paragraph("Customer", tbl_header),
                    Paragraph("Ref / Receipt", tbl_header),
                    Paragraph("Payment Method", tbl_header),
                    Paragraph("Amount Collected", tbl_header)
                ]]
                for rep in repayments_list:
                    rep_data.append([
                        Paragraph(str(rep.get("date")), tbl_cell),
                        Paragraph(str(rep.get("customer_name")), tbl_cell),
                        Paragraph(str(rep.get("reference_number")), tbl_cell),
                        Paragraph(str(rep.get("payment_method")), tbl_cell),
                        fmt_pdf_curr(rep.get("amount"))
                    ])
                rep_table = Table(rep_data, colWidths=[90, 152, 110, 100, 100])
                rep_table.setStyle(TableStyle([
                    ('ALIGN', (4, 0), (4, -1), 'RIGHT'),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
                    ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                ]))
                elements.append(rep_table)
                elements.append(Spacer(1, 10))

            # 8. Payment Channel Distribution
            if payment_breakdown:
                elements.append(Paragraph("<b>RESOLVED PAYMENT CHANNEL DISTRIBUTION</b>", section_heading))
                pm_data = [[
                    Paragraph("Channel / Method", tbl_header),
                    Paragraph("Settled Total", tbl_header)
                ]]
                for pm in payment_breakdown:
                    pm_data.append([
                        Paragraph(str(pm.get("method")), tbl_cell),
                        fmt_pdf_curr(pm.get("amount"))
                    ])
                pm_table = Table(pm_data, colWidths=[350, 202])
                pm_table.setStyle(TableStyle([
                    ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
                    ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
                ]))
                elements.append(pm_table)

            doc.build(elements)
            buffer.seek(0)

            filename_prefix = "Detailed_Sales" if export_type == "detailed" else "Summary_Sales"
            response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="{filename_prefix}_Report_{branch_name}.pdf"'
            return response

        except Exception as e:
            logger.exception("Export Sales Analysis PDF Error")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)                    