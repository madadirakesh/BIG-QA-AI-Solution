// Cucumber.js configuration — SINGLE SOURCE OF TRUTH for how the suite is loaded and run.
//
// Filename matters: cucumber-js only auto-detects a config file named cucumber.js / .cjs / .mjs /
// .json / .yaml. The previous file was named cucumber.config.js, which cucumber-js never loads, so
// its settings (including the timeout) were silently ignored. Renaming it to cucumber.js means the
// runner — and a bare `npx cucumber-js` — both pick this up automatically with no --config flag.
module.exports = {
  default: {
    // Compile the TypeScript support/step files on the fly so we can run .ts sources directly,
    // with no separate `tsc` build step.
    requireModule: ["ts-node/register"],

    // Support code, in load order: hooks (Before/After + setDefaultTimeout), step definitions,
    // page objects, and the .env config reader. Globs that currently match nothing — e.g.
    // test/stepDefinitions and test/pageObjects before the wizard generates sample code — are
    // simply ignored by cucumber-js, so this stays valid for a freshly scaffolded project.
    require: [
      "test/hooks/**/*.ts",
      "test/stepDefinitions/**/*.ts",
      "test/pageObjects/**/*.ts",
      "test/utils/configReader.ts",
    ],

    // Feature files to execute.
    paths: ["test/features/**/*.feature"],

    formatOptions: {
      snippetInterface: "async-await",
    },

    // Console progress only. The machine-readable JSON report path is timestamped per run
    // (results/<timestamp>/cucumber_report.json), so test-runner.js appends that --format on the
    // CLI rather than hard-coding a fixed path here.
    format: ["progress-bar"],

    parallel: 1,

    // NOTE: the per-step/hook timeout is intentionally NOT set here. It lives in
    // test/hooks/hooks.ts via setDefaultTimeout(60 * 1000), which is applied while the support
    // code loads and therefore takes effect no matter how cucumber-js is launched.
  },
};
