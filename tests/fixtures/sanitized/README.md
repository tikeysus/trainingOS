# Sanitized test fixtures

FIT files in this directory are allowed by `.gitignore` via the
`!tests/fixtures/sanitized/*.fit` exception.

Before adding any fixture here, verify it contains no real GPS coordinates,
precise timestamps, heart-rate data, or any other measurement that could
identify a person. Strip or anonymize all personal fields; use synthetic
values or a tool such as `fitdecode` to scrub the file.

All other `*.fit` files in the repository are blocked by `.gitignore`.
