"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError, getClipUrl } from "@/lib/api-client";
import { getClientIdToken } from "@/lib/auth-client";

/**
 * Reproductor del CLIP MP4 de un evento.
 *
 * Pide la presigned URL ON-DEMAND a `GET /clips/url?key=clip_key` (clip_presign, WP11) y la
 * pone en un <video>. La URL caduca (TTL 300s): si el <video> falla al cargar (p.ej. URL
 * expirada o aún no subido), REFRESCAMOS la presigned URL una vez de forma automática. El
 * frontend NUNCA toca S3 directo: siempre pasa por la API firmadora.
 */
export function ClipPlayer({ clipKey }: { clipKey: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Evita un bucle de refresco infinito si el objeto realmente no existe / no reproduce.
  const [refreshed, setRefreshed] = useState(false);

  const fetchUrl = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const token = await getClientIdToken();
      const resp = await getClipUrl(token, clipKey);
      setUrl(resp.url);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "no se pudo obtener la URL del clip");
      setUrl(null);
    } finally {
      setLoading(false);
    }
  }, [clipKey]);

  useEffect(() => {
    void fetchUrl();
  }, [fetchUrl]);

  // Refresco automático (una vez) ante error de carga del <video>: la presigned URL pudo expirar.
  const onVideoError = useCallback(() => {
    if (refreshed) {
      setError("el clip no se pudo reproducir (¿aún sin subir o no disponible?)");
      return;
    }
    setRefreshed(true);
    void fetchUrl();
  }, [refreshed, fetchUrl]);

  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Clip del evento</h2>
        <span className="spacer" />
        <button className="btn" onClick={() => void fetchUrl()} disabled={loading}>
          {loading ? "Cargando…" : "Refrescar URL"}
        </button>
      </div>

      {error ? <div className="error">{error}</div> : null}

      {url ? (
        <div className="video-wrap" style={{ marginTop: 8 }}>
          {/* key=url fuerza recargar el <video> cuando refrescamos la presigned URL. */}
          <video key={url} src={url} controls playsInline preload="metadata" onError={onVideoError} />
        </div>
      ) : !error ? (
        <p className="muted">Obteniendo el clip…</p>
      ) : null}

      <p className="muted mono" style={{ marginTop: 8, wordBreak: "break-all" }}>
        {clipKey}
      </p>
    </div>
  );
}
