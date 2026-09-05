import { API_BASE_URL } from './config';
import type { Lawyer } from '../lib/ui';

const getAuthHeaders = () => {
    const token = localStorage.getItem('token');
    return token ? { 'Authorization': `Bearer ${token}` } : {};
};

/**
 * Pull a human-readable message out of an error response body, regardless
 * of which shape produced it: {"message": ...} (the app's own contract),
 * {"detail": "..."} (FastAPI's default), {"detail": [{msg, loc, type}, ...]}
 * (a Pydantic 422 validation error — an array, not a string), or a legacy
 * {"error": ...}. Without this, `new Error(someArray)` stringifies an array
 * of objects to the literal text "[object Object]", which is what a long
 * chat message or a too-short document used to render as.
 */
function extractErrorMessage(data: any, fallback: string): string {
    if (!data) return fallback;
    if (typeof data.message === 'string' && data.message) return data.message;
    if (typeof data.detail === 'string' && data.detail) return data.detail;
    if (Array.isArray(data.detail) && data.detail.length > 0) {
        const first = data.detail[0];
        if (first && typeof first.msg === 'string') return first.msg;
    }
    if (typeof data.error === 'string' && data.error) return data.error;
    return fallback;
}

export interface LoginCredentials {
    email: string;
    password: string;
}

export interface RegisterData {
    name: string;
    email: string;
    password: string;
}

export interface LawyerCriteria {
    specialty?: string;
    location?: string;
    [key: string]: any;
}

// ============================================================================
// Chat API - Connected to Python Chatbot via Express proxy
// ============================================================================

export interface ChatResponse {
    response: string;
    session_id: string;
    intent?: string;
    document_info?: Record<string, any>;
    crime_report?: Record<string, any>;
    lawyers_found?: Array<Record<string, any>>;
}

export interface ChatMessage {
    message: string;
    session_id?: string;
}

/**
 * Send a chat message to the AI legal assistant
 */
export async function sendChatMessage(message: string, sessionId?: string): Promise<ChatResponse> {
    const response = await fetch(`${API_BASE_URL}/chat`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...getAuthHeaders(),
        },
        body: JSON.stringify({ message, session_id: sessionId }),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(extractErrorMessage(error, 'Failed to send message'));
    }
    return response.json();
}

export interface StreamEvent {
    type: 'token' | 'done' | 'error' | 'stopped' | 'superseded';
    content?: string;
    session_id?: string;
    response?: string;
    intent?: string;
    lawyers_found?: Array<Record<string, any>>;
    document_info?: Record<string, any>;
    document_validation?: Record<string, any>;
    crime_report?: Record<string, any>;
}

/**
 * Send a chat message and stream the response token by token via SSE.
 * Calls `onToken` for each LLM token and `onDone` with metadata when complete.
 * Pass `signal` (from an AbortController) to let the caller cancel client-side
 * reading — call stopChatStream() as well to actually stop generation on the
 * server, since aborting the fetch alone doesn't cancel the backend's task.
 */
export async function sendChatMessageStream(
    message: string,
    sessionId: string | undefined,
    onToken: (token: string) => void,
    onDone: (metadata: StreamEvent) => void,
    onError?: (error: string) => void,
    signal?: AbortSignal,
): Promise<void> {
    const response = await fetch(`${API_BASE_URL}/chat/stream`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...getAuthHeaders(),
        },
        body: JSON.stringify({ message, session_id: sessionId }),
        signal,
    });

    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(extractErrorMessage(error, 'Failed to send message'));
    }

    const reader = response.body?.getReader();
    if (!reader) throw new Error('No response body');

    const decoder = new TextDecoder();
    let buffer = '';

    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            // Parse SSE lines from buffer
            const lines = buffer.split('\n');
            buffer = lines.pop() || ''; // Keep incomplete line in buffer

            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed.startsWith('data: ')) continue;

                const jsonStr = trimmed.slice(6); // Remove "data: " prefix
                try {
                    const event: StreamEvent = JSON.parse(jsonStr);

                    if (event.type === 'token' && event.content) {
                        onToken(event.content);
                    } else if (event.type === 'done' || event.type === 'stopped' || event.type === 'superseded') {
                        // 'superseded': a newer request for the same session_id
                        // already completed (e.g. two tabs on one conversation).
                        // This stream carries no final response — onDone falls
                        // back to whatever was already streamed, same as it does
                        // for 'stopped'. Without handling it, this branch's
                        // message bubble would be stuck showing "streaming"
                        // forever, since no other event ever follows it.
                        onDone(event);
                    } else if (event.type === 'error') {
                        onError?.(event.content || 'Unknown streaming error');
                    }
                } catch {
                    // Skip malformed JSON lines
                }
            }
        }
    } catch (e: any) {
        // The user clicking Stop aborts the fetch — that's an intentional
        // stop, not a failure, so don't surface it as an error.
        if (e?.name === 'AbortError') return;
        throw e;
    }
}

