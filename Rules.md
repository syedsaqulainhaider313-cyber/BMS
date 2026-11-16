# Project rules

## Product rules

- This is a single-operator internal tool, not a multi-tenant SaaS.
- Only permission-based campaigns are in scope.
- Every contact used for a campaign must have documented consent or another reviewed lawful basis appropriate to the campaign.
- Unsubscribe, hard bounce, complaint, and manual suppression are global and take precedence over import or campaign snapshots.
- Recheck eligibility immediately before provider submission; fail closed on missing state.
- Apply provider and local quotas, rate caps, pause/cancel controls, and idempotency.
- Never implement spam-filter bypass, list scraping/purchase workflows, or inbox-placement guarantees.
- Local provider remains mock until a separate approved integration phase.

## Engineering rules

- Never commit secrets, real recipient lists, exports, or production message content.
- Provider credentials stay server-side and must be masked in logs and UI.
- Validate input at boundaries; cap file size and queue volume.
- Sanitize HTML; escape personalization values; protect exports from formula injection.
- Keep API contracts and working docs synchronized with behavior.
- Record security-sensitive and campaign lifecycle actions in an audit log.
- Any live-provider integration requires tests for suppression, quota, retries, webhooks, and emergency pause.
