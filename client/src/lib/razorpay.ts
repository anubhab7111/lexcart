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

  return new Promise<PaymentResult>((resolve, reject) => {
    if (!window.Razorpay) {
      reject(new Error("Razorpay checkout.js failed to load — check your network."));
      return;
    }
    let settled = false;
    const rzp = new window.Razorpay({
      key: order.keyId,
      amount: order.amountPaise,
      currency: order.currency,
      name: "LexCart",
      description: "Legal consultation booking",
      order_id: order.razorpayOrderId,
      prefill: { name: opts.name || "", email: opts.email || "" },
      theme: { color: "#2c6e6b" },
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
