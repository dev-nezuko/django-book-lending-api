import datetime as dt
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from utils import commit_utils
from .models import Author, Book, Loan, Member
from .tasks import check_overdue_loans, send_loan_notification


class CommitUtilsTests(TestCase):
    def test_generate_commit_message_format_and_task(self) -> None:
        """Messages should follow Feature::Task::Task Description."""
        pattern = re.compile(r"^Feature::([A-Za-z]+)::(.+)$")
        seen_tasks: set[str] = set()

        for _ in range(50):
            msg = commit_utils.generate_commit_message()
            match = pattern.match(msg)
            self.assertIsNotNone(match, msg)
            if match:
                task_label, description = match.groups()
                seen_tasks.add(task_label)
                self.assertIn(task_label, {t.value for t in commit_utils.TaskType})
                self.assertGreater(len(description.strip()), 0)
                self.assertLessEqual(len(description), 72)

        # Sanity check that we are not always generating the same TaskType.
        self.assertGreater(len(seen_tasks), 1)

    def test_generate_commit_message_for_specific_task(self) -> None:
        """Explicit TaskType should constrain the prefix."""
        msg = commit_utils.generate_commit_message(commit_utils.TaskType.TEST)
        self.assertTrue(msg.startswith("Feature::Test::"))

    def test_generate_commit_timestamps_within_range_and_ordered(self) -> None:
        """Generated timestamps should be in UTC, ordered, and within range."""
        start = dt.date(2023, 10, 1)
        end = dt.date(2023, 10, 15)
        timestamps = commit_utils.generate_commit_timestamps(
            start, end, commit_count=20, skip_weekends=True, seed=123
        )

        self.assertEqual(len(timestamps), 20)
        self.assertEqual(sorted(timestamps), timestamps)

        for ts in timestamps:
            self.assertIsNotNone(ts.tzinfo)
            self.assertEqual(ts.tzinfo, dt.timezone.utc)
            self.assertGreaterEqual(ts.date(), start)
            self.assertLessEqual(ts.date(), end)
            # Ensure we skipped Saturday (5) and Sunday (6)
            self.assertLess(ts.weekday(), 5)

    def test_build_git_date_env_sets_author_and_committer(self) -> None:
        """Environment mapping must contain matching author and committer dates."""
        ts = dt.datetime(2023, 10, 3, 10, 30, 0, tzinfo=dt.timezone.utc)
        base_env = {"EXISTING_VAR": "keep-me"}

        env = commit_utils.build_git_date_env(ts, base_env)

        expected = "2023-10-03T10:30:00Z"
        self.assertEqual(env["GIT_AUTHOR_DATE"], expected)
        self.assertEqual(env["GIT_COMMITTER_DATE"], expected)
        self.assertEqual(env["EXISTING_VAR"], "keep-me")


