import * as Phaser from "phaser";
import {
  agents,
  positionsByStep,
  steps,
  zones,
  type Agent,
  type AgentId,
  type Point,
  type Zone,
} from "../data/officeData";
import { agentSpriteAssets, allSpritesheetAssets, officeImageAssets } from "./assetsManifest";
import { officeGameBus, type OfficeGameEvent } from "./eventBus";
import { officeFont, operatingStats, tokenFlows, toneColors } from "./officeMapConfig";
import { buildWaypointRoute } from "./routePlanner";
import { createInitialWorldState, getSessionForStep, sessionLabels, type OfficeWorldState } from "./worldState";
import { officeWorld, zoneAgentMap } from "./worldConfig";

type AgentActor = {
  agent: Agent;
  container: Phaser.GameObjects.Container;
  sprite: Phaser.GameObjects.Sprite;
  badge: Phaser.GameObjects.Container;
  labelBg: Phaser.GameObjects.Graphics;
  label: Phaser.GameObjects.Text;
  status: Phaser.GameObjects.Text;
  halo: Phaser.GameObjects.Arc;
  currentPoint: Point;
};

type TokenActor = {
  container: Phaser.GameObjects.Container;
  bg: Phaser.GameObjects.Rectangle;
  label: Phaser.GameObjects.Text;
  baseColor: number;
};

export class FinanceOfficeScene extends Phaser.Scene {
  private stepIndex = 0;
  private approved = false;
  private selectedAgent: AgentId = "lead";
  private worldState: OfficeWorldState = createInitialWorldState(agents.map((agent) => agent.id));
  private actorMap = new Map<AgentId, AgentActor>();
  private zoneMap = new Map<Zone["id"], Phaser.GameObjects.Container>();
  private tokenMap = new Map<string, TokenActor>();
  private focusSpot?: Phaser.GameObjects.Ellipse;
  private approvalGlow?: Phaser.GameObjects.Sprite;
  private riskBug?: Phaser.GameObjects.Sprite;
  private roomShade?: Phaser.GameObjects.Rectangle;
  private serverroom?: Phaser.GameObjects.Sprite;
  private hudSession?: Phaser.GameObjects.Text;
  private hudStep?: Phaser.GameObjects.Text;
  private hudApproval?: Phaser.GameObjects.Text;
  private unsubscribe?: () => void;

  constructor() {
    super("FinanceOfficeScene");
  }

  preload() {
    Object.values(officeImageAssets).forEach((asset) => {
      this.load.image(asset.key, asset.path);
    });

    allSpritesheetAssets.forEach((asset) => {
      this.load.spritesheet(asset.key, asset.path, { frameWidth: asset.frameWidth, frameHeight: asset.frameHeight });
    });
  }

  create() {
    this.cameras.main.setBackgroundColor("#1a1a2e");
    this.drawRoom();
    this.createAnimations();
    this.drawAmbientSprites();
    this.drawZones();
    this.drawTokens();
    this.drawAgents();
    this.drawOperatingHud();
    this.applyStep(0, true);

    this.unsubscribe = officeGameBus.subscribe((event) => this.handleBusEvent(event));
    this.events.once(Phaser.Scenes.Events.SHUTDOWN, () => {
      this.unsubscribe?.();
    });
  }

  private handleBusEvent(event: OfficeGameEvent) {
    if (event.type === "step.changed") {
      this.applyStep(event.stepIndex);
      return;
    }

    if (event.type === "agent.selected") {
      this.selectedAgent = event.agentId;
      this.worldState.selectedAgent = event.agentId;
      this.updateAgentFocus();
      return;
    }

    if (event.type === "approval.changed") {
      this.approved = event.approved;
      this.worldState.approved = event.approved;
      this.updateApprovalLight();
    }
  }

