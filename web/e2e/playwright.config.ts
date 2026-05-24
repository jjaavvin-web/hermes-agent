import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  // Run tests serially: the pulse spec opens an SSE connection per page,
  // and with parallel workers the dashboard's per-IP connection budget
  // (uvicorn default) can drop the slowest request. Constellation canvas
  // mount can race the first /api/pulse/graph payload in parallel mode.
  // Serial keeps things deterministic with negligible wall-clock cost
  // (~12 s for the full spec).
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:9119',
  },
});
