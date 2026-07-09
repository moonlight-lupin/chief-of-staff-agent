---
name: esign-connector
description: "Send documents for e-signature via self-hosted DocuSeal instance. Create templates, send to submitters, check status, download signed copies, cancel submissions. Integrates with Document Preparer and Pipeline Manager."
version: 0.1.1
author: moonlight-lupin
license: MIT
metadata:
  requires_skills: [google-workspace]
---

# eSign Connector (DocuSeal)

Send documents for electronic signature via a self-hosted DocuSeal instance. This handles **third-party signing** — sending documents to clients/partners for their signature. For self-signing (placing your own signature on a received document), use the self-sign skill.

## When to Use

- "Send NDA to {client} for signing"
- "Send SOW for signature"
- "Check if {client} has signed the contract"
- "Download the signed NDA"
- "Cancel the pending signature request"

## Prerequisites

1. DocuSeal instance running (self-hosted, Docker)
2. `DOCUSEAL_API_KEY` in `.env`
3. Config in `company.yaml` → `esign` section

## Config (from company.yaml)

```yaml
esign:
  provider: docuseal
  url: "https://sign.yourdomain.com"
  admin_email: "admin@yourdomain.com"
  smtp_from: "admin@yourdomain.com"
  domain: "sign.yourdomain.com"
```

## API Authentication

DocuSeal uses cookie-based auth for the web UI and API token for API calls:

```bash
# API key from .env
DOCUSEAL_API_KEY=$(grep DOCUSEAL_API_KEY ~/.hermes/.env | cut -d= -f2)
```

## Operations

### 1. Create Template from PDF/DOCX

Upload a document to DocuSeal and create a fillable template with signature fields.

```bash
curl -X POST "{esign_url}/api/templates" \
  -H "Authorization: Bearer $DOCUSEAL_API_KEY" \
  -F "file=@/path/to/document.pdf" \
  -F "name=NDA Mutual"
```

Response includes `template_id` — store in Pipeline Manager deal documents.

### 2. Send for Signing

Send a template to one or more submitters.

```bash
curl -X POST "{esign_url}/api/submissions" \
  -H "Authorization: Bearer $DOCUSEAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": 42,
    "submitters": [
      {
        "name": "John Tan",
        "email": "john@client.com",
        "role": "Client",
        "fields": [
          {"name": "client_name", "value": "Acme Corp"},
          {"name": "client_address", "value": "123 Anson Road, Singapore"}
        ]
      },
      {
        "name": "Your Name",
        "email": "you@yourdomain.com",
        "role": "Service Provider"
      }
    ]
  }'
```

**Key fields:**
- `uuid`: use `randomUUID` for each submitter (DocuSeal patch)
- `role`: must be unique per submitter (DocuSeal patch)
- `fields`: prefill values become readonly in the signing UI

Response includes `submission_id` — store in Pipeline Manager.

### 3. Check Submission Status

```bash
curl -s "{esign_url}/api/submissions/{id}" \
  -H "Authorization: Bearer $DOCUSEAL_API_KEY"
```

Statuses: `pending`, `completed`, `declined`.

### 4. Download Signed Document

```bash
# Get submission details to find document URL
RESPONSE=$(curl -s "{esign_url}/api/submissions/{id}" \
  -H "Authorization: Bearer $DOCUSEAL_API_KEY")

# Extract document URL and download
DOC_URL=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['documents'][0]['url'])")
curl -o signed_document.pdf "$DOC_URL"
```

After download → offer to file to Drive via Drive Filer.

### 5. Cancel Submission

```bash
curl -X DELETE "{esign_url}/api/submissions/{id}" \
  -H "Authorization: Bearer $DOCUSEAL_API_KEY"
```

## Workflow — Send NDA to Client

1. **Document Preparer** generates NDA .docx from template with client tokens filled
2. **eSign Connector** uploads the PDF to DocuSeal → creates template with signature fields
3. **eSign Connector** sends to client + user for signing
4. Store `submission_id` in Pipeline Manager deal documents
5. **Daily Briefing** flags pending signature > 7 days
6. When signed → **eSign Connector** downloads signed PDF → **Drive Filer** files to `02_Clients/{client}/NDA/`
7. **Pipeline Manager** moves deal to "NDA Signed" stage

## Pitfalls

- **UUID patch**: DocuSeal requires unique UUIDs per submitter. Use `python3 -c "import uuid; print(uuid.uuid4())"` for each.
- **Unique role names**: Each submitter must have a unique `role` name. "Client" and "Service Provider" — never duplicate.
- **Page index 0-based**: DocuSeal API uses 0-indexed page numbers. Page 1 = index 0.
- **Cookie auth for web UI**: Browser login requires CSRF fetch, NOT form typing. Use `browser_console` for login.
- **SMTP relay**: Email notifications should go through an SMTP relay (e.g. smtp-relay.gmail.com:587).
- **Custom domain**: Signing URLs use the configured domain (set in DocuSeal `HOST` env).
- **Redact sensitive info**: Use pymupdf to redact passport/IC numbers before uploading to DocuSeal.
- **Cancel old subs**: DELETE `/api/submissions/{id}` to cancel stale submissions. Don't leave them pending.
- **Download signed docs**: GET `/api/submissions/{id}` → `documents[0].url` for the signed PDF download link.

## Integrations

| Skill | Integration |
|---|---|
| Document Preparer | After generating a doc, offer to send via eSign |
| Pipeline Manager | Track eSign submission_id per deal document |
| Daily Briefing | Flag documents pending signature > 7 days |
| Weekly Review | Documents signed this week, still pending |
| Drive Filer | Auto-file signed documents to client folder |