  private drawRoom() {
    this.add.image(officeWorld.stage.width / 2, officeWorld.stage.height / 2, officeImageAssets.background.key).setDepth(0);

    this.roomShade = this.add
      .rectangle(officeWorld.stage.width / 2, officeWorld.stage.height / 2, officeWorld.stage.width, officeWorld.stage.height, 0x050914, 0.12)
      .setDepth(1);

    const plaqueBg = this.add
      .rectangle(officeWorld.stage.width / 2, officeWorld.stage.height - 38, 510, 46, 0x5d4037, 0.92)
      .setStrokeStyle(4, 0x3e2723);
    const plaqueText = this.add
      .text(officeWorld.stage.width / 2, officeWorld.stage.height - 38, "Hermes 金融 Agent 办公室", {
        fontFamily: officeFont,
        fontSize: "18px",
        color: "#ffd700",
        stroke: "#000000",
        strokeThickness: 3,
      })
      .setOrigin(0.5);
    this.add.container(0, 0, [plaqueBg, plaqueText]).setDepth(31);
  }

  private createAnimations() {
    const createLoop = (key: string, texture: string, frameRate: number) => {
      const frameTotal = Math.max(1, this.textures.get(texture).frameTotal - 2);
      if (this.anims.exists(key)) {
        this.anims.remove(key);
      }
      this.anims.create({
        key,
        frames: this.anims.generateFrameNumbers(texture, { start: 0, end: frameTotal }),
        frameRate,
        repeat: -1,
      });
    };

    createLoop("coffee_machine", "coffee_machine", 12);
    createLoop("serverroom_on", "serverroom", 6);
    createLoop("sync_anim", "sync_anim", 12);
    createLoop("risk_bug", "error_bug", 12);

    Object.values(agentSpriteAssets).forEach((asset) => {
      createLoop(`${asset.key}_walk`, asset.key, 6);
    });
  }

  private drawAmbientSprites() {
    let posterFrame = 7;
    const poster = this.add.sprite(252, 66, "posters", 7).setOrigin(0.5).setDepth(4).setInteractive({ useHandCursor: true });
    poster.on("pointerdown", () => {
      posterFrame = (posterFrame + 1) % 32;
      poster.setFrame(posterFrame);
    });

    [
      [565, 178, 4],
      [230, 185, 7],
      [977, 496, 11],
    ].forEach(([x, y, frame]) => {
      const plant = this.add.sprite(x, y, "plants", frame).setOrigin(0.5).setDepth(5).setInteractive({ useHandCursor: true });
      plant.on("pointerdown", () => plant.setFrame(Phaser.Math.Between(0, 15)));
    });

    const coffeeShadow = this.add.image(659, 397, officeImageAssets.coffeeShadow.key).setOrigin(0.5).setDepth(98);
    coffeeShadow.setAlpha(0.76);
    this.add.sprite(659, 397, "coffee_machine").setOrigin(0.5).setDepth(99).play("coffee_machine");

    this.serverroom = this.add.sprite(1021, 142, "serverroom", 0).setOrigin(0.5).setDepth(2);
    this.serverroom.setAlpha(0.92);

    const desk = this.add.image(218, 417, officeImageAssets.desk.key).setOrigin(0.5).setDepth(70);
    desk.setAlpha(0.96);

    this.approvalGlow = this.add.sprite(1157, 592, "sync_anim", 0).setOrigin(0.5).setDepth(40);
    this.approvalGlow.setAlpha(0.45);

    this.riskBug = this.add.sprite(1007, 221, "error_bug", 0).setOrigin(0.5).setScale(0.74).setDepth(50).setVisible(false);

    const cat = this.add.sprite(94, 557, "cats", 5).setOrigin(0.5).setDepth(200).setInteractive({ useHandCursor: true });
    cat.on("pointerdown", () => cat.setFrame(Phaser.Math.Between(0, 15)));
  }

