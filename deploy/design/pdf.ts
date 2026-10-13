import sharp from 'sharp';
import { PDFDocument } from 'pdf-lib';

export async function drawingPdf(bytes: Buffer, maxImagePixels: number): Promise<Buffer> {
  const png = await sharp(bytes, { density: 150, limitInputPixels: maxImagePixels })
    .png()
    .toBuffer();
  const pdf = await PDFDocument.create();
  const image = await pdf.embedPng(png);
  const page = pdf.addPage(image.width > image.height ? [841.89, 595.28] : [595.28, 841.89]);
  const scale = Math.min(
    (page.getWidth() - 48) / image.width,
    (page.getHeight() - 48) / image.height,
  );
  const width = image.width * scale;
  const height = image.height * scale;
  page.drawImage(image, {
    x: (page.getWidth() - width) / 2,
    y: (page.getHeight() - height) / 2,
    width,
    height,
  });
  return Buffer.from(await pdf.save());
}
