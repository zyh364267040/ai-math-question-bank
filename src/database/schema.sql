PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS regions (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS exam_types (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS question_types (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS review_statuses (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS usability_statuses (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS difficulty_levels (
    level INTEGER PRIMARY KEY CHECK (level BETWEEN 1 AND 5),
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_statuses (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS source_papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    file_size INTEGER NOT NULL CHECK (file_size > 0),
    original_filename TEXT NOT NULL CHECK (length(trim(original_filename)) > 0),
    stored_path TEXT NOT NULL UNIQUE CHECK (
        stored_path LIKE 'raw_papers/%' AND
        substr(stored_path, 1, 1) <> '/' AND
        stored_path NOT LIKE '%..%' AND
        stored_path NOT LIKE '%\%'
    ),
    region_code TEXT NOT NULL REFERENCES regions(code) ON DELETE RESTRICT,
    exam_year INTEGER CHECK (exam_year IS NULL OR exam_year BETWEEN 1900 AND 9999),
    exam_type_code TEXT NOT NULL REFERENCES exam_types(code) ON DELETE RESTRICT,
    paper_name TEXT NOT NULL CHECK (length(trim(paper_name)) > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_paper_id INTEGER NOT NULL REFERENCES source_papers(id) ON DELETE RESTRICT,
    page_start INTEGER CHECK (page_start IS NULL OR page_start > 0),
    page_end INTEGER CHECK (page_end IS NULL OR page_end > 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'needs_review', 'completed', 'failed')
    ),
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (page_start IS NULL AND page_end IS NULL) OR
        (page_start IS NOT NULL AND page_end IS NOT NULL AND page_start <= page_end)
    )
);

CREATE TABLE IF NOT EXISTS import_page_render_runs (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'completed', 'failed')
    ),
    dpi INTEGER NOT NULL DEFAULT 300 CHECK (dpi = 300),
    total_pages INTEGER CHECK (total_pages IS NULL OR total_pages > 0),
    rendered_pages INTEGER NOT NULL DEFAULT 0 CHECK (rendered_pages >= 0),
    manifest_sha256 TEXT CHECK (
        manifest_sha256 IS NULL OR (
            length(manifest_sha256) = 64 AND
            manifest_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    manifest_byte_size INTEGER CHECK (
        manifest_byte_size IS NULL OR manifest_byte_size > 0
    ),
    published_batch_id TEXT CHECK (
        published_batch_id IS NULL OR length(published_batch_id) BETWEEN 1 AND 100
    ),
    source_pdf_sha256 TEXT CHECK (
        source_pdf_sha256 IS NULL OR (
            length(source_pdf_sha256) = 64 AND
            source_pdf_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    error_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (total_pages IS NULL OR rendered_pages <= total_pages)
);

CREATE TABLE IF NOT EXISTS import_layout_analysis_runs (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'completed', 'failed')
    ),
    total_pages INTEGER CHECK (total_pages IS NULL OR total_pages > 0),
    analyzed_pages INTEGER NOT NULL DEFAULT 0 CHECK (analyzed_pages >= 0),
    detected_questions INTEGER NOT NULL DEFAULT 0 CHECK (detected_questions >= 0),
    manifest_sha256 TEXT CHECK (
        manifest_sha256 IS NULL OR length(manifest_sha256) = 64
    ),
    manifest_byte_size INTEGER CHECK (
        manifest_byte_size IS NULL OR manifest_byte_size > 0
    ),
    published_batch_id TEXT CHECK (
        published_batch_id IS NULL OR length(published_batch_id) BETWEEN 1 AND 64
    ),
    source_pdf_sha256 TEXT CHECK (
        source_pdf_sha256 IS NULL OR length(source_pdf_sha256) = 64
    ),
    render_manifest_sha256 TEXT CHECK (
        render_manifest_sha256 IS NULL OR length(render_manifest_sha256) = 64
    ),
    error_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (total_pages IS NULL OR analyzed_pages <= total_pages),
    CHECK (
        status != 'completed' OR (
            total_pages IS NOT NULL AND analyzed_pages = total_pages AND
            manifest_sha256 IS NOT NULL AND manifest_byte_size IS NOT NULL AND
            published_batch_id IS NOT NULL AND source_pdf_sha256 IS NOT NULL AND
            render_manifest_sha256 IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS import_upload_receipts (
    token TEXT PRIMARY KEY CHECK (
        length(trim(token)) BETWEEN 1 AND 200
    ),
    source_paper_id INTEGER NOT NULL REFERENCES source_papers(id) ON DELETE RESTRICT,
    import_job_id INTEGER NOT NULL UNIQUE REFERENCES import_jobs(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS import_question_split_runs (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'completed', 'failed')
    ),
    question_count INTEGER CHECK (question_count IS NULL OR question_count >= 0),
    processed_pages INTEGER NOT NULL DEFAULT 0 CHECK (processed_pages >= 0),
    error_message TEXT,
    codex_run_id TEXT CHECK (
        codex_run_id IS NULL OR length(codex_run_id) BETWEEN 1 AND 200
    ),
    result_manifest_sha256 TEXT CHECK (
        result_manifest_sha256 IS NULL OR (
            length(result_manifest_sha256) = 64 AND
            result_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    render_manifest_sha256 TEXT CHECK (
        render_manifest_sha256 IS NULL OR length(render_manifest_sha256) = 64
    ),
    source_pdf_sha256 TEXT CHECK (
        source_pdf_sha256 IS NULL OR length(source_pdf_sha256) = 64
    ),
    crop_manifest_sha256 TEXT CHECK (
        crop_manifest_sha256 IS NULL OR length(crop_manifest_sha256) = 64
    ),
    crop_generation_id TEXT CHECK (
        crop_generation_id IS NULL OR length(crop_generation_id) = 32
    ),
    crop_manifest_signature TEXT CHECK (
        crop_manifest_signature IS NULL OR length(crop_manifest_signature) = 64
    ),
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        status != 'completed' OR (
            question_count IS NOT NULL AND question_count > 0 AND
            processed_pages > 0 AND codex_run_id IS NOT NULL AND
            result_manifest_sha256 IS NOT NULL AND render_manifest_sha256 IS NOT NULL AND
            source_pdf_sha256 IS NOT NULL AND crop_manifest_sha256 IS NOT NULL AND
            crop_generation_id IS NOT NULL AND crop_manifest_signature IS NOT NULL AND
            completed_at IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS historical_v1_crop_recoveries (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    source_paper_id INTEGER NOT NULL REFERENCES source_papers(id) ON DELETE RESTRICT,
    source_pdf_sha256 TEXT NOT NULL CHECK (length(source_pdf_sha256) = 64),
    render_manifest_sha256 TEXT NOT NULL CHECK (length(render_manifest_sha256) = 64),
    render_manifest_byte_size INTEGER NOT NULL CHECK (render_manifest_byte_size > 0),
    regions_manifest_sha256 TEXT NOT NULL CHECK (length(regions_manifest_sha256) = 64),
    regions_manifest_byte_size INTEGER NOT NULL CHECK (regions_manifest_byte_size > 0),
    legacy_crop_manifest_sha256 TEXT NOT NULL CHECK (length(legacy_crop_manifest_sha256) = 64),
    legacy_crop_manifest_byte_size INTEGER NOT NULL CHECK (legacy_crop_manifest_byte_size > 0),
    question_nos_json TEXT NOT NULL CHECK (json_valid(question_nos_json)),
    prior_job_status TEXT NOT NULL CHECK (prior_job_status IN ('failed','needs_review')),
    prior_split_status TEXT CHECK (
        prior_split_status IN ('pending','processing','completed','failed')
        OR prior_split_status IS NULL
    ),
    preserved_codex_run_id TEXT,
    new_crop_manifest_sha256 TEXT NOT NULL CHECK (length(new_crop_manifest_sha256) = 64),
    new_crop_generation_id TEXT NOT NULL CHECK (length(new_crop_generation_id) = 32),
    new_crop_manifest_signature TEXT NOT NULL CHECK (length(new_crop_manifest_signature) = 64),
    formal_question_count INTEGER NOT NULL CHECK (formal_question_count >= 0),
    formal_batch_sha256 TEXT NOT NULL CHECK (length(formal_batch_sha256) = 64),
    candidate_sha256 TEXT CHECK (candidate_sha256 IS NULL OR length(candidate_sha256) = 64),
    candidate_byte_size INTEGER CHECK (candidate_byte_size IS NULL OR candidate_byte_size > 0),
    draft_batch_sha256 TEXT NOT NULL CHECK (length(draft_batch_sha256) = 64),
    migration_evidence_kind TEXT NOT NULL CHECK (
        migration_evidence_kind = 'system_migration_placeholder'
    ),
    migration_evidence_json TEXT NOT NULL CHECK (json_valid(migration_evidence_json)),
    recovered_at TEXT NOT NULL,
    CHECK ((candidate_sha256 IS NULL) = (candidate_byte_size IS NULL))
);

CREATE TRIGGER IF NOT EXISTS historical_v1_crop_recoveries_immutable
BEFORE UPDATE ON historical_v1_crop_recoveries
BEGIN
    SELECT RAISE(ABORT, 'historical v1 crop recovery is immutable');
END;

CREATE TRIGGER IF NOT EXISTS historical_v1_crop_recoveries_delete_immutable
BEFORE DELETE ON historical_v1_crop_recoveries
BEGIN
    SELECT RAISE(ABORT, 'historical v1 crop recovery is immutable');
END;

CREATE TABLE IF NOT EXISTS historical_v1_pipeline_resumptions (
    import_job_id INTEGER PRIMARY KEY
        REFERENCES historical_v1_crop_recoveries(import_job_id) ON DELETE RESTRICT,
    source_paper_id INTEGER NOT NULL REFERENCES source_papers(id) ON DELETE RESTRICT,
    source_pdf_sha256 TEXT NOT NULL CHECK (length(source_pdf_sha256) = 64),
    formal_question_count INTEGER NOT NULL CHECK (formal_question_count >= 0),
    formal_batch_sha256 TEXT NOT NULL CHECK (length(formal_batch_sha256) = 64),
    crop_question_count INTEGER NOT NULL CHECK (crop_question_count > 0),
    crop_manifest_sha256 TEXT NOT NULL CHECK (length(crop_manifest_sha256) = 64),
    crop_generation_id TEXT NOT NULL CHECK (length(crop_generation_id) = 32),
    crop_manifest_signature TEXT NOT NULL CHECK (length(crop_manifest_signature) = 64),
    reviewer_run_id TEXT NOT NULL CHECK (length(trim(reviewer_run_id)) BETWEEN 1 AND 200),
    review_request_sha256 TEXT NOT NULL CHECK (length(review_request_sha256) = 64),
    review_evidence_signature TEXT NOT NULL CHECK (length(review_evidence_signature) = 64),
    reviewed_at TEXT NOT NULL,
    resumed_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS historical_v1_pipeline_resumptions_immutable
BEFORE UPDATE ON historical_v1_pipeline_resumptions
BEGIN
    SELECT RAISE(ABORT, 'historical v1 pipeline resumption is immutable');
END;

CREATE TRIGGER IF NOT EXISTS historical_v1_pipeline_resumptions_delete_immutable
BEFORE DELETE ON historical_v1_pipeline_resumptions
BEGIN
    SELECT RAISE(ABORT, 'historical v1 pipeline resumption is immutable');
END;

-- A historical recovery is the only authority for this deliberately narrow
-- lane.  It records only the mechanically derived unadmitted subset; it must
-- never be confused with the ordinary whole-batch classification run.
CREATE TABLE IF NOT EXISTS historical_residual_classification_runs (
    import_job_id INTEGER PRIMARY KEY
        REFERENCES historical_v1_crop_recoveries(import_job_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('processing','completed','failed')),
    stage TEXT NOT NULL CHECK (
        stage IN ('waiting','level2','proposal','verifier','adjudicator','completed')
    ),
    question_count INTEGER NOT NULL CHECK (question_count > 0),
    full_question_nos_json TEXT NOT NULL CHECK (json_valid(full_question_nos_json)),
    existing_question_nos_json TEXT NOT NULL CHECK (json_valid(existing_question_nos_json)),
    residual_question_nos_json TEXT NOT NULL CHECK (json_valid(residual_question_nos_json)),
    formal_question_count INTEGER NOT NULL CHECK (formal_question_count >= 0),
    formal_batch_sha256 TEXT NOT NULL CHECK (length(formal_batch_sha256)=64),
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256)=64),
    audit_sha256 TEXT NOT NULL CHECK (length(audit_sha256)=64),
    crop_manifest_sha256 TEXT NOT NULL CHECK (length(crop_manifest_sha256)=64),
    crop_generation_id TEXT NOT NULL CHECK (length(crop_generation_id)=32),
    crop_manifest_signature TEXT NOT NULL CHECK (length(crop_manifest_signature)=64),
    audit_completed_at TEXT NOT NULL,
    draft_bindings_sha256 TEXT NOT NULL CHECK (length(draft_bindings_sha256)=64),
    taxonomy_sha256 TEXT NOT NULL CHECK (length(taxonomy_sha256)=64),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256)=64),
    evidence_json TEXT CHECK (evidence_json IS NULL OR json_valid(evidence_json)),
    evidence_sha256 TEXT CHECK (evidence_sha256 IS NULL OR length(evidence_sha256)=64),
    claim_token TEXT CHECK (claim_token IS NULL OR length(claim_token)=64),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message)<=100),
    started_at TEXT NOT NULL,
    heartbeat_at TEXT,
    lease_expires_at TEXT,
    updated_at TEXT,
    completed_at TEXT,
    applied_at TEXT,
    CHECK (status!='processing' OR (
        claim_token IS NOT NULL AND heartbeat_at IS NOT NULL
        AND lease_expires_at IS NOT NULL AND updated_at IS NOT NULL
    )),
    CHECK (status='processing' OR lease_expires_at IS NULL),
    CHECK (status!='completed' OR (
        stage='completed' AND evidence_json IS NOT NULL AND evidence_sha256 IS NOT NULL
        AND completed_at IS NOT NULL AND claim_token IS NULL AND error_message IS NULL
    ))
);

CREATE TRIGGER IF NOT EXISTS historical_residual_classification_completed_immutable
BEFORE UPDATE ON historical_residual_classification_runs
WHEN OLD.status='completed'
BEGIN
    SELECT RAISE(ABORT, 'completed historical residual classification is immutable');
END;

CREATE TRIGGER IF NOT EXISTS historical_residual_classification_delete_immutable
BEFORE DELETE ON historical_residual_classification_runs
WHEN OLD.status='completed'
BEGIN
    SELECT RAISE(ABORT, 'completed historical residual classification is immutable');
END;

CREATE TABLE IF NOT EXISTS historical_residual_no_answer_decisions (
    import_job_id INTEGER PRIMARY KEY
        REFERENCES historical_v1_crop_recoveries(import_job_id) ON DELETE RESTRICT,
    confirmation_token TEXT NOT NULL UNIQUE CHECK (length(confirmation_token)=64),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256)=64),
    decided_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS historical_residual_no_answer_decisions_immutable
BEFORE UPDATE ON historical_residual_no_answer_decisions
BEGIN
    SELECT RAISE(ABORT, 'historical residual no-answer decision is immutable');
END;

CREATE TRIGGER IF NOT EXISTS historical_residual_no_answer_decisions_delete_immutable
BEFORE DELETE ON historical_residual_no_answer_decisions
BEGIN
    SELECT RAISE(ABORT, 'historical residual no-answer decision is immutable');
END;

CREATE TABLE IF NOT EXISTS historical_residual_admissions (
    import_job_id INTEGER PRIMARY KEY
        REFERENCES historical_v1_crop_recoveries(import_job_id) ON DELETE RESTRICT,
    confirmation_token TEXT NOT NULL UNIQUE CHECK (length(confirmation_token)=64),
    assessment_sha256 TEXT NOT NULL CHECK (length(assessment_sha256)=64),
    full_question_nos_json TEXT NOT NULL CHECK (json_valid(full_question_nos_json)),
    existing_question_nos_json TEXT NOT NULL CHECK (json_valid(existing_question_nos_json)),
    residual_question_nos_json TEXT NOT NULL CHECK (json_valid(residual_question_nos_json)),
    baseline_formal_question_count INTEGER NOT NULL CHECK (baseline_formal_question_count>=0),
    baseline_formal_batch_sha256 TEXT NOT NULL CHECK (length(baseline_formal_batch_sha256)=64),
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256)=64),
    audit_sha256 TEXT NOT NULL CHECK (length(audit_sha256)=64),
    crop_manifest_sha256 TEXT NOT NULL CHECK (length(crop_manifest_sha256)=64),
    classification_evidence_sha256 TEXT NOT NULL CHECK (length(classification_evidence_sha256)=64),
    backup_path TEXT NOT NULL CHECK (length(trim(backup_path))>0),
    backup_sha256 TEXT NOT NULL CHECK (length(backup_sha256)=64),
    inserted_count INTEGER NOT NULL CHECK (inserted_count>0),
    final_formal_question_count INTEGER NOT NULL CHECK (final_formal_question_count>0),
    final_formal_batch_sha256 TEXT NOT NULL CHECK (length(final_formal_batch_sha256)=64),
    completed_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS historical_residual_admissions_immutable
BEFORE UPDATE ON historical_residual_admissions
BEGIN
    SELECT RAISE(ABORT, 'historical residual admission is immutable');
END;

CREATE TRIGGER IF NOT EXISTS historical_residual_admissions_delete_immutable
BEFORE DELETE ON historical_residual_admissions
BEGIN
    SELECT RAISE(ABORT, 'historical residual admission is immutable');
END;

CREATE TABLE IF NOT EXISTS import_crop_security_reviews (
    import_job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE RESTRICT,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('mask', 'figure')),
    question_no INTEGER NOT NULL CHECK (question_no > 0),
    generation_id TEXT NOT NULL CHECK (length(generation_id) = 32),
    subject_digest TEXT NOT NULL CHECK (length(subject_digest) = 64),
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    artifact_sha256 TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
    preview_sha256 TEXT NOT NULL CHECK (length(preview_sha256) = 64),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    evidence_signature TEXT NOT NULL CHECK (length(evidence_signature) = 64),
    reviewer TEXT NOT NULL CHECK (length(trim(reviewer)) BETWEEN 1 AND 100),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reason TEXT CHECK (reason IS NULL OR reason IN ('qr_code', 'promotion_overlay')),
    bbox_json TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (import_job_id, evidence_kind, question_no, generation_id, subject_digest)
);
CREATE INDEX IF NOT EXISTS idx_crop_security_reviews_job
ON import_crop_security_reviews(import_job_id, evidence_kind, generation_id);

CREATE TABLE IF NOT EXISTS import_frozen_crop_reviews (
    import_job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE RESTRICT,
    question_no INTEGER NOT NULL CHECK (question_no > 0),
    crop_generation_id TEXT NOT NULL CHECK (length(crop_generation_id) = 32),
    crop_sha256 TEXT NOT NULL CHECK (length(crop_sha256) = 64),
    manifest_entry_sha256 TEXT NOT NULL CHECK (length(manifest_entry_sha256) = 64),
    source_manifest_sha256 TEXT NOT NULL CHECK (length(source_manifest_sha256) = 64),
    source_manifest_signature TEXT NOT NULL CHECK (length(source_manifest_signature) = 64),
    review_evidence_sha256 TEXT NOT NULL CHECK (length(review_evidence_sha256) = 64),
    review_evidence_signature TEXT NOT NULL CHECK (length(review_evidence_signature) = 64),
    evidence_relative_path TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    PRIMARY KEY (import_job_id, question_no, crop_generation_id)
);
CREATE INDEX IF NOT EXISTS idx_frozen_crop_reviews_job
ON import_frozen_crop_reviews(import_job_id, question_no);

CREATE TABLE IF NOT EXISTS import_candidate_extraction_runs (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'completed', 'failed')
    ),
    question_count INTEGER CHECK (question_count IS NULL OR question_count > 0),
    processed_questions INTEGER NOT NULL DEFAULT 0 CHECK (processed_questions >= 0),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message) <= 300),
    codex_run_id TEXT CHECK (
        codex_run_id IS NULL OR length(codex_run_id) BETWEEN 1 AND 200
    ),
    input_crop_generation_id TEXT CHECK (
        input_crop_generation_id IS NULL OR length(input_crop_generation_id) = 32
    ),
    input_manifest_sha256 TEXT CHECK (
        input_manifest_sha256 IS NULL OR length(input_manifest_sha256) = 64
    ),
    input_manifest_signature TEXT CHECK (
        input_manifest_signature IS NULL OR length(input_manifest_signature) = 64
    ),
    output_sha256 TEXT CHECK (
        output_sha256 IS NULL OR length(output_sha256) = 64
    ),
    output_byte_size INTEGER CHECK (
        output_byte_size IS NULL OR output_byte_size > 0
    ),
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (question_count IS NULL OR processed_questions <= question_count),
    CHECK (
        status != 'completed' OR (
            question_count IS NOT NULL AND processed_questions = question_count AND
            codex_run_id IS NOT NULL AND input_crop_generation_id IS NOT NULL AND
            input_manifest_sha256 IS NOT NULL AND input_manifest_signature IS NOT NULL AND
            output_sha256 IS NOT NULL AND output_byte_size IS NOT NULL AND
            completed_at IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS import_candidate_audit_runs (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'completed', 'failed')
    ),
    question_count INTEGER CHECK (question_count IS NULL OR question_count > 0),
    processed_questions INTEGER NOT NULL DEFAULT 0 CHECK (processed_questions >= 0),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message) <= 300),
    codex_run_id TEXT CHECK (
        codex_run_id IS NULL OR length(codex_run_id) BETWEEN 1 AND 200
    ),
    input_candidate_sha256 TEXT CHECK (
        input_candidate_sha256 IS NULL OR length(input_candidate_sha256) = 64
    ),
    input_candidate_byte_size INTEGER CHECK (
        input_candidate_byte_size IS NULL OR input_candidate_byte_size > 0
    ),
    input_crop_generation_id TEXT CHECK (
        input_crop_generation_id IS NULL OR length(input_crop_generation_id) = 32
    ),
    input_manifest_sha256 TEXT CHECK (
        input_manifest_sha256 IS NULL OR length(input_manifest_sha256) = 64
    ),
    input_manifest_signature TEXT CHECK (
        input_manifest_signature IS NULL OR length(input_manifest_signature) = 64
    ),
    output_sha256 TEXT CHECK (
        output_sha256 IS NULL OR length(output_sha256) = 64
    ),
    output_byte_size INTEGER CHECK (
        output_byte_size IS NULL OR output_byte_size > 0
    ),
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (question_count IS NULL OR processed_questions <= question_count),
    CHECK (
        status != 'completed' OR (
            question_count IS NOT NULL AND processed_questions = question_count AND
            codex_run_id IS NOT NULL AND input_candidate_sha256 IS NOT NULL AND
            input_candidate_byte_size IS NOT NULL AND
            input_crop_generation_id IS NOT NULL AND
            input_manifest_sha256 IS NOT NULL AND
            input_manifest_signature IS NOT NULL AND output_sha256 IS NOT NULL AND
            output_byte_size IS NOT NULL AND completed_at IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS tag_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL CHECK (category IN ('task', 'method', 'error', 'scenario')),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    UNIQUE (category, code),
    UNIQUE (category, name)
);

CREATE TABLE IF NOT EXISTS knowledge_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 3),
    parent_id INTEGER REFERENCES knowledge_points(id) ON DELETE RESTRICT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    system_version TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 1 CHECK (sort_order > 0),
    CHECK ((level = 1 AND parent_id IS NULL) OR level > 1)
);

