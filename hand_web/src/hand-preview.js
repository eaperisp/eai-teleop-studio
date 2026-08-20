import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import URDFLoader from 'urdf-loader';

const DEFAULT_PREVIEW = {
  model_root: '/assets/brainco_hand',
  urdf: 'brainco_{side}.urdf',
  side_prefix: { left: 'left', right: 'right' },
  material_mode: 'brainco',
  joints: [
    { index: 0, targets: [{ suffix: 'thumb_proximal_joint', lower: 0, upper: 1.0472 }] },
    { index: 1, targets: [{ suffix: 'thumb_metacarpal_joint', lower: 0, upper: 1.5184 }] },
    { index: 2, targets: [{ suffix: 'index_proximal_joint', lower: 0, upper: 1.4661 }] },
    { index: 3, targets: [{ suffix: 'middle_proximal_joint', lower: 0, upper: 1.4661 }] },
    { index: 4, targets: [{ suffix: 'ring_proximal_joint', lower: 0, upper: 1.4661 }] },
    { index: 5, targets: [{ suffix: 'pinky_proximal_joint', lower: 0, upper: 1.4661 }] },
  ],
};

class HandModelPreview {
  constructor(container, stateElement) {
    this.container = container;
    this.stateElement = stateElement;
    this.side = null;
    this.previewConfig = DEFAULT_PREVIEW;
    this.modelKey = '';
    this.robot = null;
    this.pose = [0, 0, 0, 0, 0, 0];
    this.loadToken = 0;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0xf4f6f7);
    this.camera = new THREE.PerspectiveCamera(34, 1, 0.001, 10);
    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      powerPreference: 'high-performance',
      preserveDrawingBuffer: true,
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 0.92;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.container.append(this.renderer.domElement);

