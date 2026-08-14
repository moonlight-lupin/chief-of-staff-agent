---
name: esign-connector
description: "Send documents for e-signature via self-hosted DocuSeal. Create templates with signature fields, dispatch to submitters, track status, download signed copies, cancel submissions. Integrates with Document Preparer and Pipeline Manager."
version: 0.2.1
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [esign, signature, docuseal, documents, chief-of-staff]
    related_skills: [document-preparer, drive-filer, daily-briefing, pipeline-manager, weekly-review, self-sign]
---

# eSign Connector (DocuSeal)

Send documents for electronic signature via a self-hosted DocuSeal instance. This handles **third-party signing** — sending documents to clients, partners, or directors for their signature. For self-signing (placing your own signature on a received document without a third-party service), use the self-sign skill.

## When to Use

- "Send NDA to {client} for signing"
- "Send SOW for signature"
- "Check if {client} has signed the contract"
- "Download the signed NDA"
- "Cancel the pending signature request"

Do not use for placing your own signature offline — that is the self-sign skill.

## Prerequisites

1. DocuSeal instance running (self-hosted, Docker)
2. Two credentials in `.env` (see Config below):
   - `DOCUSEAL_MCP_TOKEN` — for template creation and search
   - `DOCUSEAL_API_KEY` — for field placement, submissions, and status
3. `esign` section in `company.yaml`
4. LibreOffice installed (`libreoffice --headless`) for DOCX→PDF conversion
5. PyMuPDF installed (`pip install pymupdf`) for PDF merge and coordinate normalization

## Config (from company.yaml)

```yaml
esign:
  provider: docuseal
  url: "https://sign.yourdomain.com"
  domain: "sign.yourdomain.com"
  provider_email: "you@yourdomain.com"
  # Legacy configs may use admin_email; provider_email is preferred.
  provider_role: "Service Provider"
  client_role: "Client"
  auth_mode: auto        # auto | mcp_and_api | pro_api_only
  file_serving:
    mode: existing        # existing | local_https
    public_base_url: null
    cleanup_after_send: true
  defaults:
    signing_order: random # random = simultaneous | preserved = sequential
    cancel_before_resend: true
  field_detection:
    prefer: auto          # auto | table | underscore
    page_indexing: zero_based
```

### Secrets in .env (never in company.yaml)

```bash
DOCUSEAL_MCP_TOKEN=...   # Settings → MCP Server → mcp_tokens
DOCUSEAL_API_KEY=...     # Settings → API → access_tokens (X-Auth-Token header)
```

## Authentication Model

DocuSeal CE uses **two separate credential systems**. This skill uses both — no browser login required.

| Credential | Header | Used for |
|---|---|---|
| MCP token | `Authorization: Bearer` on `/mcp` | `create_template`, `search_templates`, `load_template`, `search_documents` |
| API key | `X-Auth-Token` on `/api/*` | `PATCH /api/templates/{id}` (fields), `POST /api/submissions` (send), `GET /api/submissions/{id}` (status), `DELETE /api/submissions/{id}` (cancel), `GET /api/submissions/{id}/documents` (download) |

**Auth mode routing:**

- `auto` (default) — uses both MCP and API key. Best for DocuSeal CE free.
- `mcp_and_api` — same as auto, explicit.
- `pro_api_only` — uses API key only for everything, including `POST /api/templates/pdf` with fields in one call. Requires DocuSeal Pro.

### Why two tokens?

`POST /api/templates/pdf` (create template with fields in one REST call) is **Pro-gated on CE free**. The MCP `create_template` tool creates a template without fields, and `PATCH /api/templates/{id}` (adding fields via API key) is **not** Pro-gated. So on CE free:

1. MCP creates the empty template
2. API key patches the fields

On Pro, `POST /api/templates/pdf` handles both in one call — MCP is optional.

## Operations

### 1. Convert to PDF (if DOCX)

```bash
libreoffice --headless --convert-to pdf --outdir /tmp/esign/ "document.docx"
```

Batch all DOCX files in one LibreOffice invocation.

### 2. Merge Documents (if multi-doc submission)

If sending T&Cs + SOW as one signing event, merge the PDFs with PyMuPDF:

```python
import fitz
merged = fitz.open()
for pdf in [tcs_pdf, sow_pdf]:
    merged.insert_pdf(fitz.open(pdf))
merged.save(merged_path)
```

T&Cs pages first, SOW pages last. The signature table is on the SOW's last page.

### 3. Serve PDF via HTTPS (for MCP create_template)

MCP `create_template` requires an HTTPS URL. Options:

- **`file_serving.mode: existing`** (recommended) — use a URL the customer already hosts. Upload the PDF to any reachable HTTPS location.
- **`file_serving.mode: local_https`** — not yet implemented. Planned: start a temporary file server with an HTTPS tunnel, clean up after template creation.