CREATE TABLE IF NOT EXISTS duplicate_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_code TEXT NOT NULL UNIQUE,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_code TEXT NOT NULL UNIQUE,
    stem_markdown TEXT NOT NULL CHECK (length(trim(stem_markdown)) > 0),
    answer_markdown TEXT NOT NULL DEFAULT '',
    answer_status TEXT NOT NULL DEFAULT 'provided' CHECK (answer_status IN ('provided', 'missing')),
    analysis_markdown TEXT,
    region_code TEXT NOT NULL REFERENCES regions(code) ON DELETE RESTRICT,
    exam_year INTEGER CHECK (exam_year IS NULL OR exam_year BETWEEN 1900 AND 9999),
    exam_type_code TEXT NOT NULL REFERENCES exam_types(code) ON DELETE RESTRICT,
    paper_name TEXT,
    source_question_no TEXT,
    source_page TEXT,
    score REAL CHECK (score IS NULL OR score >= 0),
    source_file_path TEXT CHECK (source_file_path IS NULL OR substr(source_file_path, 1, 1) <> '/'),
    question_type_code TEXT NOT NULL REFERENCES question_types(code) ON DELETE RESTRICT,
    difficulty_level INTEGER REFERENCES difficulty_levels(level) ON DELETE RESTRICT,
    difficulty_basis TEXT,
    primary_knowledge_point_id INTEGER NOT NULL REFERENCES knowledge_points(id) ON DELETE RESTRICT,
    ocr_review_status TEXT NOT NULL DEFAULT 'pending' REFERENCES review_statuses(code) ON DELETE RESTRICT,
    formula_review_status TEXT NOT NULL DEFAULT 'pending' REFERENCES review_statuses(code) ON DELETE RESTRICT,
    figure_review_status TEXT NOT NULL DEFAULT 'pending' REFERENCES review_statuses(code) ON DELETE RESTRICT,
    answer_review_status TEXT NOT NULL DEFAULT 'pending' REFERENCES review_statuses(code) ON DELETE RESTRICT,
    analysis_review_status TEXT NOT NULL DEFAULT 'not_applicable' REFERENCES review_statuses(code) ON DELETE RESTRICT,
    tag_review_status TEXT NOT NULL DEFAULT 'pending' REFERENCES review_statuses(code) ON DELETE RESTRICT,
    usability_status TEXT NOT NULL DEFAULT 'draft' REFERENCES usability_statuses(code) ON DELETE RESTRICT,
    content_hash TEXT NOT NULL,
    duplicate_group_id INTEGER REFERENCES duplicate_groups(id) ON DELETE SET NULL,
    deleted_at TEXT,
    deletion_reason TEXT CHECK (deletion_reason IS NULL OR deletion_reason IN ('unreadable', 'incomplete', 'duplicate', 'unneeded', 'other')),
    deletion_note TEXT CHECK (deletion_note IS NULL OR length(deletion_note) <= 500),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (analysis_markdown IS NOT NULL OR analysis_review_status = 'not_applicable'),
    CHECK (analysis_markdown IS NULL OR analysis_review_status <> 'not_applicable'),
    CHECK ((answer_status = 'missing' AND answer_markdown = '') OR
           (answer_status = 'provided' AND length(trim(answer_markdown)) > 0)),
    CHECK (
        usability_status <> 'usable' OR (
            ocr_review_status IN ('passed', 'not_applicable') AND
            formula_review_status IN ('passed', 'not_applicable') AND
            figure_review_status IN ('passed', 'not_applicable') AND
            answer_review_status = 'passed' AND
            tag_review_status = 'passed' AND
            (analysis_markdown IS NULL OR analysis_review_status = 'passed')
        )
    )
);