  private drawZones() {
    this.focusSpot = this.add.ellipse(0, 0, 190, 96, 0xf59e0b, 0.12).setDepth(10).setVisible(false);

    zones.forEach((zone) => {
      const rect = this.zoneToRect(zone);
      const container = this.add.container(rect.x, rect.y).setDepth(11);

      const hitArea = this.add.rectangle(0, 0, rect.w, rect.h, 0xffffff, 0.001).setOrigin(0);

      container.add(hitArea);
      container.setSize(rect.w, rect.h);
      container.setInteractive(new Phaser.Geom.Rectangle(0, 0, rect.w, rect.h), Phaser.Geom.Rectangle.Contains);
      container.on("pointerdown", () => {
        officeGameBus.emit({
          type: "agent.selected",
          agentId: this.focusAgentForZone(zone.id),
          openDrawer: false,
        });
      });
      this.zoneMap.set(zone.id, container);
    });
  }

  private drawTokens() {
    tokenFlows.forEach((flow) => {
      const [x, y] = flow.from;
      const bg = this.add.rectangle(0, 0, 76, 30, flow.color, 0.95).setStrokeStyle(3, 0x0f172a);
      const text = this.add.text(0, -1, flow.label, {
        fontFamily: officeFont,
        fontSize: "12px",
        color: "#0f172a",
        fontStyle: "bold",
      }).setOrigin(0.5);
      const token = this.add.container(x, y, [bg, text]).setAlpha(0).setDepth(155);
      this.tokenMap.set(flow.id, { container: token, bg, label: text, baseColor: flow.color });
    });
  }

  private drawAgents() {
    agents.forEach((agent) => {
      const position = this.pointToPixel(agent.home);
      const actor = this.createAgentActor(agent, position.x, position.y);
      this.actorMap.set(agent.id, actor);
    });
  }

  private createAgentActor(agent: Agent, x: number, y: number): AgentActor {
    const container = this.add.container(x, y).setDepth(120);
    const spriteKey = agentSpriteAssets[agent.id].key;
    const haloColor = toneColors[agent.tone];
    const halo = this.add.circle(0, 14, 28, haloColor, 0.1).setStrokeStyle(2, haloColor, 0.34);
    const shadow = this.add.ellipse(0, 28, 48, 12, 0x000000, 0.3);
    const sprite = this.add.sprite(0, 0, spriteKey, 0).setScale(2.15).setOrigin(0.5, 0.72);
    sprite.play(`${spriteKey}_walk`);

    const labelBg = this.add.graphics();
    labelBg.fillStyle(0x0b1220, 0.88);
    labelBg.lineStyle(2, haloColor, 0.44);
    labelBg.fillRoundedRect(-48, 38, 96, 24, 4);
    labelBg.strokeRoundedRect(-48, 38, 96, 24, 4);
    const label = this.add.text(0, 41, agent.name, {
      fontFamily: officeFont,
      fontSize: "11px",
      color: "#ffffff",
      stroke: "#000000",
      strokeThickness: 2,
    }).setOrigin(0.5);
    const status = this.add.text(0, 68, agent.status, {
      fontFamily: officeFont,
      fontSize: "9px",
      color: "#cbd5e1",
    }).setOrigin(0.5);
    status.setVisible(false);
    const badge = this.add.container(0, 0, [labelBg, label]);

    container.add([halo, shadow, sprite, badge, status]);
    container.setSize(112, 126);
    container.setInteractive(new Phaser.Geom.Rectangle(-56, -62, 112, 126), Phaser.Geom.Rectangle.Contains);
    container.on("pointerdown", () => {
      officeGameBus.emit({ type: "agent.selected", agentId: agent.id, openDrawer: true });
    });
    container.on("pointerover", () => {
      officeGameBus.emit({ type: "agent.hovered", agentId: agent.id });
    });
    container.on("pointerout", () => {
      officeGameBus.emit({ type: "agent.hovered", agentId: null });
    });

    this.tweens.add({
      targets: sprite,
      y: -4,
      duration: 480,
      yoyo: true,
      repeat: -1,
      ease: "Stepped",
      easeParams: [3],
    });

    return {
      agent,
      container,
      sprite,
      labelBg,
      label,
      status,
      badge,
      halo,
      currentPoint: { ...agent.home },
    };
  }