Use unguessable filenames. Set `cleanup_after_send: true`.

### 4. Create Template (MCP)

```python
# Via MCP tool:
create_template(name="NDA - Client Name", url="https://files.yourdomain.com/nda_client.pdf")
# Returns {"id": template_id}
```

### 5. Add Fields (API key — PATCH)

After template creation, GET the template to obtain `attachment_uuid` and the default submitter `uuid`, then generate a second submitter UUID for the client and PATCH fields with coordinates.

**Role names must match `esign.provider_role` and `esign.client_role` from `company.yaml`.** DocuSeal matches submission roles to template submitter names — mismatched names cause submissions to fail or bind the wrong party.

```bash
# Load config values
BASE_URL=$(python3 -c "import yaml; print(yaml.safe_load(open('shared/config/company.yaml'))['esign']['url'])")
PROVIDER_ROLE=$(python3 -c "import yaml; print(yaml.safe_load(open('shared/config/company.yaml'))['esign']['provider_role'])")
CLIENT_ROLE=$(python3 -c "import yaml; print(yaml.safe_load(open('shared/config/company.yaml'))['esign']['client_role'])")

# Step A: GET template to extract attachment_uuid and existing submitter UUID
TEMPLATE_JSON=$(curl -s "${BASE_URL}/api/templates/${TEMPLATE_ID}" \
  -H "X-Auth-Token: ${DOCUSEAL_API_KEY}")

ATTACHMENT_UUID=$(echo "$TEMPLATE_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['schema'][0]['attachment_uuid'])
")

# The default submitter from MCP create_template is 'First Party' — we need two submitters.
# Generate UUIDs for both provider and client submitters.
PROVIDER_UUID=$(python3 -c 'import uuid; print(uuid.uuid4())')
CLIENT_UUID=$(python3 -c 'import uuid; print(uuid.uuid4())')

# Step B: PATCH fields with coordinates — send ALL submitters and ALL fields
# (PATCH is full replacement, NOT append — Rule #6)
curl -s -X PATCH "${BASE_URL}/api/templates/${TEMPLATE_ID}" \
  -H "X-Auth-Token: ${DOCUSEAL_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"NDA - Client Name\",
    \"fields\": [
      {
        \"uuid\": \"$(python3 -c 'import uuid; print(uuid.uuid4())')\",
        \"name\": \"${PROVIDER_ROLE} Signature\",
        \"type\": \"signature\",
        \"required\": true,
        \"submitter_uuid\": \"${PROVIDER_UUID}\",
        \"areas\": [{
          \"x\": 0.089, \"y\": 0.727, \"w\": 0.327, \"h\": 0.038,
          \"page\": 0,
          \"attachment_uuid\": \"${ATTACHMENT_UUID}\"
        }]
      },
      {
        \"uuid\": \"$(python3 -c 'import uuid; print(uuid.uuid4())')\",
        \"name\": \"${CLIENT_ROLE} Signature\",
        \"type\": \"signature\",
        \"required\": true,
        \"submitter_uuid\": \"${CLIENT_UUID}\",
        \"areas\": [{
          \"x\": 0.590, \"y\": 0.727, \"w\": 0.327, \"h\": 0.038,
          \"page\": 0,
          \"attachment_uuid\": \"${ATTACHMENT_UUID}\"
        }]
      }
    ],
    \"submitters\": [
      {\"name\": \"${PROVIDER_ROLE}\", \"uuid\": \"${PROVIDER_UUID}\"},
      {\"name\": \"${CLIENT_ROLE}\", \"uuid\": \"${CLIENT_UUID}\"}
    ]
  }"
```

### 6. Verify Fields (critical — always run after PATCH)

```bash
curl -s "${BASE_URL}/api/templates/${TEMPLATE_ID}" \
  -H "X-Auth-Token: ${DOCUSEAL_API_KEY}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
fields = d.get('fields', [])
checks = {
    'fields_count': len(fields),
    'all_have_uuid': all(f.get('uuid') for f in fields),
    'all_have_areas': all(f.get('areas') for f in fields),
    'unique_names': len(set(f['name'] for f in fields)) == len(fields),
    'all_have_submitter_uuid': all(f.get('submitter_uuid') for f in fields),
}
for k, v in checks.items():
    status = 'OK' if v else 'FAIL'
    print(f'  {k}: {v} [{status}]')
submitters = d.get('submitters', [])
for s in submitters:
    print(f'  submitter: {s[\"name\"]} uuid={s.get(\"uuid\", \"MISSING\")}')
all_ok = all(checks.values()) and all(s.get('uuid') for s in submitters)
print(f'VERIFICATION: {\"PASS\" if all_ok else \"FAIL\"} ')
"
```

Do not send the submission until verification passes.

### 7. Send for Signing (API key)

