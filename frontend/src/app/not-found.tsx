import Link from 'next/link';

export default function NotFound() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center">
      <h2 className="text-2xl font-bold tracking-tight text-zinc-900">Page Not Found</h2>
      <p className="mt-2 text-sm text-zinc-600">The requested resource could not be found.</p>
      <Link
        href="/invoices"
        className="mt-4 rounded-xl bg-zinc-900 px-4 py-2 text-xs font-semibold text-white shadow-xs hover:bg-zinc-800"
      >
        Return to Invoices
      </Link>
    </div>
  );
}