/**
 * Stop button: tells the server to cancel the in-flight generation for this
 * session (see /api/chat/stream/stop). Call alongside aborting the fetch —
 * closing the client's connection alone does not stop server-side generation.
 */
export async function stopChatStream(sessionId: string): Promise<void> {
    await fetch(`${API_BASE_URL}/chat/stream/stop`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...getAuthHeaders(),
        },
        body: JSON.stringify({ session_id: sessionId }),
    }).catch(() => {});
}

/**
 * Upload a document for AI analysis
 */
export async function uploadDocumentForAnalysis(
    file: File,
    message?: string,
    sessionId?: string,
    signal?: AbortSignal
): Promise<ChatResponse> {
    const formData = new FormData();
    formData.append('file', file);
    if (message) formData.append('message', message);
    if (sessionId) formData.append('session_id', sessionId);

    const response = await fetch(`${API_BASE_URL}/chat/upload`, {
        method: 'POST',
        headers: {
            ...getAuthHeaders(),
        },
        body: formData,
        signal,
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(extractErrorMessage(error, 'Failed to upload document'));
    }
    return response.json();
}

/**
 * Analyze document text directly without file upload
 */
export async function analyzeDocumentText(
    documentText: string,
    sessionId?: string
): Promise<ChatResponse> {
    const response = await fetch(`${API_BASE_URL}/chat/analyze-document`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...getAuthHeaders(),
        },
        body: JSON.stringify({ document_text: documentText, session_id: sessionId }),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(extractErrorMessage(error, 'Failed to analyze document'));
    }
    return response.json();
}

/**
 * Get crime reporting guidance
 */
export async function getCrimeReportGuidance(
    description: string,
    sessionId?: string
): Promise<ChatResponse> {
    const response = await fetch(`${API_BASE_URL}/chat/crime-report`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...getAuthHeaders(),
        },
        body: JSON.stringify({ description, session_id: sessionId }),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(extractErrorMessage(error, 'Failed to get crime report guidance'));
    }
    return response.json();
}

/**
 * Find lawyers using AI-powered search
 */
export async function findLawyersAI(
    query: string,
    location?: string,
    specialization?: string
) {
    const response = await fetch(`${API_BASE_URL}/chat/find-lawyer`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...getAuthHeaders(),
        },
        body: JSON.stringify({ query, location, specialization }),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(extractErrorMessage(error, 'Failed to find lawyers'));
    }
    return response.json();
}

/**
 * Get available legal specializations
 */
export async function getSpecializations(): Promise<{ specializations: string[] }> {
    const response = await fetch(`${API_BASE_URL}/chat/specializations`);
    if (!response.ok) {
        throw new Error('Failed to fetch specializations');
    }
    return response.json();
}

/**
 * Get recognized crime types
 */
export async function getCrimeTypes(): Promise<{ crime_types: string[] }> {
    const response = await fetch(`${API_BASE_URL}/chat/crime-types`);
    if (!response.ok) {
        throw new Error('Failed to fetch crime types');
    }
    return response.json();
}

/**
 * Clear a chat session
 */
export async function clearChatSession(sessionId: string): Promise<{ message: string }> {
    const response = await fetch(`${API_BASE_URL}/chat/session/${sessionId}`, {
        method: 'DELETE',
        headers: {
            ...getAuthHeaders(),
        },
    });
    if (!response.ok) {
        throw new Error('Failed to clear session');
    }
    return response.json();
}

/**
 * Get chat session history
 */
export async function getChatSessionHistory(sessionId: string): Promise<{
    session_id: string;
    messages: { role: 'user' | 'assistant' | 'system'; content: string }[];
    count: number;
}> {
    const response = await fetch(`${API_BASE_URL}/chat/session/${sessionId}/history`, {
        headers: {
            ...getAuthHeaders(),
        },
    });
    if (!response.ok) {
        throw new Error('Failed to fetch session history');
    }
    return response.json();
}

export interface ChatSessionSummary {
    id: string;
    userId: string;
    title: string | null;
    createdAt: string;
    updatedAt: string;
}

/**
 * List the current user's persisted chat sessions, most recent first.
 * Requires auth — the caller should treat a failed request (e.g. a guest
 * with no token) as "no sessions" rather than surfacing an error.
 */
