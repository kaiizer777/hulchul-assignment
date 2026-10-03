import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'url';
import path from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
    // The first test in a file pays the one-time Vite transform of the route
    // module it dynamically imports inside the test body, so its budget is
    // transform + CPU contention, not application latency. With six files in
    // parallel that has been measured at 630-840ms on an idle machine and past
    // the 5s default on a loaded one. Raising the ceiling gives the transform
    // headroom; it is not hiding a slow code path, since the same tests stay
    // well under a second once the module cache is warm.
    testTimeout: 20000,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});