CREATE TABLE IF NOT EXISTS question_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    option_code TEXT NOT NULL,
    content_markdown TEXT NOT NULL,
    display_order INTEGER NOT NULL CHECK (display_order > 0),
    UNIQUE (question_id, option_code),
    UNIQUE (question_id, display_order)
);

CREATE TABLE IF NOT EXISTS subquestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    display_order INTEGER NOT NULL CHECK (display_order > 0),
    stem_markdown TEXT NOT NULL,
    answer_markdown TEXT NOT NULL DEFAULT '',
    answer_status TEXT NOT NULL DEFAULT 'missing' CHECK (answer_status IN ('provided', 'missing')),
    analysis_markdown TEXT,
    score REAL CHECK (score IS NULL OR score >= 0),
    UNIQUE (question_id, display_order),
    CHECK ((answer_status = 'missing' AND answer_markdown = '') OR
           (answer_status = 'provided' AND length(trim(answer_markdown)) > 0))
);

-- AI reference answers are deliberately separate from the source-paper answer
-- trust domain.  Displayable content comes only from final-review; generator and
-- independent files remain hash/model evidence.  One conflict-checked evidence
-- set may be approved per question.
CREATE TABLE IF NOT EXISTS ai_reference_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL UNIQUE REFERENCES questions(id) ON DELETE CASCADE,
    question_content_hash TEXT NOT NULL CHECK (length(question_content_hash) = 64),
    answer_markdown TEXT NOT NULL CHECK (length(trim(answer_markdown)) BETWEEN 1 AND 50000),
    analysis_markdown TEXT NOT NULL CHECK (length(trim(analysis_markdown)) BETWEEN 1 AND 100000),
    generator_model TEXT NOT NULL CHECK (length(trim(generator_model)) BETWEEN 1 AND 200),
    independent_model TEXT NOT NULL CHECK (length(trim(independent_model)) BETWEEN 1 AND 200),
    final_review_model TEXT NOT NULL CHECK (length(trim(final_review_model)) BETWEEN 1 AND 200),
    review_decision TEXT NOT NULL CHECK (review_decision = 'passed'),
    review_notes TEXT NOT NULL DEFAULT '' CHECK (length(review_notes) <= 2000),
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
    generator_sha256 TEXT NOT NULL CHECK (length(generator_sha256) = 64 AND generator_sha256 NOT GLOB '*[^0-9a-f]*'),
    independent_sha256 TEXT NOT NULL CHECK (length(independent_sha256) = 64 AND independent_sha256 NOT GLOB '*[^0-9a-f]*'),
    final_review_sha256 TEXT NOT NULL CHECK (length(final_review_sha256) = 64 AND final_review_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_reference_subquestion_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ai_reference_answer_id INTEGER NOT NULL REFERENCES ai_reference_answers(id) ON DELETE CASCADE,
    subquestion_id INTEGER NOT NULL REFERENCES subquestions(id) ON DELETE CASCADE,
    display_order INTEGER NOT NULL CHECK (display_order > 0),
    answer_markdown TEXT NOT NULL CHECK (length(trim(answer_markdown)) BETWEEN 1 AND 50000),
    analysis_markdown TEXT NOT NULL CHECK (length(trim(analysis_markdown)) BETWEEN 1 AND 100000),
    UNIQUE (ai_reference_answer_id, display_order),
    UNIQUE (ai_reference_answer_id, subquestion_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_reference_subquestion
ON ai_reference_subquestion_answers(subquestion_id);

CREATE TRIGGER IF NOT EXISTS ai_reference_subquestion_validate_insert
BEFORE INSERT ON ai_reference_subquestion_answers
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
            FROM ai_reference_answers AS ai
            JOIN subquestions AS s ON s.id = NEW.subquestion_id
            WHERE ai.id = NEW.ai_reference_answer_id
              AND ai.question_id = s.question_id
        ) THEN RAISE(ABORT, 'AI reference subquestion question mismatch')
        WHEN NOT EXISTS (
            SELECT 1 FROM subquestions AS s
            WHERE s.id = NEW.subquestion_id
              AND s.display_order = NEW.display_order
        ) THEN RAISE(ABORT, 'AI reference subquestion display_order mismatch')
    END;
END;

CREATE TRIGGER IF NOT EXISTS ai_reference_subquestion_validate_update
BEFORE UPDATE OF ai_reference_answer_id, subquestion_id, display_order
ON ai_reference_subquestion_answers
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
            FROM ai_reference_answers AS ai
            JOIN subquestions AS s ON s.id = NEW.subquestion_id
            WHERE ai.id = NEW.ai_reference_answer_id
              AND ai.question_id = s.question_id
        ) THEN RAISE(ABORT, 'AI reference subquestion question mismatch')
        WHEN NOT EXISTS (
            SELECT 1 FROM subquestions AS s
            WHERE s.id = NEW.subquestion_id
              AND s.display_order = NEW.display_order
        ) THEN RAISE(ABORT, 'AI reference subquestion display_order mismatch')
    END;
