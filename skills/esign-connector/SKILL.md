---
name: esign-connector
description: "Send documents for e-signature via self-hosted DocuSeal. Create templates with signature fields, dispatch to submitters, track status, download signed copies, cancel submissions. Integrates with Document Preparer and Pipeline Manager."
version: 0.2.0
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
- **`file_serving.mode: local_https`** — start a temporary file server with an HTTPS tunnel. Clean up after template creation.

Use unguessable filenames. Set `cleanup_after_send: true`.

### 4. Create Template (MCP)

```python
# Via MCP tool:
create_template(name="NDA - Client Name", url="https://files.yourdomain.com/nda_client.pdf")
# Returns {"id": template_id}
```

### 5. Add Fields (API key — PATCH)

After template creation, GET the template to obtain `attachment_uuid` and submitter `uuid`, then PATCH fields with coordinates.

```bash
API_KEY=$(grep DOCUSEAL_API_KEY ~/.hermes/.env | cut -d= -f2)
BASE_URL=$(python3 -c "import yaml; print(yaml.safe_load(open('shared/config/company.yaml'))['esign']['url'])")

# Step A: GET template to extract UUIDs
TEMPLATE_JSON=$(curl -s "${BASE_URL}/api/templates/${TEMPLATE_ID}" \
  -H "X-Auth-Token: ${API_KEY}")

# Extract attachment_uuid and submitter_uuid from the JSON
ATTACHMENT_UUID=$(echo "$TEMPLATE_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['schema'][0]['attachment_uuid'])
")
SUBMITTER_UUID=$(echo "$TEMPLATE_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['submitters'][0]['uuid'])
")

# Step B: PATCH fields with coordinates
curl -s -X PATCH "${BASE_URL}/api/templates/${TEMPLATE_ID}" \
  -H "X-Auth-Token: ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"NDA - Client Name\",
    \"fields\": [
      {
        \"uuid\": \"$(python3 -c 'import uuid; print(uuid.uuid4())')\",
        \"name\": \"Provider Signature\",
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
        \"name\": \"Client Signature\",
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
      {\"name\": \"Provider\", \"uuid\": \"${PROVIDER_UUID}\"},
      {\"name\": \"Client\", \"uuid\": \"${CLIENT_UUID}\"}
    ]
  }"
```

### 6. Verify Fields (critical — always run after PATCH)

```bash
curl -s "${BASE_URL}/api/templates/${TEMPLATE_ID}" \
  -H "X-Auth-Token: ${API_KEY}" | python3 -c "
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
  -H "X-Auth-Token: ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{
    \"template_id\": ${TEMPLATE_ID},
    \"send_email\": true,
    \"order\": \"random\",
    \"submitters\": [
      {\"role\": \"Provider\", \"name\": \"Your Company\", \"email\": \"you@yourdomain.com\"},
      {\"role\": \"Client\", \"name\": \"Client Name\", \"email\": \"client@example.com\"}
    ]
  }"
```

Response includes `submission_id` and `slug` (signing link) — store in Pipeline Manager.

### 8. Check Status

```bash
curl -s "${BASE_URL}/api/submissions/${SUBMISSION_ID}" \
  -H "X-Auth-Token: ${API_KEY}"
```

Statuses: `pending`, `completed`, `declined`, `expired`.

### 9. Download Signed Document

```bash
curl -s "${BASE_URL}/api/submissions/${SUBMISSION_ID}/documents" \
  -H "X-Auth-Token: ${API_KEY}" | python3 -c "
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
  -H "X-Auth-Token: ${API_KEY}"
```

Always cancel old submissions before re-sending amended documents.

## Coordinate Extraction

Signature field coordinates must be extracted per-document — the signature table position shifts as body text fills with real data. Never hardcode coordinates.

### Detection routing

| Document type | Method | Tool |
|---|---|---|
| Formal signature table (multi-row, role-bound) | ODL + pdfplumber | `esign-connector/scripts/docuseal_fields.py` |
| Underscore lines or pattern labels | Reuse self-sign detector | `self-sign/scripts/sign_detector.py` |
| Ambiguous layout | Ask user to identify blocks | User confirmation |

### Normalization (shared)

Both detection methods produce pixel coordinates (top-down). Convert to DocuSeal normalized coordinates:

```python
PAGE_W, PAGE_H = page.width, page.height  # from PyMuPDF
docuseal_x = round(px_x / PAGE_W, 4)
docuseal_y = round(px_y / PAGE_H, 4)
docuseal_w = round(px_w / PAGE_W, 4)
docuseal_h = round(px_h / PAGE_H, 4)
docuseal_page = page.page_number - 1  # 0-indexed for PATCH
```

### Using sign_detector.py (underscore/pattern detection)

```bash
python skills/self-sign/scripts/sign_detector.py \
  /path/to/document.pdf --format json \
  --company "Your Company Pte Ltd" \
  --alias "Service Provider" --alias "Consultant" \
  --config shared/config/company.yaml
```

Returns JSON with `page`, `coordinates` (pixel bbox), `party_context`, `confidence`, `matched_party`. Normalize the coordinates as above, then build the PATCH fields payload.

### ODL + pdfplumber (table detection)

For formal signature tables (e.g., phronesis-docs templates with 5-row tables: header, signature, name, title, date):

1. Run `opendataloader_pdf` to find the signature table page + bbox (last Table element).
2. Use `pdfplumber` `page.find_tables()` to get row/column boundaries.
3. Assign fields by row position: row 1 = signature, row 2 = name, row 3 = title, row 4 = date.
4. Left column = Provider, right column = Client.
5. Normalize to DocuSeal coordinates.

See `scripts/docuseal_fields.py` for the implementation. ODL is an optional dependency — if not installed, fall back to `sign_detector.py` underscore detection.

## Workflow — Send NDA to Client

1. **Document Preparer** generates NDA `.docx` from template with client tokens filled
2. **eSign Connector** converts to PDF (LibreOffice headless)
3. Serve PDF via HTTPS (`file_serving` config)
4. MCP `create_template(name, url)` → empty template
5. GET template → extract `attachment_uuid` + submitter `uuid`s
6. Run coordinate extraction (sign_detector.py or ODL+pdfplumber) → pixel coordinates
7. Normalize to DocuSeal 0-1 coordinates
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