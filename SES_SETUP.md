# Future Amazon SES setup (deferred)

This is a preparation checklist only. No AWS resources, credentials, domains, or paid services have been created or configured. Do this only after the application, compliance workflow, and production controls are ready and the operator explicitly approves the account, region, and spend.

## Decisions to make later

- AWS account ownership and MFA; least-privilege operator access.
- Region based on data location, recipient geography, latency, and SES availability.
- Sending domain/subdomain and verified From address.
- SES API versus SMTP adapter. Prefer the AWS SDK/API when practical; SMTP remains an adapter option.
- Budget, daily/second rate caps, staged ramp, and emergency stop owner.
- Event path for bounces, complaints, delivery, and rejects (for example, SNS/EventBridge/SQS choices based on current AWS design).

## Future checklist

1. Review current AWS SES pricing, service terms, region limits, and deliverability requirements before purchase/configuration.
2. Verify the domain and sender identity in the selected region.
3. Publish SPF and DKIM records supplied by SES; publish a carefully planned DMARC policy. Verify DNS propagation and alignment.
4. Request production access if the account is in the SES sandbox. Provide accurate use-case, consent, unsubscribe, bounce/complaint, and list hygiene details.
5. Create narrowly scoped credentials or a workload role. Store secrets in a secret manager; never put them in frontend assets, repository, logs, or `.env` committed to Git.
6. Configure signed, replay-resistant event delivery. Map hard bounce and complaint to global suppression; test duplicates and malformed signatures.
7. Enforce SES quota and application caps independently. Pause on threshold breach, quota errors, missing events, or elevated complaints.
8. Test in staging with synthetic/test addresses. Then run a small campaign only to opted-in recipients and monitor events before increasing volume.
9. Document credential rotation, incident response, provider pause, and rollback to mock mode.

SMTP, if selected later: use the region-specific SES endpoint, TLS, and SES-generated SMTP credentials derived for the selected region. Do not assume endpoint, port, quotas, pricing, or access status without checking current official AWS documentation at setup time.
