import React, { useEffect, useRef } from "react";

export type CoreMode = "ring" | "cube" | "face";

interface HologramProps {
  mode: CoreMode;
  assistantState: string;
  audioLevel: number; // 0..1 real mic / audio level
}

function stateColor(st: string): string {
  const s = st.toLowerCase();
  if (s.includes("error")) return "#fb7185";
  if (s.includes("approval")) return "#fbbf24";
  if (s.includes("listening") || s.includes("speech") || s.includes("speaking")) return "#22d3ee";
  if (s.includes("thinking") || s.includes("planning") || s.includes("delegating") || s.includes("executing")) return "#60a5fa";
  return "#38bdf8";
}

function stateIntensity(st: string): number {
  const s = st.toLowerCase();
  if (s === "idle") return 0.16;
  if (s.includes("listening") || s.includes("completed")) return 0.45;
  if (s.includes("speech") || s.includes("speaking") || s.includes("executing")) return 0.92;
  if (s.includes("thinking") || s.includes("planning") || s.includes("transcribing") || s.includes("understanding")) return 0.72;
  if (s.includes("approval")) return 0.6;
  if (s.includes("error")) return 0.85;
  return 0.35;
}

/** Real-time holographic Jarvis Core canvas supporting Ring, Cube (3D wireframe hypercube), and cybernetic Face modes.
 *  Driven exclusively by real backend assistant state and real audio level telemetry. */
