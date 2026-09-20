import { useEffect, useRef } from "react";
import type { ChangeEvent, FormEvent, KeyboardEvent } from "react";
import { FileText, LoaderCircle, Paperclip, X } from "lucide-react";
import { useUserInputStore } from "@/stores/user";
import { useDocumentUpload } from "@/hooks/useDocumentUpload";
import styles from "./index.module.scss";
import { Button } from "@/components/shadcn/button";
import {
  Attachment,
  AttachmentAction,
  AttachmentActions,
  AttachmentContent,
  AttachmentDescription,
  AttachmentMedia,
  AttachmentTitle,
} from "@/components/shadcn/attachment";

interface UserInputProps {
  /** 发送一条消息 */
  onSend: (text: string) => void;
  /** 是否有请求正在运行 */
  running: boolean;
}

function formatBytes(bytes: number) {
  if (bytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(bytes / 1024))} KiB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
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

  const attachmentState =
    uploadState.status === "uploading"
      ? "uploading"
      : uploadState.status === "invalid" || uploadState.status === "failed"
        ? "error"
        : "done";

  const attachmentDescription = (() => {
    if (uploadState.status === "invalid" || uploadState.status === "failed") {
      return uploadState.message;
    }
    if (uploadState.status === "uploading") {
      return `正在上传 · ${formatBytes(uploadState.file.size)}`;
    }
    if (uploadState.status === "uploaded") {
      return `已上传，等待解析 · ${formatBytes(uploadState.document.size)}`;
    }
    return "";
  })();

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
        <Attachment
          state={attachmentState}
          size="xs"
          className={styles.userInputAttachment}
        >
          <AttachmentMedia>
            {uploadState.status === "uploading" ? (
              <LoaderCircle className="animate-spin" />
            ) : (
              <FileText />
            )}
          </AttachmentMedia>
          <AttachmentContent>
            <AttachmentTitle>{uploadState.file.name}</AttachmentTitle>
            <AttachmentDescription>{attachmentDescription}</AttachmentDescription>
          </AttachmentContent>
          <AttachmentActions>
            <AttachmentAction
              type="button"
              aria-label="从输入框移除附件（已上传文件仍保存在服务端）"
              onClick={documentUpload.reset}
            >
              <X />
            </AttachmentAction>
          </AttachmentActions>
        </Attachment>
      )}
      {uploadState.status === "uploaded" && (
        <p className={styles.userInputNotice}>
          当前只完成上传，尚未解析；发送消息仍是普通聊天，不会读取此文档。
        </p>
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
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="上传 PDF"
            title="上传 PDF"
            disabled={uploadState.status === "uploading"}
            onClick={() => fileInputRef.current?.click()}
          >
            <Paperclip />
          </Button>
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
