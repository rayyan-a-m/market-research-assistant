import { SignIn } from "@clerk/nextjs";

// The sign-in methods are configured in the Clerk dashboard — <SignIn/>
// renders whatever is enabled (Google, and/or email + password), so adding a
// method is a dashboard toggle, not a code change here.
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
