import Link from "next/link";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center gap-10 px-6 py-20">
      <div className="flex flex-col gap-5">
        <h1 className="text-5xl font-semibold tracking-tight sm:text-6xl">
          Market Research
          <br />
          Intelligence Assistant
        </h1>
        <p className="max-w-xl text-xl leading-relaxed text-muted">
          Provide competitors, topics, and source URLs. The system fetches and
          analyzes each source, groups findings into themes, and verifies every
          claim against the original source content — flagging anything it can’t
          ground.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-4">
        <Link
          href="/research"
          className="rounded-full bg-accent px-6 py-3 text-base font-medium text-white transition-colors hover:bg-accent-hover"
        >
          Start a research run
        </Link>
        <Link
          href="/dashboard"
          className="text-base font-medium text-accent transition-opacity hover:opacity-70"
        >
          View past runs <span aria-hidden>›</span>
        </Link>
      </div>

      <p className="text-sm text-muted">
        Sign-in required. The research and dashboard routes are protected.
      </p>
    </main>
  );
}
