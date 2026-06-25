import { redirect } from "next/navigation";

/** Raíz: la consola arranca en la lista de flota (la auth se resuelve allí). */
export default function HomePage() {
  redirect("/fleet");
}
