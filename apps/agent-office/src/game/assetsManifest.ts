import type { AgentId } from "../data/officeData";

export type SpritesheetAsset = {
  key: string;
  path: string;
  frameWidth: number;
  frameHeight: number;
};

export type ImageAsset = {
  key: string;
  path: string;
};

const staticPath = (name: string) => `/static/${name}`;

export const officeImageAssets = {
  background: { key: "office_bg", path: staticPath("office_bg_small.webp") },
  desk: { key: "desk_v3", path: staticPath("desk-v3.webp") },
  coffeeShadow: { key: "coffee_shadow", path: staticPath("coffee-machine-shadow-v1.png") },
} satisfies Record<string, ImageAsset>;

export const officeSpritesheetAssets = {
  coffeeMachine: {
    key: "coffee_machine",
    path: staticPath("coffee-machine-v3-grid.webp"),
    frameWidth: 230,
    frameHeight: 230,
  },
  serverRoom: {
    key: "serverroom",
    path: staticPath("serverroom-spritesheet.webp"),
    frameWidth: 180,
    frameHeight: 251,
  },
  sync: {
    key: "sync_anim",
    path: staticPath("sync-animation-v3-grid.webp"),
    frameWidth: 256,
    frameHeight: 256,
  },
  riskBug: {
    key: "error_bug",
    path: staticPath("error-bug-spritesheet-grid.webp"),
    frameWidth: 220,
    frameHeight: 220,
  },
  cats: {
    key: "cats",
    path: staticPath("cats-spritesheet.webp"),
    frameWidth: 160,
    frameHeight: 160,
  },
  plants: {
    key: "plants",
    path: staticPath("plants-spritesheet.webp"),
    frameWidth: 160,
    frameHeight: 160,
  },
  posters: {
    key: "posters",
    path: staticPath("posters-spritesheet.webp"),
    frameWidth: 160,
    frameHeight: 160,
  },
} satisfies Record<string, SpritesheetAsset>;

export const agentSpriteAssets: Record<AgentId, SpritesheetAsset> = {
  data: { key: "guest_anim_1", path: staticPath("guest_anim_1.webp"), frameWidth: 32, frameHeight: 32 },
  signal: { key: "guest_anim_2", path: staticPath("guest_anim_2.webp"), frameWidth: 32, frameHeight: 32 },
  risk: { key: "guest_anim_3", path: staticPath("guest_anim_3.webp"), frameWidth: 32, frameHeight: 32 },
  research: { key: "guest_anim_4", path: staticPath("guest_anim_4.webp"), frameWidth: 32, frameHeight: 32 },
  draft: { key: "guest_anim_5", path: staticPath("guest_anim_5.webp"), frameWidth: 32, frameHeight: 32 },
  lead: { key: "guest_anim_6", path: staticPath("guest_anim_6.webp"), frameWidth: 32, frameHeight: 32 },
};

export const allSpritesheetAssets = [...Object.values(officeSpritesheetAssets), ...Object.values(agentSpriteAssets)];
