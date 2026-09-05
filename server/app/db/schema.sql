-- CreateExtension
CREATE EXTENSION IF NOT EXISTS vector;

-- CreateEnum
CREATE TYPE "BookingStatus" AS ENUM ('pending', 'confirmed', 'failed');

-- CreateTable
CREATE TABLE "users" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "email" TEXT NOT NULL,
    "password" TEXT NOT NULL,
    "role" TEXT NOT NULL DEFAULT 'client',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "users_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "lawyers" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "specialty" TEXT NOT NULL,
    "experience" INTEGER NOT NULL,
    "rating" DOUBLE PRECISION NOT NULL,
    "hourly_rate" INTEGER NOT NULL,
    "location" TEXT NOT NULL,
    "bio" TEXT NOT NULL,
    "cases" INTEGER NOT NULL,
    "success_rate" INTEGER NOT NULL,
    "education" TEXT NOT NULL,
    "languages" TEXT[],
    "availability" TEXT NOT NULL,
    "bio_embedding" vector(1024),
    "user_id" TEXT,

    CONSTRAINT "lawyers_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "bookings" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "lawyer_id" TEXT NOT NULL,
    "amount" DECIMAL(10,2) NOT NULL,
    "transaction_id" TEXT NOT NULL,
    "status" "BookingStatus" NOT NULL DEFAULT 'pending',
    "appointment_date" TEXT,
    "appointment_time" TEXT,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "bookings_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "users_email_key" ON "users"("email");

-- CreateIndex
CREATE INDEX "bookings_user_id_status_idx" ON "bookings"("user_id", "status");

-- AddForeignKey
ALTER TABLE "bookings" ADD CONSTRAINT "bookings_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "bookings" ADD CONSTRAINT "bookings_lawyer_id_fkey" FOREIGN KEY ("lawyer_id") REFERENCES "lawyers"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "lawyers" ADD CONSTRAINT "lawyers_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- Note: APScheduler (see app/scheduler.py) creates and owns its own
-- "apscheduler_jobs" table automatically via SQLAlchemyJobStore. It is not
-- defined here — nothing in this codebase needs to query it directly.

-- ============================================================================
-- Similar Case Search (Phase 1)
-- ============================================================================

CREATE TABLE "similar_case_searches" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "uploaded_doc_summary" TEXT NOT NULL,
    "firac" JSONB NOT NULL,
    "results" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "similar_case_searches_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "similar_case_searches_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE
);

-- ============================================================================
-- My Cases (Phase 2)
-- ============================================================================

CREATE TABLE "saved_cases" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "cnr" TEXT,
    "court" TEXT,
    "case_number" TEXT,
    "year" INTEGER,
    "title" TEXT,
    "status" TEXT,
    "last_synced_at" TIMESTAMPTZ(6),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "saved_cases_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "saved_cases_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE
);

CREATE UNIQUE INDEX "saved_cases_user_id_cnr_key" ON "saved_cases"("user_id", "cnr") WHERE "cnr" IS NOT NULL;

CREATE TABLE "case_events" (
    "id" TEXT NOT NULL,
    "saved_case_id" TEXT NOT NULL,
    "event_type" TEXT NOT NULL,
    "event_date" TIMESTAMPTZ(6),
    "title" TEXT,
    "detail" TEXT,
    "source_url" TEXT,
    "raw_payload" JSONB NOT NULL DEFAULT '{}',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "case_events_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "case_events_saved_case_id_fkey" FOREIGN KEY ("saved_case_id") REFERENCES "saved_cases"("id") ON DELETE CASCADE
);

CREATE INDEX "case_events_saved_case_id_event_date_idx" ON "case_events"("saved_case_id", "event_date");

CREATE TABLE "case_notes" (
    "id" TEXT NOT NULL,
    "saved_case_id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "note_text" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "case_notes_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "case_notes_saved_case_id_fkey" FOREIGN KEY ("saved_case_id") REFERENCES "saved_cases"("id") ON DELETE CASCADE,
    CONSTRAINT "case_notes_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE
);

