import type { Metadata } from "next";
import { ClerkProvider } from "@clerk/nextjs";
import "./globals.css";

// No next/font import: the design uses Apple's SF system-font stack
// (defined in globals.css as --font-sans), which resolves to San Francisco
// on Apple devices with Helvetica Neue / Arial fallbacks. There's no
// webfont to load, so nothing is fetched from Google Fonts.

export const metadata: Metadata = {
  title: "Market Research Intelligence Assistant",
  description:
    "Collect, analyze, and summarize competitive market intelligence from public sources, with every claim verified against its original source.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <ClerkProvider afterSignOutUrl="/">
      <html lang="en" suppressHydrationWarning>
        {/* suppressHydrationWarning: browser extensions (Grammarly, etc.)
            inject data-* attributes on <html>/<body> before React hydrates,
            which is a benign server/client mismatch on these root elements. */}
        <body className="antialiased" suppressHydrationWarning>{children}</body>
      </html>
    </ClerkProvider>
  );
}
