# Chief of Staff Plugin — Test Suite

Tests for the chief-of-staff Hermes plugin. Run with pytest.

## Running

```bash
cd ~/.hermes/plugins/chief-of-staff
python3 -m pytest tests/ -v
```

## What's Tested

| File | Tests |
|---|---|
| `test_config_loader.py` | Config loading, validation, missing file handling, defaults |
| `test_date_utils.py` | Deadline categorization, business-day checks, statutory deadline computation |
| `test_jurisdictions.py` | All 4 jurisdiction packs parse correctly, required fields present |
| `test_pipeline.py` | Pipeline YAML schema, add/move/list operations, stale detection |
| `test_bookkeeper.py` | Invoice/expense YAML schema, P&L report computation, overdue detection |
| `test_todo.py` | To-do YAML schema, add/list/complete operations |
| `test_sign_detector.py` | Signature detection in PDF + DOCX, party identification |
| `test_doc_utils.py` | Template token extraction, fill, template creation |
| `test_backup.py` | Backup file selection, exclusion rules, tar.gz creation |
| `test_onboard.py` | Onboarding wizard non-interactive mode, config generation |
| `test_plugin_structure.py` | All 16 skills present, frontmatter valid, plugin.yaml valid |