export async function listChatSessions(): Promise<{ sessions: ChatSessionSummary[]; count: number }> {
    const response = await fetch(`${API_BASE_URL}/chat/sessions`, {
        headers: {
            ...getAuthHeaders(),
        },
    });
    if (!response.ok) {
        throw new Error('Failed to fetch sessions');
    }
    return response.json();
}

/**
 * Check chatbot service health
 */
export async function checkChatHealth() {
    const response = await fetch(`${API_BASE_URL}/chat/health`);
    if (!response.ok) {
        throw new Error('Failed to check chat health');
    }
    return response.json();
}

// ============================================================================
// Lawyers API
// ============================================================================

export async function fetchLawyers() {
    const response = await fetch(`${API_BASE_URL}/lawyers`);
    if (!response.ok) {
        throw new Error('Failed to fetch lawyers');
    }
    return response.json();
}

export async function fetchLawyerById(id: string) {
    const response = await fetch(`${API_BASE_URL}/lawyers/${id}`);
    if (!response.ok) {
        throw new Error('Failed to fetch lawyer');
    }
    return response.json();
}

export async function recommendLawyers(criteria: LawyerCriteria) {
    const response = await fetch(`${API_BASE_URL}/lawyers/recommend`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(criteria),
    });
    if (!response.ok) {
        throw new Error('Failed to fetch recommendations');
    }
    return response.json();
}

// ============================================================================
// Authentication API
// ============================================================================

export async function login(credentials: LoginCredentials) {
    const response = await fetch(`${API_BASE_URL}/auth/login`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(credentials),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(extractErrorMessage(error, 'Login failed'));
    }
    return response.json();
}

export async function register(userData: RegisterData) {
    const response = await fetch(`${API_BASE_URL}/auth/register`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(userData),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(extractErrorMessage(error, 'Registration failed'));
    }
    return response.json();
}

export async function fetchUserProfile() {
    const response = await fetch(`${API_BASE_URL}/auth/me`, {
        headers: {
            ...getAuthHeaders(),
        },
    });
    if (!response.ok) {
        throw new Error('Failed to fetch profile');
    }
    return response.json();
}

// ============================================================================
// Bookings API (Razorpay test mode + Postgres)
// ============================================================================

export interface Booking {
    id: string;
    userId: string;
    lawyerId: string;
    amount: number;
    transactionId: string;
    status: string;
    appointmentDate?: string | null;
    appointmentTime?: string | null;
    createdAt?: string | null;
}

export interface LineItem {
    label: string;
    amountInr: number;
}

export interface CreatedOrder {
    orderId: string;
    razorpayOrderId: string;
    amountInr: number;
    amountPaise: number;
    currency: string;
    keyId: string;
    mock: boolean;
    lineItems: LineItem[];
}

const postJson = (path: string, body: unknown) =>
    requestJson(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });

/** Public key id + whether the backend runs the mock gateway (no keys). */
export async function fetchPaymentConfig(): Promise<{ keyId: string; mock: boolean }> {
    return requestJson('/bookings/config');
}

/** Server-priced Razorpay order for the given cart. */
export async function createCheckoutOrder(
    lawyerId: string,
    addonIds: string[] = [],
    campaignId?: string,
): Promise<CreatedOrder> {
    return postJson('/bookings/create-order', { lawyerId, addonIds, campaignId });
}

/** Report checkout.js's success payload for HMAC verification -> booking. */
export async function verifyPayment(payload: {
    orderId: string;
    razorpayPaymentId: string;
    razorpaySignature: string;
}) {
    const data = await postJson('/bookings/verify', payload);
    if (data.status === 'error') throw new Error(data.message || 'Payment failed');
    return data as { status: string; transactionId: string; bookingId: string };
}

/** Mock-gateway stand-in for checkout.js (dev without Razorpay keys). */
export async function mockPay(orderId: string, fail = false) {
    return postJson('/bookings/mock-pay', { orderId, fail }) as Promise<{
        razorpayPaymentId: string;
        razorpaySignature: string;
    }>;
}

export async function reportPaymentFailure(orderId: string, reason: string) {
    return postJson('/bookings/failure', { orderId, reason });
}

// ============================================================================
// Concierge (conversational checkout agent)
// ============================================================================

export interface ConciergeCart {
    lawyer: Lawyer;
    addons: { id: string; name: string; priceInr: number; description: string }[];
    lineItems: LineItem[];
    totalInr: number;
}

export interface ConciergeProposal {
    proposalId: string;
    lineItems: LineItem[];
    totalInr: number;
    boundsNote: string;
}

