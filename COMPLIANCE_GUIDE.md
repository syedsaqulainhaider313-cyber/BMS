# Legal Compliance, List Ownership, and Consent Policy

This policy document defines the statutory obligations, list intake standards, and consent verification rules required for operating the Bulk Mail Management System (BMS).

---

## 1. Regulatory Frameworks

Operating bulk communications requires compliance with relevant jurisdiction-specific laws:

| Standard | Key Requirements | BMS Implementation |
|---|---|---|
| **GDPR** (EU/UK) | Article 6(1)(a) explicit affirmative consent; Article 7 condition for consent; Article 17 Right to Erasure. | Exact consent purpose, timestamp, source, and provenance recorded on intake; audited manual contact deletion. |
| **CAN-SPAM** (US) | Accurate header data; valid postal address; conspicuous unsubscribe mechanism honored within 10 business days. | Template validation strictly enforces sender identity, valid physical postal address, and exactly one `{{unsubscribe_url}}`. |
| **CASL** (Canada) | Express consent verification; clear sender identification; easy and cost-free unsubscribe mechanism. | Unsubscribe operates immediately and idempotently upon receipt via signed HMAC token. |

---

## 2. List Sourcing & Ownership Contract

The operator certifies that:
1. **Zero Tolerance for Scraped or Purchased Lists**:
   - Lists harvested from the web, directories, social media, or purchased from third-party data brokers are strictly prohibited.
   - Every contact must originate from a first-party relationship where explicit consent was recorded.
2. **Mandatory Intake Columns**:
   Every imported CSV must map the following 5 affirmative evidence fields:
   - `Email`: Valid RFC 5322 email address.
   - `ConsentSource`: Method of intake (e.g., `web_signup_form`, `checkout_opt_in`).
   - `ConsentDate`: ISO 8601 UTC timestamp of affirmative consent.
   - `ConsentPurpose`: Specific scope of communication (e.g., `product_updates`, `security_announcements`).
   - `Evidence`: Audit trail identifier (e.g., `ip_address=192.0.2.10`, `order_id=4021`).
3. **Suppression Precedence Rule**:
   - Global suppression takes absolute priority over all imports.
   - If an email was previously suppressed (via manual operator entry, hard bounce, complaint, or unsubscribe), CSV intake will never restore its eligibility or erase its suppression status.
   - Suppression records remain even if an underlying contact is manually redacted for GDPR right-to-erasure compliance, ensuring no future import can re-contact an opted-out individual.

---

## 3. Pre-Launch Verification Gate

Before approving and dispatching any campaign, the operator must confirm:
- [ ] List was collected directly from affirmative subscriber opt-ins.
- [ ] Campaign subject and sender name accurately identify the sender.
- [ ] Template contains a valid physical postal address.
- [ ] Message content aligns strictly with the specified `ConsentPurpose`.
- [ ] Unsubscribe placeholder `{{unsubscribe_url}}` is present and functional.
