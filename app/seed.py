"""One-off task: write a few notes and exit.  Run with: python -m app.seed"""

import sys

from app.main import log, new_note, store

SEED_NOTES = [
    "seeded by a one-off task",
    "the task role wrote this, not the execution role",
    "if you can read this from GET /notes, S3 works",
]


def main():
    if store.backend == "memory":
        log(event="seed_warning", msg="NOTES_BUCKET not set; notes vanish when this exits")
    try:
        for text in SEED_NOTES:
            note = new_note(text, source="seed")
            store.add(note)
            log(event="seed_note", id=note["id"], backend=store.backend)
    except Exception as exc:
        log(event="seed_failed", error=str(exc))
        return 1
    log(event="seed_done", count=len(SEED_NOTES), backend=store.backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
