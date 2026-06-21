import { type RefObject, useEffect, useRef, useState } from "react";

export interface Size {
  width: number;
  height: number;
}

/**
 * Observa el tamaño RENDERIZADO de un elemento (para mapear coords normalizadas
 * 0..1 al tamaño en píxeles del `<img>` del MJPEG). Devuelve la ref a colocar en
 * el elemento y su tamaño actual.
 */
export function useElementSize<T extends HTMLElement>(): [RefObject<T>, Size] {
  const ref = useRef<T>(null);
  const [size, setSize] = useState<Size>({ width: 0, height: 0 });

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      setSize({ width, height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return [ref, size];
}
