from decimal import Decimal
from django.apps import apps
from django.db.models import Sum, Q
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync


def broadcast_to_branch(branch_id, event, **extra):
    """
    Broadcast an analytics/POS update to a specific branch channel group.
    """
    if not branch_id:
        return

    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    payload = {
        "type": "analytics_update",
        "event": event,
        **extra,
    }

    try:
        async_to_sync(channel_layer.group_send)(
            f"analytics_branch_{branch_id}",
            payload,
        )
    except Exception as e:
        print(f"WebSocket broadcast error for branch {branch_id}: {e}")


@receiver([post_save, post_delete], sender="stocks.Item")
def item_analytics_update(sender, instance, **kwargs):
    """
    Item changes affect inventory/analytics across configured branches.
    Broadcasts to every branch where this item is priced.
    """
    BranchItemPricing = apps.get_model("stocks", "BranchItemPricing")
    branch_ids = (
        BranchItemPricing.objects.filter(item_id=instance.id)
        .values_list("branch_id", flat=True)
        .distinct()
    )

    for branch_id in branch_ids:
        broadcast_to_branch(
            branch_id,
            "DATA_CHANGED",
            item_id=instance.id,
        )


@receiver([post_save, post_delete], sender="stocks.StockMovement")
def stock_movement_analytics_update(sender, instance, **kwargs):
    """
    Broadcast whenever a stock movement changes or is created.
    """
    broadcast_to_branch(
        instance.branch_id,
        "STOCK_MOVEMENT_CHANGED",
        movement_id=instance.id,
        item_id=instance.item_id,
    )


@receiver([post_save, post_delete], sender="stocks.SaleTransaction")
def sale_analytics_update(sender, instance, **kwargs):
    """
    Broadcast whenever a sale transaction is recorded, updated, or removed.
    """
    branch_id = getattr(instance, "branch_id", None)

    if branch_id:
        broadcast_to_branch(
            branch_id,
            "SALE_CHANGED",
            sale_id=instance.id,
        )


@receiver([post_save, post_delete], sender="stocks.PendingApproval")
def pending_approval_analytics_update(sender, instance, **kwargs):
    """
    Broadcasts approval request status changes to POS/Management terminals.
    """
    payload = getattr(instance, "payload", {}) or {}
    branch_id = (
        payload.get("branch_id")
        or payload.get("branch")
        or payload.get("target_branch")
    )

    if isinstance(branch_id, dict):
        branch_id = branch_id.get("id")

    if branch_id:
        broadcast_to_branch(
            branch_id,
            "APPROVAL_UPDATED" if not kwargs.get("created") else "APPROVAL_REQUESTED",
            approval_id=instance.id,
            status=instance.status,
            request_type=instance.request_type,
        )


@receiver(post_delete, sender="stocks.SaleItem")
def remove_stock_movement_on_sale_item_delete(sender, instance, **kwargs):
    """
    Removes the associated StockMovement and restores item stock quantity 
    in BranchItemPricing when a line item is removed.
    """
    transaction_id = getattr(instance, "transaction_id", None)
    item_id = getattr(instance, "item_id", None)
    quantity = getattr(instance, "quantity_sold", None) or getattr(instance, "quantity", 0)

    if transaction_id and item_id:
        StockMovement = apps.get_model("stocks", "StockMovement")
        
        # 1. Delete associated stock movement
        StockMovement.objects.filter(
            item_id=item_id,
            movement_type=StockMovement.MovementType.SALE,
            reference_id=transaction_id,
        ).delete()

        # 2. Add deleted quantity back to BranchItemPricing remaining_stock
        BranchItemPricing = apps.get_model("stocks", "BranchItemPricing")
        branch_id = getattr(instance.transaction, "branch_id", None)

        if branch_id:
            pricing = BranchItemPricing.objects.filter(
                item_id=item_id, 
                branch_id=branch_id
            ).first()
            if pricing and hasattr(pricing, "remaining_stock"):
                pricing.remaining_stock = Decimal(str(pricing.remaining_stock or 0)) + Decimal(str(quantity))
                pricing.save(update_fields=["remaining_stock"])


@receiver([post_save, post_delete], sender="stocks.Expense")
def expense_analytics_update(sender, instance, **kwargs):
    """
    Broadcast whenever an expense changes.
    """
    branch_id = getattr(instance, "branch_id", None)

    if branch_id:
        broadcast_to_branch(
            branch_id,
            "EXPENSE_CHANGED",
            expense_id=instance.id,
        )


@receiver([post_save, post_delete], sender="stocks.CreditRepayment")
def update_transaction_payment_status(sender, instance, **kwargs):
    SaleTransaction = apps.get_model("stocks", "SaleTransaction")
    CreditRepayment = apps.get_model("stocks", "CreditRepayment")

    # 1. Resolve target SaleTransaction
    transaction = getattr(instance, "transaction", None)
    if not transaction:
        ref = getattr(instance, "reference", None) or getattr(instance, "notes", None)
        if ref:
            normalized_ref = str(ref).replace("_", ".")
            transaction = SaleTransaction.objects.filter(
                Q(receipt_number__iexact=ref) | Q(receipt_number__iexact=normalized_ref)
            ).first()

    if not transaction:
        return

    # 2. Calculate sum of all repayments for this transaction
    repayments = CreditRepayment.objects.filter(transaction=transaction)
    total_repaid = repayments.aggregate(total=Sum("amount"))["total"] or 0.0

    # 3. Update status and payment method
    transaction.amount_paid = total_repaid
    is_fully_paid = Decimal(str(total_repaid)) >= Decimal(str(transaction.total_amount or 0))
    transaction.is_paid = is_fully_paid

    if is_fully_paid:
        latest_repayment = repayments.order_by("-id").first()
        repayment_method = getattr(latest_repayment, "payment_method", None) or getattr(instance, "payment_method", None)
        
        if repayment_method:
            transaction.payment_method = repayment_method

    transaction.save(update_fields=["amount_paid", "is_paid", "payment_method"])