import type { Metadata } from "next";

import { SignOutButton } from "@/components/SignOutButton";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "cam-counter · consola de flota",
  description: "Consola SOLO LECTURA de la flota cam-counter (devices, eventos de cruce, clips).",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body>
        <Providers>
          <header className="app-header">
            <span className="brand">cam-counter · flota</span>
            <nav>
              <a href="/fleet">Flota</a>
            </nav>
            <span className="spacer" />
            <SignOutButton />
          </header>
          <main className="container">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
