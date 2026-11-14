-- Solar Acquisition Platform Database Schema
-- PostgreSQL 15+

-- =============================================================================
-- EXTENSIONS
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================================================
-- ENUM TYPES
-- =============================================================================

-- Country codes
CREATE TYPE country_code AS ENUM ('IT', 'ES');

-- Tariff regimes
CREATE TYPE tariff_regime AS ENUM (
    -- Italy Conto Energia
    'CONTO_I',
    'CONTO_II',
    'CONTO_III',
    'CONTO_IV',
    'CONTO_V',
    -- Spain
    'RECORE',
    'MERCHANT',
    'UNKNOWN'
);

-- Owner types
CREATE TYPE owner_type AS ENUM (
    'FARMER',
    'SME',
    'MUNICIPALITY',
    'FINANCIAL',
    'DEVELOPER',
    'UNKNOWN'
);

-- Approachability levels
CREATE TYPE approachability AS ENUM ('HIGH', 'MEDIUM', 'LOW', 'UNKNOWN');

-- Scoring verdicts
CREATE TYPE verdict AS ENUM ('PURSUE', 'CONDITIONAL', 'PASS');

-- Outreach channels
CREATE TYPE outreach_channel AS ENUM (
    'PHYSICAL_MAIL',
    'EMAIL',
    'PHONE',
    'LINKEDIN'
);

-- Outreach status
CREATE TYPE outreach_status AS ENUM (
    'SENT',
    'DELIVERED',
    'OPENED',      -- Only for Spain (tracking permitted)
    'REPLIED',
    'BOUNCED',
    'OPT_OUT'
);

-- CRM stages
CREATE TYPE crm_stage AS ENUM (
    'IDENTIFIED',
    'CONTACTED',
    'RESPONDED',
    'CALL_SCHEDULED',
    'NDA_SIGNED',
    'DATA_ROOM'
);

-- =============================================================================
-- TARGETS TABLE
-- Core table for all solar plant targets
-- =============================================================================
CREATE TABLE targets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Source identification
    country country_code NOT NULL,
    source_registry VARCHAR(50) NOT NULL,  -- 'ATLAIMPIANTI', 'PRETOR', 'CNMC', etc.
    source_id VARCHAR(100),                 -- ID from source registry
    regional_registry_id VARCHAR(50),       -- Número de Registro Autonómico Definitivo (join key into regional registries)

    -- Plant details
    plant_name VARCHAR(255),
    capacity_kw DECIMAL(10, 2) NOT NULL,
    commissioning_date DATE,
    technology VARCHAR(50),                 -- 'GROUND_MOUNTED', 'ROOFTOP', etc.

    -- Location
    latitude DECIMAL(10, 7),
    longitude DECIMAL(10, 7),
    municipality VARCHAR(255),
    province VARCHAR(100),
    region VARCHAR(100),

    -- Tariff information
    tariff_regime tariff_regime DEFAULT 'UNKNOWN',
    tariff_rate_eur_kwh DECIMAL(6, 4),
    tariff_expiry_date DATE,
    fit_remaining_years DECIMAL(4, 2),

    -- Grid connection
    grid_operator VARCHAR(100),
    poi_capacity_kw DECIMAL(10, 2),

    -- Grid congestion (Bucket-1 coordinate-free enrichment, §3.7)
    curtailment_risk VARCHAR(20),           -- 'none' | 'moderate' | 'high' (REE province/zone)
    grid_congestion_zone VARCHAR(50),       -- coarse REE grid-zone label

    -- Status
    is_operating BOOLEAN DEFAULT TRUE,
    is_qualified BOOLEAN DEFAULT FALSE,    -- Passes basic filters
    qualification_notes TEXT,

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    data_pulled_at TIMESTAMP WITH TIME ZONE,

    -- Composite unique key for idempotency
    CONSTRAINT uq_target_source UNIQUE (country, source_registry, source_id)
);

-- Index for common queries
CREATE INDEX idx_targets_country ON targets(country);
CREATE INDEX idx_targets_capacity ON targets(capacity_kw);
CREATE INDEX idx_targets_commissioning ON targets(commissioning_date);
CREATE INDEX idx_targets_qualified ON targets(is_qualified);
CREATE INDEX idx_targets_tariff ON targets(tariff_regime);

