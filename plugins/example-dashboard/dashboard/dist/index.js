(function () {
  "use strict";
  // example-dashboard plugin — exists for auth coverage (see tests). The
  // backend test client hits /api/plugins/example/hello; this front-end stub
  // exists so dashboard pages do not 404 on dist/index.js during plugin load.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!SDK || !registry || typeof registry.register !== "function") return;
  const React = SDK.React;
  function ExamplePage() {
    return React.createElement(
      "div",
      { className: "p-4 text-sm" },
      "example-dashboard — stub component (auth test fixture)."
    );
  }
  registry.register("example", ExamplePage);
})();
