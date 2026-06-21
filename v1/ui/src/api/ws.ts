// Cliente WebSocket del hub con RECONEXIÓN automática (backoff acotado). El
// servidor es push-only: emite WsEnvelope; el cliente sólo escucha.

import type { WsEnvelope } from "./types";
import { wsUrl } from "./client";

type Listener = (envelope: WsEnvelope) => void;

export class WsClient {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private closed = false;
  private retryMs = 500;
  private readonly maxRetryMs = 8000;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  connect(): void {
    this.closed = false;
    this.open();
  }

  private open(): void {
    if (this.closed) return;
    const ws = new WebSocket(wsUrl());
    this.ws = ws;
    ws.onopen = () => {
      this.retryMs = 500; // reset del backoff al reconectar con éxito
    };
    ws.onmessage = (ev: MessageEvent<string>) => {
      try {
        const env = JSON.parse(ev.data) as WsEnvelope;
        this.listeners.forEach((fn) => fn(env));
      } catch {
        // mensaje malformado: se ignora
      }
    };
    ws.onclose = () => {
      this.ws = null;
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      ws.close();
    };
  }

  private scheduleReconnect(): void {
    if (this.closed) return;
    if (this.reconnectTimer !== null) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, this.retryMs);
    this.retryMs = Math.min(this.retryMs * 2, this.maxRetryMs);
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  close(): void {
    this.closed = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }
}