    const environment = new RoomEnvironment();
    const environmentGenerator = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = environmentGenerator.fromScene(environment, 0.04).texture;
    environment.dispose();
    environmentGenerator.dispose();

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.enablePan = false;
    this.controls.minDistance = 0.18;
    this.controls.maxDistance = 1.2;

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x899297, 1.35));
    const key = new THREE.DirectionalLight(0xffffff, 2.35);
    key.position.set(-0.45, 0.35, 0.55);
    key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024);
    key.shadow.bias = -0.0002;
    this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xc6e7ee, 1.1);
    fill.position.set(-0.25, -0.5, 0.2);
    this.scene.add(fill);

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.container);
    this.resize();
    this.animate();
  }

  setPose(side, values, previewConfig = null) {
    const normalizedSide = side === 'left' ? 'left' : 'right';
    const config = this.normalizePreviewConfig(previewConfig);
    const modelKey = JSON.stringify([normalizedSide, config.model_root, config.urdf]);
    this.pose = config.joints.map((joint) => this.clamp(values?.[joint.index] ?? 0));
    if (this.modelKey !== modelKey) {
      this.load(normalizedSide, config, modelKey);
      return;
    }
    this.previewConfig = config;
    this.applyPose();
  }

  load(side, config, modelKey) {
    const token = ++this.loadToken;
    this.side = side;
    this.previewConfig = config;
    this.modelKey = modelKey;
    this.showState('模型加载中');
    this.removeRobot();

    const manager = new THREE.LoadingManager();
    const loader = new URDFLoader(manager);
    let loadedRobot = null;

    manager.onLoad = () => {
      if (token !== this.loadToken || !loadedRobot) return;
      this.prepareRobot(loadedRobot);
      this.robot = loadedRobot;
      this.scene.add(loadedRobot);
      this.frameOpenPose();
      this.showState('');
      setTimeout(() => {
        if (token === this.loadToken && this.robot === loadedRobot) this.frameOpenPose();
      }, 120);
    };
    manager.onError = (url) => {
      if (token === this.loadToken) this.showState(`模型资源加载失败：${url.split('/').pop()}`);
    };

    loader.load(
      `${config.model_root}/${config.urdf.replace('{side}', side)}`,
      (robot) => { loadedRobot = robot; },
      undefined,
      () => {
        if (token === this.loadToken) this.showState('URDF 模型加载失败');
      },
    );
  }

  prepareRobot(robot) {
    robot.rotation.x = -Math.PI / 2;
    robot.updateMatrixWorld(true);
    const axisMeshes = [];
    robot.traverse((object) => {
      if (!object.isMesh) return;
      const isAxisPrimitive = object.geometry?.type === 'CylinderGeometry'
        || object.geometry?.type === 'SphereGeometry';
      if (isAxisPrimitive) {
        axisMeshes.push(object);
        return;
      }
      if (this.previewConfig.material_mode === 'brainco') {
        this.applyProductMaterials(object);
      } else if (this.previewConfig.material_mode === 'inspire') {
        this.applyInspireMaterials(object);
      }
      object.castShadow = true;
      object.receiveShadow = true;
    });
    axisMeshes.forEach((mesh) => {
      mesh.parent?.remove(mesh);
      mesh.geometry?.dispose?.();
      mesh.material?.dispose?.();
    });
    robot.updateMatrixWorld(true);
  }

  applyProductMaterials(object) {
    const geometry = object.geometry;
    const linkName = this.findLinkName(object);
    const isProximalLink = linkName.includes('_proximal_link');
    if (!geometry.getAttribute('normal')) geometry.computeVertexNormals();
    const normals = geometry.getAttribute('normal');
    const normalMatrix = new THREE.Matrix3().getNormalMatrix(object.matrixWorld);
    const worldNormal = new THREE.Vector3();

    geometry.clearGroups();
    let groupStart = 0;
    let currentMaterial = -1;
    for (let index = 0; index < normals.count; index += 3) {
      worldNormal.fromBufferAttribute(normals, index).applyNormalMatrix(normalMatrix);
      const isFrontSurface = worldNormal.x < -0.18;
      const isBackSurface = worldNormal.x > 0.18;
      const materialIndex = isBackSurface || (isFrontSurface && isProximalLink) ? 0 : 1;
      if (currentMaterial === -1) currentMaterial = materialIndex;
      if (materialIndex !== currentMaterial) {
        geometry.addGroup(groupStart, index - groupStart, currentMaterial);
        groupStart = index;
        currentMaterial = materialIndex;
      }
    }
    geometry.addGroup(groupStart, normals.count - groupStart, currentMaterial);

    object.material = [
      new THREE.MeshPhysicalMaterial({
        color: 0xb8bec2,
        metalness: 0.86,
        roughness: 0.2,
        clearcoat: 0.18,
        clearcoatRoughness: 0.2,
      }),
      new THREE.MeshPhysicalMaterial({
        color: 0x171a1c,
        metalness: 0.12,
        roughness: 0.5,
        clearcoat: 0.25,
        clearcoatRoughness: 0.34,
      }),
    ];
  }

  applyInspireMaterials(object) {
    const linkName = this.findLinkName(object);
    const isPalm = linkName.includes('hand_base');
    const isDistal = linkName.includes('distal') || linkName.includes('intermediate');
    object.material = new THREE.MeshPhysicalMaterial({
      color: isPalm ? 0x3b4247 : isDistal ? 0x22272a : 0x9ba5aa,
      metalness: isPalm ? 0.38 : 0.68,
      roughness: isDistal ? 0.48 : 0.3,
      clearcoat: 0.16,
      clearcoatRoughness: 0.28,
    });
  }

  findLinkName(object) {
    let current = object.parent;
    while (current && !current.isURDFLink) current = current.parent;
    return current?.urdfName || '';
  }

  applyPose() {
    if (!this.robot) return;
    const prefix = this.previewConfig.side_prefix?.[this.side] || this.side;
    this.previewConfig.joints.forEach((joint, poseIndex) => {
      const value = this.pose[poseIndex];
      (joint.targets || []).forEach((target) => {
        const lower = Number(target.lower || 0);
        const upper = Number(target.upper || 0);
        this.robot.setJointValue(`${prefix}_${target.suffix}`, lower + value * (upper - lower));
      });
    });
    this.robot.updateMatrixWorld(true);
  }

  frameOpenPose() {
    const currentPose = this.pose.slice();
    this.pose = this.pose.map(() => 0);
    this.applyPose();
    this.frameRobot();
    this.pose = currentPose;
    this.applyPose();
  }

  frameRobot() {
    const bounds = new THREE.Box3().setFromObject(this.robot);
    const sphere = bounds.getBoundingSphere(new THREE.Sphere());
    if (!Number.isFinite(sphere.radius) || sphere.radius <= 0) return;

    const verticalFov = THREE.MathUtils.degToRad(this.camera.fov);
    const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * this.camera.aspect);
    const fitFov = Math.min(verticalFov, horizontalFov);
    const distance = sphere.radius / Math.sin(fitFov / 2);
    const direction = new THREE.Vector3(-1, 0.08, 0.1).normalize();
    this.controls.target.copy(sphere.center);
    this.camera.up.set(0, 0, 1);
    this.camera.position.copy(sphere.center).addScaledVector(direction, distance * 0.86);
    this.camera.near = Math.max(distance / 100, 0.001);
    this.camera.far = distance * 10;
    this.camera.updateProjectionMatrix();
    this.controls.minDistance = distance * 0.55;
    this.controls.maxDistance = distance * 2.4;
    this.controls.update();
  }

  removeRobot() {
    if (!this.robot) return;
    this.scene.remove(this.robot);
    this.robot.traverse((object) => {
      object.geometry?.dispose?.();
      const materials = Array.isArray(object.material) ? object.material : [object.material];
      materials.filter(Boolean).forEach((material) => material.dispose());
    });
    this.robot = null;
  }

  resize() {
    const width = Math.max(this.container.clientWidth, 1);
    const height = Math.max(this.container.clientHeight, 1);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
  }

  showState(message) {
    this.stateElement.textContent = message;
    this.stateElement.classList.toggle('hidden', !message);
  }

  clamp(value) {
    const number = Number(value);
    return Number.isFinite(number) ? Math.max(0, Math.min(1, number)) : 0;
  }

  normalizePreviewConfig(value) {
    if (!value || !Array.isArray(value.joints) || !value.joints.length) return DEFAULT_PREVIEW;
    return value;
  }

  animate() {
    requestAnimationFrame(() => this.animate());
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  debugState() {
    return {
      loaded: Boolean(this.robot),
      side: this.side,
      modelKey: this.modelKey,
      pose: this.pose.slice(),
      meshCount: this.robot ? this.robot.getObjectsByProperty('isMesh', true).length : 0,
    };
  }
}

window.HandModelPreview = HandModelPreview;
