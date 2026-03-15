"""
DigitalAIzeme — Theme Configuration
Centralized color and typography definitions.
Edit this file to change the app's visual identity without touching templates.

Supports DARK and LIGHT modes. The active theme is selected at runtime
via the `theme_mode` app setting (stored in SQLite).
"""

# ─── Brand Palette (shared between modes) ────────────────────────────
BRAND = {
    'gold':       '#D99549',   # Warm Gold — primary accent
    'gold-hover': '#C4813A',   # Darker gold — hover
    'aqua':       '#91F9F0',   # Aqua Mint — secondary accent
    'beige':      '#E7C3A7',   # Soft Beige — tertiary accent
    'success':    '#10B981',
    'warning':    '#F59E0B',
    'error':      '#EF4444',
}

# ─── Dark Theme ──────────────────────────────────────────────────────
COLORS_DARK = {
    'bg-primary':       '#120000',
    'bg-secondary':     '#1A0A0A',
    'bg-tertiary':      '#241010',
    'accent-primary':   BRAND['gold'],
    'accent-hover':     BRAND['gold-hover'],
    'accent-secondary': BRAND['aqua'],
    'accent-tertiary':  BRAND['beige'],
    'text-primary':     '#FFFFFF',
    'text-secondary':   '#B0A090',
    'text-muted':       '#6B5B50',
    'text-disabled':    '#4A3A30',
    'border-primary':   '#2A1A1A',
    'border-hover':     '#3A2A2A',
    'border-accent':    BRAND['gold'],
    'status-success':   BRAND['success'],
    'status-warning':   BRAND['warning'],
    'status-error':     BRAND['error'],
    'status-info':      BRAND['aqua'],
    'sidebar-bg':       '#0D0000',
    'sidebar-hover':    '#1A0A0A',
    'sidebar-active':   '#D9954920',
    # Utility tokens for sub-templates
    'card-bg':          '#111111',
    'input-bg':         '#1A0A0A',
    'input-border':     '#2A1A1A',
    'modal-overlay':    'rgba(0,0,0,0.6)',
    'hover-overlay':    'rgba(255,255,255,0.04)',
}

# ─── Light Theme ─────────────────────────────────────────────────────
COLORS_LIGHT = {
    'bg-primary':       '#F8F6F3',
    'bg-secondary':     '#FFFFFF',
    'bg-tertiary':      '#F0ECE6',
    'accent-primary':   '#B37A30',     # Slightly deeper gold for contrast on white
    'accent-hover':     '#9A6828',
    'accent-secondary': '#0D9488',     # Teal (aqua darkened for readability)
    'accent-tertiary':  '#C9A07A',
    'text-primary':     '#1A1008',
    'text-secondary':   '#5C5040',
    'text-muted':       '#9A9080',
    'text-disabled':    '#C4BAB0',
    'border-primary':   '#E0D8CE',
    'border-hover':     '#C8BEB0',
    'border-accent':    '#B37A30',
    'status-success':   BRAND['success'],
    'status-warning':   BRAND['warning'],
    'status-error':     BRAND['error'],
    'status-info':      '#0D9488',
    'sidebar-bg':       '#EFEBE4',
    'sidebar-hover':    '#E4DED6',
    'sidebar-active':   '#B37A3018',
    'card-bg':          '#FFFFFF',
    'input-bg':         '#F8F6F3',
    'input-border':     '#E0D8CE',
    'modal-overlay':    'rgba(0,0,0,0.35)',
    'hover-overlay':    'rgba(0,0,0,0.04)',
}

# ─── Resolved colors (set by get_theme_colors) ──────────────────────
COLORS = COLORS_DARK  # default; overridden per-request in inject_globals

# ─── Typography ──────────────────────────────────────────────────────
FONTS = {
    'heading':    'Poppins',
    'body':       'Open Sans',
    'mono':       'Roboto Mono',
    'additional': 'Roboto',
}


def get_theme_colors(mode='dark'):
    """Return the color dict for the given mode."""
    return COLORS_LIGHT if mode == 'light' else COLORS_DARK


def tailwind_theme_config(mode='dark'):
    """Generate Tailwind CSS theme extension as a JavaScript object string."""
    c = get_theme_colors(mode)
    return f"""{{
        colors: {{
            'dm-bg':        '{c["bg-primary"]}',
            'dm-bg2':       '{c["bg-secondary"]}',
            'dm-bg3':       '{c["bg-tertiary"]}',
            'dm-gold':      '{c["accent-primary"]}',
            'dm-gold-hover':'{c["accent-hover"]}',
            'dm-aqua':      '{c["accent-secondary"]}',
            'dm-beige':     '{c["accent-tertiary"]}',
            'dm-border':    '{c["border-primary"]}',
            'dm-border2':   '{c["border-hover"]}',
            'dm-sidebar':   '{c["sidebar-bg"]}',
            'dm-card':      '{c["card-bg"]}',
            'dm-input':     '{c["input-bg"]}',
            // Backward-compatible aliases
            'dm-primary':   '{c["accent-primary"]}',
            'dm-secondary': '{c["accent-hover"]}',
            'dm-accent':    '{c["accent-primary"]}',
        }},
        fontFamily: {{
            'heading': ['{FONTS["heading"]}', 'sans-serif'],
            'body':    ['{FONTS["body"]}', 'sans-serif'],
            'mono':    ['{FONTS["mono"]}', 'monospace'],
        }}
    }}"""


def css_variables(mode='dark'):
    """Generate CSS custom properties for use in templates."""
    c = get_theme_colors(mode)
    lines = [':root {']
    for key, value in c.items():
        lines.append(f'  --dm-{key}: {value};')
    lines.append('}')
    return '\n'.join(lines)
