/* ============================================================================
 * Purpose: Client-side controller for the Cymbal Bank Disputes & Treasury
 *          Operations Portal—renders live ledger tables, manages per-role ADK
 *          chat sessions, and refreshes Agent Gateway Cloud Logging telemetry.
 * Architecture/Context: Loaded by `/static/index.html`; communicates strictly
 *          with `/api/metadata`, `/api/ledger/overview`, `/api/governance/events`,
 *          `/api/agents/{role}/chat`, and `/api/admin/reset`.
 * Dependencies/Side Effects: Pure DOM/Fetch ES2022 script; contains zero exploit
 *          helpers or hardcoded credentials.
 * Author: Pradeep Singh
 * License: Apache-2.0
 * ============================================================================ */

(() => {
  "use strict";

  const state = {
    metadata: null,
    sessions: {
      tier1: `portal-tier1-${Math.random().toString(36).slice(2, 9)}`,
      specialist: `portal-spec-${Math.random().toString(36).slice(2, 9)}`,
    },
  };

  /**
   * Formats a numeric USD value into standard `$1,234.56` banking notation.
   * @param {number} value - Dollar amount.
   * @returns {string} Formatted USD currency string.
   */
  function formatUsd(value) {
    const num = Number(value || 0);
    return num.toLocaleString("en-US", { style: "currency", currency: "USD" });
  }

  /**
   * Escapes untrusted strings prior to HTML interpolation to prevent DOM XSS.
   * @param {string} raw - Input string.
   * @returns {string} HTML-entity escaped string.
   */
  function escapeHtml(raw) {
    const div = document.createElement("div");
    div.textContent = String(raw ?? "");
    return div.innerHTML;
  }

  /**
   * Selects a semantic badge class based on status or verdict string.
   * @param {string} status - Entity status or policy verdict.
   * @returns {string} CSS class name.
   */
  function badgeClass(status) {
    const upper = String(status || "").toUpperCase();
    if (["ACTIVE", "POSTED", "REFUND_ISSUED", "ALLOW", "ALLOWED"].includes(upper)) {
      return "badge badge-green";
    }
    if (["OPEN", "DISPUTED", "HOLD_REVIEW", "MASKED", "INFO"].includes(upper)) {
      return "badge badge-amber";
    }
    if (
      [
        "DENY",
        "HOLD_FRAUD",
        "REJECTED",
        "BLOCKED_AT_INGRESS_OR_POLICY",
        "EGRESS_POLICY_ENFORCED",
      ].includes(upper)
    ) {
      return "badge badge-red";
    }
    return "badge badge-blue";
  }

  /**
   * Updates the SPIFFE workload identity badge when switching active agents.
   */
  function syncAgentIdentityBanner() {
    const select = document.getElementById("agent-role-select");
    const chip = document.getElementById("active-tier-chip");
    const spiffe = document.getElementById("active-spiffe-id");
    if (!state.metadata || !state.metadata.agents) {
      return;
    }
    const role = select.value;
    const info = state.metadata.agents[role];
    if (!info) {
      return;
    }
    chip.textContent = info.tier_badge;
    chip.className = role === "specialist" ? "tier-chip badge-green" : "tier-chip";
    spiffe.textContent = info.spiffe_id;
  }

  /**
   * Fetches `/api/metadata` to populate environment pills and SPIFFE banners.
   */
  async function loadMetadata() {
    const resp = await fetch("/api/metadata");
    if (!resp.ok) {
      return;
    }
    state.metadata = await resp.json();
    document.getElementById("env-pill").textContent =
      `${state.metadata.project_id} · ${state.metadata.region} (${state.metadata.storage_backend})`;
    document.getElementById("gateway-pill").textContent =
      `Gateways: ${state.metadata.ingress_gateway} / ${state.metadata.egress_gateway}`;
    syncAgentIdentityBanner();
  }

  /**
   * Fetches `/api/ledger/overview` and renders KPIs, disputes, accounts, and transactions.
   */
  async function loadLedger() {
    const resp = await fetch("/api/ledger/overview");
    if (!resp.ok) {
      return;
    }
    const data = await resp.json();
    document.getElementById("storage-badge").textContent = data.storage_backend;

    const accounts = data.accounts || [];
    const disputes = data.disputes || [];
    const txns = data.transactions || [];

    const totalDeposits = accounts.reduce((acc, a) => acc + Number(a.available_balance_usd || 0), 0);
    const openDisputes = disputes.filter((d) => d.status === "OPEN");
    const openExposure = openDisputes.reduce((acc, d) => acc + Number(d.disputed_amount_usd || 0), 0);
    const holdCount = accounts.filter((a) => a.account_status !== "ACTIVE").length;

    document.getElementById("kpi-deposits").textContent = formatUsd(totalDeposits);
    document.getElementById("kpi-open-disputes").textContent = String(openDisputes.length);
    document.getElementById("kpi-exposure").textContent = formatUsd(openExposure);
    document.getElementById("kpi-holds").textContent = String(holdCount);

    document.getElementById("disputes-tbody").innerHTML = disputes
      .map(
        (d) => `
        <tr>
          <td><strong>${escapeHtml(d.dispute_id)}</strong></td>
          <td>${escapeHtml(d.customer_id)}</td>
          <td>${escapeHtml(d.transaction_id)}</td>
          <td><strong>${formatUsd(d.disputed_amount_usd)}</strong></td>
          <td>${escapeHtml(d.reason_code)}</td>
          <td>${d.fraud_verified ? `<span class="badge badge-green">${escapeHtml(d.fraud_ticket_id || "VERIFIED")}</span>` : `<span class="badge badge-amber">UNVERIFIED</span>`}</td>
          <td>
            <span class="${badgeClass(d.status)}">${escapeHtml(d.status)}</span>
            ${d.resolution_note ? `<div class="panel-caption">${escapeHtml(d.resolution_note)}</div>` : ""}
          </td>
        </tr>`
      )
      .join("");

    document.getElementById("accounts-tbody").innerHTML = accounts
      .map(
        (a) => `
        <tr>
          <td><strong>${escapeHtml(a.customer_id)}</strong></td>
          <td>${escapeHtml(a.full_name)}<div class="panel-caption">${escapeHtml(a.account_number_masked)}</div></td>
          <td>${escapeHtml(a.segment)}</td>
          <td><strong>${formatUsd(a.available_balance_usd)}</strong></td>
          <td>
            <span class="${badgeClass(a.account_status)}">${escapeHtml(a.account_status)}</span>
            ${a.hold_reason ? `<div class="panel-caption">${escapeHtml(a.hold_reason)}</div>` : ""}
          </td>
        </tr>`
      )
      .join("");

    document.getElementById("txns-tbody").innerHTML = txns
      .slice(0, 12)
      .map(
        (t) => `
        <tr>
          <td><strong>${escapeHtml(t.transaction_id)}</strong></td>
          <td>${escapeHtml(t.customer_id)}</td>
          <td>${escapeHtml(t.merchant_or_counterparty)}</td>
          <td>${escapeHtml(t.channel)}</td>
          <td>${formatUsd(t.amount_usd)}</td>
          <td><span class="${badgeClass(t.status)}">${escapeHtml(t.status)}</span></td>
        </tr>`
      )
      .join("");
  }

  /**
   * Fetches `/api/governance/events` and renders the Cloud Logging + Ledger audit table.
   */
  async function loadGovernanceEvents() {
    const resp = await fetch("/api/governance/events");
    if (!resp.ok) {
      return;
    }
    const data = await resp.json();
    const events = data.events || [];
    const tbody = document.getElementById("audit-tbody");
    if (events.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="panel-caption">No governance audit events recorded yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = events
      .map(
        (ev) => `
        <tr>
          <td><code>${escapeHtml(String(ev.timestamp || "").replace("T", " ").slice(0, 19))}</code></td>
          <td><span class="badge badge-blue">${escapeHtml(ev.layer)}</span></td>
          <td><span class="${badgeClass(ev.verdict)}">${escapeHtml(ev.verdict)}</span></td>
          <td><code>${escapeHtml(ev.principal)}</code></td>
          <td><code>${escapeHtml(ev.target)}</code></td>
          <td>${escapeHtml(ev.summary)}</td>
        </tr>`
      )
      .join("");
  }

  /**
   * Appends a user or agent message bubble (with expandable tool trace) to the chat log.
   * @param {"user"|"agent"} kind - Message author type.
   * @param {string} text - Main message text.
   * @param {object|null} meta - Optional agent response metadata.
   */
  function appendChatMessage(kind, text, meta = null) {
    const transcript = document.getElementById("chat-transcript");
    const wrapper = document.createElement("div");
    wrapper.className = `chat-msg chat-msg-${kind}`;

    if (kind === "user") {
      wrapper.textContent = text;
    } else {
      const roleTitle =
        meta && meta.agent_role === "specialist"
          ? "Risk & Disputes Specialist Agent"
          : "Tier-1 Support Agent";
      const verdictHtml = meta?.gateway_verdict
        ? `<span class="${badgeClass(meta.gateway_verdict)}">${escapeHtml(meta.gateway_verdict)}</span>`
        : "";
      const latencyHtml = meta?.latency_ms ? `${escapeHtml(meta.latency_ms)} ms` : "";

      let traceHtml = "";
      if (meta && Array.isArray(meta.tool_trace) && meta.tool_trace.length > 0) {
        traceHtml = `
          <details class="trace-details">
            <summary>Tool &amp; Gateway Trace (${meta.tool_trace.length} events)</summary>
            <pre class="trace-pre">${escapeHtml(JSON.stringify(meta.tool_trace, null, 2))}</pre>
          </details>`;
      }

      wrapper.innerHTML = `
        <div class="chat-msg-meta">
          <strong>${escapeHtml(roleTitle)}</strong>
          <span>${verdictHtml} ${latencyHtml}</span>
        </div>
        <div>${escapeHtml(text)}</div>
        ${traceHtml}
      `;
    }

    transcript.appendChild(wrapper);
    transcript.scrollTop = transcript.scrollHeight;
  }

  /**
   * Submits an operator prompt to the selected ADK agent and refreshes live ledger/audit views.
   * @param {SubmitEvent} event - Form submit event.
   */
  async function handleChatSubmit(event) {
    event.preventDefault();
    const input = document.getElementById("chat-input");
    const sendBtn = document.getElementById("send-btn");
    const statusText = document.getElementById("chat-status-text");
    const role = document.getElementById("agent-role-select").value;
    const message = input.value.trim();
    if (!message) {
      return;
    }

    appendChatMessage("user", message);
    input.value = "";
    sendBtn.disabled = true;
    statusText.textContent = "Routing via Agent Gateway...";

    try {
      const resp = await fetch(`/api/agents/${encodeURIComponent(role)}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          session_id: state.sessions[role],
        }),
      });
      const payload = await resp.json();
      if (!resp.ok) {
        appendChatMessage("agent", `Error: ${payload.detail || resp.statusText}`, {
          agent_role: role,
          gateway_verdict: "ERROR",
        });
      } else {
        appendChatMessage("agent", payload.reply || "", payload);
      }
    } catch (err) {
      appendChatMessage("agent", `Network error contacting portal backend: ${err}`, {
        agent_role: role,
        gateway_verdict: "ERROR",
      });
    } finally {
      sendBtn.disabled = false;
      statusText.textContent = "Ready";
      await Promise.all([loadLedger(), loadGovernanceEvents()]);
    }
  }

  /**
   * Resets the demo ledger back to baseline seed records.
   */
  async function handleResetLedger() {
    const btn = document.getElementById("reset-ledger-btn");
    btn.disabled = true;
    try {
      await fetch("/api/admin/reset", { method: "POST" });
      await Promise.all([loadLedger(), loadGovernanceEvents()]);
    } finally {
      btn.disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    document.getElementById("agent-role-select").addEventListener("change", syncAgentIdentityBanner);
    document.getElementById("chat-form").addEventListener("submit", handleChatSubmit);
    document.getElementById("reset-ledger-btn").addEventListener("click", handleResetLedger);
    document.getElementById("refresh-all-btn").addEventListener("click", async () => {
      await Promise.all([loadLedger(), loadGovernanceEvents()]);
    });
    document.getElementById("refresh-audit-btn").addEventListener("click", loadGovernanceEvents);
    document.getElementById("clear-chat-btn").addEventListener("click", () => {
      const role = document.getElementById("agent-role-select").value;
      state.sessions[role] = `portal-${role}-${Math.random().toString(36).slice(2, 9)}`;
      document.getElementById("chat-transcript").innerHTML = `
        <div class="chat-msg chat-msg-system">
          Started fresh session <code>${escapeHtml(state.sessions[role])}</code>.
        </div>`;
    });

    await Promise.all([loadMetadata(), loadLedger(), loadGovernanceEvents()]);
  });
})();
