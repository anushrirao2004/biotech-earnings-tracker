"""Draw the digest table as a PNG with PIL. Two-tier header exactly like the
user's spreadsheet: a merged title over the two per-PR columns and one over
the three total columns."""
import os

from PIL import Image, ImageDraw, ImageFont


def _font(size, bold=False):
    cands = []
    if os.name == "nt":
        cands += [r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf"]
    try:
        import matplotlib
        d = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
        cands += [os.path.join(d, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")]
    except Exception:  # noqa: BLE001
        pass
    for c in cands:
        if os.path.exists(c):
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def render_table(rows, title, path, scale=2):
    """rows: list of 6-cell lists. Writes a PNG at `path`."""
    f = _font(13 * scale)
    fb = _font(13 * scale, bold=True)
    ft = _font(12 * scale)
    pad = 8 * scale
    row_h = 30 * scale
    # column widths: date, PR morning (tickers), PR evening, total x3
    widths = [130, 250, 170, 90, 90, 90]
    widths = [w * scale for w in widths]
    # widen the two per-PR columns to fit the longest ticker list
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    for ci in (1, 2):
        longest = max((tmp.textlength(r[ci], font=f) for r in rows), default=0) + 2 * pad
        widths[ci] = int(max(widths[ci], longest))
    W = sum(widths) + 2 * pad
    body = rows or [["no releases tracked yet", "", "", "", "", ""]]
    H = pad + 26 * scale + 2 * row_h + row_h * len(body) + pad
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.text((pad, pad), title, fill="black", font=ft)
    top = pad + 26 * scale
    x0 = pad
    xs = [x0]
    for w in widths:
        xs.append(xs[-1] + w)
    grey = "#f2f2f2"
    line = "#555555"

    def cell(x1, y1, x2, y2, text, font, fill=None):
        if fill:
            d.rectangle([x1, y1, x2, y2], fill=fill)
        d.rectangle([x1, y1, x2, y2], outline=line, width=scale)
        tw = d.textlength(text, font=font)
        d.text(((x1 + x2 - tw) / 2, y1 + (y2 - y1 - font.size) / 2 - scale), text, fill="black", font=font)

    # header tier 1
    cell(xs[0], top, xs[1], top + 2 * row_h, "India Update Date", fb, grey)
    cell(xs[1], top, xs[3], top + row_h, "# of biotech models to be updated as per PR", fb, grey)
    cell(xs[3], top, xs[6], top + row_h, "Total (Including expected results)", fb, grey)
    # header tier 2
    for i, name in enumerate(["Morning", "Evening", "Morning", "Evening", "Overall"], start=1):
        cell(xs[i], top + row_h, xs[i + 1], top + 2 * row_h, name, fb, grey)
    # body
    y = top + 2 * row_h
    if not rows:
        # placeholder spans the whole row instead of overflowing the date cell
        cell(xs[0], y, xs[6], y + row_h, body[0][0], f)
        y += row_h
        body = []
    for r in body:
        for i, text in enumerate(r):
            cell(xs[i], y, xs[i + 1], y + row_h, text, f)
        y += row_h
    img.save(path)
    return path
