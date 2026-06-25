import type { Metadata } from "next";
import Link from "next/link";

import { SignOutButton } from "@/components/SignOutButton";

import { Providers } from "./providers";
import "./globals.css";
import "@aws-amplify/ui-react/styles.css";

export const metadata: Metadata = {
  title: "cam-counter · consola de flota",
  description:
    "Consola READ-ONLY de la flota de Pis cam-counter: dispositivos, eventos de cruce y clips.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="es">
      <body>
        <Providers>
          <div className="min-h-screen">
            <header className="border-b border-gray-200 bg-white">
              <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
                <Link href="/fleet" className="text-lg font-semibold text-gray-900">
                  cam-counter <span className="text-gray-400">· flota</span>
                </Link>
                <nav className="flex items-center gap-4 text-sm">
                  <Link href="/fleet" className="text-gray-600 hover:text-gray-900">
                    Flota
                  </Link>
                  <SignOutButton />
                </nav>
              </div>
            </header>
            <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
