from decimal import Decimal
from django.apps import apps
from django.db.models import Sum, Q
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync


def broadcast_to_branch(branch_id, event, **extra):
    """
    Broadcast an analytics/POS update to branch channel groups.
    Sends to both analytics_branch_<id> and branch_<id> for frontend compatibility.
    """
    if not branch_id:
        return

    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    payload = {
        "type": "analytics_update",
        "action": event,
        "event": event,
        "branch_id": branch_id,
        **extra,
    }

    # Target channel groups
    target_groups = [
        f"analytics_branch_{branch_id}",
        f"branch_{branch_id}"
    ]

    for group_name in target_groups:
        try:
            async_to_sync(channel_layer.group_send)(
                group_name,
                payload,
            )
        except Exception as e:
            print(f"WebSocket broadcast error for group {group_name}: {e}")


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
            item_name=instance.name,
        )


@receiver([post_save, post_delete], sender="stocks.StockMovement")
def stock_movement_analytics_update(sender, instance, **kwargs):
    """
    Broadcast whenever a stock movement changes, created, or deleted.
    Calculates current stock balance, updates BranchStock, and transmits live to subscribers.
    """
    branch_id = getattr(instance, "branch_id", None)
    item_id = getattr(instance, "item_id", None)

    if not branch_id or not item_id:
        return

    new_stock = 0.0
    item_name = ""

    try:
        BranchItemPricing = apps.get_model("stocks", "BranchItemPricing")
        BranchStock = apps.get_model("stocks", "BranchStock")

        # 1. Fetch recalculated balance from BranchItemPricing property
        pricing = BranchItemPricing.objects.filter(branch_id=branch_id, item_id=item_id).first()
        if pricing:
            new_stock = float(getattr(pricing, "remaining_stock", 0) or 0)

        # 2. Sync to BranchStock for O(1) indexed database lookups
        BranchStock.objects.update_or_create(
            branch_id=branch_id,
            item_id=item_id,
            defaults={"quantity": Decimal(str(new_stock))}
        )

        item_name = getattr(instance.item, "name", "")
    except Exception as e:
        print(f"Error updating live stock level for broadcast: {e}")

    broadcast_to_branch(
        branch_id,
        "STOCK_UPDATED",
        movement_id=instance.id,
        item_id=item_id,
        item_name=item_name,
        new_stock=new_stock,
        remaining_stock=new_stock,
        movement_type=getattr(instance, "movement_type", None),
        quantity=float(getattr(instance, "quantity", 0) or 0),
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
            total_amount=float(getattr(instance, "total_amount", 0) or 0),
            payment_method=getattr(instance, "payment_method", "CASH"),
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
    Removes the associated StockMovement when a SaleItem line item is deleted.
    Deleting the outbound SALE movement automatically restores remaining_stock
    without modifying opening_stock.
    """
    transaction_id = getattr(instance, "transaction_id", None)
    item_id = getattr(instance, "item_id", None)

    if transaction_id and item_id:
        StockMovement = apps.get_model("stocks", "StockMovement")
        
        # 1. Delete associated stock movement
        StockMovement.objects.filter(
            item_id=item_id,
            movement_type=StockMovement.MovementType.SALE,
            reference_id=transaction_id,
        ).delete()

        # 2. Recalculate WAC safely
        try:
            branch = getattr(instance.transaction, "branch", None)
            item = getattr(instance, "item", None)
            if branch and item:
                from stocks.models import recalculate_wac_sales_cost
                recalculate_wac_sales_cost(branch, item)
        except Exception as e:
            print(f"Error recalculating WAC on SaleItem delete: {e}")


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
            amount=float(getattr(instance, "amount", 0) or 0),
        )


@receiver([post_save, post_delete], sender="stocks.CreditRepayment")
def update_transaction_payment_status(sender, instance, **kwargs):
    """
    Updates parent SaleTransaction status and broadcasts repayment changes.
    """
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

    # 4. Broadcast live credit update over WebSocket
    branch_id = getattr(transaction, "branch_id", None)
    if branch_id:
        broadcast_to_branch(
            branch_id,
            "CREDIT_REPAYMENT_UPDATED",
            transaction_id=transaction.id,
            amount_paid=float(total_repaid),
            is_paid=is_fully_paid,
        )