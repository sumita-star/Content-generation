"""
Bi-directional prompt git sync system.

Manages prompt files in prompts/ directory (Markdown with YAML frontmatter).
In-memory cache with mtime tracking for change detection.
- When file changes on disk -> auto-reload into cache
- When prompt saved via UI -> write back to file
"""

import os
import re
import threading
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def _parse_frontmatter_simple(text):
    """Parse YAML frontmatter between --- markers without PyYAML."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if not match:
        return {}, text
    raw = match.group(1)
    metadata = {}
    for line in raw.strip().splitlines():
        line = line.strip()
        if ':' in line:
            key, val = line.split(':', 1)
            key = key.strip()
            val = val.strip()
            # Try to cast numbers
            if val.isdigit():
                val = int(val)
            elif val.lower() in ('true', 'false'):
                val = val.lower() == 'true'
            metadata[key] = val
    body = text[match.end():]
    return metadata, body


def _parse_frontmatter(text):
    """Parse YAML frontmatter. Uses PyYAML if available, otherwise simple parser."""
    if yaml is not None:
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
        if not match:
            return {}, text
        metadata = yaml.safe_load(match.group(1)) or {}
        body = text[match.end():]
        return metadata, body
    return _parse_frontmatter_simple(text)


def _render_frontmatter(metadata):
    """Render metadata dict as YAML frontmatter block."""
    if yaml is not None:
        dumped = yaml.dump(metadata, default_flow_style=False, sort_keys=False).strip()
    else:
        lines = []
        for k, v in metadata.items():
            lines.append(f"{k}: {v}")
        dumped = "\n".join(lines)
    return f"---\n{dumped}\n---\n"


def _split_sections(body):
    """Split body text on ## Instructions and ## Brief Template headers."""
    instructions = ""
    brief_template = None

    # Find ## Instructions section
    instr_match = re.search(r'^## Instructions\s*\n', body, re.MULTILINE)
    brief_match = re.search(r'^## Brief Template\s*\n', body, re.MULTILINE)

    if instr_match:
        start = instr_match.end()
        if brief_match and brief_match.start() > instr_match.start():
            instructions = body[start:brief_match.start()].strip()
        else:
            instructions = body[start:].strip()

    if brief_match:
        brief_template = body[brief_match.end():].strip()

    return instructions, brief_template


