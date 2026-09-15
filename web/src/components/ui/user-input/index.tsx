import { useEffect, useRef } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { useUserInputStore } from "@/stores/user";
import styles from "./index.module.scss";
import { Button } from "@/components/shadcn/button";

interface UserInputProps {
  /** 发送一条消息 */
  onSend: (text: string) => void;
  /** 是否有请求正在运行 */
  running: boolean;
}

export default function UserInput({ onSend, running }: UserInputProps) {
  const ref = useRef<HTMLDivElement>(null);
  const text = useUserInputStore((state) => state.text);
  const setText = useUserInputStore((state) => state.setText);
  const clear = useUserInputStore((state) => state.clear);

  // 用户打字时，把 DOM 内容同步进 store
  const handleInput = (event: FormEvent<HTMLDivElement>) => {
    setText(event.currentTarget.textContent ?? "");
  };

  // 空文本或运行中不发送；发送后清空输入框
  const submit = () => {
    if (!text.trim() || running) {
      return;
    }
    onSend(text);
    clear();
  };

  // Enter 发送，Shift+Enter 换行
  const onEnter = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  // 外部（store）修改 text 时写回 DOM；一致时跳过，避免打字时光标跳动
  useEffect(() => {
    const el = ref.current;
    if (el && el.textContent !== text) {
      el.textContent = text;
    }
  }, [text]);

  return (
    <div className={styles.userInputContainer}>
      <div
        className={styles.userInputTextArea}
        ref={ref}
        contentEditable
        onInput={handleInput}
        onKeyDown={onEnter}
        suppressContentEditableWarning
      />
      <div className={styles.userInputHandleArea}>
        <div className={styles.userInputHandleAreaLeft}></div>
        <div className={styles.userInputHandleAreaRight}>
          <Button onClick={submit} disabled={running}>
            发送
          </Button>
        </div>
      </div>
    </div>
  );
}
