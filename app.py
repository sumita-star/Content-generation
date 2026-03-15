"""
DigitalAIzeme — Content Workflow Management Platform
Local deployment with Flask + SQLite
"""

import os
import json
import sqlite3
import subprocess
import hashlib
import secrets
import base64
from datetime import datetime, timedelta
from pathlib import Path
from flask import (
    Flask, render_template, request, jsonify, redirect, url_for, g, send_from_directory, session
)
import threading
import time as _time
from cryptography.fernet import Fernet
from theme import FONTS, get_theme_colors, tailwind_theme_config, css_variables

app = Flask(__name__)
app.config['SECRET_KEY'] = 'digitalize-me-local-2026'


# ─── Encryption ─────────────────────────────────────────────────────

def _get_encryption_key():
    """Load or generate a Fernet encryption key for API keys at rest."""
    key_file = os.path.join(os.path.dirname(__file__), '.encryption_key')
    if os.path.exists(key_file):
        with open(key_file, 'rb') as f:
            return f.read()
    key = Fernet.generate_key()
    with open(key_file, 'wb') as f:
        f.write(key)
    return key

ENCRYPTION_KEY = _get_encryption_key()
_fernet = Fernet(ENCRYPTION_KEY)

# Keys that should be encrypted at rest
ENCRYPTED_KEYS = {'anthropic_api_key', 'google_api_key', 'gemini_api_key', 'apify_api_key'}


def encrypt_value(plaintext):
    """Encrypt a string value for storage."""
    if not plaintext:
        return ''
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext):
    """Decrypt a stored value."""
    if not ciphertext:
        return ''
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except Exception:
        # Return as-is if not encrypted (migration from plaintext)
        return ciphertext


# ─── Password Hashing ──────────────────────────────────────────────

def hash_password(password, salt=None):
    """Hash a password with PBKDF2-SHA256."""
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return f"{salt}:{base64.b64encode(hashed).decode()}"


def verify_password(password, stored_hash):
    """Verify a password against a stored hash."""
    if not stored_hash or ':' not in stored_hash:
        return False
    salt = stored_hash.split(':')[0]
    return hash_password(password, salt) == stored_hash


# Template filters
@app.template_filter('from_json')
def from_json_filter(value):
    """Parse JSON string in templates."""
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


@app.context_processor
def inject_globals():
    """Inject theme config, current time, and all brands into every template."""
    db = get_db()
    all_brands = db.execute("SELECT id, name, accent_color FROM brands ORDER BY name").fetchall()
    theme_mode = get_setting(db, 'theme_mode', 'dark')
    colors = get_theme_colors(theme_mode)
    return {
        'now': datetime.now(),
        'theme_mode': theme_mode,
        'theme_colors': colors,
        'theme_fonts': FONTS,
        'tailwind_theme': tailwind_theme_config(theme_mode),
        'css_variables': css_variables(theme_mode),
        'all_brands': all_brands,
    }

DATABASE = os.path.join(os.path.dirname(__file__), 'digitalize_me.db')
BRANDS_BASE = os.path.dirname(os.path.dirname(__file__))  # Parent = DIQIT folder


# ─── Database ───────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    db.commit()
    # Migrate: add new notification columns if missing
    _migrate_notifications(db)
    # Seed DIQIT if no brands exist
    count = db.execute("SELECT COUNT(*) FROM brands").fetchone()[0]
    if count == 0:
        seed_diqit(db)
    db.close()


def _migrate_notifications(db):
    """Add Sprint 8 task columns to notifications table if they don't exist."""
    cols = {row[1] for row in db.execute("PRAGMA table_info(notifications)").fetchall()}
    migrations = [
        ('is_done', 'BOOLEAN DEFAULT 0'),
        ('action_type', 'TEXT'),
        ('action_url', 'TEXT'),
        ('action_label', 'TEXT'),
        ('step_id', 'INTEGER'),
        ('step_type', 'TEXT'),
        ('duration_minutes', 'INTEGER DEFAULT 0'),
    ]
    for col_name, col_type in migrations:
        if col_name not in cols:
            db.execute(f"ALTER TABLE notifications ADD COLUMN {col_name} {col_type}")
    db.commit()


# ─── Schema ─────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tagline TEXT,
    primary_color TEXT DEFAULT '#000000',
    accent_color TEXT DEFAULT '#EF4324',
    logo_path TEXT,
    folder_path TEXT,
    voice_summary TEXT,
    website TEXT,
    canva_brand_kit_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS content_pillars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    color TEXT DEFAULT '#6366f1',
    sort_order INTEGER DEFAULT 0,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    is_default BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    instructions TEXT,
    tools TEXT,  -- JSON array of tool names
    day_of_week TEXT,  -- e.g. "Monday", "Tuesday"
    duration_minutes INTEGER DEFAULT 15,
    sort_order INTEGER DEFAULT 0,
    linked_folder TEXT,  -- relative folder path
    checklist TEXT,  -- JSON array of checklist items
    step_type TEXT DEFAULT 'manual',  -- manual, automated, semi-auto
    upload_folder TEXT,  -- folder where finished product is uploaded
    brief_template TEXT,  -- template for downloadable brief
    trigger_type TEXT,  -- 'apps_script', 'claude', 'none'
    FOREIGN KEY (template_id) REFERENCES workflow_templates(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cadence_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    channel TEXT DEFAULT 'LinkedIn',  -- LinkedIn, Blog, Email, etc.
    posts_per_week INTEGER DEFAULT 3,
    preferred_days TEXT,  -- JSON array of days
    preferred_times TEXT,  -- JSON array of times
    notes TEXT,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    pillar_id INTEGER,
    workflow_template_id INTEGER,
    title TEXT NOT NULL,
    content_type TEXT DEFAULT 'linkedin_post',  -- linkedin_post, carousel, video, blog, email, reel
    market TEXT DEFAULT 'APAC',  -- Japan, Singapore, APAC, Australia
    status TEXT DEFAULT 'backlog',  -- backlog, research, drafting, visuals, review, ready, published, analyzed
    body_text TEXT,
    visual_prompt TEXT,
    video_prompt TEXT,
    napkin_json TEXT,
    file_paths TEXT,  -- JSON array of local file paths to assets
    publish_date DATE,
    published_at TIMESTAMP,
    notes TEXT,
    media_tags TEXT DEFAULT '[]',  -- JSON array of tool tags: stock_image, tts, translate_ja, etc.
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
    FOREIGN KEY (pillar_id) REFERENCES content_pillars(id),
    FOREIGN KEY (workflow_template_id) REFERENCES workflow_templates(id)
);

CREATE TABLE IF NOT EXISTS content_step_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_item_id INTEGER NOT NULL,
    workflow_step_id INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',  -- pending, in_progress, completed, skipped
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    notes TEXT,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE CASCADE,
    FOREIGN KEY (workflow_step_id) REFERENCES workflow_steps(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_item_id INTEGER NOT NULL,
    feedback_type TEXT DEFAULT 'revision',  -- revision, approval, rejection, note
    feedback_text TEXT NOT NULL,
    claude_response TEXT,
    status TEXT DEFAULT 'pending',  -- pending, processing, applied, dismissed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER,
    content_item_id INTEGER,
    notification_type TEXT DEFAULT 'task',  -- task, reminder, overdue, system, feedback
    title TEXT NOT NULL,
    message TEXT,
    is_read BOOLEAN DEFAULT 0,
    is_done BOOLEAN DEFAULT 0,
    due_date DATE,
    action_type TEXT,       -- trigger_claude, trigger_apify, trigger_apps_script, open_folder, go_to_page, upload
    action_url TEXT,        -- relative URL or folder path for the action
    action_label TEXT,      -- button label e.g. "Run with Claude", "Open Folder"
    step_id INTEGER,        -- linked workflow step for execution
    step_type TEXT,         -- manual, automated, semi-auto
    duration_minutes INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS pipeline_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    trigger_type TEXT DEFAULT 'manual',  -- manual, scheduled
    status TEXT DEFAULT 'pending',  -- pending, running, completed, failed
    triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    result TEXT,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS folder_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    folder_path TEXT NOT NULL,
    workflow_step_id INTEGER,
    purpose TEXT,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
    FOREIGN KEY (workflow_step_id) REFERENCES workflow_steps(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS brand_voice_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    ref_type TEXT NOT NULL,  -- text_sample, tone_keyword, do, dont, competitor_example
    content TEXT NOT NULL,
    source TEXT,  -- e.g. "LinkedIn post from Jan 2026", "CEO interview"
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS brand_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    content_item_id INTEGER,
    audit_type TEXT DEFAULT 'voice',  -- voice, visual, full
    score REAL,  -- 0-100
    findings TEXT,  -- JSON: detailed findings
    recommendations TEXT,
    audited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS analytics_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    content_item_id INTEGER,
    snapshot_date DATE NOT NULL,
    metric_type TEXT NOT NULL,  -- impressions, likes, comments, shares, clicks, engagement_rate
    metric_value REAL DEFAULT 0,
    source TEXT DEFAULT 'manual',  -- manual, apify, linkedin_api
    raw_data TEXT,  -- JSON blob for extra fields
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS content_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    plan_month TEXT NOT NULL,  -- YYYY-MM format
    status TEXT DEFAULT 'draft',  -- draft, approved, archived
    ai_recommendations TEXT,  -- JSON: AI-generated plan suggestions
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    content_type TEXT DEFAULT 'linkedin_post',
    market TEXT DEFAULT 'APAC',
    pillar_id INTEGER,
    suggested_date DATE,
    rationale TEXT,  -- Why AI recommended this
    status TEXT DEFAULT 'suggested',  -- suggested, approved, rejected, scheduled
    content_item_id INTEGER,  -- Links to created content_item when scheduled
    media_tags TEXT DEFAULT '[]',  -- JSON array of tool tags recommended by AI
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (plan_id) REFERENCES content_plans(id) ON DELETE CASCADE,
    FOREIGN KEY (pillar_id) REFERENCES content_pillars(id),
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS competitor_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    competitor_name TEXT NOT NULL,
    linkedin_url TEXT,
    website_url TEXT,
    notes TEXT,
    last_scraped_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS asset_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    content_item_id INTEGER,
    asset_type TEXT NOT NULL,  -- image, document, slides, video_prompt, social_graphic
    prompt TEXT NOT NULL,
    provider TEXT DEFAULT 'gemini',  -- gemini, claude, manual
    status TEXT DEFAULT 'pending',  -- pending, generating, completed, failed
    result_path TEXT,  -- local file path to generated asset
    result_url TEXT,  -- external URL if applicable
    result_metadata TEXT,  -- JSON: dimensions, format, etc.
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS canva_designs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    content_item_id INTEGER,
    asset_job_id INTEGER,
    canva_design_id TEXT,
    canva_design_url TEXT,
    thumbnail_url TEXT,
    export_url TEXT,
    export_path TEXT,
    design_type TEXT DEFAULT 'social_media',
    prompt_used TEXT,
    brand_kit_id TEXT,
    status TEXT DEFAULT 'pending',
    qc_result TEXT,
    qc_notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE SET NULL,
    FOREIGN KEY (asset_job_id) REFERENCES asset_jobs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS claude_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    content_item_id INTEGER,
    title TEXT NOT NULL,
    messages TEXT NOT NULL,  -- JSON array of {role, content, timestamp}
    status TEXT DEFAULT 'active',  -- active, archived
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    content_item_id INTEGER NOT NULL,
    status TEXT DEFAULT 'running',  -- running, completed, failed, cancelled
    current_step INTEGER DEFAULT 0,
    total_steps INTEGER DEFAULT 0,
    log TEXT,  -- JSON array of step results
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS content_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    content_type TEXT DEFAULT 'linkedin_post',
    market TEXT DEFAULT 'APAC',
    body_template TEXT,  -- template text with {{placeholders}}
    visual_prompt_template TEXT,
    tags TEXT,  -- JSON array of tags
    use_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS market_adaptations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_item_id INTEGER NOT NULL,
    target_market TEXT NOT NULL,
    adapted_title TEXT,
    adapted_body TEXT,
    adapted_visual_prompt TEXT,
    status TEXT DEFAULT 'draft',  -- draft, approved, published
    content_item_id INTEGER,  -- linked adapted content item once created
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_item_id) REFERENCES content_items(id) ON DELETE CASCADE,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS drive_sync_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    folder_id TEXT NOT NULL,  -- Google Drive folder ID
    folder_name TEXT,
    sync_direction TEXT DEFAULT 'both',  -- upload, download, both
    local_path TEXT,  -- local folder to sync
    last_synced_at TIMESTAMP,
    enabled BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS performance_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    insight_type TEXT NOT NULL,  -- weekly_analysis, pillar_review, cadence_review, engagement_trend
    insight_text TEXT NOT NULL,
    recommendations TEXT,  -- JSON array of actionable recommendations
    data_source TEXT DEFAULT 'apify',  -- apify, manual, combined
    period_start DATE,
    period_end DATE,
    applied BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tool_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_item_id INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    media_tag TEXT,
    trigger_mode TEXT DEFAULT 'auto',  -- auto, manual
    status TEXT DEFAULT 'pending',  -- pending, running, completed, failed, skipped
    input_params TEXT,  -- JSON
    result_data TEXT,  -- JSON
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (content_item_id) REFERENCES content_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedback_forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    form_type TEXT DEFAULT 'general',
    fields TEXT NOT NULL,
    settings TEXT DEFAULT '{}',
    share_token TEXT NOT NULL UNIQUE,
    is_active BOOLEAN DEFAULT 1,
    response_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedback_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id INTEGER NOT NULL,
    response_data TEXT NOT NULL,
    respondent_name TEXT,
    respondent_email TEXT,
    source TEXT DEFAULT 'web',
    ip_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (form_id) REFERENCES feedback_forms(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedback_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id INTEGER NOT NULL,
    insight_text TEXT NOT NULL,
    response_count_at_analysis INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (form_id) REFERENCES feedback_forms(id) ON DELETE CASCADE
);
"""


# ─── Seed Data ──────────────────────────────────────────────────────

def seed_diqit(db):
    diqit_folder = BRANDS_BASE

    # Insert brand
    db.execute("""
        INSERT INTO brands (name, tagline, primary_color, accent_color, logo_path, folder_path, voice_summary, website)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        'DIQIT',
        'Bring Your Ideas To Life',
        '#000000',
        '#EF4324',
        os.path.join(diqit_folder, 'diqit_logo.jpeg'),
        diqit_folder,
        'Professional yet approachable. Confident and direct. Operator-empathetic. Solution-anchored. Growth-minded.',
        'www.diqit.com'
    ))
    brand_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Content pillars
    pillars = [
        ('Unified Operations', 'Eliminating system fragmentation with one platform', '#EF4324'),
        ('AI-Powered Intelligence', 'Demand forecasting, inventory, labour optimization', '#3B82F6'),
        ('APAC F&B Landscape', 'Technology trends across Asia-Pacific markets', '#10B981'),
        ('Multi-Store Scaling', 'Operational excellence across multiple locations', '#8B5CF6'),
        ('Self-Service & Kiosks', 'Customer experience innovation with self-order', '#F59E0B'),
        ('Digital Transformation', 'DX journeys for traditional F&B operators', '#EC4899'),
    ]
    for i, (name, desc, color) in enumerate(pillars):
        db.execute(
            "INSERT INTO content_pillars (brand_id, name, description, color, sort_order) VALUES (?,?,?,?,?)",
            (brand_id, name, desc, color, i)
        )

    # Workflow template
    db.execute("""
        INSERT INTO workflow_templates (brand_id, name, description, is_default)
        VALUES (?, ?, ?, 1)
    """, (brand_id, 'DIQIT Weekly Content Engine', 'End-to-end content workflow from research to feedback loop'))
    template_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Workflow steps — each with step_type (manual/automated/semi-auto), upload_folder, trigger_type
    steps = [
        {
            'name': 'Research & Deep Dive',
            'description': 'Deep research to ground content in real data and trends',
            'instructions': '1. Run Gemini Deep Research on the week\'s topics\n2. Check NotebookLM for synthesized themes\n3. Review Apify analytics from last cycle\n4. Review Drive Monitor reports from past week',
            'tools': json.dumps(['Gemini Deep Research', 'NotebookLM', 'Apify', 'Google Drive']),
            'day_of_week': 'Monday', 'duration_minutes': 15, 'sort_order': 1,
            'linked_folder': '08_Research',
            'checklist': json.dumps(['Run Deep Research on topics', 'Check NotebookLM notebooks', 'Pull Apify analytics', 'Review Drive Monitor reports']),
            'step_type': 'semi-auto', 'upload_folder': '08_Research/Deep_Research', 'trigger_type': 'claude',
            'brief_template': 'TOPIC: [topic]\nRESEARCH_SCOPE: market trends, competitor data, customer pain points\nSOURCES: Google Drive DIQIT docs, web, Apify analytics\nOUTPUT: Research brief with key data points and content angles',
        },
        {
            'name': 'Content Drafting',
            'description': 'Claude drafts all posts using brand voice, incorporating research insights',
            'instructions': '1. Claude reads content calendar from Drive\n2. Incorporates Deep Research + Apify insights\n3. Drafts posts using brand voice skill (specify market per post)\n4. Generates Napkin AI JSON for diagrams/infographics\n5. Saves drafts + JSON specs to Weekly Drafts folder',
            'tools': json.dumps(['Claude', 'Brand Voice Skill', 'Buyer-Led Content Writer', 'Google Drive']),
            'day_of_week': 'Monday', 'duration_minutes': 15, 'sort_order': 2,
            'linked_folder': '04_Content',
            'checklist': json.dumps(['Read content calendar', 'Draft all weekly posts', 'Generate Napkin AI JSON specs', 'Save to Weekly Drafts folder']),
            'step_type': 'automated', 'upload_folder': '04_Content/LinkedIn', 'trigger_type': 'claude',
            'brief_template': 'TOPIC: [topic]\nMARKET: [Japan/Singapore/APAC]\nCONTENT_TYPE: [linkedin_post/carousel/video]\nPILLAR: [content pillar]\nKEY_POINTS: [main points to cover]\nTONE: Professional yet approachable, operator-empathetic',
        },
        {
            'name': 'Visual Generation — Images',
            'description': 'Generate branded images using AI tools',
            'instructions': '1. Data visuals: Paste JSON into Napkin AI → pick best variant → export\n2. Images: ImageFX for generation → Canva for brand assembly\n3. Carousels: Claude → Canva MCP with DIQIT templates\n4. Add DIQIT logo, brand typography, footer bar in Canva',
            'tools': json.dumps(['Napkin AI', 'Google ImageFX', 'Canva', 'Gemini Image Generation']),
            'day_of_week': 'Tuesday', 'duration_minutes': 10, 'sort_order': 3,
            'linked_folder': '10_Pipeline/Briefs',
            'checklist': json.dumps(['Generate Napkin AI diagrams from JSON', 'Generate ImageFX hero images', 'Create carousel slides in Canva', 'Apply DIQIT branding (logo, colors, footer)']),
            'step_type': 'automated', 'upload_folder': '10_Pipeline/Generated_Images', 'trigger_type': 'gemini',
            'brief_template': 'TOPIC: [topic]\nMARKET: [market]\nIMAGE_STYLE: single\nVIDEO: no\nPOST_TEXT:\n[post text]',
        },
        {
            'name': 'Visual Generation — Video',
            'description': 'Create video content using Google Vids and Veo 3',
            'instructions': '1. Claude writes video script (30-120s for LinkedIn)\n2. Save script to Google Drive\n3. Open Google Vids → "Help me create" → point at script\n4. Customize avatar, voiceover, visuals\n5. Generate short hook clips via Veo 3/Flow',
            'tools': json.dumps(['Google Vids', 'Veo 3 / Flow', 'Claude']),
            'day_of_week': 'Tuesday', 'duration_minutes': 10, 'sort_order': 4,
            'linked_folder': '10_Pipeline/Briefs',
            'checklist': json.dumps(['Write video scripts', 'Generate storyboard in Google Vids', 'Create 8s hook clips via Veo 3', 'Export final videos']),
            'step_type': 'manual', 'upload_folder': '10_Pipeline/Generated_Videos', 'trigger_type': 'gemini',
            'brief_template': 'TOPIC: [topic]\nMARKET: [market]\nIMAGE_STYLE: single\nVIDEO: yes\nPOST_TEXT:\n[post text]',
        },
        {
            'name': 'Brand Assembly & Polish',
            'description': 'Apply brand kit and polish visuals in Canva via Content Studio',
            'instructions': '1. Open Content Studio → Canva Polish tab\n2. Select raw images and click "Create Polish Job"\n3. Claude Code runs Canva MCP tools to apply brand kit, logo, typography\n4. Review exported designs and run QC checklist\n5. For Japan market: extra minimal, maximum white space',
            'tools': json.dumps(['Canva MCP', 'Content Studio']),
            'day_of_week': 'Tuesday', 'duration_minutes': 10, 'sort_order': 5,
            'linked_folder': '10_Pipeline/Generated_Images',
            'checklist': json.dumps(['Create polish jobs in Content Studio', 'Run Canva MCP workflow', 'QC: Logo, brand colors, typography, dimensions', 'Export polished images', 'Save to Polished_Images folder']),
            'step_type': 'semi-auto', 'upload_folder': '10_Pipeline/Polished_Images', 'trigger_type': 'none',
            'brief_template': '',
        },
        {
            'name': 'Review & Approve',
            'description': 'Review all content, make edits, mark as approved',
            'instructions': '1. Review each post copy + visual/video in Drive\n2. Make any edits (use Gemini in Docs for quick rewrites)\n3. Mark as approved\n4. Create Google Calendar events for scheduled publishing times',
            'tools': json.dumps(['Google Drive', 'Google Calendar', 'Gemini in Docs']),
            'day_of_week': 'Tuesday-Wednesday', 'duration_minutes': 5, 'sort_order': 6,
            'linked_folder': '04_Content',
            'checklist': json.dumps(['Review all post copy', 'Review all visuals/videos', 'Make edits if needed', 'Mark approved', 'Schedule in Calendar']),
            'step_type': 'manual', 'upload_folder': '', 'trigger_type': 'none',
            'brief_template': '',
        },
        {
            'name': 'Client Review',
            'description': 'Generate review package for client approval — PDF with content, visuals, and schedule',
            'instructions': '1. Click "Export Review PDF" to generate a client-ready review package\n2. PDF includes all content items in review/ready status with visuals\n3. Share PDF with client for feedback\n4. Record client feedback in the app\n5. Apply revisions if needed, then mark approved',
            'tools': json.dumps(['Review PDF Export', 'Email']),
            'day_of_week': 'Wednesday', 'duration_minutes': 10, 'sort_order': 7,
            'linked_folder': '10_Pipeline/Client_Review',
            'checklist': json.dumps(['Export review PDF', 'Send to client', 'Record client feedback', 'Apply revisions if any', 'Get final approval']),
            'step_type': 'semi-auto', 'upload_folder': '10_Pipeline/Client_Review', 'trigger_type': 'none',
            'brief_template': '',
        },
        {
            'name': 'Publish',
            'description': 'Post content to LinkedIn via Claude in Chrome or native scheduler',
            'instructions': '1. Claude in Chrome opens LinkedIn → composes post → uploads visual\n2. You confirm and click Post (or schedule)\n3. Cadence: 3-4 posts/week DIQIT page, 2-3 for Neeraj\n4. Alternative: batch-upload and schedule via LinkedIn native',
            'tools': json.dumps(['Claude in Chrome', 'LinkedIn Scheduler', 'Make.com']),
            'day_of_week': 'Wednesday-Sunday', 'duration_minutes': 2, 'sort_order': 8,
            'linked_folder': '04_Content/LinkedIn',
            'checklist': json.dumps(['Open LinkedIn', 'Compose post with copy', 'Upload visual/video', 'Publish or schedule']),
            'step_type': 'semi-auto', 'upload_folder': '', 'trigger_type': 'claude',
            'brief_template': '',
        },
        {
            'name': 'Feedback Loop',
            'description': 'Apify scrapes performance → Claude analyzes → insights feed next cycle',
            'instructions': '1. Apify scrapes latest post performance\n2. Claude analyzes: what improved, what dropped\n3. Identify which visual types drove best engagement\n4. Monthly competitor scrape\n5. Insights feed into next week\'s planning',
            'tools': json.dumps(['Apify', 'Claude', 'Google Sheets']),
            'day_of_week': 'Bi-weekly Friday', 'duration_minutes': 20, 'sort_order': 9,
            'linked_folder': '07_Analytics',
            'checklist': json.dumps(['Run Apify scrape on recent posts', 'Analyze engagement trends', 'Compare with baseline', 'Generate recommendations', 'Update content strategy']),
            'step_type': 'automated', 'upload_folder': '07_Analytics', 'trigger_type': 'apify_claude',
            'brief_template': '',
        },
    ]

    step_ids = []
    for step in steps:
        db.execute("""
            INSERT INTO workflow_steps (template_id, name, description, instructions, tools, day_of_week,
            duration_minutes, sort_order, linked_folder, checklist, step_type, upload_folder, brief_template, trigger_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            template_id, step['name'], step['description'], step['instructions'],
            step['tools'], step['day_of_week'], step['duration_minutes'],
            step['sort_order'], step['linked_folder'], step['checklist'],
            step['step_type'], step['upload_folder'], step['brief_template'], step['trigger_type']
        ))
        step_ids.append(db.execute("SELECT last_insert_rowid()").fetchone()[0])

    # Cadence rules
    db.execute("""
        INSERT INTO cadence_rules (brand_id, name, channel, posts_per_week, preferred_days, preferred_times, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (brand_id, 'DIQIT LinkedIn Page', 'LinkedIn', 4,
          json.dumps(['Monday', 'Wednesday', 'Thursday', 'Saturday']),
          json.dumps(['09:00', '12:00', '17:00']),
          '3-4 posts/week on DIQIT company page'))

    db.execute("""
        INSERT INTO cadence_rules (brand_id, name, channel, posts_per_week, preferred_days, preferred_times, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (brand_id, 'Neeraj Personal Brand', 'LinkedIn', 3,
          json.dumps(['Tuesday', 'Thursday', 'Sunday']),
          json.dumps(['08:00', '12:00', '18:00']),
          '2-3 posts/week on Neeraj\'s personal profile'))

    # Folder mappings
    folder_maps = [
        ('01_Brand', step_ids[4], 'Brand assets — logos, colors, guidelines'),
        ('02_Product', step_ids[0], 'Product docs — POSTAP features, specs'),
        ('03_Strategy', None, 'Strategy docs, content calendars, proposals'),
        ('04_Content', step_ids[6], 'Published content output'),
        ('05_Sales', None, 'Outreach playbooks, leads, prospects'),
        ('06_Neeraj_LinkedIn', None, 'CEO personal brand content'),
        ('07_Analytics', step_ids[7], 'Performance reports, analytics'),
        ('08_Research', step_ids[0], 'Market research, competitor data'),
        ('09_Admin', None, 'Contracts, invoices, admin docs'),
        ('10_Pipeline', step_ids[2], 'Content automation pipeline'),
    ]
    for folder, step_id, purpose in folder_maps:
        db.execute(
            "INSERT INTO folder_mappings (brand_id, folder_path, workflow_step_id, purpose) VALUES (?,?,?,?)",
            (brand_id, folder, step_id, purpose)
        )

    # Sample notifications
    today = datetime.now().strftime('%Y-%m-%d')
    notifications = [
        ('task', 'Weekly Content Batch', 'Start research and draft all posts for this week', today),
        ('reminder', 'Apify Feedback Loop', 'Bi-weekly performance scrape due this Friday', today),
        ('system', 'Welcome to DigitalAIzeme', 'DIQIT brand has been configured. Start by reviewing the workflow.', today),
    ]
    for ntype, title, msg, due in notifications:
        db.execute(
            "INSERT INTO notifications (brand_id, notification_type, title, message, due_date) VALUES (?,?,?,?,?)",
            (brand_id, ntype, title, msg, due)
        )

    db.commit()


# ─── Media Tag Registry ──────────────────────────────────────────────

MEDIA_TAG_REGISTRY = {
    'stock_image': {
        'tool_name': 'stock_search',
        'auto_capable': True,
        'run_at_status': 'visuals',
        'api_keys_needed': ['unsplash_api_key', 'pexels_api_key'],  # either/or
        'api_keys_mode': 'any',
        'description': 'Search & download stock images',
        'icon': 'fa-camera',
        'color': 'pink',
    },
    'tts': {
        'tool_name': 'tts',
        'auto_capable': True,
        'run_at_status': 'ready',
        'api_keys_needed': ['elevenlabs_api_key'],
        'api_keys_mode': 'all',
        'description': 'Generate audio narration',
        'icon': 'fa-microphone',
        'color': 'purple',
    },
    'translate_ja': {
        'tool_name': 'translate',
        'auto_capable': True,
        'run_at_status': 'drafting',
        'api_keys_needed': ['google_api_key'],
        'api_keys_mode': 'all',
        'description': 'Translate to Japanese',
        'icon': 'fa-language',
        'color': 'blue',
    },
    'translate_vi': {
        'tool_name': 'translate',
        'auto_capable': True,
        'run_at_status': 'drafting',
        'api_keys_needed': ['google_api_key'],
        'api_keys_mode': 'all',
        'description': 'Translate to Vietnamese',
        'icon': 'fa-language',
        'color': 'blue',
    },
    'translate_zh': {
        'tool_name': 'translate',
        'auto_capable': True,
        'run_at_status': 'drafting',
        'api_keys_needed': ['google_api_key'],
        'api_keys_mode': 'all',
        'description': 'Translate to Chinese',
        'icon': 'fa-language',
        'color': 'blue',
    },
    'remove_bg': {
        'tool_name': 'remove_bg',
        'auto_capable': True,
        'run_at_status': 'visuals',
        'api_keys_needed': ['removebg_api_key'],
        'api_keys_mode': 'all',
        'description': 'Remove image background',
        'icon': 'fa-eraser',
        'color': 'green',
    },
    'qr_code': {
        'tool_name': 'qr',
        'auto_capable': True,
        'run_at_status': 'ready',
        'api_keys_needed': [],
        'api_keys_mode': 'all',
        'description': 'Generate QR code',
        'icon': 'fa-qrcode',
        'color': 'teal',
    },
    'short_url': {
        'tool_name': 'shorten_url',
        'auto_capable': True,
        'run_at_status': 'published',
        'api_keys_needed': [],
        'api_keys_mode': 'all',
        'description': 'Shorten URL',
        'icon': 'fa-link',
        'color': 'orange',
    },
    'diagram': {
        'tool_name': 'mermaid',
        'auto_capable': False,
        'description': 'Mermaid diagram (manual)',
        'icon': 'fa-project-diagram',
        'color': 'indigo',
    },
    'chart': {
        'tool_name': 'chartjs',
        'auto_capable': False,
        'description': 'Data chart (manual)',
        'icon': 'fa-chart-bar',
        'color': 'yellow',
    },
    'excalidraw': {
        'tool_name': 'excalidraw',
        'auto_capable': False,
        'description': 'Excalidraw diagram (manual MCP)',
        'icon': 'fa-pencil-ruler',
        'color': 'cyan',
    },
}


# ─── Routes: Dashboard ─────────────────────────────────────────────

@app.route('/')
def dashboard():
    db = get_db()
    brands = db.execute("SELECT * FROM brands").fetchall()

    stats = {}
    for brand in brands:
        bid = brand['id']
        stats[bid] = {
            'total_content': db.execute("SELECT COUNT(*) FROM content_items WHERE brand_id=?", (bid,)).fetchone()[0],
            'in_progress': db.execute("SELECT COUNT(*) FROM content_items WHERE brand_id=? AND status NOT IN ('published','analyzed','backlog')", (bid,)).fetchone()[0],
            'published': db.execute("SELECT COUNT(*) FROM content_items WHERE brand_id=? AND status='published'", (bid,)).fetchone()[0],
            'pending_feedback': db.execute("SELECT COUNT(*) FROM feedback f JOIN content_items c ON f.content_item_id=c.id WHERE c.brand_id=? AND f.status='pending'", (bid,)).fetchone()[0],
        }

    # Auto-generate overdue notifications on dashboard load
    _auto_generate_overdue(db)

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    recent_notifications = db.execute("SELECT * FROM notifications WHERE is_read=0 ORDER BY created_at DESC LIMIT 5").fetchall()

    today_tasks = generate_today_tasks(db)
    last_brand = db.execute("SELECT * FROM brands ORDER BY id LIMIT 1").fetchone()

    return render_template('dashboard.html',
        brands=brands, stats=stats,
        unread_notifications=unread_notifications,
        recent_notifications=recent_notifications,
        today_tasks=today_tasks, last_brand=last_brand)


def _auto_generate_overdue(db):
    """Silently create overdue notifications for content past publish_date."""
    today_str = datetime.now().strftime('%Y-%m-%d')
    brands = db.execute("SELECT * FROM brands").fetchall()
    for brand in brands:
        bid = brand['id']
        overdue = db.execute("""
            SELECT * FROM content_items
            WHERE brand_id=? AND publish_date < ? AND status NOT IN ('published','analyzed')
        """, (bid, today_str)).fetchall()
        for item in overdue:
            existing = db.execute("""
                SELECT COUNT(*) FROM notifications
                WHERE content_item_id=? AND notification_type='overdue' AND due_date=?
            """, (item['id'], today_str)).fetchone()[0]
            if existing == 0:
                days_overdue = (datetime.now() - datetime.strptime(item['publish_date'], '%Y-%m-%d')).days
                db.execute("""
                    INSERT INTO notifications (brand_id, content_item_id, notification_type, title, message, due_date)
                    VALUES (?, ?, 'overdue', ?, ?, ?)
                """, (bid, item['id'],
                      f'OVERDUE: {item["title"]}',
                      f'{days_overdue} days overdue (due {item["publish_date"]}). Status: {item["status"]}',
                      today_str))
    db.commit()


def generate_today_tasks(db):
    """Generate tasks based on cadence rules and day of week."""
    today = datetime.now()
    day_name = today.strftime('%A')
    tasks = []

    brands = db.execute("SELECT * FROM brands").fetchall()
    for brand in brands:
        bid = brand['id']
        # Check workflow steps for today
        steps = db.execute("""
            SELECT ws.* FROM workflow_steps ws
            JOIN workflow_templates wt ON ws.template_id = wt.id
            WHERE wt.brand_id = ? AND ws.day_of_week LIKE ?
        """, (bid, f'%{day_name}%')).fetchall()

        for step in steps:
            tasks.append({
                'brand_name': brand['name'],
                'brand_color': brand['accent_color'],
                'task': step['name'],
                'description': step['description'],
                'duration': step['duration_minutes'],
                'tools': json.loads(step['tools']) if step['tools'] else [],
            })

        # Check for overdue content
        overdue = db.execute("""
            SELECT COUNT(*) FROM content_items
            WHERE brand_id=? AND publish_date < ? AND status NOT IN ('published','analyzed')
        """, (bid, today.strftime('%Y-%m-%d'))).fetchone()[0]
        if overdue > 0:
            tasks.append({
                'brand_name': brand['name'],
                'brand_color': '#EF4444',
                'task': f'{overdue} Overdue Items',
                'description': f'{overdue} content items past their publish date',
                'duration': 0,
                'tools': [],
            })

    return tasks


# ─── Routes: Brands ────────────────────────────────────────────────

@app.route('/brands')
def brands_list():
    db = get_db()
    brands = db.execute("SELECT * FROM brands ORDER BY name").fetchall()
    stats = {}
    for brand in brands:
        bid = brand['id']
        stats[bid] = {
            'pillars': db.execute("SELECT COUNT(*) FROM content_pillars WHERE brand_id=?", (bid,)).fetchone()[0],
            'content': db.execute("SELECT COUNT(*) FROM content_items WHERE brand_id=?", (bid,)).fetchone()[0],
            'published': db.execute("SELECT COUNT(*) FROM content_items WHERE brand_id=? AND status='published'", (bid,)).fetchone()[0],
        }
    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    return render_template('brands/list.html', brands=brands, stats=stats, unread_notifications=unread_notifications)


@app.route('/brands/new', methods=['GET', 'POST'])
def brand_new():
    db = get_db()
    if request.method == 'POST':
        db.execute("""
            INSERT INTO brands (name, tagline, primary_color, accent_color, folder_path, voice_summary, website)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            request.form['name'], request.form.get('tagline', ''),
            request.form.get('primary_color', '#000000'),
            request.form.get('accent_color', '#6366f1'),
            request.form.get('folder_path', ''),
            request.form.get('voice_summary', ''),
            request.form.get('website', ''),
        ))
        brand_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Create default workflow template
        db.execute("""
            INSERT INTO workflow_templates (brand_id, name, description, is_default)
            VALUES (?, ?, ?, 1)
        """, (brand_id, f'{request.form["name"]} Content Workflow', 'Default content workflow'))

        # Auto-create folder structure if folder_path is provided
        folder_path = request.form.get('folder_path', '').strip()
        folders_created = []
        if folder_path:
            for folder_name, purpose in DIQIT_FOLDER_TEMPLATE:
                full_path = os.path.join(folder_path, folder_name)
                if not os.path.exists(full_path):
                    os.makedirs(full_path, exist_ok=True)
                    folders_created.append(folder_name)

        db.commit()
        return redirect(url_for('brand_detail', brand_id=brand_id))

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    return render_template('brands/new.html', unread_notifications=unread_notifications)