class PromptRegistry:
    """Bi-directional prompt registry with file-backed storage and mtime tracking."""

    def __init__(self, prompts_dir):
        self.prompts_dir = Path(prompts_dir)
        self._cache = {}      # id -> {instructions, brief_template, metadata, file_path}
        self._mtimes = {}     # file_path -> mtime
        self._lock = threading.Lock()
        # Create directory structure if needed
        (self.prompts_dir / "workflow").mkdir(parents=True, exist_ok=True)
        (self.prompts_dir / "system").mkdir(parents=True, exist_ok=True)

    def load_all(self):
        """Scan prompts/ dir, parse all .md files, populate cache."""
        with self._lock:
            for md_file in self.prompts_dir.rglob("*.md"):
                self._load_file(md_file)

    def _load_file(self, file_path):
        """Parse and cache a single prompt file. Must be called under lock."""
        file_path = Path(file_path)
        try:
            mtime = file_path.stat().st_mtime
            text = file_path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return

        metadata, body = _parse_frontmatter(text)
        instructions, brief_template = _split_sections(body)

        prompt_id = metadata.get("id")
        if not prompt_id:
            # Derive id from filename
            prompt_id = file_path.stem
            metadata["id"] = prompt_id

        self._cache[prompt_id] = {
            "instructions": instructions,
            "brief_template": brief_template,
            "metadata": metadata,
            "file_path": str(file_path),
        }
        self._mtimes[str(file_path)] = mtime

    def get(self, prompt_id):
        """Return prompt dict or None. Auto-checks mtime for staleness."""
        with self._lock:
            entry = self._cache.get(prompt_id)
            if entry is None:
                return None
            fp = entry["file_path"]
            try:
                current_mtime = os.path.getmtime(fp)
            except OSError:
                return entry
            if self._mtimes.get(fp) != current_mtime:
                self._load_file(fp)
                entry = self._cache.get(prompt_id)
            return entry

    def save(self, prompt_id, instructions, brief_template=None):
        """Write to file AND update cache."""
        with self._lock:
            existing = self._cache.get(prompt_id)
            if existing:
                metadata = dict(existing["metadata"])
                file_path = existing["file_path"]
                # Bump version
                metadata["version"] = metadata.get("version", 1) + 1
            else:
                metadata = {
                    "id": prompt_id,
                    "type": "custom",
                    "name": prompt_id.replace("-", " ").replace("_", " ").title(),
                    "model": "claude",
                    "version": 1,
                }
                file_path = str(self.prompts_dir / f"{prompt_id}.md")

            self._write_file(prompt_id, instructions, brief_template, metadata, file_path)

    def check_for_changes(self):
        """Compare mtimes, reload changed files. Call this per-request."""
        with self._lock:
            # Check existing tracked files
            for fp, cached_mtime in list(self._mtimes.items()):
                try:
                    current_mtime = os.path.getmtime(fp)
                except OSError:
                    continue
                if current_mtime != cached_mtime:
                    self._load_file(fp)

            # Scan for new files
            for md_file in self.prompts_dir.rglob("*.md"):
                if str(md_file) not in self._mtimes:
                    self._load_file(md_file)

    def list_all(self):
        """Return list of all prompt metadata."""
        self.check_for_changes()
        results = []
        for prompt_id, entry in self._cache.items():
            meta = dict(entry["metadata"])
            meta["has_brief_template"] = entry["brief_template"] is not None
            results.append(meta)
        return results

    def _parse_file(self, file_path):
        """Parse markdown file with YAML frontmatter. Returns (metadata, instructions, brief_template)."""
        text = Path(file_path).read_text(encoding="utf-8")
        metadata, body = _parse_frontmatter(text)
        instructions, brief_template = _split_sections(body)
        return metadata, instructions, brief_template

    def _write_file(self, prompt_id, instructions, brief_template, metadata, file_path=None):
        """Write prompt back to file and update cache. Must be called under lock."""
        if file_path is None:
            existing = self._cache.get(prompt_id)
            if existing:
                file_path = existing["file_path"]
            else:
                file_path = str(self.prompts_dir / f"{prompt_id}.md")

        content = _render_frontmatter(metadata)
        content += "\n## Instructions\n\n"
        content += instructions.strip() + "\n"
        if brief_template:
            content += "\n## Brief Template\n\n"
            content += brief_template.strip() + "\n"

        fp = Path(file_path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

        mtime = fp.stat().st_mtime
        self._cache[prompt_id] = {
            "instructions": instructions.strip(),
            "brief_template": brief_template.strip() if brief_template else None,
            "metadata": metadata,
            "file_path": str(fp),
        }
        self._mtimes[str(fp)] = mtime


def create_sync_route(app, registry):
    """Register /api/prompts/sync and /api/prompts routes on a Flask app."""

    @app.route('/api/prompts/sync', methods=['POST'])
    def sync_prompts():
        registry.load_all()
        return {'ok': True, 'count': len(registry._cache)}

    @app.route('/api/prompts', methods=['GET'])
    def list_prompts():
        return {'ok': True, 'prompts': registry.list_all()}

    @app.route('/api/prompts/<prompt_id>', methods=['GET'])
    def get_prompt(prompt_id):
        entry = registry.get(prompt_id)
        if entry is None:
            return {'ok': False, 'error': 'Not found'}, 404
        return {
            'ok': True,
            'prompt': {
                'id': entry['metadata'].get('id', prompt_id),
                'instructions': entry['instructions'],
                'brief_template': entry['brief_template'],
                'metadata': entry['metadata'],
            }
        }

    @app.route('/api/prompts/<prompt_id>', methods=['PUT'])
    def update_prompt(prompt_id):
        from flask import request
        data = request.get_json(force=True)
        instructions = data.get('instructions', '')
        brief_template = data.get('brief_template')
        registry.save(prompt_id, instructions, brief_template)
        return {'ok': True, 'id': prompt_id}
