from decimal import Decimal
from datetime import date, datetime

from django.db import models, transaction
from django.db.models import Sum, F, ExpressionWrapper, DecimalField, Q
from django.utils import timezone
from django.db.models.functions import Coalesce
from django.apps import apps
from stocks.models import recalculate_wac_sales_cost, quantize_money

ZERO = Decimal("0.00")


def get_stock_model(model_name):
    return apps.get_model("stocks", model_name)


def _calculate_payment_breakdown(sales_qs, repayments_qs=None):
    """
    Calculates exact cash collected across payment methods without double counting.
    Respects date & branch filters from both sales_qs and repayments_qs.
    """
    SalePayment = get_stock_model("SalePayment")
    CreditRepayment = get_stock_model("CreditRepayment")

    payments = {
        'cash': ZERO,
        'credit_repayments': ZERO,
        'paybill': ZERO,
        'send_money': ZERO,
        'bank_transfer': ZERO,
    }

    def route_payment(raw_method, amount):
        if not amount or amount <= ZERO:
            return

        method = (
            str(raw_method or "")
            .strip()
            .upper()
            .replace(" ", "_")
            .replace("-", "_")
            .replace("/", "_")
        )

        if method in ["PAYBILL", "MPESA_PAYBILL", "PAY_BILL", "C2B", "PB"]:
            payments['paybill'] += amount
        elif method in ["SEND_MONEY", "MPESA_SEND_MONEY", "SENDMONEY", "MOBILE_MONEY", "MPESA", "SM", "M_PESA", "MOBILE"]:
            payments['send_money'] += amount
        elif method in ["CASH", "MONEY", "CURRENCY"]:
            payments['cash'] += amount
        elif method in ["BANK_TRANSFER", "BANK", "TRANSFER", "BANK_PAYMENT"]:
            payments['bank_transfer'] += amount
        elif method in ["SPLIT", "CREDIT"]:
            return
        else:
            if "PAYBILL" in method:
                payments['paybill'] += amount
            elif "BANK" in method:
                payments['bank_transfer'] += amount
            elif "CASH" in method:
                payments['cash'] += amount
            else:
                payments['send_money'] += amount

    # 1. Exclude direct credit sales (uncollected debt)
    non_credit_sales_qs = sales_qs.exclude(payment_method="CREDIT")

    # 2. Process SalePayments (Split Breakdown Table)
    sp_fields = [f.name for f in SalePayment._meta.get_fields()]
    fk_field = (
        "transaction" if "transaction" in sp_fields
        else ("sale_transaction" if "sale_transaction" in sp_fields else "sale")
    )

    split_payments = SalePayment.objects.filter(**{f"{fk_field}__in": non_credit_sales_qs})
    split_transaction_ids = set()

    if split_payments.exists():
        for p in split_payments:
            raw_amt = getattr(p, "amount", None)
            if raw_amt is None:
                raw_amt = getattr(p, "amount_paid", ZERO)

            amt = Decimal(str(raw_amt or ZERO))
            route_payment(getattr(p, "method", "CASH"), amt)

            tx_id = getattr(p, f"{fk_field}_id", None)
            if tx_id:
                split_transaction_ids.add(tx_id)

    # 3. Process Direct Sales (Excluding transactions already covered by SalePayment)
    remaining_sales = non_credit_sales_qs.exclude(id__in=split_transaction_ids)

    for sale in remaining_sales:
        pm = str(getattr(sale, "payment_method", "")).strip().upper()

        if pm == "SPLIT":
            cash_amt = Decimal(str(getattr(sale, "cash_amount", 0) or 0))
            mpesa_amt = Decimal(str(getattr(sale, "mpesa_amount", 0) or getattr(sale, "send_money_amount", 0) or 0))
            paybill_amt = Decimal(str(getattr(sale, "paybill_amount", 0) or 0))
            bank_amt = Decimal(str(getattr(sale, "bank_amount", 0) or 0))

            details = (
                getattr(sale, "payment_details", None)
                or getattr(sale, "split_breakdown", None)
                or getattr(sale, "split_payments", None)
                or {}
            )
            if isinstance(details, dict) and details:
                cash_amt = cash_amt or Decimal(str(details.get("cash", 0) or 0))
                mpesa_amt = mpesa_amt or Decimal(str(details.get("mpesa", 0) or details.get("send_money", 0) or 0))
                paybill_amt = paybill_amt or Decimal(str(details.get("paybill", 0) or 0))
                bank_amt = bank_amt or Decimal(str(details.get("bank", 0) or 0))

            payments['cash'] += cash_amt
            payments['send_money'] += mpesa_amt
            payments['paybill'] += paybill_amt
            payments['bank_transfer'] += bank_amt
        else:
            amt = Decimal(str(sale.total_amount or ZERO))
            route_payment(pm, amt)

    # 4. Respect provided repayments_qs with filters applied
    if repayments_qs is None:
        repayments_qs = CreditRepayment.objects.all()
        branch_ids = sales_qs.values_list('branch_id', flat=True).distinct()
        if branch_ids:
            repayments_qs = repayments_qs.filter(branch_id__in=branch_ids)

    repayment_total = repayments_qs.aggregate(
        total=Coalesce(Sum("amount"), ZERO)
    )["total"]

    payments['credit_repayments'] = Decimal(str(repayment_total or ZERO))

    return payments