@app.route('/brands/<int:brand_id>')
def brand_detail(brand_id):
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return redirect(url_for('brands_list'))

    pillars = db.execute("SELECT * FROM content_pillars WHERE brand_id=? ORDER BY sort_order", (brand_id,)).fetchall()
    recent_content = db.execute("SELECT * FROM content_items WHERE brand_id=? ORDER BY created_at DESC LIMIT 10", (brand_id,)).fetchall()
    cadences = db.execute("SELECT * FROM cadence_rules WHERE brand_id=?", (brand_id,)).fetchall()
    workflow = db.execute("SELECT * FROM workflow_templates WHERE brand_id=? AND is_default=1", (brand_id,)).fetchone()

    content_stats = {
        'total': db.execute("SELECT COUNT(*) FROM content_items WHERE brand_id=?", (brand_id,)).fetchone()[0],
        'backlog': db.execute("SELECT COUNT(*) FROM content_items WHERE brand_id=? AND status='backlog'", (brand_id,)).fetchone()[0],
        'in_progress': db.execute("SELECT COUNT(*) FROM content_items WHERE brand_id=? AND status NOT IN ('published','analyzed','backlog')", (brand_id,)).fetchone()[0],
        'published': db.execute("SELECT COUNT(*) FROM content_items WHERE brand_id=? AND status='published'", (brand_id,)).fetchone()[0],
    }

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    return render_template('brands/detail.html',
        brand=brand, pillars=pillars, recent_content=recent_content,
        cadences=cadences, workflow=workflow, content_stats=content_stats,
        unread_notifications=unread_notifications)


# ─── Routes: Workflow ───────────────────────────────────────────────

@app.route('/brands/<int:brand_id>/workflow')
def workflow_view(brand_id):
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    template = db.execute("SELECT * FROM workflow_templates WHERE brand_id=? AND is_default=1", (brand_id,)).fetchone()
    steps = []
    if template:
        steps = db.execute("SELECT * FROM workflow_steps WHERE template_id=? ORDER BY sort_order", (template['id'],)).fetchall()
    folder_mappings = db.execute("SELECT * FROM folder_mappings WHERE brand_id=?", (brand_id,)).fetchall()
    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    return render_template('workflow/view.html',
        brand=brand, template=template, steps=steps,
        folder_mappings=folder_mappings, unread_notifications=unread_notifications)


@app.route('/api/workflow-steps/<int:step_id>', methods=['PUT'])
def update_workflow_step(step_id):
    db = get_db()
    data = request.json
    db.execute("""
        UPDATE workflow_steps SET name=?, description=?, instructions=?, tools=?,
        day_of_week=?, duration_minutes=?, checklist=?,
        step_type=COALESCE(?, step_type), trigger_type=COALESCE(?, trigger_type),
        linked_folder=COALESCE(?, linked_folder), upload_folder=COALESCE(?, upload_folder)
        WHERE id=?
    """, (
        data['name'], data['description'], data['instructions'],
        json.dumps(data.get('tools', [])), data['day_of_week'],
        data['duration_minutes'], json.dumps(data.get('checklist', [])),
        data.get('step_type'), data.get('trigger_type'),
        data.get('linked_folder'), data.get('upload_folder'),
        step_id
    ))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/workflow-steps', methods=['POST'])
def create_workflow_step():
    """Add a new step to a workflow template."""
    db = get_db()
    data = request.json
    template_id = data['template_id']
    # Get max sort_order for this template
    max_order = db.execute("SELECT COALESCE(MAX(sort_order),0) FROM workflow_steps WHERE template_id=?",
                           (template_id,)).fetchone()[0]
    db.execute("""
        INSERT INTO workflow_steps (template_id, name, description, instructions, tools,
            day_of_week, duration_minutes, sort_order, linked_folder, checklist,
            step_type, upload_folder, brief_template, trigger_type)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        template_id,
        data.get('name', 'New Step'),
        data.get('description', ''),
        data.get('instructions', ''),
        json.dumps(data.get('tools', [])),
        data.get('day_of_week', ''),
        data.get('duration_minutes', 15),
        max_order + 1,
        data.get('linked_folder', ''),
        json.dumps(data.get('checklist', [])),
        data.get('step_type', 'manual'),
        data.get('upload_folder', ''),
        data.get('brief_template', ''),
        data.get('trigger_type', 'none'),
    ))
    db.commit()
    new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({'ok': True, 'id': new_id})


@app.route('/api/workflow-steps/<int:step_id>', methods=['DELETE'])
def delete_workflow_step(step_id):
    """Delete a workflow step."""
    db = get_db()
    step = db.execute("SELECT template_id, sort_order FROM workflow_steps WHERE id=?", (step_id,)).fetchone()
    if not step:
        return jsonify({'error': 'Step not found'}), 404
    db.execute("DELETE FROM workflow_steps WHERE id=?", (step_id,))
    # Reindex sort_order
    remaining = db.execute("SELECT id FROM workflow_steps WHERE template_id=? ORDER BY sort_order",
                           (step['template_id'],)).fetchall()
    for i, row in enumerate(remaining):
        db.execute("UPDATE workflow_steps SET sort_order=? WHERE id=?", (i + 1, row['id']))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/workflow-steps/reorder', methods=['PUT'])
def reorder_workflow_steps():
    """Reorder workflow steps within a template."""
    db = get_db()
    data = request.json
    for item in data.get('order', []):
        db.execute("UPDATE workflow_steps SET sort_order=? WHERE id=?", (item['sort_order'], item['id']))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/workflow-steps/<int:step_id>/brief')
def get_workflow_step_brief(step_id):
    """Get the brief template for a workflow step."""
    db = get_db()
    step = db.execute("SELECT brief_template, instructions FROM workflow_steps WHERE id=?", (step_id,)).fetchone()
    if not step:
        return jsonify({'error': 'Step not found'}), 404
    return jsonify({'brief_template': step['brief_template'] or '', 'instructions': step['instructions'] or ''})


# ─── Routes: Pipeline (Kanban) ─────────────────────────────────────

@app.route('/brands/<int:brand_id>/pipeline')
def pipeline_view(brand_id):
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    content_items = db.execute("""
        SELECT ci.*, cp.name as pillar_name, cp.color as pillar_color
        FROM content_items ci
        LEFT JOIN content_pillars cp ON ci.pillar_id = cp.id
        WHERE ci.brand_id = ?
        ORDER BY ci.created_at DESC
    """, (brand_id,)).fetchall()
    pillars = db.execute("SELECT * FROM content_pillars WHERE brand_id=? ORDER BY sort_order", (brand_id,)).fetchall()
    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]

    # Group by status
    columns = ['backlog', 'research', 'drafting', 'visuals', 'review', 'ready', 'published', 'analyzed']
    pipeline = {col: [] for col in columns}
    for item in content_items:
        status = item['status'] if item['status'] in columns else 'backlog'
        pipeline[status].append(item)

    return render_template('pipeline/view.html',
        brand=brand, pipeline=pipeline, columns=columns,
        pillars=pillars, unread_notifications=unread_notifications)


@app.route('/api/content-items', methods=['POST'])
def create_content_item():
    db = get_db()
    data = request.json
    db.execute("""
        INSERT INTO content_items (brand_id, pillar_id, title, content_type, market, status, body_text, publish_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data['brand_id'], data.get('pillar_id'),
        data['title'], data.get('content_type', 'linkedin_post'),
        data.get('market', 'APAC'), 'backlog',
        data.get('body_text', ''), data.get('publish_date')
    ))
    db.commit()
    return jsonify({'ok': True, 'id': db.execute("SELECT last_insert_rowid()").fetchone()[0]})


