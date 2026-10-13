"""Optional local status references; never used as ownership or acceptance evidence."""
from pathlib import Path
import re

from studio_trackers import issue_prefix


def read_references(fleet, config, refs):
    for agent_id, filename in config.get('legacyReferences', {}).items():
        try:
            path = Path(filename)
            body = path.read_text(encoding='utf-8-sig')[:16000]
            match = re.search(re.escape(issue_prefix(config)) + r'(\d+)', body)
            if match:
                refs.setdefault(int(match[1]), []).append({'agentId': agent_id, 'basis': 'Local status reference',
                                                          'observedAt': path.stat().st_mtime})
        except (OSError, UnicodeError):
            continue
    return refs
