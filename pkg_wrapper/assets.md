# Package art

The console will not show a tile with missing or wrongly sized art, and it fails
silently — so get these exact before building.

| File | Size | Format | Where it appears |
|---|---|---|---|
| `icon0.png` | 512 x 512 | PNG, no alpha | The home-screen tile |
| `pic1.png` | 1920 x 1080 | PNG | Background behind the tile when focused |
| `icon0_<lang>.png` | 512 x 512 | PNG | Optional per-language icon |
| `pic0.png` | 1920 x 1080 | PNG | Optional secondary background |

Both required files go in `sce_sys/` inside the package.

## Design notes for a tile that reads from the sofa

The hub's own palette is the obvious starting point: ground `#0b0e14`, surface
`#161b22`, accent `#0070d1`. At 512px rendered down to a tile roughly 170px wide on a
1080p TV, detail disappears — one strong shape and no text smaller than about 90px.

Avoid putting the product name in the icon: the console prints `titleName` under the
tile already, so text in the art just competes with it.

`static/img/favicon.svg` in the project root is the hub's own mark and scales cleanly
to 512px if you want to start from it:

```bash
# with ImageMagick, or any SVG rasteriser
magick -background none -density 600 ../static/img/favicon.svg -resize 512x512 icon0.png
```

For `pic1.png`, a dark vertical gradient from `#0b0e14` to `#070910` with a soft blue
radial glow in the upper left matches the dashboard's ambient treatment, so the
transition from tile to app does not jar.
