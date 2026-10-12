import logging
import threading
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, send_mail
from django.core.signing import TimestampSigner

try:
    from stocks.models import RetailBranch
except ImportError:
    try:
        from apis.models import RetailBranch
    except ImportError:
        RetailBranch = None

logger = logging.getLogger(__name__)
signer = TimestampSigner()


def _get_management_emails():
    """Helper to safely fetch and normalize management emails as a list."""
    emails = getattr(settings, "MANAGEMENT_EMAILS", ["engugi922@gmail.com"])
    if isinstance(emails, str):
        return [e.strip() for e in emails.split(",") if e.strip()]
    return list(emails) if emails else ["engugi922@gmail.com"]


def _resolve_branch_name(payload):
    """Resolves human-readable branch name from payload branch_id or branch identifier."""
    branch_id = payload.get("branch_id") or payload.get("target_branch") or payload.get("branch")
    if not branch_id:
        return "N/A"

    if RetailBranch:
        try:
            if isinstance(branch_id, int) or (isinstance(branch_id, str) and branch_id.isdigit()):
                branch = RetailBranch.objects.filter(id=int(branch_id)).first()
                if branch:
                    return branch.name
            else:
                branch = RetailBranch.objects.filter(name=branch_id).first()
                if branch:
                    return branch.name
        except Exception as err:
            logger.warning(f"Failed to resolve branch name: {err}")

    return str(branch_id)


def _dispatch_management_review_email(approval):
    """Internal synchronous function to compile and send management review email with direct action buttons."""
    management_emails = _get_management_emails()
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "engugi922@gmail.com")
    submitter_name = approval.submitted_by.get_full_name() or approval.submitted_by.username
    
    frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")
    backend_url = getattr(settings, "BACKEND_URL", "http://localhost:8000").rstrip("/")
    
    review_url = f"{frontend_url}/approvals/{approval.id}"

    # Generate secure token for one-click email actions
    token = signer.sign(str(approval.id))
    approve_via_email_url = f"{backend_url}/api/pending-approvals/{approval.id}/approve_via_email/?token={token}"
    reject_via_email_url = f"{backend_url}/api/pending-approvals/{approval.id}/reject_via_email/?token={token}"

    payload = approval.payload or {}
    items = payload.get("items", [])
    branch_name = _resolve_branch_name(payload)

    # Determine display label (Ensures Item Entry is used for item additions)
    req_type = str(approval.request_type).upper()
    if "BRANCH" in req_type:
        request_type_display = "Branch Creation"
    elif "DIRECT_SALE" in req_type or "POS_SALE" in req_type:
        request_type_display = "Sale Entry"
    else:
        request_type_display = "Item Entry to Branch"

    items_text_summary = ""
    if items:
        items_text_summary += "\nItems Requested for Import:\n"
        for idx, item in enumerate(items, 1):
            items_text_summary += (
                f"  {idx}. {item.get('name')}\n"
                f"     - Cost Price: KSh {item.get('unit_cost_price', '0.00')}\n"
                f"     - Retail Price: KSh {item.get('unit_retail_price', '0.00')}\n"
                f"     - Opening Stock: {item.get('opening_stock', 0)}\n"
            )

    subject = f"Review Required: {request_type_display} ({branch_name})"
    text_message = (
        f"Hello Management,\n\n"
        f"A new {request_type_display} was submitted by {submitter_name}.\n"
        f"Branch: {branch_name}\n"
        f"Request ID: {approval.id}\n"
        f"Submitted At: {approval.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{items_text_summary}\n"
        f"Direct Actions:\n"
        f"- Approve Request: {approve_via_email_url}\n"
        f"- Reject Request: {reject_via_email_url}\n\n"
        f"Review in Dashboard: {review_url}\n\n"
        f"Regards,\nRetail Management System"
    )

    rows_html = "".join([
        f"""
        <tr>
            <td style="padding: 8px; border: 1px solid #e2e8f0;">{item.get('name')}</td>
            <td style="padding: 8px; border: 1px solid #e2e8f0;">KSh {item.get('unit_cost_price', '0.00')}</td>
            <td style="padding: 8px; border: 1px solid #e2e8f0;">KSh {item.get('unit_retail_price', '0.00')}</td>
            <td style="padding: 8px; border: 1px solid #e2e8f0;">{item.get('opening_stock', 0)}</td>
        </tr>
        """
        for item in items
    ])

    html_message = f"""
    <div style="font-family: Arial, sans-serif; color: #1e293b; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #312e81; margin-bottom: 10px;">Review Required: {request_type_display}</h2>
        <p>A new <strong>{request_type_display}</strong> request was submitted by <strong>{submitter_name}</strong> for Branch: <strong>{branch_name}</strong>.</p>
        
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 13px;">
            <thead>
                <tr style="background-color: #f1f5f9; text-align: left;">
                    <th style="padding: 8px; border: 1px solid #cbd5e1;">Item Name</th>
                    <th style="padding: 8px; border: 1px solid #cbd5e1;">Cost Price</th>
                    <th style="padding: 8px; border: 1px solid #cbd5e1;">Retail Price</th>
                    <th style="padding: 8px; border: 1px solid #cbd5e1;">Opening Stock</th>
                </tr>
            </thead>
            <tbody>
                {rows_html if rows_html else "<tr><td colspan='4' style='padding:8px; text-align:center;'>No item details provided.</td></tr>"}
            </tbody>
        </table>

        <!-- DIRECT ONE-CLICK ACTION BUTTONS -->
        <div style="margin: 25px 0 15px 0;">
            <a href="{approve_via_email_url}" style="background-color: #059669; color: #ffffff; padding: 10px 18px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 13px; display: inline-block; margin-right: 10px;">
                ✅ Approve & Save to Database
            </a>
            <a href="{reject_via_email_url}" style="background-color: #dc2626; color: #ffffff; padding: 10px 18px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 13px; display: inline-block;">
                ❌ Reject Request
            </a>
        </div>

        <div style="margin-top: 15px; border-top: 1px solid #e2e8f0; padding-top: 15px;">
            <a href="{review_url}" style="color: #4f46e5; text-decoration: none; font-size: 12px; font-weight: bold;">
                🔗 Or view full request in Web Dashboard &rarr;
            </a>
        </div>
    </div>
    """

    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_message,
            from_email=from_email,
            to=management_emails,
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
    except Exception as exc:
        logger.error(f"Failed to send HTML management email: {exc}")


