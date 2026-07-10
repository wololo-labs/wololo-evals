# kyc-retry-hardening

Purpose: when calling an external KYC/identity provider, a transient provider
failure must NEVER silently drop the applicant. Retry transient failures with
backoff; only a definitive negative decision from the provider is terminal.

## Rules

- Treat provider HTTP 5xx and connection/timeout errors as **transient**: retry
  with exponential backoff up to the configured attempt cap.
- Treat only an explicit provider decision (approved / rejected) as terminal.
- On exhausting retries, raise a domain error the caller can act on. Never
  return a "dropped" / empty result that removes the applicant from onboarding.
- Reuse the repo's existing retry/backoff helper if one exists; do not add a new
  retry abstraction alongside it.

## Why

The seeded Atlas bug is a retry wrapper that treats a provider 5xx as terminal
and silently drops the applicant. The fix is to classify 5xx as retryable and
surface exhaustion as an error, not a silent drop.
