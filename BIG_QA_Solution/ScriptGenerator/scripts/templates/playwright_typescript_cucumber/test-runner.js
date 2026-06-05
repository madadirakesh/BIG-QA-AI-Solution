const { execSync } = require('child_process');

const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
const resultDir = `results/${timestamp}`;
const extraArgs = process.argv.slice(2).join(' ');
const tagArgs = extraArgs ? ` ${extraArgs}` : '';

process.env.RESULT_DIR = resultDir;

let testExitCode = 0;

try {
  execSync(`npx cucumber-js "test/features/**/*.feature" --require-module ts-node/register --require "test/hooks/**/*.ts" --require "test/stepDefinitions/**/*.ts" --require "test/pageObjects/**/*.ts" --require "test/utils/configReader.ts" --format json:${resultDir}/cucumber_report.json${tagArgs}`, { stdio: 'inherit' });
} catch (error) {
  testExitCode = 1;
}

try {
  execSync('npx ts-node test/utils/reports.ts', { stdio: 'inherit' });
} catch (reportError) {
  console.error('Failed to generate HTML report:', reportError.message);
}

process.exit(testExitCode);