@transaction.atomic
def process_checkout(branch, customer, payment_method, cart_items, user, split_payments_data=None):
    SaleTransaction = get_stock_model("SaleTransaction")
    SalePayment = get_stock_model("SalePayment")
    BranchItemPricing = get_stock_model("BranchItemPricing")
    StockBatch = get_stock_model("StockBatch")
    SaleItem = get_stock_model("SaleItem")

    sale_tx = SaleTransaction.objects.create(
        branch=branch,
        customer=customer,
        payment_method=payment_method,
        created_by=user,
    )

    if split_payments_data and isinstance(split_payments_data, list):
        sp_fields = [f.name for f in SalePayment._meta.get_fields()]
        fk_field = (
            "transaction" if "transaction" in sp_fields
            else ("sale_transaction" if "sale_transaction" in sp_fields else "sale")
        )
        
        for p_data in split_payments_data:
            if isinstance(p_data, dict):
                p_method = p_data.get("method") or p_data.get("payment_method") or "CASH"
                p_amount = Decimal(str(p_data.get("amount") or p_data.get("amount_paid") or ZERO))
                
                if p_amount > ZERO:
                    SalePayment.objects.create(**{
                        fk_field: sale_tx,
                        "method": p_method,
                        "amount": p_amount
                    })

    today = timezone.localdate()

    for item_data in cart_items:
        item = item_data['item']
        qty_needed = Decimal(str(item_data['quantity']))
        price = Decimal(str(item_data['unit_price']))

        pricing = BranchItemPricing.objects.filter(branch=branch, item=item).first()

        total_allocated_cost = ZERO
        remaining_to_deduct = qty_needed

        if pricing:
            active_batches = StockBatch.objects.filter(
                branch=branch,
                item=item,
                quantity_remaining__gt=0
            ).order_by('received_at', 'id')

            for batch in active_batches:
                if remaining_to_deduct <= ZERO:
                    break

                batch_qty = Decimal(str(batch.quantity_remaining or 0))
                deduct_qty = min(batch_qty, remaining_to_deduct)

                batch.quantity_remaining = batch_qty - deduct_qty
                batch.save(update_fields=['quantity_remaining'])

                total_allocated_cost += deduct_qty * Decimal(str(batch.unit_cost_price or ZERO))
                remaining_to_deduct -= deduct_qty

        allocated_qty = qty_needed - remaining_to_deduct
        if qty_needed > ZERO and allocated_qty > ZERO:
            unit_cost = quantize_money(total_allocated_cost / allocated_qty)
        else:
            unit_cost = BranchItemPricing.calculate_wac(
                branch, item, year=today.year, month=today.month
            )

        SaleItem.objects.create(
            transaction=sale_tx,
            item=item,
            quantity_sold=qty_needed,
            unit_selling_price=price,
            unit_cost_price=unit_cost,
        )

        recalculate_wac_sales_cost(branch, item)

    return sale_tx


