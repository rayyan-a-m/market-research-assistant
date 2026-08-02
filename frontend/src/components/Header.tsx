import Link from "next/link";
import { UserButton } from "@clerk/nextjs";

export function Header() {
  return (
    <header className="sticky top-0 z-10 border-b border-border bg-background/80 backdrop-blur">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-3">
        <Link href="/dashboard" className="text-sm font-semibold tracking-tight">
          Market Research Intelligence
        </Link>
        <nav className="flex items-center gap-6 text-sm">
          <Link href="/research" className="text-accent hover:opacity-70">
            New run
          </Link>
          <Link href="/dashboard" className="text-muted hover:text-foreground">
            History
          </Link>
          <UserButton />
        </nav>
      </div>
    </header>
  );
}