export interface ConciergeReply {
    reply: string;
    lawyers: Lawyer[];
    cart: ConciergeCart | null;
    proposal: ConciergeProposal | null;
    suggestions: string[];
    mock: boolean;
    sessionId: string;
}

export interface ConciergeSessionSummary {
    id: string;
    userId: string;
    title: string | null;
    createdAt: string;
    updatedAt: string;
}

export interface ConciergeHistoryMessage {
    role: 'user' | 'agent';
    content: string;
    meta: { lawyers?: Lawyer[] } | null;
}

export interface ServiceAddon {
    id: string;
    name: string;
    description: string;
    priceInr: number;
    appliesTo: string;
}

export async function fetchAddons(): Promise<ServiceAddon[]> {
    return requestJson('/concierge/addons');
}

export async function conciergeChat(sessionId: string, message: string): Promise<ConciergeReply> {
    return postJson('/concierge/chat', { sessionId, message });
}

/** The human gate: approve the agent's proposal -> creates the real order. */
export async function conciergeConfirm(sessionId: string, proposalId: string): Promise<CreatedOrder> {
    return postJson('/concierge/confirm', { sessionId, proposalId });
}

export async function conciergeReject(sessionId: string, proposalId: string) {
    return postJson('/concierge/reject', { sessionId, proposalId });
}

/** List the current user's persisted concierge conversations, most recent first. */
export async function listConciergeSessions(): Promise<{ sessions: ConciergeSessionSummary[]; count: number }> {
    return requestJson('/concierge/sessions');
}

/** Full transcript for a concierge conversation the current user owns. */
export async function getConciergeSessionHistory(
    sessionId: string,
): Promise<{ sessionId: string; messages: ConciergeHistoryMessage[]; count: number }> {
    return requestJson(`/concierge/session/${sessionId}/history`);
}

export async function clearConciergeSession(sessionId: string): Promise<{ message: string }> {
    return requestJson(`/concierge/session/${sessionId}`, { method: 'DELETE' });
}

export interface AgentAuditEntry {
    id: string;
    actor: string;
    action: string;
    rationale: string;
    amountInr: number | null;
    boundsCheck: string;
    gateStatus: string;
    orderId: string | null;
    detail: Record<string, unknown>;
    createdAt: string | null;
}

export async function fetchMyAudit(limit = 50): Promise<AgentAuditEntry[]> {
    return requestJson(`/concierge/audit?limit=${limit}`);
}

// ============================================================================
// Campaign orchestrator
// ============================================================================

export interface Campaign {
    id: string;
    name: string;
    objective: string;
    targetSegment: string;
    lawyerId: string | null;
    discountPct: number;
    budgetInr: number;
    spentInr: number;
    message: string;
    status: string;
    paymentLinkUrl: string | null;
    conversions: number;
    createdAt: string | null;
}

export async function fetchCampaigns(): Promise<Campaign[]> {
    return requestJson('/campaigns');
}

export async function fetchActiveCampaigns(): Promise<Campaign[]> {
    return requestJson('/campaigns/active');
}

export async function draftCampaign(payload: {
    objective: string;
    lawyerId: string;
    discountPct: number;
    budgetInr: number;
}): Promise<Campaign> {
    return postJson('/campaigns/draft', payload);
}

export async function approveCampaign(id: string): Promise<Campaign> {
    return postJson(`/campaigns/${id}/approve`, {});
}

export async function rejectCampaign(id: string): Promise<Campaign> {
    return postJson(`/campaigns/${id}/reject`, {});
}

export async function fetchFullAudit(limit = 100): Promise<AgentAuditEntry[]> {
    return requestJson(`/campaigns/audit/all?limit=${limit}`);
}

export interface MerchantStats {
    totalRevenueInr: number;
    revenueByChannel: Record<string, number>;
    agenticRevenueInr: number;
    agenticSharePct: number;
    paidOrderCount: number;
    upsellAttachRatePct: number;
    campaignAttributedRevenueInr: number;
    campaignDiscountSpendInr: number;
    campaignRoi: number | null;
    guardrailRefusalCount: number;
}

export async function fetchMerchantStats(): Promise<MerchantStats> {
    return requestJson('/merchant/stats');
}

/** Confirmed appointments for a user, newest first. */
export async function fetchUserBookings(userId: string): Promise<Booking[]> {
    const response = await fetch(`${API_BASE_URL}/bookings/user-bookings/${userId}`, {
        headers: { ...getAuthHeaders() },
    });
    if (!response.ok) {
        throw new Error('Failed to fetch bookings');
    }
    return response.json();
}

// ============================================================================
// Shared error helper for the new-feature endpoints below, which return
// {"message": ...} bodies (see server/CLAUDE.md's API compatibility rules).
// ============================================================================

