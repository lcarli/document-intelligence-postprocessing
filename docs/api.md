# API contract

API prefix: `/api/v1`. All routes require an Entra v2 Bearer access token,
the configured permission, an allowed caller application, and the correct tenant.
Authorization is also checked for every status/result read.

## Submit a job

`POST /jobs`, `Content-Type: application/json`.

See `examples/request.json`. Only `document` and `analysis` are accepted.
Each reference contains an exact relative blob `name` and a quoted `etag`.
No account, container, URL, credentials, arbitrary processor names, thresholds,
or mode can be supplied by the caller.

The source document and analysis are read from the configured input container.
The analysis blob contains the completed REST `analyzeResult` object. Upstream
must ensure both inputs belong to the same document version.

Response: `202`, with `job_id`, `status_path`, and `result_path`. Paths are
relative and never derived from an untrusted Host header. Prefix them with your
trusted deployment origin. If exposing the app behind a path-rewriting proxy,
configure that proxy's external path mapping explicitly.

Request JSON is limited to 8 KiB by the handler. Also configure
`FUNCTIONS_REQUEST_BODY_SIZE_LIMIT=8192` and an ingress/gateway limit to reject
oversized bodies before Python buffers them.

## Query status

`GET /jobs/{job_id}`.

```json
{
  "job_id": "00000000-0000-4000-8000-000000000010",
  "runtime_status": "Completed",
  "status": "needs_review",
  "requires_review": true
}
```

`runtime_status` describes orchestration execution. Application `status` can be
`processing`, `completed`, `needs_review`, or `failed`. A completed orchestration
can contain a failed processor, so checking only `runtime_status` is unsafe.

Failed orchestration exception details are not exposed. Inspect access-controlled
operational telemetry using the opaque job ID. `409/not_scheduled` means the
manifest exists but the scheduler has not accepted that job; the submission
requires operational investigation, not an assumption of success.

## Retrieve the result

`GET /jobs/{job_id}/result`.

If not available yet, returns `202/result_not_available`. Query status to
distinguish still-running work from a permanently failed orchestration. Otherwise
returns `200` and the result envelope, including failed application results:

```json
{
  "schema_version": "1.0",
  "job_id": "00000000-0000-4000-8000-000000000010",
  "status": "needs_review",
  "requires_review": true,
  "conflicting_targets": [],
  "created_at": "2026-01-01T00:00:00+00:00",
  "analyze_result": {
    "pages": []
  },
  "processors": [
    {
      "processor": {"name": "grayscale_checkboxes", "version": "1.0.0"},
      "mode": "shadow",
      "status": "completed",
      "requires_review": true,
      "observations": [
        {
          "observation_id": "/pages/0/selectionMarks/0",
          "scope": "page",
          "page_number": 1,
          "original_state": "selected",
          "original_confidence": 0.95,
          "decision": "needs_review",
          "disagrees_with_di": false,
          "effective_state": "selected",
          "requires_review": true,
          "score": 120,
          "sigma": 3.5,
          "crop_bounds": [10, 20, 60, 70],
          "reason": null
        }
      ],
      "findings": [],
      "timings_ms": {"render": 0.0, "roi": 0.0, "total_compute": 0.0},
      "errors": [],
      "source_sha256": {},
      "parameters": {
        "pdf_dpi": 300,
        "selected_threshold": 90,
        "unselected_threshold": 150,
        "central_fraction": 0.6,
        "sigma_min": 1.5,
        "sigma_fraction": 0.07
      }
    }
  ]
}
```

The example is abbreviated, synthetic, and not a timing/accuracy claim.
`analyze_result` is the original JSON object, unchanged semantically; its byte
formatting is not preserved. SHA-256 hashes refer to the original input bytes.

`effective_state` is an annotation, not an in-place field update. In shadow mode
it preserves DI's value even when the suggestion disagrees or abstains.
In enforce mode, abstention is `null`; consumers must not convert it to
`unselected`.

`disagrees_with_di` identifies a definite verifier decision opposite to DI's
original state. An abstention is not a definite disagreement.

`crop_bounds` are `[left, top, right, bottom]` pixel coordinates with exclusive
right/bottom boundaries. `score` is the minimum central blurred intensity.
Page/field IDs are JSON Pointers into the original result. Sensitive field names
can appear in reports, but not in Durable history or application logs.

No detected marks produces a warning finding and review, not a claim that every
expected box is empty. Per-field business completeness is outside the initial
processor's scope.

If a processor fails, its report contains fixed `errors` codes. The aggregate
status is `failed`, `requires_review=true`, and `analyze_result=null`. Inputs
remain available in private storage for investigation. No partial result should
be treated as an approved business decision.

## HTTP errors

| HTTP | Meaning |
|---|---|
| `400` | Invalid request JSON or schema. |
| `401` | Missing, invalid, expired, or incorrectly signed access token. |
| `403` | Valid token without the required caller, tenant, identity, or permission. |
| `404` | Unknown/invalid job ID, or a job owned by a different caller identity. |
| `409` | Input version, stored metadata, or orchestration consistency problem. |
| `413` | Submission body exceeds 8 KiB. |
| `415` | Submission is not JSON. |
| `503` | Transient storage unavailability; retry according to the response header. |

All application responses use `Cache-Control: no-store`. Durable/runtime failures
outside the application may also return platform errors. Retry lost submissions
carefully: no client-supplied idempotency key is implemented, so a second POST is
a new job and can duplicate work.