class InventoryService:

    @staticmethod
    def get_movement_unit_cost(movement):
        BranchItemPricing = get_stock_model("BranchItemPricing")

        m_cost = getattr(movement, 'unit_cost_price', None)
        if m_cost is not None and Decimal(str(m_cost)) > ZERO:
            return quantize_money(m_cost)

        batch = getattr(movement, 'batch', None) or getattr(movement, 'stock_batch', None)
        if batch and getattr(batch, 'unit_cost_price', None) and Decimal(str(batch.unit_cost_price)) > ZERO:
            return quantize_money(batch.unit_cost_price)

        dt = getattr(movement, 'created_at', None) or timezone.now()
        wac = BranchItemPricing.calculate_wac(
            branch=movement.branch,
            item=movement.item,
            year=dt.year,
            month=dt.month
        )
        if wac and wac > ZERO:
            return quantize_money(wac)

        pricing = BranchItemPricing.objects.filter(branch=movement.branch, item=movement.item).first()
        if pricing and getattr(pricing, 'unit_cost_price', None) and Decimal(str(pricing.unit_cost_price)) > ZERO:
            return quantize_money(pricing.unit_cost_price)

        return ZERO

    @staticmethod
    def calculate_movement_total_cost(movement):
        quantity = Decimal(str(abs(getattr(movement, 'quantity', 0) or 0)))
        unit_cost = InventoryService.get_movement_unit_cost(movement)
        return quantize_money(quantity * unit_cost)

    @staticmethod
    @transaction.atomic
    def record_purchased_batch(branch, item, quantity, unit_cost_price, purchase_date=None, supplier=None, supplier_name="", user=None):
        Purchase = get_stock_model("Purchase")
        
        if purchase_date is None:
            purchase_date = timezone.now()

        purchase = Purchase.objects.create(
            branch=branch,
            item=item,
            supplier=supplier,
            supplier_name=supplier_name,
            quantity=Decimal(str(quantity)),
            unit_cost_price=quantize_money(Decimal(str(unit_cost_price))),
            purchase_date=purchase_date,
            created_by=user,
        )

        recalculate_wac_sales_cost(branch, item)
        return purchase

    @staticmethod
    @transaction.atomic
    def record_stock_adjustment_in(branch, item, quantity, unit_cost_price=None, user=None, reason=""):
        StockMovement = get_stock_model("StockMovement")
        BranchItemPricing = get_stock_model("BranchItemPricing")

        today = timezone.localdate()

        if unit_cost_price is None or Decimal(str(unit_cost_price)) <= ZERO:
            unit_cost_price = BranchItemPricing.calculate_wac(
                branch, item, year=today.year, month=today.month
            )

        unit_cost = quantize_money(Decimal(str(unit_cost_price or ZERO)))

        movement = StockMovement.objects.create(
            branch=branch,
            item=item,
            quantity=abs(Decimal(str(quantity))),
            movement_type=StockMovement.MovementType.ADJUSTMENT_IN,
            unit_cost_price=unit_cost,
            created_by=user,
            notes=reason if hasattr(StockMovement, 'notes') else ""
        )

        Purchase = get_stock_model("Purchase")
        Purchase.objects.create(
            branch=branch,
            item=item,
            quantity=abs(Decimal(str(quantity))),
            unit_cost_price=unit_cost,
            purchase_date=timezone.now(),
            created_by=user,
            supplier_name=f"Stock Adjustment ({reason})" if reason else "Stock Adjustment In"
        )

        recalculate_wac_sales_cost(branch, item)
        return movement

    @staticmethod
    @transaction.atomic
    def process_sale_and_update_wac(sale_transaction_or_pricing, quantity_sold=0):
        SaleTransaction = get_stock_model("SaleTransaction")

        if isinstance(sale_transaction_or_pricing, SaleTransaction):
            sale_transaction = sale_transaction_or_pricing
            branch = sale_transaction.branch

            for item_detail in sale_transaction.items.all():
                recalculate_wac_sales_cost(branch, item_detail.item)

            return getattr(sale_transaction, 'branch', None)

        branch_item_pricing = sale_transaction_or_pricing
        if Decimal(str(quantity_sold or 0)) <= ZERO or not branch_item_pricing:
            return branch_item_pricing

        recalculate_wac_sales_cost(branch_item_pricing.branch, branch_item_pricing.item)
        branch_item_pricing.refresh_from_db()
        return branch_item_pricing


