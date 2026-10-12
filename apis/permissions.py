from rest_framework import permissions
from stocks.models import UserRole


def get_user_role(user):
    """Helper function to safely extract the user's role from their profile."""
    if hasattr(user, 'profile'):
        return user.profile.role
    return None


class IsManagerOrReadOnly(permissions.BasePermission):
    """
    Custom permission:
    - Anyone authenticated can read or log daily sales transactions.
    - Only staff, superusers, or Store Admins can create/modify inventory items.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Allow SAFE_METHODS (GET, HEAD, OPTIONS) for authenticated users
        if request.method in permissions.SAFE_METHODS:
            return True

        # Allow any authenticated user to create sales transactions
        if getattr(view, 'basename', None) == 'sale' or getattr(view, 'basename', None) == 'saletransaction':
            return True

        # Restrict structural modifications (POST/PUT/DELETE on Items) to staff or Store Admins
        role = get_user_role(request.user)
        return request.user.is_staff or request.user.is_superuser or role in [UserRole.SUPER_ADMIN, UserRole.STORE_ADMIN]


class IsFinancialAuditorOrManager(permissions.BasePermission):
    """
    Custom permission for financial reporting and expense management:
    - Super Admins, Store Admins, and Accountants have access.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        if request.user.is_staff or request.user.is_superuser:
            return True

        role = get_user_role(request.user)
        return role in [UserRole.SUPER_ADMIN, UserRole.STORE_ADMIN, UserRole.ACCOUNTANT]