```bash
curl -s -X POST "${BASE_URL}/api/submissions" \
  -H "X-Auth-Token: ${DOCUSEAL_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{
    \"template_id\": ${TEMPLATE_ID},
    \"send_email\": true,
    \"order\": \"random\",
    \"submitters\": [
      {\"role\": \"${PROVIDER_ROLE}\", \"name\": \"Your Company\", \"email\": \"you@yourdomain.com\"},
      {\"role\": \"${CLIENT_ROLE}\", \"name\": \"Client Name\", \"email\": \"client@example.com\"}
    ]
  }"
```

Response includes `submission_id` and `slug` (signing link) — store in Pipeline Manager.

### 8. Check Status

```bash
curl -s "${BASE_URL}/api/submissions/${SUBMISSION_ID}" \
  -H "X-Auth-Token: ${DOCUSEAL_API_KEY}"
```

Statuses: `pending`, `completed`, `declined`, `expired`.

### 9. Download Signed Document

```bash
curl -s "${BASE_URL}/api/submissions/${SUBMISSION_ID}/documents" \
  -H "X-Auth-Token: ${DOCUSEAL_API_KEY}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for doc in d.get('documents', []):
    print(f'{doc[\"name\"]}: {doc[\"url\"]}')
"

# Download
curl -o signed_document.pdf "${DOC_URL}"
```

After download → offer to file to Drive via Drive Filer.

### 10. Cancel Submission

```bash
curl -s -X DELETE "${BASE_URL}/api/submissions/${SUBMISSION_ID}" \
  -H "X-Auth-Token: ${DOCUSEAL_API_KEY}"
```

Always cancel old submissions before re-sending amended documents.

## Coordinate Extraction

Signature field coordinates must be extracted per-document — the signature table position shifts as body text fills with real data. Never hardcode coordinates. **Always run detection on the final merged PDF** (if merging T&Cs + SOW, merge first, then detect — page indices change after merge).

### Detection

Use `self-sign/scripts/sign_detector.py` for all detection. It finds signature and date locations in PDFs and DOCX by scanning for patterns, underscore runs, and party labels.

```bash
python skills/self-sign/scripts/sign_detector.py \
  /path/to/document.pdf --format json \
  --company "Your Company Pte Ltd" \
  --alias "Service Provider" --alias "Consultant" \
  --config shared/config/company.yaml
```

Returns JSON with `page` (1-based), `coordinates` (`[x0, y0, x1, y1]` pixel bbox), `party_context`, `confidence`, `matched_party`, `location_type`.

### Coordinate Conversion (label bbox → DocuSeal placement)

`sign_detector.py` returns the bbox of the **entire text line** (including the label, e.g. `Signature: ____________________`). DocuSeal needs the **blank signing area**, not the label. You must convert:

```python
import fitz  # PyMuPDF

doc = fitz.open(pdf_path)
page = doc[location["page"] - 1]  # detector page is 1-based
PAGE_W, PAGE_H = page.rect.width, page.rect.height

x0, y0, x1, y1 = location["coordinates"]

# For underscore-line signatures: shrink to the underscore area
# (offset right past the label text, use the line height for field height)
# Adjust these offsets based on the document — the goal is to place
# the field on the blank area, not on the printed label.
PADDING = 4
label_offset = 80  # approximate width of "Signature: " label — adjust per template

field_x = x0 + label_offset
field_y = y0 + PADDING
field_w = max(50, (x1 - x0) - label_offset - PADDING)
field_h = (y1 - y0) - 2 * PADDING

# Normalize to DocuSeal 0-1 coordinates (0-indexed page for PATCH)
docuseal_x = round(field_x / PAGE_W, 4)
docuseal_y = round(field_y / PAGE_H, 4)
docuseal_w = round(field_w / PAGE_W, 4)
docuseal_h = round(field_h / PAGE_H, 4)
docuseal_page = location["page"] - 1  # 1-based → 0-indexed for PATCH
```

For multi-column signature tables (provider left, client right), use PyMuPDF's `page.find_tables()` or `page.search_for("____")` to detect each underscore run separately, then normalize each to its column's coordinates.

### Ambiguous Layouts

If detection returns no results or the layout is unclear (e.g. external documents with non-standard formats), ask the user to identify signing blocks: "Please note how many signatures are needed and on which pages."

## Workflow — Send NDA to Client

1. **Document Preparer** generates NDA `.docx` from template with client tokens filled
2. **eSign Connector** converts to PDF (LibreOffice headless)
3. Serve PDF via HTTPS (`file_serving` config)
4. MCP `create_template(name, url)` → empty template
5. GET template → extract `attachment_uuid` + submitter `uuid`s
6. Run coordinate extraction (`sign_detector.py`) → pixel coordinates
7. Convert label bbox → DocuSeal placement bbox → normalize to 0-1 coordinates
8. PATCH `/api/templates/{id}` with fields + uuids (API key)
9. **Verify** fields (count, uuids, unique names, submitter uuids) — do not send until pass
10. POST `/api/submissions` → send to signers (API key)
11. Store `submission_id` in Pipeline Manager deal documents
12. **Daily Briefing** flags pending signature > 7 days
13. When signed → GET `/api/submissions/{id}/documents` → download signed PDF
14. **Drive Filer** files to `02_Clients/{client}/NDA/`
15. **Pipeline Manager** moves deal to "NDA Signed" stage

