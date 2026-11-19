"""Allow-listed tools available to chat providers."""
from typing import Any

from . import notes_tools
from .literature import tools as literature_tools

# Put workspace-note actions first, with simple tag-specific tools ahead of the
# generic editor, so small local models do not confuse note and literature tags.
_NOTE_PRIORITY = {
    "list_notes": 0, "get_note": 1, "add_note_tag": 2, "remove_note_tag": 3,
    "create_notes": 4, "create_note": 5, "edit_note": 6, "delete_note": 7, "delete_all_notes": 8,
}
_note_tools = sorted(notes_tools.TOOLS, key=lambda tool: _NOTE_PRIORITY[tool["function"]["name"]])
TOOLS = [*_note_tools, *literature_tools.TOOLS]


def execute(name: str, arguments: str | dict | None) -> dict[str, Any]:
    if name in notes_tools.NAMES:
        return notes_tools.execute(name, arguments)
    return literature_tools.execute(name, arguments)
