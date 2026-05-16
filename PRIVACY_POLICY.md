# SongZip Privacy Policy

Last updated: May 16, 2026

This document is a transparency draft for the current SongZip dashboard build. It is not legal advice, and the product's real behavior controls.

## 1. Scope and purpose

SongZip is positioned as a personal workflow dashboard for queueing media-related requests, checking progress, and optionally linking a user's own provider account through official OAuth flows when a feature needs it.

Short public summary:

"SongZip is intended for personal research, evaluation, and entertainment-related organization workflows. Where account linking is offered, it uses official provider authorization, requests limited permissions, and is not intended to sell personal data, use borrowed credentials, or hide how provider data is accessed."

## 2. Data SongZip may process

If account linking is enabled, SongZip may need to process limited data such as:

- provider account identifiers
- provider display names or email-style account labels
- access tokens
- refresh tokens
- granted scopes
- token expiry timestamps
- session identifiers and connection-status metadata

If OAuth is enabled, SongZip should not claim that it collects "no user data." The more accurate statement is that it is intended to process only the limited authorization and account data needed for the user-requested feature.

## 3. How data should be used

SongZip is intended to use data only for visible user-facing functions such as:

- completing official account-linking flows initiated by the user
- maintaining the user's active session and connected-account status
- refreshing tokens when required for the feature that was authorized
- supporting dashboard features tied to the linked account

## 4. Data use limits

SongZip should not:

- sell or rent personal data
- use personal data for ad targeting or unrelated profiling
- ask users to paste raw tokens into the interface
- accept borrowed, shared, leaked, or third-party credentials
- use linked-account data to market quota circumvention

## 5. Permissions and provider access

SongZip should use official documented access methods and request only minimum relevant permissions.

- Users should connect their own Spotify or Google accounts through official provider sign-in pages.
- Permissions should be requested in context and tied to a visible feature.
- If a future feature needs broader permissions, disclosures should be updated before access is requested.

## 6. Retention, security, and current limitation

Before public deployment, SongZip should implement:

- encrypted storage for refresh tokens and secrets
- access controls for any server storing connection records
- retention rules and disconnect / deletion workflows
- audit logging that avoids unnecessary token exposure

Current limitation:

The current local prototype stores limited OAuth connection records server-side for operational use. Until encryption and retention controls are hardened, it should not be described as a production-grade privacy implementation.

## 7. Provider-specific commitments

SongZip should:

- provide accurate privacy disclosures for Spotify data access and use
- avoid misleading users about how provider data is accessed
- respect minimum-permission expectations in Spotify and Google policies
- avoid prohibited uses of provider data, including deceptive access patterns

## 8. Source references

- Spotify Developer Policy: https://developer.spotify.com/policy
- Spotify Developer Terms: https://developer.spotify.com/terms
- Spotify Web API rate limits: https://developer.spotify.com/documentation/web-api/concepts/rate-limits
- Spotify quota modes: https://developer.spotify.com/documentation/web-api/concepts/quota-modes
- Google API Services User Data Policy: https://developers.google.com/terms/api-services-user-data-policy
- YouTube API Services Developer Policies: https://developers.google.com/youtube/terms/developer-policies
- YouTube quota and compliance audits: https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits
