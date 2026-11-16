import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "./api";

type Operator = { username: string };
type LoginResponse = { username?: string; csrf_token?: string; mfa_required?: boolean; message?: string };
type ImportPreview = {
  batch_id: number;
  commit_token: string;
  expires_at: string;
  headers: string[];
  counts: Record<string, number>;
  rows: { row: number; status: string; errors: string[] }[];
};
type TemplateDraft = {
  name: string;
  subject: string;
  sender_name: string;
  sender_email: string;
  mailing_address: string;
  html_body: string;
  text_body: string;
};
type SavedTemplate = TemplateDraft & { id: number; created_at: string; updated_at: string };
type CampaignItem = {
  id: number;
  name: string;
  template_id: number;
  consent_purpose: string;
  status: string;
  recipient_count: number;
};
type Eligibility = {
  eligible_count: number;
  excluded_count: number;
  eligible: { contact_id: number; email: string; consent_record_id: number }[];
  excluded: { contact_id: number; reason: string }[];
};
type CampaignEvent = {
  event_type: string;
  detail: string | null;
  outbox_id: number | null;
  created_at: string;
};
type CampaignReport = {
  campaign_id: number;
  campaign_name: string;
  campaign_status: string;
  metrics: Record<string, number>;
  reporting_note: string;
};
type DashboardSummary = {
  active_campaigns: number;
  contacts_with_consent: number;
  accepted_messages: number;
  suppressed_addresses: number;
};
type ContactRow = {
  id: number;
  email: string;
  display_name: string | null;
  consent_records: number;
  created_at: string;
};
type SuppressionRow = {
  email: string;
  reason: string;
  source: string;
  detail: string | null;
  created_at: string;
};

type ConfirmModalState = {
  title: string;
  description: string;
  confirmLabel: string;
  isDestructive?: boolean;
  onConfirm: () => void | Promise<void>;
} | null;

type AuditLogEntry = {
  id: number;
  actor: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  detail: string | null;
  created_at: string | null;
};

type AlertEntry = {
  level: "warning" | "critical";
  code: string;
  message: string;
};

type SystemAlertsResponse = {
  status: "healthy" | "warning" | "critical";
  lagging_outbox_jobs: number;
  dispatched_24h: number;
  bounces_24h: number;
  complaints_24h: number;
  bounce_rate_pct: number;
  complaint_rate_pct: number;
  active_alerts: AlertEntry[];
};

const emptyTemplate: TemplateDraft = {
  name: "",
  subject: "",
  sender_name: "",
  sender_email: "",
  mailing_address: "",
  html_body: "<h1>Hello</h1>\n<p>Your message here.</p>\n<p><a href=\"{{unsubscribe_url}}\">Unsubscribe</a></p>",
  text_body: "Hello\n\nYour message here.\n\nUnsubscribe: {{unsubscribe_url}}",
};