-- Additive migration for databases created before the Bucket-1 coordinate-free
-- grid-congestion enrichment (§3.7). Safe to run against an existing `targets`
-- table; preserves all loaded rows, no data loss.
ALTER TABLE targets ADD COLUMN IF NOT EXISTS curtailment_risk VARCHAR(20);
ALTER TABLE targets ADD COLUMN IF NOT EXISTS grid_congestion_zone VARCHAR(50);

-- =============================================================================
-- CADASTRAL RECORDS TABLE
-- Links targets to land registry data
-- =============================================================================
CREATE TABLE cadastral_records (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_id UUID NOT NULL REFERENCES targets(id) ON DELETE CASCADE,

    -- Italy cadastral identifiers
    foglio VARCHAR(20),
    particella VARCHAR(20),
    subalterno VARCHAR(20),
    comune_catastale VARCHAR(100),

    -- Spain cadastral identifiers
    referencia_catastral VARCHAR(20),  -- 20-char alphanumeric code

    -- Lookup metadata
    lookup_method VARCHAR(50),          -- 'WMS_API', 'MANUAL', 'GEOMETRA'
    lookup_date TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    lookup_success BOOLEAN DEFAULT TRUE,
    lookup_notes TEXT,

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- One cadastral record per target (can update)
    CONSTRAINT uq_cadastral_target UNIQUE (target_id)
);

CREATE INDEX idx_cadastral_target ON cadastral_records(target_id);
CREATE INDEX idx_cadastral_foglio ON cadastral_records(foglio, particella);
CREATE INDEX idx_cadastral_ref ON cadastral_records(referencia_catastral);

-- =============================================================================
-- OWNERS TABLE
-- Resolved ownership information
-- =============================================================================
CREATE TABLE owners (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_id UUID NOT NULL REFERENCES targets(id) ON DELETE CASCADE,

    -- Owner identification
    owner_name VARCHAR(255) NOT NULL,
    owner_type owner_type DEFAULT 'UNKNOWN',
    approachability approachability DEFAULT 'UNKNOWN',

    -- Italy-specific
    codice_fiscale VARCHAR(16),          -- Tax code (individuals or companies)
    partita_iva VARCHAR(11),             -- VAT number
    pec_email VARCHAR(255),              -- Certified email

    -- Spain-specific
    nif_cif VARCHAR(15),                 -- Tax ID

    -- General contact info
    registered_address TEXT,
    phone VARCHAR(50),
    email VARCHAR(255),                  -- Non-certified email

    -- Director/representative info
    director_name VARCHAR(255),
    director_title VARCHAR(100),

    -- Ownership details
    ownership_percentage DECIMAL(5, 2),  -- 0.00 to 100.00

    -- Data source tracking
    data_source VARCHAR(100),            -- 'VISURA', 'INFOCAMERE', 'SEC', 'REGISTRO_MERCANTIL'
    data_source_date TIMESTAMP WITH TIME ZONE,

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Composite key: one primary owner per target
    CONSTRAINT uq_owner_target UNIQUE (target_id, owner_name)
);

CREATE INDEX idx_owners_target ON owners(target_id);
CREATE INDEX idx_owners_type ON owners(owner_type);
CREATE INDEX idx_owners_approachability ON owners(approachability);
CREATE INDEX idx_owners_codice_fiscale ON owners(codice_fiscale);

