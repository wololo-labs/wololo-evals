# Security policy

## Reporting a vulnerability

Do not report vulnerabilities in public issues or discussions. Use GitHub's
private vulnerability reporting flow for this repository:

1. Open the repository's **Security** tab.
2. Select **Report a vulnerability**.
3. Include reproduction steps, impact, and any relevant version or commit.

Private reporting must be enabled in the repository settings before public
release. Maintainers should acknowledge reports and coordinate a fix before
public disclosure.

## Secret scanning

The GitGuardian workflow requires an Actions repository secret named
`GITGUARDIAN_API_KEY`. Add a GitGuardian service-account key or approved
personal key in repository settings. Never commit that key.
