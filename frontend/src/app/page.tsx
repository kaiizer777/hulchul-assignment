import { redirect } from 'next/navigation';
import { headers } from 'next/headers';
import { verifySession } from '@/lib/auth';

export default async function Home() {
  const headerList = await headers();
  const req = new Request('https://hulchul-frontend.sufiyanx.workers.dev/', {
    headers: headerList,
  });
  const session = await verifySession(req);
  if (!session) {
    redirect('/login');
  }
  redirect('/agent');
}
