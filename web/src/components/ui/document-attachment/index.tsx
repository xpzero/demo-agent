import { FileText, LoaderCircle, X } from "lucide-react";
import type { DocumentUploadState } from "@/hooks/useDocumentUpload";
import {
  Attachment,
  AttachmentAction,
  AttachmentActions,
  AttachmentContent,
  AttachmentDescription,
  AttachmentMedia,
  AttachmentTitle,
} from "@/components/shadcn/attachment";
import styles from "./index.module.scss";

type VisibleDocumentUploadState = Exclude<
  DocumentUploadState,
  { status: "idle" }
>;

interface DocumentAttachmentProps {
  state: VisibleDocumentUploadState;
  onRemove: () => void;
}

function formatBytes(bytes: number) {
  if (bytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(bytes / 1024))} KiB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function getDescription(state: VisibleDocumentUploadState) {
  if (state.status === "invalid" || state.status === "failed") {
    return state.message;
  }
  if (state.status === "uploading") {
    return `正在上传 · ${formatBytes(state.file.size)}`;
  }
  return `已上传，等待解析 · ${formatBytes(state.document.size)}`;
}

export default function DocumentAttachment({
  state,
  onRemove,
}: DocumentAttachmentProps) {
  const attachmentState =
    state.status === "uploading"
      ? "uploading"
      : state.status === "invalid" || state.status === "failed"
        ? "error"
        : "done";

  return (
    <div className={styles.documentAttachmentContainer}>
      <Attachment
        state={attachmentState}
        size="xs"
        className={styles.documentAttachment}
      >
        <AttachmentMedia>
          {state.status === "uploading" ? (
            <LoaderCircle className="animate-spin" />
          ) : (
            <FileText />
          )}
        </AttachmentMedia>
        <AttachmentContent>
          <AttachmentTitle>{state.file.name}</AttachmentTitle>
          <AttachmentDescription>{getDescription(state)}</AttachmentDescription>
        </AttachmentContent>
        <AttachmentActions>
          <AttachmentAction
            type="button"
            aria-label="从输入框移除附件（已上传文件仍保存在服务端）"
            onClick={onRemove}
          >
            <X />
          </AttachmentAction>
        </AttachmentActions>
      </Attachment>

      {state.status === "uploaded" && (
        <p className={styles.documentAttachmentNotice}>
          当前只完成上传，尚未解析；发送消息仍是普通聊天，不会读取此文档。
        </p>
      )}
    </div>
  );
}
