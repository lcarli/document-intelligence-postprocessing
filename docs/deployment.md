# Deployment and operations

This project prepares code and configuration, not an Azure environment. It
does not create resources, modify an existing DI application, or grant roles
automatically. Provision infrastructure through your organization's approved
IaC and review process.

## Recommended Azure topology

- Azure Functions v4, Python 3.12 or 3.13, Linux **Flex Consumption**, initially
  2,048 MiB. Use an appropriately limited maximum instance count and HTTP
  concurrency for your workload. `host.json` also limits activity concurrency.
- **Durable Task Scheduler** with a dedicated task hub for this app/release.
  This project does not use the legacy Azure Storage Durable backend.
- Private input/output Blob containers. A single configured public-cloud account
  endpoint is used; private DNS can resolve it to a private endpoint.
- Separate host/deployment Storage as required by the Functions platform.
- System-assigned managed identity for the Function App.
- Entra application registration for API permissions and audience.
- Application Insights with approved retention and access control.

The app does not need access to a DI resource: extraction has already completed.
If processors require different trust boundaries or compute profiles, move them
to separate apps/workers instead of assuming activity-level security isolation.

## Authentication

There is **no authentication bypass**, including local development. The
Functions binding uses `ANONYMOUS` only because **the application itself validates
Entra JWTs**, rather than accepting Function keys as user authorization.

Configure an API registration in your tenant:

1. Set `requestedAccessTokenVersion` to `2`.
2. Expose a delegated scope `Postprocess.Run` for interactive clients, and/or
   an application role `Postprocess.Run` with allowed member type `Application`
   for service-to-service callers.
3. Assign and consent the permission to approved callers. Configure their
   application IDs in `APP_AUTH_ALLOWED_CLIENT_IDS`. This is an explicit allowlist.
4. Set `APP_AUTH_TENANT_ID` and `APP_AUTH_AUDIENCE` to the expected tenant and token
   audience. Entra v2 custom-API access tokens normally use the API client UUID
   as `aud`, even when the requested scope uses an `api://...` identifier.
5. Acquire a v2 access token for the API. Requesting a management, Graph, or
   Storage token does not authorize this API.

Validation checks RS256 signature, issuer, audience, expiration, not-before,
tenant, authorized client (`azp`), object ID (`oid`), and scope/role. Ownership
uses a hash of tenant + caller app + object ID. A different admin or application
does not automatically inherit access to submitted jobs.

Use App Service Authentication/Entra at ingress as an additional layer if
required. Preserve the Bearer header for application validation. Do not expose
the Durable management API or distribute Function host/system keys to callers.
Apply private ingress, APIM restrictions, and request/rate limits according to
your organization's threat model.

## Managed identity and least privilege

| Scope | Function App identity permission |
|---|---|
| Input container | Storage Blob Data Reader |
| Output container | Storage Blob Data Contributor |
| Durable Task Scheduler / task hub | Durable Task Data Contributor |
| Host/deployment Storage | Platform-required identity-based host and package deployment roles |

Host roles depend on the selected hosting/deployment topology; follow the
Functions identity-based storage requirements rather than assuming that the
application's input/output roles are sufficient.

The calling application separately needs upload permission on the input
container and its API permission. Do not give API callers direct access to the
entire output container unless deliberately authorized: that would bypass the
API's per-job ownership checks.

`DefaultAzureCredential` supports the Function App's managed identity and local
developer identity. Interactive browser fallback is disabled. If using a
user-assigned identity, configure `AZURE_CLIENT_ID` consistently for the SDK and
the scheduler's `ClientID` connection parameter.

## Application settings

Copy `local.settings.example.json` to untracked `local.settings.json` for local
development. Replace every placeholder; invalid or missing settings fail closed.
Never commit credentials or the populated file.

| Setting | Default / purpose |
|---|---|
| `APP_BLOB_ACCOUNT_URL` | Required HTTPS `https://ACCOUNT.blob.core.windows.net`; no SAS, path, or arbitrary host. |
| `APP_INPUT_CONTAINER` | `postprocessing-inputs` |
| `APP_OUTPUT_CONTAINER` | `postprocessing-outputs` |
| `APP_AUTH_TENANT_ID` | Required tenant UUID. |
| `APP_AUTH_AUDIENCE` | Required expected API token audience. |
| `APP_AUTH_ALLOWED_CLIENT_IDS` | Required comma-separated caller application UUIDs. |
| `APP_AUTH_REQUIRED_PERMISSION` | `Postprocess.Run` |
| `CHECKBOX_VERIFIER_MODE` | `shadow`; one of `off`, `shadow`, `enforce`. |
| `CHECKBOX_SELECTED_THRESHOLD` | `90`; strictly below means selected. |
| `CHECKBOX_UNSELECTED_THRESHOLD` | `150`; strictly above means unselected. |
| `APP_MAX_DOCUMENT_BYTES` | 16 MiB, configurable up to 32 MiB. |
| `APP_MAX_ANALYSIS_BYTES` | 8 MiB, configurable up to 16 MiB. |
| `APP_MAX_PAGES` | 20, configurable up to 100. |
| `APP_MAX_PAGE_PIXELS` | 16,000,000 per rendered page, configurable up to 40,000,000. |
| `APP_MAX_OBSERVATIONS` | 5,000, configurable up to 10,000. |
| `FUNCTIONS_REQUEST_BODY_SIZE_LIMIT` | Set `8192` at ingress/worker; handler also checks this. |
| `DURABLE_TASK_SCHEDULER_CONNECTION_STRING` | Required scheduler endpoint, authentication, and task hub. |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Your access-controlled telemetry resource, if enabled. |

