document.addEventListener('DOMContentLoaded', function () {
    // Safely grab Django's jQuery instance once DOM and scripts are fully ready
    var $ = window.django && window.django.jQuery ? window.django.jQuery : window.jQuery;

    if (!$) {
        console.error('jQuery is not loaded in Django Admin scope.');
        return;
    }

    function getBranchId() {
        var branchId = $('#id_branch').val();
        if (!branchId) {
            branchId = $('[name="branch"]').val();
        }
        return branchId;
    }

    function fetchAndApplyPricing($row, itemId) {
        var branchId = getBranchId();

        if (!itemId) return;

        if (!branchId) {
            alert('Please select a Branch at the top of the form first.');
            return;
        }

        $.ajax({
            url: '/get-item-pricing/',
            method: 'GET',
            data: {
                item_id: itemId,
                branch_id: branchId
            },
            dataType: 'json',
            success: function (data) {
                if (data && data.success) {
                    // 1. Update Unit Selling Price Input
                    $row.find('input[name$="-unit_selling_price"]').val(data.unit_selling_price).trigger('change');

                    // 2. Update Unit Cost Price Input (if editable)
                    var $costInput = $row.find('input[name$="-unit_cost_price"]');
                    if ($costInput.length) {
                        $costInput.val(data.unit_cost_price).trigger('change');
                    }

                    // 3. Update Readonly Unit Cost Price display
                    var $costReadonly = $row.find('.field-unit_cost_price p, .field-unit_cost_price div.readonly, td.field-unit_cost_price');
                    if ($costReadonly.length) {
                        $costReadonly.text(data.unit_cost_price);
                    }
                }
            },
            error: function (xhr, status, error) {
                console.error('Failed to fetch pricing:', error);
            }
        });
    }

    // Bind event handlers
    $(document).on('change', 'select[name$="-item"]', function () {
        var $row = $(this).closest('tr');
        var itemId = $(this).val();
        fetchAndApplyPricing($row, itemId);
    });

    $(document).on('select2:select', 'select[name$="-item"]', function (e) {
        var $row = $(this).closest('tr');
        var itemId = e.params.data.id;
        fetchAndApplyPricing($row, itemId);
    });

    $(document).on('change', '#id_branch, select[name="branch"]', function () {
        var branchId = $(this).val();
        if (!branchId) return;

        $('tr.form-row').each(function () {
            var $row = $(this);
            var itemId = $row.find('select[name$="-item"]').val();
            if (itemId) {
                fetchAndApplyPricing($row, itemId);
            }
        });
    });
});