/** An SVG drawn as a PNG in the page, ``pixels`` on a side: how a task pane turns a vector icon into a picture. */
export async function rasterizeSvg(svg: string, pixels: number): Promise<Uint8Array> {
  const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
  try {
    const image = new Image(pixels, pixels);
    image.src = url;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = pixels;
    canvas.height = pixels;
    canvas.getContext('2d')!.drawImage(image, 0, 0, pixels, pixels);
    const png = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/png'));
    if (!png) throw new Error('The icon could not be drawn as a PNG');
    return new Uint8Array(await png.arrayBuffer());
  } finally {
    URL.revokeObjectURL(url);
  }
}