CREATE TABLE "case_ai_summaries" (
    "id" TEXT NOT NULL,
    "saved_case_id" TEXT NOT NULL,
    "summary_text" TEXT NOT NULL,
    "source_event_id" TEXT,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "case_ai_summaries_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "case_ai_summaries_saved_case_id_fkey" FOREIGN KEY ("saved_case_id") REFERENCES "saved_cases"("id") ON DELETE CASCADE,
    CONSTRAINT "case_ai_summaries_source_event_id_fkey" FOREIGN KEY ("source_event_id") REFERENCES "case_events"("id") ON DELETE SET NULL
);

-- ============================================================================
-- Notifications (Phase 2, generalized by Phase 4)
-- ============================================================================

CREATE TABLE "notification_preferences" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "email_enabled" BOOLEAN NOT NULL DEFAULT true,
    "push_enabled" BOOLEAN NOT NULL DEFAULT false,
    "sms_enabled" BOOLEAN NOT NULL DEFAULT false,
    "fcm_token" TEXT,
    "phone_number" TEXT,
    "type_overrides" JSONB NOT NULL DEFAULT '{}',

    CONSTRAINT "notification_preferences_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "notification_preferences_user_id_key" UNIQUE ("user_id"),
    CONSTRAINT "notification_preferences_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE
);

CREATE TABLE "notifications" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "type" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "body" TEXT NOT NULL,
    "related_case_id" TEXT,
    "related_case_event_id" TEXT,
    "channel" TEXT NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "scheduled_for" TIMESTAMPTZ(6),
    "sent_at" TIMESTAMPTZ(6),
    "read_at" TIMESTAMPTZ(6),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "notifications_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "notifications_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE,
    CONSTRAINT "notifications_related_case_id_fkey" FOREIGN KEY ("related_case_id") REFERENCES "saved_cases"("id") ON DELETE SET NULL,
    CONSTRAINT "notifications_related_case_event_id_fkey" FOREIGN KEY ("related_case_event_id") REFERENCES "case_events"("id") ON DELETE SET NULL
);

CREATE INDEX "notifications_user_id_created_at_idx" ON "notifications"("user_id", "created_at");

CREATE TABLE "cause_list_cache" (
    "id" TEXT NOT NULL,
    "court" TEXT NOT NULL,
    "list_date" TEXT NOT NULL,
    "fetched_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "entries" JSONB NOT NULL DEFAULT '[]',

    CONSTRAINT "cause_list_cache_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "cause_list_cache_court_list_date_key" ON "cause_list_cache"("court", "list_date");

-- ============================================================================
-- Legal Document Vault (Phase 3)
-- ============================================================================

CREATE TABLE "vault_documents" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "document_type" TEXT NOT NULL,
    "object_key" TEXT NOT NULL,
    "file_size_bytes" INTEGER NOT NULL,
    "mime_type" TEXT NOT NULL,
    "related_case_id" TEXT,
    "extracted_text" TEXT,
    "indexing_status" TEXT NOT NULL DEFAULT 'pending',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "vault_documents_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "vault_documents_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE,
    CONSTRAINT "vault_documents_related_case_id_fkey" FOREIGN KEY ("related_case_id") REFERENCES "saved_cases"("id") ON DELETE SET NULL
);

CREATE TABLE "vault_document_embeddings" (
    "id" TEXT NOT NULL,
    "vault_document_id" TEXT NOT NULL,
    "chunk_index" INTEGER NOT NULL,
    "chunk_text" TEXT NOT NULL,
    "embedding" vector(1024) NOT NULL,

    CONSTRAINT "vault_document_embeddings_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "vault_document_embeddings_vault_document_id_fkey" FOREIGN KEY ("vault_document_id") REFERENCES "vault_documents"("id") ON DELETE CASCADE
);

CREATE TABLE "vault_document_permissions" (
    "id" TEXT NOT NULL,
    "vault_document_id" TEXT NOT NULL,
    "shared_with_user_id" TEXT NOT NULL,
    "permission" TEXT NOT NULL DEFAULT 'view',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "vault_document_permissions_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "vault_document_permissions_vault_document_id_fkey" FOREIGN KEY ("vault_document_id") REFERENCES "vault_documents"("id") ON DELETE CASCADE,
    CONSTRAINT "vault_document_permissions_shared_with_user_id_fkey" FOREIGN KEY ("shared_with_user_id") REFERENCES "users"("id") ON DELETE CASCADE
);

