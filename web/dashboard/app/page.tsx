import { redirect } from "next/navigation";

/** Raíz → flota. */
export default function HomePage() {
  redirect("/fleet");
}
