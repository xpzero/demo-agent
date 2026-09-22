import { API_BASE, responseError } from "./transport";

export const MAX_PDF_BYTES = 10 * 1024 * 1024;

export type UploadedFile = {
  file_id: string;
  filename: string;
  content_type: "application/pdf";
  size: number;
  status: "uploaded";
  created_at: number;
};

export async function uploadDocument(
  file: File,
  signal?: AbortSignal,
): Promise<UploadedFile> {
  const body = new FormData();
  body.append("file", file);

  const response = await fetch(`${API_BASE}/api/documents`, {
    method: "POST",
    body,
    signal,
  });
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as UploadedFile;
}
