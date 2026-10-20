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
    CustomTokenObtainPairView,
    RegisterView,
    UserProfileView,
    CustomerViewSet,
    CashbookViewSet,
    BalanceSheetViewSet,
    FinancialReportViewSet,
    ManagementAnalyticsViewSet,
    ManagementReportsViewSet,
)

router = DefaultRouter()
router.register(r'items', ItemViewSet, basename='item')
router.register(r'purchases', PurchaseViewSet, basename='purchase')
router.register(r'supplier-payments', SupplierPaymentViewSet, basename='supplier-payment')
router.register(r'sales', SaleTransactionViewSet, basename='sale')
router.register(r'expenses', ExpenseViewSet, basename='expense')
router.register(r'reports', FinancialReportViewSet, basename='reports')
router.register(r'branches', RetailBranchViewSet, basename='branch')
router.register(r'customers', CustomerViewSet, basename='customer')
router.register(r'stock-movements', StockMovementViewSet, basename='stock-movement')
router.register(r'management-analytics', ManagementAnalyticsViewSet, basename='management-analytics')
router.register(r'credit-sales', CreditRepaymentViewSet, basename='credit-sale')
router.register(r'credit-repayments', CreditRepaymentViewSet, basename='credit-repayment')
router.register(r'financial-reports', FinancialReportViewSet, basename='financial-reports')
router.register(r'pending-approvals', PendingApprovalViewSet, basename='pending-approval')
router.register(r"cashbook", CashbookViewSet, basename="cashbook")
router.register(r"balance-sheet", BalanceSheetViewSet, basename="balance-sheet")
router.register(r'management-reports', ManagementReportsViewSet, basename='management-reports')

urlpatterns = [
    # Separate Portal Login Endpoints
    path('auth/login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/user-login/', UserLoginView.as_view(), name='user_login'),
    path('auth/management-login/', ManagementLoginView.as_view(), name='management_login'),
    path('auth/admin-login/', AdminLoginView.as_view(), name='admin_login'),

    # Profile and Token Refresh
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/profile/', UserProfileView.as_view(), name='user_profile'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('management/login/', ManagementLoginViewSet.as_view({'post': 'login'}), name='management-login'),

    # Custom endpoints
    path('branches/copy-items/', CopyBranchItemsView.as_view(), name='branch-copy-items'),

    # Router endpoints
    path('', include(router.urls)),
]