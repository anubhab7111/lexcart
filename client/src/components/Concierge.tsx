import { useEffect, useRef, useState } from "react";
import {
  conciergeChat,
  conciergeConfirm,
  conciergeReject,
  type ConciergeCart,
  type ConciergeProposal,
} from "../api";
import { payOrder } from "../lib/razorpay";
import { avatarTint, formatMoney, initials, type Lawyer, type UserProfile } from "../lib/ui";

interface Msg {
  id: number;
  role: "user" | "agent";
  text: string;
  lawyers?: Lawyer[];
}

interface Props {
  user: UserProfile | null;
  onNavigate: (v: any) => void;
  onDone: () => void;
}

const GREETING =
  "Hi! I'm the LexCart concierge. Tell me what legal help you need — for example, " +
  "\"I need help with a property dispute, budget under ₹4000\" — and I'll find the right " +
  "lawyer, suggest useful add-ons, and set up the booking. I can only propose payments: " +
  "nothing is charged until you press Confirm & Pay.";

export function Concierge({ user, onNavigate, onDone }: Props) {
  const [sessionId] = useState(() => `concierge-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`);
  const [messages, setMessages] = useState<Msg[]>([{ id: 0, role: "agent", text: GREETING }]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [cart, setCart] = useState<ConciergeCart | null>(null);
  const [proposal, setProposal] = useState<ConciergeProposal | null>(null);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [paying, setPaying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nextId = useRef(1);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, proposal]);

  const push = (m: Omit<Msg, "id">) =>
    setMessages((prev) => [...prev, { ...m, id: nextId.current++ }]);

  const send = async (text: string) => {
    const msg = text.trim();
    if (!msg || busy) return;
    if (!user) {
      onNavigate("signin");
      return;
    }
    push({ role: "user", text: msg });
    setInput("");
    setBusy(true);
    setError(null);
    try {
      const r = await conciergeChat(sessionId, msg);
      push({ role: "agent", text: r.reply, lawyers: r.lawyers });
      setCart(r.cart);
      setProposal(r.proposal);
      setSuggestions(r.suggestions || []);
    } catch (e: any) {
      push({ role: "agent", text: `Something went wrong: ${e.message}. Please try again.` });
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (simulateFailure = false) => {
    if (!proposal || !user) return;
    setPaying(true);
    setError(null);
    try {
      const order = await conciergeConfirm(sessionId, proposal.proposalId);
      const result = await payOrder(order, { name: user.name, email: user.email, simulateFailure });
      setProposal(null);
      setCart(null);
      push({
        role: "agent",
        text: `Payment verified (transaction ${result.transactionId}) — your booking is confirmed! You can see it under My Bookings, and every step I took is in the audit trail.`,
      });
      onDone();
    } catch (e: any) {
      setError(e.message);
      setProposal(null);
      push({
        role: "agent",
        text: `That payment didn't go through: ${e.message} Nothing was booked and no money moved — say "checkout" to try again.`,
      });
    } finally {
      setPaying(false);
    }
  };

  const reject = async () => {
    if (!proposal) return;
    await conciergeReject(sessionId, proposal.proposalId).catch(() => {});
    setProposal(null);
    push({ role: "agent", text: "No problem — I've cancelled that proposal. Want to change anything in the cart?" });
  };

  return (
    <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
      {/* chat column */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: "26px 8%" }}>
          {messages.map((m) => (
            <div key={m.id} className="fade-up" style={{ display: "flex", gap: 12, marginBottom: 18, flexDirection: m.role === "user" ? "row-reverse" : "row" }}>
              <div className="avatar" style={{ width: 34, height: 34, flex: "none", background: m.role === "user" ? "var(--surface-alt)" : "var(--accent)", color: m.role === "user" ? "var(--text)" : "#fff", fontSize: 13 }}>
                {m.role === "user" ? initials(user?.name || "You") : "⚡"}
              </div>
              <div style={{ maxWidth: "72%" }}>
                <div style={{ background: m.role === "user" ? "var(--accent)" : "var(--surface-alt)", color: m.role === "user" ? "#fff" : "var(--text)", borderRadius: 14, padding: "11px 15px", font: "400 14px/1.55 var(--font-body)" }}>
                  {m.text}
                </div>
                {m.lawyers && m.lawyers.length > 0 && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
                    {m.lawyers.map((l) => {
                      const tint = avatarTint(l.name);
                      return (
                        <div key={l.id} className="card" style={{ display: "flex", gap: 12, alignItems: "center", padding: 12 }}>
                          <div className="avatar" style={{ width: 40, height: 40, background: tint.bg, color: tint.fg }}>{initials(l.name)}</div>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ font: "700 13.5px var(--font-head)" }}>{l.name}</div>
                            <div style={{ font: "500 12px var(--font-body)", color: "var(--muted-2)" }}>
                              {l.specialty} · ★{l.rating} · {formatMoney(l.hourlyRate)}
                            </div>
                          </div>
                          <button className="btn btn-primary btn-sm" onClick={() => send(`Go with ${l.name}`)}>Select</button>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          ))}
          {busy && (
            <div style={{ display: "flex", gap: 10, alignItems: "center", color: "var(--muted-2)", font: "500 13px var(--font-body)" }}>
              <div className="spinner" style={{ width: 16, height: 16 }} /> The concierge is thinking…
            </div>
          )}
        </div>

        {suggestions.length > 0 && !busy && (
          <div style={{ display: "flex", gap: 8, padding: "0 8% 10px", flexWrap: "wrap" }}>
            {suggestions.map((s) => (
              <button key={s} className="chip" onClick={() => send(s)}>{s}</button>
            ))}
          </div>
        )}

        <form
          onSubmit={(e) => { e.preventDefault(); send(input); }}
          style={{ display: "flex", gap: 10, padding: "12px 8% 22px", borderTop: "1px solid var(--border)" }}
        >
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={user ? "Describe your legal need, or say 'checkout'…" : "Sign in to shop with the concierge"}
            disabled={!user || busy}
            style={{ flex: 1, border: "1px solid var(--border)", borderRadius: 12, padding: "12px 16px", font: "400 14px var(--font-body)", background: "var(--surface)" }}
          />
          <button type="submit" className="composer-send" title="Send" disabled={busy}>↑</button>
        </form>
      </div>

      {/* cart / gate column */}
      <div style={{ width: 320, flex: "none", borderLeft: "1px solid var(--border)", background: "var(--surface-alt)", padding: 20, overflowY: "auto" }}>
        <div className="eyebrow" style={{ fontSize: 11, marginBottom: 12 }}>Your cart</div>
        {!cart ? (
          <div style={{ font: "400 13px var(--font-body)", color: "var(--muted-2)" }}>
            Nothing yet. The concierge fills this as you chat — and can never pay from it without your confirmation.
          </div>
        ) : (
          <div className="card" style={{ padding: 16 }}>
            <div style={{ font: "700 14px var(--font-head)", marginBottom: 10 }}>{cart.lawyer.name}</div>
            {cart.lineItems.map((li) => (
              <div key={li.label} style={{ display: "flex", justifyContent: "space-between", font: "400 12.5px var(--font-body)", color: "var(--muted)", padding: "3px 0" }}>
                <span style={{ maxWidth: 190 }}>{li.label}</span>
                <span>{li.amountInr < 0 ? `−${formatMoney(-li.amountInr)}` : formatMoney(li.amountInr)}</span>
              </div>
            ))}
            <div className="divider" style={{ margin: "8px 0" }} />
            <div style={{ display: "flex", justifyContent: "space-between", font: "700 15px var(--font-head)" }}>
              <span>Total</span><span style={{ color: "var(--accent)" }}>{formatMoney(cart.totalInr)}</span>
            </div>
          </div>
        )}

        {proposal && (
          <div className="card" style={{ padding: 16, marginTop: 14, border: "1.5px solid var(--accent)" }}>
            <div style={{ font: "700 13.5px var(--font-head)", marginBottom: 6 }}>Approval needed</div>
            <div style={{ font: "400 12px/1.5 var(--font-body)", color: "var(--muted)", marginBottom: 6 }}>
              The concierge proposes charging <b>{formatMoney(proposal.totalInr)}</b>. It cannot proceed without you.
            </div>
            <div style={{ font: "400 11px/1.5 var(--font-body)", color: "var(--muted-3)", marginBottom: 12 }}>
              Guardrail check: {proposal.boundsNote}
            </div>
            <button className="btn btn-primary btn-block" disabled={paying} onClick={() => confirm(false)}>
              {paying ? "Processing…" : `Confirm & Pay ${formatMoney(proposal.totalInr)}`}
            </button>
            <button className="btn btn-ghost btn-sm btn-block" disabled={paying} onClick={reject} style={{ marginTop: 6 }}>
              Decline
            </button>
            <button className="btn btn-ghost btn-sm btn-block" disabled={paying} onClick={() => confirm(true)} style={{ marginTop: 2, color: "var(--muted-3)" }}>
              Simulate failed payment (demo)
            </button>
          </div>
        )}

        {error && (
          <div style={{ background: "#fbecea", color: "var(--danger)", borderRadius: 10, padding: "10px 12px", font: "500 12.5px var(--font-body)", marginTop: 12 }}>
            {error}
          </div>
        )}

        <div style={{ font: "400 11px/1.6 var(--font-body)", color: "var(--muted-3)", marginTop: 16 }}>
          Every agent step — searches, upsells, proposals, payments, refusals — is recorded in the{" "}
          <a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => onNavigate("merchant")}>audit trail</a>.
        </div>
      </div>
    </div>
  );
}
