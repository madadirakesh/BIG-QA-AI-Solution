const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
const resultsRoot = path.join(__dirname, 'Results');
const resultDir = path.join(resultsRoot, timestamp);
const extraArgs = process.argv.slice(2);
const reportJsonPath = path.join(resultDir, 'cucumber_report.json');
const localBin = (name) => path.join(
  __dirname,
  'node_modules',
  '.bin',
  process.platform === 'win32' ? `${name}.cmd` : name
);

process.env.RESULT_DIR = resultDir;
process.env.RESULTS_ROOT = resultsRoot;
fs.mkdirSync(resultDir, { recursive: true });

let testExitCode = 0;

try {
  // Module loading (ts-node), support-code discovery (hooks/steps/pageObjects/configReader),
  // feature paths, the per-step timeout, and the progress-bar format all come from the
  // auto-detected cucumber.js config. Here we only add what is run-specific: the timestamped
  // JSON report path (consumed by reports.ts via RESULT_DIR) and any tag filters the caller
  // passed through (e.g. `npm test -- --tags "@smoke"`).
  execFileSync(localBin('cucumber-js'), ['--format', `json:${reportJsonPath}`, ...extraArgs], { stdio: 'inherit' });
} catch (error) {
  testExitCode = 1;
}

try {
  execFileSync(localBin('ts-node'), ['test/utils/reports.ts'], { stdio: 'inherit' });
} catch (reportError) {
  console.error('Failed to generate HTML report:', reportError.message);
}

process.exit(testExitCode);