END;

CREATE TABLE IF NOT EXISTS question_formulas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    subquestion_id INTEGER REFERENCES subquestions(id) ON DELETE CASCADE,
    formula_latex TEXT NOT NULL,
    location TEXT NOT NULL,
    display_order INTEGER NOT NULL CHECK (display_order > 0),
    UNIQUE (question_id, location, display_order)
);

CREATE TABLE IF NOT EXISTS question_figures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    subquestion_id INTEGER REFERENCES subquestions(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL CHECK (substr(relative_path, 1, 1) <> '/'),
    purpose TEXT NOT NULL,
    display_order INTEGER NOT NULL CHECK (display_order > 0),
    alt_text TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN ('original', 'cropped', 'redrawn', 'generated')),
    image_hash TEXT NOT NULL,
    UNIQUE (question_id, display_order),
    UNIQUE (relative_path)
);

CREATE TABLE IF NOT EXISTS question_sources (
    question_id INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
    source_paper_id INTEGER NOT NULL REFERENCES source_papers(id) ON DELETE RESTRICT,
    import_job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE RESTRICT,
    source_question_no TEXT NOT NULL CHECK (length(trim(source_question_no)) > 0),
    source_pages_json TEXT NOT NULL,
    UNIQUE (import_job_id, source_question_no)
);

CREATE TABLE IF NOT EXISTS question_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    import_job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE RESTRICT,
    asset_kind TEXT NOT NULL CHECK (asset_kind IN ('complete_question', 'question_figure')),
    relative_path TEXT NOT NULL CHECK (
        substr(relative_path, 1, 1) <> '/' AND relative_path NOT LIKE '%..%' AND relative_path NOT LIKE '%\%'
    ),
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    review_status TEXT NOT NULL CHECK (review_status = 'ai_review_passed'),
    display_order INTEGER NOT NULL DEFAULT 1 CHECK (display_order > 0),
    UNIQUE (question_id, asset_kind, display_order),
    UNIQUE (import_job_id, relative_path)
);

CREATE TABLE IF NOT EXISTS question_related_knowledge_points (
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    knowledge_point_id INTEGER NOT NULL REFERENCES knowledge_points(id) ON DELETE RESTRICT,
    PRIMARY KEY (question_id, knowledge_point_id)
);

CREATE TABLE IF NOT EXISTS question_tags (
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tag_definitions(id) ON DELETE RESTRICT,
    note TEXT,
    PRIMARY KEY (question_id, tag_id)
);

CREATE TABLE IF NOT EXISTS question_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    review_item TEXT NOT NULL CHECK (review_item IN ('ocr', 'formula', 'figure', 'answer', 'analysis', 'tag', 'usability')),
    previous_status TEXT REFERENCES review_statuses(code) ON DELETE RESTRICT,
    new_status TEXT NOT NULL REFERENCES review_statuses(code) ON DELETE RESTRICT,
    reviewer TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
    reviewed_at TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS question_usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    used_at TEXT NOT NULL,
    context_type TEXT NOT NULL,
    context_name TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS question_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    version_status TEXT NOT NULL REFERENCES version_statuses(code) ON DELETE RESTRICT,
    previous_version_id INTEGER REFERENCES question_versions(id) ON DELETE RESTRICT,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (question_id, version_no)
);

CREATE TABLE IF NOT EXISTS baskets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_key TEXT NOT NULL UNIQUE CHECK (length(trim(basket_key)) > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS basket_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_id INTEGER NOT NULL REFERENCES baskets(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position > 0),
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (basket_id, question_id),
    UNIQUE (basket_id, position)
);

CREATE TABLE IF NOT EXISTS basket_exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_id INTEGER NOT NULL REFERENCES baskets(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    question_count INTEGER NOT NULL CHECK (question_count > 0),
    options_json TEXT NOT NULL,
    output_path TEXT NOT NULL UNIQUE CHECK (
        output_path LIKE 'exports/%/练习.md' AND substr(output_path, 1, 1) <> '/'
        AND output_path NOT LIKE '%..%' AND output_path NOT LIKE '%\%'
    ),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')
);

CREATE INDEX IF NOT EXISTS idx_basket_items_order ON basket_items(basket_id, position);

CREATE TABLE IF NOT EXISTS candidate_review_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
    source_question_no TEXT NOT NULL CHECK (length(trim(source_question_no)) > 0),
    source_candidate_sha256 TEXT NOT NULL CHECK (length(source_candidate_sha256) = 64),
    source_snapshot_json TEXT NOT NULL,
    edited_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','draft','approved','needs_fix','needs_recrop')),
    review_notes TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    approval_source TEXT CHECK (approval_source IN ('human', 'ai_second_pass') OR approval_source IS NULL),
    approval_evidence_json TEXT CHECK (approval_evidence_json IS NULL OR json_valid(approval_evidence_json)),
    deleted_at TEXT,
    deletion_reason TEXT CHECK (deletion_reason IS NULL OR deletion_reason IN ('unreadable', 'incomplete', 'duplicate', 'unneeded', 'other')),
    deletion_note TEXT CHECK (deletion_note IS NULL OR length(deletion_note) <= 500),
    UNIQUE (import_job_id, source_question_no)
);
CREATE INDEX IF NOT EXISTS idx_candidate_review_job_status ON candidate_review_drafts(import_job_id, status);

