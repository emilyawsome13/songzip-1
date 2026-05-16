# SongZip Research, Personal Use, and Data Handling Policy

Draft date: May 16, 2026

## 1. Important Notice

This document is a policy draft for transparency and internal compliance review. It is not legal advice and it does not by itself make a product compliant with Spotify, YouTube, Google, or any other platform's rules.

Actual compliance depends on:

- the real features offered by the product
- the provider APIs and scopes used
- how user data is stored and protected
- whether the product is commercialized
- whether the product copies, downloads, redistributes, or transforms content in ways that platform terms or law restrict

If the product's real behavior differs from this document, the real behavior controls.

## 2. Intended Purpose

SongZip is intended to be used only for the following limited purposes:

- personal research and evaluation of media metadata, catalog references, and workflow behavior
- personal entertainment-related organization of user-requested media references
- user-initiated account connections through official provider authorization flows

SongZip is not intended to be used for:

- unauthorized copying, redistribution, resale, or public distribution of protected content
- evading rate limits, quotas, or access controls
- using borrowed, shared, leaked, or third-party API tokens or credentials
- scraping or collecting provider data outside official documented access methods
- misleading users or platforms about how data is accessed, processed, or stored

## 3. Provider Access Policy

SongZip should access provider data only through official documented mechanisms.

### 3.1 Official account linking only

- Users should connect their own Spotify or Google accounts through official OAuth flows.
- SongZip should not ask users to paste raw access tokens or refresh tokens into the interface.
- SongZip should not accept or encourage the use of someone else's credentials, shared credentials, or "example" tokens.

### 3.2 Minimum necessary permissions

- SongZip should request only the scopes needed for the user-facing feature being provided.
- If a feature does not require a permission, that permission should not be requested.
- If future features need broader permissions, the privacy notice and consent flow should be updated before those permissions are requested.

### 3.3 No quota-circumvention claims

- Linking a user's own account does not automatically move developer quota obligations away from the app operator.
- The service must not represent user OAuth tokens as a valid way to bypass provider rate limits, project quotas, or app review requirements.

## 4. Content Use and Copyright Position

SongZip does not claim ownership of third-party content made available through Spotify, YouTube, Google, or other providers.

Users are responsible for ensuring that their use of any content is lawful and permitted by:

- the applicable provider terms
- copyright law
- any license or subscription terms attached to the content

SongZip should not be represented as authorizing downloads, copies, or redistributions that a provider or rights holder does not permit.

If a provider's terms prohibit a particular use, this policy should be read as prohibiting that use within SongZip as well.

## 5. User Data and Privacy Position

SongZip is intended to minimize user data handling and to avoid unrelated personal-data collection.

### 5.1 Data SongZip may need to process

For official OAuth-linked connections, SongZip may need to process limited data such as:

- provider account identifiers
- provider display names or email-style account labels
- access tokens
- refresh tokens
- granted scopes
- token expiry timestamps
- session identifiers

### 5.2 Data handling limits

SongZip should not:

- sell user data
- rent user data
- use user data for advertising profiling
- use user data for unrelated secondary purposes without new disclosure and consent
- use provider data or user data to train machine-learning or AI models where platform rules prohibit that use

### 5.3 Operational reality of the current implementation

The current local implementation stores limited OAuth connection records server-side for operational use. That means the service cannot honestly claim that it collects "no user data" if OAuth account linking is enabled.

The more accurate statement is:

"SongZip is intended to collect only the limited account and authorization data needed to provide the user-requested account-linking and dashboard features. It is not intended to sell personal data, use it for advertising, or process unrelated personal information."

## 6. Security and Retention Expectations

Before public deployment, the operator should implement and document:

- encrypted storage for refresh tokens and other secrets
- access controls for any server storing connection records
- a retention schedule for account-link data
- a process for revocation, deletion, and disconnect requests
- audit logging that avoids unnecessary token exposure

### Current limitation

The current local prototype should not be described as a production-grade privacy or security implementation until encrypted secret storage, retention controls, and deployment hardening are in place.

## 7. Transparency Commitments

SongZip should provide users with clear disclosures that explain:

- which providers are connected
- what permissions are being requested
- why those permissions are needed
- what data is stored server-side
- how a user can disconnect a provider
- how a user can request deletion of stored connection data

## 8. Research and Entertainment Statement

If this statement is used publicly, it should be used in this more accurate form:

"SongZip is intended only for personal research, evaluation, and entertainment-related organization workflows. It is not intended for unauthorized redistribution, circumvention of provider controls, or misuse of third-party credentials."

This wording is more accurate than saying the service has "nothing to do with user data," because official OAuth features do require limited user authorization data to function.

## 9. Platform-Specific Compliance Commitments

### 9.1 Spotify

SongZip should:

- comply with Spotify Developer Terms and Spotify Developer Policy
- provide a privacy policy that accurately explains data access and use
- avoid misleading users about how Spotify data is used
- avoid rate-limit abuse
- avoid prohibited use of Spotify content, including restricted machine-learning uses

### 9.2 Google / YouTube

SongZip should:

- comply with the Google API Services User Data Policy
- comply with the YouTube API Services Terms of Service and related developer policies
- request only minimum relevant permissions
- accurately represent identity, intent, and data use to both users and Google
- avoid deceptive or unauthorized use of Google API Services

## 10. Public-Facing Risk Note

If SongZip is offered with paid subscriptions, broad consumer marketing, or content-copying features, then describing it publicly as "strictly for research only" may be misleading unless the actual product and business model truly match that description.

Providers and reviewers generally evaluate actual behavior, not labels alone.

## 11. Recommended Public Summary

If you want a short public statement, this is the safest version for the current direction of the project:

"SongZip is intended for personal research, evaluation, and entertainment-related organization workflows. It uses official provider authorization where supported, requests only limited permissions needed for user-facing features, and is not intended to sell personal data, use borrowed credentials, or circumvent provider rules, quotas, or copyright restrictions."

## 12. Source References

Official sources reviewed for this draft:

- Spotify Developer Policy: https://developer.spotify.com/policy
- Spotify Developer Terms: https://developer.spotify.com/terms
- Spotify Web API Rate Limits: https://developer.spotify.com/documentation/web-api/concepts/rate-limits
- Spotify Quota Modes: https://developer.spotify.com/documentation/web-api/concepts/quota-modes
- Google API Services User Data Policy: https://developers.google.com/terms/api-services-user-data-policy
- YouTube API Services Terms of Service: https://developers.google.com/youtube/terms/previews/api-services-terms-of-service-banner
- YouTube Data API quota and compliance guidance: https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits

## 13. Practical Next Steps

Before using this publicly, the operator should:

1. Make the website privacy language match the real data flow.
2. Remove any statement that says the service takes no user data if OAuth is enabled.
3. Add a visible privacy policy link and acceptable-use link in the UI.
4. Add encrypted token storage before public deployment.
5. Re-check all claims if paid subscriptions remain part of the product.