  private drawOperatingHud() {
    const container = this.add.container(officeWorld.stage.width - 560, 18).setDepth(300);
    const bg = this.add.rectangle(0, 0, 532, 48, 0x111827, 0.88).setOrigin(0).setStrokeStyle(3, 0x64477d);
    container.add(bg);

    const sessionPill = this.add.rectangle(13, 12, 116, 24, 0x0b1220, 0.92).setOrigin(0).setStrokeStyle(2, 0x334155);
    this.hudSession = this.add.text(23, 18, sessionLabels[this.worldState.session], {
      fontFamily: officeFont,
      fontSize: "10px",
      color: "#f8fafc",
    });
    this.hudStep = this.add.text(142, 10, steps[this.stepIndex]?.name ?? "数据刷新", {
      fontFamily: officeFont,
      fontSize: "13px",
      color: "#f8fafc",
      stroke: "#000000",
      strokeThickness: 2,
    });
    this.hudApproval = this.add.text(142, 26, "审批: 待确认", {
      fontFamily: officeFont,
      fontSize: "10px",
      color: "#cbd5e1",
    });
    container.add([sessionPill, this.hudSession, this.hudStep, this.hudApproval]);

    let offsetX = 290;
    operatingStats.forEach((stat) => {
      const color = toneColors[stat.tone];
      const dot = this.add.rectangle(offsetX, 17, 9, 9, color, 1).setOrigin(0);
      const label = this.add.text(offsetX + 15, 10, stat.label, {
        fontFamily: officeFont,
        fontSize: "10px",
        color: "#cbd5e1",
      });
      const value = this.add.text(offsetX + 15, 25, stat.value, {
        fontFamily: officeFont,
        fontSize: "13px",
        color: this.colorToCss(color),
        stroke: "#000000",
        strokeThickness: 2,
      });
      container.add([dot, label, value]);
      offsetX += stat.width;
    });
  }

  private applyStep(stepIndex: number, instant = false) {
    this.stepIndex = stepIndex;
    this.worldState.stepIndex = stepIndex;
    this.worldState.session = getSessionForStep(stepIndex);
    const step = steps[stepIndex];
    const speakers = new Set(step.messages.map((message) => message.agent));

    this.zoneMap.forEach((zoneContainer, zoneId) => {
      const zone = zones.find((item) => item.id === zoneId);
      const color = zone ? toneColors[zone.tone] : 0xffffff;
      zoneContainer.setAlpha(1);
      this.tweens.killTweensOf(zoneContainer);
      zoneContainer.setScale(1);
      if (zoneId === step.focus && zone && this.focusSpot) {
        const rect = this.zoneToRect(zone);
        this.focusSpot.setPosition(rect.x + rect.w / 2, rect.y + rect.h / 2);
        this.focusSpot.setSize(Math.min(230, rect.w * 0.8), Math.min(112, rect.h * 0.62));
        this.focusSpot.setFillStyle(color, 0.14);
        this.focusSpot.setVisible(true);
        this.tweens.killTweensOf(this.focusSpot);
        this.tweens.add({
          targets: this.focusSpot,
          alpha: 0.28,
          duration: 700,
          yoyo: true,
          repeat: -1,
          ease: "Sine.easeInOut",
        });
      }
    });

    this.actorMap.forEach((actor, agentId) => {
      const point = positionsByStep[stepIndex]?.[agentId] ?? actor.agent.home;
      const action = speakers.has(agentId) ? "working" : this.worldState.session === "cryptoWatch" && agentId === "draft" ? "working" : "idle";
      this.worldState.agents[agentId] = {
        ...this.worldState.agents[agentId],
        action,
        target: point,
      };
      this.moveActor(actor, point, instant);
      actor.status.setText(this.getAgentStatusLine(actor.agent.id, speakers.has(agentId), step.name));
      actor.container.setAlpha(speakers.has(agentId) || this.selectedAgent === agentId ? 1 : 0.68);
    });

    this.animateToken(stepIndex);
    this.updateMachineState();
    this.updateWorldSessionVisuals();
    this.updateOperatingHudState();
    this.updateAgentFocus();
    this.updateApprovalLight();
  }

