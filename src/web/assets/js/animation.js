/**
 * Decorative canvas network and companion robot positioning.
 *
 * The module deliberately owns every listener and observer it creates.  Call
 * `destroy()` when the page is unmounted or replaced before constructing a
 * subsequent visual system.
 */

const NETWORK = Object.freeze({
  particleColor: "rgba(255, 255, 255, 0.74)",
  lineColor: [255, 255, 255],
  particleCount: 380,
  maxDistance: 170,
  maxConnectionsPerParticle: 6,
  hubChance: 0.18,
  hubMaxConnections: 10,
  lineOpacityMultiplier: 0.65,
  lineWidth: 0.6,
  particleSpeed: 0.22,
  particleRadius: 1.3,
  glow: true,
  maxGapRatio: 0.44,
  cardsBottomMargin: 170,
  edgeParticles: 130,
  edgeBiasPower: 3.2,
  topSideHeight: 260,
  topSideEdgeKeepRatio: 0.78,
  gapFadeDistance: 110,
  bottomFillSpacing: 90,
  scrollIdleDelay: 150,
});

const ROBOT = Object.freeze({
  baseTopVh: -13.5,
  assistantRobotWidthRatio: 0.5835,
  assistantRobotMaxWidth: 1167,
  assistantVisibleLeftInsetRatio: 0.375,
  assistantVisibleTopInsetRatio: 0.123,
  assistantVisibleTopWithinChatRatio: 0.468,
  assistantGapRatio: 0.0125,
  mobilePortraitBreakpoint: 767,
  mobileLandscapeMaxWidth: 1000,
  compactHeightBreakpoint: 760,
  phoneBreakpoint: 600,
  desktopWidthRatio: 0.44,
  desktopHeightRatio: 1.29,
  minimumCardGap: 57,
  visibleLeftRatio: 0.36,
  bottomOverlapRatio: 0.55,
  phoneCardOverlapRatio: 0.445,
  tabletCardOverlapRatio: 0.52,
  scrollActivationThreshold: 50,
  scrollSmoothingMs: 64,
  settleThreshold: 0.01,
});

const NOOP_VISUAL_SYSTEM = Object.freeze({
  refresh() {},
  destroy() {},
});

/**
 * Creates the decorative visual layer used by the digest page.
 *
 * @param {object} options
 * @param {HTMLCanvasElement} options.canvas
 * @param {HTMLElement} options.container
 * @param {HTMLElement} options.overlay
 * @param {HTMLElement} options.robot
 * @returns {{refresh: () => void, destroy: () => void}}
 */