export const HologramCore: React.FC<HologramProps> = ({ mode, assistantState, audioLevel }) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const telemetry = useRef({ mode, assistantState, audioLevel });
  telemetry.current = { mode, assistantState, audioLevel };

  useEffect(() => {
    const cvs = canvasRef.current;
    if (!cvs) return;
    const ctx = cvs.getContext("2d");
    if (!ctx) return;
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let raf = 0;
    let phase = 0;
    let lastTs = performance.now();

    const draw = (now: number) => {
      const dt = Math.min(0.05, (now - lastTs) / 1000);
      lastTs = now;
      const { mode: curMode, assistantState: curState, audioLevel: curLevel } = telemetry.current;
      const color = stateColor(curState);
      const baseEnergy = stateIntensity(curState);
      const energy = Math.min(1, baseEnergy + Math.max(0, Math.min(1, curLevel)) * 0.75);
      if (!calm) phase += dt * (0.45 + energy * 2.1);

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = cvs.getBoundingClientRect();
      const w = Math.max(180, Math.floor(rect.width));
      const h = Math.max(180, Math.floor(rect.height));
      if (cvs.width !== w * dpr || cvs.height !== h * dpr) {
        cvs.width = w * dpr;
        cvs.height = h * dpr;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const cx = w / 2;
      const cy = h / 2;
      const r = Math.min(w, h) * 0.38;

      // Ambient holographic halo driven by real energy
      const halo = ctx.createRadialGradient(cx, cy, 4, cx, cy, r * 1.35);
      halo.addColorStop(0, `${color}38`);
      halo.addColorStop(0.55, `${color}12`);
      halo.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(cx, cy, r * 1.35, 0, Math.PI * 2);
      ctx.fill();

      if (curMode === "ring") {
        // Outer segmented gyro ring
        ctx.strokeStyle = color;
        for (let ring = 0; ring < 3; ring++) {
          const rad = r * (1 - ring * 0.18);
          const dir = ring % 2 === 0 ? 1 : -1;
          const rot = phase * dir * (0.6 + ring * 0.35);
          ctx.lineWidth = ring === 1 ? 2 : 1.25;
          ctx.globalAlpha = 0.38 + energy * 0.45;
          const segs = 4 + ring * 2;
          for (let s = 0; s < segs; s++) {
            const a0 = rot + (s * Math.PI * 2) / segs;
            ctx.beginPath();
            ctx.arc(cx, cy, rad, a0, a0 + (Math.PI * 1.35) / segs);
            ctx.stroke();
          }
        }
        // Harmonic radial ticks reactive to real audio level & speech cadence
        const ticks = 36;
        ctx.globalAlpha = 0.65;
        ctx.lineWidth = 1.4;
        for (let i = 0; i < ticks; i++) {
          const ang = (i / ticks) * Math.PI * 2 - phase * 0.25;
          const mod = Math.sin(i * 0.8 + phase * 3.5) * 0.5 + 0.5;
          const amp = curState === "Idle" ? 2 : 4 + mod * energy * 15;
          ctx.beginPath();
          ctx.moveTo(cx + Math.cos(ang) * (r * 0.52), cy + Math.sin(ang) * (r * 0.52));
          ctx.lineTo(cx + Math.cos(ang) * (r * 0.52 + amp), cy + Math.sin(ang) * (r * 0.52 + amp));
          ctx.stroke();
        }
        // Core pulse sphere
        ctx.globalAlpha = 0.85;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(cx, cy, r * (0.14 + energy * 0.06), 0, Math.PI * 2);
        ctx.fill();
      } else if (curMode === "cube") {
        // 3D wireframe outer + inner rotating hypercube projection
        const proj = (scale: number, rotOffset: number) => {
          const ax = phase * 0.7 + rotOffset;
          const ay = phase * 1.05 + rotOffset * 0.5;
          const ca = Math.cos(ax), sa = Math.sin(ax);
          const cb = Math.cos(ay), sb = Math.sin(ay);
          const pts: Array<[number, number, number]> = [];
          for (const x of [-1, 1]) {
            for (const y of [-1, 1]) {
              for (const z of [-1, 1]) {
                const x1 = x * cb - z * sb;
                const z1 = x * sb + z * cb;
                const y2 = y * ca - z1 * sa;
                const z2 = y * sa + z1 * ca;
                const p = 3.2 / (3.2 - z2 * 0.6);
                pts.push([cx + x1 * scale * p, cy + y2 * scale * p, z2]);
              }
            }
          }
          return pts;
        };
        const edges = [
          [0, 1], [0, 2], [0, 4], [1, 3], [1, 5], [2, 3],
          [2, 6], [3, 7], [4, 5], [4, 6], [5, 7], [6, 7],
        ];
        const outer = proj(r * 0.56, 0);
        const inner = proj(r * (0.26 + energy * 0.08), 0.65);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.4;
        ctx.globalAlpha = 0.45 + energy * 0.4;
        ctx.beginPath();
        for (const [a, b] of edges) {
          ctx.moveTo(outer[a][0], outer[a][1]);
          ctx.lineTo(outer[b][0], outer[b][1]);
          ctx.moveTo(inner[a][0], inner[a][1]);
          ctx.lineTo(inner[b][0], inner[b][1]);
        }
        ctx.stroke();
        ctx.lineWidth = 0.8;
        ctx.globalAlpha = 0.28 + energy * 0.25;
        ctx.beginPath();
        for (let i = 0; i < 8; i++) {
          ctx.moveTo(outer[i][0], outer[i][1]);
          ctx.lineTo(inner[i][0], inner[i][1]);
        }
        ctx.stroke();
        ctx.fillStyle = "#e0f2fe";
        ctx.globalAlpha = 0.9;
        for (const p of [...outer, ...inner]) {
          ctx.beginPath();
          ctx.arc(p[0], p[1], 2.2, 0, Math.PI * 2);
          ctx.fill();
        }
      } else {
        // Cybernetic geometric wireframe face lattice + reactive vocal aperture
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1.4;
        ctx.globalAlpha = 0.55 + energy * 0.38;
        // Cranial hex-contour polygon
        const poly: Array<[number, number]> = [
          [0, -0.95], [0.58, -0.68], [0.76, -0.05], [0.55, 0.58],
          [0.24, 0.92], [-0.24, 0.92], [-0.55, 0.58], [-0.76, -0.05], [-0.58, -0.68],
        ];
        ctx.beginPath();
        poly.forEach(([px, py], i) => {
          const x = cx + px * r;
          const y = cy + py * r;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.closePath();
        ctx.stroke();
        // Cheek & temple neural struts
        ctx.globalAlpha = 0.26 + energy * 0.25;
        ctx.beginPath();
        for (const s of [-1, 1]) {
          ctx.moveTo(cx + s * 0.58 * r, cy - 0.68 * r);
          ctx.lineTo(cx + s * 0.34 * r, cy - 0.14 * r);
          ctx.lineTo(cx + s * 0.76 * r, cy - 0.05 * r);
          ctx.moveTo(cx + s * 0.34 * r, cy - 0.14 * r);
          ctx.lineTo(cx + s * 0.22 * r, cy + 0.35 * r);
          ctx.lineTo(cx + s * 0.55 * r, cy + 0.58 * r);
        }
        ctx.stroke();
        // Optic sensors
        ctx.globalAlpha = 0.9;
        for (const s of [-1, 1]) {
          const ex = cx + s * 0.28 * r;
          const ey = cy - 0.18 * r;
          ctx.beginPath();
          ctx.moveTo(ex - s * 10, ey - 2);
          ctx.lineTo(ex + s * 12, ey);
          ctx.lineTo(ex - s * 4, ey + 4);
          ctx.closePath();
          ctx.fill();
        }
        // Reactive vocal harmonic aperture
        const activeMouth = curState === "Speaking" || curState === "Speech detected" || curLevel > 0.04;
        const bars = 7;
        for (let i = 0; i < bars; i++) {
          const bx = cx + (i - (bars - 1) / 2) * 7;
          const hBar = activeMouth ? 3 + Math.abs(Math.sin(phase * 5 + i * 0.7)) * (6 + energy * 14) : 2;
          ctx.fillRect(bx - 1.5, cy + 0.42 * r - hBar / 2, 3, hBar);
        }
      }

      if (!calm) raf = requestAnimationFrame(draw);
    };

    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="hologram-canvas"
      aria-label={`Jarvis holographic core in ${mode} mode, state: ${assistantState}`}
    />
  );
};

