# Playwright + TypeScript + Cucumber BDD Template

A BDD test project: **Cucumber** scenarios driving **Playwright** in **TypeScript**, run via
`ts-node` (no compile step) with HTML reporting.

## Prerequisites

- **Node.js 18+** and **npm** (`node -v`, `npm -v`)
- Internet on first `npm install` (packages) and first run (`npx playwright install` downloads
  the browser binaries). Behind a corporate proxy, see `../../SETUP-PROXY.md`.

## Quick start

```bash
npm install
npx playwright install      # one-time browser download
npm test                    # runs test-runner.js -> cucumber-js via ts-node
npm run report              # regenerate the HTML report
```

## Layout

| Path | Purpose |
|------|---------|
| `test/features/` | Gherkin `.feature` scenarios |
| `test/stepDefinitions/` | Step bindings (generated at scaffold time / yours) |
| `test/pageObjects/` | Page Objects |
| `test/hooks/hooks.ts` | Per-scenario browser setup/teardown (exports `page` and `ICustomWorld`) |
| `test/utils/configReader.ts` | Reads `.env` |
| `test/utils/reports.ts` | HTML report generation |
| `cucumber.config.js`, `test-runner.js`, `tsconfig.json` | Config / runner |

## Configuration

Edit `.env`: `APP_URL`, `BROWSER` (chromium\|firefox\|webkit), `HEADLESS` (true\|false),
`USER`, `PASSWORD`.

Step definitions type their callbacks as `function (this: ICustomWorld)` and receive `this.page`
from the hooks — see `test/hooks/hooks.ts`.
