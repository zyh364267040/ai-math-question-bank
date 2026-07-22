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
    primary_knowledge_point_code TEXT NOT NULL REFERENCES knowledge_points(code) ON DELETE RESTRICT,
    related_knowledge_point_codes_json TEXT NOT NULL,
    classifier TEXT NOT NULL CHECK (length(trim(classifier)) BETWEEN 1 AND 100),
    reviewer TEXT NOT NULL CHECK (length(trim(reviewer)) BETWEEN 1 AND 100),
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
BEGIN
    SELECT RAISE(ABORT, 'completed knowledge classification is immutable');
END;

CREATE TRIGGER IF NOT EXISTS candidate_knowledge_classifications_delete_immutable
BEFORE DELETE ON candidate_knowledge_classifications
BEGIN
    SELECT RAISE(ABORT, 'completed knowledge classification is immutable');
END;

CREATE TABLE IF NOT EXISTS import_knowledge_classification_runs (
    import_job_id INTEGER PRIMARY KEY REFERENCES import_jobs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending','processing','completed','failed')
    ),
    stage TEXT NOT NULL DEFAULT 'waiting' CHECK (
        stage IN ('waiting','level2','proposal','verifier','publishing','review_ready')
    ),
    question_count INTEGER CHECK (question_count IS NULL OR question_count > 0),
    processed_questions INTEGER NOT NULL DEFAULT 0 CHECK (processed_questions >= 0),
    model TEXT NOT NULL DEFAULT 'qwen2.5:14b' CHECK (length(trim(model)) BETWEEN 1 AND 100),
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
    proposal_primary_code TEXT NOT NULL REFERENCES knowledge_points(code) ON DELETE RESTRICT,
    proposal_related_codes_json TEXT NOT NULL CHECK (json_valid(proposal_related_codes_json)),
    proposal_confidence TEXT NOT NULL CHECK (proposal_confidence IN ('low','medium','high')),
    proposal_reason TEXT NOT NULL CHECK (length(proposal_reason) BETWEEN 1 AND 200),
    verifier_primary_code TEXT NOT NULL REFERENCES knowledge_points(code) ON DELETE RESTRICT,
    verifier_related_codes_json TEXT NOT NULL CHECK (json_valid(verifier_related_codes_json)),
    verifier_confidence TEXT NOT NULL CHECK (verifier_confidence IN ('low','medium','high')),
    verifier_reason TEXT NOT NULL CHECK (length(verifier_reason) BETWEEN 1 AND 200),
    final_primary_code TEXT NOT NULL REFERENCES knowledge_points(code) ON DELETE RESTRICT,
    final_related_codes_json TEXT NOT NULL CHECK (json_valid(final_related_codes_json)),
    status TEXT NOT NULL CHECK (status IN ('pending','approved')),
    approval_source TEXT CHECK (approval_source IN ('local_double_pass','human') OR approval_source IS NULL),
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

CREATE TRIGGER IF NOT EXISTS knowledge_classification_applied_run_immutable
BEFORE UPDATE ON import_knowledge_classification_runs
WHEN OLD.applied_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'applied knowledge classification run is immutable');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_classification_completed_run_delete_immutable
BEFORE DELETE ON import_knowledge_classification_runs
WHEN OLD.status='completed' OR OLD.applied_at IS NOT NULL
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
)
BEGIN
    SELECT RAISE(ABORT, 'completed knowledge classification output is immutable');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_classification_applied_draft_immutable
