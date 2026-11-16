# Design principles

## Visual direction

Use a calm, high-contrast operations dashboard: dark slate surfaces, restrained mint accent, readable typography, compact but spacious data tables, and strong distinction between safe local mode and production state. Avoid celebratory or growth-hacking cues around sending volume.

## Product language

- Say “permission-based contacts”, “eligible”, “excluded”, “suppressed”, and “provider accepted”.
- Explain why a recipient is excluded.
- Use “send” only when a real provider submission occurs. In mock mode, say “simulate”.
- Never imply that acceptance guarantees inbox placement.

## Accessibility

- Semantic headings, landmarks, buttons, labels, and table headers.
- Keyboard-visible focus and complete keyboard operation.
- Status indicators include text/icon in addition to color.
- Meet WCAG 2.2 AA contrast targets and support zoom/reflow.
- Announce asynchronous import and campaign state changes to assistive technology.
- Forms identify required consent evidence and validation errors in text.

## Safety-sensitive UX

- Keep mode/provider status persistent in the application shell.
- Require review of eligible audience size, exclusions, content, and rate limit before approval.
- Give pause/cancel controls clear visibility during campaign execution.
- Present unsubscribe and complaint state as non-overridable protections.
- Avoid interface patterns that encourage sending to larger audiences without permission evidence.
