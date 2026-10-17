'use client';

/**
 * The last line of defence, for a crash in the root layout itself.
 *
 * `app/error.js` catches anything below it, but nothing catches a failure in the
 * layout — which is why this file has to render `<html>` and `<body>` itself and
 * cannot import the app's own chrome: a component that threw while rendering is
 * exactly what we cannot rely on here.
 *
 * Deliberately styled with plain inline values rather than theme classes. If the
 * error is in the stylesheet or the layout, those classes may be the thing that
 * is missing, and an unreadable error page is no better than a blank one.
 *
 * Same `data-page-error` hook as app/error.js, so the smoke check sees it.
 */

export default function GlobalError({ error, reset }) {
  return (
    <html lang="en" className="dark">
      <body
        data-page-error="true"
        style={{
          margin: 0,
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#0a0a0b',
          color: '#fafafa',
          fontFamily: 'ui-sans-serif, system-ui, sans-serif',
          padding: '1.5rem',
        }}
      >
        <div style={{ maxWidth: '40rem' }}>
          <h1 style={{ fontSize: '1.05rem', fontWeight: 600, margin: 0 }}>
            Tesserae could not start
          </h1>
          <p style={{ color: '#a1a1aa', fontSize: '0.875rem', marginTop: '0.5rem' }}>
            The page shell itself failed, so this is as bare as it gets. Nothing was
            saved or lost.
          </p>
          <pre
            data-page-error-message="true"
            style={{
              marginTop: '1rem',
              maxHeight: '12rem',
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              background: '#18181b',
              border: '1px solid #27272a',
              borderRadius: '0.5rem',
              padding: '0.75rem',
              fontSize: '0.75rem',
              fontFamily: 'ui-monospace, monospace',
            }}
          >
            {error?.message || 'No error message was provided.'}
          </pre>
          <button
            onClick={reset}
            style={{
              marginTop: '1rem',
              background: '#7c3aed',
              color: '#fff',
              border: 0,
              borderRadius: '0.5rem',
              padding: '0.5rem 1rem',
              fontSize: '0.875rem',
              cursor: 'pointer',
            }}
          >
            Try again
          </button>
        </div>
      </body>
    </html>
  );
}