CREATE TABLE IF NOT EXISTS corrected_draft_reaudits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE RESTRICT,
    source_question_no TEXT NOT NULL CHECK (length(trim(source_question_no)) BETWEEN 1 AND 3),
    reviewed_draft_version INTEGER NOT NULL CHECK (reviewed_draft_version > 0),
    edited_sha256 TEXT NOT NULL CHECK (length(edited_sha256) = 64 AND edited_sha256 NOT GLOB '*[^0-9a-f]*'),
    status TEXT NOT NULL CHECK (status IN ('processing','completed','failed')),
    source_candidate_sha256 TEXT NOT NULL CHECK (length(source_candidate_sha256) = 64),
    source_snapshot_sha256 TEXT NOT NULL CHECK (length(source_snapshot_sha256) = 64),
    batch_audit_output_sha256 TEXT NOT NULL CHECK (length(batch_audit_output_sha256) = 64),
    crop_generation_id TEXT NOT NULL CHECK (length(crop_generation_id) = 32),
    crop_manifest_sha256 TEXT NOT NULL CHECK (length(crop_manifest_sha256) = 64),
    crop_manifest_signature TEXT NOT NULL CHECK (length(crop_manifest_signature) = 64),
    crop_relative_path TEXT NOT NULL CHECK (
        crop_relative_path GLOB 'question_crops/Q[0-9][0-9][0-9].png'
        AND crop_relative_path NOT LIKE '%..%' AND crop_relative_path NOT LIKE '%\%'
    ),
    crop_sha256 TEXT NOT NULL CHECK (length(crop_sha256) = 64),
    crop_byte_size INTEGER NOT NULL CHECK (crop_byte_size > 0),
    fresh_model_run_id TEXT CHECK (fresh_model_run_id IS NULL OR length(fresh_model_run_id) BETWEEN 1 AND 200),
    audit_output_sha256 TEXT CHECK (audit_output_sha256 IS NULL OR length(audit_output_sha256) = 64),
    audit_output_byte_size INTEGER CHECK (audit_output_byte_size IS NULL OR audit_output_byte_size > 0),
    decision TEXT CHECK (decision IS NULL OR decision IN ('passed','not_passed','error')),
    confidence TEXT CHECK (confidence IS NULL OR confidence IN ('low','medium','high')),
    reviewed_at TEXT,
    approved_draft_version INTEGER CHECK (approved_draft_version IS NULL OR approved_draft_version > reviewed_draft_version),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message) <= 300),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (import_job_id, source_question_no, reviewed_draft_version, edited_sha256),
    UNIQUE (fresh_model_run_id),
    CHECK (
        status != 'completed' OR (
            fresh_model_run_id IS NOT NULL AND audit_output_sha256 IS NOT NULL
            AND audit_output_byte_size IS NOT NULL AND decision IS NOT NULL
            AND confidence IS NOT NULL AND reviewed_at IS NOT NULL
            AND error_message IS NULL
            AND ((decision = 'passed' AND approved_draft_version IS NOT NULL)
                 OR (decision != 'passed' AND approved_draft_version IS NULL))
        )
    ),
    CHECK (status != 'failed' OR (decision = 'error' AND error_message IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_corrected_reaudit_lookup
ON corrected_draft_reaudits(import_job_id, source_question_no, status, reviewed_draft_version);

CREATE TRIGGER IF NOT EXISTS corrected_draft_reaudits_completed_immutable
BEFORE UPDATE ON corrected_draft_reaudits
WHEN OLD.status = 'completed'
BEGIN
    SELECT RAISE(ABORT, 'completed corrected draft re-audit is immutable');
END;

CREATE TABLE IF NOT EXISTS candidate_knowledge_classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE RESTRICT,
    source_question_no TEXT NOT NULL CHECK (length(trim(source_question_no)) BETWEEN 1 AND 3),
    approved_draft_version INTEGER NOT NULL CHECK (approved_draft_version > 0),
    edited_sha256 TEXT NOT NULL CHECK (length(edited_sha256) = 64 AND edited_sha256 NOT GLOB '*[^0-9a-f]*'),
    classification_scope_sha256 TEXT CHECK (
        classification_scope_sha256 IS NULL OR (
            length(classification_scope_sha256)=64
            AND classification_scope_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    primary_knowledge_point_code TEXT NOT NULL REFERENCES knowledge_points(code) ON DELETE RESTRICT,
    related_knowledge_point_codes_json TEXT NOT NULL,
    classifier TEXT NOT NULL CHECK (length(trim(classifier)) BETWEEN 1 AND 100),
    reviewer TEXT NOT NULL CHECK (length(trim(reviewer)) BETWEEN 1 AND 100),
    approval_source TEXT CHECK (
        approval_source IN (
            'codex_double_pass','codex_adjudicated','local_double_pass','human'
        ) OR approval_source IS NULL
    ),
    classifier_run_id TEXT NOT NULL CHECK (length(trim(classifier_run_id)) BETWEEN 1 AND 200),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
    reason TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 200),
    created_at TEXT NOT NULL,
    UNIQUE (import_job_id, source_question_no, approved_draft_version, edited_sha256),
    UNIQUE (classifier_run_id, source_question_no)
);
CREATE INDEX IF NOT EXISTS idx_candidate_knowledge_lookup
ON candidate_knowledge_classifications(import_job_id, source_question_no, approved_draft_version);

CREATE TRIGGER IF NOT EXISTS candidate_knowledge_classifications_immutable
BEFORE UPDATE ON candidate_knowledge_classifications
WHEN NOT EXISTS (
    SELECT 1 FROM official_answer_overlay_authorizations a
    WHERE a.import_job_id=OLD.import_job_id
)
BEGIN
    SELECT RAISE(ABORT, 'completed knowledge classification is immutable');
END;

CREATE TRIGGER IF NOT EXISTS candidate_knowledge_classifications_delete_immutable
BEFORE DELETE ON candidate_knowledge_classifications
WHEN NOT EXISTS (
    SELECT 1 FROM knowledge_classification_archival_authorizations a
    WHERE a.import_job_id=OLD.import_job_id
)
BEGIN
    SELECT RAISE(ABORT, 'completed knowledge classification is immutable');
END;

CREATE TABLE IF NOT EXISTS import_knowledge_classification_runs (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending','processing','completed','failed')
    ),
    stage TEXT NOT NULL DEFAULT 'waiting' CHECK (
        stage IN (
            'waiting','level2','proposal','verifier','adjudicator',
            'publishing','review_ready'
        )
    ),
    question_count INTEGER CHECK (question_count IS NULL OR question_count > 0),
    processed_questions INTEGER NOT NULL DEFAULT 0 CHECK (processed_questions >= 0),
    model TEXT NOT NULL DEFAULT 'codex-cli' CHECK (model='codex-cli'),
    input_digest TEXT CHECK (input_digest IS NULL OR (
        length(input_digest)=64 AND input_digest NOT GLOB '*[^0-9a-f]*'
    )),
    taxonomy_digest TEXT CHECK (taxonomy_digest IS NULL OR (
        length(taxonomy_digest)=64 AND taxonomy_digest NOT GLOB '*[^0-9a-f]*'
    )),
    output_sha256 TEXT CHECK (output_sha256 IS NULL OR (
        length(output_sha256)=64 AND output_sha256 NOT GLOB '*[^0-9a-f]*'
    )),
    output_byte_size INTEGER CHECK (output_byte_size IS NULL OR output_byte_size > 0),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message) <= 100),
    claim_token TEXT CHECK (claim_token IS NULL OR length(claim_token)=64),
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_at TEXT,
    replacement_active INTEGER NOT NULL DEFAULT 0 CHECK (
        replacement_active IN (0, 1)
    ),
    replacement_attempted_at TEXT,
    replacement_completed_at TEXT,
    replacement_result TEXT CHECK (
        replacement_result IN ('processing','completed','failed')
        OR replacement_result IS NULL
    ),
    CHECK (question_count IS NULL OR processed_questions <= question_count),
    CHECK (status != 'completed' OR (
        question_count IS NOT NULL AND processed_questions=question_count
        AND input_digest IS NOT NULL AND taxonomy_digest IS NOT NULL
        AND output_sha256 IS NOT NULL AND output_byte_size IS NOT NULL
        AND completed_at IS NOT NULL AND error_message IS NULL
    ))
);
CREATE INDEX IF NOT EXISTS idx_knowledge_classification_run_status
ON import_knowledge_classification_runs(status, updated_at);

