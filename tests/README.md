# Tests

- `tests/fixtures/`: small, made-up or openly licensed (CC BY) test manuscripts. Safe to commit.
- `tests/private/`: real manuscripts used for local testing only. Git-ignored and never committed.

The text lock must catch 100% of planted edits before any release.

## Running the tests

```
pip install -r requirements.txt pytest
python -m pytest -q tests/              # synthetic manuscript, safe for CI
python tests/stress_lock_private.py 50  # your own manuscripts in tests/private/
```