@app.route('/api/content-items/<int:item_id>/status', methods=['PUT'])
def update_content_status(item_id):
    db = get_db()
    data = request.json
    db.execute("UPDATE content_items SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (data['status'], item_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/content-items/<int:item_id>/generate-draft', methods=['POST'])
def generate_content_draft(item_id):
    """Use Claude to generate the actual content draft for a content item."""
    db = get_db()
    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Claude API key not configured. Go to Settings to add it.'}), 400

    item = db.execute("SELECT * FROM content_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        return jsonify({'ok': False, 'error': 'Content item not found'}), 404

    brand = db.execute("SELECT * FROM brands WHERE id=?", (item['brand_id'],)).fetchone()
    pillar = db.execute("SELECT * FROM content_pillars WHERE id=?", (item['pillar_id'],)).fetchone() if item['pillar_id'] else None

    # Get brand voice references
    voice_refs = db.execute("SELECT * FROM brand_voice_references WHERE brand_id=?", (item['brand_id'],)).fetchall()
    voice_info = '\n'.join([f"- {v['ref_type']}: {v['content'][:200]}" for v in voice_refs]) if voice_refs else ''

    # Content type specific instructions
    type_guides = {
        'linkedin_post': 'Write a LinkedIn post (150-300 words). Use a strong hook in the first line. Include line breaks for readability. End with a clear CTA or question. Add 3-5 relevant hashtags.',
        'carousel': 'Write a LinkedIn carousel (8-10 slides). Format as:\n\nSLIDE 1 (Cover): [Bold headline]\nSLIDE 2-8: [One key point per slide, 20-30 words each]\nSLIDE 9 (CTA): [Call to action]\n\nAlso write a caption (100-150 words) with hashtags.',
        'blog': 'Write a blog post (800-1200 words). Include: compelling title, introduction with hook, 3-5 subheadings with content under each, conclusion with CTA. Use data points and examples.',
        'video': 'Write a video script (60-90 seconds). Format as:\n\nHOOK (0-5s): [attention grabber]\nPROBLEM (5-20s): [pain point]\nSOLUTION (20-50s): [how it helps]\nPROOF (50-70s): [stats/example]\nCTA (70-90s): [what to do next]\n\nAlso write a short caption for posting.',
        'email': 'Write a marketing email. Include: subject line, preview text, greeting, body (200-400 words), CTA button text, sign-off. Professional but warm tone.',
        'reel': 'Write a short-form video script (15-30 seconds). Format as:\n\nHOOK (0-3s): [stop the scroll]\nCONTENT (3-25s): [3-4 quick points]\nCTA (25-30s): [what to do]\n\nAlso write a caption with hashtags.',
    }
    type_guide = type_guides.get(item['content_type'], type_guides['linkedin_post'])

    prompt = f"""You are a content creator for "{brand['name']}".

Brand voice: {brand['voice_summary'] or 'Professional yet approachable'}
{f"Content pillar: {pillar['name']} — {pillar['description'] or ''}" if pillar else ''}
Target market: {item['market']}

{f"Brand voice references:{chr(10)}{voice_info}" if voice_info else ''}

Content brief:
- Title: {item['title']}
- Type: {item['content_type']}
- Notes: {item['notes'] or 'None'}

{type_guide}

Write the content now. Output ONLY the final content — no preamble, no "here's the content", just the content itself ready for publishing."""

    try:
        import urllib.request
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 4096,
                'messages': [{'role': 'user', 'content': prompt}]
            }).encode(),
            headers={
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01'
            }
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            draft_text = result['content'][0]['text'].strip()

        # Save the draft and advance status to 'drafting'
        new_status = 'drafting' if item['status'] in ('backlog', 'research') else item['status']
        db.execute("""
            UPDATE content_items SET body_text=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (draft_text, new_status, item_id))
        db.commit()

        return jsonify({'ok': True, 'draft': draft_text, 'status': new_status})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            msg = json.loads(body).get('error', {}).get('message', body)
        except Exception:
            msg = body
        if 'credit balance' in msg.lower() or 'billing' in msg.lower():
            return jsonify({'ok': False, 'error': 'Anthropic API credit balance is too low. Go to console.anthropic.com → Plans & Billing to add credits.'}), 402
        return jsonify({'ok': False, 'error': f'Claude API error: {msg}'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/content-items/<int:item_id>/complete-step', methods=['POST'])
def complete_step_and_chain(item_id):
    """Complete the current workflow step and auto-advance to the next one (step chaining)."""
    db = get_db()
    data = request.json or {}
    step_id = data.get('step_id')

    item = db.execute("SELECT * FROM content_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        return jsonify({'ok': False, 'error': 'Content item not found'}), 404

    template_id = item['workflow_template_id']
    if not template_id:
        # Try to find default template for the brand
        template = db.execute("SELECT * FROM workflow_templates WHERE brand_id=? AND is_default=1",
                              (item['brand_id'],)).fetchone()
        if template:
            template_id = template['id']
            db.execute("UPDATE content_items SET workflow_template_id=? WHERE id=?", (template_id, item_id))
        else:
            return jsonify({'ok': False, 'error': 'No workflow template assigned'}), 400

    steps = db.execute("SELECT * FROM workflow_steps WHERE template_id=? ORDER BY sort_order",
                       (template_id,)).fetchall()
    if not steps:
        return jsonify({'ok': False, 'error': 'No workflow steps defined'}), 400

    # Find the current step (either specified or the first incomplete one)
    current_step = None
    current_idx = -1
    if step_id:
        for i, s in enumerate(steps):
            if s['id'] == step_id:
                current_step = s
                current_idx = i
                break
    else:
        # Find the first non-completed step
        for i, s in enumerate(steps):
            progress = db.execute("""
                SELECT * FROM content_step_progress WHERE content_item_id=? AND workflow_step_id=?
            """, (item_id, s['id'])).fetchone()
            if not progress or progress['status'] != 'completed':
                current_step = s
                current_idx = i
                break

    if not current_step:
        return jsonify({'ok': False, 'error': 'All steps already completed'}), 400

    # Mark current step as completed
    existing = db.execute("""
        SELECT id FROM content_step_progress WHERE content_item_id=? AND workflow_step_id=?
    """, (item_id, current_step['id'])).fetchone()
    if existing:
        db.execute("""
            UPDATE content_step_progress SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?
        """, (existing['id'],))
    else:
        db.execute("""
            INSERT INTO content_step_progress (content_item_id, workflow_step_id, status, started_at, completed_at)
            VALUES (?, ?, 'completed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (item_id, current_step['id']))

    # Auto-advance: start next step
    next_step = None
    if current_idx + 1 < len(steps):
        next_step = steps[current_idx + 1]
        db.execute("""
            INSERT OR IGNORE INTO content_step_progress (content_item_id, workflow_step_id, status, started_at)
            VALUES (?, ?, 'in_progress', CURRENT_TIMESTAMP)
        """, (item_id, next_step['id']))

    # Advance the content item status through the pipeline
    status_flow = ['backlog', 'research', 'drafting', 'visuals', 'review', 'ready']
    new_status = status_flow[min(current_idx + 1, len(status_flow) - 1)]
    if current_idx + 1 >= len(steps):
        new_status = 'ready'
    db.execute("UPDATE content_items SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (new_status, item_id))

    # Auto-run tools that are triggered at this status
    tool_results = _auto_run_tools_for_status(item_id, new_status, db)

    # Create task notification for next step if it exists
    if next_step:
        trigger = next_step['trigger_type'] or 'none'
        if trigger == 'claude':
            action_type, action_label = 'trigger_claude', 'Run with Claude'
        elif trigger == 'apify_claude':
            action_type, action_label = 'trigger_apify', 'Scrape & Analyze'
        elif trigger in ('apps_script', 'gemini'):
            action_type, action_label = 'trigger_gemini', 'Generate with Gemini'
        else:
            action_type, action_label = 'go_to_page', 'Open Step'

        db.execute("""
            INSERT INTO notifications (brand_id, content_item_id, notification_type, title, message,
                due_date, action_type, action_url, action_label, step_id, step_type, duration_minutes)
            VALUES (?, ?, 'task', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (item['brand_id'], item_id, f'Next: {next_step["name"]}',
              f'"{item["title"]}" advanced to step: {next_step["name"]}. {next_step["description"] or ""}',
              datetime.now().strftime('%Y-%m-%d'),
              action_type, f'/brands/{item["brand_id"]}/workflow', action_label,
              next_step['id'], next_step['step_type'] or 'manual',
              next_step['duration_minutes'] or 0))

    db.commit()
    return jsonify({
        'ok': True,
        'completed_step': current_step['name'],
        'next_step': next_step['name'] if next_step else None,
        'new_status': new_status,
        'all_done': next_step is None,
        'tool_results': tool_results
    })


@app.route('/api/content-items/<int:item_id>', methods=['PUT'])
def update_content_item(item_id):
    db = get_db()
    data = request.json
    fields = []
    values = []
    for key in ['title', 'body_text', 'content_type', 'market', 'status', 'visual_prompt', 'video_prompt', 'napkin_json', 'publish_date', 'notes', 'pillar_id']:
        if key in data:
            fields.append(f"{key}=?")
            values.append(data[key])
    if fields:
        fields.append("updated_at=CURRENT_TIMESTAMP")
        values.append(item_id)
        db.execute(f"UPDATE content_items SET {', '.join(fields)} WHERE id=?", values)
        db.commit()
    return jsonify({'ok': True})


@app.route('/api/content-items/<int:item_id>', methods=['DELETE'])
def delete_content_item(item_id):
    db = get_db()
    db.execute("DELETE FROM content_items WHERE id=?", (item_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Routes: Content Preview + Feedback (Unified) ─────────────────

def scan_media_files(folder_path):
    """Scan brand folders for media files."""
    media_files = []
    if not folder_path or not os.path.exists(folder_path):
        return media_files
    for subdir in ['04_Content', '10_Pipeline/Generated_Images', '10_Pipeline/Generated_Videos', '10_Pipeline/Polished_Images']:
        full_path = os.path.join(folder_path, subdir)
        if not os.path.exists(full_path):
            continue
        for root, dirs, files in os.walk(full_path):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg',
                           '.mp4', '.mov', '.webm', '.avi',
                           '.mp3', '.wav', '.m4a', '.pdf']:
                    fpath = os.path.join(root, f)
                    rel_path = os.path.relpath(fpath, folder_path)
                    # Detect carousel (multiple numbered images in same folder with similar name)
                    is_carousel = False
                    if ext in ['.png', '.jpg', '.jpeg', '.webp']:
                        import re
                        base_name = re.sub(r'[-_]?\d+$', '', os.path.splitext(f)[0])
                        siblings = [sf for sf in files if sf != f and os.path.splitext(sf)[1].lower() in ['.png','.jpg','.jpeg','.webp']
                                    and re.sub(r'[-_]?\d+$', '', os.path.splitext(sf)[0]) == base_name]
                        is_carousel = len(siblings) >= 1

                    media_files.append({
                        'name': f,
                        'path': fpath,
                        'rel_path': rel_path,
                        'type': 'video' if ext in ['.mp4', '.mov', '.webm', '.avi'] else
                                'audio' if ext in ['.mp3', '.wav', '.m4a'] else
                                'image' if ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'] else 'document',
                        'size': os.path.getsize(fpath),
                        'modified': datetime.fromtimestamp(os.path.getmtime(fpath)).strftime('%Y-%m-%d %H:%M'),
                        'ext': ext,
                        'folder': os.path.relpath(root, folder_path),
                        'is_carousel': is_carousel,
                    })
    media_files.sort(key=lambda x: x['modified'], reverse=True)
    return media_files


@app.route('/brands/<int:brand_id>/preview')
def content_preview(brand_id):
    """Unified content preview + feedback + edit view."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return redirect(url_for('brands_list'))

    content_items = db.execute("""
        SELECT ci.*, cp.name as pillar_name, cp.color as pillar_color
        FROM content_items ci
        LEFT JOIN content_pillars cp ON ci.pillar_id = cp.id
        WHERE ci.brand_id = ?
        ORDER BY ci.updated_at DESC
    """, (brand_id,)).fetchall()

    feedbacks = db.execute("""
        SELECT f.*, ci.title as content_title
        FROM feedback f
        JOIN content_items ci ON f.content_item_id = ci.id
        WHERE ci.brand_id = ?
        ORDER BY f.created_at DESC LIMIT 20
    """, (brand_id,)).fetchall()

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    media_files = scan_media_files(brand['folder_path'])
    content_items_dicts = [dict(row) for row in content_items]
    pillars_rows = db.execute("SELECT * FROM content_pillars WHERE brand_id=? ORDER BY sort_order", (brand_id,)).fetchall()
    pillars = [dict(row) for row in pillars_rows]

    # Group carousel images by folder+base name
    carousel_groups = {}
    for mf in media_files:
        if mf['is_carousel']:
            import re
            base = re.sub(r'[-_]?\d+$', '', os.path.splitext(mf['name'])[0])
            key = mf['folder'] + '/' + base
            if key not in carousel_groups:
                carousel_groups[key] = []
            carousel_groups[key].append(mf)

    # Canva designs for the brand
    canva_designs = [dict(row) for row in db.execute("""
        SELECT cd.*, ci.title as content_title
        FROM canva_designs cd
        LEFT JOIN content_items ci ON cd.content_item_id = ci.id
        WHERE cd.brand_id = ?
        ORDER BY cd.created_at DESC
    """, (brand_id,)).fetchall()]

    return render_template('preview/unified.html',
        brand=brand, content_items=content_items, content_items_dicts=content_items_dicts,
        feedbacks=feedbacks, media_files=media_files, carousel_groups=carousel_groups,
        pillars=pillars, unread_notifications=unread_notifications,
        canva_designs=canva_designs)


@app.route('/brands/<int:brand_id>/feedback')
def feedback_view(brand_id):
    """Redirect to unified preview+feedback view."""
    return redirect(url_for('content_preview', brand_id=brand_id))


@app.route('/media/<path:filepath>')
def serve_media(filepath):
    """Serve local media files for preview."""
    full_path = os.path.join(BRANDS_BASE, filepath)
    if os.path.exists(full_path):
        directory = os.path.dirname(full_path)
        filename = os.path.basename(full_path)
        return send_from_directory(directory, filename)
    return 'File not found', 404


@app.route('/api/upload/<int:brand_id>/<path:target_folder>', methods=['POST'])
def upload_file(brand_id, target_folder):
    """Upload a finished product file to the correct brand folder."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'error': 'Brand folder not configured'}), 400

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    dest_folder = os.path.join(brand['folder_path'], target_folder)
    os.makedirs(dest_folder, exist_ok=True)

    filepath = os.path.join(dest_folder, file.filename)
    file.save(filepath)

    return jsonify({
        'ok': True,
        'filename': file.filename,
        'path': filepath,
        'folder': target_folder,
    })


# ─── Routes: Analytics ─────────────────────────────────────────────

@app.route('/brands/<int:brand_id>/analytics')
def analytics_view(brand_id):
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return redirect(url_for('brands_list'))

    # Content stats by status
    status_counts = db.execute("""
        SELECT status, COUNT(*) as cnt FROM content_items WHERE brand_id=? GROUP BY status
    """, (brand_id,)).fetchall()
    status_data = {row['status']: row['cnt'] for row in status_counts}

    # Content by type
    type_counts = db.execute("""
        SELECT content_type, COUNT(*) as cnt FROM content_items WHERE brand_id=? GROUP BY content_type
    """, (brand_id,)).fetchall()
    type_data = {row['content_type']: row['cnt'] for row in type_counts}

    # Content by pillar
    pillar_counts = db.execute("""
        SELECT cp.name, cp.color, COUNT(ci.id) as cnt
        FROM content_pillars cp
        LEFT JOIN content_items ci ON ci.pillar_id = cp.id
        WHERE cp.brand_id = ?
        GROUP BY cp.id ORDER BY cnt DESC
    """, (brand_id,)).fetchall()
    pillar_data = [dict(row) for row in pillar_counts]

    # Monthly output (last 6 months)
    monthly_output = db.execute("""
        SELECT strftime('%Y-%m', created_at) as month, COUNT(*) as cnt
        FROM content_items WHERE brand_id=?
        GROUP BY month ORDER BY month DESC LIMIT 6
    """, (brand_id,)).fetchall()
    monthly_data = [dict(row) for row in reversed(monthly_output)]

    # Published per month
    monthly_published = db.execute("""
        SELECT strftime('%Y-%m', published_at) as month, COUNT(*) as cnt
        FROM content_items WHERE brand_id=? AND published_at IS NOT NULL
        GROUP BY month ORDER BY month DESC LIMIT 6
    """, (brand_id,)).fetchall()
    published_data = [dict(row) for row in reversed(monthly_published)]

    # Analytics snapshots (engagement data)
    snapshots = db.execute("""
        SELECT a.*, ci.title as content_title
        FROM analytics_snapshots a
        LEFT JOIN content_items ci ON a.content_item_id = ci.id
        WHERE a.brand_id=?
        ORDER BY a.snapshot_date DESC LIMIT 50
    """, (brand_id,)).fetchall()
    snapshots_list = [dict(row) for row in snapshots]

    # Overdue items
    from datetime import date
    today = date.today().strftime('%Y-%m-%d')
    overdue = db.execute("""
        SELECT * FROM content_items
        WHERE brand_id=? AND publish_date < ? AND status NOT IN ('published','analyzed')
        ORDER BY publish_date
    """, (brand_id, today)).fetchall()

    # Competitors
    competitors = db.execute("SELECT * FROM competitor_profiles WHERE brand_id=?", (brand_id,)).fetchall()

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]

    # Performance insights
    insights = db.execute("""
        SELECT * FROM performance_insights
        WHERE brand_id=? ORDER BY created_at DESC LIMIT 10
    """, (brand_id,)).fetchall()
    insights_list = [dict(i) for i in insights]

    return render_template('analytics/view.html',
        brand=brand, status_data=status_data, type_data=type_data,
        pillar_data=pillar_data, monthly_data=monthly_data,
        published_data=published_data, snapshots=snapshots_list,
        overdue=overdue, competitors=[dict(c) for c in competitors],
        unread_notifications=unread_notifications, insights=insights_list)


@app.route('/api/analytics/snapshot', methods=['POST'])
def add_analytics_snapshot():
    db = get_db()
    data = request.json
    db.execute("""
        INSERT INTO analytics_snapshots (brand_id, content_item_id, snapshot_date, metric_type, metric_value, source, raw_data)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (data['brand_id'], data.get('content_item_id'), data['snapshot_date'],
          data['metric_type'], data['metric_value'], data.get('source', 'manual'),
          json.dumps(data.get('raw_data', {}))))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/competitors', methods=['POST'])
def add_competitor():
    db = get_db()
    data = request.json
    db.execute("""
        INSERT INTO competitor_profiles (brand_id, competitor_name, linkedin_url, website_url, notes)
        VALUES (?, ?, ?, ?, ?)
    """, (data['brand_id'], data['competitor_name'], data.get('linkedin_url', ''),
          data.get('website_url', ''), data.get('notes', '')))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/competitors/<int:comp_id>', methods=['DELETE'])
def delete_competitor(comp_id):
    db = get_db()
    db.execute("DELETE FROM competitor_profiles WHERE id=?", (comp_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Analytics Insights Loop ──────────────────────────────────────

@app.route('/api/brands/<int:brand_id>/analyze-performance', methods=['POST'])
def analyze_performance(brand_id):
    """Weekly self-improving loop: analyze Apify scrape data and generate insights for planning."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return jsonify({'ok': False, 'error': 'Brand not found'}), 404

    # Gather analytics data
    snapshots = db.execute("""
        SELECT a.*, ci.title, ci.content_type, ci.pillar_id, cp.name as pillar_name
        FROM analytics_snapshots a
        LEFT JOIN content_items ci ON a.content_item_id = ci.id
        LEFT JOIN content_pillars cp ON ci.pillar_id = cp.id
        WHERE a.brand_id = ?
        ORDER BY a.snapshot_date DESC LIMIT 100
    """, (brand_id,)).fetchall()

    # Gather published content performance
    published = db.execute("""
        SELECT ci.*, cp.name as pillar_name
        FROM content_items ci
        LEFT JOIN content_pillars cp ON ci.pillar_id = cp.id
        WHERE ci.brand_id = ? AND ci.status IN ('published', 'analyzed')
        ORDER BY ci.published_at DESC LIMIT 30
    """, (brand_id,)).fetchall()

    # Build performance summary for AI
    perf_data = []
    for p in published:
        item_metrics = [s for s in snapshots if s['content_item_id'] == p['id']]
        metrics = {s['metric_type']: s['metric_value'] for s in item_metrics}
        perf_data.append({
            'title': p['title'], 'type': p['content_type'],
            'pillar': p['pillar_name'] or 'Unassigned',
            'date': p['publish_date'], **metrics
        })

    # Get pillars
    pillars = db.execute("SELECT * FROM content_pillars WHERE brand_id=?", (brand_id,)).fetchall()
    pillar_names = [p['name'] for p in pillars]

    # Get cadence rules
    cadences = db.execute("SELECT * FROM cadence_rules WHERE brand_id=?", (brand_id,)).fetchall()

    # Build analysis prompt
    prompt = f"""Analyze the content performance for brand "{brand['name']}" and generate actionable insights.

Published content performance (recent 30):
{json.dumps(perf_data[:20], indent=2)}

Content pillars: {', '.join(pillar_names)}

Cadence rules: {json.dumps([dict(c) for c in cadences], indent=2)}

Provide analysis in this JSON format:
{{
    "summary": "2-3 sentence performance summary",
    "top_performing": [{{"title": "...", "why": "..."}}],
    "underperforming_pillars": ["pillar names that need more/better content"],
    "content_type_insights": "which types perform best and why",
    "cadence_compliance": "are we meeting cadence targets",
    "recommendations": [
        {{"action": "specific recommendation", "priority": "high/medium/low", "pillar": "affected pillar or null"}}
    ],
    "pillar_adjustments": [
        {{"pillar": "name", "suggestion": "keep/expand/reduce/merge", "reason": "why"}}
    ]
}}"""

    # Call Claude for analysis
    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Anthropic API key not configured'}), 400

    try:
        import urllib.request
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 2000,
                'messages': [{'role': 'user', 'content': prompt}]
            }).encode(),
            headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            ai_text = result['content'][0]['text']
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    # Extract JSON from response
    try:
        json_match = ai_text
        if '```' in ai_text:
            json_match = ai_text.split('```')[1].replace('json\n', '').strip()
        insights_data = json.loads(json_match)
    except (json.JSONDecodeError, IndexError):
        insights_data = {'summary': ai_text, 'recommendations': []}

    # Store insight
    from datetime import timedelta
    today = datetime.now().date()
    week_ago = today - timedelta(days=7)
    db.execute("""
        INSERT INTO performance_insights (brand_id, insight_type, insight_text, recommendations, data_source, period_start, period_end)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (brand_id, 'weekly_analysis', insights_data.get('summary', ''),
          json.dumps(insights_data.get('recommendations', [])),
          'combined', week_ago.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')))

    # Create notification
    db.execute("""
        INSERT INTO notifications (brand_id, notification_type, title, message, action_type, action_url)
        VALUES (?, 'system', ?, ?, 'go_to_page', ?)
    """, (brand_id, 'Performance insights generated',
          insights_data.get('summary', 'New insights available')[:200],
          f'/brands/{brand_id}/analytics'))
    db.commit()

    return jsonify({'ok': True, 'insights': insights_data})


@app.route('/api/brands/<int:brand_id>/review-pillars', methods=['POST'])
def review_pillars(brand_id):
    """Monthly self-improving loop: review and suggest pillar updates based on performance insights."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return jsonify({'ok': False, 'error': 'Brand not found'}), 404

    # Get current pillars with content counts
    pillars = db.execute("""
        SELECT cp.*, COUNT(ci.id) as content_count,
            SUM(CASE WHEN ci.status='published' THEN 1 ELSE 0 END) as published_count
        FROM content_pillars cp
        LEFT JOIN content_items ci ON ci.pillar_id = cp.id
        WHERE cp.brand_id = ?
        GROUP BY cp.id ORDER BY cp.sort_order
    """, (brand_id,)).fetchall()

    # Get recent insights
    insights = db.execute("""
        SELECT * FROM performance_insights
        WHERE brand_id = ? ORDER BY created_at DESC LIMIT 5
    """, (brand_id,)).fetchall()

    # Get engagement data per pillar
    pillar_engagement = db.execute("""
        SELECT cp.name as pillar, a.metric_type, AVG(a.metric_value) as avg_value
        FROM analytics_snapshots a
        JOIN content_items ci ON a.content_item_id = ci.id
        JOIN content_pillars cp ON ci.pillar_id = cp.id
        WHERE a.brand_id = ?
        GROUP BY cp.name, a.metric_type
    """, (brand_id,)).fetchall()

    prompt = f"""Review content pillars for brand "{brand['name']}" and suggest monthly updates.

Current pillars:
{json.dumps([{'name': p['name'], 'description': p['description'], 'content_count': p['content_count'], 'published': p['published_count']} for p in pillars], indent=2)}

Recent performance insights:
{json.dumps([{'summary': i['insight_text'], 'recommendations': i['recommendations']} for i in insights], indent=2)}

Engagement by pillar:
{json.dumps([dict(p) for p in pillar_engagement], indent=2)}

Suggest adjustments in this JSON format:
{{
    "overall_assessment": "1-2 sentence assessment",
    "pillar_updates": [
        {{"name": "existing pillar", "action": "keep|expand|reduce|rename|retire", "new_description": "updated description if needed", "reason": "why"}}
    ],
    "new_pillars": [
        {{"name": "suggested new pillar", "description": "what it covers", "reason": "gap this fills"}}
    ],
    "balance_score": 8,
    "balance_note": "how balanced the pillar mix is (1-10)"
}}"""

    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Anthropic API key not configured'}), 400

    try:
        import urllib.request
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 1500,
                'messages': [{'role': 'user', 'content': prompt}]
            }).encode(),
            headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            ai_text = result['content'][0]['text']
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    try:
        json_match = ai_text
        if '```' in ai_text:
            json_match = ai_text.split('```')[1].replace('json\n', '').strip()
        review_data = json.loads(json_match)
    except (json.JSONDecodeError, IndexError):
        review_data = {'overall_assessment': ai_text, 'pillar_updates': [], 'new_pillars': []}

    # Store as insight
    db.execute("""
        INSERT INTO performance_insights (brand_id, insight_type, insight_text, recommendations, data_source)
        VALUES (?, 'pillar_review', ?, ?, 'combined')
    """, (brand_id, review_data.get('overall_assessment', ''),
          json.dumps(review_data)))
    db.commit()

    return jsonify({'ok': True, 'review': review_data})


@app.route('/api/brands/<int:brand_id>/insights', methods=['GET'])
def get_insights(brand_id):
    """Get stored performance insights for a brand."""
    db = get_db()
    insights = db.execute("""
        SELECT * FROM performance_insights
        WHERE brand_id = ? ORDER BY created_at DESC LIMIT 20
    """, (brand_id,)).fetchall()
    return jsonify({'ok': True, 'insights': [dict(i) for i in insights]})


@app.route('/api/brands/<int:brand_id>/apply-insight/<int:insight_id>', methods=['POST'])
def apply_insight(brand_id, insight_id):
    """Mark an insight as applied (used for pillar updates)."""
    db = get_db()
    data = request.json or {}

    # If pillar updates are provided, apply them
    if 'pillar_updates' in data:
        for update in data['pillar_updates']:
            if update.get('action') == 'retire':
                db.execute("DELETE FROM content_pillars WHERE brand_id=? AND name=?",
                          (brand_id, update['name']))
            elif update.get('action') in ('rename', 'expand', 'reduce'):
                db.execute("UPDATE content_pillars SET description=? WHERE brand_id=? AND name=?",
                          (update.get('new_description', ''), brand_id, update['name']))

    if 'new_pillars' in data:
        max_order = db.execute("SELECT COALESCE(MAX(sort_order),0) FROM content_pillars WHERE brand_id=?",
                              (brand_id,)).fetchone()[0]
        for i, np in enumerate(data['new_pillars']):
            colors = ['#6366f1', '#ec4899', '#14b8a6', '#f59e0b', '#8b5cf6', '#ef4444']
            db.execute("""
                INSERT INTO content_pillars (brand_id, name, description, color, sort_order)
                VALUES (?, ?, ?, ?, ?)
            """, (brand_id, np['name'], np.get('description', ''),
                  colors[i % len(colors)], max_order + i + 1))

    db.execute("UPDATE performance_insights SET applied=1 WHERE id=?", (insight_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Folder Structure Mapping ─────────────────────────────────────

DIQIT_FOLDER_TEMPLATE = [
    ('01_Brand', 'Logo files, brand guidelines, company profile'),
    ('02_Product', 'Product features, docs, kiosk designs'),
    ('03_Strategy', 'Marketing strategies, content calendars, proposals'),
    ('04_Content', 'Created content (blogs, social posts, reels, LinkedIn)'),
    ('04_Content/Blogs', 'Blog articles'),
    ('04_Content/Social Media Posts', 'Social media content'),
    ('04_Content/Reels', 'Video reels'),
    ('04_Content/LinkedIn', 'LinkedIn-specific content'),
    ('05_Sales', 'Outreach playbooks, leads, prospects'),
    ('06_LinkedIn', 'Personal brand, authority plan, outreach'),
    ('07_Analytics', 'Performance reports, analytics data'),
    ('08_Research', 'Competitor research, market data'),
    ('08_Research/Deep_Research', 'Deep research outputs from Gemini/Claude'),
    ('09_Admin', 'Contracts, invoices, onboarding docs'),
    ('10_Pipeline', 'Pipeline workflow files'),
    ('10_Pipeline/Briefs', 'Content briefs'),
    ('10_Pipeline/Generated_Images', 'AI-generated images via Gemini'),
    ('10_Pipeline/Generated_Videos', 'Video content'),
    ('10_Pipeline/Client_Review', 'Content packages for client review'),
    ('10_Pipeline/Polished_Images', 'Canva-polished branded images'),
    ('Archive', 'Old versions, duplicates'),
]


@app.route('/api/brands/<int:brand_id>/folder-structure', methods=['GET'])
def get_folder_structure(brand_id):
    """Return the folder structure mapped to this brand."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'ok': False, 'error': 'Brand has no folder path configured'})

    base = brand['folder_path']
    folders = []
    for folder_name, purpose in DIQIT_FOLDER_TEMPLATE:
        full_path = os.path.join(base, folder_name)
        exists = os.path.isdir(full_path)
        file_count = len(os.listdir(full_path)) if exists else 0
        folders.append({
            'name': folder_name, 'purpose': purpose,
            'path': full_path, 'exists': exists, 'file_count': file_count
        })
    return jsonify({'ok': True, 'base_path': base, 'folders': folders})


@app.route('/api/brands/<int:brand_id>/create-folder-structure', methods=['POST'])
def create_folder_structure(brand_id):
    """Create the standard DIQIT folder structure for a brand."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'ok': False, 'error': 'Set the brand folder_path first'})

    base = brand['folder_path']
    created = []
    for folder_name, purpose in DIQIT_FOLDER_TEMPLATE:
        full_path = os.path.join(base, folder_name)
        if not os.path.exists(full_path):
            os.makedirs(full_path, exist_ok=True)
            created.append(folder_name)

    db.commit()
    return jsonify({'ok': True, 'created': created, 'total': len(created)})


@app.route('/api/brands/<int:brand_id>/update-folder-path', methods=['PUT'])
def update_brand_folder_path(brand_id):
    """Update the folder_path for a brand."""
    db = get_db()
    data = request.json
    folder_path = data.get('folder_path', '')
    db.execute("UPDATE brands SET folder_path=? WHERE id=?", (folder_path, brand_id))
    db.commit()
    return jsonify({'ok': True})


# ─── Routes: Planning ──────────────────────────────────────────────

@app.route('/brands/<int:brand_id>/planning')
def planning_view(brand_id):
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return redirect(url_for('brands_list'))

    plans = db.execute("""
        SELECT * FROM content_plans WHERE brand_id=? ORDER BY plan_month DESC
    """, (brand_id,)).fetchall()
    plans_list = []
    for plan in plans:
        p = dict(plan)
        p['plan_items'] = [dict(row) for row in db.execute("""
            SELECT pi.*, cp.name as pillar_name, cp.color as pillar_color
            FROM plan_items pi
            LEFT JOIN content_pillars cp ON pi.pillar_id = cp.id
            WHERE pi.plan_id=? ORDER BY pi.suggested_date
        """, (plan['id'],)).fetchall()]
        plans_list.append(p)

    pillars = [dict(row) for row in db.execute(
        "SELECT * FROM content_pillars WHERE brand_id=? ORDER BY sort_order", (brand_id,)).fetchall()]

    # Stats for the AI prompt context
    status_counts = db.execute("""
        SELECT status, COUNT(*) as cnt FROM content_items WHERE brand_id=? GROUP BY status
    """, (brand_id,)).fetchall()
    content_stats = {row['status']: row['cnt'] for row in status_counts}

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]

    return render_template('planning/view.html',
        brand=brand, plans=plans_list, pillars=pillars,
        content_stats=content_stats, unread_notifications=unread_notifications)


@app.route('/api/plans', methods=['POST'])
def create_plan():
    db = get_db()
    data = request.json
    db.execute("""
        INSERT INTO content_plans (brand_id, plan_month, status, ai_recommendations, notes)
        VALUES (?, ?, 'draft', ?, ?)
    """, (data['brand_id'], data['plan_month'], data.get('ai_recommendations', ''), data.get('notes', '')))
    plan_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # If AI recommendations include items, create them
    for item in data.get('items', []):
        db.execute("""
            INSERT INTO plan_items (plan_id, title, content_type, market, pillar_id, suggested_date, rationale, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'suggested')
        """, (plan_id, item['title'], item.get('content_type', 'linkedin_post'),
              item.get('market', 'APAC'), item.get('pillar_id'), item.get('suggested_date'),
              item.get('rationale', '')))
    db.commit()
    return jsonify({'ok': True, 'plan_id': plan_id})


@app.route('/api/plans/<int:plan_id>/items/<int:item_id>/status', methods=['PUT'])
def update_plan_item_status(plan_id, item_id):
    db = get_db()
    data = request.json
    new_status = data['status']
    db.execute("UPDATE plan_items SET status=? WHERE id=? AND plan_id=?", (new_status, item_id, plan_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/plans/<int:plan_id>/schedule', methods=['POST'])
def schedule_plan(plan_id):
    """Push approved plan items to the content calendar and optionally into the pipeline."""
    db = get_db()
    data = request.json or {}
    auto_pipeline = data.get('auto_pipeline', False)

    plan = db.execute("SELECT * FROM content_plans WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        return jsonify({'ok': False, 'error': 'Plan not found'}), 404

    approved_items = db.execute("""
        SELECT * FROM plan_items WHERE plan_id=? AND status='approved'
    """, (plan_id,)).fetchall()

    created = 0
    pipeline_started = 0
    for item in approved_items:
        db.execute("""
            INSERT INTO content_items (brand_id, title, content_type, market, pillar_id, publish_date, status, notes, media_tags)
            VALUES (?, ?, ?, ?, ?, ?, 'backlog', ?, ?)
        """, (plan['brand_id'], item['title'], item['content_type'], item['market'],
              item['pillar_id'], item['suggested_date'], f"From plan: {item['rationale'] or ''}",
              item['media_tags'] or '[]'))
        content_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("UPDATE plan_items SET content_item_id=?, status='scheduled' WHERE id=?", (content_id, item['id']))
        created += 1

        # Auto-pipeline: assign default workflow template and create first step progress
        if auto_pipeline:
            template = db.execute("SELECT * FROM workflow_templates WHERE brand_id=? AND is_default=1",
                                  (plan['brand_id'],)).fetchone()
            if template:
                db.execute("UPDATE content_items SET workflow_template_id=? WHERE id=?",
                           (template['id'], content_id))
                first_step = db.execute("""
                    SELECT * FROM workflow_steps WHERE template_id=? ORDER BY sort_order LIMIT 1
                """, (template['id'],)).fetchone()
                if first_step:
                    db.execute("""
                        INSERT INTO content_step_progress (content_item_id, workflow_step_id, status, started_at)
                        VALUES (?, ?, 'in_progress', CURRENT_TIMESTAMP)
                    """, (content_id, first_step['id']))
                    db.execute("UPDATE content_items SET status='research' WHERE id=?", (content_id,))
                    pipeline_started += 1

    db.execute("UPDATE content_plans SET status='approved', updated_at=CURRENT_TIMESTAMP WHERE id=?", (plan_id,))

    # Notify about the scheduling
    db.execute("""
        INSERT INTO notifications (brand_id, notification_type, title, message, due_date,
            action_type, action_url, action_label)
        VALUES (?, 'system', ?, ?, ?, 'go_to_page', ?, 'View Calendar')
    """, (plan['brand_id'], f'{created} items scheduled',
          f'Plan items have been pushed to the calendar{" and pipeline" if pipeline_started else ""}.',
          datetime.now().strftime('%Y-%m-%d'),
          f'/brands/{plan["brand_id"]}/calendar'))

    db.commit()
    return jsonify({'ok': True, 'created': created, 'pipeline_started': pipeline_started})


@app.route('/api/plans/<int:plan_id>/generate', methods=['POST'])
def generate_plan_recommendations(plan_id):
    """Use Claude to generate content recommendations for a plan."""
    db = get_db()
    plan = db.execute("SELECT * FROM content_plans WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        return jsonify({'ok': False, 'error': 'Plan not found'}), 404

    brand = db.execute("SELECT * FROM brands WHERE id=?", (plan['brand_id'],)).fetchone()
    pillars = db.execute("SELECT id, name, description FROM content_pillars WHERE brand_id=?", (plan['brand_id'],)).fetchall()
    pillar_info = ', '.join([f"{p['name']}: {p['description'] or ''}" for p in pillars])

    # Get recent performance data
    recent = db.execute("""
        SELECT title, content_type, market, status, publish_date
        FROM content_items WHERE brand_id=? ORDER BY created_at DESC LIMIT 20
    """, (plan['brand_id'],)).fetchall()
    recent_content = '\n'.join([f"- {r['title']} ({r['content_type']}, {r['market']}, {r['status']})" for r in recent])

    cadences = db.execute("SELECT * FROM cadence_rules WHERE brand_id=?", (plan['brand_id'],)).fetchall()
    cadence_info = ', '.join([f"{c['name']} ({c['channel']})x{c['posts_per_week']}/week" for c in cadences])

    prompt = f"""You are a content strategist for "{brand['name']}".
Brand voice: {brand['voice_summary'] or 'Professional yet approachable'}
Content pillars: {pillar_info}
Cadence rules: {cadence_info or 'No cadence rules set'}
Target month: {plan['plan_month']}

Recent content produced:
{recent_content or 'No recent content'}

Available media tools you can recommend per item:
- stock_image: Stock photos (Unsplash/Pexels) — good for linkedin_post, carousel, blog
- tts: Audio narration (ElevenLabs) — good for blog, video content repurposing
- translate_ja: Translate to Japanese — for Japan market items
- translate_vi: Translate to Vietnamese — for Vietnam market items
- translate_zh: Translate to Chinese — for China/APAC market items
- remove_bg: Remove image background — pair with stock_image for clean visuals
- qr_code: Generate QR code — for published content with landing pages
- short_url: Shorten URL — for any published content
- diagram: Mermaid diagram (manual) — for technical/process/architecture content
- chart: Data chart (manual) — for data-driven/analytics/benchmark content

Generate a content plan for {plan['plan_month']}. For each item provide:
- title: A specific, engaging title
- content_type: One of linkedin_post, carousel, video, blog, email, reel
- market: One of APAC, Japan, Singapore, Australia
- rationale: Why this content matters now (1 sentence)
- media_tags: Array of tool tags this content needs (choose from list above based on content_type and market)

Return ONLY a JSON array of objects. No markdown, no explanation. Example:
[{{"title":"...", "content_type":"linkedin_post", "market":"Singapore", "rationale":"...", "media_tags":["stock_image","short_url"]}}]
Generate 8-12 items spread across the month, covering all pillars."""

    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Claude API key not configured'}), 400

    import urllib.request
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 2000,
            'messages': [{'role': 'user', 'content': prompt}]
        }).encode(),
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            response_text = result['content'][0]['text'].strip()
            # Parse the JSON array from Claude's response
            if response_text.startswith('['):
                items = json.loads(response_text)
            else:
                # Try to extract JSON from the response
                import re
                match = re.search(r'\[.*\]', response_text, re.DOTALL)
                if match:
                    items = json.loads(match.group())
                else:
                    return jsonify({'ok': False, 'error': 'Could not parse AI response'}), 500

            # Distribute dates across the month
            year, month_num = plan['plan_month'].split('-')
            import calendar
            days_in_month = calendar.monthrange(int(year), int(month_num))[1]
            spacing = max(1, days_in_month // len(items)) if items else 1

            for i, item in enumerate(items):
                day = min(1 + i * spacing, days_in_month)
                suggested_date = f"{plan['plan_month']}-{day:02d}"
                # Match pillar by name if possible
                pillar_id = None
                for p in pillars:
                    if p['name'].lower() in item.get('rationale', '').lower() or p['name'].lower() in item.get('title', '').lower():
                        pillar_id = p['id'] if hasattr(p, '__getitem__') else None
                        break

                media_tags = json.dumps(item.get('media_tags', []))
                db.execute("""
                    INSERT INTO plan_items (plan_id, title, content_type, market, pillar_id, suggested_date, rationale, media_tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (plan_id, item['title'], item.get('content_type', 'linkedin_post'),
                      item.get('market', 'APAC'), pillar_id, suggested_date,
                      item.get('rationale', ''), media_tags))

            db.execute("UPDATE content_plans SET ai_recommendations=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (response_text, plan_id))
            db.commit()
            return jsonify({'ok': True, 'count': len(items)})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            err_data = json.loads(body)
            msg = err_data.get('error', {}).get('message', body)
        except Exception:
            msg = body
        if 'credit balance' in msg.lower() or 'billing' in msg.lower():
            return jsonify({'ok': False, 'error': 'Anthropic API credit balance is too low. Go to console.anthropic.com → Plans & Billing to add credits.'}), 402
        if e.code == 401:
            return jsonify({'ok': False, 'error': 'Invalid Claude API key. Check your key in Settings.'}), 401
        if e.code == 429:
            return jsonify({'ok': False, 'error': 'Claude API rate limit reached. Please wait a minute and try again.'}), 429
        return jsonify({'ok': False, 'error': f'Claude API error ({e.code}): {msg}'}), 500
    except urllib.error.URLError as e:
        return jsonify({'ok': False, 'error': f'Could not reach Claude API. Check your internet connection. ({e.reason})'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Routes: Brand Voice ──────────────────────────────────────────

@app.route('/brands/<int:brand_id>/voice')
def brand_voice_view(brand_id):
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return redirect(url_for('brands_list'))

    # Voice references grouped by type
    refs = db.execute("""
        SELECT * FROM brand_voice_references WHERE brand_id=? ORDER BY ref_type, created_at DESC
    """, (brand_id,)).fetchall()
    grouped_refs = {}
    for r in refs:
        t = r['ref_type']
        if t not in grouped_refs:
            grouped_refs[t] = []
        grouped_refs[t].append(dict(r))

    # Recent audits
    audits = db.execute("""
        SELECT ba.*, ci.title as content_title
        FROM brand_audits ba
        LEFT JOIN content_items ci ON ba.content_item_id = ci.id
        WHERE ba.brand_id=? ORDER BY ba.audited_at DESC LIMIT 20
    """, (brand_id,)).fetchall()
    audits_list = [dict(a) for a in audits]

    # Content items for audit dropdown
    content_items = db.execute("""
        SELECT id, title, content_type, status FROM content_items
        WHERE brand_id=? AND body_text IS NOT NULL AND body_text != ''
        ORDER BY created_at DESC LIMIT 50
    """, (brand_id,)).fetchall()

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]

    return render_template('voice/view.html',
        brand=brand, grouped_refs=grouped_refs, audits=audits_list,
        content_items=content_items, unread_notifications=unread_notifications)


@app.route('/api/voice-references', methods=['POST'])
def create_voice_reference():
    db = get_db()
    data = request.json
    db.execute("""
        INSERT INTO brand_voice_references (brand_id, ref_type, content, source)
        VALUES (?, ?, ?, ?)
    """, (data['brand_id'], data['ref_type'], data['content'], data.get('source', '')))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/voice-references/<int:ref_id>', methods=['DELETE'])
def delete_voice_reference(ref_id):
    db = get_db()
    db.execute("DELETE FROM brand_voice_references WHERE id=?", (ref_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/voice-score', methods=['POST'])
def score_voice():
    """Use Claude to score content against brand voice references."""
    db = get_db()
    data = request.json
    brand_id = data['brand_id']
    content_text = data.get('content_text', '')
    content_item_id = data.get('content_item_id')

    if content_item_id and not content_text:
        item = db.execute("SELECT body_text FROM content_items WHERE id=?", (content_item_id,)).fetchone()
        if item:
            content_text = item['body_text'] or ''

    if not content_text:
        return jsonify({'ok': False, 'error': 'No content to score'}), 400

    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    refs = db.execute("SELECT ref_type, content FROM brand_voice_references WHERE brand_id=?", (brand_id,)).fetchall()

    # Build voice profile from references
    dos = [r['content'] for r in refs if r['ref_type'] == 'do']
    donts = [r['content'] for r in refs if r['ref_type'] == 'dont']
    tone_keywords = [r['content'] for r in refs if r['ref_type'] == 'tone_keyword']
    samples = [r['content'] for r in refs if r['ref_type'] == 'text_sample']

    voice_profile = f"""Brand: {brand['name']}
Voice summary: {brand['voice_summary'] or 'Professional yet approachable'}
Tone keywords: {', '.join(tone_keywords) if tone_keywords else 'Not defined'}
Do's: {chr(10).join('- ' + d for d in dos) if dos else 'None specified'}
Don'ts: {chr(10).join('- ' + d for d in donts) if donts else 'None specified'}
Example text samples:
{chr(10).join('---' + chr(10) + s[:300] for s in samples[:3]) if samples else 'No samples provided'}"""

    prompt = f"""You are a brand voice auditor. Score the following content against the brand's voice guidelines.

{voice_profile}

CONTENT TO SCORE:
{content_text[:2000]}

Evaluate on these dimensions (each 0-100):
1. Tone alignment — Does it match the brand's tone keywords and voice?
2. Do's compliance — Does it follow the brand's writing guidelines?
3. Don'ts compliance — Does it avoid the things the brand shouldn't do?
4. Overall consistency — How consistent is it with the sample text?

Return ONLY a JSON object like:
{{"overall_score": 82, "tone_alignment": 85, "dos_compliance": 90, "donts_compliance": 75, "consistency": 78, "strengths": ["clear messaging", "professional tone"], "improvements": ["could be more direct", "avoid jargon in intro"], "rewrite_suggestion": "Optional: a brief rewritten version of the first paragraph that better matches brand voice"}}"""

    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Claude API key not configured'}), 400

    import urllib.request
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 1500,
            'messages': [{'role': 'user', 'content': prompt}]
        }).encode(),
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            response_text = result['content'][0]['text'].strip()
            if response_text.startswith('{'):
                score_data = json.loads(response_text)
            else:
                import re
                match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if match:
                    score_data = json.loads(match.group())
                else:
                    return jsonify({'ok': False, 'error': 'Could not parse AI response'}), 500

            # Save audit record
            db.execute("""
                INSERT INTO brand_audits (brand_id, content_item_id, audit_type, score, findings, recommendations)
                VALUES (?, ?, 'voice', ?, ?, ?)
            """, (brand_id, content_item_id,
                  score_data.get('overall_score', 0),
                  json.dumps(score_data),
                  json.dumps(score_data.get('improvements', []))))
            db.commit()

            return jsonify({'ok': True, 'score': score_data})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/brand-audits/<int:audit_id>', methods=['DELETE'])
def delete_brand_audit(audit_id):
    db = get_db()
    db.execute("DELETE FROM brand_audits WHERE id=?", (audit_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Routes: Asset Creation Hub ───────────────────────────────────

@app.route('/brands/<int:brand_id>/assets')
def assets_view(brand_id):
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return redirect(url_for('brands_list'))

    jobs = db.execute("""
        SELECT aj.*, ci.title as content_title
        FROM asset_jobs aj
        LEFT JOIN content_items ci ON aj.content_item_id = ci.id
        WHERE aj.brand_id=? ORDER BY aj.created_at DESC LIMIT 50
    """, (brand_id,)).fetchall()
    jobs_list = [dict(j) for j in jobs]

    content_items = db.execute("""
        SELECT id, title, content_type, body_text FROM content_items
        WHERE brand_id=? ORDER BY created_at DESC LIMIT 50
    """, (brand_id,)).fetchall()

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]

    return render_template('assets/view.html',
        brand=brand, jobs=jobs_list, content_items=content_items,
        unread_notifications=unread_notifications)


@app.route('/api/assets/generate', methods=['POST'])
def generate_asset():
    """Generate an asset using Claude or Gemini."""
    db = get_db()
    data = request.json
    brand_id = data['brand_id']
    asset_type = data['asset_type']
    prompt = data['prompt']
    content_item_id = data.get('content_item_id') or None
    provider = data.get('provider', 'claude')

    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()

    # Create job record
    db.execute("""
        INSERT INTO asset_jobs (brand_id, content_item_id, asset_type, prompt, provider, status)
        VALUES (?, ?, ?, ?, ?, 'generating')
    """, (brand_id, content_item_id, asset_type, prompt, provider))
    job_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()

    # Build the generation prompt based on asset type
    type_instructions = {
        'image': 'Generate a detailed image generation prompt for an AI image tool. Include style, composition, colors, mood.',
        'document': 'Generate a complete document outline with sections, key points, and suggested content for each section.',
        'slides': 'Generate a slide deck outline: for each slide provide a title, bullet points, and speaker notes.',
        'video_prompt': 'Generate a video script with scenes, narration, visual descriptions, and timing.',
        'social_graphic': 'Generate specs for a social media graphic: layout, text overlay, colors, imagery description.',
    }

    full_prompt = f"""You are a content asset creator for "{brand['name']}".
Brand voice: {brand['voice_summary'] or 'Professional yet approachable'}

Task: Create a {asset_type.replace('_', ' ')} asset.
{type_instructions.get(asset_type, 'Generate the requested asset.')}

User request: {prompt}

Return a detailed, ready-to-use output. Format as structured text with clear sections."""

    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        db.execute("UPDATE asset_jobs SET status='failed', error_message='API key not configured' WHERE id=?", (job_id,))
        db.commit()
        return jsonify({'ok': False, 'error': 'Claude API key not configured'}), 400

    import urllib.request
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 3000,
            'messages': [{'role': 'user', 'content': full_prompt}]
        }).encode(),
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            response_text = result['content'][0]['text'].strip()

            # Save the generated content as a file
            assets_dir = os.path.join(os.path.dirname(__file__), 'generated_assets')
            os.makedirs(assets_dir, exist_ok=True)
            filename = f"{asset_type}_{job_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            filepath = os.path.join(assets_dir, filename)
            with open(filepath, 'w') as f:
                f.write(response_text)

            db.execute("""
                UPDATE asset_jobs SET status='completed', result_path=?,
                result_metadata=?, completed_at=CURRENT_TIMESTAMP WHERE id=?
            """, (filepath, json.dumps({'length': len(response_text), 'provider': provider}), job_id))
            db.commit()

            return jsonify({'ok': True, 'job_id': job_id, 'result': response_text[:500]})
    except Exception as e:
        db.execute("UPDATE asset_jobs SET status='failed', error_message=? WHERE id=?", (str(e), job_id))
        db.commit()
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/assets/<int:job_id>/content')
def get_asset_content(job_id):
    """Read the generated asset content."""
    db = get_db()
    job = db.execute("SELECT * FROM asset_jobs WHERE id=?", (job_id,)).fetchone()
    if not job or not job['result_path']:
        return jsonify({'ok': False, 'error': 'Asset not found'}), 404
    try:
        with open(job['result_path'], 'r') as f:
            content = f.read()
        return jsonify({'ok': True, 'content': content})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/assets/<int:job_id>', methods=['DELETE'])
def delete_asset(job_id):
    db = get_db()
    job = db.execute("SELECT result_path FROM asset_jobs WHERE id=?", (job_id,)).fetchone()
    if job and job['result_path'] and os.path.exists(job['result_path']):
        os.remove(job['result_path'])
    db.execute("DELETE FROM asset_jobs WHERE id=?", (job_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Routes: Claude Conversations ─────────────────────────────────

@app.route('/api/conversations', methods=['POST'])
def create_conversation():
    """Start a new Claude conversation for iterative refinement."""
    db = get_db()
    data = request.json
    brand_id = data['brand_id']
    content_item_id = data.get('content_item_id') or None
    title = data.get('title', 'New conversation')
    initial_message = data.get('message', '')

    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()

    # Get voice refs for system context
    refs = db.execute("SELECT ref_type, content FROM brand_voice_references WHERE brand_id=?", (brand_id,)).fetchall()
    tone_keywords = [r['content'] for r in refs if r['ref_type'] == 'tone_keyword']

    system_context = f"You are a content assistant for {brand['name']}. Brand voice: {brand['voice_summary'] or 'Professional yet approachable'}."
    if tone_keywords:
        system_context += f" Tone: {', '.join(tone_keywords)}."

    # If linked to a content item, include its context
    content_context = ''
    if content_item_id:
        item = db.execute("SELECT * FROM content_items WHERE id=?", (content_item_id,)).fetchone()
        if item:
            content_context = f"\nContent: \"{item['title']}\" ({item['content_type']}, {item['market']})\nCurrent draft:\n{item['body_text'] or '(empty)'}"
            if not title or title == 'New conversation':
                title = f"Refine: {item['title'][:40]}"

    full_first_message = initial_message
    if content_context:
        full_first_message = content_context + '\n\n' + initial_message

    messages = [{'role': 'user', 'content': full_first_message, 'timestamp': datetime.now().isoformat()}]

    # Call Claude
    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Claude API key not configured'}), 400

    import urllib.request
    api_messages = [{'role': 'user', 'content': full_first_message}]
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 2000,
            'system': system_context,
            'messages': api_messages
        }).encode(),
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            assistant_text = result['content'][0]['text'].strip()

        messages.append({'role': 'assistant', 'content': assistant_text, 'timestamp': datetime.now().isoformat()})

        db.execute("""
            INSERT INTO claude_conversations (brand_id, content_item_id, title, messages, status)
            VALUES (?, ?, ?, ?, 'active')
        """, (brand_id, content_item_id, title, json.dumps(messages)))
        conv_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()

        return jsonify({'ok': True, 'conversation_id': conv_id, 'messages': messages})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/conversations/<int:conv_id>/reply', methods=['POST'])
def conversation_reply(conv_id):
    """Continue a Claude conversation."""
    db = get_db()
    data = request.json
    user_message = data['message']

    conv = db.execute("SELECT * FROM claude_conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        return jsonify({'ok': False, 'error': 'Conversation not found'}), 404

    brand = db.execute("SELECT * FROM brands WHERE id=?", (conv['brand_id'],)).fetchone()
    refs = db.execute("SELECT ref_type, content FROM brand_voice_references WHERE brand_id=?", (conv['brand_id'],)).fetchall()
    tone_keywords = [r['content'] for r in refs if r['ref_type'] == 'tone_keyword']

    system_context = f"You are a content assistant for {brand['name']}. Brand voice: {brand['voice_summary'] or 'Professional yet approachable'}."
    if tone_keywords:
        system_context += f" Tone: {', '.join(tone_keywords)}."

    messages = json.loads(conv['messages'])
    messages.append({'role': 'user', 'content': user_message, 'timestamp': datetime.now().isoformat()})

    # Build API messages (strip timestamps)
    api_messages = [{'role': m['role'], 'content': m['content']} for m in messages]

    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Claude API key not configured'}), 400

    import urllib.request
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 2000,
            'system': system_context,
            'messages': api_messages
        }).encode(),
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            assistant_text = result['content'][0]['text'].strip()

        messages.append({'role': 'assistant', 'content': assistant_text, 'timestamp': datetime.now().isoformat()})

        db.execute("""
            UPDATE claude_conversations SET messages=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (json.dumps(messages), conv_id))
        db.commit()

        return jsonify({'ok': True, 'messages': messages})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/conversations/<int:conv_id>/apply', methods=['POST'])
def conversation_apply(conv_id):
    """Apply the latest assistant response back to the content item."""
    db = get_db()
    conv = db.execute("SELECT * FROM claude_conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv or not conv['content_item_id']:
        return jsonify({'ok': False, 'error': 'No content item linked'}), 400

    messages = json.loads(conv['messages'])
    # Find last assistant message
    last_assistant = None
    for m in reversed(messages):
        if m['role'] == 'assistant':
            last_assistant = m['content']
            break

    if not last_assistant:
        return jsonify({'ok': False, 'error': 'No assistant response to apply'}), 400

    db.execute("""
        UPDATE content_items SET body_text=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
    """, (last_assistant, conv['content_item_id']))
    db.commit()

    return jsonify({'ok': True})


@app.route('/api/conversations', methods=['GET'])
def list_conversations():
    """List conversations for a brand."""
    db = get_db()
    brand_id = request.args.get('brand_id')
    convs = db.execute("""
        SELECT cc.id, cc.title, cc.status, cc.content_item_id, cc.created_at, cc.updated_at,
               ci.title as content_title
        FROM claude_conversations cc
        LEFT JOIN content_items ci ON cc.content_item_id = ci.id
        WHERE cc.brand_id=? ORDER BY cc.updated_at DESC LIMIT 30
    """, (brand_id,)).fetchall()
    return jsonify({'ok': True, 'conversations': [dict(c) for c in convs]})


@app.route('/api/conversations/<int:conv_id>')
def get_conversation(conv_id):
    """Get full conversation with messages."""
    db = get_db()
    conv = db.execute("SELECT * FROM claude_conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    return jsonify({'ok': True, 'conversation': dict(conv), 'messages': json.loads(conv['messages'])})


@app.route('/api/conversations/<int:conv_id>', methods=['DELETE'])
def delete_conversation(conv_id):
    db = get_db()
    db.execute("DELETE FROM claude_conversations WHERE id=?", (conv_id,))
    db.commit()
    return jsonify({'ok': True})


# ─── Routes: Pipeline Run (Chained Execution) ────────────────────

@app.route('/api/pipeline/run', methods=['POST'])
def run_pipeline():
    """Run full pipeline for a content item — advances through all workflow steps."""
    db = get_db()
    data = request.json
    brand_id = data['brand_id']
    content_item_id = data['content_item_id']

    # Get workflow steps
    template = db.execute("SELECT * FROM workflow_templates WHERE brand_id=? AND is_default=1", (brand_id,)).fetchone()
    if not template:
        return jsonify({'ok': False, 'error': 'No workflow template configured'}), 400

    steps = db.execute("SELECT * FROM workflow_steps WHERE template_id=? ORDER BY sort_order", (template['id'],)).fetchall()
    if not steps:
        return jsonify({'ok': False, 'error': 'No workflow steps defined'}), 400

    # Create pipeline run record
    db.execute("""
        INSERT INTO pipeline_runs (brand_id, content_item_id, status, current_step, total_steps, log)
        VALUES (?, ?, 'running', 0, ?, '[]')
    """, (brand_id, content_item_id, len(steps)))
    run_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()

    # Process each step — create step progress records and advance status
    log_entries = []
    status_flow = ['backlog', 'research', 'drafting', 'visuals', 'review', 'ready']
    content = db.execute("SELECT * FROM content_items WHERE id=?", (content_item_id,)).fetchone()

    for i, step in enumerate(steps):
        step_result = {
            'step': i + 1,
            'name': step['name'],
            'status': 'completed',
            'timestamp': datetime.now().isoformat()
        }

        # Create or update step progress
        existing = db.execute("""
            SELECT id FROM content_step_progress WHERE content_item_id=? AND workflow_step_id=?
        """, (content_item_id, step['id'])).fetchone()

        if existing:
            db.execute("""
                UPDATE content_step_progress SET status='completed', completed_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (existing['id'],))
        else:
            db.execute("""
                INSERT INTO content_step_progress (content_item_id, workflow_step_id, status, started_at, completed_at)
                VALUES (?, ?, 'completed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (content_item_id, step['id']))

        # Advance content status if applicable
        if i < len(status_flow):
            new_status = status_flow[min(i + 1, len(status_flow) - 1)]
            db.execute("UPDATE content_items SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (new_status, content_item_id))

        log_entries.append(step_result)

        # Update run progress
        db.execute("UPDATE pipeline_runs SET current_step=?, log=? WHERE id=?",
                   (i + 1, json.dumps(log_entries), run_id))
        db.commit()

    # Mark run as completed
    db.execute("""
        UPDATE pipeline_runs SET status='completed', completed_at=CURRENT_TIMESTAMP, log=?
        WHERE id=?
    """, (json.dumps(log_entries), run_id))
    db.commit()

    return jsonify({
        'ok': True,
        'run_id': run_id,
        'steps_completed': len(log_entries),
        'final_status': status_flow[min(len(steps), len(status_flow) - 1)]
    })


# ─── Routes: Template Library ─────────────────────────────────────

@app.route('/brands/<int:brand_id>/templates')
def templates_view(brand_id):
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return redirect(url_for('brands_list'))

    templates = db.execute("""
        SELECT * FROM content_templates WHERE brand_id=? ORDER BY use_count DESC, created_at DESC
    """, (brand_id,)).fetchall()
    templates_list = [dict(t) for t in templates]

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]

    return render_template('templates/view.html',
        brand=brand, templates=templates_list, unread_notifications=unread_notifications)


@app.route('/api/templates', methods=['POST'])
def create_template():
    db = get_db()
    data = request.json
    db.execute("""
        INSERT INTO content_templates (brand_id, name, description, content_type, market, body_template, visual_prompt_template, tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (data['brand_id'], data['name'], data.get('description', ''),
          data.get('content_type', 'linkedin_post'), data.get('market', 'APAC'),
          data.get('body_template', ''), data.get('visual_prompt_template', ''),
          json.dumps(data.get('tags', []))))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/templates/<int:template_id>', methods=['PUT'])
def update_template(template_id):
    db = get_db()
    data = request.json
    db.execute("""
        UPDATE content_templates SET name=?, description=?, content_type=?, market=?,
        body_template=?, visual_prompt_template=?, tags=?, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (data['name'], data.get('description', ''),
          data.get('content_type', 'linkedin_post'), data.get('market', 'APAC'),
          data.get('body_template', ''), data.get('visual_prompt_template', ''),
          json.dumps(data.get('tags', [])), template_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/templates/<int:template_id>', methods=['DELETE'])
def delete_template(template_id):
    db = get_db()
    db.execute("DELETE FROM content_templates WHERE id=?", (template_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/templates/<int:template_id>/use', methods=['POST'])
def use_template(template_id):
    """Create a content item from a template."""
    db = get_db()
    data = request.json
    tpl = db.execute("SELECT * FROM content_templates WHERE id=?", (template_id,)).fetchone()
    if not tpl:
        return jsonify({'ok': False, 'error': 'Template not found'}), 404

    title = data.get('title', tpl['name'])
    body = tpl['body_template'] or ''
    # Replace placeholders
    for key, val in data.get('placeholders', {}).items():
        body = body.replace('{{' + key + '}}', val)

    db.execute("""
        INSERT INTO content_items (brand_id, title, content_type, market, body_text, visual_prompt, status)
        VALUES (?, ?, ?, ?, ?, ?, 'backlog')
    """, (tpl['brand_id'], title, tpl['content_type'], data.get('market', tpl['market']),
          body, tpl['visual_prompt_template'] or ''))
    item_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    db.execute("UPDATE content_templates SET use_count = use_count + 1 WHERE id=?", (template_id,))
    db.commit()
    return jsonify({'ok': True, 'content_item_id': item_id})


# ─── Routes: Multi-Market Adaptation ─────────────────────────────

@app.route('/api/adapt', methods=['POST'])
def adapt_content():
    """Use Claude to adapt content for a different market."""
    db = get_db()
    data = request.json
    source_item_id = data['content_item_id']
    target_market = data['target_market']

    source = db.execute("SELECT * FROM content_items WHERE id=?", (source_item_id,)).fetchone()
    if not source:
        return jsonify({'ok': False, 'error': 'Content item not found'}), 404

    brand = db.execute("SELECT * FROM brands WHERE id=?", (source['brand_id'],)).fetchone()

    market_context = {
        'Japan': 'Japanese business culture: formal, relationship-focused, hierarchical. Use honorific language concepts. Reference local F&B chains and market dynamics.',
        'Singapore': 'Singapore market: multicultural, efficiency-driven, tech-savvy. Reference local aggregators (Grab, Foodpanda). Use Singlish-aware but professional tone.',
        'Australia': 'Australian market: casual-professional, direct, value-driven. Reference local chains and pub/cafe culture. Use Australian English.',
        'APAC': 'Pan-APAC: broad regional appeal, avoid country-specific references. Focus on shared challenges across Asian markets.',
    }

    prompt = f"""You are a content localization expert for "{brand['name']}".
Brand voice: {brand['voice_summary'] or 'Professional yet approachable'}

Adapt the following content from {source['market']} market to {target_market} market.
{market_context.get(target_market, '')}

ORIGINAL TITLE: {source['title']}
ORIGINAL CONTENT:
{source['body_text'] or '(no body text)'}

Provide:
1. Adapted title
2. Adapted body text
3. Any cultural notes

Return ONLY a JSON object:
{{"adapted_title": "...", "adapted_body": "...", "cultural_notes": "..."}}"""

    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Claude API key not configured'}), 400

    import urllib.request
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 2000,
            'messages': [{'role': 'user', 'content': prompt}]
        }).encode(),
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            response_text = result['content'][0]['text'].strip()
            if response_text.startswith('{'):
                adapted = json.loads(response_text)
            else:
                import re
                match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if match:
                    adapted = json.loads(match.group())
                else:
                    return jsonify({'ok': False, 'error': 'Could not parse AI response'}), 500

            db.execute("""
                INSERT INTO market_adaptations (source_item_id, target_market, adapted_title, adapted_body, adapted_visual_prompt)
                VALUES (?, ?, ?, ?, ?)
            """, (source_item_id, target_market, adapted.get('adapted_title', source['title']),
                  adapted.get('adapted_body', ''), source.get('visual_prompt', '')))
            adaptation_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.commit()

            return jsonify({'ok': True, 'adaptation_id': adaptation_id, 'adapted': adapted})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/adapt/<int:adaptation_id>/create', methods=['POST'])
def create_from_adaptation(adaptation_id):
    """Create a content item from an approved adaptation."""
    db = get_db()
    adapt = db.execute("SELECT ma.*, ci.brand_id, ci.content_type, ci.pillar_id FROM market_adaptations ma JOIN content_items ci ON ma.source_item_id = ci.id WHERE ma.id=?", (adaptation_id,)).fetchone()
    if not adapt:
        return jsonify({'ok': False, 'error': 'Adaptation not found'}), 404

    db.execute("""
        INSERT INTO content_items (brand_id, title, content_type, market, pillar_id, body_text, visual_prompt, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'backlog', ?)
    """, (adapt['brand_id'], adapt['adapted_title'], adapt['content_type'],
          adapt['target_market'], adapt['pillar_id'],
          adapt['adapted_body'], adapt['adapted_visual_prompt'],
          f"Adapted from item #{adapt['source_item_id']}"))
    item_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    db.execute("UPDATE market_adaptations SET status='approved', content_item_id=? WHERE id=?", (item_id, adaptation_id))
    db.commit()
    return jsonify({'ok': True, 'content_item_id': item_id})


# ─── Routes: Google Drive Sync Config ────────────────────────────

@app.route('/api/drive-sync', methods=['POST'])
def create_drive_sync():
    db = get_db()
    data = request.json
    db.execute("""
        INSERT INTO drive_sync_config (brand_id, folder_id, folder_name, sync_direction, local_path, enabled)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (data['brand_id'], data['folder_id'], data.get('folder_name', ''),
          data.get('sync_direction', 'both'), data.get('local_path', ''),
          data.get('enabled', True)))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/drive-sync/<int:sync_id>', methods=['DELETE'])
def delete_drive_sync(sync_id):
    db = get_db()
    db.execute("DELETE FROM drive_sync_config WHERE id=?", (sync_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/drive-sync/<int:sync_id>/toggle', methods=['PUT'])
def toggle_drive_sync(sync_id):
    db = get_db()
    current = db.execute("SELECT enabled FROM drive_sync_config WHERE id=?", (sync_id,)).fetchone()
    if current:
        db.execute("UPDATE drive_sync_config SET enabled=? WHERE id=?", (not current['enabled'], sync_id))
        db.commit()
    return jsonify({'ok': True})


# ─── Routes: Calendar ──────────────────────────────────────────────

@app.route('/brands/<int:brand_id>/calendar')
def calendar_view(brand_id):
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    content_items = db.execute("""
        SELECT ci.*, cp.name as pillar_name, cp.color as pillar_color
        FROM content_items ci
        LEFT JOIN content_pillars cp ON ci.pillar_id = cp.id
        WHERE ci.brand_id = ? AND ci.publish_date IS NOT NULL
        ORDER BY ci.publish_date
    """, (brand_id,)).fetchall()
    cadences = db.execute("SELECT * FROM cadence_rules WHERE brand_id=?", (brand_id,)).fetchall()
    pillars = db.execute("SELECT * FROM content_pillars WHERE brand_id=? ORDER BY sort_order", (brand_id,)).fetchall()
    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    content_items_json = [dict(row) for row in content_items]

    return render_template('calendar/view.html',
        brand=brand, content_items=content_items, content_items_json=content_items_json,
        cadences=cadences, pillars=pillars, unread_notifications=unread_notifications)


@app.route('/api/content-items/recurring', methods=['POST'])
def create_recurring_content():
    """Create multiple content items on a recurring schedule."""
    db = get_db()
    data = request.json
    brand_id = data['brand_id']
    title_template = data['title']  # e.g. "Weekly LinkedIn Post"
    content_type = data.get('content_type', 'linkedin_post')
    market = data.get('market', 'APAC')
    pillar_id = data.get('pillar_id') or None
    body_text = data.get('body_text', '')
    start_date = datetime.strptime(data['start_date'], '%Y-%m-%d')
    interval_days = int(data.get('interval_days', 7))
    count = min(int(data.get('count', 4)), 52)  # max 52 items

    created = []
    for i in range(count):
        pub_date = start_date + timedelta(days=i * interval_days)
        title = f"{title_template} #{i+1}" if count > 1 else title_template
        db.execute("""
            INSERT INTO content_items (brand_id, pillar_id, title, content_type, market, status, body_text, publish_date)
            VALUES (?, ?, ?, ?, ?, 'backlog', ?, ?)
        """, (brand_id, pillar_id, title, content_type, market, body_text, pub_date.strftime('%Y-%m-%d')))
        item_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        created.append({'id': item_id, 'title': title, 'date': pub_date.strftime('%Y-%m-%d')})

    db.commit()
    return jsonify({'ok': True, 'created': created, 'total': len(created)})


@app.route('/api/feedback', methods=['POST'])
def create_feedback():
    db = get_db()
    data = request.json
    db.execute("""
        INSERT INTO feedback (content_item_id, feedback_type, feedback_text, status)
        VALUES (?, ?, ?, 'pending')
    """, (data['content_item_id'], data.get('feedback_type', 'revision'), data['feedback_text']))

    # Also create a notification
    content = db.execute("SELECT * FROM content_items WHERE id=?", (data['content_item_id'],)).fetchone()
    if content:
        db.execute("""
            INSERT INTO notifications (brand_id, content_item_id, notification_type, title, message, due_date)
            VALUES (?, ?, 'feedback', ?, ?, ?)
        """, (content['brand_id'], data['content_item_id'],
              f'Feedback on: {content["title"]}',
              data['feedback_text'][:200],
              datetime.now().strftime('%Y-%m-%d')))

    db.commit()
    return jsonify({'ok': True})


@app.route('/api/feedback/<int:feedback_id>/send-to-claude', methods=['POST'])
def send_feedback_to_claude(feedback_id):
    """Send feedback to Claude for processing. This generates a Claude command."""
    db = get_db()
    feedback = db.execute("SELECT f.*, ci.* FROM feedback f JOIN content_items ci ON f.content_item_id=ci.id WHERE f.id=?", (feedback_id,)).fetchone()
    if not feedback:
        return jsonify({'error': 'Feedback not found'}), 404

    brand = db.execute("SELECT * FROM brands WHERE id=?", (feedback['brand_id'],)).fetchone()

    # Build the Claude prompt
    claude_prompt = f"""Please revise the following content based on the feedback provided.

BRAND: {brand['name']}
CONTENT TITLE: {feedback['title']}
CONTENT TYPE: {feedback['content_type']}
MARKET: {feedback['market']}

CURRENT CONTENT:
{feedback['body_text'] or '(no body text)'}

FEEDBACK:
{feedback['feedback_text']}

Please provide the revised content that addresses this feedback, maintaining the {brand['name']} brand voice."""

    # Update feedback status
    db.execute("UPDATE feedback SET status='processing' WHERE id=?", (feedback_id,))
    db.commit()

    return jsonify({
        'ok': True,
        'claude_prompt': claude_prompt,
        'instruction': 'Copy this prompt to Claude to get revised content. Paste the result back to apply.'
    })


@app.route('/api/feedback/<int:feedback_id>/apply', methods=['POST'])
def apply_feedback(feedback_id):
    """Apply Claude's revised content back to the content item."""
    db = get_db()
    data = request.json
    feedback = db.execute("SELECT * FROM feedback WHERE id=?", (feedback_id,)).fetchone()
    if not feedback:
        return jsonify({'error': 'Feedback not found'}), 404

    # Update the content item with revised content
    if data.get('revised_body'):
        db.execute("UPDATE content_items SET body_text=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (data['revised_body'], feedback['content_item_id']))

    if data.get('revised_visual_prompt'):
        db.execute("UPDATE content_items SET visual_prompt=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (data['revised_visual_prompt'], feedback['content_item_id']))

    # Mark feedback as applied
    db.execute("UPDATE feedback SET status='applied', claude_response=?, resolved_at=CURRENT_TIMESTAMP WHERE id=?",
               (data.get('revised_body', ''), feedback_id))
    db.commit()
    return jsonify({'ok': True})


# ─── Routes: Notifications ─────────────────────────────────────────

@app.route('/notifications')
def notifications_view():
    db = get_db()
    # Split into actionable tasks (today's undone) and alerts (info/past)
    today_str = datetime.now().strftime('%Y-%m-%d')
    tasks = db.execute("""
        SELECT n.*, b.name as brand_name FROM notifications n
        LEFT JOIN brands b ON n.brand_id=b.id
        WHERE n.is_done=0 AND n.notification_type IN ('task', 'system')
        ORDER BY n.due_date ASC, n.created_at DESC
    """).fetchall()
    done_today = db.execute("""
        SELECT n.*, b.name as brand_name FROM notifications n
        LEFT JOIN brands b ON n.brand_id=b.id
        WHERE n.is_done=1 AND n.due_date=?
        ORDER BY n.created_at DESC
    """, (today_str,)).fetchall()
    alerts = db.execute("""
        SELECT n.*, b.name as brand_name FROM notifications n
        LEFT JOIN brands b ON n.brand_id=b.id
        WHERE n.notification_type IN ('overdue', 'feedback', 'reminder')
          AND n.is_done=0
        ORDER BY n.created_at DESC
    """).fetchall()
    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    # Get most recent brand for sidebar nav persistence
    last_brand = db.execute("SELECT * FROM brands ORDER BY id LIMIT 1").fetchone()
    brands = [dict(row) for row in db.execute("SELECT id, name FROM brands ORDER BY name").fetchall()]
    return render_template('notifications/view.html',
        tasks=tasks, done_today=done_today, alerts=alerts,
        unread_notifications=unread_notifications, last_brand=last_brand, brands=brands)


@app.route('/api/notifications/<int:notif_id>/read', methods=['PUT'])
def mark_notification_read(notif_id):
    db = get_db()
    db.execute("UPDATE notifications SET is_read=1 WHERE id=?", (notif_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/notifications/<int:notif_id>/done', methods=['PUT'])
def mark_notification_done(notif_id):
    db = get_db()
    db.execute("UPDATE notifications SET is_done=1, is_read=1 WHERE id=?", (notif_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/notifications/read-all', methods=['PUT'])
def mark_all_notifications_read():
    db = get_db()
    db.execute("UPDATE notifications SET is_read=1")
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/notifications/generate-daily', methods=['POST'])
def generate_daily_notifications():
    """Generate notifications based on cadence rules for today."""
    db = get_db()
    today = datetime.now()
    day_name = today.strftime('%A')
    today_str = today.strftime('%Y-%m-%d')

    brands = db.execute("SELECT * FROM brands").fetchall()
    count = 0
    for brand in brands:
        bid = brand['id']
        # Check workflow steps for today
        steps = db.execute("""
            SELECT ws.* FROM workflow_steps ws
            JOIN workflow_templates wt ON ws.template_id = wt.id
            WHERE wt.brand_id = ? AND ws.day_of_week LIKE ?
        """, (bid, f'%{day_name}%')).fetchall()

        for step in steps:
            # Don't duplicate
            existing = db.execute("""
                SELECT COUNT(*) FROM notifications
                WHERE brand_id=? AND title=? AND due_date=?
            """, (bid, step['name'], today_str)).fetchone()[0]
            if existing == 0:
                # Determine action based on step trigger_type
                trigger = step['trigger_type'] or 'none'
                stype = step['step_type'] or 'manual'
                if trigger == 'claude':
                    action_type, action_label = 'trigger_claude', 'Run with Claude'
                elif trigger == 'apify_claude':
                    action_type, action_label = 'trigger_apify', 'Scrape & Analyze'
                elif trigger in ('apps_script', 'gemini'):
                    action_type, action_label = 'trigger_gemini', 'Generate with Gemini'
                elif step['upload_folder']:
                    action_type, action_label = 'upload', 'Upload Files'
                elif step['linked_folder']:
                    action_type, action_label = 'open_folder', 'Open Folder'
                else:
                    action_type, action_label = 'go_to_page', 'Go to Workflow'

                action_url = f'/brands/{bid}/workflow'
                if step['linked_folder']:
                    action_url = step['linked_folder']

                db.execute("""
                    INSERT INTO notifications (brand_id, notification_type, title, message, due_date,
                        action_type, action_url, action_label, step_id, step_type, duration_minutes)
                    VALUES (?, 'task', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (bid, step['name'], step['description'], today_str,
                      action_type, action_url, action_label,
                      step['id'], stype, step['duration_minutes'] or 0))
                count += 1

        # Check for overdue items
        overdue = db.execute("""
            SELECT * FROM content_items
            WHERE brand_id=? AND publish_date < ? AND status NOT IN ('published','analyzed')
        """, (bid, today_str)).fetchall()
        for item in overdue:
            existing = db.execute("""
                SELECT COUNT(*) FROM notifications
                WHERE content_item_id=? AND notification_type='overdue' AND due_date=?
            """, (item['id'], today_str)).fetchone()[0]
            if existing == 0:
                db.execute("""
                    INSERT INTO notifications (brand_id, content_item_id, notification_type, title, message, due_date,
                        action_type, action_url, action_label)
                    VALUES (?, ?, 'overdue', ?, ?, ?, 'go_to_page', ?, 'Open in Pipeline')
                """, (bid, item['id'], f'OVERDUE: {item["title"]}',
                      f'This item was due on {item["publish_date"]} and is still {item["status"]}',
                      today_str, f'/brands/{bid}/pipeline'))
                count += 1

    db.commit()
    return jsonify({'ok': True, 'generated': count})


# ─── Routes: Pipeline Trigger ──────────────────────────────────────

@app.route('/api/pipeline/trigger', methods=['POST'])
def trigger_pipeline():
    """Manually trigger the content generation pipeline or a specific workflow step."""
    db = get_db()
    data = request.json
    brand_id = data['brand_id']
    step_id = data.get('step_id')
    trigger_type = data.get('trigger_type', 'manual')
    step_name = data.get('step_name', 'Pipeline')

    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return jsonify({'error': 'Brand not found'}), 404

    # Record the trigger
    db.execute("""
        INSERT INTO pipeline_triggers (brand_id, trigger_type, status)
        VALUES (?, ?, 'running')
    """, (brand_id, trigger_type))
    trigger_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()

    pipeline_folder = os.path.join(brand['folder_path'] or '', '10_Pipeline', 'Briefs')

    result_info = {
        'ok': True,
        'trigger_id': trigger_id,
        'step_name': step_name,
        'trigger_type': trigger_type,
        'briefs_folder_exists': os.path.exists(pipeline_folder),
        'briefs_count': 0,
        'message': '',
    }

    if os.path.exists(pipeline_folder):
        briefs = [f for f in os.listdir(pipeline_folder) if f.endswith('.txt') or f.endswith('.md')]
        result_info['briefs_count'] = len(briefs)

    if trigger_type in ('apps_script', 'gemini'):
        if result_info['briefs_count'] > 0:
            result_info['message'] = f'Found {result_info["briefs_count"]} briefs ready for in-app Gemini image generation.'
        else:
            result_info['message'] = 'No briefs found in the pipeline. Create content briefs first.'
    elif trigger_type == 'claude':
        result_info['message'] = f'Step "{step_name}" triggered for Claude processing. Use Claude to execute this step with the current content.'
    else:
        result_info['message'] = f'Step "{step_name}" triggered manually.'

    # Update trigger status
    db.execute("""
        UPDATE pipeline_triggers SET status='completed', completed_at=CURRENT_TIMESTAMP, result=?
        WHERE id=?
    """, (json.dumps(result_info), trigger_id))

    # Create a notification for the trigger
    db.execute("""
        INSERT INTO notifications (brand_id, notification_type, title, message, due_date)
        VALUES (?, 'system', ?, ?, ?)
    """, (brand_id, f'Trigger: {step_name}', result_info['message'], datetime.now().strftime('%Y-%m-%d')))

    db.commit()

    return jsonify(result_info)


@app.route('/api/pipeline/create-brief', methods=['POST'])
def create_pipeline_brief():
    """Create a content brief file in the pipeline briefs folder."""
    db = get_db()
    data = request.json
    brand = db.execute("SELECT * FROM brands WHERE id=?", (data['brand_id'],)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'error': 'Brand folder not configured'}), 400

    briefs_folder = os.path.join(brand['folder_path'], '10_Pipeline', 'Briefs')
    os.makedirs(briefs_folder, exist_ok=True)

    # Format the brief
    brief_content = f"""TOPIC: {data['topic']}
MARKET: {data.get('market', 'APAC')}
IMAGE_STYLE: {data.get('image_style', 'single')}
VIDEO: {data.get('video', 'no')}
POST_TEXT:
{data.get('post_text', '')}
"""

    slug = data['topic'].lower().replace(' ', '_')[:40]
    filename = f"{datetime.now().strftime('%Y%m%d')}_{slug}.txt"
    filepath = os.path.join(briefs_folder, filename)

    with open(filepath, 'w') as f:
        f.write(brief_content)

    return jsonify({'ok': True, 'filename': filename, 'path': filepath})


# ─── Routes: Folder Explorer ───────────────────────────────────────

@app.route('/brands/<int:brand_id>/folders')
def folders_view(brand_id):
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    folder_mappings = db.execute("""
        SELECT fm.*, ws.name as step_name
        FROM folder_mappings fm
        LEFT JOIN workflow_steps ws ON fm.workflow_step_id = ws.id
        WHERE fm.brand_id = ?
    """, (brand_id,)).fetchall()
    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]

    # Build folder tree
    folder_tree = []
    base = brand['folder_path']
    if base and os.path.exists(base):
        for entry in sorted(os.listdir(base)):
            full = os.path.join(base, entry)
            if os.path.isdir(full) and not entry.startswith('.') and entry != 'digitalize-me':
                children = []
                try:
                    for child in sorted(os.listdir(full)):
                        child_full = os.path.join(full, child)
                        if os.path.isdir(child_full):
                            file_count = len([f for f in os.listdir(child_full) if not f.startswith('.')])
                            children.append({'name': child, 'type': 'dir', 'count': file_count})
                        else:
                            children.append({'name': child, 'type': 'file', 'size': os.path.getsize(child_full)})
                except PermissionError:
                    pass
                folder_tree.append({
                    'name': entry,
                    'path': full,
                    'children': children[:20],  # Limit for display
                    'total_children': len(children),
                })

    return render_template('folders/view.html',
        brand=brand, folder_mappings=folder_mappings,
        folder_tree=folder_tree, unread_notifications=unread_notifications)


# ─── Routes: Content Pillars ───────────────────────────────────────

@app.route('/api/pillars', methods=['POST'])
def create_pillar():
    db = get_db()
    data = request.json
    max_order = db.execute("SELECT COALESCE(MAX(sort_order),0) FROM content_pillars WHERE brand_id=?",
                           (data['brand_id'],)).fetchone()[0]
    db.execute(
        "INSERT INTO content_pillars (brand_id, name, description, color, sort_order) VALUES (?,?,?,?,?)",
        (data['brand_id'], data['name'], data.get('description', ''), data.get('color', '#6366f1'), max_order + 1)
    )
    db.commit()
    new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({'ok': True, 'id': new_id})


@app.route('/api/pillars/<int:pillar_id>', methods=['PUT'])
def update_pillar(pillar_id):
    db = get_db()
    data = request.json
    db.execute("UPDATE content_pillars SET name=?, description=?, color=? WHERE id=?",
               (data['name'], data.get('description', ''), data.get('color', '#6366f1'), pillar_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/pillars/<int:pillar_id>', methods=['DELETE'])
def delete_pillar(pillar_id):
    db = get_db()
    # Unlink content items from this pillar before deleting
    db.execute("UPDATE content_items SET pillar_id=NULL WHERE pillar_id=?", (pillar_id,))
    db.execute("DELETE FROM content_pillars WHERE id=?", (pillar_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/brands/<int:brand_id>/suggest-pillars', methods=['POST'])
def suggest_pillars(brand_id):
    """Use Claude to suggest pillar updates based on performance insights."""
    db = get_db()
    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Anthropic API key not configured'}), 400

    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    pillars = db.execute("SELECT * FROM content_pillars WHERE brand_id=? ORDER BY sort_order", (brand_id,)).fetchall()
    insights = db.execute("""
        SELECT * FROM performance_insights WHERE brand_id=? ORDER BY created_at DESC LIMIT 5
    """, (brand_id,)).fetchall()

    pillar_list = ', '.join([f'"{p["name"]}" ({p["description"]})' for p in pillars])
    insight_text = '\n'.join([f'- {i["insight_text"]}' for i in insights]) if insights else 'No insights yet.'

    prompt = f"""You are a content strategy advisor for "{brand['name']}".

Current content pillars: {pillar_list}

Recent performance insights:
{insight_text}

Based on performance data, suggest updates to the content pillars. Return a JSON array of suggestions:
[{{"action": "add"|"update"|"remove", "name": "pillar name", "description": "description", "color": "#hex", "reason": "why"}}]

Keep it practical — suggest 2-4 changes max. Only suggest changes that would measurably improve engagement."""

    try:
        import urllib.request
        req_body = json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 2048,
            'messages': [{'role': 'user', 'content': prompt}]
        })
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=req_body.encode(),
            headers={
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
            }
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            response_text = result['content'][0]['text'] if result.get('content') else '[]'

        # Extract JSON from response
        import re
        json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
        suggestions = json.loads(json_match.group()) if json_match else []

        return jsonify({'ok': True, 'suggestions': suggestions})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Routes: Cadence ───────────────────────────────────────────────

@app.route('/api/cadence', methods=['POST'])
def create_cadence():
    db = get_db()
    data = request.json
    db.execute("""
        INSERT INTO cadence_rules (brand_id, name, channel, posts_per_week, preferred_days, preferred_times, notes)
        VALUES (?,?,?,?,?,?,?)
    """, (
        data['brand_id'], data['name'], data.get('channel', 'LinkedIn'),
        data.get('posts_per_week', 3),
        json.dumps(data.get('preferred_days', [])),
        json.dumps(data.get('preferred_times', [])),
        data.get('notes', '')
    ))
    db.commit()
    new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({'ok': True, 'id': new_id})


@app.route('/api/cadence/<int:cadence_id>', methods=['PUT'])
def update_cadence(cadence_id):
    db = get_db()
    data = request.json
    db.execute("""
        UPDATE cadence_rules SET name=?, channel=?, posts_per_week=?,
        preferred_days=?, preferred_times=?, notes=? WHERE id=?
    """, (
        data['name'], data.get('channel', 'LinkedIn'),
        data.get('posts_per_week', 3),
        json.dumps(data.get('preferred_days', [])),
        json.dumps(data.get('preferred_times', [])),
        data.get('notes', ''),
        cadence_id
    ))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/cadence/<int:cadence_id>', methods=['DELETE'])
def delete_cadence(cadence_id):
    db = get_db()
    db.execute("DELETE FROM cadence_rules WHERE id=?", (cadence_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/brands/<int:brand_id>/suggest-cadence', methods=['POST'])
def suggest_cadence(brand_id):
    """Use Claude to suggest cadence updates based on performance."""
    db = get_db()
    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Anthropic API key not configured'}), 400

    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    cadences = db.execute("SELECT * FROM cadence_rules WHERE brand_id=?", (brand_id,)).fetchall()
    insights = db.execute("""
        SELECT * FROM performance_insights WHERE brand_id=? ORDER BY created_at DESC LIMIT 5
    """, (brand_id,)).fetchall()

    cadence_list = '\n'.join([f'- {c["name"]}: {c["channel"]}, {c["posts_per_week"]}x/week on {c["preferred_days"]}'
                              for c in cadences])
    insight_text = '\n'.join([f'- {i["insight_text"]}' for i in insights]) if insights else 'No insights yet.'

    prompt = f"""You are a publishing cadence advisor for "{brand['name']}".

Current cadence rules:
{cadence_list}

Recent performance insights:
{insight_text}

Suggest cadence adjustments. Return a JSON array:
[{{"action": "add"|"update"|"remove", "name": "rule name", "channel": "LinkedIn", "posts_per_week": 3, "preferred_days": ["Tuesday","Thursday"], "preferred_times": ["9:00 AM"], "reason": "why"}}]

Focus on timing optimization and channel mix. 2-3 suggestions max."""

    try:
        import urllib.request
        req_body = json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 2048,
            'messages': [{'role': 'user', 'content': prompt}]
        })
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=req_body.encode(),
            headers={
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
            }
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            response_text = result['content'][0]['text'] if result.get('content') else '[]'

        import re
        json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
        suggestions = json.loads(json_match.group()) if json_match else []

        return jsonify({'ok': True, 'suggestions': suggestions})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/brands/<int:brand_id>/cadence-check', methods=['GET'])
def cadence_check(brand_id):
    """Check cadence compliance — warn when schedule drifts from cadence rules."""
    db = get_db()
    cadences = db.execute("SELECT * FROM cadence_rules WHERE brand_id=?", (brand_id,)).fetchall()
    if not cadences:
        return jsonify({'ok': True, 'warnings': [], 'message': 'No cadence rules configured'})

    now = datetime.now()
    week_start = now - timedelta(days=now.weekday())
    week_end = week_start + timedelta(days=6)
    next_week_end = week_end + timedelta(days=7)

    warnings = []
    for cadence in cadences:
        target_per_week = cadence['posts_per_week']
        channel = cadence['channel'] or 'LinkedIn'
        preferred_days = json.loads(cadence['preferred_days'] or '[]')

        # Map channel to content_type for counting
        type_map = {
            'LinkedIn': ('linkedin_post', 'carousel'),
            'Blog': ('blog',),
            'Email': ('email',),
            'Video': ('video', 'reel'),
        }
        content_types = type_map.get(channel, (channel.lower(),))
        type_placeholders = ','.join(['?' for _ in content_types])

        # Count items scheduled this week
        this_week_count = db.execute(f"""
            SELECT COUNT(*) FROM content_items
            WHERE brand_id=? AND publish_date BETWEEN ? AND ?
            AND content_type IN ({type_placeholders})
        """, (brand_id, week_start.strftime('%Y-%m-%d'), week_end.strftime('%Y-%m-%d'),
              *content_types)).fetchone()[0]

        # Count items scheduled next week
        next_week_count = db.execute(f"""
            SELECT COUNT(*) FROM content_items
            WHERE brand_id=? AND publish_date BETWEEN ? AND ?
            AND content_type IN ({type_placeholders})
        """, (brand_id, (week_end + timedelta(days=1)).strftime('%Y-%m-%d'),
              next_week_end.strftime('%Y-%m-%d'), *content_types)).fetchone()[0]

        # Check day alignment
        scheduled_days = db.execute(f"""
            SELECT publish_date FROM content_items
            WHERE brand_id=? AND publish_date BETWEEN ? AND ?
            AND content_type IN ({type_placeholders})
        """, (brand_id, week_start.strftime('%Y-%m-%d'), week_end.strftime('%Y-%m-%d'),
              *content_types)).fetchall()
        actual_days = set()
        for row in scheduled_days:
            if row['publish_date']:
                d = datetime.strptime(row['publish_date'], '%Y-%m-%d')
                actual_days.add(d.strftime('%A'))

        misaligned_days = []
        if preferred_days and actual_days:
            misaligned_days = [d for d in actual_days if d not in preferred_days]

        if this_week_count < target_per_week:
            gap = target_per_week - this_week_count
            warnings.append({
                'type': 'under_scheduled',
                'severity': 'warning' if gap <= 1 else 'error',
                'channel': channel,
                'cadence_name': cadence['name'],
                'message': f'{channel}: {this_week_count}/{target_per_week} posts this week — {gap} short',
                'target': target_per_week,
                'actual': this_week_count,
                'gap': gap,
            })

        if next_week_count < target_per_week:
            gap = target_per_week - next_week_count
            warnings.append({
                'type': 'next_week_gap',
                'severity': 'info',
                'channel': channel,
                'cadence_name': cadence['name'],
                'message': f'{channel}: only {next_week_count}/{target_per_week} scheduled next week',
                'target': target_per_week,
                'actual': next_week_count,
                'gap': gap,
            })

        if misaligned_days:
            warnings.append({
                'type': 'day_misaligned',
                'severity': 'info',
                'channel': channel,
                'cadence_name': cadence['name'],
                'message': f'{channel}: posts on {", ".join(misaligned_days)} — preferred days are {", ".join(preferred_days)}',
            })

    return jsonify({'ok': True, 'warnings': warnings})


# ─── Settings ───────────────────────────────────────────────────────

def get_setting(db, key, default=''):
    row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    value = row['value']
    if key in ENCRYPTED_KEYS:
        return decrypt_value(value)
    return value


def set_setting(db, key, value):
    stored_value = value
    if key in ENCRYPTED_KEYS and value:
        stored_value = encrypt_value(value)
    db.execute("""
        INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
    """, (key, stored_value))
    db.commit()


@app.route('/settings')
def settings_view():
    db = get_db()
    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    password_set = bool(get_setting(db, 'settings_password_hash'))
    authenticated = session.get('settings_authenticated', False)

    # If password is set but user isn't authenticated, show login gate
    last_brand = db.execute("SELECT * FROM brands ORDER BY id LIMIT 1").fetchone()
    if password_set and not authenticated:
        theme_mode = get_setting(db, 'theme_mode', 'dark')
        return render_template('settings.html',
            unread_notifications=unread_notifications, locked=True, password_set=True,
            api_keys={}, theme_mode=theme_mode, dynamic_keys=[], last_brand=last_brand)

    # Load dynamic API keys
    dynamic_keys = json.loads(get_setting(db, 'dynamic_api_keys', '[]'))

    api_keys = {
        'anthropic_api_key': get_setting(db, 'anthropic_api_key'),
        'google_api_key': get_setting(db, 'google_api_key'),
        'gemini_api_key': get_setting(db, 'gemini_api_key'),
        'apify_api_key': get_setting(db, 'apify_api_key'),
    }
    theme_mode = get_setting(db, 'theme_mode', 'dark')

    # Load Drive sync configs
    drive_syncs = db.execute("""
        SELECT dsc.*, b.name as brand_name FROM drive_sync_config dsc
        LEFT JOIN brands b ON dsc.brand_id = b.id
        ORDER BY dsc.created_at DESC
    """).fetchall()
    drive_syncs_list = [dict(s) for s in drive_syncs]
    brands = [dict(row) for row in db.execute("SELECT id, name FROM brands ORDER BY name").fetchall()]

    return render_template('settings.html',
        unread_notifications=unread_notifications, api_keys=api_keys,
        theme_mode=theme_mode, locked=False, password_set=password_set,
        dynamic_keys=dynamic_keys, drive_syncs=drive_syncs_list, last_brand=last_brand,
        brands=brands)


@app.route('/api/settings/password', methods=['POST'])
def set_settings_password():
    """Set or change the settings page password."""
    db = get_db()
    data = request.json
    new_password = data.get('password', '')
    current_password = data.get('current_password', '')
    email = data.get('email', '')

    existing_hash = get_setting(db, 'settings_password_hash')

    # If a password already exists, verify current password
    if existing_hash:
        if not verify_password(current_password, existing_hash):
            return jsonify({'ok': False, 'error': 'Current password is incorrect'}), 401

    if len(new_password) < 4:
        return jsonify({'ok': False, 'error': 'Password must be at least 4 characters'}), 400

    set_setting(db, 'settings_password_hash', hash_password(new_password))
    if email:
        set_setting(db, 'settings_recovery_email', email)
    session['settings_authenticated'] = True
    return jsonify({'ok': True})


@app.route('/api/settings/login', methods=['POST'])
def settings_login():
    """Authenticate to access settings."""
    db = get_db()
    data = request.json
    password = data.get('password', '')
    stored_hash = get_setting(db, 'settings_password_hash')

    if verify_password(password, stored_hash):
        session['settings_authenticated'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Incorrect password'}), 401


@app.route('/api/settings/logout', methods=['POST'])
def settings_logout():
    """Lock settings page."""
    session.pop('settings_authenticated', None)
    return jsonify({'ok': True})


@app.route('/api/settings', methods=['POST'])
def save_settings():
    db = get_db()
    data = request.json
    for key, value in data.items():
        if key in ENCRYPTED_KEYS:
            set_setting(db, key, value)
    return jsonify({'ok': True})


@app.route('/api/settings/dynamic-keys', methods=['POST'])
def save_dynamic_keys():
    """Save dynamically managed API keys."""
    db = get_db()
    data = request.json
    keys = data.get('keys', [])
    # Encrypt each key value before storing
    stored_keys = []
    for k in keys:
        stored_keys.append({
            'name': k.get('name', ''),
            'description': k.get('description', ''),
            'value': encrypt_value(k.get('value', '')),
            'icon': k.get('icon', 'fas fa-key'),
        })
    set_setting(db, 'dynamic_api_keys', json.dumps(stored_keys))
    return jsonify({'ok': True})


@app.route('/api/settings/dynamic-keys', methods=['GET'])
def get_dynamic_keys():
    """Get dynamically managed API keys (decrypted)."""
    db = get_db()
    stored = json.loads(get_setting(db, 'dynamic_api_keys', '[]'))
    # Decrypt values for display
    for k in stored:
        k['value'] = decrypt_value(k.get('value', ''))
    return jsonify({'ok': True, 'keys': stored})


@app.route('/api/brands/<int:brand_id>/folder-path')
def get_brand_folder_path(brand_id):
    """Return the brand's folder_path for JS usage."""
    db = get_db()
    brand = db.execute("SELECT folder_path FROM brands WHERE id=?", (brand_id,)).fetchone()
    return jsonify({'folder_path': brand['folder_path'] if brand else ''})


@app.route('/api/settings/theme', methods=['POST'])
def toggle_theme():
    """Toggle between light and dark theme."""
    db = get_db()
    data = request.json
    mode = data.get('mode', 'dark')
    if mode not in ('light', 'dark'):
        mode = 'dark'
    set_setting(db, 'theme_mode', mode)
    return jsonify({'ok': True, 'mode': mode})


# ─── Folder Access API ──────────────────────────────────────────────

@app.route('/api/open-folder', methods=['POST'])
def open_folder():
    """Open a folder in macOS Finder."""
    data = request.json
    folder_path = data.get('path', '')
    brand_folder = data.get('brand_folder', '')

    if brand_folder and folder_path:
        full_path = os.path.join(brand_folder, folder_path)
    elif folder_path:
        full_path = folder_path
    else:
        return jsonify({'ok': False, 'error': 'No path provided'}), 400

    # Resolve and validate the path
    full_path = os.path.expanduser(full_path)
    if not os.path.exists(full_path):
        # Try creating the directory if it doesn't exist
        try:
            os.makedirs(full_path, exist_ok=True)
        except OSError:
            return jsonify({'ok': False, 'error': f'Path does not exist: {full_path}'}), 404

    try:
        subprocess.Popen(['open', full_path])
        return jsonify({'ok': True, 'path': full_path})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Apify Scraping API ──────────────────────────────────────────────

@app.route('/api/apify/scrape', methods=['POST'])
def apify_scrape():
    """Run an Apify actor for LinkedIn or web scraping."""
    db = get_db()
    api_key = get_setting(db, 'apify_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Apify API key not configured. Go to Settings.'}), 400

    data = request.json
    actor_id = data.get('actor_id', 'apify/web-scraper')
    run_input = data.get('input', {})
    brand_id = data.get('brand_id')

    try:
        import urllib.request
        req_body = json.dumps(run_input)
        url = f'https://api.apify.com/v2/acts/{actor_id}/runs?token={api_key}'
        req = urllib.request.Request(url, data=req_body.encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
            run_id = result.get('data', {}).get('id', '')

        if brand_id:
            db.execute("""
                INSERT INTO notifications (brand_id, notification_type, title, message, due_date)
                VALUES (?, 'system', ?, ?, ?)
            """, (brand_id, f'Apify: {actor_id}',
                  f'Scrape started. Run ID: {run_id}',
                  datetime.now().strftime('%Y-%m-%d')))
            db.commit()

        return jsonify({'ok': True, 'run_id': run_id, 'actor_id': actor_id})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/apify/results/<run_id>')
def apify_results(run_id):
    """Fetch results from a completed Apify run."""
    db = get_db()
    api_key = get_setting(db, 'apify_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Apify API key not configured.'}), 400
    try:
        import urllib.request
        url = f'https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?token={api_key}'
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=60) as resp:
            items = json.loads(resp.read().decode())
        return jsonify({'ok': True, 'items': items, 'count': len(items)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Folder ↔ Database Sync ──────────────────────────────────────────

def _parse_content_from_md(filepath):
    """Parse a content markdown file and extract individual posts."""
    items = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception:
        return items

    # Split by ## POST headers
    import re
    posts = re.split(r'^## (POST \d+)', text, flags=re.MULTILINE)
    if len(posts) < 2:
        return items

    for i in range(1, len(posts), 2):
        header = posts[i]  # e.g. "POST 1"
        body = posts[i + 1] if i + 1 < len(posts) else ''

        # Parse first line for date and format
        first_line = body.strip().split('\n')[0] if body.strip() else ''
        # Extract date like "Apr 1 (Wed)" or "— Apr 1 (Wed)"
        date_match = re.search(r'(?:—\s*)?(?:Apr|Mar|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Jan|Feb)\s+\d+', first_line)
        format_match = re.search(r'\*\*Format:\*\*\s*(.+)', body)
        author_match = re.search(r'\*\*Author:\*\*\s*(.+)', body)
        title_match = re.search(r'\*\*Title/Hook:\*\*\s*(.+)', body)
        market_match = re.search(r'\*\*Market:\*\*\s*(.+)', body)

        title = title_match.group(1).strip() if title_match else f'{header.strip()}'
        fmt = format_match.group(1).strip() if format_match else 'linkedin_post'

        # Map format to content_type
        fmt_lower = fmt.lower()
        if 'carousel' in fmt_lower:
            content_type = 'carousel'
        elif 'video' in fmt_lower:
            content_type = 'video'
        elif 'reel' in fmt_lower:
            content_type = 'reel'
        else:
            content_type = 'linkedin_post'

        market = market_match.group(1).strip() if market_match else 'APAC'

        # Extract publish date
        publish_date = None
        date_in_header = re.search(r'(Apr|Mar|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Jan|Feb)\s+(\d+)', first_line)
        if date_in_header:
            month_names = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                          'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}
            m = month_names.get(date_in_header.group(1), 4)
            d = int(date_in_header.group(2))
            publish_date = f'2026-{m:02d}-{d:02d}'

        # Get the body text (caption/post text)
        caption_match = re.search(r'### (?:Caption|Post Text|LinkedIn Caption)\s*\n(.+?)(?=\n###|\n\*\*#|\Z)',
                                  body, re.DOTALL)
        body_text = caption_match.group(1).strip() if caption_match else ''

        items.append({
            'title': title[:200],
            'content_type': content_type,
            'market': market,
            'publish_date': publish_date,
            'body_text': body_text[:5000],
            'status': 'ready',
            'source_file': filepath,
        })

    return items


@app.route('/api/brands/<int:brand_id>/sync-folders', methods=['POST'])
def sync_folders(brand_id):
    """Scan brand folders and sync content to the database."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'ok': False, 'error': 'Brand folder not configured'}), 400

    base = brand['folder_path']
    synced = {'content_items': 0, 'files_found': 0, 'skipped': 0}

    # 1. Scan 04_Content for markdown/text content files
    content_dir = os.path.join(base, '04_Content')
    if os.path.exists(content_dir):
        for root, dirs, files in os.walk(content_dir):
            for f in files:
                if f.startswith('.'):
                    continue
                fp = os.path.join(root, f)
                synced['files_found'] += 1

                if f.endswith('.md') and 'Posts' in f:
                    # Parse multi-post markdown files
                    items = _parse_content_from_md(fp)
                    for item in items:
                        # Check if already exists by title + brand
                        existing = db.execute("""
                            SELECT id FROM content_items WHERE brand_id=? AND title=?
                        """, (brand_id, item['title'])).fetchone()
                        if not existing:
                            db.execute("""
                                INSERT INTO content_items (brand_id, title, content_type, market,
                                    status, body_text, publish_date, notes)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, (brand_id, item['title'], item['content_type'], item['market'],
                                  item['status'], item['body_text'], item['publish_date'],
                                  f"Synced from: {os.path.relpath(fp, base)}"))
                            synced['content_items'] += 1
                        else:
                            synced['skipped'] += 1

    # 2. Scan 10_Pipeline/Briefs for pipeline content
    briefs_dir = os.path.join(base, '10_Pipeline', 'Briefs')
    if os.path.exists(briefs_dir):
        for f in os.listdir(briefs_dir):
            if f.startswith('.') or not (f.endswith('.md') or f.endswith('.txt')):
                continue
            fp = os.path.join(briefs_dir, f)
            synced['files_found'] += 1
            title = f.replace('.md', '').replace('.txt', '').replace('_', ' ')
            existing = db.execute("""
                SELECT id FROM content_items WHERE brand_id=? AND title=?
            """, (brand_id, title)).fetchone()
            if not existing:
                try:
                    with open(fp, 'r') as bf:
                        brief_text = bf.read()[:5000]
                except Exception:
                    brief_text = ''
                db.execute("""
                    INSERT INTO content_items (brand_id, title, content_type, status, body_text, notes)
                    VALUES (?, ?, 'linkedin_post', 'backlog', ?, ?)
                """, (brand_id, title, brief_text, f"From pipeline brief: {f}"))
                synced['content_items'] += 1

    # 3. Update file counts in folder mappings (enrichment)
    for fm in db.execute("SELECT * FROM folder_mappings WHERE brand_id=?", (brand_id,)).fetchall():
        fpath = os.path.join(base, fm['folder_path'])
        if os.path.exists(fpath):
            file_count = sum(1 for _, _, files in os.walk(fpath) for f in files if not f.startswith('.'))
            # Store count as part of purpose if not already there
            synced['files_found'] += file_count

    db.commit()

    # Create sync notification
    if synced['content_items'] > 0:
        db.execute("""
            INSERT INTO notifications (brand_id, notification_type, title, message, due_date,
                action_type, action_url, action_label)
            VALUES (?, 'system', ?, ?, ?, 'go_to_page', ?, 'View Pipeline')
        """, (brand_id, f'Folder sync: {synced["content_items"]} items imported',
              f'Scanned folders and imported {synced["content_items"]} new content items. {synced["skipped"]} already existed.',
              datetime.now().strftime('%Y-%m-%d'), f'/brands/{brand_id}/pipeline'))
        db.commit()

    return jsonify({'ok': True, **synced})


@app.route('/api/brands/<int:brand_id>/file-drop', methods=['POST'])
def file_drop(brand_id):
    """Smart file drop — auto-route uploaded files to the correct DIQIT folder based on content."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'ok': False, 'error': 'Brand folder not configured'}), 400

    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'No file provided'}), 400

    file = request.files['file']
    filename = file.filename or 'unnamed'
    base = brand['folder_path']

    # Determine target folder based on file extension and name
    ext = os.path.splitext(filename)[1].lower()
    name_lower = filename.lower()

    # Routing rules
    if any(kw in name_lower for kw in ['logo', 'brand', 'guideline', 'headshot']):
        target_folder = '01_Brand'
    elif any(kw in name_lower for kw in ['postap', 'product', 'feature', 'kiosk', 'spec']):
        target_folder = '02_Product'
    elif any(kw in name_lower for kw in ['strategy', 'calendar', 'proposal', 'plan']):
        target_folder = '03_Strategy'
    elif any(kw in name_lower for kw in ['blog', 'post', 'carousel', 'reel', 'content', 'linkedin', 'social']):
        target_folder = '04_Content'
    elif any(kw in name_lower for kw in ['lead', 'prospect', 'outreach', 'playbook', 'target', 'sales', 'intent']):
        target_folder = '05_Sales'
    elif any(kw in name_lower for kw in ['neeraj', 'authority', 'personal']):
        target_folder = '06_Neeraj_LinkedIn'
    elif any(kw in name_lower for kw in ['analytics', 'report', 'performance', 'metric']):
        target_folder = '07_Analytics'
    elif any(kw in name_lower for kw in ['research', 'competitor', 'market', 'keyword', 'intelligence']):
        target_folder = '08_Research'
    elif any(kw in name_lower for kw in ['invoice', 'contract', 'agreement', 'admin', 'onboard']):
        target_folder = '09_Admin'
    elif any(kw in name_lower for kw in ['brief', 'pipeline', 'script', 'prompt', 'automation']):
        target_folder = '10_Pipeline'
    elif ext in ['.mp4', '.mov', '.webm', '.avi']:
        target_folder = '04_Content/Reels'
    elif ext in ['.psd', '.ai', '.fig', '.sketch']:
        target_folder = '01_Brand'
    elif ext in ['.csv', '.xlsx', '.xls']:
        if any(kw in name_lower for kw in ['analytics', 'performance']):
            target_folder = '07_Analytics'
        else:
            target_folder = '08_Research'
    else:
        target_folder = '04_Content'

    # Save file
    dest_dir = os.path.join(base, target_folder)
    os.makedirs(dest_dir, exist_ok=True)

    # Avoid overwriting — add number suffix if file exists
    dest_path = os.path.join(dest_dir, filename)
    if os.path.exists(dest_path):
        name, ext_part = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_dir, f"{name}_{counter}{ext_part}")
            counter += 1

    file.save(dest_path)

    # Create notification
    db.execute("""
        INSERT INTO notifications (brand_id, notification_type, title, message, due_date,
            action_type, action_url, action_label)
        VALUES (?, 'system', ?, ?, ?, 'open_folder', ?, 'Open Folder')
    """, (brand_id, f'File dropped: {filename}',
          f'Auto-routed to {target_folder}/',
          datetime.now().strftime('%Y-%m-%d'), target_folder))
    db.commit()

    return jsonify({
        'ok': True,
        'filename': filename,
        'routed_to': target_folder,
        'full_path': dest_path
    })


# ─── File Watcher for Auto-Triggers ──────────────────────────────────

_watcher_started = False

def start_file_watcher():
    """Watch brand folders for file changes and auto-trigger workflow steps."""
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True

    def watch_loop():
        # Track known files: {path: mtime}
        known = {}
        while True:
            _time.sleep(10)  # Check every 10 seconds
            try:
                with app.app_context():
                    db_conn = sqlite3.connect(DATABASE)
                    db_conn.row_factory = sqlite3.Row

                    brands = db_conn.execute("SELECT * FROM brands").fetchall()
                    for brand in brands:
                        folder = brand['folder_path']
                        if not folder or not os.path.exists(folder):
                            continue

                        # Get workflow steps with linked folders & auto triggers
                        steps = db_conn.execute("""
                            SELECT ws.* FROM workflow_steps ws
                            JOIN workflow_templates wt ON ws.template_id = wt.id
                            WHERE wt.brand_id = ? AND ws.trigger_type IN ('claude', 'apps_script', 'gemini')
                              AND ws.step_type IN ('automated', 'semi-auto')
                              AND ws.linked_folder IS NOT NULL AND ws.linked_folder != ''
                        """, (brand['id'],)).fetchall()

                        for step in steps:
                            watch_path = os.path.join(folder, step['linked_folder'])
                            if not os.path.exists(watch_path):
                                continue

                            # Scan for new/modified files
                            for root, _, files in os.walk(watch_path):
                                for f in files:
                                    if f.startswith('.'):
                                        continue
                                    fp = os.path.join(root, f)
                                    try:
                                        mtime = os.path.getmtime(fp)
                                    except OSError:
                                        continue
                                    prev = known.get(fp)
                                    known[fp] = mtime
                                    if prev is None:
                                        continue  # First scan — just record
                                    if mtime > prev:
                                        # File was modified → create notification
                                        existing = db_conn.execute("""
                                            SELECT COUNT(*) FROM notifications
                                            WHERE brand_id=? AND title=? AND due_date=?
                                        """, (brand['id'],
                                              f'Auto: {step["name"]}',
                                              datetime.now().strftime('%Y-%m-%d'))).fetchone()[0]
                                        if existing == 0:
                                            trigger = step['trigger_type'] or 'none'
                                            if trigger == 'claude':
                                                fw_action_type, fw_action_label = 'trigger_claude', 'Auto-Run with Claude'
                                            elif trigger == 'apify_claude':
                                                fw_action_type, fw_action_label = 'trigger_apify', 'Scrape & Analyze'
                                            elif trigger in ('apps_script', 'gemini'):
                                                fw_action_type, fw_action_label = 'trigger_gemini', 'Generate with Gemini'
                                            else:
                                                fw_action_type, fw_action_label = 'open_folder', 'Open Folder'
                                            db_conn.execute("""
                                                INSERT INTO notifications
                                                    (brand_id, notification_type, title, message, due_date,
                                                     action_type, action_url, action_label, step_id, step_type)
                                                VALUES (?, 'system', ?, ?, ?, ?, ?, ?, ?, ?)
                                            """, (brand['id'],
                                                  f'Auto: {step["name"]}',
                                                  f'File changed in {step["linked_folder"]}: {f}. Step "{step["name"]}" is ready to trigger.',
                                                  datetime.now().strftime('%Y-%m-%d'),
                                                  fw_action_type, step['linked_folder'],
                                                  fw_action_label, step['id'], step['step_type']))
                                            db_conn.commit()
                    db_conn.close()
            except Exception:
                pass  # Silently handle watcher errors

    t = threading.Thread(target=watch_loop, daemon=True)
    t.start()


@app.route('/api/tasks/<int:notif_id>/validate', methods=['POST'])
def validate_task_completion(notif_id):
    """Check if a task is done by looking for files in the linked folder."""
    db = get_db()
    notif = db.execute("SELECT * FROM notifications WHERE id=?", (notif_id,)).fetchone()
    if not notif:
        return jsonify({'ok': False, 'error': 'Task not found'}), 404

    step_id = notif['step_id']
    brand_id = notif['brand_id']
    if not step_id:
        return jsonify({'ok': False, 'validated': False, 'reason': 'No linked workflow step'})

    step = db.execute("SELECT * FROM workflow_steps WHERE id=?", (step_id,)).fetchone()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not step or not brand or not brand['folder_path']:
        return jsonify({'ok': False, 'validated': False, 'reason': 'Missing step or brand folder'})

    # Check upload_folder or linked_folder for recent files
    check_folder = step['upload_folder'] or step['linked_folder']
    if not check_folder:
        return jsonify({'ok': True, 'validated': False, 'reason': 'No folder to check'})

    full_path = os.path.join(brand['folder_path'], check_folder)
    if not os.path.exists(full_path):
        return jsonify({'ok': True, 'validated': False, 'reason': f'Folder not found: {check_folder}'})

    # Look for files modified today
    today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    recent_files = []
    for root, _, files in os.walk(full_path):
        for f in files:
            if f.startswith('.'):
                continue
            fp = os.path.join(root, f)
            try:
                if os.path.getmtime(fp) >= today_start:
                    recent_files.append({
                        'name': f,
                        'path': os.path.relpath(fp, brand['folder_path']),
                        'modified': datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%H:%M'),
                    })
            except OSError:
                continue

    validated = len(recent_files) > 0
    if validated:
        # Auto-mark as done
        db.execute("UPDATE notifications SET is_done=1, is_read=1 WHERE id=?", (notif_id,))
        db.commit()

    return jsonify({
        'ok': True,
        'validated': validated,
        'recent_files': recent_files,
        'folder': check_folder,
        'reason': f'Found {len(recent_files)} file(s) modified today' if validated else 'No new files found today',
    })


@app.route('/api/tasks/validate-all', methods=['POST'])
def validate_all_tasks():
    """Check all pending tasks for auto-completion by scanning folders."""
    db = get_db()
    tasks = db.execute("""
        SELECT n.id, n.step_id, n.brand_id FROM notifications n
        WHERE n.is_done=0 AND n.step_id IS NOT NULL
    """).fetchall()

    validated_count = 0
    for task in tasks:
        step = db.execute("SELECT * FROM workflow_steps WHERE id=?", (task['step_id'],)).fetchone()
        brand = db.execute("SELECT * FROM brands WHERE id=?", (task['brand_id'],)).fetchone()
        if not step or not brand or not brand['folder_path']:
            continue

        check_folder = step['upload_folder'] or step['linked_folder']
        if not check_folder:
            continue

        full_path = os.path.join(brand['folder_path'], check_folder)
        if not os.path.exists(full_path):
            continue

        today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
        has_recent = False
        for root, _, files in os.walk(full_path):
            for f in files:
                if f.startswith('.'):
                    continue
                try:
                    if os.path.getmtime(os.path.join(root, f)) >= today_start:
                        has_recent = True
                        break
                except OSError:
                    continue
            if has_recent:
                break

        if has_recent:
            db.execute("UPDATE notifications SET is_done=1, is_read=1 WHERE id=?", (task['id'],))
            validated_count += 1

    db.commit()
    return jsonify({'ok': True, 'validated': validated_count})


@app.route('/api/gemini/generate-image', methods=['POST'])
def gemini_generate_image():
    """Generate an image using Gemini API (replaces external Apps Script for image gen)."""
    db = get_db()
    gemini_key = get_setting(db, 'gemini_api_key')
    if not gemini_key:
        return jsonify({'ok': False, 'error': 'Gemini API key not configured. Go to Settings.'}), 400

    data = request.json
    brand_id = data.get('brand_id')
    prompt = data.get('prompt', '')
    step_id = data.get('step_id')

    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return jsonify({'ok': False, 'error': 'Brand not found'}), 404

    try:
        import urllib.request
        # Use Gemini 2.0 Flash for image generation
        api_url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key={gemini_key}'
        req_body = json.dumps({
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'responseModalities': ['TEXT', 'IMAGE']}
        })
        req = urllib.request.Request(api_url, data=req_body.encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())

        # Extract image data from response
        saved_files = []
        if result.get('candidates'):
            parts = result['candidates'][0].get('content', {}).get('parts', [])
            for i, part in enumerate(parts):
                if part.get('inlineData'):
                    img_data = base64.b64decode(part['inlineData']['data'])
                    mime = part['inlineData'].get('mimeType', 'image/png')
                    ext = '.png' if 'png' in mime else '.jpg'

                    # Save to pipeline folder
                    output_dir = os.path.join(brand['folder_path'] or '', '10_Pipeline', 'Generated_Images')
                    os.makedirs(output_dir, exist_ok=True)
                    filename = f"gemini_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{i}{ext}"
                    filepath = os.path.join(output_dir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(img_data)
                    saved_files.append({'name': filename, 'path': filepath})

        # Create notification
        db.execute("""
            INSERT INTO notifications (brand_id, notification_type, title, message, due_date)
            VALUES (?, 'system', ?, ?, ?)
        """, (brand_id, 'Gemini: Image Generated',
              f'Generated {len(saved_files)} image(s) from prompt: {prompt[:100]}...',
              datetime.now().strftime('%Y-%m-%d')))
        db.commit()

        return jsonify({'ok': True, 'files': saved_files, 'count': len(saved_files)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/apps-script/run-local', methods=['POST'])
def run_apps_script_local():
    """Execute Google automation in-app: read briefs from folder, generate images via Gemini,
    and save results. Replaces the need for external Google Apps Script."""
    db = get_db()
    data = request.json
    brand_id = data.get('brand_id')
    step_id = data.get('step_id')

    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'ok': False, 'error': 'Brand folder not configured'}), 400

    gemini_key = get_setting(db, 'gemini_api_key')
    if not gemini_key:
        return jsonify({'ok': False, 'error': 'Gemini API key not configured'}), 400

    briefs_folder = os.path.join(brand['folder_path'], '10_Pipeline', 'Briefs')
    output_folder = os.path.join(brand['folder_path'], '10_Pipeline', 'Generated_Images')
    os.makedirs(output_folder, exist_ok=True)

    if not os.path.exists(briefs_folder):
        return jsonify({'ok': False, 'error': 'No briefs folder found'}), 400

    # Read all briefs
    briefs = []
    for f in sorted(os.listdir(briefs_folder)):
        if f.endswith('.txt') or f.endswith('.md'):
            filepath = os.path.join(briefs_folder, f)
            with open(filepath, 'r') as fh:
                content = fh.read()
            briefs.append({'filename': f, 'content': content})

    if not briefs:
        return jsonify({'ok': False, 'error': 'No briefs found to process'}), 400

    processed = []
    errors = []
    for brief in briefs:
        # Parse brief fields
        lines = brief['content'].strip().split('\n')
        topic = ''
        image_style = 'single'
        post_text = ''
        in_post_text = False
        for line in lines:
            if line.startswith('TOPIC:'):
                topic = line.replace('TOPIC:', '').strip()
            elif line.startswith('IMAGE_STYLE:'):
                image_style = line.replace('IMAGE_STYLE:', '').strip()
            elif line.startswith('POST_TEXT:'):
                in_post_text = True
            elif in_post_text:
                post_text += line + '\n'

        if not topic:
            continue

        # Build image generation prompt
        img_prompt = f"""Create a professional LinkedIn post image for: {topic}
Brand: {brand['name']}
Style: Dark, futuristic, data-driven tech aesthetic with subtle blue/orange tones.
Text overlay: Keep minimal. Clean, modern design suitable for LinkedIn.
Post context: {post_text[:300]}"""

        try:
            import urllib.request
            api_url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key={gemini_key}'
            req_body = json.dumps({
                'contents': [{'parts': [{'text': img_prompt}]}],
                'generationConfig': {'responseModalities': ['TEXT', 'IMAGE']}
            })
            req = urllib.request.Request(api_url, data=req_body.encode(),
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode())

            if result.get('candidates'):
                parts = result['candidates'][0].get('content', {}).get('parts', [])
                for i, part in enumerate(parts):
                    if part.get('inlineData'):
                        img_data = base64.b64decode(part['inlineData']['data'])
                        slug = topic.lower().replace(' ', '_')[:30]
                        filename = f"{slug}_{datetime.now().strftime('%Y%m%d')}_{i}.png"
                        filepath = os.path.join(output_folder, filename)
                        with open(filepath, 'wb') as fh:
                            fh.write(img_data)
                        processed.append({'brief': brief['filename'], 'image': filename})
        except Exception as e:
            errors.append({'brief': brief['filename'], 'error': str(e)})

    # Create notification
    db.execute("""
        INSERT INTO notifications (brand_id, notification_type, title, message, due_date)
        VALUES (?, 'system', ?, ?, ?)
    """, (brand_id, 'Pipeline: Images Generated',
          f'Processed {len(processed)} brief(s), {len(errors)} error(s).',
          datetime.now().strftime('%Y-%m-%d')))
    db.commit()

    return jsonify({'ok': True, 'processed': processed, 'errors': errors})


@app.route('/api/claude/run', methods=['POST'])
def run_claude_step():
    """Call Claude API to execute an automated workflow step."""
    db = get_db()
    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Anthropic API key not configured. Go to Settings to add it.'}), 400

    data = request.json
    step_name = data.get('step_name', '')
    brand_id = data.get('brand_id')
    prompt = data.get('prompt', '')

    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return jsonify({'ok': False, 'error': 'Brand not found'}), 404

    # Call Claude API
    try:
        import urllib.request
        req_body = json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 4096,
            'messages': [{'role': 'user', 'content': prompt}]
        })
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=req_body.encode(),
            headers={
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
            }
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            response_text = result['content'][0]['text'] if result.get('content') else ''

        # Create notification
        db.execute("""
            INSERT INTO notifications (brand_id, notification_type, title, message, due_date)
            VALUES (?, 'system', ?, ?, ?)
        """, (brand_id, f'Claude: {step_name}',
              f'Claude completed "{step_name}". Response: {response_text[:200]}...',
              datetime.now().strftime('%Y-%m-%d')))
        db.commit()

        return jsonify({'ok': True, 'response': response_text, 'step_name': step_name})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Client Review & PDF Export ────────────────────────────────────

@app.route('/brands/<int:brand_id>/client-review')
def client_review(brand_id):
    """Print-friendly client review page with all review/ready content."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return 'Brand not found', 404

    # Get content items that need or are ready for client review
    items = db.execute("""
        SELECT ci.*, cp.name as pillar_name, cp.color as pillar_color
        FROM content_items ci
        LEFT JOIN content_pillars cp ON ci.pillar_id = cp.id
        WHERE ci.brand_id = ? AND ci.status IN ('review', 'ready')
        ORDER BY ci.publish_date ASC, ci.created_at DESC
    """, (brand_id,)).fetchall()

    # Get any pending feedback for these items
    item_feedback = {}
    for item in items:
        fb = db.execute("""
            SELECT * FROM feedback WHERE content_item_id = ? ORDER BY created_at DESC
        """, (item['id'],)).fetchall()
        item_feedback[item['id']] = fb

    # Scan for associated image files
    item_images = {}
    base_folder = brand['folder_path'] or ''
    img_folder = os.path.join(base_folder, '10_Pipeline', 'Generated_Images')
    content_folder = os.path.join(base_folder, '04_Content', 'LinkedIn')
    for item in items:
        images = []
        slug = item['title'].lower().replace(' ', '_')[:20] if item['title'] else ''
        for folder in [img_folder, content_folder]:
            if os.path.isdir(folder):
                for f in os.listdir(folder):
                    if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) and (
                        slug in f.lower() or not slug):
                        images.append(os.path.join(folder, f))
                    if len(images) >= 3:
                        break
        item_images[item['id']] = images[:3]

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    return render_template('brands/client_review.html',
                           brand=brand, items=items, item_feedback=item_feedback,
                           item_images=item_images, unread_notifications=unread_notifications)


@app.route('/api/brands/<int:brand_id>/export-review-pdf', methods=['POST'])
def export_review_pdf(brand_id):
    """Generate a print-friendly HTML review document and save to Client_Review folder."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return jsonify({'ok': False, 'error': 'Brand not found'}), 404

    items = db.execute("""
        SELECT ci.*, cp.name as pillar_name
        FROM content_items ci
        LEFT JOIN content_pillars cp ON ci.pillar_id = cp.id
        WHERE ci.brand_id = ? AND ci.status IN ('review', 'ready')
        ORDER BY ci.publish_date ASC
    """, (brand_id,)).fetchall()

    if not items:
        return jsonify({'ok': False, 'error': 'No content items in review/ready status'})

    # Build a clean HTML review document
    today = datetime.now().strftime('%Y-%m-%d')
    html_parts = [f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{brand['name']} — Content Review {today}</title>
<style>
body {{ font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 40px; color: #333; }}
h1 {{ border-bottom: 3px solid {brand['primary_color'] or '#000'}; padding-bottom: 10px; }}
.item {{ page-break-inside: avoid; border: 1px solid #ddd; border-radius: 8px; padding: 20px; margin: 20px 0; }}
.item h3 {{ margin-top: 0; color: {brand['primary_color'] or '#000'}; }}
.meta {{ font-size: 0.85em; color: #666; margin-bottom: 12px; }}
.body-text {{ white-space: pre-wrap; line-height: 1.6; background: #f9f9f9; padding: 15px; border-radius: 6px; }}
.feedback-box {{ border: 2px dashed #ccc; border-radius: 6px; padding: 15px; margin-top: 15px; min-height: 60px; }}
.feedback-box p {{ margin: 0; font-size: 0.85em; color: #999; }}
.footer {{ margin-top: 40px; padding-top: 15px; border-top: 1px solid #ddd; font-size: 0.8em; color: #999; text-align: center; }}
@media print {{ .no-print {{ display: none; }} body {{ padding: 20px; }} }}
</style></head><body>
<h1>{brand['name']} — Content Review</h1>
<p style="color:#666">Generated: {today} &middot; {len(items)} item(s) for review</p>
<hr>
"""]

    for idx, item in enumerate(items, 1):
        html_parts.append(f"""
<div class="item">
    <h3>{idx}. {item['title'] or 'Untitled'}</h3>
    <div class="meta">
        Type: <strong>{item['content_type'] or 'N/A'}</strong> &middot;
        Market: <strong>{item['market'] or 'N/A'}</strong> &middot;
        Pillar: <strong>{item['pillar_name'] or 'N/A'}</strong> &middot;
        Status: <strong>{item['status']}</strong>
        {f' &middot; Publish: <strong>{item["publish_date"]}</strong>' if item['publish_date'] else ''}
    </div>
    <div class="body-text">{item['body_text'] or '(No content yet)'}</div>
    <div class="feedback-box">
        <p>Client Feedback:</p>
    </div>
</div>""")

    html_parts.append(f"""
<div class="footer">
    {brand['name']} Content Review &middot; {today} &middot; Generated by DigitalAIzeme
</div>
</body></html>""")

    html_content = '\n'.join(html_parts)

    # Save to Client_Review folder
    base = brand['folder_path'] or ''
    review_folder = os.path.join(base, '10_Pipeline', 'Client_Review')
    os.makedirs(review_folder, exist_ok=True)
    filename = f"review_{brand['name'].lower().replace(' ', '_')}_{today}.html"
    filepath = os.path.join(review_folder, filename)
    with open(filepath, 'w') as f:
        f.write(html_content)

    return jsonify({
        'ok': True,
        'file': filepath,
        'filename': filename,
        'items_count': len(items),
        'message': f'Review document saved to {filepath}. Open in browser and print to PDF.'
    })


@app.route('/api/brands/<int:brand_id>/review-needed', methods=['GET'])
def check_review_needed(brand_id):
    """Check if any content items need client review."""
    db = get_db()
    items = db.execute("""
        SELECT COUNT(*) as cnt, status FROM content_items
        WHERE brand_id = ? AND status IN ('review', 'ready')
        GROUP BY status
    """, (brand_id,)).fetchall()

    review_count = sum(row['cnt'] for row in items)
    return jsonify({
        'ok': True,
        'needs_review': review_count > 0,
        'count': review_count,
        'breakdown': {row['status']: row['cnt'] for row in items}
    })


# ─── Brand Onboarding Wizard ──────────────────────────────────────

FILE_CATEGORIES = {
    'brand': {
        'folder': '01_Brand',
        'extensions': ['.pdf', '.ai', '.eps', '.svg', '.png', '.jpg', '.psd'],
        'keywords': ['brand', 'logo', 'guideline', 'identity', 'style guide', 'color', 'font'],
    },
    'product': {
        'folder': '02_Product',
        'extensions': ['.pdf', '.docx', '.md', '.txt', '.pptx'],
        'keywords': ['product', 'feature', 'spec', 'postap', 'pos', 'kiosk', 'menu', 'kds'],
    },
    'strategy': {
        'folder': '03_Strategy',
        'extensions': ['.pdf', '.docx', '.md', '.txt', '.pptx', '.xlsx'],
        'keywords': ['strategy', 'plan', 'calendar', 'proposal', 'roadmap', 'campaign', 'marketing'],
    },
    'content': {
        'folder': '04_Content',
        'extensions': ['.md', '.txt', '.docx'],
        'keywords': ['post', 'blog', 'article', 'linkedin', 'social', 'carousel', 'reel', 'content'],
    },
    'sales': {
        'folder': '05_Sales',
        'extensions': ['.pdf', '.xlsx', '.csv', '.docx', '.pptx'],
        'keywords': ['sales', 'lead', 'prospect', 'outreach', 'playbook', 'deal', 'pitch'],
    },
    'analytics': {
        'folder': '07_Analytics',
        'extensions': ['.xlsx', '.csv', '.pdf'],
        'keywords': ['analytics', 'report', 'performance', 'metric', 'dashboard', 'insight'],
    },
    'research': {
        'folder': '08_Research',
        'extensions': ['.pdf', '.docx', '.md', '.txt', '.xlsx'],
        'keywords': ['research', 'competitor', 'market', 'keyword', 'trend', 'analysis'],
    },
    'admin': {
        'folder': '09_Admin',
        'extensions': ['.pdf', '.docx', '.xlsx'],
        'keywords': ['contract', 'invoice', 'agreement', 'nda', 'onboard', 'sow'],
    },
}


@app.route('/brands/<int:brand_id>/onboarding')
def brand_onboarding(brand_id):
    """Onboarding wizard: scan, organize, analyze, recommend."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return redirect(url_for('brands_list'))

    # Check current onboarding status
    pillars = db.execute("SELECT * FROM content_pillars WHERE brand_id=?", (brand_id,)).fetchall()
    cadences = db.execute("SELECT * FROM cadence_rules WHERE brand_id=?", (brand_id,)).fetchall()
    voice_refs = db.execute("SELECT * FROM brand_voice_references WHERE brand_id=?", (brand_id,)).fetchall()
    folder_exists = bool(brand['folder_path'] and os.path.isdir(brand['folder_path']))

    # Calculate onboarding score
    steps = {
        'folder': {'label': 'Folder Structure', 'done': folder_exists, 'weight': 15},
        'files_organized': {'label': 'Files Organized', 'done': False, 'weight': 15},
        'brand_voice': {'label': 'Brand Voice', 'done': len(voice_refs) >= 3, 'weight': 20},
        'pillars': {'label': 'Content Pillars', 'done': len(pillars) >= 3, 'weight': 15},
        'cadence': {'label': 'Publishing Cadence', 'done': len(cadences) >= 1, 'weight': 10},
        'voice_summary': {'label': 'Voice Summary', 'done': bool(brand['voice_summary']), 'weight': 10},
        'website': {'label': 'Website', 'done': bool(brand['website']), 'weight': 5},
        'colors': {'label': 'Brand Colors', 'done': brand['primary_color'] != '#000000' or brand['accent_color'] != '#6366f1', 'weight': 5},
        'workflow': {'label': 'Workflow Template', 'done': bool(db.execute("SELECT 1 FROM workflow_templates WHERE brand_id=?", (brand_id,)).fetchone()), 'weight': 5},
    }

    # Check files organization
    if folder_exists:
        organized_count = 0
        for cat_info in FILE_CATEGORIES.values():
            cat_path = os.path.join(brand['folder_path'], cat_info['folder'])
            if os.path.isdir(cat_path) and os.listdir(cat_path):
                organized_count += 1
        steps['files_organized']['done'] = organized_count >= 3

    total_weight = sum(s['weight'] for s in steps.values())
    earned_weight = sum(s['weight'] for s in steps.values() if s['done'])
    score = int((earned_weight / total_weight) * 100) if total_weight else 0

    unread_notifications = db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    return render_template('brands/onboarding.html',
                           brand=brand, steps=steps, score=score,
                           pillars=pillars, cadences=cadences, voice_refs=voice_refs,
                           folder_exists=folder_exists, unread_notifications=unread_notifications)


@app.route('/api/brands/<int:brand_id>/onboard/scan', methods=['POST'])
def onboard_scan(brand_id):
    """Scan the brand folder and categorize all files."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand or not brand['folder_path'] or not os.path.isdir(brand['folder_path']):
        return jsonify({'ok': False, 'error': 'No valid folder path configured'})

    base = brand['folder_path']
    files = []
    for root, dirs, filenames in os.walk(base):
        # Skip hidden dirs and already-organized standard folders
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        rel_root = os.path.relpath(root, base)
        for fname in filenames:
            if fname.startswith('.'):
                continue
            filepath = os.path.join(root, fname)
            ext = os.path.splitext(fname)[1].lower()
            size_kb = os.path.getsize(filepath) / 1024
            rel_path = os.path.relpath(filepath, base)

            # Already in a standard folder?
            in_standard = any(rel_path.startswith(cat['folder']) for cat in FILE_CATEGORIES.values())

            # Suggest category
            suggested_cat = None
            fname_lower = fname.lower()
            for cat_name, cat_info in FILE_CATEGORIES.items():
                if ext in cat_info['extensions']:
                    for kw in cat_info['keywords']:
                        if kw in fname_lower or kw in rel_root.lower():
                            suggested_cat = cat_name
                            break
                if suggested_cat:
                    break
            # Fallback: match by extension
            if not suggested_cat:
                for cat_name, cat_info in FILE_CATEGORIES.items():
                    if ext in cat_info['extensions']:
                        suggested_cat = cat_name
                        break

            files.append({
                'path': rel_path,
                'name': fname,
                'ext': ext,
                'size_kb': round(size_kb, 1),
                'in_standard': in_standard,
                'suggested_category': suggested_cat,
                'suggested_folder': FILE_CATEGORIES[suggested_cat]['folder'] if suggested_cat else None,
            })

    # Summary
    unorganized = [f for f in files if not f['in_standard'] and f['suggested_category']]
    organized = [f for f in files if f['in_standard']]
    return jsonify({
        'ok': True,
        'total_files': len(files),
        'organized': len(organized),
        'unorganized': len(unorganized),
        'files': files,
        'unorganized_files': unorganized,
    })


@app.route('/api/brands/<int:brand_id>/onboard/organize', methods=['POST'])
def onboard_organize(brand_id):
    """Move unorganized files to their suggested folders."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'ok': False, 'error': 'No folder path configured'})

    data = request.json
    files_to_move = data.get('files', [])
    base = brand['folder_path']
    moved = []
    errors = []

    for f in files_to_move:
        src = os.path.join(base, f['path'])
        dest_folder = os.path.join(base, f['target_folder'])
        os.makedirs(dest_folder, exist_ok=True)
        dest = os.path.join(dest_folder, f['name'])
        try:
            if os.path.exists(src) and not os.path.exists(dest):
                import shutil
                shutil.move(src, dest)
                moved.append({'from': f['path'], 'to': os.path.relpath(dest, base)})
            elif os.path.exists(dest):
                errors.append({'file': f['path'], 'error': 'File already exists in destination'})
        except Exception as e:
            errors.append({'file': f['path'], 'error': str(e)})

    return jsonify({'ok': True, 'moved': len(moved), 'errors': len(errors), 'details': moved, 'error_details': errors})


@app.route('/api/brands/<int:brand_id>/onboard/analyze', methods=['POST'])
def onboard_analyze(brand_id):
    """Use Claude to analyze brand documents and generate recommendations."""
    db = get_db()
    api_key = get_setting(db, 'anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Anthropic API key not configured. Go to Settings.'}), 400

    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return jsonify({'ok': False, 'error': 'Brand not found'}), 404

    data = request.json
    analysis_type = data.get('type', 'all')  # all, voice, pillars, cadence

    # Gather context from brand folder
    base = brand['folder_path'] or ''
    context_texts = []

    # Read text from key folders
    scan_folders = {
        '01_Brand': 'Brand guidelines and identity',
        '02_Product': 'Product documentation',
        '03_Strategy': 'Marketing strategy',
        '04_Content': 'Existing content samples',
    }
    for folder_name, purpose in scan_folders.items():
        folder_path = os.path.join(base, folder_name)
        if os.path.isdir(folder_path):
            for fname in os.listdir(folder_path)[:5]:  # Max 5 files per folder
                fpath = os.path.join(folder_path, fname)
                if os.path.isfile(fpath) and fname.endswith(('.txt', '.md')):
                    try:
                        with open(fpath, 'r', errors='ignore') as fh:
                            text = fh.read()[:2000]
                            context_texts.append(f"[{purpose} — {fname}]:\n{text}")
                    except Exception:
                        pass

    brand_context = f"""Brand: {brand['name']}
Tagline: {brand['tagline'] or 'Not set'}
Website: {brand['website'] or 'Not set'}
Voice Summary: {brand['voice_summary'] or 'Not set'}
Primary Color: {brand['primary_color']}
Accent Color: {brand['accent_color']}

Documents found:
{'---'.join(context_texts[:10]) if context_texts else 'No text documents found in folders.'}"""

    prompts = {}
    if analysis_type in ('all', 'voice'):
        prompts['voice'] = f"""Analyze this brand and generate brand voice guidelines.

{brand_context}

Return a JSON object with exactly these fields:
{{
  "voice_summary": "2-3 sentence brand voice summary",
  "tone_keywords": ["keyword1", "keyword2", ... (5-8 keywords)],
  "dos": ["writing guideline 1", "guideline 2", ... (4-6 items)],
  "donts": ["thing to avoid 1", "avoid 2", ... (4-6 items)],
  "sample_phrases": ["example phrase 1", "phrase 2", ... (3-5 examples)]
}}
Return ONLY valid JSON, no markdown."""

    if analysis_type in ('all', 'pillars'):
        prompts['pillars'] = f"""Analyze this brand and suggest 5-7 content pillars for their social media strategy.

{brand_context}

Return a JSON array of pillar objects:
[
  {{"name": "Pillar Name", "description": "One-line description", "color": "#hexcolor", "reason": "Why this pillar matters"}}
]
Use professional hex colors. Return ONLY valid JSON, no markdown."""

    if analysis_type in ('all', 'cadence'):
        prompts['cadence'] = f"""Analyze this brand and recommend a publishing cadence for their social media.

{brand_context}

Return a JSON array of cadence rules:
[
  {{"name": "Channel Name", "channel": "LinkedIn", "posts_per_week": 3, "preferred_days": ["Monday", "Wednesday", "Friday"], "preferred_times": ["09:00", "12:00"], "notes": "Reason for this schedule"}}
]
Return ONLY valid JSON, no markdown."""

    results = {}
    for key, prompt in prompts.items():
        try:
            import urllib.request
            req_body = json.dumps({
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 4096,
                'messages': [{'role': 'user', 'content': prompt}]
            })
            req = urllib.request.Request(
                'https://api.anthropic.com/v1/messages',
                data=req_body.encode(),
                headers={
                    'Content-Type': 'application/json',
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01',
                }
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                result = json.loads(resp.read().decode())
                text = result['content'][0]['text'] if result.get('content') else ''
                # Parse JSON from response
                text = text.strip()
                if text.startswith('```'):
                    text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
                results[key] = json.loads(text)
        except Exception as e:
            results[key] = {'error': str(e)}

    return jsonify({'ok': True, 'recommendations': results})


@app.route('/api/brands/<int:brand_id>/onboard/apply', methods=['POST'])
def onboard_apply(brand_id):
    """Apply onboarding recommendations — pillars, cadence, voice."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand:
        return jsonify({'ok': False, 'error': 'Brand not found'}), 404

    data = request.json
    applied = []

    # Apply voice recommendations
    if 'voice' in data:
        voice = data['voice']
        if voice.get('voice_summary'):
            db.execute("UPDATE brands SET voice_summary=? WHERE id=?", (voice['voice_summary'], brand_id))
            applied.append('voice_summary')
        for kw in voice.get('tone_keywords', []):
            db.execute("INSERT INTO brand_voice_references (brand_id, ref_type, content, source) VALUES (?,?,?,?)",
                       (brand_id, 'tone_keyword', kw, 'AI onboarding analysis'))
        if voice.get('tone_keywords'):
            applied.append('tone_keywords')
        for item in voice.get('dos', []):
            db.execute("INSERT INTO brand_voice_references (brand_id, ref_type, content, source) VALUES (?,?,?,?)",
                       (brand_id, 'do', item, 'AI onboarding analysis'))
        if voice.get('dos'):
            applied.append('dos')
        for item in voice.get('donts', []):
            db.execute("INSERT INTO brand_voice_references (brand_id, ref_type, content, source) VALUES (?,?,?,?)",
                       (brand_id, 'dont', item, 'AI onboarding analysis'))
        if voice.get('donts'):
            applied.append('donts')
        for phrase in voice.get('sample_phrases', []):
            db.execute("INSERT INTO brand_voice_references (brand_id, ref_type, content, source) VALUES (?,?,?,?)",
                       (brand_id, 'text_sample', phrase, 'AI onboarding analysis'))
        if voice.get('sample_phrases'):
            applied.append('sample_phrases')

    # Apply pillar recommendations
    if 'pillars' in data:
        # Clear existing AI-generated pillars (keep manually created ones)
        for p in data['pillars']:
            max_order = db.execute("SELECT COALESCE(MAX(sort_order),0) FROM content_pillars WHERE brand_id=?",
                                   (brand_id,)).fetchone()[0]
            db.execute("INSERT INTO content_pillars (brand_id, name, description, color, sort_order) VALUES (?,?,?,?,?)",
                       (brand_id, p['name'], p.get('description', ''), p.get('color', '#6366f1'), max_order + 1))
        applied.append(f'pillars ({len(data["pillars"])})')

    # Apply cadence recommendations
    if 'cadence' in data:
        for c in data['cadence']:
            db.execute("""
                INSERT INTO cadence_rules (brand_id, name, channel, posts_per_week, preferred_days, preferred_times, notes)
                VALUES (?,?,?,?,?,?,?)
            """, (brand_id, c['name'], c.get('channel', 'LinkedIn'), c.get('posts_per_week', 3),
                  json.dumps(c.get('preferred_days', [])), json.dumps(c.get('preferred_times', [])),
                  c.get('notes', '')))
        applied.append(f'cadence ({len(data["cadence"])})')

    db.commit()
    return jsonify({'ok': True, 'applied': applied})


@app.route('/api/brands/<int:brand_id>/onboard/sync-from-folders', methods=['POST'])
def onboard_sync_from_folders(brand_id):
    """Sync changes made externally (e.g. via Cowork) back into the app."""
    db = get_db()
    brand = db.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'ok': False, 'error': 'No folder path configured'})

    base = brand['folder_path']
    synced = []

    # Check for brand voice file in 01_Brand
    voice_file = os.path.join(base, '01_Brand', 'brand_voice.md')
    if os.path.isfile(voice_file):
        with open(voice_file, 'r', errors='ignore') as f:
            voice_text = f.read().strip()
        if voice_text:
            db.execute("UPDATE brands SET voice_summary=? WHERE id=?", (voice_text[:500], brand_id))
            synced.append('brand_voice.md → voice summary')

    # Check for pillars file in 03_Strategy
    pillars_file = os.path.join(base, '03_Strategy', 'content_pillars.md')
    if os.path.isfile(pillars_file):
        with open(pillars_file, 'r', errors='ignore') as f:
            pillars_text = f.read().strip()
        if pillars_text:
            # Parse simple format: "# Pillar Name\nDescription\n"
            lines = pillars_text.split('\n')
            new_pillars = []
            current = None
            for line in lines:
                if line.startswith('# ') or line.startswith('## '):
                    if current:
                        new_pillars.append(current)
                    current = {'name': line.lstrip('#').strip(), 'description': ''}
                elif current and line.strip():
                    current['description'] += line.strip() + ' '
            if current:
                new_pillars.append(current)
            if new_pillars:
                # Delete old pillars and replace
                db.execute("DELETE FROM content_pillars WHERE brand_id=?", (brand_id,))
                colors = ['#EF4324', '#3B82F6', '#10B981', '#8B5CF6', '#F59E0B', '#EC4899', '#14B8A6', '#F97316']
                for i, p in enumerate(new_pillars):
                    db.execute("INSERT INTO content_pillars (brand_id, name, description, color, sort_order) VALUES (?,?,?,?,?)",
                               (brand_id, p['name'], p['description'].strip(), colors[i % len(colors)], i))
                synced.append(f'content_pillars.md → {len(new_pillars)} pillars')

    # Check for cadence file in 03_Strategy
    cadence_file = os.path.join(base, '03_Strategy', 'cadence.md')
    if os.path.isfile(cadence_file):
        with open(cadence_file, 'r', errors='ignore') as f:
            cadence_text = f.read().strip()
        if cadence_text and '# ' in cadence_text:
            synced.append('cadence.md → found (manual review recommended)')

    # Sync content files from 04_Content
    content_folder = os.path.join(base, '04_Content')
    new_content = 0
    if os.path.isdir(content_folder):
        for root, _, files in os.walk(content_folder):
            for fname in files:
                if not fname.endswith(('.md', '.txt')):
                    continue
                filepath = os.path.join(root, fname)
                rel_path = os.path.relpath(filepath, base)
                # Check if already imported
                existing = db.execute("SELECT 1 FROM content_items WHERE brand_id=? AND title=?",
                                      (brand_id, fname.rsplit('.', 1)[0])).fetchone()
                if existing:
                    continue
                try:
                    with open(filepath, 'r', errors='ignore') as f:
                        text = f.read().strip()
                    if text and len(text) > 20:
                        title = fname.rsplit('.', 1)[0].replace('_', ' ').replace('-', ' ')
                        db.execute("""
                            INSERT INTO content_items (brand_id, title, body_text, content_type, status, market)
                            VALUES (?,?,?,?,?,?)
                        """, (brand_id, title, text, 'linkedin_post', 'backlog', 'APAC General'))
                        new_content += 1
                except Exception:
                    pass
    if new_content:
        synced.append(f'04_Content → {new_content} new content items')

    db.commit()
    return jsonify({'ok': True, 'synced': synced, 'changes': len(synced)})


# ─── Canva Integration ─────────────────────────────────────────────

DESIGN_TYPE_MAP = {
    'linkedin_post': 'InstagramPost',
    'carousel': 'Presentation',
    'blog': 'BlogGraphic',
    'email': 'EmailHeader',
    'reel': 'InstagramPost',
    'video': 'InstagramPost',
}

@app.route('/api/canva/prepare-polish', methods=['POST'])
def canva_prepare_polish():
    """Create a Canva polish job with all context Claude Code needs for MCP execution."""
    db = get_db()
    data = request.json
    brand_id = data.get('brand_id')
    content_item_id = data.get('content_item_id')
    source_image_path = data.get('source_image_path', '')
    design_type = data.get('design_type', 'social_media')
    custom_prompt = data.get('custom_prompt', '')

    brand = db.execute('SELECT * FROM brands WHERE id = ?', (brand_id,)).fetchone()
    if not brand:
        return jsonify({'ok': False, 'error': 'Brand not found'}), 404

    # Build context for Claude Code MCP workflow
    content_item = None
    if content_item_id:
        content_item = db.execute('SELECT * FROM content_items WHERE id = ?', (content_item_id,)).fetchone()

    # Auto-generate prompt from content context
    if custom_prompt:
        prompt = custom_prompt
    else:
        parts = [f"Create a branded {design_type} design for {brand['name']}."]
        if content_item:
            parts.append(f"Title: {content_item['title']}")
            if content_item['visual_prompt']:
                parts.append(f"Visual style: {content_item['visual_prompt']}")
            ct = content_item['content_type'] or ''
            if ct:
                parts.append(f"Format: {ct}")
        parts.append(f"Brand colors: {brand['primary_color']} (primary), {brand['accent_color']} (accent).")
        parts.append("Include logo placement area, brand typography, and footer bar.")
        prompt = ' '.join(parts)

    brand_kit_id = brand['canva_brand_kit_id'] or ''

    db.execute('''INSERT INTO canva_designs (brand_id, content_item_id, design_type, prompt_used, brand_kit_id, status)
                  VALUES (?, ?, ?, ?, ?, 'pending')''',
               (brand_id, content_item_id, design_type, prompt, brand_kit_id))
    db.commit()
    design_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

    # Return structured job spec for Claude Code
    canva_design_type = DESIGN_TYPE_MAP.get(content_item['content_type'], 'InstagramPost') if content_item else 'InstagramPost'
    job_spec = {
        'ok': True,
        'design_record_id': design_id,
        'brand': {
            'name': brand['name'],
            'primary_color': brand['primary_color'],
            'accent_color': brand['accent_color'],
            'brand_kit_id': brand_kit_id,
        },
        'content': {
            'title': content_item['title'] if content_item else '',
            'body': (content_item['body_text'] or '')[:200] if content_item else '',
            'type': content_item['content_type'] if content_item else '',
            'market': content_item['market'] if content_item else 'APAC',
        },
        'source_image_path': source_image_path,
        'source_image_url': f'/media/{source_image_path}' if source_image_path else '',
        'canva_design_type': canva_design_type,
        'prompt': prompt,
        'mcp_steps': [
            '1. list-brand-kits → find brand kit (or use brand_kit_id if provided)',
            '2. upload-asset-from-url → upload source image to Canva',
            '3. generate-design → create branded design with brand_kit_id + asset',
            '4. create-design-from-candidate → add to Canva account',
            '5. start-editing-transaction → begin editing',
            '6. perform-editing-operations → replace text, adjust images, apply brand colors',
            '7. commit-editing-transaction → save changes',
            '8. export-design → export as PNG',
            f'9. PUT /api/canva/designs/{design_id} → update app with results',
        ],
    }
    return jsonify(job_spec)


@app.route('/api/canva/designs/<int:design_id>', methods=['PUT'])
def canva_update_design(design_id):
    """Update a Canva design record after MCP operations complete."""
    db = get_db()
    design = db.execute('SELECT * FROM canva_designs WHERE id = ?', (design_id,)).fetchone()
    if not design:
        return jsonify({'ok': False, 'error': 'Design not found'}), 404

    data = request.json
    updates = []
    params = []
    for field in ['canva_design_id', 'canva_design_url', 'thumbnail_url', 'export_url', 'export_path', 'status', 'brand_kit_id']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])

    if data.get('status') in ('exported', 'qc_passed'):
        updates.append('completed_at = CURRENT_TIMESTAMP')

    if updates:
        params.append(design_id)
        db.execute(f'UPDATE canva_designs SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()

    return jsonify({'ok': True})


@app.route('/api/canva/designs', methods=['GET'])
def canva_list_designs():
    """List Canva designs for a brand, optionally filtered by content item."""
    db = get_db()
    brand_id = request.args.get('brand_id')
    content_item_id = request.args.get('content_item_id')

    query = 'SELECT cd.*, ci.title as content_title FROM canva_designs cd LEFT JOIN content_items ci ON cd.content_item_id = ci.id WHERE cd.brand_id = ?'
    params = [brand_id]
    if content_item_id:
        query += ' AND cd.content_item_id = ?'
        params.append(content_item_id)
    query += ' ORDER BY cd.created_at DESC'

    designs = [dict(row) for row in db.execute(query, params).fetchall()]
    return jsonify({'ok': True, 'designs': designs})


@app.route('/api/canva/qc/<int:design_id>', methods=['POST'])
def canva_qc(design_id):
    """Record QC results for a Canva design."""
    db = get_db()
    design = db.execute('SELECT * FROM canva_designs WHERE id = ?', (design_id,)).fetchone()
    if not design:
        return jsonify({'ok': False, 'error': 'Design not found'}), 404

    data = request.json
    checklist = data.get('checklist', [])
    notes = data.get('notes', '')
    all_passed = all(item.get('passed', False) for item in checklist)
    status = 'qc_passed' if all_passed else 'qc_failed'

    db.execute('''UPDATE canva_designs SET qc_result = ?, qc_notes = ?, status = ?, completed_at = CURRENT_TIMESTAMP
                  WHERE id = ?''', (json.dumps(checklist), notes, status, design_id))

    # Auto-advance content item status if QC passed
    if all_passed and design['content_item_id']:
        item = db.execute('SELECT status FROM content_items WHERE id = ?', (design['content_item_id'],)).fetchone()
        if item and item['status'] == 'visuals':
            db.execute("UPDATE content_items SET status = 'review', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                       (design['content_item_id'],))

    db.commit()
    return jsonify({'ok': True, 'status': status, 'all_passed': all_passed})


@app.route('/api/canva/designs/<int:design_id>', methods=['DELETE'])
def canva_delete_design(design_id):
    """Delete a Canva design record."""
    db = get_db()
    db.execute('DELETE FROM canva_designs WHERE id = ?', (design_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/brands/<int:brand_id>/canva-brand-kit', methods=['PUT'])
def update_canva_brand_kit(brand_id):
    """Save Canva brand kit ID for a brand."""
    db = get_db()
    data = request.json
    brand_kit_id = data.get('brand_kit_id', '')
    db.execute('UPDATE brands SET canva_brand_kit_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
               (brand_kit_id, brand_id))
    db.commit()
    return jsonify({'ok': True})


# ─── Free-Tier Tool Integrations ───────────────────────────────────

# --- Phase 1: MCP Job Specs (Excalidraw, Scheduled Tasks, GCal, Gmail) ---

@app.route('/api/excalidraw/job-spec', methods=['POST'])
def excalidraw_job_spec():
    """Generate a job spec for Claude Code to create an Excalidraw diagram via MCP."""
    data = request.json
    brand_id = data.get('brand_id')
    description = data.get('description', '')
    diagram_type = data.get('diagram_type', 'flowchart')
    content_item_id = data.get('content_item_id')
    db = get_db()
    brand = db.execute('SELECT * FROM brands WHERE id=?', (brand_id,)).fetchone()
    if not brand:
        return jsonify({'ok': False, 'error': 'Brand not found'}), 404
    context = {'brand_name': brand['name']}
    if content_item_id:
        item = db.execute('SELECT title, body FROM content_items WHERE id=?', (content_item_id,)).fetchone()
        if item:
            context['content_title'] = item['title']
            context['content_body'] = (item['body'] or '')[:500]
    return jsonify({
        'ok': True,
        'job_spec': {
            'tool': 'excalidraw',
            'mcp_action': 'create_view',
            'description': description,
            'diagram_type': diagram_type,
            'context': context,
            'save_to': f"10_Pipeline/Generated_Images/diagram_{diagram_type}_{int(datetime.now().timestamp())}.png",
            'instructions': f"Use the Excalidraw MCP create_view tool to create a {diagram_type} diagram: {description}. Brand: {brand['name']}."
        }
    })


@app.route('/api/gcal/job-spec', methods=['POST'])
def gcal_sync_job_spec():
    """Generate a job spec for Claude Code to sync content items to Google Calendar via MCP."""
    data = request.json
    brand_id = data.get('brand_id')
    content_item_ids = data.get('content_item_ids', [])
    db = get_db()
    brand = db.execute('SELECT * FROM brands WHERE id=?', (brand_id,)).fetchone()
    if not brand:
        return jsonify({'ok': False, 'error': 'Brand not found'}), 404
    if content_item_ids:
        placeholders = ','.join('?' * len(content_item_ids))
        items = db.execute(f'SELECT id, title, content_type, publish_date, status FROM content_items WHERE id IN ({placeholders})', content_item_ids).fetchall()
    else:
        items = db.execute('SELECT id, title, content_type, publish_date, status FROM content_items WHERE brand_id=? AND publish_date IS NOT NULL ORDER BY publish_date', (brand_id,)).fetchall()
    events = []
    for item in items:
        events.append({
            'title': f"[{brand['name']}] {item['title']}",
            'date': item['publish_date'],
            'description': f"Content type: {item['content_type']}\nStatus: {item['status']}\nDigitalAIzeme Content Studio",
            'content_item_id': item['id']
        })
    return jsonify({
        'ok': True,
        'job_spec': {
            'tool': 'google_calendar',
            'mcp_action': 'gcal_create_event',
            'brand': brand['name'],
            'events': events,
            'instructions': f"Use the Google Calendar MCP tools to create calendar events for {len(events)} content items from {brand['name']}. For each event, use gcal_create_event with the title, date (as all-day event), and description provided."
        }
    })


@app.route('/api/gmail/job-spec', methods=['POST'])
def gmail_draft_job_spec():
    """Generate a job spec for Claude Code to create a Gmail draft via MCP."""
    data = request.json
    content_item_id = data.get('content_item_id')
    db = get_db()
    item = db.execute('SELECT ci.*, b.name as brand_name FROM content_items ci JOIN brands b ON ci.brand_id=b.id WHERE ci.id=?', (content_item_id,)).fetchone()
    if not item:
        return jsonify({'ok': False, 'error': 'Content item not found'}), 404
    body_text = item['body'] or ''
    email_body = f"{body_text[:2000]}\n\n---\nContent from {item['brand_name']} | Generated by DigitalAIzeme"
    return jsonify({
        'ok': True,
        'job_spec': {
            'tool': 'gmail',
            'mcp_action': 'gmail_create_draft',
            'subject': f"{item['title']} — {item['brand_name']}",
            'body': email_body,
            'content_type': item['content_type'],
            'instructions': f"Use the Gmail MCP gmail_create_draft tool to create an email draft. Subject: \"{item['title']} — {item['brand_name']}\". Adapt the body content for email format — add a greeting, make it scannable, and include a CTA."
        }
    })


@app.route('/api/schedule/job-spec', methods=['POST'])
def schedule_job_spec():
    """Generate a job spec for Claude Code to create a scheduled task via MCP."""
    data = request.json
    step_id = data.get('step_id')
    frequency = data.get('frequency', 'weekly')
    brand_id = data.get('brand_id')
    db = get_db()
    step = db.execute('SELECT * FROM workflow_steps WHERE id=?', (step_id,)).fetchone()
    if not step:
        return jsonify({'ok': False, 'error': 'Step not found'}), 404
    brand = db.execute('SELECT name FROM brands WHERE id=?', (brand_id,)).fetchone()
    cron_map = {
        'daily': '0 9 * * *',
        'weekly': '0 9 * * 1',
        'biweekly': '0 9 * * 1/2',
        'monthly': '0 9 1 * *'
    }
    cron = cron_map.get(frequency, '0 9 * * 1')
    return jsonify({
        'ok': True,
        'job_spec': {
            'tool': 'scheduled_tasks',
            'mcp_action': 'create_scheduled_task',
            'step_name': step['name'],
            'brand': brand['name'] if brand else '',
            'frequency': frequency,
            'cron': cron,
            'instructions': f"Use the Scheduled Tasks MCP create_scheduled_task tool to schedule \"{step['name']}\" for {brand['name'] if brand else 'brand'} with cron expression '{cron}' ({frequency}). The task prompt should trigger this workflow step."
        }
    })


# --- Media Tag Registry API ---

@app.route('/api/media-tag-registry')
def get_media_tag_registry():
    """Return the media tag registry for frontend tag pickers."""
    registry = {}
    for tag, config in MEDIA_TAG_REGISTRY.items():
        registry[tag] = {
            'tool_name': config['tool_name'],
            'auto_capable': config.get('auto_capable', False),
            'run_at_status': config.get('run_at_status', ''),
            'description': config.get('description', ''),
            'icon': config.get('icon', 'fa-wrench'),
            'color': config.get('color', 'gray'),
            'api_keys_needed': config.get('api_keys_needed', []),
        }
    return jsonify({'ok': True, 'registry': registry})


@app.route('/api/content-items/<int:item_id>/tool-executions')
def get_tool_executions(item_id):
    """Get tool execution history for a content item."""
    db = get_db()
    execs = [dict(row) for row in db.execute(
        "SELECT * FROM tool_executions WHERE content_item_id = ? ORDER BY created_at DESC", (item_id,)
    ).fetchall()]
    return jsonify({'ok': True, 'executions': execs})


def _derive_tool_params(tag, item):
    """Derive API call parameters from a content item for a given media tag."""
    brand_id = item.get('brand_id')
    item_id = item.get('id')
    title = item.get('title', '')
    body = item.get('body_text', '')

    if tag == 'stock_image':
        return {'q': title, 'brand_id': brand_id}
    elif tag == 'tts':
        return {'text': body[:5000], 'brand_id': brand_id, 'content_item_id': item_id}
    elif tag.startswith('translate_'):
        lang_code = tag.split('_', 1)[1]  # translate_ja → ja
        return {'text': body[:5000], 'target': lang_code, 'brand_id': brand_id, 'content_item_id': item_id}
    elif tag == 'remove_bg':
        return {'brand_id': brand_id, 'content_item_id': item_id}
    elif tag == 'qr_code':
        return {'content_item_id': item_id}
    elif tag == 'short_url':
        return {'content_item_id': item_id}
    return {}


def _execute_tool_internal(tag, params, db):
    """Execute a tool internally without HTTP self-request. Returns result dict or raises."""
    import urllib.request, urllib.parse

    if tag == 'stock_image':
        # Search for top image, then download it
        query = params.get('q', '')
        brand_id = params.get('brand_id')
        source = 'unsplash'
        api_key = get_setting(db, 'unsplash_api_key', '')
        if not api_key:
            source = 'pexels'
            api_key = get_setting(db, 'pexels_api_key', '')
        if not api_key:
            raise ValueError('No stock image API key configured (Unsplash or Pexels)')

        if source == 'unsplash':
            url = f'https://api.unsplash.com/search/photos?query={urllib.parse.quote(query)}&per_page=1'
            req = urllib.request.Request(url, headers={'Authorization': f'Client-ID {api_key}'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            photos = data.get('results', [])
            if not photos:
                raise ValueError(f'No stock images found for: {query}')
            img_url = photos[0]['urls']['regular']
        else:
            url = f'https://api.pexels.com/v1/search?query={urllib.parse.quote(query)}&per_page=1'
            req = urllib.request.Request(url, headers={'Authorization': api_key})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            photos = data.get('photos', [])
            if not photos:
                raise ValueError(f'No stock images found for: {query}')
            img_url = photos[0]['src']['large']

        # Download the image
        brand = db.execute('SELECT folder_path FROM brands WHERE id=?', (brand_id,)).fetchone()
        if not brand or not brand['folder_path']:
            raise ValueError('Brand folder not configured')
        save_dir = os.path.join(brand['folder_path'], '10_Pipeline', 'Generated_Images')
        os.makedirs(save_dir, exist_ok=True)
        filename = f"stock_{source}_{int(datetime.now().timestamp())}.jpg"
        save_path = os.path.join(save_dir, filename)
        req = urllib.request.Request(img_url, headers={'User-Agent': 'DigitalAIzeme/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            with open(save_path, 'wb') as f:
                f.write(resp.read())
        return {'path': save_path, 'filename': filename, 'source': source}

    elif tag == 'tts':
        text = params.get('text', '')
        if not text:
            raise ValueError('No body text available for TTS')
        api_key = get_setting(db, 'elevenlabs_api_key', '')
        if not api_key:
            raise ValueError('ElevenLabs API key not configured')
        voice_id = '21m00Tcm4TlvDq8ikWAM'
        url = f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}'
        payload = json.dumps({'text': text, 'model_id': 'eleven_monolingual_v1', 'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75}})
        req = urllib.request.Request(url, data=payload.encode(), headers={'xi-api-key': api_key, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            audio_data = resp.read()
        brand = db.execute('SELECT folder_path FROM brands WHERE id=?', (params.get('brand_id'),)).fetchone()
        filename = f"tts_{params.get('content_item_id', 'audio')}_{int(datetime.now().timestamp())}.mp3"
        if brand and brand['folder_path']:
            save_dir = os.path.join(brand['folder_path'], '10_Pipeline')
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, filename)
        else:
            save_path = os.path.join(os.path.dirname(__file__), filename)
        with open(save_path, 'wb') as f:
            f.write(audio_data)
        return {'path': save_path, 'filename': filename, 'size': len(audio_data)}

    elif tag.startswith('translate_'):
        text = params.get('text', '')
        if not text:
            raise ValueError('No body text available for translation')
        api_key = get_setting(db, 'google_api_key', '')
        if not api_key:
            raise ValueError('Google API key not configured')
        target_lang = params.get('target', 'ja')
        url = f'https://translation.googleapis.com/language/translate/v2?key={api_key}'
        payload = json.dumps({'q': text, 'target': target_lang, 'format': 'text'})
        req = urllib.request.Request(url, data=payload.encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        translated = result['data']['translations'][0]['translatedText']
        # Create market adaptation record
        content_item_id = params.get('content_item_id')
        brand_id = params.get('brand_id')
        if content_item_id and brand_id:
            lang_names = {'ja': 'Japan', 'vi': 'Vietnam', 'ms': 'Singapore', 'th': 'Thailand', 'id': 'Indonesia', 'zh': 'China', 'ko': 'Korea'}
            market = lang_names.get(target_lang, target_lang)
            db.execute("""INSERT INTO market_adaptations (brand_id, content_item_id, target_market, adapted_title, adapted_body, language_code, status)
                VALUES (?, ?, ?, ?, ?, ?, 'draft')""", (brand_id, content_item_id, market, '', translated, target_lang))
        return {'translated_length': len(translated), 'target': target_lang}

    elif tag == 'remove_bg':
        # Find the most recent stock image downloaded for this content item
        content_item_id = params.get('content_item_id')
        prev = db.execute("""SELECT result_data FROM tool_executions
            WHERE content_item_id=? AND media_tag='stock_image' AND status='completed'
            ORDER BY created_at DESC LIMIT 1""", (content_item_id,)).fetchone()
        if not prev or not prev['result_data']:
            raise ValueError('No stock image found to remove background from')
        prev_result = json.loads(prev['result_data'])
        image_path = prev_result.get('path', '')
        if not os.path.exists(image_path):
            raise ValueError(f'Image file not found: {image_path}')
        api_key = get_setting(db, 'removebg_api_key', '')
        if not api_key:
            raise ValueError('Remove.bg API key not configured')
        with open(image_path, 'rb') as img_file:
            img_data = img_file.read()
        boundary = '----WebKitFormBoundary' + str(int(datetime.now().timestamp()))
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image_file"; filename="image.png"\r\nContent-Type: image/png\r\n\r\n').encode() + img_data + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="size"\r\n\r\nauto\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request('https://api.remove.bg/v1.0/removebg', data=body, headers={'X-Api-Key': api_key, 'Content-Type': f'multipart/form-data; boundary={boundary}'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            result_data = resp.read()
        out_name = os.path.splitext(os.path.basename(image_path))[0] + '_nobg.png'
        out_path = os.path.join(os.path.dirname(image_path), out_name)
        with open(out_path, 'wb') as f:
            f.write(result_data)
        return {'path': out_path, 'filename': out_name}

    elif tag == 'qr_code':
        content_item_id = params.get('content_item_id')
        item = db.execute('SELECT title, notes FROM content_items WHERE id=?', (content_item_id,)).fetchone()
        # Try to extract a URL from notes, fallback to placeholder
        url_target = ''
        if item and item['notes']:
            import re
            urls = re.findall(r'https?://\S+', item['notes'])
            if urls:
                url_target = urls[0]
        if not url_target:
            url_target = f'https://diqit.com/content/{content_item_id}'
        qr_url = f'https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={urllib.parse.quote(url_target)}'
        return {'qr_url': qr_url, 'original_url': url_target}

    elif tag == 'short_url':
        content_item_id = params.get('content_item_id')
        item = db.execute('SELECT notes FROM content_items WHERE id=?', (content_item_id,)).fetchone()
        # Find a URL to shorten from notes
        url_target = ''
        if item and item['notes']:
            import re
            urls = re.findall(r'https?://\S+', item['notes'])
            if urls:
                url_target = urls[0]
        if not url_target:
            url_target = f'https://diqit.com/content/{content_item_id}'
        api_url = f'https://tinyurl.com/api-create.php?url={urllib.parse.quote(url_target)}'
        req = urllib.request.Request(api_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            short_url = resp.read().decode().strip()
        db.execute('UPDATE content_items SET notes = COALESCE(notes, "") || ? WHERE id = ?',
                   (f'\nShort URL: {short_url}', content_item_id))
        return {'short_url': short_url, 'original_url': url_target}

    raise ValueError(f'Unknown tool tag: {tag}')


def _auto_run_tools_for_status(item_id, new_status, db):
    """Auto-run all applicable tools when a content item reaches a new status."""
    item = db.execute("SELECT * FROM content_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        return []
    media_tags = json.loads(item['media_tags'] or '[]')
    results = []

    for tag in media_tags:
        config = MEDIA_TAG_REGISTRY.get(tag)
        if not config or not config.get('auto_capable'):
            continue
        if config.get('run_at_status') != new_status:
            continue

        # Check if already executed
        existing = db.execute(
            "SELECT id FROM tool_executions WHERE content_item_id=? AND media_tag=? AND status='completed'",
            (item_id, tag)).fetchone()
        if existing:
            continue

        # Check API keys
        keys_needed = config.get('api_keys_needed', [])
        keys_mode = config.get('api_keys_mode', 'all')
        if keys_needed:
            if keys_mode == 'any':
                keys_ok = any(get_setting(db, k) for k in keys_needed)
            else:
                keys_ok = all(get_setting(db, k) for k in keys_needed)
            if not keys_ok:
                db.execute("""INSERT INTO tool_executions (content_item_id, tool_name, media_tag, trigger_mode, status, error_message)
                    VALUES (?, ?, ?, 'auto', 'skipped', 'API key not configured')""",
                    (item_id, config['tool_name'], tag))
                results.append({'tag': tag, 'status': 'skipped', 'error': 'API key not configured'})
                continue

        # Derive params and execute
        item_dict = dict(item)
        params = _derive_tool_params(tag, item_dict)
        db.execute("""INSERT INTO tool_executions (content_item_id, tool_name, media_tag, trigger_mode, status, input_params)
            VALUES (?, ?, ?, 'auto', 'running', ?)""",
            (item_id, config['tool_name'], tag, json.dumps(params)))
        exec_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        try:
            result = _execute_tool_internal(tag, params, db)
            db.execute("""UPDATE tool_executions SET status='completed', result_data=?, completed_at=CURRENT_TIMESTAMP
                WHERE id=?""", (json.dumps(result), exec_id))
            results.append({'tag': tag, 'status': 'completed', 'result': result})
        except Exception as e:
            db.execute("""UPDATE tool_executions SET status='failed', error_message=?, completed_at=CURRENT_TIMESTAMP
                WHERE id=?""", (str(e), exec_id))
            results.append({'tag': tag, 'status': 'failed', 'error': str(e)})

    return results


@app.route('/api/content-items/<int:item_id>/run-tools', methods=['POST'])
def run_content_tools(item_id):
    """Manually trigger tool execution for a content item at its current status."""
    db = get_db()
    item = db.execute("SELECT * FROM content_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        return jsonify({'ok': False, 'error': 'Content item not found'}), 404
    results = _auto_run_tools_for_status(item_id, item['status'], db)
    db.commit()
    return jsonify({'ok': True, 'results': results, 'status': item['status']})


@app.route('/api/content-items/<int:item_id>/media-tags', methods=['PUT'])
def update_media_tags(item_id):
    """Update media_tags for a content item."""
    db = get_db()
    data = request.json
    tags = data.get('media_tags', [])
    db.execute("UPDATE content_items SET media_tags=? WHERE id=?", (json.dumps(tags), item_id))
    db.commit()
    return jsonify({'ok': True})


# --- Phase 2: Direct API Routes (Stock Images, TTS, Cloudinary) ---

@app.route('/api/stock/search')
def stock_search():
    """Search stock images from Unsplash or Pexels."""
    query = request.args.get('q', '')
    source = request.args.get('source', 'unsplash')
    page = request.args.get('page', '1')
    if not query:
        return jsonify({'ok': False, 'error': 'Query required'}), 400
    db = get_db()
    results = []
    try:
        if source == 'unsplash':
            api_key = get_setting(db, 'unsplash_api_key', '')
            if not api_key:
                return jsonify({'ok': False, 'error': 'Unsplash API key not configured. Add it in Settings.'}), 400
            url = f'https://api.unsplash.com/search/photos?query={urllib.parse.quote(query)}&page={page}&per_page=12'
            req = urllib.request.Request(url, headers={'Authorization': f'Client-ID {api_key}'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            for photo in data.get('results', []):
                results.append({
                    'id': photo['id'], 'url': photo['urls']['regular'], 'thumb': photo['urls']['small'],
                    'alt': photo.get('alt_description', ''), 'author': photo['user']['name'],
                    'download_url': photo['urls']['regular'], 'source': 'unsplash'
                })
        elif source == 'pexels':
            api_key = get_setting(db, 'pexels_api_key', '')
            if not api_key:
                return jsonify({'ok': False, 'error': 'Pexels API key not configured. Add it in Settings.'}), 400
            url = f'https://api.pexels.com/v1/search?query={urllib.parse.quote(query)}&page={page}&per_page=12'
            req = urllib.request.Request(url, headers={'Authorization': api_key})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            for photo in data.get('photos', []):
                results.append({
                    'id': photo['id'], 'url': photo['src']['large'], 'thumb': photo['src']['medium'],
                    'alt': photo.get('alt', ''), 'author': photo['photographer'],
                    'download_url': photo['src']['original'], 'source': 'pexels'
                })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True, 'results': results, 'source': source})


@app.route('/api/stock/download', methods=['POST'])
def stock_download():
    """Download a stock image to the brand's Generated_Images folder."""
    data = request.json
    brand_id = data.get('brand_id')
    image_url = data.get('url')
    filename = data.get('filename', f"stock_{int(datetime.now().timestamp())}.jpg")
    db = get_db()
    brand = db.execute('SELECT folder_path FROM brands WHERE id=?', (brand_id,)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'ok': False, 'error': 'Brand folder not configured'}), 400
    save_dir = os.path.join(brand['folder_path'], '10_Pipeline', 'Generated_Images')
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)
    try:
        req = urllib.request.Request(image_url, headers={'User-Agent': 'DigitalAIzeme/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            with open(save_path, 'wb') as f:
                f.write(resp.read())
        return jsonify({'ok': True, 'path': save_path, 'filename': filename})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/tts/generate', methods=['POST'])
def tts_generate():
    """Generate audio from text using ElevenLabs TTS API."""
    data = request.json
    text = data.get('text', '')[:5000]
    voice_id = data.get('voice_id', '21m00Tcm4TlvDq8ikWAM')  # Default: Rachel
    brand_id = data.get('brand_id')
    content_item_id = data.get('content_item_id')
    db = get_db()
    api_key = get_setting(db, 'elevenlabs_api_key', '')
    if not api_key:
        return jsonify({'ok': False, 'error': 'ElevenLabs API key not configured. Add it in Settings.'}), 400
    if not text:
        return jsonify({'ok': False, 'error': 'Text required'}), 400
    try:
        url = f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}'
        payload = json.dumps({'text': text, 'model_id': 'eleven_monolingual_v1', 'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75}})
        req = urllib.request.Request(url, data=payload.encode(), headers={'xi-api-key': api_key, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            audio_data = resp.read()
        brand = db.execute('SELECT folder_path FROM brands WHERE id=?', (brand_id,)).fetchone()
        filename = f"tts_{content_item_id or 'audio'}_{int(datetime.now().timestamp())}.mp3"
        if brand and brand['folder_path']:
            save_dir = os.path.join(brand['folder_path'], '10_Pipeline')
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, filename)
        else:
            save_path = os.path.join(os.path.dirname(__file__), filename)
        with open(save_path, 'wb') as f:
            f.write(audio_data)
        return jsonify({'ok': True, 'path': save_path, 'filename': filename, 'size': len(audio_data)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# --- Phase 3: Simple APIs (Translate, Remove BG, QR, URL Shortener) ---

@app.route('/api/translate', methods=['POST'])
def translate_content():
    """Translate content using Google Cloud Translation API."""
    data = request.json
    text = data.get('text', '')[:5000]
    target_lang = data.get('target', 'ja')
    content_item_id = data.get('content_item_id')
    brand_id = data.get('brand_id')
    db = get_db()
    api_key = get_setting(db, 'google_api_key', '')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Google API key not configured'}), 400
    try:
        url = f'https://translation.googleapis.com/language/translate/v2?key={api_key}'
        payload = json.dumps({'q': text, 'target': target_lang, 'format': 'text'})
        req = urllib.request.Request(url, data=payload.encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        translated = result['data']['translations'][0]['translatedText']
        if content_item_id and brand_id:
            lang_names = {'ja': 'Japan', 'vi': 'Vietnam', 'ms': 'Singapore', 'th': 'Thailand', 'id': 'Indonesia', 'zh': 'China', 'ko': 'Korea'}
            market = lang_names.get(target_lang, target_lang)
            db.execute("""INSERT INTO market_adaptations (brand_id, content_item_id, target_market, adapted_title, adapted_body, language_code, status)
                VALUES (?, ?, ?, ?, ?, ?, 'draft')""",
                (brand_id, content_item_id, market, '', translated, target_lang))
            db.commit()
        return jsonify({'ok': True, 'translated': translated, 'target': target_lang})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/remove-bg', methods=['POST'])
def remove_background():
    """Remove background from an image using Remove.bg API."""
    data = request.json
    image_path = data.get('image_path', '')
    brand_id = data.get('brand_id')
    db = get_db()
    api_key = get_setting(db, 'removebg_api_key', '')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Remove.bg API key not configured. Add it in Settings.'}), 400
    brand = db.execute('SELECT folder_path FROM brands WHERE id=?', (brand_id,)).fetchone()
    if not brand or not brand['folder_path']:
        return jsonify({'ok': False, 'error': 'Brand folder not configured'}), 400
    full_path = os.path.join(brand['folder_path'], image_path) if not os.path.isabs(image_path) else image_path
    if not os.path.exists(full_path):
        return jsonify({'ok': False, 'error': 'Image file not found'}), 404
    try:
        with open(full_path, 'rb') as img_file:
            img_data = img_file.read()
        boundary = '----WebKitFormBoundary' + str(int(datetime.now().timestamp()))
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image_file"; filename="image.png"\r\nContent-Type: image/png\r\n\r\n').encode() + img_data + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="size"\r\n\r\nauto\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request('https://api.remove.bg/v1.0/removebg', data=body, headers={'X-Api-Key': api_key, 'Content-Type': f'multipart/form-data; boundary={boundary}'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            result_data = resp.read()
        out_name = os.path.splitext(os.path.basename(full_path))[0] + '_nobg.png'
        out_path = os.path.join(os.path.dirname(full_path), out_name)
        with open(out_path, 'wb') as f:
            f.write(result_data)
        return jsonify({'ok': True, 'path': out_path, 'filename': out_name})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/qr')
def generate_qr():
    """Generate a QR code image for a URL."""
    url = request.args.get('url', '')
    size = request.args.get('size', '200')
    if not url:
        return jsonify({'ok': False, 'error': 'URL required'}), 400
    qr_url = f'https://api.qrserver.com/v1/create-qr-code/?size={size}x{size}&data={urllib.parse.quote(url)}'
    return jsonify({'ok': True, 'qr_url': qr_url, 'original_url': url})


@app.route('/api/shorten-url', methods=['POST'])
def shorten_url():
    """Shorten a URL using TinyURL (free, no API key needed)."""
    data = request.json
    long_url = data.get('url', '')
    content_item_id = data.get('content_item_id')
    if not long_url:
        return jsonify({'ok': False, 'error': 'URL required'}), 400
    try:
        api_url = f'https://tinyurl.com/api-create.php?url={urllib.parse.quote(long_url)}'
        req = urllib.request.Request(api_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            short_url = resp.read().decode().strip()
        if content_item_id:
            db = get_db()
            db.execute('UPDATE content_items SET notes = COALESCE(notes, "") || ? WHERE id = ?',
                       (f'\nShort URL: {short_url}', content_item_id))
            db.commit()
        return jsonify({'ok': True, 'short_url': short_url, 'original_url': long_url})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Feedback Forms ───────────────────────────────────────────────

# Rate limiter for public form submissions
_form_submit_cooldowns = {}

@app.route('/brands/<int:brand_id>/feedback-forms')
def feedback_forms_list(brand_id):
    db = get_db()
    brand = db.execute('SELECT * FROM brands WHERE id = ?', (brand_id,)).fetchone()
    if not brand:
        return 'Brand not found', 404
    forms = [dict(r) for r in db.execute(
        'SELECT * FROM feedback_forms WHERE brand_id = ? ORDER BY created_at DESC', (brand_id,)
    ).fetchall()]
    return render_template('feedback_forms/list.html', brand=brand, forms=forms,
                           unread_notifications=db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0])


@app.route('/brands/<int:brand_id>/feedback-forms/new')
@app.route('/brands/<int:brand_id>/feedback-forms/<int:form_id>/edit')
def feedback_form_builder(brand_id, form_id=None):
    db = get_db()
    brand = db.execute('SELECT * FROM brands WHERE id = ?', (brand_id,)).fetchone()
    if not brand:
        return 'Brand not found', 404
    form = None
    if form_id:
        form = dict(db.execute('SELECT * FROM feedback_forms WHERE id = ? AND brand_id = ?',
                               (form_id, brand_id)).fetchone() or {})
    return render_template('feedback_forms/builder.html', brand=brand, form=form,
                           unread_notifications=db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0])


@app.route('/brands/<int:brand_id>/feedback-forms/<int:form_id>')
def feedback_form_responses(brand_id, form_id):
    db = get_db()
    brand = db.execute('SELECT * FROM brands WHERE id = ?', (brand_id,)).fetchone()
    if not brand:
        return 'Brand not found', 404
    form = db.execute('SELECT * FROM feedback_forms WHERE id = ? AND brand_id = ?',
                      (form_id, brand_id)).fetchone()
    if not form:
        return 'Form not found', 404
    form = dict(form)
    responses = [dict(r) for r in db.execute(
        'SELECT * FROM feedback_responses WHERE form_id = ? ORDER BY created_at DESC', (form_id,)
    ).fetchall()]
    insights = [dict(r) for r in db.execute(
        'SELECT * FROM feedback_insights WHERE form_id = ? ORDER BY created_at DESC', (form_id,)
    ).fetchall()]
    return render_template('feedback_forms/responses.html', brand=brand, form=form,
                           responses=responses, insights=insights,
                           unread_notifications=db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0])


@app.route('/api/feedback-forms', methods=['POST'])
def create_feedback_form():
    data = request.json
    brand_id = data.get('brand_id')
    title = data.get('title', '').strip()
    if not brand_id or not title:
        return jsonify({'ok': False, 'error': 'brand_id and title required'}), 400
    fields = json.dumps(data.get('fields', []))
    settings = json.dumps(data.get('settings', {}))
    share_token = secrets.token_hex(6)
    db = get_db()
    cur = db.execute('''INSERT INTO feedback_forms (brand_id, title, description, form_type, fields, settings, share_token)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''',
                     (brand_id, title, data.get('description', ''), data.get('form_type', 'general'),
                      fields, settings, share_token))
    db.commit()
    return jsonify({'ok': True, 'id': cur.lastrowid, 'share_token': share_token})


@app.route('/api/feedback-forms/<int:form_id>', methods=['PUT'])
def update_feedback_form(form_id):
    data = request.json
    db = get_db()
    form = db.execute('SELECT * FROM feedback_forms WHERE id = ?', (form_id,)).fetchone()
    if not form:
        return jsonify({'ok': False, 'error': 'Form not found'}), 404
    db.execute('''UPDATE feedback_forms SET title=?, description=?, form_type=?, fields=?, settings=?, is_active=?,
                  updated_at=CURRENT_TIMESTAMP WHERE id=?''',
               (data.get('title', form['title']), data.get('description', form['description']),
                data.get('form_type', form['form_type']), json.dumps(data.get('fields', json.loads(form['fields']))),
                json.dumps(data.get('settings', json.loads(form['settings']))),
                data.get('is_active', form['is_active']), form_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/feedback-forms/<int:form_id>', methods=['DELETE'])
def delete_feedback_form(form_id):
    db = get_db()
    db.execute('DELETE FROM feedback_forms WHERE id = ?', (form_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/f/<share_token>')
def public_feedback_form(share_token):
    """Public feedback form — no auth required."""
    db = get_db()
    form = db.execute('SELECT f.*, b.name as brand_name, b.primary_color, b.accent_color FROM feedback_forms f JOIN brands b ON f.brand_id = b.id WHERE f.share_token = ?',
                      (share_token,)).fetchone()
    if not form:
        return 'Form not found', 404
    form = dict(form)
    if not form['is_active']:
        return render_template('feedback_forms/public.html', form=form, closed=True)
    # Check expiry
    settings = json.loads(form.get('settings') or '{}')
    if settings.get('expires_at'):
        from datetime import datetime
        try:
            if datetime.now().isoformat() > settings['expires_at']:
                return render_template('feedback_forms/public.html', form=form, closed=True)
        except Exception:
            pass
    source = 'qr' if request.args.get('qr') else 'link'
    return render_template('feedback_forms/public.html', form=form, closed=False, source=source)


@app.route('/f/<share_token>/submit', methods=['POST'])
def submit_feedback_response(share_token):
    """Accept public form submission — no auth required."""
    db = get_db()
    form = db.execute('SELECT * FROM feedback_forms WHERE share_token = ? AND is_active = 1',
                      (share_token,)).fetchone()
    if not form:
        return jsonify({'ok': False, 'error': 'Form not found or closed'}), 404
    # Rate limiting
    ip_raw = request.remote_addr or 'unknown'
    ip_hash = hashlib.sha256(ip_raw.encode()).hexdigest()[:16]
    import time
    now = time.time()
    cooldown_key = f"{share_token}:{ip_hash}"
    if cooldown_key in _form_submit_cooldowns:
        if now - _form_submit_cooldowns[cooldown_key] < 30:
            return jsonify({'ok': False, 'error': 'Please wait before submitting again'}), 429
    # Check allow_multiple
    settings = json.loads(form['settings'] or '{}')
    if not settings.get('allow_multiple', True):
        existing = db.execute('SELECT id FROM feedback_responses WHERE form_id = ? AND ip_hash = ?',
                              (form['id'], ip_hash)).fetchone()
        if existing:
            return jsonify({'ok': False, 'error': 'You have already submitted a response'}), 409
    data = request.json
    db.execute('''INSERT INTO feedback_responses (form_id, response_data, respondent_name, respondent_email, source, ip_hash)
                  VALUES (?, ?, ?, ?, ?, ?)''',
               (form['id'], json.dumps(data.get('responses', {})),
                data.get('name', ''), data.get('email', ''),
                data.get('source', 'web'), ip_hash))
    db.execute('UPDATE feedback_forms SET response_count = response_count + 1 WHERE id = ?', (form['id'],))
    db.commit()
    _form_submit_cooldowns[cooldown_key] = now
    return jsonify({'ok': True, 'message': settings.get('thank_you_message', 'Thank you for your feedback!')})


@app.route('/api/feedback-forms/<int:form_id>/analyze', methods=['POST'])
def analyze_feedback_form(form_id):
    """Trigger Claude AI analysis of form responses."""
    db = get_db()
    form = db.execute('SELECT f.*, b.name as brand_name FROM feedback_forms f JOIN brands b ON f.brand_id = b.id WHERE f.id = ?',
                      (form_id,)).fetchone()
    if not form:
        return jsonify({'ok': False, 'error': 'Form not found'}), 404
    responses = [dict(r) for r in db.execute(
        'SELECT * FROM feedback_responses WHERE form_id = ? ORDER BY created_at DESC', (form_id,)
    ).fetchall()]
    if not responses:
        return jsonify({'ok': False, 'error': 'No responses to analyze'}), 400
    fields = json.loads(form['fields'])
    # Aggregate data
    aggregated = {}
    for field in fields:
        fid = field['id']
        ftype = field['type']
        values = []
        for resp in responses:
            rd = json.loads(resp['response_data'])
            if fid in rd and rd[fid] is not None:
                values.append(rd[fid])
        if ftype in ('rating', 'nps') and values:
            nums = [v for v in values if isinstance(v, (int, float))]
            aggregated[field['label']] = {
                'type': ftype, 'count': len(nums),
                'average': round(sum(nums) / len(nums), 1) if nums else 0,
                'distribution': {str(i): nums.count(i) for i in range(0, 11 if ftype == 'nps' else 6) if nums.count(i) > 0}
            }
        elif ftype in ('select', 'multi_select') and values:
            counts = {}
            for v in values:
                items = v if isinstance(v, list) else [v]
                for item in items:
                    counts[item] = counts.get(item, 0) + 1
            aggregated[field['label']] = {'type': ftype, 'counts': counts}
        elif values:
            aggregated[field['label']] = {'type': ftype, 'sample_responses': values[:20]}
    # Build prompt
    prompt = f"""Analyze {len(responses)} feedback responses for "{form['title']}" ({form['form_type']} form) for the brand {form['brand_name']}.

AGGREGATED DATA:
{json.dumps(aggregated, indent=2)}

Please provide:
1. **Executive Summary** (2-3 sentences)
2. **Key Themes & Patterns** (bulleted list)
3. **Sentiment Analysis** (positive/neutral/negative breakdown with percentages)
4. **Actionable Recommendations** (3-5 specific actions for improving content or service)
5. **Content/Service Alignment Score** (1-10) with explanation"""
    # Call Claude
    settings_row = db.execute("SELECT value FROM app_settings WHERE key = 'anthropic_api_key'").fetchone()
    if not settings_row or not settings_row['value']:
        return jsonify({'ok': False, 'error': 'Claude API key not configured'}), 400
    api_key = settings_row['value']
    if api_key.startswith('enc:'):
        from cryptography.fernet import Fernet
        enc_key = db.execute("SELECT value FROM app_settings WHERE key = 'encryption_key'").fetchone()
        if enc_key:
            api_key = Fernet(enc_key['value'].encode()).decrypt(api_key[4:].encode()).decode()
    try:
        body = json.dumps({'model': 'claude-sonnet-4-20250514', 'max_tokens': 2000,
                           'messages': [{'role': 'user', 'content': prompt}]}).encode()
        req = urllib.request.Request('https://api.anthropic.com/v1/messages',
                                    data=body, headers={'Content-Type': 'application/json',
                                                        'x-api-key': api_key, 'anthropic-version': '2023-06-01'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
        insight_text = result['content'][0]['text']
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Claude API error: {str(e)}'}), 500
    db.execute('INSERT INTO feedback_insights (form_id, insight_text, response_count_at_analysis) VALUES (?, ?, ?)',
               (form_id, insight_text, len(responses)))
    db.commit()
    return jsonify({'ok': True, 'insight': insight_text})


# ─── Initialize & Run ──────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    start_file_watcher()
    port = int(os.environ.get('PORT', 5001))
    host = os.environ.get('HOST', '0.0.0.0')
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"\n  ╔══════════════════════════════════════════╗")
    print(f"  ║     DIGITALAIZEME — Content Platform     ║")
    print(f"  ║     http://localhost:{port}                 ║")
    print(f"  ║     http://{local_ip}:{port}            ║")
    print(f"  ╚══════════════════════════════════════════╝\n")
    app.run(debug=True, host=host, port=port)
