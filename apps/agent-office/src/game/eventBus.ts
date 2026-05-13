import type { AgentId } from "../data/officeData";

export type OfficeGameEvent =
  | {
      type: "step.changed";
      stepIndex: number;
    }
  | {
      type: "agent.selected";
      agentId: AgentId;
      openDrawer?: boolean;
    }
  | {
      type: "agent.hovered";
      agentId: AgentId | null;
    }
  | {
      type: "approval.changed";
      approved: boolean;
    };

type Listener = (event: OfficeGameEvent) => void;

const listeners = new Set<Listener>();

export const officeGameBus = {
  emit(event: OfficeGameEvent) {
    listeners.forEach((listener) => listener(event));
  },
  subscribe(listener: Listener) {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
  clear() {
    listeners.clear();
  },
};
