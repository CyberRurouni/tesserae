/** @type {import('next').Config */
const nextConfig = {
  reactStrictMode: true,
  // NOTE: the app's own route handlers under app/api/* are the single source of
  // truth. Do not add a catch-all /api rewrite here — it would silently bypass
  // every handler and proxy straight to the Python backend instead.
  env: {
    PYTHON_BACKEND_URL: process.env.PYTHON_BACKEND_URL || 'http://localhost:8000',
  },
};

module.exports = nextConfig;