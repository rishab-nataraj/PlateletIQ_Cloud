import React from 'react'
import ReactDOM from 'react-dom/client'
import * as Sentry from '@sentry/react'
import App from './App'
import './index.css'

// Sentry: browser errors with stack traces, tagged with the Vercel release.
// Off unless VITE_SENTRY_DSN is set at build time.
if (import.meta.env.VITE_SENTRY_DSN) {
  Sentry.init({
    dsn: import.meta.env.VITE_SENTRY_DSN,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT ?? import.meta.env.VITE_VERCEL_ENV ?? import.meta.env.MODE,
    release: import.meta.env.VITE_VERCEL_GIT_COMMIT_SHA,
    integrations: [Sentry.browserTracingIntegration()],
    tracesSampleRate: 0.2,
    // Only trace calls to our own API, never third-party hosts.
    tracePropagationTargets: [/\/api\/v1\//],
  })
}

function Crashed() {
  return (
    <div style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center', background: '#F5F5F7' }}>
      <div style={{ textAlign: 'center', maxWidth: 420 }}>
        <p style={{ fontSize: 17, fontWeight: 600, color: '#1D1D1F' }}>Something went wrong in the console.</p>
        <p style={{ fontSize: 13, color: '#6E6E73', marginTop: 6 }}>
          The error has been reported. Reload the page to continue.
        </p>
        <button
          onClick={() => window.location.reload()}
          style={{ marginTop: 14, padding: '9px 18px', borderRadius: 999, background: '#0071E3', color: 'white', fontSize: 14, border: 0, cursor: 'pointer' }}
        >
          Reload
        </button>
      </div>
    </div>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Sentry.ErrorBoundary fallback={<Crashed />}>
      <App />
    </Sentry.ErrorBoundary>
  </React.StrictMode>,
)
