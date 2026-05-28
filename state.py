"""Tiny persisted settings store.

Currently just remembers the operator's choice of output display so that
the next launch lands on the same monitor without anyone having to edit
`Start VJ.sh`.
"""
import json
from pathlib import Path

STATE_PATH = Path(__file__).parent / "vj_state.json"


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except OSError:
        # Best-effort — losing state is not worth crashing the show over.
        pass


def update_state(**kwargs) -> dict:
    state = load_state()
    state.update(kwargs)
    save_state(state)
    return state
