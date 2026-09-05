import { useEffect, useState } from "react";
import {
  approveCampaign,
  draftCampaign,
  fetchCampaigns,
  fetchFullAudit,
  fetchLawyers,
  fetchMerchantStats,
  rejectCampaign,
  type AgentAuditEntry,
  type Campaign,
  type MerchantStats,
} from "../api";
import { formatMoney, type Lawyer, type UserProfile } from "../lib/ui";

interface Props {
  user: UserProfile | null;
}

const GATE_COLORS: Record<string, string> = {
  pending: "#c98a1e",
  approved: "#3e9a5f",
  rejected: "#c0392b",
  not_required: "#a3a29c",
};

type Tab = "audit" | "campaigns" | "agents";

export function MerchantDashboard({ user }: Props) {
  const [tab, setTab] = useState<Tab>("audit");
  const [audit, setAudit] = useState<AgentAuditEntry[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [lawyers, setLawyers] = useState<Lawyer[]>([]);
  const [stats, setStats] = useState<MerchantStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [objective, setObjective] = useState("Re-engage visitors who browsed but didn't book this month");
  const [lawyerId, setLawyerId] = useState("");
  const [discountPct, setDiscountPct] = useState(15);
  const [budgetInr, setBudgetInr] = useState(10000);

  const refresh = () => {
    fetchFullAudit().then(setAudit).catch((e) => setError(e.message));
    fetchCampaigns().then(setCampaigns).catch(() => {});
    fetchMerchantStats().then(setStats).catch(() => {});
    fetchLawyers().then((ls: Lawyer[]) => {
      setLawyers(ls);
      if (ls.length && !lawyerId) setLawyerId(ls[0].id);
    }).catch(() => {});
  };

  useEffect(() => {
    if (user) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  const draft = async () => {
    setBusy(true);
    setError(null);
    try {
      await draftCampaign({ objective, lawyerId, discountPct, budgetInr });
      refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: (id: string) => Promise<Campaign>, id: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn(id);
      refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (!user) {
    return <div className="container" style={{ padding: 40 }}>Sign in to view the merchant dashboard.</div>;
  }

  return (
    <div style={{ flex: 1, overflowY: "auto" }}>
      <div className="container" style={{ maxWidth: 980, padding: "28px 20px" }}>
        <h1 style={{ font: "700 24px var(--font-head)", marginBottom: 4 }}>Merchant control room</h1>
        <p style={{ font: "400 13.5px var(--font-body)", color: "var(--muted-2)", marginBottom: 20 }}>
          Every money action the agents take is bounded, gated, and recorded here. Demo note: every signed-in user acts as the merchant.
        </p>

        {stats && stats.paidOrderCount > 0 && <GrowthPanel stats={stats} />}

        <div style={{ display: "flex", gap: 8, marginBottom: 20 }}>
          {(["audit", "campaigns", "agents"] as Tab[]).map((t) => (
            <button key={t} className={`chip${tab === t ? " active" : ""}`} onClick={() => setTab(t)}
              style={tab === t ? { background: "var(--accent)", color: "#fff" } : {}}>
              {t === "audit" ? "Audit trail" : t === "campaigns" ? "Campaign orchestrator" : "AI buyers"}
            </button>
          ))}
        </div>

        {error && <div style={{ background: "#fbecea", color: "var(--danger)", borderRadius: 10, padding: "10px 14px", font: "500 13px var(--font-body)", marginBottom: 14 }}>{error}</div>}

        {tab === "audit" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {audit.length === 0 && <div style={{ color: "var(--muted-2)", font: "400 13px var(--font-body)" }}>No agent activity yet — try the concierge or run the demo AI buyer.</div>}
            {audit.map((a) => (
              <div key={a.id} className="card" style={{ padding: 14 }}>
                <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                  <span style={{ font: "700 13px var(--font-head)" }}>{a.action.replace(/_/g, " ")}</span>
                  <span className="chip" style={{ fontSize: 11, padding: "2px 8px" }}>{a.actor}</span>
                  {a.amountInr != null && <span style={{ font: "600 12.5px var(--font-body)", color: "var(--accent)" }}>{formatMoney(a.amountInr)}</span>}
                  <span style={{ font: "600 11px var(--font-body)", color: GATE_COLORS[a.gateStatus] || "var(--muted-3)" }}>
                    gate: {a.gateStatus}
                  </span>
                  {a.boundsCheck !== "n/a" && (
                    <span style={{ font: "600 11px var(--font-body)", color: a.boundsCheck === "passed" ? "#3e9a5f" : "#c0392b" }}>
                      bounds: {a.boundsCheck}
                    </span>
                  )}
                  <span style={{ marginLeft: "auto", font: "400 11px var(--font-body)", color: "var(--muted-3)" }}>
                    {a.createdAt ? new Date(a.createdAt).toLocaleString() : ""}
                  </span>
                </div>
                <div style={{ font: "400 12.5px/1.5 var(--font-body)", color: "var(--muted)", marginTop: 6 }}>{a.rationale}</div>
              </div>
            ))}
          </div>
        )}

        {tab === "campaigns" && (
          <>
            <div className="card" style={{ padding: 18, marginBottom: 18 }}>
              <div style={{ font: "700 15px var(--font-head)", marginBottom: 12 }}>Ask the agent to draft a campaign</div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <label style={{ gridColumn: "1 / -1", font: "500 12px var(--font-body)", color: "var(--muted-2)" }}>
                  Objective
                  <input value={objective} onChange={(e) => setObjective(e.target.value)}
                    style={{ width: "100%", marginTop: 4, border: "1px solid var(--border)", borderRadius: 10, padding: "10px 12px", font: "400 13.5px var(--font-body)" }} />
                </label>
                <label style={{ font: "500 12px var(--font-body)", color: "var(--muted-2)" }}>
                  Lawyer
                  <select value={lawyerId} onChange={(e) => setLawyerId(e.target.value)}
                    style={{ width: "100%", marginTop: 4, border: "1px solid var(--border)", borderRadius: 10, padding: "10px 12px", font: "400 13.5px var(--font-body)" }}>
                    {lawyers.map((l) => <option key={l.id} value={l.id}>{l.name} — {l.specialty}</option>)}
                  </select>
                </label>
                <div style={{ display: "flex", gap: 12 }}>
                  <label style={{ flex: 1, font: "500 12px var(--font-body)", color: "var(--muted-2)" }}>
                    Discount % (max 30)
                    <input type="number" value={discountPct} onChange={(e) => setDiscountPct(Number(e.target.value))}
                      style={{ width: "100%", marginTop: 4, border: "1px solid var(--border)", borderRadius: 10, padding: "10px 12px", font: "400 13.5px var(--font-body)" }} />
                  </label>
                  <label style={{ flex: 1, font: "500 12px var(--font-body)", color: "var(--muted-2)" }}>
                    Budget ₹
                    <input type="number" value={budgetInr} onChange={(e) => setBudgetInr(Number(e.target.value))}
                      style={{ width: "100%", marginTop: 4, border: "1px solid var(--border)", borderRadius: 10, padding: "10px 12px", font: "400 13.5px var(--font-body)" }} />
                  </label>
                </div>
              </div>
              <button className="btn btn-primary" disabled={busy} onClick={draft} style={{ marginTop: 14 }}>
                {busy ? "Working…" : "Draft campaign"}
              </button>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {campaigns.map((c) => (
                <div key={c.id} className="card" style={{ padding: 16 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                    <span style={{ font: "700 14px var(--font-head)" }}>{c.name}</span>
                    <span className="chip" style={{ fontSize: 11, padding: "2px 8px" }}>{c.status}</span>
                    <span style={{ font: "500 12px var(--font-body)", color: "var(--muted-2)" }}>
                      −{c.discountPct}% · budget {formatMoney(c.budgetInr)} · spent {formatMoney(c.spentInr)} · {c.conversions} conversions
                    </span>
                  </div>
                  <div style={{ font: "400 12.5px/1.5 var(--font-body)", color: "var(--muted)", margin: "6px 0" }}>
                    <b>Target:</b> {c.targetSegment} — “{c.message}”
                  </div>
                  {c.paymentLinkUrl && (
                    <div style={{ font: "500 12px var(--font-body)", color: "var(--accent)" }}>Payment link: {c.paymentLinkUrl}</div>
                  )}
                  {c.status === "draft" && (
                    <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                      <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => act(approveCampaign, c.id)}>Approve & launch</button>
                      <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => act(rejectCampaign, c.id)}>Reject</button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}

        {tab === "agents" && (
          <div className="card" style={{ padding: 18 }}>
            <div style={{ font: "700 15px var(--font-head)", marginBottom: 8 }}>Sell to AI buyers</div>
            <div style={{ font: "400 13px/1.7 var(--font-body)", color: "var(--muted)" }}>
              LexCart is discoverable and transactable by external AI agents:
              <ol style={{ paddingLeft: 20, margin: "8px 0" }}>
                <li>Agents discover the machine-readable catalog at <code>/.well-known/agent-catalog.json</code> (schema.org-style services, prices, bounds, auth).</li>
                <li>They authenticate with an <code>X-Agent-Key</code> and use <code>/api/agent/v1</code> to quote, order, and pay (Razorpay test mode).</li>
                <li>Every key has a daily spend limit, every order passes the same guardrails, and everything lands in the audit trail.</li>
              </ol>
              Try it from the repo: <code>python demo/ai_buyer.py --need "property dispute" --budget 4000</code>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const CHANNEL_LABELS: Record<string, string> = {
  web: "Web checkout",
  concierge: "Concierge (in-app AI)",
  agent_api: "AI buyers",
  campaign: "Campaigns",
};

const CHANNEL_COLORS: Record<string, string> = {
  web: "#a3a29c",
  concierge: "#3e9a5f",
  agent_api: "#2f6fed",
  campaign: "#c98a1e",
};

function Metric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div style={{ flex: "1 1 140px" }}>
      <div style={{ font: "700 22px var(--font-head)" }}>{value}</div>
      <div style={{ font: "500 12px var(--font-body)", color: "var(--muted-2)" }}>{label}</div>
      {sub && <div style={{ font: "400 11px var(--font-body)", color: "var(--muted-3)", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

function GrowthPanel({ stats }: { stats: MerchantStats }) {
  const channels = Object.entries(stats.revenueByChannel).sort((a, b) => b[1] - a[1]);
  return (
    <div className="card" style={{ padding: 18, marginBottom: 20 }}>
      <div style={{ font: "700 15px var(--font-head)", marginBottom: 14 }}>Revenue &amp; growth</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginBottom: 18 }}>
        <Metric label="Total revenue (paid)" value={formatMoney(stats.totalRevenueInr)} sub={`${stats.paidOrderCount} orders`} />
        <Metric label="Via agentic channels" value={`${stats.agenticSharePct}%`} sub={formatMoney(stats.agenticRevenueInr)} />
        <Metric label="Upsell attach rate" value={`${stats.upsellAttachRatePct}%`} sub="orders with an add-on" />
        <Metric
          label="Campaign ROI"
          value={stats.campaignRoi != null ? `${stats.campaignRoi}×` : "—"}
          sub={stats.campaignDiscountSpendInr ? `${formatMoney(stats.campaignAttributedRevenueInr)} / ${formatMoney(stats.campaignDiscountSpendInr)} discount` : "no campaign sales yet"}
        />
        <Metric label="Guardrail refusals" value={String(stats.guardrailRefusalCount)} sub="orders blocked before the gateway" />
      </div>
      <div style={{ font: "600 11px var(--font-body)", color: "var(--muted-2)", marginBottom: 6, textTransform: "uppercase", letterSpacing: 0.4 }}>
        Revenue by channel
      </div>
      <div style={{ display: "flex", height: 10, borderRadius: 6, overflow: "hidden", marginBottom: 8 }}>
        {channels.map(([ch, amt]) => (
          <div key={ch} title={`${CHANNEL_LABELS[ch] || ch}: ${formatMoney(amt)}`}
            style={{ width: `${(100 * amt) / stats.totalRevenueInr}%`, background: CHANNEL_COLORS[ch] || "#888" }} />
        ))}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 14 }}>
        {channels.map(([ch, amt]) => (
          <div key={ch} style={{ display: "flex", alignItems: "center", gap: 6, font: "500 12px var(--font-body)", color: "var(--muted)" }}>
            <span style={{ width: 8, height: 8, borderRadius: 4, background: CHANNEL_COLORS[ch] || "#888", display: "inline-block" }} />
            {CHANNEL_LABELS[ch] || ch} — {formatMoney(amt)} ({Math.round((100 * amt) / stats.totalRevenueInr)}%)
          </div>
        ))}
      </div>
    </div>
  );
}
