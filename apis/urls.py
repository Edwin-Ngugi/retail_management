from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework import permissions

from .views import (
    ItemViewSet,
    PurchaseViewSet,
    SupplierPaymentViewSet,
    SaleTransactionViewSet,
    CreditRepaymentViewSet,
    ExpenseViewSet,
    FinancialReportViewSet,
    RetailBranchViewSet,
    StockMovementViewSet,
    PendingApprovalViewSet,
    CopyBranchItemsView,
    CustomTokenObtainPairView,
    LogoutViewSet,
    ChangePasswordView,
    RegisterView,
    UserProfileView,
    CustomerViewSet,
    CashbookViewSet,
    BalanceSheetViewSet,
    ManagementAnalyticsViewSet,
    DashboardOverviewAPIView,
    ManagementReportsViewSet,
    RequestOTPView,
    VerifyOTPAndResetPassword,
    AdminAuthViewSet,
    DetailedSalesAnalysisViewSet,
    PurchaseAnalyticsViewSet,

    # User roles and permissions
    UserPermissionViewSet,
)

router = DefaultRouter()

# Inventory and stock
router.register(r'items', ItemViewSet, basename='item')
router.register(r'stock-movements', StockMovementViewSet, basename='stock-movement')

# Purchasing
router.register(r'purchases', PurchaseViewSet, basename='purchase')
router.register(r'supplier-payments', SupplierPaymentViewSet, basename='supplier-payment')
router.register(r'purchases-analytics', PurchaseAnalyticsViewSet, basename='purchases-analytics')

# Sales and credit
router.register(r'sales', SaleTransactionViewSet, basename='sale')
router.register(r'credit-sales', CreditRepaymentViewSet, basename='credit-sale')
router.register(r'credit-repayments', CreditRepaymentViewSet, basename='credit-repayment')
router.register(r'sales-analysis-detailed', DetailedSalesAnalysisViewSet, basename='sales-analysis-detailed')

# Business operations
router.register(r'expenses', ExpenseViewSet, basename='expense')
router.register(r'customers', CustomerViewSet, basename='customer')
router.register(r'branches', RetailBranchViewSet, basename='branch')
router.register(r'pending-approvals', PendingApprovalViewSet, basename='pending-approval')

# Financial reporting
router.register(r'reports', FinancialReportViewSet, basename='reports')
router.register(r'financial-reports', FinancialReportViewSet, basename='financial-reports')
router.register(r'cashbook', CashbookViewSet, basename='cashbook')
router.register(r'balance-sheet', BalanceSheetViewSet, basename='balance-sheet')

# Management
router.register(r'management-analytics', ManagementAnalyticsViewSet, basename='management-analytics')
router.register(r'management-reports', ManagementReportsViewSet, basename='management-reports')

# User roles and permissions
router.register(r'user-permissions', UserPermissionViewSet, basename='user-permissions')

router.register(r'admin-auth', AdminAuthViewSet, basename='admin-auth')

urlpatterns = [
    # Unified Authentication & JWT (Handles both /login/ and /auth/login/)
    path('login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair_alias'),
    path('auth/login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/logout/', LogoutViewSet.as_view({'post': 'logout'}), name='auth_logout'),


    # Registration and account management
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/profile/', UserProfileView.as_view(), name='user_profile'),
    path('auth/change-password/', ChangePasswordView.as_view(), name='change_password'),

    # Password reset
    path('auth/request-otp/', RequestOTPView.as_view(), name='request_otp'),
    path('auth/verify-otp-reset/', VerifyOTPAndResetPassword.as_view(), name='verify_otp_reset'),

    # Dashboard
    path('dashboard/overview/', DashboardOverviewAPIView.as_view(), name='dashboard-overview'),

    # Branch utilities
    path('branches/copy-items/', CopyBranchItemsView.as_view(), name='branch-copy-items'),

    # All registered API endpoints
    path('', include(router.urls)),
]