import * as React from "react";
import { Button, Input } from "antd";
import { ChevronRight, MessageSquareText, X } from "lucide-react";
import type { ChatLine } from "../consoleTypes";

export function ChatDock({
  chatLines,
  message,
  setMessage,
  sendMessage,
  open,
  setOpen,
  streaming,
  status,
}: {
  chatLines: ChatLine[];
  message: string;
  setMessage: React.Dispatch<React.SetStateAction<string>>;
  sendMessage: () => Promise<void>;
  open: boolean;
  setOpen: React.Dispatch<React.SetStateAction<boolean>>;
  streaming: boolean;
  status: string;
}) {
  const logRef = React.useRef<HTMLDivElement | null>(null);
  const canSend = message.trim().length > 0 && !streaming;
  const activeAssistantId = [...chatLines]
    .reverse()
    .find((line) => line.role === "assistant")?.id;

  React.useEffect(() => {
    if (!open) {
      return;
    }
    logRef.current?.scrollTo({
      top: logRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [chatLines, open, status]);

  return (
    <aside className={open ? "chat-dock is-open" : "chat-dock"} aria-live="polite">
      {open ? (
        <section className="chat-window" role="dialog" aria-label="金融 Agent 聊天窗口">
          <header className="chat-window-head">
            <div className="chat-title">
              <span className="chat-avatar">
                <MessageSquareText size={18} />
              </span>
              <div>
                <strong>金融 Agent</strong>
                <span>{streaming ? "正在生成回复" : "随时待命"}</span>
              </div>
            </div>
            <div className="chat-head-actions">
              <span className={streaming ? "chat-status is-active" : "chat-status"}>{status}</span>
              <button
                className="icon-button"
                type="button"
                aria-label="收起聊天窗口"
                title="收起"
                onClick={() => setOpen(false)}
              >
                <X size={16} />
              </button>
            </div>
          </header>

          <div className="chat-log" ref={logRef}>
            {chatLines.map((line) => {
              const isLastTypingAssistant =
                streaming &&
                line.role === "assistant" &&
                line.id === activeAssistantId;
              return (
              <div
                key={line.id}
                className={`chat-message chat-message-${line.role}${
                  isLastTypingAssistant ? " is-typing" : ""
                }`}
              >
                <span>
                  {line.role === "user"
                    ? "你"
                    : line.role === "error"
                      ? "错误"
                      : line.role === "status"
                        ? (line.label ?? "流程")
                        : "Agent"}
                </span>
                <p>
                  {line.content ? (
                    <>
                      {line.content}
                      {isLastTypingAssistant ? (
                        <span className="typewriter-cursor" aria-hidden="true" />
                      ) : null}
                    </>
                  ) :
                    (streaming && line.role === "assistant" ? (
                      <span className="typing-dots" aria-label="正在生成">
                        <i />
                        <i />
                        <i />
                      </span>
                    ) : null)}
                </p>
              </div>
              );
            })}
          </div>

          <form
            className="chat-compose"
            onSubmit={(event) => {
              event.preventDefault();
              if (canSend) {
                void sendMessage();
              }
            }}
          >
            <Input
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="问：有哪些适合买入？"
              disabled={streaming}
              onPressEnter={() => {
                if (canSend) {
                  void sendMessage();
                }
              }}
            />
            <Button type="primary" disabled={!canSend} loading={streaming} icon={<ChevronRight size={15} />}>
              {streaming ? "生成中" : "发送"}
            </Button>
          </form>
        </section>
      ) : (
        <button className="chat-launcher" type="button" onClick={() => setOpen(true)}>
          <MessageSquareText size={18} />
          <span>Agent</span>
        </button>
      )}
    </aside>
  );
}
