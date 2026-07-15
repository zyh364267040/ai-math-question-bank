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
    error_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (total_pages IS NULL OR rendered_pages <= total_pages)
);

CREATE TABLE IF NOT EXISTS import_upload_receipts (
    token TEXT PRIMARY KEY CHECK (
        length(trim(token)) BETWEEN 1 AND 200
    ),
    source_paper_id INTEGER NOT NULL REFERENCES source_papers(id) ON DELETE RESTRICT,
    import_job_id INTEGER NOT NULL UNIQUE REFERENCES import_jobs(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
