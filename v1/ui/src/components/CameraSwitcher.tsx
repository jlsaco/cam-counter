import type { Camera } from "../api/types";

interface Props {
  cameras: Camera[];
  selected: string | null;
  onSelect: (cameraId: string) => void;
}

/** Selector de cámara (multi-cámara por Pi). */
export function CameraSwitcher({ cameras, selected, onSelect }: Props) {
  if (cameras.length === 0) {
    return <p className="text-sm text-zinc-400">Sin cámaras configuradas.</p>;
  }
  return (
    <div className="flex flex-wrap gap-2">
      {cameras.map((cam) => {
        const active = cam.camera_id === selected;
        return (
          <button
            key={cam.camera_id}
            type="button"
            onClick={() => onSelect(cam.camera_id)}
            className={`rounded px-3 py-1.5 text-sm ${
              active
                ? "bg-amber-500 text-black"
                : "bg-zinc-800 text-zinc-200 hover:bg-zinc-700"
            }`}
          >
            <span
              className={`mr-1.5 inline-block h-2 w-2 rounded-full ${
                cam.online ? "bg-emerald-400" : "bg-zinc-500"
              }`}
            />
            {cam.camera_id}
          </button>
        );
      })}
    </div>
  );
}
