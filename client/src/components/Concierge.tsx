import { useCallback, useEffect, useRef, useState, type MouseEvent } from "react";
import {
  clearConciergeSession,
  conciergeChat,
  conciergeConfirm,
  conciergeReject,
  getConciergeSessionHistory,
  listConciergeSessions,
  type ConciergeCart,
  type ConciergeProposal,
  type ConciergeSessionSummary,
} from "../api";
import { payOrder } from "../lib/razorpay";
import { avatarTint, formatMoney, initials, uuid, type Lawyer, type UserProfile } from "../lib/ui";

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
  const [sessionId, setSessionId] = useState<string | undefined>(undefined);
  const [messages, setMessages] = useState<Msg[]>([{ id: 0, role: "agent", text: GREETING }]);
  const [sessions, setSessions] = useState<ConciergeSessionSummary[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
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

  const refreshSessions = useCallback(async () => {
    if (!user) {
      setSessions([]);
      return;
    }
    try {
      const { sessions: rows } = await listConciergeSessions();
      setSessions(rows);
    } catch {
      // Non-fatal — leave whatever list we already have.
    }
  }, [user]);

  // Covers initial mount, login, and logout. On logout, also drop any
  // transcript on screen so the next account never sees it.
  useEffect(() => {
    refreshSessions();
    if (!user) {
      setSessionId(undefined);
      setMessages([{ id: 0, role: "agent", text: GREETING }]);
      setCart(null);
      setProposal(null);
      setSuggestions([]);
      nextId.current = 1;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  const push = (m: Omit<Msg, "id">) =>
    setMessages((prev) => [...prev, { ...m, id: nextId.current++ }]);

  const newConversation = () => {
    setSessionId(undefined);
    setMessages([{ id: 0, role: "agent", text: GREETING }]);
    setCart(null);
    setProposal(null);
    setSuggestions([]);
    setError(null);
    nextId.current = 1;
  };

  // Restores the transcript + inline lawyer cards only (matching Ask AI) --
  // the live cart and any pending proposal are turn-derived server state,
  // not part of the durable history, so they stay empty until the user
  // chats again.
  const loadSession = useCallback(
    async (id: string) => {
      if (id === sessionId || busy) return;
      setLoadingHistory(true);
      try {
        const { messages: history } = await getConciergeSessionHistory(id);
        setSessionId(id);
        setMessages(
          history.map((m, i) => ({
            id: i,
            role: m.role === "user" ? "user" : "agent",
            text: m.content,
            lawyers: m.meta?.lawyers,
          }))
        );
        nextId.current = history.length + 1;
        setCart(null);
        setProposal(null);
        setSuggestions([]);
        setError(null);
      } catch {
        // Leave the current view unchanged on failure.
      } finally {
        setLoadingHistory(false);
      }
    },
    [sessionId, busy]
  );

  const deleteSession = useCallback(
    async (e: MouseEvent, id: string) => {
      e.stopPropagation();
      try {
        await clearConciergeSession(id);
      } catch {
        // ignore
      }
      await refreshSessions();
      if (id === sessionId) newConversation();
    },
    [sessionId, refreshSessions]
  );

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
      const activeSessionId = sessionId ?? uuid();
      const r = await conciergeChat(activeSessionId, msg);
      setSessionId(r.sessionId || activeSessionId);
      push({ role: "agent", text: r.reply, lawyers: r.lawyers });
      setCart(r.cart);
      setProposal(r.proposal);
      setSuggestions(r.suggestions || []);
      refreshSessions();
    } catch (e: any) {
      push({ role: "agent", text: `Something went wrong: ${e.message}. Please try again.` });
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (simulateFailure = false) => {
    if (!proposal || !user || !sessionId) return;
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
    if (!proposal || !sessionId) return;
    await conciergeReject(sessionId, proposal.proposalId).catch(() => {});
    setProposal(null);
    push({ role: "agent", text: "No problem — I've cancelled that proposal. Want to change anything in the cart?" });
  };

  return (
    <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
      {/* conversations sidebar */}
      <div className="lw-sidebar" style={{ width: 220, flex: "none", background: "var(--surface-alt)", borderRight: "1px solid var(--border)", display: "flex", flexDirection: "column", padding: "18px 14px" }}>
        <button onClick={newConversation} className="side-new">
          <span style={{ fontSize: 15, color: "var(--accent)" }}>+</span> New conversation
        </button>
        <div className="eyebrow" style={{ padding: "0 12px 8px", fontSize: 11 }}>Recent</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2, overflow: "auto" }}>
          {user && sessions.length === 0 && (
            <div style={{ padding: "6px 12px", font: "400 12.5px var(--font-body)", color: "var(--muted-3)" }}>No conversations yet</div>
          )}
          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => loadSession(s.id)}
              className={`side-thread${s.id === sessionId ? " active" : ""}`}
              style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6 }}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.title || "Untitled"}</span>
              <span
                onClick={(e) => deleteSession(e, s.id)}
                className="side-thread-delete"
                style={{ flex: "none", opacity: 0, cursor: "pointer" }}
                title="Delete conversation"
              >
                ✕
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* chat column */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        {loadingHistory ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <div style={{ display: "flex", gap: 4 }}>
              {[0, 1, 2].map((i) => (
                <span key={i} style={{ width: 6, height: 6, borderRadius: 99, background: "var(--faint)", display: "inline-block", animation: `lw-dotPulse 1.2s infinite ${i * 0.15}s` }} />
              ))}
            </div>
          </div>
        ) : (
        <div ref={scrollRef} style={{ flex: 1, overflow: "auto", display: "flex", justifyContent: "center" }}>
          <div style={{ width: "100%", maxWidth: 720, padding: "48px 24px 24px", display: "flex", flexDirection: "column", gap: 32 }}>
            {messages.map((m) => (m.role === "user" ? (
              <div key={m.id} style={{ alignSelf: "flex-end", maxWidth: "72%", background: "var(--user-bubble-bg)", color: "var(--user-bubble-fg)", padding: "14px 18px", borderRadius: 18, font: "400 15px/1.6 var(--font-body)", whiteSpace: "pre-wrap" }}>{m.text}</div>
            ) : (
              <div key={m.id} className="fade-up" style={{ display: "flex", gap: 12 }}>
                <div style={{ width: 30, height: 30, flex: "none", borderRadius: 9, background: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", font: "700 13px var(--font-head)" }}>L</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ font: "600 13px var(--font-body)", color: "var(--muted-2)", marginBottom: 6 }}>LexCart Concierge</div>
                  <div style={{ font: "400 16px/1.75 var(--font-body)", color: "var(--text-strong)", whiteSpace: "pre-wrap" }}>{m.text}</div>
                  {m.lawyers && m.lawyers.length > 0 && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 16 }}>
                      {m.lawyers.map((l) => {
                        const tint = avatarTint(l.name);
                        return (
                          <div key={l.id} className="card" style={{ display: "flex", gap: 14, alignItems: "center" }}>
                            <div className="avatar" style={{ width: 46, height: 46, background: tint.bg, color: tint.fg }}>{initials(l.name)}</div>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ font: "700 14.5px var(--font-head)" }}>{l.name}</div>
                              <div style={{ font: "500 12.5px var(--font-body)", color: "var(--muted-2)" }}>
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
            )))}
            {busy && (
              <div style={{ display: "flex", gap: 12 }}>
                <div style={{ width: 30, height: 30, flex: "none", borderRadius: 9, background: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", font: "700 13px var(--font-head)" }}>L</div>
                <div style={{ display: "flex", gap: 4, padding: "10px 0" }}>
                  {[0, 1, 2].map((i) => (
                    <span key={i} style={{ width: 6, height: 6, borderRadius: 99, background: "var(--faint)", display: "inline-block", animation: `lw-dotPulse 1.2s infinite ${i * 0.15}s` }} />
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
        )}

        {/* composer */}
        <div style={{ display: "flex", justifyContent: "center", padding: "0 24px 22px", flex: "none" }}>
          <div style={{ width: "100%", maxWidth: 720 }}>
            {suggestions.length > 0 && !busy && (
              <div style={{ display: "flex", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
                {suggestions.map((s) => (
                  <button key={s} className="chip" onClick={() => send(s)}>{s}</button>
                ))}
              </div>
            )}
            <form
              onSubmit={(e) => { e.preventDefault(); send(input); }}
              style={{ display: "flex", alignItems: "flex-end", gap: 10, background: "var(--surface)", border: "1px solid var(--border-2)", borderRadius: 18, padding: "12px 12px 12px 18px", boxShadow: "var(--shadow-soft)" }}
            >
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }}
                placeholder={user ? "Describe your legal need, or say 'checkout'…" : "Sign in to shop with the concierge"}
                disabled={!user || busy}
                rows={1}
                style={{ flex: 1, border: "none", outline: "none", resize: "none", background: "transparent", font: "400 15px/1.5 var(--font-body)", color: "var(--text)", maxHeight: 160 }}
              />
              <button type="submit" className="composer-send" title="Send" disabled={busy || !user}>↑</button>
            </form>
          </div>
        </div>
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