  private moveActor(actor: AgentActor, point: Point, instant: boolean) {
    const route = buildWaypointRoute(actor.currentPoint, point);
    const finalPosition = this.pointToPixel(point);
    this.tweens.killTweensOf(actor.container);

    if (instant) {
      actor.container.setPosition(finalPosition.x, finalPosition.y);
      this.setActorFacing(actor, point.face);
      actor.currentPoint = { ...point };
      return;
    }

    const pixelRoute = route.map((item) => this.pointToPixel(item));
    if (pixelRoute.length <= 1) {
      actor.container.setPosition(finalPosition.x, finalPosition.y);
      this.setActorFacing(actor, point.face);
      actor.currentPoint = { ...point };
      return;
    }

    const moveSegment = (index: number) => {
      if (index >= pixelRoute.length) {
        this.setActorFacing(actor, point.face);
        actor.currentPoint = { ...point };
        return;
      }

      const previous = pixelRoute[index - 1];
      const next = pixelRoute[index];
      this.setActorFacing(actor, next.x < previous.x ? "left" : "right");
      this.tweens.add({
        targets: actor.container,
        x: next.x,
        y: next.y,
        duration: Math.max(180, Math.floor(920 / (pixelRoute.length - 1))),
        ease: "Sine.easeInOut",
        onComplete: () => moveSegment(index + 1),
      });
    };

    moveSegment(1);
  }

  private animateToken(stepIndex: number) {
    this.tokenMap.forEach((token) => {
      token.container.setAlpha(0);
      token.bg.setFillStyle(token.baseColor, 0.95);
      this.tweens.killTweensOf(token.container);
    });

    const flow = tokenFlows[stepIndex] ?? tokenFlows[0];
    const token = this.tokenMap.get(flow.id);
    if (!token) {
      return;
    }

    const [fromX, fromY] = flow.from;
    const [toX, toY] = flow.to;
    token.container.setAlpha(0.95);
    token.container.setPosition(fromX, fromY);
    this.tweens.add({
      targets: token.container,
      x: toX,
      y: toY,
      duration: 1450,
      repeat: -1,
      ease: "Stepped",
      easeParams: [8],
    });
  }

  private updateMachineState() {
    if (this.serverroom) {
      if (this.worldState.session === "marketOpen" || this.worldState.session === "incidentMode") {
        this.serverroom.play("serverroom_on", true);
      } else {
        this.serverroom.stop();
        this.serverroom.setFrame(0);
      }
    }

    if (this.riskBug) {
      const showRisk = this.worldState.session === "incidentMode";
      this.riskBug.setVisible(showRisk);
      if (showRisk) {
        this.riskBug.play("risk_bug", true);
      } else {
        this.riskBug.stop();
      }
    }
  }

  private updateAgentFocus() {
    const speakers = this.currentSpeakerIds();
    this.actorMap.forEach((actor, agentId) => {
      const focused = agentId === this.selectedAgent;
      const active = speakers.has(agentId);
      const color = toneColors[actor.agent.tone];
      actor.halo.setAlpha(focused || active ? 0.28 : 0.08);
      actor.label.setAlpha(focused ? 1 : 0.86);
      actor.status.setVisible(focused);
      actor.status.setAlpha(focused ? 0.9 : 0);
      actor.badge.setVisible(focused || active);
      actor.badge.setAlpha(focused || active ? 0.92 : 0);
      actor.container.setScale(focused ? 1.04 : 1);
      actor.container.setDepth(focused || active ? 260 : 120 + Math.floor(actor.container.y / 12));
    });
  }

