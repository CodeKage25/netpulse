/* A 3D scene, in about a hundred lines of canvas.

   Dishylink renders its obstruction dome in raw WebGL because it has 5,839 patches to
   place. A carrier stack is a few dozen boxes, and at that size a hand-rolled
   projection with a painter's-algorithm sort is faster to write, easier to read, and
   indistinguishable at 60fps — without a shader or a library in sight.

   The camera orbits a point: yaw and pitch are angles, distance is how far back. Every
   face carries its own depth so the sort can draw far-to-near, which is the whole of
   hidden-surface removal when nothing intersects. */

function orbit(yaw, pitch, distance) {
  const cy = Math.cos(yaw), sy = Math.sin(yaw);
  const cp = Math.cos(pitch), sp = Math.sin(pitch);
  return function project(x, y, z, width, height) {
    // Rotate about the vertical axis, then tilt.
    const rx = x * cy - z * sy;
    const rz = x * sy + z * cy;
    const ry = y * cp - rz * sp;
    const depth = y * sp + rz * cp + distance;
    // A guard rather than a clip: geometry behind the camera would otherwise fold
    // through the origin and draw as a spike across the whole canvas.
    const scale = depth > 0.05 ? 1 / depth : 0;
    return {
      x: width / 2 + rx * scale * height,
      y: height / 2 - ry * scale * height,
      depth,
      scale,
    };
  };
}

/* One axis-aligned box, as the four faces that can ever be seen from above.
   The bottom is omitted deliberately — the camera is always above the floor, so
   drawing it costs a quarter of the fill for nothing. */
function boxFaces(x0, x1, y0, y1, z0, z1) {
  return [
    { name: "top", points: [[x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1]], shade: 1.0 },
    { name: "front", points: [[x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], shade: 0.78 },
    { name: "back", points: [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0]], shade: 0.62 },
    { name: "left", points: [[x0, y0, z0], [x0, y0, z1], [x0, y1, z1], [x0, y1, z0]], shade: 0.55 },
    { name: "right", points: [[x1, y0, z0], [x1, y0, z1], [x1, y1, z1], [x1, y1, z0]], shade: 0.70 },
  ];
}

/* Mix a colour toward black by a shade factor, so the faces of one box read as one
   object lit from a single direction rather than five unrelated rectangles. */
function shaded(rgb, factor) {
  const [r, g, b] = rgb;
  return `rgb(${Math.round(r * factor)},${Math.round(g * factor)},${Math.round(b * factor)})`;
}

