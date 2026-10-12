from celery import shared_task
from apis.emails import send_management_review_email, send_submitter_status_email
from stocks.models import PendingApproval

@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_management_review_email_task(approval_id):
    approval = PendingApproval.objects.filter(id=approval_id).first()
    if approval:
        send_management_review_email(approval)

@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_submitter_status_email_task(approval_id, approved, reason=None):
    approval = PendingApproval.objects.filter(id=approval_id).first()
    if approval:
        send_submitter_status_email(approval, approved=approved, reason=reason)