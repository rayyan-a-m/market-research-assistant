import { SignUp } from "@clerk/nextjs";

// Which methods appear here is configured in the Clerk dashboard, same as the
// sign-in page: Google OAuth, or an email address verified by a one-time code.
export default function SignUpPage() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-8 px-6 py-16">
      <div className="text-center">
        <h1 className="text-2xl font-semibold tracking-tight">Market Research Intelligence</h1>
        <p className="mt-2 text-sm text-muted">Create an account to run and review research.</p>
      </div>
      <SignUp />
    </main>
  );
}