-- =============================================================================
-- SCORES TABLE
-- AI-generated scorecard evaluations
-- =============================================================================
CREATE TABLE scores (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_id UUID NOT NULL REFERENCES targets(id) ON DELETE CASCADE,

    -- Overall score
    total_score DECIMAL(5, 2) NOT NULL,  -- raw sum; 0-32 at screening, 0-100 at dd/full
    verdict verdict NOT NULL,

    -- Two-stage scoring (see IMPLEMENTATION_PLAN.md §3.6)
    scoring_stage VARCHAR(20) DEFAULT 'screening',  -- 'screening', 'dd', or 'full'
    screening_score DECIMAL(5, 2),             -- 0-100 normalized over screening-criteria max
    screening_verdict verdict,                 -- verdict derived from screening_score

    -- Category scores (from 100-point schema)
    revenue_contract_score DECIMAL(5, 2),      -- Max 22
    grid_repowering_score DECIMAL(5, 2),       -- Max 22
    resource_performance_score DECIMAL(5, 2),  -- Max 18
    technical_condition_score DECIMAL(5, 2),   -- Max 16
    land_permitting_score DECIMAL(5, 2),       -- Max 12
    commercial_structure_score DECIMAL(5, 2),  -- Max 10

    -- Detailed criteria scores (JSON)
    criteria_scores JSONB NOT NULL,            -- All 26 criteria with scores

    -- Deal-breaker gates
    gates_passed BOOLEAN NOT NULL DEFAULT FALSE,
    failed_gates JSONB,                        -- Array of failed gate IDs

    -- Confidence and evidence
    overall_confidence DECIMAL(3, 2),          -- 0.00 to 1.00
    evidence_citations JSONB,                  -- Sources used for scoring
    unknown_criteria JSONB,                    -- Criteria that couldn't be scored

    -- Scoring metadata
    model_version VARCHAR(50),
    prompt_version VARCHAR(50),
    scored_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    scored_by VARCHAR(50) DEFAULT 'AGENT',     -- 'AGENT' or 'HUMAN'

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Latest score per target
CREATE INDEX idx_scores_target ON scores(target_id);
CREATE INDEX idx_scores_verdict ON scores(verdict);
CREATE INDEX idx_scores_total ON scores(total_score DESC);
CREATE INDEX idx_scores_screening ON scores(screening_score DESC);
CREATE INDEX idx_scores_date ON scores(scored_at DESC);

-- Additive migration for databases created before the two-stage realign (§3.6).
-- Safe to run against an existing `scores` table; no data loss.
ALTER TABLE scores ADD COLUMN IF NOT EXISTS scoring_stage VARCHAR(20) DEFAULT 'screening';
ALTER TABLE scores ADD COLUMN IF NOT EXISTS screening_score DECIMAL(5, 2);
ALTER TABLE scores ADD COLUMN IF NOT EXISTS screening_verdict verdict;
CREATE INDEX IF NOT EXISTS idx_scores_screening ON scores(screening_score DESC);

-- =============================================================================
-- OUTREACH LOG TABLE
-- Track all outreach attempts
-- =============================================================================
CREATE TABLE outreach_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_id UUID NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    owner_id UUID REFERENCES owners(id) ON DELETE SET NULL,

    -- Outreach details
    channel outreach_channel NOT NULL,
    status outreach_status NOT NULL DEFAULT 'SENT',

    -- Content (for audit)
    template_id VARCHAR(50),
    subject_line VARCHAR(255),
    message_preview TEXT,                       -- First 500 chars

    -- Tracking (Spain only - Italy uses blind sequences)
    tracking_enabled BOOLEAN DEFAULT FALSE,
    opened_at TIMESTAMP WITH TIME ZONE,
    clicked_at TIMESTAMP WITH TIME ZONE,

    -- Response
    response_received_at TIMESTAMP WITH TIME ZONE,
    response_summary TEXT,

    -- CRM stage
    crm_stage crm_stage DEFAULT 'CONTACTED',

    -- Compliance
    consent_basis VARCHAR(50) DEFAULT 'LEGITIMATE_INTEREST',
    opt_out_requested BOOLEAN DEFAULT FALSE,
    opt_out_at TIMESTAMP WITH TIME ZONE,

    -- Sequence tracking
    sequence_number INTEGER DEFAULT 1,         -- 1st, 2nd, 3rd touch
    sequence_id VARCHAR(50),

    -- Timestamps
    sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_outreach_target ON outreach_log(target_id);
CREATE INDEX idx_outreach_owner ON outreach_log(owner_id);
CREATE INDEX idx_outreach_status ON outreach_log(status);
CREATE INDEX idx_outreach_channel ON outreach_log(channel);
CREATE INDEX idx_outreach_sent ON outreach_log(sent_at DESC);

-- =============================================================================
-- MEMOS TABLE
-- Generated screening memos
-- =============================================================================
CREATE TABLE memos (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_id UUID NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    score_id UUID REFERENCES scores(id) ON DELETE SET NULL,

    -- Memo content
    title VARCHAR(255) NOT NULL,
    executive_summary TEXT,
    full_content TEXT NOT NULL,                -- Full memo markdown/HTML

    -- Sections (structured)
    scorecard_breakdown JSONB,
    repower_bess_analysis JSONB,
    outstanding_dd_checklist JSONB,            -- Items that need DD

    -- Status
    is_draft BOOLEAN DEFAULT TRUE,
    approved_by VARCHAR(100),
    approved_at TIMESTAMP WITH TIME ZONE,

    -- Metadata
    generated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_memos_target ON memos(target_id);
CREATE INDEX idx_memos_draft ON memos(is_draft);

-- =============================================================================
-- AUDIT LOG TABLE
-- Track all data changes for compliance
-- =============================================================================
CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- What changed
    table_name VARCHAR(50) NOT NULL,
    record_id UUID NOT NULL,
    action VARCHAR(20) NOT NULL,               -- 'INSERT', 'UPDATE', 'DELETE'

    -- Change details
    old_values JSONB,
    new_values JSONB,
    changed_fields TEXT[],

    -- Who/what made the change
    changed_by VARCHAR(100),                   -- User or 'SYSTEM'
    change_source VARCHAR(100),                -- 'SCRAPER', 'API', 'MANUAL', etc.

    -- When
    changed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_audit_table ON audit_log(table_name);
CREATE INDEX idx_audit_record ON audit_log(record_id);
CREATE INDEX idx_audit_date ON audit_log(changed_at DESC);

-- =============================================================================
-- HELPER VIEWS
-- =============================================================================

-- Qualified targets ready for scoring
CREATE VIEW v_qualified_targets AS
SELECT
    t.*,
    cr.foglio,
    cr.particella,
    cr.referencia_catastral,
    o.owner_name,
    o.owner_type,
    o.approachability,
    o.pec_email,
    o.email
FROM targets t
LEFT JOIN cadastral_records cr ON t.id = cr.target_id
LEFT JOIN owners o ON t.id = o.target_id
WHERE t.is_qualified = TRUE;

-- Pursue list (scored targets with PURSUE verdict)
CREATE VIEW v_pursue_list AS
SELECT
    t.*,
    s.total_score,
    s.verdict,
    s.scored_at,
    o.owner_name,
    o.owner_type,
    o.pec_email,
    o.email
FROM targets t
JOIN scores s ON t.id = s.target_id
LEFT JOIN owners o ON t.id = o.target_id
WHERE s.verdict = 'PURSUE'
ORDER BY s.total_score DESC;

-- Outreach pipeline status
CREATE VIEW v_outreach_pipeline AS
SELECT
    t.id AS target_id,
    t.plant_name,
    t.country,
    t.capacity_kw,
    o.owner_name,
    ol.crm_stage,
    ol.channel,
    ol.status,
    ol.sent_at,
    ol.response_received_at,
    s.total_score,
    s.verdict
FROM targets t
JOIN owners o ON t.id = o.target_id
LEFT JOIN outreach_log ol ON t.id = ol.target_id
LEFT JOIN scores s ON t.id = s.target_id
ORDER BY ol.sent_at DESC;

-- =============================================================================
-- TRIGGERS
-- =============================================================================

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tr_targets_updated_at
    BEFORE UPDATE ON targets
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER tr_owners_updated_at
    BEFORE UPDATE ON owners
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

-- =============================================================================
-- COMMENTS
-- =============================================================================
COMMENT ON TABLE targets IS 'Core table for all solar plant acquisition targets';
COMMENT ON TABLE cadastral_records IS 'Land registry data linking plants to parcels';
COMMENT ON TABLE owners IS 'Resolved ownership information for outreach';
COMMENT ON TABLE scores IS 'AI-generated scorecard evaluations';
COMMENT ON TABLE outreach_log IS 'Audit trail of all outreach attempts';
COMMENT ON TABLE memos IS 'Generated screening memos for qualified targets';
COMMENT ON TABLE audit_log IS 'Full audit trail for compliance';
