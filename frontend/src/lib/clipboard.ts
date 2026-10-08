/** Clipboard helpers for the media detail actions (PRD §26). */

async function requireClipboard(): Promise<Clipboard> {
  if (!("clipboard" in navigator) || !navigator.clipboard) {
    throw new Error("Clipboard access is not available in this browser");
  }
  return navigator.clipboard;
}

/** Copy plain text (used for the original file URL). */
export async function copyText(text: string): Promise<void> {
  const clipboard = await requireClipboard();
  await clipboard.writeText(text);
}

/** Absolute same-origin URL for a stored file. */
export function absoluteFileUrl(path: string): string {
  return new URL(path, window.location.href).href;
}

/**
 * Copy the image itself as PNG. The blob is re-encoded first because browsers
 * only accept `image/png` (or `text/html`) in `ClipboardItem`.
 */
export async function copyImageAsPng(url: string): Promise<void> {
  const clipboard = await requireClipboard();
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Could not fetch the image (HTTP ${response.status})`);
  const blob = await response.blob();

  if (blob.type === "image/png") {
    await clipboard.write([new ClipboardItem({ "image/png": blob })]);
    return;
  }

  const bitmap = await createImageBitmap(blob);
  const canvas = document.createElement("canvas");
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Canvas is unavailable — cannot re-encode the image");
  context.drawImage(bitmap, 0, 0);
  bitmap.close();

  const png = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (encoded) => (encoded ? resolve(encoded) : reject(new Error("PNG encoding failed"))),
      "image/png",
    );
  });
  await clipboard.write([new ClipboardItem({ "image/png": png })]);
}
