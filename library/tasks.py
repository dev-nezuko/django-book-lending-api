from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import Loan


@shared_task
def send_loan_notification(loan_id):
    try:
        loan = Loan.objects.select_related('member__user', 'book').get(pk=loan_id)
    except Loan.DoesNotExist:
        return

    if not loan.member.user.email:
        return

    send_mail(
        subject='Book Loaned Successfully',
        message=(
            f'Hello {loan.member.user.username},\n\n'
            f'You have successfully loaned "{loan.book.title}".\n'
            f'Please return it by {loan.due_date:%Y-%m-%d}.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[loan.member.user.email],
        fail_silently=False,
    )


@shared_task
def check_overdue_loans():
    overdue_loans = Loan.objects.filter(
        is_returned=False,
        due_date__lt=timezone.localdate(),
    ).select_related('member__user', 'book')

    for loan in overdue_loans:
        email = loan.member.user.email
        if not email:
            continue
        send_mail(
            subject='Overdue library book',
            message=(
                f'Please return "{loan.book.title}". '
                f'It was due on {loan.due_date:%Y-%m-%d}.'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