class AnalyticsService:

    @staticmethod
    def _resolve_branch(branch):
        if not branch:
            return None
        RetailBranch = get_stock_model("RetailBranch")
        if isinstance(branch, RetailBranch):
            return branch
        try:
            if str(branch).isdigit():
                return RetailBranch.objects.filter(id=int(branch)).first()
        except (ValueError, TypeError):
            pass
        return RetailBranch.objects.filter(name__iexact=str(branch)).first()

    @staticmethod
    def generate_comprehensive_report(branch_id, start_date, end_date):
        SaleTransaction = get_stock_model("SaleTransaction")
        CreditRepayment = get_stock_model("CreditRepayment")
        SaleItem = get_stock_model("SaleItem")
        BranchItemPricing = get_stock_model("BranchItemPricing")

        resolved_branch = AnalyticsService._resolve_branch(branch_id)
        sales_qs = SaleTransaction.objects.all()
        repayments_qs = CreditRepayment.objects.all()

        if resolved_branch:
            sales_qs = sales_qs.filter(branch_id=resolved_branch.id)
            repayments_qs = repayments_qs.filter(branch_id=resolved_branch.id)

        if start_date and end_date:
            sales_qs = sales_qs.filter(
                Q(sale_date__range=[start_date, end_date]) |
                Q(created_at__range=[start_date, end_date])
            )
            repayments_qs = repayments_qs.filter(
                Q(date__range=[start_date, end_date]) |
                Q(created_at__range=[start_date, end_date])
            )

        rev, cogs = AnalyticsService._calculate_sales_totals(sales_qs)
        net_profit = rev - cogs

        payments = _calculate_payment_breakdown(sales_qs, repayments_qs=repayments_qs)

        fast_moving_qs = (
            SaleItem.objects.filter(transaction__in=sales_qs)
            .values('item__name')
            .annotate(
                total_qty=Sum('quantity_sold'),
                total_rev=Sum(
                    ExpressionWrapper(
                        F('quantity_sold') * (F('unit_selling_price') - F('discount_per_item')),
                        output_field=DecimalField()
                    )
                )
            )
            .order_by('-total_qty')[:5]
        )
        
        fast_moving = [
            {
                "product__name": row['item__name'],
                "total_qty": float(row['total_qty'] or 0),
                "total_rev": float(row['total_rev'] or 0.0)
            }
            for row in fast_moving_qs
        ]

        pricing_qs = BranchItemPricing.objects.all()
        if resolved_branch:
            pricing_qs = pricing_qs.filter(branch_id=resolved_branch.id)

        filter_kwargs = {}
        if start_date and end_date:
            filter_kwargs['item__sale_items__transaction__sale_date__range'] = [start_date, end_date]

        slow_moving_qs = (
            pricing_qs.annotate(
                sold=Coalesce(Sum('item__sale_items__quantity_sold', filter=Q(**filter_kwargs)), ZERO)
            )
            .order_by('sold')[:5]
            .values('item__name', 'sold', 'branch_id')
        )
        
        slow_moving = []
        for row in slow_moving_qs:
            b_pricing = BranchItemPricing.objects.filter(
                branch_id=row['branch_id'], item__name=row['item__name']
            ).first()
            slow_moving.append({
                "name": row['item__name'],
                "sold": float(row['sold'] or 0),
                "current_stock": float(b_pricing.remaining_stock) if b_pricing else 0.0
            })

        insights = []
        if net_profit < ZERO:
            insights.append({
                "type": "critical",
                "title": "Margin Pressure Alert",
                "advice": "Cost of goods exceeds current revenue. Review supplier pricing or re-evaluate low-margin promotional items."
            })
        
        total_credit_invoiced = sales_qs.filter(payment_method='CREDIT').aggregate(total=Coalesce(Sum('total_amount'), ZERO))['total']
        if rev > ZERO and total_credit_invoiced > (rev * Decimal('0.30')):
            insights.append({
                "type": "warning",
                "title": "High Credit Exposure",
                "advice": "Credit sales represent over 30% of total revenue. Implement stricter credit terms to safeguard liquidity."
            })
            
        if fast_moving:
            top_seller = fast_moving[0]['product__name']
            insights.append({
                "type": "success",
                "title": "Stock Availability Focus",
                "advice": f"'{top_seller}' is your highest-velocity item. Ensure safety stock buffers are increased to prevent stockouts."
            })

        return {
            "financials": {
                "revenue": float(rev),
                "cogs": float(cogs),
                "net_profit": float(net_profit)
            },
            "payments": {k: float(v) for k, v in payments.items()},
            "fast_moving": fast_moving,
            "slow_moving": slow_moving,
            "insights": insights
        }

    @staticmethod
    def recalculate_wac_sales_cost(branch, item):
        return recalculate_wac_sales_cost(branch, item)

    @staticmethod
    def get_current_wac(item, branch=None, year=None, month=None):
        if not item:
            return ZERO
        resolved_branch = AnalyticsService._resolve_branch(branch)
        if not resolved_branch:
            return ZERO
        BranchItemPricing = get_stock_model("BranchItemPricing")
        return BranchItemPricing.calculate_wac(resolved_branch, item, year=year, month=month)

    @staticmethod
    def calculate_weighted_stock_value(pricing, year=None, month=None):
        BranchItemPricing = get_stock_model("BranchItemPricing")

        if not pricing:
            return 0, ZERO
        remaining_qty = pricing.remaining_stock
        current_wac = BranchItemPricing.calculate_wac(pricing.branch, pricing.item, year=year, month=month)
        stock_value = Decimal(str(remaining_qty)) * current_wac
        return remaining_qty, quantize_money(stock_value)

    @staticmethod
    def get_item_metrics(item, branch_id=None, year=None, month=None):
        BranchItemPricing = get_stock_model("BranchItemPricing")
        StockMovement = get_stock_model("StockMovement")

        empty_metrics = {
            "opening_stock": 0.0,
            "total_restocked": 0.0,
            "total_sold": 0.0,
            "remaining_stock": 0.0,
            "unit_cost_price": 0.0,
            "weighted_unit_cost": 0.0,
            "remaining_stock_value": 0.0,
        }

        if not item:
            return empty_metrics

        resolved_branch = AnalyticsService._resolve_branch(branch_id) if branch_id else None
        if not resolved_branch:
            return empty_metrics

        pricing = BranchItemPricing.objects.filter(branch=resolved_branch, item=item).first()
        if not pricing:
            return empty_metrics

        opening_stock = Decimal(str(pricing.get_effective_opening_stock(year=year, month=month) or 0))
        movements = StockMovement.objects.filter(branch=resolved_branch, item=item)

        if year and month:
            movements = movements.filter(
                Q(created_at__year=year, created_at__month=month) |
                Q(date__year=year, date__month=month)
            )

        inbound_types = {
            StockMovement.MovementType.PURCHASE,
            StockMovement.MovementType.CUSTOMER_RETURN,
            StockMovement.MovementType.TRANSFER_IN,
            StockMovement.MovementType.ADJUSTMENT_IN,
        }

        outbound_types = {
            StockMovement.MovementType.SALE,
            StockMovement.MovementType.SUPPLIER_RETURN,
            StockMovement.MovementType.TRANSFER_OUT,
            StockMovement.MovementType.DAMAGE,
            StockMovement.MovementType.ADJUSTMENT_OUT,
        }

        total_restocked = ZERO
        total_sold = ZERO

        for movement in movements:
            quantity = Decimal(str(abs(movement.quantity or 0)))
            if movement.movement_type in inbound_types:
                total_restocked += quantity
            elif movement.movement_type in outbound_types:
                if movement.movement_type == StockMovement.MovementType.SALE:
                    total_sold += quantity

        remaining_stock = Decimal(str(pricing.remaining_stock or 0))
        weighted_cost = BranchItemPricing.calculate_wac(resolved_branch, item, year=year, month=month)
        remaining_value = quantize_money(remaining_stock * weighted_cost)

        return {
            "opening_stock": float(opening_stock),
            "total_restocked": float(total_restocked),
            "total_sold": float(total_sold),
            "remaining_stock": float(remaining_stock),
            "unit_cost_price": float(weighted_cost),
            "weighted_unit_cost": float(weighted_cost),
            "remaining_stock_value": float(remaining_value),
        }

    @staticmethod
    def _calculate_sales_totals(sales):
        SaleItem = get_stock_model("SaleItem")
        BranchItemPricing = get_stock_model("BranchItemPricing")

        total_revenue = sales.aggregate(total=Coalesce(Sum("total_amount"), ZERO))["total"]
        
        sale_items = SaleItem.objects.filter(transaction__in=sales)
        calculated_revenue = sale_items.aggregate(
            rev=Coalesce(
                Sum(
                    ExpressionWrapper(
                        F("quantity_sold") * (F("unit_selling_price") - Coalesce(F("discount_per_item"), ZERO)),
                        output_field=DecimalField(),
                    )
                ),
                ZERO,
            )
        )["rev"]

        if not total_revenue or total_revenue == ZERO:
            total_revenue = calculated_revenue

        total_cogs = ZERO
        for item in sale_items.select_related("item", "transaction__branch"):
            unit_cost = item.unit_cost_price or ZERO
            if unit_cost <= ZERO and item.item and item.transaction and item.transaction.branch:
                dt = getattr(item.transaction, 'sale_date', None) or getattr(item.transaction, 'created_at', None) or timezone.now()
                unit_cost = BranchItemPricing.calculate_wac(
                    item.transaction.branch,
                    item.item,
                    year=dt.year,
                    month=dt.month,
                )
            total_cogs += Decimal(str(item.quantity_sold or 0)) * Decimal(str(unit_cost or ZERO))

        return quantize_money(total_revenue), quantize_money(total_cogs)

    @staticmethod
    def _get_items_sold(sales, branch_id=None, year=None, month=None):
        SaleItem = get_stock_model("SaleItem")
        Item = get_stock_model("Item")

        sale_items = SaleItem.objects.filter(transaction__in=sales)

        aggregated_sales = (
            sale_items
            .values("item_id", "item__name")
            .annotate(
                total_qty=Sum("quantity_sold"),
                total_revenue=Sum(
                    ExpressionWrapper(
                        F("quantity_sold") * (F("unit_selling_price") - Coalesce(F("discount_per_item"), ZERO)),
                        output_field=DecimalField(),
                    )
                ),
                total_cogs=Sum(
                    ExpressionWrapper(
                        F("quantity_sold") * F("unit_cost_price"),
                        output_field=DecimalField(),
                    )
                ),
            )
        )

        items_sold = []
        for row in aggregated_sales:
            qty = Decimal(str(row["total_qty"] or 0))
            revenue = Decimal(str(row["total_revenue"] or ZERO))
            cogs = Decimal(str(row["total_cogs"] or ZERO))
            profit = revenue - cogs

            item_id = row.get("item_id")
            item = Item.objects.filter(id=item_id).first() if item_id else None

            if item:
                metrics = AnalyticsService.get_item_metrics(item, branch_id=branch_id, year=year, month=month)
            else:
                metrics = {
                    "opening_stock": 0.0,
                    "remaining_stock": 0.0,
                    "remaining_stock_value": 0.0,
                }

            items_sold.append({
                "id": item_id,
                "name": row.get("item__name", "Unknown"),
                "units_sold": float(qty),
                "quantity_sold": float(qty),
                "revenue": float(revenue),
                "cogs": float(cogs),
                "profit": float(profit),
                "opening_stock": metrics["opening_stock"],
                "remaining_stock": metrics["remaining_stock"],
                "remaining_stock_value": float(metrics["remaining_stock_value"]),
            })

        return items_sold

    @staticmethod
    def calculate_summary_pnl(
        user=None,
        period_type="day",
        branch=None,
        target_date=None,
        tax_rate=None,
    ):
        SaleTransaction = get_stock_model("SaleTransaction")
        Expense = get_stock_model("Expense")
        CreditRepayment = get_stock_model("CreditRepayment")

        if tax_rate is None:
            tax_rate = ZERO

        resolved_branch = AnalyticsService._resolve_branch(branch)
        sales = SaleTransaction.objects.all()
        expenses = Expense.objects.all()
        repayments = CreditRepayment.objects.all()

        if user is not None and getattr(user, "is_authenticated", False):
            sales = sales.filter(branch__retailer=user)
            expenses = expenses.filter(retailer=user)
            repayments = repayments.filter(branch__retailer=user)

        if resolved_branch:
            sales = sales.filter(branch_id=resolved_branch.id)
            expenses = expenses.filter(branch_id=resolved_branch.id)
            repayments = repayments.filter(branch_id=resolved_branch.id)

        if isinstance(target_date, str):
            try:
                target_date = datetime.strptime(
                    target_date, "%Y-%m-%d"
                ).date()
            except ValueError:
                target_date = timezone.localdate()
        elif target_date is None:
            target_date = timezone.localdate()

        target_year = target_date.year if target_date else None
        target_month = target_date.month if target_date else None

        if period_type in ["day", "daily"]:
            sales = sales.filter(
                Q(sale_date__date=target_date)
                | Q(created_at__date=target_date)
            )
            expenses = expenses.filter(
                Q(date=target_date) | Q(created_at__date=target_date)
            )
            repayments = repayments.filter(
                Q(date=target_date) | Q(created_at__date=target_date)
            )

        elif period_type in ["month", "monthly"]:
            sales = sales.filter(
                Q(
                    sale_date__year=target_year,
                    sale_date__month=target_month,
                )
                | Q(
                    created_at__year=target_year,
                    created_at__month=target_month,
                )
            )
            expenses = expenses.filter(
                Q(date__year=target_year, date__month=target_month)
                | Q(
                    created_at__year=target_year,
                    created_at__month=target_month,
                )
            )
            repayments = repayments.filter(
                Q(
                    date__year=target_year,
                    date__month=target_month,
                )
                | Q(
                    created_at__year=target_year,
                    created_at__month=target_month,
                )
            )

        elif period_type in ["year", "yearly"]:
            sales = sales.filter(
                Q(sale_date__year=target_year)
                | Q(created_at__year=target_year)
            )
            expenses = expenses.filter(
                Q(date__year=target_year) | Q(created_at__year=target_year)
            )
            repayments = repayments.filter(
                Q(date__year=target_year) | Q(created_at__year=target_year)
            )

        sales = sales.distinct()
        expenses = expenses.distinct()
        repayments = repayments.distinct()

        (
            total_revenue,
            total_cogs,
        ) = AnalyticsService._calculate_sales_totals(sales)
        gross_profit = total_revenue - total_cogs

        total_expenses = expenses.aggregate(
            total=Coalesce(Sum("amount"), ZERO)
        )["total"]

        pre_tax_profit = gross_profit - total_expenses
        tax_rate = Decimal(str(tax_rate or ZERO))

        taxation = (
            pre_tax_profit * (tax_rate / Decimal("100.00"))
            if pre_tax_profit > ZERO
            else ZERO
        )
        net_profit_after_tax = pre_tax_profit - taxation

        branch_id = resolved_branch.id if resolved_branch else None
        items_sold = AnalyticsService._get_items_sold(
            sales,
            branch_id=branch_id,
            year=target_year,
            month=target_month,
        )

        payments = _calculate_payment_breakdown(
            sales, repayments_qs=repayments
        )

        credit_sales = sales.filter(
            Q(payment_method="CREDIT")
        )
        new_debt_issued = credit_sales.aggregate(
            total=Coalesce(Sum("total_amount"), ZERO)
        )["total"]
        debt_recovered = payments.get("credit_repayments", ZERO)
        net_receivable_change = new_debt_issued - debt_recovered

        payment_data = {k: float(v) for k, v in payments.items()}

        # Correct total cash inflow includes direct collections + repayments
        total_inflow = sum(
            [
                payments.get("cash", ZERO),
                payments.get("paybill", ZERO),
                payments.get("send_money", ZERO),
                payments.get("bank_transfer", ZERO),
                payments.get("credit_repayments", ZERO),
            ]
        )

        return {
            "period_type": period_type,
            "date": target_date.strftime("%Y-%m-%d") if target_date else None,
            "year": target_year,
            "month": target_month,
            "branch_id": branch_id,
            "branch_name": (
                resolved_branch.name if resolved_branch else "All Branches"
            ),
            "total_sales_count": sales.count(),
            "total_revenue": float(total_revenue),
            "gross_sales": float(total_revenue),
            "cost_of_sales": float(total_cogs),
            "cogs": float(total_cogs),
            "gross_profit": float(gross_profit),
            "expenses": float(total_expenses),
            "operating_expenses": float(total_expenses),
            "net_profit": float(pre_tax_profit),
            "pre_tax_profit": float(pre_tax_profit),
            "taxation": float(taxation),
            "net_profit_after_tax": float(net_profit_after_tax),
            "payment_methods": payment_data,
            "payments": payment_data,
            "cash_breakdown": payment_data,
            "total_inflow": float(total_inflow),
            "items_sold": items_sold,
            "item_performance": {
                "all_items": items_sold,
            },
            "cash_on_hand": float(payments.get("cash", ZERO)),
            "accounts_receivable": float(new_debt_issued),
            "new_debt_issued": float(new_debt_issued),
            "debt_recovered": float(debt_recovered),
            "net_receivable_change": float(net_receivable_change),
            "accounts_payable": 0.0,
        }

    @staticmethod
    def calculate_monthly_pnl(
        user=None,
        year=None,
        month=None,
        tax_rate=None,
        branch_id=None,
    ):
        if tax_rate is None:
            tax_rate = ZERO

        today = timezone.localdate()
        target_year = (
            int(year)
            if year is not None and str(year).isdigit()
            else today.year
        )
        target_month = (
            int(month)
            if month is not None and str(month).isdigit()
            else today.month
        )
        target_date = date(target_year, target_month, 1)

        return AnalyticsService.calculate_summary_pnl(
            user=user,
            period_type="month",
            branch=branch_id,
            target_date=target_date,
            tax_rate=tax_rate,
        )


