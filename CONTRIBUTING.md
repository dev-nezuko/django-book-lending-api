# Contributing

Keep a change focused on a library workflow or a clearly described documentation
improvement. Explain domain decisions, validation boundaries and transaction
behavior; comments that merely repeat the next operation are usually unnecessary.

## Verify a change

Use the existing requirements and a separate virtual environment. The isolated
test configuration needs no PostgreSQL, Redis or outgoing email service:

```sh
python manage.py test --settings=library_system.test_settings
python manage.py makemigrations --check --dry-run --settings=library_system.test_settings
```

For lending changes, test both the saved loan and the available-copy count.
Include failed writes, repeated returns and invalid renewal inputs where relevant.
Queue notifications only after transaction commit, and keep email/task delivery
mocked or in memory during tests. SQLite tests do not demonstrate PostgreSQL
row-lock behavior; verify that separately when changing concurrency guarantees.

## Commits

Use `Feature::Task::Description`, with a concrete description of the actual work.
Typical task labels are `BugFix`, `Test`, `Docs`, `Refactor`, `Chore` and `Infra`.
Bug fixes and their regression tests belong together; keep unrelated changes out
of that commit. Never add a claim of passing tests, production readiness or a
performance result that was not verified.

Do not commit `.env`, credentials, local databases, generated static files or
virtual environments. `.env.example` contains nonfunctional development examples.
