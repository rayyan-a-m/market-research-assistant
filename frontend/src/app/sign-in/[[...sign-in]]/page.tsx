import { SignIn } from "@clerk/nextjs";

// Sign-in methods live in the Clerk dashboard, not here: <SignIn/> renders
// whatever is enabled, currently Google OAuth and a one-time email code. No
// password is stored anywhere, and adding a method later is a dashboard
// toggle rather than a change to this file.
//
// Reaching this page instead of Clerk's hosted one requires
// NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in in the environment (see
// .env.local.example and deployment.md §6).
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
