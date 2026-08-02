import { SignIn } from "@clerk/nextjs";

// Google-only is configured in the Clerk dashboard (Social connections →
// Google, everything else disabled), so <SignIn/> renders a single
// "Continue with Google" button — no form, no password.
export default function SignInPage() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-8 px-6 py-16">
      <div className="text-center">
        <h1 className="text-2xl font-semibold tracking-tight">Market Research Intelligence</h1>
        <p className="mt-2 text-sm text-muted">Sign in to run and review research.</p>
      </div>
      <SignIn />
    </main>
  );
}