CREATE TABLE IF NOT EXISTS candidate_knowledge_classification_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE RESTRICT,
    source_question_no TEXT NOT NULL CHECK (length(trim(source_question_no)) BETWEEN 1 AND 3),
    approved_draft_version INTEGER NOT NULL CHECK (approved_draft_version > 0),
    edited_sha256 TEXT NOT NULL CHECK (length(edited_sha256)=64),
    classification_scope_sha256 TEXT CHECK (
        classification_scope_sha256 IS NULL OR (
            length(classification_scope_sha256)=64
            AND classification_scope_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    proposal_primary_code TEXT NOT NULL REFERENCES knowledge_points(code) ON DELETE RESTRICT,
    proposal_related_codes_json TEXT NOT NULL CHECK (json_valid(proposal_related_codes_json)),
    proposal_confidence TEXT NOT NULL CHECK (proposal_confidence IN ('low','medium','high')),
    proposal_reason TEXT NOT NULL CHECK (length(proposal_reason) BETWEEN 1 AND 200),
    verifier_primary_code TEXT NOT NULL REFERENCES knowledge_points(code) ON DELETE RESTRICT,
    verifier_related_codes_json TEXT NOT NULL CHECK (json_valid(verifier_related_codes_json)),
    verifier_confidence TEXT NOT NULL CHECK (verifier_confidence IN ('low','medium','high')),
    verifier_reason TEXT NOT NULL CHECK (length(verifier_reason) BETWEEN 1 AND 200),
    adjudicator_primary_code TEXT REFERENCES knowledge_points(code) ON DELETE RESTRICT,
    adjudicator_related_codes_json TEXT CHECK (
        adjudicator_related_codes_json IS NULL OR json_valid(adjudicator_related_codes_json)
    ),
    adjudicator_confidence TEXT CHECK (
        adjudicator_confidence IN ('low','medium','high') OR adjudicator_confidence IS NULL
    ),
    adjudicator_reason TEXT CHECK (
        adjudicator_reason IS NULL OR length(adjudicator_reason) BETWEEN 1 AND 200
    ),
    final_primary_code TEXT NOT NULL REFERENCES knowledge_points(code) ON DELETE RESTRICT,
    final_related_codes_json TEXT NOT NULL CHECK (json_valid(final_related_codes_json)),
    final_reason TEXT CHECK (final_reason IS NULL OR length(final_reason) BETWEEN 1 AND 200),
    status TEXT NOT NULL CHECK (status IN ('pending','approved')),
    approval_source TEXT CHECK (
        approval_source IN (
            'codex_double_pass','codex_adjudicated','local_double_pass','human'
        ) OR approval_source IS NULL
    ),
    human_review_note TEXT NOT NULL DEFAULT '' CHECK (length(human_review_note) <= 200),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(import_job_id, source_question_no),
    CHECK ((status='pending' AND approval_source IS NULL) OR
           (status='approved' AND approval_source IS NOT NULL AND reviewed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_knowledge_classification_draft_review
ON candidate_knowledge_classification_drafts(import_job_id, status, source_question_no);

CREATE TABLE IF NOT EXISTS knowledge_classification_archival_authorizations (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    authorization_token TEXT NOT NULL UNIQUE CHECK (
        length(authorization_token)=64
        AND authorization_token NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_classification_replacement_snapshots (
    import_job_id INTEGER PRIMARY KEY
        REFERENCES import_knowledge_classification_runs(import_job_id) ON DELETE RESTRICT,
    claim_token TEXT NOT NULL UNIQUE CHECK (
        length(claim_token)=64 AND claim_token NOT GLOB '*[^0-9a-f]*'
    ),
    run_snapshot_json TEXT NOT NULL CHECK (json_valid(run_snapshot_json)),
    drafts_snapshot_json TEXT NOT NULL CHECK (json_valid(drafts_snapshot_json)),
    output_sha256 TEXT NOT NULL CHECK (
        length(output_sha256)=64 AND output_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    output_byte_size INTEGER NOT NULL CHECK (output_byte_size > 0),
    backup_name TEXT NOT NULL CHECK (
        length(backup_name) BETWEEN 1 AND 120
        AND backup_name NOT LIKE '%/%' AND backup_name NOT LIKE '%\%'
        AND backup_name NOT LIKE '%..%'
    ),
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS knowledge_classification_applied_run_immutable
BEFORE UPDATE ON import_knowledge_classification_runs
WHEN OLD.applied_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'applied knowledge classification run is immutable');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_classification_completed_run_delete_immutable
BEFORE DELETE ON import_knowledge_classification_runs
WHEN (OLD.status='completed' OR OLD.applied_at IS NOT NULL)
AND NOT EXISTS (
    SELECT 1 FROM knowledge_classification_archival_authorizations a
    WHERE a.import_job_id=OLD.import_job_id
)
BEGIN
    SELECT RAISE(ABORT, 'completed knowledge classification run is immutable');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_classification_completed_output_immutable
BEFORE UPDATE ON import_knowledge_classification_runs
WHEN OLD.status='completed' AND (
    NEW.status != OLD.status OR NEW.question_count != OLD.question_count
    OR NEW.model != OLD.model OR NEW.input_digest != OLD.input_digest
    OR NEW.taxonomy_digest != OLD.taxonomy_digest
    OR NEW.output_sha256 != OLD.output_sha256
    OR NEW.output_byte_size != OLD.output_byte_size
    OR NEW.completed_at != OLD.completed_at
) AND NOT (
    OLD.applied_at IS NULL
    AND NEW.status='processing'
    AND NEW.replacement_active=1
    AND NEW.question_count IS OLD.question_count
    AND NEW.model IS OLD.model
    AND NEW.input_digest IS OLD.input_digest
    AND NEW.taxonomy_digest IS OLD.taxonomy_digest
    AND NEW.output_sha256 IS OLD.output_sha256
    AND NEW.output_byte_size IS OLD.output_byte_size
    AND NEW.completed_at IS OLD.completed_at
)
BEGIN
    SELECT RAISE(ABORT, 'completed knowledge classification output is immutable');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_classification_applied_draft_immutable
BEFORE UPDATE ON candidate_knowledge_classification_drafts
WHEN EXISTS (
    SELECT 1 FROM import_knowledge_classification_runs r
    WHERE r.import_job_id=OLD.import_job_id AND r.applied_at IS NOT NULL
) AND NOT EXISTS (
    SELECT 1 FROM official_answer_overlay_authorizations a
    WHERE a.import_job_id=OLD.import_job_id
)
BEGIN
    SELECT RAISE(ABORT, 'applied knowledge classification draft is immutable');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_classification_applied_draft_delete_immutable
BEFORE DELETE ON candidate_knowledge_classification_drafts
WHEN EXISTS (
    SELECT 1 FROM import_knowledge_classification_runs r
    WHERE r.import_job_id=OLD.import_job_id AND r.applied_at IS NOT NULL
)
AND NOT EXISTS (
    SELECT 1 FROM knowledge_classification_archival_authorizations a
    WHERE a.import_job_id=OLD.import_job_id
)
BEGIN
    SELECT RAISE(ABORT, 'applied knowledge classification draft is immutable');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_classification_replacement_snapshot_immutable
BEFORE UPDATE ON knowledge_classification_replacement_snapshots
BEGIN
    SELECT RAISE(ABORT, 'knowledge classification replacement snapshot is immutable');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_classification_active_replacement_snapshot_delete_immutable
BEFORE DELETE ON knowledge_classification_replacement_snapshots
WHEN EXISTS (
    SELECT 1 FROM import_knowledge_classification_runs r
    WHERE r.import_job_id=OLD.import_job_id AND r.replacement_active=1
)
BEGIN
    SELECT RAISE(ABORT, 'active knowledge classification replacement snapshot is immutable');
END;

CREATE TABLE IF NOT EXISTS import_web_admission_runs (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('processing','completed','failed')),
    stage TEXT NOT NULL CHECK (
        stage IN ('preparing_backup','processing','admitted_pending_finalize','completed','failed')
    ),
    claim_token TEXT CHECK (
        claim_token IS NULL OR (
            length(claim_token)=64 AND claim_token NOT GLOB '*[^0-9a-f]*'
        )
    ),
    expected_count INTEGER NOT NULL CHECK (expected_count > 0),
    backup_relative_path TEXT CHECK (
        backup_relative_path IS NULL OR (
        length(trim(backup_relative_path)) BETWEEN 1 AND 500
        AND substr(backup_relative_path,1,1) <> '/'
        AND backup_relative_path NOT LIKE '%..%'
        AND backup_relative_path NOT LIKE '%\%'
        )
    ),
    backup_sha256 TEXT CHECK (
        backup_sha256 IS NULL OR (
        length(backup_sha256)=64 AND backup_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    pre_backup_source_digest TEXT CHECK (
        pre_backup_source_digest IS NULL OR length(pre_backup_source_digest)=64
    ),
    backup_snapshot_digest TEXT CHECK (
        backup_snapshot_digest IS NULL OR length(backup_snapshot_digest)=64
    ),
    question_code_digest TEXT CHECK (
        question_code_digest IS NULL OR (
            length(question_code_digest)=64
            AND question_code_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    formal_batch_digest TEXT CHECK (
        formal_batch_digest IS NULL OR (
            length(formal_batch_digest)=64
            AND formal_batch_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    inserted_count INTEGER CHECK (inserted_count IS NULL OR inserted_count >= 0),
    already_present_count INTEGER CHECK (
        already_present_count IS NULL OR already_present_count >= 0
    ),
    eligible_count INTEGER CHECK (eligible_count IS NULL OR eligible_count >= 0),
    finalize_backup_relative_path TEXT CHECK (
        finalize_backup_relative_path IS NULL OR (
            length(trim(finalize_backup_relative_path)) BETWEEN 1 AND 500
            AND substr(finalize_backup_relative_path,1,1) <> '/'
            AND finalize_backup_relative_path NOT LIKE '%..%'
            AND finalize_backup_relative_path NOT LIKE '%\%'
        )
    ),
    finalize_backup_sha256 TEXT CHECK (
        finalize_backup_sha256 IS NULL OR (
            length(finalize_backup_sha256)=64
            AND finalize_backup_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    finalize_source_digest TEXT CHECK (
        finalize_source_digest IS NULL OR length(finalize_source_digest)=64
    ),
    finalize_backup_snapshot_digest TEXT CHECK (
        finalize_backup_snapshot_digest IS NULL OR length(finalize_backup_snapshot_digest)=64
    ),
    safe_error TEXT CHECK (safe_error IS NULL OR length(safe_error) <= 100),
    claimed_at TEXT,
    heartbeat_at TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (status='processing' AND claim_token IS NOT NULL)
        OR (status IN ('completed','failed') AND claim_token IS NULL)
    ),
    CHECK (status!='processing' OR (
        claimed_at IS NOT NULL AND heartbeat_at IS NOT NULL
        AND lease_expires_at IS NOT NULL
    )),
    CHECK (
        (status='processing' AND stage IN ('preparing_backup','processing','admitted_pending_finalize'))
        OR (status='failed' AND stage IN ('processing','failed','admitted_pending_finalize'))
        OR (status='completed' AND stage='completed')
    ),
    CHECK (stage!='completed' OR (
        status='completed' AND claim_token IS NULL AND completed_at IS NOT NULL
        AND question_code_digest IS NOT NULL
        AND formal_batch_digest IS NOT NULL
        AND inserted_count IS NOT NULL AND already_present_count IS NOT NULL
        AND eligible_count=expected_count
        AND finalize_backup_relative_path IS NOT NULL
        AND finalize_backup_sha256 IS NOT NULL AND safe_error IS NULL
    )),
    CHECK (stage NOT IN ('admitted_pending_finalize','completed') OR (
        question_code_digest IS NOT NULL AND inserted_count IS NOT NULL
        AND already_present_count IS NOT NULL AND eligible_count=expected_count
    )),
    CHECK (stage NOT IN ('processing','admitted_pending_finalize','completed') OR (
        backup_relative_path IS NOT NULL AND backup_sha256 IS NOT NULL
    )),
    CHECK ((backup_relative_path IS NULL)=(backup_sha256 IS NULL)),
    CHECK ((finalize_backup_relative_path IS NULL)=(finalize_backup_sha256 IS NULL)),
    CHECK (status='processing' OR lease_expires_at IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_web_admission_run_claim
ON import_web_admission_runs(status, lease_expires_at);

CREATE VIEW IF NOT EXISTS completed_formal_admissions AS
SELECT import_job_id
FROM import_web_admission_runs
WHERE status='completed'
UNION
SELECT import_job_id
FROM historical_residual_admissions;

CREATE TRIGGER IF NOT EXISTS web_admission_completed_immutable
BEFORE UPDATE ON import_web_admission_runs
WHEN OLD.status='completed'
BEGIN
    SELECT RAISE(ABORT, 'completed web admission run is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_completed_delete_immutable
BEFORE DELETE ON import_web_admission_runs
WHEN OLD.status='completed'
BEGIN
    SELECT RAISE(ABORT, 'completed web admission run is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_job_status_update
BEFORE UPDATE OF status ON import_jobs
WHEN OLD.status='completed' AND NEW.status!='completed' AND EXISTS (
    SELECT 1 FROM completed_formal_admissions r
    WHERE r.import_job_id=OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission job status is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_run_completed_job_insert
BEFORE INSERT ON import_web_admission_runs
WHEN NEW.status='completed' AND NOT EXISTS (
    SELECT 1 FROM import_jobs j
    WHERE j.id=NEW.import_job_id AND j.status='completed'
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission run requires completed job');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_run_completed_job_update
BEFORE UPDATE OF status ON import_web_admission_runs
WHEN NEW.status='completed' AND NOT EXISTS (
    SELECT 1 FROM import_jobs j
    WHERE j.id=NEW.import_job_id AND j.status='completed'
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission run requires completed job');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_questions_update
BEFORE UPDATE ON questions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.id
) AND (
    NEW.id IS NOT OLD.id OR NEW.question_code IS NOT OLD.question_code
    OR NEW.stem_markdown IS NOT OLD.stem_markdown
    OR NEW.answer_markdown IS NOT OLD.answer_markdown
    OR NEW.answer_status IS NOT OLD.answer_status
    OR NEW.analysis_markdown IS NOT OLD.analysis_markdown
    OR NEW.region_code IS NOT OLD.region_code
    OR NEW.exam_year IS NOT OLD.exam_year
    OR NEW.exam_type_code IS NOT OLD.exam_type_code
    OR NEW.paper_name IS NOT OLD.paper_name
    OR NEW.source_question_no IS NOT OLD.source_question_no
    OR NEW.source_page IS NOT OLD.source_page
    OR NEW.score IS NOT OLD.score
    OR NEW.source_file_path IS NOT OLD.source_file_path
    OR NEW.question_type_code IS NOT OLD.question_type_code
    OR NEW.difficulty_level IS NOT OLD.difficulty_level
    OR NEW.difficulty_basis IS NOT OLD.difficulty_basis
    OR NEW.primary_knowledge_point_id IS NOT OLD.primary_knowledge_point_id
    OR NEW.ocr_review_status IS NOT OLD.ocr_review_status
    OR NEW.formula_review_status IS NOT OLD.formula_review_status
    OR NEW.figure_review_status IS NOT OLD.figure_review_status
    OR NEW.answer_review_status IS NOT OLD.answer_review_status
    OR NEW.analysis_review_status IS NOT OLD.analysis_review_status
    OR NEW.tag_review_status IS NOT OLD.tag_review_status
    OR NEW.usability_status IS NOT OLD.usability_status
    OR NEW.content_hash IS NOT OLD.content_hash
    OR NEW.duplicate_group_id IS NOT OLD.duplicate_group_id
    OR NEW.created_at IS NOT OLD.created_at
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission question is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_questions_delete
BEFORE DELETE ON questions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission question is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_sources_insert
BEFORE INSERT ON question_sources
WHEN EXISTS (
    SELECT 1 FROM completed_formal_admissions r
    WHERE r.import_job_id=NEW.import_job_id
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission source is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_sources_update
BEFORE UPDATE ON question_sources
WHEN EXISTS (
    SELECT 1 FROM completed_formal_admissions r
    WHERE r.import_job_id IN (OLD.import_job_id,NEW.import_job_id)
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission source is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_sources_delete
BEFORE DELETE ON question_sources
WHEN EXISTS (
    SELECT 1 FROM completed_formal_admissions r
    WHERE r.import_job_id=OLD.import_job_id
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission source is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_options_insert
BEFORE INSERT ON question_options
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission option is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_options_update
BEFORE UPDATE ON question_options
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id)
)
BEGIN SELECT RAISE(ABORT, 'completed web admission option is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_options_delete
BEFORE DELETE ON question_options
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission option is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_subquestions_insert
BEFORE INSERT ON subquestions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission subquestion is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_subquestions_update
BEFORE UPDATE ON subquestions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id
)
OR EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission subquestion is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_subquestions_delete
BEFORE DELETE ON subquestions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission subquestion is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_formulas_insert
BEFORE INSERT ON question_formulas
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission formula is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_formulas_update
BEFORE UPDATE ON question_formulas
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id)
)
BEGIN SELECT RAISE(ABORT, 'completed web admission formula is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_formulas_delete
BEFORE DELETE ON question_formulas
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission formula is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_figures_insert
BEFORE INSERT ON question_figures
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission figure is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_figures_update
BEFORE UPDATE ON question_figures
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id)
)
BEGIN SELECT RAISE(ABORT, 'completed web admission figure is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_figures_delete
BEFORE DELETE ON question_figures
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission figure is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_tags_insert
BEFORE INSERT ON question_tags
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission tag is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_tags_update
BEFORE UPDATE ON question_tags
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id)
)
BEGIN SELECT RAISE(ABORT, 'completed web admission tag is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_tags_delete
BEFORE DELETE ON question_tags
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission tag is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_knowledge_insert
BEFORE INSERT ON question_related_knowledge_points
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission knowledge relation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_knowledge_update
BEFORE UPDATE ON question_related_knowledge_points
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id)
)
BEGIN SELECT RAISE(ABORT, 'completed web admission knowledge relation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_knowledge_delete
BEFORE DELETE ON question_related_knowledge_points
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission knowledge relation is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_assets_insert
BEFORE INSERT ON question_assets
WHEN EXISTS (
    SELECT 1 FROM completed_formal_admissions r
    WHERE r.import_job_id=NEW.import_job_id
)
OR EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission asset is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_assets_update
BEFORE UPDATE ON question_assets
WHEN EXISTS (
    SELECT 1 FROM completed_formal_admissions r
    WHERE r.import_job_id IN (OLD.import_job_id,NEW.import_job_id)
)
OR EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id)
)
BEGIN SELECT RAISE(ABORT, 'completed web admission asset is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_assets_delete
BEFORE DELETE ON question_assets
WHEN EXISTS (
    SELECT 1 FROM completed_formal_admissions r
    WHERE r.import_job_id=OLD.import_job_id
)
OR EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission asset is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_reviews_insert
BEFORE INSERT ON question_reviews
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission review is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_reviews_update
BEFORE UPDATE ON question_reviews
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id)
)
BEGIN SELECT RAISE(ABORT, 'completed web admission review is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_reviews_delete
BEFORE DELETE ON question_reviews
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission review is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_versions_insert
BEFORE INSERT ON question_versions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_versions_update
BEFORE UPDATE ON question_versions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id)
)
BEGIN SELECT RAISE(ABORT, 'completed web admission version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_versions_delete
BEFORE DELETE ON question_versions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN completed_formal_admissions r ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id
)
BEGIN SELECT RAISE(ABORT, 'completed web admission version is immutable'); END;

CREATE INDEX IF NOT EXISTS idx_questions_content_hash ON questions(content_hash);
CREATE INDEX IF NOT EXISTS idx_questions_source ON questions(region_code, exam_year, exam_type_code, paper_name, source_question_no);
CREATE INDEX IF NOT EXISTS idx_questions_primary_knowledge ON questions(primary_knowledge_point_id);
CREATE INDEX IF NOT EXISTS idx_questions_type_difficulty ON questions(question_type_code, difficulty_level);
CREATE INDEX IF NOT EXISTS idx_questions_usability ON questions(usability_status);
CREATE INDEX IF NOT EXISTS idx_questions_duplicate_group ON questions(duplicate_group_id);
CREATE INDEX IF NOT EXISTS idx_related_knowledge_point ON question_related_knowledge_points(knowledge_point_id);
CREATE INDEX IF NOT EXISTS idx_question_tags_tag ON question_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_question_reviews_question_time ON question_reviews(question_id, reviewed_at);
CREATE INDEX IF NOT EXISTS idx_question_sources_paper ON question_sources(source_paper_id);
CREATE INDEX IF NOT EXISTS idx_question_assets_question ON question_assets(question_id, asset_kind);
CREATE INDEX IF NOT EXISTS idx_usage_question_time ON question_usage_records(question_id, used_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_parent ON knowledge_points(parent_id);
CREATE INDEX IF NOT EXISTS idx_source_papers_source ON source_papers(region_code, exam_year, exam_type_code);
CREATE INDEX IF NOT EXISTS idx_import_jobs_source_status ON import_jobs(source_paper_id, status);
CREATE INDEX IF NOT EXISTS idx_import_upload_receipts_source ON import_upload_receipts(source_paper_id);
CREATE INDEX IF NOT EXISTS idx_import_question_split_status ON import_question_split_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_import_candidate_extraction_status ON import_candidate_extraction_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_import_candidate_audit_status ON import_candidate_audit_runs(status, updated_at);

-- Official answer sections are a separate source-bound trust domain.  A blank
-- candidate answer never implies that the source paper had no answer section.
CREATE TABLE IF NOT EXISTS import_answer_sources (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    source_answer_state TEXT NOT NULL CHECK (source_answer_state IN (
        'source_has_no_answer','source_has_answer_unprocessed','source_answer_linked'
    )),
    answer_page_start INTEGER CHECK (answer_page_start IS NULL OR answer_page_start > 0),
    answer_page_end INTEGER CHECK (answer_page_end IS NULL OR answer_page_end > 0),
    render_manifest_sha256 TEXT CHECK (
        render_manifest_sha256 IS NULL OR length(render_manifest_sha256)=64
    ),
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256)=64),
    draft_batch_sha256 TEXT NOT NULL CHECK (length(draft_batch_sha256)=64),
    expected_question_count INTEGER NOT NULL CHECK (expected_question_count > 0),
    classification_evidence_sha256 TEXT NOT NULL CHECK (length(classification_evidence_sha256)=64),
    applied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (source_answer_state='source_has_no_answer' AND answer_page_start IS NULL
         AND answer_page_end IS NULL AND render_manifest_sha256 IS NULL)
        OR
        (source_answer_state!='source_has_no_answer' AND answer_page_start IS NOT NULL
         AND answer_page_end IS NOT NULL AND answer_page_start<=answer_page_end
         AND render_manifest_sha256 IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS import_answer_pages (
    import_job_id INTEGER NOT NULL REFERENCES import_answer_sources(import_job_id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    relative_path TEXT NOT NULL CHECK (
        relative_path GLOB 'pages/page_[0-9][0-9][0-9].png'
        AND relative_path NOT LIKE '%..%' AND relative_path NOT LIKE '%\%'
    ),
    png_sha256 TEXT NOT NULL CHECK (length(png_sha256)=64),
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    pixel_width INTEGER NOT NULL CHECK (pixel_width > 0),
    pixel_height INTEGER NOT NULL CHECK (pixel_height > 0),
    PRIMARY KEY(import_job_id,page_number)
);

CREATE TABLE IF NOT EXISTS import_answer_extraction_runs (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_answer_sources(import_job_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('processing','completed','failed')),
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256)=64),
    draft_batch_sha256 TEXT NOT NULL CHECK (length(draft_batch_sha256)=64),
    answer_pages_sha256 TEXT NOT NULL CHECK (length(answer_pages_sha256)=64),
    model_run_id TEXT UNIQUE CHECK (model_run_id IS NULL OR length(model_run_id) BETWEEN 1 AND 200),
    raw_artifact_sha256 TEXT CHECK (raw_artifact_sha256 IS NULL OR length(raw_artifact_sha256)=64),
    raw_artifact_byte_size INTEGER CHECK (raw_artifact_byte_size IS NULL OR raw_artifact_byte_size > 0),
    output_sha256 TEXT CHECK (output_sha256 IS NULL OR length(output_sha256)=64),
    output_byte_size INTEGER CHECK (output_byte_size IS NULL OR output_byte_size > 0),
    question_count INTEGER NOT NULL CHECK (question_count > 0),
    claim_token TEXT UNIQUE CHECK (claim_token IS NULL OR length(claim_token)=64),
    lease_expires_at TEXT,
    error_message TEXT CHECK (error_message IS NULL OR length(error_message)<=100),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (status!='completed' OR (
        model_run_id IS NOT NULL AND raw_artifact_sha256 IS NOT NULL
        AND raw_artifact_byte_size IS NOT NULL AND output_sha256 IS NOT NULL
        AND output_byte_size IS NOT NULL AND completed_at IS NOT NULL
    ))
);

CREATE TABLE IF NOT EXISTS candidate_official_answers (
    import_job_id INTEGER NOT NULL REFERENCES import_answer_extraction_runs(import_job_id) ON DELETE RESTRICT,
    source_question_no TEXT NOT NULL CHECK (length(trim(source_question_no)) BETWEEN 1 AND 3),
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256)=64),
    draft_batch_sha256 TEXT NOT NULL CHECK (length(draft_batch_sha256)=64),
    content_kind TEXT NOT NULL CHECK (content_kind IN ('short_answer','worked_solution')),
    answer_markdown TEXT NOT NULL,
    analysis_markdown TEXT NOT NULL,
    subquestions_json TEXT NOT NULL CHECK (json_valid(subquestions_json)),
    source_pages_json TEXT NOT NULL CHECK (json_valid(source_pages_json)),
    source_page_hashes_json TEXT NOT NULL CHECK (json_valid(source_page_hashes_json)),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
    extraction_artifact_sha256 TEXT NOT NULL CHECK (length(extraction_artifact_sha256)=64),
    PRIMARY KEY(import_job_id,source_question_no)
);

CREATE TABLE IF NOT EXISTS import_answer_review_runs (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_answer_extraction_runs(import_job_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('processing','completed','failed')),
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256)=64),
    draft_batch_sha256 TEXT NOT NULL CHECK (length(draft_batch_sha256)=64),
    answer_pages_sha256 TEXT NOT NULL CHECK (length(answer_pages_sha256)=64),
    extraction_artifact_sha256 TEXT NOT NULL CHECK (length(extraction_artifact_sha256)=64),
    producer_model_run_id TEXT NOT NULL CHECK (length(producer_model_run_id) BETWEEN 1 AND 200),
    reviewer_model_run_id TEXT UNIQUE CHECK (reviewer_model_run_id IS NULL OR length(reviewer_model_run_id) BETWEEN 1 AND 200),
    raw_artifact_sha256 TEXT CHECK (raw_artifact_sha256 IS NULL OR length(raw_artifact_sha256)=64),
    output_sha256 TEXT CHECK (output_sha256 IS NULL OR length(output_sha256)=64),
    question_count INTEGER NOT NULL CHECK (question_count > 0),
    claim_token TEXT UNIQUE CHECK (claim_token IS NULL OR length(claim_token)=64),
    lease_expires_at TEXT,
    error_message TEXT CHECK (error_message IS NULL OR length(error_message)<=100),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (reviewer_model_run_id IS NULL OR reviewer_model_run_id != producer_model_run_id),
    CHECK (status!='completed' OR (
        reviewer_model_run_id IS NOT NULL AND raw_artifact_sha256 IS NOT NULL
        AND output_sha256 IS NOT NULL AND completed_at IS NOT NULL
    ))
);

CREATE TABLE IF NOT EXISTS candidate_official_answer_reviews (
    import_job_id INTEGER NOT NULL REFERENCES import_answer_review_runs(import_job_id) ON DELETE RESTRICT,
    source_question_no TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('passed','failed')),
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256)=64),
    draft_batch_sha256 TEXT NOT NULL CHECK (length(draft_batch_sha256)=64),
    answer_pages_sha256 TEXT NOT NULL CHECK (length(answer_pages_sha256)=64),
    extraction_artifact_sha256 TEXT NOT NULL CHECK (length(extraction_artifact_sha256)=64),
    answer_content_sha256 TEXT NOT NULL CHECK (length(answer_content_sha256)=64),
    answer_analysis_sha256 TEXT NOT NULL CHECK (length(answer_analysis_sha256)=64),
    source_pages_json TEXT NOT NULL CHECK (json_valid(source_pages_json)),
    source_page_hashes_json TEXT NOT NULL CHECK (json_valid(source_page_hashes_json)),
    review_evidence_json TEXT NOT NULL CHECK (json_valid(review_evidence_json)),
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY(import_job_id,source_question_no)
);

-- A short-lived row authorizes only the answer-overlay transaction to rebind
-- immutable applied classification rows.  It is deleted before commit.
CREATE TABLE IF NOT EXISTS official_answer_overlay_authorizations (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    authorization_token TEXT NOT NULL UNIQUE CHECK (
        length(authorization_token)=64
        AND authorization_token NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_official_answer_overlays (
    import_job_id INTEGER NOT NULL,
    source_question_no TEXT NOT NULL,
    prior_draft_version INTEGER NOT NULL CHECK (prior_draft_version > 0),
    prior_edited_sha256 TEXT NOT NULL CHECK (length(prior_edited_sha256)=64),
    new_draft_version INTEGER NOT NULL CHECK (new_draft_version=prior_draft_version+1),
    new_edited_sha256 TEXT NOT NULL CHECK (length(new_edited_sha256)=64),
    visual_scope_before_sha256 TEXT NOT NULL CHECK (length(visual_scope_before_sha256)=64),
    visual_scope_after_sha256 TEXT NOT NULL CHECK (length(visual_scope_after_sha256)=64),
    classification_scope_before_sha256 TEXT NOT NULL CHECK (length(classification_scope_before_sha256)=64),
    classification_scope_after_sha256 TEXT NOT NULL CHECK (length(classification_scope_after_sha256)=64),
    approval_source TEXT NOT NULL CHECK (approval_source IN ('human','ai_second_pass')),
    approval_evidence_sha256 TEXT NOT NULL CHECK (length(approval_evidence_sha256)=64),
    extraction_artifact_sha256 TEXT NOT NULL CHECK (length(extraction_artifact_sha256)=64),
    review_artifact_sha256 TEXT NOT NULL CHECK (length(review_artifact_sha256)=64),
    answer_content_sha256 TEXT NOT NULL CHECK (length(answer_content_sha256)=64),
    answer_analysis_sha256 TEXT NOT NULL CHECK (length(answer_analysis_sha256)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(import_job_id,source_question_no),
    FOREIGN KEY(import_job_id,source_question_no)
        REFERENCES candidate_review_drafts(import_job_id,source_question_no)
        ON DELETE RESTRICT,
    CHECK (visual_scope_before_sha256=visual_scope_after_sha256),
    CHECK (classification_scope_before_sha256=classification_scope_after_sha256)
);

CREATE TRIGGER IF NOT EXISTS candidate_official_answer_overlays_immutable
BEFORE UPDATE ON candidate_official_answer_overlays
BEGIN
    SELECT RAISE(ABORT, 'official answer overlay evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS candidate_official_answer_overlays_delete_immutable
BEFORE DELETE ON candidate_official_answer_overlays
BEGIN
    SELECT RAISE(ABORT, 'official answer overlay evidence is immutable');
END;

-- Parser-rejected model output is retained only as untrusted diagnostic evidence.
-- No admission/review query consumes this table.
CREATE TABLE IF NOT EXISTS import_answer_raw_diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE RESTRICT,
    stage TEXT NOT NULL CHECK (stage IN ('extraction','review')),
    model_run_id TEXT NOT NULL CHECK (length(model_run_id) BETWEEN 1 AND 200),
    artifact_relative_path TEXT NOT NULL UNIQUE CHECK (
        artifact_relative_path GLOB 'official_answer_diagnostics/[a-z0-9_-]*.json'
        AND artifact_relative_path NOT LIKE '%..%' AND artifact_relative_path NOT LIKE '%\%'
    ),
    raw_sha256 TEXT NOT NULL CHECK (length(raw_sha256)=64),
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    trusted INTEGER NOT NULL DEFAULT 0 CHECK (trusted=0),
    created_at TEXT NOT NULL,
    UNIQUE(import_job_id,stage,model_run_id,raw_sha256)
);
