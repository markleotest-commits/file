// Vercel Edge Middleware — HTTP Basic Auth for the whole site.
//
// No dependencies, Web-standard APIs only (no next/server import, so this
// works on a pure-static project). The password comes from the SITE_PASSWORD
// env var, which is NEVER committed anywhere — it lives only in the Vercel
// dashboard / `vercel env`.
//
// Fail CLOSED: SITE_PASSWORD unset or empty = 401 for everyone (fix by
// setting the env var and redeploying, see README_DEPLOY.md).
//
// How a browser login works here: the first visit gets 401 +
// WWW-Authenticate, the browser shows its login dialog, and resends every
// later request with an Authorization header. Any username works — only the
// password is checked (colons inside the password are fine).

export const config = {
  matcher: '/:path*',
};

function unauthorized() {
  return new Response('Authentication required.', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="Misroute Checker", charset="UTF-8"',
      'Cache-Control': 'no-store',
    },
  });
}

export default function middleware(request) {
  let pwd = '';
  try {
    pwd =
      (typeof process !== 'undefined' &&
        process.env &&
        process.env.SITE_PASSWORD) ||
      '';
  } catch (e) {
    pwd = '';
  }
  if (!pwd) return unauthorized();

  const auth = request.headers.get('authorization') || '';
  if (!auth.startsWith('Basic ')) return unauthorized();

  let decoded = '';
  try {
    decoded = atob(auth.slice(6).trim());
  } catch (e) {
    return unauthorized();
  }
  const idx = decoded.indexOf(':');
  const given = idx >= 0 ? decoded.slice(idx + 1) : '';
  if (!given || given !== pwd) return unauthorized();

  // No response returned = the request continues to the static file.
  return undefined;
}
