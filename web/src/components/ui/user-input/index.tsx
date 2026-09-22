import { useEffect, useRef } from "react";
import type { ChangeEvent, FormEvent, KeyboardEvent } from "react";
import { Paperclip } from "lucide-react";
import { useUserInputStore } from "@/stores/user";
import { useDocumentUpload } from "@/hooks/useDocumentUpload";
import DocumentAttachment from "@/components/ui/document-attachment";
import styles from "./index.module.scss";
import { Button } from "@/components/shadcn/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/shadcn/tooltip";

interface UserInputProps {
  /** 发送一条消息 */
  onSend: (text: string, refFileIds: string[]) => void;
  /** 是否有请求正在运行 */
  running: boolean;
}

export default function UserInput({ onSend, running }: UserInputProps) {
  const ref = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const text = useUserInputStore((state) => state.text);
  const setText = useUserInputStore((state) => state.setText);
  const clear = useUserInputStore((state) => state.clear);
  const documentUpload = useDocumentUpload();
  const { state: uploadState } = documentUpload;

  // 用户打字时，把 DOM 内容同步进 store
  const handleInput = (event: FormEvent<HTMLDivElement>) => {
    setText(event.currentTarget.textContent ?? "");
  };

  // 空文本或运行中不发送；发送后清空输入框
  const submit = () => {
    if (!text.trim() || running) {
      return;
    }
    const refFileIds =
      uploadState.status === "uploaded" ? [uploadState.document.file_id] : [];
    onSend(text, refFileIds);
    clear();
    if (refFileIds.length > 0) {
      documentUpload.reset();
    }
  };

  // Enter 发送，Shift+Enter 换行；中文输入法选词确认的 Enter 不触发发送
  const onEnter = (event: KeyboardEvent<HTMLDivElement>) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      submit();
    }
  };

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // 清空 input 后，同一文件也可以再次选择并触发 change。
    event.target.value = "";
    if (file) {
      void documentUpload.selectFile(file);
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

      {uploadState.status !== "idle" && (
        <DocumentAttachment
          state={uploadState}
          onRemove={documentUpload.reset}
        />
      )}

      <div className={styles.userInputHandleArea}>
        <div className={styles.userInputHandleAreaLeft}>
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf,application/pdf"
            hidden
            onChange={onFileChange}
          />
          <Tooltip>
            <TooltipTrigger
              render={
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="上传 PDF"
                  disabled={
                    uploadState.status === "uploading" ||
                    uploadState.status === "cancelling"
                  }
                  onClick={() => fileInputRef.current?.click()}
                >
                  <Paperclip />
                </Button>
              }
            />
            <TooltipContent>仅支持单个 PDF，最大 10 MiB</TooltipContent>
          </Tooltip>
        </div>
        <div className={styles.userInputHandleAreaRight}>
          <Button onClick={submit} disabled={running}>
            发送
          </Button>
        </div>
      </div>
    </div>
  );
}
