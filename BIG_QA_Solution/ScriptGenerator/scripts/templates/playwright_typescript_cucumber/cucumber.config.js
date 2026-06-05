module.exports = {
  default: {
    formatOptions: {
      snippetInterface: "async-await"
    },
    paths: [
      "test/features/*.feature"
    ],
    dryRun: false,
    require: [
      "dist/test/**/*.js"  // Point this to the compiled JS, not the TS
    ],
    format: [
      "progress-bar",
      "json:results/cucumber_report.json"
    ],
    parallel: 1,
    timeout:30000
}
}