function parseColor(value) {
  const hex = value.trim();
  if (hex.startsWith("#")) {
    const n = hex.length === 4
      ? hex.slice(1).split("").map(c => parseInt(c + c, 16))
      : [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
    return n;
  }
  const found = hex.match(/[\d.]+/g);
  return found ? found.slice(0, 3).map(Number) : [128, 128, 128];
}

/* A scene that owns its canvas, its camera and its pointer handling.
   Callers hand it a list of boxes and it draws them; it knows nothing about radios. */
function createScene(canvas, { yaw = 0.95, pitch = 0.34, distance = 3.3 } = {}) {
  const camera = { yaw, pitch, distance };
  let boxes = [];
  let labels = [];
  let floor = null;
  let backdrop = null;
  let hover = null;
  let onHover = null;

  function resize() {
    const ratio = window.devicePixelRatio || 1;
    const box = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round(box.width * ratio));
    canvas.height = Math.max(1, Math.round(box.height * ratio));
    return { width: box.width, height: box.height, ratio };
  }

  function draw() {
    const { width, height, ratio } = resize();
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const project = orbit(camera.yaw, camera.pitch, camera.distance);

    // A haze toward the horizon. Two stops of almost nothing, but it separates the
    // far end of the scene from the near end without drawing a single extra face.
    if (backdrop) {
      const wash = context.createLinearGradient(0, 0, 0, height);
      wash.addColorStop(0, backdrop);
      wash.addColorStop(1, "transparent");
      context.fillStyle = wash;
      context.fillRect(0, 0, width, height);
    }

    // The ground plane first, so the boxes have something to stand on and depth is
    // readable. Without it the bars float and the perspective is guesswork.
    if (floor) {
      const line = (a, b, colour, wide) => {
        const p0 = project(a[0], a[1], a[2], width, height);
        const p1 = project(b[0], b[1], b[2], width, height);
        if (!p0.scale || !p1.scale) return;
        context.beginPath();
        context.moveTo(p0.x, p0.y);
        context.lineTo(p1.x, p1.y);
        context.strokeStyle = colour;
        context.lineWidth = wide;
        context.stroke();
      };
      for (const x of floor.xs) line([x, floor.y, floor.z0], [x, floor.y, floor.z1], floor.grid, 1);
      for (const z of floor.zs) line([floor.x0, floor.y, z], [floor.x1, floor.y, z], floor.grid, 1);
      // The near edge is "now". Drawn brighter because everything behind it is the
      // past, and a scene where the present is not obvious is a scene read backwards.
      line([floor.x0, floor.y, floor.z1], [floor.x1, floor.y, floor.z1], floor.now || floor.edge, 2);
      line([floor.x0, floor.y, floor.z0], [floor.x1, floor.y, floor.z0], floor.edge, 1);
      // Height ticks up the left wall, so a bar's height is a quantity and not a mood.
      for (const tick of floor.ticks || []) {
        line([floor.x0, tick.y, floor.z0], [floor.x0, tick.y, floor.z1], floor.grid, 1);
      }
    }

    // Every face from every box, sorted far to near. With no intersecting geometry
    // this is exact, not an approximation.
    const faces = [];
    for (const box of boxes) {
      const rgb = parseColor(box.color);
      for (const face of boxFaces(box.x0, box.x1, box.y0, box.y1, box.z0, box.z1)) {
        const projected = face.points.map(([x, y, z]) => project(x, y, z, width, height));
        if (projected.some(p => p.scale === 0)) continue;
        faces.push({
          projected,
          fill: shaded(rgb, face.shade * (box.dim || 1)),
          depth: projected.reduce((sum, p) => sum + p.depth, 0) / 4,
          box,
        });
      }
    }
    faces.sort((a, b) => b.depth - a.depth);

    for (const face of faces) {
      context.beginPath();
      face.projected.forEach((point, index) =>
        index ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y));
      context.closePath();
      context.fillStyle = face.fill;
      context.fill();
      if (face.box === hover) {
        context.strokeStyle = "rgba(255,255,255,0.85)";
        context.lineWidth = 1.5;
        context.stroke();
      }
    }

    for (const label of labels) {
      const at = project(label.x, label.y, label.z, width, height);
      if (at.scale === 0) continue;
      context.fillStyle = label.color;
      context.font = `${label.size || 11}px -apple-system, system-ui, sans-serif`;
      context.textAlign = label.align || "center";
      context.fillText(label.text, at.x, at.y);
    }
  }

  let dragging = null;
  canvas.addEventListener("pointerdown", event => {
    dragging = { x: event.clientX, y: event.clientY, yaw: camera.yaw, pitch: camera.pitch };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointerup", () => { dragging = null; });
  canvas.addEventListener("pointerleave", () => {
    dragging = null;
    if (hover) { hover = null; draw(); if (onHover) onHover(null); }
  });
  canvas.addEventListener("pointermove", event => {
    if (dragging) {
      camera.yaw = dragging.yaw + (event.clientX - dragging.x) * 0.008;
      // Clamped so the camera cannot pass under the floor or over the top, where the
      // scene inverts and stops being readable.
      camera.pitch = Math.max(0.05, Math.min(1.35,
        dragging.pitch + (event.clientY - dragging.y) * 0.006));
      draw();
      return;
    }
    const box = canvas.getBoundingClientRect();
    const found = pick(event.clientX - box.left, event.clientY - box.top, box);
    if (found !== hover) { hover = found; draw(); if (onHover) onHover(found); }
  });
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    camera.distance = Math.max(1.4, Math.min(8, camera.distance + event.deltaY * 0.004));
    draw();
  }, { passive: false });

  /* Hit-testing without a depth buffer: project each box's top face and take the
     nearest one whose polygon contains the pointer. Good enough for boxes that never
     overlap in the horizontal plane, which carriers on distinct frequencies do not. */
  function pick(px, py, rect) {
    const project = orbit(camera.yaw, camera.pitch, camera.distance);
    let best = null;
    let bestDepth = Infinity;
    for (const box of boxes) {
      const face = boxFaces(box.x0, box.x1, box.y0, box.y1, box.z0, box.z1)[0];
      const points = face.points.map(([x, y, z]) => project(x, y, z, rect.width, rect.height));
      if (points.some(p => p.scale === 0)) continue;
      const depth = points.reduce((sum, p) => sum + p.depth, 0) / 4;
      if (depth < bestDepth && inside(px, py, points)) { best = box; bestDepth = depth; }
    }
    return best;
  }

  function inside(px, py, points) {
    let hit = false;
    for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
      const a = points[i], b = points[j];
      if ((a.y > py) !== (b.y > py) && px < ((b.x - a.x) * (py - a.y)) / (b.y - a.y) + a.x) {
        hit = !hit;
      }
    }
    return hit;
  }

  /* A short turn when the scene first appears. Static 3D reads as a flat pattern until
     it moves once; a second of rotation is the cheapest way to say "this has depth,
     and you can turn it". It settles at the starting angle rather than somewhere new,
     so nothing is left in a position the user did not choose. */
  function introduce() {
    const target = camera.yaw;
    const from = target - 0.55;
    const started = performance.now();
    camera.yaw = from;
    function step(now) {
      const t = Math.min(1, (now - started) / 900);
      // Ease out: fast at first, settling rather than stopping dead.
      camera.yaw = from + (target - from) * (1 - Math.pow(1 - t, 3));
      draw();
      if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  return {
    introduce,
    set(nextBoxes, nextLabels = [], nextFloor = null, nextBackdrop = null) {
      boxes = nextBoxes;
      labels = nextLabels;
      floor = nextFloor;
      backdrop = nextBackdrop;
      draw();
    },
    onHover(handler) { onHover = handler; },
    draw,
    camera,
  };
}
