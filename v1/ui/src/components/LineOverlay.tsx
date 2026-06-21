import { type PointerEvent as ReactPointerEvent, useCallback, useRef, useState } from "react";
import type { LineGeom, Point2D } from "../api/types";

interface Props {
  line: LineGeom;
  width: number;
  height: number;
  /** Si es editable, los extremos se pueden arrastrar (drag client-side). */
  editable?: boolean;
  onChange?: (line: LineGeom) => void;
}

const HANDLE_R = 8;

function clamp01(v: number): number {
  return Math.min(1, Math.max(0, v));
}

/**
 * Overlay SVG de la línea-umbral. Las coords normalizadas 0..1 se mapean al
 * tamaño RENDERIZADO (`width`/`height` en px) del `<img>` del MJPEG. En modo
 * editable, arrastrar un extremo actualiza la línea SIN round-trip (a ~60fps);
 * el guardado lo decide `LineEditor`.
 */
export function LineOverlay({ line, width, height, editable, onChange }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [dragging, setDragging] = useState<"a" | "b" | null>(null);

  const toNorm = useCallback(
    (clientX: number, clientY: number): Point2D => {
      const rect = svgRef.current?.getBoundingClientRect();
      if (!rect || rect.width === 0 || rect.height === 0) return { x: 0, y: 0 };
      return {
        x: clamp01((clientX - rect.left) / rect.width),
        y: clamp01((clientY - rect.top) / rect.height),
      };
    },
    [],
  );

  const onPointerDown = (which: "a" | "b") => (e: ReactPointerEvent) => {
    if (!editable) return;
    e.preventDefault();
    (e.target as Element).setPointerCapture(e.pointerId);
    setDragging(which);
  };

  const onPointerMove = (e: ReactPointerEvent) => {
    if (!editable || !dragging || !onChange) return;
    const p = toNorm(e.clientX, e.clientY);
    onChange({ ...line, [dragging]: p });
  };

  const onPointerUp = (e: ReactPointerEvent) => {
    if (!dragging) return;
    (e.target as Element).releasePointerCapture?.(e.pointerId);
    setDragging(null);
  };

  const ax = line.a.x * width;
  const ay = line.a.y * height;
  const bx = line.b.x * width;
  const by = line.b.y * height;

  return (
    <svg
      ref={svgRef}
      width={width}
      height={height}
      className="absolute inset-0"
      style={{ touchAction: "none" }}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
    >
      <line
        x1={ax}
        y1={ay}
        x2={bx}
        y2={by}
        stroke="#ffc400"
        strokeWidth={3}
        strokeLinecap="round"
      />
      {editable && (
        <>
          <circle
            cx={ax}
            cy={ay}
            r={HANDLE_R}
            fill="#ffc400"
            stroke="#0c0e12"
            strokeWidth={2}
            style={{ cursor: "grab" }}
            onPointerDown={onPointerDown("a")}
            aria-label="endpoint-a"
          />
          <circle
            cx={bx}
            cy={by}
            r={HANDLE_R}
            fill="#ffc400"
            stroke="#0c0e12"
            strokeWidth={2}
            style={{ cursor: "grab" }}
            onPointerDown={onPointerDown("b")}
            aria-label="endpoint-b"
          />
        </>
      )}
    </svg>
  );
}
