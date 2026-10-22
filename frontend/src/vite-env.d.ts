/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Production API, e.g. https://plateletiq-api.onrender.com/api/v1 */
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_API_PORT?: string;
  /** Sentry browser SDK. Public by design (a DSN only allows sending events). */
  readonly VITE_SENTRY_DSN?: string;
  readonly VITE_SENTRY_ENVIRONMENT?: string;
  /** Exposed automatically by Vercel for Vite projects. */
  readonly VITE_VERCEL_ENV?: string;
  readonly VITE_VERCEL_GIT_COMMIT_SHA?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