class LendingWorkflowTests(APITestCase):
    def setUp(self):
        self.author = Author.objects.create(first_name='Ada', last_name='Example')
        self.book = Book.objects.create(
            title='A Library Example', author=self.author,
            isbn='9780000000001', genre='fiction', available_copies=1,
        )
        self.user = User.objects.create_user('reader', email='reader@example.test')
        self.member = Member.objects.create(user=self.user)
        self.borrow_url = f'/api/books/{self.book.pk}/loan/'
        self.return_url = f'/api/books/{self.book.pk}/return_book/'

    def borrow(self):
        return self.client.post(self.borrow_url, {'member_id': self.member.pk}, format='json')

    def test_borrow_persists_loan_and_reduces_stock(self):
        response = self.borrow()
        self.assertEqual(response.status_code, 201)
        self.book.refresh_from_db()
        loan = Loan.objects.get()
        self.assertEqual(self.book.available_copies, 0)
        self.assertEqual(loan.member, self.member)
        self.assertEqual(loan.due_date, timezone.localdate() + timedelta(days=14))
        self.assertEqual(response.data['loan']['id'], loan.pk)
        self.assertEqual(response.data['loan']['due_date'], loan.due_date.isoformat())

    @patch('library.views.send_loan_notification.delay')
    def test_notification_is_dispatched_only_after_commit(self, dispatch):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self.borrow()
            dispatch.assert_not_called()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(callbacks), 1)
        dispatch.assert_called_once_with(Loan.objects.get().pk)

    @patch('library.views.send_loan_notification.delay', side_effect=RuntimeError('Broker unavailable'))
    def test_broker_failure_does_not_disguise_a_successful_loan(self, dispatch):
        with self.assertLogs('django.test', level='ERROR'):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.borrow()
        self.assertEqual(response.status_code, 201)
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 0)
        self.assertEqual(Loan.objects.count(), 1)

    def test_out_of_stock_does_not_create_another_loan(self):
        self.borrow()
        response = self.borrow()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Loan.objects.count(), 1)
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 0)

    def test_invalid_member_payload_does_not_change_inventory(self):
        for value in (None, 'not-an-id', 99999):
            with self.subTest(value=value):
                response = self.client.post(self.borrow_url, {'member_id': value}, format='json')
                self.assertEqual(response.status_code, 400)
        self.assertEqual(Loan.objects.count(), 0)
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 1)

    def test_missing_member_is_a_validation_error(self):
        self.assertEqual(self.client.post(self.borrow_url, {}, format='json').status_code, 400)

    def test_missing_book_is_not_found(self):
        response = self.client.post('/api/books/99999/loan/', {'member_id': self.member.pk})
        self.assertEqual(response.status_code, 404)

    def test_failed_loan_creation_rolls_back_inventory(self):
        with patch('library.views.Loan.objects.create', side_effect=RuntimeError('Write failed')):
            with self.assertRaises(RuntimeError):
                self.borrow()
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 1)
        self.assertFalse(Loan.objects.exists())

    def test_return_restores_one_copy_and_cannot_return_it_twice(self):
        self.borrow()
        response = self.client.post(self.return_url, {'member_id': self.member.pk})
        self.assertEqual(response.status_code, 200)
        loan = Loan.objects.get()
        self.assertTrue(loan.is_returned)
        self.assertEqual(loan.return_date, timezone.localdate())
        response = self.client.post(self.return_url, {'member_id': self.member.pk})
        self.assertEqual(response.status_code, 400)
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 1)

    def test_inventory_write_failure_does_not_leave_an_orphan_loan(self):
        with patch('library.views.Book.save', side_effect=RuntimeError('Inventory write failed')):
            with self.assertRaises(RuntimeError):
                self.borrow()
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 1)
        self.assertFalse(Loan.objects.exists())

    def test_two_borrowed_copies_can_be_returned_one_at_a_time(self):
        self.book.available_copies = 2
        self.book.save(update_fields=['available_copies'])
        self.borrow()
        self.borrow()
        self.assertEqual(Loan.objects.count(), 2)
        response = self.client.post(self.return_url, {'member_id': self.member.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Loan.objects.filter(is_returned=False).count(), 1)
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 1)

    def test_return_without_an_active_loan_does_not_add_stock(self):
        response = self.client.post(self.return_url, {'member_id': self.member.pk})
        self.assertEqual(response.status_code, 400)
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 1)

    def test_loan_records_cannot_bypass_the_inventory_actions(self):
        self.assertEqual(self.client.post('/api/loans/', {}).status_code, 405)
        loan = Loan.objects.create(book=self.book, member=self.member)
        self.assertEqual(self.client.patch(f'/api/loans/{loan.pk}/', {}).status_code, 405)
        self.assertEqual(self.client.delete(f'/api/loans/{loan.pk}/').status_code, 405)

    def test_loan_list_includes_due_dates_and_uses_pagination(self):
        Loan.objects.create(book=self.book, member=self.member)
        response = self.client.get('/api/loans/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertIn('due_date', response.data['results'][0])

    def test_author_list_uses_the_configured_page_size(self):
        for index in range(11):
            Author.objects.create(first_name=f'Author {index}', last_name='Example')
        response = self.client.get('/api/authors/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 12)
        self.assertEqual(len(response.data['results']), 10)
        self.assertIsNotNone(response.data['next'])


class LoanExtensionTests(APITestCase):
    def setUp(self):
        author = Author.objects.create(first_name='Ada', last_name='Example')
        self.book = Book.objects.create(title='Example', author=author, isbn='9780000000002', genre='fiction')
        member = Member.objects.create(user=User.objects.create_user('reader'))
        self.loan = Loan.objects.create(book=self.book, member=member)
        self.url = f'/api/loans/{self.loan.pk}/extended_due_date/'

    def test_positive_extension_persists_and_is_serialized(self):
        before = self.loan.due_date
        response = self.client.post(self.url, {'additional_days': 3}, format='json')
        self.assertEqual(response.status_code, 200)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.due_date, before + timedelta(days=3))
        self.assertEqual(response.data['due_date'], self.loan.due_date.isoformat())

    def test_invalid_extension_values_do_not_change_the_loan(self):
        before = self.loan.due_date
        for value in (None, 0, -1, 'not-a-number', 1.5, True, 10**20):
            with self.subTest(value=value):
                response = self.client.post(self.url, {'additional_days': value}, format='json')
                self.assertEqual(response.status_code, 400)
                self.loan.refresh_from_db()
                self.assertEqual(self.loan.due_date, before)

    def test_missing_extension_days_is_a_validation_error(self):
        self.assertEqual(self.client.post(self.url, {}, format='json').status_code, 400)

    def test_returned_loan_cannot_be_extended(self):
        self.loan.is_returned = True
        self.loan.save(update_fields=['is_returned'])
        self.assertEqual(self.client.post(self.url, {'additional_days': 3}).status_code, 400)

    def test_overdue_loan_cannot_be_extended(self):
        self.loan.due_date = timezone.localdate() - timedelta(days=1)
        self.loan.save(update_fields=['due_date'])
        self.assertEqual(self.client.post(self.url, {'additional_days': 3}).status_code, 400)

    def test_loan_due_today_can_be_extended(self):
        self.loan.due_date = timezone.localdate()
        self.loan.save(update_fields=['due_date'])
        self.assertEqual(self.client.post(self.url, {'additional_days': 3}).status_code, 200)


class LoanMailTests(TestCase):
    def setUp(self):
        author = Author.objects.create(first_name='Ada', last_name='Example')
        self.book = Book.objects.create(title='Example', author=author, isbn='9780000000003', genre='fiction')
        self.user = User.objects.create_user('reader', email='reader@example.test')
        self.member = Member.objects.create(user=self.user)

    def test_new_loan_message_includes_the_book_and_due_date(self):
        loan = Loan.objects.create(book=self.book, member=self.member)
        send_loan_notification(loan.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['reader@example.test'])
        self.assertIn(self.book.title, mail.outbox[0].body)
        self.assertIn(loan.due_date.isoformat(), mail.outbox[0].body)

    def test_deleted_loan_does_not_send_mail(self):
        send_loan_notification(99999)
        self.assertEqual(len(mail.outbox), 0)

    def test_member_without_an_email_does_not_send_mail(self):
        self.user.email = ''
        self.user.save(update_fields=['email'])
        loan = Loan.objects.create(book=self.book, member=self.member)
        send_loan_notification(loan.pk)
        self.assertEqual(len(mail.outbox), 0)

    def test_overdue_reminders_exclude_today_future_and_returned_loans(self):
        today = timezone.localdate()
        overdue = Loan.objects.create(book=self.book, member=self.member, due_date=today - timedelta(days=1))
        Loan.objects.create(book=self.book, member=self.member, due_date=today)
        Loan.objects.create(book=self.book, member=self.member, due_date=today + timedelta(days=1))
        Loan.objects.create(book=self.book, member=self.member, due_date=today - timedelta(days=2), is_returned=True)
        check_overdue_loans()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(overdue.due_date.isoformat(), mail.outbox[0].body)

    def test_overdue_member_without_email_is_skipped(self):
        self.user.email = ''
        self.user.save(update_fields=['email'])
        Loan.objects.create(book=self.book, member=self.member, due_date=timezone.localdate() - timedelta(days=1))
        check_overdue_loans()
        self.assertEqual(len(mail.outbox), 0)

