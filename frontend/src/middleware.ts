import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

// Research and dashboard require a signed-in user; everything else (the
// landing page, sign-in) is public.
const isProtected = createRouteMatcher(["/research(.*)", "/dashboard(.*)"]);

export default clerkMiddleware(async (auth, req) => {
  if (isProtected(req)) await auth.protect();
});

export const config = {
  matcher: [
    // Skip Next internals and static files, run on everything else
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpg|jpeg|gif|png|svg|ico|webp|woff2?)).*)",
    "/(api|trpc)(.*)",
  ],
};