BEFORE UPDATE ON candidate_knowledge_classification_drafts
WHEN EXISTS (
    SELECT 1 FROM import_knowledge_classification_runs r
    WHERE r.import_job_id=OLD.import_job_id AND r.applied_at IS NOT NULL
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
BEGIN
    SELECT RAISE(ABORT, 'applied knowledge classification draft is immutable');
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
    SELECT 1 FROM import_web_admission_runs r
    WHERE r.import_job_id=OLD.id AND r.status='completed'
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
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.id AND r.status='completed'
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
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.id AND r.status='completed'
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission question is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_sources_insert
BEFORE INSERT ON question_sources
WHEN EXISTS (
    SELECT 1 FROM import_web_admission_runs r
    WHERE r.import_job_id=NEW.import_job_id AND r.status='completed'
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission source is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_sources_update
BEFORE UPDATE ON question_sources
WHEN EXISTS (
    SELECT 1 FROM import_web_admission_runs r
    WHERE r.import_job_id IN (OLD.import_job_id,NEW.import_job_id) AND r.status='completed'
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission source is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_sources_delete
BEFORE DELETE ON question_sources
WHEN EXISTS (
    SELECT 1 FROM import_web_admission_runs r
    WHERE r.import_job_id=OLD.import_job_id AND r.status='completed'
)
BEGIN
    SELECT RAISE(ABORT, 'completed web admission source is immutable');
END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_options_insert
BEFORE INSERT ON question_options
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission option is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_options_update
BEFORE UPDATE ON question_options
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id) AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission option is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_options_delete
BEFORE DELETE ON question_options
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission option is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_subquestions_insert
BEFORE INSERT ON subquestions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission subquestion is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_subquestions_update
BEFORE UPDATE ON subquestions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id AND r.status='completed'
)
OR EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission subquestion is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_subquestions_delete
BEFORE DELETE ON subquestions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission subquestion is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_formulas_insert
BEFORE INSERT ON question_formulas
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission formula is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_formulas_update
BEFORE UPDATE ON question_formulas
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id) AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission formula is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_formulas_delete
BEFORE DELETE ON question_formulas
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission formula is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_figures_insert
BEFORE INSERT ON question_figures
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission figure is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_figures_update
BEFORE UPDATE ON question_figures
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id) AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission figure is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_figures_delete
BEFORE DELETE ON question_figures
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission figure is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_tags_insert
BEFORE INSERT ON question_tags
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission tag is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_tags_update
BEFORE UPDATE ON question_tags
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id) AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission tag is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_tags_delete
BEFORE DELETE ON question_tags
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission tag is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_knowledge_insert
BEFORE INSERT ON question_related_knowledge_points
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission knowledge relation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_knowledge_update
BEFORE UPDATE ON question_related_knowledge_points
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id) AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission knowledge relation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_knowledge_delete
BEFORE DELETE ON question_related_knowledge_points
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission knowledge relation is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_assets_insert
BEFORE INSERT ON question_assets
WHEN EXISTS (
    SELECT 1 FROM import_web_admission_runs r
    WHERE r.import_job_id=NEW.import_job_id AND r.status='completed'
)
OR EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission asset is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_assets_update
BEFORE UPDATE ON question_assets
WHEN EXISTS (
    SELECT 1 FROM import_web_admission_runs r
    WHERE r.import_job_id IN (OLD.import_job_id,NEW.import_job_id) AND r.status='completed'
)
OR EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id) AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission asset is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_assets_delete
BEFORE DELETE ON question_assets
WHEN EXISTS (
    SELECT 1 FROM import_web_admission_runs r
    WHERE r.import_job_id=OLD.import_job_id AND r.status='completed'
)
OR EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r
      ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission asset is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_reviews_insert
BEFORE INSERT ON question_reviews
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission review is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_reviews_update
BEFORE UPDATE ON question_reviews
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id) AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission review is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_reviews_delete
BEFORE DELETE ON question_reviews
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission review is immutable'); END;

CREATE TRIGGER IF NOT EXISTS web_admission_protect_versions_insert
BEFORE INSERT ON question_versions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r ON r.import_job_id=s.import_job_id
    WHERE s.question_id=NEW.question_id AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_versions_update
BEFORE UPDATE ON question_versions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r ON r.import_job_id=s.import_job_id
    WHERE s.question_id IN (OLD.question_id,NEW.question_id) AND r.status='completed'
)
BEGIN SELECT RAISE(ABORT, 'completed web admission version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_admission_protect_versions_delete
BEFORE DELETE ON question_versions
WHEN EXISTS (
    SELECT 1 FROM question_sources s JOIN import_web_admission_runs r ON r.import_job_id=s.import_job_id
    WHERE s.question_id=OLD.question_id AND r.status='completed'
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