def send_management_review_email(approval):
    """Fires the email sending task in a background thread to prevent API blocking."""
    thread = threading.Thread(target=_dispatch_management_review_email, args=(approval,))
    thread.start()


def _dispatch_submitter_status_email(approval, approved=True, reason=None):
    """Internal synchronous function to notify submitter."""
    submitter_email = getattr(approval.submitted_by, "email", None)
    if not submitter_email:
        return

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "engugi922@gmail.com")
    status_label = "APPROVED" if approved else "REJECTED"
    
    req_type = str(approval.request_type).upper()
    if "BRANCH" in req_type:
        request_type_display = "Branch Creation"
    elif "DIRECT_SALE" in req_type or "POS_SALE" in req_type:
        request_type_display = "Sale Entry"
    else:
        request_type_display = "Item Entry to Branch"

    subject = f"Submission {status_label}: {request_type_display}"

    if approved:
        message = (
            f"Hello {approval.submitted_by.username},\n\n"
            f"Your request '{request_type_display}' (ID: {approval.id}) "
            f"has been APPROVED by management and saved into live records.\n"
        )
    else:
        message = (
            f"Hello {approval.submitted_by.username},\n\n"
            f"Your request '{request_type_display}' (ID: {approval.id}) "
            f"was REJECTED by management.\n\n"
            f"Reason:\n{reason or 'No specific reason provided.'}\n"
        )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=[submitter_email],
            fail_silently=False,
        )
    except Exception as exc:
        logger.error(f"Failed to send submitter status email: {exc}")


def send_submitter_status_email(approval, approved=True, reason=None):
    """Fires status email in a background thread to prevent API blocking."""
    thread = threading.Thread(
        target=_dispatch_submitter_status_email,
        args=(approval, approved, reason)
    )
    thread.start()