export function App() {
  const [operator, setOperator] = useState<Operator | null>(null);
  const [csrf, setCsrf] = useState("");
  const [username, setUsername] = useState("operator");
  const [password, setPassword] = useState("");
  const [mfaRequired, setMfaRequired] = useState(false);
  const [totpCode, setTotpCode] = useState("");
  const [authError, setAuthError] = useState("");
  const [checking, setChecking] = useState(true);

  // Active section tab
  const [activeSection, setActiveSection] = useState<"overview" | "contacts" | "templates" | "campaigns" | "reports" | "suppression" | "settings">("overview");

  // In-flight action tracking
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [confirmModal, setConfirmModal] = useState<ConfirmModalState>(null);

  // Data states
  const [dashboardSummary, setDashboardSummary] = useState<DashboardSummary | null>(null);
  const [dashboardError, setDashboardError] = useState("");
  const [healthStatus, setHealthStatus] = useState<{ status: string; email_provider: string } | null>(null);

  // Contacts & Import
  const [contacts, setContacts] = useState<ContactRow[]>([]);
  const [contactSearch, setContactSearch] = useState("");
  const [csv, setCsv] = useState<File | null>(null);
  const [headers, setHeaders] = useState<string[]>([]);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [importMessage, setImportMessage] = useState("");
  const [importError, setImportError] = useState("");

  // Templates
  const [savedTemplates, setSavedTemplates] = useState<SavedTemplate[]>([]);
  const [template, setTemplate] = useState<TemplateDraft>(emptyTemplate);
  const [editingTemplateId, setEditingTemplateId] = useState<number | null>(null);
  const [templatePreview, setTemplatePreview] = useState<{ subject: string; sender: string; mailing_address: string; html_body: string; text_body: string } | null>(null);
  const [templateMessage, setTemplateMessage] = useState("");
  const [templateError, setTemplateError] = useState("");

  // Campaigns
  const [campaigns, setCampaigns] = useState<CampaignItem[]>([]);
  const [campaignSearch, setCampaignSearch] = useState("");
  const [campaignName, setCampaignName] = useState("");
  const [campaignPurpose, setCampaignPurpose] = useState("");
  const [campaignTemplateId, setCampaignTemplateId] = useState("");
  const [selectedCampaignId, setSelectedCampaignId] = useState<number | null>(null);
  const [eligibility, setEligibility] = useState<Eligibility | null>(null);
  const [campaignMessage, setCampaignMessage] = useState("");
  const [campaignError, setCampaignError] = useState("");
  const [campaignEventRows, setCampaignEventRows] = useState<CampaignEvent[]>([]);
  const [campaignReport, setCampaignReport] = useState<CampaignReport | null>(null);

  // Suppressions
  const [suppressions, setSuppressions] = useState<SuppressionRow[]>([]);
  const [suppressionEmail, setSuppressionEmail] = useState("");
  const [suppressionDetail, setSuppressionDetail] = useState("");
  const [suppressionMessage, setSuppressionMessage] = useState("");
  const [suppressionError, setSuppressionError] = useState("");

  // Audit Logs & System Alerts
  const [auditLogs, setAuditLogs] = useState<AuditLogEntry[]>([]);
  const [systemAlerts, setSystemAlerts] = useState<SystemAlertsResponse | null>(null);

  async function loadDashboardSummary() {
    try {
      const summary = await api<DashboardSummary>("/dashboard/summary");
      setDashboardSummary(summary);
      setDashboardError("");
    } catch (err) {
      setDashboardError(err instanceof Error ? err.message : "Could not load overview metrics");
    }
  }

  async function loadHealth() {
    try {
      const h = await api<{ status: string; email_provider: string }>("/health");
      setHealthStatus(h);
    } catch {
      setHealthStatus(null);
    }
  }

  async function loadContacts() {
    try {
      const list = await api<ContactRow[]>("/contacts");
      setContacts(list);
    } catch {
      // Ignore initial load failure if unauthenticated
    }
  }

  async function loadTemplates() {
    try {
      const list = await api<SavedTemplate[]>("/templates");
      setSavedTemplates(list);
    } catch {
      // Ignore
    }
  }

  async function loadCampaigns() {
    try {
      const list = await api<CampaignItem[]>("/campaigns");
      setCampaigns(list);
    } catch {
      // Ignore
    }
  }

  async function loadSuppressions() {
    try {
      const list = await api<SuppressionRow[]>("/suppressions");
      setSuppressions(list);
    } catch {
      // Ignore
    }
  }

  async function loadAuditLogs() {
    try {
      const logs = await api<AuditLogEntry[]>("/audit-logs?limit=50");
      setAuditLogs(logs);
    } catch {
      // Ignore
    }
  }

  async function loadSystemAlerts() {
    try {
      const alerts = await api<SystemAlertsResponse>("/system/alerts");
      setSystemAlerts(alerts);
    } catch {
      // Ignore
    }
  }

  // Session check on mount
  useEffect(() => {
    api<Operator>("/auth/me")
      .then(async (current) => {
        setOperator(current);
        const csrfState = await api<{ csrf_token: string }>("/auth/csrf");
        setCsrf(csrfState.csrf_token);
      })
      .catch(() => {})
      .finally(() => setChecking(false));
  }, []);

  // Load data when authenticated
  useEffect(() => {
    if (operator) {
      void loadDashboardSummary();
      void loadHealth();
      void loadContacts();
      void loadTemplates();
      void loadCampaigns();
      void loadSuppressions();
      void loadAuditLogs();
      void loadSystemAlerts();
    }
  }, [operator]);

  // Auth actions
  async function signIn(event: FormEvent) {
    event.preventDefault();
    setAuthError("");
    setPendingAction("signin");
    try {
      const payload: { username: string; password: string; totp_code?: string } = { username, password };
      if (mfaRequired && totpCode) {
        payload.totp_code = totpCode.trim();
      }
      const result = await api<LoginResponse>("/auth/login", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (result.mfa_required) {
        setMfaRequired(true);
        return;
      }
      if (result.username && result.csrf_token) {
        setOperator({ username: result.username });
        setCsrf(result.csrf_token);
        setPassword("");
        setTotpCode("");
        setMfaRequired(false);
      }
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setPendingAction(null);
    }
  }

  async function signOut() {
    setPendingAction("signout");
    try {
      await api("/auth/logout", {
        method: "POST",
        headers: { "X-CSRF-Token": csrf },
      });
    } finally {
      setOperator(null);
      setCsrf("");
      setPendingAction(null);
    }
  }

  // CSV intake
  async function readHeaders(file: File | null) {
    setCsv(file);
    setPreview(null);
    setImportMessage("");
    setImportError("");
    if (!file) {
      setHeaders([]);
      return;
    }
    const text = await file.slice(0, 100_000).text();
    const first = text.split(/\r?\n/, 1)[0] || "";
    const parsed = first.split(",").map((s) => s.trim().replace(/^"|"$/g, ""));
    setHeaders(parsed);
    const guess = (patterns: RegExp[]) => parsed.find((h) => patterns.some((p) => p.test(h))) || "";
    setMapping({
      email: guess([/^email$/i, /e-mail/i]),
      consent_source: guess([/consent.*source/i, /signup.*source/i]),
      consented_at: guess([/consent.*date/i, /consent.*time/i]),
      consent_purpose: guess([/purpose/i, /list/i]),
      consent_provenance: guess([/provenance/i, /evidence/i]),
      display_name: guess([/^name$/i, /display.*name/i]),
    });
  }

  async function requestPreview(event: FormEvent) {
    event.preventDefault();
    if (!csv) return;
    setImportError("");
    setImportMessage("");
    setPendingAction("preview_import");
    const form = new FormData();
    form.append("file", csv);
    form.append("mapping_json", JSON.stringify(mapping));
    try {
      const response = await fetch("/api/imports/preview", {
        method: "POST",
        body: form,
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf },
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Preview failed");
      setPreview(body as ImportPreview);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : "Preview failed");
    } finally {
      setPendingAction(null);
    }
  }

  function promptCommitImport() {
    if (!preview) return;
    setConfirmModal({
      title: "Commit Contact Import",
      description: `Commit ${preview.counts.valid} contacts with validated consent evidence into the database? Suppressed addresses will be filtered out.`,
      confirmLabel: "Commit Contacts",
      onConfirm: async () => {
        setPendingAction("commit_import");
        setImportError("");
        try {
          const result = await api<{ created: number; skipped_at_commit: number }>(
            `/imports/${preview.batch_id}/commit`,
            {
              method: "POST",
              headers: { "X-CSRF-Token": csrf },
              body: JSON.stringify({ commit_token: preview.commit_token }),
            }
          );
          setImportMessage(
            `Successfully imported ${result.created} contacts. (${result.skipped_at_commit} skipped due to changes).`
          );
          setPreview(null);
          setCsv(null);
          void loadContacts();
          void loadDashboardSummary();
        } catch (err) {
          setImportError(err instanceof Error ? err.message : "Commit failed");
        } finally {
          setPendingAction(null);
        }
      },
    });
  }

  // Templates
  async function previewTemplateAction(event: FormEvent) {
    event.preventDefault();
    setTemplateError("");
    setTemplateMessage("");
    setPendingAction("preview_template");
    try {
      const rendered = await api<{
        subject: string;
        sender: string;
        mailing_address: string;
        html_body: string;
        text_body: string;
      }>("/templates/preview", {
        method: "POST",
        headers: { "X-CSRF-Token": csrf },
        body: JSON.stringify(template),
      });
      setTemplatePreview(rendered);
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : "Preview rendering failed");
    } finally {
      setPendingAction(null);
    }
  }

  async function saveTemplateAction(event: FormEvent) {
    event.preventDefault();
    setTemplateError("");
    setTemplateMessage("");
    setPendingAction("save_template");
    try {
      const path = editingTemplateId ? `/templates/${editingTemplateId}` : "/templates";
      const method = editingTemplateId ? "PUT" : "POST";
      const saved = await api<SavedTemplate>(path, {
        method,
        headers: { "X-CSRF-Token": csrf },
        body: JSON.stringify(template),
      });
      setSavedTemplates((cur) => [saved, ...cur.filter((t) => t.id !== saved.id)]);
      setEditingTemplateId(saved.id);
      setTemplateMessage("Template saved successfully.");
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setPendingAction(null);
    }
  }

  function promptDeleteTemplate() {
    if (!editingTemplateId) return;
    const name = template.name || "this template";
    setConfirmModal({
      title: "Delete Email Template",
      description: `Permanently delete "${name}"? Existing approved campaigns will retain their frozen copies.`,
      confirmLabel: "Delete Template",
      isDestructive: true,
      onConfirm: async () => {
        setPendingAction("delete_template");
        try {
          await api(`/templates/${editingTemplateId}`, {
            method: "DELETE",
            headers: { "X-CSRF-Token": csrf },
          });
          setSavedTemplates((cur) => cur.filter((t) => t.id !== editingTemplateId));
          setEditingTemplateId(null);
          setTemplate(emptyTemplate);
          setTemplatePreview(null);
          setTemplateMessage("Template deleted.");
        } catch (err) {
          setTemplateError(err instanceof Error ? err.message : "Delete failed");
        } finally {
          setPendingAction(null);
        }
      },
    });
  }

  // Campaigns
  async function createCampaignAction(event: FormEvent) {
    event.preventDefault();
    setCampaignError("");
    setCampaignMessage("");
    setPendingAction("create_campaign");
    try {
      const created = await api<CampaignItem>("/campaigns", {
        method: "POST",
        headers: { "X-CSRF-Token": csrf },
        body: JSON.stringify({
          name: campaignName,
          template_id: Number(campaignTemplateId),
          consent_purpose: campaignPurpose,
        }),
      });
      setCampaigns((cur) => [created, ...cur]);
      setSelectedCampaignId(created.id);
      setCampaignName("");
      const report = await api<Eligibility>(`/campaigns/${created.id}/eligibility`);
      setEligibility(report);
      setCampaignMessage(`Draft created. ${report.eligible_count} eligible contacts found for "${campaignPurpose}".`);
      void loadDashboardSummary();
    } catch (err) {
      setCampaignError(err instanceof Error ? err.message : "Campaign creation failed");
    } finally {
      setPendingAction(null);
    }
  }

  async function previewEligibilityAction(id: number) {
    setSelectedCampaignId(id);
    setCampaignError("");
    setCampaignMessage("");
    setPendingAction(`eligibility_${id}`);
    try {
      const report = await api<Eligibility>(`/campaigns/${id}/eligibility`);
      setEligibility(report);
    } catch (err) {
      setCampaignError(err instanceof Error ? err.message : "Eligibility preview failed");
    } finally {
      setPendingAction(null);
    }
  }

  function promptApproveCampaign(id: number, name: string) {
    setConfirmModal({
      title: "Approve Campaign & Freeze Audience",
      description: `Approve "${name}"? This freezes the message content and recipient snapshot. No further recipient additions can occur.`,
      confirmLabel: "Approve Snapshot",
      onConfirm: async () => {
        setPendingAction(`approve_${id}`);
        setCampaignError("");
        try {
          const approved = await api<CampaignItem>(`/campaigns/${id}/approve`, {
            method: "POST",
            headers: { "X-CSRF-Token": csrf },
          });
          setCampaigns((cur) => cur.map((c) => (c.id === id ? approved : c)));
          setCampaignMessage(
            `Campaign approved with ${approved.recipient_count} frozen recipients. Ready to queue.`
          );
          void loadDashboardSummary();
        } catch (err) {
          setCampaignError(err instanceof Error ? err.message : "Approval failed");
        } finally {
          setPendingAction(null);
        }
      },
    });
  }

  function promptTransitionCampaign(id: number, name: string, action: "pause" | "resume" | "cancel") {
    if (action === "cancel") {
      setConfirmModal({
        title: "Cancel Campaign",
        description: `Cancel "${name}"? All pending or retry mock jobs will be marked terminal FAILED and cannot be resumed.`,
        confirmLabel: "Cancel Campaign",
        isDestructive: true,
        onConfirm: () => doTransition(id, action),
      });
    } else {
      void doTransition(id, action);
    }
  }

  async function doTransition(id: number, action: "pause" | "resume" | "cancel") {
    setPendingAction(`transition_${id}_${action}`);
    try {
      const updated = await api<CampaignItem>(`/campaigns/${id}/${action}`, {
        method: "POST",
        headers: { "X-CSRF-Token": csrf },
      });
      setCampaigns((cur) => cur.map((c) => (c.id === id ? updated : c)));
      setCampaignMessage(
        action === "cancel"
          ? "Campaign cancelled. Pending outbox jobs are now terminal."
          : `Campaign ${action}d.`
      );
      void loadDashboardSummary();
    } catch (err) {
      setCampaignError(err instanceof Error ? err.message : `Could not ${action} campaign`);
    } finally {
      setPendingAction(null);
    }
  }

  async function queueMockCampaignAction(id: number) {
    setPendingAction(`queue_${id}`);
    setCampaignError("");
    try {
      const result = await api<{ status: string; new_jobs: number }>(`/campaigns/${id}/enqueue-mock`, {
        method: "POST",
        headers: { "X-CSRF-Token": csrf },
      });
      setCampaignMessage(`Enqueued ${result.new_jobs} mock jobs into database outbox.`);
      setCampaignEventRows(await api<CampaignEvent[]>(`/campaigns/${id}/events`));
      void loadDashboardSummary();
    } catch (err) {
      setCampaignError(err instanceof Error ? err.message : "Queueing failed");
    } finally {
      setPendingAction(null);
    }
  }

  async function loadCampaignEventsAction(id: number) {
    setSelectedCampaignId(id);
    setPendingAction(`events_${id}`);
    try {
      const events = await api<CampaignEvent[]>(`/campaigns/${id}/events`);
      setCampaignEventRows(events);
    } catch (err) {
      setCampaignError(err instanceof Error ? err.message : "Could not load events");
    } finally {
      setPendingAction(null);
    }
  }

  function promptDeleteEvents(id: number, name: string) {
    setConfirmModal({
      title: "Delete Event Timeline",
      description: `Permanently delete detailed event timeline for "${name}"? Aggregate report metrics and outbox records remain intact.`,
      confirmLabel: "Delete Event Timeline",
      isDestructive: true,
      onConfirm: async () => {
        setPendingAction(`del_events_${id}`);
        try {
          await api(`/campaigns/${id}/events`, {
            method: "DELETE",
            headers: { "X-CSRF-Token": csrf },
          });
          if (selectedCampaignId === id) setCampaignEventRows([]);
          setCampaignMessage("Event timeline deleted and audited.");
        } catch (err) {
          setCampaignError(err instanceof Error ? err.message : "Could not delete events");
        } finally {
          setPendingAction(null);
        }
      },
    });
  }

  async function loadCampaignReportAction(id: number) {
    setSelectedCampaignId(id);
    setPendingAction(`report_${id}`);
    try {
      const report = await api<CampaignReport>(`/campaigns/${id}/report`);
      setCampaignReport(report);
    } catch (err) {
      setCampaignError(err instanceof Error ? err.message : "Could not load report");
    } finally {
      setPendingAction(null);
    }
  }

  async function downloadCampaignExport(id: number) {
    const response = await fetch(`/api/campaigns/${id}/export.csv`, { credentials: "same-origin" });
    if (!response.ok) {
      setCampaignError("Export download failed.");
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `campaign-${id}-report.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  // Suppressions
  async function addManualSuppressionAction(event: FormEvent) {
    event.preventDefault();
    setSuppressionError("");
    setSuppressionMessage("");
    setPendingAction("add_suppression");
    try {
      const result = await api<{ email: string; reason: string }>("/suppressions", {
        method: "POST",
        headers: { "X-CSRF-Token": csrf },
        body: JSON.stringify({ email: suppressionEmail, detail: suppressionDetail || "Operator manual entry" }),
      });
      setSuppressionMessage(`${result.email} added to global suppression (${result.reason}).`);
      setSuppressionEmail("");
      setSuppressionDetail("");
      void loadSuppressions();
      void loadDashboardSummary();
    } catch (err) {
      setSuppressionError(err instanceof Error ? err.message : "Could not add suppression");
    } finally {
      setPendingAction(null);
    }
  }

  // Contact Deletion
  function promptDeleteContact(contact: ContactRow) {
    setConfirmModal({
      title: "Delete Contact & Redact Personal Data",
      description: `Permanently delete ${contact.email}? This will erase consent records, redact personal details from past campaign snapshots and outbox records. Global suppression, if active, will be retained to prevent accidental re-imports.`,
      confirmLabel: "Permanently Delete",
      isDestructive: true,
      onConfirm: async () => {
        setPendingAction(`del_contact_${contact.id}`);
        try {
          await api(`/contacts/${contact.id}`, {
            method: "DELETE",
            headers: { "X-CSRF-Token": csrf },
          });
          setContacts((cur) => cur.filter((c) => c.id !== contact.id));
          void loadDashboardSummary();
        } catch (err) {
          setImportError(err instanceof Error ? err.message : "Contact deletion failed");
        } finally {
          setPendingAction(null);
        }
      },
    });
  }

  // Filtered lists
  const filteredContacts = useMemo(() => {
    if (!contactSearch.trim()) return contacts;
    const q = contactSearch.toLowerCase();
    return contacts.filter(
      (c) => c.email.toLowerCase().includes(q) || (c.display_name && c.display_name.toLowerCase().includes(q))
    );
  }, [contacts, contactSearch]);

  const filteredCampaigns = useMemo(() => {
    if (!campaignSearch.trim()) return campaigns;
    const q = campaignSearch.toLowerCase();
    return campaigns.filter(
      (c) => c.name.toLowerCase().includes(q) || c.consent_purpose.toLowerCase().includes(q)
    );
  }, [campaigns, campaignSearch]);

  // Loading skeleton during session verification
  if (checking) {
    return (
      <div className="grid min-h-screen place-items-center bg-slate-950 text-slate-400">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent border-t-transparent" />
          <p className="text-sm font-medium">Loading private workspace…</p>
        </div>
      </div>
    );
  }

  // Operator Sign In
  if (!operator) {
    return (
      <main className="grid min-h-screen place-items-center bg-slate-950 px-5 text-slate-100">
        <div className="w-full max-w-md rounded-2xl border border-white/10 bg-slate-900/60 p-8 shadow-2xl backdrop-blur-xl">
          <div className="mb-6 flex items-center gap-3">
            <span className="grid h-11 w-11 place-items-center rounded-xl bg-accent font-bold text-ink shadow-md">
              B
            </span>
            <div>
              <p className="font-semibold text-slate-100">BMS</p>
              <p className="text-xs text-slate-400">Bulk Mail Management System</p>
            </div>
          </div>

          <h1 className="text-2xl font-bold tracking-tight text-slate-100">Operator Sign In</h1>
          <p className="mt-2 text-sm leading-6 text-slate-400">
            Private internal tool. Public registration and external unauthenticated access are disabled.
          </p>

          <form onSubmit={signIn} className="mt-6 space-y-4">
            {!mfaRequired ? (
              <>
                <div>
                  <label htmlFor="username" className="block text-xs font-medium text-slate-300">
                    Operator Username
                  </label>
                  <input
                    id="username"
                    type="text"
                    required
                    autoComplete="username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-950 px-3.5 py-2.5 text-sm text-slate-100 outline-none transition focus:border-accent focus:ring-1 focus:ring-accent"
                  />
                </div>

                <div>
                  <label htmlFor="password" className="block text-xs font-medium text-slate-300">
                    Password
                  </label>
                  <input
                    id="password"
                    type="password"
                    required
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-950 px-3.5 py-2.5 text-sm text-slate-100 outline-none transition focus:border-accent focus:ring-1 focus:ring-accent"
                  />
                </div>
              </>
            ) : (
              <div className="space-y-3">
                <div className="rounded-lg border border-accent/20 bg-accent/10 p-3 text-xs text-accent">
                  <p className="font-semibold">Two-Factor Authentication Active</p>
                  <p className="mt-0.5 text-slate-300">Enter the 6-digit code from your authenticator app (Google Authenticator, Bitwarden, 1Password, etc.).</p>
                </div>
                <div>
                  <label htmlFor="totpCode" className="block text-xs font-medium text-slate-300">
                    Authentication Code (TOTP)
                  </label>
                  <input
                    id="totpCode"
                    type="text"
                    inputMode="numeric"
                    autoFocus
                    required
                    maxLength={6}
                    placeholder="123456"
                    value={totpCode}
                    onChange={(e) => setTotpCode(e.target.value)}
                    className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-950 px-3.5 py-2.5 text-center text-lg font-mono tracking-widest text-slate-100 outline-none transition focus:border-accent focus:ring-1 focus:ring-accent"
                  />
                </div>
              </div>
            )}

            {authError && (
              <div role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-300">
                {authError}
              </div>
            )}

            <button
              type="submit"
              disabled={pendingAction === "signin"}
              className="mt-2 flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-4 py-3 font-semibold text-ink shadow transition hover:opacity-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
            >
              {pendingAction === "signin" ? (
                <>
                  <div className="h-4 w-4 animate-spin rounded-full border-2 border-ink border-t-transparent" />
                  <span>Verifying…</span>
                </>
              ) : mfaRequired ? (
                "Verify Code & Sign In"
              ) : (
                "Continue to Dashboard"
              )}
            </button>

            {mfaRequired && (
              <button
                type="button"
                onClick={() => {
                  setMfaRequired(false);
                  setTotpCode("");
                  setAuthError("");
                }}
                className="w-full text-center text-xs text-slate-400 hover:text-slate-200"
              >
                ← Back to credentials
              </button>
            )}
          </form>

          <div className="mt-6 border-t border-white/5 pt-4 text-center">
            <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-400/20 bg-amber-400/10 px-2.5 py-1 text-[11px] font-medium text-amber-300">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />
              MOCK MODE · No real network dispatch
            </span>
          </div>
        </div>
      </main>
    );
  }

  const navItems = [
    { id: "overview", label: "Overview", icon: "📊" },
    { id: "contacts", label: "Contacts & Import", icon: "👥" },
    { id: "templates", label: "Email Templates", icon: "✉️" },
    { id: "campaigns", label: "Campaigns", icon: "🚀" },
    { id: "reports", label: "Reports & Logs", icon: "📈" },
    { id: "suppression", label: "Suppression", icon: "🛡️" },
    { id: "settings", label: "Settings & System", icon: "⚙️" },
  ] as const;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto flex min-h-screen max-w-7xl flex-col md:flex-row">
        {/* Sidebar */}
        <aside className="hidden w-64 shrink-0 flex-col justify-between border-r border-white/10 bg-slate-950 p-6 md:flex">
          <div>
            <div className="mb-8 flex items-center gap-3">
              <span className="grid h-10 w-10 place-items-center rounded-xl bg-accent font-bold text-ink shadow">
                B
              </span>
              <div>
                <p className="font-bold text-slate-100">BMS</p>
                <p className="text-xs text-slate-400">Bulk Mail Manager</p>
              </div>
            </div>

            <nav className="space-y-1.5" aria-label="Main Navigation">
              {navItems.map((item) => (
                <button
                  key={item.id}
                  onClick={() => setActiveSection(item.id)}
                  aria-current={activeSection === item.id ? "page" : undefined}
                  className={`flex w-full items-center gap-3 rounded-xl px-3.5 py-2.5 text-sm font-medium transition ${
                    activeSection === item.id
                      ? "bg-accent/15 text-accent shadow-sm"
                      : "text-slate-400 hover:bg-white/[0.04] hover:text-slate-200"
                  }`}
                >
                  <span className="text-base">{item.icon}</span>
                  <span>{item.label}</span>
                </button>
              ))}
            </nav>
          </div>

          <div className="rounded-xl border border-white/10 bg-slate-900/40 p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs font-semibold text-slate-300">Operator</p>
                <p className="text-xs text-slate-400 truncate max-w-[120px]">{operator.username}</p>
              </div>
              <button
                onClick={() => void signOut()}
                className="rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1 text-xs text-slate-300 transition hover:bg-rose-500/20 hover:text-rose-300"
              >
                Sign out
              </button>
            </div>
            <div className="mt-3 flex items-center gap-1.5 text-[11px] text-accent">
              <span className="h-1.5 w-1.5 rounded-full bg-accent animate-pulse" />
              <span>Isolated Local Mode</span>
            </div>
          </div>
        </aside>

        {/* Mobile Nav */}
        <div className="flex overflow-x-auto border-b border-white/10 bg-slate-950 p-2 md:hidden">
          {navItems.map((item) => (
            <button
              key={item.id}
              onClick={() => setActiveSection(item.id)}
              className={`whitespace-nowrap rounded-lg px-3 py-2 text-xs font-medium transition ${
                activeSection === item.id
                  ? "bg-accent/15 text-accent"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              <span className="mr-1.5">{item.icon}</span>
              {item.label}
            </button>
          ))}
        </div>

        {/* Main Content Area */}
        <main className="min-w-0 flex-1 p-5 sm:p-8 lg:p-10">
          {/* Header Banner */}
          <header className="mb-8 flex flex-wrap items-center justify-between gap-4">
            <div>
              <p className="text-xs font-medium uppercase tracking-wider text-slate-400">
                Workspace / {activeSection.toUpperCase()}
              </p>
              <h1 className="mt-1 text-2xl sm:text-3xl font-bold tracking-tight text-slate-100">
                {activeSection === "overview" && "System Dashboard"}
                {activeSection === "contacts" && "Contact Directory & Intake"}
                {activeSection === "templates" && "Sanitized Email Templates"}
                {activeSection === "campaigns" && "Campaign Review & Approval"}
                {activeSection === "reports" && "Delivery Reports & Audit Timeline"}
                {activeSection === "suppression" && "Global Suppression List"}
                {activeSection === "settings" && "System Settings & Webhook Status"}
              </h1>
            </div>

            <div className="flex items-center gap-3">
              <span className="rounded-full border border-accent/30 bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent">
                MOCK ONLY · ZERO SPEND
              </span>
            </div>
          </header>

          {/* Section: Overview */}
          {activeSection === "overview" && (
            <div className="space-y-6">
              {/* Safeguard Alert */}
              <div className="rounded-2xl border border-amber-300/25 bg-amber-300/[0.06] p-5">
                <div className="flex items-start gap-3">
                  <span className="text-xl">🔒</span>
                  <div>
                    <h2 className="text-sm font-semibold text-amber-200">
                      Permission-Based Safeguards Active
                    </h2>
                    <p className="mt-1 text-xs leading-5 text-amber-200/80">
                      This environment operates in local mock mode. All campaign snapshots freeze verified consent records and recheck global suppression at dispatch. No email leaves the machine.
                    </p>
                  </div>
                </div>
              </div>

              {/* Active Operational Alerts */}
              {systemAlerts && systemAlerts.active_alerts.length > 0 && (
                <div className="rounded-2xl border border-rose-500/30 bg-rose-500/10 p-5">
                  <div className="flex items-center gap-2">
                    <span className="text-lg">⚠️</span>
                    <h3 className="text-sm font-semibold text-rose-200">
                      Active Operational Alerts ({systemAlerts.active_alerts.length})
                    </h3>
                  </div>
                  <div className="mt-3 space-y-2">
                    {systemAlerts.active_alerts.map((alert, idx) => (
                      <div
                        key={idx}
                        className={`rounded-lg border p-3 text-xs ${
                          alert.level === "critical"
                            ? "border-rose-400/40 bg-rose-950/40 text-rose-200"
                            : "border-amber-400/40 bg-amber-950/40 text-amber-200"
                        }`}
                      >
                        <span className="font-semibold uppercase tracking-wider text-[10px] mr-2">
                          [{alert.level}]
                        </span>
                        {alert.message}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* KPI Cards */}
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-slate-300">Operational Health Metrics</h2>
                <button
                  onClick={() => void loadDashboardSummary()}
                  className="rounded-lg border border-white/10 bg-slate-900/60 px-3 py-1.5 text-xs text-slate-300 transition hover:bg-white/[0.08]"
                >
                  Refresh Metrics
                </button>
              </div>

              {dashboardError && (
                <div role="alert" className="rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-200">
                  {dashboardError}
                </div>
              )}

              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                {[
                  {
                    label: "Active Campaigns",
                    val: dashboardSummary?.active_campaigns,
                    note: "Approved and ready in queue",
                    icon: "🚀",
                  },
                  {
                    label: "Contacts with Consent",
                    val: dashboardSummary?.contacts_with_consent,
                    note: "Purpose-matched lawful basis",
                    icon: "👥",
                  },
                  {
                    label: "Messages Dispatched",
                    val: dashboardSummary?.accepted_messages,
                    note: "Mock accepted & delivered",
                    icon: "📨",
                  },
                  {
                    label: "Suppressed Addresses",
                    val: dashboardSummary?.suppressed_addresses,
                    note: "Unsubscribes, bounces & blocks",
                    icon: "🛡️",
                  },
                ].map((stat) => (
                  <div
                    key={stat.label}
                    className="rounded-2xl border border-white/10 bg-slate-900/50 p-5 shadow-sm"
                  >
                    <div className="flex items-center justify-between text-slate-400">
                      <span className="text-xs font-medium">{stat.label}</span>
                      <span className="text-lg">{stat.icon}</span>
                    </div>
                    <p className="mt-3 text-3xl font-bold text-slate-100">
                      {stat.val !== undefined ? stat.val : "—"}
                    </p>
                    <p className="mt-1 text-xs text-slate-400">{stat.note}</p>
                  </div>
                ))}
              </div>

              {/* Quick Actions Card */}
              <div className="rounded-2xl border border-white/10 bg-slate-900/40 p-6">
                <h3 className="font-semibold text-slate-100">Quick Workflow Navigation</h3>
                <p className="mt-1 text-xs text-slate-400">Jump directly to common tasks:</p>
                <div className="mt-4 grid gap-3 sm:grid-cols-3">
                  <button
                    onClick={() => setActiveSection("contacts")}
                    className="flex flex-col items-start rounded-xl border border-white/10 bg-white/[0.02] p-4 text-left transition hover:border-accent hover:bg-accent/5"
                  >
                    <span className="text-lg">📥</span>
                    <span className="mt-2 text-sm font-semibold text-slate-200">Import CSV Contacts</span>
                    <span className="mt-1 text-xs text-slate-400">Validate consent evidence and provenance</span>
                  </button>
                  <button
                    onClick={() => setActiveSection("templates")}
                    className="flex flex-col items-start rounded-xl border border-white/10 bg-white/[0.02] p-4 text-left transition hover:border-accent hover:bg-accent/5"
                  >
                    <span className="text-lg">✏️</span>
                    <span className="mt-2 text-sm font-semibold text-slate-200">Manage Templates</span>
                    <span className="mt-1 text-xs text-slate-400">Sanitized HTML with mandatory unsubscribe</span>
                  </button>
                  <button
                    onClick={() => setActiveSection("campaigns")}
                    className="flex flex-col items-start rounded-xl border border-white/10 bg-white/[0.02] p-4 text-left transition hover:border-accent hover:bg-accent/5"
                  >
                    <span className="text-lg">📋</span>
                    <span className="mt-2 text-sm font-semibold text-slate-200">Review & Approve</span>
                    <span className="mt-1 text-xs text-slate-400">Freeze audience snapshots and trigger mock queue</span>
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Section: Contacts & Import */}
          {activeSection === "contacts" && (
            <div className="space-y-6">
              {/* CSV Intake Form */}
              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h2 className="text-base font-semibold text-slate-100">CSV Contact Intake</h2>
                    <p className="mt-1 text-xs text-slate-400">
                      Standard limits: Max 1 MB / 5,000 rows. Consent source, date, purpose, and provenance are mandatory.
                    </p>
                  </div>
                </div>

                <form onSubmit={requestPreview} className="mt-5 space-y-4">
                  <div>
                    <label className="block text-xs font-medium text-slate-300">Select CSV file</label>
                    <input
                      type="file"
                      accept=".csv,text/csv"
                      onChange={(e: ChangeEvent<HTMLInputElement>) =>
                        void readHeaders(e.target.files?.[0] || null)
                      }
                      className="mt-1.5 block w-full text-xs text-slate-300 file:mr-4 file:rounded-lg file:border-0 file:bg-white/10 file:px-4 file:py-2.5 file:text-xs file:font-semibold file:text-slate-100 hover:file:bg-white/15"
                    />
                  </div>

                  {headers.length > 0 && (
                    <div className="rounded-xl border border-white/10 bg-slate-950/60 p-4">
                      <p className="text-xs font-semibold text-slate-200">Map CSV Columns to Model</p>
                      <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                        {[
                          ["email", "Email Address *"],
                          ["consent_source", "Consent Source *"],
                          ["consented_at", "Consent Timestamp *"],
                          ["consent_purpose", "Consent Purpose/Topic *"],
                          ["consent_provenance", "Evidence / IP / Provenance *"],
                          ["display_name", "Display Name (Optional)"],
                        ].map(([key, label]) => (
                          <div key={key}>
                            <label className="block text-[11px] font-medium text-slate-400">{label}</label>
                            <select
                              required={key !== "display_name"}
                              value={mapping[key] || ""}
                              onChange={(e) => setMapping({ ...mapping, [key]: e.target.value })}
                              className="mt-1 block w-full rounded-lg border border-white/10 bg-slate-900 px-3 py-2 text-xs text-slate-200 outline-none focus:border-accent"
                            >
                              <option value="">Choose CSV column…</option>
                              {headers.map((h) => (
                                <option key={h} value={h}>
                                  {h}
                                </option>
                              ))}
                            </select>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className="flex gap-3">
                    <button
                      type="submit"
                      disabled={!csv || pendingAction === "preview_import"}
                      className="flex items-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-xs font-semibold text-ink transition hover:opacity-95 disabled:opacity-40"
                    >
                      {pendingAction === "preview_import" ? "Validating rows…" : "Validate & Preview Rows"}
                    </button>
                  </div>
                </form>

                {importError && (
                  <div role="alert" className="mt-4 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-300">
                    {importError}
                  </div>
                )}
                {importMessage && (
                  <div role="status" className="mt-4 rounded-lg border border-accent/30 bg-accent/10 p-3 text-xs text-accent">
                    {importMessage}
                  </div>
                )}

                {/* Import Preview Table */}
                {preview && (
                  <div className="mt-6 border-t border-white/10 pt-5">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p className="text-sm font-semibold text-slate-100">Staged Preview Results</p>
                        <p className="text-xs text-slate-400">
                          {Object.entries(preview.counts)
                            .map(([k, v]) => `${k}: ${v}`)
                            .join(" · ")}{" "}
                          · Expires at {new Date(preview.expires_at).toLocaleTimeString()}
                        </p>
                      </div>
                      <button
                        onClick={promptCommitImport}
                        disabled={!preview.counts.valid || pendingAction === "commit_import"}
                        className="rounded-lg bg-accent px-4 py-2.5 text-xs font-semibold text-ink transition hover:opacity-95 disabled:opacity-40"
                      >
                        Commit Valid Contacts ({preview.counts.valid})
                      </button>
                    </div>

                    <div className="mt-4 max-h-60 overflow-auto rounded-lg border border-white/10 bg-slate-950">
                      <table className="w-full text-left text-xs">
                        <thead className="sticky top-0 bg-slate-900 text-slate-400">
                          <tr>
                            <th scope="col" className="p-3">Row</th>
                            <th scope="col" className="p-3">Status</th>
                            <th scope="col" className="p-3">Details / Validation</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-white/5">
                          {preview.rows.map((row) => (
                            <tr key={row.row} className="hover:bg-white/[0.02]">
                              <td className="p-3 font-mono">{row.row}</td>
                              <td className="p-3">
                                <span
                                  className={`rounded px-2 py-0.5 text-[10px] font-semibold ${
                                    row.status === "valid"
                                      ? "bg-emerald-500/20 text-emerald-300"
                                      : "bg-rose-500/20 text-rose-300"
                                  }`}
                                >
                                  {row.status.toUpperCase()}
                                </span>
                              </td>
                              <td className="p-3 text-slate-400">{row.errors.join("; ") || "Clean consent record"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </section>

              {/* Contacts Directory */}
              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2 className="text-base font-semibold text-slate-100">
                      Active Contact Directory ({contacts.length})
                    </h2>
                    <p className="mt-1 text-xs text-slate-400">
                      Personal data can be manually deleted. Deletion automatically redacts outbox history and freezes suppression.
                    </p>
                  </div>
                  <input
                    type="search"
                    placeholder="Search by email or name…"
                    value={contactSearch}
                    onChange={(e) => setContactSearch(e.target.value)}
                    className="w-full sm:w-64 rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-xs text-slate-200 outline-none focus:border-accent"
                  />
                </div>

                {contacts.length === 0 ? (
                  <div className="mt-6 rounded-xl border border-dashed border-white/10 p-8 text-center">
                    <p className="text-sm font-semibold text-slate-300">No contacts imported yet</p>
                    <p className="mt-1 text-xs text-slate-500">
                      Upload a CSV file above to intake contacts with documented consent.
                    </p>
                  </div>
                ) : (
                  <div className="mt-4 max-h-80 overflow-auto rounded-lg border border-white/10 bg-slate-950">
                    <table className="w-full text-left text-xs">
                      <thead className="sticky top-0 bg-slate-900 text-slate-400">
                        <tr>
                          <th scope="col" className="p-3">Email Address</th>
                          <th scope="col" className="p-3">Display Name</th>
                          <th scope="col" className="p-3">Consent Records</th>
                          <th scope="col" className="p-3 text-right">Action</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-white/5">
                        {filteredContacts.map((contact) => (
                          <tr key={contact.id} className="hover:bg-white/[0.02]">
                            <td className="p-3 font-mono text-slate-200">{contact.email}</td>
                            <td className="p-3 text-slate-400">{contact.display_name || "—"}</td>
                            <td className="p-3 text-slate-400">{contact.consent_records} active</td>
                            <td className="p-3 text-right">
                              <button
                                onClick={() => promptDeleteContact(contact)}
                                className="rounded px-2.5 py-1 text-xs text-rose-300 transition hover:bg-rose-500/20"
                              >
                                Delete & Redact
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </div>
          )}

          {/* Section: Email Templates */}
          {activeSection === "templates" && (
            <div className="space-y-6">
              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2 className="text-base font-semibold text-slate-100">Email Template Editor</h2>
                    <p className="mt-1 text-xs text-slate-400">
                      Enforces HTML sanitization (nh3), text alternative, physical mailing address, and mandatory{" "}
                      <code className="text-accent">{"{{unsubscribe_url}}"}</code> placeholder.
                    </p>
                  </div>
                  <button
                    onClick={() => {
                      setTemplate(emptyTemplate);
                      setEditingTemplateId(null);
                      setTemplatePreview(null);
                      setTemplateMessage("");
                      setTemplateError("");
                    }}
                    className="rounded-lg border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs text-slate-200 transition hover:bg-white/[0.08]"
                  >
                    + New Template
                  </button>
                </div>

                {/* Template Chips */}
                {savedTemplates.length > 0 && (
                  <div className="mt-4 flex flex-wrap gap-2">
                    {savedTemplates.map((item) => (
                      <button
                        key={item.id}
                        onClick={() => {
                          const { id, created_at, updated_at, ...draft } = item;
                          setTemplate(draft);
                          setEditingTemplateId(id);
                          setTemplatePreview(null);
                          setTemplateMessage("");
                          setTemplateError("");
                        }}
                        className={`rounded-full border px-3.5 py-1.5 text-xs transition ${
                          editingTemplateId === item.id
                            ? "border-accent bg-accent/10 font-semibold text-accent"
                            : "border-white/10 text-slate-300 hover:bg-white/[0.04]"
                        }`}
                      >
                        {item.name}
                      </button>
                    ))}
                  </div>
                )}

                {/* Form */}
                <form onSubmit={saveTemplateAction} className="mt-6 space-y-4">
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <label className="block text-xs font-medium text-slate-300">Template Name</label>
                      <input
                        required
                        maxLength={160}
                        value={template.name}
                        onChange={(e) => setTemplate({ ...template, name: e.target.value })}
                        className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-xs text-slate-100 outline-none focus:border-accent"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-slate-300">Subject Line</label>
                      <input
                        required
                        maxLength={200}
                        value={template.subject}
                        onChange={(e) => setTemplate({ ...template, subject: e.target.value })}
                        className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-xs text-slate-100 outline-none focus:border-accent"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-slate-300">Sender Name</label>
                      <input
                        required
                        maxLength={100}
                        value={template.sender_name}
                        onChange={(e) => setTemplate({ ...template, sender_name: e.target.value })}
                        className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-xs text-slate-100 outline-none focus:border-accent"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-slate-300">Sender Email</label>
                      <input
                        required
                        type="email"
                        maxLength={320}
                        value={template.sender_email}
                        onChange={(e) => setTemplate({ ...template, sender_email: e.target.value })}
                        className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-xs text-slate-100 outline-none focus:border-accent"
                      />
                    </div>
                    <div className="sm:col-span-2">
                      <label className="block text-xs font-medium text-slate-300">Physical Mailing Address</label>
                      <input
                        required
                        maxLength={300}
                        placeholder="e.g. 100 Main St, Suite 400, Springfield, USA"
                        value={template.mailing_address}
                        onChange={(e) => setTemplate({ ...template, mailing_address: e.target.value })}
                        className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-xs text-slate-100 outline-none focus:border-accent"
                      />
                    </div>
                  </div>

                  <div>
                    <div className="flex items-center justify-between">
                      <label className="block text-xs font-medium text-slate-300">HTML Body</label>
                      <span className="text-[11px] text-slate-500">
                        Must contain <code className="text-accent">{"{{unsubscribe_url}}"}</code>
                      </span>
                    </div>
                    <textarea
                      required
                      rows={6}
                      value={template.html_body}
                      onChange={(e) => setTemplate({ ...template, html_body: e.target.value })}
                      className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-950 p-3 font-mono text-xs text-slate-200 outline-none focus:border-accent"
                    />
                  </div>

                  <div>
                    <label className="block text-xs font-medium text-slate-300">Plain Text Alternative</label>
                    <textarea
                      required
                      rows={4}
                      value={template.text_body}
                      onChange={(e) => setTemplate({ ...template, text_body: e.target.value })}
                      className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-950 p-3 font-mono text-xs text-slate-200 outline-none focus:border-accent"
                    />
                  </div>

                  <div className="flex flex-wrap items-center gap-3 pt-2">
                    <button
                      type="button"
                      onClick={(e) => void previewTemplateAction(e as unknown as FormEvent)}
                      disabled={pendingAction === "preview_template"}
                      className="rounded-lg border border-white/15 bg-white/[0.04] px-4 py-2.5 text-xs font-semibold text-slate-200 transition hover:bg-white/[0.08]"
                    >
                      {pendingAction === "preview_template" ? "Sanitizing…" : "Preview Live Sanitized Output"}
                    </button>
                    <button
                      type="submit"
                      disabled={pendingAction === "save_template"}
                      className="rounded-lg bg-accent px-4 py-2.5 text-xs font-semibold text-ink transition hover:opacity-95"
                    >
                      {editingTemplateId ? "Update Template" : "Save as New Template"}
                    </button>
                    {editingTemplateId && (
                      <button
                        type="button"
                        onClick={promptDeleteTemplate}
                        className="rounded-lg border border-rose-500/30 px-4 py-2.5 text-xs font-semibold text-rose-300 transition hover:bg-rose-500/10"
                      >
                        Delete Template
                      </button>
                    )}
                  </div>
                </form>

                {templateError && (
                  <div role="alert" className="mt-4 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-300">
                    {templateError}
                  </div>
                )}
                {templateMessage && (
                  <div role="status" className="mt-4 rounded-lg border border-accent/30 bg-accent/10 p-3 text-xs text-accent">
                    {templateMessage}
                  </div>
                )}

                {/* Rendered Preview Panel */}
                {templatePreview && (
                  <div className="mt-6 border-t border-white/10 pt-5">
                    <h3 className="text-xs font-semibold text-slate-200">
                      Live Preview: {templatePreview.sender} — {templatePreview.subject}
                    </h3>
                    <div className="mt-3 grid gap-4 lg:grid-cols-2">
                      <div className="rounded-xl border border-white/10 bg-white p-4 text-slate-900 shadow">
                        <p className="text-[11px] font-semibold text-slate-500">HTML IFrame (Sandbox Mode)</p>
                        <iframe
                          title="Sanitized Email Preview"
                          sandbox=""
                          srcDoc={`<!doctype html><meta charset="utf-8"><body style="font-family:system-ui,-apple-system,sans-serif;padding:12px;color:#0f172a">${templatePreview.html_body}<hr style="margin-top:20px;border:0;border-top:1px solid #cbd5e1"><small style="color:#64748b">${templatePreview.mailing_address}</small></body>`}
                          className="mt-2 h-72 w-full rounded border-0 bg-white"
                        />
                      </div>
                      <div className="rounded-xl border border-white/10 bg-slate-950 p-4">
                        <p className="text-[11px] font-semibold text-slate-400">Plain Text Alternative</p>
                        <pre className="mt-2 h-72 overflow-auto whitespace-pre-wrap rounded bg-slate-900/60 p-3 font-mono text-xs text-slate-200">
                          {templatePreview.text_body}
                        </pre>
                      </div>
                    </div>
                  </div>
                )}
              </section>
            </div>
          )}

          {/* Section: Campaigns */}
          {activeSection === "campaigns" && (
            <div className="space-y-6">
              {/* Campaign Creation */}
              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <h2 className="text-base font-semibold text-slate-100">Create Campaign Draft</h2>
                <p className="mt-1 text-xs text-slate-400">
                  Select a validated template and define the exact consent purpose. Approval freezes the audience snapshot.
                </p>

                <form onSubmit={createCampaignAction} className="mt-5 grid gap-3 sm:grid-cols-3">
                  <input
                    required
                    maxLength={160}
                    placeholder="Campaign Name (e.g. November Product Update)"
                    value={campaignName}
                    onChange={(e) => setCampaignName(e.target.value)}
                    className="rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-xs text-slate-100 outline-none focus:border-accent"
                  />
                  <select
                    required
                    value={campaignTemplateId}
                    onChange={(e) => setCampaignTemplateId(e.target.value)}
                    className="rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-xs text-slate-200 outline-none focus:border-accent"
                  >
                    <option value="">Select Email Template…</option>
                    {savedTemplates.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.name}
                      </option>
                    ))}
                  </select>
                  <input
                    required
                    maxLength={300}
                    placeholder="Exact Consent Purpose (e.g. monthly_newsletter)"
                    value={campaignPurpose}
                    onChange={(e) => setCampaignPurpose(e.target.value)}
                    className="rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-xs text-slate-100 outline-none focus:border-accent"
                  />
                  <button
                    type="submit"
                    disabled={pendingAction === "create_campaign"}
                    className="sm:col-span-3 rounded-lg bg-accent px-4 py-2.5 text-xs font-semibold text-ink transition hover:opacity-95 disabled:opacity-50"
                  >
                    Create Draft & Calculate Eligible Audience
                  </button>
                </form>

                {campaignError && (
                  <div role="alert" className="mt-4 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-300">
                    {campaignError}
                  </div>
                )}
                {campaignMessage && (
                  <div role="status" className="mt-4 rounded-lg border border-accent/30 bg-accent/10 p-3 text-xs text-accent">
                    {campaignMessage}
                  </div>
                )}
              </section>

              {/* Eligibility Preview Card */}
              {eligibility && selectedCampaignId && (
                <div className="rounded-2xl border border-accent/30 bg-accent/[0.04] p-5">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <p className="text-sm font-semibold text-slate-100">
                        Audience Eligibility Analysis for Campaign #{selectedCampaignId}
                      </p>
                      <p className="mt-1 text-xs text-slate-300">
                        Eligible: <strong className="text-accent">{eligibility.eligible_count}</strong> · Excluded:{" "}
                        <strong className="text-rose-300">{eligibility.excluded_count}</strong>
                      </p>
                    </div>
                  </div>

                  {eligibility.excluded_count > 0 && (
                    <div className="mt-3 rounded-lg border border-white/10 bg-slate-950/60 p-3 text-xs text-slate-400">
                      <p className="font-medium text-slate-300">Exclusion breakdown:</p>
                      <p className="mt-1">
                        {Object.entries(
                          eligibility.excluded.reduce<Record<string, number>>((acc, row) => {
                            acc[row.reason] = (acc[row.reason] || 0) + 1;
                            return acc;
                          }, {})
                        )
                          .map(([reason, count]) => `${reason}: ${count}`)
                          .join(" · ")}
                      </p>
                    </div>
                  )}
                </div>
              )}

              {/* Campaigns Table */}
              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2 className="text-base font-semibold text-slate-100">
                      Campaigns ({campaigns.length})
                    </h2>
                    <p className="mt-1 text-xs text-slate-400">
                      State machine: Draft → Approved → Paused/Resumed or Cancelled.
                    </p>
                  </div>
                  <input
                    type="search"
                    placeholder="Search campaigns…"
                    value={campaignSearch}
                    onChange={(e) => setCampaignSearch(e.target.value)}
                    className="w-full sm:w-60 rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-xs text-slate-200 outline-none focus:border-accent"
                  />
                </div>

                {campaigns.length === 0 ? (
                  <div className="mt-6 rounded-xl border border-dashed border-white/10 p-8 text-center">
                    <p className="text-sm font-semibold text-slate-300">No campaigns created</p>
                    <p className="mt-1 text-xs text-slate-500">
                      Create your first campaign draft using the form above.
                    </p>
                  </div>
                ) : (
                  <div className="mt-4 space-y-3">
                    {filteredCampaigns.map((item) => (
                      <div
                        key={item.id}
                        className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-white/10 bg-slate-950 p-4 transition hover:border-white/20"
                      >
                        <div>
                          <div className="flex items-center gap-2.5">
                            <span className="font-semibold text-slate-100">{item.name}</span>
                            <span
                              className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase ${
                                item.status === "approved"
                                  ? "bg-emerald-500/20 text-emerald-300"
                                  : item.status === "paused"
                                  ? "bg-amber-500/20 text-amber-300"
                                  : item.status === "cancelled"
                                  ? "bg-rose-500/20 text-rose-300"
                                  : "bg-slate-500/20 text-slate-300"
                              }`}
                            >
                              {item.status}
                            </span>
                          </div>
                          <p className="mt-1 text-xs text-slate-400">
                            Purpose: <span className="font-mono text-slate-300">{item.consent_purpose}</span>
                            {item.status === "approved" && (
                              <span> · {item.recipient_count} frozen snapshot recipients</span>
                            )}
                          </p>
                        </div>

                        <div className="flex flex-wrap items-center gap-2">
                          {item.status === "draft" && (
                            <>
                              <button
                                onClick={() => void previewEligibilityAction(item.id)}
                                className="rounded-lg border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs text-slate-200 hover:bg-white/[0.08]"
                              >
                                Review Audience
                              </button>
                              <button
                                onClick={() => promptApproveCampaign(item.id, item.name)}
                                className="rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-ink hover:opacity-90"
                              >
                                Approve Snapshot
                              </button>
                            </>
                          )}

                          {item.status === "approved" && (
                            <>
                              <button
                                onClick={() => void queueMockCampaignAction(item.id)}
                                className="rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/20"
                              >
                                Queue Mock Jobs
                              </button>
                              <button
                                onClick={() => promptTransitionCampaign(item.id, item.name, "pause")}
                                className="rounded-lg border border-amber-400/30 px-3 py-1.5 text-xs text-amber-200 hover:bg-amber-400/10"
                              >
                                Pause
                              </button>
                            </>
                          )}

                          {item.status === "paused" && (
                            <button
                              onClick={() => promptTransitionCampaign(item.id, item.name, "resume")}
                              className="rounded-lg border border-accent/30 px-3 py-1.5 text-xs text-accent hover:bg-accent/10"
                            >
                              Resume
                            </button>
                          )}

                          {item.status !== "cancelled" && (
                            <button
                              onClick={() => promptTransitionCampaign(item.id, item.name, "cancel")}
                              className="rounded-lg border border-rose-500/30 px-3 py-1.5 text-xs text-rose-300 hover:bg-rose-500/10"
                            >
                              Cancel
                            </button>
                          )}

                          <button
                            onClick={() => void loadCampaignEventsAction(item.id)}
                            className="rounded-lg border border-white/10 px-3 py-1.5 text-xs text-slate-300 hover:bg-white/[0.05]"
                          >
                            Events
                          </button>
                          <button
                            onClick={() => void loadCampaignReportAction(item.id)}
                            className="rounded-lg border border-white/10 px-3 py-1.5 text-xs text-slate-300 hover:bg-white/[0.05]"
                          >
                            Report
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              {/* Campaign Events Log Drawer */}
              {campaignEventRows.length > 0 && selectedCampaignId && (
                <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                  <div className="flex items-center justify-between">
                    <h3 className="text-sm font-semibold text-slate-100">
                      Campaign #{selectedCampaignId} Event Stream ({campaignEventRows.length})
                    </h3>
                    <button
                      onClick={() =>
                        promptDeleteEvents(
                          selectedCampaignId,
                          campaigns.find((c) => c.id === selectedCampaignId)?.name || ""
                        )
                      }
                      className="rounded border border-rose-500/30 px-2.5 py-1 text-xs text-rose-300 hover:bg-rose-500/10"
                    >
                      Delete Timeline (Audited)
                    </button>
                  </div>

                  <div className="mt-3 max-h-60 overflow-auto rounded-lg border border-white/10 bg-slate-950">
                    <table className="w-full text-left text-xs">
                      <thead className="sticky top-0 bg-slate-900 text-slate-400">
                        <tr>
                          <th scope="col" className="p-3">Event Type</th>
                          <th scope="col" className="p-3">Detail</th>
                          <th scope="col" className="p-3">Outbox ID</th>
                          <th scope="col" className="p-3">Timestamp</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-white/5">
                        {campaignEventRows.map((ev, i) => (
                          <tr key={`${ev.event_type}-${i}`} className="hover:bg-white/[0.02]">
                            <td className="p-3 font-semibold text-slate-200">{ev.event_type}</td>
                            <td className="p-3 text-slate-400">{ev.detail || "—"}</td>
                            <td className="p-3 text-slate-500 font-mono">{ev.outbox_id ?? "—"}</td>
                            <td className="p-3 text-slate-500">{new Date(ev.created_at).toLocaleString()}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}
            </div>
          )}

          {/* Section: Reports & Logs */}
          {activeSection === "reports" && (
            <div className="space-y-6">
              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2 className="text-base font-semibold text-slate-100">Campaign Delivery Reports</h2>
                    <p className="mt-1 text-xs text-slate-400">
                      Strict distinction: Provider acceptance is distinguished from delivered events. Formula-safe CSV exports.
                    </p>
                  </div>
                </div>

                <div className="mt-4 flex flex-wrap gap-2">
                  {campaigns.map((c) => (
                    <div key={c.id} className="flex items-center gap-1.5 rounded-lg border border-white/10 p-1.5">
                      <button
                        onClick={() => void loadCampaignReportAction(c.id)}
                        className={`rounded-md px-3 py-1.5 text-xs font-medium transition ${
                          selectedCampaignId === c.id
                            ? "bg-accent/20 text-accent font-semibold"
                            : "text-slate-300 hover:bg-white/[0.05]"
                        }`}
                      >
                        {c.name}
                      </button>
                      <button
                        onClick={() => void downloadCampaignExport(c.id)}
                        title="Download CSV report with formula neutralization"
                        className="rounded-md border border-white/10 bg-white/[0.04] px-2 py-1.5 text-xs text-accent transition hover:bg-white/[0.08]"
                      >
                        Export CSV
                      </button>
                    </div>
                  ))}
                </div>

                {campaignReport && (
                  <div className="mt-6 border-t border-white/10 pt-5">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <h3 className="text-sm font-bold text-slate-100">
                        {campaignReport.campaign_name} (Status: {campaignReport.campaign_status})
                      </h3>
                      <button
                        onClick={() => void downloadCampaignExport(campaignReport.campaign_id)}
                        className="rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-ink"
                      >
                        Download Formula-Safe CSV
                      </button>
                    </div>

                    <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                      {[
                        ["Snapshot Recipients", "snapshot_recipients", "Total immutable audience"],
                        ["Queued Jobs", "queued", "Messages in outbox queue"],
                        ["Provider Accepted", "accepted", "Handed off to delivery pipeline"],
                        ["Delivered", "delivered", "Confirmed delivery events"],
                        ["Pending / In Retry", "pending", "Waiting for dispatch window"],
                        ["Failed", "failed", "Terminal errors / retry cap reached"],
                        ["Suppressed at Dispatch", "suppressed", "Opted out or blocked before send"],
                      ].map(([label, key, note]) => (
                        <div key={key} className="rounded-xl border border-white/10 bg-slate-950 p-4">
                          <p className="text-xs text-slate-400">{label}</p>
                          <p className="mt-2 text-2xl font-bold text-slate-100">
                            {campaignReport.metrics[key] ?? 0}
                          </p>
                          <p className="mt-1 text-[11px] text-slate-500">{note}</p>
                        </div>
                      ))}
                    </div>

                    <p className="mt-4 text-xs italic text-slate-500">{campaignReport.reporting_note}</p>
                  </div>
                )}
              </section>
            </div>
          )}

          {/* Section: Suppression */}
          {activeSection === "suppression" && (
            <div className="space-y-6">
              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <h2 className="text-base font-semibold text-slate-100">Add Global Manual Suppression</h2>
                <p className="mt-1 text-xs text-slate-400">
                  Global suppression records are permanent across all future CSV imports and campaigns unless deleted directly by an operator.
                </p>

                <form onSubmit={addManualSuppressionAction} className="mt-4 flex flex-wrap gap-3">
                  <input
                    required
                    type="email"
                    maxLength={320}
                    placeholder="recipient@example.com"
                    value={suppressionEmail}
                    onChange={(e) => setSuppressionEmail(e.target.value)}
                    className="flex-1 min-w-[240px] rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-xs text-slate-100 outline-none focus:border-accent"
                  />
                  <input
                    maxLength={300}
                    placeholder="Reason/detail (optional)"
                    value={suppressionDetail}
                    onChange={(e) => setSuppressionDetail(e.target.value)}
                    className="flex-1 min-w-[200px] rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-xs text-slate-100 outline-none focus:border-accent"
                  />
                  <button
                    type="submit"
                    disabled={pendingAction === "add_suppression"}
                    className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-2.5 text-xs font-semibold text-rose-300 transition hover:bg-rose-500/20"
                  >
                    Add Global Suppression
                  </button>
                </form>

                {suppressionError && (
                  <div role="alert" className="mt-4 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-300">
                    {suppressionError}
                  </div>
                )}
                {suppressionMessage && (
                  <div role="status" className="mt-4 rounded-lg border border-accent/30 bg-accent/10 p-3 text-xs text-accent">
                    {suppressionMessage}
                  </div>
                )}
              </section>

              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <div className="flex items-center justify-between">
                  <h2 className="text-base font-semibold text-slate-100">
                    Active Global Suppression List ({suppressions.length})
                  </h2>
                  <button
                    onClick={() => void loadSuppressions()}
                    className="rounded border border-white/10 px-2.5 py-1 text-xs text-slate-400 hover:text-slate-200"
                  >
                    Refresh
                  </button>
                </div>

                {suppressions.length === 0 ? (
                  <div className="mt-4 rounded-xl border border-dashed border-white/10 p-6 text-center text-xs text-slate-500">
                    No active suppression records.
                  </div>
                ) : (
                  <div className="mt-4 max-h-80 overflow-auto rounded-lg border border-white/10 bg-slate-950">
                    <table className="w-full text-left text-xs">
                      <thead className="sticky top-0 bg-slate-900 text-slate-400">
                        <tr>
                          <th scope="col" className="p-3">Suppressed Email</th>
                          <th scope="col" className="p-3">Reason</th>
                          <th scope="col" className="p-3">Source</th>
                          <th scope="col" className="p-3">Recorded At</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-white/5">
                        {suppressions.map((row, i) => (
                          <tr key={`${row.email}-${i}`} className="hover:bg-white/[0.02]">
                            <td className="p-3 font-mono text-slate-200">{row.email}</td>
                            <td className="p-3">
                              <span className="rounded bg-rose-500/15 px-2 py-0.5 text-[10px] font-semibold text-rose-300">
                                {row.reason.toUpperCase()}
                              </span>
                            </td>
                            <td className="p-3 text-slate-400">{row.source}</td>
                            <td className="p-3 text-slate-500">{new Date(row.created_at).toLocaleString()}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </div>
          )}

          {/* Section: Settings & System */}
          {activeSection === "settings" && (
            <div className="space-y-6">
              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <h2 className="text-base font-semibold text-slate-100">System Architecture & Health</h2>
                <div className="mt-4 grid gap-3 sm:grid-cols-2">
                  <div className="rounded-xl border border-white/10 bg-slate-950 p-4">
                    <p className="text-xs text-slate-400">Liveness / Health Check</p>
                    <p className="mt-1 text-sm font-semibold text-accent">
                      {healthStatus?.status === "ok" ? "🟢 Healthy (/health)" : "🔴 Offline"}
                    </p>
                    <p className="mt-1 text-xs text-slate-500">
                      Active Email Provider: <code className="text-slate-300">{healthStatus?.email_provider}</code>
                    </p>
                  </div>

                  <div className="rounded-xl border border-white/10 bg-slate-950 p-4">
                    <p className="text-xs text-slate-400">Inbound Webhook Endpoint</p>
                    <p className="mt-1 font-mono text-xs text-slate-200">POST /webhooks/provider</p>
                    <p className="mt-1 text-xs text-slate-500">
                      HMAC-SHA256 signature verification active (X-Provider-Signature).
                    </p>
                  </div>
                </div>
              </section>

              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <h2 className="text-base font-semibold text-slate-100">Local Retention Policy</h2>
                <p className="mt-2 text-xs leading-6 text-slate-300">
                  Manual deletion policy is currently enforced: contact records, consent evidence, and event timelines remain indefinitely until explicitly deleted by an operator. Automatic purges are intentionally disabled to guarantee an audit trail. Contact deletion executes full personal data redaction on retained campaign history.
                </p>
              </section>

              {/* Audit Log Table */}
              <section className="rounded-2xl border border-white/10 bg-slate-900/50 p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2 className="text-base font-semibold text-slate-100">
                      Security & Operations Audit Trail ({auditLogs.length})
                    </h2>
                    <p className="mt-1 text-xs text-slate-400">
                      Immutable record of logins, campaign approvals, contact deletions, webhooks, and administrative actions.
                    </p>
                  </div>
                  <button
                    onClick={() => void loadAuditLogs()}
                    className="rounded-lg border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs text-slate-200 transition hover:bg-white/[0.08]"
                  >
                    Refresh Audit Log
                  </button>
                </div>

                {auditLogs.length === 0 ? (
                  <div className="mt-4 rounded-xl border border-dashed border-white/10 p-6 text-center text-xs text-slate-500">
                    No audit records logged yet.
                  </div>
                ) : (
                  <div className="mt-4 max-h-80 overflow-auto rounded-lg border border-white/10 bg-slate-950">
                    <table className="w-full text-left text-xs">
                      <thead className="sticky top-0 bg-slate-900 text-slate-400">
                        <tr>
                          <th scope="col" className="p-3">Action</th>
                          <th scope="col" className="p-3">Actor</th>
                          <th scope="col" className="p-3">Details / Parameters</th>
                          <th scope="col" className="p-3">Timestamp</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-white/5">
                        {auditLogs.map((log) => (
                          <tr key={log.id} className="hover:bg-white/[0.02]">
                            <td className="p-3 font-mono font-medium text-slate-200">{log.action}</td>
                            <td className="p-3 text-slate-400">{log.actor}</td>
                            <td className="p-3 font-mono text-[11px] text-slate-400">{log.detail || "—"}</td>
                            <td className="p-3 text-slate-500">
                              {log.created_at ? new Date(log.created_at).toLocaleString() : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </div>
          )}
        </main>
      </div>

      {/* Accessible Confirmation Modal Dialog */}
      {confirmModal && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="modal-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4 backdrop-blur-sm"
        >
          <div className="w-full max-w-md rounded-2xl border border-white/15 bg-slate-900 p-6 shadow-2xl">
            <h3 id="modal-title" className="text-lg font-bold text-slate-100">
              {confirmModal.title}
            </h3>
            <p className="mt-2 text-xs leading-5 text-slate-300">
              {confirmModal.description}
            </p>

            <div className="mt-6 flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setConfirmModal(null)}
                className="rounded-lg border border-white/15 bg-white/[0.04] px-4 py-2.5 text-xs font-semibold text-slate-300 hover:bg-white/[0.08]"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => {
                  const action = confirmModal.onConfirm;
                  setConfirmModal(null);
                  void action();
                }}
                className={`rounded-lg px-4 py-2.5 text-xs font-semibold ${
                  confirmModal.isDestructive
                    ? "bg-rose-600 text-white hover:bg-rose-500"
                    : "bg-accent text-ink hover:opacity-90"
                }`}
              >
                {confirmModal.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
