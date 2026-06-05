# Installing behind a corporate proxy / firewall

When you create a project, the app downloads three kinds of things over HTTPS:

1. Language packages (`npm install`, `pip install`, `mvn install`, `dotnet restore`)
2. **Playwright browser binaries** (`playwright install`) — this is the step that fails most often
3. (At test-run time) the generated TypeScript project re-runs `npx playwright install`

A corporate network can break these in **two different ways**. The fix is different for
each, so first figure out which one you have by reading the error message.

---

## A. TLS / certificate errors (most common)

**What you see** — the download *connects* but the error mentions a certificate, e.g.:

```
unable to get local issuer certificate
self-signed certificate in certificate chain
SSL certificate problem
```

**Why** — your company proxy (Zscaler, Netskope, etc.) inspects HTTPS traffic by replacing
the real certificate with its own. Node doesn't trust that certificate by default.

**The fix** — trust your company's certificate. The app already does the safe default for
you (`NODE_USE_SYSTEM_CA=1`, which trusts the certificates in your OS). You usually don't
need to do anything. If it still fails:

| Set this on the app's environment | When to use it |
|---|---|
| `BIG_QA_CA_CERTS=C:\path\to\corporate-ca.pem` | Your Node is older than v22.15, **or** the company CA isn't in the OS store. Point it at the company root-CA `.pem` file (ask IT for it). |
| `BIG_QA_INSECURE_TLS=1` | **Last resort only.** Turns OFF certificate checking entirely (insecure — accepts any certificate). The app logs a warning every time it's used. Prefer the row above. |

> Do **not** set `NODE_TLS_REJECT_UNAUTHORIZED=0` by hand. `BIG_QA_INSECURE_TLS=1` does the
> same thing but only for the install commands, and it's logged — so it can't be left on
> silently for the whole app.

---

## B. Firewall blocks the download (host is unreachable)

**What you see** — the download never connects at all, e.g.:

```
ETIMEDOUT  /  ECONNREFUSED  /  getaddrinfo ENOTFOUND
Could not resolve host
407 Proxy Authentication Required
```

**Why** — the firewall blocks the download host outright. **A certificate setting will not
help here** — there's no connection to negotiate a certificate on. Pick one option:

| Option | Set on the app's environment | Notes |
|---|---|---|
| **Go through the proxy** | `HTTPS_PROXY=http://user:pass@proxy:8080`<br>`HTTP_PROXY=http://user:pass@proxy:8080`<br>`NO_PROXY=localhost,127.0.0.1` | Use if your network allows egress only via the corporate proxy. |
| **Use an internal mirror** | `PLAYWRIGHT_DOWNLOAD_HOST=https://nexus.yourcorp.com/playwright` | If IT hosts the browser bundles on Nexus/Artifactory. Point npm/pip/Maven at their internal registries the same way. |
| **Install offline** | `PLAYWRIGHT_BROWSERS_PATH=C:\shared\ms-playwright`<br>`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` | Download the browsers once on a machine with internet, copy them to a shared path, and reuse them — no download needed. |
| **Ask IT to allowlist** | (firewall rule) | The clean long-term fix. Allowlist `cdn.playwright.dev`, `playwright.azureedge.net`, and your npm/PyPI/Maven registries. |

---

## How to set these variables

These must be set on the **environment of the machine running the app** (not inside a
generated project), then **restart the app** so it picks them up.

**Windows (PowerShell), for the current session:**

```powershell
$env:BIG_QA_CA_CERTS = "C:\certs\corporate-ca.pem"
# then start the app from the same PowerShell window
```

**Windows, permanently (all future sessions):**

```powershell
setx BIG_QA_CA_CERTS "C:\certs\corporate-ca.pem"
# close and reopen the terminal afterwards
```

**Linux / macOS:**

```bash
export BIG_QA_CA_CERTS=/etc/ssl/certs/corporate-ca.pem
```

You can also put them in the app's `.env` file alongside the existing settings.

---

## Quick reference

| Error keyword | Cause | First thing to try |
|---|---|---|
| `self-signed certificate`, `unable to get local issuer` | TLS interception (Section A) | Already auto-fixed; if not, set `BIG_QA_CA_CERTS` |
| `ETIMEDOUT`, `ENOTFOUND`, `407 Proxy` | Host blocked (Section B) | Set `HTTPS_PROXY`, or use a mirror / offline cache |