## External Documents (third-party PDFs)

The coordinate extraction pipeline works on any PDF with a detectable signature table or underscore lines. For external documents (director resolutions, bank forms, government forms):

1. Source the document: vendor website → email inbox → local filesystem → ask user
2. Skip LibreOffice conversion if already PDF
3. Verify table row count matches expectations — external docs may have fewer rows
4. For underscore-line signatures, use `sign_detector.py` (PyMuPDF `page.search_for`)
5. If layout is ambiguous, ask the user to identify signing blocks

## Rules (Non-Negotiable)

1. **Every field MUST have `uuid`** — use `python3 -c "import uuid; print(uuid.uuid4())"`. Without it, the signing page shows "Missing field".

2. **Each role's fields MUST have unique names** — `Provider Signature` / `Client Signature`, never bare `Signature`. DocuSeal links same-named fields across submitters.

3. **Pages are 0-indexed** — `page: 0` = first page. The official API docs say 1-indexed for `POST /templates/pdf`, but PATCH uses 0-indexed.

4. **Coordinates go inside the `areas[]` array** — not flat on the field object. The key is `page`, not `page_number`.

5. **Each submitter MUST have an explicit `uuid`** — the API does not auto-assign the client submitter's uuid.

6. **PATCH is full replacement** — you must send ALL fields, not just new ones. Existing fields not in the PATCH body are deleted. Always GET the template first, merge, then PATCH.

7. **Use `/api/` paths, never `/api/v1/`** — self-hosted CE uses internal API paths. `/api/v1/` returns 404.

8. **After fixing template fields, create a NEW submission** — existing submissions are frozen.

9. **Cancel (DELETE) the old submission before re-sending** an amended document — old signing links must be invalidated.

10. **Always run the post-PATCH verification checklist** before sending — field count, uuids present, unique names, submitter uuids. Do not send until it passes.

11. **Coordinates must be re-extracted per document** — the table position shifts as body text fills. Never hardcode coordinates.

12. **Secrets by `.env` reference only** — `company.yaml` holds config; `.env` holds tokens. Never commit secrets.

13. **Redact sensitive PII before upload** (passport, NRIC, IC numbers) using PyMuPDF.

14. **self-sign ≠ esign-connector** — one places your signature locally; the other collects third-party signatures via DocuSeal.

## Integrations

| Skill | Integration |
|---|---|
| Document Preparer | After generating a doc, offer to send via eSign |
| Pipeline Manager | Track eSign submission_id per deal document |
| Daily Briefing | Flag documents pending signature > 7 days |
| Weekly Review | Documents signed this week, still pending |
| Drive Filer | Auto-file signed documents to client folder |
| Self-Sign | Shares sign_detector.py for coordinate detection |

## Pitfalls

- **MCP token ≠ API key** — they are different credentials with different headers. MCP uses `Authorization: Bearer` on `/mcp`; API key uses `X-Auth-Token` on `/api/*`.
- **`POST /templates/pdf` is Pro-gated on CE free** — use MCP `create_template` + API key PATCH instead.
- **PATCH replaces all fields** — not append. Always GET first, modify, then PATCH the full set.
- **`POST /submissions/pdf` (one-off from PDF) is also Pro-gated** — always go through template → PATCH → submission flow on CE free.
- **Session expiry** — API key does not expire between calls (unlike browser cookies). No re-login needed.
- **SMTP relay** — email notifications go through the configured SMTP relay (e.g. smtp-relay.gmail.com:587). Set in DocuSeal's Settings → Email → SMTP.
- **Custom domain** — signing URLs use the configured domain (set in DocuSeal `HOST` env).
- **Multiple submitters** — if only one signature is needed (e.g., director resolution), use one submitter, not two.

## Verification Checklist

- [ ] Template created with the correct signature/date fields and unique field names per role.
- [ ] Submitter roles match the parties (`esign.provider_role` / `esign.client_role`).
- [ ] Send response persisted (`submission_id` / slug stored, e.g. on the Pipeline Manager deal).
- [ ] Status trackable via `GET /api/submissions/{id}` (`pending` / `completed` / `declined` / `expired`).
- [ ] Download integrity verified (signed PDF retrieved and offered to Drive Filer).
- [ ] Cancel confirmed (`DELETE /api/submissions/{id}` succeeded before any re-send).