-- ============================================================================
-- Chat History
-- ============================================================================

CREATE TYPE "MessageRole" AS ENUM ('user', 'assistant', 'system');

CREATE TABLE "chat_sessions" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "title" TEXT,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "chat_sessions_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "chat_sessions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE
);

CREATE INDEX "chat_sessions_user_id_updated_at_idx" ON "chat_sessions"("user_id", "updated_at" DESC);

CREATE TABLE "chat_messages" (
    "id" TEXT NOT NULL,
    "session_id" TEXT NOT NULL,
    "role" "MessageRole" NOT NULL,
    -- content is always canonical English (language-independent memory).
    -- language / content_display preserve the original-language turn for
    -- re-rendering history; NULL / 'en' for English turns and legacy rows.
    "content" TEXT NOT NULL,
    "language" TEXT NOT NULL DEFAULT 'en',
    "content_display" TEXT,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "chat_messages_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "chat_messages_session_id_fkey" FOREIGN KEY ("session_id") REFERENCES "chat_sessions"("id") ON DELETE CASCADE
);

CREATE INDEX "chat_messages_session_id_created_at_idx" ON "chat_messages"("session_id", "created_at");

-- ============================================================================
-- Concierge conversation history (separate from chat_sessions/chat_messages
-- above -- see the comment on ConciergeSession in app/db/models.py)
-- ============================================================================

CREATE TABLE "concierge_sessions" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "title" TEXT,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "concierge_sessions_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "concierge_sessions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE
);

CREATE INDEX "concierge_sessions_user_id_updated_at_idx" ON "concierge_sessions"("user_id", "updated_at" DESC);

CREATE TABLE "concierge_messages" (
    "id" TEXT NOT NULL,
    "session_id" TEXT NOT NULL,
    "role" TEXT NOT NULL,
    "content" TEXT NOT NULL,
    "meta" JSONB,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "concierge_messages_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "concierge_messages_session_id_fkey" FOREIGN KEY ("session_id") REFERENCES "concierge_sessions"("id") ON DELETE CASCADE
);

CREATE INDEX "concierge_messages_session_id_created_at_idx" ON "concierge_messages"("session_id", "created_at");

-- ============================================================================
-- Personal Legal Calendar (Phase 4)
-- ============================================================================

CREATE TABLE "calendar_events" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "event_type" TEXT NOT NULL,
    "start_at" TIMESTAMPTZ(6) NOT NULL,
    "end_at" TIMESTAMPTZ(6),
    "related_case_id" TEXT,
    "related_booking_id" TEXT,
    "related_case_event_id" TEXT,
    "google_calendar_event_id" TEXT,
    "outlook_event_id" TEXT,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "calendar_events_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "calendar_events_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE,
    CONSTRAINT "calendar_events_related_case_id_fkey" FOREIGN KEY ("related_case_id") REFERENCES "saved_cases"("id") ON DELETE SET NULL,
    CONSTRAINT "calendar_events_related_booking_id_fkey" FOREIGN KEY ("related_booking_id") REFERENCES "bookings"("id") ON DELETE SET NULL,
    CONSTRAINT "calendar_events_related_case_event_id_fkey" FOREIGN KEY ("related_case_event_id") REFERENCES "case_events"("id") ON DELETE SET NULL
);

-- One calendar_events row per source case_events hearing row — lets
-- sync_case_events() upsert instead of creating stale duplicates if a
-- hearing's own case_event row is ever updated in place.
CREATE UNIQUE INDEX "calendar_events_related_case_event_id_key" ON "calendar_events"("related_case_event_id") WHERE "related_case_event_id" IS NOT NULL;

CREATE INDEX "calendar_events_user_id_start_at_idx" ON "calendar_events"("user_id", "start_at");

-- ============================================================================
-- Agentic Commerce (Razorpay buildathon — Track 01)
-- ============================================================================

CREATE TYPE "OrderStatus" AS ENUM ('created', 'paid', 'failed', 'refunded');
CREATE TYPE "CampaignStatus" AS ENUM ('draft', 'active', 'rejected', 'completed');

