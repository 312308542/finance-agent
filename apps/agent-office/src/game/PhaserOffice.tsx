import React from "react";
import * as Phaser from "phaser";
import { financeOfficeGameConfig } from "./FinanceOfficeScene";
import { officeGameBus } from "./eventBus";

type PhaserOfficeProps = {
  stepIndex: number;
  approved: boolean;
};

export function PhaserOffice({ stepIndex, approved }: PhaserOfficeProps) {
  const gameRef = React.useRef<Phaser.Game | null>(null);

  React.useEffect(() => {
    if (gameRef.current) {
      return;
    }

    gameRef.current = new Phaser.Game(financeOfficeGameConfig);

    return () => {
      officeGameBus.clear();
      gameRef.current?.destroy(true);
      gameRef.current = null;
    };
  }, []);

  React.useEffect(() => {
    officeGameBus.emit({ type: "step.changed", stepIndex });
  }, [stepIndex]);

  React.useEffect(() => {
    officeGameBus.emit({ type: "approval.changed", approved });
  }, [approved]);

  return <div id="phaser-office-root" className="phaser-office-root" />;
}
