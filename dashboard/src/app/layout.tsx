import type { Metadata } from "next";
import { TRPCProvider } from "@/trpc/provider";
import { AppShell } from "@/components/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: "fondaco",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <TRPCProvider>
          <AppShell>{children}</AppShell>
        </TRPCProvider>
      </body>
    </html>
  );
}
