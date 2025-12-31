from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from .models import Author, Book, Loan, Member
from .serializers import (
    AuthorSerializer,
    BookSerializer,
    LoanExtensionSerializer,
    LoanMemberSerializer,
    LoanSerializer,
    MemberSerializer,
)
from .tasks import send_loan_notification


class AuthorViewSet(viewsets.ModelViewSet):
    queryset = Author.objects.all().order_by('id')
    serializer_class = AuthorSerializer


class BookViewSet(viewsets.ModelViewSet):
    queryset = Book.objects.select_related('author').order_by('id')
    serializer_class = BookSerializer

    def _locked_book(self, request, pk):
        book = get_object_or_404(
            self.filter_queryset(self.get_queryset()).select_for_update(), pk=pk
        )
        self.check_object_permissions(request, book)
        return book

    @action(detail=True, methods=['post'])
    def loan(self, request, pk=None):
        payload = LoanMemberSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        with transaction.atomic():
            book = self._locked_book(request, pk)
            if book.available_copies < 1:
                return Response(
                    {'error': 'No available copies.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            loan = Loan.objects.create(
                book=book, member=payload.validated_data['member_id']
            )
            book.available_copies -= 1
            book.save(update_fields=['available_copies'])
            # A worker must not see the loan before the database commits it.
            # Broker failure must not make a committed loan look unsuccessful.
            transaction.on_commit(
                lambda: send_loan_notification.delay(loan.pk), robust=True
            )
        return Response(
            {'status': 'Book loaned successfully.', 'loan': LoanSerializer(loan).data},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'])
    def return_book(self, request, pk=None):
        payload = LoanMemberSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        with transaction.atomic():
            book = self._locked_book(request, pk)
            loan = (
                Loan.objects.select_for_update()
                .filter(
                    book=book,
                    member=payload.validated_data['member_id'],
                    is_returned=False,
                )
                .order_by('loan_date', 'pk')
                .first()
            )
            if loan is None:
                return Response(
                    {'error': 'Active loan does not exist.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            loan.is_returned = True
            loan.return_date = timezone.localdate()
            loan.save(update_fields=['is_returned', 'return_date'])
            book.available_copies += 1
            book.save(update_fields=['available_copies'])
        return Response({'status': 'Book returned successfully.'})


class MemberViewSet(viewsets.ModelViewSet):
    queryset = Member.objects.select_related('user').order_by('id')
    serializer_class = MemberSerializer


class LoanViewSet(viewsets.ReadOnlyModelViewSet):
    # Stock changes belong to the book actions, not arbitrary loan CRUD writes.
    queryset = Loan.objects.select_related('book__author', 'member__user').order_by('id')
    serializer_class = LoanSerializer

    @action(detail=True, methods=['post'], url_path='extended_due_date')
    def extended_due_date(self, request, pk=None):
        payload = LoanExtensionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        with transaction.atomic():
            loan = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            self.check_object_permissions(request, loan)
            if loan.is_returned:
                return Response(
                    {'error': 'Returned loans cannot be extended.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if loan.due_date < timezone.localdate():
                return Response(
                    {'error': 'Overdue loans cannot be extended.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                due_date = loan.due_date + timedelta(
                    days=payload.validated_data['additional_days']
                )
            except OverflowError:
                return Response(
                    {'error': 'The extended due date is outside the supported range.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            loan.due_date = due_date
            loan.save(update_fields=['due_date'])
        return Response(self.get_serializer(loan).data)
