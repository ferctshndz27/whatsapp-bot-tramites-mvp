document.addEventListener('DOMContentLoaded', () => {
  initVelarisBackground();
  initHeroShader();
  initVelarisGlow();
});

function initHeroShader() {
  const heroSection = document.querySelector('.hero');
  if (!heroSection || typeof THREE === 'undefined') return;

  const container = document.createElement('div');
  container.className = 'hero-canvas';
  heroSection.insertBefore(container, heroSection.firstChild);

  const vertexShader = `
    void main() {
      gl_Position = vec4( position, 1.0 );
    }
  `;

  const fragmentShader = `
    #define TWO_PI 6.2831853072
    #define PI 3.14159265359

    precision highp float;
    uniform vec2 resolution;
    uniform float time;
    uniform float pixelRatio;

    void main(void) {
      // 100.0 representa los píxeles CSS que queremos mover a la derecha
      float offsetX = 350.0 * pixelRatio;
      
      // Se lo restamos a X para que el centro visual se desplace hacia la derecha
      vec2 adjustedCoord = vec2(gl_FragCoord.x - offsetX, gl_FragCoord.y);
      vec2 uv = (adjustedCoord * 2.0 - resolution.xy) / min(resolution.x, resolution.y);
      
      float t = time * 0.05;
      float lineWidth = 0.002;

      vec3 color = vec3(0.0);
      for(int j = 0; j < 3; j++){
        for(int i = 0; i < 5; i++){
          color[j] += lineWidth * float(i * i) / abs(fract(t - 0.01 * float(j) + float(i) * 0.01) * 5.0 - length(uv) + mod(uv.x + uv.y, 0.2));
        }
      }
      
      gl_FragColor = vec4(color[0], color[1], color[2], 1.0);
    }
  `;

  const camera = new THREE.Camera();
  camera.position.z = 1;
  const scene = new THREE.Scene();
  const geometry = new THREE.PlaneGeometry(2, 2);

  const uniforms = {
    time: { type: "f", value: 1.0 },
    resolution: { type: "v2", value: new THREE.Vector2() },
    pixelRatio: { type: "f", value: window.devicePixelRatio || 1.0 },
  };

  const material = new THREE.ShaderMaterial({
    uniforms: uniforms,
    vertexShader: vertexShader,
    fragmentShader: fragmentShader,
    transparent: true,
    blending: THREE.AdditiveBlending, // Use additive blending so it looks like light
  });

  const mesh = new THREE.Mesh(geometry, material);
  scene.add(mesh);

  // We want a dark background for the canvas itself
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  container.appendChild(renderer.domElement);

  const onWindowResize = () => {
    const width = container.clientWidth;
    const height = container.clientHeight;
    renderer.setSize(width, height);
    uniforms.resolution.value.x = renderer.domElement.width;
    uniforms.resolution.value.y = renderer.domElement.height;
    uniforms.pixelRatio.value = window.devicePixelRatio || 1.0;
  };

  onWindowResize();
  window.addEventListener("resize", onWindowResize, false);

  const animate = () => {
    requestAnimationFrame(animate);
    uniforms.time.value += 0.05;
    renderer.render(scene, camera);
  };

  animate();
}
function initVelarisGlow() {
  const cards = document.querySelectorAll(".card");
  
  cards.forEach(card => {
    // Insert at the beginning so it acts as background
    const glow = document.createElement('div');
    glow.className = 'velaris-glow';
    card.insertBefore(glow, card.firstChild);

    // 3D Tilt Effect Logic
    card.addEventListener('mousemove', e => {
      const rect = card.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      
      // Calculate rotation based on cursor position relative to center
      const centerX = rect.width / 2;
      const centerY = rect.height / 2;
      
      const tiltX = ((x - centerX) / centerX) * 5; // Max 5 degrees rotation
      const tiltY = ((centerY - y) / centerY) * 5; // Invert Y axis
      
      card.style.setProperty('--tilt-x', `${tiltX}deg`);
      card.style.setProperty('--tilt-y', `${tiltY}deg`);
    });

    card.addEventListener('mouseleave', () => {
      // Reset tilt on mouse leave
      card.style.setProperty('--tilt-x', `0deg`);
      card.style.setProperty('--tilt-y', `0deg`);
    });
  });

  const containers = document.querySelectorAll('.grid, .steps');
  containers.forEach(container => {
    container.addEventListener('mousemove', e => {
      const containerCards = container.querySelectorAll(".card");
      containerCards.forEach(card => {
        const rect = card.getBoundingClientRect();
        // Calculate mouse position relative to each card for the glow effect
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        card.style.setProperty('--mouse-x', `${x}px`);
        card.style.setProperty('--mouse-y', `${y}px`);
      });
    });
  });
}

