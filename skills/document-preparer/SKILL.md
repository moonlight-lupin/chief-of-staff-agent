---
name: document-preparer
description: Generate DOCX documents from tokenized templates and reverse-engineer reusable templates for the Chief of Staff document workflow.
version: 0.1.0
author: Phronesis Applied
license: MIT
metadata:
  hermes:
    tags: [documents, docx, templates, contracts, chief-of-staff]
    related_skills: [self-sign, drive-filer, pipeline-manager, bookkeeper]
---

# Document Preparer

## Overview

Document Preparer creates business documents from `.docx` templates and turns existing `.docx` files into reusable templates. Phase 1 deliberately supports `.docx` only. Google Docs support is deferred; when a Google Doc is involved, export it to DOCX through `google-workspace`, process locally, then optionally upload/file the result through Drive Filer.

The bundled `doc_utils.py` script provides token extraction, template filling, template creation, and registry updates.

## When to Use

Use this skill when the user asks to:

- Generate an NDA, SOW, invoice, proposal, T&Cs, letter, or contract from a template.
- Fill placeholders such as `{{client_name}}`, `{{date}}`, or `{{amount}}` in a `.docx`.
- Convert an existing document into a reusable template.
- Register or inspect available document templates.
- Generate a document as part of Pipeline Manager or Bookkeeper workflows.

Do not use this skill for PDF editing; use source DOCX where possible, or another document-processing skill if the only source is a PDF.

## Template Registry

The registry lives at:

```text
/root/.hermes/plugins/chief-of-staff/shared/templates/index.yaml
```

Use the example seed at `shared/config/template-index.yaml.example` when bootstrapping. Registry shape:

```yaml
templates:
  - name: "NDA Mutual"
    file: "shared/templates/NDA_mutual.docx"
    tokens: ["client_name", "client_address", "date", "jurisdiction"]
    category: legal
    description: "Mutual non-disclosure agreement"
    last_used: null
```

Registry file paths are relative to the plugin root unless absolute.

## Token Sources

Populate tokens from three layers, in order. Later layers override earlier values when explicitly provided by the user.

1. `company.yaml` — sender/company values:
   - `company.name`, `company.jurisdiction`, `company.currency`, registration numbers, addresses, signatory names.
2. `pipeline.yaml` — deal/client values:
   - `client_name`, `contact_name`, `contact_email`, `scope`, `amount`, `currency`, `start_date`, `duration`, `deal_id`.
3. User input — missing or one-off values:
   - custom clauses, invoice due date, payment terms, special instructions.

Never invent legal or commercial terms. If a required token cannot be resolved from config or pipeline data, ask the user.

## Mode A — Fill Template

1. Select a template from `shared/templates/`, Drive `05_Templates/`, or a user-provided path.
2. Extract tokens:

```bash
python /root/.hermes/plugins/chief-of-staff/skills/document-preparer/scripts/doc_utils.py \
  extract --template /path/to/template.docx
```

3. Build a token dictionary from company config, pipeline data, and user-provided values.
4. Show missing tokens and ask the user for values.
5. Fill the template:

```bash
python /root/.hermes/plugins/chief-of-staff/skills/document-preparer/scripts/doc_utils.py \
  fill --template /path/to/template.docx \
  --output /path/to/output.docx \
  --tokens '{"client_name":"Acme Corp","date":"2026-07-09"}'
```

6. Open or inspect the resulting file if practical.
7. Offer next actions:
   - Self-Sign if the user's company is a signatory.
   - Drive Filer to file under the client/project folder.
   - Bookkeeper record creation if the document is an invoice.

Completion criterion: generated `.docx` exists and all placeholders were either filled or intentionally left unresolved and reported.

## Mode B — Reverse-Engineer a Document Into a Template

1. Confirm the source file is `.docx`.
2. Identify variable parts with the user: names, dates, amounts, addresses, scope descriptions, payment terms, jurisdiction, invoice numbers.
3. Build a mapping from literal text to tokens, for example:

```json
{
  "Acme Corp": "client_name",
  "SGD 4,500": "amount",
  "15 July 2026": "due_date"
}
```

4. Create the template:

```bash
python /root/.hermes/plugins/chief-of-staff/skills/document-preparer/scripts/doc_utils.py \
  template --doc /path/to/source.docx \
  --output /root/.hermes/plugins/chief-of-staff/shared/templates/SOW_standard.docx \
  --mappings '{"Acme Corp":"client_name","SGD 4,500":"amount"}'
```

5. Extract tokens from the resulting template as a validation step.
6. Register the template in `shared/templates/index.yaml`.
7. Offer to upload it to Drive `05_Templates/` through Drive Filer.

Completion criterion: new template file exists, token extraction returns expected tokens, and registry entry is updated.

## `doc_utils.py` Functions

The script exposes:

- `fill_template(template_path, tokens_dict, output_path)` — replace `{{tokens}}` in `.docx` and save.
- `extract_tokens(template_path)` — find all placeholders in `.docx` body, tables, headers, and footers.
- `create_template_from_doc(doc_path, token_mappings, output_path)` — replace identified text with `{{tokens}}`.
- `register_template(name, file, tokens, category, index_path)` — add/update a template entry in `index.yaml`.

CLI examples:

```bash
python doc_utils.py fill --template X.docx --output Y.docx --tokens '{"client_name":"Acme"}'
python doc_utils.py extract --template X.docx
python doc_utils.py register --name "NDA Mutual" --file shared/templates/NDA_mutual.docx --tokens client_name date jurisdiction --category legal
```

## Integrations

| Skill | Integration |
|---|---|
| Pipeline Manager | Chooses templates based on deal stage and supplies client/deal tokens. |
| Self-Sign | Signs generated documents when the user's company is a party. |
| Drive Filer | Files generated documents and templates into Drive. |
| Bookkeeper | Invoice generation creates both a document and an invoice record. |

## Common Pitfalls

1. **Leaving tokens unresolved silently.** Always extract tokens after fill and report any remaining placeholders.
2. **Breaking Word formatting.** Token replacement may merge runs when placeholders span runs; verify output before sending externally.
3. **Inventing terms.** Ask for missing commercial/legal terms; do not guess.
4. **Wrong registry path.** Runtime registry is `shared/templates/index.yaml`; the `.example` file is only a seed.
5. **Treating Google Docs as Phase 1 support.** Export to DOCX first; native Google Docs support is deferred.

## Verification Checklist

- [ ] Source template is `.docx`.
- [ ] Tokens were extracted before filling.
- [ ] Token values came from company config, pipeline data, or explicit user input.
- [ ] Output `.docx` exists.
- [ ] Remaining placeholders were checked and reported.
- [ ] Template registry was updated when creating a reusable template.
- [ ] Next actions offered: Self-Sign, Drive Filer, Bookkeeper where relevant.
