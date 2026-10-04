import { NextResponse, type NextRequest } from 'next/server';
import { verifySession, getSessionToken } from '@/lib/auth';

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Bypass static files and API routes
  if (
    pathname.startsWith('/_next') ||
    pathname.startsWith('/api') ||
    pathname === '/favicon.ico'
  ) {
    return NextResponse.next();
  }

  const token = getSessionToken(request);
  const session = token ? await verifySession(request) : null;
  const isAuthenticated = !!session;

  // /login route
  if (pathname === '/login') {
    if (isAuthenticated) {
      return NextResponse.redirect(new URL('/agent', request.url));
    }
    return NextResponse.next();
  }

  // Root / route
  if (pathname === '/') {
    if (isAuthenticated) {
      return NextResponse.redirect(new URL('/agent', request.url));
    }
    return NextResponse.redirect(new URL('/login', request.url));
  }

  // Protected app routes (/agent, /invoices, /purchase-orders, /vendors, etc.)
  if (!isAuthenticated) {
    const loginUrl = new URL('/login', request.url);
    if (pathname !== '/agent') {
      loginUrl.searchParams.set('returnTo', pathname + request.nextUrl.search);
    }
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    /*
     * Match all request paths except:
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon.ico (favicon file)
     * - api routes (/api/*)
     */
    '/((?!api|_next/static|_next/image|favicon.ico).*)',
  ],
};
