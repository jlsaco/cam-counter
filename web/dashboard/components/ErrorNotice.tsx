/**
 * Aviso de error reutilizable (Server o Client). Muestra el mensaje ya saneado por el llamante
 * (p.ej. `ApiError.message`), nunca trazas crudas.
 */
export function ErrorNotice({ message }: { message: string }) {
  return (
    <div className="rounded border border-red-300 bg-red-50 p-4 text-sm text-red-800">
      <p className="font-medium">No se pudo cargar la información.</p>
      <p className="mt-1 break-words">{message}</p>
    </div>
  );
}
