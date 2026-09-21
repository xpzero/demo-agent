import { useEffect, useRef, useState } from "react";
import {
  MAX_PDF_BYTES,
  uploadDocument,
  type UploadedDocument,
} from "@/adapter/documents";

export type DocumentUploadState =
  | { status: "idle" }
  | { status: "invalid"; file: File; message: string }
  | { status: "uploading"; file: File }
  | { status: "uploaded"; file: File; document: UploadedDocument }
  | { status: "failed"; file: File; message: string };

function validatePdf(file: File): string | null {
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    return "只支持 PDF 文件";
  }
  if (file.type !== "application/pdf") {
    return "文件类型必须是 PDF";
  }
  if (file.size === 0) {
    return "PDF 文件不能为空";
  }
  if (file.size > MAX_PDF_BYTES) {
    return "PDF 不能超过 10 MiB";
  }
  return null;
}

export function useDocumentUpload() {
  const [state, setState] = useState<DocumentUploadState>({ status: "idle" });
  const controllerRef = useRef<AbortController | null>(null);

  const reset = () => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setState({ status: "idle" });
  };

  const selectFile = async (file: File) => {
    controllerRef.current?.abort();
    const validationMessage = validatePdf(file);
    if (validationMessage) {
      controllerRef.current = null;
      setState({ status: "invalid", file, message: validationMessage });
      return;
    }

    const controller = new AbortController();
    controllerRef.current = controller;
    setState({ status: "uploading", file });
    try {
      const document = await uploadDocument(file, controller.signal);
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setState({ status: "uploaded", file, document });
      }
    } catch (error) {
      if (controllerRef.current !== controller) {
        return;
      }
      controllerRef.current = null;
      if (error instanceof DOMException && error.name === "AbortError") {
        setState({ status: "idle" });
        return;
      }
      setState({
        status: "failed",
        file,
        message: error instanceof Error ? error.message : "上传失败",
      });
    }
  };

  useEffect(() => () => controllerRef.current?.abort(), []);

  return { state, selectFile, reset };
}
