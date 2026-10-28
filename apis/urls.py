from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

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
    UserLoginView,
    ManagementLoginView,
    AdminLoginView,
    ChangePasswordView,
    CustomTokenObtainPairView,
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

# Sales and credit
router.register(r'sales', SaleTransactionViewSet, basename='sale')
router.register(r'credit-sales', CreditRepaymentViewSet, basename='credit-sale')
router.register(r'credit-repayments', CreditRepaymentViewSet, basename='credit-repayment')

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

urlpatterns = [
    # Authentication
    path('auth/login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/user-login/', UserLoginView.as_view(), name='user_login'),
    path('auth/management-login/', ManagementLoginView.as_view(), name='management_login'),
    path('auth/admin-login/', AdminLoginView.as_view(), name='admin_login'),
    path('management/login/', ManagementLoginView.as_view(), name='management-login'),

    # Registration and account management
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/profile/', UserProfileView.as_view(), name='user_profile'),
    path('auth/change-password/', ChangePasswordView.as_view(), name='change_password'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

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