from django.http import JsonResponse
from django.contrib.admin.views.decorators import staff_member_required
from .models import BranchItemPricing

@staff_member_required
def get_item_pricing(request):
    item_id = request.GET.get('item_id')
    branch_id = request.GET.get('branch_id')

    if not item_id or not branch_id:
        return JsonResponse({'success': False, 'message': 'Missing item_id or branch_id'})

    pricing = BranchItemPricing.objects.filter(
        item_id=item_id, 
        branch_id=branch_id
    ).first()

    if pricing:
        return JsonResponse({
            'success': True,
            'unit_selling_price': f"{pricing.unit_retail_price:.2f}",
            'unit_cost_price': f"{pricing.unit_cost_price:.2f}",
        })

    return JsonResponse({
        'success': False,
        'unit_selling_price': "0.00",
        'unit_cost_price': "0.00"
    })