class AnalyticsService:
    # ... existing methods ...

    @staticmethod
    def get_lightweight_management_summary(branch, start_date, end_date):
        """
        Fast aggregation helper for executive dashboard overview cards.
        """
        SaleTransaction = get_stock_model("SaleTransaction")
        CreditRepayment = get_stock_model("CreditRepayment")
        Expense = get_stock_model("Expense")

        sales = SaleTransaction.objects.filter(
            branch=branch,
            sale_date__date__range=(start_date, end_date)
        )
        repayments = CreditRepayment.objects.filter(
            branch=branch,
            date__range=(start_date, end_date)
        )
        expenses = Expense.objects.filter(
            branch=branch,
            date__range=(start_date, end_date)
        )

        sales_summary = sales.aggregate(
            gross=Coalesce(Sum("total_amount"), ZERO, output_field=MONEY_FIELD),
            count=Count("id")
        )
        repayment_sum = repayments.aggregate(
            total=Coalesce(Sum("amount"), ZERO, output_field=MONEY_FIELD)
        )["total"]
        expense_sum = expenses.aggregate(
            total=Coalesce(Sum("amount"), ZERO, output_field=MONEY_FIELD)
        )["total"]

        return {
            "gross_sales": float(sales_summary["gross"]),
            "transaction_count": sales_summary["count"],
            "repayments_received": float(repayment_sum),
            "total_expenses": float(expense_sum),
            "expense_count": expenses.count(),
        }
        