export function createVisualSystem({ canvas, container, overlay, robot } = {}) {
  if (
    !canvas ||
    typeof canvas.getContext !== "function" ||
    !container ||
    !overlay ||
    !robot
  ) {
    return NOOP_VISUAL_SYSTEM;
  }

  const context = canvas.getContext("2d");
  if (!context) {
    return NOOP_VISUAL_SYSTEM;
  }

  const documentRef = canvas.ownerDocument || document;
  const windowRef = documentRef.defaultView || window;
  const reducedMotionMedia = typeof windowRef.matchMedia === "function"
    ? windowRef.matchMedia("(prefers-reduced-motion: reduce)")
    : null;
  const mobileMedia = typeof windowRef.matchMedia === "function"
    ? windowRef.matchMedia(
      `(max-width: ${ROBOT.mobilePortraitBreakpoint}px), ` +
      `(max-width: ${ROBOT.mobileLandscapeMaxWidth}px) and (max-height: ${ROBOT.compactHeightBreakpoint}px)`,
    )
    : null;
  const phoneMedia = typeof windowRef.matchMedia === "function"
    ? windowRef.matchMedia(`(max-width: ${ROBOT.phoneBreakpoint}px)`)
    : null;
  const ResizeObserverConstructor = windowRef.ResizeObserver || globalThis.ResizeObserver;
  const robotImage = robot.querySelector("img");
  const assistantChat = documentRef.querySelector(".assistant-chat-window");

  const originalRobotStyles = {
    position: robot.style.position,
    top: robot.style.top,
    left: robot.style.left,
    width: robot.style.width,
    height: robot.style.height,
    transition: robot.style.transition,
    transform: robot.style.transform,
    willChange: robot.style.willChange,
  };
  const originalRobotClassName = robot.className;
  const originalCanvasAttributes = {
    width: canvas.getAttribute("width"),
    height: canvas.getAttribute("height"),
  };

  let destroyed = false;
  let robotInitiallyPositioned = false;
  let animationFrame = 0;
  let refreshFrame = 0;
  let postPaintRefreshFrame = 0;
  let robotFrame = 0;
  let networkResumeTimer = 0;
  let networkPausedForScroll = false;
  let canvasWidth = 0;
  let canvasHeight = 0;
  let cardsBottomY = 630;
  let gapCloseY = 800;
  let particles = [];
  let networkLayout = null;
  let resizeObserver = null;
  let robotTargetTranslateY = 0;
  let robotRenderedTranslateY = 0;
  let robotLastTimestamp = 0;
  let lastRobotTransform = "";
  let robotMetrics = {
    assistant: false,
    dynamic: false,
    baseTop: 0,
    maxTop: 0,
    maxScroll: 0,
    left: 0,
    containerLeft: 0,
  };

  function prefersReducedMotion() {
    return Boolean(reducedMotionMedia && reducedMotionMedia.matches);
  }

  function isMobile() {
    return mobileMedia
      ? mobileMedia.matches
      : (
        (windowRef.innerWidth || 0) <= ROBOT.mobilePortraitBreakpoint ||
        (
          (windowRef.innerWidth || 0) <= ROBOT.mobileLandscapeMaxWidth &&
          (windowRef.innerHeight || 0) <= ROBOT.compactHeightBreakpoint
        )
      );
  }

  function isPhone() {
    return phoneMedia
      ? phoneMedia.matches
      : (windowRef.innerWidth || 0) <= ROBOT.phoneBreakpoint;
  }

  function activeAssistantSurface() {
    if (!documentRef.body?.classList.contains("assistant-view") || !assistantChat) {
      return null;
    }
    return assistantChat;
  }

  function layoutSurface() {
    return activeAssistantSurface() || overlay;
  }

  function clamp(value, minimum, maximum) {
    return Math.min(Math.max(value, minimum), maximum);
  }

  function gapHalfWidth(y) {
    const maximumGap = canvasWidth * NETWORK.maxGapRatio;
    const normalizedY = Math.min(y / gapCloseY, 1);
    const ellipticalCurve = Math.sqrt(Math.max(0, 1 - normalizedY * normalizedY));
    const distancePastCards = y - cardsBottomY;
    const closureFactor = distancePastCards <= 0
      ? 1
      : Math.max(0, 1 - distancePastCards / NETWORK.cardsBottomMargin);

    return maximumGap * ellipticalCurve * closureFactor;
  }

  function isInGap(x, y) {
    return Math.abs(x - canvasWidth / 2) < gapHalfWidth(y);
  }

  function gapFadeFactor(x, y) {
    const distanceFromGapEdge = Math.abs(x - canvasWidth / 2) - gapHalfWidth(y);
    if (distanceFromGapEdge <= 0) {
      return 0;
    }
    if (distanceFromGapEdge >= NETWORK.gapFadeDistance) {
      return 1;
    }

    const progress = distanceFromGapEdge / NETWORK.gapFadeDistance;
    return progress * progress * (3 - 2 * progress);
  }

  function samplePosition() {
    let x;
    let y;
    let attempts = 0;

    do {
      x = Math.random() * canvasWidth;
      y = Math.random() * canvasHeight;
      attempts += 1;
    } while (isInGap(x, y) && attempts < 50);

    return { x, y };
  }

  function edgeBiasedCoordinate(dimension) {
    const random = Math.random();
    const towardZero = Math.random() < 0.5;
    const biased = towardZero
      ? Math.pow(random, NETWORK.edgeBiasPower)
      : 1 - Math.pow(random, NETWORK.edgeBiasPower);
    return biased * dimension;
  }

  function sampleEdgePosition() {
    let x;
    let y;
    let attempts = 0;

    do {
      x = edgeBiasedCoordinate(canvasWidth);
      y = edgeBiasedCoordinate(canvasHeight);
      attempts += 1;
    } while (isInGap(x, y) && attempts < 50);

    return { x, y };
  }

  class Particle {
    constructor(edgeBiased = false) {
      const position = edgeBiased ? sampleEdgePosition() : samplePosition();
      this.x = position.x;
      this.y = position.y;
      this.vx = (Math.random() - 0.5) * NETWORK.particleSpeed;
      this.vy = (Math.random() - 0.5) * NETWORK.particleSpeed;
      this.isHub = Math.random() < NETWORK.hubChance;
      this.maxConnections = this.isHub
        ? NETWORK.hubMaxConnections
        : NETWORK.maxConnectionsPerParticle;
      this.radius = this.isHub
        ? NETWORK.particleRadius * 1.6 + Math.random() * 1.2
        : NETWORK.particleRadius + Math.random() * 1.0;
    }

    update() {
      const previousX = this.x;
      const previousY = this.y;

      this.x += this.vx;
      this.y += this.vy;

      if (this.x <= 0 || this.x >= canvasWidth) {
        this.vx *= -1;
      }
      if (this.y <= 0 || this.y >= canvasHeight) {
        this.vy *= -1;
      }

      this.x = Math.max(0, Math.min(canvasWidth, this.x));
      this.y = Math.max(0, Math.min(canvasHeight, this.y));

      if (isInGap(this.x, this.y)) {
        this.x = previousX;
        this.y = previousY;
        this.vx *= -1;
        this.vy *= -1;
      }
    }

    draw() {
      const fade = gapFadeFactor(this.x, this.y);
      if (fade <= 0) {
        return;
      }

      context.beginPath();
      context.arc(this.x, this.y, this.radius, 0, Math.PI * 2);
      context.fillStyle = NETWORK.particleColor;
      context.globalAlpha = fade;

      if (NETWORK.glow) {
        context.shadowBlur = 8;
        context.shadowColor = "rgba(255,255,255,0.8)";
      }

      context.fill();
      context.shadowBlur = 0;
      context.globalAlpha = 1;
    }
  }

  function scaledParticleCounts() {
    const viewportHeight = windowRef.innerHeight || 800;
    const heightFactor = Math.max(1, canvasHeight / viewportHeight);
    return {
      count: Math.round(NETWORK.particleCount * heightFactor),
      edge: Math.round(NETWORK.edgeParticles * heightFactor),
    };
  }

  function addBottomFillParticles() {
    const startY = cardsBottomY + 10;
    if (startY >= canvasHeight - 5) {
      return;
    }

    const spacing = NETWORK.bottomFillSpacing;
    const columns = Math.max(1, Math.ceil(canvasWidth / spacing));
    const rows = Math.max(1, Math.ceil((canvasHeight - startY) / spacing));

    for (let row = 0; row < rows; row += 1) {
      for (let column = 0; column < columns; column += 1) {
        const jitterX = (Math.random() - 0.5) * spacing * 0.9;
        const jitterY = (Math.random() - 0.5) * spacing * 0.9;
        const x = Math.min(
          canvasWidth,
          Math.max(0, column * spacing + spacing / 2 + jitterX),
        );
        const y = Math.min(
          canvasHeight,
          Math.max(startY, row * spacing + startY + spacing / 2 + jitterY),
        );

        if (isInGap(x, y)) {
          continue;
        }

        const particle = new Particle(false);
        particle.x = x;
        particle.y = y;
        particles.push(particle);
      }
    }
  }

  function shouldKeepTopSideEdgeParticle(particle) {
    if (particle.y >= NETWORK.topSideHeight || isInGap(particle.x, particle.y)) {
      return true;
    }
    return Math.random() < NETWORK.topSideEdgeKeepRatio;
  }

  function initialiseParticles() {
    particles = [];
    const counts = scaledParticleCounts();

    for (let index = 0; index < counts.count; index += 1) {
      particles.push(new Particle(false));
    }

    for (let index = 0; index < counts.edge; index += 1) {
      const particle = new Particle(true);
      if (shouldKeepTopSideEdgeParticle(particle)) {
        particles.push(particle);
      }
    }

    addBottomFillParticles();
  }

  function resizeCanvas() {
    const nextWidth = canvas.offsetWidth;
    const nextHeight = canvas.offsetHeight;

    if (nextWidth !== canvasWidth || nextHeight !== canvasHeight) {
      canvasWidth = canvas.width = canvas.offsetWidth;
      canvasHeight = canvas.height = canvas.offsetHeight;
      return;
    }

    canvasWidth = nextWidth;
    canvasHeight = nextHeight;
  }

  function measureGap() {
    const containerRect = container.getBoundingClientRect();
    const overlayRect = layoutSurface().getBoundingClientRect();
    const overlayBottom = overlayRect.bottom - containerRect.top;
    cardsBottomY = overlayBottom;
    gapCloseY = Math.max(overlayBottom + NETWORK.cardsBottomMargin, 1);
  }

  function buildConnectionGrid() {
    const cellSize = NETWORK.maxDistance;
    const columns = Math.floor(canvasWidth / cellSize) + 1;
    const rows = Math.floor(canvasHeight / cellSize) + 1;
    const cells = new Array(columns * rows);

    for (let index = 0; index < particles.length; index += 1) {
      const particle = particles[index];
      const column = Math.floor(particle.x / cellSize);
      const row = Math.floor(particle.y / cellSize);
      const cellIndex = row * columns + column;
      const cell = cells[cellIndex];
      if (cell) {
        cell.push(index);
      } else {
        cells[cellIndex] = [index];
      }
    }

    return { cells, columns, rows };
  }

  function connectParticles() {
    const [red, green, blue] = NETWORK.lineColor;
    const cellSize = NETWORK.maxDistance;
    const { cells, columns, rows } = buildConnectionGrid();

    for (let index = 0; index < particles.length; index += 1) {
      const particle = particles[index];
      const candidates = [];
      const column = Math.floor(particle.x / cellSize);
      const row = Math.floor(particle.y / cellSize);

      for (let columnOffset = -1; columnOffset <= 1; columnOffset += 1) {
        for (let rowOffset = -1; rowOffset <= 1; rowOffset += 1) {
          const neighborColumn = column + columnOffset;
          const neighborRow = row + rowOffset;
          if (
            neighborColumn < 0 || neighborColumn >= columns ||
            neighborRow < 0 || neighborRow >= rows
          ) {
            continue;
          }

          const cell = cells[neighborRow * columns + neighborColumn];
          if (!cell) {
            continue;
          }

          for (let cellIndex = 0; cellIndex < cell.length; cellIndex += 1) {
            const neighborIndex = cell[cellIndex];
            if (index === neighborIndex) {
              continue;
            }

            const neighbor = particles[neighborIndex];
            const deltaX = particle.x - neighbor.x;
            const deltaY = particle.y - neighbor.y;
            const distance = Math.sqrt(deltaX * deltaX + deltaY * deltaY);

            if (distance < NETWORK.maxDistance) {
              candidates.push({ neighborIndex, distance });
            }
          }
        }
      }

      candidates.sort((left, right) => (
        left.distance - right.distance || left.neighborIndex - right.neighborIndex
      ));
      const connections = candidates.slice(0, particle.maxConnections);

      connections.forEach(({ neighborIndex, distance }) => {
        if (neighborIndex < index) {
          return;
        }

        const fade = (
          gapFadeFactor(particles[index].x, particles[index].y) +
          gapFadeFactor(particles[neighborIndex].x, particles[neighborIndex].y)
        ) / 2;
        if (fade <= 0) {
          return;
        }

        const opacity = (1 - distance / NETWORK.maxDistance) *
          NETWORK.lineOpacityMultiplier * fade;
        context.beginPath();
        context.strokeStyle = `rgba(${red}, ${green}, ${blue}, ${opacity.toFixed(2)})`;
        context.lineWidth = NETWORK.lineWidth;
        context.moveTo(particle.x, particle.y);
        context.lineTo(particles[neighborIndex].x, particles[neighborIndex].y);
        context.stroke();
      });
    }
  }

  function drawFrame() {
    if (canvasWidth <= 0 || canvasHeight <= 0) {
      return;
    }

    context.clearRect(0, 0, canvasWidth, canvasHeight);
    particles.forEach((particle) => {
      particle.update();
      particle.draw();
    });
    connectParticles();
  }

  function canAnimate() {
    return !destroyed &&
      !documentRef.hidden &&
      !networkPausedForScroll &&
      canvasWidth > 0 &&
      canvasHeight > 0;
  }

  function stopAnimation() {
    if (animationFrame) {
      windowRef.cancelAnimationFrame(animationFrame);
      animationFrame = 0;
    }
  }

  function animationTick() {
    animationFrame = 0;
    if (!canAnimate()) {
      return;
    }

    drawFrame();
    animationFrame = windowRef.requestAnimationFrame(animationTick);
  }

  function startAnimation() {
    if (!animationFrame && canAnimate()) {
      animationFrame = windowRef.requestAnimationFrame(animationTick);
    }
  }

  function clearNetworkResumeTimer() {
    if (networkResumeTimer) {
      windowRef.clearTimeout(networkResumeTimer);
      networkResumeTimer = 0;
    }
  }

  function pauseNetworkForScroll() {
    if (destroyed || documentRef.hidden) {
      return;
    }

    networkPausedForScroll = true;
    stopAnimation();
    clearNetworkResumeTimer();
    networkResumeTimer = windowRef.setTimeout(() => {
      networkResumeTimer = 0;
      networkPausedForScroll = false;
      startAnimation();
    }, NETWORK.scrollIdleDelay);
  }

  function cardElements() {
    const assistantSurface = activeAssistantSurface();
    if (assistantSurface) {
      return [assistantSurface];
    }
    return Array.from(overlay.children).filter((element) => element.nodeType === 1);
  }

  function clearMobileRobotOverrides() {
    robot.style.position = originalRobotStyles.position;
    robot.style.top = originalRobotStyles.top;
    robot.style.left = originalRobotStyles.left;
    robot.style.width = originalRobotStyles.width;
    robot.style.height = originalRobotStyles.height;
    robot.style.transition = originalRobotStyles.transition;
    robot.style.transform = originalRobotStyles.transform;
    robot.style.willChange = originalRobotStyles.willChange;
    robotTargetTranslateY = 0;
    robotRenderedTranslateY = 0;
    robotLastTimestamp = 0;
    lastRobotTransform = "";
  }

  function readScrollHeight() {
    const bodyHeight = documentRef.body ? documentRef.body.scrollHeight : 0;
    return Math.max(documentRef.documentElement.scrollHeight, bodyHeight);
  }

  function refreshRobotMetrics() {
    if (isMobile()) {
      clearMobileRobotOverrides();
      const containerRect = container.getBoundingClientRect();
      const firstCard = cardElements()[0];
      const firstCardTop = firstCard
        ? firstCard.getBoundingClientRect().top - containerRect.top
        : 0;
      const robotRect = robot.getBoundingClientRect();
      const robotWidth = robotRect.width || robot.offsetWidth || 0;
      const robotHeight = robotRect.height || robotWidth * (9 / 16);
      const cardOverlapRatio = isPhone()
        ? ROBOT.phoneCardOverlapRatio
        : ROBOT.tabletCardOverlapRatio;
      const cardAnchoredTop = firstCardTop - robotHeight * cardOverlapRatio;
      robotMetrics = {
        assistant: false,
        dynamic: false,
        baseTop: Math.max(0, cardAnchoredTop),
        maxTop: 0,
        maxScroll: 0,
        left: 0,
        containerLeft: 0,
      };
      return;
    }

    const viewportWidth = Math.max(windowRef.innerWidth || 0, 1);
    const viewportHeight = Math.max(windowRef.innerHeight || 0, 1);
    const containerRect = container.getBoundingClientRect();
    const overlayRect = layoutSurface().getBoundingClientRect();
    const cards = cardElements();
    const assistantSurface = activeAssistantSurface();
    const desiredWidth = assistantSurface
      ? Math.min(viewportWidth * ROBOT.assistantRobotWidthRatio, ROBOT.assistantRobotMaxWidth)
      : viewportWidth * ROBOT.desktopWidthRatio;
    const desiredHeight = assistantSurface
      ? desiredWidth * (9 / 16)
      : viewportHeight * ROBOT.desktopHeightRatio;

    robot.style.width = `${Math.round(desiredWidth)}px`;
    robot.style.height = `${Math.round(desiredHeight)}px`;

    if (assistantSurface) {
      const chatRect = assistantSurface.getBoundingClientRect();
      const assistantGap = clamp(
        viewportWidth * ROBOT.assistantGapRatio,
        18,
        26,
      );
      robotMetrics = {
        assistant: true,
        dynamic: false,
        baseTop: (
          chatRect.top +
          chatRect.height * ROBOT.assistantVisibleTopWithinChatRatio -
          desiredHeight * ROBOT.assistantVisibleTopInsetRatio
        ),
        maxTop: 0,
        maxScroll: 0,
        left: (
          chatRect.right -
          containerRect.left +
          assistantGap -
          desiredWidth * ROBOT.assistantVisibleLeftInsetRatio
        ),
        containerLeft: containerRect.left,
      };
      return;
    }

    const firstCardLeft = cards.length > 0
      ? Math.max(0, cards[0].getBoundingClientRect().left - containerRect.left)
      : 0;
    const furthestCardRight = cards.reduce((right, card) => {
      const cardRight = card.getBoundingClientRect().right - containerRect.left;
      return Math.max(right, cardRight);
    }, 0);
    const preferredLeft = (container.clientWidth || viewportWidth) * 0.7;
    const preferredVisibleLeft = preferredLeft + desiredWidth * ROBOT.visibleLeftRatio;
    const collisionAdjustment = Math.max(
      0,
      ROBOT.minimumCardGap - (preferredVisibleLeft - furthestCardRight),
    );
    const left = Math.max(
      preferredLeft + collisionAdjustment,
      firstCardLeft + ROBOT.minimumCardGap,
    );
    const baseTop = (ROBOT.baseTopVh / 100) * viewportHeight;
    const overlayBottom = overlayRect.bottom - containerRect.top;
    const maxTop = Math.max(baseTop, overlayBottom - desiredHeight * ROBOT.bottomOverlapRatio);
    const rawMaxScroll = Math.max(0, readScrollHeight() - viewportHeight);
    const maxScroll = rawMaxScroll > ROBOT.scrollActivationThreshold ? rawMaxScroll : 0;

    robotMetrics = {
      assistant: false,
      dynamic: maxScroll > 0,
      baseTop,
      maxTop,
      maxScroll,
      left,
      containerLeft: containerRect.left,
    };
  }

  function robotTranslateForScroll(scrollY) {
    if (!robotMetrics.dynamic) {
      return 0;
    }

    const progress = clamp(scrollY / robotMetrics.maxScroll, 0, 1);
    const top = robotMetrics.baseTop + progress * (robotMetrics.maxTop - robotMetrics.baseTop);
    return top - robotMetrics.baseTop;
  }

  function setRobotTransform(translateY) {
    const transform = `translate3d(0, ${translateY.toFixed(3)}px, 0)`;
    if (transform === lastRobotTransform) {
      return;
    }

    robot.style.transform = transform;
    lastRobotTransform = transform;
  }

  function applyRobotLayout() {
    if (destroyed) {
      return;
    }

    if (isMobile()) {
      robot.style.position = "absolute";
      robot.style.top = `${robotMetrics.baseTop.toFixed(2)}px`;
      robot.style.left = "calc(50% - 3px)";
      robot.style.transition = "none";
      robot.style.willChange = "auto";
      robot.style.transform = "translate3d(-50%, 0, 0)";
      robotTargetTranslateY = 0;
      robotRenderedTranslateY = 0;
      robotLastTimestamp = 0;
      lastRobotTransform = robot.style.transform;
      return;
    }

    if (robotMetrics.assistant) {
      robot.style.position = "fixed";
      robot.style.top = `${robotMetrics.baseTop.toFixed(2)}px`;
      robot.style.left = `${Math.round(robotMetrics.containerLeft + robotMetrics.left)}px`;
      robot.style.transition = "none";
      robot.style.willChange = "transform";
    } else if (!robotMetrics.dynamic) {
      robot.style.position = "fixed";
      robot.style.top = `${ROBOT.baseTopVh}vh`;
      robot.style.left = `${Math.round(robotMetrics.containerLeft + robotMetrics.left)}px`;
      robot.style.transition = "none";
      robot.style.willChange = "transform";
    } else {
      robot.style.position = "absolute";
      robot.style.top = `${robotMetrics.baseTop.toFixed(2)}px`;
      robot.style.left = `${Math.round(robotMetrics.left)}px`;
      robot.style.transition = "none";
      robot.style.willChange = "transform";
    }

    const scrollY = windowRef.scrollY || windowRef.pageYOffset || 0;
    const translateY = robotTranslateForScroll(scrollY);
    robotTargetTranslateY = translateY;
    robotRenderedTranslateY = translateY;
    robotLastTimestamp = 0;
    setRobotTransform(translateY);
  }

  function animateRobotPosition(timestamp) {
    robotFrame = 0;
    if (destroyed || isMobile()) {
      return;
    }

    // Campiona la posizione reale dello scroll a ogni fotogramma. Gli eventi
    // scroll possono essere raggruppati dal browser; leggere qui il valore
    // corrente evita destinazioni stale e piccoli salti tra un evento e l'altro.
    robotTargetTranslateY = robotTranslateForScroll(
      windowRef.scrollY || windowRef.pageYOffset || 0,
    );

    if (prefersReducedMotion()) {
      robotRenderedTranslateY = robotTargetTranslateY;
    } else {
      const elapsed = robotLastTimestamp
        ? Math.min(Math.max(timestamp - robotLastTimestamp, 1), 34)
        : 16.67;
      const smoothing = 1 - Math.exp(-elapsed / ROBOT.scrollSmoothingMs);
      const delta = robotTargetTranslateY - robotRenderedTranslateY;

      robotRenderedTranslateY = Math.abs(delta) <= ROBOT.settleThreshold
        ? robotTargetTranslateY
        : robotRenderedTranslateY + delta * smoothing;
      robotLastTimestamp = timestamp;
    }

    setRobotTransform(robotRenderedTranslateY);

    if (Math.abs(robotTargetTranslateY - robotRenderedTranslateY) > ROBOT.settleThreshold) {
      robotFrame = windowRef.requestAnimationFrame(animateRobotPosition);
    }
  }

  function updateRobotPosition() {
    if (destroyed || isMobile()) {
      return;
    }

    robotTargetTranslateY = robotTranslateForScroll(windowRef.scrollY || windowRef.pageYOffset || 0);
    if (prefersReducedMotion()) {
      robotRenderedTranslateY = robotTargetTranslateY;
      setRobotTransform(robotRenderedTranslateY);
      return;
    }

    if (!robotFrame) {
      robotFrame = windowRef.requestAnimationFrame(animateRobotPosition);
    }
  }

  function revealRobotAtInitialPosition() {
    if (robotInitiallyPositioned) {
      return;
    }
    robotInitiallyPositioned = true;
    robot.classList.remove("is-positioning");
  }

  function scheduleRobotPosition() {
    if (destroyed || documentRef.hidden) {
      return;
    }
    updateRobotPosition();
  }

  function hasNetworkLayoutChanged() {
    const cards = cardElements();
    const changed = !networkLayout ||
      networkLayout.width !== canvasWidth ||
      networkLayout.height !== canvasHeight ||
      networkLayout.cardsBottomY !== cardsBottomY ||
      networkLayout.gapCloseY !== gapCloseY ||
      networkLayout.cards.length !== cards.length ||
      networkLayout.cards.some((card, index) => card !== cards[index]);

    networkLayout = {
      width: canvasWidth,
      height: canvasHeight,
      cardsBottomY,
      gapCloseY,
      cards,
    };

    return changed;
  }

  function refresh() {
    if (destroyed) {
      return;
    }

    if (refreshFrame) {
      windowRef.cancelAnimationFrame(refreshFrame);
      refreshFrame = 0;
    }
    if (postPaintRefreshFrame) {
      windowRef.cancelAnimationFrame(postPaintRefreshFrame);
      postPaintRefreshFrame = 0;
    }

    resizeCanvas();
    measureGap();
    const layoutChanged = hasNetworkLayoutChanged();
    if (canvasWidth <= 0 || canvasHeight <= 0) {
      stopAnimation();
      particles = [];
      refreshRobotMetrics();
      applyRobotLayout();
      revealRobotAtInitialPosition();
      return;
    }

    if (layoutChanged) {
      stopAnimation();
      initialiseParticles();
      drawFrame();
    }

    refreshRobotMetrics();
    applyRobotLayout();
    revealRobotAtInitialPosition();
    startAnimation();
  }

  function scheduleRefresh() {
    if (destroyed || refreshFrame || postPaintRefreshFrame) {
      return;
    }
    refreshFrame = windowRef.requestAnimationFrame(() => {
      refreshFrame = 0;
      postPaintRefreshFrame = windowRef.requestAnimationFrame(() => {
        postPaintRefreshFrame = 0;
        refresh();
      });
    });
  }

  function onVisibilityChange() {
    if (documentRef.hidden) {
      clearNetworkResumeTimer();
      networkPausedForScroll = false;
      stopAnimation();
      return;
    }
    refresh();
  }

  function onMotionPreferenceChange() {
    if (destroyed) {
      return;
    }
    updateRobotPosition();
  }

  function addMediaListener(mediaQuery, listener) {
    if (!mediaQuery) {
      return;
    }
    if (typeof mediaQuery.addEventListener === "function") {
      mediaQuery.addEventListener("change", listener);
    } else if (typeof mediaQuery.addListener === "function") {
      mediaQuery.addListener(listener);
    }
  }

  function removeMediaListener(mediaQuery, listener) {
    if (!mediaQuery) {
      return;
    }
    if (typeof mediaQuery.removeEventListener === "function") {
      mediaQuery.removeEventListener("change", listener);
    } else if (typeof mediaQuery.removeListener === "function") {
      mediaQuery.removeListener(listener);
    }
  }

  function onWindowResize() {
    scheduleRefresh();
  }

  function onWindowScroll() {
    if (!isMobile()) {
      scheduleRobotPosition();
    }
    pauseNetworkForScroll();
  }

  windowRef.addEventListener("resize", onWindowResize, { passive: true });
  windowRef.addEventListener("scroll", onWindowScroll, { passive: true });
  documentRef.addEventListener("visibilitychange", onVisibilityChange);
  addMediaListener(reducedMotionMedia, onMotionPreferenceChange);
  addMediaListener(mobileMedia, scheduleRefresh);
  addMediaListener(phoneMedia, scheduleRefresh);
  robotImage?.addEventListener("load", scheduleRefresh);

  if (typeof ResizeObserverConstructor === "function") {
    resizeObserver = new ResizeObserverConstructor(scheduleRefresh);
    resizeObserver.observe(container);
    resizeObserver.observe(overlay);
    if (assistantChat) resizeObserver.observe(assistantChat);
  }

  if (documentRef.fonts && documentRef.fonts.ready) {
    documentRef.fonts.ready.then(() => {
      if (!destroyed) {
        scheduleRefresh();
      }
    });
  }

  refresh();

  return {
    refresh,
    destroy() {
      if (destroyed) {
        return;
      }
      destroyed = true;
      stopAnimation();

      if (refreshFrame) {
        windowRef.cancelAnimationFrame(refreshFrame);
        refreshFrame = 0;
      }
      if (postPaintRefreshFrame) {
        windowRef.cancelAnimationFrame(postPaintRefreshFrame);
        postPaintRefreshFrame = 0;
      }
      if (robotFrame) {
        windowRef.cancelAnimationFrame(robotFrame);
        robotFrame = 0;
      }
      clearNetworkResumeTimer();
      if (resizeObserver) {
        resizeObserver.disconnect();
      }

      windowRef.removeEventListener("resize", onWindowResize);
      windowRef.removeEventListener("scroll", onWindowScroll);
      documentRef.removeEventListener("visibilitychange", onVisibilityChange);
      removeMediaListener(reducedMotionMedia, onMotionPreferenceChange);
      removeMediaListener(mobileMedia, scheduleRefresh);
      removeMediaListener(phoneMedia, scheduleRefresh);
      robotImage?.removeEventListener("load", scheduleRefresh);

      robot.style.position = originalRobotStyles.position;
      robot.style.top = originalRobotStyles.top;
      robot.style.left = originalRobotStyles.left;
      robot.style.width = originalRobotStyles.width;
      robot.style.height = originalRobotStyles.height;
      robot.style.transition = originalRobotStyles.transition;
      robot.style.transform = originalRobotStyles.transform;
      robot.style.willChange = originalRobotStyles.willChange;
      robot.className = originalRobotClassName;

      if (originalCanvasAttributes.width === null) {
        canvas.removeAttribute("width");
      } else {
        canvas.setAttribute("width", originalCanvasAttributes.width);
      }
      if (originalCanvasAttributes.height === null) {
        canvas.removeAttribute("height");
      } else {
        canvas.setAttribute("height", originalCanvasAttributes.height);
      }
    },
  };
}
