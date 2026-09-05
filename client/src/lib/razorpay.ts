/**
 * One payment runner for every checkout surface (Payment page, concierge).
 *
 * Mock mode (backend has no Razorpay keys): asks the server's mock gateway
 * for a signed payment and verifies it — same verify path as production.
 * Real test mode: opens Razorpay checkout.js with the server-created order;
 * the handler posts the signature back for HMAC verification. Failures and
 * dismissals are reported to the server so the audit trail sees them.
 */

import {
  type CreatedOrder,
  mockPay,
  reportPaymentFailure,
  verifyPayment,
} from "../api";

declare global {
  interface Window {
    Razorpay?: any;
  }
}

export interface PaymentResult {
  status: string;
  transactionId: string;
  bookingId: string;
}

const CHECKOUT_SRC = "https://checkout.razorpay.com/v1/checkout.js";

// index.html loads checkout.js once at page load with no error handling; if
// that single attempt fails (a transient network blip, an ad-blocker, a slow
// connection), window.Razorpay stays undefined for the rest of the page's
// life and every later "say checkout to try again" would fail identically.
// Loading it here instead means each payment attempt gets a fresh chance:
// already-loaded is instant, and a prior failure doesn't poison the next try.
let checkoutLoad: Promise<void> | null = null;

function loadCheckoutScript(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  if (checkoutLoad) return checkoutLoad;

  checkoutLoad = new Promise<void>((resolve, reject) => {
    // A stale tag (e.g. index.html's, if its one-shot load already failed)
    // won't re-fire load/error just because we attach new listeners to it --
    // the browser already settled that request. Always start a fresh one.
    document.querySelectorAll(`script[src="${CHECKOUT_SRC}"]`).forEach((el) => el.remove());
    const script = document.createElement("script");
    script.src = CHECKOUT_SRC;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("checkout.js failed to load"));
    document.head.appendChild(script);
  }).catch((e) => {
    checkoutLoad = null; // let the next payment attempt try again
    throw e;
  });

  return checkoutLoad;
}

export async function payOrder(
  order: CreatedOrder,
  opts: { name?: string; email?: string; simulateFailure?: boolean } = {},
): Promise<PaymentResult> {
  if (order.mock) {
    const { razorpayPaymentId, razorpaySignature } = await mockPay(
      order.orderId,
      opts.simulateFailure === true,
    );
    return (await verifyPayment({
      orderId: order.orderId,
      razorpayPaymentId,
      razorpaySignature,
    })) as PaymentResult;
  }

  try {
    await loadCheckoutScript();
  } catch {
    throw new Error("Razorpay checkout.js failed to load — check your network and try again.");
  }

  return new Promise<PaymentResult>((resolve, reject) => {
    let settled = false;
    const rzp = new window.Razorpay({
      key: order.keyId,
      amount: order.amountPaise,
      currency: order.currency,
      name: "LexCart",
      description: "Legal consultation booking",
      order_id: order.razorpayOrderId,
      prefill: { name: opts.name || "", email: opts.email || "" },
      theme: { color: "#8c6f2f" },
      handler: async (resp: any) => {
        settled = true;
        try {
          resolve(
            (await verifyPayment({
              orderId: order.orderId,
              razorpayPaymentId: resp.razorpay_payment_id,
              razorpaySignature: resp.razorpay_signature,
            })) as PaymentResult,
          );
        } catch (e) {
          reject(e);
        }
      },
      modal: {
        ondismiss: () => {
          if (settled) return;
          reportPaymentFailure(order.orderId, "user dismissed the checkout modal").catch(() => {});
          reject(new Error("Payment was cancelled."));
        },
      },
    });
    // payment.failed is retryable inside the modal, so it is NOT reported
    // as terminal here — only a dismissed modal marks the order failed.
    rzp.open();
  });
}
