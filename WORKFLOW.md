# Operator workflow

This describes the intended full workflow. The current scaffold only provides the UI shell and API health route; campaign operations are future phases.

## Before importing

1. Confirm the organization/list owner and the campaign's permitted purpose and audience.
2. Obtain evidence of permission or another documented lawful basis appropriate to the specific campaign and jurisdiction. Do not use scraped, purchased, or unexplained lists.
3. Preserve source, timestamp, list/purpose, and import batch. A CSV row alone is not proof of consent.

## Prepare a campaign

1. Create a client/work label and import a CSV through preview, mapping, and validation.
2. Resolve invalid addresses and duplicates. Suppressed addresses remain suppressed even if present in a new import.
3. Draft content with accurate sender identity, truthful subject, relevant content, and a working unsubscribe method. Add any required postal/address and legal details for the applicable jurisdiction.
4. Preview both HTML and plain text; send test messages only to the operator's own address.
5. Review eligible count and excluded counts by reason. Confirm consent evidence, suppression, quota/rate cap, content, and sender identity.
6. Approve and schedule only after review. Campaign is pausable and cancellable.

## During and after sending

- Recheck consent/status/suppression at dispatch. Unsubscribe takes effect immediately and removes pending work.
- Pause automatically on quota exhaustion, repeated provider errors, complaint/bounce thresholds, or event pipeline failure.
- Apply hard bounce and complaint events to the global suppression ledger. Never retry permanent failures.
- Review reports; distinguish provider acceptance from delivery. Investigate anomalies before resuming.
- Retain evidence and operational records only as long as justified by policy; support deletion/export while retaining the minimum suppression evidence needed to prevent re-imported opt-outs.

## Stop conditions

Do not launch if permission, sender identity, unsubscribe, suppression status, provider quota, or event handling cannot be verified. Do not attempt spam-filter bypass or promise inbox placement.