CREATE TABLE "service_addons" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT NOT NULL,
    "price_inr" INTEGER NOT NULL,
    "applies_to" TEXT NOT NULL DEFAULT '',
    "active" BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT "service_addons_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "orders" (
    "id" TEXT NOT NULL,
    "user_id" TEXT REFERENCES "users"("id") ON DELETE SET NULL,
    "lawyer_id" TEXT NOT NULL REFERENCES "lawyers"("id") ON DELETE RESTRICT,
    "addon_ids" JSONB NOT NULL DEFAULT '[]',
    "base_amount_inr" INTEGER NOT NULL,
    "addon_amount_inr" INTEGER NOT NULL DEFAULT 0,
    "fee_amount_inr" INTEGER NOT NULL DEFAULT 0,
    "discount_amount_inr" INTEGER NOT NULL DEFAULT 0,
    "total_amount_inr" INTEGER NOT NULL,
    "razorpay_order_id" TEXT NOT NULL,
    "razorpay_payment_id" TEXT,
    "razorpay_refund_id" TEXT,
    "status" "OrderStatus" NOT NULL DEFAULT 'created',
    "channel" TEXT NOT NULL DEFAULT 'web',
    "campaign_id" TEXT,
    "agent_key_id" TEXT,
    "buyer_reference" TEXT,
    "booking_id" TEXT REFERENCES "bookings"("id") ON DELETE SET NULL,
    "failure_reason" TEXT,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "orders_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "orders_razorpay_order_id_key" ON "orders"("razorpay_order_id");
CREATE INDEX "orders_user_id_idx" ON "orders"("user_id");
-- Unique (not just indexed): agent_create_order's buyerReference dedup and
-- confirm_campaign_link_payment's razorpay_payment_id dedup are both
-- read-then-insert, which races under concurrent retries/duplicate webhook
-- deliveries -- the uniqueness is what turns the loser's insert into a
-- catchable IntegrityError instead of a silent duplicate order.
CREATE UNIQUE INDEX "orders_agent_key_id_buyer_reference_key" ON "orders"("agent_key_id", "buyer_reference")
    WHERE "buyer_reference" IS NOT NULL;
CREATE UNIQUE INDEX "orders_razorpay_payment_id_key" ON "orders"("razorpay_payment_id")
    WHERE "razorpay_payment_id" IS NOT NULL;

CREATE TABLE "agent_actions" (
    "id" TEXT NOT NULL,
    "actor" TEXT NOT NULL,
    "actor_ref" TEXT NOT NULL DEFAULT '',
    "user_id" TEXT REFERENCES "users"("id") ON DELETE SET NULL,
    "action" TEXT NOT NULL,
    "rationale" TEXT NOT NULL DEFAULT '',
    "amount_inr" INTEGER,
    "bounds_check" TEXT NOT NULL DEFAULT 'n/a',
    "gate_status" TEXT NOT NULL DEFAULT 'not_required',
    "order_id" TEXT,
    "detail" JSONB NOT NULL DEFAULT '{}',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "agent_actions_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "agent_actions_created_at_idx" ON "agent_actions"("created_at");

CREATE TABLE "agent_api_keys" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "key_hash" TEXT NOT NULL,
    "active" BOOLEAN NOT NULL DEFAULT TRUE,
    "daily_limit_inr" INTEGER NOT NULL DEFAULT 50000,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_used_at" TIMESTAMPTZ(6),
    CONSTRAINT "agent_api_keys_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "agent_api_keys_key_hash_key" ON "agent_api_keys"("key_hash");

CREATE TABLE "campaigns" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "objective" TEXT NOT NULL,
    "target_segment" TEXT NOT NULL DEFAULT '',
    "lawyer_id" TEXT REFERENCES "lawyers"("id") ON DELETE SET NULL,
    "discount_pct" INTEGER NOT NULL,
    "budget_inr" INTEGER NOT NULL,
    "spent_inr" INTEGER NOT NULL DEFAULT 0,
    "message" TEXT NOT NULL DEFAULT '',
    "status" "CampaignStatus" NOT NULL DEFAULT 'draft',
    "payment_link_id" TEXT,
    "payment_link_url" TEXT,
    "conversions" INTEGER NOT NULL DEFAULT 0,
    "approved_at" TIMESTAMPTZ(6),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "campaigns_pkey" PRIMARY KEY ("id")
);
