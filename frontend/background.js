/*
 * Decorative animated "light rays" background, drawn with WebGL (OGL library).
 * Purely cosmetic: the app works the same if you delete this file and its <script> tags.
 */
(function initLightRays() {
    const container = document.getElementById('lightRaysBackground');
    if (!container || typeof OGL === 'undefined') return;

    const { Renderer, Camera, Transform, Geometry, Program, Mesh, Vec2, Vec3 } = OGL;

    const renderer = new Renderer({ dpr: 2, alpha: true });
    const gl = renderer.gl;
    gl.clearColor(0, 0, 0, 0);
    container.appendChild(gl.canvas);

    const camera = new Camera(gl, { fov: 45 });
    camera.position.set(0, 0, 5);

    function resize() {
        renderer.setSize(container.offsetWidth, container.offsetHeight);
        camera.perspective({ aspect: gl.canvas.width / gl.canvas.height });
    }
    window.addEventListener('resize', resize);
    resize();

    // Vertex shader: places the triangle that covers the screen
    const vertex = `
        attribute vec3 position;
        attribute vec3 normal;
        uniform mat4 modelViewMatrix;
        uniform mat4 projectionMatrix;
        uniform mat3 normalMatrix;
        varying vec3 vNormal;
        void main() {
            vNormal = normalize(normalMatrix * normal);
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
    `;

    // Fragment shader: colours each pixel with rotating rays coming from uOrigin
    const fragment = `
        precision highp float;
        uniform float uTime;
        uniform vec3 uColor;
        uniform vec2 uOrigin;
        varying vec3 vNormal;
        void main() {
            float lighting = dot(vNormal, normalize(vec3(10)));
            vec3 color = uColor + lighting * 0.1;
            vec2 toOrigin = uOrigin - gl_FragCoord.xy / vec2(800.0, 600.0);
            float angle = atan(toOrigin.y, toOrigin.x);
            float rays = (sin(angle * 20.0 + uTime * 2.0) * 0.5 + 0.5) * smoothstep(1.0, 0.0, length(toOrigin));
            gl_FragColor = vec4(color, rays * 0.3);
        }
    `;

    const program = new Program(gl, {
        vertex,
        fragment,
        uniforms: {
            uTime: { value: 0 },
            uColor: { value: new Vec3(0, 1, 1) },  // cyan
            uOrigin: { value: new Vec2(0.5, 1) },  // top-center
        },
        transparent: true,
        cullFace: false,
    });

    const geometry = new Geometry(gl, {
        position: { size: 3, data: new Float32Array([-1, -1, 0, 3, -1, 0, -1, 3, 0]) },
        uv: { size: 2, data: new Float32Array([0, 0, 2, 0, 0, 2]) },
        normal: { size: 3, data: new Float32Array([0, 0, 1, 0, 0, 1, 0, 0, 1]) },
    });

    const scene = new Transform();
    new Mesh(gl, { geometry, program }).setParent(scene);

    function update(time) {
        requestAnimationFrame(update);
        program.uniforms.uTime.value = time * 0.001;
        renderer.render({ scene, camera });
    }
    requestAnimationFrame(update);
})();