  private updateApprovalLight() {
    if (!this.approvalGlow) {
      return;
    }

    if (this.approved || this.worldState.session === "cryptoWatch") {
      this.approvalGlow.setAlpha(this.approved ? 0.92 : 0.52);
      this.approvalGlow.play("sync_anim", true);
    } else {
      this.approvalGlow.stop();
      this.approvalGlow.setFrame(0);
      this.approvalGlow.setAlpha(0.2);
    }

    const token = this.tokenMap.get("token-order");
    if (token) {
      token.bg.setFillStyle(this.approved ? 0xbbf7d0 : token.baseColor, 1);
      if (this.approved) {
        token.container.setAlpha(1);
      }
    }
  }

  private currentSpeakerIds() {
    return new Set(steps[this.stepIndex].messages.map((message) => message.agent));
  }

  private focusAgentForZone(zoneId: string): AgentId {
    return zoneAgentMap[zoneId as Zone["id"]] ?? "lead";
  }

  private pointToPixel(point: Point) {
    return {
      x: (point.x / 100) * officeWorld.stage.width,
      y: (point.y / 100) * officeWorld.stage.height,
    };
  }

  private zoneToRect(zone: Zone) {
    return {
      x: (zone.x / 100) * officeWorld.stage.width,
      y: (zone.y / 100) * officeWorld.stage.height,
      w: (zone.w / 100) * officeWorld.stage.width,
      h: (zone.h / 100) * officeWorld.stage.height,
    };
  }

  private setActorFacing(actor: AgentActor, face?: Point["face"]) {
    const scale = face === "left" ? -2.15 : 2.15;
    actor.sprite.setScale(scale, 2.15);
  }

  private updateWorldSessionVisuals() {
    if (!this.roomShade) {
      return;
    }

    const alphaMap = {
      marketOpen: 0.12,
      marketClosed: 0.2,
      cryptoWatch: 0.18,
      offDuty: 0.24,
      nightAudit: 0.22,
      incidentMode: 0.18,
    } satisfies Record<OfficeWorldState["session"], number>;

    const fillMap = {
      marketOpen: 0x050914,
      marketClosed: 0x06111c,
      cryptoWatch: 0x06111c,
      offDuty: 0x050914,
      nightAudit: 0x08131f,
      incidentMode: 0x1a0b0b,
    } satisfies Record<OfficeWorldState["session"], number>;

    this.roomShade.setFillStyle(fillMap[this.worldState.session], alphaMap[this.worldState.session]);
  }

  private updateOperatingHudState() {
    this.hudSession?.setText(sessionLabels[this.worldState.session]);
    this.hudStep?.setText(steps[this.stepIndex]?.name ?? "数据刷新");
    this.hudApproval?.setText(this.approved ? "审批: Hermes 已接收" : "审批: 待确认");
  }

  private getAgentStatusLine(agentId: AgentId, active: boolean, stepName: string) {
    if (active) {
      return `处理中: ${stepName}`;
    }

    if (this.worldState.session === "incidentMode" && agentId === "risk") {
      return "风险事件处理中";
    }

    if (this.worldState.session === "cryptoWatch" && agentId === "draft") {
      return "等待最终确认";
    }

    return agents.find((item) => item.id === agentId)?.status ?? "";
  }

  private colorToCss(color: number) {
    return `#${color.toString(16).padStart(6, "0")}`;
  }
}

export const financeOfficeGameConfig: Phaser.Types.Core.GameConfig = {
  type: Phaser.AUTO,
  parent: "phaser-office-root",
  width: officeWorld.stage.width,
  height: officeWorld.stage.height,
  backgroundColor: "#1a1a2e",
  pixelArt: true,
  roundPixels: true,
  scale: {
    mode: Phaser.Scale.FIT,
    autoCenter: Phaser.Scale.CENTER_BOTH,
  },
  scene: [FinanceOfficeScene],
};