/* ===========================================================================
   Fondo Velaris — un solo lienzo fijo detrás de toda la página.

   Port fiel del shader de «Velaris» (ForgeUI, Aman Shakya): su licencia
   permite el uso comercial e incrustado y prohíbe redistribuirlo como
   librería. Va en WebGL 1 a pelo, sin Three.js.
   Ruido simplex 2D de Ashima Arts / Stefan Gustavson (MIT).

   Ajustes: preset «oro profundo», velocidad 2.3, grano 0.
   =========================================================================== */
function initVelarisBackground() {
  const canvas = document.getElementById('velaris-bg');
  if (!canvas) return;

  // Preset «oro profundo»: fondo negro, oro claro → oro → oro viejo → negro.
  const BG = '#000000';
  const COLORS = ['#e9c767', '#c9a227', '#6b4f0f', '#000000'];
  const SPEED = 2.3;
  const GRAIN = 0.0;
  const TIEMPO_INICIAL = 7.5;   // arranca con las manchas ya desplegadas
  const MAX_PIXELS = 2400000;

  const opciones = { antialias: false, alpha: false, depth: false, stencil: false };
  const gl = canvas.getContext('webgl', opciones) ||
             canvas.getContext('experimental-webgl', opciones);
  if (!gl) return;

  const VERT = [
    'attribute vec2 position;',
    'varying vec2 vUv;',
    'void main(){ vUv = position * 0.5 + 0.5; gl_Position = vec4(position, 0.0, 1.0); }'
  ].join('\n');

  const FRAG = [
    '#ifdef GL_FRAGMENT_PRECISION_HIGH',
    'precision highp float;',
    '#else',
    'precision mediump float;',
    '#endif',
    'varying vec2 vUv;',
    'uniform vec2  u_resolution;',
    'uniform float u_time;',
    'uniform float u_grain;',
    'uniform vec3  u_colors[4];',
    'uniform vec3  u_bg;',
    'vec3 permute(vec3 x) { return mod(((x*34.0)+1.0)*x, 289.0); }',
    'float snoise(vec2 v){',
    '  const vec4 C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439);',
    '  vec2 i  = floor(v + dot(v, C.yy));',
    '  vec2 x0 = v - i + dot(i, C.xx);',
    '  vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);',
    '  vec4 x12 = x0.xyxy + C.xxzz;',
    '  x12.xy -= i1;',
    '  i = mod(i, 289.0);',
    '  vec3 p = permute(permute(i.y + vec3(0.0, i1.y, 1.0)) + i.x + vec3(0.0, i1.x, 1.0));',
    '  vec3 m = max(0.5 - vec3(dot(x0,x0), dot(x12.xy,x12.xy), dot(x12.zw,x12.zw)), 0.0);',
    '  m = m*m; m = m*m;',
    '  vec3 x = 2.0 * fract(p * C.www) - 1.0;',
    '  vec3 h = abs(x) - 0.5;',
    '  vec3 ox = floor(x + 0.5);',
    '  vec3 a0 = x - ox;',
    '  m *= 1.79284291400159 - 0.85373472095314 * (a0*a0 + h*h);',
    '  vec3 g;',
    '  g.x  = a0.x  * x0.x  + h.x  * x0.y;',
    '  g.yz = a0.yz * x12.xz + h.yz * x12.yw;',
    '  return 130.0 * dot(m, g);',
    '}',
    'void main(){',
    '  vec2 uv = vUv;',
    '  float ratio = u_resolution.x / u_resolution.y;',
    '  vec2 p = uv - 0.5;',
    '  p.x *= ratio;',
    '  float t = u_time * 0.1;',
    '  float n1 = snoise(p * 0.4  + vec2( t * 0.2,  -t * 0.3));',
    '  float n2 = snoise(p * 0.55 + vec2(-t * 0.15,  t * 0.25) + n1 * 0.25);',
    '  float n3 = snoise(p * 0.75 + vec2( t * 0.1,  -t * 0.2)  + n2 * 0.2);',
    '  vec3 col = u_bg;',
    '  float dist = length(p) * 1.5;',
    '  float vignette = 1.0 - smoothstep(0.3, 1.2, dist);',
    '  col = mix(col, u_colors[0], smoothstep(-0.2, 0.5, n1) * 0.85);',
    '  col = mix(col, u_colors[1], smoothstep(-0.1, 0.6, n2) * 0.7);',
    '  col = mix(col, u_colors[2], smoothstep(-0.3, 0.4, n3) * 0.6);',
    '  col = mix(col, u_colors[3], smoothstep(0.0, 0.7, n1 * n2) * 0.5);',
    '  float glow = smoothstep(0.8, 0.0, dist) * 0.3;',
    '  col += u_colors[1] * glow;',
    '  col = mix(col * 0.2, col, vignette);',
    '  float grain = fract(sin(dot(uv, vec2(12.9898, 78.233))) * 43758.5453 + u_time);',
    '  col += (grain - 0.5) * u_grain * 0.1;',
    '  gl_FragColor = vec4(col, 1.0);',
    '}'
  ].join('\n');

  let prog = null;
  let loc = null;
  let corriendo = false;
  let raf = 0;
  let ultimo = 0;
  let tiempo = TIEMPO_INICIAL;

  const mq = window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : null;

  function aRgb(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }

  function compilar(tipo, src) {
    const s = gl.createShader(tipo);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      gl.deleteShader(s);
      return null;
    }
    return s;
  }

  function construir() {
    const vs = compilar(gl.VERTEX_SHADER, VERT);
    const fs = compilar(gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) return false;

    const p = gl.createProgram();
    gl.attachShader(p, vs);
    gl.attachShader(p, fs);
    gl.linkProgram(p);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) return false;

    prog = p;
    gl.useProgram(prog);

    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER,
                  new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const a = gl.getAttribLocation(prog, 'position');
    gl.enableVertexAttribArray(a);
    gl.vertexAttribPointer(a, 2, gl.FLOAT, false, 0, 0);

    loc = {
      res: gl.getUniformLocation(prog, 'u_resolution'),
      time: gl.getUniformLocation(prog, 'u_time')
    };

    // Constantes: un programa recién enlazado trae todos los uniformes en cero,
    // así que esto se repite tal cual al recuperar el contexto.
    gl.uniform1f(gl.getUniformLocation(prog, 'u_grain'), GRAIN);
    gl.uniform3fv(gl.getUniformLocation(prog, 'u_bg'), aRgb(BG));
    const planos = [];
    for (let i = 0; i < 4; i++) planos.push.apply(planos, aRgb(COLORS[i]));
    gl.uniform3fv(gl.getUniformLocation(prog, 'u_colors'), new Float32Array(planos));
    return true;
  }

  function medir() {
    if (!prog) return false;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let w = Math.max(1, Math.round(canvas.clientWidth * dpr));
    let h = Math.max(1, Math.round(canvas.clientHeight * dpr));
    const px = w * h;
    if (px > MAX_PIXELS) {
      const k = Math.sqrt(MAX_PIXELS / px);
      w = Math.max(1, Math.round(w * k));
      h = Math.max(1, Math.round(h * k));
    }
    const cambio = (canvas.width !== w || canvas.height !== h);
    if (cambio) { canvas.width = w; canvas.height = h; }
    // Sin condición a propósito: tras recuperar el contexto el viewport vuelve
    // a cero, y u_resolution en cero deja ratio en NaN — página negra para
    // siempre. Subirlos siempre cuesta dos llamadas y evita eso.
    gl.viewport(0, 0, w, h);
    gl.uniform2f(loc.res, w, h);
    return cambio;
  }

  function pintar() {
    if (!prog) return;
    gl.uniform1f(loc.time, tiempo);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  function cuadro(ahora) {
    if (!corriendo) return;
    // dt acumulado y tapado a 0.1 s: si la pestaña estuvo minutos en segundo
    // plano, el primer cuadro al volver no salta media hora de animación.
    const dt = ultimo ? Math.min((ahora - ultimo) / 1000, 0.1) : 0;
    ultimo = ahora;
    tiempo += dt * SPEED;
    pintar();
    raf = requestAnimationFrame(cuadro);
  }

  function arrancar() {
    if (corriendo || !prog) return;
    // prefers-reduced-motion no detiene un requestAnimationFrame por su cuenta:
    // hay que respetarlo aquí. Queda un solo fotograma, quieto.
    if (mq && mq.matches) { pintar(); return; }
    corriendo = true;
    ultimo = 0;
    raf = requestAnimationFrame(cuadro);
  }

  function detener() {
    corriendo = false;
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
  }

  function alRedimensionar() {
    // Asignar canvas.width borra el búfer. Si el bucle está parado por
    // reduced-motion, nadie repinta y la página se queda en negro.
    if (medir() && !corriendo) pintar();
  }

  canvas.addEventListener('webglcontextlost', (e) => {
    e.preventDefault();
    detener();
    prog = null;
    loc = null;
    // Sin la clase vuelve el degradado del CSS: la página nunca se queda negra.
    document.documentElement.classList.remove('velaris');
  }, false);

  canvas.addEventListener('webglcontextrestored', () => {
    if (!construir()) return;
    document.documentElement.classList.add('velaris');
    medir();
    arrancar();
  }, false);

  if (!construir()) return;
  document.documentElement.classList.add('velaris');
  medir();
  arrancar();

  // El evento resize de window no cubre el caso de cargar con el viewport en
  // cero (pestaña en segundo plano, panel oculto): el lienzo se quedaría en
  // 1x1 para siempre. El observador sí ve el momento en que la caja crece.
  if (window.ResizeObserver) {
    new ResizeObserver(alRedimensionar).observe(canvas);
  }
  window.addEventListener('resize', alRedimensionar);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) detener();
    else arrancar();
  });
  if (mq && mq.addEventListener) {
    mq.addEventListener('change', () => { detener(); arrancar(); });
  }
}