async function requestJson(path: string, options: RequestInit = {}) {
    const response = await fetch(`${API_BASE_URL}${path}`, {
        ...options,
        headers: { ...(options.headers || {}), ...getAuthHeaders() },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(extractErrorMessage(data, 'Request failed'));
    }
    return data;
}

// ============================================================================
// Bare Act Explorer
// ============================================================================

export async function searchBareAct(query: string, actHint?: string) {
    const params = new URLSearchParams({ q: query });
    if (actHint) params.set('act', actHint);
    return requestJson(`/bare-acts/search?${params.toString()}`);
}

// ============================================================================
// Similar Case Search
// ============================================================================

export async function searchSimilarCases(file: File) {
    const formData = new FormData();
    formData.append('file', file);
    return requestJson('/similar-cases/search', { method: 'POST', body: formData });
}

export async function searchSimilarCasesByText(text: string) {
    return requestJson('/similar-cases/search-text', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
    });
}

// ============================================================================
// My Cases
// ============================================================================

export interface SavedCase {
    id: string;
    cnr: string | null;
    court: string | null;
    caseNumber: string | null;
    year: number | null;
    title: string | null;
    status: string | null;
    lastSyncedAt: string | null;
    createdAt: string | null;
}

export async function saveCase(payload: { cnr?: string; court?: string; caseNumber?: string; year?: number }) {
    return requestJson('/cases', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
}

export async function fetchSavedCases(): Promise<SavedCase[]> {
    return requestJson('/cases');
}

export async function fetchCaseDetail(caseId: string) {
    return requestJson(`/cases/${caseId}`);
}

export async function addCaseNote(caseId: string, noteText: string) {
    return requestJson(`/cases/${caseId}/notes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ noteText }),
    });
}

export async function syncCase(caseId: string) {
    return requestJson(`/cases/${caseId}/sync`, { method: 'POST' });
}

export async function deleteCase(caseId: string) {
    return requestJson(`/cases/${caseId}`, { method: 'DELETE' });
}

// ============================================================================
// Court Cause List Search
// ============================================================================

export async function searchCauseList(params: { court: string; date: string; advocate?: string; judge?: string; caseNumber?: string }) {
    const qs = new URLSearchParams({ court: params.court, date: params.date });
    if (params.advocate) qs.set('advocate', params.advocate);
    if (params.judge) qs.set('judge', params.judge);
    if (params.caseNumber) qs.set('caseNumber', params.caseNumber);
    return requestJson(`/cause-list/search?${qs.toString()}`);
}

// ============================================================================
// Legal Document Vault
// ============================================================================

export interface VaultDocument {
    id: string;
    title: string;
    documentType: string;
    fileSizeBytes: number;
    mimeType: string;
    relatedCaseId: string | null;
    indexingStatus: string;
    createdAt: string | null;
}

export async function uploadVaultDocument(file: File, title: string, documentType: string, relatedCaseId?: string) {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('title', title);
    formData.append('documentType', documentType);
    if (relatedCaseId) formData.append('relatedCaseId', relatedCaseId);
    return requestJson('/vault/documents', { method: 'POST', body: formData });
}

export async function fetchVaultDocuments(): Promise<VaultDocument[]> {
    return requestJson('/vault/documents');
}

export async function searchVaultDocuments(query: string) {
    return requestJson('/vault/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }),
    });
}

export async function deleteVaultDocument(documentId: string) {
    return requestJson(`/vault/documents/${documentId}`, { method: 'DELETE' });
}

// ============================================================================
// Personal Legal Calendar
// ============================================================================

export interface CalendarEvent {
    id: string;
    title: string;
    eventType: string;
    startAt: string;
    endAt: string | null;
    relatedCaseId: string | null;
}

export async function fetchCalendarEvents(): Promise<CalendarEvent[]> {
    return requestJson('/calendar/events');
}

export async function createCalendarEvent(payload: { title: string; eventType: string; startAt: string; endAt?: string }) {
    return requestJson('/calendar/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
}

export async function deleteCalendarEvent(eventId: string) {
    return requestJson(`/calendar/events/${eventId}`, { method: 'DELETE' });
}

// ============================================================================
// Smart Notifications
// ============================================================================

export interface AppNotification {
    id: string;
    type: string;
    title: string;
    body: string;
    channel: string;
    status: string;
    readAt: string | null;
    createdAt: string | null;
}

export async function fetchNotifications(): Promise<AppNotification[]> {
    return requestJson('/notifications');
}

export async function markNotificationRead(notificationId: string) {
    return requestJson(`/notifications/${notificationId}/read`, { method: 'PATCH' });
}
