# JMeter Performance Framework

Maven-based JMeter framework for **API**, **functional (Playwright)** and **CLI** test execution.
It handles **CSV, JSON and XML** payloads and writes **CSV results and an HTML dashboard**.

## Folder structure

```
jmeter-framework/
├── pom.xml                      Maven build (jmeter-maven-plugin, profiles, properties)
├── TestScripts/                 JMeter test plans (*.jmx) and helper scripts
│   ├── api/api_payload_test.jmx            HTTP load test, JSON + XML + CSV payloads
│   ├── functional/playwright_ui_test.jmx   Playwright for Java, one sample per step
│   ├── functional/playwright_steps.groovy  Playwright step logic (start/navigate/verify/stop)
│   ├── cli/cli_execution_test.jmx          OS Process Sampler, data-driven from CSV
│   └── common/payload_handler.groovy       Shared payload loader
├── Payloads/
│   ├── csv/   users.csv, cli_commands.csv
│   ├── json/  create_user.json
│   └── xml/   create_order.xml
├── Results/                     Created by the run (csv/, html/, screenshots/)
└── src/test/java/...            PayloadValidationTest (checks every payload before JMeter runs)
```

## Prerequisites
- JDK 17+ and Maven 3.8+
- Internet access on the first run (Maven downloads JMeter and Playwright)
- For functional tests, install the Playwright browser once:
  `mvn -Pinstall-browsers generate-resources`

## Run

| Goal | Command |
|---|---|
| Everything | `mvn clean verify` |
| API only | `mvn clean verify -Papi` |
| Functional (Playwright) only | `mvn clean verify -Pfunctional` |
| CLI only | `mvn clean verify -Pcli` |
| Change load | `mvn clean verify -Papi -Dperf.threads=20 -Dperf.rampup=10 -Dperf.duration=120` |
| Other API host | `mvn clean verify -Papi -Dperf.apiHost=my.api.com -Dperf.apiProtocol=https` |
| Watch the browser | `mvn clean verify -Pfunctional -Dperf.pw.headless=false` |
| Do not fail build on errors | `-Dperf.ignoreFailures=true` |

`mvn clean verify` first runs `PayloadValidationTest`, then the JMeter plans, then checks the results.

## Reports
- **CSV**: `Results/csv/` (raw samples, one file per test plan)
- **HTML dashboard**: `Results/html/` (open `index.html` in the generated sub-folder)
- **Screenshots** of failed Playwright steps: `Results/screenshots/`

## Payloads
Put files under `Payloads/csv`, `json` or `xml`. The shared `payload_handler.groovy`:
1. reads the file named in the JSR223 PreProcessor parameter (for example `json/create_user.json`),
2. replaces `${variable}` placeholders with JMeter variables (for example CSV columns),
   escaping values for JSON or XML,
3. checks the result is well-formed JSON or XML,
4. sets `payload_body` and `payload_content_type`, which the HTTP sampler uses.

To add an API call: copy a sampler in `api_payload_test.jmx`, add your file to `Payloads/`,
and change the PreProcessor parameter to the new path.
CSV files are also used as data sources (CSV Data Set Config) for API and CLI runs.

## Adding tests
Any `*.jmx` placed under `TestScripts/api`, `functional` or `cli` is picked up by the matching profile.
For a new type, add a folder and a profile in `pom.xml` with `<perf.tests>yourfolder/**/*.jmx</perf.tests>`.

## Notes
- The default API target is `httpbin.org`, which echoes requests back; point `perf.apiHost` at your own service.
- Keep Playwright thread counts low: each virtual user starts its own browser.
- CLI test uses `java` commands so it runs on any OS. Edit `Payloads/csv/cli_commands.csv`
  (and set `-Dcli.loops=<row count>` in the test plan if you add rows).
