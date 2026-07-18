"use client";

import { useEffect, useRef } from "react";

type FieldPoint = {
  x: number;
  y: number;
  z: number;
  phase: number;
  drift: number;
  cluster: number;
  anchor: boolean;
};

type ProjectedPoint = FieldPoint & {
  sx: number;
  sy: number;
  depth: number;
  alpha: number;
};

const clusterCenters = [
  [-.62, -.26, -.42],
  [.08, -.38, .26],
  [.62, .12, -.08],
  [-.05, .42, .44],
] as const;

function seededRandom(seed: number) {
  let value = seed >>> 0;
  return () => {
    value += 0x6d2b79f5;
    let next = value;
    next = Math.imul(next ^ next >>> 15, next | 1);
    next ^= next + Math.imul(next ^ next >>> 7, next | 61);
    return ((next ^ next >>> 14) >>> 0) / 4294967296;
  };
}

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

export function HeroMemoryField() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const hero = canvas?.closest<HTMLElement>(".home-hero");
    const context = canvas?.getContext("2d");
    if (!canvas || !hero || !context) return;

    const random = seededRandom(7319);
    const points: FieldPoint[] = Array.from({ length: 78 }, (_, index) => {
      const cluster = index % clusterCenters.length;
      const center = clusterCenters[cluster];
      return {
        x: center[0] + (random() - .5) * .92,
        y: center[1] + (random() - .5) * .72,
        z: center[2] + (random() - .5) * .82,
        phase: random() * Math.PI * 2,
        drift: .025 + random() * .06,
        cluster,
        anchor: index % 19 === 0,
      };
    });

    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let width = 0;
    let height = 0;
    let pixelRatio = 1;
    let visible = false;
    let reduced = motion.matches;
    let frame = 0;
    let pointerX = 0;
    let pointerY = 0;
    let targetX = 0;
    let targetY = 0;
    let staticDrawNeeded = true;

    const resize = () => {
      const bounds = hero.getBoundingClientRect();
      width = Math.max(1, bounds.width);
      height = Math.max(1, bounds.height);
      pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * pixelRatio);
      canvas.height = Math.round(height * pixelRatio);
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      staticDrawNeeded = true;
    };

    const project = (point: FieldPoint, time: number): ProjectedPoint => {
      const yaw = time * .000055 + pointerX * .14;
      const pitch = -.12 + pointerY * .1;
      const animatedX = point.x + Math.sin(time * .00024 + point.phase) * point.drift;
      const animatedY = point.y + Math.cos(time * .0002 + point.phase * 1.3) * point.drift;
      const animatedZ = point.z + Math.sin(time * .00017 + point.phase * .7) * point.drift;
      const cosYaw = Math.cos(yaw);
      const sinYaw = Math.sin(yaw);
      const rotatedX = animatedX * cosYaw - animatedZ * sinYaw;
      const yawZ = animatedX * sinYaw + animatedZ * cosYaw;
      const cosPitch = Math.cos(pitch);
      const sinPitch = Math.sin(pitch);
      const rotatedY = animatedY * cosPitch - yawZ * sinPitch;
      const rotatedZ = animatedY * sinPitch + yawZ * cosPitch;
      const depth = rotatedZ + 3.05;
      const scale = 1 / depth;
      return {
        ...point,
        sx: width * .7 + rotatedX * width * .82 * scale,
        sy: height * .33 + rotatedY * height * 1.02 * scale,
        depth,
        alpha: clamp(.46 - (depth - 2.1) * .13, .07, .42),
      };
    };

    const draw = (time: number) => {
      context.clearRect(0, 0, width, height);
      pointerX += (targetX - pointerX) * .045;
      pointerY += (targetY - pointerY) * .045;

      const fog = context.createRadialGradient(width * .72, height * .31, 0, width * .72, height * .31, Math.min(width, height) * .5);
      fog.addColorStop(0, "rgba(30,30,30,.055)");
      fog.addColorStop(.48, "rgba(60,60,60,.025)");
      fog.addColorStop(1, "rgba(255,255,255,0)");
      context.fillStyle = fog;
      context.fillRect(0, 0, width, height);

      const projected = points.map((point) => project(point, time)).sort((a, b) => b.depth - a.depth);

      context.lineWidth = 1;
      for (let first = 0; first < projected.length; first += 1) {
        const a = projected[first];
        for (let second = first + 1; second < projected.length; second += 1) {
          const b = projected[second];
          if (a.cluster !== b.cluster && !a.anchor && !b.anchor) continue;
          const distance = Math.hypot(a.sx - b.sx, a.sy - b.sy);
          if (distance > 118 || Math.abs(a.depth - b.depth) > .72) continue;
          const alpha = (1 - distance / 118) * Math.min(a.alpha, b.alpha) * .42;
          context.strokeStyle = `rgba(30,30,30,${alpha})`;
          context.beginPath();
          context.moveTo(a.sx, a.sy);
          context.lineTo(b.sx, b.sy);
          context.stroke();
        }
      }

      for (const point of projected) {
        const radius = (point.anchor ? 6.4 : 2.5) * (3.25 / point.depth);
        if (point.anchor) {
          const halo = context.createRadialGradient(point.sx, point.sy, 0, point.sx, point.sy, radius * 5.5);
          halo.addColorStop(0, `rgba(20,20,20,${point.alpha * .22})`);
          halo.addColorStop(1, "rgba(20,20,20,0)");
          context.fillStyle = halo;
          context.beginPath();
          context.arc(point.sx, point.sy, radius * 5.5, 0, Math.PI * 2);
          context.fill();
        }
        context.fillStyle = `rgba(18,18,18,${point.alpha})`;
        context.beginPath();
        context.arc(point.sx, point.sy, Math.max(.8, radius), 0, Math.PI * 2);
        context.fill();
        if (point.anchor) {
          context.strokeStyle = `rgba(18,18,18,${point.alpha * .7})`;
          context.beginPath();
          context.arc(point.sx, point.sy, radius * 2.3, 0, Math.PI * 2);
          context.stroke();
        }
      }
    };

    const tick = (time: number) => {
      if (visible && document.visibilityState === "visible") {
        if (!reduced) draw(time);
        else if (staticDrawNeeded) {
          draw(4200);
          staticDrawNeeded = false;
        }
      }
      frame = window.requestAnimationFrame(tick);
    };

    const onPointerMove = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      const bounds = hero.getBoundingClientRect();
      targetX = clamp((event.clientX - bounds.left) / bounds.width - .5, -.5, .5);
      targetY = clamp((event.clientY - bounds.top) / bounds.height - .5, -.5, .5);
    };
    const onPointerLeave = () => { targetX = 0; targetY = 0; };
    const onMotionChange = () => {
      reduced = motion.matches;
      staticDrawNeeded = true;
    };

    const resizeObserver = new ResizeObserver(resize);
    const visibilityObserver = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      staticDrawNeeded = true;
    }, { threshold: [0, .1] });
    resizeObserver.observe(hero);
    visibilityObserver.observe(hero);
    hero.addEventListener("pointermove", onPointerMove);
    hero.addEventListener("pointerleave", onPointerLeave);
    motion.addEventListener("change", onMotionChange);
    resize();
    frame = window.requestAnimationFrame(tick);

    return () => {
      window.cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      visibilityObserver.disconnect();
      hero.removeEventListener("pointermove", onPointerMove);
      hero.removeEventListener("pointerleave", onPointerLeave);
      motion.removeEventListener("change", onMotionChange);
    };
  }, []);

  return <canvas className="hero-memory-field" ref={canvasRef} aria-hidden="true" />;
}
