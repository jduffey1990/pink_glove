import { fileURLToPath, URL } from 'node:url'
import Vue from '@vitejs/plugin-vue'
import Fonts from 'unplugin-fonts/vite'
import Vuetify, { transformAssetUrls } from 'vite-plugin-vuetify'
import { defineConfig } from 'vitest/config'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    Vue({
      template: { transformAssetUrls },
    }),
    // https://github.com/vuetifyjs/vuetify-loader/tree/master/packages/vite-plugin#readme
    Vuetify({
      autoImport: true,
      styles: {
        configFile: 'src/styles/settings.scss',
      },
    }),
    Fonts({
      fontsource: {
        families: [
          {
            name: 'Roboto',
            weights: [100, 300, 400, 500, 700, 900],
            styles: ['normal', 'italic'],
          },
        ],
      },
    }),
  ],
  define: { 'process.env': {} },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('src', import.meta.url)),
    },
    extensions: [
      '.js',
      '.json',
      '.jsx',
      '.mjs',
      '.ts',
      '.tsx',
      '.vue',
    ],
  },
  server: {
    // Matches CORS_ALLOWED_ORIGINS and FRONTEND_BASE_URL in app/.env.example.
    // Changing it means changing those too, or credentialed requests fail CORS.
    port: 3000,
    // Fail rather than drift to 3001. Vite's default is to hunt for the next
    // free port, which hands you a dev server that looks fine and that the
    // backend refuses on every call -- the browser only says "blocked by CORS
    // policy", which reads as a backend fault. Usually this means the compose
    // `ui` service already holds 3000: use that one, or stop it first.
    strictPort: true,
    // Bound to all interfaces so the container's dev server is reachable from
    // the host. Harmless locally; the dev server is never deployed.
    host: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.spec.ts'],
  },
})