For Azure, use an identity-based scheduler connection:

```text
Endpoint=https://YOUR_SCHEDULER_ENDPOINT;Authentication=ManagedIdentity;TaskHub=postprocessing
```

Use the scheduler's **actual resource endpoint**, not a constructed hostname.
For a user-assigned identity, append `;ClientID=YOUR_MANAGED_IDENTITY_CLIENT_ID`.

Configure identity-based Functions host Storage according to the hosting plan,
for example `AzureWebJobsStorage__accountName`, `__credential=managedidentity`,
and the relevant service URI/client-ID settings. **Do not deploy the local
Azurite `UseDevelopmentStorage=true` value to Azure.**

`host.json` uses `azureManaged` and extension bundle `[4.32.0, 5.0.0)`, consistent
with the current official Durable Task Scheduler quickstart and Python samples.
The hub in `host.json` and the scheduler connection must agree. Use separate task
hubs for separate apps/environments; never share a hub across incompatible code.

## Networking and data protection

- Require HTTPS and TLS 1.2+, disable public Blob access/shared keys where
  supported, and use private endpoints plus private DNS for application data.
- Integrate the Function App with an approved VNet. Verify outbound reachability
  to Blob, scheduler, Entra signing-key discovery, managed identity, and telemetry
  using approved routing and DNS.
- Set scheduler network restrictions deliberately. An empty allowlist may deny
  all access; never open it broadly to work around an organizational policy.
- Secure the Function App's deployment/SCM endpoint and use federated identity
  for CI deployment instead of publish-profile secrets.
- Apply malware scanning and native-library patching requirements to untrusted
  documents. Pixel/byte/page limits are safeguards, not a PDF sandbox.
- Do not include personal data in blob names, job tags, or telemetry. Review
  platform-generated logs separately from this code's sanitized logging.
- Configure encryption, retention, input versioning/immutability, output lifecycle,
  backup, and incident handling for your data classification.
- Keep referenced inputs until jobs and relevant retries finish. Purge scheduler
  history and private manifests/results through an approved retention process.

## Local Function host

The offline demo and unit tests do not require these services. To exercise the
real HTTP/orchestration path locally, you need:

- Azure Functions Core Tools v4 and a supported x64 Python environment.
- Azurite for Functions host Storage.
- The Durable Task Scheduler emulator, configured with the task hub
  `postprocessing`.
- An approved Azure Blob development account and a developer identity with the
  required container permissions. The application adapter deliberately does not
  accept arbitrary HTTP or emulator storage endpoints.
- Real Entra API settings and an authorized access token.

Run the emulators using the current Microsoft quickstart, then:

```powershell
Copy-Item .\local.settings.example.json .\local.settings.json
# Populate the untracked local.settings.json first.
func start
```

Submit synthetic inputs and verify unauthorized access, cross-owner denial,
successful completion, review, changed ETags, invalid files, transient failures,
and retries. Local emulator success alone does not validate private Azure
networking or managed-identity permissions.

## Publish the code

After infrastructure, identity, networking, and settings are reviewed:

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m pytest --cov=postprocessing --cov=function_app --cov-fail-under=85
func azure functionapp publish YOUR_FUNCTION_APP_NAME --python
```

Publish from the repository root. Build/install dependencies for the target Linux
runtime; do not upload a Windows virtual environment or `.test-packages`.
No automatic Azure deployment workflow is included. GitHub CI validates code
only and has read-only repository permissions.

## Operational acceptance before enforcement

1. Test the real Azure path with synthetic inputs, Entra tokens, private DNS,
   managed identity, Storage, and the scheduler. Confirm restart/retry behavior.
2. Use independent labeled samples, including faint marks, blank halftone boxes,
   different form families, and multiple input resolutions.
3. Report false positives, false negatives, abstentions, missing DI detections,
   and coverage separately. Review is not a correct automated classification.
4. Measure full submit-to-completion p50/p95 and throughput under realistic load.
   Report renderer/ROI compute separately from queueing, I/O, and cold starts.
5. Keep `shadow` until the accepted-error rate and review policy are approved.
   Raising/lowering a global threshold cannot guarantee zero regression.
6. Roll out enforce mode per approved application configuration. Do not change
   the original DI result; consumers deliberately adopt effective annotations.
7. Drain in-flight jobs or preserve old processor versions and compatible
   orchestration code during deployments. A changed version is not silently
   substituted for the snapshotted one.

This repository does not include an Azure integration/load-test result or
production accuracy claim. Those must be produced in the deployment environment.
