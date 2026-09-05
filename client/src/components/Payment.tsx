import { useState, useEffect } from "react";
import {
  createCheckoutOrder,
  fetchPaymentConfig,
  fetchActiveCampaigns,
  fetchAddons,
  type Campaign,
} from "../api";
import { payOrder } from "../lib/razorpay";
import { initials, avatarTint, formatMoney, type Lawyer, type UserProfile } from "../lib/ui";

interface Addon {
  id: string;
  name: string;
  description: string;
  priceInr: number;
  appliesTo: string;
}

interface Props {
  lawyer: Lawyer;
  user: UserProfile | null;
  onBack: () => void;
  onSuccess: () => void;
}

export function Payment({ lawyer, user, onBack, onSuccess }: Props) {
  const [mock, setMock] = useState<boolean | null>(null);
  const [addons, setAddons] = useState<Addon[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [processing, setProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchPaymentConfig()
      .then((c) => setMock(c.mock))
      .catch(() => setError("Couldn't reach the payment gateway. Is the backend running?"));
    fetchAddons()
      .then((all: Addon[]) =>
        setAddons(all.filter((a) => !a.appliesTo || a.appliesTo.includes(lawyer.specialty))),
      )
      .catch(() => {});
    fetchActiveCampaigns()
      .then((cs) => setCampaign(cs.find((c) => c.lawyerId === lawyer.id) ?? null))
      .catch(() => {});
  }, [lawyer.id, lawyer.specialty]);

  const consultation = lawyer.hourlyRate;
  const fee = Math.round(consultation * 0.05);
  const addonTotal = addons
    .filter((a) => selected.includes(a.id))
    .reduce((sum, a) => sum + a.priceInr, 0);
  const discount = campaign
    ? Math.floor(((consultation + addonTotal) * campaign.discountPct) / 100)
    : 0;
  const total = consultation + addonTotal + fee - discount;
  const tint = avatarTint(lawyer.name);

  const toggle = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  const pay = async (simulateFailure = false) => {
    if (!user) {
      setError("Your session has expired. Please sign in again to complete this booking.");
      return;
    }
    setProcessing(true);
    setError(null);
    try {
      const order = await createCheckoutOrder(lawyer.id, selected, campaign?.id);
      await payOrder(order, { name: user.name, email: user.email, simulateFailure });
      onSuccess();
    } catch (e: any) {
      setError(e.message || "Payment failed. Please try again.");
    } finally {
      setProcessing(false);
    }
  };

  const rows = [
    { label: "Consultation", value: formatMoney(consultation) },
    ...addons
      .filter((a) => selected.includes(a.id))
      .map((a) => ({ label: a.name, value: formatMoney(a.priceInr) })),
    { label: "Platform fee (5%)", value: formatMoney(fee) },
    ...(discount > 0
      ? [{ label: `Campaign: ${campaign!.name} (−${campaign!.discountPct}%)`, value: `−${formatMoney(discount)}` }]
      : []),
  ];

  return (
    <div style={{ flex: 1, display: "flex", justifyContent: "center" }}>
      <div className="container" style={{ maxWidth: 620 }}>
        <button className="btn btn-ghost btn-sm" onClick={onBack} style={{ marginBottom: 18, paddingLeft: 0 }}>← Back to profile</button>

        {/* booking summary */}
        <div className="card" style={{ padding: 22, marginBottom: 18 }}>
          <div style={{ display: "flex", gap: 14, alignItems: "center", marginBottom: 18 }}>
            <div className="avatar" style={{ width: 48, height: 48, background: tint.bg, color: tint.fg }}>{initials(lawyer.name)}</div>
            <div style={{ flex: 1 }}>
              <div style={{ font: "700 15px var(--font-head)" }}>{lawyer.name}</div>
              <div style={{ font: "500 12.5px var(--font-body)", color: "var(--muted-2)" }}>{lawyer.specialty} · Consultation</div>
            </div>
          </div>

          {addons.length > 0 && (
            <div style={{ marginBottom: 14 }}>
              <div style={{ font: "600 12px var(--font-body)", letterSpacing: ".06em", textTransform: "uppercase", color: "var(--muted-3)", marginBottom: 8 }}>
                Recommended add-ons
              </div>
              {addons.map((a) => (
                <label key={a.id} style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: "7px 0", cursor: "pointer" }}>
                  <input type="checkbox" checked={selected.includes(a.id)} onChange={() => toggle(a.id)} style={{ marginTop: 3 }} />
                  <span style={{ flex: 1 }}>
                    <span style={{ font: "600 13px var(--font-body)" }}>{a.name}</span>
                    <span style={{ font: "600 13px var(--font-body)", color: "var(--accent)" }}> · {formatMoney(a.priceInr)}</span>
                    <div style={{ font: "400 12px var(--font-body)", color: "var(--muted-2)" }}>{a.description}</div>
                  </span>
                </label>
              ))}
            </div>
          )}

          {rows.map((r) => (
            <div key={r.label} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", font: "400 13.5px var(--font-body)", color: "var(--muted)" }}>
              <span>{r.label}</span><span>{r.value}</span>
            </div>
          ))}
          <div className="divider" style={{ margin: "10px 0" }} />
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ font: "600 14px var(--font-body)" }}>Total</span>
            <span style={{ font: "700 22px var(--font-head)", color: "var(--accent)" }}>{formatMoney(total)}</span>
          </div>
        </div>

        {/* payment */}
        <div className="card" style={{ padding: 22 }}>
          <div style={{ font: "700 16px var(--font-head)", marginBottom: 4 }}>Secure payment</div>
          <div style={{ font: "400 13px var(--font-body)", color: "var(--muted-2)", marginBottom: 18 }}>
            {mock
              ? "Payments run on LexCart's built-in mock gateway (no Razorpay keys configured) — the full order → verify flow, no real money."
              : "Payments are processed by Razorpay in test mode. Use a Razorpay test card to complete the booking."}
          </div>

          {error && <div style={{ background: "#fbecea", color: "var(--danger)", borderRadius: 10, padding: "10px 14px", font: "500 13px var(--font-body)", marginBottom: 14 }}>{error}</div>}

          {mock === null && !error ? (
            <div style={{ padding: "36px 0", display: "flex", flexDirection: "column", alignItems: "center", gap: 14 }}>
              <div className="spinner" style={{ width: 28, height: 28, borderColor: "rgba(44,110,107,.25)", borderTopColor: "var(--accent)" }} />
              <div style={{ font: "500 13px var(--font-body)", color: "var(--muted-2)" }}>Establishing a secure connection…</div>
            </div>
          ) : (
            <>
              <button className="btn btn-primary btn-block btn-lg" disabled={processing || mock === null} onClick={() => pay(false)}>
                {processing ? "Processing…" : `Pay ${formatMoney(total)} with Razorpay`}
              </button>
              {mock && (
                <button className="btn btn-ghost btn-sm btn-block" disabled={processing} onClick={() => pay(true)} style={{ marginTop: 8 }}>
                  Simulate a failed payment (demo)
                </button>
              )}
              <div style={{ textAlign: "center", marginTop: 12, font: "500 11px var(--font-body)", letterSpacing: ".08em", textTransform: "uppercase", color: "var(--muted-3)" }}>🔒 Razorpay {mock ? "mock" : "test"} mode · server-verified signatures</div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
