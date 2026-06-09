const { execSync } = require('child_process');

const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
const resultDir = `results/${timestamp}`;
const extraArgs = process.argv.slice(2).join(' ');
const tagArgs = extraArgs ? ` ${extraArgs}` : '';

process.env.RESULT_DIR = resultDir;

let testExitCode = 0;

try {
  // Module loading (ts-node), support-code discovery (hooks/steps/pageObjects/configReader),
  // feature paths, the per-step timeout, and the progress-bar format all come from the
  // auto-detected cucumber.js config. Here we only add what is run-specific: the timestamped
  // JSON report path (consumed by reports.ts via RESULT_DIR) and any tag filters the caller
  // passed through (e.g. `npm test -- --tags "@smoke"`).
  execSync(`npx cucumber-js --format json:${resultDir}/cucumber_report.json${tagArgs}`, { stdio: 'inherit' });
} catch (error) {
  testExitCode = 1;
}

try {
  execSync('npx ts-node test/utils/reports.ts', { stdio: 'inherit' });
} catch (reportError) {
  console.error('Failed to generate HTML report:', reportError.message);
}

process.exit(testExitCode);